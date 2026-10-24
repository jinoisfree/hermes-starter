"""Complete public Notion content API for profiles that explicitly enable full_api.

The checked-in input catalogue supplies routes, never caller-controlled URLs. Mutations
share notion.py's durable, conversation-bound undo journal. OAuth/admin/Notion AI
management is deliberately outside this content integration and its credentials.
"""
from __future__ import annotations

import copy
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import stat
import threading
import time
import urllib.parse
import uuid

VERSION = '2026-03-11'
ACTIONS = {'api', 'api_help'}
READ_POST = {'post-database-query', 'post-search', 'create-view-query', 'query-meeting-notes'}
CATALOG = json.loads(Path(__file__).with_name('notion-api.json').read_text())
OPS = CATALOG['operations']
LOCK = threading.RLock()
RECEIPTS = {}
CALLS = {}


def is_write(args):
    name = args.get('operation')
    return args.get('action') == 'api' and name in OPS and OPS[name]['method'] != 'GET' and name not in READ_POST


def help_result(args):
    ref = args.get('schema_ref')
    if ref:
        if ref not in CATALOG['references']:
            return {'ok': False, 'error': 'Unknown schema_ref. Copy a $ref from api_help.'}
        return {'ok': True, 'schema_ref': ref, 'schema': CATALOG['references'][ref]}
    name = args.get('operation')
    if name:
        if name not in OPS: return {'ok': False, 'error': 'Unknown operation. Use api_help.'}
        return {'ok': True, 'operation': name, **OPS[name],
                'instructions': 'Use api with operation, params (path/query values), body (JSON). Resolve $ref via api_help(schema_ref). upload-file uses file_path, offset?, length?, part_number?.'}
    return {'ok': True, 'api_version': VERSION, 'source': CATALOG['source'],
            'operations': [{'operation': name, 'method': op['method'], 'path': op['path'], 'summary': op['summary']}
                           for name, op in OPS.items()]}


def route(n, args):
    name = args.get('operation')
    if name not in OPS: raise n.NotionError('api_help에서 operation 목록을 확인하세요.')
    op = OPS[name]; params = args.get('params', {})
    if not isinstance(params, dict): raise n.NotionError('params must be an object')
    allowed = {p['name'] for p in op['parameters']}
    if params.keys() - allowed: raise n.NotionError('Unknown params: ' + ', '.join(sorted(params.keys() - allowed)))
    path = op['path']; query = []
    for p in op['parameters']:
        key = p['name']
        if key not in params:
            if p.get('required'): raise n.NotionError('Missing params.' + key)
            continue
        value = params[key]
        if isinstance(value, (dict, list)):
            if p['in'] == 'path': raise n.NotionError('Invalid path parameter: ' + key)
            values = value if isinstance(value, list) else [json.dumps(value)]
        else: values = [value]
        if p['in'] == 'path':
            decoded = urllib.parse.unquote(value) if isinstance(value, str) else ''
            if (not decoded or decoded in ('.', '..') or any(c in decoded for c in '\r\n\x00?#\\')
                    or ('/' in decoded and key != 'property_id') or '..' in decoded.split('/')):
                raise n.NotionError('Invalid path parameter: ' + key)
            # Property IDs may contain encoded punctuation; keep them in one URL segment.
            path = path.replace('{' + key + '}', urllib.parse.quote(decoded, safe=''))
        else:
            for value in values:
                if isinstance(value, bool): value = str(value).lower()
                query.append((key, value))
    body = args.get('body')
    if body is not None and not isinstance(body, dict): raise n.NotionError('body must be a JSON object')
    if op['method'] in ('GET', 'DELETE') and body is not None: raise n.NotionError('This operation does not accept body')
    if len(json.dumps(body, ensure_ascii=False).encode()) > 500 * 1024:
        raise n.NotionError('Notion 요청 본문은 500 KiB 이하로 나누세요.')
    return op['method'], path + ('?' + urllib.parse.urlencode(query) if query else ''), copy.deepcopy(body)


def request(n, home, method, path, body=None, opener=None, **kwargs):
    return n.request(home, method, path, body, opener=opener, version=VERSION, **kwargs)


def read(n, home, path, opener):
    return request(n, home, 'GET', path, opener=opener)


