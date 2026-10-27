"""Find the Common ID work a Gmail message belongs to, and remember the thread once updated.

Matching is read-only and works from the shared Common ID store: a thread already linked to a
work is a sure match; otherwise words of the subject are compared with work and source titles.
Keyword matches are candidates only. The agent confirms them against the real sources first.
"""
from __future__ import annotations

import json
import re
import sqlite3
try:
    from .gmail_watch import normalized_account, normalized_rfc_id, WatchError
except ImportError:
    from gmail_watch import normalized_account, normalized_rfc_id, WatchError
from pathlib import Path
from typing import Any

THREAD = re.compile(r"[0-9a-fA-F]{1,32}")
PREFIX = re.compile(r"^\s*(?:(?:re|fw|fwd|aw|wg|답장|회신|전달|답변)\s*(?:\[\d+\])?\s*[:：]\s*)+", re.I)
WORD = re.compile(r"[0-9A-Za-z가-힣]+")
PARTICLES = ("으로", "에서", "에게", "까지", "부터", "에", "의", "을", "를", "은", "는", "이", "가", "와", "과", "로", "께", "도")
STOP = frozenset("""re fw fwd the and for from with 관련 안내 요청 문의 회신 전달 답변 답장 건 드립니다 드림 부탁 부탁드립니다
확인 공유 메일 자료 첨부 송부 제출 검토 회의 일정 변경 업데이트 참고 감사합니다 안녕하세요 및 등 건의""".split())
MAX_CANDIDATES = 5

SCHEMA = {
    'name': 'mail_work',
    'description': (
        '업무비서 회사 메일 → 연결 업무 찾기·기록. action=match는 읽기 전용이다. thread_id·message_id·subject(·keywords)로 '
        'Mail 앱은 rfc_ids(자기 Message-ID와 References)로 찾고 link는 thread_id 대신 account+rfc_id를 쓴다. 같은 메일 타래가 이미 연결된 업무(match=same_thread)와 제목 낱말이 겹치는 업무 후보(match=keywords)를 돌려준다. '
        '후보는 확정이 아니다. 실제 원본(메모·일정·미리알림·Notion)을 조회해 맞는지 확인한 뒤 고친다. 연결 업무가 없으면 '
        'search_terms로 apple_notes_direct_lookup(search_body=true)·calendar_direct_lookup·reminders_direct_lookup·notion search를 찾는다. '
        'action=link는 소유자 대화에서 메일 내용을 실제로 반영한 뒤 그 타래를 업무(common_id)에 기록해 다음 메일부터 바로 찾게 한다. '
        '예약·배경에서는 match만 가능하다.'),
    'parameters': {
        'type': 'object',
        'properties': {
            'action': {'type': 'string', 'enum': ['match', 'link']},
            'thread_id': {'type': 'string', 'description': 'gmail_direct가 돌려준 Gmail threadId'},
            'message_id': {'type': 'string', 'description': 'gmail_direct가 돌려준 Gmail message id'},
            'rfc_id': {'type': 'string', 'description': 'Mail 앱 메일의 Message-ID(link용)'},
            'rfc_ids': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 51,
                        'description': 'match용: 자기 rfc_id와 In-Reply-To·References의 Message-ID'},
            'subject': {'type': 'string'},
            'keywords': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 10,
                         'description': '본문·첨부에서 고른 업무 이름·상대 회사·과제명 등 추가 낱말'},
            'include_completed': {'type': 'boolean', 'default': False},
            'common_id': {'type': 'string', 'description': 'link 대상 업무'},
            'account': {'type': 'string', 'description': '메일 계정(기록용)'},
        },
        'required': ['action'],
        'additionalProperties': False,
    },
}


class MailWorkError(ValueError):
    pass


def clean_subject(subject: Any) -> str:
    text = PREFIX.sub('', str(subject or '')).strip()
    return re.sub(r"\s+", " ", text)


def _stem(word: str) -> str:
    for particle in PARTICLES:
        if len(word) > len(particle) + 1 and word.endswith(particle):
            return word[: -len(particle)]
    return word


def terms(*texts: Any) -> list[str]:
    found = []
    for text in texts:
        for raw in WORD.findall(clean_subject(text) if isinstance(text, str) else ''):
            word = _stem(raw.casefold())
            if len(word) < 2 or word in STOP or word.isdigit() and len(word) < 4:
                continue
            if word not in found:
                found.append(word)
    return found


