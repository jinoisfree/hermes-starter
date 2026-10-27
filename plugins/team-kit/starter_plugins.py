"""Owner-only public-plugin decisions. Daily maintenance consumes durable Slack choices."""
from datetime import datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import time


def module(name):
    spec = importlib.util.spec_from_file_location('starter_plugins_' + name, Path(__file__).with_name(name + '.py'))
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


R = module('learning_report')
L = R.learning
CHOICES = {'공개': 'public', '비공개': 'private', '나중에': 'later'}
BUTTONS = '[[버튼: 공개 | 비공개 | 나중에]]'


def today():
    return datetime.now(R.KST).date().isoformat()


def config(home):
    from ruamel.yaml import YAML
    raw = YAML(typ='safe').load((Path(home) / 'config.yaml').read_text()) or {}
    settings = raw.get('plugins', {}).get('entries', {}).get('team-kit', {}).get('settings', {})
    enabled = settings.get('starter_plugins', {}).get('enabled') is True
    channel = settings.get('report_channel', '')
    if enabled and not re.fullmatch(r'[CG][A-Z0-9]+', channel):
        raise ValueError('Missing report channel')
    return {'enabled': enabled, 'channel': channel}


def state_path(home):
    return Path(home) / 'starter_plugins/state.json'


def load(home):
    try:
        state = json.loads(state_path(home).read_text())
    except FileNotFoundError:
        return {'reports': {}, 'current': {}}
    if not isinstance(state, dict) or not isinstance(state.get('reports'), dict) or not isinstance(state.get('current'), dict):
        raise ValueError('Invalid starter plugin state; not replaced')
    return state


def send(home, state, report, part, key, thread=False):
    if part.get('ts'): return
    marker = 'starter_plugin_' + hashlib.sha256(key.encode()).hexdigest()[:40]
    recovery = report if thread else {k: v for k, v in report.items() if k != 'thread_ts'}
    ts = R.recover(home, recovery, part, marker) if part.get('attempted_at') else None
    if not ts:
        part['attempted_at'] = time.time()
        L.save(state_path(home), state)
        body = {'channel': report['channel'], 'text': part['text'], 'metadata': R.layout.metadata(marker),
                'parse': 'none', 'link_names': False, 'unfurl_links': False, 'unfurl_media': False}
        if thread: body['thread_ts'] = report['thread_ts']
        ts = str(R.slack(home, 'chat.postMessage', body)['ts'])
    part['ts'] = ts
    if not thread: report['thread_ts'] = ts
    L.save(state_path(home), state)


def paint(home, state, report, status, note):
    if report.get('painted') == [status, note]: return
    L.extension('report_status').mark(lambda method, body: R.slack(home, method, body),
                                    report['channel'], report['thread_ts'], status, note)
    report['painted'] = [status, note]
    L.save(state_path(home), state)


def ask(home, pending):
    cfg = config(home)
    if not cfg['enabled']: return []
    owner = R.owner(home)  # Fail closed if ownership is missing or ambiguous.
    posted = []
    with L.locked(state_path(home)):
        state = load(home)
        for item in pending:
            name = item['name']
            if not re.fullmatch(r'[a-z][a-z0-9-]*', name): raise ValueError('Invalid plugin name')
            key = state['current'].get(name)
            previous = state['reports'].get(key, {})
            if previous and not (previous.get('decision') == 'later' and previous.get('decided_date', today()) < today()):
                if not previous.get('thread_ts'):
                    send(home, state, previous, previous['root'], key)
                continue
            key = name + ':' + today()
            findings = item.get('findings', {})
            result = ', '.join(f'{kind} {count}건' for kind, count in sorted(findings.items())) if findings else '통과'
            text = (f'*🔴 새 플러그인 `{name}` 공개판 포함 여부*\n\n*포함 후보*\n• 파일 {int(item["files"])}개\n'
                    f'\n*개인정보 검사*\n• {R.escape(result)}\n\n*반영 시점*\n• 소유자 결정은 다음 04:30 정비에서 반영한다. '
                    '공개 선택도 검사에 통과해야 게시한다. 나중에는 다음 날 다시 묻는다.\n' + BUTTONS)
            report = {'name': name, 'channel': cfg['channel'], 'owner': owner, 'day': today(),
                      'root': {'text': text}, 'processed': {}, 'button_replies': {}}
            state['reports'][key] = report
            state['current'][name] = key
            L.save(state_path(home), state)
            send(home, state, report, report['root'], key)
            posted.append(name)
    return posted


