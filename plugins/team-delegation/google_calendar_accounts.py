"""Account-bound Calendar API; company EventKit links remain unchanged during migration."""
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo
import json
import os
import re
import tempfile

KST = ZoneInfo('Asia/Seoul')
EVENTS = 'https://www.googleapis.com/auth/calendar.events'
CALENDARS = 'https://www.googleapis.com/auth/calendar.calendarlist'


def selection(td, args, *, write=False):
    cfg = td.personal_scopes.config(td)
    if not cfg or cfg.get('invalid'):raise ValueError(td.personal_scopes.UNKNOWN)
    owner = td.personal_scopes.OWNERS[td.personal_scopes.current(td)]
    account = str(args.get('account') or cfg[owner]['gmail']).casefold()
    scope = next((s for s in ('personal','company') if cfg[s]['gmail']==account), None)
    if scope is None:raise ValueError(td.personal_scopes.UNKNOWN)
    if write and scope != owner:raise ValueError(td.personal_scopes.wrong_owner(scope))
    if scope=='personal' and not cfg[scope].get('connected'):
        raise ValueError(td.personal_scopes.PERSONAL_DISCONNECTED)
    root = td._gmail_root()
    home = Path(root)/'profiles/personal-secretary' if scope=='personal' else Path(root)
    return cfg,scope,account,home


def service(td, args, *, write=False, factory=None):
    cfg,scope,account,home=selection(td,args,write=write)
    token=home/'google_token.json'
    needed={EVENTS,CALENDARS}
    if not token.is_file():raise ValueError(td.personal_scopes.PERSONAL_DISCONNECTED if scope=='personal' else 'COMPANY_CALENDAR_CONSENT_REQUIRED')
    raw=json.loads(token.read_text())
    if not needed.issubset(set(raw.get('scopes') or [])):
        raise ValueError('PERSONAL_CALENDAR_CONSENT_REQUIRED' if scope=='personal' else 'COMPANY_CALENDAR_CONSENT_REQUIRED: company-user Calendar 추가 동의 전')
    if factory is None:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
        creds=Credentials.from_authorized_user_file(str(token))
        if not creds.valid:creds.refresh(Request())
        factory=lambda kind,version:build(kind,version,credentials=creds,cache_discovery=False)
    gmail=factory('gmail','v1')
    if gmail.users().getProfile(userId='me').execute().get('emailAddress','').casefold()!=account:
        raise ValueError('GOOGLE_ACCOUNT_MISMATCH')
    calendar=factory('calendar','v3')
    primary=calendar.calendarList().get(calendarId='primary').execute()
    if primary.get('id','').casefold()!=account or primary.get('accessRole')!='owner':
        raise ValueError('GOOGLE_CALENDAR_ACCOUNT_MISMATCH')
    return calendar,account,primary


def calendar_id(args,account):
    value=str(args.get('calendar_id') or 'primary')
    # A verified account's primary calendar only. No request-supplied calendar can cross ownership.
    if value not in {'primary',account}:raise ValueError('GOOGLE_CALENDAR_SCOPE_UNVERIFIED')
    return account


def event_row(value,account):
    start=value.get('start',{});end=value.get('end',{})
    return {'calendarItemIdentifier':'google:'+value['id'], 'eventIdentifier':'google:'+value['id'],
        'google_event_id':value['id'],'externalIdentifier':value.get('iCalUID'),
        'title':value.get('summary',''),'start':start.get('dateTime') or start.get('date'),
        'end':end.get('dateTime') or end.get('date'),'isAllDay':'date' in start,
        'calendarIdentifier':account,'calendar':account,'source':account,'backend':'google',
        'location':value.get('location',''),'notes':value.get('description',''),
        'url':value.get('htmlLink',''),'etag':value.get('etag'),
        'alarms':[r['minutes'] for r in value.get('reminders',{}).get('overrides',[]) if r.get('method')=='popup'],
        'color_id':value.get('colorId','')}


