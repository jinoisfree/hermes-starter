"""Trusted source identity acquisition, before opening registry write transactions."""
from pathlib import Path
import json
import subprocess
from contextvars import ContextVar
from contextlib import contextmanager

RECEIPT = ContextVar("verified_source_receipt", default=None)

@contextmanager
def verified_receipt(kind, identifier, value):
    token = RECEIPT.set((kind, identifier, validated(kind, value)))
    try:yield
    finally:RECEIPT.reset(token)

from source_lookup import SourceReadError, require

IDENTITY_FIELDS = {
    'notes': ('title', 'created_at', 'folder_id', 'folder_name', 'account_id'),
    'calendar': ('title', 'start', 'end', 'calendar_id', 'recurring', 'id_kind'),
    'todo': ('title', 'list_id', 'list_name'),
    'notion': ('title', 'created_at', 'database_id'),
}


def validated(kind, value):
    if not isinstance(value, dict) or value.get('owner_scope') not in {'company', 'personal'}:
        raise SourceReadError('transient', 'source_scope_unverified')
    identity = value.get('identity')
    if not isinstance(identity, dict) or any(
        key not in identity or identity[key] is None or identity[key] == ''
        for key in IDENTITY_FIELDS[kind]
    ):
        raise SourceReadError('transient', 'source_identity_incomplete')
    return {'owner_scope': value['owner_scope'], 'identity': {k:identity[k] for k in IDENTITY_FIELDS[kind]}}


class Reader:
    def __init__(self, home=None):
        import hermes_yaml
        from notion_completion_sync import HOME, load_module, NotionReader
        self.home = Path(home or HOME)
        root = Path.home() / '.hermes'
        self.cfg = hermes_yaml.safe_load((root/'config.yaml').read_text())['plugins']['entries']['team-kit']['settings']['personal_scopes']
        if self.cfg.get('enabled') is not True:
            raise SourceReadError('transient', 'source_scope_unverified')
        self.td = load_module('_source_identity_tools', Path(__file__).with_name('__init__.py'))
        self.notion = NotionReader(self.home)

    def calendar_candidates(self, metadata):
        """Search only the saved date; verify each candidate by exact native ID."""
        from datetime import datetime
        from zoneinfo import ZoneInfo
        saved = validated('calendar', metadata)
        ident = saved['identity']
        if saved['owner_scope'] != 'company' or ident['id_kind'] != 'eventkit' or ident['recurring']:
            return []
        try:
            start = datetime.fromisoformat(ident['start'].replace('Z', '+00:00'))
            day = start.astimezone(ZoneInfo('Asia/Seoul')).date() if start.tzinfo else start.date()
            response = json.loads(self.td._calendar_direct_lookup({'date':str(day)}))
            if response.get('ok') is not True:
                raise SourceReadError('transient', 'calendar_candidate_read_failed')
            rows = response['receipt']['results']
            matches = []
            for sid in dict.fromkeys(r['calendarItemIdentifier'] for r in rows):
                candidate = self('calendar', sid)
                if candidate == saved:
                    matches.append((sid, candidate))
            return matches
        except SourceReadError:
            raise
        except Exception:
            raise SourceReadError('transient', 'calendar_candidate_read_failed') from None

    def __call__(self, kind, identifier):
        scopes = self.td.personal_scopes
        if kind == 'notes':
            meta = scopes.native_metadata(self.td, kind, {'note_id':identifier}, {})
            owner = scopes.scope_of(self.cfg, kind, meta['current'])
            identity = meta.get('original')
        elif kind == 'calendar':
            if identifier.startswith('google:'):
                # A bare google ID carries no verified calendar/account identity.
                # Callers must provide the trusted API receipt via a scoped reader.
                raise SourceReadError('out_of_scope', 'google_calendar_source')
            outcome = self.td._calendar_event_lookup(identifier)
            row = outcome['row'] if outcome['status'] == 'unsupported' else require(outcome)
            owner = scopes.scope_of(self.cfg, kind, {'identifier':row['calendarIdentifier'], 'source':row.get('calendarSource')})
            identity = {'title':row['title'],'start':row['start'],'end':row['end'],
                        'calendar_id':row['calendarIdentifier'],'recurring':row.get('recurring',False),'id_kind':'eventkit'}
        elif kind == 'todo':
            import reminder_reads
            try:
                snapshot = reminder_reads.CURRENT.get()
                if snapshot is not None:
                    rows = [r for r in reminder_reads.all() if r['id'] == identifier]
                    row = rows[0] if len(rows)==1 else None
                else:
                    row = reminder_reads.one(identifier)
            except reminder_reads.ReminderReadError:
                raise SourceReadError('transient','reminder_read_failed') from None
            if row is None:
                raise SourceReadError('missing','reminder_not_found')
            owner = scopes.scope_of(self.cfg,kind,{'id':row.get('listID'),'name':row.get('listName')})
            identity = {'title':row.get('title'),'list_id':row.get('listID'),'list_name':row.get('listName')}
        elif kind == 'notion':
            from notion_completion_sync import DATABASE, page_id, page_title
            page = self.notion.page(identifier)
            database = page_id(page.get('parent',{}).get('database_id',''))
            if database != DATABASE:
                raise SourceReadError('out_of_scope','company_notion_database_required')
            owner = 'company'
            identity = {'title':page_title(page)[1],'created_at':page.get('created_time'),'database_id':database}
        else:
            raise SourceReadError('unsupported','unsupported_work_source')
        return validated(kind, {'owner_scope':owner,'identity':identity})


_reader = None


def production_reader(kind, identifier):
    global _reader
    receipt = RECEIPT.get()
    if receipt is not None and receipt[:2] == (kind, identifier):
        return receipt[2]
    if _reader is None:
        _reader = Reader()
    return _reader(kind, identifier)


def calendar_candidates(metadata):
    global _reader
    if _reader is None:
        _reader = Reader()
    return _reader.calendar_candidates(metadata)

production_reader.calendar_candidates = calendar_candidates