def messages(home, report):
    body = {'channel': report['channel'], 'ts': report['thread_ts'], 'limit': 100}
    rows = []
    while True:
        data = R.slack(home, 'conversations.replies', body)
        rows.extend(data.get('messages', []))
        cursor = (data.get('response_metadata') or {}).get('next_cursor')
        if not cursor: return rows
        body['cursor'] = cursor


def skip_report_thread(home, event=None, **_):
    cfg = config(home)
    source = getattr(event, 'source', None)
    if not cfg['enabled'] or source is None: return None
    if str(getattr(getattr(source, 'platform', None), 'value', '')) != 'slack': return None
    owner = R.owner(home)
    if str(getattr(source, 'chat_id', '')) != cfg['channel'] or str(getattr(source, 'user_id', '')) != owner:
        return None
    thread = str(getattr(source, 'thread_id', '') or '')
    if not thread: return None
    with L.locked(state_path(home)):
        state = load(home)
        report = next((r for r in state['reports'].values() if r.get('thread_ts') == thread and r['channel'] == cfg['channel']), None)
        if report is None: return None
        if (getattr(event, 'raw_message', None) or {}).get('_hermes_button'):
            report['button_replies'][str(getattr(event, 'message_id', '') or time.time())] = {'text': str(getattr(event, 'text', '') or '')}
            L.save(state_path(home), state)
    return {'action': 'skip', 'reason': 'starter_plugins_thread'}


def collect(home):
    if not config(home)['enabled']: return []
    result = []
    owner = R.owner(home)
    with L.locked(state_path(home)):
        state = load(home)
        for key in state['current'].values():
            report = state['reports'][key]
            if report.get('finished') or not report.get('thread_ts') or report['owner'] != owner: continue
            clicks = [{'ts': ts, 'user': owner, **row} for ts, row in report['button_replies'].items()]
            for message in sorted(messages(home, report) + clicks, key=lambda row: float(row['ts'])):
                ts = str(message['ts'])
                if message.get('user') != owner or message.get('subtype') or ts == report['thread_ts'] or ts in report['processed']:
                    continue
                choice = CHOICES.get(message.get('text', '').strip())
                if choice:
                    report.update(decision=choice, decided_date=today())
                    text = '결정을 기록했어. 다음 정비에서 반영할게.' if choice != 'later' else '오늘은 미루고 다음 날 다시 물을게.'
                else:
                    text = '공개·비공개·나중에 중 하나로 다시 답해 줘.\n' + BUTTONS
                report['processed'][ts] = {'text': text}
                L.save(state_path(home), state)
            for ts, part in report['processed'].items():
                send(home, state, report, part, key + ':reply:' + ts, thread=True)
            if report.get('decision'):
                paint(home, state, report, 'partial', '소유자 결정 반영 중')
                if report['decision'] == 'later':
                    paint(home, state, report, 'done', '다음 날 다시 질문')
                    report['finished'] = True
                    L.save(state_path(home), state)
                else:
                    result.append({'name': report['name'], 'decision': report['decision'], 'date': report['decided_date']})
    return result


def finish(home, name, ok, note):
    if not config(home)['enabled']: return
    with L.locked(state_path(home)):
        state = load(home)
        key = state['current'].get(name)
        if not key: return
        report = state['reports'][key]
        if report.get('finished'): return
        if report.get('decision') not in {'public', 'private'}: raise ValueError('No owner decision')
        part_key = 'finish:' + str(bool(ok)) + ':' + note
        part = report.setdefault('results', {}).setdefault(part_key, {'text': ('*🟢 공개판 결정 반영 완료*' if ok else '*🔴 공개판 결정 반영 실패*') + '\n' + R.escape(note) + ('' if ok else '\n' + BUTTONS)})
        send(home, state, report, part, key + ':' + part_key, thread=True)
        paint(home, state, report, 'done' if ok else 'failed', note)
        report['finished'] = bool(ok)
        L.save(state_path(home), state)
