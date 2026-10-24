"""Bounded morning summaries and readable Slack detail pages (no network I/O)."""
import re


def escape(text):
    return str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def excerpt(text, limit=80):
    """One plain line, bounded after escaping; never cut an escape sequence."""
    text = ' '.join(str(text).split())
    text = re.sub(r'[*_`~]', '', text)
    result = ''
    for char in text:
        escaped = escape(char)
        if len(result) + len(escaped) > limit - 1:
            return result + '…'
        result += escaped
    return result


def summary(title, status, rows, action):
    paragraphs = [f'*{title}*\n{status}']
    for index, (label, text) in enumerate(rows[:5], 1):
        paragraphs.append(f'*{index}. {excerpt(label, 48)}*\n> {excerpt(text, 90)}')
    if len(rows) > 5:
        paragraphs.append(f'외 *{len(rows) - 5}건* · 전체 {len(rows)}건은 스레드에서 확인')
    paragraphs.append('📎 전체 내용·근거는 이 스레드에 이어서\n' + action)
    return '\n\n'.join(paragraphs)


def empty_learning(day):
    """One dated notice, with no review action or nonexistent detail thread."""
    return f'*🔵 아침 학습 보고 · {day.isoformat()}*\n\n새로 학습한 내용이 없어. (0건)'


def chunks(paragraphs, limit=2800):
    """Preserve blank lines and short paragraphs, including very long single lines."""
    pages, current = [], ''
    for paragraph in paragraphs:
        # A Slack link or escaped character must stay together at a page boundary.
        tokens = re.findall(r'<[^>\n]+>|&(?:amp|lt|gt);|.', paragraph, re.DOTALL)
        parts, part = [], ''
        for token in tokens:
            if part and len(part) + len(token) > limit:
                parts.append(part)
                part = ''
            # Only malformed/oversized links reach this fallback.
            while len(token) > limit:
                if part:
                    parts.append(part)
                    part = ''
                parts.append(token[:limit])
                token = token[limit:]
            part += token
        if part:
            parts.append(part)
        for part in parts:
            if current and len(current) + len(part) + 2 > limit:
                pages.append(current)
                current = ''
            current += ('\n\n' if current else '') + part
    if current:
        pages.append(current)
    return pages



def metadata(marker):
    return {'event_type': 'hermes_morning_report', 'event_payload': {'marker': marker}}


def matches(message, marker):
    # Reports sent before the text-only layout still carry block IDs.
    return ((message.get('metadata') or {}).get('event_payload', {}).get('marker') == marker
            or any(block.get('block_id') == marker for block in message.get('blocks', [])))


def item_messages(text, kind, number):
    """One item per message; only its last bounded continuation carries actions."""
    marker = "\n[[버튼: " + " | ".join(f"{kind} {number}번 {action}" for action in ("승인", "거절", "수정")) + "]]"
    pages = chunks([text], limit=2800 - len(marker))
    pages[-1] += marker
    return pages


def review_part(text):
    match = re.search(r"\n\[\[버튼: (학습|스킬) (\d+)번 승인 \| \1 \2번 거절 \| \1 \2번 수정\]\]\s*\Z", text)
    return dict(text=text, **({"review_kind": match[1], "item": int(match[2])} if match else {}))
