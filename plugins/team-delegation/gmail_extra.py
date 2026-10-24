#!/usr/bin/env python3
"""Gmail calls the bundled google_api.py does not offer: attachments in and out, and cc.

Run as a subprocess with the interpreter that has the Google client. One JSON request on stdin,
one JSON answer on stdout; errors are a safe code on stderr with exit 1.
"""
from __future__ import annotations

import base64
import html
import json
import mimetypes
import re
import sys
from email.message import EmailMessage
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

try:
    from . import gmail_rules
except ImportError:
    import gmail_rules


ExtraError = gmail_rules.RuleError


def _parts(part: dict):
    yield part
    for child in part.get("parts") or []:
        yield from _parts(child)


def files(payload: dict) -> list[dict]:
    """Attached files of a message payload, in order. Inline images without a name are skipped."""
    found = []
    for part in _parts(payload):
        name = part.get("filename")
        body = part.get("body") or {}
        if name and (body.get("attachmentId") or body.get("data")):
            found.append({"filename": str(name), "size": int(body.get("size") or 0),
                          "mime": str(part.get("mimeType") or ""), "_body": body})
    return found


def listing(service, message_id: str) -> list[dict]:
    message = service.users().messages().get(userId="me", id=message_id, format="full").execute()
    return files(message.get("payload") or {})


def public(found: list[dict]) -> list[dict]:
    return [{key: value for key, value in item.items() if key != "_body"} for item in found]


def download(service, message_id: str, targets: list) -> list[dict]:
    """Save the i-th attachment to targets[i] (None skips it). The caller chose and checked the paths."""
    found = listing(service, message_id)
    if len(targets) != len(found):
        raise ExtraError("GMAIL_ATTACHMENTS_CHANGED")
    for item, target in zip(found, targets):
        if not target:
            continue
        data = item["_body"].get("data")
        if not data:
            data = service.users().messages().attachments().get(
                userId="me", messageId=message_id, id=item["_body"]["attachmentId"]).execute().get("data")
        if not isinstance(data, str):
            raise ExtraError("GMAIL_ATTACHMENT_UNREADABLE")
        try:
            Path(target).write_bytes(base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)))
        except OSError as exc:
            raise ExtraError("MAIL_ATTACHMENT_FOLDER_UNAVAILABLE") from exc
    return public(found)


def _decoded(part: dict) -> str:
    data = (part.get("body") or {}).get("data")
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace") if data else ""


def text(payload: dict) -> str:
    """The readable body: the plain part wherever it is nested, else the HTML part without its tags."""
    parts = [part for part in _parts(payload) if not part.get("filename")]
    for part in parts:
        if part.get("mimeType") == "text/plain" and (body := _decoded(part)).strip():
            return body
    for part in parts:
        if part.get("mimeType") == "text/html" and (body := _decoded(part)).strip():
            body = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", " ", body)
            body = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h[1-6])>", "\n", body)
            body = html.unescape(re.sub(r"<[^>]+>", " ", body))
            return re.sub(r"[ \t\xa0]+", " ", re.sub(r"\n\s*\n+", "\n\n", body)).strip()
    return ""


def thread(api, service, message_id: str, limit: int) -> list[dict]:
    """The conversation a message belongs to, oldest first: what came in and what was answered."""
    thread_id = service.users().messages().get(userId="me", id=message_id, format="minimal").execute()["threadId"]
    messages = service.users().threads().get(userId="me", id=thread_id, format="full").execute().get("messages") or []
    result = []
    for message in messages[-limit:]:
        headers = api._headers_dict(message)
        result.append({
            "id": message["id"], "threadId": message["threadId"],
            "from": headers.get("from", ""), "to": headers.get("to", ""),
            "subject": headers.get("subject", ""), "date": headers.get("date", ""),
            "sent_by_owner": "SENT" in (message.get("labelIds") or []),
            "body": text(message.get("payload") or {}),
            "attachments": [item["filename"] for item in files(message.get("payload") or {})],
        })
    return [{"earlier_messages_omitted": len(messages) - len(result)}, *result]


def build(request: dict) -> EmailMessage:
    message = EmailMessage()
    message["To"] = ", ".join(request["to"])
    if request.get("cc"):
        message["Cc"] = ", ".join(request["cc"])
    message["Subject"] = request["subject"]
    message.set_content(request["body"])
    for path in request.get("attachments") or []:
        file = Path(path)
        kind = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        main, _, sub = kind.partition("/")
        try:
            content = file.read_bytes()
        except OSError as exc:
            raise ExtraError("GMAIL_ATTACHMENT_NOT_READABLE") from exc
        message.add_attachment(content, maintype=main, subtype=sub, filename=file.name)
    return message


