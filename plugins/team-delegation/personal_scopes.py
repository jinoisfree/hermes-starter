"""Shared personal/company ownership. Resolve original metadata before every native write."""
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
import json
import os
import subprocess

try:
    from . import google_calendar_accounts as google_calendar
except ImportError:
    import google_calendar_accounts as google_calendar

import source_lookup

SCOPED_TOOLS = frozenset({'calendar_direct_mutation', 'reminders_direct_mutation',
    'apple_notes_direct_mutation', 'personal_work_bundle', 'personal_work_rollback',
    'gmail_direct', 'google_calendar_color_set'})
OWNERS = {'personal-secretary': 'personal', 'work-assistant': 'company'}
FLAGS = ('HERMES_CRON_SESSION', 'HERMES_SINGLE_QUERY_SESSION', 'HERMES_KANBAN_TASK')
MAIL_ACCOUNT = ContextVar('personal_scope_mail_account', default='')
PERSONAL_DISCONNECTED = 'PERSONAL_ACCOUNT_NOT_CONNECTED: 개인 계정 연결 전'
UNKNOWN = 'PERSONAL_SCOPE_UNVERIFIED: 대상 폴더·목록·캘린더를 확인할 수 없어 쓰기를 거부한다.'
MAIL_UNKNOWN = 'PERSONAL_SCOPE_MAIL_ACCOUNT_UNKNOWN: 개인·회사 어느 범위에도 등록되지 않은 메일 계정이다.'


def config(td):
    settings = td._PLUGIN_CONTEXT.get_config('personal_roles', {}) if td._PLUGIN_CONTEXT else {}
    path = settings.get('scope_config') if isinstance(settings, dict) else None
    if not path:
        return None
    try:
        import hermes_yaml
        data = hermes_yaml.safe_load(Path(path).read_text())
        cfg = data['plugins']['entries']['team-kit']['settings']['personal_scopes']
        if cfg.get('enabled') is not True:
            raise ValueError('disabled')
        return cfg
    except Exception:
        # An explicitly configured boundary must never fall back to the former unrestricted executor.
        return {'invalid': True}


def current(td):
    return td.personal_roles.profile(td)


def background(td):
    return any(td.get_session_env(flag, '') or os.environ.get(flag) for flag in FLAGS)


def mailbox(td, args, cfg):
    return str(args.get('account') or MAIL_ACCOUNT.get() or cfg[OWNERS[current(td)]]['gmail']).strip().casefold()


def mail_scope(cfg, account):
    """Scope owning a mailbox: its Gmail account or a listed Mac Mail app account."""
    return next((k for k in OWNERS.values() if account == cfg[k]['gmail'].casefold()
                 or account in {str(a).strip().casefold() for a in cfg[k].get('mail_accounts') or ()}), None)


def role_problem(td, name, args):
    cfg = config(td)
    if cfg is None or current(td) not in OWNERS:
        return None
    if cfg.get('invalid'):
        return UNKNOWN
    if name == 'gmail_direct':
        account = mailbox(td, args, cfg)
        target = mail_scope(cfg, account)
        if target is None:
            return MAIL_UNKNOWN
        # Each secretary reads and writes only its own scope's mailboxes (2026-10-18).
        if target != OWNERS[current(td)]:
            return wrong_owner(target) + MAIL_HANDOFF
        if target == 'personal' and not cfg['personal'].get('connected'):
            return PERSONAL_DISCONNECTED
        action = str(args.get('action') or '').strip().lower()
        if action in {'labels', 'filter_list', 'label_create', 'label_apply', 'filter_create', 'filter_delete', 'mail_organize', 'inbox_restore'} and background(td):
            return 'PERSONAL_SCOPE_BACKGROUND_REFUSED: 예약·배경에서 라벨·필터 호출은 허용되지 않는다.'
        if action in {'lookup', 'read', 'thread', 'download', 'accounts', 'ack', 'labels', 'filter_list'}:
            if background(td) and not (current(td) == 'work-assistant' and target == 'company'
                    and args.get('action') in {'lookup', 'read', 'thread', 'ack'}
                    and (td.get_session_env('HERMES_CRON_SESSION', '') or os.environ.get('HERMES_CRON_SESSION'))
                    and not any(td.get_session_env(f, '') or os.environ.get(f) for f in FLAGS[1:])):
                return 'PERSONAL_SCOPE_BACKGROUND_REFUSED: 예약·배경에서 허용되지 않은 메일 호출이다.'
            return None
        if target != OWNERS[current(td)]:
            return wrong_owner(target)
    if name in SCOPED_TOOLS:
        if (background(td) or td.get_session_env('HERMES_SESSION_PLATFORM', '') not in {'slack', 'photon'}
                or not td.get_session_env('HERMES_SESSION_CHAT_ID', '')
                or not td.get_session_env('HERMES_SESSION_USER_ID', '')):
            return 'PERSONAL_SCOPE_OWNER_CHAT_REQUIRED: 원본 쓰기는 인증된 소유자 대화에서만 가능하다.'
    return None


