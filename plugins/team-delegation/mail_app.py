"""Stand-in for Gmail: the same mailbox through the Mac Mail app.

Used only when the Gmail API cannot be reached. Reads the inbox and sends an already approved
message from that account. Never moves, flags or deletes.
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))
SCAN = 500
ID_PATTERN = r"m[0-9]{1,18}"

JXA = r"""
function accountBoxes(account) {
  const result = [];
  const excluded = /^(sent(?: messages| items| mail)?|drafts?|trash|deleted(?: messages| items)?|junk(?: e-mail| email)?|spam|archive|archives|보낸\s*(?:편지함|메일함|메일)|임시\s*(?:보관함|편지함|메일함)|휴지통|삭제된\s*항목|정크(?:\s*메일)?|스팸(?:\s*메일함)?|보관(?:함)?)$/i;
  function visit(boxes, depth) {
    if (depth > 20) throw new Error("MAIL_APP_MAILBOX_DEPTH");
    for (const box of boxes) {
      if (excluded.test(String(box.name()).trim())) continue;
      result.push(box);
      visit(box.mailboxes(), depth + 1);
    }
  }
  visit(account.mailboxes(), 0);
  const all = result.find(b => /^(전체\s*보관함|All Mail)$/i.test(b.name()));
  return all ? [all] : result;
}
function run(argv) {
  const a = JSON.parse(argv[0]);
  const Mail = Application("Mail");
  if (a.op === "accounts") return JSON.stringify({accounts: Mail.accounts().map(x => ({
    name: String(x.name()), addresses: x.emailAddresses().map(e => String(e).toLowerCase()), enabled: x.enabled()
  }))});
  const account = Mail.accounts().find(x => x.emailAddresses().map(e => e.toLowerCase()).includes(a.account));
  if (!account) return JSON.stringify({error: "MAIL_APP_NO_ACCOUNT"});
  const boxes = account.mailboxes();
  const inbox = boxes.find(b => /^(전체보관함|All Mail)$/i.test(b.name())) || boxes.find(b => /^inbox$/i.test(b.name()));
  const selected = a.all_mailboxes ? accountBoxes(account) : (inbox ? [inbox] : []);
  if (!selected.length && a.op !== "send") return JSON.stringify({error: "MAIL_APP_NO_INBOX"});
  if (a.op === "send") {
    const message = Mail.OutgoingMessage({subject: a.subject, content: a.body, visible: false, sender: a.account});
    Mail.outgoingMessages.push(message);
    for (const address of a.to) message.toRecipients.push(Mail.Recipient({address: address}));
    for (const address of a.cc) message.ccRecipients.push(Mail.CcRecipient({address: address}));
    for (const file of a.attachments) message.attachments.push(Mail.Attachment({fileName: Path(file)}));
    if (a.attachments.length) delay(2 + a.attachments.length);  // Mail attaches asynchronously.
    const attached = message.attachments.length;
    if (attached !== a.attachments.length) return JSON.stringify({error: "MAIL_APP_ATTACH_FAILED"});
    return JSON.stringify({sent: message.send() === true, recipients: a.to.length + a.cc.length, attached: attached});
  }

  const meta = m => ({id: "m" + m.id(), rfc_id: m.messageId(), from: m.sender(), subject: m.subject(),
                      ts: Math.floor(m.dateReceived().getTime() / 1000)});
  if (a.op === "read" || a.op === "download") {
    const hit = selected.flatMap(box => box.messages.whose({id: a.id})());
    if (!hit.length) return JSON.stringify({error: "MAIL_APP_NOT_FOUND"});
    const item = meta(hit[0]);
    const files = hit[0].mailAttachments();
    item.attachments = files.map(f => ({filename: f.name(), size: f.fileSize() || 0}));
    if (a.op === "read") {
      item.body = hit[0].content() || "";
      return JSON.stringify({item: item});
    }
    // a.targets[i] is the checked destination for the i-th attachment, or null to skip it.
    files.forEach((f, i) => { if (a.targets[i]) Mail.save(f, {in: Path(a.targets[i])}); });
    return JSON.stringify({item: item});
  }
  // Batch metadata per mailbox, then sort/deduplicate across the account.
  const rows = [];
  for (const box of selected) {
    const messages = box.messages;
    const ids = messages.id(), dates = messages.dateReceived(), senders = messages.sender(), subjects = messages.subject();
    const order = ids.map((_, i) => i).sort((x,y) => dates[y]-dates[x]).slice(0,a.scan);
    for (const i of order) rows.push({id: "m"+ids[i], from:senders[i]||"", subject:subjects[i]||"",
      ts:Math.floor(dates[i].getTime()/1000), message:messages[i]});
  }
  rows.sort((x,y) => y.ts-x.ts);
  const items = [], seen = new Set();
  for (const row of rows) {
    if (items.length >= a.limit) break;
    if (seen.has(row.id)) continue;
    seen.add(row.id);
    if (a.since && row.ts < a.since) continue;
    if (row.from.toLowerCase().includes(a.account)) continue;
    if (!a.terms.every(t => (row.from+" "+row.subject).toLowerCase().includes(t))) continue;
    items.push({id:row.id, from:row.from, subject:row.subject, ts:row.ts, rfc_id:row.message.messageId()});
  }
  return JSON.stringify({items: items});
}
"""


class MailAppError(ValueError):
    """A safe error code, without mail contents."""


def valid_id(value) -> bool:
    return isinstance(value, str) and re.fullmatch(ID_PATTERN, value) is not None


def _call(payload: dict, runner=None) -> dict:
    execute = runner or subprocess.run
    try:
        proc = execute(["/usr/bin/osascript", "-l", "JavaScript", "-e", JXA, json.dumps(payload)],
                       capture_output=True, text=True, timeout=60, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MailAppError("MAIL_APP_UNAVAILABLE:" + type(exc).__name__) from exc
    if proc.returncode != 0:
        if "-1743" in str(getattr(proc, "stderr", "")):
            raise MailAppError("MAIL_APP_PERMISSION_DENIED")
        raise MailAppError("MAIL_APP_FAILED:" + str(proc.returncode))
    try:
        result = json.loads(str(proc.stdout or "").strip())
    except ValueError as exc:
        raise MailAppError("MAIL_APP_INVALID_RESPONSE") from exc
    if not isinstance(result, dict):
        raise MailAppError("MAIL_APP_INVALID_RESPONSE")
    if result.get("error"):
        raise MailAppError(str(result["error"]) if str(result["error"]).startswith("MAIL_APP_")
                           else "MAIL_APP_FAILED")
    return result


def _clean(item) -> dict:
    if (not isinstance(item, dict) or not valid_id(item.get("id")) or type(item.get("ts")) is not int):
        raise MailAppError("MAIL_APP_INVALID_RESPONSE")
    result = {"id": item["id"], "from": str(item.get("from") or ""), "subject": str(item.get("subject") or ""),
              "date": datetime.fromtimestamp(item["ts"], KST).isoformat(timespec="seconds"),
              "ts": item["ts"], "snippet": "", "source": "mail_app"}
    if isinstance(item.get("rfc_id"), str) and item["rfc_id"]:
        result["rfc_id"] = item["rfc_id"]
    return result


def _account(account) -> str:
    account = str(account or "").strip().casefold()
    if not account:
        raise MailAppError("MAIL_APP_ACCOUNT_NOT_SET")
    return account


def recent(account: str, *, days: int = 2, limit: int = 100, runner=None) -> list[dict]:
    payload = {"op": "list", "account": _account(account), "since": int(time.time()) - days * 86400,
               "terms": [], "limit": limit, "scan": SCAN}
    items = _call(payload, runner).get("items")
    if not isinstance(items, list):
        raise MailAppError("MAIL_APP_INVALID_RESPONSE")
    return [_clean(item) for item in items]


def query_terms(query: str) -> tuple[list[str], int]:
    """Plain words and the newer_than window of a Gmail query; other operators are dropped."""
    since, terms = 0, []
    for token in str(query or "").split():
        window = re.fullmatch(r"newer_than:(\d{1,3})([dhmy])", token)
        if window:
            seconds = {"h": 3600, "d": 86400, "m": 30 * 86400, "y": 365 * 86400}[window.group(2)]
            since = int(time.time()) - int(window.group(1)) * seconds
        elif ":" not in token and not token.startswith("-") and token.upper() not in {"OR", "AND"}:
            word = token.strip("\"'()").casefold()
            if word:
                terms.append(word)
    return terms, since


def search(account: str, query: str, limit: int, runner=None, *, all_mailboxes=False) -> list[dict]:
    terms, since = query_terms(query)
    payload = {"op": "list", "account": _account(account), "since": since, "terms": terms,
               "limit": limit, "scan": SCAN, "all_mailboxes": all_mailboxes}
    items = _call(payload, runner).get("items")
    if not isinstance(items, list):
        raise MailAppError("MAIL_APP_INVALID_RESPONSE")
    return [_clean(item) for item in items]


def read(account: str, message_id: str, runner=None, *, all_mailboxes=False) -> dict:
    if not valid_id(message_id):
        raise MailAppError("MAIL_APP_INVALID_ID")
    raw = _call({"op": "read", "account": _account(account), "id": int(message_id[1:]), "all_mailboxes": all_mailboxes}, runner).get("item")
    item = _clean(raw)
    if item["id"] != message_id or not isinstance(raw.get("body"), str):
        raise MailAppError("MAIL_APP_INVALID_RESPONSE")
    item["body"] = raw["body"]
    item["attachments"] = _attachments(raw)
    return item


def _attachments(raw: dict) -> list[dict]:
    files = raw.get("attachments", [])
    if not isinstance(files, list) or not all(
            isinstance(f, dict) and isinstance(f.get("filename"), str) and type(f.get("size")) is int for f in files):
        raise MailAppError("MAIL_APP_INVALID_RESPONSE")
    return [{"filename": f["filename"], "size": f["size"]} for f in files]


def attachments(account: str, message_id: str, runner=None, *, all_mailboxes=False) -> list[dict]:
    return read(account, message_id, runner, all_mailboxes=all_mailboxes)["attachments"]


def download(account: str, message_id: str, targets: list, runner=None, *, all_mailboxes=False) -> None:
    """Save the i-th attachment to targets[i] (an absolute path, or None to skip). The caller picks the paths."""
    if not valid_id(message_id):
        raise MailAppError("MAIL_APP_INVALID_ID")
    _call({"op": "download", "account": _account(account), "id": int(message_id[1:]),
           "targets": [str(t) if t else None for t in targets], "all_mailboxes": all_mailboxes}, runner)


def recipients(to) -> list[str]:
    addresses = [part.strip() for part in re.split(r"[,;]", str(to or "")) if part.strip()]
    if not addresses or len(addresses) > 20 or not all(
            re.fullmatch(r"[^\s<>@,;\"']+@[^\s<>@,;\"']+\.[^\s<>@,;\"']+", address) for address in addresses):
        raise MailAppError("MAIL_APP_INVALID_RECIPIENT")
    return addresses


def send(account: str, to: str, subject: str, body: str, runner=None, *, cc: str = "",
         attachments: list | None = None) -> dict:
    """Send one plain-text message from the account. The caller owns the approval and the file checks."""
    addresses = recipients(to)
    copies = recipients(cc) if str(cc or "").strip() else []
    files = [str(path) for path in attachments or []]
    if not str(subject or "").strip() or not str(body or "").strip():
        raise MailAppError("MAIL_APP_SEND_FIELDS_REQUIRED")
    result = _call({"op": "send", "account": _account(account), "to": addresses, "cc": copies,
                    "attachments": files, "subject": str(subject), "body": str(body)}, runner)
    if (result.get("sent") is not True or result.get("recipients") != len(addresses) + len(copies)
            or result.get("attached") != len(files)):
        raise MailAppError("MAIL_APP_SEND_FAILED")
    return {"status": "sent", "via": "mail_app", "from": _account(account), "to": addresses,
            **({"cc": copies} if copies else {}), **({"attachments": len(files)} if files else {})}


def accounts(runner=None) -> list[dict]:
    rows = _call({"op": "accounts"}, runner).get("accounts")
    if not isinstance(rows, list) or any(not isinstance(x, dict) or
        not isinstance(x.get("name"), str) or type(x.get("enabled")) is not bool or
        not isinstance(x.get("addresses"), list) or any(not isinstance(a,str) for a in x["addresses"])
        for x in rows):
        raise MailAppError("MAIL_APP_INVALID_RESPONSE")
    return [{"name":x["name"], "addresses":[a.casefold() for a in x["addresses"]], "enabled":x["enabled"]} for x in rows]
