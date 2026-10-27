"""Slack mrkdwn report text -> Block Kit, so scripted reports read like the agents' replies (2026-10-16).

Report layout (owner decision 2026-10-19): a title, then sections, each in its own coloured bar. A section starts at a
bold line or heading after a blank line; its words pick the colour (red urgent/failed, yellow important/warning, green
to check or approve, grey otherwise); the title's status mark (an information mark counts as grey) or words are
the fallback colour, and the mark is not shown in the title.
SNS담당 (sns-manager) keeps the single-bar layout it already had.

A bold first line becomes a large header, the status emoji in it picks the side-bar colour, bullets and
indented lines become nested lists, `>` lines quotes and ``` fences code. The original ``text`` always
stays as the notification fallback; anything Slack might reject returns None so the text is sent alone.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

COLORS = {'fail': '#E01E5A', 'warn': '#ECB22E', 'ok': '#2EB67D', 'info': '#1D9BD1', 'check': '#2EB67D', 'plain': '#9AA0A6'}
# A section heading's words -> its bar colour; the first match wins.
# Yellow means "answer received, still working" (owner, 2026-10-23/26): caution words in a section no
# longer turn the bar yellow; only a title's 🟡/⚠️ mark does.
SECTION_STATUS = (('fail', ('긴급', '중요도 높음', '실패', '오류', '장애', '위험', '차단')),
                  ('check', ('중요도 보통', '중요', '주의', '경고', '지연',
                             '체크', '확인 필요', '확인할', '승인', '할 일', '결정', '답 필요', '답변 필요')))
STATUS = (('fail', ('🔴', '❌', '🚨', '⛔', ':red_circle:', ':x:', ':rotating_light:')),
          ('warn', ('🟡', '⚠️', '⚠', '🟠', ':warning:', ':large_yellow_circle:', ':large_orange_circle:')),
          ('ok', ('🟢', '✅', ':large_green_circle:', ':white_check_mark:')),
          ('info', ('🔵', 'ℹ️', '📌', ':large_blue_circle:', ':information_source:')))
_UNDERLINE_TAG = re.compile(r'</?u>')


def _profile():
    """The profile whose copy this is: ~/.hermes/profiles/<name>/plugins/team-kit/slack_blocks.py; '' for the root."""
    here = Path(__file__).resolve()
    return here.parents[2].name if len(here.parents) > 3 and here.parents[3].name == 'profiles' else ''


SECTIONED = _profile() != 'sns-manager'
MAX_BLOCKS = 48
MAX_SECTIONS = 12
MAX_CHARS = 11000

_TITLE = re.compile(r'^\*([^*\n]{1,140})\*$')
_BULLET = re.compile(r'^(\s*)(?:[•\-–▪◦·]|(\d+)[.)])\s+(.*)$')
_ARROW = re.compile(r'^(\s*)↳\s*(.*)$')
_FENCE = re.compile(r'^\s*```')
_HEADING = re.compile(r'^\s{0,3}#{1,6}\s+(.+?)\s*#*$')
_RULE = re.compile(r'^\s*(?:-{3,}|_{3,}|—{2,})\s*$')
_MD_LINK = re.compile(r'\[([^\]\n]+)\]\((https?://[^)\s]+)\)')
_TOKEN = re.compile(r'<u>(?P<u>.+?)</u>|<(?P<link>[^<>\n]+)>|`(?P<code>[^`\n]+)`'
                    r'|(?P<url>https?://[^\s<>]+[^\s<>.,;:!?)\]』」’”])'
                    r'|\*\*(?P<b2>(?=\S)[^*\n]+?(?<=\S))\*\*'
                    r'|(?<!\*)\*(?P<b>(?=\S)[^*\n]+?(?<=\S))\*(?!\*)|(?<![\w_])_(?P<i>[^_\n]+?)_(?![\w_])'
                    r'|(?<!~)~(?P<s>(?=\S)[^~\n]+?(?<=\S))~(?!~)')


def _plain(text):
    return text.replace('&lt;', '<').replace('&gt;', '>').replace('&amp;', '&')


def _link(raw, style):
    target, _, label = raw.partition('|')
    if target.startswith('@'):
        return {'type': 'user', 'user_id': target[1:]}
    if target.startswith('#'):
        return {'type': 'channel', 'channel_id': target[1:]}
    if target.startswith('!'):
        name = target[1:].split('^')[0]
        if name in ('here', 'channel', 'everyone'):
            return {'type': 'broadcast', 'range': name}
        return {'type': 'text', 'text': _plain(label or target), **({'style': style} if style else {})}
    if ':' not in target:
        return {'type': 'text', 'text': _plain('<' + raw + '>'), **({'style': style} if style else {})}
    return {'type': 'link', 'url': _plain(target), **({'text': _plain(label)} if label else {}),
            **({'style': style} if style else {})}


def inline(text, style=None, plain=False):
    """mrkdwn emphasis/links -> rich_text elements; ``plain`` keeps every character literal."""
    style = dict(style or {})
    if plain:
        return [{'type': 'text', 'text': text, **({'style': style} if style else {})}] if text else []
    # Agent-written markdown links reuse the mrkdwn link path.
    text = _MD_LINK.sub(lambda m: '<' + m.group(2) + '|' + m.group(1) + '>', text)
    out, pos = [], 0
    for m in _TOKEN.finditer(text):
        if m.start() > pos:
            out.append({'type': 'text', 'text': _plain(text[pos:m.start()]), **({'style': dict(style)} if style else {})})
        kind = m.lastgroup
        if kind == 'link':
            out.append(_link(m.group('link'), style))
        elif kind == 'url':
            out.append({'type': 'link', 'url': _plain(m.group('url')), **({'style': dict(style)} if style else {})})
        elif kind == 'code':
            out.append({'type': 'text', 'text': _plain(m.group('code')), 'style': {**style, 'code': True}})
        else:
            key = {'u': 'underline', 'b': 'bold', 'b2': 'bold', 'i': 'italic', 's': 'strike'}[kind]
            out.extend(inline(m.group(kind), {**style, key: True}))
        pos = m.end()
    if pos < len(text):
        out.append({'type': 'text', 'text': _plain(text[pos:]), **({'style': dict(style)} if style else {})})
    return [e for e in out if e.get('type') != 'text' or e['text']]


def status(title):
    for name, marks in STATUS:
        if any(mark in title for mark in marks):
            return name
    return None


def without_status(title):
    """The title without the status mark it starts with; the bar's colour carries it instead."""
    for _, marks in STATUS:
        for mark in marks:
            if title.startswith(mark):
                rest = title[len(mark):].lstrip('\ufe0f').strip()
                return rest or title
    return title


