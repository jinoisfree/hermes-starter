"""Profile-local deadline ledger and narrowly scoped, at-most-once Slack delivery."""
from __future__ import annotations
import contextlib
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import tempfile
import logging
import urllib.parse
import urllib.request
from datetime import date, datetime
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
CHANNEL = "REPLACE_SLACK_ID"  # 보고 채널 (2026-10-18); deadlines are still collected from #starter_user-world
REASONS = {"담당자 확인", "완료 근거 확인", "마감 경과", "취소"}


def extension(name):
    spec = importlib.util.spec_from_file_location("deadline_" + name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SOURCE_CHECKS = extension('deadline_sources')


def enabled(config):
    return isinstance(config, dict) and config.get("enabled") is True


def stage(item, today, mode="morning"):
    if item.get("closed"):
        return None
    d = (date.fromisoformat(item["due"]) - today).days
    sent = item.get("notifications", {})
    if mode == "recheck":
        return "0-recheck" if d == 0 and "0-recheck" not in sent else None
    candidate = "7" if 4 <= d <= 7 else "3" if 2 <= d <= 3 else "1" if d == 1 else "0" if d == 0 else None
    if candidate is None or any((0 if s == "0-recheck" else int(s)) <= int(candidate) for s in sent):
        return None
    return candidate


def validate(data):
    try:
        assert data["version"] == 1 and isinstance(data["items"], dict)
        for key, item in data["items"].items():
            assert key == item["id"] and re.fullmatch(r"(notion|calendar|reminder|slack|notes|gmail):[^\s]{1,240}", key)
            assert isinstance(item["title"], str) and item["title"]
            date.fromisoformat(item["due"])
            datetime.fromisoformat(item["discovered_at"])
            assert isinstance(item["source"], str)
            assert isinstance(item["history"], list) and isinstance(item["replies"], list)
            assert item["closed"] is None or isinstance(item["closed"], dict)
            assert isinstance(item["notifications"], dict)
            for s, receipt in item["notifications"].items():
                assert s in {"7", "3", "1", "0", "0-recheck"}
                assert receipt["status"] in {"pending", "sent", "uncertain"}
                datetime.fromisoformat(receipt["at"])
                assert receipt["status"] != "sent" or bool(receipt["ts"])
    except (KeyError, TypeError, ValueError, AssertionError):
        raise ValueError("deadline_state_corrupt") from None
    return data


def load(path):
    if not path.exists():
        return {"version": 1, "items": {}}
    try:
        return validate(json.loads(path.read_text()))
    except (ValueError, TypeError):
        raise ValueError("deadline_state_corrupt") from None


def save(path, data):
    validate(data)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".deadline-")
    try:
        with os.fdopen(fd, "w") as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.write("\n")
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextlib.contextmanager
def locked(home):
    folder = Path(home) / "state"
    path = folder / "deadline-watch.json"
    lock = folder / "deadline-watch.lock"
    if any(p.is_symlink() for p in (folder, path, lock)):
        raise ValueError("deadline_symlink_refused")
    folder.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "a") as file:
        fcntl.flock(file, fcntl.LOCK_EX)
        yield path, load(path)


def snapshot(home, mode="morning", now=None):
    now = now or datetime.now(KST)
    today = now.astimezone(KST).date()
    folder = Path(home) / "state"
    path = folder / "deadline-watch.json"
    if any(p.is_symlink() for p in (folder, path, folder / "deadline-watch.lock")):
        raise ValueError("deadline_symlink_refused")
    # Writers publish complete snapshots with os.replace. Reading the previous
    # version must not wait for a notification's network request under LOCK_EX.
    data = load(path)
    items = [dict(item, stage=stage(item, today, mode)) for item in data["items"].values() if not item["closed"]]
    if mode == "recheck":
        items = [item for item in items if item["stage"] == "0-recheck"]
    return {"date": today.isoformat(), "mode": mode, "wakeAgent": mode == "morning" or bool(items), "items": items}


