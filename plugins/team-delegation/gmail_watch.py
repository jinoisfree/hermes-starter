"""Read-only Gmail watch helpers and profile-local acknowledgement state."""
from __future__ import annotations

import fcntl
import hashlib
import importlib.util
import json
import os
import re
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

# Not "in:inbox": filters file mail under a label without it reaching the inbox, and that mail
# matters just as much. Search already leaves out spam and trash.
QUERY = "newer_than:2d -in:sent -in:draft -in:chats -category:promotions -category:social"
# Mail the owner sent: only offered when it belongs to a linked work (mail_watch filters it).
SENT_QUERY = "newer_than:2d in:sent -in:chats"
SENT_STATE = "mail-watch-sent.json"
MAX_PASSES = 2
BATCH_SIZE = 20
# Gmail ids are hex; the Mail app stand-in uses "m" + its own number.
ID_PATTERN = r"(?:[0-9a-fA-F]{1,32}|m[0-9]{1,18})"
THREAD_PATTERN = r"[0-9a-fA-F]{1,32}"


class WatchError(ValueError):
    """A safe error code, without credentials or mail contents."""


def valid_id(value) -> bool:
    return isinstance(value, str) and re.fullmatch(ID_PATTERN, value) is not None


def normalized_account(value) -> str:
    if not isinstance(value, str):
        raise WatchError("GMAIL_ACCOUNT_INVALID")
    account = value.strip().casefold()
    if account and not re.fullmatch(r"[^\s<>/?#|]+@[^\s<>/?#|]+\.[^\s<>/?#|]+", account):
        raise WatchError("GMAIL_ACCOUNT_INVALID")
    return account


def normalized_rfc_id(value) -> str:
    """Comparable Message-ID, without brackets; never use mail content as a URL."""
    if not isinstance(value, str):
        return ""
    value = value.strip().removeprefix("<").removesuffix(">").casefold()
    return value if re.fullmatch(r"[^\s<>\"']{1,300}@[^\s<>\"']{1,300}", value) and len(value) <= 300 else ""


def company_state_name(account: str) -> str:
    account = normalized_account(account)
    safe = re.sub(r"[^a-z0-9._-]", "_", account)[:100]
    return "mail-watch-mailapp-" + safe + "-" + hashlib.sha256(account.encode()).hexdigest()[:12] + ".json"


def _link_base(account: str) -> str:
    account = normalized_account(account)
    return ("https://mail.google.com/mail/u/?authuser=" + quote(account, safe="")
            if account else "https://mail.google.com/mail/u/0/")


def mail_link(thread_id, account: str = "") -> str | None:
    if not isinstance(thread_id, str) or re.fullmatch(THREAD_PATTERN, thread_id) is None:
        return None
    return _link_base(account) + "#all/" + thread_id


def search_link(rfc_id, account: str = "") -> str | None:
    """Mail read through the Mail app has no Gmail thread id; find it by its Message-ID header."""
    if not isinstance(rfc_id, str) or re.fullmatch(r"<?[^\s<>\"']{3,300}>?", rfc_id) is None:
        return None
    return _link_base(account) + "#search/" + quote("rfc822msgid:" + rfc_id.strip("<>"), safe="")


def with_link(item: dict, account: str = "") -> dict:
    result = dict(item)
    # Never accept an address supplied by mail content or an API response.
    result.pop("link", None)
    if link := mail_link(item.get("threadId"), account) or search_link(item.get("rfc_id"), account):
        result["link"] = link
    return result


def _load(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict) or state.get("version") != 1:
            raise ValueError
        # Missing account is a legacy state: the next verified mailbox run re-baselines it.
        normalized_account(state.get("account", ""))
        messages = state["messages"]
        if not isinstance(messages, dict):
            raise ValueError
        for message_id, entry in messages.items():
            if (not valid_id(message_id) or not isinstance(entry, dict)
                    or type(entry.get("acknowledged")) is not bool
                    or type(entry.get("passes")) is not int
                    or not 0 <= entry["passes"] <= MAX_PASSES):
                raise ValueError
        return state
    except (ValueError, KeyError, TypeError, UnicodeError) as exc:
        raise WatchError("MAIL_WATCH_STATE_CORRUPT") from exc


