"""Owner-approved skill execution, opt-in and confined to writable scratch space."""
import os
from pathlib import Path
import re
import shlex

PROFILES = {'work-assistant', 'personal-secretary', 'system-doctor'}
SHELL = {'terminal', 'process', 'process_manage', 'execute_code'}
INSTALL = re.compile(r"\b(?:pip3?|npm|pnpm|yarn|brew|uv)(?:\s+(?:pip|--?[\w=-]+))*\s+(?:install|add|sync|update|upgrade)\b", re.I)


def enabled(td):
    return (td._PLUGIN_CONTEXT is not None and
            td._PLUGIN_CONTEXT.get_config('selected_skills', {}).get('enabled') is True and
            td.personal_roles.profile(td) in PROFILES)


def owner_chat(td):
    if any(td.get_session_env(k, '') or os.environ.get(k) for k in
           ('HERMES_CRON_SESSION', 'HERMES_SINGLE_QUERY_SESSION', 'HERMES_KANBAN_TASK', 'HERMES_DIAGNOSIS_REQUEST')):
        return False
    from tools.skill_provenance import is_background_review
    return (not is_background_review() and td.get_session_env('HERMES_SESSION_PLATFORM', '') == 'slack'
            and bool(td.get_session_env('HERMES_SESSION_CHAT_ID', ''))
            and bool(td.get_session_env('HERMES_SESSION_USER_ID', '')))


def offered(td):
    if not enabled(td) or not owner_chat(td):
        return set()
    return {'terminal', 'process_manage', 'process'} | (
        {'image_generate'} if td.personal_roles.profile(td) == 'personal-secretary' else set())


def problem(td, name, args):
    if not enabled(td) or name not in SHELL | {'image_generate'}:
        return None
    if not owner_chat(td) or name == 'execute_code':
        return '선택 스킬 실행은 소유자 Slack 대화에서만 가능하며 execute_code·배경 실행은 허용하지 않는다.'
    if name == 'image_generate' and td.personal_roles.profile(td) != 'personal-secretary':
        return '이미지 생성은 개인비서만 허용한다.'
    if name == 'terminal':
        command = args.get('command')
        if not isinstance(command, str) or not command.strip():
            return '실행할 command가 필요하다.'
        if INSTALL.search(command):
            return '대화 중 패키지 설치·갱신은 거부한다. 필요한 의존성은 Paseo에서 설치한다.'
    return None


def sandbox_argv(profile, command):
    """Build a clean-environment sandbox for both chat and fixed watcher runners."""
    user_home = Path.home().resolve()
    hermes_root = (user_home / '.hermes').resolve()
    home = hermes_root / 'profiles' / profile
    workspace = home / 'workspace'
    dependencies = (user_home / '.local/share/hermes-selected-skills').resolve()
    policy = Path(__file__).with_name('selected-skills-' + ('watchers' if profile == 'system-doctor' else 'chat') + '.sb')
    if profile not in PROFILES or not Path('/usr/bin/sandbox-exec').is_file() or not policy.is_file():
        raise ValueError('sandbox policy unavailable')
    if not workspace.is_dir() or workspace.is_symlink():
        raise ValueError('workspace unavailable or symlink')
    parameters = {'HOME': user_home, 'HERMES_ROOT': hermes_root, 'SKILLS': (home/'skills').resolve(),
                  'WORKSPACE': workspace.resolve(), 'WATCHER_STATE': (home/'watcher-state').resolve(),
                  'DEPENDENCIES': dependencies, 'REPOSITORIES': (user_home/'workspace').resolve(),
                  'PROFILE_HOME': home.resolve()}
    argv = ['/usr/bin/sandbox-exec']
    for key, value in parameters.items():
        argv += ['-D', key + '=' + str(value)]
    argv += ['-f', str(policy), '/usr/bin/env', '-i', 'HOME=' + str(user_home),
             'PATH=' + str(dependencies / 'venv/bin') + ':/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin',
             'LANG=en_US.UTF-8', 'LC_ALL=en_US.UTF-8', 'TMPDIR=/private/tmp',
             'PYTHONDONTWRITEBYTECODE=1', '/bin/bash', '--noprofile', '--norc', '-c', command]
    return argv


def wrap(td, args):
    """Always wrap untrusted input; no model-supplied already-wrapped escape hatch."""
    return dict(args, command=shlex.join(sandbox_argv(td.personal_roles.profile(td), args['command'])))
