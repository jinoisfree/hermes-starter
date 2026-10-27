"""Notes summary lists and the HTML subset preserved by Apple's body importer."""
from __future__ import annotations

import html
import re

COLORS = {"red": "#FF3B30", "gray": "#8E8E93", "blue": "#0A84FF"}
# Only a complete, reserved prefix is markup; symbols elsewhere are literal.
FORMAT = re.compile(r"^\{fmt:(red|gray|blue|plain|u|red,u|gray,u|blue,u|plain,u)\}\s*")
DATE = re.compile(r"(?<![\w/])(?:\d{4}[-./]\d{1,2}[-./]\d{1,2}|\d{1,2}/\d{1,2}|(?:\d{4}년\s*)?\d{1,2}월\s*\d{1,2}일)(?!\d)")
LEGACY_BLOCKS = {
    "확인사항": "확인 및 결정", "확인": "확인 및 결정", "확인해야 할 사항": "확인 및 결정",
    "조치": "확인 및 결정", "일정/기한": "일정", "기한": "일정", "스케줄": "일정",
    "스케쥴": "일정", "운영정보": "운영 방식", "운영": "운영 방식", "운영 정보": "운영 방식",
    "장소": "장소 정보", "담당자": "담당자 정보", "담당": "담당자 정보",
    "링크": "관련 링크", "유의": "유의사항", "주의": "유의사항",
}


def parse(summary, *, errors=None):
    grouped = {}
    parent = None
    for number, raw in enumerate(summary.splitlines(), 1):
        line = raw.strip()
        if not line.startswith(("- ", "• ", "○ ")):
            continue  # Existing non-summary text is not interpreted as markup.
        detail = line[2:].strip()
        # Consume a row style before splitting the section at its colon.
        leading = FORMAT.match(detail)
        if leading:
            detail = detail[leading.end():]
        child = raw.startswith("  - ") and parent is not None
        if child:
            rows = parent["children"]
        else:
            section, separator, rest = detail.partition(":")
            if not separator:
                section, separator, rest = detail.partition("：")
            heading = LEGACY_BLOCKS.get(section.strip(), section.strip()) if separator else "핵심 내용"
            if separator:
                detail = rest.strip()
            rows = grouped.setdefault(heading or "핵심 내용", [])
        checked = None
        flags = leading[1].split(",") if leading else []
        color = next((f for f in flags if f != "u"), "plain") if leading else None
        underline = "u" in flags
        escaped = detail.startswith("\\")
        if escaped:
            detail = detail[1:]
        else:
            if detail.startswith(("[ ] ", "[x] ")):
                checked = detail[1] == "x"
                detail = detail[4:]
            match = FORMAT.match(detail)
            if match:
                flags = match[1].split(",")
                color = next((f for f in flags if f != "u"), "plain")
                underline = "u" in flags
                detail = detail[match.end():]
        if errors is not None and ((not child and "{fmt" in heading) or
                                   (not escaped and "{fmt" in detail)):
            errors.append((number, raw))
        label, _, value = detail.partition("|")
        label, value = label.strip(), value.strip()
        if color is None and any(word in label for word in ("유의", "주의", "위험", "기한", "마감")):
            color = "red"
        # Task marks, their text and their child details share the owner's blue.
        if checked is not None or (child and parent["checked"] is not None):
            color = "blue"
        row = dict(label=label, value=value, checked=checked, color=color,
                   underline=underline, children=[])
        rows.append(row)
        if not child:
            parent = row
    return grouped


def validate_summary(summary):
    """Report unconsumed reserved markup before a write; escaped rows stay literal."""
    errors = []
    parse(summary, errors=errors)
    if errors:
        number, raw = errors[0]
        return (f"summary {number}행의 서식을 해석하지 못했습니다: {raw}. "
                "구역 콜론 뒤, 라벨 앞에 둡니다. 예: - 확인 필요: {fmt:gray} 수익 모델 | 값")
    return None


def _value_html(value):
    if re.fullmatch(r"https?://[^\s]+", value):
        safe = html.escape(value, quote=True)
        return f'<a href="{safe}">{safe}</a>'
    result, start = [], 0
    for match in DATE.finditer(value):
        result.extend((html.escape(value[start:match.start()]), "<b>" + html.escape(match[0]) + "</b>"))
        start = match.end()
    result.append(html.escape(value[start:]))
    return "".join(result)


