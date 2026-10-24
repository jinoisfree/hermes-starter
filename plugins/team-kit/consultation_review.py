"""Read existing consultation ledgers without combining customer identities or sending."""
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import re
import sqlite3
from zoneinfo import ZoneInfo

KST = ZoneInfo('Asia/Seoul')
SCHEMAS = {
    'consultation_report': {'name': 'consultation_report', 'description': 'Read-only consultation statistics from existing Instagram decision logs and Naver TalkTalk events. Explicit KST date range; channels and units remain separate. No raw customer identities or messages in the report.',
        'parameters': {'type': 'object', 'properties': {'start': {'type': 'string', 'description': 'YYYY-MM-DD inclusive'},
            'end': {'type': 'string', 'description': 'YYYY-MM-DD exclusive; at most 90 days'}}, 'required': ['start', 'end'], 'additionalProperties': False}},
    'store_guidance_check': {'name': 'store_guidance_check', 'description': 'Call instagram_dm_guide first in this turn, then compare matching store guide, monthly times and recent feed excerpts. Reports exact numeric differences as candidates with source paths; never decides which notice supersedes another.',
        'parameters': {'type': 'object', 'properties': {'query': {'type': 'string', 'description': 'One explicit topic such as 영업, 예약, 가격, 주차. Literal search; choose a short shared term.'}}, 'required': ['query'], 'additionalProperties': False}},
}
TOPICS = {'예약': ('예약', '대관', '인원'), '시간': ('시간', '영업', '마감', '휴무'), '가격': ('가격', '금액', '요금', '비용', '결제'),
          '변경취소': ('변경', '취소', '환불'), '시설': ('주차', '수영', '반려', '시설'), '첨부': ('사진', '첨부', '이미지', '스토리')}


def topics(text):
    return [k for k, words in TOPICS.items() if any(w in text for w in words)] or ['기타']


def reason_categories(text):
    categories = {'첨부직접확인': r'사진|첨부|스토리|공유', '예약결제현장확인': r'예약|결제|케이크|현장|확인후',
                  '자료근거부족': r'자료|근거|숫자|링크|공지|월별|상황 기록',
                  '승인답변대기': r'승인|기다리|질문', '횟수한도': r'횟수|회까지|한도|제한',
                  '답장시간경과': r'24시간|30분|10분|오래|지났|넘었',
                  '조회전송실패': r'실패|오류|확인하지 못|unknown|sending',
                  '문의답변변경': r'inbound_changed|reply_changed|새 문의|새로운 문의'}
    return [k for k, pattern in categories.items() if re.search(pattern, text)] or ['기타확인']


def safe(path, root):
    path = Path(path)
    if path.absolute() != path.resolve() or root.resolve() not in path.resolve().parents:
        raise ValueError('consultation_store_symlink_or_outside_profile')
    if not path.is_file(): raise ValueError('consultation_store_missing')
    return path


