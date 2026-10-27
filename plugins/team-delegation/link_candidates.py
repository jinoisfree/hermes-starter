"""Read-only evidence for owner decisions. Work titles are never search inputs."""
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
from source_lookup import SourceReadError

DELETED_FOLDERS = {'recently deleted', '최근 삭제된 항목', '최근 삭제된 메모'}


def stamp(value):
    if isinstance(value, (int, float)):
        return float(value)
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return dt.timestamp() if dt.tzinfo else None
    except ValueError:
        return None


def deleted(identity):
    return str(identity.get('folder_name', '')).strip().casefold() in DELETED_FOLDERS


class Finder:
    def __init__(self, store, inventory):
        self.store, self.inventory = store, inventory

    def find(self, missing, work, *, exact_title=None):
        kind = missing['domain']
        metadata = missing.get('metadata', {})
        saved = metadata.get('identity') or {}
        anchor = stamp(missing.get('linked_at')) or stamp(work.get('created_at'))
        matches, removed = [], []
        try:
            rows = self.inventory(kind, missing)
            for row in rows:
                if row.get('owner_scope') != 'company':
                    continue
                ident = row['identity']
                if exact_title is not None:
                    matched = ident.get('title') == exact_title
                    basis = 'owner_title'
                elif saved:
                    # Identity-only title matches without a temporal field are not evidence.
                    temporal = 'start' if kind == 'calendar' else 'created_at'
                    container = {'notes':'folder_id','calendar':'calendar_id','todo':'list_id','notion':'database_id'}.get(kind)
                    matched = bool(saved.get('title') and saved.get(container)) and stamp(saved.get(temporal)) is not None and all(
                        ident.get(key) == value for key, value in saved.items())
                    basis = 'identity'
                else:
                    created = stamp(ident.get('created_at'))
                    matched = anchor is not None and created is not None and abs(created-anchor) <= 5
                    basis = 'creation_time'
                if not matched:
                    continue
                if deleted(ident):
                    removed.append({'source_id': row['source_id'], 'identity': ident})
                    continue
                if self.store.find_by_source(domain=kind, source_id=row['source_id']) is not None:
                    continue
                matches.append({**row, 'basis': basis})
        except (SourceReadError, OSError, subprocess.TimeoutExpired, ValueError, KeyError) as exc:
            return {'status': 'transient', 'reason': getattr(exc, 'reason', 'candidate_read_failed'),
                    'candidates': [], 'recently_deleted': [], 'certain': False}
        # Duplicates from an inventory must not manufacture uniqueness or numbered choices.
        matches = list({r['source_id']: r for r in matches}.values())
        return {'status': 'ok', 'candidates': matches[:3], 'total_candidates': len(matches),
                'recently_deleted': removed, 'certain': len(matches)==1 and matches[0]['basis']!='owner_title'}


class Inventory:
    def __init__(self, home):
        from source_identity import Reader
        self.reader = Reader(home)
        self.cache = {}
        self.errors = {}

    def __call__(self, kind, missing):
        if kind in self.errors:
            raise SourceReadError('transient', self.errors[kind])
        if kind == 'notes':
            if kind not in self.cache:
                proc = subprocess.run(['/usr/bin/osascript', '-l', 'JavaScript',
                    str(Path(__file__).with_name('link_candidates_notes.js'))],
                    capture_output=True, text=True, timeout=120)
                if proc.returncode:
                    raise SourceReadError('transient', 'notes_candidate_read_failed')
                rows = json.loads(proc.stdout)
                for row in rows:
                    row['owner_scope'] = self.reader.td.personal_scopes.scope_of(
                        self.reader.cfg, 'notes', row.pop('scope'))
                self.cache[kind] = rows
            return self.cache[kind]
        if kind == 'calendar':
            identity = missing.get('metadata', {}).get('identity')
            if not identity:
                return []  # EventKit inventory provides no usable creation timestamp.
            return [{'source_id': sid, **value} for sid, value in
                    self.reader.calendar_candidates(missing['metadata'])]
        if kind == 'todo':
            import reminder_reads
            if kind not in self.cache:
                try:
                    rows = reminder_reads.all()
                except reminder_reads.ReminderReadError:
                    self.errors[kind] = 'reminder_read_failed'
                    raise SourceReadError('transient', 'reminder_read_failed') from None
                self.cache[kind] = [{'source_id': r['id'], 'owner_scope': self.reader.td.personal_scopes.scope_of(
                    self.reader.cfg, 'todo', {'id': r.get('listID'), 'name': r.get('listName')}),
                    'identity': {'title': r.get('title'), 'list_id': r.get('listID'), 'list_name': r.get('listName'),
                                 'created_at': r.get('creationDate') or r.get('createdDate')}} for r in rows]
            return self.cache[kind]
        return []

    def fresh(self, kind, identifier):
        result = self.reader(kind, identifier)
        if kind == 'todo':
            # Keep optional evidence stable when it was present in the offered inventory.
            import reminder_reads
            row = reminder_reads.one(identifier)
            if row is None:
                raise SourceReadError('missing', 'reminder_not_found')
            result['identity']['created_at'] = row.get('creationDate') or row.get('createdDate')
        return {'source_id': identifier, **result}
