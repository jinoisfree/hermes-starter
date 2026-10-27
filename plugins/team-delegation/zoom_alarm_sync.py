"""Owner-authorized background alarm for company Zoom meetings (2026-10-25).

Every run looks a few days ahead in EventKit, and each company Zoom occurrence that has no
N-minute alarm gets one added (existing alarms kept, span "this"). An occurrence is touched at
most once: if the owner later removes the alarm, it stays removed. Nothing else is written.
"""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOME = Path.home() / '.hermes/profiles/work-assistant'
WEEKDAYS = '월화수목금토일'


def seoul():
    from zoneinfo import ZoneInfo
    return ZoneInfo('Asia/Seoul')


def moment(value):
    return datetime.fromisoformat(str(value).replace('Z', '+00:00'))


def occurrence_key(row):
    return f"{row.get('eventIdentifier') or row['calendarItemIdentifier']}|{moment(row['start']).astimezone(timezone.utc).isoformat()}"


def label(row):
    start = moment(row['start']).astimezone(seoul())
    return f"{start:%-m/%-d}({WEEKDAYS[start.weekday()]}) {start:%H:%M} {row.get('title') or '(제목 없음)'}"


class Calendar:
    """EventKit reads through the shared query binary; the only write is _calendar_event_add_alarm."""

    def __init__(self, tools=None, runner=subprocess.run):
        self._tools = tools
        self.runner = runner

    @property
    def tools(self):
        if self._tools is None:
            from notion_completion_sync import load_module
            self._tools = load_module('_zoom_alarm_calendar_tools', Path(__file__).with_name('__init__.py'))
        return self._tools

    def day(self, date):
        source = Path(__file__).with_name('eventkit_calendar_query.swift')
        binary = self.tools._calendar_cache_root(self.tools.PERSONAL_PROFILE) / 'eventkit_calendar_query-v2'
        if not binary.exists() or binary.stat().st_mtime_ns < source.stat().st_mtime_ns:
            compiled = self.runner(['/usr/bin/swiftc', '-parse-as-library', str(source), '-o', str(binary)],
                                   capture_output=True, text=True, timeout=120, check=False)
            if compiled.returncode != 0:
                raise RuntimeError('calendar_executor_compile_failed')
        proc = self.runner([str(binary), date, 'Asia/Seoul'], capture_output=True, text=True, timeout=30, check=False)
        if proc.returncode:
            raise RuntimeError(f'calendar_read_failed:{proc.returncode}')
        return json.loads(proc.stdout).get('events') or []

    def company(self, row):
        """Same company-scope guard as the linked-work watcher; personal calendars stay with assistant."""
        import notion_completion_sync
        try:
            notion_completion_sync.company_scope_guard(
                'calendar', {'action': 'update', 'event_id': row['calendarItemIdentifier'], 'expected_start': row['start']}, strict=True)
        except Exception:
            return False
        return True

    def add_alarm(self, row, minutes):
        return self.tools._calendar_event_add_alarm(row, minutes, self.runner)


def run(state, calendar, *, minutes=5, days=7, now=None, dry_run=False, persist=None):
    """Returns (added, failed) labels; state['handled'] remembers every occurrence already decided."""
    now = now or datetime.now(timezone.utc)
    handled = state.setdefault('handled', {})
    for key in [k for k, v in handled.items() if moment(v['start']) < now - timedelta(days=2)]:
        del handled[key]
    today = now.astimezone(seoul()).date()
    rows, seen = [], set()
    for offset in range(days + 1):
        for row in calendar.day((today + timedelta(days=offset)).isoformat()):
            key = occurrence_key(row)
            if key not in seen:
                seen.add(key)
                rows.append(row)
    added, failed = [], []
    for row in rows:
        key = occurrence_key(row)
        if not dry_run and handled.get(key, {}).get('status') == 'pending':
            # A previous process ended after reserving the write. Report once, never replay.
            handled[key]['status'] = 'failed'
            failed.append(label(row))
            if persist:
                persist(state)
        if (key in handled or row.get('zoomMeeting') is not True or row.get('isAllDay')
                or row.get('writable') is False or moment(row['start']) <= now + timedelta(minutes=minutes)):
            continue
        current = {round(-float(a['relativeOffset']) / 60) for a in row.get('alarmDetails') or [] if not a.get('absoluteDate')}
        if minutes in current:
            handled[key] = {'start': row['start'], 'status': 'present'}
            continue
        if not calendar.company(row):
            handled[key] = {'start': row['start'], 'status': 'not_company'}
            continue
        if dry_run:
            added.append(label(row))
            continue
        # Reserve durably before the external call. A crash or timeout must never replay it.
        handled[key] = {'start': row['start'], 'status': 'pending'}
        if persist:
            persist(state)
        try:
            after = calendar.add_alarm(row, minutes)
        except Exception:
            after = None
        # Never retry an unverified write: the owner hears about it once and decides.
        handled[key] = {'start': row['start'], 'status': 'added' if after else 'failed'}
        if after:
            handled[occurrence_key(after)] = handled[key]
            added.append(label(row))
        else:
            failed.append(label(row))
        if persist:
            persist(state)
    return added, failed