# 2026-10-19 사용자 결정: 남의 메일은 거부로 끝내지 않고 starter_user-world에서 담당 비서에게 바로 넘긴다.
MAIL_HANDOFF = " starter_user-world 스레드면 거부로 끝내지 말고 team_project plan(profile=담당 비서) 후 dispatch(task_id, tool='gmail_direct')로 바로 넘긴다."


def wrong_owner(scope):
    return "개인 범위는 개인비서 담당('ㅈ'으로 요청)" if scope == 'personal' else '회사 범위는 업무비서 담당이다.'


def scope_of(cfg, domain, row):
    if not isinstance(row, dict):
        raise ValueError(UNKNOWN)
    if domain == 'notes':
        personal = cfg['personal']
        if row.get('name') == personal['notes_folder']:
            if (row.get('account') != personal['notes_account']
                    or row.get('id') != personal['notes_folder_id']):
                raise ValueError(UNKNOWN)
            return 'personal'
        if not row.get('id') or not row.get('account') or not row.get('name'):
            raise ValueError(UNKNOWN)
        return 'company'
    if domain == 'todo':
        if not row.get('id') or not row.get('name'):
            raise ValueError(UNKNOWN)
        return 'personal' if row['name'] == cfg['personal']['reminders_list'] else 'company'
    if domain == 'calendar':
        if not row.get('identifier'):
            raise ValueError(UNKNOWN)
        # The personal account is the explicit exception, as with Notes folders and lists.
        # EventKit source titles may be localized and company IDs are informational only.
        personal = cfg['personal']
        return 'personal' if (row['identifier'] in personal.get('calendar_ids', [])
            or row.get('source') == personal['calendar_account']) else 'company'
    raise ValueError(UNKNOWN)


