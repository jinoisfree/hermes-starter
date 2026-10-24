"""Required deliverables and metadata-only evidence; lookup never proves a write."""
import re
from decimal import Decimal, InvalidOperation


_TARGETS = [('notion', r'노션|notion'), ('notes', r'메모'),
            ('todo', r'미리알림|리마인더'), ('calendar', r'캘린더|일정')]
_WRITE = r'작성|만들|생성|등록|추가|수정|변경|고쳐|써\s*줘|진행|처리|실행|정리|갱신|업데이트|적용|바꿔|저장'


def required(text):
    from gateway.project_requests import operation_text
    text = operation_text(text)
    # Finished inputs and negated clauses are not new deliverables. Preserve
    # shared verbs in lists ("메모, 일정, 노션 진행"), but split read-before-write
    # sequences before collecting their targets.
    text = re.sub(r'\([^()\n]*(?:기존\s*완료|이미\s*완료|수정\s*불필요)[^()\n]*\)', '', text)
    text = re.sub(r'\n\s*((?:진행|처리|실행|작성|등록|생성)(?:해\s*줘|해|해주세요)?)[ \t]*(?=\n|$)', r' \1', text)
    clauses = re.split(r'[.!?;\n]+|(?<=말고)|(?<=않고)|(?<=완료했고)|(?<=완료하고)|'
                       r'(?:조회|읽기|확인|참고)(?:한)?\s*(?:후|뒤|다음)', text)
    outputs = set()
    for clause in clauses:
        if (not re.search(_WRITE, clause, re.I)
                or re.search(r'하지\s*(?:말|않)|불필요|필요\s*없|(?:기존|이미)\s*완료|'
                             r'(?:됐|되었|했|한|된)(?:는지|지만|\s*여부)|여부만|'
                             r'(?:보여|알려|확인|조회|읽어|요약)(?:만)?\s*(?:해)?줘\s*$', clause)):
            continue
        outputs.update(kind for kind, pattern in _TARGETS if re.search(pattern, clause, re.I))
    return [kind for kind, _ in _TARGETS if kind in outputs]


def capture(tool, args, result):
    action = str((args or {}).get('action', ''))
    kinds = []
    writes = {'create', 'update', 'append', 'edit', 'add'}
    notion_content_writes = {'post-page', 'patch-page', 'move-page', 'update-page-markdown', 'update-a-block',
                            'patch-block-children', 'create-a-database', 'update-a-data-source', 'create-database',
                            'update-database', 'create-a-comment', 'update-a-comment', 'create-view',
                            'update-a-view', 'create-meeting-note'}
    if tool == 'notion':
        operations = (args or {}).get('operations', []) if action == 'batch' else [args or {}]
        if any(op.get('action') in writes for op in operations) or (action == 'api' and (args or {}).get('operation') in notion_content_writes
                and result.get('write_performed') is True and result.get('ok') is True):
            kinds.append('notion')
    # Bundle results carry the domains of the actual executed operations.
    if tool == 'personal_work_bundle':
        for op in result.get('operations', []):
            if op.get('domain') in {'notes', 'todo', 'calendar'} and op.get('action') in writes:
                kinds.append(op['domain'])
    for kind, names in {'notes': {'notes', 'apple_notes', 'apple_notes_direct_mutation'},
                        'todo': {'reminders', 'todo', 'reminders_direct_mutation'},
                        'calendar': {'calendar', 'calendar_direct_mutation'}}.items():
        if tool in names and action in writes:
            kinds.append(kind)
    observed_values = []
    if tool == 'notion':
        if action == 'api' and result.get('write_performed'):
            for prop in result.get('data', {}).get('properties', {}).values():
                if isinstance(prop, dict) and prop.get('type') in ('title', 'rich_text'):
                    observed_values.append(''.join(t.get('plain_text', t.get('text', {}).get('content', '')) for t in prop.get(prop['type'], []))[:200])
        for operation in result.get('operations', [result]):
            page = operation.get('created') or operation.get('updated') or {}
            observed_values += [v for v in page.get('properties', {}).values() if isinstance(v, str) and len(v) <= 200]
    return {'action': action, 'outputs': sorted(set(kinds)), 'observed_values': observed_values}


def register(store, key, request, message_id):
    kinds = required(request)
    if not kinds:
        return
    title = re.search(r'제목(?:은)?\s*[:：]\s*([^\n]+)', request)
    expected = title.group(1).strip() if title else ''
    def update(data):
        outputs = data.setdefault('required_outputs', [])
        for kind in kinds:
            if not any(o['kind'] == kind and o['request_id'] == message_id for o in outputs):
                outputs.append({'kind': kind, 'request_id': message_id, 'status': 'pending',
                                'expected_title': expected if kind == 'notion' else ''})
        return data
    store.change(key, update)


def _same_or_later(request_id, original_id):
    if request_id == original_id:
        return True
    # Project requests are Slack message timestamps. Never order opaque IDs
    # lexically or let unknown provenance satisfy a previous request.
    if not all(re.fullmatch(r"\d+\.\d+", str(value or "")) for value in (request_id, original_id)):
        return False
    try:
        return Decimal(request_id) > Decimal(original_id)
    except InvalidOperation:
        return False


def _record(data, receipt, request_id):
    if receipt.get('completion_eligible') is False:
        return
    for output in data.get('required_outputs', []):
        if (output.get('status') != 'pending' or not _same_or_later(request_id, output['request_id'])
                or output['kind'] not in receipt.get('outputs', [])):
            continue
        if output.get('expected_title') and output['expected_title'] not in receipt.get('observed_values', []):
            continue
        output.update(status='verified', receipt_id=receipt['id'], profile=receipt['profile'])


def record(store, key, receipt, request_id):
    if receipt.get('completion_eligible') is False:
        return
    def update(data):
        _record(data, receipt, request_id)
        return data
    store.change(key, update)


def reconcile(store, key):
    """Use persisted evidence and explicit cancellation; never replay tools."""
    def update(data):
        tasks = data.get('tasks', [])
        for task in tasks:
            for receipt in task.get('receipts', []):
                _record(data, receipt, receipt.get('request_id') or task['request_id'])
        kept = []
        for output in data.get('required_outputs', []):
            related = [t for t in tasks if t['request_id'] == output['request_id']]
            # Explicit domain obligations permit cancelling one domain. Legacy
            # tasks without a domain keep it pending unless all were cancelled.
            owners = [t for t in related if not t.get('required_outputs')
                      or output['kind'] in t['required_outputs']]
            if output.get('status') == 'pending' and owners and all(t['status'] == 'cancelled' for t in owners):
                continue
            kept.append(output)
        if 'required_outputs' in data:
            data['required_outputs'] = kept
        return data
    return store.change(key, update)


def check(task, receipts):
    expected = set(task.get('required_outputs', []))
    actual = {kind for r in receipts if r.get('completion_eligible') is not False for kind in r.get('outputs', [])}
    if expected - actual:
        raise ValueError('필수 산출물의 실제 변경 근거가 없다: ' + ', '.join(sorted(expected - actual)))


def check_close(project):
    missing = [o['kind'] for o in project.get('required_outputs', []) if o['status'] != 'verified']
    if missing:
        raise ValueError('원래 요청의 필수 산출물이 남아 있다: ' + ', '.join(sorted(set(missing))))