def _save(path: Path, state: dict) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".mail-watch-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def locked_state(home: Path, name: str = "mail-watch.json"):
    path = home / "state" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    with (path.parent / (Path(name).stem + ".lock")).open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield path, _load(path)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


MESSAGE_RETENTION_DAYS = 30


def prune_messages(state):
    cutoff = time.time() - MESSAGE_RETENTION_DAYS * 86400
    retained = {}
    for key, row in state["messages"].items():
        try:
            stamp = datetime.fromisoformat(row["updated_at"])
            if stamp.tzinfo is None or stamp.timestamp() >= cutoff:
                retained[key] = row
        except (KeyError, ValueError, TypeError):
            retained[key] = row  # Unknown legacy dates must not cause repeated mail notices.
    state["messages"] = retained


def _entry(acknowledged=False, passes=0, reason="") -> dict:
    return {"acknowledged": acknowledged, "passes": passes, "reason": reason,
            "updated_at": datetime.now(timezone.utc).isoformat()}


def acknowledge(home: Path, ids: list[str]) -> list[str]:
    if (not isinstance(ids, list) or not 1 <= len(ids) <= 1000
            or not all(valid_id(value) for value in ids)):
        raise WatchError("GMAIL_INVALID_IDS")
    ids = list(dict.fromkeys(ids))
    secondary = [home / "state" / SENT_STATE, *sorted((home / "state").glob("mail-watch-mailapp-*.json"))]
    for secondary_path in secondary:
        if not secondary_path.exists():
            continue
        # One ack covers all watches, but secondary states only acknowledge IDs already offered there.
        with locked_state(home, secondary_path.name) as (path, state):
            known = [value for value in ids if state and value in state["messages"]]
            for message_id in known:
                state["messages"][message_id] = _entry(True, state["messages"][message_id].get("passes", 0), "agent_ack")
            if known:
                _save(path, state)
    with locked_state(home) as (path, state):
        if state is None:
            state = {"version": 1, "account": "", "messages": {}}
        for message_id in ids:
            previous = state["messages"].get(message_id, {})
            state["messages"][message_id] = _entry(True, previous.get("passes", 0), "agent_ack")
        prune_messages(state)
        _save(path, state)
    return ids


