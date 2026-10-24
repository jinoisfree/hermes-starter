"""Deadline identity: preserve date-only values, compare timed values in Seoul."""
from datetime import date, datetime
import re
from zoneinfo import ZoneInfo

KST = ZoneInfo('Asia/Seoul')


def canonical(value, *, all_day=False):
    text = str(value or '').strip()
    if not text:
        return None
    if re.fullmatch(r'\d{4}-\d{2}-\d{2}', text):
        return date.fromisoformat(text).isoformat()
    moment = datetime.fromisoformat(text.replace('Z', '+00:00'))
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=KST)
    moment = moment.astimezone(KST)
    if all_day:
        return moment.date().isoformat()
    if moment.second or moment.microsecond:
        raise ValueError('deadline_minute_precision_required')
    return moment.isoformat(timespec='minutes')


def native(value):
    value = canonical(value)
    if value is None:
        raise ValueError('deadline_required')
    return value if len(value) == 10 else datetime.fromisoformat(value).strftime('%Y-%m-%d %H:%M')


def reminder_options(value):
    value = canonical(value)
    # A date-only source does not authorize inventing a notification time.
    return ['--due', native(value), '--clear-alarm'] if len(value) == 10 else [
        '--due', native(value), '--alarm', native(value)]


def label(value):
    value = canonical(value)
    return value if len(value) == 10 else native(value)


NOTE_STAMP = re.compile(r'(?:\d{4}[-./]\d{1,2}[-./]\d{1,2}|\d{1,2}/\d{1,2}|\d{1,2}월\s*\d{1,2}일)'
                        r'(?:(?:T|\s+)(?:(오전|오후)\s*)?(\d{1,2})(?::|시\s*)(\d{2})?(?:분)?(?::00)?(Z|[+-]\d{2}:\d{2})?)?')


def note_value(text, parse_day):
    match = NOTE_STAMP.search(text)
    if not match:
        return None
    day = parse_day(match.group(0))
    if not day:
        return None
    period, hour, minute, zone = match.groups()
    if hour is None:
        return day
    h = int(hour)
    if period:
        if not 1 <= h <= 12: return None
        h = h % 12 + (12 if period == '오후' else 0)
    try:
        return canonical(f'{day}T{h:02d}:{int(minute or 0):02d}{zone or "+09:00"}')
    except ValueError:
        return None
