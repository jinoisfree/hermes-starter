"""Twice-daily private mail metadata digest. Configured account; report room or configured DM; no mailbox changes."""
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
import fcntl
import importlib.util
import json
import os
import re
import tempfile

ACCOUNT='user(at)example.com'
DM='REPLACE_SLACK_ID'
JOB_ID='30e7a58f5bdc'
RETENTION_DAYS=30
KST=ZoneInfo('Asia/Seoul')


def load(name,path):
    spec=importlib.util.spec_from_file_location('private_digest_'+name,path)
    value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value);return value


def settings(home):
    import hermes_yaml
    path=home/'config.yaml'
    data=hermes_yaml.safe_load(path.read_text()) if path.exists() else {}
    cfg=(data or {}).get('plugins',{}).get('entries',{}).get('team-kit',{}).get('settings',{}).get('personal_mail_summary',{})
    if not isinstance(cfg,dict):raise ValueError('PERSONAL_MAIL_CONFIG_INVALID')
    return {key:cfg.get(key,default) for key,default in [('account',ACCOUNT),('dm_chat_id',DM),('job_id',JOB_ID)]}


def recent(rows,now):
    kept={}
    for key,value in rows.items():
        try:
            at=datetime.fromisoformat(value['at'])
            if at.tzinfo is None:raise ValueError('ambiguous timestamp')
        except (KeyError,TypeError,ValueError):
            kept[key]=value  # Unknown age must not permit duplicate delivery.
            continue
        if (now-at).total_seconds() <= RETENTION_DAYS*86400:kept[key]=value
    return kept


def fetch(root,home):
    import hermes_yaml
    cfg=hermes_yaml.safe_load((root/'config.yaml').read_text())['plugins']['entries']['team-kit']['settings']['personal_scopes']
    if not cfg['personal'].get('connected'):raise ValueError('PERSONAL_ACCOUNT_NOT_CONNECTED: 개인 계정 연결 전')
    watch=load('gmail_watch',home/'plugins/team-delegation/gmail_watch.py')
    _,api=watch.gmail_client(root,token_home=home)
    account=settings(home)['account']
    if watch.mailbox_address(api)!=account:raise ValueError('PERSONAL_MAIL_ACCOUNT_MISMATCH')
    messages=api.users().messages();page=None;rows=[]
    for _ in range(3):
        response=messages.list(userId='me',q='in:inbox newer_than:2d',maxResults=100,**({'pageToken':page} if page else {})).execute()
        for item in response.get('messages',[]):
            raw=messages.get(userId='me',id=item['id'],format='metadata',metadataHeaders=['From','Subject','Auto-Submitted','Precedence','List-Unsubscribe']).execute()
            h={v['name'].lower():v.get('value','') for v in raw.get('payload',{}).get('headers',[])}
            automatic=(h.get('auto-submitted','no').lower()!='no' or h.get('precedence','').lower() in {'bulk','list','junk'}
                or bool(h.get('list-unsubscribe')) or bool(set(raw.get('labelIds',[]))&{'CATEGORY_PROMOTIONS','CATEGORY_UPDATES','CATEGORY_SOCIAL','CATEGORY_FORUMS'})
                or bool(re.search(r'(?:no.?reply|notification|newsletter|알림|뉴스레터)',h.get('from',''),re.I)))
            text=h.get('subject','')+' '+raw.get('snippet','')
            reply_candidate=bool(re.search(r'회신|답변|답장|확인.*부탁|please.*(?:reply|respond)|응답',text,re.I))
            deadline_candidate=bool(re.search(r'마감|기한|due|deadline',text,re.I))
            promotional=bool(set(raw.get('labelIds',[]))&{'CATEGORY_PROMOTIONS'}) or bool(h.get('list-unsubscribe'))
            # A machine-sent official deadline/reply request must not disappear into update counts.
            if not promotional and (reply_candidate or deadline_candidate):automatic=False
            rows.append({'id':raw['id'],'thread':raw.get('threadId',raw['id']),'from':h.get('from','발신인 미확인'),
                'subject':h.get('subject','제목 없음'),'snippet':raw.get('snippet','')[:200],
                'automatic':automatic,'reply_candidate':reply_candidate,
                'deadline_candidate':deadline_candidate})
        page=response.get('nextPageToken')
        if not page:return rows
    raise ValueError('PERSONAL_MAIL_COVERAGE_INCOMPLETE')


