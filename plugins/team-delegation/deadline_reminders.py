"""Owner-authorized deadline -> Reminder creation, restricted by deadline_watch.handle.

Re-read the source, preserve precision, reserve before native writes, then verify and link.
No mail/Notion/Notes/calendar mutation is exposed here. Uncertain writes are never replayed.
"""
from __future__ import annotations
import contextlib
import fcntl
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import os
from datetime import datetime, date
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
from common_work_links import CommonWorkStore
import deadline_time as dt

DATABASE = ''
ACCOUNT = 'user(at)example.com'
DATE = re.compile(r'(\d{4})\s*(?:[-./]|년\s*)(\d{1,2})\s*(?:[-./]|월\s*)(\d{1,2})(?:일)?'
                  r'(?:(?:T|\s+)(?:(오전|오후)\s*)?(\d{1,2})(?::|시\s*)(\d{2})?(?:분)?(?::00)?(Z|[+-]\d{2}:\d{2})?)?')


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    sys.modules[name] = value
    spec.loader.exec_module(value)
    return value


def title_key(value):
    return re.sub(r'\W+', '', str(value).casefold())


def quote_deadline(quote):
    found = []
    for m in DATE.finditer(quote):
        year, month, day, period, hour, minute, zone = m.groups()
        value = f'{int(year):04d}-{int(month):02d}-{int(day):02d}'
        if hour is not None:
            h = int(hour)
            if period:
                if not 1 <= h <= 12: raise ValueError('invalid_deadline_time')
                h = h % 12 + (12 if period == '오후' else 0)
            value += f'T{h:02d}:{int(minute or 0):02d}{zone or "+09:00"}'
        found.append(dt.canonical(value))
    if len(set(found)) != 1:
        raise ValueError('one_explicit_deadline_with_year_required')
    return found[0]


def save(path, data):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.deadline-reminders-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name): os.unlink(name)


@contextlib.contextmanager
def ledger(home):
    folder = Path(home) / 'state'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / 'deadline-reminders.json'
    lock = folder / 'deadline-reminders.lock'
    if any(p.is_symlink() for p in (folder, path, lock)):
        raise ValueError('deadline_ledger_symlink')
    with lock.open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        data = json.loads(path.read_text()) if path.exists() else {'version': 1, 'items': {}}
        if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('items'), dict):
            raise ValueError('deadline_ledger_corrupt')
        yield path, data


