"""Owner-started video jobs: session pattern grants until cancel/restart/6h.

No tool argument grants consent. The gateway event hook owns job creation;
terminal middleware only adds patterns after its existing sandbox/role guards.
"""
from pathlib import Path
import fcntl
import json
import logging
import threading
import time
from uuid import uuid4

TTL = 6 * 60 * 60
LOCK = threading.RLock()
JOBS = {}
LOG = logging.getLogger(__name__)


def _approval():
    from tools import approval
    # Private revocation ABI: refuse new grants if it is no longer available.
    lock = getattr(approval, '_lock', None)
    if not (isinstance(getattr(approval, '_session_approved', None), dict)
            and hasattr(lock, '__enter__') and hasattr(lock, '__exit__')
            and callable(getattr(approval, '_floor_block', None))):
        raise RuntimeError('video_job approval revocation ABI unavailable')
    return approval


def _record(job, action, keys=(), call_id='', reason=''):
    path = job['home'] / 'logs/tool-policy.jsonl'
    path.parent.mkdir(parents=True, exist_ok=True)
    row = dict(at=time.time(), reason='video_job', action=action, job_id=job['id'],
               session_key=job['session'], keys=sorted(keys), call_id=call_id, detail=reason)
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with path.open('a') as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')


def _close(key, reason):
    job = JOBS.get(key)
    if job is None:
        return
    if job['keys']:
        approval = _approval()
        with approval._lock:
            values = approval._session_approved.get(key)
            if values is not None:
                values.difference_update(job['keys'])
                if not values:
                    approval._session_approved.pop(key, None)
    JOBS.pop(key, None)
    job['timer'].cancel()
    _record(job, 'revoke', job['keys'], reason=reason)


def expire():
    """Also checked synchronously before tool guards; timer handles idle jobs."""
    with LOCK:
        now = time.monotonic()
        for key, job in list(JOBS.items()):
            if now >= job['until']:
                _close(key, 'expired')


def _timeout(key, job_id):
    try:
        with LOCK:
            if key in JOBS and JOBS[key]['id'] == job_id:
                _close(key, 'expired')
    except Exception:
        LOG.exception('video_job expiration failed')


def _owner(home):
    import slack_report
    return slack_report._sender().owner(home)


async def dispatch(home, event=None, session_store=None, **_):
    """Authenticated Slack source, not model history or a supplied tool flag."""
    from hermes_constants import get_hermes_home
    home = Path(home).resolve()
    source = getattr(event, 'source', None)
    if (home.name != 'personal-secretary' or Path(get_hermes_home()).resolve() != home
            or getattr(event, 'internal', False) or getattr(source, 'is_bot', False)
            or getattr(source, 'profile', None) not in (None, 'personal-secretary')
            or getattr(getattr(source, 'platform', None), 'value', '') != 'slack'):
        return
    owner = _owner(home)
    if not owner or getattr(source, 'user_id', '') != owner:
        return
    text = str(getattr(event, 'text', '') or '').strip()
    if text not in ('제작해', '취소'):
        return
    # Same SessionStore method used by routing, including workspace/profile and
    # per-user/thread isolation settings. No hand-built approximation of keys.
    key = session_store._generate_session_key(source)
    if not key or not source.chat_id:
        raise RuntimeError('video_job session key unavailable')
    with LOCK:
        expire()
        _close(key, 'cancel' if text == '취소' else 'new_job')
        if text == '취소':
            return
        _approval()
        job_id = uuid4().hex
        timer = threading.Timer(TTL, _timeout, args=(key, job_id))
        timer.daemon = True
        job = dict(id=job_id, session=key, home=home, owner=owner,
                   chat=source.chat_id, thread=source.thread_id or '',
                   until=time.monotonic() + TTL, keys=set(), timer=timer)
        _record(job, 'open')
        JOBS[key] = job
        timer.start()


def grant(td, original, wrapped, context):
    """Called only with the freshly generated sandbox wrapper, before core guards.

    Pattern grants intentionally live for the job, including overlapping calls.
    Previously approved patterns are never owned/revoked by this plugin. If a
    human later approves a plugin-owned key too, job closure removes that key
    (the core set cannot distinguish origins); future use asks again.
    """
    expire()
    ss = td.selected_skills
    if (td.personal_roles.profile(td) != 'personal-secretary' or not ss.enabled(td)
            or not ss.owner_chat(td) or ss.problem(td, 'terminal', original)):
        return
    try:
        approval = _approval()
    except RuntimeError:
        LOG.warning('video_job disabled: approval revocation ABI unavailable')
        return
    key = approval.get_current_session_key(default='')
    with LOCK:
        job = JOBS.get(key)
        if not job:
            return
        if (td.get_session_env('HERMES_SESSION_USER_ID', '') != job['owner']
                or td.get_session_env('HERMES_SESSION_CHAT_ID', '') != job['chat']
                or td.get_session_env('HERMES_SESSION_THREAD_ID', '') != job['thread']):
            raise RuntimeError('video_job source/session mismatch')
        job_id = job['id']
    # Verify middleware passed the final policy-owned command, never a model's
    # claimed wrapper. Scanning does not execute the command or request consent.
    if wrapped['command'] != ss.wrap(td, original)['command']:
        raise RuntimeError('video_job sandbox wrapper mismatch')
    # sandbox-exec's quoted -c payload hides some anchored hardline patterns
    # (e.g. rm -rf /) from the core's outer-command scan. Preserve ALL floors
    # on the original payload before adding recoverable pattern approvals.
    if approval._floor_block(original['command'], sudo_guard=True) is not None:
        raise RuntimeError('video_job hardline/deny floor blocked command')
    dangerous, pattern, _ = approval.detect_dangerous_command(wrapped['command'])
    scan = approval._tirith_scan(wrapped['command'])
    keys = {pattern} if dangerous and pattern else set()
    if scan['action'] in ('warn', 'block'):
        findings = scan.get('findings') or []
        keys.add('tirith:' + str(findings[0].get('rule_id', 'unknown') if findings else 'unknown'))
    with LOCK:
        expire()
        job = JOBS.get(key)
        # A cancel/new job while scanning must not authorize an old call.
        if not job or job['id'] != job_id:
            return
        for pattern in sorted(keys):
            if not approval.is_approved(key, pattern):
                # Own the key before granting, so partial failures remain revocable.
                job['keys'].add(pattern)
                approval.approve_session(key, pattern)
                _record(job, 'grant', [pattern], str(context.get('tool_call_id') or ''))
