"""Read-only log anomaly patterns and per-tool failure rates for system-doctor diagnosis.

Ported from moonlight-lupin/agent-skills `agent-ops/log-analyzer`
(scripts/analyze_logs.py, scripts/hermes_log_window.py, scripts/state_failures.py)
https://github.com/moonlight-lupin/agent-skills/tree/ae671a22253ed3dfe81a96d8405aecd3fc9baec6/agent-ops/log-analyzer
commit ae671a22253ed3dfe81a96d8405aecd3fc9baec6

MIT License, Copyright (c) 2025-2026 moonlight-lupin. Permission is hereby granted,
free of charge, to any person obtaining a copy of this software and associated
documentation files, to deal in the Software without restriction, subject to the
condition that the above copyright notice and this permission notice shall be
included in all copies or substantial portions of the Software. THE SOFTWARE IS
PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND.

Local adaptations: Hermes comma-millisecond timestamps and `[session]` prefixes,
timestamp-only continuation lines (gateway.error.log), tail-only reading with a
byte cap, KST first/last seen, anomaly detectors limited to WARNING+ entries (INFO
lines mention "timeout"/"provider" routinely; upstream pitfall 10), `Tool X
returned error` tool failures, provider API errors, and the state.db monitor
classifying our JSON shapes (`exit_code`, `ok:false`, `success:false`, top-level
`error`) with SQLite JSON functions instead of loading result bodies.
Never writes, sends, restarts or runs subprocesses.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta
import os
import re
import sqlite3
import time

LOG_FILES = {'gateway': 'gateway.log', 'error': 'errors.log', 'agent': 'agent.log', 'gateway_error': 'gateway.error.log'}
LOG_TAIL_BYTES = 6 * 1024 * 1024
MAX_ENTRY_LINES = 200
TOP_GROUPS = 8
SAMPLE_CHARS = 240
CLUSTER_THRESHOLD = 3

LINE_RE = re.compile(r'^(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})(?:[,.](?P<ms>\d{1,6}))?(?:Z|[+-]\d{2}:?\d{2})?'
                     r'(?:\s+(?P<level>DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL|FATAL)\b)?\s?(?P<rest>.*)$')
SESSION_RE = re.compile(r'^\[[^\]]{1,80}\]\s+')
LOGGER_RE = re.compile(r'^(?P<logger>[A-Za-z0-9_.]{1,120}):\s+')
ERROR_LEVELS = {'ERROR', 'CRITICAL', 'FATAL'}
WARNING_LEVELS = {'WARNING', 'WARN'}

URL_RE = re.compile(r'https?://\S+')
IP_RE = re.compile(r'\b(?:\d{1,3}\.){3}\d{1,3}\b')
SID_RE = re.compile(r'\b\d{8}_\d{6}_[a-f0-9]+\b')
UUID_RE = re.compile(r'(?i)\b[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}\b')
HEX_RE = re.compile(r'(?i)\b(?=[0-9a-f]*[a-f])(?=[0-9a-f]*\d)[0-9a-f]{8,}\b')
SLACK_ID_RE = re.compile(r'\b[CDGUWTB][A-Z0-9]{8,12}\b')
NUMBER_RE = re.compile(r'\d+(?:\.\d+)?')
RATE_LIMIT_RE = re.compile(r'\b429\b|rate[ _-]?limit|too many requests|quota exceeded|ratelimited', re.I)
TIMEOUT_RE = re.compile(r'timed out|deadline exceeded|connection timeout|\btimeout\b|TimeoutError|TimeoutExpired|ReadTimeout', re.I)
PROVIDER_ERROR_RE = re.compile(r'API call failed|Retrying API call|error_type=\w+|\b\w*APIError\b|APIConnectionError|overloaded|InternalServerError|ServiceUnavailable|\b5\d\d\b (?:Server|Service|Bad Gateway)', re.I)
PROVIDER_RE = re.compile(r'\bprovider[=: ]+([A-Za-z0-9_.-]+)', re.I)
ERROR_TYPE_RE = re.compile(r'error_type=([A-Za-z0-9_.]+)')
KNOWN_PROVIDERS = ('claude', 'anthropic', 'openrouter', 'openai', 'google', 'gmail', 'slack', 'notion', 'instagram',
                   'groq', 'mistral', 'azure', 'bedrock', 'exa', 'ddgs', 'naver')
HERMES_TOOL_RE = re.compile(r'\bTool ([A-Za-z0-9_.-]+) returned error')
TOOL_PATTERNS = (HERMES_TOOL_RE,
                 re.compile(r'\btool_call[=: ]+([A-Za-z0-9_.-]+)', re.I),
                 re.compile(r'\btool[=:]\s*([A-Za-z0-9_.-]+)', re.I),
                 re.compile(r'\btool\s+([A-Za-z0-9_.-]+)\s+(?:failed|error|timeout)', re.I))
TRACEBACK = 'Traceback (most recent call last)'
EXCEPTION_LINE_RE = re.compile(r'^(?:[A-Za-z_][\w.]*\.)?[A-Za-z_]\w*(?:Error|Exception|Exit|Interrupt|Expired|Warning)\b:?')


def normalize(message):
    """Upstream normalize_message plus Hermes session/hash/Slack-id placeholders."""
    text = message.splitlines()[0] if message else ''
    text = SESSION_RE.sub('', text)
    text = URL_RE.sub('[URL]', text)
    text = SID_RE.sub('[SID]', text)
    text = UUID_RE.sub('[UUID]', text)
    text = HEX_RE.sub('[HEX]', text)
    text = IP_RE.sub('[IP]', text)
    text = SLACK_ID_RE.sub('[ID]', text)
    text = NUMBER_RE.sub('[N]', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return (text or '[empty message]')[:160]


def read_tail(diag, path, cap=None):
    cap = cap or LOG_TAIL_BYTES
    path = diag.safe(path, [path.parent])
    if not path.is_file():
        raise ValueError('Log file is missing or not a regular file')
    with path.open('rb') as stream:
        size = stream.seek(0, os.SEEK_END)
        start = max(0, size - cap)
        stream.seek(start)
        data = stream.read(cap)
    if start:
        data = data.partition(b'\n')[2]
    return data.decode('utf-8', errors='replace'), bool(start), size


def parse_entries(text, tz):
    """Group physical lines into entries; timestamp-only/indented lines continue the previous entry."""
    entries, current, physical, last_stamp = [], None, 0, None
    for raw in text.splitlines():
        physical += 1
        match = LINE_RE.match(raw)
        stamp = None
        if match:
            try:
                stamp = datetime.fromisoformat(match['ts'].replace('T', ' ')).replace(tzinfo=tz)
            except ValueError:
                match = None
        if match and stamp:
            last_stamp = stamp
        if match and match['level']:
            rest = match['rest'].strip()
            session = SESSION_RE.match(rest)
            body = rest[session.end():] if session else rest
            logger = LOGGER_RE.match(body)
            message = body[logger.end():] if logger else body
            current = {'at': stamp, 'level': 'WARNING' if match['level'] == 'WARN' else match['level'],
                       'component': logger['logger'] if logger else 'unknown', 'message': message, 'raw': raw,
                       'extra': [], 'lines': 1}
            entries.append(current)
            continue
        content = match['rest'] if match else raw
        if current is not None:
            current['lines'] += 1
            if len(current['extra']) < MAX_ENTRY_LINES:
                current['extra'].append(content)
        elif content.strip():
            # Lines before any leveled entry (e.g. a tail starting mid-traceback) stay separate.
            current = {'at': stamp or last_stamp, 'level': None, 'component': 'unknown', 'message': content.strip(),
                       'raw': raw, 'extra': [], 'lines': 1}
            entries.append(current)
    return entries, physical


def exception_line(entry):
    for line in reversed(entry['extra']):
        line = line.strip()
        if EXCEPTION_LINE_RE.match(line):
            return line
    return entry['message']


def extract_tool(entry):
    text = entry['message']
    for regex in TOOL_PATTERNS:
        match = regex.search(text)
        if match:
            tool = match.group(1).strip('.,;:[](){}').lower()
            if tool and tool not in {'failed', 'error', 'timeout', 'policy'}:
                return tool
    for tool in ('browser_navigate', 'execute_code', 'terminal', 'web_search', 'web_extract', 'vision_analyze'):
        if tool in text:
            return tool
    component = entry['component'].lower()
    if component.startswith('tools.'):
        return component.split('.', 1)[1]
    return 'unspecified'


def extract_provider(entry):
    text = (entry['component'] + ' ' + entry['message']).lower()
    match = PROVIDER_RE.search(text)
    if match:
        return match.group(1)[:60]
    for provider in KNOWN_PROVIDERS:
        if provider in text:
            return provider
    return entry['component'][:60] or 'unknown'


def analyze(diag, entries, since, until, top=TOP_GROUPS):
    """Port of analyze_entries for one already-windowed entry list."""
    kst = diag.KST
    def show(at):
        return at.astimezone(kst).strftime('%Y-%m-%d %H:%M:%S') if at else None
    def sample(entry, text=None):
        return diag.health_error_kind(text or entry['raw'])[:SAMPLE_CHARS]
    def grouped(items, key_fn, limit=top, sample_fn=None):
        groups = {}
        for entry in items:
            key = diag.health_error_kind(key_fn(entry))[:160]  # Redacted group keys.
            group = groups.setdefault(key, {'key': key, 'count': 0, 'first_seen_kst': None, 'last_seen_kst': None,
                                            'sample': (sample_fn or sample)(entry)})
            group['count'] += 1
            stamp = show(entry['at'])
            if stamp:
                group['first_seen_kst'] = group['first_seen_kst'] or stamp
                group['last_seen_kst'] = stamp
        ordered = sorted(groups.values(), key=lambda g: (-g['count'], g['key']))
        return {'groups': len(ordered), 'total': sum(g['count'] for g in ordered), 'top': ordered[:limit],
                'truncated': len(ordered) > limit}

    errors = [e for e in entries if e['level'] in ERROR_LEVELS]
    warnings = [e for e in entries if e['level'] in WARNING_LEVELS]
    notable = errors + warnings
    error_ids = {id(e) for e in errors}
    def full(entry):
        return entry['message'] + '\n' + '\n'.join(entry['extra'][-5:])
    crashes = [e for e in entries if e['level'] in {'CRITICAL', 'FATAL'} or any(TRACEBACK in line for line in e['extra'])
               or TRACEBACK in e['message']]
    tool_failures = [e for e in notable if extract_tool(e) != 'unspecified' and (HERMES_TOOL_RE.search(e['message']) or id(e) in error_ids)]
    components = defaultdict(lambda: {'errors': 0, 'warnings': 0})
    for entry in notable:
        components[entry['component']]['errors' if id(entry) in error_ids else 'warnings'] += 1
    timeline = Counter()
    bucket = '%Y-%m-%d' if (until - since) > timedelta(hours=48) else '%Y-%m-%d %H:00'
    for entry in errors:
        if entry['at']:
            timeline[entry['at'].astimezone(kst).strftime(bucket)] += 1
    error_groups = grouped(errors, lambda e: normalize(e['message']))
    for group in error_groups['top']:
        group['cluster'] = group['count'] >= CLUSTER_THRESHOLD
    return {
        'totals': {'entries': len(entries), 'errors': len(errors), 'warnings': len(warnings),
                   'traceback_chains': sum(sum(TRACEBACK in line for line in [e['message'], *e['extra']]) for e in entries)},
        'error_groups': error_groups,
        'warning_groups': grouped(warnings, lambda e: normalize(e['message']), limit=5),
        'rate_limits': grouped([e for e in notable if RATE_LIMIT_RE.search(full(e))], extract_provider, limit=5),
        'timeouts': grouped([e for e in notable if TIMEOUT_RE.search(full(e))], extract_tool, limit=5),
        'tool_failures': grouped(tool_failures, extract_tool),
        'provider_errors': grouped([e for e in notable if PROVIDER_ERROR_RE.search(full(e))],
                                   lambda e: (ERROR_TYPE_RE.search(e['message']) or [None, extract_provider(e)])[1], limit=5),
        'crashes': grouped(crashes, lambda e: normalize(exception_line(e)), limit=5,
                           sample_fn=lambda e: sample(e, exception_line(e))),
        'components': dict(sorted(components.items(), key=lambda kv: -(kv[1]['errors'] * 1000 + kv[1]['warnings']))[:10]),
        'error_timeline_kst': dict(sorted(timeline.items())[-48:]),
    }


# --- state.db failure-rate monitor (port of scripts/state_failures.py) ---------------------------

TEXT_FATAL_MARKERS = ('command not found', 'no such file or directory', 'permission denied',
                      'traceback (most recent call last)', 'failed to connect', 'connection refused',
                      'econnrefused', 'no such host', 'not found')


def categorize_failure(output):
    low = (output or '').lower()
    if re.search(r'blocked|approval|승인|실행 전 최신 업무 방법|not allowed|forbidden|denied|거부', low):
        return 'guard_or_approval'
    if 'timed out' in low or 'timeout' in low or 'deadline exceeded' in low:
        return 'timeout'
    if any(k in low for k in ('connection refused', 'econnrefused', 'no such host', 'unreachable', 'failed to connect')):
        return 'network'
    if 'command not found' in low:
        return 'command_not_found'
    if 'permission denied' in low or 'eacces' in low:
        return 'permission'
    if 'no such file' in low or 'no such directory' in low or 'enoent' in low or 'not found' in low:
        return 'not_found'
    if 'traceback' in low or 'error:' in low or 'exit status' in low:
        return 'explicit_error'
    return 'other'


STATE_SQL = """
SELECT tool_name,
       CASE WHEN json_valid(content) THEN json_type(content) END AS kind,
       CASE WHEN json_valid(content) AND json_type(content)='object' THEN coalesce(json_extract(content,'$.exit_code'), json_extract(content,'$.output.exit_code')) END AS exit_code,
       CASE WHEN json_valid(content) AND json_type(content)='object' THEN json_type(content,'$.ok') END AS ok,
       CASE WHEN json_valid(content) AND json_type(content)='object' THEN json_type(content,'$.success') END AS success,
       CASE WHEN json_valid(content) AND json_type(content)='object' AND json_type(content,'$.error') IN ('text','object','array','true')
            THEN substr(CAST(json_extract(content,'$.error') AS TEXT),1,300) END AS error,
       CASE WHEN json_valid(content) THEN NULL ELSE lower(substr(content,1,2000)) END AS text