def perform(home, args, *, mode="morning", now=None, opener=None, session_id="", turn_id="", source_check=False):
    """Trusted core; handle() authenticates the runner before entering here."""
    now = (now or datetime.now(KST)).astimezone(KST)
    today, at = now.date(), now.isoformat()
    action = args.get("action")
    if action == "list":
        value=snapshot(home, mode, now)
        if source_check:value['source_read_receipts']=SOURCE_CHECKS.available(home,session_id,turn_id)
        return value
    with locked(home) as (path, data):
        key = args.get("id", "")
        if not isinstance(key, str) or not re.fullmatch(r"(notion|calendar|reminder|slack|notes|gmail):[^\s]{1,240}", key):
            raise ValueError("stable_source_id_required")
        item = data["items"].get(key)
        if action == "upsert":
            title, due, source = args.get("title"), args.get("due"), args.get("source")
            if not isinstance(title, str) or not 1 <= len(title.strip()) <= 300:
                raise ValueError("title_required")
            date.fromisoformat(due)
            if not isinstance(source, str) or not 1 <= len(source) <= 1000:
                raise ValueError("source_required")
            if item is None:
                item = {"id": key, "title": title, "due": due, "source": source, "discovered_at": at,
                        "notifications": {}, "replies": [], "history": [], "closed": None}
                data["items"][key] = item
            elif item["due"] != due:
                item["history"].append({"due": item["due"], "notifications": item["notifications"], "changed_at": at})
                # Restore receipts if a deadline is changed back; do not lose uncertain sends.
                item["notifications"] = next((h["notifications"].copy() for h in reversed(item["history"]) if h.get("due") == due), {})
            item.update(title=title.strip(), due=due, source=source)
            if args.get('deadline_at'):
                stamp = str(args['deadline_at'])
                moment = datetime.fromisoformat(stamp)
                if moment.tzinfo is not None: moment = moment.astimezone(KST)
                if moment.date().isoformat() != due: raise ValueError('deadline_date_mismatch')
                item['deadline_at'] = stamp
            else:
                item.pop('deadline_at', None)
        elif item is None:
            raise ValueError("unknown_deadline")
        elif action == "source_check":
            if not source_check:raise ValueError('official_source_check_disabled')
            if not 0 <= (date.fromisoformat(item['due'])-today).days <= 7:
                raise ValueError('official_source_check_outside_d7')
            cache=[r.get('source_check',{}) for r in data['items'].values()]
            item['source_check']=SOURCE_CHECKS.record(home,args,now,session_id=session_id,turn_id=turn_id,cache=cache)
        elif action == "close":
            reason, reply, evidence = args.get("reason"), args.get("reply", ""), args.get("evidence", "")
            if reason not in REASONS:
                raise ValueError("invalid_close_reason")
            if not isinstance(reply, str) or len(reply) > 2000 or not isinstance(evidence, str) or len(evidence) > 2000:
                raise ValueError("invalid_close_source")
            if (reason == "담당자 확인" and not reply.strip()) or (reason in {"완료 근거 확인", "취소"} and not evidence.strip()):
                raise ValueError("close_source_required")
            if reason == "마감 경과" and date.fromisoformat(item["due"]) >= today:
                raise ValueError("deadline_not_past")
            if not item["closed"]:
                if reply:
                    item["replies"].append({"text": reply, "at": args.get("reply_at") or at})
                item["closed"] = {"reason": reason, "at": at, "evidence": evidence}
        elif action == "reply":
            reply = args.get("reply", "")
            if not isinstance(reply, str) or not 1 <= len(reply.strip()) <= 2000:
                raise ValueError("exact_owner_reply_required")
            record = {"text": reply, "at": args.get("reply_at") or at}
            if record not in item["replies"]:
                item["replies"].append(record)
        elif action == "reopen":
            if item["closed"]:
                item["history"].append({"reopened_at": at, "closed": item["closed"]})
                item["closed"] = None
        elif action == "notify":
            s = stage(item, today, mode)
            if s is None:
                raise ValueError("notification_not_due")
            text = args.get("text", "")
            if not isinstance(text, str) or not 1 <= len(text.strip()) <= 1800:
                raise ValueError("notification_text_required")
            reader = extension("slack_read")
            secret = reader.token(home)
            if not secret:
                raise ValueError("slack_token_missing")
            label = "당일 15:00 재확인" if s == "0-recheck" else "당일" if s == "0" else s + "일 전 단계"
            escape = extension("report_layout").escape
            source_note = '\n\n'+SOURCE_CHECKS.description(item.get('source_check'),item['due'],now) if source_check else ''
            title_prefix = '🔴 '  # waits for the owner's answer (red); closing repaints it green
            display_due = escape(item.get('deadline_at') or item['due'])
            body = {"channel": CHANNEL, "text": f"*{title_prefix}마감 점검 · {escape(item['title'])}*\n\n*마감: {display_due}* · {label}\n\n{escape(text.strip())}\n\n출처: {escape(item['source'])}{escape(source_note)}\n[[버튼: 완료했어 | 못했어 | 수정할게 | 확인했어]]",
                    "mrkdwn": True, "parse": "none", "link_names": False,
                    "unfurl_links": False, "unfurl_media": False}
            try:
                body = extension("slack_blocks").apply(body)
            except Exception:
                pass  # Formatting never blocks a deadline notice.
            req = urllib.request.Request("https://slack.com/api/chat.postMessage", data=json.dumps(body).encode(),
                headers={"Authorization": "Bearer " + secret, "Content-Type": "application/json; charset=utf-8"})
            receipt = {"at": at, "status": "pending", "ts": None}
            item["notifications"][s] = receipt
            save(path, data)  # Durable reservation BEFORE network I/O, including process death.
            try:
                with (opener or urllib.request.build_opener(reader.NoRedirect()).open)(req, timeout=20) as response:
                    result = json.load(response)
                if isinstance(result, dict) and result.get("ok") is False:
                    del item["notifications"][s]
                    save(path, data)
                    raise ValueError("slack_rejected")
                if not isinstance(result, dict) or result.get("ok") is not True or not result.get("ts"):
                    raise OSError("missing_receipt")
                receipt.update(status="sent", ts=result["ts"])
            except (OSError, json.JSONDecodeError):
                receipt["status"] = "uncertain"
            save(path, data)
            return {"stage": s, "receipt": receipt, "ok": receipt["status"] == "sent"}
        else:
            raise ValueError("invalid_action")
        save(path, data)
        return {"ok": True, "item": item, "stage": stage(item, today, mode)}


