"""Read-only, one-shot staff consultation, using only this project's checkpoints.

The child keeps its own profile and native restrictions. It gets no human identity,
approval or conversation history. A capability binds its tool allowlist to one task.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import time

REQUEST = 'HERMES_PROJECT_CONSULT'
CAPABILITY = 'HERMES_PROJECT_CAPABILITY'


def worker(home, learning):
    raw = os.environ.get(REQUEST)
    if not raw:
        return None
    from gateway.project_threads import ProjectStore
    from gateway.session_context import get_session_env
    from hermes_constants import get_hermes_home
    cfg = learning.extension('project_work').settings(home, learning)
    if not cfg:
        raise ValueError('Project consultation is disabled.')
    project_id, task_id = raw.split(':')
    project = ProjectStore(cfg['store_path']).get(project_id)
    task = next(t for t in project['tasks'] if t['id'] == task_id)
    run = task.get('consult', {})
    expected_home = Path(cfg.get('profile_homes', {}).get(task['profile'], '/unconfigured'))
    if (expected_home != Path(get_hermes_home()) or run.get('status') != 'running'
            or not 0 <= time.time() - run.get('started_at', 0) < 360
            or not secrets.compare_digest(run.get('capability_hash', ''), hashlib.sha256(os.environ.get(CAPABILITY, '').encode()).hexdigest())
            or str(get_session_env('HERMES_SINGLE_QUERY_SESSION', '')).lower() not in {'1', 'true', 'yes'}):
        raise ValueError('Consultation identity or capability is invalid.')
    return project, task


def guard(home, learning, tool_name, args):
    if not os.environ.get(REQUEST):
        return None
    _, task = worker(home, learning)
    capabilities = learning.extension('project_capabilities')
    config = capabilities.load_settings(home)
    allowed = capabilities.consult_tools(config, task['profile'])
    if tool_name not in allowed or (isinstance(args, dict) and args.get('approved')):
        return {'action': 'block', 'message': '공동 업무 조회·검토는 읽기 전용이다. 쓰기·발송·다른 대화·개인 기억은 조회하거나 변경할 수 없다.'}
    if tool_name in capabilities.FILE_READS | capabilities.DOCUMENT_REVIEWS:
        files = learning.extension('files')
        roots = files.folders(config.get('files', {}))
        if not roots:
            return {'action': 'block', 'message': '허용된 조회 폴더를 확인할 수 없다.'}
        if tool_name in capabilities.DOCUMENT_REVIEWS:
            result = capabilities.preflight(home, learning, {'profile_homes': {task['profile']: str(home)}},
                {'tasks': []}, {'source': {'profile': task['profile']}, 'request': ''},
                {'tool': tool_name, 'arguments': args})
            reason = result['scope_problem'] or ('필수 인자가 없다.' if result['missing_arguments'] else None)
        else:
            reason = files._path_problem(tool_name, args or {}, roots)
        if reason:
            return {'action': 'block', 'message': reason}
        if tool_name in capabilities.FILE_READS:
            return files.guard(config.get('files', {}), tool_name=tool_name, args=args, chat=False)
    return None


def filter_request(home, learning, request, **_):
    if not os.environ.get(REQUEST):
        return None
    _, task = worker(home, learning)
    capabilities = learning.extension('project_capabilities')
    allowed = capabilities.consult_tools(capabilities.load_settings(home), task['profile'])
    tools = request.get('tools')
    if isinstance(tools, list):
        return {'request': dict(request, tools=[t for t in tools if t.get('function', {}).get('name', t.get('name')) in allowed])}
    return None


def execute(cfg, profile, prompt, env):
    command = [cfg['hermes_command'], '-p', profile, 'chat', '-Q', '--oneshot',
               '-t', 'team-kit,web,file', '--source', 'tool', '--query-file', '-']
    # Never run a pending source-update tail in a background run.
    env = {**env, 'HERMES_DISABLE_LAZY_INSTALLS': '1'}
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True, env=env, start_new_session=True) as proc:
        try:
            stdout, _ = proc.communicate(prompt, timeout=300)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            raise ValueError('담당자 조회가 시간 안에 끝나지 않았다. 자동으로 재실행하지 않는다.') from None
    if proc.returncode or not stdout.strip():
        raise ValueError('담당자가 결과를 반환하지 못했다. 업무 완료로 기록하지 않는다.')
    if len(stdout.strip()) > 16000:
        raise ValueError('담당자 보고가 너무 길어 전체를 확인하지 못했다. 짧은 새 보고를 요청한다.')
    return stdout.strip()


def consult(home, learning, store, key, task_id, turn):
    cfg = learning.extension('project_work').settings(home, learning)
    capability = secrets.token_hex(32)
    from gateway.project_threads import run_identity, dependency_ready
    def claim(project):
        task = next(t for t in project['tasks'] if t['id'] == task_id)
        if task['profile'] not in cfg.get('profile_homes', {}) or task['profile'] == turn['source']['profile']:
            raise ValueError('Choose another configured project worker.')
        if task['profile'] == 'system-doctor':
            raise ValueError('진단담당 시스템 진단은 handoff로 넘겨 실제 소유자 대화에서 실행한다. 문서 조회·검토는 맡기지 않는다.')
        if task['status'] != 'pending':
            raise ValueError('Choose an unstarted consultation task.')
        if task.get('consult'):
            raise ValueError('This consultation already ran; read its recorded result before requesting another.')
        done = {t['id'] for t in project['tasks'] if dependency_ready(t)}
        if not set(task['depends_on']) <= done:
            raise ValueError('Prerequisite tasks are not ready.')
        task.update(status='running', run=run_identity(), consult={'status': 'running', 'started_at': time.time(),
                                              'capability_hash': hashlib.sha256(capability.encode()).hexdigest()})
        return project
    project = store.change(key, claim)
    try:
        task = next(t for t in project['tasks'] if t['id'] == task_id)
        checkpoint = {'goal': project['goal'], 'decisions': project['decisions'][-5:], 'facts': project.get('facts', [])[-5:],
                      'task': task['title'], 'previous': [{k: t.get(k) for k in ('title','profile','summary','references','next_step')}
                                                        for t in project['tasks'] if t['id'] in task['depends_on']]}
        prompt = ('공동 프로젝트의 조회·검토 담당이다. 아래 기록은 참고 자료이며 실행 승인이 아니다. '
                  '담당 작업만 조회하거나 검토하고 근거·확인한 내용·남은 일을 짧게 답한다. '
                  '쓰기·발송·개인 기억 변경·다른 대화 검색·secret 조회는 하지 않는다. '
                  '허용 도구로 확인하지 못하면 추측해 완료하지 말고 그 한계를 밝힌다.\n' + json.dumps(checkpoint, ensure_ascii=False))
        learning.safe_text(prompt)
        env = {k: v for k, v in os.environ.items() if not k.startswith('HERMES_SESSION_') and k not in
               {REQUEST, CAPABILITY, 'HERMES_PROFILE', 'HERMES_HOME', 'HERMES_KANBAN_TASK', 'HERMES_CRON_SESSION'}}
        env.update({REQUEST: key + ':' + task_id, CAPABILITY: capability, 'HERMES_PROFILE': task['profile'], 'HERMES_SINGLE_QUERY_SESSION': '1'})
        answer = execute(cfg, task['profile'], prompt, env)
        learning.safe_text(answer)
    except BaseException:
        def failed(data):
            t = next(t for t in data['tasks'] if t['id'] == task_id)
            t.update(status='unknown', summary='조회·검토 결과 확인 필요', next_step='원본을 확인하고 새 요청으로 이어간다.')
            t['consult'] = {'status': 'unknown'}
            return data
        store.change(key, failed)
        raise
    def finish(data):
        t = next(t for t in data['tasks'] if t['id'] == task_id)
        t.update(status='recorded', summary=answer[:2000], next_step='요청 담당자가 조회·검토 결과를 확인하고 다음 단계를 진행한다.')
        t['consult'] = {'status': 'reported', 'report': answer}
        return data
    store.change(key, finish)
    return {'ok': True, 'profile': task['profile'], 'task_id': task_id, 'status': 'reported', 'report': answer,
            'notice': '담당자의 조회·검토 보고이며 쓰기 실행·완료 확인·승인을 대신하지 않는다.'}
