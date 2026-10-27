"""Read-only measurement of per-turn system prompt overhead for system-doctor diagnosis.

Ported from moonlight-lupin/agent-skills `agent-ops/input-token-overheads`
(SKILL.md overhead map and health ratio, scripts/audit_overheads.py injected-file checks)
https://github.com/moonlight-lupin/agent-skills/tree/ae671a22253ed3dfe81a96d8405aecd3fc9baec6/agent-ops/input-token-overheads
commit ae671a22253ed3dfe81a96d8405aecd3fc9baec6

MIT License, Copyright (c) 2025-2026 moonlight-lupin. Permission is hereby granted,
free of charge, to any person obtaining a copy of this software and associated
documentation files, to deal in the Software without restriction, subject to the
condition that the above copyright notice and this permission notice shall be
included in all copies or substantial portions of the Software. THE SOFTWARE IS
PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND.

Local adaptation: instead of upstream estimates (top-K description averages), this
measures the actual system prompt Hermes stored for recent sessions (sessions /
system_prompts in state.db), splits it at Hermes' own markers, and reports only
sizes and section titles. The prompt text, SOUL, memory and USER contents are never
returned. Token counts are estimates (Hermes estimate_tokens_rough method).
"""
from __future__ import annotations

from datetime import datetime
import json
import re
import sqlite3
import time

SOURCE_RE = re.compile(r'[a-z_]{1,20}')
BUILTIN_START = 'You run on Hermes Agent'
BANNER_RE = re.compile(r'^═{10,}$')
PLUGIN_START, PLUGIN_END = '<!-- hermes-plugin-sections:start -->', '<!-- hermes-plugin-sections:end -->'
PLUGIN_HEAD = re.compile(r'^## Plugin Context: (.+)$')
PLUGIN_CHARS = re.compile(r'^<!-- hermes-plugin-section-chars:(\d+) -->$')
RUNTIME_START, RUNTIME_END = '# Hermes runtime environment', '<!-- End Hermes runtime environment -->'
BUILTIN_LINES = (('You have persistent memory', 'memory guidance'), ('You are in a ', 'platform hint'),
                 ('You are on ', 'platform hint'), ('You are communicating', 'platform hint'))
CJK_RE = re.compile('[ᄀ-ᇿ぀-ヿ㄰-㆏㐀-䶿一-鿿가-힯豈-﫿]')
ASSUMED_CONTEXT = 200000
FIRST_CALL_RE = r'\[{sid}\] agent\.conversation_loop: API call #1: .*?\bin=(\d+)'


def tokens(text):
    """Same method as hermes agent.model_metadata.estimate_tokens_rough (estimate, not a tokenizer)."""
    if not text:
        return 0
    if text.isascii():
        return (len(text) + 3) // 4
    stripped = CJK_RE.sub('', text)
    return (len(text) - len(stripped)) + (len(stripped.encode('utf-8', 'replace')) + 3) // 4


def title(line):
    return re.sub(r'\s+', ' ', line.strip().lstrip('#').strip())[:80]


