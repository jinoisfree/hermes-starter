"""Read gateway-authenticated captions; document text cannot impersonate an owner."""
from pathlib import Path


def current(home, learning):
    from gateway.session_context import get_session_env as env
    from gateway.project_threads import ProjectStore, identity
    cfg = learning.extension('memory_recall').config(home).get('projects', {})
    if cfg.get('enabled') is not True or env('HERMES_SESSION_PLATFORM', '') != 'slack':
        return None
    fields = [env('HERMES_SESSION_' + name, '') for name in ('SCOPE_ID', 'CHAT_ID', 'THREAD_ID', 'USER_ID')]
    message = env('HERMES_SESSION_MESSAGE_ID', '')
    if not all(fields) or not message or fields[1] != cfg.get('channel_id'):
        return None
    path = cfg.get('store_path', '')
    if not path or not Path(path).is_file():
        return None
    data = ProjectStore(path).get(identity(*fields)) or {}
    record = data.get('requests', {}).get(message)
    if not record:
        return None
    if record.get('owner_id') != fields[3] or record.get('message_id') != message or record.get('truncated'):
        raise ValueError('The original owner request cannot be fully verified.')
    return {**record, 'chat_id': fields[1], 'thread_id': fields[2]}


def verified(home, learning, row, session_id):
    record = current(home, learning)
    if not record:
        return None
    if (not row or row[0] != 'slack' or str(row[8] or '') != record['message_id']
            or row[2] != record['chat_id'] or row[4] != record['thread_id']):
        raise ValueError('The persisted message does not match the authenticated transport request.')
    if record['owner_id'] not in learning.owners(home):
        raise ValueError('Only the configured Slack owner may teach rules.')
    return {'text': record['text'], 'user_id': record['owner_id'], 'chat_id': row[2],
            'chat_type': row[3], 'thread_id': row[4], 'at': row[6], 'message_id': row[7],
            'platform_message_id': row[8], 'session_id': session_id}


def text(home, learning, fallback):
    record = current(home, learning)
    return record['text'] if record else learning.extension('work_runtime').clean_request(fallback)