def stable(value):
    """Ignore server bookkeeping, but compare content, IDs, children and schema exactly."""
    if isinstance(value, dict):
        if value.get('type') == 'file' and isinstance(value.get('file'), dict) and value['file'].get('url'):
            value = copy.deepcopy(value)
            parsed = urllib.parse.urlsplit(value['file']['url'])
            value['file']['url'] = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', ''))
        return {k: stable(v) for k, v in value.items() if k not in {
            'request_id', 'last_edited_time', 'last_edited_by', 'created_time', 'created_by', 'expiry_time',
            'public_url', 'plain_text', 'href', 'has_more', 'next_cursor'}}
    if isinstance(value, list): return [stable(v) for v in value]
    return value


def fingerprint(value):
    return hashlib.sha256(json.dumps(stable(value), ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def clean(value):
    if isinstance(value, list): return [clean(v) for v in value]
    if not isinstance(value, dict): return value
    return {k: clean(v) for k, v in value.items() if k not in {
        'plain_text', 'href', 'created_time', 'last_edited_time', 'created_by', 'last_edited_by', 'expiry_time'}}


def restore_value(n, value):
    """Response -> write payload. Signed Notion files cannot be recreated from an expiring URL."""
    if isinstance(value, list): return [restore_value(n, v) for v in value]
    if not isinstance(value, dict): return value
    if value.get('type') == 'file' and isinstance(value.get('file'), dict) and value['file'].get('url'):
        raise n.NotionError('Notion 내부 첨부는 API가 반환한 임시 URL만으로 원형 복원을 보장할 수 없습니다.')
    if value.get('type') == 'person' or value.get('object') == 'user': return {'id': value['id']}
    return {k: restore_value(n, v) for k, v in clean(value).items() if k not in {'object'}}


def prop_by(properties, key):
    if key in properties: return key, properties[key]
    for name, p in properties.items():
        if urllib.parse.unquote(str(p.get('id', ''))) == urllib.parse.unquote(key): return name, p
    return key, None


def all_items(n, home, path, opener, method='GET', body=None):
    values = []; cursor = None; seen = set()
    while True:
        b = copy.deepcopy(body or {})
        url = path
        if method == 'GET': url += ('&' if '?' in path else '?') + urllib.parse.urlencode({'page_size': 100, **({'start_cursor': cursor} if cursor else {})})
        else:
            b['page_size'] = 100
            if cursor: b['start_cursor'] = cursor
        result = request(n, home, method, url, b if method != 'GET' else None, opener)
        values.extend(result.get('results', []))
        if not result.get('has_more'): return values
        cursor = result.get('next_cursor')
        if not cursor or cursor in seen: raise n.NotionError('Notion 페이지 나눔 응답이 불완전합니다.')
        seen.add(cursor)


def page_property(n, home, page, key, opener):
    name, prop = prop_by(page.get('properties', {}), key)
    if prop is None: raise n.NotionError('원래 속성을 찾지 못했습니다: ' + key)
    kind = prop.get('type'); value = copy.deepcopy(prop.get(kind))
    if kind in ('title', 'rich_text', 'people', 'relation') and (prop.get('has_more') or (isinstance(value, list) and len(value) >= 25)):
        pid = urllib.parse.quote(urllib.parse.unquote(prop['id']), safe='')
        parts = all_items(n, home, f"pages/{page['id']}/properties/{pid}", opener)
        value = []
        for part in parts:
            given = part.get(kind)
            if isinstance(given, list): value.extend(given)
            elif given is not None: value.append(given)
    if kind in ('formula', 'rollup', 'created_time', 'last_edited_time', 'created_by', 'last_edited_by', 'unique_id', 'button'):
        raise n.NotionError('계산·읽기 전용 속성입니다: ' + str(kind))
    if kind in ('select', 'status'): value = {'name': value['name']} if value else None
    elif kind == 'multi_select': value = [{'name': v['name']} for v in value or []]
    elif kind in ('people', 'relation'): value = [{'id': v['id']} for v in value or []]
    return name, {kind: restore_value(n, value)}


def resource_path(name, path):
    path = path.split('?')[0]
    if name in ('move-page', 'update-page-markdown'): return path.rsplit('/', 1)[0] if name == 'move-page' else path
    if name == 'patch-block-children': return path
    return path


def deleting(name, body, before=None):
    if OPS[name]['method'] == 'DELETE': return True
    def flags(x):
        if isinstance(x, dict):
            if any(x.get(k) is True for k in ('archived', 'is_archived', 'in_trash', 'erase_content', 'allow_deleting_content')): return True
            return any(flags(v) for v in x.values())
        if isinstance(x, list): return any(flags(v) for v in x)
        return False
    if flags(body): return True
    if name == 'update-a-data-source':
        for key, given in (body or {}).get('properties', {}).items():
            if given is None: return True
            _, old = prop_by((before or {}).get('properties', {}), key)
            if old:
                for kind in ('select', 'multi_select', 'status'):
                    options = given.get(kind, {}).get('options')
                    if isinstance(options, list):
                        if any(not any((v.get('id') and v['id'] == oldv.get('id')) or (v.get('name') and v['name'] == oldv.get('name')) for v in options)
                               for oldv in old.get(kind, {}).get('options', [])): return True
    return False


def payload_part(n, args, files_config):
    if args.get('operation') != 'upload-file': return None
    if not isinstance(files_config, dict) or files_config.get('enabled') is not True: raise n.NotionError('파일 도구가 꺼져 있습니다.')
    roots = n._files.folders(files_config) + [n._files.attachments(), n._files.renders()]
    path = n._files.inside(args.get('file_path'), roots)
    if path is None: raise n.NotionError('파일 허용 경로 밖입니다.')
    offset, length = args.get('offset', 0), args.get('length', 20 * 1024 * 1024)
    if type(offset) is not int or offset < 0 or type(length) is not int or not 1 <= length <= 20 * 1024 * 1024:
        raise n.NotionError('offset>=0, length=1..20 MiB로 파일을 나눠 전송하세요.')
    part = args.get('part_number')
    if part is not None and (type(part) is not int or not 1 <= part <= 1000): raise n.NotionError('part_number=1..1000')
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in path.parts[1:-1]:
            new = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd); fd = new
        source = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    finally: os.close(fd)
    with os.fdopen(source, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode): raise n.NotionError('일반 파일만 업로드합니다.')
        if part is None and (offset or info.st_size > length): raise n.NotionError('20 MiB 초과 파일은 multi_part 생성 후 part_number·offset·length로 나눠 보내세요.')
        if offset >= info.st_size: raise n.NotionError('파일 내용이 없거나 offset이 파일 끝을 넘습니다.')
        stream.seek(offset); raw = stream.read(length)
    return {'bytes': raw, 'sha256': hashlib.sha256(raw).hexdigest(), 'filename': path.name,
            'mime': mimetypes.guess_type(path.name)[0] or 'application/octet-stream', 'part_number': part}


def api_plan(n, config, home, args, files_config, opener):
    if config.get('full_api') is not True: raise n.NotionError('이 프로필은 확장 Notion API가 꺼져 있습니다.')
    method, path, body = route(n, args)
    if not is_write(args): return {'method': method, 'path': path, 'body': body}
    if n.writable(config) is not None: raise n.NotionError('확장 API 쓰기는 전체 연결 범위를 허용한 프로필에서만 실행합니다.')
    if n.conversation() is None: raise n.NotionError('Notion 변경은 소유자가 요청한 식별 가능한 Slack 대화에서 실행합니다.')
    name = args['operation']; part = payload_part(n, args, files_config)
    plan = {'method': method, 'path': path, 'body': body, 'operation': name, 'before': {}, 'inverse': [], 'limitations': []}
    if part: plan['file'] = {k: v for k, v in part.items() if k != 'bytes'}
    target = resource_path(name, path)
    modifies = method in ('PATCH', 'DELETE') or name == 'move-page'
    if modifies:
        if name == 'patch-block-children':
            before = {'results': all_items(n, home, target, opener)}
        elif name == 'delete-view-query':
            before = {}  # Ephemeral query cleanup has no durable inverse.
        else: before = read(n, home, target, opener)
        plan['before'][target] = before
        build_inverse(n, home, plan, opener)
    plan['deletion'] = deleting(name, body, plan['before'].get(target))
    return plan


def step(method, path, body=None, *, verify=None, expected=None):
    result = {'method': method, 'path': path, 'body': body, 'state': 'ready'}
    if verify is not None: result['verify'] = verify; result['expected'] = expected
    return result


def build_inverse(n, home, plan, opener):
    name, path, body = plan['operation'], resource_path(plan['operation'], plan['path']), plan['body'] or {}
    before = plan['before'][path]; inv = {}; extras = []
    try:
        if name == 'patch-block-children': return  # Added block IDs arrive in the response.
        if name == 'delete-a-block': inv = {'in_trash': False}
        elif name == 'delete-view-query':
            plan['limitations'].append('일회성 보기 조회 삭제는 복원 대상이 아닙니다.'); return
        elif name in ('delete-view', 'delete-a-comment'):
            plan['limitations'].append('Notion API는 삭제한 보기·댓글을 같은 ID로 복원하지 못합니다.'); return
        elif name == 'move-page':
            parent = before.get('parent', {}); kind = parent.get('type')
            if kind not in ('page_id', 'data_source_id'): raise n.NotionError('원래 위치로 이동하는 API가 없습니다.')
            inv = {'parent': {kind: parent[kind]}}
            plan['inverse'].append(step('POST', plan['path'], inv, verify=path, expected={'parent': before['parent']})); return
        elif name == 'update-page-markdown':
            if before.get('truncated') or before.get('unknown_block_ids'): raise n.NotionError('본문 일부가 생략되어 전체 복원을 보장할 수 없습니다.')
            inv = {'type': 'replace_content', 'replace_content': {'new_str': before['markdown']},
                   'markdown_version': 'v1'}
        elif name == 'patch-page':
            for key in body:
                if key == 'properties':
                    inv[key] = {}
                    for prop in body[key]:
                        oldname, value = page_property(n, home, before, prop, opener); inv[key][oldname] = value
                elif key in ('erase_content', 'template'):
                    if key == 'template': plan['limitations'].append('템플릿은 비동기로 본문·속성을 바꾸므로 자동 복원을 보장하지 않습니다.')
                    markdown_path = path + '/markdown'
                    markdown = read(n, home, markdown_path, opener); plan['before'][markdown_path] = markdown
                    if markdown.get('truncated') or markdown.get('unknown_block_ids'): raise n.NotionError('일부 본문을 읽지 못해 본문 복원은 제한됩니다.')
                    extras = [step('PATCH', markdown_path, {'type': 'replace_content', 'replace_content': {'new_str': markdown['markdown']}}, verify=markdown_path, expected={'markdown': markdown['markdown']})]
                elif key in ('archived', 'is_archived', 'in_trash'): inv['in_trash'] = bool(before.get('in_trash', before.get('archived', False)))
                elif key != 'notifications': inv[key] = restore_value(n, before.get(key))
        elif name == 'update-a-data-source':
            inv, extras = schema_inverse(n, home, plan, opener)
        elif name == 'update-a-block':
            for key in body:
                if key in ('archived', 'in_trash'): inv['in_trash'] = bool(before.get('in_trash', before.get('archived', False)))
                elif key == 'type': continue
                else:
                    original = before.get(key)
                    if isinstance(body[key], dict) and isinstance(original, dict): original = {k: original.get(k) for k in body[key]}
                    inv[key] = restore_value(n, original)
        elif name == 'update-a-comment': inv = {'rich_text': restore_value(n, before['rich_text'])}
        else:
            for key in body: inv[key] = restore_value(n, before.get(key))
        if inv:
            verify = path
            expect = copy.deepcopy(inv)
            if name == 'update-page-markdown': expect = {'markdown': before['markdown']}
            elif name == 'update-a-comment': expect = {'rich_text': clean(before['rich_text'])}
            plan['inverse'].append(step('PATCH', path, inv, verify=verify, expected=expect))
        plan['inverse'].extend(extras)
    except (n.NotionError, KeyError, TypeError) as exc:
        # Preserve snapshots and let explicitly requested changes proceed; never promise an unavailable undo.
        plan['limitations'].append(str(exc)); plan['inverse'] = []


def schema_inverse(n, home, plan, opener):
    path = plan['path']; before = plan['before'][path]; body = plan['body']; inv = {}; row_fields = []
    for key, val in body.items():
        if key != 'properties': inv[key] = restore_value(n, before.get(key)); continue
        inv['properties'] = {}
        for field, update in val.items():
            oldname, original = prop_by(before.get('properties', {}), field)
            if original is None:
                inv['properties'][field] = None
                continue
            kind = original['type']; old = {k: restore_value(n, original[k]) for k in ('name', 'description', 'type', kind) if k in original}
            # Prefer stable IDs for a renamed property; deleted properties must be recreated by name.
            target = oldname if update is None else original.get('id', oldname)
            inv['properties'][target] = old
            newkind = (update or {}).get('type') or next((k for k in (update or {}) if k not in ('name', 'description')), kind)
            if update is None or newkind != kind or any(k in (update or {}) for k in ('select', 'multi_select', 'status')):
                if kind not in ('formula', 'rollup', 'created_time', 'last_edited_time', 'created_by', 'last_edited_by', 'unique_id', 'button'): row_fields.append(oldname)
    extra = []
    if row_fields:
        rows = all_items(n, home, path + '/query', opener, method='POST')
        for row in rows:
            pagepath = 'pages/' + row['id']; plan['before'][pagepath] = row
            props = dict(page_property(n, home, row, key, opener) for key in row_fields)
            extra.append(step('PATCH', pagepath, {'properties': props}, verify=pagepath, expected={'properties': props}))
    return inv, extra


def projection(actual, wanted):
    if not isinstance(wanted, dict) or not isinstance(actual, dict): return copy.deepcopy(actual)
    result = {}
    for key, value in wanted.items():
        if key == 'properties' and isinstance(value, dict):
            result[key] = {}
            for name, prop in value.items():
                oldname, original = prop_by(actual.get(key, {}), name)
                if original is not None: result[key][oldname] = copy.deepcopy(original)
        elif key in actual: result[key] = projection(actual[key], value)
    return result


def matches(actual, expected):
    """Check requested fields while allowing Notion response metadata/defaults."""
    if isinstance(expected, dict):
        if not isinstance(actual, dict): return False
        if 'file_upload' in expected:
            if not (actual.get('type') == 'file' and actual.get('file', {}).get('url')): return False
            return matches(actual, {k: v for k, v in expected.items() if k not in ('type', 'file_upload')})
        for k, v in expected.items():
            if k in ('type', 'object') and k not in actual: continue
            if k == 'properties' and isinstance(v, dict):
                for field, val in v.items():
                    _, found = prop_by(actual.get('properties', {}), field)
                    if val is None:
                        if found is not None: return False
                    elif found is None or not matches(found, val): return False
            elif k not in actual or not matches(actual[k], v): return False
        return True
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(matches(a, b) for a, b in zip(actual, expected))
    return actual == expected


def remember(n, home, args, plan, call_id):
    with LOCK:
        for key, item in list(RECEIPTS.items()):
            if time.time() - item['time'] >= 3600: RECEIPTS.pop(key)
        while len(RECEIPTS) >= 128: RECEIPTS.pop(next(iter(RECEIPTS)))
        for key, token in list(CALLS.items()):
            if token not in RECEIPTS: CALLS.pop(key)
        token = uuid.uuid4().hex
        RECEIPTS[token] = {'home': str(home), 'digest': n.call_digest(args), 'who': n.conversation(), 'time': time.time(), 'plan': copy.deepcopy(plan)}
        CALLS[(str(home), call_id or id(args))] = token


def approval_arguments(n, home, args, call_id):
    with LOCK:
        token = CALLS.pop((str(home), call_id or id(args)), None); item = RECEIPTS.get(token)
        if item is None or item['digest'] != n.call_digest(args): return {'action': 'block', 'message': 'Notion 실행 전 확인 정보가 없습니다. 다시 호출하세요.'}
        return {'action': 'modify', 'args': {'_approval_snapshot': token}}


def consume(n, home, args, required):
    with LOCK: item = RECEIPTS.pop(args.get('_approval_snapshot'), None)
    if item is None:
        if required or args.get('_approval_snapshot'): raise n.NotionError('Notion 실행 확인 정보가 없거나 이미 사용됐습니다.')
        return None
    if item['home'] != str(home) or item['digest'] != n.call_digest(args) or item['who'] != n.conversation() or time.time() - item['time'] >= 3600:
        raise n.NotionError('Notion 실행 확인 정보가 만료되었거나 대화·인수가 달라졌습니다.')
    return item['plan']


def latest(data, who):
    return next((e for e in reversed(data['entries']) if e['conversation'] == who and not e['consumed']), None)


def rollback_plan(n, home, args, opener):
    with n.journal(home) as (data, save):
        entry = latest(data, n.conversation())
        if entry is None or entry.get('kind') != 'api': return None
        if args.get('key') and args['key'] != entry['key']: raise n.NotionError('ROLLBACK_KEY_NOT_LATEST')
        if not entry.get('undo_ready') or not entry.get('inverse') or entry.get('limitations'):
            raise n.NotionError('최근 API 변경의 완전한 자동 복원을 보장할 수 없습니다. report와 저장된 원본을 확인하세요.')
        after = {}
        for path in entry['after']:
            value = read(n, home, path, opener)
            if fingerprint(value) != fingerprint(entry['after'][path]): raise n.NotionError('그 사이 Notion에서 바뀌었습니다. 최신 변경을 덮어쓰지 않습니다.')
            after[path] = value
        deletion = any(s['method'] == 'DELETE' or any(s.get('body', {}).get(k) is True for k in ('in_trash', 'archived')) or
                       any(v is None for v in s.get('body', {}).get('properties', {}).values()) for s in entry['inverse'])
        return {'rollback': entry['key'], 'after': after, 'inverse': entry['inverse'], 'deletion': deletion,
                'older': time.time() - entry['created_at'] >= 1800, 'title': entry['title']}


def guard(n, config, home, args, chat, files_config, opener, call_id):
    try:
        if config.get('full_api') is not True: raise n.NotionError('이 프로필은 확장 Notion API가 꺼져 있습니다.')
        if args.get('action') == 'api_help': return None
        if args.get('action') == 'rollback':
            if not chat or not n.conversation(): raise n.NotionError('되돌리기는 같은 사용자 대화에서 실행합니다.')
            plan = rollback_plan(n, home, args, opener)
            if plan is None: return None
        else:
            if not is_write(args): route(n, args); return None
            if not chat: raise n.NotionError('예약·배경 실행에서는 Notion을 변경하지 않습니다.')
            plan = api_plan(n, config, home, args, files_config, opener)
        remember(n, home, args, plan, call_id)
        approval = plan.get('deletion') or plan.get('older') or config.get('approval') != 'deletion_only'
        if approval:
            return {'action': 'approve', 'rule_key': 'team-kit-notion-api:' + n.call_digest(args)[:16],
                    'message': ('승인하면 Notion 최근 변경을 복원합니다. ' if plan.get('rollback') else '승인하면 Notion 삭제를 실행합니다. ') +
                               str(plan.get('operation', plan.get('title', ''))) + '\n' + str(plan.get('path', '')) +
                               '\n' + json.dumps(plan.get('body', plan.get('inverse')), ensure_ascii=False)[:4000]}
        return None
    except (n.NotionError, OSError, ValueError, KeyError, TypeError) as exc:
        return {'action': 'block', 'message': str(exc)}


def multipart(part):
    boundary = 'notion-' + uuid.uuid4().hex
    filename = part['filename'].replace('"', '_').replace('\r', '_').replace('\n', '_')
    data = b''
    if part['part_number'] is not None:
        data += f'--{boundary}\r\nContent-Disposition: form-data; name="part_number"\r\n\r\n{part["part_number"]}\r\n'.encode()
    data += f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\nContent-Type: {part["mime"]}\r\n\r\n'.encode()
    data += part['bytes'] + f'\r\n--{boundary}--\r\n'.encode()
    return data, 'multipart/form-data; boundary=' + boundary


def complete_inverse(n, home, plan, result, opener):
    name = plan['operation']; inverse = copy.deepcopy(plan['inverse']); watches = set(plan['before'])
    ident = result.get('id')
    created_paths = {'post-page': 'pages', 'create-database': 'databases', 'create-a-database': 'data_sources',
                     'create-view': 'views', 'create-a-comment': 'comments', 'create-meeting-note': 'blocks'}
    if name in created_paths and ident:
        path = created_paths[name] + '/' + ident; watches = {path}
        if created_paths[name] in ('views', 'comments', 'blocks'): inverse = [step('DELETE', path)]
        else: inverse = [step('PATCH', path, {'in_trash': True}, verify=path, expected={'in_trash': True})]
    elif name == 'patch-block-children':
        ids = [b['id'] for b in result.get('results', []) if b.get('id')]
        if len(ids) != len((plan['body'] or {}).get('children', [])) or result.get('has_more'):
            plan['limitations'].append('추가 블록 ID를 모두 받지 못했습니다.')
        watches = {'blocks/' + i for i in ids}
        inverse = [step('DELETE', 'blocks/' + i, verify='blocks/' + i, expected={'in_trash': True}) for i in reversed(ids)]
    if name in ('delete-a-comment', 'delete-view', 'delete-view-query'): watches = set()
    after = {}
    for path in watches: after[path] = read(n, home, path, opener)
    return inverse, after


def verified(plan, result, after):
    if result.get('object') == 'async_task': return False
    name, body = plan['operation'], plan['body'] or {}
    if name == 'patch-block-children':
        children = body.get('children', []); created = result.get('results', [])
        return (bool(after) and len(after) == len(children) == len(created) and
                all(matches(after.get('blocks/' + item.get('id', ''), {}), expected)
                    for item, expected in zip(created, children)))
    if name == 'update-page-markdown':
        current = after.get(plan['path'], {})
        if current.get('truncated') or 'markdown' not in current: return False
        if body.get('type') == 'replace_content': return current['markdown'] == body['replace_content']['new_str']
        # The endpoint applies all search/replace edits atomically; readback is also retained for undo.
        return 'markdown' in result
    if name == 'patch-page' and ('template' in body or body.get('erase_content')):
        return False  # Template application can be asynchronous even with an HTTP 200 page response.
    if plan['method'] in ('PATCH', 'POST') and plan['before']:
        current = after.get(resource_path(name, plan['path']), {})
        expect = {k: v for k, v in body.items() if k not in ('notifications', 'allow_async')}
        if name == 'update-a-data-source' and 'properties' in expect:
            original = plan['before'][plan['path']].get('properties', {})
            expect['properties'] = {(prop_by(original, k)[1] or {}).get('id', k): v for k, v in expect['properties'].items()}
        return matches(current, expect)
    if plan['method'] == 'DELETE':
        if name == 'delete-a-block': return bool(result.get('in_trash') or result.get('archived'))
        return result.get('object') != 'error'
    return result.get('object') != 'error' and bool(result)


def handle(n, config, home, args, files_config, opener, chat, require_snapshot):
    if config.get('full_api') is not True: raise n.NotionError('이 프로필은 확장 Notion API가 꺼져 있습니다.')
    if args['action'] == 'api_help': return help_result(args)
    if not is_write(args):
        method, path, body = route(n, args)
        return {'ok': True, 'operation': args['operation'], 'data': request(n, home, method, path, body, opener)}
    if chat is False: raise n.NotionError('예약·배경 실행에서는 Notion을 변경하지 않습니다.')
    approved = consume(n, home, args, require_snapshot)
    plan = api_plan(n, config, home, args, files_config, opener)
    if approved is not None and approved != plan: raise n.NotionError('실행 전 Notion 원본 또는 파일이 바뀌었습니다. 다시 읽고 합치세요.')
    who = n.conversation()
    with n.journal(home) as (data, save):
        entry = {'kind': 'api', 'conversation': who, 'created_at': time.time(), 'title': args['operation'],
                 'key': uuid.uuid4().hex, 'consumed': False, 'status': 'pending', 'operations': [],
                 'request': {k: v for k, v in args.items() if not k.startswith('_')},
                 'before': plan['before'], 'inverse': plan['inverse'], 'after': {}, 'limitations': plan['limitations']}
        rechecked = api_plan(n, config, home, args, files_config, opener)
        if rechecked != plan: raise n.NotionError('변경 직전 Notion 원본이 바뀌었습니다. 다시 읽고 합치세요.')
        data['entries'].append(entry); save()  # A crash/timeout must leave a durable pending record, never a retryable success.
        try:
            body, content_type = plan['body'], None
            if 'file' in plan:
                part = payload_part(n, args, files_config)
                if {k: v for k, v in part.items() if k != 'bytes'} != plan['file']: raise n.NotionError('업로드 파일이 바뀌었습니다.')
                body, content_type = multipart(part)
            result = request(n, home, plan['method'], plan['path'], body, opener, **({'content_type': content_type, 'timeout': 60} if content_type else {}))
            entry['result'] = result; entry['status'] = 'unverified'; save()
            inverse, after = complete_inverse(n, home, plan, result, opener)
            entry.update(inverse=inverse, after=after, limitations=plan['limitations'],
                         undo_ready=bool(inverse) and bool(after) and result.get('object') != 'async_task')
            good = verified(plan, result, after); entry['status'] = 'done' if good else 'unverified'; save()
            report = {'operation': args['operation'], 'target': plan['path'], 'changed_fields': [f'properties.{key}' for key in (plan['body'] or {}).get('properties', {})] + [key for key in (plan['body'] or {}) if key != 'properties'],
                      'before': plan['before'], 'after': after, 'limitations': entry['limitations']}
            # Small report metadata + exact API result; full snapshots stay in the private journal.
            report.pop('before'); report.pop('after')
            return {'ok': good, 'status': entry['status'], 'data': result, 'report': report,
                    'operation': args['operation'], 'write_performed': True, 'approval_required': False,
                    'verification': {'readback': good}, 'rollback_available': entry['undo_ready'] and not entry['limitations'],
                    'rollback_key': entry['key'], 'message': '생성·수정 결과를 사용자에게 보고하세요. 미확인/실패는 성공으로 보고하거나 재시도하지 마세요.'}
        except (n.NotionError, OSError, ValueError, KeyError, TypeError) as exc:
            entry['status'] = 'unknown' if getattr(exc, 'unknown', False) or entry.get('result') else 'failed'; entry['error'] = str(exc)
            if entry['status'] == 'failed': entry['consumed'] = True
            save()
            return {'ok': False, 'status': entry['status'], 'error': str(exc), 'rollback_key': entry['key'],
                    'rollback_available': False, 'write_performed': bool(entry.get('result')), 'approval_required': False}


def rollback(n, config, home, args, opener, require_snapshot):
    if n.writable(config) is not None or config.get('rollback_enabled', True) is False: raise n.NotionError('Notion 복원 범위 설정을 확인하세요.')
    approved = consume(n, home, args, require_snapshot)
    plan = rollback_plan(n, home, args, opener)
    if plan is None: raise n.NotionError('NO_RECENT_NOTION_API_ROLLBACK')
    if approved is not None and approved != plan: raise n.NotionError('되돌릴 기록 또는 현재 값이 바뀌었습니다.')
    with n.journal(home) as (data, save):
        entry = latest(data, n.conversation())
        if entry['key'] != plan['rollback']: raise n.NotionError('ROLLBACK_KEY_NOT_LATEST')
        results = []
        for s in entry['inverse']:
            if s['state'] == 'done': continue
            if s['state'] != 'ready': raise n.NotionError('이전 복원의 반영 여부가 불명확합니다. 먼저 현재 값을 확인하세요.')
            s['state'] = 'pending'; save()
            try:
                response = request(n, home, s['method'], s['path'], s['body'], opener)
                if s.get('verify'):
                    actual = read(n, home, s['verify'], opener)
                    if not matches(actual, s['expected']): raise n.NotionError('복원 뒤 값이 일치하지 않습니다.')
                if s.get('verify') in entry['after']: entry['after'][s['verify']] = actual
                s['state'] = 'done'; results.append({'target': s['path'], 'status': 'restored'}); save()
            except (n.NotionError, OSError, KeyError, ValueError, TypeError) as exc:
                s['state'] = 'unknown'; save()
                return {'ok': False, 'status': 'partial', 'error': str(exc), 'results': results, 'approval_required': False}
        entry['consumed'] = True; save()
        return {'ok': True, 'status': 'restored', 'results': results, 'approval_required': False, 'key': entry['key']}


def is_api_rollback(n, home):
    if n.conversation() is None: return False
    with n.journal(home) as (data, save):
        entry = latest(data, n.conversation())
        return entry is not None and entry.get('kind') == 'api'


def extend_schema(result):
    result = copy.deepcopy(result)
    p = result['parameters']['properties']
    p['action']['enum'] = sorted(set(p['action']['enum']) | ACTIONS)
    p.update(operation={'type': 'string', 'description': 'Official operation ID from api_help. Call api_help(operation=...) for input schema.'},
             params={'type': 'object', 'description': 'Path/query parameters from api_help. IDs only, not URLs.'},
             body={'type': 'object', 'description': 'Official API JSON request body. $ref schemas via api_help(schema_ref=...).'},
             schema_ref={'type': 'string', 'description': 'api_help only: exact $ref to inspect.'},
             file_path={'type': 'string', 'description': 'api upload-file: absolute allowed local file path, any supported file type.'},
             offset={'type': 'integer', 'minimum': 0}, length={'type': 'integer', 'minimum': 1, 'maximum': 20971520},
             part_number={'type': 'integer', 'minimum': 1, 'maximum': 1000})
    result['description'] = ('Notion 전체 문서 API: api_help로 46개 기능/입력 형식을 찾고 api(operation,params,body)로 실행. '
        '페이지·표·데이터 소스·모든 열 종류·보기·블록·마크다운·이동·댓글·파일 업로드를 지원. '
        '연결에 공유된 모든 위치에서 생성 즉시, 수정 먼저 실행·후보고, 삭제만 승인. rollback은 최근 변경 복원(삭제를 수반하면 승인). '
        '읽기·수정 결과의 report/verification/rollback_available을 확인하고 복원 제한을 숨기지 말 것. ' + result['description'])
    return result