def send(service, request: dict) -> dict:
    raw = base64.urlsafe_b64encode(build(request).as_bytes()).decode()
    result = service.users().messages().send(userId="me", body={"raw": raw}).execute()
    if not result.get("id"):
        raise ExtraError("GMAIL_SEND_NOT_CONFIRMED")
    return {"status": "sent", "id": result["id"], "threadId": result.get("threadId", "")}


def labels(service) -> dict:
    rows = service.users().labels().list(userId="me").execute().get("labels") or []
    return {"user_labels": [{"id": row["id"], "name": row["name"]}
                            for row in rows if row.get("type") == "user"],
            "system_labels": [row["name"] for row in rows if row.get("type") == "system"]}


def user_label(service, label_id) -> str:
    # Accept only a current user label, never TRASH/SPAM or any other system label.
    if not isinstance(label_id, str) or not label_id.strip():
        raise ExtraError("GMAIL_USER_LABEL_REQUIRED")
    if not any(row["id"] == label_id for row in labels(service)["user_labels"]):
        raise ExtraError("GMAIL_USER_LABEL_REQUIRED")
    return label_id


def label_create(service, name) -> dict:
    if not isinstance(name, str) or not name.strip():
        raise ExtraError("GMAIL_LABEL_NAME_REQUIRED")
    name = name.strip()
    existing = labels(service)
    for row in existing["user_labels"]:
        if row["name"].casefold() == name.casefold():
            return {**row, "created": False}
    if name.casefold() in {n.casefold() for n in existing["system_labels"]}:
        raise ExtraError("GMAIL_USER_LABEL_REQUIRED")
    result = service.users().labels().create(userId="me", body={"name": name}).execute()
    return {"id": result["id"], "name": result["name"], "created": True}


def _flag(request: dict, name: str) -> bool:
    value = request.get(name, False)
    if not isinstance(value, bool):
        raise ExtraError("GMAIL_INVALID_BOOLEAN")
    return value


def message_ids(service, query: str) -> list[str]:
    """Read the bounded snapshot before any writes. The estimate is not an exact count."""
    ids, seen_pages, page = set(), set(), None
    while True:
        options = {"userId": "me", "q": query, "maxResults": 500}
        if page:
            options["pageToken"] = page
        result = service.users().messages().list(**options).execute()
        ids.update(row["id"] for row in result.get("messages") or [])
        if len(ids) > 500:
            raise ExtraError("GMAIL_LABEL_APPLY_LIMIT_EXCEEDED")
        page = result.get("nextPageToken")
        if not page:
            break
        if page in seen_pages:
            raise ExtraError("GMAIL_LABEL_QUERY_PAGINATION_FAILED")
        seen_pages.add(page)
    return sorted(ids)


def apply_ids(service, ids, label_id, skip_inbox):
    if ids:
        body = {"ids": ids, "addLabelIds": [label_id]}
        if skip_inbox:
            body["removeLabelIds"] = ["INBOX"]
        service.users().messages().batchModify(userId="me", body=body).execute()


def label_apply(service, request: dict) -> dict:
    gmail_rules.validate_operation("label_apply", request)
    label_id = user_label(service, request.get("label_id"))
    skip_inbox = _flag(request, "skip_inbox")
    ids = message_ids(service, request["query"])
    apply_ids(service, ids, label_id, skip_inbox)
    # Gmail batchModify acknowledges the batch, without a per-message changed count.
    return {"label_id": label_id, "matched_count": len(ids), "modified_count": len(ids),
            "count_basis": "batchModify accepted message IDs", "skip_inbox": skip_inbox}


def inbox_restore(service, request: dict) -> dict:
    gmail_rules.validate_operation("inbox_restore", request)
    # Group the caller's OR expressions so the exclusions apply to every match.
    query = "-in:trash -in:spam (" + request["query"] + ")"
    ids = message_ids(service, query)
    if ids:
        service.users().messages().batchModify(userId="me", body={"ids": ids, "addLabelIds": ["INBOX"]}).execute()
    return {"matched_count": len(ids), "modified_count": len(ids),
            "count_basis": "batchModify accepted message IDs"}


def filter_create(service, request: dict) -> dict:
    # Build the action ourselves. Reject hidden/raw action fields even outside schema validation.
    if set(request) - {"op", "root", "token_home", "criteria", "label_id", "skip_inbox", "mark_read"}:
        raise ExtraError("GMAIL_FILTER_ACTION_REFUSED")
    gmail_rules.validate_operation("filter_create", request)
    criteria = request["criteria"]
    action = filter_action(user_label(service, request.get("label_id")), request)
    result = service.users().settings().filters().create(
        userId="me", body={"criteria": criteria, "action": action}).execute()
    return {"id": result["id"], "criteria": criteria, "action": action, "created": True}