def _connect(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise MailWorkError('COMMON_WORK_STORE_MISSING')
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def _titles(metadata: dict) -> list[str]:
    identity = metadata.get('identity') if isinstance(metadata.get('identity'), dict) else {}
    values = [identity.get('title'), metadata.get('title'), metadata.get('name')]
    url = str(metadata.get('url') or '')
    if 'notion' in url:
        # Notion page URLs carry the page title before the id: .../BIXPO-2026-3f85...
        slug = url.rstrip('/').rsplit('/', 1)[-1]
        values.append(re.sub(r"-?[0-9a-f]{32}$", '', slug).replace('-', ' '))
    return [str(v) for v in values if v]


def _load(conn: sqlite3.Connection, include_completed: bool) -> dict[str, dict]:
    where = '' if include_completed else "WHERE status = 'active'"
    works = {row['common_id']: {'common_id': row['common_id'], 'title': row['title'], 'status': row['status'],
                                'sources': [], 'notion_pages': [], 'mail_threads': [], 'mail_messages': [], 'haystack': [row['title']]}
             for row in conn.execute(f"SELECT common_id, title, status FROM common_work {where}")}
    for row in conn.execute('SELECT common_id, domain, source_id, metadata_json FROM source_links'):
        if (work := works.get(row['common_id'])) is None:
            continue
        metadata = json.loads(row['metadata_json'] or '{}')
        names = _titles(metadata)
        work['sources'].append({'domain': row['domain'], 'source_id': row['source_id'],
                                **({'title': names[0]} if names else {})})
        work['haystack'] += names
    for row in conn.execute("SELECT common_id, ref_type, external_id, metadata_json FROM knowledge_refs "
                            "WHERE unlinked_at IS NULL"):
        if (work := works.get(row['common_id'])) is None:
            continue
        metadata = json.loads(row['metadata_json'] or '{}')
        if row['ref_type'] == 'notion_page':
            work['notion_pages'].append({'page_id': row['external_id'],
                                         **({'url': metadata['url']} if metadata.get('url') else {})})
            work['haystack'] += _titles(metadata)
        elif row['ref_type'] == 'mail_message':
            if rfc_id := normalized_rfc_id(row['external_id']):
                work['mail_messages'].append(rfc_id)
            if metadata.get('subject'):
                work['haystack'].append(clean_subject(metadata['subject']))
        elif row['ref_type'] in ('gmail_thread', 'gmail_message'):
            work['mail_threads'].append(row['external_id'])
            if metadata.get('subject'):
                work['haystack'].append(clean_subject(metadata['subject']))
    return works


def _score(words: list[str], haystack: list[str]) -> list[str]:
    hay = [_stem(w.casefold()) for text in haystack for w in WORD.findall(text)]
    joined = ' '.join(text.casefold() for text in haystack)
    hits = []
    for word in words:
        if word in joined or any(len(h) >= 2 and (h in word or word in h) and min(len(h), len(word)) >= 2
                                 and (len(h) >= 3 or h == word) for h in hay):
            hits.append(word)
    return hits


def match(path: Path, args: dict) -> dict:
    thread_id = str(args.get('thread_id') or '').strip()
    message_id = str(args.get('message_id') or '').strip()
    ids = [value for value in (thread_id, message_id) if value]
    if any(not THREAD.fullmatch(value) for value in ids):
        raise MailWorkError('GMAIL_ID_INVALID')
    keywords = [k for k in args.get('keywords') or [] if isinstance(k, str)][:10]
    words = terms(args.get('subject') or '', *keywords)
    raw_rfc = args.get('rfc_ids') or []
    if not isinstance(raw_rfc, list) or len(raw_rfc) > 51 or any(not normalized_rfc_id(v) for v in raw_rfc):
        raise MailWorkError('MAIL_RFC_IDS_INVALID')
    rfc_ids = list(dict.fromkeys(normalized_rfc_id(v) for v in raw_rfc))
    if not ids and not rfc_ids and not words:
        raise MailWorkError('thread_id, message_id, rfc_ids, subject or keywords required')
    conn = _connect(path)
    try:
        works = _load(conn, args.get('include_completed') is True)
    finally:
        conn.close()
    found = []
    for work in works.values():
        same = ([value for value in ids if value in work['mail_threads']]
                + [value for value in rfc_ids if value in work['mail_messages']])
        hits = _score(words, work['haystack'])
        # Years and numbers alone (2026) connect unrelated work; they only add weight.
        # One shared word is not enough either: a product name (ECAS) spans several works,
        # e.g. a past consultation and the ongoing plan revision (2026-10-26).
        named = [h for h in hits if not h.isdigit()]
        strong = len(set(named)) >= 2
        if not same and not strong:
            continue
        entry = {key: work[key] for key in ('common_id', 'title', 'status', 'sources', 'notion_pages')}
        entry['match'] = 'same_thread' if same else 'keywords'
        if hits:
            entry['matched_terms'] = hits
        entry['_rank'] = (bool(same), len(named), len(hits), sum(map(len, hits)))
        found.append(entry)
    found.sort(key=lambda e: e['_rank'], reverse=True)
    for entry in found:
        entry.pop('_rank')
    return {'ok': True, 'subject': clean_subject(args.get('subject') or ''), 'search_terms': words[:6],
            'candidates': found[:MAX_CANDIDATES], 'more': max(0, len(found) - MAX_CANDIDATES),
            'note': ('same_thread는 이미 이 메일 타래로 연결된 업무다. keywords는 후보이므로 원본을 읽어 확인한다. '
                     '후보가 없으면 search_terms로 메모·일정·미리알림·Notion을 직접 찾는다.')}


def compact(result: dict) -> list[dict]:
    """Short form for the scheduled mail report."""
    return [{'common_id': c['common_id'], 'title': c['title'], 'match': c['match'],
             'sources': sorted({s['domain'] for s in c['sources']} | ({'notion'} if c['notion_pages'] else set())),
             **({'matched_terms': c['matched_terms']} if c['match'] == 'keywords' and c.get('matched_terms') else {})}
            for c in result.get('candidates', [])]


def link(store, args: dict) -> dict:
    common_id = str(args.get('common_id') or '').strip()
    thread_id = str(args.get('thread_id') or '').strip()
    message_id = str(args.get('message_id') or '').strip()
    rfc_id = normalized_rfc_id(args.get('rfc_id'))
    try:
        account = normalized_account(args.get('account') or '')
    except WatchError as exc:
        raise MailWorkError('MAIL_ACCOUNT_INVALID') from exc
    if thread_id:
        if not THREAD.fullmatch(thread_id) or (message_id and not THREAD.fullmatch(message_id)):
            raise MailWorkError('link needs a valid Gmail thread_id')
        ref_type, external_id = 'gmail_thread', thread_id
        receipt = 'gmail:' + (message_id or thread_id)
        identity = {'thread_id': thread_id}
    elif account and rfc_id:
        ref_type, external_id = 'mail_message', rfc_id
        receipt = 'mail_app:' + rfc_id
        identity = {'account': account, 'rfc_id': rfc_id}
    else:
        raise MailWorkError('link needs common_id and a Gmail thread_id or account + rfc_id')
    if not common_id:
        raise MailWorkError('link needs common_id')
    work = store.get(common_id)
    if external_id in {r['external_id'] for r in work.get('knowledge_refs', []) if r['ref_type'] == ref_type}:
        return {'ok': True, 'already_linked': True, 'common_id': common_id, **identity}
    metadata = {key: value for key, value in (('subject', clean_subject(args.get('subject') or '')),
                                               ('account', account), ('message_id', message_id)) if value}
    store.link_knowledge_ref(common_id, ref_type=ref_type, external_id=external_id,
                             relation_type='discussed_in', link_reason='verified_workflow_evidence',
                             receipt_id=receipt, metadata=metadata)
    return {'ok': True, 'linked': True, 'common_id': common_id, 'title': work['title'], **identity}


def role_problem(td, action: str) -> str | None:
    if td.personal_roles.profile(td) != 'work-assistant':
        return 'MAIL_WORK_PROFILE_REFUSED: 업무비서 전용 도구다.'
    if action == 'match':
        return None
    env = lambda key: td.get_session_env(key, '') or td.os.environ.get(key, '')
    if any(env(flag) for flag in ('HERMES_CRON_SESSION', 'HERMES_SINGLE_QUERY_SESSION', 'HERMES_KANBAN_TASK')):
        return 'MAIL_WORK_BACKGROUND_REFUSED: 예약·배경에서는 match만 한다.'
    if (env('HERMES_SESSION_PLATFORM') not in {'slack', 'photon'} or not env('HERMES_SESSION_USER_ID')
            or not env('HERMES_SESSION_CHAT_ID')):
        return 'MAIL_WORK_OWNER_CHAT_REQUIRED: link는 인증된 소유자 대화에서만 한다.'
    return None
