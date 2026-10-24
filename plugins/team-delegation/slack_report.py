"""Block Kit rendering and DM delivery for the work-assistant linked-work watcher (2026-10-16).

Slack has no arbitrary text colour or size: size comes from header/context blocks, colour from
the attachment side bar and status emoji, emphasis from bold/italic/code/underline rich text.
"""
from __future__ import annotations

import json
import re
import importlib.util
from functools import lru_cache
from pathlib import Path

COLORS = {'ok': '#2EB67D', 'warn': '#ECB22E', 'fail': '#E01E5A'}
TOKEN_RE = re.compile(r'(「[^」]*」|`[^`]*`)')


def _inline(text, *, underline=False):
    parts = []
    for piece in TOKEN_RE.split(text):
        if not piece:
            continue
        style = {}
        if piece.startswith('「'):
            style['bold'] = True
        elif piece.startswith('`'):
            piece, style = piece[1:-1], {'code': True}
        if underline and not style:
            style['underline'] = True
        parts.append({'type': 'text', 'text': piece, **({'style': style} if style else {})})
    return parts


def _items(lines):
    """('done'|'warn', depth, text) from the watcher's bullet lines."""
    out, kind = [], 'done'
    for raw in '\n'.join(lines).splitlines():
        if not raw.strip():
            continue
        if raw.startswith('• '):
            text = raw[2:]
            kind = 'warn' if text.startswith('⚠️') else 'done'
            out.append((kind, 0, text.removeprefix('⚠️').strip()))
        else:
            out.append((kind, 1, raw.strip().removeprefix('↳').strip()))
    return out


def render(title, url, subtitle, lines, when):
    items = _items(lines)
    failed = any(k == 'warn' and '못 넣음' in t for k, _, t in items)
    status = 'fail' if failed else 'warn' if any(k == 'warn' for k, _, _ in items) else 'ok'
    mark = {'ok': '🟢', 'warn': '🟡', 'fail': '🔴'}[status]
    blocks = [{'type': 'header', 'text': {'type': 'plain_text', 'text': f'{mark} {title}'[:150], 'emoji': True}},
              {'type': 'context', 'elements': [{'type': 'mrkdwn', 'text': f'{subtitle} · {when}' + (f' · <{url}|노션에서 열기>' if url else '')}]}]
    body = []
    for kind, heading in (('done', '반영한 것'), ('warn', '확인이 필요한 것')):
        group = [(d, t) for k, d, t in items if k == kind]
        if not group:
            continue
        body.append({'type': 'rich_text_section', 'elements': [{'type': 'text', 'text': heading, 'style': {'bold': True}}]})
        run, depth = [], None
        for d, t in group:
            if depth is not None and d != depth:
                body.append({'type': 'rich_text_list', 'style': 'bullet', 'indent': depth, 'elements': run})
                run = []
            depth = d
            run.append({'type': 'rich_text_section', 'elements': _inline(t, underline=(kind == 'warn' and d == 0 and '주세요' in t))})
        body.append({'type': 'rich_text_list', 'style': 'bullet', 'indent': depth, 'elements': run})
    attachments = [{'color': COLORS[status], 'blocks': [{'type': 'rich_text', 'elements': body}]}] if body else []
    return blocks, attachments


@lru_cache(maxsize=1)
def _sender():
    file = Path(__file__).resolve().parent.parent / 'team-kit' / 'learning_report.py'
    spec = importlib.util.spec_from_file_location('linked_work_learning_report', file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def report_room(home):
    """Owner report room (team-kit.settings.report_channel); None keeps the owner's DM."""
    import hermes_yaml as yaml
    try:
        config = yaml.safe_load((Path(home) / 'config.yaml').read_text()) or {}
    except FileNotFoundError:
        return None
    room = (((config.get('plugins') or {}).get('entries') or {}).get('team-kit', {}).get('settings') or {}).get('report_channel')
    return room if isinstance(room, str) and re.fullmatch(r'[CG][A-Z0-9]+', room) else None


def post_dm(home, reports):
    """Send each report to the owner report room (else DM) with the work-assistant app. Returns texts not delivered."""
    R = _sender()
    channel = report_room(home) or R.slack(home, 'conversations.open', {'users': R.owner(home)})['channel']['id']
    missed = []
    for report in reports:
        blocks, attachments = render(report['title'], report.get('url'), report['subtitle'], report['lines'], report['when'])
        try:
            R.slack(home, 'chat.postMessage', {'channel': channel, 'text': report['fallback'], 'blocks': blocks,
                                              'attachments': attachments, 'unfurl_links': False})
        except Exception:
            missed.append(report['fallback'])
    return missed
