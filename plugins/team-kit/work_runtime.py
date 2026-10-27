"""Owner conversation handoffs, checkpoints and per-request model routing.

A and B use the SAME Hermes session and ordinary tool dispatcher. This module never
executes a business tool, grants an approval, or starts a worker. Checkpoint ids are
the existing owner session/message identity, not another business-object registry.
"""
from __future__ import annotations

from collections import OrderedDict
import copy
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import threading
import time

LOG = logging.getLogger("team_kit.work")
_TURNS = OrderedDict()
_LOCK = threading.RLock()
COMPLETED_RETENTION_DAYS = 30
_PENDING_CACHE = OrderedDict()
PENDING_CACHE_LIMIT = 512

PROVIDER = "claude-subscription-directsdk-experimental"
WORK_MODEL = "claude-opus-5-5"
INTERPRETER = EXECUTOR = WORK_MODEL
BACKGROUND_MODEL = "claude-sonnet-5-5"
READS = {"contacts_direct_lookup", "folder_scan", "doc_read", "pdf_render", "file_location", "vision_analyze", "notion", "slack_history", "calendar_direct_lookup", "reminders_direct_lookup", "apple_notes_direct_lookup", "messages_direct_lookup",
         "common_work_get", "knowledge_ref_get", "file_list", "read_file", "search_files", "web_search", "web_extract",
         "browser_snapshot", "browser_navigate", "browser_scroll", "browser_back", "kanban_show", "kanban_list"}
READS |= {"deadline_watch", "google_calendar_color_get", "work_crosscheck", "mail_waiting", "doc_compare", "table_analyze",
          "mail_read", "calendar_read", "notes_read", "reminders_read", "file_link"}
SIMPLE_TOOLS = {"calendar_direct_lookup", "reminders_direct_lookup"}
BUSINESS = READS | {"contacts_direct_mutation", "file_move", "file_copy", "folder_create", "doc_edit", "pdf_pages", "gmail_direct", "personal_work_bundle", "personal_work_rollback", "google_calendar_color_set",
                    "calendar_direct_mutation", "reminders_direct_mutation", "apple_notes_direct_mutation",
                    "common_work_create", "common_work_link", "common_work_complete", "knowledge_ref_link", "knowledge_ref_unlink",
                    "browser_click", "browser_type", "browser_press", "write_file", "patch", "file_trash",
                    "claude_code_artifact", "kanban_create", "kanban_unblock", "kanban_comment", "paseo"}
BUSINESS |= {"doc_convert", "doc_fill", "task_reminders_sync"}
PREFLIGHT = {"common_work_get", "kanban_show", "kanban_list"}
UNGATED_READS = {"session_search", "skill_view", "skills_list", "team_recall"}
# 2026-10-21 사용자 결정(선 작성 후 보고): 일정·미리알림 단건 쓰기는 A가 조회 후 바로 실행하고,
# 업무 기록은 실제 도구 결과로 자동 작성한다. 승인 버튼·범위 검사는 각 도구가 그대로 맡는다.
DIRECT = {"task_reminders_sync", "contacts_direct_lookup", "contacts_direct_mutation", "calendar_direct_lookup", "reminders_direct_lookup", "google_calendar_color_get",
          "calendar_direct_mutation", "google_calendar_color_set", "reminders_direct_mutation", "personal_work_bundle"}
# 2026-10-26 사용자 결정: '○○ 완료처리'는 A가 조회 뒤 바로 끝낸다. 메모 조회, 연결 업무 완료,
# 단독 메모의 complete 폴더 이동만 더한다(그 밖의 메모 쓰기는 계속 team_work 인계).
DIRECT |= {"apple_notes_direct_lookup", "common_work_get", "common_work_complete"}
DIRECT_NOTE = (" 일정·미리알림 단건 생성·수정·색 지정·삭제와 연락처 단건·묶음 쓰기는 A가 필요한 조회 뒤 바로 처리한다. 추가성 쓰기는 선 작성 후 보고다. 연락처 create/import_vcf/group_create/group_add는 즉시 실행하며 update/delete/group_rename/group_delete/group_remove는 승인 버튼을 유지한다. team_work(start) 없이 A가 필요한 조회 뒤 "
               "바로 실행하고 결과로 답한다. 업무 기록은 실제 도구 결과로 자동 작성되므로 team_work를 부르지 않는다. "
               "업무비서 할 일 등록 체크리스트와 선택 뒤 묶음·노션·연결은 A가 같은 턴에 처리한다. "
               "소유자가 업무·일정·메모를 완료처리하라고 하면 A가 찾은 원본의 common_id를 common_work_get으로 확인하고 common_work_complete 한 번으로 끝낸다(메모 본문을 고쳐 완료를 흉내 내지 않는다). "
               "연결 업무가 없는 단독 메모만 apple_notes_direct_mutation(action=move, folder=complete)로 옮긴다. 그 밖에 여러 담당자·선행 조건·메일·파일·Notion이 얽힌 일은 team_work(start)로 B에게 넘긴다.")
