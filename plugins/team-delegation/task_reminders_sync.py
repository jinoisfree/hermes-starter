"""Per-task company reminders; reserve writes, verify readback, never replay uncertain calls."""
from __future__ import annotations
from pathlib import Path
import json
import subprocess
from datetime import datetime

try:
    from . import deadline_reminders as old
except ImportError:
    import deadline_reminders as old
import deadline_time as dates
import work_content as content

SCHEMA = {'name': 'task_reminders_sync', 'description':
    '업무비서의 업무별 할 일 미리알림 맞추기. common_id 또는 source_kind(notion|notes)+source_id로 원본을 다시 읽는다. '
    '할 일마다 마감 당일 1개 생성·이동·완료·삭제. 노션·메모가 없으면 업무 제목으로 due(등록 초안 마감), '
    '연결 일정 시작 순으로 마감을 찾아 1개 관리하며 마감이 없으면 no_due로 건너뛴다. '
    '노션·메모의 마감 없는 열린 할 일은 결과 no_due에 남긴다(할 일 줄 | 뒤는 마감 날짜). '
    '날짜만 있으면 시각 없음, 연도 없으면 KST 올해, 7일 제한 없음. '
    '등록의 작성 승인 후 바로 호출한다. 사용자 수정 항목은 관리 제외, 불명확 쓰기는 재시도 금지. '
    '소유자 대화와 승인된 연결 감시·08:40/15:00 점검만 허용.',
    'parameters': {'type': 'object', 'properties': {
        'common_id': {'type': 'string'}, 'source_kind': {'type': 'string', 'enum': ['notion', 'notes']},
        'source_id': {'type': 'string'}, 'due': {'type': 'string', 'description': '노션·메모 출처가 없는 업무의 등록 초안 마감. 연도 없으면 KST 올해. 일정 날짜보다 우선.'},
        'dry_run': {'type': 'boolean', 'default': False}}, 'additionalProperties': False}}


def settings(home):
    import hermes_yaml
    cfg = hermes_yaml.safe_load((Path(home)/'config.yaml').read_text())
    return cfg['plugins']['entries']['team-kit']['settings']['deadline_watch']['auto_reminders']


def role_problem(td, *, session_id=''):
    if not session_id:
        from tools.approval_context import _approval_session_id
        session_id = str(_approval_session_id.get() or '')
    if td.personal_roles.profile(td) != 'work-assistant':
        return 'TASK_REMINDERS_PROFILE_REFUSED'
    env = lambda key: td.get_session_env(key, '') or td.os.environ.get(key, '')
    if env('HERMES_KANBAN_TASK') or env('HERMES_SINGLE_QUERY_SESSION'):
        return 'TASK_REMINDERS_BACKGROUND_REFUSED'
    if env('HERMES_CRON_SESSION'):
        from hermes_constants import get_hermes_home
        cfg = settings(get_hermes_home())
        if cfg.get('enabled') is True and any(session_id.startswith('cron_'+job+'_') for job in cfg.get('jobs', [])):
            return None
        return 'TASK_REMINDERS_BACKGROUND_REFUSED'
    if (env('HERMES_SESSION_PLATFORM') not in {'slack', 'photon'} or not env('HERMES_SESSION_USER_ID')
            or not env('HERMES_SESSION_CHAT_ID')):
        return 'TASK_REMINDERS_OWNER_CHAT_REQUIRED'
    return None


def fingerprint(row):
    return {'title': row.get('title'), 'list': row.get('listName'),
            'due': dates.canonical(row.get('dueDate'), all_day=row.get('dueDateIsAllDay') is True),
            'alarm': dates.canonical(row.get('alarmDate')), 'notes': row.get('notes') or ''}


