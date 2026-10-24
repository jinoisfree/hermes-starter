"""Manual, delivery-free diagnosis regression checks; private cases stay in shared/."""
from __future__ import annotations
import argparse
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
from datetime import datetime

import diagnosis_requests as R

ROOT = Path.home() / '.hermes'


def folder():
    p = ROOT / 'shared/diagnosis-cases'
    if p.resolve() != p.absolute():
        raise ValueError('Case folder cannot follow symlinks')
    return p


def diagnostic():
    spec = importlib.util.spec_from_file_location('diagnosis_check_reads', ROOT / 'plugins/team-delegation/diagnosis.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def matches(pattern, text):
    if isinstance(pattern, str): return pattern in text
    if isinstance(pattern, dict) and set(pattern) == {'regex'}:
        return re.search(pattern['regex'], text) is not None
    raise ValueError('Patterns must be literal strings or {regex: expression}')


def judge(case, result):
    exp = case['expect']; answer = result.get('answer') or ''; failures = []
    silent = result.get('status') == 'normal' and answer == '없음'
    if exp['outcome'] == 'silent':
        if not silent: failures.append('expected_silent')
    elif exp['outcome'] == 'answer':
        if result.get('status') != 'no_post' or silent: failures.append('expected_answer')
        short, full, reason = R.split_answer(answer)
        if reason: failures.append('split:' + reason)
        if len(short) > exp.get('verdict_max_chars', 300): failures.append('verdict_chars')
        sentences = [s for s in re.split(r'[.!?。！？]+(?:\s+|$)|\n+', short) if s.strip()]
        if len(sentences) > exp.get('verdict_max_sentences', 2): failures.append('verdict_sentences')
        if re.search(r'https?://|www\.', short): failures.append('verdict_link')
    else: raise ValueError('Unknown expected outcome')
    _, coaching, reason = R.split_coaching(answer)
    if exp.get('coaching') == 'absent' and R.COACHING_SEPARATOR in answer:
        failures.append('coaching_unexpected')
    if exp.get('coaching') == 'required':
        if not coaching or reason: failures.append('coaching_missing_or_invalid:' + reason)
        customers = set(re.findall(r'`([A-Za-z0-9_.]{2,40})`', answer))
        customers.update(re.findall(r'고객\s+([A-Za-z0-9_.]{2,40})', json.dumps(case.get('request',{}), ensure_ascii=False)))
        customers.update(re.findall(r'(?<![A-Za-z0-9_])(?=[A-Za-z0-9_.]*_)[A-Za-z0-9_][A-Za-z0-9_.]{1,39}', ' '.join(str(case.get('request',{}).get(k,'')) for k in ('expected','actual'))))
        if any(customer in coaching for customer in customers): failures.append('coaching_customer')
        if re.search(r'\d{10}\.\d{6}|(?<!\w)[a-fA-F0-9]{7,40}(?!\w)|\.py\b|\d{1,2}:\d{2}|\d{4}-\d{2}-\d{2}', coaching):
            failures.append('coaching_specific_detail')
    for pattern in exp.get('must_include', []):
        if not matches(pattern, answer): failures.append('missing:' + json.dumps(pattern, ensure_ascii=False))
    for pattern in exp.get('must_not_include', []):
        if matches(pattern, answer): failures.append('forbidden:' + json.dumps(pattern, ensure_ascii=False))
    return failures


def prerequisites(case, d):
    missing = []
    for index, item in enumerate(case['expect'].get('requires', [])):
        try:
            if item['type'] == 'slack':
                channel, ts = d.permalink(channel=item['channel'], ts=item['ts'])
                data = d.slack_api('conversations.replies', {'channel': channel, 'ts': ts, 'limit': 1})
                if not data.get('messages'): raise ValueError('Message missing')
            elif item['type'] == 'state':
                home = d.home(item['profile']); roots = [home / 'state', home / 'learning']
                relative = Path(item['file'])
                if relative.is_absolute() or '..' in relative.parts or relative.suffix != '.json': raise ValueError('Invalid state requirement')
                data = json.loads(d.read(home / relative, roots))
                for key in item['path']: data = data[key]
                if data != item['value']: raise ValueError('Value no longer matches')
            elif item['type'] == 'commit':
                sha = item['hash']
                if not re.fullmatch(r'[a-fA-F0-9]{7,40}', sha): raise ValueError('Invalid hash')
                d.git_read(d.SETTINGS_REPO, ['show', '--no-ext-diff', '--no-textconv', '--no-patch', '--format=%H', sha, '--'])
            else: raise ValueError('Unknown requirement')
        except Exception as exc:
            missing.append({'requirement': index, 'reason': type(exc).__name__})
    return missing


def telemetry(identifier, started):
    p = ROOT / 'profiles/system-doctor/state.db'
    if not p.exists(): return {'session_id': None, 'tool_calls': None}
    with sqlite3.connect(p.as_uri() + '?mode=ro', uri=True, timeout=2) as db:
        db.execute('PRAGMA query_only=ON')
        row = db.execute("SELECT session_id FROM messages WHERE role='user' AND timestamp>=? AND instr(content,?)>0 ORDER BY id DESC LIMIT 1", (started, identifier)).fetchone()
        if not row: return {'session_id': None, 'tool_calls': None}
        count = sum(len(json.loads(c or '[]')) for (c,) in db.execute('SELECT tool_calls FROM messages WHERE session_id=?', (row[0],)))
        return {'session_id': row[0], 'tool_calls': count}


def record(summary):
    folder().mkdir(parents=True, exist_ok=True, mode=0o700)
    p = folder() / 'results.jsonl'
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'a') as f:
        f.write(json.dumps(summary, ensure_ascii=False) + '\n')