def poll(home: Path, fetch, account: str = "", *, mailbox: str = "", source: str = "gmail",
         state_name: str = "mail-watch.json", include_links: bool = True,
         exclude_rfc_ids: set[str] | None = None) -> dict:
    """source names where fetch reads from ("gmail" or "mail_app"); the two give one mail different ids."""
    mailbox = normalized_account(mailbox)
    account = normalized_account(account)
    if account and account != mailbox:
        return {"wakeAgent": False, "reason": "account_mismatch", "items": []}
    with locked_state(home, state_name) as (path, state):
        # Validate before contacting Gmail. Fetch is GET/list only.
        items = fetch()
        if not isinstance(items, list) or not all(
                isinstance(item, dict) and valid_id(item.get("id")) for item in items):
            raise WatchError("GMAIL_INVALID_WATCH_RESPONSE")
        items = list({item["id"]: item for item in items}.values())
        items = [with_link(item, account) if include_links else
                 {k: v for k, v in item.items() if k != "link"} for item in items]
        duplicates = {item["id"] for item in items if normalized_rfc_id(item.get("rfc_id"))
                      and normalized_rfc_id(item.get("rfc_id")) in (exclude_rfc_ids or set())}
        if state is None or normalized_account(state.get("account", "")) != mailbox:
            state = {"version": 1, "account": mailbox, "source": source, "checked_at": int(time.time()),
                     "messages": {item["id"]: _entry(True, reason="duplicate_of_gmail" if item["id"] in duplicates
                                                          else "initial_baseline") for item in items}}
            prune_messages(state)
            _save(path, state)
            return {"wakeAgent": False, "baseline_count": len(items), "items": []}
        if state.get("source", "gmail") != source:
            # The other source already handled everything up to its last check. Mail without a
            # usable time, or a state from before checks were timed, counts as handled too.
            checked = state.get("checked_at")
            for item in items:
                received = item.get("ts")
                if (item["id"] not in state["messages"] and not (
                        type(checked) is int and type(received) is int and received > checked)):
                    state["messages"][item["id"]] = _entry(True, reason="source_switch")
        state.update(source=source, checked_at=int(time.time()))
        pending = []
        for item in items:
            if item["id"] in duplicates:
                previous = state["messages"].get(item["id"], {})
                state["messages"][item["id"]] = _entry(True, previous.get("passes", 0), "duplicate_of_gmail")
                continue
            entry = state["messages"].get(item["id"], _entry())
            if entry["acknowledged"]:
                # Still visible in the polling window: keep its dedup receipt fresh.
                entry["updated_at"] = datetime.now(timezone.utc).isoformat()
                continue
            if entry["passes"] >= MAX_PASSES:
                state["messages"][item["id"]] = _entry(True, MAX_PASSES, "pass_limit")
                continue
            if len(pending) >= BATCH_SIZE:
                continue
            passes = entry["passes"] + 1
            state["messages"][item["id"]] = _entry(
                passes >= MAX_PASSES, passes, "pass_limit" if passes >= MAX_PASSES else "offered")
            pending.append({key: item[key] for key in
                            ("id", "threadId", "from", "to", "subject", "date", "snippet", "link", "work_matches",
                             "account", "source", "rfc_id", "references")
                            if key in item})
        prune_messages(state)
        _save(path, state)
    return {"wakeAgent": bool(pending), "items": pending}


def gmail_client(root: Path, *, token_home: Path | None = None):
    """Reuse the root Hermes OAuth client; callers keep this same client after checking identity."""
    script = root / "hermes-agent/skills/productivity/google-workspace/scripts/google_api.py"
    spec = importlib.util.spec_from_file_location("mail_watch_google_api", script)
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    # Cron is profile-bound; the connected OAuth credential belongs to the root home.
    api.HERMES_HOME = root
    api.TOKEN_PATH = (token_home or root) / "google_token.json"
    api.CLIENT_SECRET_PATH = root / "google_client_secret.json"
    return api, api.build_service("gmail", "v1")


def mailbox_address(service) -> str:
    account = normalized_account(service.users().getProfile(userId="me").execute().get("emailAddress"))
    if not account:
        raise WatchError("GMAIL_PROFILE_INVALID")
    return account


def fetch_recent(root: Path, *, client=None, query: str = QUERY) -> list[dict]:
    """Page metadata using the same OAuth client whose mailbox was already verified."""
    api, service = client if client is not None else gmail_client(root)
    messages = service.users().messages()
    items, page = [], None
    while True:
        parameters = {"userId": "me", "q": query, "maxResults": 100}
        if page:
            parameters["pageToken"] = page
        response = messages.list(**parameters).execute()
        for entry in response.get("messages", []):
            message = messages.get(userId="me", id=entry["id"], format="metadata",
                                   metadataHeaders=["From", "To", "Subject", "Date", "Message-ID"]).execute()
            headers = api._headers_dict(message)
            received = str(message.get("internalDate") or "")
            items.append({"id": message["id"], "threadId": message["threadId"],
                          **({"ts": int(received) // 1000} if received.isdigit() else {}),
                          "from": headers.get("from", ""), "subject": headers.get("subject", ""),
                          "rfc_id": headers.get("message-id", ""),
                          **({"to": headers["to"]} if query == SENT_QUERY and headers.get("to") else {}),
                          "date": headers.get("date", ""), "snippet": message.get("snippet", "")})
        page = response.get("nextPageToken")
        if not page:
            return items