def slack_call(home, method, body, *, opener=None):
    """Bot-token Slack call; posts go through slack_blocks (bodies with blocks pass unchanged)."""
    reader = extension("slack_read")
    secret = reader.token(home)
    if not secret:
        raise ValueError("slack_token_missing")
    url = "https://slack.com/api/" + method
    headers = {"Authorization": "Bearer " + secret}
    if method == "conversations.replies":
        req = urllib.request.Request(url + "?" + urllib.parse.urlencode(body), headers=headers)
    else:
        if method in {"chat.postMessage", "chat.update"}:
            body = extension("slack_blocks").apply(body, update=method == "chat.update")
        headers["Content-Type"] = "application/json; charset=utf-8"
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
    with (opener or urllib.request.build_opener(reader.NoRedirect()).open)(req, timeout=20) as response:
        result = json.load(response)
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise ValueError("slack_rejected")
    return result


def repaint_closed(home, item, *, opener=None):
    """A closed deadline turns its sent notices green (취소 grey); a failure keeps the close."""
    closed = item.get("closed") or {}
    state = "declined" if closed.get("reason") == "취소" else "done"
    marks = extension("report_status")
    ok = True
    for receipt in (item.get("notifications") or {}).values():
        if receipt.get("status") != "sent" or not receipt.get("ts"):
            continue
        try:
            marks.mark(lambda method, body: slack_call(home, method, body, opener=opener),
                       CHANNEL, receipt["ts"], state, closed.get("reason", ""))
        except Exception as exc:
            ok = False
            logging.getLogger(__name__).warning("deadline notice repaint failed (%s)", type(exc).__name__)
    return ok