def split_sections(prompt):
    """Return ordered (kind, title, text, declared_chars) blocks split at Hermes markers."""
    lines = prompt.split('\n')
    marks = []  # (line index, kind, title)
    builtin_seen = False
    state = 'soul'
    owned_head = None  # Plugin heading already attributed to the block opened at the start marker.
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if state == 'plugins':
            head = PLUGIN_HEAD.match(stripped)
            if head and i != owned_head:
                marks.append((i, 'plugin', head[1]))
            elif stripped == PLUGIN_END:
                state = 'after'
                if i + 1 < len(lines):
                    marks.append((i + 1, 'other', 'unlabelled'))
            i += 1
            continue
        if state == 'runtime':
            if stripped == RUNTIME_END:
                state = 'after'
                if i + 1 < len(lines):
                    marks.append((i + 1, 'other', 'unlabelled'))
            i += 1
            continue
        if stripped == PLUGIN_START:
            j = next((k for k in range(i + 1, len(lines)) if lines[k].strip()), None)
            head = PLUGIN_HEAD.match(lines[j].strip()) if j is not None else None
            marks.append((i, 'plugin', head[1] if head else 'plugin sections (unlabelled)'))
            owned_head = j if head else None
            state = 'plugins'
            i += 1
            continue
        if stripped == RUNTIME_START:
            marks.append((i, 'runtime', 'Hermes runtime environment'))
            state = 'runtime'
            i += 1
            continue
        if BANNER_RE.match(stripped) and i + 2 < len(lines) and BANNER_RE.match(lines[i + 2].strip()):
            name = lines[i + 1].strip()
            kind = 'memory' if name.startswith('MEMORY') else 'user_profile' if name.startswith('USER PROFILE') else 'injected'
            marks.append((i, kind, name))
            state = 'injected'
            i += 3
            continue
        if line.startswith('Active Hermes profile:'):
            marks.append((i, 'session_metadata', 'profile/date/model/platform lines'))
            state = 'after'
            i += 1
            continue
        if line.startswith(BUILTIN_START) and state == 'soul':
            builtin_seen = True
            marks.append((i, 'builtin', 'Hermes agent intro'))
            state = 'builtin'
            i += 1
            continue
        if state == 'soul':
            if line.startswith('## Skills'):
                marks.append((i, 'skills_index', 'Skills index'))
                state = 'skills'
            elif i == 0 or line.startswith('## '):
                marks.append((i, 'soul', title(line) or 'SOUL preamble'))
        elif state in {'builtin', 'after'}:
            if line.startswith('## Skills'):
                marks.append((i, 'skills_index', 'Skills index'))
                state = 'skills'
            elif line.startswith(('# ', '## ')):
                marks.append((i, 'builtin', title(line)))
                state = 'builtin'
            else:
                for prefix, name in BUILTIN_LINES:
                    if line.startswith(prefix):
                        marks.append((i, 'builtin', name))
                        state = 'builtin'
                        break
        elif state == 'skills':
            if stripped == '</available_skills>':
                # Keep the one-line instruction after the index with the index block.
                state = 'skills_tail'
        elif state == 'skills_tail' and stripped and not stripped.startswith('Only proceed'):
            state = 'after'
            continue  # Re-evaluate this line as a potential marker.
        elif state == 'injected' and stripped and (line.startswith(('# ', '## ')) and not line.startswith('## Plugin')):
            marks.append((i, 'builtin', title(line)))
            state = 'builtin'
        i += 1
    if not marks or marks[0][0] != 0:
        marks.insert(0, (0, 'soul' if builtin_seen else 'other', 'prompt start'))
    blocks = []
    for index, (start, kind, name) in enumerate(marks):
        end = marks[index + 1][0] if index + 1 < len(marks) else len(lines)
        text = '\n'.join(lines[start:end])
        if not text.strip():
            continue
        declared = next((int(m[1]) for m in map(PLUGIN_CHARS.match, (l.strip() for l in lines[start:end])) if m), None)
        blocks.append((kind, name[:80], text, declared))
    return blocks


def summarize(prompt, top):
    blocks = split_sections(prompt)
    total_chars = len(prompt)
    total_tokens = tokens(prompt)
    rows, kinds = [], {}
    for kind, name, text, declared in blocks:
        chars, est = len(text), tokens(text)
        bucket = kinds.setdefault(kind, {'kind': kind, 'blocks': 0, 'chars': 0, 'tokens_est': 0})
        bucket['blocks'] += 1; bucket['chars'] += chars; bucket['tokens_est'] += est
        row = {'kind': kind, 'title': name, 'chars': chars, 'tokens_est': est,
               'share_pct': round(100 * chars / max(total_chars, 1), 1)}
        if declared is not None:
            row['declared_plugin_chars'] = declared
        rows.append(row)
    rows.sort(key=lambda r: -r['chars'])
    for bucket in kinds.values():
        bucket['share_pct'] = round(100 * bucket['chars'] / max(total_chars, 1), 1)
    skills = re.search(r'<available_skills>\n(.*?)\n</available_skills>', prompt, re.S)
    index = skills[1].split('\n') if skills else []
    return {'chars': total_chars, 'bytes': len(prompt.encode('utf-8')), 'tokens_est': total_tokens,
            'non_ascii_chars': sum(1 for c in prompt if not c.isascii()),
            'by_kind': sorted(kinds.values(), key=lambda b: -b['chars']),
            'sections': rows[:top], 'sections_total': len(rows), 'sections_truncated': len(rows) > top,
            'skills_index': {'enabled_skills': sum(1 for l in index if re.match(r'^\s{4}- ', l)),
                             'categories': sum(1 for l in index if re.match(r'^\s{2}\S', l)),
                             'chars': len(skills[0]) if skills else 0}}


