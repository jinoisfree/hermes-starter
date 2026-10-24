"""Compare actual current read receipts and inspect Gmail reply candidates."""
from datetime import datetime
import json
from pathlib import Path
import subprocess
from zoneinfo import ZoneInfo

REF = {'type': 'object', 'properties': {'receipt_id': {'type': 'string'}, 'pointer': {'type': 'string', 'description': 'Exact JSON Pointer returned by list.'},
       'quote': {'type': 'string', 'description': 'Optional exact substring of the source string, e.g. its stated deadline.'}},
       'required': ['receipt_id', 'pointer'], 'additionalProperties': False}
SCHEMAS = {
    'work_crosscheck': {'name': 'work_crosscheck', 'description': 'Compare actual source values from this turn’s Gmail, Calendar, Reminders, Notes and read-only Notion receipts. List pointers first. Differences are candidates, not proof of the same task or missing completion. Never changes originals.',
        'parameters': {'type': 'object', 'properties': {
            'action': {'type': 'string', 'enum': ['list', 'compare']}, 'query': {'type': 'string'},
            'references': {'type': 'array', 'items': REF, 'minItems': 2, 'maxItems': 10}}, 'required': ['action'], 'additionalProperties': False}},
    'mail_waiting': {'name': 'mail_waiting', 'description': 'Read Gmail sent threads in an explicit KST date range, checking current thread metadata for later incoming messages. Returns reply candidates, not an assertion that a reply is owed. No sending, marking, reminders or background runs.',
        'parameters': {'type': 'object', 'properties': {'start': {'type': 'string', 'description': 'YYYY-MM-DD inclusive, KST'},
            'end': {'type': 'string', 'description': 'YYYY-MM-DD exclusive, KST'},
            'limit': {'type': 'integer', 'minimum': 1, 'maximum': 50},
            'account': {'type':'string','enum':['user(at)example.com','user(at)example.com']}}, 'required': ['start', 'end'], 'additionalProperties': False}},
}


def leaves(value, pointer='', depth=0):
    if depth > 20: return
    if isinstance(value, dict):
        for k, v in value.items():
            yield from leaves(v, pointer + '/' + str(k).replace('~', '~0').replace('/', '~1'), depth + 1)
    elif isinstance(value, list):
        for i, v in enumerate(value): yield from leaves(v, pointer + '/' + str(i), depth + 1)
    else:
        yield pointer, value


def selected(data, pointer):
    if not isinstance(pointer, str) or not pointer.startswith('/') or len(pointer) > 1000:
        raise ValueError('invalid_json_pointer')
    for part in pointer[1:].split('/'):
        part = part.replace('~1', '/').replace('~0', '~')
        if isinstance(data, list) and part.isdigit() and int(part) < len(data): data = data[int(part)]
        elif isinstance(data, dict) and part in data: data = data[part]
        else: raise ValueError('source_pointer_not_found')
    if isinstance(data, (dict, list)):
        raise ValueError('select_a_scalar_source_value')
    return data