def report(added, failed, minutes, when):
    lines = [f'• {item}' for item in added]
    if added:
        lines.append(f'↳ 기존 알림은 그대로 두고 시작 {minutes}분 전 알림만 더했어요. 지운 알림은 다시 넣지 않아요.')
    lines += [f'• ⚠️ {item}: 알림을 못 넣음. 자동으로 다시 시도하지 않아요. 직접 확인해 주세요.' for item in failed]
    title = f'Zoom 회의 {minutes}분 전 알림 추가' if added else f'Zoom 회의 {minutes}분 전 알림 확인 필요'
    mark = '🔴' if failed else '🟢'
    return {'title': title, 'subtitle': '회사 캘린더', 'lines': lines, 'when': when,
            'fallback': '\n'.join([f'*{mark} {title}*', *lines])}


def main(*, dry_run=False):
    import hermes_yaml as yaml
    home = Path(os.environ.get('HERMES_HOME', '')).resolve()
    if home != HOME.resolve():
        raise RuntimeError('work-assistant home required')
    config = yaml.safe_load((home / 'config.yaml').read_text())
    settings = config.get('plugins', {}).get('entries', {}).get('team-delegation', {}).get('settings', {}).get('zoom_alarm_sync', {})
    if settings.get('enabled') is not True:
        return
    minutes, days = int(settings.get('minutes', 5)), int(settings.get('days', 7))
    if not 0 < minutes <= 60 or not 0 <= days <= 14:
        raise ValueError('unauthorized zoom alarm scope')
    path = home / 'state/zoom-alarm-sync.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix('.lock')
    if any(p.is_symlink() for p in (path.parent, path, lock_path)):
        raise ValueError('sync_state_symlink')
    from work_content import save
    with lock_path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        state = json.loads(path.read_text()) if path.exists() else {}
        try:
            added, failed = run(state, Calendar(), minutes=minutes, days=days, dry_run=dry_run,
                                persist=None if dry_run else lambda data: save(path, data))
            state['read_failures'] = 0
        except Exception as exc:
            if dry_run:
                print(f'Zoom 알림 시험 조회 실패 ({type(exc).__name__}); 처리 기록은 변경하지 않았어요.')
                return
            # Write exceptions are handled per occurrence above. Unstarted writes remain untouched.
            state['read_failures'] = state.get('read_failures', 0) + 1
            save(path, state)
            if state['read_failures'] == 3:
                print(f'*🔴 Zoom 회의 알림 확인 필요*\n• ⚠️ 캘린더 조회가 3번 연속 실패해 Zoom 알림을 넣지 못하고 있어요 ({type(exc).__name__}). 권한과 연결 상태 확인이 필요해요.')
            return
        if dry_run:
            print('\n'.join(added) or '추가할 Zoom 회의 없음')
            return
        save(path, state)
    if added or failed:
        import slack_report
        item = report(added, failed, minutes, datetime.now(seoul()).strftime('%-m/%-d %H:%M'))
        try:
            missed = slack_report.post_dm(home, [item])
        except Exception:
            missed = [item['fallback']]
        if missed:
            print('\n\n'.join(missed))


if __name__ == '__main__':
    main(dry_run='--dry-run' in sys.argv)
