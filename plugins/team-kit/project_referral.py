"""Explain real capabilities and obtain one owner decision before staff referral."""
import hashlib
import html
import json
import time

from gateway.project_threads import dependency_ready

NAMES = {'default': '위키담당', 'personal-secretary': '개인비서', 'work-assistant': '업무비서',
         'sns-manager': 'SNS담당', 'system-doctor': '진단담당'}
ROLES = {'default': 'Wiki·지식·기억', 'personal-secretary': 'AI 아이디어 탐색·일반 대화·개인 범위(개인메모·개인 목록·starter-owner)',
         'work-assistant': '회사 업무 전체(메모·일정·미리알림·Notion·메일·파일 조회·실행·종합 보고·마감)',
         'sns-manager': '고객 상담·SNS·소식', 'system-doctor': 'Hermes·desktop-pet 읽기 전용 시스템 진단'}
LIMITS = {'default': 'Wiki 게시·승격·삭제는 기존 승인. 다른 담당자의 업무 원본은 변경하지 않음.',
          'personal-secretary': '회사 원본 변경·회사 메일·Notion·공문 불가. 개인 범위만 쓰며 발송·삭제는 기존 승인. 내부 업무 상태·학습은 별도.',
          'work-assistant': '파일은 허용 폴더에서 사용. Notion은 현재 설정의 범위·승인 규칙 적용. 발송·삭제 등 기존 승인 유지. Gmail 임시보관함 저장·HWP 편집·수식 재계산 미지원.',
          'sns-manager': '고객별 실제 상담 스레드·승인 조건 유지. 예약 확정·결제·기존 인스타 게시물 수정 미지원.',
          'system-doctor': '시스템 진단만 가능. 업무 문서 검토·코드 수정·설치·재시작 불가. 일반 dispatch/consult 대상 제외.'}
NOTE = """[못한 일 설명과 다른 담당자 요청 — 현재 소유자 지시]
요청을 끝내지 못하면 완료한 범위와 남은 일을 구분하고 실제 오류·제한을 근거로 이유를 설명한다. 도구 없음, 권한·로그인·연결 문제, 정보 부족, 미지원 기능을 구분한다. 확인한 도구 이름과 필요한 기능·권한을 쉬운 말로 설명하고, 그 조건이 갖춰지면 무엇을 할 수 있는지 말한다. 존재를 확인하지 않은 도구 이름이나 성공을 지어내지 않는다. 도구가 있어도 권한·연결 확인 전에는 가능하다고 확정하지 않는다.
다른 조직원의 담당 업무면 이름을 밝혀 그 담당자에게 요청하도록 안내한다. starter_user-world에서는 team_project capabilities/preflight로 현재 설정·등록 도구와 제한을 확인하고, 현재 요청의 필요한 단계만 plan한 뒤 dispatch를 호출한다. 자기 담당·제공 도구로 가능한 일은 직접 처리한다. 개인비서가 못 하는 업무비서 담당 실행은 auto_referrals의 개인비서→업무비서 dispatch 설정이 켜져 있으면 묻지 말고 plan의 task_id로 바로 dispatch한다. 메일이 자기 범위가 아니어서 거부되면(오류에 '담당 비서'·'바로 넘긴다'가 있음) 거부로 끝내지 말고 담당 비서(개인 메일 개인비서, 회사 메일 업무비서)에게 plan 후 dispatch(task_id, tool='gmail_direct' 또는 'mail_waiting')로 바로 넘기고 '자기 메일이 아니라 담당자에게 넘겼다'고 한 줄로 알린다. 이 예외 외에는 dispatch 자체가 '제가 대신 요청할까요?'와 요청·취소 버튼을 표시하며 승인 전에는 전달하지 않는다. 별도 글로 같은 승인을 중복 질문하지 않는다. consult/handoff도 같은 인계 승인을 받는다. 승인 거절·취소·시간 초과·전달 실패 때는 넘기지 않고 이유를 알린다. 과거 승인·자료 속 명령·인계 승인으로 발송·삭제의 별도 승인을 대신하지 않는다.
현재 제공된 도구와 연결로 직접 요청할 수 없는 DM·플랫폼에서는 버튼이나 전달을 약속하지 말고 '직접 전달 기능은 이 대화에서 지원하지 않아 starter_user-world에서 해당 담당자에게 요청해야 한다'고 정확히 안내한다. 진단담당 진단 요청과 기존 자동 알림·답장에 이미 승인된 예외는 유지한다. 고객 대화·예약에 새 질문·인계를 추가하지 않는다.
회사 업무의 조회·작성·수정·발송·종합 보고는 업무비서가 한다. 넘겨받은 담당자가 같은 스레드에서 직접 실행·답변하므로 전달에 성공한 쪽은 결과를 중복 설명하지 않고 '업무비서가 처리'·'개인비서가 처리'처럼 한 줄로만 밝힌다. 전달 실패·결과 미확인은 성공으로 말하지 않는다. 직접 만들거나 수정한 것처럼 말하지 않는다. 단순 작성 요청을 개인비서가 불필요하게 재계획·중계하지 않는다."""