def filter_action(label_id, request):
    action = {"addLabelIds": [label_id]}
    remove = []
    if _flag(request, "skip_inbox"):
        remove.append("INBOX")
    if _flag(request, "mark_read"):
        remove.append("UNREAD")
    if remove:
        action["removeLabelIds"] = remove
    return action


def _filter_key(criteria, action):
    # Gmail may omit empty/default fields and reorder label ID lists on reads.
    criteria = {k: v for k, v in criteria.items() if v not in (None, "", False)}
    action = {k: sorted(v) if k in {"addLabelIds", "removeLabelIds"} else v
              for k, v in action.items() if v not in (None, "", [])}
    return json.dumps([criteria, action], sort_keys=True, separators=(",", ":"))


def organize(service, request: dict) -> dict:
    """Preflight everything, then delete filters, classify and restore, with partial receipts."""
    results, mutation_calls, index, stage, error_op = [], 0, None, "validation", "organize"
    deleted_filters, restored, failed_filter_id, failed_restore = [], [], None, None
    try:
        if set(request) - {"op", "root", "token_home", "rules", "delete_filter_ids", "restore_inbox_queries"}:
            raise ExtraError("GMAIL_FILTER_ACTION_REFUSED")
        gmail_rules.validate_operation("mail_organize", request)
        rules = request.get("rules", [])
        delete_ids = list(dict.fromkeys(request.get("delete_filter_ids", [])))
        restore_queries = list(dict.fromkeys(request.get("restore_inbox_queries", [])))
        stage, error_op = "labels_preflight", "labels"
        existing = labels(service) if rules else {"user_labels": [], "system_labels": []}
        names = {row["name"].casefold(): row["id"] for row in existing["user_labels"]}
        systems = {name.casefold() for name in existing["system_labels"]}
        snapshots = []
        for index, rule in enumerate(rules, 1):
            stage, error_op = "labels_preflight", "labels"
            if rule["label_name"].strip().casefold() in systems:
                raise ExtraError("GMAIL_USER_LABEL_REQUIRED")
            stage, error_op = "messages_preflight", "label_apply"
            query = gmail_rules.criteria_query(rule["criteria"])
            snapshots.append(message_ids(service, query) if rule.get("apply_existing", True) else [])
        index, stage, error_op = None, "filters_preflight", "filter_list"
        filters = (service.users().settings().filters().list(userId="me").execute().get("filter") or []) if rules or delete_ids else []
        current_filter_ids = {f["id"] for f in filters}
        for failed_filter_id in delete_ids:
            if failed_filter_id not in current_filter_ids:
                raise ExtraError("GMAIL_FILTER_NOT_FOUND")
        failed_filter_id = None
        restore_snapshots = []
        stage, error_op = "restore_preflight", "inbox_restore"
        for failed_restore, query in enumerate(restore_queries, 1):
            restore_snapshots.append(message_ids(service, "-in:trash -in:spam (" + query + ")"))
        failed_restore = None
        # A deleted matching filter must be recreated, not reported as reused.
        known_filters = {_filter_key(f.get("criteria") or {}, f.get("action") or {}): f["id"]
                         for f in filters if f["id"] not in delete_ids}
        stage, error_op = "filter_delete", "filter_delete"
        for failed_filter_id in delete_ids:
            row = {"id": failed_filter_id, "deleted": None}
            deleted_filters.append(row)
            mutation_calls += 1
            row.update(filter_delete(service, failed_filter_id))
        failed_filter_id = None
        for index, (rule, ids) in enumerate(zip(rules, snapshots), 1):
            name = rule["label_name"].strip()
            row = {"rule_index": index, "label_name": name, "label_id": names.get(name.casefold()),
                   "label_created": False, "filter_id": None, "filter_created": False,
                   "matched_count": len(ids), "modified_count": 0}
            results.append(row)
            stage, error_op = "label_create", "label_create"
            if row["label_id"] is None:
                mutation_calls += 1
                row["label_created"] = None  # A failed request may still have reached Gmail.
                made = service.users().labels().create(userId="me", body={"name": name}).execute()
                row.update(label_id=made["id"], label_created=True)
                names[name.casefold()] = made["id"]
            stage, error_op = "filter_create", "filter_create"
            action = filter_action(row["label_id"], rule)
            key = _filter_key(rule["criteria"], action)
            if key in known_filters:
                row["filter_id"] = known_filters[key]
            else:
                mutation_calls += 1
                row["filter_created"] = None
                made = service.users().settings().filters().create(
                    userId="me", body={"criteria": rule["criteria"], "action": action}).execute()
                row.update(filter_id=made["id"], filter_created=True)
                known_filters[key] = made["id"]
            stage, error_op = "label_apply", "label_apply"
            if ids:
                mutation_calls += 1
                row["modified_count"] = None
                apply_ids(service, ids, row["label_id"], rule.get("skip_inbox", False))
                row["modified_count"] = len(ids)
            row["status"] = "completed"
        index, stage, error_op = None, "inbox_restore", "inbox_restore"
        for failed_restore, (query, ids) in enumerate(zip(restore_queries, restore_snapshots), 1):
            row = {"query": query, "matched_count": len(ids), "modified_count": 0}
            restored.append(row)
            if ids:
                mutation_calls += 1
                row["modified_count"] = None
                service.users().messages().batchModify(userId="me", body={"ids": ids, "addLabelIds": ["INBOX"]}).execute()
                row["modified_count"] = len(ids)
            row["status"] = "completed"
        return {"ok": True, "status": "completed", "results": results, "mutation_calls": mutation_calls,
                "deleted_filters": deleted_filters, "restored": restored,
                "count_basis": "batchModify accepted message IDs"}
    except Exception as exc:
        return {"ok": False, "status": "partial" if mutation_calls else "failed",
                "results": results, "deleted_filters": deleted_filters, "restored": restored,
                "error": safe_error(exc, error_op), "failed_filter_id": failed_filter_id,
                "failed_restore": failed_restore,
                "failed_rule": getattr(exc, "rule_index", None) or index,
                "failed_stage": stage, "mutation_calls": mutation_calls}