def cron_mode(config, session_id):
    for job_id, mode in config.get("jobs", {}).items():
        if isinstance(session_id, str) and session_id.startswith("cron_" + job_id + "_") and mode in {"morning", "recheck"}:
            return mode
    return None


def filter_request(config, request, *, kind, session_id=""):
    """Core auto-enables plugin toolsets: do not add this tool to unrelated jobs."""
    if kind == "chat" or (kind == "cron" and cron_mode(config, session_id)):
        return None
    tools = request.get("tools")
    if not isinstance(tools, list):
        return None
    filtered = [t for t in tools if t.get("function", {}).get("name", t.get("name")) != "deadline_watch"]
    if len(filtered) == len(tools):
        return None
    return {"request": dict(request, tools=filtered)}


def handle(config, home, args, *, kind, session_id="", opener=None, **kwargs):
    try:
        if not enabled(config) or config.get("channel") != CHANNEL or Path(home).name not in {"personal-secretary", "work-assistant"}:
            raise ValueError("deadline_watch_disabled_or_invalid_scope")
        if set(args) - set(SCHEMA["parameters"]["properties"]):
            raise ValueError("unexpected_arguments")
        mode = None
        if kind == "cron":
            mode = cron_mode(config, session_id)
            if mode is None:
                raise ValueError("deadline_job_required")
            if args.get("action") in {"reply", "reopen"} or args.get("reason") == "담당자 확인":
                raise ValueError("owner_reply_requires_chat")
        elif kind == "chat":
            if args.get("action") not in {"list", "close", "reply", "reopen"}:
                raise ValueError("deadline_chat_action_refused")
            learning = extension("learning")
            src = learning.source(home, session_id)
            if args.get("action") in {"close", "reply"}:
                if (args.get("action") == "close" and args.get("reason") != "담당자 확인") or not args.get("reply") or args["reply"] not in src["text"]:
                    raise ValueError("exact_owner_reply_required")
                args = dict(args, reply_at=src["at"])
        else:
            raise ValueError("deadline_runner_refused")
        if args.get('action') == 'ensure_reminder':
            auto = config.get('auto_reminders') or {}
            if (kind != 'cron' or Path(home).name != 'work-assistant' or auto.get('enabled') is not True
                    or not any(session_id.startswith('cron_' + job + '_') for job in auto.get('jobs', []))):
                raise ValueError('deadline_auto_reminder_job_required')
            spec = importlib.util.spec_from_file_location('team_deadline_reminders', Path(home) / 'plugins/team-delegation/deadline_reminders.py')
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            if args.get('source_kind') in {'notion', 'notes'}:
                task_sync = module.module('deadline_task_reminders_sync', Path(home)/'plugins/team-delegation/task_reminders_sync.py')
                return json.dumps(task_sync.sync(home, {'source_kind': args['source_kind'], 'source_id': args.get('source_id')}, config=auto), ensure_ascii=False)
            return json.dumps(module.ensure(home, args, auto), ensure_ascii=False)
        result = perform(home, args, mode=mode or "morning", opener=opener, session_id=session_id, turn_id=kwargs.get('turn_id',''), source_check=config.get('official_source_check') is True)
        if args.get("action") == "close" and result.get("ok"):
            result["status_updated"] = repaint_closed(home, result["item"], opener=opener)
        return json.dumps(result, ensure_ascii=False)
    except (ValueError, TypeError, OSError) as exc:
        return json.dumps({"ok": False, "error": str(exc) if isinstance(exc, ValueError) else type(exc).__name__}, ensure_ascii=False)


