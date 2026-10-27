"""Narrow, owner-authorized Notion -> Reminders completion watcher (2026-10-16).

No conversation guard is relaxed. Only attachment-free Notes can be appended;
attachment-bearing notes are rebuilt with their exported originals.
"""
from __future__ import annotations

import hashlib
import time
import difflib
import fcntl
import importlib.util
import json
import logging
from logging.handlers import RotatingFileHandler
import traceback
import os
from pathlib import Path
import re
import subprocess
import sys
import unicodedata
from datetime import datetime, timedelta, timezone

from common_work_links import CommonWorkStore
import deadline_time
import deadline_link_sync
import title_link_sync

DATABASE = ''
DUE = '다음 마감'
SEOUL = None  # set lazily (zoneinfo import is slow in tests)


# Only these fixed codes may leave the exception boundary; never log exception text.
WORK_ERROR_REASONS = {
    'single_original_per_domain_required': '같은 종류의 원본이 여러 개 연결돼 있어요',
    'single_nonrecurring_calendar_original_required': '일정이 없거나 반복 일정이라 처리 대상이 아니에요',
    'PERSONAL_SCOPE_UNVERIFIED': '회사 범위인지 확인할 수 없어요',
    'personal_scope_denied': '개인 범위는 개인비서 담당이에요',
    'company_notion_database_required': '회사 업무 노션 데이터베이스가 아니에요',
    'unsupported_work_source': '지원하지 않는 원본 종류예요',
    'linked_task_sources_conflict': '연결된 원본의 할 일 내용이 서로 달라요',
    'ambiguous_task_deadline': '할 일의 마감 날짜가 불명확해 소유자 확인이 필요해요',
    'note_structure_preserved': '구역이 나뉜 메모라 할 일 완료 표시 말고는 다른 원본의 변경을 옮기지 않았어요',
    'reminder_read_failed': '미리알림을 읽지 못했어요',
    'title_write_pending': '제목 반영 확인이 필요해 멈췄어요. 자동 재실행하지 않아요',
    'unknown_error': '분류되지 않은 오류가 반복됐어요. 로컬 로그 확인이 필요해요',
}
NON_TARGET_REASONS = frozenset(WORK_ERROR_REASONS) - {
    'linked_task_sources_conflict', 'ambiguous_task_deadline', 'reminder_read_failed', 'unknown_error', 'title_write_pending'}
TRANSIENT_REASONS = {'reminder_read_failed', 'unknown_error'}


CONTENT_WARNINGS = {  # work_sync.Engine warning lines -> fixed reason codes for 진단담당
    '본문·할 일 반영 확인이 필요해 멈췄어요': 'work_content_write_uncertain',
    '앞선 본문·할 일 반영이 불명확해': 'work_content_pending_uncertain',
    '두 원본이 서로 다르게 바뀌었어요': 'work_content_conflict',
}


def content_warning_code(line):
    return next((code for text, code in CONTENT_WARNINGS.items() if text in line), None)


def request_warning_diagnosis(common_id, reason):
    """Bounded program call to 진단담당 (team-kit diagnosis_requests); None means tell the owner directly."""
    try:
        script = Path(__file__).resolve().parents[1] / 'team-kit/diagnosis_requests.py'
        values = {'kind': 'work_watch_warning', 'common_id': common_id, 'error_type': reason,
                  'failures': 1, 'since': time.time()}
        result = subprocess.run([sys.executable, str(script), '--program-request'], input=json.dumps(values),
                                text=True, capture_output=True, timeout=10)
        response = json.loads(result.stdout)
        return response.get('request_id') if response.get('ok') else None
    except Exception:
        return None


def diagnosis_status(root, request_id):
    try:
        row = json.loads((Path(root) / 'shared/diagnosis-requests' / f'{request_id}.json').read_text())
        return str(row.get('status') or 'missing')
    except (OSError, ValueError):
        return 'missing'


def work_error_reason(exc):
    message = str(exc)
    if message == "개인 범위는 개인비서 담당('ㅈ'으로 요청)":
        return 'personal_scope_denied'
    code = message.split(':', 1)[0].strip()
    return code if code in WORK_ERROR_REASONS else 'unknown_error'


def log_work_error(home, cid, reason, exc, *, frames=None, exception_type=None):
    # Frames only: format_exception would expose messages, source lines and chained data.
    frames = frames if frames is not None else [{'file': Path(frame.filename).name, 'line': frame.lineno, 'function': frame.name}
              for frame in traceback.extract_tb(exc.__traceback__)]
    record = json.dumps({'cid': cid, 'reason': reason, 'exception': exception_type or type(exc).__name__,
                         'traceback': frames}, ensure_ascii=False)
    try:
        path = home / 'logs/notion-completion-sync.log'
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=1024 * 1024, backupCount=3, encoding='utf-8')
        try:
            handler.setFormatter(logging.Formatter('%(asctime)s %(message)s'))
            handler.emit(logging.LogRecord('work-sync', logging.ERROR, '', 0, record, (), None))
        finally:
            handler.close()
    except OSError:
        # Logging must not stop other work or send sensitive data to cron stdout.
        pass


def seoul():
    global SEOUL
    if SEOUL is None:
        from zoneinfo import ZoneInfo
        SEOUL = ZoneInfo('Asia/Seoul')
    return SEOUL


def day_of(value):
    """YYYY-MM-DD in Seoul for an ISO date/datetime string, else None."""
    text = str(value or '').strip()
    if not text:
        return None
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', text):
        return text
    try:
        moment = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=seoul())
    return moment.astimezone(seoul()).strftime('%Y-%m-%d')


def mmdd(day):
    """'2026-10-15' -> '10/15' for owner-facing lines."""
    try:
        return f'{int(day[5:7])}/{int(day[8:10])}'
    except (TypeError, ValueError, IndexError):
        return str(day or '')


def todo_text(todo):
    """First line of a to-do; continuation lines are notes, not the task name."""
    text = ''.join(t.get('plain_text', t.get('text', {}).get('content', '')) for t in todo.get('rich_text', []))
    return text.strip().splitlines()[0].strip() if text.strip() else ''


def shift_day(value, old, new):
    """Move an ISO datetime by the whole-day difference between two YYYY-MM-DD days."""
    delta = datetime.fromisoformat(new) - datetime.fromisoformat(old)
    moment = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return (moment + delta).isoformat()
HOME = Path.home() / '.hermes/profiles/work-assistant'


def normalize(value):
    return ''.join(c for c in unicodedata.normalize('NFKC', value).casefold() if c.isalnum())


def page_id(value):
    value = str(value).replace('-', '').lower()
    if not re.fullmatch('[0-9a-f]{32}', value):
        raise ValueError('invalid page identifier')
    return value


