"""Share exact request permits after the normal file/Notion scope guards have accepted a call."""
from pathlib import Path
import importlib.util
from types import SimpleNamespace


def module(home):
    path = Path(home)/'plugins/team-delegation/request_approvals.py'
    spec = importlib.util.spec_from_file_location('team_kit_request_permits', path)
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    return api


def gate(home, name, args, decision, context):
    home = Path(home)
    if home.name not in {'work-assistant', 'personal-secretary'} or not decision or decision.get('action') != 'approve':
        return decision
    from gateway.session_context import get_session_env
    import os
    api = module(home)
    td = SimpleNamespace(get_session_env=get_session_env,
        personal_roles=SimpleNamespace(profile=lambda td: home.name),
        personal_scopes=SimpleNamespace(background=lambda td: any(get_session_env(flag, '') or os.environ.get(flag)
            for flag in ('HERMES_CRON_SESSION', 'HERMES_SINGLE_QUERY_SESSION', 'HERMES_KANBAN_TASK'))),
        _personal_role_problem=lambda *a: None)
    if api.allowed(td, name, args, context):
        return None
    return api.gate(td, name, args, decision, context)
