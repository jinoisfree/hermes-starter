"""Short-lived, turn-scoped read receipts. Never persisted or shared across conversations."""
from collections import OrderedDict
import copy
import json
import threading
import time

READS = {'contacts_direct_lookup', 'calendar_direct_lookup', 'reminders_direct_lookup', 'apple_notes_direct_lookup',
         'gmail_direct', 'notion', 'instagram_dm_guide'}
_DATA = OrderedDict()
_LOCK = threading.RLock()
TTL = 600


def observe(home, *, parser, session_id='', turn_id='', tool_name='', tool_call_id='', result=None, args=None, **_):
    if tool_name not in READS or not session_id or not turn_id or not tool_call_id:
        return
    args = args if isinstance(args, dict) else {}
    if tool_name == 'gmail_direct' and args.get('action') not in {'lookup', 'read', 'thread'}:
        return
    if tool_name == 'notion' and args.get('action') not in {'databases', 'schema', 'query', 'page', 'search', 'users'}:
        return
    # Imported by the same registration module, so no duplicate in-memory store.
    data = parser(tool_name, result)
    if not isinstance(data, dict) or data.get('ok') is not True or data.get('error'):
        return
    raw = json.dumps(data, ensure_ascii=False)
    if len(raw) > 250000:
        return
    key = (str(home), str(session_id), str(turn_id))
    now = time.monotonic()
    with _LOCK:
        for old in list(_DATA):
            if now - _DATA[old]['at'] > TTL:
                del _DATA[old]
        turn = _DATA.setdefault(key, {'at': now, 'items': OrderedDict()})
        turn['at'] = now
        turn['items'][str(tool_call_id)] = {'tool': tool_name, 'data': copy.deepcopy(data),
                                          'checked_at': time.time(), 'arguments': copy.deepcopy(args)}
        while len(turn['items']) > 12:
            turn['items'].popitem(last=False)
        _DATA.move_to_end(key)
        while len(_DATA) > 32:
            _DATA.popitem(last=False)


def snapshot(home, session_id, turn_id):
    with _LOCK:
        turn = _DATA.get((str(home), str(session_id), str(turn_id)))
        if not turn or time.monotonic() - turn['at'] > TTL:
            raise ValueError('no_current_read_evidence_read_originals_in_this_turn_first')
        return copy.deepcopy(turn['items'])
