"""Owner-requested read-only reports. No scheduler, mutation or external posting."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from functools import lru_cache

PROFILES = {
    'work-assistant': ('table_analyze', 'doc_compare', 'work_crosscheck', 'mail_waiting'),
    'sns-manager': ('consultation_report', 'store_guidance_check'),
    'personal-secretary': ('work_crosscheck', 'mail_waiting'),
}
MODULES = {'work-assistant': 'document_review', 'sns-manager': 'consultation_review', 'personal-secretary': 'personal_review'}
NOTES = {
    'work-assistant': '회사 업무의 날짜·상태 대조는 work_crosscheck, 회신 후보는 mail_waiting으로 현재 근거만 읽는다. 예약·배경에서는 이 두 도구를 쓰지 않는다. 표의 누락·중복·값 차이·집계는 table_analyze, 문서 두 버전의 본문 차이는 doc_compare로 확인한다. 원문 파일과 셀·문단·쪽 위치를 보고한다. 수식은 계산하지 않고 원문 수식으로 비교하며 숫자 집계에서 제외한다. 일부만 읽으면 전체 동일·전체 합계로 단정하지 않는다.',
    'sns-manager': '상담 집계는 consultation_report(start,end), 매장 안내 대조는 store_guidance_check(query)로 확인한다. 두 채널은 고객을 합치지 않으며 기록 단위와 집계 기간을 밝힌다. 안내 차이는 충돌 후보이며 적용 날짜·대상을 원문에서 확인한다. 이 도구는 고객에게 보내거나 규칙을 학습하지 않는다.',
    'personal-secretary': '관련 업무의 날짜·상태 대조는 먼저 기존 읽기 도구로 해당 원문을 조회한 뒤 work_crosscheck(action=list)로 이번 요청의 근거 번호와 JSON 위치를 얻고 compare로 실제 값을 대조한다. 메일 본문 속 날짜는 정확한 인용을 지정할 수 있다. 값 차이는 충돌 후보이며 같은 업무·같은 의미인지 확인한다. 회신 대기는 mail_waiting(start,end)로 보낸 메일의 현재 타래를 읽는다. 회신 미발견은 업무 미완료나 독촉 발송 승인이 아니다.',
}


@lru_cache(maxsize=8)
def module(name):
    spec = importlib.util.spec_from_file_location('team_review_' + name, Path(__file__).with_name(name + '.py'))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def tools_for(config, profile):
    names=PROFILES.get(profile, ())
    return names + ('table_analyze','doc_compare') if profile=='personal-secretary' and config.get('secretary_tools') is True else names


def module_for(profile, name):
    return module('personal_review' if name in {'work_crosscheck', 'mail_waiting'} else 'document_review' if name in {'table_analyze','doc_compare'} else MODULES[profile])


def handle(config, home, name, args, *, kind, session_id='', turn_id='', files_config=None, learning=None, **_):
    try:
        profile = Path(home).name
        if config.get('enabled') is not True or name not in tools_for(config, profile):
            raise ValueError('review_tool_disabled_or_wrong_profile')
        consultation = consult_access(home, learning)
        if consultation:
            blocked = learning.extension('project_consult').guard(home, learning, name, args)
            if blocked:
                raise ValueError(blocked['message'])
        if kind != 'chat' and not consultation:
            raise ValueError('review_requires_owner_chat')
        # Transport identity, including background flags, is checked at execution too.
        if not consultation:
            module('mail_read').authorize(home)
        from gateway.session_context import get_session_env
        if not consultation and get_session_env('HERMES_SESSION_PROFILE', '') != profile:
            raise ValueError('review_profile_mismatch')
        if not consultation and not get_session_env('HERMES_SESSION_MESSAGE_ID', ''):
            raise ValueError('current_owner_message_required')
        target = module_for(profile, name)
        schema = target.SCHEMAS[name]['parameters']
        if not isinstance(args, dict) or set(args) - set(schema['properties']) or set(schema.get('required', [])) - set(args):
            raise ValueError('invalid_review_arguments')
        evidence = module('review_evidence').snapshot(home, session_id, turn_id) if name in {'work_crosscheck', 'store_guidance_check'} else None
        result = target.handle(name, args, home=Path(home), session_id=session_id, files_config=files_config, evidence=evidence)
        return json.dumps(result, ensure_ascii=False, default=str, allow_nan=False)
    except Exception as exc:
        # API/library exceptions can contain credentials or raw customer data.
        error = str(exc) if type(exc) is ValueError else type(exc).__name__
        return json.dumps({'ok': False, 'error': error[:250]}, ensure_ascii=False)


def consult_access(home, learning):
    if learning is None:
        return False
    worker = learning.extension('project_consult').worker(home, learning)
    return bool(worker and worker[1]['profile'] == 'work-assistant' and Path(home).name == 'work-assistant')


def filter_request(config, profile, request, *, kind, home=None, learning=None):
    consultation = consult_access(home, learning) if home is not None else False
    allowed = set(tools_for(config, profile)) if config.get('enabled') is True and (kind == 'chat' or consultation) else set()
    all_names = {name for names in PROFILES.values() for name in names}
    tools = request.get('tools')
    if not isinstance(tools, list):
        return None
    filtered = [t for t in tools if t.get('function', {}).get('name', t.get('name')) not in all_names - allowed]
    return {'request': dict(request, tools=filtered)} if len(filtered) != len(tools) else None