def file_size(diag, path, base):
    """Sizes only. A shared USER.md symlink is reported by its approved target, never followed silently."""
    info = {'exists': path.exists() or path.is_symlink()}
    if not info['exists']:
        return info
    target = path
    if path.is_symlink():
        target = path.resolve()
        info['symlink_to_shared'] = target == diag.ROOT / 'memories' / target.name
        if not info['symlink_to_shared']:
            info['error'] = 'unapproved_symlink'
            return info
    try:
        text = diag.read(target, [base, diag.ROOT / 'memories'])
    except (ValueError, OSError) as exc:
        info['error'] = type(exc).__name__
        return info
    info.update(bytes=len(text.encode('utf-8')), chars=len(text), tokens_est=tokens(text),
                entries=text.count('\n§\n') + 1 if text.strip() else 0)
    return info


def skills_on_disk(diag, folder):
    root = folder / 'skills'
    count = 0
    if root.is_dir():
        diag.safe(root, [root])
        for path in root.rglob('SKILL.md'):
            if any(part.startswith('.') for part in path.relative_to(root).parts):
                continue
            count += 1
            if count >= 2000:
                break
    disabled = None
    try:
        import hermes_yaml as yaml
        cfg = yaml.safe_load(diag.read(folder / 'config.yaml', [folder])) or {}
        disabled = len(((cfg.get('skills') or {}).get('disabled') or []))
    except Exception:
        pass
    return {'skill_md_files': count, 'disabled_in_config': disabled}


def first_call_inputs(diag, folder, session_ids):
    """Measured provider input tokens of API call #1 per session (system prompt + tool schemas + first message)."""
    path = folder / 'logs' / 'agent.log'
    if not path.exists() or not session_ids:
        return {}
    try:
        text, _ = diag.health_tail(path)
    except (ValueError, OSError):
        return {}
    found = {}
    for sid in session_ids:
        match = re.search(FIRST_CALL_RE.format(sid=re.escape(sid)), text)
        if match:
            found[sid] = int(match[1])
    return found


def first_call_breakdown(diag, folder, session_id):
    """Sizes team-kit prompt_probe recorded for the session's first request (system prompt, tool schemas by toolset, messages)."""
    path = folder / 'logs' / 'prompt_first_calls.jsonl'
    if not path.exists():
        return None
    try:
        lines = diag.read(path, [folder]).splitlines()
    except (ValueError, OSError):
        return None
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get('session_id') == session_id:
            return row
    return None


def health(ratio_pct):
    return 'excellent' if ratio_pct < 5 else 'healthy' if ratio_pct < 15 else 'acceptable' if ratio_pct < 25 else 'unhealthy'


