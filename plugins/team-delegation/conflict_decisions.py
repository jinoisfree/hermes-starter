"""Owner choice for a linked-work content conflict (2026-10-27 사용자 결정).

work_sync stops a work when two originals change differently, and before this the work stayed stopped
silently. Once such a warning reaches the owner (진단담당 first, 2026-10-26), a report-room question asks which
original to follow, or to keep each as it is. The click only records the choice in work-sync.json; the next
10-minute pass applies it through the normal write path (preflight, readback, no replay) and turns the
question green.
"""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
import fcntl
import json
import logging
import os
from pathlib import Path
import time

import slack_report
import work_content as content

LABELS = {'notion': '노션 기준', 'notes': '메모 기준', 'calendar': '일정 기준'}
NAMES = {'notion': '노션', 'notes': '메모', 'calendar': '일정'}
KEEP = '그대로 두기'
HEAD = '연결 원본 내용 충돌'


def enabled(home):
    return Path(home).name == 'work-assistant'


@contextmanager
def locked(home):
    path = Path(home) / 'state/conflict-decisions.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix('.lock')
    if path.is_symlink() or lock.is_symlink():
        raise ValueError('conflict_state_symlink')
    with lock.open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        state = json.loads(path.read_text()) if path.exists() else {'asks': {}}
        def save():
            tmp = path.with_name(path.name + '.tmp')
            with tmp.open('w') as output:
                os.chmod(tmp, 0o600)
                json.dump(state, output, ensure_ascii=False, indent=2)
                output.flush(); os.fsync(output.fileno())
            os.replace(tmp, path)
        yield state, save


def choices(kinds):
    return [LABELS[kind] for kind in kinds if kind in LABELS] + [KEEP]


def question(title, kinds):
    names = '·'.join(NAMES.get(kind, kind) for kind in kinds)
    return (f'*🔴 {title} — {HEAD}*\n'
            f'같은 점검 구간에 {names}이 서로 다르게 바뀌어 어느 쪽에도 쓰지 않고 멈췄어요. 어느 원본 기준으로 맞출까요?\n'
            f'_고른 원본의 제목·할 일을 나머지에 옮겨요(본문은 원본마다 그대로). `{KEEP}`는 지금 내용을 각각 두고 다음 변경부터 다시 맞춰요._\n'
            f'[[버튼: {" | ".join(choices(kinds))}]]')


def conflict_of(home, cid):
    path = Path(home) / 'state/work-sync.json'
    if not path.exists():
        return None
    with content.ledger(home, 'work-sync') as (_, state):
        return (state['works'].get(cid) or {}).get('conflict')


def ask(home, items, *, send=None):
    """Post one question per conflicted work. Returns the items that could not be asked (warn plainly)."""
    send = send or (lambda channel, text, **kw: slack_report.post_thread(home, channel, text, **kw))
    channel = slack_report.report_room(home)
    if not channel:
        return list(items)
    missed = []
    for cid, title in items:
        conflict = conflict_of(home, cid)
        if not conflict:
            continue  # resolved before the warning reached the owner
        with locked(home) as (state, save):
            row = state['asks'].get(cid)
            if row and row['status'] in {'waiting', 'chosen'}:
                continue
            state['asks'][cid] = {'common_id': cid, 'title': title, 'kinds': conflict['kinds'], 'channel': channel,
                                  'message_ts': None, 'status': 'waiting', 'at': time.time()}
            save()  # Never resend blindly after an ambiguous Slack timeout.
            try:
                state['asks'][cid]['message_ts'] = send(channel, question(title, conflict['kinds']))
            except Exception as exc:
                logging.getLogger(__name__).warning('conflict_ask_failed:%s', type(exc).__name__)
                state['asks'][cid]['status'] = 'failed'
                missed.append((cid, title))
            save()
    return missed


def handle(home, channel, thread, text, button=None):
    """Owner answer in a conflict question thread. Returns (state, reply) or None if not ours."""
    with locked(home) as (state, save):
        asks = [a for a in state['asks'].values() if a['channel'] == channel and a['message_ts'] == thread]
        if len(asks) != 1:
            return None
        row = asks[0]
        if button and (button.get('label') != text or button.get('message_ts') != thread):
            return None
        options = choices(row['kinds'])
        if text not in options:
            return ('waiting', '버튼으로 골라 주세요: ' + ' · '.join(f'`{o}`' for o in options))
        if row['status'] != 'waiting':
            return ('done', '이미 받은 선택이 있어요. 바꾸지 않았어요.')
        choice = next((kind for kind, label in LABELS.items() if label == text), 'keep')
        with content.ledger(home, 'work-sync') as (path, ledger):
            work = ledger['works'].get(row['common_id'])
            if not work or not work.get('conflict'):
                row['status'] = 'closed'; save()
                return ('done', '그 사이 충돌이 풀려 있어요. 아무것도 바꾸지 않았어요.')
            work['resolution'] = {'choice': choice, 'at': time.time()}
            content.save(path, ledger)
        row.update(status='chosen', choice=text); save()
        return ('waiting', f'`{text}`로 받았어요. 다음 점검(10분 안)에 맞추고 이 글에 결과를 표시할게요.')


def close(home, cid, resolved, *, send=None):
    """Turn the question green with what the sync actually did (3-color rule)."""
    send = send or (lambda channel, text, **kw: slack_report.post_thread(home, channel, text, **kw))
    with locked(home) as (state, save):
        row = state['asks'].get(cid)
        if not row or row['status'] not in {'waiting', 'chosen'} or not row.get('message_ts'):
            return
        outcome = {'applied': f'`{row.get("choice")}`으로 나머지 원본을 맞췄어요.',
                   'kept': '각 원본을 그대로 두고 지금 내용을 새 기준으로 삼았어요.',
                   'cleared': '원본이 다시 맞춰져 충돌이 풀렸어요. 따로 바꾸지 않았어요.'}[resolved['status']]
        send(row['channel'], f'*🟢 {row["title"]} — {HEAD}*\n{outcome}', message_ts=row['message_ts'])
        row['status'] = 'closed'; save()


async def dispatch(home, event=None, **_):
    if not enabled(home) or not (Path(home) / 'state/conflict-decisions.json').is_file():
        return None
    source = getattr(event, 'source', None)
    if getattr(getattr(source, 'platform', None), 'value', '') != 'slack':
        return None
    if getattr(source, 'user_id', '') != slack_report._sender().owner(home):
        return None
    from hermes_constants import get_hermes_home
    if Path(get_hermes_home()).resolve() != Path(home).resolve():
        return None
    def process():
        channel, thread = getattr(source, 'chat_id', ''), getattr(source, 'thread_id', '')
        try:
            result = handle(home, channel, thread, str(getattr(event, 'text', '') or '').strip(),
                            (getattr(event, 'raw_message', None) or {}).get('_hermes_button'))
        except Exception as exc:
            logging.getLogger(__name__).warning('conflict_choice_failed:%s', type(exc).__name__)
            result = ('failed', '처리가 중단됐어요. 버튼을 다시 눌러 주세요.')
        if result is None:
            return None
        color = {'done': '🟢', 'waiting': '🟡', 'failed': '🔴'}[result[0]]
        try:
            slack_report.post_thread(home, channel, f'*{color} {HEAD}*\n{result[1]}', thread_ts=thread)
        except Exception as exc:
            logging.getLogger(__name__).warning('conflict_choice_notify_failed:%s', type(exc).__name__)
        return {'action': 'skip', 'reason': 'linked-work conflict choice handled by plugin'}
    return await asyncio.to_thread(process)