def lookup(td,args,context):
    date=td._resolve_calendar_date(args.get('date'),'Asia/Seoul')
    if not date:raise ValueError('valid calendar date required')
    start=datetime.fromisoformat(date).replace(tzinfo=KST);end=start+timedelta(days=1)
    cfg=td.personal_scopes.config(td)
    accounts=[cfg['personal']['gmail'],cfg['company']['gmail']] if args.get('account')=='all' else [args.get('account')]
    rows=[];calendars=[];failures=[]
    for account in accounts:
        try:
            api,actual,primary=service(td,{**args,'account':account},factory=context.get('google_service_factory'))
            cid=calendar_id(args,actual);page=None;count=0
            while True:
                data=api.events().list(calendarId=cid,timeMin=start.isoformat(),timeMax=end.isoformat(),
                    singleEvents=True,orderBy='startTime',maxResults=250,pageToken=page).execute()
                rows.extend(event_row(r,actual) for r in data.get('items',[]) if r.get('status')!='cancelled');count+=1
                page=data.get('nextPageToken')
                if not page:break
                if count>=4:raise ValueError('GOOGLE_CALENDAR_RESULT_LIMIT')
            calendars.append({'identifier':actual,'title':primary.get('summary',actual),'source':actual,'backend':'google'})
        except ValueError as exc:
            if args.get('account')!='all':raise
            failures.append({'account':account,'error':str(exc)})
    rows.sort(key=lambda r:r.get('start') or '')
    return json.dumps({'ok':not failures,'receipt':{'work_type':'calendar_lookup','authority':'Google Calendar API',
        'queried_at':datetime.now(KST).isoformat(),'results':rows,'result_count':len(rows),'calendar_results':calendars,
        'mutation_calls':0,'coverage_complete':not failures},'failures':failures},ensure_ascii=False)


def preflight(td,args,context):
    api,account,_=service(td,args,write=True,factory=context.get('google_service_factory'))
    cid=calendar_id(args,account);action=args.get('action');before=None
    if action not in {'create','update','delete'}:raise ValueError('invalid Google calendar action')
    if args.get('span','this')!='this' or args.get('_restore_alarms') is not None:
        raise ValueError('GOOGLE_CALENDAR_RECURRING_OR_ALARM_RESTORE_UNSUPPORTED')
    if args.get('url') or args.get('clear_url'):raise ValueError('GOOGLE_CALENDAR_URL_WRITE_UNSUPPORTED')
    if action!='create':
        identifier=str(args.get('event_id') or '')
        if not identifier.startswith('google:') or not re.fullmatch('[A-Za-z0-9_-]+',identifier[7:]):
            raise ValueError('GOOGLE_EVENT_ID_REQUIRED: API 조회의 google: ID를 사용한다.')
        before=api.events().get(calendarId=cid,eventId=identifier[7:]).execute()
        row=event_row(before,account)
        # Prevent another occurrence or changed event being edited from an old preview.
        if args.get('expected_title')!=row['title'] or args.get('expected_start')!=row['start']:
            raise ValueError('GOOGLE_EVENT_ORIGINAL_CHANGED')
        if before.get('recurringEventId') or before.get('recurrence'):
            raise ValueError('GOOGLE_CALENDAR_RECURRING_WRITE_DEFERRED')
    checked=dict(args,calendar_id=cid,account=account)
    return api,cid,before,checked


def mutation(td,args,context):
    if problem:=td.personal_scopes.role_problem(td,'calendar_direct_mutation',args):raise ValueError(problem)
    selection(td,args,write=True)  # connection/ownership errors precede payload validation
    args=dict(args);action=args.get('action')
    if error:=td._normalize_calendar_dates(args,action):raise ValueError(error)
    if error:=td._calendar_alarm_error(args,action):raise ValueError(error)
    if action=='create' and not all(args.get(k) for k in ('title','start','end')):raise ValueError('title, start, and end required')
    api,cid,before,checked=preflight(td,args,context)
    body={}
    for source,target in [('title','summary'),('location','location'),('notes','description')]:
        if source in args:body[target]=args[source]
        if args.get('clear_'+source):body[target]=''
    for key in ('start','end'):
        if key in args:
            moment=args[key]
            if args.get('all_day'):
                day=datetime.fromisoformat(moment).date()+(timedelta(days=1) if key=='end' else timedelta())
                body[key]={'date':day.isoformat()}
            else:body[key]={'dateTime':moment,'timeZone':'Asia/Seoul'}
    if 'alarms' in args or args.get('clear_alarms'):
        body['reminders']={'useDefault':False,'overrides':[{'method':'popup','minutes':v} for v in args.get('alarms',[])]}
    eid=before['id'] if before else None
    if action=='create':
        context['_api_write_attempted']=True
        after=api.events().insert(calendarId=cid,body=body,sendUpdates='none').execute()
    elif action=='update':
        request=api.events().patch(calendarId=cid,eventId=eid,body=body,sendUpdates='none')
        if not before.get('etag'):raise ValueError('GOOGLE_EVENT_ETAG_REQUIRED')
        request.headers['If-Match']=before['etag'];context['_api_write_attempted']=True;after=request.execute()
    else:
        request=api.events().delete(calendarId=cid,eventId=eid,sendUpdates='none')
        if not before.get('etag'):raise ValueError('GOOGLE_EVENT_ETAG_REQUIRED')
        request.headers['If-Match']=before['etag'];context['_api_write_attempted']=True;request.execute();after=None
    payload={'calendarItemIdentifier':'google:'+(after or before)['id'],
        'before':event_row(before,cid) if before else None,'after':event_row(after,cid) if after else None}
    proc=SimpleNamespace(returncode=0,stdout=json.dumps(payload),stderr='')
    return td._native_mutation_result('Google Calendar API',action,payload['calendarItemIdentifier'],proc,
        common_work_domain='' if args.get('_skip_common_work') else 'calendar',
        common_work_title=args.get('title') or (before or {}).get('summary',''),common_id=args.get('common_id',''))