class Native:
    def __init__(self, home):
        import hermes_yaml
        self.home = Path(home)
        self.root = self.home.parent.parent
        self.td = module('_deadline_native_tools', HERE / '__init__.py')
        settings = hermes_yaml.safe_load((self.home / 'config.yaml').read_text())['plugins']['entries']['team-delegation']['settings']
        self.td._PLUGIN_CONTEXT = SimpleNamespace(get_config=lambda k, d=None: settings.get(k, d))
        self.scopes = hermes_yaml.safe_load((self.root / 'config.yaml').read_text())['plugins']['entries']['team-kit']['settings']['personal_scopes']
        if self.scopes.get('enabled') is not True:
            raise ValueError('company_scope_required')
        self.store = CommonWorkStore(self.root / 'shared/common-work-links.sqlite3')

    def guard(self, domain, args):
        self.td.personal_scopes.authorize_mutation(self.td, domain, args, {}, scope_config=self.scopes, owner_scope='company')

    def source(self, kind, identifier, task, quote, field):
        if kind == 'notion':
            api = module('_deadline_notion_read', self.home / 'plugins/team-kit/notion.py')
            pid = api.object_id(identifier)
            page = api.request(self.home, 'GET', 'pages/' + pid)
            if str(page.get('parent', {}).get('database_id', '')).replace('-', '') != DATABASE:
                raise ValueError('company_notion_database_required')
            data = api.row(page)
            if data['trashed'] or data['properties'].get('상태') in {'Done', 'Cancelled'}:
                raise ValueError('source_closed')
            body = api._body(self.home, pid, None)
            if body.get('more'): raise ValueError('source_incomplete')
            def blocks(rows):
                for row in rows:
                    yield row
                    yield from blocks(row.get('children', []))
            rows = list(blocks(body['blocks']))
            todo = [b for b in rows if b.get('type') == 'to_do' and title_key(b.get('text', '').split('\n')[0]) == title_key(task)]
            if len(todo) != 1 or todo[0].get('checked'):
                raise ValueError('single_open_notion_task_required')
            if field == '다음 마감':
                if sum(b.get('type') == 'to_do' and not b.get('checked') for b in rows) != 1:
                    raise ValueError('next_deadline_task_ambiguous_use_task_quote')
                value = data['properties'].get(field)
                if not isinstance(value, dict) or not value.get('start') or value.get('end'):
                    raise ValueError('single_next_deadline_required')
                due = dt.canonical(value['start'])
            else:
                if not quote or task not in quote or quote not in todo[0].get('text', ''): raise ValueError('task_deadline_quote_required')
                due = quote_deadline(quote)
            return {'kind': kind, 'id': pid, 'due': due, 'url': data['url'], 'task_id': todo[0]['id'], 'field': field}
        if kind == 'notes':
            self.guard('notes', {'action': 'update', 'note_id': identifier})  # metadata read only
            data = self.td._note_verification_snapshot(identifier, subprocess.run)
            if not data or not isinstance(data.get('plaintext'), str): raise ValueError('source_unavailable')
            text = data['plaintext']
            state = self.td._note_link_state(text)
            if state['tasks'].get(task) is True: raise ValueError('source_closed')
            if not quote or task not in quote or quote not in text: raise ValueError('task_deadline_quote_required')
            due = quote_deadline(quote)
            precise_field = '다음 마감' if list(state['tasks']) == [task] and state.get('due') == due else ''
            return {'kind': kind, 'id': identifier, 'due': due, 'url': identifier, 'field': precise_field}
        if kind == 'gmail':
            if not re.fullmatch('[0-9a-f]+', identifier): raise ValueError('gmail_id_required')
            watch = self.td.gmail_watch
            api, service = watch.gmail_client(self.root)
            if watch.mailbox_address(service) != ACCOUNT: raise ValueError('company_mailbox_required')
            extra = module('_deadline_mail_read', HERE / 'gmail_extra.py')
            rows = extra.thread(api, service, identifier, 100)
            if rows[0].get('earlier_messages_omitted'): raise ValueError('source_incomplete')
            # A later reply may change the deadline or prove completion. Do not use stale mail.
            if not rows[1:] or rows[-1]['id'] != identifier or rows[-1]['sent_by_owner']:
                raise ValueError('latest_incoming_message_required')
            text = self.td._unquoted(rows[-1]['body'])
            if not quote or task not in quote or quote not in text: raise ValueError('task_deadline_quote_required')
            return {'kind': kind, 'id': identifier, 'due': quote_deadline(quote),
                    'url': watch.mail_link(rows[-1]['threadId'], ACCOUNT), 'field': ''}
        raise ValueError('source_kind_required')

    def all(self):
        result = subprocess.run(['remindctl', 'show', 'all', '--json', '--no-input', '--no-color'], capture_output=True, text=True, timeout=30)
        if result.returncode: raise ValueError('reminder_read_failed')
        rows = json.loads(result.stdout)
        if not isinstance(rows, list) or any(not isinstance(r, dict) or not r.get('id') for r in rows):
            raise ValueError('reminder_read_invalid')
        return rows

    def create(self, task, due, marker, source, list_name):
        self.guard('todo', {'action': 'create', 'list': list_name})
        options = ['--due', dt.native(due)]
        if len(due) > 10: options += ['--alarm', dt.native(due)]
        result = subprocess.run(['remindctl', 'add', '--title', task, '--list', list_name, *options,
                                 '--notes', marker + '\n출처: ' + source['url'], '--json', '--no-input', '--no-color'],
                                capture_output=True, text=True, timeout=30)
        if result.returncode: raise ValueError('reminder_create_uncertain')
        receipt = json.loads(result.stdout)
        if not isinstance(receipt, dict) or not receipt.get('id'): raise ValueError('reminder_create_uncertain')
        return receipt['id']