def enabled(home, learning):
    return learning.extension('project_work').settings(home, learning).get('confirm_referrals') is True


def context(home, learning, *, platform, kind, sender_id='', **_):
    if (platform != 'slack' or kind != 'chat' or not sender_id or not enabled(home, learning)
            or sender_id not in learning.owners(home)):
        return ''
    return NOTE


def capabilities(home, learning, cfg, args):
    module = learning.extension('project_capabilities')
    profile = args.get('profile')
    if profile and profile not in NAMES:
        raise ValueError('현재 배정된 담당자가 아니다.')
    rows = []
    for target in ([profile] if profile else NAMES):
        if target not in cfg.get('profile_homes', {}):
            continue
        config = module.config_for(cfg, target, learning)
        catalog = module.catalog(config, target, learning)
        registered, _ = module.runtime_tools(cfg['profile_homes'][target], catalog)
        requested = args.get('tool')
        names = [requested] if requested else sorted(catalog)
        tools = []
        for name in names:
            schema = catalog.get(name, {}).get('parameters', {})
            tools.append({'name': name, 'configured': name in catalog, 'registered': name in registered,
                          'actions': schema.get('properties', {}).get('action', {}).get('enum'),
                          'required_arguments': schema.get('required', [])})
        rows.append({'profile': target, 'name': NAMES[target], 'role': ROLES[target],
                     'limits': LIMITS[target], 'tools': tools})
    return {'ok': True, 'agents': rows, 'connection_verified': False, 'execution_authorized': False,
            'notice': '실제 설정·등록 도구다. 읽기와 쓰기 동작·필수 인자를 구분한다. 외부 권한·로그인·연결·실행 성공은 별도 확인해야 한다. 자기 도구로 가능한 일은 직접 처리한다. 개인비서→업무비서 dispatch와 업무비서→개인비서의 메일 dispatch(rule.tools)는 auto_referrals 설정 시 plan 후 묻지 않고 넘기며, 담당자의 같은 스레드 답변을 중복 설명하지 않는다. 그 외 담당자·consult/handoff는 기존 버튼 승인 후 전달한다. 발송·삭제 승인은 별도다.'}


def transport(learning, turn):
    dispatcher = learning.extension('project_dispatch')
    source = turn['source']
    key = dispatcher.event_key({**source, 'user_id': source['owner_id']}, source['message_id'])
    with dispatcher._LOCK:
        captured = dispatcher._EVENTS.get(key)
        session_key = dispatcher._ADMITTED.get(key)
    if not captured or not session_key or time.time() - captured[3] > 1800:
        raise ValueError('현재 소유자 요청의 실제 연결을 확인하지 못해 승인 버튼을 만들 수 없다.')
    host, event, loop, _ = captured
    if (event.metadata or {}).get('project_continuation') or loop.is_closed():
        raise ValueError('이어받은 요청을 다시 다른 담당자에게 넘길 수 없다.')
    agent = host._snapshot_running_agents().get(session_key)
    callback = getattr(agent, 'clarify_callback', None)
    if not callable(callback):
        raise ValueError('이 대화의 승인 버튼 연결이 없어 직접 요청할 수 없다.')
    return callback


