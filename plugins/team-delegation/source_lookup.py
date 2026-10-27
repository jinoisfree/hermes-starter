"""Read-only source outcomes. Missing is never inferred from a generic failure."""
from __future__ import annotations


class SourceReadError(ValueError):
    def __init__(self, status, reason):
        self.status, self.reason = status, reason
        # Retain the legacy fail-closed prefix for existing authorization callers.
        super().__init__('PERSONAL_SCOPE_UNVERIFIED: ' + reason)


def result(status, reason, row=None):
    return {'status': status, 'reason': reason, 'row': row}


def require(outcome):
    if outcome['status'] != 'ok':
        raise SourceReadError(outcome['status'], outcome['reason'])
    return outcome['row']


def calendar_failure(returncode, stderr):
    # The existing Swift executor wraps NSError in exit 70. Match its fixed domain,
    # not human-readable messages or an arbitrary nonzero exit code.
    if returncode == 77 or 'TE_CALENDAR_PERMISSION_DENIED' in stderr:
        return result('transient', 'calendar_permission_denied')
    if 'TE_CALENDAR_EVENT_NOT_FOUND' in stderr:
        return result('missing', 'calendar_not_found')
    return result('transient', 'calendar_read_failed')


def notes_failure(stderr):
    # -1728 is only trusted after the metadata script successfully enumerated the
    # accounts/folders; it returns an explicit result in that case.
    if '-1743' in stderr or '-10004' in stderr:
        return result('transient', 'notes_permission_denied')
    return result('transient', 'notes_read_failed')