def native_metadata(td, domain, args, context):
    if domain == 'calendar':
        wanted = args.get('event_id') or args.get('eventkit_id')
        # Exact-ID read is independent of today's query window. Mutations still
        # validate the original occurrence and destination below.
        original = None
        if wanted:
            outcome = td._calendar_event_lookup(wanted, context.get('runner') or subprocess.run)
            original = outcome.get('row')
            if outcome['status'] not in {'ok','unsupported'}:
                source_lookup.require(outcome)
        date = str(args.get('expected_start') or args.get('start') or args.get('date') or 'today')[:10]
        reply = json.loads(td._calendar_direct_lookup({'date': date}, **context))
        if reply.get('ok') is not True:
            raise ValueError(UNKNOWN)
        receipt = reply['receipt']; rows = receipt['calendar_results']; item = None
        wanted = args.get('event_id') or args.get('eventkit_id')
        if wanted:
            events = [original] if original else []
            if not events:
                raise source_lookup.SourceReadError('transient','calendar_scope_unverified')
            selected = [r for r in rows if r.get('identifier') == events[0].get('calendarIdentifier')]
            if len(selected) != 1:
                raise ValueError(UNKNOWN)
            item = selected[0]
        return {'calendars': rows, 'current': item}
    if domain == 'todo':
        # Use the same native executor and exact IDs as reminders_direct_lookup/mutation.
        # GUI AppleEvents may have a different TCC grant from remindctl in the gateway.
        runner = context.get('runner') or subprocess.run
        def read(command):
            proc = runner(command + ['--json', '--no-input', '--no-color'],
                          capture_output=True, text=True, timeout=20, check=False)
            if proc.returncode != 0:raise ValueError(UNKNOWN)
            value = json.loads(proc.stdout)
            if not isinstance(value, list):raise ValueError(UNKNOWN)
            return value
        lists = [{'id': row.get('id'), 'name': row.get('title')} for row in read(['remindctl', 'list'])]
        item = None
        if args.get('reminder_id'):
            import reminder_reads
            row = reminder_reads.one(args['reminder_id'], runner)
            found = [row] if row is not None else []
            if len(found) != 1:raise ValueError(UNKNOWN)
            matched = [row for row in lists if row.get('id') == found[0].get('listID')
                       and row.get('name') == found[0].get('listName')]
            if len(matched) != 1:raise ValueError(UNKNOWN)
            item = matched[0]
        return {'lists': lists, 'current': item}
    source = Path(__file__).with_name('personal_scope_native.js')
    payload = {'domain': domain, 'id': args.get('note_id' if domain == 'notes' else 'reminder_id')}
    runner = context.get('runner') or subprocess.run
    try:
        proc = runner(['osascript', '-l', 'JavaScript', str(source), json.dumps(payload)],
                      capture_output=True, text=True, timeout=20, check=False)
    except subprocess.TimeoutExpired:
        raise source_lookup.SourceReadError('transient', 'notes_read_timeout') from None
    except OSError:
        raise source_lookup.SourceReadError('transient', 'notes_read_failed') from None
    if proc.returncode != 0:
        source_lookup.require(source_lookup.notes_failure(proc.stderr or ''))
    try:
        value = json.loads(proc.stdout)
    except (ValueError, TypeError):
        raise source_lookup.SourceReadError('transient', 'notes_read_invalid') from None
    if not isinstance(value, dict):
        raise source_lookup.SourceReadError('transient', 'notes_read_invalid')
    if value.get('source_status'):
        if value.get('source_status') == 'missing' and value.get('source_reason') in {'notes_store_missing', 'note_not_found'}:
            raise source_lookup.SourceReadError('missing', value['source_reason'])
        raise source_lookup.SourceReadError('transient', 'notes_read_invalid')
    return value


def authorize_mutation(td, domain, args, context, *, scope_config=None, owner_scope=None):
    cfg = scope_config if scope_config is not None else config(td)
    if cfg.get('invalid'):
        raise ValueError(UNKNOWN)
    owner = owner_scope or OWNERS[current(td)]
    args = dict(args)
    if domain == 'calendar' and owner == 'personal' and not cfg['personal'].get('connected'):
        raise ValueError(PERSONAL_DISCONNECTED)
    metadata = native_metadata(td, domain, args, context)
    key = {'notes': 'folder', 'todo': 'list', 'calendar': 'calendar_id'}[domain]
    rows = metadata[{'notes': 'folders', 'todo': 'lists', 'calendar': 'calendars'}[domain]]
    action = args.get('action')
    current_row = metadata.get('current')
    if action != 'create':
        actual = scope_of(cfg, domain, current_row)
        if actual != owner:
            raise ValueError(wrong_owner(actual))
    named = args.get(key)
    if not named and action == 'create':
        if domain == 'notes': named = cfg['personal']['notes_folder'] if owner == 'personal' else td._notes_create_folder('')
        elif domain == 'todo': named = cfg['personal']['reminders_list'] if owner == 'personal' else td.DEFAULT_REMINDERS_LIST
        # Several read-only calendars (holidays, birthdays) also count as company; pick the configured one.
        elif domain == 'calendar': named = cfg[owner].get('default_calendar_id')
    selected = []
    if named:
        field = 'identifier' if domain == 'calendar' else 'name'
        selected = [row for row in rows if row.get(field) == named]
    elif domain == 'calendar' and action == 'create':
        for row in rows:
            try:
                if scope_of(cfg, domain, row) == owner:selected.append(row)
            except ValueError:pass
    elif current_row:
        selected = [current_row]
    if len(selected) != 1:
        raise ValueError(UNKNOWN)
    actual = scope_of(cfg, domain, selected[0])
    if actual != owner:
        raise ValueError(wrong_owner(actual))
    if domain == 'calendar' and actual == 'personal' and not cfg['personal'].get('connected'):
        raise ValueError(PERSONAL_DISCONNECTED)
    # Pin the exact resolved destination instead of the application's default calendar/folder/list.
    args[key] = selected[0]['identifier' if domain == 'calendar' else 'name']
    return args


