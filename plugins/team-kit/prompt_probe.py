"""Sizes of each turn's first model request, for 진단담당's diagnosis_prompt_overhead_read.

The stored system prompt has no tool schemas, so the first call's input could only be
split into "system prompt" and an unexplained rest. This llm_request middleware records,
per turn's first call, how large the system prompt, each toolset's tool schemas and the
messages are. Only sizes, tool names and toolset names are written; never any text.
Tool names per toolset let 진단담당's tool diet count how often a toolset is used.
It never changes the request.
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path

logger = logging.getLogger(__name__)
FILE = 'logs/prompt_first_calls.jsonl'
KEEP = 300
MAX_BYTES = 1_000_000
CJK_RE = re.compile('[ᄀ-ᇿ぀-ヿ㄰-㆏㐀-䶿一-鿿가-힯豈-﫿]')


def tokens(text):
    """Same estimate as diagnosis_overhead.tokens (Hermes estimate_tokens_rough)."""
    if not text:
        return 0
    if text.isascii():
        return (len(text) + 3) // 4
    stripped = CJK_RE.sub('', text)
    return (len(text) - len(stripped)) + (len(stripped.encode('utf-8', 'replace')) + 3) // 4


def size(value):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return {'chars': len(text), 'tokens_est': tokens(text)}


def tool_name(tool):
    function = tool.get('function') if isinstance(tool.get('function'), dict) else tool
    return str(function.get('name') or '?')


def toolset_of(name):
    try:
        from tools.registry import registry
        return registry.get_toolset_for_tool(name) or 'unknown'
    except Exception:
        return 'unknown'


def measure(request):
    messages = request.get('messages') if isinstance(request.get('messages'), list) else []
    system = [m for m in messages if isinstance(m, dict) and m.get('role') in ('system', 'developer')]
    rest = [m for m in messages if isinstance(m, dict) and m.get('role') not in ('system', 'developer')]
    system_text = '\n'.join(m['content'] if isinstance(m.get('content'), str) else json.dumps(m.get('content'), ensure_ascii=False, default=str) for m in system)
    if isinstance(request.get('system'), (str, list)):
        system_text += json.dumps(request['system'], ensure_ascii=False, default=str) if isinstance(request['system'], list) else request['system']
    toolsets = {}
    for tool in request.get('tools') or []:
        if not isinstance(tool, dict):
            continue
        name = tool_name(tool)
        bucket = toolsets.setdefault(toolset_of(name), {'tools': 0, 'chars': 0, 'tokens_est': 0, 'largest': [], 'names': []})
        measured = size(tool)
        bucket['tools'] += 1
        bucket['chars'] += measured['chars']
        bucket['tokens_est'] += measured['tokens_est']
        bucket['largest'].append((measured['chars'], name))
        bucket['names'].append(name)  # tool diet: toolset usage = calls of these tools
    for bucket in toolsets.values():
        bucket['largest'] = [name for _, name in sorted(bucket['largest'], reverse=True)[:3]]
    tools_total = {'tools': sum(b['tools'] for b in toolsets.values()), 'chars': sum(b['chars'] for b in toolsets.values()),
                   'tokens_est': sum(b['tokens_est'] for b in toolsets.values())}
    return {'system_prompt': size(system_text), 'tools': tools_total,
            'toolsets': dict(sorted(toolsets.items(), key=lambda item: -item[1]['chars'])),
            'messages': {'count': len(rest), **size(rest)}}


def record(home, request, *, session_id='', api_call_count=None, platform='', **_):
    """llm_request middleware: append one size record per turn's first call; never modifies the request."""
    if api_call_count != 1 or not session_id or not isinstance(request, dict):
        return None
    try:
        row = {'ts': round(time.time(), 3), 'session_id': session_id, 'platform': platform, **measure(request)}
        path = Path(home) / FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(row, ensure_ascii=False) + '\n')
        if path.stat().st_size > MAX_BYTES:
            lines = path.read_text(encoding='utf-8').splitlines()
            tmp = path.with_suffix('.tmp')
            tmp.write_text('\n'.join(lines[-KEEP:]) + '\n', encoding='utf-8')
            tmp.replace(path)
    except Exception:
        logger.debug('team-kit prompt probe failed', exc_info=True)
    return None