def period(args):
    values = []
    for k in ('start', 'end'):
        v = args.get(k)
        if not isinstance(v, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', v): raise ValueError('explicit_dates_required')
        values.append(datetime.strptime(v, '%Y-%m-%d').replace(tzinfo=KST))
    if not 0 < (values[1] - values[0]).days <= 90: raise ValueError('date_range_must_be_1_to_90_days')
    return [v.timestamp() for v in values]


def instagram(home, start, end):
    path = safe(home / 'state/private-service-24-auto-decisions.jsonl', home)
    if path.stat().st_size > 16 * 1024 * 1024: raise ValueError('instagram_ledger_over_16MB')
    decisions, reasons, basis, unique = Counter(), Counter(), Counter(), set()
    invalid = 0; first = None; last = None
    with path.open() as stream:
        for line in stream:
            try:
                row = json.loads(line)
                at = row['at']
                if type(at) not in (int, float): raise ValueError()
                first = at if first is None else min(first, at)
                last = at if last is None else max(last, at)
                if not start <= at < end: continue
                decision = row.get('decision')
                if decision not in {'eligible', 'approval_required', 'approved', 'discarded', 'sent', 'failed', 'unknown'}:
                    decision = 'other'
                decisions[decision] += 1
                if row.get('conversation_id'): unique.add(str(row['conversation_id']))
                for reason in row.get('reasons', []):
                    # Aggregate fixed categories, never return identifying free text.
                    for category in reason_categories(str(reason)): reasons[category] += 1
                for item in row.get('basis', []):
                    for category in topics(str(item)): basis[category] += 1
            except (ValueError, KeyError, TypeError): invalid += 1
    return {'source': path.name, 'unit': 'decision_log_events_not_inbound_messages', 'events': sum(decisions.values()),
            'decisions': dict(decisions), 'owner_check_reason_topics': dict(reasons), 'basis_topics': dict(basis),
            'unique_conversations': len(unique), 'invalid_lines': invalid, 'coverage_complete': False,
            'read_complete': invalid == 0, 'earliest_record_at': first, 'latest_record_at': last,
            'note': 'One inquiry can generate several decisions. Eligible/approved does not prove sent or resolved; counts cover retained decision records only. Topics are literal keyword categories, multi-label.'}


def naver(home, start, end):
    path = safe(home / 'state/naver-talk.sqlite3', home)
    statuses, inquiry, reasons = Counter(), Counter(), Counter()
    auto_sent = manual_sent = total = 0
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        records = db.execute('SELECT status,auto,body,reason FROM events WHERE role=? AND at>=? AND at<? ORDER BY id LIMIT 20001', ('user', start, end)).fetchall()
        for status, auto, body, reason in records[:20000]:
            total += 1
            statuses[status if status in {'queued', 'new', 'pending', 'processing', 'owner', 'sent', 'failed', 'unknown', 'sending', 'skipped', 'superseded', 'cancelled'} else 'other'] += 1
            if status == 'sent':
                if auto: auto_sent += 1
                else: manual_sent += 1
            for topic in topics(body or ''): inquiry[topic] += 1
            if status in {'owner', 'failed', 'unknown'}:
                for topic in reason_categories(reason or ''): reasons[topic] += 1
    return {'source': path.name, 'unit': 'inbound_user_events', 'events': total, 'statuses': dict(statuses),
            'auto_sent': auto_sent, 'approved_sent': manual_sent, 'inquiry_topics': dict(inquiry),
            'owner_check_reason_topics': dict(reasons), 'read_complete': len(records) <= 20000, 'coverage_complete': False,
            'note': 'Counts use received time and current recorded status. Sent means API acceptance, not read/resolved. Retention completeness is unknown; keyword topics are multi-label.'}


def report(home, args):
    start, end = period(args)
    result = {'ok': True, 'start': args['start'], 'end_exclusive': args['end'], 'timezone': 'Asia/Seoul',
              'checked_at_kst': datetime.now(KST).isoformat(), 'channels': {}, 'cross_channel_customers_merged': False}
    for channel, reader in [('instagram', instagram), ('naver_talk', naver)]:
        try: result['channels'][channel] = reader(home, start, end)
        except Exception as exc: result['channels'][channel] = {'ok': False, 'error': str(exc) if type(exc) is ValueError else type(exc).__name__, 'coverage_complete': False}
    result['partial'] = any(x.get('ok') is False or x.get('read_complete') is False for x in result['channels'].values())
    if all(x.get('ok') is False for x in result['channels'].values()): result['ok'] = False
    return result


def guidance(args, evidence):
    query = args.get('query')
    if not isinstance(query, str) or not query.strip() or len(query) > 40: raise ValueError('short_literal_topic_required')
    receipts = [(k, v) for k, v in evidence.items() if v['tool'] == 'instagram_dm_guide']
    if not receipts: raise ValueError('read_instagram_dm_guide_in_this_turn_first')
    identifier, receipt = receipts[-1]
    data = receipt['data']
    blocks = [('store_guide', str(data.get('store_guide') or '')), ('monthly_times', str(data.get('monthly_times') or '')),
              ('times_by_month', json.dumps(data.get('times_by_month', {}), ensure_ascii=False, indent=2))]
    notices = data.get('latest_feed_notices', {})
    if not isinstance(notices, dict): raise ValueError('invalid_guide_notices')
    blocks.append(('profile_text', str(notices.get('profile_text') or '')))
    for i, post in enumerate(notices.get('recent_posts', [])):
        blocks.append((f'feed:{i} {post.get("posted", "")} {post.get("link", "")}', str(post.get('caption') or '')))
    matches = []
    for source, body in blocks:
        lines = body.splitlines()
        for i, line in enumerate(lines):
            if query.casefold() in line.casefold():
                text = '\n'.join(lines[max(0, i-1):i+2])
                matches.append({'source': source, 'matched_line': i+1, 'excerpt': text[:1200], 'truncated': len(text) > 1200,
                                'numeric_tokens': sorted(set(re.findall(r'\d+(?:[.:/-]\d+)*', text)))})
    numeric = {tuple(m['numeric_tokens']) for m in matches if m['numeric_tokens']}
    return {'ok': True, 'receipt_id': identifier, 'query': query, 'checked_at': receipt['checked_at'], 'matches': matches[:40],
            'match_count': len(matches), 'details_truncated': len(matches) > 40 or any(m['truncated'] for m in matches),
            'numeric_difference_candidate': len(numeric) > 1, 'feed_read_error': bool(notices.get('error')),
            'time_warnings': data.get('time_warnings', []), 'coverage_complete': False,
            'note': 'Literal topic matches in the currently returned guide and recent posts only. Numeric differences are candidates, not proven contradictions. Check date, room and applicability. No rules or notices were changed.'}


def handle(name, args, *, home, evidence=None, **_):
    return report(home, args) if name == 'consultation_report' else guidance(args, evidence)
