"""Configuration-based project checks. Never invokes a business tool or grants approval."""
from pathlib import Path
import json
import os
import re
import tempfile
import time

from gateway.project_threads import dependency_ready

FILE_READS = frozenset({'read_file', 'search_files', 'file_list', 'folder_scan', 'doc_read', 'pdf_render', 'file_location'})
DOCUMENT_REVIEWS = frozenset({'table_analyze', 'doc_compare'})
SYSTEM = re.compile(r'desktop-pet|디닷|디도츠|hermes|헤르메스|게이트웨이|gateway|런타임|runtime', re.I)


def offered(home, learning, *, request=None, session_id='', turn_id='', **_):
    """Observe final API schemas only; never persist messages, arguments or secrets."""
    if not learning.extension('project_work').in_project(home, learning, session_id):
        return
    body = (request or {}).get('body', {})
    tools = body.get('tools') if isinstance(body, dict) else None
    if not isinstance(tools, list) or any(not isinstance(t, dict) or t.get('_truncated') for t in tools):
        return
    names = [t.get('function', t).get('name') for t in tools]
    value = {'pid': os.getpid(), 'at': time.time(), 'session_id': session_id, 'turn_id': turn_id,
             'tools': sorted(n for n in names if isinstance(n, str))}
    folder = Path(home) / 'state'; folder.mkdir(exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', dir=folder, prefix='.project-tools-', delete=False) as f:
        json.dump(value, f)
    os.replace(f.name, folder / 'project-tools.json')


def runtime_tools(home, tools):
    from tools.registry import registry
    registered = []
    for name in list(tools):
        entry = registry.get_entry(name, scope=str(Path(home).resolve()))
        if entry:
            tools[name] = entry.schema
            registered.append(name)
    try:
        observed = json.loads((Path(home) / 'state/project-tools.json').read_text())
    except (OSError, ValueError):
        observed = {}
    return registered, observed if observed.get('pid') == os.getpid() else {}


def system_task(title):
    from gateway.common_router import _direct_text
    return bool(SYSTEM.search(_direct_text(title)))


def check_role(profile, title):
    if profile == 'system-doctor' and not system_task(title):
        raise ValueError('진단담당은 desktop-pet·Hermes 시스템만 진단한다. 문서 검토는 업무비서에게 맡긴다.')


def config_for(cfg, profile, learning):
    path = cfg.get('profile_homes', {}).get(profile)
    if not path:
        raise ValueError('Project worker home is not configured.')
    home = Path(path)
    if home.is_symlink() or (home / 'config.yaml').is_symlink():
        raise ValueError('Project configuration cannot follow links.')
    return load_settings(home)


def load_settings(home):
    from ruamel.yaml import YAML
    data = YAML(typ='safe').load((Path(home) / 'config.yaml').read_text()) or {}
    return data.get('plugins', {}).get('entries', {}).get('team-kit', {}).get('settings', {})


def catalog(config, profile, learning):
    result = {}
    files = learning.extension('files')
    file_cfg = config.get('files', {})
    if profile in {'work-assistant', 'personal-secretary'} and file_cfg.get('enabled') is True and file_cfg.get('folders'):
        for name in files.HANDLERS:
            if files.available(file_cfg, name) and name != 'file_link':
                result[name] = files.schema(file_cfg, name)
        for name, required in [('read_file', ['path']), ('search_files', ['pattern'])]:
            result[name] = {'parameters': {'required': required}}
    if (profile == 'work-assistant' or config.get('review_tools', {}).get('secretary_tools') is True) and config.get('review_tools', {}).get('enabled') is True and 'doc_read' in result:
        result.update(learning.extension('document_review').SCHEMAS)
    if profile in {'work-assistant', 'personal-secretary'} and config.get('notion', {}).get('enabled') is True:
        result['notion'] = learning.extension('notion').schema(config['notion'])
    if config.get('review_tools', {}).get('enabled') is True:
        reviews = learning.extension('review_tools')
        if profile in reviews.MODULES:
            result.update(reviews.module(reviews.MODULES[profile]).SCHEMAS)
    for setting, module, name in [('news_watch', 'news_watch_read', 'news_watch_read'),
                                   ('x_read', 'x_read', 'x_posts'), ('slack_read', 'slack_read', 'slack_history'),
                                   ('diagnosis_request', 'diagnosis_requests', 'team_diagnosis_request')]:
        if config.get(setting, {}).get('enabled') is True:
            schema = getattr(learning.extension(module), 'SCHEMA', None)
            result[name] = schema or {'schema_verified': False}
    roles = {
        'personal-secretary': ['calendar_direct_lookup', 'calendar_direct_mutation', 'reminders_direct_lookup',
                       'reminders_direct_mutation', 'apple_notes_direct_lookup', 'apple_notes_direct_mutation',
                       'gmail_direct', 'personal_work_bundle', 'personal_work_rollback', 'common_work_get', 'work_crosscheck', 'mail_waiting'],
        'default': ['wiki_direct'],
        'sns-manager': ['instagram_dm_inbox', 'instagram_dm_thread', 'instagram_dm_draft', 'instagram_dm_send',
                      'instagram_comments', 'instagram_comment_manage', 'instagram_post_publish', 'instagram_post_schedule',
                      'naver_talk_status', 'naver_talk_draft'],
        'system-doctor': sorted(t for t in learning.extension('diagnosis_runtime').READS if t.startswith('diagnosis_')),
    }
    if config.get('personal_roles', {}).get('enabled') is True:
        reads = ['google_calendar_color_get', 'calendar_direct_lookup', 'reminders_direct_lookup', 'apple_notes_direct_lookup',
                 'gmail_direct', 'common_work_get', 'knowledge_ref_get']
        roles['personal-secretary'] = reads + ['work_crosscheck', 'mail_waiting']
        if config.get('personal_roles', {}).get('scope_config'):
            roles['personal-secretary'] += ['calendar_direct_mutation', 'reminders_direct_mutation',
                                    'apple_notes_direct_mutation', 'personal_work_bundle', 'personal_work_rollback']
        roles['work-assistant'] = reads + ['contacts_direct_lookup', 'contacts_direct_mutation', 'personal_work_bundle', 'personal_work_rollback',
                                     'google_calendar_color_set', 'common_work_complete',
                                     'work_crosscheck', 'mail_waiting', 'slack_history', 'deadline_watch']
        if profile == 'personal-secretary' and not config.get('personal_roles', {}).get('scope_config'):
            for name in list(result):
                if name in files.CORE_CHANGE_TOOLS | files.OWN_CHANGE_TOOLS:
                    del result[name]
    # This part is a role contract, not an assertion that a tool was offered or authenticated.
    for name in roles.get(profile, []):
        result.setdefault(name, {'schema_verified': False, 'role_contract': True})
    return result


def consult_tools(config, profile):
    if profile == 'system-doctor':
        return set()  # Diagnosis uses the real owner's conversation, never a document worker.
    tools = {'skill_view', 'skills_list', 'web_search', 'web_extract'}
    file_cfg = config.get('files', {})
    if profile in {'work-assistant', 'personal-secretary'} and file_cfg.get('enabled') is True and file_cfg.get('folders'):
        tools.update(FILE_READS)
        if profile == 'work-assistant' and config.get('review_tools', {}).get('enabled') is True:
            tools.update(DOCUMENT_REVIEWS)
    return tools


def preflight(home, learning, cfg, project, turn, args):
    task = next((t for t in project['tasks'] if t['id'] == args.get('task_id')), None)
    if args.get('task_id') and task is None:
        raise ValueError('Unknown project task.')
    profile = task['profile'] if task else args.get('profile', turn['source']['profile'])
    check_role(profile, task['title'] if task else turn['request'])
    config = config_for(cfg, profile, learning)
    tools = catalog(config, profile, learning)
    registered, offered = runtime_tools(cfg['profile_homes'][profile], tools)
    consult = consult_tools(config, profile)
    tool = args.get('tool')
    arguments = args.get('arguments', {})
    if not isinstance(arguments, dict):
        raise ValueError('arguments must be an object.')
    schema = tools.get(tool, {}).get('parameters', {})
    invalid = [name for name, value in arguments.items() if name in schema.get('properties', {})
               and 'enum' in schema['properties'][name] and value not in schema['properties'][name]['enum']]
    missing = [name for name in schema.get('required', []) if name not in arguments]
    waiting = [key for key in (task or {}).get('depends_on', [])
               if not any(t['id'] == key and dependency_ready(t) for t in project['tasks'])]
    reason = '허용되지 않은 인자 값: ' + ', '.join(invalid) if invalid else None
    if tool in FILE_READS or tool in learning.extension('files').OWN_CHANGE_TOOLS:
        if tool in tools and not missing:
            files = learning.extension('files')
            reason = files._path_problem(tool, arguments, files.folders(config.get('files', {})))
    if tool in DOCUMENT_REVIEWS and tool in tools and not missing:
        if tool == 'table_analyze' and not isinstance(arguments.get('sources'), list):
            raise ValueError('sources must be an array.')
        paths = [v.get('path') for v in arguments.get('sources', []) if isinstance(v, dict)] if tool == 'table_analyze' else [arguments.get('before'), arguments.get('after')]
        files = learning.extension('files')
        roots = files.folders(config.get('files', {}))
        if not paths or any(files.inside(path, roots) is None for path in paths):
            reason = '문서 원본을 이 담당자의 허용 폴더 안에서 지정해야 한다.'
    approvals = [a['id'] for a in project.get('approvals', []) if a.get('task_id') == (task or {}).get('id')
                 and a['status'] in {'pending', 'approved', 'executing', 'unknown'}]
    read = tool in consult
    approval = '실제 소유자 대화에서 도구별 승인·권한을 다시 확인한다.'
    if read:
        approval = '읽기 전용. 실제 호출 때 경로·자료 접근 범위를 다시 확인한다.'
    elif profile == 'work-assistant' and tool in config.get('files', {}).get('no_approval', []):
        approval = '명시적인 소유자 요청에 한해 기존 승인 예외 적용. 덮어쓰기 금지.'
    elif profile == 'work-assistant' and tool == 'notion':
        approval = ('연결에 공유된 모든 페이지·표에서 생성 즉시, 수정 후보고·되돌리기, 삭제만 도구 승인. api_help로 전체 기능을 확인하며 배경 쓰기는 거부한다.'
                    if config.get('notion', {}).get('full_api') is True else
                    '현재 소유자 요청의 create/update/append/edit는 기존 예외 적용. trash와 배경 실행은 예외가 아니다.')
    elif profile == 'personal-secretary' and tool == 'notion':
        approval = '개인비서 Notion은 읽기 전용이다. 작성은 업무비서의 현재 요청 실행으로 연결한다.'
    return {'ok': True, 'profile': profile, 'task_id': (task or {}).get('id'),
            'configured_tools': sorted(tools), 'consult_tools': sorted(consult),
            'tool': tool, 'configured': tool in tools if tool else None,
            'missing_arguments': missing, 'waiting_for': waiting, 'approval_ids': approvals, 'scope_problem': reason,
            'approval': approval, 'runtime_verified': False, 'execution_authorized': False,
            'schema_verified': bool(tool in tools and tools[tool].get('schema_verified') is not False),
            'registered_tools': registered, 'last_offered_tools': offered.get('tools'), 'last_offered_at': offered.get('at'),
            'offered_in_current_turn': (tool in offered['tools']) if tool and offered and offered.get('session_id') == turn.get('session_id') and offered.get('turn_id') == turn.get('turn_id') else None,
            'connection_verified': False,
            'status': 'needs_input' if missing or waiting or reason or approvals else
                      'owner_verification_required' if tool and tool not in tools else 'configuration_checked',
            'notice': '설정·선행 조건 확인이다. 인증, 외부 서비스 연결, 실제 자료 존재, 실행 완료나 승인을 보증하지 않는다. 진단담당 진단·개인 업무·고객 업무는 실제 담당자의 소유자 대화에서 확인한다.'}