def filter_delete(service, filter_id) -> dict:
    if not isinstance(filter_id, str) or not filter_id.strip():
        raise ExtraError("GMAIL_FILTER_ID_REQUIRED")
    service.users().settings().filters().delete(userId="me", id=filter_id).execute()
    return {"id": filter_id, "deleted": True}


def safe_error(exc: Exception, operation: str) -> str:
    if isinstance(exc, (ExtraError, gmail_rules.RuleError)):
        return str(exc)
    # Inspect structured error reasons only; never print URLs, tokens or response text.
    if getattr(getattr(exc, "resp", None), "status", None) == 403:
        try:
            error = json.loads(exc.content).get("error") or {}
            reasons = {row.get("reason") for row in [*(error.get("errors") or []),
                                                     *(error.get("details") or [])]}
        except (ValueError, TypeError, AttributeError):
            reasons = set()
        if reasons & {"insufficientPermissions", "ACCESS_TOKEN_SCOPE_INSUFFICIENT",
                      "insufficientAuthenticationScopes"}:
            scope = "gmail.settings.basic" if operation.startswith("filter_") else "gmail.modify"
            return "GMAIL_SCOPE_MISSING:" + scope
    return type(exc).__name__


def main() -> int:
    operation = ""
    try:
        request = json.loads(sys.stdin.read())
        root = Path(request["root"])
        sys.path.insert(0, str(root / "hermes-agent"))
        import gmail_watch
        api, service = gmail_watch.gmail_client(root, token_home=Path(request["token_home"])) if request.get("token_home") else gmail_watch.gmail_client(root)
        operation = request["op"]
        if operation == "details":
            message = service.users().messages().get(userId="me", id=request["message_id"], format="full").execute()
            payload = message.get("payload") or {}
            answer = {"attachments": public(files(payload)), "body": text(payload)}
        elif operation == "thread":
            answer = thread(api, service, request["message_id"], int(request["limit"]))
        elif operation == "attachments":
            answer = public(listing(service, request["message_id"]))
        elif operation == "download":
            answer = download(service, request["message_id"], request["targets"])
        elif operation == "send":
            answer = send(service, request)
        elif operation == "labels":
            answer = labels(service)
        elif operation == "label_create":
            answer = label_create(service, request.get("label_name"))
        elif operation == "label_apply":
            answer = label_apply(service, request)
        elif operation == "filter_list":
            answer = {"filters": service.users().settings().filters().list(userId="me").execute().get("filter") or []}
        elif operation == "filter_create":
            answer = filter_create(service, request)
        elif operation == "filter_delete":
            answer = filter_delete(service, request.get("filter_id"))
        elif operation == "organize":
            answer = organize(service, request)
        elif operation == "inbox_restore":
            answer = inbox_restore(service, request)
        else:
            raise ExtraError("GMAIL_EXTRA_BAD_OPERATION")
        print(json.dumps(answer, ensure_ascii=False))
        return 0
    except Exception as exc:
        # Client exceptions can carry credentials or mail data. Print a safe code only.
        code = safe_error(exc, operation)
        print("GMAIL_EXTRA_FAILED:" + code, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
