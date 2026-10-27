"""Linked originals that work-assistant's own tools just wrote (2026-10-27 diag-1ea80e2f0831).

An approved request can edit the linked memo and the Notion page separately. The 10-minute linked-work
sync would then see two originals changed differently and stop as an owner conflict. The post_tool_call
hook records which originals a tool wrote; the sync adopts such tool-only edits as the new baseline
instead of reporting a conflict. Nothing is written to any original here.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import work_content as content

NAME = 'work-tool-writes'
KEEP = 7 * 86400
NOTION_WRITES = {'update', 'append', 'edit', 'trash'}
NOTES_WRITES = {'update'}


def key(kind, identifier):
    identifier = str(identifier or '').strip()
    if kind == 'notion':
        found = re.findall(r'[0-9a-f]{32}', identifier.replace('-', '').lower())
        identifier = found[-1] if found else identifier.lower()
    return f'{kind}|{identifier}'


def _parse(result):
    if isinstance(result, dict):
        return result
    try:
        data = json.loads(result)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _done(row):
    # A write that may have happened (uncertain) counts too: only the originals' readback decides content.
    return isinstance(row, dict) and (row.get('ok') is True or row.get('status') in {'done', 'unknown', 'uncertain'})


def targets(tool_name, args, result):
    args, data = args if isinstance(args, dict) else {}, _parse(result)
    if not data or (data.get('error') and not data.get('operations')):
        return set()
    if tool_name == 'notion':
        ops = args.get('operations') if args.get('action') == 'batch' else [args]
        wrote = data.get('ok') is True or any(_done(op) for op in data.get('operations') or [])
        return {key('notion', op['page_id']) for op in ops or [] if wrote and isinstance(op, dict)
                and op.get('action') in NOTION_WRITES and op.get('page_id')}
    if tool_name == 'apple_notes_direct_mutation':
        return ({key('notes', args['note_id'])} if data.get('ok') is True and args.get('action') in NOTES_WRITES
                and args.get('note_id') else set())
    if tool_name == 'personal_work_bundle':
        found = set()
        for op in data.get('operations') or []:
            receipt = op.get('receipt') if isinstance(op, dict) else None
            if op.get('domain') == 'notes' and op.get('action') in NOTES_WRITES and (_done(op) or _done(receipt)):
                target = (receipt or {}).get('target_id') or ((receipt or {}).get('result') or {}).get('note_id')
                if target:
                    found.add(key('notes', target))
        return found
    return set()


def observe(home, *, tool_name='', args=None, result=None, **_):
    """post_tool_call hook; never raises into the tool turn."""
    try:
        found = targets(tool_name, args, result)
        if not found:
            return
        now = time.time()
        with content.ledger(home, NAME) as (path, state):
            marks = state['works']
            for name in [name for name, at in marks.items() if not isinstance(at, (int, float)) or now - at > KEEP]:
                marks.pop(name)
            marks.update({name: now for name in found})
            content.save(path, state)
    except Exception:
        pass


def marked(home, keys):
    """Ledger source keys ('kind|id') that a tool wrote since they were last cleared."""
    home = Path(home)
    path = home / 'state' / (NAME + '.json')
    if not path.exists():
        return set()
    with content.ledger(home, NAME) as (_, state):
        return {name for name in keys if key(*name.split('|', 1)) in state['works']}


def clear(home, keys, before):
    """Drop marks the given run already read; marks written after the run started stay."""
    home = Path(home)
    path = home / 'state' / (NAME + '.json')
    if not path.exists():
        return
    with content.ledger(home, NAME) as (path, state):
        names = {key(*name.split('|', 1)) for name in keys}
        stale = [name for name in names if state['works'].get(name, before + 1) <= before]
        if stale:
            for name in stale:
                state['works'].pop(name)
            content.save(path, state)