LEARNING = {"memory", "team_learn", "team_learn_review", "team_skill_review", "skill_manage"}
GMAIL_WRITES = frozenset({"send", "label_create", "label_apply", "filter_create", "filter_delete", "mail_organize", "inbox_restore"})
GMAIL_INPUT_ERRORS = frozenset({
    "GMAIL_LABEL_NAME_REQUIRED", "GMAIL_QUERY_REQUIRED", "GMAIL_LABEL_ID_REQUIRED",
    "GMAIL_CRITERIA_REQUIRED", "GMAIL_FILTER_CRITERIA_INVALID", "GMAIL_FILTER_ID_REQUIRED",
    "GMAIL_SKIP_INBOX_REQUIRED", "GMAIL_MARK_READ_REQUIRED", "GMAIL_APPLY_EXISTING_REQUIRED",
    "GMAIL_RULES_REQUIRED", "GMAIL_RULE_INVALID", "GMAIL_FILTER_ACTION_REFUSED",
    "GMAIL_DELETE_FILTER_IDS_REQUIRED", "GMAIL_RESTORE_INBOX_QUERIES_REQUIRED",
})
SIMPLE = re.compile(r"(?:(?:안녕|안녕하세요|고마워|감사합니다)[.!~ ]*|"
                    r"(?:오늘|내일|이번\s*주)\s*(?:내\s*)?(?:일정|캘린더|할\s*일|미리\s*알림)"
                    r"(?:(?:을|를)?\s*(?:좀\s*)?(?:알려\s*줘|보여\s*줘|확인해\s*줘|조회해\s*줘|알려주세요|보여주세요|확인해주세요|조회해주세요))?[?.!~ ]*)")


def completion_move(tool, args):
    """Moving a memo the owner called finished into 'complete' (2026-10-26) is A's direct write."""
    args = args or {}
    return (tool == "apple_notes_direct_mutation" and str(args.get("action") or "").strip() == "move"
            and str(args.get("folder") or "").strip() == "complete")


def direct_tools(home, learning, turn):
    extra = learning.extension('task_capture').DIRECT if turn and turn.get('task_capture') else set()
    return DIRECT | extra


def settings(home, learning):
    value = learning.extension("memory_recall").config(home).get("work", {})
    return value if isinstance(value, dict) and value.get("enabled") is True else {}


def key(home, turn_id):
    return (str(Path(home).resolve()), str(turn_id))


def binding(home, turn_id):
    with _LOCK:
        return _TURNS.get(key(home, turn_id)) if turn_id else None


def folder(home):
    path = Path(home) / "state/team-work"
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("Work checkpoints must remain in the profile.")
    return path


def path(home, case_id):
    if not isinstance(case_id, str) or not re.fullmatch(r"[a-f0-9]{20}", case_id):
        raise ValueError("Use a case_id returned by team_work.")
    file = folder(home) / (case_id + ".json")
    if file.is_symlink():
        raise ValueError("Work checkpoints cannot follow links.")
    return file


def read(home, case_id, owner_id):
    file = path(home, case_id)
    if file.stat().st_size > 2_000_000:
        raise ValueError("Work checkpoint is too large.")
    data = json.loads(file.read_text())
    if data["source"]["user_id"] != owner_id:
        raise ValueError("This checkpoint belongs to a different owner.")
    return data


def save(home, data, learning):
    data["updated_at"] = time.time()
    data.update(learning.extension("location_context").scrub(home, data))
    body = json.dumps(data, ensure_ascii=False)
    learning.safe_text(body)
    if len(body.encode()) > 1_900_000:
        raise ValueError("Work checkpoint reached its bound; review this work before adding more steps.")
    learning.save(path(home, data["case_id"]), data)


RECENT_RECEIPTS = 10
SUCCESS_IDS = 60


def _receipt_row(receipt):
    row = {k: receipt[k] for k in ("tool_call_id", "tool", "status", "write", "at") if k in receipt}
    if "resolved" in receipt:
        row["resolved"] = copy.deepcopy(receipt["resolved"])
    return row


def public(data, *, detail=False):
    out = {k: copy.deepcopy(data[k]) for k in
           ("case_id", "source", "goal", "scope", "plan", "done_when", "difficulty", "tools", "status",
            "progress", "next_step", "references", "review", "updated_at") if k in data}
    # Keep every evidence ID and resolution; only older narrative is deferred to read(detail=True).
    progress = out.get("progress", [])
    omitted = 0
    if not detail:
        for row in progress[:-3]:
            if "summary" in row and "receipt_id" not in row:
                del row["summary"]
                row["summary_omitted"] = True
                omitted += 1
    if omitted:
        out["progress_summary"] = {
            "total": len(progress), "omitted_summaries": omitted,
            "detail_request": {"action": "read", "case_id": data["case_id"], "detail": True},
            "hint": "Read details before relying on an omitted step's narrative; evidence IDs are not its meaning.",
        }
    if "receipts" in data:
        # Replies stay bounded; the stored checkpoint and every guard keep the full receipts.
        receipts = data["receipts"]
        recent = len(receipts) - RECENT_RECEIPTS
        shown = [_receipt_row(r) for i, r in enumerate(receipts) if detail or i >= recent or "resolved" in r
                 or (r.get("write") and r.get("status") in {"unknown", "partial"})]
        success = [r["tool_call_id"] for r in receipts if r.get("status") == "success" and r.get("tool_call_id")]
        out["receipts"] = shown
        out["receipt_summary"] = {"total": len(receipts), "shown": len(shown), "omitted": len(receipts) - len(shown),
                                  "success_ids": success if detail else success[-SUCCESS_IDS:],
                                  "success_ids_omitted": 0 if detail else max(0, len(success) - SUCCESS_IDS)}
    return out