def crosscheck(args, evidence):
    evidence = {k: v for k, v in evidence.items() if v['tool'] != 'instagram_dm_guide'}
    if args['action'] == 'list':
        if 'references' in args: raise ValueError('references_require_compare')
        query = args.get('query', '')
        if not isinstance(query, str) or len(query) > 200: raise ValueError('invalid_query')
        items = []
        for identifier, receipt in evidence.items():
            for pointer, value in leaves(receipt['data']):
                if query.casefold() not in (pointer + ' ' + str(value)).casefold(): continue
                items.append({'receipt_id': identifier, 'tool': receipt['tool'], 'pointer': pointer,
                    'value': value[:1000] if isinstance(value, str) else value,
                    'value_truncated': isinstance(value, str) and len(value) > 1000,
                    'checked_at_kst': datetime.fromtimestamp(receipt['checked_at'], ZoneInfo('Asia/Seoul')).isoformat()})
        return {'ok': True, 'values': items[:150], 'count': len(items), 'truncated': len(items) > 150,
                'scope': 'Successful read receipts in this session and turn only; not all related tasks.'}
    if args['action'] != 'compare' or 'query' in args:
        raise ValueError('invalid_crosscheck_action')
    refs = args.get('references')
    if not isinstance(refs, list) or not 2 <= len(refs) <= 10: raise ValueError('two_to_ten_references_required')
    rows, identities = [], set()
    for ref in refs:
        if not isinstance(ref, dict) or set(ref) - {'receipt_id', 'pointer', 'quote'}:
            raise ValueError('invalid_source_reference')
        receipt = evidence.get(ref.get('receipt_id'))
        if receipt is None: raise ValueError('source_not_in_current_read_evidence')
        value = selected(receipt['data'], ref.get('pointer'))
        quote = ref.get('quote')
        if quote is not None:
            if not isinstance(quote, str) or not quote or not isinstance(value, str) or quote not in value:
                raise ValueError('quote_must_be_exact_source_substring')
            value = quote
        if isinstance(value, str) and len(value) > 2000:
            raise ValueError('select_an_exact_short_quote_for_long_text')
        identity = (ref['receipt_id'], ref['pointer'])
        if identity in identities: raise ValueError('duplicate_source_reference')
        identities.add(identity)
        rows.append({**ref, 'tool': receipt['tool'], 'value': value, 'checked_at': receipt['checked_at']})
    known = all(r['value'] is not None and r['value'] != '' for r in rows)
    same = len({json.dumps([type(r['value']).__name__, r['value']], ensure_ascii=False) for r in rows}) == 1 if known else None
    return {'ok': True, 'sources': rows, 'values_equal': same, 'status': 'unknown' if same is None else 'equal' if same else 'difference_candidate',
            'note': 'Exact value comparison only. Verify task identity, timezone, applicability and field meaning; absent values are unknown. No source was changed.'}


def mail(home, args):
    import hermes_yaml as yaml
    from pm.environments import project_python
    cfg = yaml.safe_load((home / 'config.yaml').read_text())
    account = cfg.get('plugins', {}).get('entries', {}).get('team-delegation', {}).get('settings', {}).get('gmail', {}).get('account')
    account=args.get('account') or account
    if not account: raise ValueError('gmail_account_not_configured')
    args={k:v for k,v in args.items() if k!='account'}
    root = home.parent.parent
    token_home=root
    scope_path=cfg.get('plugins',{}).get('entries',{}).get('team-delegation',{}).get('settings',{}).get('personal_roles',{}).get('scope_config')
    if scope_path:
        shared=yaml.safe_load(Path(scope_path).read_text())['plugins']['entries']['team-kit']['settings']['personal_scopes']
        # Each secretary reads only its own scope's mailbox (2026-10-18 사용자 결정), like gmail_direct.
        own={'personal-secretary':'personal','work-assistant':'company'}.get(home.name)
        if own and str(account).casefold()!=str(shared[own]['gmail']).casefold():
            raise ValueError("PERSONAL_SCOPE_WRONG_OWNER: "+("개인 메일은 개인비서 담당('ㅈ'으로 요청)" if own=='company' else '회사 메일은 업무비서 담당이다.')
                             +" starter_user-world 스레드면 거부로 끝내지 말고 team_project plan(profile=담당 비서) 후 dispatch(task_id, tool='mail_waiting')로 바로 넘긴다.")
        if account==shared['personal']['gmail']:
            if not shared['personal'].get('connected'):raise ValueError('PERSONAL_ACCOUNT_NOT_CONNECTED: 개인 계정 연결 전')
            token_home=root/'profiles/personal-secretary'
        elif account!=shared['company']['gmail']:raise ValueError('gmail_account_not_configured')
    proc = subprocess.run([str(project_python(root / 'hermes-agent')), str(Path(__file__).with_name('mail_waiting_worker.py'))],
        input=json.dumps({'root': str(root), 'account': account, 'args': args, 'token_home':str(token_home)}), capture_output=True, text=True, timeout=120)
    if proc.returncode: raise ValueError('mail_waiting_failed_account_or_connection')
    return json.loads(proc.stdout)


def handle(name, args, *, home, evidence=None, **_):
    return crosscheck(args, evidence) if name == 'work_crosscheck' else mail(home, args)
