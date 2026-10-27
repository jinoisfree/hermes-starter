"""SQLite registry linking one personal work item to native source objects."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
import unicodedata
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping


_COMMON_WORK_INIT_LOCK = threading.Lock()


LINKABLE_DOMAINS = frozenset({"calendar", "todo", "notes"})
LINK_REASONS = frozenset({"approved_mutation", "explicit_existing_object_link"})
KNOWLEDGE_LINK_REASONS = frozenset(
    {"explicit_existing_object_link", "verified_workflow_evidence"}
)
KNOWLEDGE_REF_AUTHORITIES = {
    "notion_page": "Notion",
    "gmail_message": "Gmail",
    "gmail_thread": "Gmail",
    "mail_message": "Mac Mail app",
    "wiki_page": "starter_user1 LLM Wiki",
    "memory_entry": "Hermes memory",
    "source_session": "Hermes session",
    "mutation_receipt": "Hermes receipt",
    "query_receipt": "Hermes receipt",
}
KNOWLEDGE_RELATIONS = frozenset(
    {
        "originated_from",
        "discussed_in",
        "documented_by",
        "summarized_by",
        "verified_by",
        "supports",
        "superseded_by",
    }
)


class CommonWorkError(ValueError):
    pass


class ApprovalRequired(CommonWorkError):
    pass


class PartialCompletionError(CommonWorkError):
    def __init__(self, message: str, *, item: Mapping[str, Any]) -> None:
        super().__init__(message)
        self.item = dict(item)


def _validate_source(domain: str, source_id: str) -> tuple[str, str]:
    normalized_domain = str(domain or "").strip().casefold()
    normalized_id = str(source_id or "").strip()
    if normalized_domain not in LINKABLE_DOMAINS:
        raise CommonWorkError(f"domain is not linkable: {normalized_domain or '<empty>'}")
    if not normalized_id:
        raise CommonWorkError(f"{normalized_domain} source ID is required")
    if normalized_domain == "notes" and normalized_id.isdecimal():
        raise CommonWorkError("Notes list indexes are not stable source IDs")
    return normalized_domain, normalized_id


def _validate_knowledge_ref(
    ref_type: str,
    external_id: str,
    relation_type: str,
    link_reason: str,
) -> tuple[str, str, str, str]:
    normalized_type = str(ref_type or "").strip().casefold()
    normalized_id = str(external_id or "").strip()
    normalized_relation = str(relation_type or "").strip().casefold()
    normalized_reason = str(link_reason or "").strip().casefold()
    if normalized_type not in KNOWLEDGE_REF_AUTHORITIES:
        raise CommonWorkError(
            f"unsupported knowledge reference type: {normalized_type or '<empty>'}"
        )
    if not normalized_id:
        raise CommonWorkError(f"{normalized_type} external ID is required")
    if normalized_relation not in KNOWLEDGE_RELATIONS:
        raise CommonWorkError(
            f"unsupported knowledge relation: {normalized_relation or '<empty>'}"
        )
    if normalized_reason not in KNOWLEDGE_LINK_REASONS:
        raise CommonWorkError(
            "knowledge references require explicit confirmation or verified workflow evidence"
        )
    return normalized_type, normalized_id, normalized_relation, normalized_reason


class CommonWorkStore:
    def __init__(self, path: str | Path, *, identity_reader=None) -> None:
        self.path = Path(path)
        self.identity_reader = identity_reader
        with _COMMON_WORK_INIT_LOCK:
            conn = self._connect()
            try:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS common_work (
                        common_id TEXT PRIMARY KEY,
                        title TEXT NOT NULL,
                        status TEXT NOT NULL CHECK (status IN ('active', 'completed')),
                        created_at REAL NOT NULL,
                        completed_at REAL
                    );
                    CREATE TABLE IF NOT EXISTS source_links (
                        common_id TEXT NOT NULL REFERENCES common_work(common_id) ON DELETE RESTRICT,
                        domain TEXT NOT NULL,
                        source_id TEXT NOT NULL,
                        metadata_json TEXT NOT NULL,
                        linked_at REAL NOT NULL,
                        PRIMARY KEY (common_id, domain, source_id),
                        UNIQUE (domain, source_id)
                    );
                    CREATE TABLE IF NOT EXISTS common_work_events (
                        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        common_id TEXT NOT NULL REFERENCES common_work(common_id) ON DELETE RESTRICT,
                        event_type TEXT NOT NULL,
                        payload_json TEXT NOT NULL,
                        created_at REAL NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS source_health (
                        common_id TEXT NOT NULL REFERENCES common_work(common_id),
                        domain TEXT NOT NULL,
                        source_id TEXT NOT NULL,
                        status TEXT NOT NULL CHECK(status IN ('ok','missing','out_of_scope','unsupported','transient','conflict')),
                        reason TEXT NOT NULL,
                        metadata_json TEXT NOT NULL,
                        checked_at REAL NOT NULL,
                        failures INTEGER NOT NULL,
                        PRIMARY KEY(common_id, domain, source_id)
                    );
                    CREATE TABLE IF NOT EXISTS personal_bundle_executions (
                        bundle_key TEXT PRIMARY KEY,
                        payload_digest TEXT NOT NULL,
                        status TEXT NOT NULL CHECK (
                            status IN ('executing', 'completed', 'partial', 'failed')
                        ),
                        receipt_json TEXT,
                        created_at REAL NOT NULL,
                        updated_at REAL NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS knowledge_refs (
                        ref_id TEXT PRIMARY KEY,
                        common_id TEXT NOT NULL REFERENCES common_work(common_id) ON DELETE RESTRICT,
                        ref_type TEXT NOT NULL,
                        external_id TEXT NOT NULL,
                        relation_type TEXT NOT NULL,
                        authority TEXT NOT NULL,
                        link_reason TEXT NOT NULL,
                        source_session_id TEXT,
                        receipt_id TEXT,
                        metadata_json TEXT NOT NULL,
                        verified_at REAL NOT NULL,
                        created_at REAL NOT NULL,
                        unlinked_at REAL,
                        unlink_reason TEXT
                    );
                    CREATE INDEX IF NOT EXISTS idx_source_links_common_id
                        ON source_links(common_id);
                    CREATE INDEX IF NOT EXISTS idx_knowledge_refs_common_id
                        ON knowledge_refs(common_id, unlinked_at);
                    CREATE UNIQUE INDEX IF NOT EXISTS idx_knowledge_refs_active_unique
                        ON knowledge_refs(common_id, ref_type, external_id, relation_type)
                        WHERE unlinked_at IS NULL;
                    """
                )
                conn.commit()
            finally:
                conn.close()

    @classmethod
    def production(cls, path):
        from source_identity import production_reader
        actual = Path.home()/'.hermes/shared/common-work-links.sqlite3'
        return cls(path, identity_reader=production_reader if Path(path).resolve()==actual.resolve() else None)

    def source_metadata(self, domain, source_id, metadata=None):
        """All link paths use the same trusted reader, outside write transactions.

        Isolated registries can omit a reader. Production wiring supplies one;
        caller-provided identity/scope never overrides a trusted read.
        """
        value = dict(metadata or {})
        if self.identity_reader is not None:
            from source_identity import validated
            try:
                trusted = validated(domain, self.identity_reader(domain, source_id))
            except ValueError as exc:
                raise CommonWorkError('source identity unavailable') from exc
            value.update(trusted)
            value['link_state'] = {'status':'ok', 'checked_at':time.time(), 'failures':0}
        return value

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.parent.chmod(0o700)
        conn = sqlite3.connect(self.path, timeout=5.0)
        self.path.chmod(0o600)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 5000")
        return conn

    @staticmethod
    def _new_id() -> str:
        return f"cw_{uuid.uuid4().hex}"

    @staticmethod
    def _new_knowledge_ref_id() -> str:
        return f"kr_{uuid.uuid4().hex}"

    @staticmethod
    def _event(
        conn: sqlite3.Connection,
        common_id: str,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> None:
        conn.execute(
            """
            INSERT INTO common_work_events (common_id, event_type, payload_json, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                common_id,
                event_type,
                json.dumps(dict(payload), ensure_ascii=False, sort_keys=True),
                time.time(),
            ),
        )

    def create(
        self,
        *,
        title: str,
        sources: Iterable[Mapping[str, Any]],
        link_reason: str,
        common_id: str | None = None,
    ) -> dict[str, Any]:
        normalized_title = str(title or "").strip()
        if not normalized_title:
            raise CommonWorkError("title is required")
        normalized_reason = str(link_reason or "").strip()
        if normalized_reason not in LINK_REASONS:
            raise CommonWorkError("a valid link_reason is required")
        normalized_sources = []
        for source in sources:
            domain, source_id = _validate_source(source.get("domain"), source.get("source_id"))
            normalized_sources.append(
                {
                    "domain": domain,
                    "source_id": source_id,
                    "metadata": {
                        **self.source_metadata(domain, source_id, source.get("metadata")),
                        "link_reason": normalized_reason,
                    },
                }
            )
        if not normalized_sources:
            raise CommonWorkError("at least one native source ID is required")

        if self.identity_reader is not None:
            normalized_title = normalized_sources[0]['metadata']['identity']['title']
        identifier = str(common_id or self._new_id()).strip()
        now = time.time()
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO common_work (common_id, title, status, created_at) VALUES (?, ?, 'active', ?)",
                (identifier, normalized_title, now),
            )
            for source in normalized_sources:
                conn.execute(
                    """
                    INSERT INTO source_links
                        (common_id, domain, source_id, metadata_json, linked_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        identifier,
                        source["domain"],
                        source["source_id"],
                        json.dumps(source["metadata"], ensure_ascii=False, sort_keys=True),
                        now,
                    ),
                )
            self._event(conn, identifier, "created", {
                "sources": normalized_sources,
                "link_reason": normalized_reason,
            })
        return self.get(identifier)

    def register_company_source(self, *, domain, source_id, title, source_date=None, eligible=None):
        """Reserve source ownership and select a unique active work in one SQLite transaction."""
        domain, source_id = _validate_source(domain, source_id)
        title = str(title or '').strip()
        metadata = self.source_metadata(domain, source_id, {'link_reason': 'approved_mutation', 'owner_scope': 'company', 'source_date': source_date})
        if self.identity_reader is not None:
            if metadata['owner_scope'] != 'company':
                raise CommonWorkError('company source required')
            title = metadata['identity']['title']
        normalized = ''.join(c for c in unicodedata.normalize('NFKC', title).casefold() if c.isalnum())
        if not normalized:
            raise CommonWorkError('title is required')
        candidates = []
        verified = {}
        def signature(item):
            return (item['title'], item['status'], item['sources'], item.get('knowledge_refs', []))
        if eligible is not None:
            # Native reads must finish before the SQLite write lock; a calendar read opens this registry too.
            with self._connect() as read_conn:
                originals = list(read_conn.execute("SELECT common_id,title FROM common_work WHERE status='active'"))
            for row in originals:
                key = ''.join(c for c in unicodedata.normalize('NFKC', row['title']).casefold() if c.isalnum())
                if key == normalized:
                    item = self.get(row['common_id'])
                    verified[row['common_id']] = (signature(item), eligible(item, source_date))
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            old = conn.execute('SELECT common_id FROM source_links WHERE domain=? AND source_id=?', (domain, source_id)).fetchone()
            if old is not None:
                identifier = old['common_id']
            else:
                unverified = False
                for row in conn.execute("SELECT common_id, title FROM common_work WHERE status='active'"):
                    key = ''.join(c for c in unicodedata.normalize('NFKC', row['title']).casefold() if c.isalnum())
                    if key != normalized:
                        continue
                    item = self.get(row['common_id'])
                    if eligible is not None:
                        proof = verified.get(item['common_id'])
                        if proof is None or proof[0] != signature(item):
                            unverified = True
                            continue
                        if not proof[1]:
                            continue
                    if eligible is None:
                        dates = {v.get('metadata', {}).get('source_date') for v in item['sources']}
                        if dates != {source_date} or any(v.get('metadata', {}).get('owner_scope') == 'personal' for v in item['sources']):
                            continue
                    candidates.append(item)
                auto = not unverified and len(candidates) == 1 and not any(v['domain'] == domain for v in candidates[0]['sources'])
                identifier = candidates[0]['common_id'] if auto else self._new_id()
                if not auto:
                    conn.execute("INSERT INTO common_work (common_id,title,status,created_at) VALUES (?,?,'active',?)", (identifier, title, time.time()))
                conn.execute('INSERT INTO source_links (common_id,domain,source_id,metadata_json,linked_at) VALUES (?,?,?,?,?)',
                             (identifier, domain, source_id, json.dumps(metadata, ensure_ascii=False), time.time()))
                evidence = {'domain': domain, 'source_id': source_id, 'link_reason': 'approved_mutation',
                            'basis': {'normalized_title': normalized, 'source_date': source_date, 'candidate_count': len(candidates)}}
                if auto:
                    self._event(conn, identifier, 'auto_linked', evidence)
                else:
                    self._event(conn, identifier, 'created', evidence)
        item = self.get(identifier)
        if len(candidates) > 1:
            item['link_candidates'] = [{'common_id': v['common_id'], 'title': v['title']} for v in candidates]
        return item

    def link(
        self,
        common_id: str,
        *,
        domain: str,
        source_id: str,
        metadata: Mapping[str, Any] | None = None,
        link_reason: str,
    ) -> dict[str, Any]:
        normalized_domain, normalized_id = _validate_source(domain, source_id)
        normalized_reason = str(link_reason or "").strip()
        if normalized_reason not in LINK_REASONS:
            raise CommonWorkError("a valid link_reason is required")
        normalized_metadata = {**self.source_metadata(normalized_domain, normalized_id, metadata), "link_reason": normalized_reason}
        with self._connect() as conn:
            row = conn.execute(
                "SELECT status FROM common_work WHERE common_id = ?", (common_id,)
            ).fetchone()
            if row is None:
                raise CommonWorkError(f"unknown common_id: {common_id}")
            if row["status"] != "active":
                raise CommonWorkError("completed common work cannot receive new links")
            conn.execute(
                """
                INSERT INTO source_links
                    (common_id, domain, source_id, metadata_json, linked_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    common_id,
                    normalized_domain,
                    normalized_id,
                    json.dumps(normalized_metadata, ensure_ascii=False, sort_keys=True),
                    time.time(),
                ),
            )
            self._event(
                conn,
                common_id,
                "linked",
                {
                    "domain": normalized_domain,
                    "source_id": normalized_id,
                    "link_reason": normalized_reason,
                },
            )
        return self.get(common_id)

    def get(self, common_id: str) -> dict[str, Any]:
        with self._connect() as conn:
            item = conn.execute(
                "SELECT * FROM common_work WHERE common_id = ?", (common_id,)
            ).fetchone()
            if item is None:
                raise CommonWorkError(f"unknown common_id: {common_id}")
            links = conn.execute(
                """
                SELECT domain, source_id, metadata_json, linked_at
                FROM source_links WHERE common_id = ? ORDER BY domain, linked_at
                """,
                (common_id,),
            ).fetchall()
            knowledge_refs = self._knowledge_refs(conn, common_id)
        sources = [
            {
                "domain": row["domain"],
                "source_id": row["source_id"],
                "metadata": json.loads(row["metadata_json"]),
                "linked_at": row["linked_at"],
            }
            for row in links
        ]
        reminders = [source for source in sources if source["domain"] == "todo"]
        completed_reminders = sum(
            source["metadata"].get("completion", {}).get("status") == "completed"
            for source in reminders
        )
        completion_state = (
            "completed" if item["status"] == "completed"
            else "partial" if completed_reminders
            else "pending"
        )
        return {
            "common_id": item["common_id"],
            "title": item["title"],
            "status": item["status"],
            "created_at": item["created_at"],
            "completed_at": item["completed_at"],
            "completion_state": completion_state,
            "completion_progress": {
                "completed_reminders": completed_reminders,
                "total_reminders": len(reminders),
            },
            "sources": sources,
            "knowledge_refs": knowledge_refs,
        }

    def find_by_source(self, *, domain: str, source_id: str) -> dict[str, Any] | None:
        normalized_domain, normalized_id = _validate_source(domain, source_id)
        with self._connect() as conn:
            row = conn.execute(
                "SELECT common_id FROM source_links WHERE domain = ? AND source_id = ?",
                (normalized_domain, normalized_id),
            ).fetchone()
        return self.get(row["common_id"]) if row else None

    def replace_note_source(self, *, old_id: str, new_id: str) -> None:
        """Move explicit note links atomically, keeping metadata and an audit event."""
        _, old_id = _validate_source("notes", old_id)
        _, new_id = _validate_source("notes", new_id)
        if old_id == new_id:
            raise CommonWorkError("replacement must have a new ID")
        fresh = self.source_metadata('notes', new_id) if self.identity_reader is not None else {}
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("SELECT common_id, metadata_json FROM source_links WHERE domain='notes' AND source_id=?", (old_id,)).fetchall()
            if conn.execute("SELECT 1 FROM source_links WHERE domain='notes' AND source_id=?", (new_id,)).fetchone():
                if not rows:  # already moved (rollback retry after native failure)
                    return
                raise CommonWorkError("replacement note is already linked")
            for row in rows:
                previous = json.loads(row['metadata_json'])
                if fresh and previous.get('owner_scope') and fresh['owner_scope'] != previous['owner_scope']:
                    raise CommonWorkError('replacement note scope changed')
                metadata = {**previous, **fresh}
                conn.execute("UPDATE source_links SET source_id=?, metadata_json=? WHERE domain='notes' AND source_id=?",
                             (new_id, json.dumps(metadata, ensure_ascii=False), old_id))
                if fresh:
                    now = time.time()
                    conn.execute('INSERT OR REPLACE INTO source_health VALUES (?,?,?,?,?,?,?,?)',
                                 (row['common_id'],'notes',new_id,'ok','agent_rebuilt',json.dumps(metadata,ensure_ascii=False),now,0))
                    conn.execute("UPDATE source_health SET status='missing',reason='agent_rebuilt',checked_at=? WHERE common_id=? AND domain='notes' AND source_id=?",
                                 (now,row['common_id'],old_id))
                self._event(conn, row["common_id"], "note_source_replaced", {"old_id": old_id, "new_id": new_id, "identity": metadata.get("identity")})

    def relink_calendar(self, common_id, old_id, new_id, identity):
        """Replace a proven missing ID locally, with ownership rechecked under lock."""
        from source_identity import validated
        fresh = validated('calendar', identity)
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute("SELECT metadata_json FROM source_links WHERE common_id=? AND domain='calendar' AND source_id=?",
                               (common_id,old_id)).fetchone()
            if row is None or old_id == new_id:
                return False
            metadata = json.loads(row['metadata_json'])
            if validated('calendar', metadata) != fresh or fresh['owner_scope'] != 'company':
                raise CommonWorkError('calendar replacement identity changed')
            if conn.execute("SELECT 1 FROM source_links WHERE domain='calendar' AND source_id=?", (new_id,)).fetchone():
                return False
            now = time.time()
            metadata.update(fresh)
            metadata['link_state'] = {'status':'ok','reason':'relinked','checked_at':now,'failures':0}
            encoded = json.dumps(metadata,ensure_ascii=False)
            conn.execute("UPDATE source_links SET source_id=?,metadata_json=? WHERE common_id=? AND domain='calendar' AND source_id=?",
                         (new_id,encoded,common_id,old_id))
            for sid,status,reason in [(old_id,'missing','relinked'),(new_id,'ok','relinked')]:
                conn.execute('INSERT OR REPLACE INTO source_health VALUES (?,?,?,?,?,?,?,?)',
                             (common_id,'calendar',sid,status,reason,encoded,now,0))
            self._event(conn,common_id,'relinked',{'domain':'calendar','old_id':old_id,'new_id':new_id})
            return True

    def owner_relink_result(self, common_id, decision_id):
        with self._connect() as conn:
            row = conn.execute("SELECT payload_json FROM common_work_events WHERE common_id=? AND event_type='owner_relinked' AND json_extract(payload_json,'$.decision_id')=?", (common_id,decision_id)).fetchone()
        return json.loads(row[0]) if row else None

    def owner_relink(self, common_id, domain, old_id, new_id, identity, decision_id):
        """Local-only owner decision. Event makes crash/repeated-click recovery idempotent."""
        from source_identity import validated
        trusted = validated(domain, identity)
        if trusted['owner_scope'] != 'company':
            raise CommonWorkError('company_source_required')
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            previous = conn.execute("SELECT payload_json,common_id FROM common_work_events WHERE event_type='owner_relinked' AND json_extract(payload_json,'$.decision_id')=?", (decision_id,)).fetchone()
            if previous:
                data = json.loads(previous[0])
                if previous[1] != common_id or (data['old_id'],data['new_id'],data['domain']) != (old_id,new_id,domain):
                    raise CommonWorkError('decision_already_used')
                return False
            missing = conn.execute("SELECT metadata_json FROM source_health WHERE common_id=? AND domain=? AND source_id=? AND status='missing'", (common_id,domain,old_id)).fetchone()
            work = conn.execute('SELECT status FROM common_work WHERE common_id=?',(common_id,)).fetchone()
            if missing is None or work is None or work[0]!='active':
                raise CommonWorkError('missing_link_changed')
            if conn.execute('SELECT 1 FROM source_links WHERE domain=? AND source_id IN (?,?)',(domain,old_id,new_id)).fetchone():
                raise CommonWorkError('source_already_linked')
            now = time.time()
            metadata = {**json.loads(missing[0]), **trusted, 'link_reason':'owner_confirmed_relink',
                        'link_state':{'status':'ok','reason':'owner_relinked','checked_at':now,'failures':0}}
            encoded = json.dumps(metadata,ensure_ascii=False)
            conn.execute("UPDATE source_health SET reason='relinked' WHERE common_id=? AND domain=? AND source_id=?", (common_id,domain,old_id))
            conn.execute('INSERT INTO source_links VALUES (?,?,?,?,?)',(common_id,domain,new_id,encoded,now))
            conn.execute('INSERT OR REPLACE INTO source_health VALUES (?,?,?,?,?,?,?,?)',
                         (common_id,domain,new_id,'ok','owner_relinked',encoded,now,0))
            self._event(conn,common_id,'owner_relinked',{'domain':domain,'old_id':old_id,'new_id':new_id,'decision_id':decision_id})
            return True

    def source_health(self, common_id):
        with self._connect() as conn:
            rows = conn.execute('SELECT * FROM source_health WHERE common_id=?', (common_id,)).fetchall()
        return [{**dict(row), 'metadata':json.loads(row['metadata_json'])} for row in rows]

    @staticmethod
    def _detach_source(conn, common_id, domain, source_id, reason):
        conn.execute('DELETE FROM source_links WHERE common_id=? AND domain=? AND source_id=?',
                     (common_id, domain, source_id))
        CommonWorkStore._event(conn, common_id, 'rollback_unlinked',
                              {'domain':domain,'source_id':source_id,'reason':reason})

    def record_source_state(self, common_id, domain, source_id, status, reason, *, identity=None):
        """Atomic state transition + optional missing-link detach; no external writes."""
        if status not in {'ok','missing','out_of_scope','unsupported','transient','conflict'}:
            raise CommonWorkError('invalid source state')
        with self._connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            if domain == 'notion':
                linked = conn.execute("SELECT metadata_json FROM knowledge_refs WHERE common_id=? AND ref_type='notion_page' AND external_id=? AND unlinked_at IS NULL",
                                      (common_id,source_id)).fetchone()
            else:
                linked = conn.execute('SELECT metadata_json FROM source_links WHERE common_id=? AND domain=? AND source_id=?',
                                      (common_id,domain,source_id)).fetchone()
            old = conn.execute('SELECT * FROM source_health WHERE common_id=? AND domain=? AND source_id=?',
                               (common_id,domain,source_id)).fetchone()
            if linked is None:
                return None  # Concurrent detachment/rebuild: never resurrect the old ID.
            metadata = json.loads(linked['metadata_json'])
            if identity:
                from source_identity import validated
                metadata.update(validated(domain, identity))
            failures = 0 if status == 'ok' else (old['failures']+1 if old and old['status']==status and old['reason']==reason else 1)
            now = time.time()
            metadata['link_state'] = {'status':status,'reason':reason,'checked_at':now,'failures':failures}
            encoded = json.dumps(metadata,ensure_ascii=False)
            changed = old is None or (old['status'],old['reason']) != (status,reason)
            conn.execute('INSERT OR REPLACE INTO source_health VALUES (?,?,?,?,?,?,?,?)',
                         (common_id,domain,source_id,status,reason,encoded,now,failures))
            if domain == 'notion':
                conn.execute("UPDATE knowledge_refs SET metadata_json=? WHERE common_id=? AND ref_type='notion_page' AND external_id=? AND unlinked_at IS NULL",
                             (encoded,common_id,source_id))
                if status == 'missing':
                    conn.execute("UPDATE knowledge_refs SET unlinked_at=?,unlink_reason='source_missing' WHERE common_id=? AND ref_type='notion_page' AND external_id=? AND unlinked_at IS NULL",
                                 (now,common_id,source_id))
            else:
                conn.execute('UPDATE source_links SET metadata_json=? WHERE common_id=? AND domain=? AND source_id=?',
                             (encoded,common_id,domain,source_id))
                if status == 'missing':
                    self._detach_source(conn,common_id,domain,source_id,'source_missing')
            if changed:
                event = {'domain':domain,'source_id':source_id,'status':status,'reason':reason,
                         'previous_status':old['status'] if old else None}
                self._event(conn,common_id,'source_state_changed',event)
                return event
        return None

    def rollback_unlink_source(self, *, domain: str, source_id: str) -> dict[str, Any] | None:
        """Detach a source deleted by the scoped latest-change rollback."""
        normalized_domain, normalized_id = _validate_source(domain, source_id)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT common_id FROM source_links WHERE domain = ? AND source_id = ?",
                (normalized_domain, normalized_id),
            ).fetchone()
            if row is None:
                return None
            common_id = str(row["common_id"])
            self._detach_source(conn, common_id, normalized_domain, normalized_id, 'latest_change_rollback')
        return self.get(common_id)

    @staticmethod
    def _knowledge_refs(
        conn: sqlite3.Connection,
        common_id: str,
        *,
        include_unlinked: bool = False,
    ) -> list[dict[str, Any]]:
        where = "common_id = ?" if include_unlinked else "common_id = ? AND unlinked_at IS NULL"
        rows = conn.execute(
            f"""
            SELECT ref_id, ref_type, external_id, relation_type, authority,
                   link_reason, source_session_id, receipt_id, metadata_json,
                   verified_at, created_at, unlinked_at, unlink_reason
            FROM knowledge_refs
            WHERE {where}
            ORDER BY created_at, ref_id
            """,
            (common_id,),
        ).fetchall()
        return [
            {
                "ref_id": row["ref_id"],
                "ref_type": row["ref_type"],
                "external_id": row["external_id"],
                "relation_type": row["relation_type"],
                "authority": row["authority"],
                "link_reason": row["link_reason"],
                "source_session_id": row["source_session_id"],
                "receipt_id": row["receipt_id"],
                "metadata": json.loads(row["metadata_json"]),
                "verified_at": row["verified_at"],
                "created_at": row["created_at"],
                "unlinked_at": row["unlinked_at"],
                "unlink_reason": row["unlink_reason"],
            }
            for row in rows
        ]

    def list_knowledge_refs(
        self, common_id: str, *, include_unlinked: bool = False
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if conn.execute(
                "SELECT 1 FROM common_work WHERE common_id = ?", (common_id,)
            ).fetchone() is None:
                raise CommonWorkError(f"unknown common_id: {common_id}")
            return self._knowledge_refs(
                conn, common_id, include_unlinked=include_unlinked
            )

    def link_knowledge_ref(
        self,
        common_id: str,
        *,
        ref_type: str,
        external_id: str,
        relation_type: str,
        link_reason: str,
        source_session_id: str | None = None,
        receipt_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        new_work_title: str = '',
    ) -> dict[str, Any]:
        normalized_type, normalized_id, normalized_relation, normalized_reason = (
            _validate_knowledge_ref(
                ref_type, external_id, relation_type, link_reason
            )
        )
        normalized_session = str(source_session_id or "").strip() or None
        normalized_receipt = str(receipt_id or "").strip() or None
        if normalized_reason == "verified_workflow_evidence" and not normalized_receipt:
            raise CommonWorkError(
                "verified workflow evidence requires an immutable receipt ID"
            )
        if normalized_type == 'notion_page':
            metadata = self.source_metadata('notion', normalized_id, metadata)
            if self.identity_reader is not None and not common_id:
                new_work_title = metadata['identity']['title']
        identifier = self._new_knowledge_ref_id()
        now = time.time()
        authority = KNOWLEDGE_REF_AUTHORITIES[normalized_type]
        with self._connect() as conn:
            if not common_id and new_work_title:
                if normalized_type != 'notion_page' or normalized_reason != 'verified_workflow_evidence':
                    raise CommonWorkError('new knowledge-only work requires verified Notion evidence')
                conn.execute('BEGIN IMMEDIATE')
                old = conn.execute("SELECT common_id FROM knowledge_refs WHERE ref_type=? AND external_id=? AND unlinked_at IS NULL", (normalized_type, normalized_id)).fetchall()
                if len(old) > 1:
                    raise CommonWorkError('Notion page belongs to multiple works')
                if old:
                    return self.get(old[0]['common_id'])
                common_id = self._new_id()
                conn.execute("INSERT INTO common_work (common_id,title,status,created_at) VALUES (?,?,'active',?)", (common_id, new_work_title.strip(), now))
                self._event(conn, common_id, 'created', {'knowledge_only': True, 'receipt_id': normalized_receipt})
            if conn.execute(
                "SELECT 1 FROM common_work WHERE common_id = ?", (common_id,)
            ).fetchone() is None:
                raise CommonWorkError(f"unknown common_id: {common_id}")
            try:
                conn.execute(
                    """
                    INSERT INTO knowledge_refs (
                        ref_id, common_id, ref_type, external_id, relation_type,
                        authority, link_reason, source_session_id, receipt_id,
                        metadata_json, verified_at, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        identifier,
                        common_id,
                        normalized_type,
                        normalized_id,
                        normalized_relation,
                        authority,
                        normalized_reason,
                        normalized_session,
                        normalized_receipt,
                        json.dumps(dict(metadata or {}), ensure_ascii=False, sort_keys=True),
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise CommonWorkError("knowledge reference is already linked") from exc
            self._event(
                conn,
                common_id,
                "knowledge_ref_linked",
                {
                    "ref_id": identifier,
                    "ref_type": normalized_type,
                    "external_id": normalized_id,
                    "relation_type": normalized_relation,
                    "link_reason": normalized_reason,
                    "source_session_id": normalized_session,
                    "receipt_id": normalized_receipt,
                },
            )
        return self.get(common_id)

    def unlink_knowledge_ref(
        self,
        common_id: str,
        *,
        ref_id: str,
        reason: str,
    ) -> dict[str, Any]:
        normalized_ref_id = str(ref_id or "").strip()
        normalized_reason = str(reason or "").strip()
        if not normalized_ref_id or not normalized_reason:
            raise CommonWorkError("ref_id and unlink reason are required")
        now = time.time()
        with self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE knowledge_refs
                SET unlinked_at = ?, unlink_reason = ?
                WHERE common_id = ? AND ref_id = ? AND unlinked_at IS NULL
                """,
                (now, normalized_reason, common_id, normalized_ref_id),
            )
            if cursor.rowcount != 1:
                raise CommonWorkError("active knowledge reference was not found")
            self._event(
                conn,
                common_id,
                "knowledge_ref_unlinked",
                {"ref_id": normalized_ref_id, "reason": normalized_reason},
            )
        return self.get(common_id)

    def begin_bundle_execution(
        self, *, bundle_key: str, payload_digest: str
    ) -> dict[str, Any] | None:
        normalized_key = str(bundle_key or "").strip()
        normalized_digest = str(payload_digest or "").strip()
        if not normalized_key or not normalized_digest:
            raise CommonWorkError("bundle key and payload digest are required")
        now = time.time()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT payload_digest, status, receipt_json "
                "FROM personal_bundle_executions WHERE bundle_key = ?",
                (normalized_key,),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO personal_bundle_executions "
                    "(bundle_key, payload_digest, status, receipt_json, created_at, updated_at) "
                    "VALUES (?, ?, 'executing', NULL, ?, ?)",
                    (normalized_key, normalized_digest, now, now),
                )
                return None
            if row["payload_digest"] != normalized_digest:
                raise CommonWorkError("bundle payload digest mismatch")
            if row["status"] == "executing":
                raise CommonWorkError("bundle execution is already in progress")
            receipt = json.loads(row["receipt_json"] or "{}")
            receipt["replayed"] = True
            return receipt

    def finish_bundle_execution(
        self,
        *,
        bundle_key: str,
        payload_digest: str,
        status: str,
        receipt: Mapping[str, Any],
    ) -> None:
        normalized_status = str(status or "").strip()
        if normalized_status not in {"completed", "partial", "failed"}:
            raise CommonWorkError("invalid bundle terminal status")
        with self._connect() as conn:
            cursor = conn.execute(
                "UPDATE personal_bundle_executions "
                "SET status = ?, receipt_json = ?, updated_at = ? "
                "WHERE bundle_key = ? AND payload_digest = ? AND status = 'executing'",
                (
                    normalized_status,
                    json.dumps(dict(receipt), ensure_ascii=False, sort_keys=True),
                    time.time(),
                    str(bundle_key),
                    str(payload_digest),
                ),
            )
            if cursor.rowcount != 1:
                raise CommonWorkError("bundle execution state changed before completion")

    def complete(
        self,
        common_id: str,
        *,
        approved: bool,
        reminder_completer: Callable[[str], Mapping[str, Any]] | None = None,
    ) -> dict[str, Any]:
        item = self.get(common_id)
        if item["status"] == "completed":
            return item
        reminders = [source for source in item["sources"] if source["domain"] == "todo"]
        if reminders and not approved:
            raise ApprovalRequired("linked Reminders require explicit completion approval")
        if reminders and reminder_completer is None:
            raise CommonWorkError("a receipt-producing reminder completer is required")

        receipts = []
        for source in reminders:
            reminder_id = source["source_id"]
            completion = dict(source["metadata"].get("completion") or {})
            if completion.get("status") == "completed":
                receipts.append(dict(completion.get("receipt") or {
                    "ok": True,
                    "source_id": reminder_id,
                    "status": "completed",
                }))
                continue
            if completion.get("status") == "in_progress":
                raise PartialCompletionError(
                    f"Reminder completion state is ambiguous: {reminder_id}",
                    item=self.get(common_id),
                )

            started_at = time.time()
            started_metadata = dict(source["metadata"])
            started_metadata["completion"] = {
                "status": "in_progress",
                "started_at": started_at,
            }
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE source_links SET metadata_json = ?
                    WHERE common_id = ? AND domain = 'todo' AND source_id = ?
                    """,
                    (json.dumps(started_metadata, ensure_ascii=False, sort_keys=True), common_id, reminder_id),
                )
                self._event(conn, common_id, "reminder_completion_started", {
                    "source_id": reminder_id,
                })

            receipt = dict(reminder_completer(reminder_id))  # type: ignore[misc]
            if receipt.get("ok") is not True:
                failed_metadata = dict(source["metadata"])
                failed_metadata["completion"] = {
                    "status": "failed",
                    "started_at": started_at,
                    "failed_at": time.time(),
                    "receipt": receipt,
                }
                with self._connect() as conn:
                    conn.execute(
                        """
                        UPDATE source_links SET metadata_json = ?
                        WHERE common_id = ? AND domain = 'todo' AND source_id = ?
                        """,
                        (json.dumps(failed_metadata, ensure_ascii=False, sort_keys=True), common_id, reminder_id),
                    )
                    self._event(conn, common_id, "reminder_completion_failed", {
                        "source_id": reminder_id,
                        "receipt": receipt,
                    })
                raise PartialCompletionError(
                    f"Reminder completion failed: {reminder_id}",
                    item=self.get(common_id),
                )
            completed_metadata = dict(source["metadata"])
            completed_metadata["completion"] = {
                "status": "completed",
                "started_at": started_at,
                "completed_at": time.time(),
                "receipt": receipt,
            }
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE source_links SET metadata_json = ?
                    WHERE common_id = ? AND domain = 'todo' AND source_id = ?
                    """,
                    (json.dumps(completed_metadata, ensure_ascii=False, sort_keys=True), common_id, reminder_id),
                )
                self._event(conn, common_id, "reminder_completed", {
                    "source_id": reminder_id,
                    "receipt": receipt,
                })
            receipts.append(receipt)

        now = time.time()
        with self._connect() as conn:
            conn.execute(
                "UPDATE common_work SET status = 'completed', completed_at = ? WHERE common_id = ?",
                (now, common_id),
            )
            self._event(
                conn,
                common_id,
                "completed",
                {
                    "reminder_receipts": receipts,
                    "preserved_domains": sorted({
                        source["domain"] for source in item["sources"]
                        if source["domain"] != "todo"
                    }),
                },
            )
        return self.get(common_id)
