"""Short-lived, runner-owned capability for one read-only organization review."""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import time

RUN_ENV = 'HERMES_ORG_REVIEW_RUN'
TOKEN_ENV = 'HERMES_ORG_REVIEW_TOKEN'
ROOT = Path.home() / '.hermes'
TTL = 960
# Conversation-bearing tools cannot preserve the report's private-conversation
# exclusions. Keep these unavailable in this runner; ordinary diagnosis is unchanged.
PRIVATE_READS = {'diagnosis_slack_read', 'diagnosis_case_read', 'diagnosis_state_read',
                 'diagnosis_records_read', 'diagnosis_log_patterns_read', 'diagnosis_recurrence_read', 'session_search',
                 'request_usage_lookup', 'gateway_runtime_snapshot', 'diagnosis_runs_read'}


def folder():
    return ROOT / 'profiles/system-doctor/state/org-review-runs'


@contextmanager
def capability():
    base = folder()
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    token = secrets.token_hex(32)
    identifier = secrets.token_hex(16)
    path = base / (identifier + '.json')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as f:
        json.dump({'capability_hash': hashlib.sha256(token.encode()).hexdigest(),
                   'started_at': time.time(), 'status': 'running'}, f)
    try:
        yield {RUN_ENV: identifier, TOKEN_ENV: token}
    finally:
        path.unlink(missing_ok=True)


def valid_run():
    from gateway.session_context import get_session_env as env
    import re
    if not os.environ.get(RUN_ENV):
        return False
    identifier = os.environ[RUN_ENV]
    if not re.fullmatch('[0-9a-f]{32}', identifier):
        raise ValueError('Invalid organization review capability')
    if ((env('HERMES_SESSION_PROFILE', '') or os.environ.get('HERMES_PROFILE')) != 'system-doctor'
            or os.environ.get('HERMES_KANBAN_TASK') or os.environ.get('HERMES_DIAGNOSIS_REQUEST')
            or env('HERMES_CRON_SESSION', '') not in ('', '0', 'false')
            or env('HERMES_SINGLE_QUERY_SESSION', '').lower() not in ('1', 'true', 'yes')):
        raise ValueError('Organization review requires system-doctor oneshot')
    path = folder() / (identifier + '.json')
    if path.resolve() != path.absolute():
        raise ValueError('Symlink capability denied')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as f:
        info = os.fstat(f.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('Unsafe capability file')
        record = json.load(f)
    digest = hashlib.sha256(os.environ.get(TOKEN_ENV, '').encode()).hexdigest()
    if (record.get('status') != 'running' or not secrets.compare_digest(record.get('capability_hash', ''), digest)
            or not 0 <= time.time() - record.get('started_at', 0) <= TTL):
        raise ValueError('Expired or invalid organization review capability')
    import hermes_yaml as yaml
    cfg = yaml.safe_load((ROOT / 'profiles/system-doctor/config.yaml').read_text())
    settings = cfg.get('plugins', {}).get('entries', {}).get('team-kit', {}).get('settings', {})
    if not settings.get('diagnosis', {}).get('enabled') or not settings.get('org_review', {}).get('enabled'):
        raise ValueError('Organization review or diagnosis is disabled')
    return True