def note(home, info=None):
    if str((info or {}).get("platform", "")).lower() != "slack":
        return ""
    try:
        items = [i for i in snapshot(home)["items"] if i["notifications"]]
    except (ValueError, OSError):
        return "[마감 점검] 기록 조회 실패. 완료·중복 방지를 확인했다고 말하지 않는다."
    if not items:
        return ""
    lines = [
        "[알린 열린 마감] '완료했어' 또는 제출·완료했다는 답, '취소됐어' 등 취소 답은 "
        "deadline_watch(action=close, id, reason=담당자 확인, reply=답 원문)로 종료한다. "
        "'못했어'·'수정할게'·'확인했어'는 deadline_watch(action=reply, id, reply=답 원문)로 기록하고 닫지 않는다. "
        "'못했어'에는 일정을 옮길지 한 줄로 묻고, '수정할게'에는 무엇을(날짜·시간·내용) 바꿀지 묻는다. "
        "답을 받으면 기존 Notion 변경 예외와 승인 경계 안에서 반영하고 결과를 보고한다. 새 쓰기 권한은 열지 않는다. "
        "'확인했어'에는 짧게 확인했다고만 답한다. 그 밖의 미완료·보류 답도 reply로 기록하고 닫지 않는다. "
        "업무가 불명확하면 확인한다. 전체 목록은 list. 이미 닫힌 건은 다시 알리지 않는다."
    ]
    for item in items:
        last = list(item["notifications"])[-1]
        line = json.dumps({k: item[k] for k in ("id", "title", "due")}, ensure_ascii=False) + " 단계=" + last
        if len("\n".join(lines)) + 1 + len(line) > 1800:
            break
        lines.append(line)
    return "\n".join(lines)


SCHEMA = {"name": "deadline_watch", "description": "마감 기록 list/upsert/close/reply/reopen. ensure_reminder는 승인된 업무비서 마감 예약만 원본 재조회 후 실제 마감과 같은 시각의 미리알림을 생성·연결한다. 충돌·불확실 결과는 재시도하지 않고 보고한다. 허용된 마감 예약만 notify하며 고정 보고 채널(REPLACE_SLACK_ID)에 코드가 계산한 단계별 한 번 발송한다(마감 수집 원문은 starter_user-world). 원문 자료는 저장하지 않는다.",
    "parameters": {"type": "object", "additionalProperties": False, "required": ["action"], "properties": {
        "source_unavailable":{"type":"boolean","description":"공식 원문 접근 실패. 출처 미확인으로 남긴다."},
        "action": {"type": "string", "enum": ["list", "upsert", "notify", "close", "reply", "reopen", "source_check", "ensure_reminder"]},
        **{k: {"type": "string", "description": v} for k, v in {
            "source_kind": "ensure_reminder 전용: notion | notes | gmail",
            "source_id": "ensure_reminder 원본 page/note/message ID. 제목이나 가공 ID 금지",
            "task": "원문에 실제 있는 미완료 할 일 이름. Notion은 체크박스의 첫 줄과 같아야 함",
            "deadline_at": "실제 마감 YYYY-MM-DD 또는 YYYY-MM-DDTHH:MM+09:00. 알림도 같은 시각",
            "deadline_field": "Notion 다음 마감 속성이 해당 할 일 마감일 때만 다음 마감. 그 외 생략",
            "official_url": "실제로 연 공식 원문 URL. 같은 항목 ID·URL의 검증된 source_check만 당일 재사용",
            "official_due": "원문 인용에 실제 있는 연도 포함 마감 YYYY-MM-DD",
            "official_time": "인용문 속 정확한 마감 시각",
            "official_location": "인용문 속 정확한 장소",
            "official_basis": "원문 운영 주체가 이 일정의 공식 근거인 이유(모델이 원문에서 확인)",
            "source_receipt_id": "이번 턴 web_extract/browser 읽기 tool_call_id",
            "source_quote": "반환된 원문 속 연도 포함 날짜·시각·장소의 정확한 인용 600자 이내",
            "id": "출처:원본ID (notion/calendar/reminder/slack/notes/gmail). 같은 업무는 기존 ID를 재사용한다.",
            "title": "업무 제목", "due": "KST 마감 YYYY-MM-DD. Notion 기간 끝 또는 시작", "source": "원본 링크",
            "text": "확인한 완료 근거 범위·조회 실패와 질문 하나. 제목·마감·단계·링크는 자동 첨부",
            "reason": "담당자 확인 | 완료 근거 확인 | 마감 경과 | 취소", "reply": "현재 소유자의 정확한 답 원문",
            "evidence": "완료·취소 근거의 출처 링크 또는 원본 ID (본문 금지)"}.items()}}}
}
