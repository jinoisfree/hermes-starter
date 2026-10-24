"""SQLite registry linking one personal work item to native source objects."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
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
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
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
                        **dict(source.get("metadata") or {}),
                        "link_reason": normalized_reason,
                    },
                }
            )
        if not normalized_sources:
            raise CommonWorkError("at least one native source ID is required")

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
        normalized_metadata = {**dict(metadata or {}), "link_reason": normalized_reason}
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
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("SELECT common_id FROM source_links WHERE domain='notes' AND source_id=?", (old_id,)).fetchall()
            if conn.execute("SELECT 1 FROM source_links WHERE domain='notes' AND source_id=?", (new_id,)).fetchone():
                if not rows:  # already moved (rollback retry after native failure)
                    return
                raise CommonWorkError("replacement note is already linked")
            conn.execute("UPDATE source_links SET source_id=? WHERE domain='notes' AND source_id=?", (new_id, old_id))
            for row in rows:
                self._event(conn, row["common_id"], "note_source_replaced", {"old_id": old_id, "new_id": new_id})

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
            conn.execute(
                "DELETE FROM source_links WHERE common_id = ? AND domain = ? AND source_id = ?",
                (common_id, normalized_domain, normalized_id),
            )
            self._event(conn, common_id, "rollback_unlinked", {
                "domain": normalized_domain,
                "source_id": normalized_id,
                "reason": "latest_change_rollback",
            })
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
        identifier = self._new_knowledge_ref_id()
        now = time.time()
        authority = KNOWLEDGE_REF_AUTHORITIES[normalized_type]
        with self._connect() as conn:
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