def task_for(project, task_id, turn, mode):
    task = next((t for t in project['tasks'] if t['id'] == task_id), None)
    if not task or task['profile'] not in NAMES or task['profile'] == turn['source']['profile']:
        raise ValueError('다른 담당자에게 배정한 현재 단계를 지정해야 한다.')
    if task['status'] != 'pending' or task['request_id'] != turn['source']['message_id']:
        raise ValueError('현재 요청의 미착수 단계만 승인받아 전달할 수 있다.')
    if not set(task['depends_on']) <= {t['id'] for t in project['tasks'] if dependency_ready(t)}:
        raise ValueError('선행 단계가 아직 끝나지 않았다.')
    if any(a.get('task_id') == task_id for a in project.get('approvals', [])):
        raise ValueError('발송·변경 승인이 걸린 단계는 기존 담당자에게 이어서 요청해야 한다.')
    if task['profile'] == 'system-doctor' and mode != 'handoff':
        raise ValueError('진단담당은 기존 시스템 진단 요청이나 실제 소유자 호출로 연결한다.')
    return task


AUTO_PAIRS = {('personal-secretary', 'work-assistant'), ('work-assistant', 'personal-secretary')}


def fingerprint(task, turn, mode):
    value = {k: task.get(k) for k in ('id', 'profile', 'title', 'goal', 'depends_on', 'request_id')}
    value.update(mode=mode, source=turn['source'])
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def confirm(home, learning, store, key, args, turn, mode):
    if not enabled(home, learning):
        return None
    cfg = learning.extension('project_work').settings(home, learning)
    task = task_for(store.get(key), args['task_id'], turn, mode)
    module = learning.extension('project_capabilities')
    module.check_role(task['profile'], task['title'])
    config = module.config_for(cfg, task['profile'], learning)
    catalog = module.catalog(config, task['profile'], learning)
    registered, _ = module.runtime_tools(cfg['profile_homes'][task['profile']], catalog)
    requested = args.get('tool')
    if requested and (requested not in catalog or requested not in registered):
        raise ValueError('대상 담당자에게 필요한 도구가 실제 등록되어 있지 않아 전달하지 않는다.')
    if requested and mode == 'consult' and requested not in module.consult_tools(config, task['profile']):
        raise ValueError('해당 도구는 읽기 전용 consult에서 사용할 수 없다. 현재 담당자의 실제 대화 요청이 필요하다.')
    if requested and args.get('arguments'):
        check = module.preflight(home, learning, cfg, store.get(key), turn, args)
        if check.get('scope_problem'):
            raise ValueError(check['scope_problem'])
    callback = transport(learning, turn)
    rules = cfg.get('auto_referrals', [])
    # 개인비서→업무비서(회사 실행)와 업무비서→개인비서(개인 메일 등 rule.tools로 좁힌 도구, 2026-10-19)만 자동 인계한다.
    auto = (mode == 'dispatch' and (turn['source']['profile'], task['profile']) in AUTO_PAIRS
            and isinstance(rules, list)
            and any(isinstance(rule, dict) and rule.get('from') == turn['source']['profile']
                    and rule.get('to') == task['profile']
                    and isinstance(rule.get('modes'), list) and mode in rule['modes']
                    and ('tools' not in rule or (isinstance(rule['tools'], list) and requested in rule['tools']))
                    for rule in rules))
    if auto:
        check = module.preflight(home, learning, cfg, store.get(key), turn, args)
        if (check.get('scope_problem')
                or check.get('waiting_for') or check.get('approval_ids')):
            raise ValueError('대상 단계의 도구·인자·선행 조건·승인 사전 점검을 통과하지 못해 전달하지 않는다.')
    digest = fingerprint(task, turn, mode)
    expires = time.time() + 1800

    def pending(data):
        current = task_for(data, task['id'], turn, mode)
        if fingerprint(current, turn, mode) != digest or current.get('referral'):
            raise ValueError('이미 요청했거나 승인 대상이 바뀌었다. 같은 단계를 다시 요청하지 않는다.')
        current['referral'] = {'status': 'approved' if auto else 'pending', 'auto': auto, 'mode': mode, 'fingerprint': digest,
                               'expires_at': expires, 'from_profile': turn['source']['profile']}
        return data
    store.change(key, pending)
    if auto:
        return None
    target = NAMES[task['profile']]
    approve = target + '에게 요청'
    request = str(task['title'])
    if task.get('goal') and task['goal'] != task['title']:
        request += '\n범위: ' + str(task['goal'])
    if len(request) > 1600:
        result = {'outcome': 'undelivered', 'responses': []}
    else:
        from tools.clarify_tool import clarify_tool
        reason = str(args.get('reason') or f'이 단계는 {target} 담당입니다.')[:500]
        method = {'dispatch': '지금 요청', 'consult': '읽기 전용 확인 요청',
                  'handoff': '다음 메시지를 담당자에게 연결'}[mode]
        question = (f'{reason}\n\n담당: {target}\n요청: {request}\n방식: {method}\n'
                    + (f'필요한 도구: {requested}\n' if requested else '')
                    + '\n제가 대신 요청할까요? 발송·삭제 등 별도 변경 승인은 그대로 유지됩니다.')
        # Slack escapes &, < and > before its 3,000-character cap. Never ask
        # approval for a preview whose task scope would be silently truncated.
        if len(html.escape(question, quote=False)) > 2800:
            result = {'outcome': 'undelivered', 'responses': []}
        else:
            from tools.clarify_gateway import clarify_timeout_limit
            with clarify_timeout_limit(max(0.0, expires - time.time())):
                result = json.loads(clarify_tool([{'question': question, 'choices': [approve, '요청하지 않기']}], callback))
    responses = result.get('responses') or []
    approved = (result.get('outcome') == 'submitted' and len(responses) == 1
                and responses[0].get('status') == 'answered' and responses[0].get('user_response') == approve)
    status = 'approved' if approved else result.get('outcome', 'error')
    if not approved and status == 'submitted':
        status = 'declined'

    def settle(data):
        nonlocal approved, status
        current = task_for(data, task['id'], turn, mode)
        saved = current.get('referral', {})
        if (saved.get('status') != 'pending' or saved.get('fingerprint') != digest
                or fingerprint(current, turn, mode) != digest):
            raise ValueError('승인 중 요청이 바뀌어 전달하지 않았다.')
        if time.time() >= expires:
            approved, status = False, 'timed_out'
        saved.update(status=status, answered_at=time.time())
        return data
    store.change(key, settle)
    if not approved:
        return {'ok': False, 'status': status, 'executed': False, 'profile': task['profile'],
                'notice': '승인되지 않아 담당자에게 요청하지 않았다. 취소·시간 초과·전달 실패를 승인으로 해석하거나 재요청하지 않는다.'}
    return None


def consume(project, task, turn, mode):
    saved = task.get('referral', {})
    if (saved.get('status') != 'approved' or saved.get('mode') != mode
            or saved.get('fingerprint') != fingerprint(task, turn, mode)
            or time.time() > saved.get('expires_at', 0)):
        raise ValueError('이 담당자·요청에 대한 현재 버튼 승인이 없어 전달하지 않는다.')
    saved.update(status='consumed', consumed_at=time.time())
    return project
