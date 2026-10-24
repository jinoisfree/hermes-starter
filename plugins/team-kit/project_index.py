"""Owner-scoped work index and explicit summary-only continuation."""
import json
import re


def allowed(project, source):
    return all(project.get(k) == source.get(k) for k in ('scope_id', 'chat_id', 'owner_id')) and not re.search(
        r'(?i)\bsecret\b|비밀\s*(?:자료|폴더|프로젝트|업무)', str(project.get('title', '')))


def overview(project, profile):
    tasks = project.get('tasks', [])
    if profile == 'system-doctor':
        tasks = [t for t in tasks if t['profile'] == profile]
        if not tasks:
            return None
    return {'project_id': project['id'], 'title': 'desktop-pet·Hermes 시스템 진단' if profile == 'system-doctor' else project['title'],
            'status': project['status'], 'updated_at': project.get('updated_at'),
            'review_state': project.get('review_state') if profile != 'system-doctor' else None,
            'remaining': [{'id': t['id'], 'profile': t['profile'], 'title': t['title'], 'status': t['status'],
                           'next_step': t.get('next_step', '')} for t in tasks if t['status'] not in {'completed', 'cancelled'}][-10:],
            'url': 'https://app.example.com/' + project['chat_id'] + '/p' + project['thread_id'].replace('.', '')}


def find(store, source, query='', limit=20):
    limit = max(1, min(int(limit), 50))
    with store.connect() as db:
        rows = db.execute('SELECT data FROM projects').fetchall()
    result = []
    for row in rows:
        project = json.loads(row[0])
        if not allowed(project, source):
            continue
        item = overview(project, source['profile'])
        if item and (not query or query.casefold() in json.dumps(item, ensure_ascii=False).casefold()):
            result.append(item)
    return sorted(result, key=lambda i: i['updated_at'] or 0, reverse=True)[:limit]


def resume(store, current, source, args, request):
    from gateway.project_requests import direct_request
    request = direct_request(request)
    selected = store.get(args['project_id'])
    if not selected or not allowed(selected, source) or selected['id'] == current:
        raise ValueError('같은 소유자의 다른 업무를 선택해야 한다.')
    item = overview(selected, source['profile'])
    if not item:
        raise ValueError('이 담당자가 볼 수 없는 업무다.')
    # Explicit reference in the real owner request, never a model-supplied ID alone.
    if not any(value and value in request for value in (selected['id'], selected['thread_id'], item['url'], selected['title'])):
        raise ValueError('사용자 요청에서 이어갈 업무 제목이나 원본 링크를 지정해야 한다.')
    def update(data):
        data.setdefault('resumed_from', {})[selected['id']] = item
        return data
    store.change(current, update)
    return {'ok': True, 'resumed': item, 'approval_transferred': False,
            'notice': '선택한 업무의 요약만 연결했다. 원본 스레드 링크에서 계속하거나 현재 요청으로 남은 단계만 계획한다. 과거 승인·전체 대화는 가져오지 않는다.'}


def followup_candidates(store, source, request, *, now):
    """Only owner top-level backward references; never merge or transfer approval."""
    from gateway.project_requests import direct_request
    request = direct_request(request)
    if source['thread_id'] != source['message_id'] or source['profile'] == 'system-doctor':
        return []
    if not re.search(r'(?:기존\s*일정|겹친\s*일정|그\s*(?:건|일정|내용|작업)|아까|방금|앞서|이어서)', request):
        return []
    if not re.search(r'수정|진행|계속|맞다|맞아|처리|이어', request):
        return []
    if re.search(r'새로운?\s*(?:업무|작업)|별개|다른\s*(?:건|업무)', request):
        return []
    with store.connect() as db:
        rows = db.execute('SELECT data FROM projects').fetchall()
    result = []
    for row in rows:
        p = json.loads(row[0])
        if (not allowed(p, source) or p.get('thread_id') == source['thread_id']
                or p.get('status') != 'open' or not 0 <= now - float(p.get('updated_at') or 0) <= 1800):
            continue
        tasks = p.get('tasks', [])
        summary = str((p.get('last_discussion') or {}).get('summary') or '')
        unfinished = [t for t in tasks if t.get('status') in {'pending', 'blocked', 'unknown', 'failed'}]
        waiting = re.compile(r'실패|확인(?:이)?\s*필요|답변.*기다|승인.*기다|진행할까|수정할까|어떻게.*할까|못했|못했어|못했습니다')
        reports = [t for t in tasks if t.get('status') == 'recorded' and waiting.search(str(t.get('summary', '')))]
        if not unfinished and not reports and not waiting.search(summary):
            continue
        item = overview(p, source['profile'])
        chosen = (unfinished or reports or [{}])[-1]
        item['summary'] = ' '.join(str(chosen.get('next_step') or chosen.get('summary') or summary or p['title']).split())[:180]
        result.append({k: item[k] for k in ('project_id', 'title', 'url', 'summary', 'updated_at')})
    return sorted(result, key=lambda x: x['updated_at'], reverse=True)[:3]


def confirms_followup(request):
    from gateway.project_requests import direct_request
    confirmation = direct_request(request)
    return bool(re.search(r'진행|수정|처리|계속|맞아|맞다|맞습니다|맞아요', confirmation)
                and not re.search(r'하지\s*마|말아|중단|취소|아니|하지\s*않', confirmation))


def refresh_followup(store, key, *, now, source=None, request=''):
    """Clear only local confirmation state; never resume work or transfer approval."""
    def expired_or_declined(data):
        pending = data.get('followup_pending')
        if not pending:
            return False
        created = pending.get('created_at')
        expired = not isinstance(created, (int, float)) or not 0 <= now - created < 1800
        declined = (source is not None and source['message_id'] != pending['message_id']
                    and not confirms_followup(request))
        return expired or declined
    project = store.get(key)
    if expired_or_declined(project):
        def clear(data):
            if expired_or_declined(data):
                data['followup_checked_message_id'] = data['followup_pending']['message_id']
                data.pop('followup_pending')
            return data
        project = store.change(key, clear)
    return project