def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def save(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    with temp.open('w') as stream:
        os.chmod(temp, 0o600)
        json.dump(state, stream, ensure_ascii=False, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(path)


def page_title(page):
    found = [(name, value) for name, value in page.get('properties', {}).items() if 'title' in value]
    if len(found) != 1:
        raise ValueError('single Notion title property required')
    name, value = found[0]
    return name, ''.join(v.get('plain_text', v.get('text', {}).get('content', '')) for v in value['title'])


class NotionReader:
    def __init__(self, home):
        self.home = home
        self.api = load_module('_completion_notion_api', home / 'plugins/team-kit/notion.py')

    def request(self, method, path, body=None):
        # This class exposes only the three read operations below.
        return self.api.request(self.home, method, path, body)

    def changed(self, since):
        cursor = None
        while True:
            body = {'page_size': 100, 'filter': {'timestamp': 'last_edited_time',
                    'last_edited_time': {'on_or_after': since}}}
            if cursor:
                body['start_cursor'] = cursor
            result = self.request('POST', f'databases/{DATABASE}/query', body)
            yield from result['results']
            if not result.get('has_more'):
                break
            cursor = result['next_cursor']

    def page(self, identifier):
        return self.request('GET', f'pages/{identifier}')

    # Linked-work sync writes (2026-10-16 owner decision). Callers verify the page's database.
    def check_todo(self, block_id):
        return self.request('PATCH', f'blocks/{block_id}', {'to_do': {'checked': True}})

    def set_due(self, identifier, day):
        return self.request('PATCH', f'pages/{identifier}', {'properties': {DUE: {'date': {'start': day}}}})

    def set_title(self, identifier, expected, title):
        before = self.page(identifier)
        if (page_id(before.get('parent', {}).get('database_id', '')) != DATABASE or before.get('in_trash')
                or before.get('archived') or page_title(before)[1] != expected):
            raise ValueError('Notion title identity/scope changed')
        name, _ = page_title(before)
        self.request('PATCH', f'pages/{identifier}', {'properties': {name: {'title': [{'text': {'content': title}}]}}})
        if page_title(self.page(identifier))[1] != title:
            raise RuntimeError('Notion title verification failed')

    def blocks(self, identifier):
        result, stack, seen = [], [identifier], set()
        while stack:
            parent = stack.pop()
            if parent in seen:
                raise ValueError('cyclic blocks')
            seen.add(parent)
            cursor = None
            while True:
                suffix = '?page_size=100' + (f'&start_cursor={cursor}' if cursor else '')
                data = self.request('GET', f'blocks/{parent}/children{suffix}')
                for block in data['results']:
                    result.append(block)
                    if len(result) > 10000:
                        raise ValueError('block limit exceeded')
                    if block.get('has_children'):
                        stack.append(block['id'])
                if not data.get('has_more'):
                    break
                cursor = data['next_cursor']
        return result


def company_scope_guard(domain, args, *, strict=False):
    """The existing background-write exception covers company originals only."""
    import hermes_yaml
    from types import SimpleNamespace
    root = Path.home() / '.hermes'
    shared = hermes_yaml.safe_load((root / 'config.yaml').read_text())['plugins']['entries']['team-kit']['settings'].get('personal_scopes')
    if not shared or shared.get('enabled') is not True:
        if strict:
            raise ValueError('company title scope unavailable')
        return
    tools = load_module('_company_scope_sync', Path(__file__).with_name('__init__.py'))
    settings = hermes_yaml.safe_load((HOME / 'config.yaml').read_text())['plugins']['entries']['team-delegation']['settings']
    tools._PLUGIN_CONTEXT = SimpleNamespace(get_config=lambda k,d=None:settings.get(k,d))
    tools.personal_scopes.authorize_mutation(tools, domain, args, {}, scope_config=shared, owner_scope='company')


class Reminders:
    def __init__(self):
        self.plugin = None
        self.rows = None

    def all(self):
        import reminder_reads
        return reminder_reads.all(subprocess.run)

    def set_due(self, identifier, value):
        company_scope_guard('todo', {'action': 'update', 'reminder_id': identifier})
        proc = subprocess.run(['remindctl', 'edit', identifier, *deadline_time.reminder_options(value), '--json', '--no-input', '--no-color'],
                              capture_output=True, text=True, timeout=30)
        self.rows = None
        if proc.returncode:
            raise RuntimeError('reminder due edit failed')
        row = self.current(identifier)
        expected = deadline_time.canonical(value)
        if (deadline_time.canonical(row.get('dueDate'), all_day=row.get('dueDateIsAllDay') is True) != expected
                or deadline_time.canonical(row.get('alarmDate')) != (expected if len(expected) > 10 else None)):
            raise RuntimeError('reminder due/alarm verification failed')

    def current(self, identifier):
        import reminder_reads
        row = reminder_reads.one(identifier, subprocess.run)
        if row is None or not isinstance(row.get('isCompleted'), bool):
            raise RuntimeError('reminder identity/state ambiguous')
        return row

    def rename(self, identifier, expected, title):
        company_scope_guard('todo', {'action': 'update', 'reminder_id': identifier}, strict=True)
        if self.current(identifier).get('title') != expected:
            raise ValueError('reminder title changed')
        proc = subprocess.run(['remindctl', 'edit', identifier, '--title', title, '--json', '--no-input', '--no-color'],
                              capture_output=True, text=True, timeout=30)
        self.rows = None
        if proc.returncode or self.current(identifier).get('title') != title:
            raise RuntimeError('reminder title verification failed')

    def complete(self, identifier):
        company_scope_guard('todo', {'action': 'complete', 'reminder_id': identifier})
        if self.plugin is None:
            self.plugin = load_module('_completion_personal_tools', Path(__file__).with_name('__init__.py'))
        receipt = self.plugin._complete_reminder_with_receipt(identifier)
        self.rows = None
        if receipt.get('ok') is not True or not self.current(identifier)['isCompleted']:
            raise RuntimeError('completion not verified')
        return {'ok': True, 'source_id': identifier, 'status': 'completed'}


class Calendar:
    def __init__(self):
        self.plugin = None

    def tools(self):
        if self.plugin is None:
            self.plugin = load_module('_linked_calendar_tools', Path(__file__).with_name('__init__.py'))
        return self.plugin

    def lookup(self, identifier):
        return self.tools()._calendar_event_lookup(identifier)

    def get(self, identifier):
        return self.tools()._calendar_event_get(identifier)

    def rename(self, event, title):
        company_scope_guard('calendar', {'action': 'update', 'event_id': event['calendarItemIdentifier'], 'expected_start': event['start']}, strict=True)
        after = self.tools()._calendar_event_rename(event, title)
        if not after or after.get('title') != title:
            raise RuntimeError('calendar title verification failed')

    def set_notes(self, event, notes):
        company_scope_guard('calendar', {'action': 'update', 'event_id': event['calendarItemIdentifier'], 'expected_start': event['start']}, strict=True)
        if self.tools()._calendar_event_set_notes(event, notes) is None:
            raise RuntimeError('calendar notes verification failed')

    def move(self, event, start, end):
        company_scope_guard('calendar', {'action': 'update', 'event_id': event['calendarItemIdentifier'], 'expected_start': event['start']})
        if self.tools()._calendar_event_move(event, start, end) is None:
            raise RuntimeError('calendar move failed')


def describe(before):
    """Short attachment summary such as 'PPTX 1개, 표 2개' for an owner report."""
    names = [n for n in (before or {}).get('attachment_names') or [] if n]
    kinds = {}
    for name in dict.fromkeys(names):
        kind = (Path(name).suffix.lstrip('.') or '파일').upper()
        kind = '사진' if kind in {'PNG', 'JPG', 'JPEG', 'HEIC', 'GIF', 'WEBP', 'TIF', 'TIFF'} else kind
        kinds[kind] = kinds.get(kind, 0) + 1
    tables = str((before or {}).get('body_html') or '').count('<table')
    parts = [f'{k} {v}개' for k, v in kinds.items()] + ([f'표 {tables}개'] if tables else [])
    return ', '.join(parts) or '첨부'


class Notes:
    def __init__(self):
        self.plugin = None

    def tools(self):
        if self.plugin is None:
            self.plugin = load_module('_completion_note_tools', Path(__file__).with_name('__init__.py'))
        return self.plugin

    def current(self, identifier):
        return self.tools()._note_verification_snapshot(identifier, subprocess.run)

    def link_state(self, identifier):
        before = self.current(identifier)
        return None if not before else {**self.tools()._note_link_state(before.get('plaintext') or ''), 'title': before.get('title')}

    def rename(self, identifier, expected, title):
        company_scope_guard('notes', {'action': 'update', 'note_id': identifier, 'rename_title': True}, strict=True)
        before = self.current(identifier)
        if not before or before.get('title') != expected:
            raise ValueError('note title changed')
        if not self.tools()._rename_linked_note(identifier, title, before).get('ok'):
            raise RuntimeError('note title/body verification failed')

    def edit(self, identifier, lines, edits, before):
        company_scope_guard('notes', {'action': 'update', 'note_id': identifier})
        return self.tools()._edit_linked_note(identifier, before, lines, edits)

    def append(self, identifier, line, before):
        company_scope_guard('notes', {'action': 'update', 'note_id': identifier})
        if before and before.get('attachment_names'):
            return self.tools()._append_note_rebuild(identifier, line, before)
        return self.tools()._append_plain_note(identifier, line, before)


ROLLBACK_RETENTION_DAYS = 30
BODY_FIELDS = ("body_html", "plaintext")


def compact_bodies(value, archive, now):
    if isinstance(value, list):
        return [compact_bodies(v, archive, now) for v in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, child in value.items():
        if key in BODY_FIELDS and isinstance(child, str):
            digest = hashlib.sha256(child.encode()).hexdigest()
            archive[digest] = {"text": child, "saved_at": now}
            result[key + "_ref"] = digest
            result[key + "_sha256"] = digest
            result[key + "_summary"] = re.sub(r"\s+", " ", re.sub(r"<[^>]*>", "", child))[:160]
        else:
            result[key] = compact_bodies(child, archive, now)
    return result


def body_refs(value):
    if isinstance(value, list):
        return set().union(*(body_refs(v) for v in value)) if value else set()
    if not isinstance(value, dict):
        return set()
    found = {value[k + "_ref"] for k in BODY_FIELDS if k + "_ref" in value}
    for child in value.values():
        found.update(body_refs(child))
    return found


def restore_bodies(value, archive):
    if isinstance(value, list):
        return [restore_bodies(v, archive) for v in value]
    if not isinstance(value, dict):
        return value
    result = {k: restore_bodies(v, archive) for k, v in value.items()}
    for key in BODY_FIELDS:
        if key + "_ref" not in result:
            continue
        digest = result.pop(key + "_ref")
        row = archive.get(digest)
        if not row or hashlib.sha256(row["text"].encode()).hexdigest() != digest:
            raise ValueError("Rollback body expired or unavailable; do not restore a summary")
        result[key] = row["text"]
        result.pop(key + "_sha256", None)
        result.pop(key + "_summary", None)
    return result



class Sync:
    def __init__(self, store, reader, reminders, state_path, *, dry_run=False, notes=None, calendar=None, title_sync=True):
        self.execution_reads = False
        self.link_lifecycle = None
        self.content_engine = None
        self.title_sync = title_sync
        self.notes = notes if notes is not None else Notes()
        self.calendar = calendar
        self.store, self.reader, self.reminders = store, reader, reminders
        self.path, self.dry = state_path, dry_run
        self.state = json.loads(state_path.read_text()) if state_path.exists() else {}
        for key, default in [('pages', {}), ('actions', {}), ('notices', []), ('works', {}), ('work_failures', {})]:
            self.state.setdefault(key, default)
        # The DB journal survives a crash between an external write and state replacement.
        self.rollback_path = state_path.with_name(state_path.stem + "-rollback.json")
        self.rollback = json.loads(self.rollback_path.read_text()) if self.rollback_path.exists() else {}
        if not isinstance(self.rollback, dict) or any(
                not isinstance(v, dict) or not isinstance(v.get("text"), str)
                or not isinstance(v.get("saved_at"), (int, float)) for v in self.rollback.values()):
            raise ValueError("Invalid sync rollback archive; not replaced")
        now = time.time()
        self.state['actions'] = compact_bodies(self.state['actions'], self.rollback, now)
        updates = []
        with store._connect() as conn:
            events = conn.execute("SELECT event_id, payload_json FROM common_work_events WHERE event_type='notion_completion_sync' ORDER BY event_id").fetchall()
        for row in events:
            original = json.loads(row['payload_json'])
            event = compact_bodies(original, self.rollback, now)
            self.state['actions'][event['key']] = event
            if event != original:
                updates.append((json.dumps(event, ensure_ascii=False), row['event_id']))
        protected = set()
        for action in self.state['actions'].values():
            if action.get('status') != 'done':
                protected.update(body_refs(action))
        self.rollback = {k: v for k, v in self.rollback.items()
                         if k in protected or v['saved_at'] >= now - ROLLBACK_RETENTION_DAYS * 86400}
        if not self.dry:
            # Original bodies become durable BEFORE either duplicate is removed.
            save(self.rollback_path, self.rollback)
            with store._connect() as conn:
                conn.executemany("UPDATE common_work_events SET payload_json=? WHERE event_id=?", updates)
            self.persist()
        self.messages = []
        self.reports = []  # one Block Kit report per Notion page / linked work
        self.lines = None  # bullets for the Notion page being processed
        self.batch = None  # note lines queued for one write per note
        self.completion = None  # work_completion.Completion when enabled
        self.warning_diagnosis = None  # request_warning_diagnosis in production: 진단담당 first (2026-10-26)
        self.warning_meta = {}  # warning line -> (common_id, reason code)
        self.conflict_asks = []  # (common_id, title) content conflicts released to the owner: ask with buttons
        self.resolved = []  # (common_id, outcome) conflicts the content sync closed this pass

    def work_report(self, item, lines):
        head = f'*연결된 업무 반영 — {item.get("title") or "제목 확인 불가"}*'
        self.messages.append('\n'.join([head, *lines]))
        url = next((r.get('metadata', {}).get('url') for r in item.get('knowledge_refs', []) if r.get('ref_type') == 'notion_page'), None)
        self.reports.append({'title': item.get('title') or '제목 확인 불가', 'url': url, 'subtitle': '연결된 업무 반영',
                             'lines': lines, 'when': datetime.now(seoul()).strftime('%-m/%-d %H:%M'), 'fallback': head})

    def say(self, line):
        (self.lines if self.lines is not None else self.messages).append(line)

    def persist(self):
        if not self.dry:
            save(self.path, self.state)

    def drop_repeated_standing(self, cid, lines):
        """Standing states (warnings, skipped or past-due reminders) recur on every pass until
        someone acts; report them once per work and again only after they change or clear.
        Change lines (written, created, moved...) are always reported."""
        standing = [line for line in lines if line.startswith('• ⚠️') or
                    line.startswith(('• 할 일 미리알림 skipped:', '• 할 일 미리알림 past_due:'))]
        reported = self.state.setdefault('standing_reports', {})
        signature = hashlib.sha256('\n'.join(standing).encode()).hexdigest() if standing else None
        repeated = signature is not None and reported.get(cid) == signature
        if reported.get(cid) != signature:
            if signature: reported[cid] = signature
            else: reported.pop(cid, None)
            self.persist()
        return [line for line in lines if line not in standing] if repeated else lines

    def report(self, key, message):
        if key not in self.state['notices']:
            self.say(message)
            self.state['notices'].append(key)
            self.persist()

    def rollback_before(self, key):
        """Read-only reconstruction for the existing owner-approved restore procedure."""
        return restore_bodies(self.state['actions'][key].get('before', {}), self.rollback)

    def event(self, common_id, key, status, **fields):
        fields = compact_bodies(fields, self.rollback, time.time())
        if not self.dry:
            save(self.rollback_path, self.rollback)
            with self.store._connect() as conn:
                self.store._event(conn, common_id, 'notion_completion_sync',
                                  {'key': key, 'status': status, **fields})
        self.state['actions'][key] = {'status': status, **fields}
        self.persist()

    def complete(self, common_id, pid, marker, identifier, item=''):
        key = '|'.join([pid, marker, identifier])
        old = self.state['actions'].get(key, {})
        if old.get('status') == 'done':
            return {'ok': True, 'source_id': identifier, 'status': 'completed'}
        current = self.reminders.current(identifier)
        if self.dry:
            if not current['isCompleted']:
                self.say(f'• [시험 실행] 미리알림 「{current.get("title") or "제목 확인 불가"}」 완료 예정')
            return {'ok': True, 'source_id': identifier, 'status': 'completed'}
        if old.get('status') == 'pending' and not current['isCompleted']:
            raise RuntimeError('previous write unresolved; manual review required')
        before = old.get('before', {'isCompleted': current['isCompleted']})
        if not current['isCompleted']:
            self.event(common_id, key, 'pending', before=before, source_id=identifier)
            self.reminders.complete(identifier)
            cause = f'할 일 「{item}」 체크 → ' if item else ''
            self.say(f'• {cause}미리알림 「{current.get("title") or "제목 확인 불가"}」 완료 처리')
        self.event(common_id, key, 'done', before=before, source_id=identifier)
        return {'ok': True, 'source_id': identifier, 'status': 'completed'}

    def append_note(self, cid, pid, marker, identifier, text, line=None, edits=()):
        # Keyed by the common work, not the note: a rebuild gives the note a new ID.
        key = '|'.join([pid, marker, 'note:' + cid])
        legacy = '|'.join([pid, marker, identifier])
        prior = self.state['actions'].get(key) or self.state['actions'].get(legacy) or {}
        if prior.get('status') == 'done':
            return
        if prior.get('status') == 'pending':
            self.report(f'notes-unknown:{key}', f'• ⚠️ 메모 — 지난번 쓰기 결과가 확실하지 않아 다시 시도하지 않았어요. '
                        f'메모를 열어 `{prior.get("line") or text}` 줄이 들어갔는지 확인해 주세요.')
            return
        day = datetime.now(seoul()).strftime('%Y-%m-%d')
        entry = (key, line if line is not None else f'✅ 완료: {text} (노션 체크 {mmdd(day)})', list(edits))
        if self.batch is not None:
            # One note write per page/work: a rebuild per line would churn the note ID.
            entries = self.batch.setdefault((cid, identifier), [])
            if entry[0] not in {e[0] for e in entries}:
                entries.append(entry)
            return
        self.write_note(cid, identifier, [entry])

    def flush_notes(self):
        batch, self.batch = self.batch or {}, None
        for (cid, identifier), entries in batch.items():
            self.write_note(cid, identifier, entries)

    def write_note(self, cid, identifier, entries):
        entries = [e if len(e) == 3 else (*e, []) for e in entries]
        lines = [line for _, line, _ in entries if line]
        edits = [x for _, _, ed in entries for x in ed]
        before = self.notes.current(identifier)
        label = (before or {}).get('title') or '제목 확인 불가'
        if not before or not isinstance(before.get('attachment_names'), list):
            self.report(f'notes-held:{cid}:{identifier}', f'• ⚠️ 메모 「{label}」에 기록을 못 넣음 — 메모를 읽지 못했어요. 메모는 바뀌지 않았어요.')
            return
        if self.dry:
            self.say(f'• [시험 실행] 메모 「{label}」 끝에 {len(lines)}줄 추가 예정\n' + '\n'.join(f'    `{x}`' for x in lines))
            return
        for key, line, _ in entries:
            self.event(cid, key, 'pending', before=before, line=line, source_id=identifier)
        if edits and hasattr(self.notes, 'edit'):
            receipt = self.notes.edit(identifier, lines, edits, before)
        else:
            receipt = self.notes.append(identifier, '\n'.join(lines), before)
        if receipt.get('ok') is True:
            for key, line, _ in entries:
                self.event(cid, key, 'done', before=before, line=line, source_id=identifier)
            for e in receipt.get('applied') or []:
                if e['kind'] == 'check':
                    # Our own tick: the memo pass must not read it back as the owner's.
                    self.event(cid, f"{cid}|memo-done|{identifier}|{e['label']}", 'done', source_id=identifier)
                self.say(f'• 메모 「{label}」의 할 일 「{e["label"]}」을 ☑로 표시' if e['kind'] == 'check'
                         else f'• 메모 「{label}」의 다음 마감을 {mmdd(e["old"])} → {mmdd(e["new"])}로 고침')
            if receipt.get('changed') and lines:
                self.say(f'• 메모 「{label}」 맨 끝에 {"한 줄" if len(lines) == 1 else f"{len(lines)}줄"} 추가\n' + '\n'.join(f'    `{x}`' for x in lines))
                if receipt.get('new_note_id'):
                    self.say(f'    ↳ 첨부({describe(before)})가 있어 사진·파일이 깨지지 않도록 새 메모로 다시 만들었어요. '
                             '내용·표·첨부는 그대로이고, 메모 ID와 만든 날짜만 바뀌었어요. 메모·미리알림 연결은 새 메모로 옮겼고, '
                             '이전 메모는 Notes의 최근 삭제됨에 30일간 남아 있어요.')
            elif not receipt.get('changed'):
                self.say(f'• 메모 「{label}」에는 같은 기록이 이미 있어 그대로 뒀어요.')
        elif receipt.get('needs_choice'):
            # Photos and documents together: the owner decides how (asked once, kept as a waiting choice).
            for key, line, _ in entries:
                self.event(cid, key, 'done', before=before, line=line, source_id=identifier, asked=receipt.get('choice_id'))
            self.report(f'notes-choice:{receipt.get("choice_id")}',
                        f'• ⚠️ 메모 「{label}」는 사진과 PDF·파일이 함께 있어 기록을 넣지 않았어요. 어떻게 넣을지 답해 주세요.\n'
                        + '\n'.join(f'    `{x}`' for x in lines)
                        + '\n    ↳ 「새로 만들어」라고 답하면 새 메모로 다시 만들어 넣어요(메모 ID·링크·만든 날짜가 바뀜).'
                        + '\n    ↳ 답하지 않으면 메모는 그대로 두고, 이 줄만 넣지 않은 상태로 남아요.')
        elif receipt.get('written') is False:
            # Nothing was written: safe to try again next run; tell the owner only once.
            for key, line, _ in entries:
                self.event(cid, key, 'retry', before=before, line=line, source_id=identifier)
            self.report(f'notes-failed:{entries[0][0]}', f'• ⚠️ 메모 「{label}」에 기록을 못 넣음 — {receipt.get("reason") or "검증 실패"}. '
                        '아무것도 쓰기 전에 멈춰서 메모는 바뀌지 않았고, 10분 뒤 다음 확인 때 다시 시도해요.')
        else:
            self.report(f'notes-failed:{entries[0][0]}', f'• ⚠️ 메모 「{label}」에 기록을 못 넣음 — {receipt.get("reason") or "검증 실패"}. '
                        '쓰기 도중 확인에 실패해 자동으로 다시 시도하지 않아요. 메모를 열어 상태를 확인해 주세요.')
            # Leave pending even after a reported rollback: never retry ambiguous native writes.

    def linked(self, pid):
        with self.store._connect() as conn:
            rows = conn.execute("SELECT common_id, external_id FROM knowledge_refs WHERE ref_type='notion_page' AND unlinked_at IS NULL").fetchall()
        return sorted({r['common_id'] for r in rows if r['external_id'].replace('-', '').lower() == pid})

    def candidates(self, title):
        with self.store._connect() as conn:
            rows = conn.execute("SELECT common_id, title FROM common_work WHERE status='active'").fetchall()
        ranked = sorted(((difflib.SequenceMatcher(None, normalize(title), normalize(r['title'])).ratio(), r['title']) for r in rows), reverse=True)
        return ', '.join(f'「{name}」' for score, name in ranked[:3] if score >= .35) or '없음'

    def process(self, page):
        self.lines = []
        self.batch = {}
        try:
            self._process(page)
            self.flush_notes()
        finally:
            self.batch = None
            lines, self.lines = self.lines, None
            if lines:
                props = page.get('properties', {})
                title = ''.join(t.get('plain_text', '') for t in (props.get('업무/프로젝트') or {}).get('title', [])) or '제목 확인 불가'
                done = ((props.get('상태') or {}).get('status') or {}).get('name') == 'Done'
                head = f'*노션 완료 반영 — <{page["url"]}|{title}>*' if page.get('url') else f'*노션 완료 반영 — {title}*'
                if done:
                    head += '\n노션 업무 상태가 *Done*이 됐어요.'
                    lines = ['• 노션 업무 상태가 「Done」이 됐어요.', *lines]
                self.messages.append('\n'.join([head, *lines]))
                self.reports.append({'title': title, 'url': page.get('url'), 'subtitle': '노션 완료 반영', 'lines': lines,
                                     'when': datetime.now(seoul()).strftime('%-m/%-d %H:%M'), 'fallback': head})

    def _process(self, page):
        pid = page_id(page['id'])
        if page.get('archived') or page.get('in_trash'):
            self.report(f'archived:{pid}', '• 휴지통·보관된 노션 페이지라 건너뛰었어요.')
            return
        if page_id(page.get('parent', {}).get('database_id', '')) != DATABASE:
            raise ValueError('page outside authorized database')
        props = page['properties']
        title = ''.join(t.get('plain_text', t.get('text', {}).get('content', '')) for t in props['업무/프로젝트']['title'])
        done = (props.get('상태', {}).get('status') or {}).get('name') == 'Done'
        checked, checked_day = {}, {}
        for block in self.reader.blocks(pid):
            todo = block.get('to_do')
            if todo and todo.get('checked'):
                checked[block['id']] = todo_text(todo)
                checked_day[block['id']] = day_of(block.get('last_edited_time'))
        previous = self.state['pages'].get(pid, {})
        if previous.get('done') and not done or set(previous.get('checked', [])) - checked.keys():
            self.report(f'reopened:{pid}:{page["last_edited_time"]}', '• 노션에서 완료 표시가 해제됐어요. 이미 완료 처리한 미리알림·메모는 되돌리지 않았어요.')
        links = self.linked(pid)
        if done or checked:
            if not links:
                hint = self.candidates(title)
                self.report(f'unlinked:{pid}:0', '• ⚠️ 이 노션 페이지는 메모·미리알림과 연결돼 있지 않아 아무것도 바꾸지 않았어요.' + (
                    f'\n    비슷한 업무: {hint}\n    업무비서에게 "이 노션을 「…」에 연결해"라고 하면 다음 확인부터 반영해요.'
                    if hint != '없음' else ''))
            else:
                items = [self.store.get(cid) for cid in links]
                # Source ownership is unique in the registry; still deduplicate the union.
                todos = {s['source_id']: (item['common_id'], s) for item in items
                         for s in item['sources'] if s['domain'] == 'todo'}
                if done:
                    for item in items:
                        cid = item['common_id']
                        label = item.get('title') or '제목 확인 불가'
                        if self.dry:
                            for sid, (owner, _) in todos.items():
                                if owner == cid:
                                    self.complete(cid, pid, 'page_done', sid)
                            if item['status'] != 'completed':
                                self.say(f'• [시험 실행] 공통 업무 「{label}」 완료 예정')
                        elif item['status'] != 'completed':
                            self.store.complete(cid, approved=True, reminder_completer=lambda sid: self.complete(cid, pid, 'page_done', sid))
                            self.say(f'• 업무 「{label}」 전체를 완료로 표시하고 연결된 미리알림을 모두 완료 처리')
                else:
                    natives = [(sid, cid, self.reminders.current(sid)) for sid, (cid, _) in todos.items()]
                    unmatched = []
                    for bid, text in checked.items():
                        wanted = normalize(text)
                        matches = [(sid, cid) for sid, cid, n in natives
                                   if ((todos[sid][1].get('metadata', {}).get('deadline') or {}).get('task_id') == bid
                                       if (todos[sid][1].get('metadata', {}).get('deadline') or {}).get('task_id')
                                       else wanted and wanted in normalize(n.get('title', '')))]
                        if len(matches) == 1:
                            sid, cid = matches[0]
                            self.complete(cid, pid, bid, sid, item=text)
                        elif todos and not matches:
                            if f'match:{pid}:{bid}:0' not in self.state['notices']:
                                self.state['notices'].append(f'match:{pid}:{bid}:0')
                                unmatched.append(text)
                        elif todos:
                            self.report(f'match:{pid}:{bid}:{len(matches)}', f'• 할 일 「{text}」 체크 → 제목이 맞는 미리알림이 {len(matches)}개라 '
                                        '어느 것인지 정할 수 없어 미리알림은 건드리지 않았어요.')
                    if unmatched:
                        self.say('• 체크된 할 일 ' + ', '.join(f'「{t}」' for t in unmatched)
                                 + ' — 이름이 맞는 미리알림이 없어 미리알림은 그대로 뒀어요.')
                        self.persist()
                for item in items:
                    markers = {'page_done': title} if done else checked
                    for marker, text in markers.items():
                        # The day Notion recorded the check, not the day this watcher first saw it.
                        when = checked_day.get(marker) or day_of(page.get('last_edited_time'))
                        how = '노션 Done' if marker == 'page_done' else '노션 체크'
                        line = f'✅ 완료: {text} ({how} {mmdd(when)})' if when else None
                        # Re-read links each time: an earlier append may have rebuilt the note.
                        for nid in self.note_ids(self.store.get(item['common_id'])):
                            self.append_note(item['common_id'], pid, marker, nid, text, line=line,
                                             edits=[{'kind': 'check', 'label': text}] if marker != 'page_done' else ())
        self.state['pages'][pid] = {'done': done, 'checked': list(checked)}
        self.persist()

    # ---- Linked work: memo · Notion · Reminders · Calendar (2026-10-16 owner decision) ----
    def snapshot(self, item):
        """Current state of every source linked to one common work; unreadable parts are None."""
        snap = {'notion': None, 'todo': {}, 'calendar': {}}
        pages = [r['external_id'] for r in item.get('knowledge_refs', []) if r.get('ref_type') == 'notion_page']
        if len(pages) == 1:
            try:
                page = self.reader.page(pages[0])
                if page_id(page.get('parent', {}).get('database_id', '')) == DATABASE and not page.get('in_trash'):
                    props = page['properties']
                    due = ((props.get(DUE) or {}).get('date') or {}).get('start')
                    todos = {b['id']: [todo_text(b['to_do']), bool(b['to_do'].get('checked'))]
                             for b in self.reader.blocks(page_id(page['id'])) if b.get('to_do')}
                    snap['notion'] = {'pid': page_id(page['id']), 'url': page.get('url', ''),
                                      'done': ((props.get('상태') or {}).get('status') or {}).get('name') == 'Done',
                                      'due': day_of(due), 'exact_due': deadline_time.canonical(due),
                                      'due_range': bool(((props.get(DUE) or {}).get('date') or {}).get('end')), 'todos': todos,
                                      'title': page_title(page)[1]}
            except Exception:
                snap['notion'] = None
        snap['notes'] = {}
        if hasattr(self.notes, 'link_state'):
            for source in item['sources']:
                if source['domain'] == 'notes':
                    try:
                        snap['notes'][source['source_id']] = self.notes.link_state(source['source_id'])
                        if snap['notes'][source['source_id']]:
                            snap['notes'][source['source_id']]['exact_due'] = deadline_time.canonical(snap['notes'][source['source_id']].get('due'))
                    except Exception:
                        snap['notes'][source['source_id']] = None
        rows = {str(r.get('id', '')).lower(): r for r in self.reminders.all()}
        for source in item['sources']:
            sid = source['source_id']
            if source['domain'] == 'todo':
                row = next((r for k, r in rows.items() if k.startswith(sid.lower())), None)
                snap['todo'][sid] = None if row is None else {
                    'title': row.get('title') or '', 'done': bool(row.get('isCompleted')),
                    'due': day_of(row.get('dueDate')), 'raw': row.get('dueDate'), 'allday': bool(row.get('dueDateIsAllDay')),
                    'exact_due': deadline_time.canonical(row.get('dueDate'), all_day=row.get('dueDateIsAllDay') is True)}
            elif source['domain'] == 'calendar':
                event = self.calendar.get(sid)
                snap['calendar'][sid] = None if event is None else {
                    'title': event.get('title') or '', 'day': day_of(event.get('start')), 'event': event,
                    'recurring': bool(event.get('recurring')),
                    'exact_due': deadline_time.canonical(event.get('start'), all_day=event.get('allDay') is True)}
        return snap

    def note_ids(self, item):
        return [s['source_id'] for s in item['sources'] if s['domain'] == 'notes']

    def log_notes(self, item, marker, line, edits=()):
        today = mmdd(datetime.now(seoul()).strftime('%Y-%m-%d'))
        for nid in self.note_ids(self.store.get(item['common_id'])):
            self.append_note(item['common_id'], 'work:' + item['common_id'], marker, nid, '', line=line.replace('{today}', today) if line else '', edits=edits)

    def work_revisions(self, item):
        try:
            sources = self.content_engine.sources
            return {kind+'|'+sid:sources.read(kind,sid)['revision'] for kind,sid in sources.references(item)}
        except Exception:
            return None  # An unavailable read never proves an unchanged source.

    def work_health(self, item, status, reason, errors):
        health = self.state.setdefault('work_health', {})
        cid = item['common_id']
        signature = sorted((r['domain'],r['source_id']) for r in item['sources']) + sorted(
            ('notion',r['external_id']) for r in item.get('knowledge_refs',[]) if r['ref_type']=='notion_page')
        signature = json.loads(json.dumps(signature))
        old = health.get(cid)
        current = {'status':status,'reason':reason,'links':signature}
        if status=='conflict':
            current['revisions'] = self.work_revisions(item)
        transitioned = old is None or (old.get('status'),old.get('reason')) != (status,reason)
        warn = transitioned
        if reason in TRANSIENT_REASONS:
            # Same 3-consecutive-run rule as the plain error path: one failed read is not "repeated".
            current['count'] = 1 if transitioned else old.get('count', 1) + 1
            warn = current['count'] == 3
        if old != current:
            health[cid] = current
            # 'unsupported' (e.g. several calendar originals in one work) is a design limit the owner
            # cannot act on: keep it in the state, do not report it (2026-10-26).
            if warn and status not in ('ok', 'unsupported'):
                label = WORK_ERROR_REASONS.get(reason, '원본 조회·연결 상태를 확인해 주세요')
                errors.append(f'• ⚠️ 「{item.get("title") or "제목 확인 불가"}」 — {label}')
                self.warning_meta[errors[-1]] = (cid, reason)
            self.persist()
            return transitioned
        return False

    def sync_links(self):
        with self.store._connect() as conn:
            ids = [r['common_id'] for r in conn.execute("""
                SELECT common_id FROM common_work WHERE (status='active' AND (
                    EXISTS (SELECT 1 FROM source_links s WHERE s.common_id=common_work.common_id
                            AND COALESCE(json_extract(s.metadata_json, '$.link_state.status'),'ok') IN ('ok','transient'))
                    OR EXISTS (SELECT 1 FROM knowledge_refs k WHERE k.common_id=common_work.common_id
                               AND k.ref_type='notion_page' AND k.unlinked_at IS NULL
                               AND COALESCE(json_extract(k.metadata_json, '$.link_state.status'),'ok') IN ('ok','transient')))) OR (
                    status='completed'
                    AND EXISTS (SELECT 1 FROM source_links s WHERE s.common_id=common_work.common_id AND s.domain='calendar')
                    AND NOT EXISTS (SELECT 1 FROM source_links s WHERE s.common_id=common_work.common_id AND s.domain='notes')
                    AND NOT EXISTS (SELECT 1 FROM knowledge_refs k WHERE k.common_id=common_work.common_id
                                    AND k.ref_type='notion_page' AND k.unlinked_at IS NULL))
            """)]
        errors = []
        for cid in ids:
            item = self.store.get(cid)
            kinds = {s['domain'] for s in item['sources']} | ({'notion'} if any(
                r.get('ref_type') == 'notion_page' for r in item.get('knowledge_refs', [])) else set())
            if any(s.get('metadata', {}).get('deadline') for s in item['sources']):
                kinds.add('deadline_source')
            if len(kinds) < 2 and self.content_engine is None:
                continue
            if self.link_lifecycle is not None:
                decisions = self.link_lifecycle.inspect(item, dry_run=self.dry)
                labels = {'missing':'원본을 찾지 못해 연결만 해제했어요. 새 원본 연결은 소유자 확인이 필요해요',
                          'unsupported':'자동 동기화를 지원하지 않는 원본이에요',
                          'transient':'권한 또는 일시적 조회 실패로 처리를 미뤘어요',
                          'conflict':'연결된 원본 내용이 서로 달라요'}
                for decision in decisions:
                    if self.dry and decision.get('report'):
                        errors.append(f'• ⚠️ 「{item.get("title") or "제목 확인 불가"}」 — {labels[decision["status"]]} ({decision["domain"]})')
                    if decision.get('changed') and decision['status'] != 'ok':
                        log_work_error(self.path.parent.parent, cid, decision['reason'],
                                       ValueError(decision['status']), frames=decision.get('traceback',[]),
                                       exception_type=decision.get('exception_type'))
                if any(d['status']=='transient' for d in decisions):
                    continue
                if not self.dry and any(d.get('old_id') for d in decisions):
                    item = self.store.get(cid)
                allowed = {(d['domain'],d['source_id']) for d in decisions if d['status']=='ok'}
                item = {**item, 'sources':[r for r in item['sources'] if (r['domain'],r['source_id']) in allowed],
                        'knowledge_refs':[r for r in item.get('knowledge_refs',[]) if
                            r['ref_type']!='notion_page' or ('notion',r['external_id']) in allowed]}
                if not allowed:
                    continue
                health = self.state.get('work_health',{}).get(cid,{})
                signature = json.loads(json.dumps(sorted((r['domain'],r['source_id']) for r in item['sources']) + sorted(
                    ('notion',r['external_id']) for r in item.get('knowledge_refs',[]) if r['ref_type']=='notion_page')))
                if health.get('status')=='conflict' and health.get('links')==signature:
                    revisions = self.work_revisions(item)
                    if revisions is not None and revisions==health.get('revisions'):
                        if self.completion is not None and not self.dry:
                            # 진행도 is a status mark, independent of the content conflict.
                            self.lines = []
                            self.completion.fill_progress(item)
                            lines, self.lines = self.lines, None
                            if lines:
                                self.work_report(item, lines)
                        continue
            self.lines = []
            self.batch = {}
            try:
                if self.completion is not None and not self.dry and item['status'] == 'active':
                    # 진행도 first: a content conflict or read error below must not hold back the status mark.
                    self.completion.fill_progress(item)
                if self.content_engine is not None:
                    outcome = self.content_engine.run(item)
                    if outcome.get('resolved'):
                        self.resolved.append((cid, outcome['resolved']))
                    if self.link_lifecycle is not None:
                        self.work_health(item, outcome.get('connection_status','ok'), outcome.get('connection_reason','read_ok'), errors)
                    warned = False
                    for line in outcome['messages']:
                        code = content_warning_code(line)
                        if code and self.warning_diagnosis is not None:
                            # Content-sync warnings follow the 진단담당-first route too, once per work and reason.
                            warned = True
                            key = f'work-error:{cid}:{code}'
                            if key not in self.state['notices']:
                                self.state['notices'].append(key)
                                errors.append(f'• ⚠️ 「{item.get("title") or "제목 확인 불가"}」 — {line.removeprefix("• ⚠️ ")}')
                                self.warning_meta[errors[-1]] = (cid, code, item.get('title') or '제목 확인 불가')
                            continue
                        self.say(line)
                    if not warned and self.warning_diagnosis is not None:
                        self.state['notices'] = [k for k in self.state['notices']
                                                 if not k.startswith(f'work-error:{cid}:work_content_')]
                    if self.completion is not None:
                        self.completion.check({**item, 'status': self.store.get(cid)['status']})
                    if outcome['handled']:
                        self.state['work_failures'].pop(cid, None)
                        self.persist()
                        continue
                if item['status'] != 'active':
                    continue
                self.sync_work(item)
                self.flush_notes()
                self.state['work_failures'].pop(cid, None)
                self.persist()
            except Exception as exc:
                if self.link_lifecycle is not None:
                    reason = work_error_reason(exc)
                    status = 'conflict' if reason in {'linked_task_sources_conflict','ambiguous_task_deadline'} else 'transient'
                    if self.work_health(item,status,reason,errors):
                        log_work_error(self.path.parent.parent,cid,reason,exc)
                else:
                    reason = ('title_write_pending' if title_link_sync.pending(self, cid)
                              else work_error_reason(exc))
                    log_work_error(self.path.parent.parent, cid, reason, exc)
                    previous = self.state['work_failures'].get(cid, {})
                    count = previous.get('count', 0) + 1 if previous.get('reason') == reason else 1
                    self.state['work_failures'][cid] = {'reason': reason, 'count': count}
                    # Same originals as Sources.references; count before its duplicate-domain guard.
                    originals = sum(s['domain'] in {'notes', 'calendar'} for s in item['sources']) + sum(
                        r.get('ref_type') == 'notion_page' for r in item.get('knowledge_refs', []))
                    skip = self.content_engine is not None and originals == 1 and reason in NON_TARGET_REASONS
                    prefix = f'work-error:{cid}:'
                    # Preserve old exception-type keys, without suppressing future distinct coded reasons.
                    already = prefix + reason in self.state['notices'] or any(
                        key.startswith(prefix) and key[len(prefix):] not in WORK_ERROR_REASONS
                        for key in self.state['notices'])
                    if not skip and not already and (reason not in TRANSIENT_REASONS or count >= 3):
                        errors.append(f'• ⚠️ 「{item.get("title") or "제목 확인 불가"}」 — {WORK_ERROR_REASONS[reason]}')
                        self.warning_meta[errors[-1]] = (cid, reason)
                        self.state['notices'].append(prefix + reason)
                    self.persist()
            finally:
                self.batch = None
                lines, self.lines = self.lines, None
                lines = self.drop_repeated_standing(cid, lines)
                if lines:
                    self.work_report(item, lines)

        if self.completion is not None:
            # Works completed in conversation or by a reminder are no longer in the active loop.
            for item in self.completion.completed_items():
                self.lines = []
                self.completion.check(item)
                lines, self.lines = self.lines, None
                if lines:
                    self.work_report(item, lines)
            self.completion.finish_pass()

        if self.link_lifecycle is not None and not self.dry:
            # Migration and watcher share the same durable transition journal.
            # A detached source is still reportable after it leaves source_links.
            with self.store._connect() as conn:
                transitions = conn.execute("SELECT event_id, common_id, payload_json FROM common_work_events WHERE event_type='source_state_changed' ORDER BY event_id").fetchall()
            notices = set(self.state['notices'])
            latest = {}
            for event in transitions:
                data = json.loads(event['payload_json'])
                latest[(event['common_id'],data['domain'],data['source_id'])] = event['event_id']
            for event in transitions:
                key = f'link-state-event:{event["event_id"]}'
                if key in notices:
                    continue
                data = json.loads(event['payload_json'])
                self.state['notices'].append(key)
                if (event['event_id']==latest[(event['common_id'],data['domain'],data['source_id'])]
                        and data['status'] not in {'ok','out_of_scope'}):
                    if data['status']=='missing' and getattr(self,'link_decisions_enabled',False):
                        from link_decisions import decision_key
                        pending=self.state.setdefault('pending_link_decisions',[])
                        key=decision_key({'common_id':event['common_id'],**data})
                        if key not in pending:pending.append(key)
                        continue
                    title = self.store.get(event['common_id'])['title']
                    label = {'missing':'원본을 찾지 못해 연결만 해제했어요. 새 원본 연결은 소유자 확인이 필요해요',
                             'unsupported':'자동 동기화를 지원하지 않는 원본이에요',
                             'transient':'권한 또는 일시적 조회 실패로 처리를 미뤘어요',
                             'conflict':'연결된 원본 내용이 서로 달라요'}[data['status']]
                    errors.append(f'• ⚠️ 「{title}」 (`{event["common_id"]}`) — {label} ({data["domain"]})')
                    self.warning_meta[errors[-1]] = (event['common_id'], 'link_' + data['status'])
            self.persist()
        errors = self.route_warnings(errors)
        if errors:
            head = '*🟡 연결된 업무 확인 필요*'
            text = '\n'.join([head, *errors])
            self.messages.append(text)
            self.reports.append({'title': '연결된 업무 확인 필요', 'url': None,
                                 'subtitle': '연결 업무 감시 오류 요약', 'lines': errors,
                                 'when': datetime.now(seoul()).strftime('%-m/%-d %H:%M'), 'fallback': text})

    def route_warnings(self, errors):
        """2026-10-26 사용자 결정: '확인 필요' warnings go to 진단담당 first. The owner gets a warning only
        when 진단담당 says the owner must fix the original, the diagnosis fails or is refused (limits,
        disabled), or no verdict arrives within an hour. A program defect reaches the owner as 진단담당's
        report-room result (with the existing autofix flow); a recovered one stays silent."""
        if self.dry or self.warning_diagnosis is None:
            return errors
        pending = self.state.setdefault('warning_diagnoses', {})
        owner, now = [], time.time()
        def release(line, meta, note=''):
            # 2026-10-27: a content conflict reaches the owner as a question with a button per original.
            if meta and meta[1] == 'work_content_conflict' and len(meta) > 2:
                self.conflict_asks.append((meta[0], meta[2]))
            else:
                owner.append(line + note)
        for rid, row in list(pending.items()):
            status = diagnosis_status(self.path.parent.parent.parent.parent, rid)
            if status in {'normal', 'posted'}:
                pending.pop(rid)
            elif status == 'owner_needed':
                release(row['line'], row.get('meta')); pending.pop(rid)
            elif status in {'failed', 'no_post', 'missing'} or now - row['at'] > 3600:
                release(row['line'], row.get('meta'), ' _(진단담당 진단을 받지 못해 그대로 알려요)_'); pending.pop(rid)
        for line in errors:
            meta = self.warning_meta.get(line)
            rid = meta and self.warning_diagnosis(meta[0], meta[1])
            if rid:
                pending[rid] = {'line': line, 'at': now, 'meta': list(meta)}
            else:
                release(line, meta)
        self.persist()
        return owner

    def sync_work(self, item):
        cid = item['common_id']
        cur = self.snapshot(item)
        prev = self.state['works'].get(cid)
        if prev is None:
            if not self.dry:
                self.state['works'][cid] = self.storable(cur, None)
                self.persist()
            return
        if self.title_sync and title_link_sync.synchronize(self, item, cur, prev):
            return
        if self.dry:
            return
        notion, pnotion = cur['notion'], prev.get('notion')
        # 1. Reminder completed outside Notion -> check the matching Notion to-do, log in memo.
        for sid, row in cur['todo'].items():
            before = (prev.get('todo') or {}).get(sid)
            if not row or not before or before['done'] or not row['done']:
                continue
            managed = next((s.get('metadata', {}).get('deadline') for s in item['sources'] if s['domain'] == 'todo' and s['source_id'] == sid), None)
            done_key = f"{cid}|reminder-done|{sid}"
            if self.state['actions'].get(done_key, {}).get('status') == 'done':
                continue
            if notion:
                wanted = normalize(row['title'])
                task_id = next((s.get('metadata', {}).get('deadline', {}).get('task_id') for s in item['sources'] if s['domain'] == 'todo' and s['source_id'] == sid), None)
                hits = [bid for bid, (text, checked) in notion['todos'].items()
                        if not checked and (bid == task_id if task_id else normalize(text) and (normalize(text) in wanted or wanted in normalize(text)))]
                if len(hits) == 1:
                    bid = hits[0]
                    self.reader.check_todo(bid)
                    notion['todos'][bid][1] = True
                    text = notion['todos'][bid][0]
                    # The Notion pass must not repeat this completion as a new check.
                    self.event(cid, '|'.join([notion['pid'], bid, 'note:' + cid]), 'done')
                    self.event(cid, '|'.join([notion['pid'], bid, sid]), 'done', source_id=sid)
                    self.say(f'• 미리알림 「{row["title"]}」이 완료됨 → 노션 할 일 「{text}」 체크')
                elif any(not c for _, c in notion['todos'].values()):
                    self.report(f'link-todo:{cid}:{sid}', f'• 미리알림 「{row["title"]}」이 완료됐지만, 노션에서 이름이 맞는 할 일이 '
                                f'{len(hits)}개라 노션은 건드리지 않았어요.')
            ticks = [{'kind': 'check', 'label': label} for nstate in (cur.get('notes') or {}).values() if nstate
                     for label, done in nstate['tasks'].items() if not done and normalize(label) and (normalize(label) == normalize(row['title']) if managed else normalize(label) in normalize(row['title']))]
            self.log_notes(item, f'reminder-done:{sid}', f'✅ 완료: {row["title"]} (미리알림 완료 {{today}})', edits=ticks)
            self.event(cid, done_key, 'done', source_id=sid)
        # 1b. A task ticked in the memo -> check the Notion to-do and complete the reminder of that name.
        for nid, nstate in (cur.get('notes') or {}).items():
            pstate = (prev.get('notes') or {}).get(nid)
            if not nstate or not pstate:
                continue
            for label, done in nstate['tasks'].items():
                if not done or pstate['tasks'].get(label) is not False:
                    continue
                key = f"{cid}|memo-done|{nid}|{label}"
                if self.state['actions'].get(key, {}).get('status') == 'done':
                    continue
                wanted = normalize(label)
                moved = []
                if notion:
                    hits = [bid for bid, (text, checked) in notion['todos'].items() if not checked and normalize(text) and
                            (normalize(text) in wanted or wanted in normalize(text))]
                    if len(hits) == 1:
                        self.reader.check_todo(hits[0])
                        notion['todos'][hits[0]][1] = True
                        self.event(cid, '|'.join([notion['pid'], hits[0], 'note:' + cid]), 'done')
                        moved.append(f'노션 할 일 「{notion["todos"][hits[0]][0]}」 체크')
                rems = [(sid, r) for sid, r in cur['todo'].items() if r and not r['done'] and wanted and wanted in normalize(r['title'])]
                if len(rems) == 1:
                    sid, r = rems[0]
                    self.reminders.complete(sid)
                    r['done'] = True
                    self.event(cid, f"{cid}|reminder-done|{sid}", 'done', source_id=sid)
                    moved.append(f'미리알림 「{r["title"]}」 완료')
                self.say(f'• 메모에서 할 일 「{label}」이 ☑로 바뀜' + (f' → {", ".join(moved)}' if moved else ' (이름이 맞는 노션 할 일·미리알림이 없어 그대로 뒀어요)'))
                self.event(cid, key, 'done', source_id=nid)
        # Timed deadlines (including same-day time changes) and auto-created task links.
        if deadline_link_sync.synchronize(self, item, cur, prev):
            self.state['works'][cid] = self.storable(cur, prev)
            self.persist()
            return
        # 2. Legacy date-only links keep their approved whole-day movement behavior.
        changes = []
        if notion and pnotion and notion['due'] and pnotion.get('due') and notion['due'] != pnotion['due']:
            changes.append(('notion', None, pnotion['due'], notion['due'], '노션 다음 마감'))
        for sid, row in cur['todo'].items():
            before = (prev.get('todo') or {}).get(sid)
            if row and before and not row['done'] and row['due'] and before.get('due') and row['due'] != before['due']:
                changes.append(('todo', sid, before['due'], row['due'], f'미리알림 「{row["title"]}」'))
        for eid, ev in cur['calendar'].items():
            before = (prev.get('calendar') or {}).get(eid)
            if ev and before and not ev['recurring'] and ev['day'] and before.get('day') and ev['day'] != before['day']:
                changes.append(('calendar', eid, before['day'], ev['day'], f'일정 「{ev["title"]}」'))
        for nid, nstate in (cur.get('notes') or {}).items():
            before = (prev.get('notes') or {}).get(nid)
            if nstate and before and nstate.get('due') and before.get('due') and nstate['due'] != before['due']:
                changes.append(('notes', nid, before['due'], nstate['due'], '메모 다음 마감'))
        for origin, oid, old, new, label in changes[:1] if len(changes) > 1 and len({(o, n) for _, _, o, n, _ in changes}) > 1 else changes:
            self.move_date(item, cur, origin, oid, old, new, label)
        if len(changes) > 1 and len({(o, n) for _, _, o, n, _ in changes}) > 1:
            self.report(f'date-conflict:{cid}:{changes[1][2]}:{changes[1][3]}',
                        '• 다른 곳에서도 날짜가 바뀌어 있어 첫 번째 변경만 반영했어요: ' + ', '.join(f'{c[4]} {c[2]}→{c[3]}' for c in changes[1:]))
        self.state['works'][cid] = self.storable(cur, prev)
        self.persist()

    def move_date(self, item, cur, origin, oid, old, new, label):
        cid = item['common_id']
        moved = []
        notion = cur['notion']
        if origin != 'notion' and notion and notion['due'] == old:
            self.reader.set_due(notion['pid'], new)
            notion['due'] = new
            notion['exact_due'] = new
            moved.append('노션 다음 마감')
        todos = [(sid, r) for sid, r in cur['todo'].items() if r and sid != oid and not r['done'] and r['due'] == old]
        if len(todos) == 1:
            sid, row = todos[0]
            value = new if row['allday'] or not row['raw'] else datetime.fromisoformat(
                shift_day(row['raw'], old, new)).astimezone(seoul()).strftime('%Y-%m-%d %H:%M')
            self.reminders.set_due(sid, value)
            row['due'] = new
            row['exact_due'] = deadline_time.canonical(value)
            moved.append(f'미리알림 「{row["title"]}」')
        elif len(todos) > 1:
            self.report(f'date-todos:{cid}:{old}:{new}', f'• 같은 날짜({old})의 미리알림이 {len(todos)}개라 미리알림 날짜는 바꾸지 않았어요.')
        events = [(eid, e) for eid, e in cur['calendar'].items() if e and eid != oid and not e['recurring'] and e['day'] == old]
        if len(events) == 1:
            eid, ev = events[0]
            event = ev['event']
            self.calendar.move(event, shift_day(event['start'], old, new), shift_day(event['end'], old, new))
            ev['day'] = new
            ev['exact_due'] = deadline_time.canonical(shift_day(event['start'], old, new), all_day=event.get('allDay') is True)
            moved.append(f'일정 「{ev["title"]}」')
        elif len(events) > 1:
            self.report(f'date-events:{cid}:{old}:{new}', f'• 같은 날짜({old})의 일정이 {len(events)}개라 일정은 옮기지 않았어요.')
        due_edits = [{'kind': 'due', 'old': old, 'new': new}]
        memo_moved = [nid for nid, n in (cur.get('notes') or {}).items() if n and nid != oid and n.get('due') == old]
        for nid in memo_moved:
            cur['notes'][nid]['due'] = new
            cur['notes'][nid]['exact_due'] = new
        if memo_moved:
            moved.append('메모 다음 마감')
        self.say(f'• {label} 날짜가 {mmdd(old)} → {mmdd(new)}로 바뀜' + (f' → {", ".join(moved)}도 {mmdd(new)}로 옮김' if moved else ' (같은 날짜로 연결된 다른 항목은 없어요)'))
        self.log_notes(item, f'date:{origin}:{oid}:{old}:{new}', f'📅 날짜 변경: {mmdd(old)} → {mmdd(new)} ({label}에서 바뀜, {{today}})',
                       edits=due_edits if memo_moved else ())

    @staticmethod
    def storable(cur, prev):
        """Persist only comparable fields; keep the previous value where a source was unreadable."""
        prev = prev or {}
        out = {'notion': cur['notion'] if cur['notion'] is not None else prev.get('notion'), 'todo': {}, 'calendar': {}, 'notes': {},
               'title_sync_initialized': True}
        for kind in ('todo', 'calendar', 'notes'):
            for key, value in (cur.get(kind) or {}).items():
                value = value if value is not None else (prev.get(kind) or {}).get(key)
                if value is not None:
                    out[kind][key] = {k: v for k, v in value.items() if k != 'event'}
        return out

    def run(self):
        if not self.execution_reads:
            return self._run()
        import reminder_reads
        with reminder_reads.batch():
            state = self.state.setdefault('reminder_run', {'failures':0, 'reported':False})
            try:
                reminder_reads.all(subprocess.run)
            except reminder_reads.ReminderReadError as exc:
                state['failures'] += 1
                log_work_error(self.path.parent.parent, 'run', 'reminder_read_failed', exc)
                if state['failures'] >= 3 and not state['reported']:
                    head = '*🟡 연결된 업무 확인 필요*'
                    lines = ['• ⚠️ 미리알림 조회가 3회 연속 실패해 이번 연결 업무 처리를 모두 미뤘어요. 권한과 연결 상태 확인이 필요해요.']
                    text = '\n'.join([head,*lines])
                    self.messages.append(text)
                    self.reports.append({'title':'연결된 업무 확인 필요','subtitle':'미리알림 조회 실패',
                                         'lines':lines,'when':datetime.now(seoul()).strftime('%-m/%-d %H:%M'),'fallback':text})
                    state['reported'] = True
                self.persist()
                return '\n\n'.join(self.messages)
            state.update(failures=0,reported=False)
            self.persist()
            return self._run()

    def _run(self):
        now = datetime.now(timezone.utc)
        if self.content_engine is not None:
            self.sync_links()
            self.persist()
            return '\n\n'.join(self.messages)
        since = self.state.get('cursor', (now - timedelta(days=7)).isoformat())
        pages = {page_id(p['id']): p for p in self.reader.changed(since)}
        # Revisit tracked pages: a new link does not change Notion's last_edited_time.
        for pid in self.state['pages']:
            if pid not in pages:
                pages[pid] = self.reader.page(pid)
        for page in pages.values():
            self.process(page)
        if self.calendar is not None:
            self.sync_links()
        self.state['cursor'] = now.isoformat()
        self.persist()
        return '\n\n'.join(self.messages)


def main(*, dry_run=False):
    import hermes_yaml as yaml
    home = Path(os.environ.get('HERMES_HOME', '')).resolve()
    if home != HOME.resolve():
        raise RuntimeError('work-assistant home required')
    config = yaml.safe_load((home / 'config.yaml').read_text())
    settings = config.get('plugins', {}).get('entries', {}).get('team-delegation', {}).get('settings', {}).get('notion_completion_sync', {})
    if settings.get('enabled') is not True:
        return
    if page_id(settings.get('database_id', '')) != DATABASE or settings.get('report_channel') != 'owner_dm':
        raise ValueError('unauthorized sync scope')
    state = home / 'state/notion-completion-sync.json'
    state.parent.mkdir(parents=True, exist_ok=True)
    with state.with_suffix('.lock').open('w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        store = CommonWorkStore.production(Path.home() / '.hermes/shared/common-work-links.sqlite3')
        sync = Sync(store, NotionReader(home), Reminders(), state, dry_run=dry_run, calendar=Calendar(),
                    title_sync=settings.get('title_sync', True) is True)
        from link_lifecycle import Lifecycle
        from source_identity import production_reader
        sync.link_lifecycle = Lifecycle(store, production_reader)
        sync.execution_reads = True
        import work_sync
        import work_sources
        import task_reminders_sync
        def sync_task_reminders(item, dry):
            native = task_reminders_sync.Native(home)
            native.watcher_item = item
            return task_reminders_sync.sync(home, {'common_id':item['common_id'], 'dry_run':dry},
                                            native=native, serialized=True)
        sync.content_engine = work_sync.configured(home, work_sources.Sources(home, reader=sync.reader, notes=sync.notes, calendar=sync.calendar),
            store, settings, dry_run=dry_run, reminders=sync_task_reminders)
        sync.link_decisions_enabled = settings.get('link_decisions') is True
        sync.warning_diagnosis = request_warning_diagnosis
        import work_completion
        completion = settings.get('completion') if isinstance(settings.get('completion'), dict) else {}
        if sync.content_engine is not None and completion.get('enabled') is True and completion.get('since'):
            sync.completion = work_completion.Completion(sync, since=completion['since'])
        try:
            output = sync.run()
        except Exception as exc:
            # Do not leak native records, Notion bodies or tokens through exception messages.
            if sync.messages:
                print('\n'.join(sync.messages))
            print(f'Notion 완료 동기화 중단: {type(exc).__name__} — 커서 유지, 로컬 기록 확인 필요')
            raise SystemExit(1) from None
        if output and dry_run:
            print(output)
        elif sync.reports:
            # Reports go to the owner report room (else DM) with the work-assistant app; plain text only if that fails.
            import slack_report
            try:
                missed = slack_report.post_dm(home, sync.reports)
            except Exception:
                missed = [output]
            if missed:
                print('\n\n'.join(missed))

        if not dry_run and (sync.conflict_asks or sync.resolved):
            import conflict_decisions
            try:
                for cid, resolved in sync.resolved:
                    conflict_decisions.close(home, cid, resolved)
                missed = conflict_decisions.ask(home, sync.conflict_asks)
                if missed:
                    print('\n'.join(f'연결된 업무 확인 필요: 「{title}」 — 두 원본이 서로 다르게 바뀌어 충돌로 보류했어요.'
                                    for _, title in missed))
            except Exception as exc:
                log_work_error(home, 'conflict-decisions', 'conflict_ask_failed', exc)

        if sync.completion is not None and not dry_run:
            try:
                sync.completion.flush_asks()
            except Exception as exc:
                log_work_error(home, 'completion', 'completion_ask_failed', exc)

        if sync.link_decisions_enabled and not dry_run and sync.state.get('pending_link_decisions'):
            from link_decisions import Decisions, Inventory
            try:
                result=Decisions(home,store,Inventory(home)).request(dry_run=False,only_keys=sync.state['pending_link_decisions'])
                handled={r['id'] for r in result['rows'] if r['status']=='ok'}
                sync.state['pending_link_decisions']=[key for key in sync.state['pending_link_decisions'] if key not in handled]
                sync.persist()
            except Exception as exc:
                log_work_error(home,'link-decisions','decision_request_failed',exc)


if __name__ == '__main__':
    main(dry_run='--dry-run' in sys.argv)
