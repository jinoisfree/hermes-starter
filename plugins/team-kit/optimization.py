"""진단담당 시스템 최적화의 근거 수집 (2026-10-25 소유자 결정: 코드 리뷰·방치된 작업·도구 다이어트·스킬 정리).

Only metadata leaves this module: file paths and counts, tool and toolset names, skill names and
dates. Never file contents, tool arguments or conversation text.

- stale_work (매일, 보고만): uncommitted files left for hours in Hermes-desktop-pet / hermes-agent and
  Paseo agents that have been "running" for hours. Someone else's half-done work is never handed
  to Paseo automatically.
- tool_diet (일요일, 조직개편안 근거): per profile, toolsets whose schemas are large in the first
  request (team-kit prompt_probe records) but whose tools were not called in the last 7 days.
- skill_hygiene (일요일, 보고만): self-made skills never used or unused for 30 days.
- code_review_item (일요일, 자동 처리 항목): a fixed 조직개편안 item asking Paseo to review the
  last 7 days of commits in both repositories.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import sqlite3
import subprocess
import time
from pathlib import Path

ROOT = Path.home() / '.hermes'
SETTING = Path.home() / 'workspace/Hermes-desktop-pet'
AGENT = ROOT / 'hermes-agent'
REPOS = (('Hermes-desktop-pet', SETTING), ('hermes-agent', AGENT))
PASEO = '/Applications/Paseo.app/Contents/Resources/bin/paseo'
PROFILES = ('default', 'sns-manager', 'personal-secretary', 'system-doctor', 'research-assistant', 'work-assistant')
STALE_HOURS = 12
STUCK_HOURS = 6
DIET_MIN_TOKENS = 1000
DIET_DAYS = 7
# AGENTS.md (2026-10-18): the skills toolset stays; memory is a core layer.
DIET_KEEP = {'skills', 'memory'}
SKILL_UNUSED_DAYS = 30
SKILL_NEW_DAYS = 14


def home(root, profile):
    return root if profile == 'default' else root / 'profiles' / profile


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], capture_output=True, text=True, timeout=60, check=True).stdout


def stale_work(now=None, repos=REPOS, paseo=PASEO):
    now = now or time.time()
    result = {'repos': [], 'paseo': []}
    for name, repo in repos:
        try:
            lines = git(repo, 'status', '--porcelain', '--untracked-files=all').splitlines()
        except (OSError, subprocess.SubprocessError):
            continue
        old = []
        for line in lines:
            path = repo / line[3:].split(' -> ')[-1]
            if path.exists() and now - path.stat().st_mtime >= STALE_HOURS * 3600:
                old.append((path.stat().st_mtime, line[3:].split(' -> ')[-1]))
        if old:
            old.sort()
            result['repos'].append({'repo': name, 'files': len(old), 'oldest_hours': round((now - old[0][0]) / 3600),
                                    'paths': [p for _, p in old[:5]]})
    try:
        agents = json.loads(subprocess.run([paseo, 'ls', '--json'], capture_output=True, text=True, timeout=60, check=True).stdout)
        for agent in agents:
            if agent.get('status') != 'running':
                continue
            detail = json.loads(subprocess.run([paseo, 'inspect', agent['id'], '--json'], capture_output=True, text=True,
                                               timeout=60, check=True).stdout)
            updated = datetime.fromisoformat(detail['UpdatedAt'].replace('Z', '+00:00')).timestamp()
            if now - updated >= STUCK_HOURS * 3600:
                result['paseo'].append({'id': agent['id'][:8], 'title': str(agent.get('name', ''))[:60],
                                        'cwd': str(agent.get('cwd', ''))[:80], 'idle_hours': round((now - updated) / 3600)})
    except (OSError, subprocess.SubprocessError, ValueError, KeyError):
        result['paseo_error'] = True
    return result


def first_call(base):
    """Newest prompt_probe record of a Slack conversation (falls back to any platform)."""
    path = base / 'logs/prompt_first_calls.jsonl'
    if not path.exists():
        return None
    rows = []
    for line in path.read_text(errors='replace').splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get('toolsets'), dict):
            rows.append(row)
    slack = [r for r in rows if r.get('platform') == 'slack']
    return (slack or rows or [None])[-1]


def tool_uses(base, since):
    path = base / 'state.db'
    if not path.exists():
        return None
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2) as db:
        db.execute('PRAGMA query_only=ON')
        return dict(db.execute("SELECT tool_name, count(*) FROM messages WHERE role='tool' AND tool_name IS NOT NULL "
                               "AND timestamp>=? GROUP BY tool_name", (since,)).fetchall())


def tool_diet(root=ROOT, now=None):
    now = now or time.time()
    since = now - DIET_DAYS * 86400
    result = {}
    for profile in PROFILES:
        base = home(root, profile)
        row = first_call(base)
        if row is None:
            continue
        try:
            uses = tool_uses(base, since)
        except sqlite3.Error:
            uses = None
        if uses is None:
            continue
        candidates = []
        for name, bucket in row['toolsets'].items():
            names = bucket.get('names') or []
            if name in DIET_KEEP or bucket.get('tokens_est', 0) < DIET_MIN_TOKENS or not names:
                continue
            used = sum(uses.get(tool, 0) for tool in names)
            if used == 0:
                candidates.append({'toolset': name, 'tools': bucket.get('tools', len(names)), 'tokens_est': bucket['tokens_est'],
                                   'tool_names': names[:12]})
        result[profile] = {'first_call_tool_tokens_est': row['tools']['tokens_est'], 'measured_at': row.get('ts'),
                           'days': DIET_DAYS, 'unused_large_toolsets': sorted(candidates, key=lambda c: -c['tokens_est'])}
    return result


def parse_time(value):
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def skill_hygiene(root=ROOT, now=None):
    now = now or time.time()
    result = {}
    for profile in PROFILES:
        base = home(root, profile)
        usage_file = base / 'skills/.usage.json'
        if not usage_file.exists():
            continue
        try:
            usage = json.loads(usage_file.read_text())
        except ValueError:
            continue
        never, stale = [], []
        for name, row in sorted(usage.items()):
            if not isinstance(row, dict) or row.get('archived_at') or row.get('pinned'):
                continue
            used = parse_time(row.get('last_used_at')) if row.get('last_used_at') else None
            created = parse_time(row.get('created_at')) if row.get('created_at') else None
            if used is None and created is not None and now - created >= SKILL_NEW_DAYS * 86400:
                never.append(name)
            elif used is not None and now - used >= SKILL_UNUSED_DAYS * 86400:
                stale.append(name)
        total = sum(1 for p in (base / 'skills').rglob('SKILL.md') if not any(x.startswith('.') for x in p.relative_to(base / 'skills').parts))
        if never or stale:
            result[profile] = {'skills_total': total, 'self_made': len(usage), 'never_used': never[:10], f'unused_{SKILL_UNUSED_DAYS}d': stale[:10]}
    return result


def recent_commits(days=7, repos=REPOS):
    rows = {}
    for name, repo in repos:
        try:
            lines = git(repo, 'log', f'--since={days} days ago', '--no-merges', '--format=%h').split()
        except (OSError, subprocess.SubprocessError):
            lines = []
        rows[name] = len(lines)
    return rows


CODE = ('.py', '.sh', '.ts', '.tsx', '.js', '.mjs', '.swift')
# Plugin copies in profiles, skills and tests are not separate code to review.
SKIP = ('hermes/profiles/', 'hermes/skills/', 'tests/', 'Tests/', 'test_')
REVIEW_QUOTA = {'Hermes-desktop-pet': 8, 'desktop-pet': 5, 'hermes-agent': 5}


def changed_code(days=7, repos=REPOS):
    """Most-changed code files of the period per area (path and changed lines only), for a bounded review."""
    groups = {}
    for name, repo in repos:
        try:
            base = git(repo, 'rev-list', '-1', f'--before={days} days ago', 'HEAD').strip()
            rows = git(repo, 'diff', '--numstat', f'{base}..HEAD').splitlines() if base else []
        except (OSError, subprocess.SubprocessError):
            continue
        for row in rows:
            added, deleted, path = (row.split('\t') + ['', '', ''])[:3]
            if added == '-' or not (path.endswith(CODE) or path.startswith('local-bin/') or path in ('sync', 'make-starter')):
                continue
            if any(part in path for part in SKIP) and not path.startswith('hermes/profiles/system-doctor/scripts/'):
                continue
            area = 'desktop-pet' if path.startswith('desktop-pet/') else name
            groups.setdefault(area, []).append((int(added) + int(deleted), path))
    return {area: [f'{path} ({lines}줄)' for lines, path in sorted(rows, reverse=True)[:REVIEW_QUOTA.get(area, 5)]]
            for area, rows in groups.items()}


def code_review_item(n, days=7, repos=REPOS):
    counts = recent_commits(days, repos)
    if not any(counts.values()):
        return None
    summary = ', '.join(f'{name} {count}개' for name, count in counts.items())
    files = changed_code(days, repos)
    targets = '; '.join(f'{area}: ' + ', '.join(paths) for area, paths in files.items()) or '변경된 실행 코드 없음'
    if len(targets) > 1400:
        targets = targets[:1400] + ' …'

    return {
        'n': n, 'title': '주간 코드 리뷰', 'profiles': ['default'],
        'evidence': f'지난 {days}일 커밋: {summary} (git log --since, 병합 제외). 자동개편 커밋은 사람 검토 없이 들어간다.',
        'proposal': f'지난 {days}일 두 저장소의 커밋을 코드 리뷰해 확실한 버그·보안 문제·불필요한 코드만 고친다.',
        'effect': '자동 진행된 변경의 실수를 일주일 안에 잡는다.',
        'rollback': '이 리뷰의 표식 커밋을 롤백 버튼으로 되돌린다.',
        'instruction': (f'목표: 지난 {days}일 Hermes-desktop-pet(~/workspace/Hermes-desktop-pet)와 hermes-agent(~/.hermes/hermes-agent starter_user 브랜치)의 '
                        f'커밋을 코드 리뷰한다. 근거: git log --since="{days} days ago" --no-merges 로 범위를 확인한다({summary}). '
                        f'대상 파일(변경량 상위, 이 순서로 보고 시간이 남으면 나머지): {targets}. hermes/ 아래 경로는 실행 원본 ~/.hermes/ 기준으로 고치고, desktop-pet/ 파일은 desktop-pet/AGENTS.md 규칙을 따른다. '
                        '한 번에 모든 변경을 보지 않는다. 결과 notes에 실제로 본 파일 수를 적는다. '
                        '살필 것: 정확성 버그(조건·경계·예외·경쟁 상태), 보안(비밀값·승인 경계·외부 쓰기), 테스트 누락, 불필요하게 복잡한 코드. '
                        '완료 조건: 재현이나 테스트로 확인된 문제만 고치고 각 수정에 테스트를 더한다. 확인되지 않은 의심과 스타일 지적은 고치지 않고 결과 notes에 적는다. '
                        'AGENTS.md를 먼저 읽고 실행 원본 수정 후 ./sync, 관련 docs 갱신, 게이트웨이 Python 테스트, 필요 시 재시작 규칙을 따른다. '
                        '사용자 결정·승인 경계를 바꾸는 수정은 하지 않는다. 푸시는 소유자 확인 버튼으로 따로 승인된다.'),
    }


def notes(evidence):
    """Owner-facing info block for the 조직개편안 root: report only, never an automatic item."""
    lines = []
    stale = evidence.get('stale_work') or {}
    if stale.get('repos') or stale.get('paseo'):
        lines.append('*방치된 작업*')
        for row in stale.get('repos', []):
            lines.append(f"• {row['repo']} 커밋 안 된 파일 {row['files']}개, 가장 오래된 것 {row['oldest_hours']}시간: "
                         + ', '.join(f'`{p}`' for p in row['paths'][:3]))
        for row in stale.get('paseo', []):
            lines.append(f"• Paseo `{row['id']}` {row['title']} — {row['idle_hours']}시간째 실행 중으로 멈춰 보임")
    hygiene = evidence.get('skill_hygiene') or {}
    if hygiene:
        lines.append('*스킬 정리 후보* (보고만, 자동으로 지우지 않음)')
        for profile, row in hygiene.items():
            parts = []
            if row['never_used']:
                parts.append('한 번도 안 씀 ' + ', '.join(f'`{n}`' for n in row['never_used'][:5]))
            unused = row.get(f'unused_{SKILL_UNUSED_DAYS}d') or []
            if unused:
                parts.append(f'{SKILL_UNUSED_DAYS}일 넘게 안 씀 ' + ', '.join(f'`{n}`' for n in unused[:5]))
            lines.append(f"• {profile} (스킬 {row['skills_total']}개): " + ' · '.join(parts))
    return '\n'.join(lines)
