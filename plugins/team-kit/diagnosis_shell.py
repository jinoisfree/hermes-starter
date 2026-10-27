"""Bounded local read pipelines, under a mandatory macOS sandbox."""
import importlib.util
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import signal
import subprocess
import time

ROOT = Path.home() / '.hermes'
REPO = (Path.home() / 'workspace/Hermes-desktop-pet').resolve()
COMMANDS = {'grep', 'rg', 'sed', 'awk', 'head', 'tail', 'wc', 'sort', 'uniq', 'cut', 'ls', 'find', 'stat', 'cat', 'jq', 'git'}
TIMEOUT = 30
MAX_OUTPUT = 20000


def executable(name):
    candidates = [Path('/opt/homebrew/bin') / name, Path('/usr/bin') / name, Path('/bin') / name]
    if name == 'git':
        candidates.insert(0, Path('/Library/Developer/CommandLineTools/usr/bin/git'))
    if name == 'rg':
        candidates += sorted(Path('/opt/homebrew/Caskroom/codex').glob('*/codex-path/rg'), reverse=True)
    for path in candidates:
        if path.is_file():
            return str(path.resolve())
    raise ValueError('Read command is unavailable: ' + name)


def pipeline(command):
    if not isinstance(command, str) or not command.strip() or len(command) > 6000:
        raise ValueError('A bounded read command is required')
    # Deliberately reject these even inside quotes: there is no shell expansion.
    if any(c in command for c in ('>', '<', ';', '&', '`', '\n', '\r', '\x00')):
        raise ValueError('Shell expansion, redirection and command chaining are forbidden')
    if '$(' in command or '${' in command:
        raise ValueError('Command substitution is forbidden')
    lexer = shlex.shlex(command, posix=True, punctuation_chars='|')
    lexer.whitespace_split = True
    lexer.commenters = ''
    parts, stage = [], []
    for token in lexer:
        if token == '|':
            if not stage:
                raise ValueError('Empty pipeline stage')
            parts.append(stage)
            stage = []
        elif token == '||':
            raise ValueError('Conditional execution is forbidden')
        else:
            stage.append(token)
    if not stage or len(parts) > 7:
        raise ValueError('Invalid pipeline')
    parts.append(stage)
    for args in parts:
        name = args[0]
        if name not in COMMANDS:
            raise ValueError('Command is not a permitted read')
        if name == 'sed' and (len(args) < 3 or args[1] != '-n' or not re.fullmatch(r'\d+(?:,\d+)?p', args[2])):
            raise ValueError('sed permits only -n START[,END]p')
        if name == 'find' and any(a.startswith(('-exec', '-ok', '-delete', '-fprint', '-fls')) for a in args[1:]):
            raise ValueError('find mutation/execute actions are forbidden')
        if name == 'awk' and any(re.search(r'\b(system|getline)\b|[|]', a) for a in args[1:]):
            raise ValueError('awk may not launch commands or open input pipes')
        if name == 'git':
            if len(args) < 4 or args[1] != '-C' or Path(args[2]).expanduser().resolve() not in {REPO, (ROOT/'hermes-agent').resolve()} or args[3] not in {'log', 'show', 'diff', 'status', 'blame'}:
                raise ValueError('git requires -C approved-repository log/show/diff/status/blame')
            if any(a.startswith(('--output', '--ext-diff', '--textconv', '--exec', '--config', '--no-index')) for a in args[4:]):
                raise ValueError('git external execution/output options are forbidden')
            args[2] = str(Path(args[2]).expanduser().resolve())
            if args[3] in {'log', 'show', 'diff', 'blame'}:
                args.insert(4, '--no-textconv')
                args.insert(4, '--no-ext-diff')
            args[1:1] = ['--no-pager', '-c', 'core.precomposeunicode=false', '-c', 'core.fsmonitor=false', '-c', 'core.hooksPath=/dev/null', '-c', 'diff.external=', '-c', 'core.pager=cat']
        args[0] = executable(name)
        # Expand ~ ourselves without admitting shell substitutions.
        for i in range(1, len(args)):
            if args[i].startswith('~/'):
                args[i] = str(Path(args[i]).expanduser())
    return ' | '.join(shlex.join(args) for args in parts)