def color(td,args,context,*,write):
    if write and (problem:=td.personal_scopes.role_problem(td,'google_calendar_color_set',args)):raise ValueError(problem)
    api,account,primary=service(td,args,write=write,factory=context.get('google_service_factory'))
    cid=calendar_id(args,account)
    if args.get('target','event')=='calendar':
        before={k:primary.get(k) for k in ['colorId','backgroundColor','foregroundColor']}
        if not write:return json.dumps({'ok':True,'account':account,'calendar_id':cid,'colors':before,'mutation_calls':0})
        cfg,scope,_,home=selection(td,args,write=True)
        expected='#FFD60A' if scope=='personal' else '#007AFF'
        # Persist the original color before any patch, preserving it across repeated calls.
        folder=home/'state';folder.mkdir(exist_ok=True)
        path=folder/'google-calendar-original-colors.json'
        original=json.loads(path.read_text()) if path.exists() else {}
        if cid not in original:
            original[cid]={'at':datetime.now(KST).isoformat(),**before}
            fd,temp=tempfile.mkstemp(dir=folder,prefix='.calendar-colors-')
            try:
                with os.fdopen(fd,'w') as stream:json.dump(original,stream);stream.flush();os.fsync(stream.fileno())
                os.replace(temp,path)
            finally:
                if os.path.exists(temp):os.unlink(temp)
        context['_api_write_attempted']=True
        api.calendarList().patch(calendarId=cid,colorRgbFormat=True,
            body={'backgroundColor':expected,'foregroundColor':'#000000'}).execute()
        after=api.calendarList().get(calendarId=cid).execute()
        if after.get('backgroundColor','').casefold()!=expected.casefold():raise ValueError('GOOGLE_CALENDAR_COLOR_VERIFY_FAILED')
        return json.dumps({'ok':True,'account':account,'before':before,'after':{k:after.get(k) for k in before},'mutation_calls':1})
    eid=str(args.get('event_id') or '')
    if eid.startswith('google:'):eid=eid[7:]
    elif args.get('eventkit_id'):
        # Retain exact EventKit-to-iCalUID matching with the newly verified account credential.
        service2,cid2,_,_,before,eid=td._google_calendar_color_target(args,{**context,'google_calendar_service':api})
    else:raise ValueError('GOOGLE_EVENT_ID_REQUIRED')
    before=api.events().get(calendarId=cid,eventId=eid).execute()
    old=before.get('colorId','')
    if not write:return json.dumps({'ok':True,'account':account,'event_id':'google:'+eid,'color_id':old,'mutation_calls':0})
    if not re.fullmatch(r'(?:[1-9]|1[01])',str(args.get('color_id') or '')):raise ValueError('GOOGLE_COLOR_ID_INVALID')
    request=api.events().patch(calendarId=cid,eventId=eid,body={'colorId':str(args['color_id'])},sendUpdates='none')
    if not before.get('etag'):raise ValueError('GOOGLE_EVENT_ETAG_REQUIRED')
    request.headers['If-Match']=before['etag'];context['_api_write_attempted']=True;after=request.execute()
    if after.get('colorId')!=str(args['color_id']):raise ValueError('GOOGLE_EVENT_COLOR_VERIFY_FAILED')
    return json.dumps({'ok':True,'account':account,'event_id':'google:'+eid,'before':old,'color_id':after['colorId'],'mutation_calls':1})