def related(store, source):
    if source['kind'] == 'notes':
        return store.find_by_source(domain='notes', source_id=source['id'])
    ref = 'notion_page' if source['kind'] == 'notion' else 'gmail_message'
    with store._connect() as conn:
        rows = conn.execute('SELECT DISTINCT common_id, external_id FROM knowledge_refs WHERE ref_type=? AND unlinked_at IS NULL', (ref,)).fetchall()
        def identity(value):
            return value.replace('-', '').lower() if ref == 'notion_page' else value
        ids = sorted({r[0] for r in rows if identity(r[1]) == identity(source['id'])})
    if len(ids) > 1: raise ValueError('ambiguous_common_work')
    return store.get(ids[0]) if ids else None


def verify(row, due, task, list_name):
    return (row.get('title') == task and row.get('listName') == list_name and row.get('isCompleted') is False
            and dt.canonical(row.get('dueDate'), all_day=row.get('dueDateIsAllDay') is True) == due
            and dt.canonical(row.get('alarmDate')) == (due if len(due) > 10 else None))


def _ensure(home, args, config, *, native=None, now=None):
    now = now or datetime.now(dt.KST)
    task = str(args.get('task') or '').strip()
    kind, identifier = args.get('source_kind'), str(args.get('source_id') or '').strip()
    quote = str(args.get('source_quote') or '')
    field = args.get('deadline_field') or ''
    due = dt.canonical(args.get('deadline_at'))
    if not task or len(task) > 300 or not identifier or len(quote) > 2000 or not due:
        raise ValueError('deadline_source_and_task_required')
    if field not in {'', '다음 마감'} or (field and kind != 'notion'):
        raise ValueError('unsupported_deadline_field')
    distance = (date.fromisoformat(due[:10]) - now.astimezone(dt.KST).date()).days
    if distance < 0 or (len(due) > 10 and datetime.fromisoformat(due) < now): raise ValueError('past_deadline_refused')
    if distance > 7: raise ValueError('deadline_outside_seven_days')
    native = native or Native(home)
    source = native.source(kind, identifier, task, quote, field)
    if due != source['due']: raise ValueError('deadline_conflict')
    item = related(native.store, source)
    if item and item['status'] != 'active': raise ValueError('common_work_closed')
    identity = kind + ':' + source['id'] + '|' + title_key(task)
    key = hashlib.sha256(identity.encode()).hexdigest()
    if item:
        linked_ids = {r['source_id'] for r in item['sources'] if r['domain'] == 'todo'}
        for row in native.all():
            if row['id'] in linked_ids:
                other = title_key(row.get('title'))
                wanted = title_key(task)
                if other and other != wanted and (other in wanted or wanted in other):
                    raise ValueError('similar_linked_task_review_required')
    marker = '[hermes-deadline:' + key + ']'
    list_name = config.get('list', 'Hermes')
    if list_name != 'Hermes': raise ValueError('fixed_company_list_required')
    with ledger(home) as (path, data):
        previous = data['items'].get(key)
        rows = native.all()
        marked = [r for r in rows if marker in str(r.get('notes') or '')]
        # Both completed and open matches block new creation; never resurrect a checked task.
        matches = [r for r in rows if title_key(r.get('title')) == title_key(task) and r.get('listName') == list_name]
        if len(marked) > 1 or len(matches) > 1: raise ValueError('duplicate_reminder_candidates')
        if previous and previous.get('status') in {'pending', 'uncertain'}:
            raise ValueError('previous_write_uncertain_review_required')
        if previous and not marked and not matches: raise ValueError('managed_reminder_missing_no_recreate')
        if marked or matches:
            row = (marked or matches)[0]
            if row.get('isCompleted'): return {'ok': True, 'status': 'already_completed', 'reminder_id': row['id']}
            if not verify(row, due, task, list_name): raise ValueError('existing_reminder_conflict')
            # Titles alone do not authorize connecting an unrelated existing reminder.
            existing = native.store.find_by_source(domain='todo', source_id=row['id'])
            if not marked and (not item or not existing or item['common_id'] != existing['common_id']):
                return {'ok': True, 'status': 'existing_unlinked_review_required', 'reminder_id': row['id']}
            rid = row['id']
            created = False
        else:
            if sum(v.get('created_day') == now.date().isoformat() for v in data['items'].values()) >= 20:
                raise ValueError('daily_creation_limit')
            data['items'][key] = {'status': 'pending', 'due': due, 'source_kind': kind, 'source_id': source['id'],
                                  'created_day': now.date().isoformat()}
            save(path, data)  # a crash or timeout must not create another reminder
            try:
                rid = native.create(task, due, marker, source, list_name)
                found = [r for r in native.all() if r['id'] == rid]
                if len(found) != 1 or not verify(found[0], due, task, list_name):
                    raise ValueError('reminder_write_unverified')
            except Exception:
                data['items'][key]['status'] = 'uncertain'; save(path, data)
                raise ValueError('reminder_write_uncertain_review_required') from None
            created = True
        metadata = {'deadline': {**source, 'task': task, 'value': due, 'key': key}}
        existing = native.store.find_by_source(domain='todo', source_id=rid)
        try:
            if existing:
                if item and existing['common_id'] != item['common_id']: raise ValueError('common_work_conflict')
                item = existing
            elif item:
                item = native.store.link(item['common_id'], domain='todo', source_id=rid, metadata=metadata, link_reason='approved_mutation')
            else:
                sources = [{'domain': 'todo', 'source_id': rid, 'metadata': metadata}]
                if kind == 'notes': sources.append({'domain': 'notes', 'source_id': source['id']})
                item = native.store.create(title=task, sources=sources, link_reason='approved_mutation')
            if kind in {'notion', 'gmail'}:
                ref = 'notion_page' if kind == 'notion' else 'gmail_message'
                if not any(r['ref_type'] == ref and
                           (r['external_id'].replace('-', '').lower() == source['id'].replace('-', '').lower()
                            if ref == 'notion_page' else r['external_id'] == source['id']) for r in item['knowledge_refs']):
                    receipt = 'deadline-source:' + hashlib.sha256(json.dumps(source, sort_keys=True).encode()).hexdigest()
                    with native.store._connect() as conn:
                        native.store._event(conn, item['common_id'], 'deadline_source_verified',
                                            {'receipt_id': receipt, 'source': source, 'reminder_id': rid})
                    native.store.link_knowledge_ref(item['common_id'], ref_type=ref, external_id=source['id'],
                        relation_type='originated_from', link_reason='verified_workflow_evidence', receipt_id=receipt,
                        metadata={'url': source['url']})
            data['items'][key] = {**data['items'].get(key, {}), 'status': 'linked', 'reminder_id': rid,
                                  'common_id': item['common_id'], 'due': due}
            save(path, data)
        except Exception:
            data['items'][key] = {**data['items'].get(key, {}), 'status': 'uncertain', 'reminder_id': rid}
            save(path, data)
            raise ValueError('reminder_link_uncertain_review_required') from None
    return {'ok': True, 'status': 'created' if created else 'already_exists', 'reminder_id': rid,
            'common_id': item['common_id'], 'deadline_at': due, 'alarm_at': due if len(due) > 10 else None}


def ensure(home, args, config, *, native=None, now=None):
    folder = Path(home) / 'state'
    folder.mkdir(parents=True, exist_ok=True)
    lock = folder / 'notion-completion-sync.lock'
    if folder.is_symlink() or lock.is_symlink(): raise ValueError('deadline_sync_symlink')
    # Serialize creation/linking with the ten-minute connection watcher.
    with lock.open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        return _ensure(home, args, config, native=native, now=now)
