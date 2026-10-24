"""Repaint an owner report after its decision: bar colour, title mark, one status line, buttons gone.

Scripts call ``mark`` with the report they posted; members call the ``report_status`` tool from the
report's thread once the approved work actually finished. Only the bot's own message changes.
"""
from __future__ import annotations

from datetime import datetime
import json
import re
import time
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
COLORS = {'fail': '#E01E5A', 'warn': '#ECB22E', 'ok': '#2EB67D', 'plain': '#9AA0A6'}
# state -> (title mark, bar, default status line, buttons removed)
STATES = {
    # Owner decision 2026-10-23: red = needs an answer, yellow = answered and in progress,
    # green = finally approved and closed (a refusal is a closing too).
    'pending': ('🔴', 'fail', '❗ 답변 필요', False),
    'partial': ('🟡', 'warn', '⏳ 일부 처리 · 진행 중', False),
    'done': ('🟢', 'ok', '✅ 처리 완료 · 종결', True),
    'declined': ('🟢', 'ok', '↩️ 거절 · 실행하지 않음 · 종결', True),
    'failed': ('🔴', 'fail', '❌ 처리 실패 · 다시 답변 필요', False),
}
BUTTON_BLOCK = 'hermes_reply_buttons'
STATUS_BLOCK = 'hermes_report_status'
REPORT_CHANNELS = ('REPLACE_SLACK_ID', 'REPLACE_SLACK_ID', 'REPLACE_SLACK_ID')  # 보고방, 학원, SNS담당방
_MARK = re.compile(r'^(\*?)(?:🔴|🟡|🟢|🔵|🟠|⚪|:red_circle:|:large_yellow_circle:|:large_green_circle:|'
                   r':large_blue_circle:|:large_orange_circle:|:white_circle:)\s*')
_KEEP = ('color', 'fallback', 'blocks', 'mrkdwn_in', 'text')


def _status_block(line):
    return {'type': 'context', 'block_id': STATUS_BLOCK, 'elements': [{'type': 'mrkdwn', 'text': line}]}


def _clean(blocks, drop_buttons):
    return [b for b in blocks or [] if b.get('block_id') != STATUS_BLOCK
            and not (drop_buttons and b.get('block_id') == BUTTON_BLOCK)]


def _retitle(blocks, mark):
    out = []
    for block in blocks:
        if block.get('type') == 'header' and isinstance((block.get('text') or {}).get('text'), str):
            text = block['text']['text']
            text = _MARK.sub('', text, count=1)
            block = {**block, 'text': {**block['text'], 'text': f'{mark} {text}'}}
        out.append(block)
    return out


def repaint(message, state, note='', *, now=None):
    """chat.update fields for ``message`` (as conversations.replies returns it) in ``state``."""
    if state not in STATES:
        raise ValueError('state는 pending|partial|done|declined|failed 중 하나야.')
    mark, colour, label, final = STATES[state]
    when = datetime.fromtimestamp(time.time() if now is None else now, KST).strftime('%H:%M KST')
    note = re.sub(r'\s+', ' ', str(note or '')).strip()[:200]
    line = f'{label} · {when}' + (f' · {note}' if note else '')
    text = message.get('text') or ' '
    match = _MARK.match(text)
    text = (match[1] + mark + ' ' + text[match.end():]) if match else text
    blocks = _retitle(_clean(message.get('blocks'), final), mark)
    attachments = [{k: v for k, v in a.items() if k in _KEEP} for a in message.get('attachments') or []]
    if not attachments:
        # No bar yet: the body moves under one, the title stays on top.
        head = [b for b in blocks if b.get('type') == 'header'][:1]
        body = [b for b in blocks if b not in head]
        blocks, attachments = head, [{'blocks': body}]
    for part in attachments:
        part['color'] = COLORS[colour]
        if isinstance(part.get('blocks'), list):
            part['blocks'] = _clean(part['blocks'], final)
    last = attachments[-1]
    last['blocks'] = (last.get('blocks') or [])[:49] + [_status_block(line)]
    return {'text': text, 'blocks': blocks, 'attachments': attachments}


def find_message(slack, channel, ts):
    data = slack('conversations.replies', {'channel': channel, 'ts': ts, 'limit': 1, 'inclusive': 'true'})
    message = next((m for m in data.get('messages') or [] if m.get('ts') == ts), None)
    if message is None:
        raise ValueError('보고 메시지를 찾지 못했어.')
    return message


def mark(slack, channel, ts, state, note='', *, bot_user=None, now=None):
    """Repaint one report the calling bot posted; ``slack(method, body)`` goes through slack_blocks."""
    message = find_message(slack, channel, ts)
    if bot_user and message.get('user') != bot_user:
        raise ValueError('이 봇이 올린 보고만 상태를 바꿀 수 있어.')
    fields = repaint(message, state, note, now=now)
    slack('chat.update', {'channel': channel, 'ts': ts, **fields})
    return fields


NOTE = ('승인이 필요한 보고(마감 점검·메일 알림 제안·정기회의 초안·위키 백업 등)의 스레드에서, 소유자가 결정한 일을 '
        '실제로 끝냈다는 도구 결과를 확인한 뒤에만 부른다. 끝냈으면 done, 거절·취소면 declined, 일부만 했으면 partial, '
        '실행이 실패했으면 failed. 원래 보고의 색 막대·제목 표식·상태 줄을 바꾸고 done·declined면 버튼을 없앤다. '
        '실행 전이나 결과가 불명확할 때는 부르지 않는다. 학습·스킬 보고는 검토 도구가 직접 바꾸므로 쓰지 않는다.')
SCHEMA = {'name': 'report_status', 'description': NOTE, 'parameters': {'type': 'object', 'properties': {
    'state': {'type': 'string', 'enum': ['done', 'partial', 'declined', 'failed', 'pending']},
    'note': {'type': 'string', 'description': '상태 줄에 붙일 짧은 결과(예: 미리알림 2건 생성). 200자 이내.'}},
    'required': ['state']}}


def handle(home, args, base, session_id='', **_):
    """Tool: repaint the root of the current owner Slack thread, only if this bot posted it."""
    try:
        src = base.learning.source(home, session_id, allow_approval=True)
        channel, thread = src.get('chat_id', ''), src.get('thread_id', '')
        if src.get('user_id') != base.owner(home):
            raise ValueError('소유자 대화에서만 보고 상태를 바꿀 수 있어.')
        if not thread:
            raise ValueError('보고 스레드 안에서만 쓸 수 있어.')
        if not (src.get('chat_type') == 'dm' or channel in REPORT_CHANNELS):
            raise ValueError('보고방·학원·SNS담당방·소유자 DM의 보고 스레드에서만 쓸 수 있어.')
        slack = lambda method, body: base.slack(home, method, body)
        bot = slack('auth.test', {}).get('user_id')
        mark(slack, channel, thread, args.get('state', ''), args.get('note', ''), bot_user=bot)
        return json.dumps({'ok': True, 'status_updated': True, 'state': args.get('state')}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({'ok': False, 'status_updated': False,
                           'error': str(exc) if isinstance(exc, ValueError) else type(exc).__name__}, ensure_ascii=False)
