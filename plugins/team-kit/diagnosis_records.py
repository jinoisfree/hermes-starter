"""Program-owned private diagnosis journal. No model write tool is exposed."""
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
import fcntl
import hashlib
import json
import os
import re
import sqlite3
from agent.redact import redact_sensitive_text

ROOT = Path.home() / '.hermes'
SEPARATOR = '<<<JANG_CHEN_DIAGNOSIS_DETAIL_7F3A>>>'


def clean(value, limit):
    text = redact_sensitive_text(str(value or ''), force=True)
    text = re.sub(r'(?i)((?:api_key|access_token|refresh_token|password)["\s]*[:=]["\s]*)([^\s,"}]+)', r'\1[REDACTED]', text)
    return text[:limit]


def build(*, identifier, source, answer='', session_id='', subject_profile='', channel='', thread_ts='', message_ts='', category='', expected='', actual='', user_message='', evidence='', coaching=''):
    answer = clean(answer, 12000)
    if '<<<JANG_CHEN_DIAGNOSIS_COACHING_7F3A>>>' in answer:
        answer = answer.split('<<<JANG_CHEN_DIAGNOSIS_COACHING_7F3A>>>', 1)[0].strip()
    if source == 'owner_dm':
        block = re.search(r'```\s*\n([^`]+)\n```\s*$', answer)
        if block and len(block[1]) <= 600:
            coaching = block[1].strip(); answer = answer[:block.start()].rstrip()
    short, full = '', answer
    if answer.splitlines().count(SEPARATOR) == 1:
        short, full = answer.split(SEPARATOR); short, full = short.strip(), full.strip()
    text = answer + '\n' + clean(evidence, 20000)
    record = dict(id=identifier, recorded_at=datetime.now(ZoneInfo('Asia/Seoul')).isoformat(), source=source,
                  subject_profile=subject_profile, channel=channel, thread_ts=thread_ts, message_ts=message_ts,
                  category=category, outcome='silent' if answer == '없음' else 'answer' if answer else 'failed',
                  short_verdict=clean(short, 300), full_result=full[:6000], truncated=len(full)>6000,
                  session_id=session_id, coaching=clean(coaching, 600), customers=list(dict.fromkeys(re.findall(r'`([A-Za-z0-9_.]{2,40})`', text)))[:3],
                  commits=list(dict.fromkeys(re.findall(r'(?<![\w])(?=[a-fA-F0-9]{7,40}(?![\w]))(?=[a-fA-F0-9]*[a-fA-F])[a-fA-F0-9]{7,40}(?![\w])', answer))))
    if source in {'black_cow_request','program_request'}:
        record.update(expected=clean(expected,200), actual=clean(actual,200))
    else:
        record['user_message'] = clean(user_message,200)
    return record


def append(record):
    folder = ROOT / 'shared/diagnosis-records'
    if folder.resolve() != folder.absolute(): raise ValueError('Journal symlink forbidden')
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = folder / 'records.jsonl'
    with os.fdopen(os.open(folder / '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            if path.is_symlink(): raise ValueError('Journal symlink forbidden')
            rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()] if path.exists() else []
            if any(row['id'] == record['id'] for row in rows): return
            rows = (rows + [record])[-500:]
            tmp = folder / 'records.tmp'
            with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600), 'w') as f:
                for row in rows: f.write(json.dumps(row, ensure_ascii=False) + '\n')
                f.flush(); os.fsync(f.fileno())
            tmp.replace(path)
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def request_record(r):
    session_id, evidence = '', ''
    path = ROOT / 'profiles/system-doctor/state.db'
    if path.exists() and not path.is_symlink():
        with sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=2) as db:
            db.execute('PRAGMA query_only=ON')
            row = db.execute("SELECT session_id FROM messages WHERE role='user' AND instr(content,?)>0 ORDER BY id DESC LIMIT 1", ('진단 요청 식별자: '+r['id'],)).fetchone()
            if row:
                session_id = row[0]
                evidence = '\n'.join(str(x[0] or '') for x in db.execute("SELECT content FROM messages WHERE session_id=? AND role='tool' AND tool_name IN ('diagnosis_case_read','diagnosis_slack_read') LIMIT 4",(session_id,)))[:20000]
    append(build(identifier=r['id'], source='program_request' if r.get('source')=='program' else 'black_cow_request' if r.get('requester_profile','sns-manager')=='sns-manager' else 'staff_request', answer=r.get('answer',''), session_id=session_id,
                 subject_profile=r.get('requester_profile','sns-manager'), evidence=evidence, coaching=r.get('coaching',''), **{k:r.get(k,'') for k in ('channel','thread_ts','message_ts','category','expected','actual')}))


def owner_record(home, turn_id, turn, answer, session_id):
    # Only a verified owner DM can supply this turn. Do not infer a subject from the answer.
    path = Path(home) / 'state.db'
    with sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=2) as db:
        db.execute('PRAGMA query_only=ON')
        row = db.execute('SELECT source,chat_type FROM sessions WHERE id=?',(session_id,)).fetchone()
    if not row or tuple(row) != ('slack','dm'): return
    text = turn['user_message']
    link = re.search(r'https://[\w-]+\.slack\.com/archives/([CDG][A-Z0-9]+)/p(\d{10})(\d{6})(?:\?[^\s>]*thread_ts=(\d{10}\.\d{6}))?', text)
    channel, thread, message = '', '', ''
    if link: channel, message = link[1], link[2]+'.'+link[3]; thread = link[4] or message
    profiles = [p for word,p in [('SNS담당','sns-manager'),('개인비서','personal-secretary'),('동석','default'),('너굴','research-assistant'),('하마','work-assistant')] if word in text]
    identifier = hashlib.sha256((str(home)+':'+session_id+':'+str(turn_id)).encode()).hexdigest()
    append(build(identifier=identifier,source='owner_dm',answer=answer,session_id=session_id,
                 subject_profile=profiles[0] if len(profiles)==1 else '',channel=channel,thread_ts=thread,message_ts=message,user_message=text,evidence=text))
