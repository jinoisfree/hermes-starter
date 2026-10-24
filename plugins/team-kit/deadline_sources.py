"""Current read evidence and per-day source facts for deadline reports (never edit originals)."""
from collections import OrderedDict
from datetime import datetime
from urllib.parse import urlparse
import json
import re
import threading
import time

_READS={'web_extract','browser_navigate','browser_snapshot'}
_EVIDENCE=OrderedDict()
_LOCK=threading.RLock()


def strings(value):
    if isinstance(value,str):yield value
    elif isinstance(value,dict):
        for item in value.values():yield from strings(item)
    elif isinstance(value,list):
        for item in value:yield from strings(item)


def observe(home,*,tool_name='',result=None,tool_call_id='',session_id='',turn_id='',args=None,parser=None,**_):
    if tool_name not in _READS or not all((tool_call_id,session_id,turn_id)):return
    data=parser(tool_name,result)
    if not isinstance(data,dict):return
    if tool_name=='web_extract':
        rows=[r for r in data.get('results',[]) if isinstance(r,dict) and not r.get('error')
              and r.get('url') and (r.get('content') or r.get('raw_content'))]
        if not rows:return
        data={'ok':True,'results':rows}
    if not (data.get('ok') is True or data.get('success') is True):return
    if data.get('error') or data.get('partial') or data.get('failures'):return
    if len(json.dumps(data,ensure_ascii=False))>300000:return
    key=(str(home),session_id,turn_id,tool_call_id)
    # Only returned URL/text count. A requested URL alone is not proof it loaded after redirects.
    with _LOCK:
        _EVIDENCE[key]={'at':time.monotonic(),'data':data}
        while len(_EVIDENCE)>64:_EVIDENCE.popitem(last=False)


def available(home,session_id,turn_id):
    rows=[]
    with _LOCK:
        for key,value in _EVIDENCE.items():
            if key[:3] != (str(home),session_id,turn_id) or time.monotonic()-value['at']>600:continue
            data=value['data']
            urls=[row.get('url') for row in data.get('results',[]) if row.get('url')] if 'results' in data else [data.get('url')]
            rows.append({'source_receipt_id':key[3],'returned_urls':[u for u in urls if u]})
    return rows


def record(home,args,now,*,session_id='',turn_id='',cache=()):
    url=args.get('official_url','')
    from tools.url_safety import is_safe_url
    parsed=urlparse(url)
    if parsed.scheme not in {'http','https'} or not is_safe_url(url):raise ValueError('official_source_public_url_required')
    for previous in cache:
        if (previous.get('url')==url and args.get('id') and previous.get('item_id')==args['id']
                and previous.get('status')=='checked' and previous.get('checked_at','')[:10]==now.date().isoformat()):
            return {**previous,'cache_reused':True}
    base={'url':url,'item_id':args.get('id'),'checked_at':now.isoformat(),'official_due':None,'official_time':'','official_location':'','cache_reused':False}
    if args.get('source_unavailable'):
        return {**base,'status':'unverified','reason':'출처 미확인'}
    with _LOCK:
        receipt=_EVIDENCE.get((str(home),session_id,turn_id,args.get('source_receipt_id','')))
    if not receipt or time.monotonic()-receipt['at']>600:raise ValueError('official_source_current_read_receipt_required')
    data=receipt['data']
    if 'results' in data:
        matching=[r for r in data['results'] if r.get('url')==url]
        if len(matching)!=1:raise ValueError('official_source_returned_url_required')
        data=matching[0]
    leaves=list(strings(data));quote=args.get('source_quote','')
    if not 1<=len(quote)<=600 or not any(quote in value for value in leaves):raise ValueError('official_source_exact_quote_required')
    if not any(url==value or url in value.split() for value in leaves):raise ValueError('official_source_returned_url_required')
    if not args.get('official_basis'):raise ValueError('official_source_operator_relationship_required')
    due=args.get('official_due','')
    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',due):raise ValueError('official_source_explicit_date_required')
    day=datetime.fromisoformat(due).date()
    patterns=[f'{day.year}-{day.month:02d}-{day.day:02d}',f'{day.year}.{day.month}.{day.day}',f'{day.year}/{day.month}/{day.day}',f'{day.year}년 {day.month}월 {day.day}일']
    # Quote must include the full date rather than a model-invented year.
    patterns.extend(f'{day.year}{sep}{day.month:02d}{sep}{day.day:02d}' for sep in ('.','/'))
    compact=re.sub(r'\s+','',quote)
    if not any(re.sub(r'\s+','',p) in compact for p in patterns):raise ValueError('official_source_date_not_in_quote')
    for field in ('official_time','official_location'):
        if args.get(field) and args[field] not in quote:raise ValueError('official_source_field_not_in_quote')
    return {**base,'status':'checked','official_due':due,'official_time':args.get('official_time',''),
        'official_location':args.get('official_location',''),'basis':args['official_basis'][:300]}


def description(check,due,now):
    if not check or check.get('checked_at','')[:10]!=now.date().isoformat():
        return '출처 미확인 · 공식 원문을 오늘 확인하지 못함'
    if check.get('status')!='checked':return f"출처 미확인 · {check['url']} · 확인 {check['checked_at']} KST"
    status='일치' if check.get('official_due')==due else '불일치'
    detail=f"공식 출처 {status} · 원문 마감 {check['official_due']} · 시각 {check.get('official_time') or '미확인'} · 장소 {check.get('official_location') or '미확인'}"
    return f"{detail.strip()} · {check['url']} · 확인 {check['checked_at']} KST"+(' · 기록은 변경하지 않음' if status=='불일치' else '')
