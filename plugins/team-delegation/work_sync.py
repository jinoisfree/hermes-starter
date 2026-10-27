"""Owner-authorized full linked-work sync, using immutable baselines and a no-replay journal."""
from __future__ import annotations
import time
import work_content as content
import work_tool_writes

CONTENT_VERSION = 'work-content-v1'
SYNCED = ('title', 'tasks')  # 2026-10-27 사용자 결정: bodies differ by format per original and are not synced


def synced(row):
    """What moves between originals: title and tasks (text, due, done). Each original keeps its own body."""
    value = content.semantic(row)
    return {field: value[field] for field in SYNCED}

def configured(home, sources, store, settings, **kwargs):
    return Engine(home, sources, store, **kwargs) if settings.get('content_sync', True) is True else None


class Engine:
    def __init__(self, home, sources, store, *, reminders=None, dry_run=False):
        self.home, self.sources, self.store = home, sources, store
        self.reminders, self.dry = reminders, dry_run
        self.reads = {}  # last originals read per work, reused by completion detection

    def event(self, cid, payload):
        if not self.dry:
            with self.store._connect() as conn:
                self.store._event(conn, cid, 'work_sync', payload)

    def run(self, item):
        cid, messages = item['common_id'], []
        refs = self.sources.references(item)
        work_deadline = len(refs) == 1 and refs[0][0] == 'calendar'
        if (item.get('status') != 'active' and not work_deadline) or not refs:
            return {'handled': False, 'messages': []}
        duplicate_domains = len(refs) != len({kind for kind, _ in refs})
        if len(refs) == 1 or duplicate_domains:
            if self.reminders:
                result = self.reminders(item, self.dry)
                messages = [f'• 할 일 미리알림 {key}: {len(result[key])}개' for key in
                            ('created', 'moved', 'completed', 'deleted', 'past_due', 'skipped') if result.get(key)]
            return {'handled': True, 'messages': messages,
                    **({'connection_status': 'unsupported', 'connection_reason': 'single_original_per_domain_required'}
                       if duplicate_domains else {})}
        started = time.time()
        with content.ledger(self.home, 'work-sync') as (path, state):
            work = state['works'].get(cid)
            current = {kind+'|'+identifier: self.sources.read(kind, identifier) for kind, identifier in refs}
            self.reads[cid] = current
            if any(row.get('closed') for row in current.values()):
                return {'handled': True, 'messages': ['• ⚠️ 업무 원본이 삭제·보관됨 — 업무 자체 삭제는 전파하지 않았어요.']}
            if work is None or ((set(work['sources']) != set(current) or work.get('content_version') != CONTENT_VERSION)
                                and not work.get('pending')):
                if not self.dry:
                    state['works'][cid] = {'sources': current, 'content_version': CONTENT_VERSION}
                    content.save(path, state)
                    work_tool_writes.clear(self.home, current, started)
                    if work and work.get('conflict'):
                        return {'handled': True, 'messages': [], 'resolved': {'choice': None, 'status': 'cleared'}}
                return {'handled': True, 'messages': []}
            if work.get('pending'):
                wanted = work['pending']['after']
                if (set(wanted) == {kind for kind, _ in refs} and
                        all(synced(row['content']) == synced(wanted[key.split('|', 1)[0]]) for key, row in current.items())):
                    if not self.dry:
                        self.event(cid, {'status': 'reconciled', **work['pending']})
                        work.pop('pending'); work['sources'] = current; content.save(path, state)
                elif set(current) != set(work['pending']['before']):
                    # An original was replaced or relinked after the uncertain write (e.g. an owner-approved
                    # note rebuild), so the old desired state can never be read back. Adopt the current
                    # originals as the new baseline without replaying anything.
                    if not self.dry:
                        self.event(cid, {'status': 'superseded', **work['pending']})
                        state['works'][cid] = {'sources': current, 'content_version': CONTENT_VERSION}
                        content.save(path, state)
                        work_tool_writes.clear(self.home, current, started)
                    return {'handled': True, 'messages': ['• 앞선 불명확한 반영 뒤 연결 원본이 바뀌어 현재 원본을 새 기준으로 삼았어요. 자동 재실행은 하지 않았어요.']}
                else:
                    return {'handled': True, 'messages': ['• ⚠️ 앞선 본문·할 일 반영이 불명확해 자동 재실행하지 않았어요. 원본 확인이 필요해요.']}
            changed = [key for key in current if synced(current[key]['content']) != synced(work['sources'][key]['content'])]
            resolved = None
            if work.get('conflict') and work.get('resolution'):
                # The owner chose which original wins after a conflict question (2026-10-27).
                choice = work.pop('resolution').get('choice')
                winner = next((key for key in current if key.split('|', 1)[0] == choice), None)
                resolved = {'choice': choice, 'status': 'kept' if winner is None else 'applied'}
                changed = [] if winner is None else [winner]
                edits = [] if winner is None else [synced(current[winner]['content'])]
                if not self.dry:
                    self.event(cid, {'status': 'owner_resolution', **resolved})
            elif changed:
                edits = [{field: value for field, value in synced(current[key]['content']).items()
                          if value != synced(work['sources'][key]['content'])[field]} for key in changed]
                alternatives = {content.digest(edit) for edit in edits}
                if len(alternatives) != 1 and work_tool_writes.marked(self.home, changed) != set(changed):
                    # Keep the old baseline, remember the conflict so the owner can pick a side, and keep
                    # the reminder sync (Notion owns tasks, 2026-10-26) running meanwhile.
                    if not self.dry and not work.get('conflict'):
                        work['conflict'] = {'kinds': sorted(key.split('|', 1)[0] for key in changed), 'at': time.time()}
                        content.save(path, state)
                    messages = ['• ⚠️ 같은 점검 구간에 두 원본이 서로 다르게 바뀌었어요. 제목·할 일을 쓰지 않고 충돌로 보류했어요.']
                    if self.reminders:
                        result = self.reminders(item, self.dry)
                        messages += [f'• 할 일 미리알림 {key}: {len(result[key])}개' for key in
                                     ('created', 'moved', 'completed', 'deleted', 'past_due', 'skipped') if result.get(key)]
                    return {'handled': True, 'messages': messages, 'conflict': work.get('conflict')}
                if len(alternatives) != 1:
                    # Every differing original was written by work-assistant's own approved tool calls (separate
                    # memo and Notion edits of one request), not by the owner: adopt them as the new baseline
                    # without copying either side (diag-1ea80e2f0831).
                    if not self.dry:
                        self.event(cid, {'status': 'tool_writes_rebaselined', 'sources': changed})
                    changed = []
            if changed:
                desired = {kind: {**content.semantic(current[kind+'|'+identifier]['content']), **edits[0]}
                           for kind, identifier in refs}
                # A structured memo reads lossily (dates leave to-do text) and cannot take flat rows back, so
                # with one involved only completion and the title move between originals (2026-10-26 CIIE).
                structured = getattr(self.sources, 'structured', lambda row: False)
                lossy = any(key.startswith('notes|') and structured(current[key]) for key in changed)
                for kind, identifier in refs:
                    row = current[kind+'|'+identifier]
                    if lossy or (kind == 'notes' and structured(row)):
                        desired[kind] = content.semantic(content.structured_desired(content.semantic(row['content']), desired[kind]))
                targets = [(kind, identifier) for kind, identifier in refs
                           if content.semantic(current[kind+'|'+identifier]['content']) != desired[kind]]
                for kind, identifier in targets:
                    self.sources.preflight(kind, identifier, current[kind+'|'+identifier], desired[kind])
                if self.dry:
                    return {'handled': True, 'messages': ['• [시험 실행] 연결된 제목·할 일을 한 원본의 변경으로 맞출 예정이에요.']}
                work['pending'] = {'before': current, 'after': desired, 'targets': targets}
                content.save(path, state)
                self.event(cid, {'status': 'pending', **work['pending']})
                try:
                    for kind, identifier in targets:
                        current[kind+'|'+identifier] = self.sources.apply(kind, identifier, current[kind+'|'+identifier], desired[kind])
                    # Re-read every original, including the winning source: concurrent edits cannot be hidden.
                    refs = self.sources.references(self.store.get(cid))
                    if len(refs) != len(desired) or {kind for kind, _ in refs} != set(desired):
                        raise RuntimeError('work_sync_originals_changed_during_write')
                    verified = {kind+'|'+identifier: self.sources.read(kind, identifier) for kind, identifier in refs}
                    if any(row.get('closed') or synced(row['content']) != synced(desired[key.split('|', 1)[0]])
                           for key, row in verified.items()):
                        raise RuntimeError('work_sync_readback_differs')
                    self.event(cid, {'status': 'done', **work['pending']})
                    with self.store._connect() as conn:
                        conn.execute("UPDATE common_work SET title=? WHERE common_id=? AND status='active'", (next(iter(desired.values()))['title'], cid))
                    work.pop('pending')
                    current = verified
                    self.reads[cid] = verified
                    messages.append('• 연결된 제목·할 일 목록·마감·완료를 맞췄어요.' if resolved is None else
                                    '• 소유자가 고른 원본 기준으로 제목·할 일을 맞췄어요.')
                except Exception as exc:
                    from notion_completion_sync import log_work_error
                    log_work_error(self.home, cid, 'work_content_write_uncertain', exc)
                    return {'handled': True, 'messages': ['• ⚠️ 본문·할 일 반영 확인이 필요해 멈췄어요. 부분 반영 가능성이 있어 자동 재실행하지 않아요.']}
            if self.reminders:
                result = self.reminders(item, self.dry)
                for bucket in ('created', 'moved', 'completed', 'deleted', 'past_due', 'skipped'):
                    if result.get(bucket):
                        messages.append(f'• 할 일 미리알림 {bucket}: {len(result[bucket])}개')
            if not self.dry:
                work['sources'] = current
                if work.pop('conflict', None) and resolved is None:
                    resolved = {'choice': None, 'status': 'cleared'}
                content.save(path, state)
                work_tool_writes.clear(self.home, current, started)
        return {'handled': True, 'messages': messages, **({'resolved': resolved} if resolved else {})}