def section_status(heading):
    for name, words in SECTION_STATUS:
        if heading and any(word in heading for word in words):
            return name
    return None


def _depth(spaces):
    """Reports indent with 2 or 4 spaces per level; both mean one level per step."""
    return (len(spaces.expandtabs(4)) + 3) // 4


def _section(elements):
    return {'type': 'rich_text_section', 'elements': elements or [{'type': 'text', 'text': ' '}]}


def _paragraph(lines, plain, arrows=False):
    """One blank-line-separated paragraph -> one rich_text block. ``arrows`` keeps a ↳ detail line's arrow."""
    elements, run, run_key, prose, quote = [], [], None, [], []

    def flush_list():
        nonlocal run, run_key
        if run:
            elements.append({'type': 'rich_text_list', 'style': run_key[1], 'indent': run_key[0], 'elements': run})
        run, run_key = [], None

    def flush_prose():
        if prose:
            joined = []
            for index, line in enumerate(prose):
                if index:
                    joined.append({'type': 'text', 'text': '\n'})
                joined.extend(inline(line, plain=plain))
            elements.append(_section(joined))
            prose.clear()

    def flush_quote():
        if quote:
            joined = []
            for index, line in enumerate(quote):
                if index:
                    joined.append({'type': 'text', 'text': '\n'})
                joined.extend(inline(line, plain=plain))
            elements.append({'type': 'rich_text_quote', 'elements': joined or [{'type': 'text', 'text': ' '}]})
            quote.clear()

    for line in lines:
        bullet, arrow = _BULLET.match(line), _ARROW.match(line)
        if line.lstrip().startswith('>') or line.lstrip().startswith('&gt;'):
            flush_list(), flush_prose()
            quote.append(re.sub(r'^\s*(?:>|&gt;)\s?', '', line))
            continue
        flush_quote()
        if bullet or arrow or (line.startswith('  ') and (run or prose)):
            flush_prose()
            if bullet:
                depth, body = _depth(bullet.group(1)), bullet.group(3)
                kind = 'ordered' if bullet.group(2) else 'bullet'
            elif arrow:
                depth, body, kind = max(1, _depth(arrow.group(1))), ('↳ ' if arrows else '') + arrow.group(2), 'bullet'
            else:
                depth, body, kind = max(1, _depth(line[:len(line) - len(line.lstrip())])), line.strip(), 'bullet'
            depth = min(depth, 5)
            if run_key is not None and run_key != (depth, kind):
                flush_list()
            run_key = (depth, kind)
            run.append(_section(inline(body, plain=plain)))
            continue
        flush_list()
        prose.append(line)
    flush_list(), flush_prose(), flush_quote()
    return {'type': 'rich_text', 'elements': elements} if elements else None


