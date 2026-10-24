"""Precision-aware updates for linked deadlines; never treat an event date as a task deadline."""
from datetime import datetime
import deadline_time as dt


def synchronize(sync, item, cur, prev):
    """Handle timed changes and managed date-only tasks. Return True to suppress legacy date sync."""
    metadata = {s['source_id']: s.get('metadata', {}).get('deadline') for s in item['sources'] if s['domain'] == 'todo'}
    managed = {sid: m for sid, m in metadata.items() if m}
    def values(snap):
        result = {}
        if snap.get('notion'):
            n = snap['notion']
            result[('notion', n['pid'])] = n.get('exact_due')
        for kind in ('todo', 'notes', 'calendar'):
            for sid, row in (snap.get(kind) or {}).items():
                if row and not row.get('done') and not row.get('recurring'):
                    result[(kind, sid)] = row.get('exact_due')
        return result
    before, current = values(prev), values(cur)
    changes = [(key, before[key], new) for key, new in current.items()
               if before.get(key) and new and before[key] != new]
    if managed:
        changes = [change for change in changes if change[0][0] != 'calendar']
    precise = any((old[10:] != new[10:]) or (len(old) > 10 and any(k != key and v == old for k, v in current.items()))
                  for key, old, new in changes)
    if not precise and not managed:
        return False
    if not changes:
        return bool(managed)
    # Conflicts must never be resolved by applying whichever change was discovered first.
    if len({(old, new) for _, old, new in changes}) > 1:
        sync.report('deadline-conflict:' + item['common_id'] + ':' + repr(changes),
                    '• ⚠️ 연결된 마감이 서로 다르게 바뀌어 날짜·시간은 그대로 두었어요. 기준 마감을 확인해 주세요.')
        return True
    origins = {key for key, _, _ in changes}
    _, old, new = changes[0]
    def associated(sid, origin):
        m = managed.get(sid)
        if not m: return True
        if origin == ('todo', sid): return True
        return ((m['kind'] == 'notion' and m.get('field') == '다음 마감' and origin == ('notion', m['id']))
                or (m['kind'] == 'notes' and m.get('field') == '다음 마감' and origin == ('notes', m['id'])))
    changed_todos = [sid for (kind, sid) in origins if kind == 'todo' and sid in managed]
    allowed = set()
    for key, value in current.items():
        if value != old or key in origins: continue
        kind, sid = key
        if managed:
            # Automatic task links never authorize moving an event start or another task.
            if kind == 'calendar': continue
            if kind == 'todo' and not any(associated(sid, origin) for origin in origins): continue
            if kind in {'notion', 'notes'} and changed_todos and not any(associated(rid, key) for rid in changed_todos): continue
        allowed.add(key)
    # Different task reminders sharing a timestamp are not evidence of the same deadline.
    todos = [key for key in allowed if key[0] == 'todo']
    if len(todos) > 1:
        for key in todos: allowed.remove(key)
        sync.report(f'deadline-many:{item["common_id"]}:{old}:{new}', '• 같은 마감의 할 일이 여러 개라 미리알림 변경은 보류했어요.')
    moved = []
    for kind, sid in sorted(allowed):
        if kind == 'notion':
            if cur['notion'].get('due_range'): continue
            sync.reader.set_due(sid, new)
            cur['notion'].update(exact_due=new, due=new[:10])
            moved.append('노션 다음 마감')
        elif kind == 'todo':
            sync.reminders.set_due(sid, dt.native(new))
            cur['todo'][sid].update(exact_due=new, due=new[:10], raw=new, allday=len(new) == 10)
            moved.append('미리알림 「' + cur['todo'][sid]['title'] + '」(알림 포함)')
        elif kind == 'calendar':
            # Date-only <-> timed is not an instruction to convert an all-day event.
            if len(old) == 10 or len(new) == 10: continue
            event = cur['calendar'][sid]['event']
            delta = datetime.fromisoformat(new) - datetime.fromisoformat(old)
            end = datetime.fromisoformat(event['end'].replace('Z', '+00:00')) + delta
            sync.calendar.move(event, new, end.isoformat())
            cur['calendar'][sid].update(exact_due=new, day=new[:10])
            moved.append('연결된 일정')
        elif kind == 'notes':
            sync.append_note(item['common_id'], 'deadline', f'{sid}:{old}:{new}', sid,
                             '마감 변경', line=f'📅 마감 변경: {dt.label(old)} → {dt.label(new)}',
                             edits=[{'kind': 'due', 'old': old, 'new': new}])
            cur['notes'][sid].update(exact_due=new, due=new)
            moved.append('메모 다음 마감')
    # When a reminder itself was edited, align its native notification at the same time.
    for kind, sid in origins:
        if kind == 'todo':
            sync.reminders.set_due(sid, dt.native(new))
            cur['todo'][sid].update(exact_due=new, raw=new, allday=len(new) == 10)
            moved.append('변경한 미리알림의 알림 시각')
    if moved:
        sync.say(f'• 마감 {dt.label(old)} → {dt.label(new)}: ' + ', '.join(moved))
    elif managed:
        sync.report(f'deadline-unmatched:{item["common_id"]}:{old}:{new}',
                    '• 마감 변경을 확인했지만 같은 할 일·같은 이전 마감으로 확인되는 연결이 없어 다른 원본은 유지했어요.')
    return True
