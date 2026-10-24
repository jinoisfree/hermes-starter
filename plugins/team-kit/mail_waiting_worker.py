"""Gmail metadata-only reply inspection. Uses existing OAuth; has no write methods."""
from datetime import datetime
from email.utils import getaddresses
import importlib.util
import json
from pathlib import Path
import re
import sys
from zoneinfo import ZoneInfo

KST = ZoneInfo('Asia/Seoul')


def interval(args):
    if not isinstance(args, dict) or set(args) - {'start', 'end', 'limit'}:
        raise ValueError('invalid_mail_waiting_arguments')
    dates = []
    for key in ('start', 'end'):
        v = args.get(key)
        if not isinstance(v, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', v): raise ValueError('explicit_dates_required')
        dates.append(datetime.strptime(v, '%Y-%m-%d').replace(tzinfo=KST))
    if not 0 < (dates[1] - dates[0]).days <= 90: raise ValueError('date_range_must_be_1_to_90_days')
    limit = args.get('limit', 20)
    if type(limit) is not int or not 1 <= limit <= 50: raise ValueError('limit_must_be_1_to_50')
    return dates[0].timestamp(), dates[1].timestamp(), limit


def headers(message):
    return {h['name'].lower(): h.get('value', '') for h in message.get('payload', {}).get('headers', [])}


def inspect_thread(data, account, start, end):
    messages = [m for m in data.get('messages', []) if 'DRAFT' not in m.get('labelIds', [])]
    if any(not m.get('internalDate') or not m.get('id') for m in messages):
        raise ValueError('thread_metadata_incomplete')
    sent = [m for m in messages if 'SENT' in m.get('labelIds', []) and start <= int(m['internalDate']) / 1000 < end]
    if not sent: return None
    original = max(sent, key=lambda m: int(m['internalDate']))
    # A subsequent follow-up resets the reply baseline, even if outside the original search range.
    later_sent = [m for m in messages if 'SENT' in m.get('labelIds', []) and int(m['internalDate']) >= int(original['internalDate'])]
    baseline = max(later_sent, key=lambda m: int(m['internalDate']))
    later = [m for m in messages if int(m['internalDate']) > int(baseline['internalDate']) and 'SENT' not in m.get('labelIds', [])]
    human, automatic, unknown = [], [], []
    for m in later:
        h = headers(m)
        senders = {a.casefold() for _, a in getaddresses([h.get('from', '')]) if a}
        if account.casefold() in senders: continue
        if not senders:
            unknown.append(m['id']); continue
        if h.get('auto-submitted', 'no').lower() != 'no' or h.get('precedence', '').lower() in {'bulk', 'list', 'junk'} or h.get('content-type', '').lower().startswith('multipart/report'):
            automatic.append(m['id'])
        else:
            human.append(m['id'])
    h = headers(baseline)
    return {'thread_id': data['id'], 'sent_message_id': baseline['id'], 'subject': h.get('subject', ''), 'to': h.get('to', ''),
            'sent_at_kst': datetime.fromtimestamp(int(baseline['internalDate']) / 1000, KST).isoformat(),
            'status': 'incoming_reply_candidate' if human else 'unknown_sender' if unknown else 'automatic_reply_only' if automatic else 'no_later_incoming_found',
            'incoming_message_ids': human, 'automatic_message_ids': automatic, 'unknown_message_ids': unknown,
            'link': 'https://mail.google.com/mail/u/' + account + '/#all/' + data['id']}


def execute(service, account, args):
    start, end, limit = interval(args)
    actual = service.users().getProfile(userId='me').execute().get('emailAddress', '')
    if not account or actual.casefold() != account.casefold(): raise ValueError('gmail_account_mismatch')
    result = service.users().threads().list(userId='me', q=f'in:sent after:{int(start)-1} before:{int(end)}', maxResults=limit).execute()
    threads = result.get('threads', [])
    if not isinstance(threads, list): raise ValueError('invalid_thread_listing')
    rows, failures = [], []
    for t in threads[:limit]:
        try:
            data = service.users().threads().get(userId='me', id=t['id'], format='metadata',
                metadataHeaders=['From', 'To', 'Subject', 'Auto-Submitted', 'Precedence', 'Content-Type']).execute()
            row = inspect_thread(data, account, start, end)
            if row: rows.append(row)
        except Exception as exc:
            failures.append({'thread_id': t.get('id'), 'error': type(exc).__name__})
    return {'ok': True, 'start': args['start'], 'end_exclusive': args['end'], 'timezone': 'Asia/Seoul',
            'checked_at_kst': datetime.now(KST).isoformat(), 'threads': rows, 'failures': failures,
            'coverage_complete': not failures and not result.get('nextPageToken') and len(threads) <= limit,
            'more_threads': bool(result.get('nextPageToken')), 'checked_threads': len(threads[:limit]),
            'note': 'Reply candidates in Gmail threads only. Separate-subject replies, aliases and business completion need owner review. No mail was changed or sent.'}


if __name__ == '__main__':
    try:
        request = json.load(sys.stdin)
        interval(request['args'])
        root = Path(request['root']); sys.path.insert(0, str(root / 'hermes-agent'))
        script = root / 'hermes-agent/skills/productivity/google-workspace/scripts/google_api.py'
        spec = importlib.util.spec_from_file_location('waiting_gmail_api', script)
        api = importlib.util.module_from_spec(spec); spec.loader.exec_module(api)
        token_home=Path(request.get('token_home') or root)
        if token_home not in {root,root/'profiles/personal-secretary'}:raise ValueError('invalid_token_home')
        api.HERMES_HOME = root; api.TOKEN_PATH = token_home / 'google_token.json'; api.CLIENT_SECRET_PATH = root / 'google_client_secret.json'
        print(json.dumps(execute(api.build_service('gmail', 'v1'), request['account'], request['args']), ensure_ascii=False))
    except Exception as exc:
        print(json.dumps({'ok': False, 'error': str(exc) if type(exc) is ValueError else type(exc).__name__}))