def pending(home, owner_id, *, limit=5, project_source=None):
    items = []
    for file in folder(home).glob("*.json"):
        try:
            stat = file.lstat()
            signature = (stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
            cache_key = (str(file), owner_id)
            with _LOCK:
                cached = _PENDING_CACHE.get(cache_key)
            if cached and cached[0] == signature:
                row = copy.deepcopy(cached[1])
            else:
                row = read(home, file.stem, owner_id)
                row = {k: row.get(k) for k in ("case_id", "source", "goal", "status", "next_step", "references", "updated_at")}
                with _LOCK:
                    _PENDING_CACHE[cache_key] = (signature, copy.deepcopy(row))
                    _PENDING_CACHE.move_to_end(cache_key)
                    while len(_PENDING_CACHE) > PENDING_CACHE_LIMIT:
                        _PENDING_CACHE.popitem(last=False)
            if row["status"] == "completed" and row["updated_at"] < time.time() - COMPLETED_RETENTION_DAYS * 86400:
                # Serialize against writers; reread before removing a resumed case.
                import fcntl
                lock = file.with_suffix(".lock")
                if lock.is_symlink():
                    raise ValueError("Work checkpoint locks cannot follow links.")
                with lock.open("a") as handle:
                    fcntl.flock(handle, fcntl.LOCK_EX)
                    latest = read(home, file.stem, owner_id)
                    if latest["status"] == "completed" and latest["updated_at"] < time.time() - COMPLETED_RETENTION_DAYS * 86400:
                        file.unlink()
                with _LOCK:
                    _PENDING_CACHE.pop(cache_key, None)
                continue
        except ValueError as exc:
            if "different owner" in str(exc):
                continue
            LOG.warning("work checkpoint skipped (%s): %s", file.name, type(exc).__name__)
            continue
        except (OSError, KeyError, TypeError) as exc:
            LOG.warning("work checkpoint skipped (%s): %s", file.name, type(exc).__name__)
            continue
        if project_source and any(row["source"].get(k) != project_source.get(k) for k in ("chat_id", "thread_id")):
            continue
        if row["status"] != "completed":
            items.append({k: row.get(k) for k in ("case_id", "goal", "status", "next_step", "references", "updated_at")})
    return sorted(items, key=lambda r: r["updated_at"], reverse=True)[:limit]


def clean_request(text):
    text = text.rsplit("[New message]", 1)[-1].strip()
    return re.sub(r"^\[[^\]\n]*Slack user <@[^>]+>\]\s*", "", text).strip()


def context(home, learning, *, session_id, turn_id="", sender_id="", user_message="", parent_session_id="", model="", **_):
    if not settings(home, learning) or not turn_id or parent_session_id:
        return ""
    # Explicit selection of another model opts this turn out of the fixed A/B route.
    if model and model.removesuffix("[1m]") not in {WORK_MODEL, BACKGROUND_MODEL, "opus", "sonnet"}:
        return ""
    if not learning.extension("memory_live")._pre_owner(home, learning, session_id, sender_id, user_message):
        return ""
    request = learning.extension('request_source').text(home, learning, user_message)
    location = learning.extension("location_context").binding(home, turn_id)
    location_only = bool(location and location["session_id"] == session_id and location["only"])
    simple = bool(settings(home, learning).get("simple_requests", True) and (SIMPLE.fullmatch(request) or location_only))
    with _LOCK:
        _TURNS[key(home, turn_id)] = {"owner": sender_id, "session_id": session_id, "request": request,
                                    "phase": "simple" if simple else "interpret", "last_role": "", "simple_request": simple,
                                    "case_id": None, "observations": [], "started_at": time.time(),
                                    "location_only": location_only,
                                    "task_capture": learning.extension("task_capture").active(home, request, session_id)}
        while len(_TURNS) > 256:
            _TURNS.popitem(last=False)
    if simple:
        if location_only:
            return ("[현재 작업 역할과 복구]\n이번 요청은 B medium이 바로 답하는 현재 위치 단순 조회다. "
                    "이번 요청에 붙은 위치 블록의 판정과 경과 시간으로 바로 답한다. "
                    "skill_view·read_file·team_work와 추가 도구를 부르지 않는다. "
                    "단축어의 정확한 이름은 '위치 보내기'다. 과거 대화의 다른 이름을 쓰지 않는다.")
        return ("[현재 작업 역할과 복구]\n이번 요청은 B medium이 직접 처리하는 명확한 단순 대화·일정·할 일 조회다. "
                "관련 스킬을 확인하고 요청한 원본만 조회한 뒤 바로 답한다. 작업 인계 기록은 필요 없다. "
                "조회 실패·불명확한 결과는 다음 모델 요청에서 A가 확인한다.")
    project_source = None
    if learning.extension("project_work").in_project(home, learning, session_id):
        from gateway.session_context import get_session_env
        project_source = {"chat_id": get_session_env("HERMES_SESSION_CHAT_ID", ""), "thread_id": get_session_env("HERMES_SESSION_THREAD_ID", "")}
    previous = pending(home, sender_id, project_source=project_source)
    note = ("[현재 작업 역할과 복구]\n대화 해석·학습·최종 검토는 A, 실행은 B가 맡으며 같은 대화와 기존 실행 승인 경계를 쓴다. "
            "일반 업무 도구를 쓰기 전 team_work(start)에 현재 사용자 원문, 목표, 범위, 단계, 완료 기준, 사용할 도구와 난도를 기록한다. "
            "A는 필요한 사실·스킬과 기존 작업을 먼저 확인한다. B는 단계 결과를 checkpoint로 남기고, 완료 또는 막힘을 review로 A에 넘긴다. "
            "A가 실제 도구 근거를 확인한 뒤 finish한다. "
            "중단 작업은 list/read로 기존 기록을 찾고 원본 상태를 확인한 뒤 resume한다. 원래 카드·Common ID를 재사용하고, "
            "이전 승인이나 성공한 쓰기를 재실행하지 않는다. 아래 목록은 새 실행 요청이 아니다.")
    if settings(home, learning).get("simple_requests", True):
        note += " 단순한 일정·할 일 조회는 B medium으로 바로 처리할 수 있다."
    note += DIRECT_NOTE
    return note + ("\n미완료 작업: " + json.dumps(previous, ensure_ascii=False) if previous else "")


def authorized(home, learning, session_id, turn_id):
    if not settings(home, learning):
        raise ValueError("Work routing is not enabled.")
    learning.extension("learning_skills").human_context()
    src = learning.source(home, session_id, allow_approval=True)
    turn = binding(home, turn_id)
    if not turn or src["user_id"] != turn["owner"] or learning.extension('request_source').text(home, learning, src['text']) != turn["request"]:
        raise ValueError("Work requires the current verified owner turn, not a previous or background request.")
    return src, turn


def text(value, label, limit):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= limit:
        raise ValueError(f"{label} must contain 1–{limit} characters.")
    return value.strip()


def strings(value, label, maximum=12):
    if not isinstance(value, list) or not 1 <= len(value) <= maximum:
        raise ValueError(f"{label} needs 1–{maximum} items.")
    return [text(item, label, 300) for item in value]


def writing(tool, args):
    if tool == 'notion':
        return args.get('action') in {'create', 'update', 'append', 'edit', 'trash', 'batch', 'api'} and args.get('method', 'POST') != 'GET'
    if tool in READS or tool in {"claude_code_artifact"}:
        return False
    if tool == "gmail_direct":
        return str(args.get("action") or "").strip().lower() in GMAIL_WRITES
    if tool == "doc_fill":
        return args.get("action") != "inspect"
    return tool in BUSINESS


def guard(home, learning, *, tool_name, args, session_id, turn_id="", **_):
    if not settings(home, learning) or not binding(home, turn_id):
        return None
    src, turn = authorized(home, learning, session_id, turn_id)
    if turn.get("location_only"):
        return {"action": "block", "message": "현재 위치 단순 조회는 제공된 위치 블록으로 답하고 추가 도구를 호출하지 않는다."}
    if tool_name == "team_work" and turn["last_role"] == "simple":
        return {"action": "block", "message": "단순 조회 역할에서는 team_work를 호출하지 않는다."}
    if tool_name not in BUSINESS | LEARNING:
        return None
    if tool_name in LEARNING:
        if turn["last_role"] in {"execute", "simple"}:
            if turn["last_role"] == "simple":
                turn["phase"] = "interpret"
            return {"action":"block", "message":"학습·기억 변경은 A가 원문과 실제 결과를 확인한 뒤 한다. 먼저 team_work(review)로 넘긴다."}
        return None
    if turn["phase"] == "simple" and turn["last_role"] == "simple" and tool_name in SIMPLE_TOOLS:
        return None
    if turn["phase"] == "simple":
        turn["phase"] = "interpret"
    if turn["last_role"] in {"interpret", "review"} and tool_name in PREFLIGHT:
        return None
    capture_allowed = (tool_name in DIRECT or learning.extension('task_capture').allowed(tool_name, args or {}))
    if ((capture_allowed and tool_name in direct_tools(home, learning, turn) or completion_move(tool_name, args)) and turn["last_role"] == "interpret" and turn["phase"] == "interpret"
            and (not turn["case_id"] or turn.get("direct"))):
        turn["direct"] = True
        return _write_problem(tool_name, args, turn["observations"], learning)
    if turn["phase"] != "execute" or turn["last_role"] != "execute" or not turn["case_id"]:
        return {"action":"block", "message":"먼저 A가 team_work(start)로 현재 요청의 목표·범위·단계·완료 기준·도구를 넘긴다. 다음 B 요청에서 업무를 실행한다."}
    work = read(home, turn["case_id"], src["user_id"])
    if tool_name not in work["tools"]:
        return {"action":"block", "message":"인계한 도구 범위 밖이다. team_work(review)로 A에 범위 재검토를 요청한다."}
    return _write_problem(tool_name, args, work["receipts"], learning)


def _write_problem(tool_name, args, receipts, learning):
    if not writing(tool_name, args or {}):
        return None
    digest = learning.extension("learning_skills").digest([tool_name, args])
    if any(r["write"] and r["status"] == "success" and r["args_digest"] == digest for r in receipts):
        return {"action":"block", "message":"이 작업에서 같은 쓰기가 이미 성공했다. 기존 결과와 ID를 확인하고 다음 단계로 진행한다."}
    uncertain = [r for r in receipts if r["write"] and r["status"] in {"unknown", "partial"}]
    # Rollback's own consumed/remaining journal governs retries, including old unknown receipts.
    if uncertain and (tool_name != "personal_work_rollback"
                      or any(r["tool"] != "personal_work_rollback" for r in uncertain)):
        return {"action":"block", "message":"앞선 쓰기의 반영 여부가 불명확하다. 원본 조회로 상태를 확인한 뒤 A가 team_work(resolve_uncertain)로 정리한다. 자동 재실행하지 않는다."}
    return None  # Existing permission and business idempotency checks still run.


def references(tool, args, payload):
    found = []
    if tool.startswith("kanban_"):
        item = payload.get("task") or payload
        identifier = item.get("task_id") or item.get("id") or args.get("task_id")
        if identifier:
            found.append({"kind":"kanban", "id":str(identifier), "status":item.get("status", "unknown")})
    if tool.startswith("common_work_") or tool in {"personal_work_bundle", "personal_work_rollback"}:
        for item in (payload, payload.get("item"), payload.get("receipt")):
            if isinstance(item, dict) and item.get("common_id"):
                found.append({"kind":"common_work", "id":str(item["common_id"]), "status":item.get("status", "unknown")})
    return found


def observe(home, learning, *, session_id, turn_id="", tool_name, args, result, status="ok", tool_call_id="", **_):
    if tool_name not in BUSINESS or not binding(home, turn_id):
        return
    src, turn = authorized(home, learning, session_id, turn_id)
    evidence = learning.extension("learning_evidence")
    payload = evidence.payload(tool_name, result) or {}
    outcome = evidence.classify(tool_name, result)
    if status != "ok":
        outcome = "failure" if status in {"blocked", "denied", "plugin_block"} or not writing(tool_name, args or {}) else "unknown"
    elif outcome == "unknown" and not evidence._negative(payload) and not payload.get("staged"):
        if payload.get("ok") is True or payload.get("success") is True:
            outcome = "success"  # Tool success only; A still reviews task completion.
    if writing(tool_name, args or {}) and outcome == "failure" and status == "ok":
        outcome = "unknown"  # A negative transport/result alone does not prove a write had no effect.
    if (tool_name == "gmail_direct" and payload.get("ok") is False
            and (payload.get("approval_required") is True or payload.get("needs_owner_approval") is True)
            and payload.get("mutation_calls") == 0):
        outcome = "failure"  # Approval preview returned before any Gmail mutation; approval may follow.
    if (tool_name == "gmail_direct" and payload.get("ok") is False
            and payload.get("mutation_calls") == 0 and payload.get("error") in GMAIL_INPUT_ERRORS):
        outcome = "failure"  # Missing/invalid fields were rejected before a preview or Gmail write.
    if (tool_name == "personal_work_rollback" and payload.get("ok") is False
            and (payload.get("error") in {
                "NO_RECENT_PERSONAL_WORK_ROLLBACK", "ROLLBACK_BUNDLE_KEY_NOT_LATEST",
                "INVALID_ROLLBACK_ARGUMENTS", "LATEST_CHANGE_NOT_REVERSIBLE",
            } or (payload.get("error") == "OLDER_CHANGE_NEEDS_CONFIRMATION"
                  and payload.get("mutation_calls") == 0))):
        outcome = "failure"  # These exact errors return from selection before any native handler.
    if (tool_name == "google_calendar_color_set" and payload.get("ok") is False
            and payload.get("mutation_calls") == 0):
        outcome = "failure"  # No patch was attempted; retry after reauthentication is safe.
    if (writing(tool_name, args or {})
            and payload.get("ok") is False
            and type(payload.get("mutation_calls")) is int and payload["mutation_calls"] == 0
            and (tool_name != "personal_work_bundle" or
                 (type(payload.get("approval_calls")) is int and payload['approval_calls'] == 0))
            and payload.get("applied") is not True and not payload.get("execution_uncertain")):
        outcome = "failure"  # Explicit zero-write refusals may be corrected without replaying an uncertain write.
    digest = learning.extension("learning_skills").digest
    row = {"tool_call_id": str(tool_call_id or digest([turn_id, tool_name, args, result])[:24]),
           "tool":tool_name, "status":outcome, "write":writing(tool_name, args or {}), "at":time.time(),
           "args_digest":digest([tool_name, args]), "result_digest":digest(result),
           "session_id":session_id, "message_id":src["message_id"],
           "references":references(tool_name, args or {}, payload)}
    turn["observations"].append(row)
    turn["observations"] = turn["observations"][-200:]
    if turn["phase"] == "simple" and outcome != "success":
        turn["phase"] = "interpret"
    if turn.get("direct") and row["write"] and not turn["case_id"]:
        _direct_record(home, learning, src, turn)
    if not turn["case_id"]:
        return
    with learning.locked(path(home, turn["case_id"])):
        work = read(home, turn["case_id"], src["user_id"])
        if not any(r["tool_call_id"] == row["tool_call_id"] for r in work["receipts"]):
            work["receipts"].append(row)
        refs = {(r["kind"], r["id"]):r for r in work["references"]}
        refs.update({(r["kind"], r["id"]):r for r in row["references"]})
        work["references"] = list(refs.values())
        if turn.get("direct"):
            writes = [r for r in work["receipts"] if r["write"]]
            done = bool(writes) and all(r["status"] == "success" for r in writes)
            work.update(status="completed" if done else "waiting",
                        next_step="완료" if done else "실패·불명확한 쓰기를 원본 조회로 확인한다. 자동 재실행하지 않는다.")
        save(home, work, learning)


def _direct_record(home, learning, src, turn):
    """The after-the-fact record of a direct write: the reviewer reads receipts, not a model summary."""
    case_id = learning.extension("learning_usage").case_id(src, learning.extension("learning_skills"))
    file = path(home, case_id)
    with learning.locked(file):
        if not file.exists():
            request = turn["request"].strip() or "일정·미리알림 단건 쓰기"
            # Earlier lookups in this turn are part of the evidence too.
            work = {"case_id":case_id, "source":{k:v for k,v in src.items() if k != "text"},
                    "created_at":time.time(), "receipts":list(turn["observations"][:-1]), "references":[], "progress":[],
                    "goal":request[:500], "scope":"선 작성 후 보고: 소유자 대화의 일정·미리알림 단건 쓰기",
                    "plan":["조회 후 바로 실행하고 결과로 답한다"], "done_when":["쓰기 도구가 성공을 반환한다"],
                    "tools":sorted(direct_tools(home, learning, turn)), "difficulty":"simple", "status":"executing", "direct":True,
                    "next_step":"결과 기록"}
            save(home, work, learning)
    turn["case_id"] = case_id


def _start(home, args, src, turn, learning):
    if turn["last_role"] == "simple":
        turn["phase"] = "interpret"
    if turn["last_role"] not in {"interpret", "review"}:
        raise ValueError("Only A may define the execution scope. Ask for review first.")
    learning.validate_quote(src, args.get("user_said", ""))
    tools = args.get("tools", [])
    # Read-only helpers that are never gated: drop them instead of failing the handoff and costing a
    # whole model turn (2026-10-26). Anything else outside BUSINESS still fails, naming the tool.
    tools = [t for t in tools if t not in UNGATED_READS] if isinstance(tools, list) else tools
    if not isinstance(tools, list) or len(tools) > 30 or any(t not in BUSINESS for t in tools):
        bad = [t for t in tools if t not in BUSINESS] if isinstance(tools, list) else []
        raise ValueError("tools must name offered business tools, at most 30." + (f" Not business tools: {bad}" if bad else ""))
    plan = strings(args.get("plan"), "plan")
    difficulty = args.get("difficulty", "normal")
    if difficulty not in {"simple", "normal", "complex"}:
        raise ValueError("Use simple, normal or complex difficulty.")
    if difficulty == "simple" and (len(plan) != 1 or not set(tools) <= READS):
        difficulty = "normal"
    case_id = turn["case_id"] or learning.extension("learning_usage").case_id(src, learning.extension("learning_skills"))
    file = path(home, case_id)
    with learning.locked(file):
        if file.exists():
            work = read(home, case_id, src["user_id"])
            if work["status"] == "completed" and not work.get("direct"):
                raise ValueError("This owner request is already completed; do not repeat it.")
            # Same-turn replanning preserves receipts and canonical ids.
        else:
            work = {"case_id":case_id, "source":{k:v for k,v in src.items() if k != "text"},
                    "created_at":time.time(), "receipts":[], "references":[], "progress":[]}
        work.update(goal=text(args.get("goal"), "goal", 500), scope=text(args.get("scope"), "scope", 1000),
                    plan=plan, done_when=strings(args.get("done_when"), "done_when", 8),
                    tools=list(dict.fromkeys(tools)), difficulty=difficulty, status="executing", next_step=plan[0], direct=False)
        save(home, work, learning)
    turn.update(phase="execute", case_id=case_id, direct=False)
    if difficulty == "complex":
        turn["effort"] = "xhigh"
    return {"ok":True, "handoff":public(work, detail=args.get("detail") is True), "original_request":src["text"],
            "approval":"No approval is transferred. Use the existing tool approval path for every action."}


def _resolve_uncertain(work, args, turn):
    if turn["last_role"] not in {"interpret", "review"}:
        raise ValueError("Only A may resolve an uncertain write after reading the original.")
    receipt = next((r for r in work["receipts"] if r["tool_call_id"] == args.get("receipt_id")), None)
    if not receipt or receipt["write"] is not True or receipt["status"] not in {"unknown", "partial"}:
        raise ValueError("receipt_id must identify an unknown or partial write in this checkpoint.")
    outcome = args.get("outcome")
    if outcome not in {"applied", "not_applied"}:
        raise ValueError("Resolve as applied or not_applied.")
    identifiers = args.get("evidence_ids")
    if not isinstance(identifiers, list) or not identifiers or any(not isinstance(i, str) for i in identifiers):
        raise ValueError("At least one successful lookup receipt is required.")
    valid = {r["tool_call_id"] for r in work["receipts"]
             if r["write"] is False and r["status"] == "success" and r["at"] > receipt["at"]}
    if any(i not in valid for i in identifiers):
        raise ValueError("Evidence must be successful reads after the uncertain write in this checkpoint.")
    resolution = {"outcome": outcome, "evidence_ids": list(dict.fromkeys(identifiers)),
                  "summary": text(args.get("summary"), "summary", 1500), "at": time.time(),
                  "model": turn.get("last_route", {}).get("model", INTERPRETER)}
    receipt.update(status="success" if outcome == "applied" else "failure", resolved=resolution)
    work["progress"].append({"action": "resolve_uncertain", "receipt_id": receipt["tool_call_id"], **resolution})
    return receipt


def handle(home, args, *, learning, session_id="", turn_id="", **_):
    try:
        if not turn_id:
            # Native registry handlers receive task_id/session_id, while the
            # dispatcher binds the authoritative turn id in this ContextVar.
            # Never infer a turn from a model-supplied argument or a latest-session map.
            from tools.approval_context import _approval_session_id, _approval_turn_id
            if session_id and _approval_session_id.get() == session_id:
                turn_id = _approval_turn_id.get()
        src, turn = authorized(home, learning, session_id, turn_id)
        action = args.get("action")
        project_scope = learning.extension("project_work").in_project(home, learning, session_id)
        if action == "list":
            return json.dumps({"ok":True, "pending":pending(home, src["user_id"], limit=20, project_source=src if project_scope else None)}, ensure_ascii=False)
        if action == "metrics":
            return json.dumps(learning.extension("work_metrics").summary(home), ensure_ascii=False)
        if action == "start":
            return json.dumps(_start(home, args, src, turn, learning), ensure_ascii=False)
        case_id = args.get("case_id") or turn["case_id"]
        with learning.locked(path(home, case_id)):
            work = read(home, case_id, src["user_id"])
            if project_scope and any(work["source"].get(k) != src.get(k) for k in ("chat_id", "thread_id")):
                raise ValueError("This work belongs to another project thread.")
            if action == "read":
                return json.dumps({"ok":True, "checkpoint":public(work, detail=args.get("detail") is True), "hint":"Before resume, use common_work_get/kanban_show to refresh every referenced original. Past approvals do not transfer."}, ensure_ascii=False)
            resolved = None
            if action == "resolve_uncertain":
                resolved = _resolve_uncertain(work, args, turn)
            elif action == "resume":
                learning.validate_quote(src, args.get("user_said", ""))
                if turn["last_role"] not in {"interpret", "review"} or work["status"] == "completed":
                    raise ValueError("A can resume only unfinished work after interpreting the new owner request.")
                observed = {(r["kind"], r["id"]):r for receipt in turn["observations"]
                            if receipt["tool"] in PREFLIGHT and receipt["status"] == "success" for r in receipt["references"]}
                if any((r["kind"],r["id"]) not in observed for r in work["references"]):
                    raise ValueError("Read each original Common ID/card in this turn before resuming; do not create replacement objects.")
                work["references"] = [observed[(r["kind"],r["id"])] for r in work["references"]]
                work["resumed_from"] = {k:v for k,v in src.items() if k != "text"}
                already_done = work["references"] and all(r["status"] in {"done", "completed"} for r in work["references"])
                work["status"] = "awaiting_review" if already_done else "executing"
                turn.update(phase="review" if already_done else "execute", case_id=case_id)
            elif action in {"checkpoint", "review"}:
                if turn["case_id"] != case_id or turn["last_role"] != "execute":
                    raise ValueError("B records progress for its current handoff only.")
                summary = text(args.get("summary"), "summary", 1500)
                identifiers = args.get("evidence_ids", [])
                valid = {r["tool_call_id"] for r in work["receipts"] if r["status"] == "success"}
                if not isinstance(identifiers, list) or any(i not in valid for i in identifiers):
                    raise ValueError("Evidence ids must refer to successful actual tool results in this checkpoint.")
                work["progress"].append({"at":time.time(), "summary":summary, "evidence_ids":identifiers})
                work["next_step"] = text(args.get("next_step", "A가 결과·미완료·승인 필요 사항을 검토"), "next_step", 500)
                if action == "review":
                    work["status"] = "awaiting_review"
                    turn["phase"] = "review"
            elif action == "finish":
                if turn["case_id"] != case_id or turn["last_role"] != "review" or work["status"] != "awaiting_review":
                    raise ValueError("A must receive a physical review-model turn before finishing B's work.")
                outcome = args.get("outcome")
                if outcome not in {"completed", "waiting", "blocked"}:
                    raise ValueError("Finish as completed, waiting or blocked.")
                if outcome == "completed":
                    if any(r["kind"] == "kanban" and r["status"] not in {"done", "completed"} for r in work["references"]):
                        raise ValueError("A running/background card is not completed. Finish as waiting.")
                    if work["tools"] and not any(r["status"] == "success" for r in work["receipts"]):
                        raise ValueError("No successful tool evidence; this task cannot be marked completed.")
                    if any(r["write"] and r["status"] in {"unknown", "partial"} for r in work["receipts"]):
                        raise ValueError("Uncertain writes require investigation, not a completion claim.")
                work["status"] = outcome
                work["review"] = {"summary":text(args.get("summary"), "summary", 1500), "at":time.time(),
                                  "model":INTERPRETER, "tool_success_is_task_success":False}
                turn["phase"] = "interpret"
            else:
                raise ValueError("Use start/list/read/resume/checkpoint/review/finish/resolve_uncertain/metrics.")
            save(home, work, learning)
            if resolved is not None:
                # A's direct-write guard reads observations, B's guard reads receipts.
                for observation in turn["observations"]:
                    if observation["tool_call_id"] == resolved["tool_call_id"]:
                        observation.update(status=resolved["status"], resolved=copy.deepcopy(resolved["resolved"]))
        return json.dumps({"ok":True, "checkpoint":public(work, detail=args.get("detail") is True)}, ensure_ascii=False)
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return json.dumps({"ok":False, "error":str(exc)}, ensure_ascii=False)


def request(home, learning, payload, *, turn_id="", **_):
    if _.get("aux_task") or _.get("auxiliary") or _.get("provider") not in (None, "", PROVIDER):
        return None
    cfg = settings(home, learning)
    if not cfg:
        return None
    turn = binding(home, turn_id)
    if turn and turn.get("location_only") and not learning.extension("location_context").binding(home, turn_id):
        turn.update(location_only=False, phase="interpret")
    if not turn:
        # Existing automation keeps its own tools/policies, on the execution model.
        from gateway.session_context import get_session_env
        if os.environ.get("HERMES_KANBAN_TASK"):
            role, difficulty = "background", "complex"  # Long research cards use B xhigh.
        elif str(get_session_env("HERMES_CRON_SESSION", "")).strip().lower() in {"1", "true", "yes", "on"}:
            role, difficulty = "background", "simple"
        else:
            return None
    else:
        role = turn["phase"]
        difficulty = "simple" if role == "simple" else "normal"
        if role == "execute":
            work = read(home, turn["case_id"], turn["owner"])
            difficulty = work["difficulty"]
        turn["last_role"] = role
    model = BACKGROUND_MODEL if role == "background" else WORK_MODEL
    effort = "medium" if difficulty == "simple" else "xhigh" if difficulty == "complex" else "high"
    if turn and turn.get("effort") == "xhigh":
        effort = "xhigh"
    # Per-profile opt-in; existing assistant/background effort selection is unchanged.
    if role == "execute" and cfg.get("executor_effort") in {"low", "medium", "high", "xhigh", "max"}:
        effort = cfg["executor_effort"]
    result = copy.deepcopy(payload)
    result["model"] = model
    extra = result.get("extra_body") or {}
    extra["reasoning"] = {"enabled":True, "effort":effort}
    result["extra_body"] = extra
    if turn:
        turn["last_route"] = {"role": role, "model": model, "effort": effort}
        offered = {t.get("function", {}).get("name") for t in result.get("tools", [])}
        business = sorted(offered & BUSINESS)
        allowed = set(business)
        if role in {"interpret", "review"}:
            allowed &= PREFLIGHT | (direct_tools(home, learning, turn) if role == "interpret" else set())
        elif role == "execute":
            allowed &= set(work["tools"])
        elif role == "simple":
            allowed &= SIMPLE_TOOLS
        filtered = []
        for tool in result.get("tools", []):
            name = tool.get("function", {}).get("name")
            if name == "team_work":
                items = tool["function"]["parameters"]["properties"]["tools"]["items"]
                if business:
                    items["enum"] = business
                else:
                    items.pop("enum", None)  # An empty enum is not a valid tool schema.
            filtered.append(tool)
        if "tools" in result:
            result["tools"] = sorted(filtered, key=lambda tool: tool.get("function", {}).get("name", ""))
        message = {
            "interpret":"A: 원문과 최신 기억·스킬로 요청을 해석한다. 대화·학습은 직접 처리하고 업무는 team_work(start)로 B에 인계한다. 단순 대화에는 작업 기록을 만들지 않는다." + DIRECT_NOTE,
            "execute":"B: 인계한 범위·단계·도구로 실행하고 실제 결과를 확인한다. 기존 승인 절차를 따른다. 중간 결과는 checkpoint, 완료 또는 막힘은 team_work(review)로 A에게 넘긴다. 최종 답변 전에 review를 호출한다.",
            "review":"A: B의 요약과 실제 도구 결과를 구분해 완료 기준을 확인한다. team_work(finish)로 completed/waiting/blocked를 기록한 뒤 평소 성격과 말투로 답한다. 추가 실행이 필요하면 start로 범위를 다시 정한다.",
            "simple":"B medium: 명확한 단순 조회를 바로 처리한다. 최신 업무 스킬을 확인하고 원본 조회 뒤 바로 답한다. 작업 인계는 필요 없다.",
        }[role]
        if role in {"interpret", "review"} and set(business) - allowed:
            message += (
                " 지금 A가 실행할 수 없는 업무 도구도 연결이 끊긴 것이 아니다."
                " team_work의 tools 목록에 있는 도구는 team_work(start)로 B에게 넘기면 다음 B 요청에서 실행할 수 있다."
                " 사용자가 방금 변경을 되돌리라고 하면 personal_work_rollback을 tools에 넣어 넘긴다."
                " 도구가 없다고 답하지 않는다."
            )
        if role == "simple" and turn.get("location_only"):
            message = ("B medium: 붙은 위치 블록의 판정·값·경과 시간으로 바로 답한다. "
                       "skill_view·read_file·team_work를 부르지 않고 추가 도구 호출 없이 답한다. "
                       "위치를 추측하지 않고 블록의 판정별 안내를 따른다. "
                       "단축어의 정확한 이름은 '위치 보내기'다. 과거 대화의 다른 이름을 쓰지 않는다.")
        label = "업무비서" if Path(home).name == "work-assistant" else "개인비서"
        result.setdefault("messages", []).append({"role":"user", "content":f"[{label} 실행 역할 — 새 사용자 요청이 아님]\n" + message})
        if role in {"interpret", "review"}:
            try:
                note = learning.extension("learning_usage").prompt(home, learning, turn)
                if note:
                    result["messages"].append({"role": "user", "content": note})
            except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
                LOG.warning("learning confirmation prompt skipped: %s", type(exc).__name__)

    LOG.info("model route role=%s model=%s effort=%s turn=%s", role, model, effort, turn_id)
    return {"request":result}


def ended(home, learning, *, turn_id="", **_):
    turn = binding(home, turn_id)
    if not turn or not turn["case_id"]:
        return
    with learning.locked(path(home, turn["case_id"])):
        work = read(home, turn["case_id"], turn["owner"])
        if work["status"] in {"executing", "awaiting_review"}:
            work["status"] = "interrupted"
            save(home, work, learning)


SCHEMA = {"name":"team_work", "description":"Handoff a verified owner request from interpretation to execution, checkpoint actual results, review, and resume existing work. A alone may resolve_uncertain with receipt_id, applied/not_applied, summary and successful read evidence_ids from after that write in the same case. Never transfers approval or executes business actions.",
          "parameters":{"type":"object", "properties":{
              "action":{"type":"string", "enum":["start","list","read","resume","checkpoint","review","finish","resolve_uncertain","metrics"]},
              "detail":{"type":"boolean","description":"Return all progress narratives and receipt rows; use read with detail=true when omitted steps matter."},
              "receipt_id":{"type":"string"}, "case_id":{"type":"string"}, "user_said":{"type":"string"},
              "goal":{"type":"string","maxLength":500}, "scope":{"type":"string","maxLength":1000},
              "plan":{"type":"array","items":{"type":"string"},"maxItems":12},
              "done_when":{"type":"array","items":{"type":"string"},"maxItems":8},
              "tools":{"type":"array","items":{"type":"string"},"maxItems":30},
              "difficulty":{"type":"string","enum":["simple","normal","complex"]},
              "summary":{"type":"string","maxLength":1500}, "next_step":{"type":"string","maxLength":500},
              "evidence_ids":{"type":"array","items":{"type":"string"}},
              "outcome":{"type":"string","enum":["completed","waiting","blocked","applied","not_applied"]}},
              "required":["action"],"additionalProperties":False}}