def policy():
    def q(value):
        return json.dumps(str(value))
    read_roots = [ROOT.resolve(), REPO, '/System', '/usr/lib', '/usr/share', '/bin', '/usr/bin',
                  '/Library/Developer/CommandLineTools/usr/lib', '/opt/homebrew/Cellar', '/opt/homebrew/lib', '/opt/homebrew/opt', '/private/var/db/dyld']
    execs = {executable(n) for n in COMMANDS} | {'/bin/bash', '/usr/bin/env'}
    read_roots += list(execs)
    # Applies equally to source, settings snapshots and new profiles. Operating
    # data is not source code: block entire areas rather than today's file list.
    denied = [r'(^|/)[.]env([^/]*)(/|$)', r'(^|/)auth[.]json$',
              r'(?i)(token|secret|credentials)', r'(?i)(client[^/]*[.]json|google[^/]*[.]json)$',
              r'(^|/)(sessions|terminal-sessions|state|state-snapshots|backups|[.]curator_backups|pending|pending_messages|bot_relay|hook_outputs|workspace|sandboxes|cache|audio_cache|image_cache|assets|plans|memories|home|pairing|kanban|runtime)(/|$)',
              r'(^|/)(diagnosis-cases|diagnosis-records|diagnosis-requests|project-audits)(/|$)',
              r'(?i)[.](db|sqlite|sqlite3)(-|$)', r'(?i)[.](pem|key|p12)$',
              r'/cron/(output|runs|history)(/|$)',
              r'/logs/']
    # Logs are denied by default; only bounded infrastructure logs and usage
    # metadata have an exception. gateway.log can contain inbound user text.
    rules = ['(version 1)', '(allow default)', '(deny network*)', '(deny appleevent-send)',
             '(deny mach-lookup)', '(deny process-info*)', '(allow process-info* (target self))',
             '(deny process-exec)', '(allow process-exec ' + ' '.join('(literal '+q(p)+')' for p in sorted(execs)) + ')',
             '(deny file-read*)', '(allow file-read* ' + ' '.join('(subpath '+q(p)+')' for p in read_roots) + ' (literal "/dev/null") (literal "/"))',
             '(allow file-read-metadata)', '(deny file-write*)', '(allow file-write* (literal "/dev/null"))']
    for pattern in denied[:-1]:
        if pattern.startswith('(?i)'):
            pattern = ''.join('[' + c.lower() + c.upper() + ']' if c.isalpha() else c for c in pattern[4:])
        rules.append('(deny file-read* (regex #' + q(pattern) + '))')
    rules.append('(deny file-read* (require-all (regex #"/logs/") (require-not (regex #"/logs/(errors[.]log|gateway[.]error[.]log|response_usage[.]jsonl)$"))))')
    return '\n'.join(rules)


def sandbox_argv(command):
    if not Path('/usr/bin/sandbox-exec').is_file():
        raise ValueError('sandbox-exec is required')
    return ['/usr/bin/sandbox-exec', '-p', policy(), '/usr/bin/env', '-i',
            'PATH=/opt/homebrew/bin:/usr/bin:/bin', 'HOME=/nonexistent', 'LANG=en_US.UTF-8',
            'LC_ALL=en_US.UTF-8', 'GIT_CONFIG_NOSYSTEM=1', 'GIT_CONFIG_GLOBAL=/dev/null',
            'GIT_OPTIONAL_LOCKS=0', 'GIT_PAGER=cat', '/bin/bash', '--noprofile', '--norc', '-o', 'pipefail', '-c', command]


def execute(command):
    # Read at most a bounded amount and terminate the process group on timeout
    # or overflow; communicate() alone would buffer an unbounded pipeline.
    argv = sandbox_argv(pipeline(command))
    deadline = time.monotonic() + TIMEOUT
    output = bytearray()
    truncated = False
    with subprocess.Popen(argv, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          start_new_session=True, env={}) as proc:
        with selectors.DefaultSelector() as selector:
            selector.register(proc.stdout, selectors.EVENT_READ)
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()
                    raise TimeoutError('Read command exceeded 30 seconds')
                for key, _ in selector.select(min(remaining, 1)):
                    chunk = os.read(key.fd, 8192)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    output.extend(chunk)
                    if len(output) >= MAX_OUTPUT * 4:
                        truncated = True
                        os.killpg(proc.pid, signal.SIGKILL)
                        selector.unregister(key.fileobj)
                        break
            try:
                proc.wait(timeout=max(0.01, deadline-time.monotonic()))
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
                raise TimeoutError('Read command exceeded 30 seconds') from None
    spec = importlib.util.spec_from_file_location('shell_redact', Path(__file__).with_name('error_digest.py'))
    redactor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(redactor)
    text = redactor.redact(output.decode('utf-8', errors='replace'))
    return {'exit_code': proc.returncode, 'output': text[:MAX_OUTPUT], 'truncated': truncated or len(text) > MAX_OUTPUT}


def handle(args, **_):
    if os.environ.get('HERMES_DIAGNOSIS_REQUEST'):
        raise ValueError('Requester diagnosis cannot use shell reads')
    if not isinstance(args, dict) or set(args) != {'command'}:
        raise ValueError('Only command is accepted')
    return execute(args['command'])