def render(rows,now,*,human_total=None,account=ACCOUNT):
    def escape(text):return str(text).replace('&','&amp;').replace('<','&lt;').replace('>','&gt;').replace('*','').replace('\n',' ')
    humans=[r for r in rows if not r['automatic']]
    total=len(humans) if human_total is None else human_total
    lines=['*🔵 개인 메일 요약*',f'{now.strftime("%Y-%m-%d %H:%M KST")} · {escape(account)}',
        '', '*확인한 새 메일*',f'  • 사람 발신·답변/마감 후보 {total}건 · 광고·자동 알림 {len(rows)-len(humans)}건']
    for row in sorted(humans,key=lambda r:(not r['deadline_candidate'],not r['reply_candidate']))[:20]:
        tags=(' · 답 필요 후보' if row['reply_candidate'] else '')+(' · 마감 표기 후보' if row['deadline_candidate'] else '')
        lines += [f"  • {escape(row['subject'])}{tags}",f"    ↳ {escape(row['from'])}: {escape(row['snippet'])}",
            f"    ↳ https://mail.google.com/mail/u/{account}/#all/{row['thread']}"]
    if total>20:lines.append(f'  • 나머지 {total-20}건은 다음 요약에서 확인')
    lines += ['', '*확인 범위*','  • 최근 2일 받은편지함의 헤더·미리보기 기반 후보이며, 실제 회신 의무·공식 마감은 원문 확인이 필요하다.',
              '  • 메일 원본을 변경하거나 읽음 처리·발송하지 않았다.']
    return '\n'.join(lines)


def save(path,data):
    fd,temp=tempfile.mkstemp(dir=path.parent,prefix='.personal-mail-')
    try:
        with os.fdopen(fd,'w') as stream:json.dump(data,stream);stream.flush();os.fsync(stream.fileno())
        os.replace(temp,path)
    finally:
        if os.path.exists(temp):os.unlink(temp)


def run(root,home,*,reader=None,sender=None,now=None):
    if home!=root/'profiles/personal-secretary':raise ValueError('PERSONAL_MAIL_HOME_MISMATCH')
    cfg=settings(home);account=cfg['account']
    now=now or datetime.now(KST)
    if now.hour not in {9,18}:raise ValueError('PERSONAL_MAIL_SCHEDULE_REQUIRED')
    rows=(reader or (lambda:fetch(root,home)))()
    folder=home/'state';folder.mkdir(exist_ok=True);path=folder/'personal-mail-summary.json'
    if any(p.is_symlink() for p in (folder,path,folder/'personal-mail-summary.lock')):raise ValueError('PERSONAL_MAIL_SYMLINK_REFUSED')
    with (folder/'personal-mail-summary.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        data=json.loads(path.read_text()) if path.exists() else {'version':1,'account':account,'messages':{},'runs':{}}
        if data.get('account')!=account or data.get('version')!=1:raise ValueError('PERSONAL_MAIL_STATE_INVALID')
        data['messages']=recent(data['messages'],now)
        data['runs']=recent(data['runs'],now)
        slot=now.strftime('%Y-%m-%d %H')
        if slot in data['runs']:return {'status':'already_attempted','mutation_calls':0}
        unseen=[r for r in rows if r['id'] not in data['messages']]
        human_total=sum(not r['automatic'] for r in unseen)
        # Cap before reserving so omitted human mail remains available next time.
        humans=sorted([r for r in unseen if not r['automatic']],key=lambda r:(not r['deadline_candidate'],not r['reply_candidate']))[:20]
        selected=humans+[r for r in unseen if r['automatic']]
        data['runs'][slot]={'at':now.isoformat(),'status':'pending','job_id':cfg['job_id']}
        for row in selected:data['messages'][row['id']]={'at':now.isoformat(),'status':'pending'}
        save(path,data)  # at-most-once reservation before network, including process death
        text=render(selected,now,human_total=human_total,account=account)
        if sender is None:
            R=load('learning_report',Path(__file__).with_name('learning_report.py'))
            sender=lambda body:R.slack(home,'chat.postMessage',body)
        target=load('learning',Path(__file__).with_name('learning.py')).owner_report_room(home) or cfg['dm_chat_id']
        try:
            receipt=sender({'channel':target,'text':text,'unfurl_links':False,'unfurl_media':False})
            status='sent' if receipt.get('ok') is True and receipt.get('ts') and receipt.get('channel',target)==target else 'uncertain'
        except Exception:status='uncertain'
        data['runs'][slot]['status']=status
        for row in selected:data['messages'][row['id']]['status']=status
        save(path,data)
        return {'status':status,'human_count':len(humans),'automatic_count':len(selected)-len(humans),'mutation_calls':0}