def read_problem(td, name, args):
    cfg = config(td)
    if cfg is None or current(td) not in OWNERS:
        return None
    if cfg.get('invalid'):return UNKNOWN
    named = str(args.get('account') or '').casefold()
    if named and named not in {cfg['personal']['gmail'], cfg['company']['gmail'], 'all'}:return UNKNOWN
    if (name in {'gmail_direct', 'calendar_direct_lookup', 'google_calendar_color_get'}
            and str(args.get('account') or '').casefold() == cfg['personal']['gmail']
            and not cfg['personal'].get('connected')):
        return PERSONAL_DISCONNECTED
    return None


def install(td):
    for name, domain in [('_apple_notes_direct_mutation', 'notes'), ('_reminders_direct_mutation', 'todo'),
                         ('_calendar_direct_mutation', 'calendar'), ('_google_calendar_color_set', 'calendar')]:
        original = getattr(td, name)
        def build(fn, kind):
            @wraps(fn)
            def wrapped(args, **context):
                cfg = config(td)
                if cfg is None or current(td) not in OWNERS:return fn(args, **context)
                try:
                    if problem := role_problem(td, fn.__name__.lstrip('_'), args):raise ValueError(problem)
                    if fn.__name__ == '_google_calendar_color_set':
                        return google_calendar.color(td, args, context, write=True)
                    if kind == 'calendar' and OWNERS[current(td)] == 'personal':
                        return google_calendar.mutation(td, args, context)
                    checked = authorize_mutation(td, kind, args, context)
                    return fn(checked, **context)
                except Exception as exc:
                    return td._error(str(exc) if isinstance(exc, ValueError) else UNKNOWN,
                        mutation_calls=int(bool(context.get('_api_write_attempted'))),
                        reflection_unknown=bool(context.get('_api_write_attempted')))
            return wrapped
        setattr(td, name, build(original, domain))
    original_bundle = td._personal_work_bundle
    @wraps(original_bundle)
    def bundle(args, **context):
        if config(td) is None or current(td) not in OWNERS:return original_bundle(args, **context)
        try:
            if problem := role_problem(td, 'personal_work_bundle', args):raise ValueError(problem)
            normalized = td._normalize_personal_bundle_args(args)
            operations, _, error = td._validate_bundle(normalized)
            if error:raise ValueError(error)
            for op in operations:
                payload = {'action': op['action'], **op['arguments']}
                if op['domain'] == 'calendar' and current(td) == 'personal-secretary':
                    checked = google_calendar.preflight(td, payload, context)[3]
                else:
                    checked = authorize_mutation(td, op['domain'], payload, context)
                op['arguments'] = {k:v for k,v in checked.items() if k != 'action'}
            normalized['operations'] = operations
            return original_bundle(normalized, **context)
        except Exception as exc:
            return td._error(str(exc) if isinstance(exc, ValueError) else UNKNOWN,
                        mutation_calls=int(bool(context.get('_api_write_attempted'))),
                        reflection_unknown=bool(context.get('_api_write_attempted')))
    td._personal_work_bundle = bundle
    for name in ['_calendar_direct_lookup', '_google_calendar_color_get']:
        fn = getattr(td, name)
        def build_read(original):
            @wraps(original)
            def lookup(args, **context):
                if problem := read_problem(td, original.__name__.lstrip('_'), args):return td._error(problem)
                cfg = config(td)
                if cfg is not None and current(td) in OWNERS:
                    try:
                        if original.__name__ == '_google_calendar_color_get':
                            return google_calendar.color(td, args, context, write=False)
                        if (args.get('backend') == 'google' or args.get('account') in {'all',cfg['personal']['gmail']}
                                or (current(td) == 'personal-secretary' and args.get('backend') != 'eventkit')):
                            return google_calendar.lookup(td, args, context)
                    except Exception as exc:
                        return td._error(str(exc) if isinstance(exc, ValueError) else 'GOOGLE_CALENDAR_API_FAILED', mutation_calls=0)
                result = original(args, **context)
                cfg = config(td)
                if original.__name__ == '_calendar_direct_lookup' and cfg is not None and not cfg.get('invalid'):
                    value = json.loads(result)
                    value['scope_status'] = {'personal_google_connected': bool(cfg['personal'].get('connected')),
                        'notice': '개인 계정 연결 전' if not cfg['personal'].get('connected') else 'EventKit 계정과 권한도 별도로 확인한다.'}
                    account = str(args.get('account') or '').casefold()
                    if account and value.get('ok'):
                        owner = next((k for k in OWNERS.values() if cfg[k]['gmail'] == account), None)
                        rows = value['receipt']['calendar_results']; selected = []
                        for row in rows:
                            try:
                                if scope_of(cfg, 'calendar', row) == owner:selected.append(row['identifier'])
                            except ValueError:pass
                        if not selected:return td._error('PERSONAL_SCOPE_CALENDAR_NOT_CONNECTED: 계정별 EventKit 캘린더 연결·식별 전')
                        value['receipt']['results'] = [row for row in value['receipt']['results'] if row.get('calendarIdentifier') in selected]
                        value['receipt']['result_count'] = len(value['receipt']['results'])
                    return json.dumps(value, ensure_ascii=False)
                return result
            return lookup
        setattr(td, name, build_read(fn))