def profile_overhead(diag, profile, source, limit, top):
    folder = diag.home(profile)
    path = folder / 'state.db'
    result = {'profile': profile}
    if not path.exists():
        result['error'] = 'state_db_missing'
        return result
    diag.safe(path, [folder], database=True)
    deadline = time.monotonic() + 4
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10000)
        has_table = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='system_prompts'").fetchone()
        prompt_sql = "coalesce(s.system_prompt, p.prompt)" if has_table else 's.system_prompt'
        join = 'LEFT JOIN system_prompts p ON p.hash=s.system_prompt_hash' if has_table else ''
        newest = db.execute(f"SELECT s.id, s.started_at, s.model, {prompt_sql} AS prompt FROM sessions s {join} "
                            f"WHERE s.source=? AND s.parent_session_id IS NULL AND {prompt_sql} IS NOT NULL "
                            "ORDER BY s.started_at DESC LIMIT 1", (source,)).fetchone()
        recent = db.execute("SELECT id, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, api_call_count, "
                            "message_count FROM sessions WHERE source=? AND parent_session_id IS NULL AND api_call_count>0 "
                            "ORDER BY started_at DESC LIMIT ?", (source, limit)).fetchall()
        distinct = db.execute(f"SELECT count(DISTINCT s.system_prompt_hash) FROM (SELECT system_prompt_hash FROM sessions "
                              "WHERE source=? AND parent_session_id IS NULL ORDER BY started_at DESC LIMIT ?) s", (source, limit)).fetchone()[0]
    if newest is None:
        result['error'] = 'no_session_with_stored_system_prompt'
    else:
        result['newest_session'] = {'id': newest['id'], 'model': newest['model'],
                                    'started_at_kst': datetime.fromtimestamp(newest['started_at'], diag.KST).strftime('%Y-%m-%d %H:%M:%S')}
        result['system_prompt'] = summarize(newest['prompt'], top)
    measured = first_call_inputs(diag, folder, [newest['id']] if newest else [])
    if newest is not None and newest['id'] in measured:
        first = measured[newest['id']]
        prompt_est = result['system_prompt']['tokens_est']
        result['first_api_call'] = {'input_tokens_measured': first, 'system_prompt_tokens_est': prompt_est,
                                    'rest_tokens_est': max(first - prompt_est, 0),
                                    'ratio_of_assumed_context_pct': round(100 * first / ASSUMED_CONTEXT, 1),
                                    'health': health(100 * first / ASSUMED_CONTEXT),
                                    'assumed_context_tokens': ASSUMED_CONTEXT}
        probe = first_call_breakdown(diag, folder, newest['id'])
        if probe is None:
            result['first_api_call']['breakdown'] = None
        else:
            parts = probe['system_prompt']['tokens_est'] + probe['tools']['tokens_est'] + probe['messages']['tokens_est']
            result['first_api_call']['breakdown'] = {
                'system_prompt_tokens_est': probe['system_prompt']['tokens_est'],
                'tool_schemas': {'tools': probe['tools']['tools'], 'tokens_est': probe['tools']['tokens_est'],
                                 'share_of_measured_pct': round(100 * probe['tools']['tokens_est'] / first, 1) if first else None},
                'tool_schemas_by_toolset': [{'toolset': name, 'tools': b['tools'], 'tokens_est': b['tokens_est'],
                                             'largest_tools': b.get('largest', [])}
                                            for name, b in list(probe['toolsets'].items())[:top]],
                'toolsets_total': len(probe['toolsets']),
                'messages': {'count': probe['messages']['count'], 'tokens_est': probe['messages']['tokens_est']},
                'unattributed_tokens_est': first - parts}
    count = len(recent)
    def avg(key):
        return round(sum(r[key] or 0 for r in recent) / count) if count else None
    calls = sum(r['api_call_count'] or 0 for r in recent)
    total_input = sum((r['input_tokens'] or 0) + (r['cache_read_tokens'] or 0) + (r['cache_write_tokens'] or 0) for r in recent)
    result['recent_sessions'] = {'sessions': count, 'source': source, 'distinct_system_prompts': distinct,
                                 'avg_input_tokens_uncached': avg('input_tokens'), 'avg_cache_read_tokens': avg('cache_read_tokens'),
                                 'avg_cache_write_tokens': avg('cache_write_tokens'), 'avg_output_tokens': avg('output_tokens'),
                                 'avg_api_calls': round(calls / count, 1) if count else None,
                                 'avg_total_input_per_api_call': round(total_input / calls) if calls else None,
                                 'cache_read_share_pct': round(100 * sum(r['cache_read_tokens'] or 0 for r in recent) / total_input, 1) if total_input else None}
    result['files'] = {'SOUL.md': file_size(diag, folder / 'SOUL.md', folder),
                       'memories/MEMORY.md': file_size(diag, folder / 'memories' / 'MEMORY.md', folder),
                       'memories/USER.md': file_size(diag, folder / 'memories' / 'USER.md', folder)}
    result['skills'] = skills_on_disk(diag, folder)
    return result


def prompt_overhead(diag, args):
    profile = args.get('profile', 'all')
    if profile != 'all' and profile not in diag.PROFILES:
        raise ValueError('profile must be all or one of ' + ', '.join(diag.PROFILES))
    source = args.get('source', 'slack')
    if not isinstance(source, str) or not SOURCE_RE.fullmatch(source):
        raise ValueError('source must be a session source such as slack, cron or cli')
    limit = args.get('limit', 10)
    if type(limit) is not int or not 1 <= limit <= 50:
        raise ValueError('limit must be an integer from 1 to 50')
    profiles = diag.PROFILES if profile == 'all' else (profile,)
    top = 5 if profile == 'all' else 30
    rows = []
    for name in profiles:
        try:
            rows.append(profile_overhead(diag, name, source, limit, top))
        except (ValueError, OSError, sqlite3.Error) as exc:
            rows.append({'profile': name, 'error': type(exc).__name__})
    return {'profiles': rows,
            'note': ('Sizes and section titles of the stored system prompt of the newest session only; the prompt '
                     'text is never returned. Stored prompt reflects files at that session start, which may differ '
                     'from current files. tokens_est uses Hermes estimate_tokens_rough (Hangul/CJK about 1 token '
                     'per character, other text UTF-8 bytes/4) and is not a tokenizer count; input_tokens_measured '
                     'comes from the provider usage line in agent.log (API call #1); rest_tokens_est = measured minus prompt '
                     'estimate = tool schemas + first user message + request overhead. Tool schemas are not in the '
                     'stored prompt; breakdown splits that first request into system prompt, tool schemas by toolset and messages '
                     'from team-kit prompt_probe size records (logs/prompt_first_calls.jsonl, sizes and tool names only; null '
                     'when the session predates the probe). unattributed_tokens_est = measured minus the three estimates '
                     '(estimate error and request framing). Health uses an assumed 200k context window (upstream default).')}
