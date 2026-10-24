"""Composite read-only deployment evidence and past/current incident comparison."""
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess


def deployment(diag, args):
    path = diag.safe(args.get('path', ''), diag.code_roots())
    if not path.is_file() or path.stat().st_size > diag.MAX_FILE:
        raise ValueError('A real bounded source file is required')
    history = diag.history_read({'path': str(path), 'limit': 3})
    paths = [path]
    relative = None
    # Compare only copies that the existing synchronizer manages; no arbitrary traversal.
    for plugin in ('team-kit', 'team-delegation'):
        base = diag.ROOT / 'plugins' / plugin
        if base in path.parents:
            relative = path.relative_to(base)
            paths.extend(p / relative for p in sorted((diag.ROOT / 'profiles').glob('*/plugins/' + plugin))
                         if not (plugin == 'team-delegation' and p.parts[-3] == 'system-doctor' and str(relative) == 'plugin.yaml'))
            break
    checks = []
    for p in paths:
        try:
            p = diag.safe(p, diag.code_roots())
            if p.stat().st_size > diag.MAX_FILE: raise ValueError('Oversized copy')
            checks.append({'path': str(p), 'sha256': hashlib.sha256(p.read_bytes()).hexdigest(),
                           'mtime': p.stat().st_mtime})
        except (OSError, ValueError): checks.append({'path': str(p), 'error': 'missing_or_unreadable_copy'})
    copies_equal = all('sha256' in x for x in checks) and len({x.get('sha256') for x in checks}) == 1
    state = json.loads(diag.read(diag.ROOT / 'gateway_state.json', [diag.ROOT]))
    pid = state.get('pid')
    process, started = {}, None
    if type(pid) is int and pid > 1:
        run = subprocess.run(['/bin/ps', '-p', str(pid), '-o', 'lstart=', '-o', 'command='],
                             capture_output=True, text=True, timeout=3, env={**os.environ, 'LC_ALL': 'C'})
        line = run.stdout.strip()
        fields = line.split(None, 5)
        if run.returncode == 0 and len(fields) == 6 and 'hermes' in fields[5] and 'gateway' in fields[5]:
            started = datetime.strptime(' '.join(fields[:5]), '%a %b %d %H:%M:%S %Y').astimezone(diag.KST)
            process = {'pid': pid, 'started_at': started.isoformat(), 'identity_verified': True}
    if not process: process = {'pid': pid, 'identity_verified': False}
    newer = all(started.timestamp() > x['mtime'] for x in checks if 'mtime' in x) if started else None
    logs = {}
    for name in ('gateway.log', 'gateway.error.log'):
        try:
            text, clipped = diag.health_tail(diag.ROOT / 'logs' / name)
            lines = []
            for line in text.splitlines():
                at = diag.health_stamp(line)
                if started and at and at >= started:
                    if name.endswith('error.log') or 'connected' in line:
                        lines.append(diag.redact(line)[:350])
            logs[name] = {'since_start': lines[-30:], 'matching_lines': len(lines), 'tail_only': clipped,
                          'start_known': started is not None, 'details_truncated': len(lines) > 30}
        except (ValueError, OSError): logs[name] = {'error': 'log_unavailable'}
    return {'source_history': history, 'copies': checks, 'copies_equal': copies_equal,
            'process': process, 'process_started_after_files': newer, 'logs': logs,
            'state_updated_at': state.get('updated_at'), 'state_platforms': state.get('platforms'),
            'status': 'copies_differ' if not copies_equal else 'process_unverified' if newer is None else 'restart_required' if not newer else 'files_and_process_consistent',
            'runtime_schema_verified': False,
            'note': 'File hashes and process age are evidence, not proof that a current session loaded the schema. Check reconnects and post-restart errors; no restart, test execution or changes were performed.'}


def recurrence(diag, args):
    identifier, query = args.get('record_id'), args.get('query')
    if not isinstance(identifier, str) or not identifier or not isinstance(query, str) or not 2 <= len(query) <= 200:
        raise ValueError('record_id and a literal error marker (2–200 characters) are required')
    found = [r for r in diag.records_rows() if r.get('id') == identifier]
    if len(found) != 1: raise ValueError('Prior diagnosis record not found or duplicated')
    previous = found[0]
    profile = args.get('profile') or previous.get('subject_profile')
    if profile not in diag.PROFILES: raise ValueError('An explicit current profile is required')
    if not args.get('start') or not args.get('end'):
        raise ValueError('Explicit current log start/end are required')
    current = diag.logs_read({k: v for k, v in args.items() if k in {'query', 'start', 'end'}} | {'profile': profile})
    evidence = '\n'.join(str(previous.get(k, '')) for k in ('full_result', 'short_verdict', 'expected', 'actual'))
    return {'previous': {k: previous.get(k) for k in ('id', 'recorded_at', 'subject_profile', 'short_verdict', 'full_result', 'commits', 'truncated')},
            'current': current, 'marker': query, 'marker_in_previous': query in evidence,
            'same_subject_profile': previous.get('subject_profile') == profile,
            'same_cause_confirmed': False,
            'note': 'Literal error recurrence evidence only. Matching text does not prove the same cause; inspect current code/state and commits before proposing another fix.'}
