"""Portable linked-work text and per-task deadlines, without service access."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
from pathlib import Path
import re
import os
import tempfile
import unicodedata
import difflib

import deadline_time as dates

TASK = re.compile(r'^\s*(?:[-*]\s*)?(?:(\[[ xX]\])|([☐☑✅]))\s*(.+)$')


def normalized(value):
    return ''.join(c for c in unicodedata.normalize('NFKC', str(value)).casefold() if c.isalnum())


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def rich_text(block):
    return ''.join(t.get('plain_text', t.get('text', {}).get('content', ''))
                   for t in block.get('rich_text', []))


def task(text, *, done=False, identifier=None, now=None):
    parts = str(text).strip().split(' | ', 1)
    name = parts[0].strip()
    due_text = parts[1] if len(parts) > 1 else name
    try:
        due = dates.extract(due_text, now=now, required=False)
        ambiguous = False
    except ValueError:
        due, ambiguous = None, True
        if done:
            # A finished task often gains its completion date ('… | 10/7 08:30 완료 (10/26 완료 처리)').
            # It needs no reminder, so its first date is its deadline instead of an owner question.
            match = dates.DEADLINE.search(due_text)
            try:
                due = match and dates.extract(match.group(0), now=now)
                ambiguous = False
            except ValueError:
                due = None
    if len(parts) == 1 and due:
        name = dates.DEADLINE.sub('', name).strip(' :：,|까지') or parts[0].strip()
    return {'text': name, 'due': due, 'done': bool(done),
            **({'source_id': identifier} if identifier else {}), **({'ambiguous': True} if ambiguous else {})}


def semantic(content):
    return {'title': str(content.get('title') or '').strip(),
            'body': str(content.get('body') or '').strip(),
            'tasks': [{k: row.get(k) for k in ('text', 'due', 'done', 'ambiguous') if k in row}
                      for row in content.get('tasks', [])]}


def from_text(title, text, *, now=None, due=None):
    tasks, body = [], []
    for line in str(text or '').splitlines():
        match = TASK.match(line)
        if match:
            tasks.append(task(match[3], done=match[1] in {'[x]', '[X]'} or match[2] in {'☑', '✅'}, now=now))
        elif line.startswith('다음 마감:'):
            due = dates.extract(line, now=now, required=False)
        elif line not in {'[업무 본문]', '[할 일]'}:
            body.append(line)
    if len(tasks) == 1 and not tasks[0].get('due') and due:
        tasks[0]['due'] = dates.canonical(due)
    return semantic({'title': title, 'tasks': tasks, 'body': '\n'.join(body)})


FLAT_ROW = re.compile(r'^- (본문|할 일): ')
MARK_ROW = re.compile(r'^(\s*-\s(?:[^:\[\n]+:\s)?)\[([ xX])\](.*)$')


def structured_summary(summary):
    """A memo summary with sections or nested rows (not just '- 본문:'/'- 할 일:' rows written by the
    linked-work sync). Rewriting it as flat rows destroys its layout (CIIE, 2026-10-26)."""
    return any(line.strip() and not FLAT_ROW.match(line) for line in str(summary or '').splitlines())


def task_key(text):
    """Task identity across originals: dates and spacing differ by representation, not by meaning."""
    return re.sub(r'[\s()（）:：,·.]+', '', dates.DEADLINE.sub('', str(text or '')))


def structured_desired(own, wanted):
    """What a structured memo can take from another original: completion of the same task (matched by
    task_key) and the title. Its own text, dates, body and layout stay as they are."""
    done = {}
    for row in wanted.get('tasks', []):
        done.setdefault(task_key(row['text']), []).append(row['done'])
    tasks = []
    for row in own.get('tasks', []):
        options = done.get(task_key(row['text'])) or []
        tasks.append({**row, 'done': options[0]} if len(options) == 1 else dict(row))
    return {**own, 'title': wanted.get('title') or own.get('title'), 'tasks': tasks}


def flip_marks(summary, tasks):
    """Set the checkbox of each task row in document order; None if the rows and tasks do not align."""
    lines, rows = str(summary).splitlines(), []
    for index, line in enumerate(lines):
        if MARK_ROW.match(line):
            rows.append(index)
    if len(rows) != len(tasks):
        return None
    for index, row in zip(rows, tasks):
        head, _, tail = MARK_ROW.match(lines[index]).groups()
        lines[index] = head + ('[x]' if row['done'] else '[ ]') + tail
    return '\n'.join(lines)


def render(content):
    rows = ['[업무 본문]', content.get('body') or '', '[할 일]']
    for row in content.get('tasks', []):
        rows.append(('[x] ' if row['done'] else '[ ] ') + row['text'] +
                    (' | ' + row['due'] if row.get('due') else ''))
    return '\n'.join(rows)


def next_due(content):
    return min((r['due'] for r in content.get('tasks', []) if r.get('due') and not r.get('done')), default=None)


def reconcile(previous, incoming, *, reserved=()):
    """Keep task identity across edits; an unmatched replacement keeps its old positional identity."""
    out = [dict(row) for row in incoming]
    by_id = {r['source_id']: r['key'] for r in previous if r.get('source_id') and r.get('key')}
    for row in out:
        if row.get('source_id') in by_id:
            row['key'] = by_id[row['source_id']]
    assigned = {row['key'] for row in out if row.get('key')}
    old_names = [normalized(row['text']) for row in previous]
    new_names = [normalized(row['text']) for row in out]
    matcher = difflib.SequenceMatcher(a=old_names, b=new_names, autojunk=False)
    for opcode, i, j, k, end in matcher.get_opcodes():
        if opcode == 'equal' or (opcode == 'replace' and j-i == end-k):
            for old, new in zip(previous[i:j], out[k:end]):
                if old.get('key') and old['key'] not in assigned and not new.get('key'):
                    new['key'] = old['key']
                    assigned.add(old['key'])
    used = {r.get('key') for r in previous} | set(reserved) | assigned
    for index, row in enumerate(out):
        if 'key' not in row:
            key = digest([row['text'], index, sorted(k for k in used if k)])[:24]
            row['key'] = key
            used.add(key)
    return out


def save(path, data):
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError('sync_state_symlink')
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.work-sync-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(data, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextmanager
def ledger(home, name):
    folder = Path(home)/'state'
    path, lock = folder/(name+'.json'), folder/(name+'.lock')
    if any(p.is_symlink() for p in (folder, path, lock)):
        raise ValueError('sync_state_symlink')
    folder.mkdir(parents=True, exist_ok=True)
    with lock.open('a') as stream:
        os.chmod(lock, 0o600)
        fcntl.flock(stream, fcntl.LOCK_EX)
        state = json.loads(path.read_text()) if path.exists() else {'version': 1, 'works': {}}
        if state.get('version') != 1 or not isinstance(state.get('works'), dict):
            raise ValueError('sync_state_corrupt')
        yield path, state