def check(case):
    requester_profile = case['request'].get('requester_profile', 'sns-manager')
    if not isinstance(requester_profile, str) or requester_profile not in R.REQUESTERS:
        raise ValueError('Unknown requester profile')
    d = diagnostic(); started = time.time()
    summary = {'time': datetime.now().astimezone().isoformat(), 'case': case['name'],
               'session_id': None, 'tool_calls': 0, 'elapsed_seconds': 0}
    try: summary['base_commit'] = d.git_read(d.SETTINGS_REPO, ['log', '-1', '--format=%H', '--']).decode().strip()
    except Exception: summary['base_commit'] = None
    missing = prerequisites(case, d)
    if missing:
        summary.update(verdict='unavailable', missing=missing); record(summary); return summary
    identifier = secrets.token_hex(16)
    r = {**case['request'], 'id': identifier, 'requester_profile': requester_profile,
         'created_at': started, 'status': 'pending', 'validation_only': True}
    check_dir = R.folder() / '.checks'
    check_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if check_dir.resolve() != check_dir.absolute(): raise ValueError('Check folder cannot follow symlinks')
    path = check_dir / (identifier + '.json')
    prompt = R.prompt
    result = {}
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as f: json.dump(r, f, ensure_ascii=False)
        R.prompt = lambda request: prompt(request) + '\n검증 실행 식별자(조사 자료): ' + identifier
        # Both the caller and validation_only enforce the existing no-post branch.
        with contextlib.redirect_stdout(io.StringIO()): result = R.run(identifier, no_post=True)
        summary.update(telemetry(identifier, started))
        failures = judge(case, result)
        summary.update(verdict='fail' if failures else 'pass', failures=failures)
    except Exception as exc:
        summary.update(verdict='fail', failures=['execution:' + type(exc).__name__])
    finally:
        R.prompt = prompt
        path.unlink(missing_ok=True)
        path.with_suffix('.tmp').unlink(missing_ok=True)
    summary['elapsed_seconds'] = time.time() - started
    record(summary)
    return {**summary, 'answer': result.get('answer'), 'short_verdict': result.get('short_verdict'),
            'full_result': result.get('full_result'), 'coaching':result.get('coaching',''), 'coaching_skipped':result.get('coaching_skipped'), 'runner_status': result.get('status')}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument('case'); args = parser.parse_args(argv)
    if args.case != 'all' and not re.fullmatch(r'[a-zA-Z0-9_-]+', args.case): raise ValueError('Invalid case name')
    paths = sorted(folder().glob('*.json')) if args.case == 'all' else [folder() / (args.case + '.json')]
    failed = False
    for path in paths:
        if path.is_symlink() or path.stat().st_size > 64000: raise ValueError('Invalid case file')
        result = check(json.loads(path.read_text()))
        print(json.dumps(result, ensure_ascii=False), flush=True)
        failed |= result['verdict'] == 'fail'
    return int(failed)


if __name__ == '__main__':
    raise SystemExit(main())
