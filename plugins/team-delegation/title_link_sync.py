"""Company linked-work title synchronization, with a durable no-replay write journal."""
from __future__ import annotations
import hashlib
import json


def rows(snapshot):
    out = {}
    notion = snapshot.get('notion')
    if notion:
        out[('notion', notion['pid'])] = notion
    for kind in ('todo', 'calendar', 'notes'):
        for identifier, value in (snapshot.get(kind) or {}).items():
            out[(kind, identifier)] = value
    return out


def pending(sync, cid):
    return [(key, value) for key, value in sync.state['actions'].items()
            if key.startswith(cid + '|title|') and value.get('status') == 'pending']


def registry_title(sync, cid, title):
    if not sync.dry:
        with sync.store._connect() as conn:
            conn.execute("UPDATE common_work SET title=? WHERE common_id=? AND status='active'", (title, cid))


def synchronize(sync, item, cur, prev):
    """True means stop this work without advancing the baseline: conflict/unresolved title write."""
    cid = item['common_id']
    current, previous = rows(cur), rows(prev)
    for key, action in pending(sync, cid):
        targets = action['after']['titles']
        if not all(current.get((row['domain'], row['id'])) and current[(row['domain'], row['id'])].get('title') == row['title'] for row in targets):
            sync.report(key + ':unresolved', '• ⚠️ 앞선 제목 변경의 반영 여부가 불명확해 자동 재실행하지 않아요. 원본 확인이 필요해요.')
            return True
        if not sync.dry:
            registry_title(sync, cid, action['after']['title'])
            sync.event(cid, key, 'done', before=action['before'], after=action['after'])
            for row in targets:
                if previous.get((row['domain'], row['id'])):
                    previous[(row['domain'], row['id'])]['title'] = row['title']
    if not prev.get('title_sync_initialized') or any(not previous.get(key) or not previous[key].get('title') for key in current):
        return False  # Upgrade/newly linked originals establish a baseline before any title propagation.
    changed = [(key, row['title']) for key, row in current.items()
               if row and row.get('title') and previous.get(key) and previous[key].get('title')
               and row['title'] != previous[key]['title']]
    if not changed:
        return False
    wanted = {title for _, title in changed}
    signature = hashlib.sha256(json.dumps(changed, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:20]
    key = cid + '|title|' + signature
    if len(wanted) != 1:
        sync.report(key + ':conflict', '• ⚠️ 연결된 원본의 제목이 서로 다르게 바뀌어 아무 제목도 고치지 않았어요. 기준 제목을 골라 주세요.')
        return True
    # Every original must be identifiable, active and unique within its domain.
    kinds = [kind for kind, _ in current]
    if (len(set(kinds)) < 2 or len(kinds) != len(set(kinds)) or
            any(not row or not row.get('title') or row.get('done') or row.get('recurring') for row in current.values())):
        sync.report(key + ':ambiguous', '• 제목 변경을 봤지만 원본이 여러 개이거나 조회·진행 상태가 불명확해 연동을 보류했어요.')
        return True
    title = next(iter(wanted))
    before = {'titles': [{'domain': k[0], 'id': k[1], 'title': row['title']} for k, row in current.items()]}
    after = {'title': title, 'titles': [{**row, 'title': title} for row in before['titles']]}
    if sync.dry:
        sync.say(f'• [시험 실행] 연결된 원본 제목을 「{title}」로 맞출 예정')
        return False
    sync.event(cid, key, 'pending', before=before, after=after)
    for (kind, identifier), row in current.items():
        if row['title'] == title:
            continue
        if kind == 'notion':
            sync.reader.set_title(identifier, row['title'], title)
        elif kind == 'todo':
            sync.reminders.rename(identifier, row['title'], title)
        elif kind == 'calendar':
            sync.calendar.rename(row['event'], title)
            row['event']['title'] = title
        else:
            sync.notes.rename(identifier, row['title'], title)
        row['title'] = title
    registry_title(sync, cid, title)
    sync.event(cid, key, 'done', before=before, after=after)
    sync.say(f'• 연결된 원본 제목을 「{title}」로 맞췄어요.')
    sync.log_notes(item, 'title:' + signature, f'변경: 연결 업무 제목 → {title} ({{today}})')
    return False
