"""Project-bound links to existing native work and verified Notion outputs."""
import importlib.util
from pathlib import Path
import re


def registry(cfg):
    path = cfg.get('common_work_store')
    module_path = cfg.get('common_work_module')
    if not path or not module_path or not Path(path).is_file():
        raise ValueError('공통 업무 연결 저장소를 확인하지 못했다.')
    spec = importlib.util.spec_from_file_location('project_common_work', module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.CommonWorkStore(path)


def artifacts(tool, result):
    common = result.get('common_work') or {}
    ids = [common['common_id']] if isinstance(common, dict) and common.get('common_id') else []
    pages = []
    if tool == 'notion':
        operations = result.get('operations', [result])
        if result.get('write_performed') is True and result.get('data', {}).get('object') == 'page':
            operations = [result['data']]
        for operation in operations:
            if not isinstance(operation, dict):
                continue
            page = operation.get('created') or operation.get('updated') or operation.get('page') or operation.get('result') or operation
            if isinstance(page, dict):
                identifier = page.get('page_id') or page.get('id')
                if identifier and re.fullmatch(r'[a-fA-F0-9-]{32,36}', str(identifier)):
                    pages.append({'id': identifier, 'url': page.get('url', '')})
    return {'common_ids': ids, 'notion_pages': pages}


def page_key(value):
    """Compare Notion pages by their 32-hex ID whether given dashed, bare or as a link."""
    runs = [r for r in re.findall(r'[a-fA-F0-9]+', str(value or '').replace('-', '')) if len(r) >= 32]
    return runs[-1][-32:].lower() if runs else ''


def observe(store, key, receipt):
    if not receipt.get('common_ids'):
        return
    def update(data):
        data['common_ids'] = sorted(set(data.get('common_ids', [])) | set(receipt['common_ids']))
        return data
    store.change(key, update)


def handle(cfg, store, key, turn, args):
    from gateway.project_requests import direct_request
    if turn['source']['profile'] == 'system-doctor':
        raise ValueError('진단담당에게 개인 업무·문서 연결을 전달하지 않는다.')
    project = store.get(key)
    ids = project.get('common_ids', [])
    common_id = args.get('common_id')
    if common_id:
        if common_id not in ids and common_id not in direct_request(turn['request']):
            raise ValueError('현재 프로젝트에 연결됐거나 소유자가 현재 요청에서 지정한 공통 업무 ID만 조회한다.')
        ids = [common_id]
    database = registry(cfg)
    if args['action'] == 'link_result':
        if turn['source']['profile'] != 'work-assistant':
            raise ValueError('Notion 결과 연결은 업무비서만 한다.')
        if len(ids) != 1:
            raise ValueError('common_id로 연결할 공통 업무 하나를 지정한다.')
        wanted = page_key(args.get('source_id'))
        if not wanted:
            raise ValueError('source_id에 Notion 페이지 ID나 링크를 넣는다.')
        # Same-turn receipts only; an omitted evidence_id selects the receipt that shows this page.
        evidence_id = args.get('evidence_id')
        candidates = ([turn['receipts'].get(evidence_id, {})] if evidence_id
                      else list(reversed(turn['receipts'].values())))
        receipt, page = next(((r, p) for r in candidates if r.get('completion_eligible') is not False
                              for p in r.get('notion_pages', []) if page_key(p['id']) == wanted), ({}, None))
        if not page:
            raise ValueError('이 페이지를 이번 턴의 notion 조회·작성 결과에서 찾지 못했다. 같은 턴에 notion으로 페이지를 연 뒤 다시 연결한다.')
        database.link_knowledge_ref(common_id=ids[0], ref_type='notion_page', external_id=page['id'],
            relation_type='documented_by', link_reason='verified_workflow_evidence',
            source_session_id=turn['session_id'], receipt_id=receipt['id'], metadata={'url': page['url'], 'project_id': key})
    items = []
    for identifier in ids[:10]:
        item = database.get(identifier)
        # Share object IDs and links, never native body/customer data or unrelated knowledge refs.
        items.append({'common_id': item['common_id'], 'title': item['title'], 'status': item['status'],
                      'sources': [{k: s.get(k) for k in ('domain', 'source_id')} for s in item.get('sources', [])],
                      'notion_pages': [{k: r.get(k) for k in ('external_id', 'metadata')} for r in item.get('knowledge_refs', []) if r['ref_type'] == 'notion_page']})
    return {'ok': True, 'links': items, 'notice': '공통 ID와 원본 연결이다. 원본 완료 상태나 자동 양방향 동기화를 뜻하지 않는다.'}
