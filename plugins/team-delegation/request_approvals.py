"""One human approval for an exact, bounded list of writes in one authenticated owner request."""
from __future__ import annotations
from copy import deepcopy
import hashlib
import json
import threading
import time
import sys
from types import SimpleNamespace

_SHARED = sys.modules.setdefault('_hermes_request_write_permits', SimpleNamespace(lock=threading.RLock(), plans={}))
LOCK, PLANS = _SHARED.lock, _SHARED.plans
WRITES = {'personal_work_bundle', 'gmail_direct', 'calendar_direct_mutation', 'reminders_direct_mutation',
          'apple_notes_direct_mutation', 'contacts_direct_mutation', 'google_calendar_color_set', 'notion',
          'file_move', 'file_copy', 'folder_create', 'doc_edit', 'pdf_pages', 'doc_convert', 'doc_fill',
          'write_file', 'patch', 'file_trash', 'file_link'}
NOTE = ('[요청당 승인 한 번] 한 요청 안 승인 대상 쓰기가 여러 개면 첫 도구 인자의 request_plan에 '
        '[{tool: 도구명, arguments: 정확한 인자}] 전체 계획을 담는다. 메일 수신자·본문·첨부와 삭제 대상도 포함한다. '
        '첫 버튼 승인 후 같은 요청의 계획 안 쓰기는 다시 묻지 않는다. 계획 밖 새 쓰기만 새 계획으로 한 번 승인받는다. '
        '할 일 등록의 작성 선택 후 생성은 재승인 없이 실행한다. 승인을 요청했다는 사실은 승인받은 것이 아니다.')
PLAN_SCHEMA = {'type': 'array', 'maxItems': 100, 'items': {'type': 'object', 'required': ['tool', 'arguments'],
    'properties': {'tool': {'type': 'string', 'enum': sorted(WRITES)}, 'arguments': {'type': 'object'}},
    'additionalProperties': False}, 'description': '이번 요청에서 승인받을 전체 정확한 쓰기 목록. 첫 승인에만 제출.'}


def digest(tool, args):
    clean = {k: v for k, v in args.items() if k not in {'approved', 'request_plan'}}
    return hashlib.sha256(json.dumps([tool, clean], ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def scope(td, context):
    from tools.approval_context import _approval_session_id, _approval_turn_id
    session = str(context.get('session_id') or _approval_session_id.get() or '')
    turn = str(context.get('turn_id') or _approval_turn_id.get() or '')
    if not session or not turn or td.personal_scopes.background(td):
        return None
    if (td.get_session_env('HERMES_SESSION_PLATFORM', '') not in {'slack', 'photon'}
            or not td.get_session_env('HERMES_SESSION_USER_ID', '')
            or not td.get_session_env('HERMES_SESSION_CHAT_ID', '')):
        return None
    return (td.personal_roles.profile(td), session, turn)


def prune():
    for key, row in list(PLANS.items()):
        if time.monotonic() - row['at'] > 3600:
            PLANS.pop(key, None)


def allowed(td, tool, args, context):
    identity = scope(td, context)
    if not identity or tool not in WRITES or td._personal_role_problem(tool, args):
        return False
    wanted = digest(tool, args)
    with LOCK:
        prune()
        return any(row['scope'] == identity and row['approved'] and wanted in row['digests']
                   and wanted not in row['used'] for row in PLANS.values())


def gate(td, tool, args, decision, context):
    plan = args.get('request_plan')
    if not plan or not decision or decision.get('action') != 'approve':
        return decision
    identity = scope(td, context)
    if not identity:
        return {'action': 'block', 'message': '요청별 승인에는 인증된 소유자 대화와 현재 요청 ID가 필요합니다.'}
    if (not isinstance(plan, list) or not 1 <= len(plan) <= 100 or
            any(not isinstance(row, dict) or row.get('tool') not in WRITES or not isinstance(row.get('arguments'), dict) for row in plan)):
        return {'action': 'block', 'message': '전체 쓰기 계획의 도구·정확한 인자가 올바르지 않습니다.'}
    digests = {digest(row['tool'], row['arguments']) for row in plan}
    if digest(tool, args) not in digests:
        return {'action': 'block', 'message': '현재 쓰기가 전체 승인 계획에 없습니다.'}
    key = 'request-writes:'+hashlib.sha256(json.dumps([identity, sorted(digests)]).encode()).hexdigest()
    with LOCK:
        prune()
        existing = PLANS.get(key)
        if existing:
            return {'action': 'block', 'message': '이 계획은 이미 승인을 요청했거나 실행했습니다. 결과를 확인하고 자동 재실행하지 않습니다.'}
        PLANS[key] = {'scope': identity, 'digests': digests, 'used': set(), 'approved': False, 'at': time.monotonic()}
    return {'action': 'approve', 'rule_key': key, 'message': '승인하면 이번 요청의 전체 계획을 한 번에 실행합니다:\n'+
            '\n'.join(f'{i}. {row["tool"]}: '+json.dumps(row['arguments'], ensure_ascii=False, sort_keys=True)
                      for i, row in enumerate(plan, 1))}


def response(pattern_key='', choice='', **_):
    key = str(pattern_key).removeprefix('plugin_rule:')
    with LOCK:
        prune()
        if key in PLANS:
            PLANS[key]['approved'] = choice in {'once', 'session', 'always'}


def consume(td, tool, args, context):
    identity, wanted = scope(td, context), digest(tool, args)
    with LOCK:
        for row in PLANS.values():
            if row['scope'] == identity and row['approved'] and wanted in row['digests'] and wanted not in row['used']:
                row['used'].add(wanted)
                return True
    return False


def ended(*, session_id='', turn_id='', **_):
    with LOCK:
        for key, row in list(PLANS.items()):
            if row['scope'][1:] == (session_id, turn_id) or (turn_id and row['scope'][2] == turn_id):
                PLANS.pop(key, None)


def schemas(request):
    result = deepcopy(request)
    if not isinstance(result.get('tools'), list):
        return result
    for tool in result.get('tools') or []:
        fn = tool.get('function', tool)
        if fn.get('name') in WRITES:
            params = fn.get('parameters') or {}
            params.setdefault('properties', {})['request_plan'] = PLAN_SCHEMA
    return result