def linked_existing(native, item, task, rows, list_name):
    matches = []
    for row in old.matching_reminders(rows, task['text'], list_name):
        if row.get('title') != task['text'] or not isinstance(row.get('isCompleted'), bool):
            continue
        actual = fingerprint(row)
        if actual['due'] != task.get('due') or (not task.get('due') and not row['isCompleted']):
            continue
        alarm = task.get('due') if task.get('due') and len(task['due']) > 10 else None
        if actual['alarm'] != alarm and not (row['isCompleted'] and actual['alarm'] is None):
            continue
        linked = native.store.find_by_source(domain='todo', source_id=row['id'])
        if linked and linked['common_id'] == item['common_id']:
            matches.append(row)
    return matches[0] if len(matches) == 1 else None


def unchanged_managed(row, expected):
    current = fingerprint(row)
    if row.get('isCompleted') and current['alarm'] is None:
        expected = {**expected, 'alarm': None}
    return current == expected


class Native(old.Native):
    def __init__(self, home):
        super().__init__(home)
        from work_sources import Sources
        self.sources = Sources(home)

    def resolve(self, args):
        # Reuse only within this resolution. check_source/apply still read fresh before writes.
        snapshots_by_ref = {}
        def read(kind, identifier):
            key = (kind, identifier)
            if key not in snapshots_by_ref:
                snapshots_by_ref[key] = self.sources.read(kind, identifier)
            return snapshots_by_ref[key]
        cid = str(args.get('common_id') or '')
        if cid:
            if args.get('source_id') or args.get('source_kind'):
                raise ValueError('common_id_or_source_required')
            candidate = getattr(self, 'watcher_item', None)
            item = candidate if candidate is not None and candidate['common_id']==cid else self.store.get(cid)
            refs = [r for r in self.sources.references(item) if r[0] in {'notion', 'notes'}]
            if not refs:
                kind, identifier = 'work', cid
                source = self.work_source(item, args.get('due'), read=read)
            else:
                snapshots = [(kind, identifier, read(kind, identifier)) for kind, identifier in refs]
                if len({content.digest(content.semantic(r['content'])['tasks']) for _, _, r in snapshots}) > 1:
                    # Owner decision 2026-10-26: Notion is the standard when task lists differ.
                    notion = [snap for snap in snapshots if snap[0] == 'notion']
                    if len(notion) != 1:
                        raise ValueError('linked_task_sources_conflict')
                    snapshots = notion
                kind, identifier, source = snapshots[0]
        else:
            kind, identifier = args.get('source_kind'), str(args.get('source_id') or '')
            if kind not in {'notion', 'notes'} or not identifier:
                raise ValueError('common_id_or_source_required')
            source = read(kind, identifier)
            item = old.related(self.store, {'kind': kind, 'id': identifier})
            if item is None:
                if args.get('dry_run') is True:
                    item = {'common_id': 'preview:'+content.digest([kind, identifier]), 'status': 'active',
                            'sources': [{'domain': 'notes', 'source_id': identifier}] if kind == 'notes' else [],
                            'knowledge_refs': [{'ref_type': 'notion_page', 'external_id': identifier}] if kind == 'notion' else []}
                elif kind == 'notes':
                    item = self.store.create(title=source['content']['title'], sources=[{'domain': kind, 'source_id': identifier}], link_reason='approved_mutation')
                else:
                    receipt = 'task-source:'+content.digest(source['raw'])
                    item = self.store.link_knowledge_ref('', ref_type='notion_page', external_id=identifier,
                        relation_type='originated_from', link_reason='verified_workflow_evidence', receipt_id=receipt,
                        new_work_title=source['content']['title'])
        if (item.get('status') != 'active' and kind != 'work') or source['closed']:
            raise ValueError('source_closed')
        for ref_kind, ref_id in self.sources.references(item):
            if read(ref_kind, ref_id).get('closed'):
                raise ValueError('linked_work_closed')
        for linked in item.get('sources', []):
            if linked['domain'] == 'todo' and linked.get('metadata',{}).get('link_state',{}).get('status','ok')=='ok':
                self.guard('todo', {'action': 'update', 'reminder_id': linked['source_id']})
        return item, {'kind': kind, 'id': identifier, 'url': identifier, **source}

    def work_source(self, item, due=None, *, read=None):
        """One stable work-level task; calendar reads retain the existing company/identity guards."""
        read = read or self.sources.read
        snapshots = [(kind, identifier, read(kind, identifier))
                     for kind, identifier in self.sources.references(item)]
        if any(row['closed'] for _, _, row in snapshots):
            raise ValueError('linked_work_closed')
        calendar = next((row for kind, _, row in snapshots if kind == 'calendar'), None)
        deadline = dates.extract(due) if due is not None else (
            dates.canonical(calendar['raw']['start'], all_day=calendar['raw'].get('isAllDay', calendar['raw'].get('allDay')) is True)
            if calendar else None)
        raw = {'title': item['title'], 'status': item['status'],
               'originals': [(kind, identifier, row['revision']) for kind, identifier, row in snapshots]}
        return {'content': {'title': item['title'], 'body': '', 'tasks': [
                    {'text': item['title'], 'due': deadline, 'done': item['status'] == 'completed',
                     'key': 'work-deadline', 'source_id': item['common_id']}]},
                'raw': raw, 'revision': content.digest(raw), 'closed': False, 'draft_due': due}

    def mutate(self, action, row, task, due):
        identifier = row['id']
        self.guard('todo', {'action': action, 'reminder_id': identifier})
        latest = self.current_reminder(identifier)
        current = [latest] if latest else []
        if len(current) != 1 or fingerprint(current[0]) != fingerprint(row) or current[0].get('isCompleted') != row.get('isCompleted'):
            raise ValueError('reminder_changed_before_write')
        if action == 'delete':
            cmd = ['delete', identifier, '--force']
        elif action == 'complete':
            cmd = ['complete', identifier]
        else:
            cmd = ['edit', identifier, '--title', task, *dates.reminder_options(due)]
        result = subprocess.run(['remindctl', *cmd, '--json', '--no-input', '--no-color'],
                                capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise RuntimeError('task_reminder_write_uncertain')

    def link(self, item, row, task, marker):
        self.store.link(item['common_id'], domain='todo', source_id=row['id'],
            metadata={'task_reminder': {'key': task['key'], 'marker': marker}}, link_reason='approved_mutation')

    def unlink(self, item, row):
        with self.store._connect() as conn:
            conn.execute("DELETE FROM source_links WHERE common_id=? AND domain='todo' AND source_id=?", (item['common_id'], row['id']))
            self.store._event(conn, item['common_id'], 'task_reminder_deleted', {'source_id': row['id']})

    def reverse_complete(self, source, task):
        if source['kind'] == 'work':
            self.check_source(source)
            with self.store._connect() as conn:
                conn.execute("UPDATE common_work SET status='completed', completed_at=? WHERE common_id=? AND status='active'",
                             (datetime.now(dates.KST).timestamp(), source['id']))
                self.store._event(conn, source['id'], 'completed', {'reason': 'task_reminder_completion', 'task': task['key']})
            return {**source, **self.work_source(self.store.get(source['id']), source.get('draft_due'))}
        before = self.sources.read(source['kind'], source['id'])
        desired = content.semantic(before['content'])
        hits = [r for r in desired['tasks'] if content.normalized(r['text']) == content.normalized(task['text'])]
        if len(hits) != 1:
            raise ValueError('reverse_completion_task_ambiguous')
        hits[0]['done'] = True
        after = self.sources.apply(source['kind'], source['id'], before, desired)
        identifier = self.sources.aliases.get(source['id'], source['id'])
        return {**source, **after, 'id': identifier, 'url': identifier}

    def check_source(self, source):
        current = (self.work_source(self.store.get(source['id']), source.get('draft_due')) if source['kind'] == 'work'
                   else self.sources.read(source['kind'], source['id']))
        if current['revision'] != source['revision'] or current['closed']:
            raise ValueError('task_source_changed_before_write')


def sync(home, args, *, native=None, config=None, now=None, serialized=False):
    if not serialized:
        folder = Path(home)/'state'
        folder.mkdir(parents=True, exist_ok=True)
        lock = folder/'notion-completion-sync.lock'
        if folder.is_symlink() or lock.is_symlink():
            raise ValueError('sync_state_symlink')
        import fcntl
        with lock.open('a') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            return sync(home, args, native=native, config=config, now=now, serialized=True)
    native, config = native or Native(home), config or settings(home)
    if config.get('enabled') is not True or config.get('list') != 'Hermes':
        raise ValueError('company_task_reminder_list_required')
    if 'due' in args:
        if not isinstance(args['due'], str) or not args.get('common_id'):
            raise ValueError('work_deadline_requires_common_id_and_string')
        args = {**args, 'due': dates.extract(args['due'], now=now)}
    item, source = native.resolve(args)
    dry = args.get('dry_run') is True
    result = {'ok': True, 'common_id': item['common_id'], 'created': [], 'moved': [],
              'completed': [], 'deleted': [], 'skipped': [], 'past_due': [], 'no_due': [], 'mutation_calls': 0}
    if source['kind'] == 'work' and not source['content']['tasks'][0]['due'] and item['status'] != 'completed':
        result['skipped'].append({'task': item['title'], 'reason': 'no_due'})
        return {**result, 'applied': False}
    try:
        with content.ledger(home, 'task-reminders-sync') as (path, state):
            work = state['works'].setdefault(item['common_id'], {'tasks': [], 'managed': {}})
            tasks = content.reconcile(work['tasks'], source['content']['tasks'], reserved=work['managed'])
            if len(tasks) > 500:
                raise ValueError('task_source_incomplete')
            if any(row.get('ambiguous') for row in tasks):
                raise ValueError('ambiguous_task_deadline')
            seen = {row['key'] for row in tasks}
            for task in tasks + [dict(text=m['expected']['title'], key=key, deleted=True) for key, m in work['managed'].items() if key not in seen]:
                key = task['key']
                managed = work['managed'].get(key)
                if managed and managed.get('status') == 'retired_due':
                    if task.get('due') and not task.get('done') and not task.get('deleted'):
                        work['managed'].pop(key)
                        managed = None
                    else:
                        continue
                if managed and managed.get('status') == 'deleted':
                    continue
                marker = '[hermes-deadline:'+content.digest([item['common_id'], key])+']'
                rows = native.all()
                marked = [r for r in rows if marker in str(r.get('notes') or '')]
                row = next((r for r in rows if managed and r['id'] == managed.get('id')), None)
                if len(marked) > 1 or (not managed and marked):
                    result['skipped'].append({'task': task['text'], 'reason': 'duplicate_or_unjournaled_marker'}); continue
                if managed is None and not task.get('deleted'):
                    row = linked_existing(native, item, task, rows, config['list'])
                    if row:
                        managed = {'status': 'linked', 'id': row['id'], 'expected': fingerprint(row),
                                   'completion_only': not task.get('due')}
                        work['managed'][key] = managed
                if managed and managed.get('status') in {'pending', 'uncertain', 'excluded', 'deleted'}:
                    result['skipped'].append({'task': task['text'], 'reason': managed['status']}); continue
                if managed and (not row or not unchanged_managed(row, managed['expected'])):
                    managed['status'] = 'excluded'
                    result['skipped'].append({'task': task['text'], 'reason': 'user_modified_or_removed'}); continue
                if managed and managed.get('completion_only') and (task.get('done') or task.get('deleted')):
                    continue
                if row and row.get('isCompleted') and not task.get('done') and not task.get('deleted'):
                    if not dry:
                        managed['status'] = 'pending'; content.save(path, state)
                        try:
                            source = native.reverse_complete(source, task)
                            managed['status'] = 'linked'
                            result['mutation_calls'] += 1
                        except Exception:
                            managed['status'] = 'uncertain'
                            result['ok'] = False; result['execution_uncertain'] = True
                            content.save(path, state)
                            result['skipped'].append({'task': task['text'], 'reason': 'write_uncertain_manual_review'})
                            break
                    task['done'] = True
                    result['completed'].append({'task': task['text'], 'reverse': True}); continue
                action = ('delete' if task.get('deleted') else
                          ('complete' if row and not row.get('isCompleted') else None) if task.get('done') else
                          'delete' if row and not task.get('due') else
                          'update' if row and (row.get('title') != task['text'] or fingerprint(row)['due'] != task.get('due')) else
                          'create' if not row and not task.get('done') and task.get('due') else None)
                if not action:
                    if not row and not task.get('done') and not task.get('deleted') and not task.get('due'):
                        # Kept apart from 'skipped' so the 10-minute watcher does not report it.
                        result['no_due'].append({'task': task['text'], 'reason': 'no_due'})
                    continue
                if action in {'create', 'update'} and dates.past(task['due'], now=now):
                    result['past_due'].append({'task': task['text'], 'due': task['due']}); continue
                if action == 'create' and old.matching_reminders(rows, task['text'], config['list']):
                    result['skipped'].append({'task': task['text'], 'reason': 'existing_unmanaged_including_completed'}); continue
                bucket = {'create': 'created', 'update': 'moved', 'complete': 'completed', 'delete': 'deleted'}[action]
                if dry:
                    result[bucket].append({'task': task['text'], 'dry_run': True}); continue
                try:
                    native.check_source(source)
                except Exception:
                    result['ok'] = False
                    result['skipped'].append({'task': task['text'], 'reason': 'source_changed_or_unavailable'})
                    break
                work['managed'][key] = {'status': 'pending', 'id': row['id'] if row else None,
                                        'expected': fingerprint(row) if row else {'title': task['text']},
                                        'before': row, 'after': task}
                content.save(path, state)
                try:
                    if action == 'create':
                        identifier = native.create(task['text'], task['due'], marker, source, config['list'])
                    else:
                        identifier = row['id']; native.mutate(action, row, task['text'], task.get('due'))
                    result['mutation_calls'] += 1
                    if hasattr(native, 'current_reminder'):
                        latest = native.current_reminder(identifier)
                        after = [latest] if latest else []
                    else:
                        after = [r for r in native.all() if r['id'] == identifier]
                    if action == 'delete':
                        if after: raise RuntimeError('reminder_delete_unverified')
                        native.unlink(item, row)
                        work['managed'][key].update(status='deleted' if task.get('deleted') else 'retired_due')
                    else:
                        if len(after) != 1: raise RuntimeError('reminder_identity_unverified')
                        if action == 'complete':
                            if not after[0].get('isCompleted'): raise RuntimeError('completion_unverified')
                        elif not old.verify(after[0], task['due'], task['text'], config['list']):
                            raise RuntimeError('reminder_due_alarm_unverified')
                        if action == 'create': native.link(item, after[0], task, marker)
                        work['managed'][key].update(status='linked', id=identifier, expected=fingerprint(after[0]))
                    result[bucket].append({'task': task['text'], 'reminder_id': identifier})
                    content.save(path, state)
                except Exception:
                    work['managed'][key]['status'] = 'uncertain'
                    result['ok'] = False; result['execution_uncertain'] = True
                    content.save(path, state)
                    result['skipped'].append({'task': task['text'], 'reason': 'write_uncertain_manual_review'})
                    break
            if not dry:
                work['tasks'] = tasks
                content.save(path, state)
    except (ValueError, OSError):
        if not result['mutation_calls'] and not result.get('execution_uncertain'):
            raise
        result['ok'] = False; result['execution_uncertain'] = True
        result['skipped'].append({'reason': 'verification_failed_after_write'})
    result['applied'] = result['mutation_calls'] > 0
    return result
