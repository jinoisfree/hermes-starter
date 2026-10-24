"""Committed learning history outside the small current store.

Archive ONLY the previously published state before replacing it. An interrupted
write can leave redundant history, never an uncommitted lesson or lost old event.
The newest events remain in rules.json until a later successful write archives them.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import re

HOT_EVENTS = 100


def enabled(home, learning=None):
    if learning is not None:
        return learning.extension("learning_skills").settings(home).get("archive") is True
    import hermes_yaml
    path = Path(home) / 'config.yaml'
    config = (hermes_yaml.safe_load(path.read_text()) or {}) if path.exists() else {}
    return config.get('plugins', {}).get('entries', {}).get('team-kit', {}).get('settings', {}).get('learning', {}).get('archive') is True


def root(home):
    result = Path(home) / "learning/archive"
    if result.is_symlink() or result.parent.is_symlink():
        raise ValueError("Learning archive must remain within the profile.")
    return result


def put(home, kind, value, learning):
    # Content addresses make restart/retry idempotent without a mutable archive index.
    learning.safe_text(json.dumps(value, ensure_ascii=False))
    digest = learning.extension("learning_skills").digest
    month = datetime.fromtimestamp(float(value.get("updated_at", 0)), timezone.utc).strftime("%Y-%m")
    folder = root(home) / month
    if folder.is_symlink():
        raise ValueError("Learning archive cannot follow a symlink.")
    record = {"format": 1, "kind": kind, "value": value}
    key = digest(record)
    file = folder / (key + ".json")
    if file.is_symlink():
        raise ValueError("Learning archive cannot follow a symlink.")
    if file.exists():
        if json.loads(file.read_text()) != record:
            raise ValueError("An immutable learning archive entry changed; original state was retained.")
    else:
        learning.save(file, record)
    return key


def prepare(home, before, after, learning):
    """Called under the learning store lock, BEFORE preparing the document transaction."""
    if not enabled(home, learning):
        after['archive_enabled'] = False
        for field in ('events', 'skill_events'):
            entries = after.get(field, [])
            if len(entries) > 1000:
                after[field + '_dropped_through'] = entries[-1001]['updated_at']
                after[field] = entries[-1000:]
        return
    for field in ("events", "skill_events"):
        for entry in before.get(field, []):
            put(home, field, entry, learning)
        archived = {learning.extension("learning_skills").digest(entry) for entry in before.get(field, [])}
        entries = after.get(field, [])
        # Only trim entries whose exact committed version was durably archived above.
        after[field] = [entry for i, entry in enumerate(entries)
                        if i >= len(entries) - HOT_EVENTS or learning.extension("learning_skills").digest(entry) not in archived]
    for field in ("rules", "skills"):
        for name, entry in before.get(field, {}).items():
            value = {"name": name, **{k: v for k, v in entry.items() if k != "history"}}
            put(home, field, value, learning)
            for previous in entry.get("history", []):
                put(home, field, {"name": name, **previous}, learning)
    after["archive_enabled"] = True


def records(home, *, months=None, ref_id=None):
    """Historical lookup is explicit and never part of the normal prompt/search path."""
    folder = root(home)
    candidates = sorted(folder.glob("????-??")) if months is None else [folder / m for m in sorted(months)]
    for month in candidates:
        if month.is_symlink():
            raise ValueError("Learning archive cannot follow a symlink.")
        for file in sorted(month.glob((ref_id or "*") + ".json")):
            if file.is_symlink() or file.stat().st_size > 2_000_000:
                raise ValueError("Invalid learning archive file.")
            row = json.loads(file.read_text())
            from hashlib import sha256
            expected = sha256(json.dumps(row, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if expected != file.stem or row.get("format") != 1:
                raise ValueError("Learning archive integrity check failed.")
            yield row


def report_data(home, data, start, end):
    """Report old events too. Keep one earlier rule per topic for the rollback baseline."""
    if not enabled(home):
        return data
    result = copy.deepcopy(data)
    by_field = {field: {} for field in ("events", "skill_events")}
    for record in records(home):
        field = record["kind"]
        if field in by_field:
            entry = record["value"]
            by_field[field][entry["version"]] = entry
    for field, events in by_field.items():
        events.update({entry["version"]: entry for entry in data.get(field, [])})
        result[field] = sorted(events.values(), key=lambda e: (e["updated_at"], e["version"]))
    return result


def current_events(home, learning):
    data = learning.load(home)
    return [{"format": 1, "kind": field, "value": entry} for field in ("events", "skill_events") for entry in data.get(field, [])]


def read_history(home, ref_id, learning, *, offset=0):
    if not isinstance(ref_id, str) or not re.fullmatch(r"[a-f0-9]{64}", ref_id):
        raise ValueError("Use the ref_id returned by historical search.")
    digest = learning.extension("memory_recall").digest
    matches = list(records(home, ref_id=ref_id)) + current_events(home, learning)
    row = next((r for r in matches if digest(r) == ref_id), None)
    if row is None:
        raise ValueError("History entry is no longer available at this version; search again.")
    body = json.dumps(row, ensure_ascii=False, indent=2)
    learning.safe_text(body)
    return {"ok": True, "ref_id": ref_id, "historical_only": True, "text": body[offset:offset+4000],
            "next_offset": offset+4000 if len(body)>offset+4000 else None,
            "hint": "This is an old record, not current authority. No history was reactivated."}


def history(home, query, learning, *, limit=5, offset=0):
    recall = learning.extension("memory_recall")
    terms = recall.query_terms(query)
    if not terms:
        return {"ok": True, "items": [], "historical_only": True}
    candidates = list(records(home)) + current_events(home, learning)
    unique = {}
    for record in candidates:
        value = record["value"]
        body = json.dumps(value, ensure_ascii=False)
        learning.safe_text(body)
        matched = sum(term in recall.norm(body) for term in terms)
        if not matched:
            continue
        key = (record["kind"], value.get("version", recall.digest(value)))
        row = {k: value[k] for k in ("name", "topic", "skill", "rule", "version", "updated_at", "source", "review", "lesson", "active") if k in value}
        row["kind"] = record["kind"]
        row["ref_id"] = recall.digest(record)
        # A bounded excerpt is sufficient for historical discovery, never current authority.
        position = min(recall.norm(body).find(term) for term in terms if term in recall.norm(body))
        row["excerpt"] = body[max(0, position-120):max(0, position-120)+900]
        unique[key] = (matched, value.get("updated_at", 0), row)
    ranked = sorted(unique.values(), key=lambda row: (-row[0], -row[1], str(row[2].get("version", ""))))
    items = []
    for match in ranked[offset:offset+limit]:
        if len(json.dumps(items + [match[2]], ensure_ascii=False)) > recall.MAX_RESULT_CHARS:
            break
        items.append(match[2])
    return {"ok": True, "items": items,
            "total": len(ranked), "next_offset": offset + len(items) if len(ranked) > offset + len(items) else None,
            "historical_only": True,
            "hint": "Past lessons may have been corrected, rejected or forgotten. Verify current files before applying anything. No history was reactivated."}