def render(grouped, icon_for):
    def row_html(row):
        mark = "" if row["checked"] is None else ("☑ " if row["checked"] else "☐ ")
        content = mark + "<b>" + html.escape(row["label"]) + "</b>"
        if row["value"]:
            content += ": " + _value_html(row["value"])
        if row["underline"]:
            content = "<u>" + content + "</u>"
        if row["color"] in COLORS:
            content = '<font color="' + COLORS[row["color"]] + '">' + content + '</font>'
        if row["children"]:
            content += "<ul>" + "".join(row_html(child) for child in row["children"]) + "</ul>"
        return "<li>" + content + "</li>"
    rendered = []
    for heading, rows in grouped.items():
        icon = icon_for(heading)
        display = (icon + " " if icon else "") + heading
        rendered.append('<div><br></div><div style="font-size:22px;font-weight:700;line-height:1.3;">'
                        + html.escape(display) + '</div><ul style="font-size:15px;line-height:1.45;">'
                        + "".join(row_html(row) for row in rows) + '</ul>')
    return "".join(rendered)


class ListReader:
    """Accept both nested li/ul and Notes' normalized sibling li, ul layout."""
    def __init__(self, heading):
        self.heading = heading
        self.depth = 0
        self.rows = []
        self.current = None
        self.styles = []

    def start(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "ul":
            self.depth += 1
            self.current = None
        elif tag == "li":
            self.current = {"depth": self.depth, "spans": []}
            self.rows.append(self.current)
        elif tag == "br" and self.current is not None:
            self.data(" ")
        if tag not in ("br", "img", "input", "hr"):
            self.styles.append((tag, attrs))

    def end(self, tag):
        if tag == "ul":
            self.depth -= 1
        if tag in ("li", "ul"):
            self.current = None
        for i in range(len(self.styles) - 1, -1, -1):
            if self.styles[i][0] == tag:
                del self.styles[i:]
                break

    def data(self, text):
        if self.current is None:
            return
        bold = any(tag in ("b", "strong") for tag, _ in self.styles)
        under = any(tag == "u" for tag, _ in self.styles) and not any(tag == "a" for tag, _ in self.styles)
        color = next((attrs["color"].upper() for _, attrs in reversed(self.styles) if "color" in attrs), "")
        self.current["spans"].append((text, bold, under, color))

    def text(self):
        lines = []
        for row in self.rows:
            chars = [(c, bold, under, color) for text, bold, under, color in row["spans"] for c in text]
            while chars and chars[0][0].isspace(): chars.pop(0)
            while chars and chars[-1][0].isspace(): chars.pop()
            if not chars:
                continue
            checked = None
            if len(chars) > 1 and chars[0][0] in "☐☑" and not chars[0][1] and chars[1][0] == " " and not chars[1][1]:
                checked = chars[0][0] == "☑"
                chars = chars[2:]
            # Labels are bold; a colon inside a label is literal, not a separator.
            split = next((i for i, c in enumerate(chars) if c[0] == ":" and not c[1]
                          and i + 1 < len(chars) and chars[i + 1][0] == " "), None)
            label = "".join(c[0] for c in (chars[:split] if split is not None else chars)).strip()
            value = "".join(c[0] for c in chars[split + 1:]).strip() if split is not None else ""
            colors = {c[3] for c in chars if not c[0].isspace()}
            color = next((name for name, code in COLORS.items() if code in colors), None)
            under = any(c[2] for c in chars)
            # plain prevents the old automatic red heuristic from changing a readback.
            flags = ([color] if color else ["plain"] if under or any(w in label for w in ("유의", "주의", "위험", "기한", "마감")) else [])
            if under: flags.append("u")
            prefix = ("[x] " if checked else "[ ] ") if checked is not None else ""
            if flags: prefix += "{fmt:" + ",".join(flags) + "} "
            if not prefix and (label.startswith(("[ ] ", "[x] ", "\\")) or FORMAT.match(label)):
                label = "\\" + label
            body = prefix + label + (" | " + value if value else "")
            lines.append(("  - " if row["depth"] > 1 else "- " + self.heading + ": ") + body)
        return "\n".join(lines)
