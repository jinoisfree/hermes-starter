"""Live, admitted owner-turn continuation. No CLI impersonation or old approvals."""
import asyncio
import concurrent.futures
import dataclasses
import threading
import time

from gateway.project_threads import dependency_ready, run_identity

_LOCK = threading.RLock()
_EVENTS = {}
_ADMITTED = {}


def event_key(source, message_id):
    source = {**source, 'profile': source.get('profile') or 'default'}
    return tuple(str(source.get(k) or '') for k in ('scope_id', 'chat_id', 'thread_id', 'user_id', 'profile')) + (str(message_id),)


async def capture(home, learning, *, event, gateway, **_):
    """Capture transport provenance, but never treat this pre-auth hook as admission."""
    if getattr(event, 'internal', False) or getattr(event.source, 'is_bot', False):
        return
    source = event.source.to_dict()
    cfg = learning.extension('project_work').settings(home, learning)
    if source.get('chat_id') != cfg.get('channel_id') or source.get('user_id') not in learning.owners(home):
        return
    key = event_key(source, event.message_id)
    with _LOCK:
        _EVENTS[key] = (gateway, event, asyncio.get_running_loop(), time.time())
        for old in list(_EVENTS):
            if time.time() - _EVENTS[old][3] > 1800:
                _EVENTS.pop(old, None)
                _ADMITTED.pop(old, None)
        while len(_EVENTS) > 512:
            old = next(iter(_EVENTS))
            _EVENTS.pop(old, None)
            _ADMITTED.pop(old, None)


def admitted(home, learning, *, source, message_id, session_key, **_):
    with _LOCK:
        key = event_key(source, message_id)
        if key in _EVENTS:
            _ADMITTED[key] = session_key
            event = _EVENTS[key][1]
            from gateway.project_requests import record_request
            from gateway.project_threads import ProjectStore
            cfg = learning.extension('project_work').settings(home, learning)
            raw = getattr(event, 'raw_message', None)
            text = str(raw.get('text') or '') if isinstance(raw, dict) else event.text
            # Continuations keep the original receipt and author; no second author is invented.
            if not (event.metadata or {}).get('project_continuation'):
                event.source.message_id = str(message_id)
                store = ProjectStore(cfg['store_path'])
                record_request(store, event.source, text, source.get('profile') or 'default')
                origin = (event.metadata or {}).get('project_origin')
                if origin:
                    from gateway.project_threads import identity
                    project_id = identity(source['scope_id'], source['chat_id'], source['thread_id'], source['user_id'])
                    store.change(project_id, lambda data: dict(data, origin=origin))


def claim(store, key, task_id, turn, *, referral=None):
    """Atomically reserve once. Unknown attempts require inspection, never retry."""
    def update(data):
        task = next(t for t in data['tasks'] if t['id'] == task_id)
        if task['profile'] == turn['source']['profile'] or task['status'] != 'pending' or task.get('dispatch'):
            raise ValueError('다른 담당자의 미착수 단계만 한 번 실행할 수 있다.')
        if task['request_id'] != turn['source']['message_id']:
            raise ValueError('현재 요청에서 계획한 단계만 실행한다. 과거 승인·단계는 자동 재실행하지 않는다.')
        if not set(task['depends_on']) <= {t['id'] for t in data['tasks'] if dependency_ready(t)}:
            raise ValueError('선행 단계가 아직 완료되지 않았다.')
        if any(a.get('task_id') == task_id for a in data.get('approvals', [])):
            raise ValueError('승인 이력이 있는 단계는 기존 담당자의 승인 절차로 이어간다.')
        if task['profile'] == 'system-doctor':
            raise ValueError('진단담당 시스템 진단은 team_diagnosis_request 또는 실제 진단담당 호출로 이어간다.')
        if referral is not None:
            referral.consume(data, task, turn, 'dispatch')
        task['dispatch'] = {'status': 'running', 'from_profile': turn['source']['profile'],
                            'message_id': turn['source']['message_id'], 'started_at': time.time()}
        task.update(status='running', run=run_identity())
        return data
    return store.change(key, update)


class NotStarted(ValueError):
    """Preflight failed before the target handler was invoked."""


