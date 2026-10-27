"""One global reminder inventory per watcher run; writes retain fresh ID readback."""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import json
import re
import subprocess

CURRENT = ContextVar('watcher_reminder_reads', default=None)


class ReminderReadError(ValueError):
    def __init__(self):
        super().__init__('reminder_read_failed')


def _run(args, runner):
    try:
        return runner(['remindctl', *args, '--json', '--no-input', '--no-color'],
                      capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise ReminderReadError() from None


def _inventory(runner):
    proc = _run(['show', 'all'], runner)
    try:
        rows = json.loads(proc.stdout) if not proc.returncode else None
    except (ValueError, TypeError):
        rows = None
    if isinstance(rows,dict):
        rows = rows.get('reminders',rows.get('items'))
    if not isinstance(rows,list) or any(not isinstance(r,dict) or not r.get('id') for r in rows):
        raise ReminderReadError()
    return rows


class Snapshot:
    def __init__(self):
        self.rows = None
        self.failed = False

    def all(self, runner):
        if self.failed:
            raise ReminderReadError()
        if self.rows is None:
            try:self.rows = _inventory(runner)
            except ReminderReadError:
                self.failed = True
                raise
        return deepcopy(self.rows)


@contextmanager
def batch():
    token = CURRENT.set(Snapshot())
    try:yield CURRENT.get()
    finally:CURRENT.reset(token)


def all(runner=None):
    runner = runner or subprocess.run
    snapshot = CURRENT.get()
    return snapshot.all(runner) if snapshot is not None else _inventory(runner)


def one(identifier, runner=None):
    """Fresh exact-ID read, never a cached write verification.

    v0.3.8 Errors.swift has a distinct, exact reminderNotFound message. Only
    that message for a complete UUID proves absence; access failures do not.
    """
    if not identifier or str(identifier).isdecimal():
        raise ReminderReadError()
    proc = _run(['info',identifier], runner or subprocess.run)
    text = ((proc.stderr or '') + (proc.stdout or '')).strip()
    if re.fullmatch(r'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', identifier) and text in {
        f'Reminder not found: "{identifier}".', f'Error: Reminder not found: "{identifier}".'}:
        row = None
    else:
        try:row = json.loads(proc.stdout) if not proc.returncode else None
        except (ValueError,TypeError):row = None
        if not isinstance(row,dict) or str(row.get('id','')).lower()!=identifier.lower():
            raise ReminderReadError()
    snapshot = CURRENT.get()
    if snapshot is not None and snapshot.rows is not None:
        snapshot.rows = [r for r in snapshot.rows if str(r['id']).lower()!=identifier.lower()]
        if row is not None:snapshot.rows.append(deepcopy(row))
    return row