def render(text, *, plain=False, sectioned=None):
    """``(blocks, attachments)`` for a report text, or None when it should go out as text only.

    ``sectioned`` (default: every profile but SNS담당) puts the sections under one bar coloured by the most urgent heading."""
    sectioned = SECTIONED if sectioned is None else sectioned
    try:
        if not isinstance(text, str) or not text.strip() or len(text) > MAX_CHARS:
            return None
        lines = text.replace('\r\n', '\n').strip('\n').split('\n')
        header, mark, titled = None, None, None
        first = lines[0].strip()
        title = _TITLE.match(first) or _HEADING.match(first)
        if title and not plain:
            clean = _plain(re.sub(r'</?u>|[*_~`]', '', title.group(1))).strip()
            mark = status(clean)
            titled = section_status(clean)
            shown = without_status(clean) if sectioned else clean
            header = {'type': 'header', 'text': {'type': 'plain_text', 'text': shown[:150], 'emoji': True}}
            lines = lines[1:]
        # The words before the first heading are a section of their own, without a heading.
        sections = [{'heading': None, 'blocks': []}]
        body, para, fence = sections[-1]['blocks'], [], None
        for line in lines + ['']:
            if fence is not None:
                if _FENCE.match(line):
                    body.append({'type': 'rich_text', 'elements': [{'type': 'rich_text_preformatted', 'elements': [
                        {'type': 'text', 'text': _plain('\n'.join(fence)) or ' '}]}]})
                    fence = None
                else:
                    fence.append(line)
                continue
            if _FENCE.match(line):
                if para:
                    body.append(_paragraph(para, plain, sectioned))
                    para = []
                fence = []
                continue
            heading = _HEADING.match(line) if not plain else None
            bold = _TITLE.match(line.strip()) if sectioned and not plain and not para else None
            if sectioned and (heading or bold):
                if para:
                    body.append(_paragraph(para, plain, sectioned))
                    para = []
                name = _plain(re.sub(r'</?u>|[*_~`]', '', (heading or bold).group(1))).strip()
                sections.append({'heading': name, 'blocks': []})
                body = sections[-1]['blocks']
                para.append('*' + name + '*')
                continue
            if not plain and (_RULE.match(line) or heading):
                if para:
                    body.append(_paragraph(para, plain, sectioned))
                    para = []
                if _RULE.match(line):
                    body.append({'type': 'divider'})
                else:
                    para.append('*' + heading.group(1).strip('* ') + '*')
                continue
            if line.strip():
                para.append(line.rstrip())
            elif para:
                body.append(_paragraph(para, plain, sectioned))
                para = []
        if fence is not None:
            return None
        for section in sections:
            section['blocks'] = [b for b in section['blocks'] if b]
        sections = [section for section in sections if section['blocks']]
        body = [b for section in sections for b in section['blocks']]
        if not header and not body:
            return None
        if len(body) + 1 > MAX_BLOCKS or len(sections) > MAX_SECTIONS:
            return None
        top = [header] if header else []
        # Sections keep to the owner's four colours, so an information mark (🔵) leaves them grey.
        fallback = (mark if mark != 'info' else None) or titled or 'plain'
        if sectioned and body and (header or any(section['heading'] for section in sections)):
            # One bar for the whole post (owner, 2026-10-19): Slack and desktop-pet draw two lines beside every
            # section once a post has several attachments. The most urgent section heading picks its colour.
            found = {section_status(section['heading']) for section in sections} | {fallback}
            colour = next(name for name in ('fail', 'warn', 'check', 'ok', 'plain') if name in found)
            return top, [{'color': COLORS[colour], 'blocks': body}]
        if mark and body:
            return top, [{'color': COLORS[mark], 'blocks': body}]
        return top + body, []
    except Exception:
        return None