async def execute(host, original, profile, attachment_context=None):
    """Normal target authorization, runtime scope and tool guards remain in force."""
    try:
        from gateway.session_identity import canonical_identity
        if not host._running or getattr(host, '_draining', False) or getattr(host, '_external_drain_active', False):
            raise ValueError('게이트웨이가 새 실행을 받을 수 없다.')
        source = dataclasses.replace(original.source, profile=profile, role_authorized=False)
        adapters = host.adapters if profile == 'default' else host._profile_adapters.get(profile, {})
        adapter = adapters.get(source.platform)
        if adapter is None or canonical_identity(source, runner=host, adapter=adapter, transport_profile=profile) is None:
            raise ValueError('대상 담당자의 실제 연결을 확인하지 못했다.')
        if host._is_session_running(host._session_key_for_source(source)):
            raise ValueError('대상 담당자가 같은 스레드를 처리 중이다. 중복 실행하지 않는다.')
        if original.media_urls:
            if attachment_context is None:
                raise ValueError('현재 요청의 첨부 인계 경로를 확인하지 못했습니다.')
            attachments, source_home, target_home = attachment_context
            original = await asyncio.to_thread(attachments.forward, original, source_home, target_home)
    except Exception as exc:
        raise NotStarted(str(exc)) from exc
    event = dataclasses.replace(original, source=source, internal=False, allow_gateway_control=False,
                                metadata={**(original.metadata or {}), 'project_continuation': True})
    # Direct handler avoids a second public-router claim of the same Slack event.
    # It still runs the normal target auth, pause, session and tool authorization.
    return await host._make_profile_message_handler(profile)(event)


def dispatch(home, learning, store, key, args, turn):
    source = turn['source']
    event_source = {**source, 'user_id': source['owner_id']}
    event_id = event_key(event_source, source['message_id'])
    with _LOCK:
        captured = _EVENTS.get(event_id)
        admitted_key = _ADMITTED.get(event_id)
    if not captured or not admitted_key or time.time() - captured[3] > 1800:
        raise ValueError('현재 요청의 실제 게이트웨이 승인·연결을 확인하지 못했다.')
    host, event, loop, _ = captured
    if (event.metadata or {}).get('project_continuation'):
        raise ValueError('이어받은 담당자는 재위임하지 않고 배정 단계만 처리한다.')
    if event.text.lstrip().startswith('/') or loop.is_closed():
        raise ValueError('이 요청은 담당자 실행으로 넘길 수 없다.')
    referral = learning.extension('project_referral')
    declined = referral.confirm(home, learning, store, key, args, turn, 'dispatch')
    if declined:
        return declined
    attachment_context = None
    if getattr(event, 'media_urls', None):
        configured = learning.extension('project_work').settings(home, learning).get('profile_homes', {})
        proposed = next(t for t in store.get(key)['tasks'] if t['id'] == args['task_id'])
        if configured.get(source['profile']) != str(home) or not configured.get(proposed['profile']):
            raise ValueError('첨부 인계 담당자의 등록된 폴더를 확인하지 못했습니다.')
        attachment_context = (learning.extension('project_attachments'), home, configured[proposed['profile']])
    project = claim(store, key, args['task_id'], turn,
                    referral=referral if referral.enabled(home, learning) else None)
    task = next(t for t in project['tasks'] if t['id'] == args['task_id'])
    status, report, error = 'unknown', '', None
    coroutine = None
    future = None
    try:
        coroutine = execute(host, event, task['profile'], attachment_context)
        future = asyncio.run_coroutine_threadsafe(coroutine, loop)
        report = str(future.result(timeout=180) or '')[:12000]
        status = 'returned' if report else 'unknown'
    except BaseException as exc:
        error = exc
        if future is None:
            if coroutine is not None:
                coroutine.close()
            status = 'not_started'
        elif isinstance(exc, NotStarted):
            status = 'not_started'
        # Timeout or interruption after scheduling can follow an external write.
        report = '실행 결과 확인 필요: ' + type(exc).__name__
    def settle(data):
        current = next(t for t in data['tasks'] if t['id'] == task['id'])
        if status == 'not_started':
            current.pop('dispatch', None)
            current.pop('run', None)
            current.update(status='pending', next_step='실행 전 연결·첨부 조건을 확인한 뒤 다시 요청')
        else:
            current['dispatch'].update(status=status, report=report, returned_at=time.time())
            if current['status'] in {'pending', 'running'}:
                current.update(status='unknown', next_step='대상 담당자의 실행 기록과 원본 결과 확인')
        return data
    updated = store.change(key, settle)
    if error is not None:
        raise error
    current = next(t for t in updated['tasks'] if t['id'] == task['id'])
    return {'ok': True, 'status': status, 'profile': task['profile'], 'task_id': task['id'],
            'task_status': current['status'], 'report': report, 'approval_transferred': False,
            'notice': '담당자 답변은 보고다. 완료 여부는 해당 단계의 실제 도구 근거로 판정한다.'}


def active(project, source):
    return next((t for t in project['tasks'] if t['profile'] == source['profile'] and
                 t.get('dispatch', {}).get('status') == 'running' and
                 t['dispatch']['message_id'] == source['message_id']), None)