FROM messages WHERE role='tool' AND tool_name IS NOT NULL AND timestamp > ?
ORDER BY id DESC LIMIT ?
"""
STATE_ROW_LIMIT = 20000


def classify(row):
    """Structured failure signal (upstream classify_result adapted to Hermes plugin JSON)."""
    if row['kind'] is not None:
        if row['exit_code'] is not None:
            return row['exit_code'] != 0, row['error'] or 'exit_code=%s' % row['exit_code']
        if row['ok'] == 'false' or row['success'] == 'false' or (row['error'] or '').strip():
            return True, row['error'] or 'ok=false'
        return False, ''
    text = row['text'] or ''
    for marker in TEXT_FATAL_MARKERS:
        if marker in text:
            return True, text[:300]
    return False, ''


def state_failures(diag, profile, since_epoch, min_calls=5):
    folder = diag.home(profile)
    path = folder / 'state.db'
    if not path.exists():
        return {'error': 'state_db_missing'}
    diag.safe(path, [folder], database=True)
    deadline = time.monotonic() + 4
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10000)
        rows = db.execute(STATE_SQL, (since_epoch, STATE_ROW_LIMIT)).fetchall()
    stats = defaultdict(lambda: {'total': 0, 'failures': 0})
    categories = defaultdict(Counter)
    samples = []
    for row in rows:
        failed, snippet = classify(row)
        tool = str(row['tool_name'])[:80]
        stats[tool]['total'] += 1
        if failed:
            stats[tool]['failures'] += 1
            category = categorize_failure(snippet)
            categories[category][tool] += 1
            if len(samples) < 5:
                samples.append({'tool': tool, 'category': category, 'error': diag.health_error_kind(snippet)[:160]})
    total = sum(s['total'] for s in stats.values())
    failures = sum(s['failures'] for s in stats.values())
    ranked = [{'tool': t, **s, 'failure_rate_pct': round(100 * s['failures'] / s['total'], 1)}
              for t, s in stats.items() if s['total'] >= min_calls and s['failures']]
    ranked.sort(key=lambda t: (-t['failures'] / t['total'], -t['failures']))
    return {'tool_calls': total, 'failures': failures,
            'overall_success_rate_pct': round(100 * (1 - failures / max(total, 1)), 1),
            'row_limit_reached': len(rows) >= STATE_ROW_LIMIT,
            'top_failing_tools': ranked[:8], 'min_calls_for_ranking': min_calls,
            'categories': [{'category': c, 'count': sum(t.values()), 'tools': dict(t.most_common(5))}
                           for c, t in sorted(categories.items(), key=lambda kv: -sum(kv[1].values()))],
            'failure_samples': samples,
            'note': 'Failure = exit_code!=0, ok/success false or top-level error in JSON results; narrow fatal markers for text results. guard_or_approval are policy refusals, not crashes.'}


def log_patterns(diag, args):
    profile = args.get('profile', 'default')
    if profile not in diag.PROFILES:
        raise ValueError('profile must be one of ' + ', '.join(diag.PROFILES))
    log = args.get('log', 'gateway')
    if log not in LOG_FILES:
        raise ValueError('log must be gateway, error, agent or gateway_error')
    hours = args.get('hours', 24)
    if type(hours) is not int or not 1 <= hours <= 168:
        raise ValueError('hours must be an integer from 1 to 168')
    folder = diag.home(profile) / 'logs'
    path = folder / LOG_FILES[log]
    if not path.exists():
        raise ValueError('No %s for this profile' % LOG_FILES[log])
    text, tail_only, size = read_tail(diag, path)
    now = datetime.now(diag.KST)
    since = now - timedelta(hours=hours)
    entries, physical = parse_entries(text, diag.KST)
    stamped = [e['at'] for e in entries if e['at']]
    window = [e for e in entries if e['at'] and since <= e['at'] <= now]
    result = {'profile': profile, 'log': LOG_FILES[log], 'hours': hours,
              'window_kst': [since.strftime('%Y-%m-%d %H:%M:%S'), now.strftime('%Y-%m-%d %H:%M:%S')],
              'file_bytes': size, 'tail_only': tail_only, 'bytes_read_cap': LOG_TAIL_BYTES,
              'lines_scanned': physical, 'entries_in_window': len(window),
              'undated_entries_skipped': sum(1 for e in entries if not e['at']),
              'oldest_entry_read_kst': min(stamped).strftime('%Y-%m-%d %H:%M:%S') if stamped else None,
              # False when the tail cap or log rotation left the start of the window unread.
              'window_fully_covered': bool(stamped and min(stamped) <= since),
              **analyze(diag, window, since, now)}
    try:
        result['tool_failure_rates'] = state_failures(diag, profile, since.timestamp())
    except (ValueError, OSError, sqlite3.Error) as exc:
        result['tool_failure_rates'] = {'error': type(exc).__name__}
    result['note'] = ('Patterns are leads, not root causes. Detectors use WARNING+ entries only; groups use '
                      'normalized first lines; samples are redacted. If window_fully_covered is false, the start '
                      'of the window was outside the tail cap or already rotated away.')
    return result