# Same contract as the gateway's plugins/platforms/slack/reply_buttons.py: a trailing [[버튼: a | b]] line
# becomes reply buttons, and a click is handled there as the owner typing that label in the thread.
_BUTTONS = re.compile(r'\n?[ \t]*\[\[버튼:([^\]\n]*)\]\][ \t]*\s*\Z')
BUTTON_BLOCK = 'hermes_reply_buttons'


def buttons(labels):
    """Trailing marker line for reply buttons (at most 5 labels of 30 characters)."""
    labels = [str(label).strip() for label in labels if str(label).strip() and '|' not in str(label)]
    return '\n[[버튼: ' + ' | '.join(labels) + ']]' if labels else ''


def _split_buttons(text):
    match = _BUTTONS.search(text)
    if not match:
        return text, []
    labels = [label.strip() for label in match.group(1).split('|')]
    labels = list(dict.fromkeys(label for label in labels if label and len(label) <= 30))[:5]
    return text[:match.start()].rstrip(), labels


def _actions(labels):
    return {'type': 'actions', 'block_id': BUTTON_BLOCK, 'elements': [
        {'type': 'button', 'action_id': f'hermes_reply_button_{i}',
         'text': {'type': 'plain_text', 'text': label, 'emoji': True}, 'value': label}
        for i, label in enumerate(labels)]}


def apply(body, *, plain=None, update=False):
    """Text body -> Block Kit, plus reply buttons from a trailing ``[[버튼: …]]`` line."""
    if isinstance(body, dict) and isinstance(body.get('text'), str):
        text, labels = _split_buttons(body['text'])
        if labels:
            # Owner decision 2026-10-23: a message with buttons is a question to the owner. It is posted
            # under a red bar (buttons after the body); the gateway turns it yellow once answered and green
            # when closed. An update keeps the colour its report chose (yellow while partly decided).
            out = _apply({**body, 'text': text}, plain=plain, update=update)
            attachments = out.get('attachments')
            if attachments and isinstance(attachments[-1], dict) and isinstance(attachments[-1].get('blocks'), list) \
                    and len(attachments[-1]['blocks']) < 50:
                bars = [{**a, 'color': COLORS['fail']} if not update else a for a in attachments]
                bars[-1] = {**bars[-1], 'blocks': bars[-1]['blocks'] + [_actions(labels)]}
                return {**out, 'attachments': bars}
            base = list(out.get('blocks') or []) or [{'type': 'section', 'text': {'type': 'mrkdwn', 'text': text[:3000]}}]
            if len(base) < 50:
                # Slack prints ``text`` when there are no top-level blocks, so one block stays above the bar.
                out = {**out, 'blocks': base[:1], 'attachments': [{'color': COLORS['warn' if update else 'fail'], 'blocks': base[1:] + [_actions(labels)]}]}
            return out
    return _apply(body, plain=plain, update=update)


def _apply(body, *, plain=None, update=False):
    """Add blocks to a chat.postMessage/chat.update body built from ``text``; returns a new body.

    Existing ``blocks`` win. Existing ``attachments`` (e.g. per-item colour bars) are kept and the rendered
    text goes above them. ``update`` always sends ``blocks`` because chat.update keeps a message's old
    blocks when the field is missing, which would hide the new text.
    """
    if not isinstance(body, dict) or 'blocks' in body or not isinstance(body.get('text'), str):
        return body
    rendered = render(body['text'], plain=(body.get('mrkdwn') is False) if plain is None else plain)
    out = dict(body)
    if not rendered:
        if update:
            out['blocks'] = []
        return out
    blocks, attachments = rendered
    out['text'] = _UNDERLINE_TAG.sub('', body['text'])  # notification fallback has no underline
    if body.get('attachments'):
        out['blocks'] = blocks + [block for item in attachments for block in item['blocks']]
    else:
        out['blocks'] = blocks
        if attachments:
            out['attachments'] = attachments
    return out


def with_blocks(body, update=False):
    """Keep plain text delivery if rendering fails; never log report contents."""
    try:
        return apply(body, update=update)
    except Exception as exc:
        logging.getLogger(__name__).warning("Slack report rendering failed (%s)", type(exc).__name__)
        return body