def token_home(td, root):
    cfg = config(td)
    if cfg is not None and not cfg.get('invalid') and MAIL_ACCOUNT.get() == cfg['personal']['gmail']:
        return Path(root) / 'profiles' / 'personal-secretary'
    return Path(root)


def install_mail(td):
    original = td._gmail_direct
    @wraps(original)
    def mail(args, runner=None, mail_runner=None, **context):
        context = {**context, 'runner': runner, 'mail_runner': mail_runner}
        cfg = config(td)
        if cfg is None or current(td) not in OWNERS:return original(args, **context)
        token = None
        try:
            if problem := role_problem(td, 'gmail_direct', args):raise ValueError(problem)
            selected = mailbox(td, args, cfg)
            token = MAIL_ACCOUNT.set(selected)
            if args.get('action') == 'accounts':
                value = json.loads(original(args, **context))
                if value.get('ok'):
                    value['accounts'] = [row for row in value.get('accounts', []) if any(
                        mail_scope(cfg, str(a).casefold()) == OWNERS[current(td)] for a in row.get('addresses', []))]
                return json.dumps(value, ensure_ascii=False)
            if selected != cfg[OWNERS[current(td)]]['gmail'].casefold():
                # A listed Mac Mail app account; the original tool reads it through Mail, read-only.
                return original({**args, 'account': selected}, **context)
            root = td._gmail_root()
            home = token_home(td, root)
            if not (home / 'google_token.json').is_file():
                raise ValueError(PERSONAL_DISCONNECTED if selected == cfg['personal']['gmail'] else 'GMAIL_NOT_CONNECTED')
            # Check the actual mailbox, never the requested From/account string alone.
            _, service = td.gmail_watch.gmail_client(root, token_home=home)
            if td.gmail_watch.mailbox_address(service) != selected:
                raise ValueError('PERSONAL_SCOPE_MAIL_ACCOUNT_MISMATCH: 연결된 편지함이 지정 계정과 다르다.')
            return original({**args, 'account': selected}, **context)
        except Exception as exc:
            return td._error(str(exc) if isinstance(exc, ValueError) else 'PERSONAL_SCOPE_MAIL_ACCOUNT_UNVERIFIED', mutation_calls=0)
        finally:
            if token is not None:MAIL_ACCOUNT.reset(token)
    td._gmail_direct = mail


def install_completion(td):
    original = td._complete_reminder_with_receipt
    @wraps(original)
    def complete(identifier):
        if config(td) is not None and current(td) in OWNERS:
            # common_work_complete reaches this helper without the direct mutation handler.
            if problem := role_problem(td, 'reminders_direct_mutation', {'action': 'complete'}):
                raise ValueError(problem)
            authorize_mutation(td, 'todo', {'action': 'complete', 'reminder_id': identifier}, {})
        return original(identifier)
    td._complete_reminder_with_receipt = complete
