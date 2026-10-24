"""Daily 08:30 KST owner reports (report room or DM), with recoverable sends and versioned reviews."""
from __future__ import annotations

import argparse
from datetime import date, datetime, time as daytime, timedelta
import hashlib
import importlib.util
import json
import logging
from pathlib import Path
import sqlite3
import re
import time
from types import SimpleNamespace
import urllib.request
import urllib.parse
from zoneinfo import ZoneInfo

_spec = importlib.util.spec_from_file_location("team_kit_report_learning", Path(__file__).with_name("learning.py"))
learning = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(learning)
KST = ZoneInfo("Asia/Seoul")
_spec = importlib.util.spec_from_file_location("team_report_layout", Path(__file__).with_name("report_layout.py"))
layout = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(layout)


def compact_report(home):
    return True  # All profiles use the same compact owner report, including custom homes.


def path(home):
    return Path(home) / "state" / "team-learning-reports.json"


def load(home, now=None):
    try:
        data = json.loads(path(home).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"start_date": (now or datetime.now(KST)).date().isoformat(), "reports": {}}
    if not isinstance(data, dict) or not isinstance(data.get("reports"), dict):
        raise ValueError("Invalid learning report state; not replaced.")
    date.fromisoformat(data["start_date"])
    return data


def owner(home):
    users = learning.owners(home)
    if len(users) != 1:
        raise ValueError("Daily learning reports require exactly one SLACK_ALLOWED_USERS owner.")
    return next(iter(users))


def with_blocks(body, update=False):
    """Report text -> Block Kit (slack_blocks); a load or render problem keeps the text-only body."""
    try:
        spec = importlib.util.spec_from_file_location("team_kit_slack_blocks", Path(__file__).with_name("slack_blocks.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        rendered = module.with_blocks(body, update=update)
        # Learning reviews own this bar: a topic such as "실패 보고" must not override its state.
        title = re.match(r"^\*([🟡🟢🔴]) (?:아침 학습 보고|아침 스킬 보고|학습 항목|스킬 항목)", body.get("text", ""))
        if title and rendered.get("attachments"):
            colour = module.COLORS[{"🟡": "warn", "🟢": "ok", "🔴": "fail"}[title[1]]]
            rendered = {**rendered, "attachments": [{**part, "color": colour}
                                                     for part in rendered["attachments"]]}
        return rendered
    except Exception as exc:
        logging.getLogger(__name__).warning("Slack report helper load failed (%s)", type(exc).__name__)
        return body


def slack(home, method, body):
    if method in {"chat.postMessage", "chat.update"}:
        body = with_blocks(body, update=method == "chat.update")
    token = learning.env(home, "SLACK_BOT_TOKEN")
    if not token:
        raise ValueError("SLACK_BOT_TOKEN is missing.")
    url = "https://slack.com/api/" + method
    headers = {"Authorization": "Bearer " + token}
    if method in {"conversations.history", "conversations.replies"}:
        request = urllib.request.Request(url + "?" + urllib.parse.urlencode(body), headers=headers)
    else:
        headers["Content-Type"] = "application/json; charset=utf-8"
        request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers)
    with urllib.request.urlopen(request, timeout=20) as response:
        data = json.load(response)
    if not data.get("ok"):
        raise RuntimeError(f"Slack refused {method}: {data.get('error', 'unknown_error')}")
    return data


def check(home):
    user = owner(home)
    auth = slack(home, "auth.test", {})
    channel = slack(home, "conversations.open", {"users": user})["channel"]["id"]
    # Recovery needs history access. Check now without posting any message.
    history = slack(home, "conversations.history", {"channel": channel, "limit": 1})
    messages = history.get("messages", [])
    if messages:
        slack(home, "conversations.replies", {"channel": channel, "ts": messages[0]["ts"], "limit": 1})
        print(f"{home}: conversations.replies OK")
    else:
        print(f"{home}: conversations.replies skipped (empty DM)")
    print(f"{home}: OK bot={auth['user_id']} owner={user} dm={channel}; no message sent")
    return channel


def snapshot(home, day):
    start = datetime.combine(day, daytime.min, KST).timestamp()
    end = datetime.combine(day + timedelta(days=1), daytime.min, KST).timestamp()
    data = learning.load(home)
    data = learning.extension("learning_archive").report_data(home, data, start, end)
    if data.get("events_dropped_through", 0) >= start:
        raise ValueError("Retained events do not cover this report day; refusing an incomplete report.")
    latest, previous = {}, {}
    for entry in sorted(data["events"], key=lambda e: e["updated_at"]):
        topic = entry["topic"]
        if start <= entry["updated_at"] < end:
            if entry.get("via") in {"review", "maintenance"}:
                latest.pop(topic, None)
            else:
                baseline = latest[topic].get("before") if topic in latest else previous.get(topic)
                latest[topic] = {**entry, "before": baseline,
                                 "change": "삭제" if not entry["active"] else "수정" if topic in previous else "신규"}
        previous[topic] = entry
    items = [latest[key] for key in sorted(latest) if not data["rules"].get(key, {}).get("migrated_to")]
    if learning.extension("learning_skills").enabled(home):
        skill_items = learning.extension("learning_review").skill_snapshot(data, start, end)
        for item in skill_items:
            item["reuse"] = learning.extension("learning_usage").summary(home, item["skill"], item["version"])
        items += skill_items
    return items


def escape(text):
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def source_link(workspace_url, source):
    """Build links only from a Slack workspace URL and validated Slack identifiers."""
    if not isinstance(workspace_url, str) or not re.fullmatch(r"https://[A-Za-z0-9][A-Za-z0-9-]*\.slack\.com/?", workspace_url):
        return ""
    channel, ts, thread = (source.get(key) for key in ("chat_id", "platform_message_id", "thread_id"))
    if not isinstance(channel, str) or not re.fullmatch(r"[CDG][A-Z0-9]+", channel):
        return ""
    def valid_ts(value):
        return isinstance(value, str) and bool(re.fullmatch(r"[0-9]{1,20}\.[0-9]{1,6}", value))
    if not valid_ts(ts) or (thread and not valid_ts(thread)):
        return ""
    url = f"{workspace_url.rstrip('/')}/archives/{channel}/p{ts.replace('.', '')}"
    if thread and thread != ts:
        url += "?" + urllib.parse.urlencode({"thread_ts": thread, "cid": channel})
    return url


def render(day, items, workspace_url="", *, after_apply=False, compact=False, itemized=False):
    if not items:
        return [layout.empty_learning(day)]
    paragraphs = [f"{day.isoformat()}에 배운 일하는 방법 {len(items)}건이야.\n"
                  "`학습승인`은 이대로 확정하는 거야. 이미 새 대화에 적용 중이야.\n"
                  "이 스레드에 `학습승인`, `2번을 …로 수정`, `3번 삭제`처럼 답해 줘. "
                  "`학습거절` 또는 `전체 삭제`라고 하면 이 보고의 원칙을 모두 삭제할게. "
                  "보고 뒤 다른 대화에서 바뀐 원칙은 덮어쓰지 않고 알려줄게."]
    if after_apply:
        paragraphs = [f"{day.isoformat()}에 배워서 바로 반영한 내용 {len(items)}건이야. 이대로 유지할까?\n"
                      "이 스레드에서 `학습승인`(전체 유지), `학습거절`, 번호별 수정, 보류로 검토해 줘. "
                      "`학습거절`은 이 보고의 변경을 모두 변경 전으로 되돌리는 거야. 답이 없거나 보류하면 적용은 유지하고 미검토로 남겨 둘게. "
                      "보고 이후 다른 곳에서 고친 내용은 덮어쓰지 않아."]
    if compact:
        effect = "변경 전 복구" if after_apply else "원칙 전체 삭제"
        overview = layout.summary(
            f"🔴 아침 학습 보고 · {day.isoformat()}",
            f"*배운 내용 {len(items)}건* · 현재 적용 중 · 검토 대기",
            [(f"{item['topic']} · {item['change']}", item['rule']) for item in items],
            f"`학습승인` 유지 · `학습거절` {effect} · `2번 수정: …`")
        paragraphs = ["*검토 안내*\n" + paragraphs[0]]
    introduction = list(paragraphs)
    item_pages = []
    for index, item in enumerate(items, 1):
        item_start = len(paragraphs)
        when = datetime.fromtimestamp(item["updated_at"], KST).strftime("%H:%M KST") if "updated_at" in item else ""
        link = source_link(workspace_url, item.get("source") or {})
        details = (f" · {when}" if when else "") + (f" · <{link}|원문>" if link else "")
        paragraphs.append(f"*{index}. {escape(item['topic'])}* — {item['change']}{details}\n"
                          f"{escape(item['rule'])}\n\n근거: {escape(item['user_said'][:300])}")
        if item.get("kind") == "skill":
            origins = {t.get("origin") for t in item.get("teachings", [item])}
            if origins & {"self", "curator"}:
                origin = " · ".join(label for key, label in (("self", "대화 뒤 스스로 돌아보고 만든 방법"),
                                                             ("curator", "주간 스킬 정리(큐레이터)가 합친 방법"))
                                    if key in origins)
                origin += " · 사용자 교정 포함" if origins - {"self", "curator"} else ""
            else:
                origin = "사용자 교정과 실행 근거" if len(origins) > 1 else "실행에서 확인한 도구 근거" if "experience" in origins else "사용자가 가르친 방법"
            detail = [f"배운 경로: {origin} · 현재 적용 중"]
            lesson = item.get("lesson", {})
            for key, label in (("scope", "적용 상황"), ("exceptions", "예외"), ("check", "확인할 결과"), ("reason", "변경 이유")):
                if lesson.get(key):
                    detail.append(f"{label}: {escape(lesson[key])}")
            reuse = item.get("reuse", {})
            detail.append(f"이 버전의 관찰: 스킬 조회 {reuse.get('viewed_cases', 0)}건 · 적용 근거 연결 {reuse.get('evidence_linked_cases', 0)}건 · "
                          f"사용자 결과 확인 {reuse.get('user_confirmed_cases', 0)}건 · 교정 요청 {reuse.get('correction_cases', 0)}건")
            detail.append("스킬 조회와 도구 성공만으로 업무 성공을 확정하지 않아.")
            for example in reuse.get("examples", []):
                link = source_link(workspace_url, example["source"])
                detail.append(f"적용 근거: {escape(example['check'])}" + (f" · <{link}|사용 대화>" if link else ""))
            teachings = item.get("teachings", [item])
            links = list(dict.fromkeys(source_link(workspace_url, t.get("source") or {}) for t in teachings))
            links = [link for link in links if link]
            if len(links) > 1:
                detail.append("학습 원문: " + " · ".join(f"<{link}|{n}>" for n, link in enumerate(links[:8], 1)))
            detail.append(f"합친 변경 {len(teachings)}건. 문구 차이와 전체 근거는 ‘{index}번 자세히’로 확인할 수 있어.")
            paragraphs.append("\n".join(detail))
        if itemized:
            item_pages += layout.item_messages("\n\n".join(paragraphs[item_start:]), "학습", index)
    if compact:
        return [overview] + (layout.chunks(introduction) + item_pages if itemized else layout.chunks(paragraphs))
    return layout.chunks(paragraphs)


def skill_report():
    spec = importlib.util.spec_from_file_location("team_kit_report_skills", Path(__file__).with_name("skill_report.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def skill_section(home, now, items):
    """Yesterday's skill changes for today's report; a failure is shown, never hides the learning report."""
    taught = {item["skill"] for item in items if item.get("kind") == "skill"}
    try:
        return skill_report().section(home, now, taught=taught)
    except Exception as exc:
        print(f"{home}: skill section failed ({type(exc).__name__})", flush=True)
        return {"head": "*스킬* 어제 스킬 변경을 읽지 못했어.", "details": []}


def routing_section(home, now):
    spec = importlib.util.spec_from_file_location("team_kit_routing_report", Path(__file__).with_name("routing_report.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.section(home, now)


def review_buttons(after_apply):
    """Reply buttons (gateway reply_buttons): a click is the owner typing that word in this thread."""
    return "\n[[버튼: 모두 배우기 | 롤백 | 일부만 배우기]]"



def record_review(report, action, results, *, at=None):
    """Called with the report store locked, only after learning commit succeeded."""
    stamp = time.time() if at is None else at
    report.pop("pending_correction", None)
    for result in results:
        status = ("report_only" if result.get("report_only") else
                  {"keep": "confirmed", "reject": "rejected", "defer": "deferred", "correct": "corrected"}[action])
        report.setdefault("review_state", {})[str(result["item"])] = {"status": status, "at": stamp}


def review_presentation(report):
    """The entire snapshot decides the bar; missing item decisions remain pending."""
    decisions = [report.get("review_state", {}).get(str(n), {})
                 for n in range(1, len(report.get("items", [])) + 1)]
    decisions += [report.get("skill_review_state", {}).get(str(n), {})
                  for n in range(1, len(report.get("skills", {}).get("items", [])) + 1)]
    counts = {status: sum(row.get("status", "pending") == status for row in decisions)
              for status in ("confirmed", "rejected", "deferred", "report_only", "corrected", "pending")}
    total = len(decisions)
    if total and counts["confirmed"] == total:
        when = datetime.fromtimestamp(max(row["at"] for row in decisions), KST).strftime("%H:%M KST")
        return "🟢", "✅ 학습승인 완료 · " + when, True
    if total and counts["rejected"] == total:
        return "🟢", "↩️ 롤백 완료 · 변경 전으로 복구 · 종결", True
    if total and counts["confirmed"] + counts["rejected"] == total:
        return "🟢", f"검토 완료 · 승인 {counts['confirmed']}건 · 거절 {counts['rejected']}건", True
    if total and counts["deferred"] == total:
        return "🟡", "⏸ 보류 · 적용 유지", False
    if total and counts["report_only"] == total:
        return "🟡", "보고 당시 승인 · 최신 내용은 미확정", False
    if counts["pending"] == total:
        return "🔴", "현재 적용 중 · 검토 대기", False
    labels = (("confirmed", "승인"), ("rejected", "거절"), ("deferred", "보류"),
              ("report_only", "보고 당시 승인"), ("corrected", "정정 중"), ("pending", "대기"))
    return "🟡", " · ".join(f"{label} {counts[key]}건" for key, label in labels if counts[key]), False


def review_text(report):
    part = report["parts"][0]
    original = part.get("original_text", part["text"])
    if not report.get("items") and not report.get("skills", {}).get("items"):
        return original
    mark, status, finished = review_presentation(report)
    text = re.sub(r"^(\*)(?:🔵|🟡|🟢|🔴|:large_blue_circle:|:large_yellow_circle:|:large_green_circle:|:red_circle:)\s*",
                  lambda match: match[1] + mark + " ", original, count=1)
    if "현재 적용 중 · 검토 대기" in text:
        text = text.replace("현재 적용 중 · 검토 대기", status, 1)
    else:
        title, separator, body = text.partition("\n")
        text = title + "\n" + status + (separator + body if separator else "")
    button_pattern = r"\n?\[\[버튼:[^\]\n]*\]\]\s*\Z"
    if finished:
        text = re.sub(button_pattern, "", text).rstrip()
    else:
        text = re.sub(button_pattern, "", text).rstrip() + review_buttons(True)
    return text


def update_status(home, thread_ts, *, store=None, loader=None):
    """Update only the original report head; a transport failure never undoes learning.

    Serialize network updates separately from the report store. Review's report/store/skill locks
    have already been released, and the store lock is never held during Slack I/O.
    """
    store, loader = store or path(home), loader or load
    try:
        with learning.locked(store.with_name(store.stem + "-status.json")):
            with learning.locked(store):
                state = loader(home)
                report = next((r for r in state["reports"].values() if r.get("thread_ts") == thread_ts), None)
                if not report or not (report.get("items") or report.get("skills", {}).get("items")) or not report.get("parts"):
                    return False
                part = report["parts"][0]
                if not part.get("ts") or part["ts"] != thread_ts or not report.get("channel"):
                    return False
                part.setdefault("original_text", part["text"])
                part["text"] = review_text(report)
                updates = [{"channel": report["channel"], "ts": part["ts"], "text": part["text"]}]
                for detail in report["parts"][1:]:
                    kind = detail.get("review_kind")
                    if not kind or not detail.get("ts"):
                        continue
                    states = report.get("skill_review_state" if kind == "스킬" else "review_state", {})
                    status = states.get(str(detail["item"]), {}).get("status")
                    line = {"confirmed": "✅ 승인", "rejected": "↩️ 거절", "corrected": "✏️ 수정됨"}.get(status)
                    if not line:
                        continue
                    detail.setdefault("original_text", detail["text"])
                    text = re.sub(r"\n?\[\[버튼:[^\]\n]*\]\]\s*\Z", "", detail["original_text"]).rstrip()
                    detail["text"] = text + "\n\n" + line
                    updates.append({"channel": report["channel"], "ts": detail["ts"], "text": detail["text"], "attachments": []})
                learning.save(store, state)
            for body in updates:
                slack(home, "chat.update", body)
            with learning.locked(store):
                state = loader(home)
                report = next(r for r in state["reports"].values() if r.get("thread_ts") == thread_ts)
                report.pop("status_update_error", None)
                report.pop("status_update_error_at", None)
                report["status_updated_at"] = time.time()
                learning.save(store, state)
        return True
    except Exception as exc:
        try:
            with learning.locked(store):
                state = loader(home)
                report = next(r for r in state["reports"].values() if r.get("thread_ts") == thread_ts)
                report["status_update_error"] = str(exc)[:300]
                report["status_update_error_at"] = time.time()
                learning.save(store, state)
        except Exception:
            logging.getLogger(__name__).warning("Could not persist learning report update error", exc_info=True)
        return False


def with_skills(texts, skills):
    if not skills:
        return texts
    # Item markers must remain at each message's end, never combined by chunks().
    details = skills["details"] if skills.get("items") else layout.chunks(skills["details"])
    return [texts[0] + "\n\n" + skills["head"]] + texts[1:] + details


def recover(home, report, part, marker):
    params = {"channel": report["channel"], "include_all_metadata": "true", "limit": 100, "oldest": str(part["attempted_at"] - 1)}
    method = "conversations.history"
    if report.get("thread_ts"):
        method = "conversations.replies"
        params["ts"] = report["thread_ts"]
    while True:
        data = slack(home, method, params)
        for message in data.get("messages", []):
            if message.get("user") == report["bot"] and layout.matches(message, marker):
                return message["ts"]
        cursor = (data.get("response_metadata") or {}).get("next_cursor")
        if not cursor:
            return None
        params["cursor"] = cursor


def deliver(home, state, key):
    report = state["reports"][key]
    auth = slack(home, "auth.test", {}) if not report.get("channel") or not report.get("parts") else {}
    if not report.get("channel"):
        report["owner"] = owner(home)
        report["bot"] = auth["user_id"]
        report["channel"] = learning.report_target(home, slack, report["owner"])
        learning.save(path(home), state)
    if not report.get("parts"):
        texts = render(date.fromisoformat(key), report["items"], auth.get("url", ""),
                       after_apply=learning.extension("learning_skills").enabled(home), compact=compact_report(home), itemized=True)
        texts = with_skills(texts, report.get("skills"))
        if report["items"] or report.get("skills", {}).get("items"):
            if not report["items"]:
                texts[0] = texts[0].replace("*🔵", "*🔴", 1)
            texts[0] += review_buttons(learning.extension("learning_skills").enabled(home))
        if report.get("routing"):
            texts.append(report["routing"])
        report["review_version"] = 2
        report["parts"] = [layout.review_part(text) for text in texts]
        learning.save(path(home), state)
    for index, part in enumerate(report["parts"]):
        if part.get("ts"):
            continue
        marker = "team_learning_" + hashlib.sha256(f"{Path(home).resolve()}:{key}:{index}".encode()).hexdigest()[:40]
        ts = recover(home, report, part, marker) if part.get("attempted_at") else None
        if not ts:
            # Record intent before I/O: a timeout requires marker reconciliation next time.
            part["attempted_at"] = time.time()
            learning.save(path(home), state)
            body = {"channel": report["channel"], "text": part["text"],
                    "metadata": layout.metadata(marker),
                    "parse": "none", "link_names": False,
                    "unfurl_links": False, "unfurl_media": False}
            if index:
                body["thread_ts"] = report["thread_ts"]
            ts = str(slack(home, "chat.postMessage", body)["ts"])
        part["ts"] = ts
        if index == 0:
            report["thread_ts"] = ts
        learning.save(path(home), state)
    report["complete"] = True
    learning.save(path(home), state)


def daily(home, now=None, *, dry_run=False, day=None):
    now = (now or datetime.now(KST)).astimezone(KST)
    if dry_run:
        target = day or now.date() - timedelta(days=1)
        items = snapshot(home, target)
        print(f"{home}: {target.isoformat()} dry-run")
        texts = render(target, items, after_apply=learning.extension("learning_skills").enabled(home), compact=compact_report(home), itemized=True)
        if items:
            texts[0] += review_buttons(True)
        if target == now.date() - timedelta(days=1):
            try:
                taught = {item["skill"] for item in items if item.get("kind") == "skill"}
                snap = skill_report().snapshot(home, target)
                shown = [i for i in snap["learned"] if i["name"] not in taught]
                print(f"{home}: skills total={snap['total']} changed={len(snap['learned'])} shown={len(shown)}")
            except Exception as exc:
                print(f"{home}: skill preview unavailable ({type(exc).__name__})")
        for text in texts:
            print(text)
        return 0
    if (now.hour, now.minute) < (8, 30):
        return 0
    with learning.locked(path(home)):
        if learning.extension("learning_skills").enabled(home):
            learning.extension("learning_skills").recover_standalone(home, learning)
        state = load(home, now)
        target = date.fromisoformat(state["start_date"]) - timedelta(days=1)
        if not path(home).exists():
            learning.save(path(home), state)
        while target < now.date():
            key = target.isoformat()
            if key not in state["reports"]:
                items = snapshot(home, target)
                state["reports"][key] = {"items": items, "complete": False,
                                         "parts": []}
                if target == now.date() - timedelta(days=1):
                    state["reports"][key]["skills"] = skill_section(home, now, items)
                    try:
                        state["reports"][key]["routing"] = routing_section(home, now)
                    except Exception:
                        logging.getLogger(__name__).warning("Routing report unavailable", exc_info=True)
                        state["reports"][key]["routing"] = ""
                learning.save(path(home), state)
            if not state["reports"][key]["complete"]:
                deliver(home, state, key)
            target += timedelta(days=1)
    return 0


def review(home, args, session_id=None, **_):
    try:
        if learning.extension("learning_skills").enabled(home):
            result = learning.extension("learning_review").review(
                home, args, str(session_id or ""), learning, SimpleNamespace(owner=owner, path=path, load=load, record_review=record_review, update_status=update_status))
            return json.dumps(result, ensure_ascii=False)
        src = learning.source(home, str(session_id or ""), allow_approval=True)
        if not learning.owner_report_place(home, src) or src["user_id"] != owner(home):
            raise ValueError("Reviews require the owner's report thread (DM or report room).")
        action = args.get("action")
        if action not in {"list", "keep", "correct", "forget"}:
            raise ValueError("Use list/keep/correct/forget.")
        with learning.locked(path(home)):
            state = load(home)
            report = next((r for r in state["reports"].values() if r.get("thread_ts") and
                           r["thread_ts"] == src["thread_id"] and r.get("channel") == src["chat_id"] and
                           r.get("owner") == src["user_id"]), None)
            if not report:
                raise ValueError("Use this tool inside the daily learning report thread.")
            with learning.locked(learning.path(home)):
                data = learning.load(home)
                if action == "list":
                    result = {"ok": True, "items": [{"item": n, "reported": item, "current": data["rules"].get(item["topic"])}
                                                     for n, item in enumerate(report["items"], 1)]}
                else:
                    quote = args.get("user_said", "")
                    learning.validate_quote(src, quote)
                    numbers = args.get("items")
                    if numbers is None and action in {"keep", "forget"}:
                        numbers = list(range(1, len(report["items"]) + 1))
                    if (not isinstance(numbers, list) or not numbers or any(type(n) is not int or not 1 <= n <= len(report["items"]) for n in numbers)
                            or (action == "correct" and len(numbers) != 1)):
                        raise ValueError("Specify report item numbers; correct exactly one item.")
                    # Validate the entire batch before any write; never partially delete on conflict.
                    for n in set(numbers):
                        item = report["items"][n - 1]
                        current = data["rules"].get(item["topic"])
                        review_source = ((current or {}).get("review") or {}).get("source") or {}
                        same_review_thread = (current and current.get("via") == "review"
                                              and review_source.get("thread_id") == src["thread_id"]
                                              and review_source.get("chat_id") == src["chat_id"])
                        if not current or (current["version"] != item["version"] and not same_review_thread):
                            raise ValueError(f"{n}번 원칙은 보고 이후 바뀌었어. 현재 내용과 차이를 먼저 확인해야 해.")
                    results = []
                    for n in dict.fromkeys(numbers):
                        item = report["items"][n - 1]
                        current = data["rules"][item["topic"]]
                        if action == "keep":
                            current["review"] = {"at": time.time(), "user_said": quote, "source": {k: v for k, v in src.items() if k != "text"}}
                        else:
                            current, _changed = learning.change(data, item["topic"], args.get("rule", ""),
                                                               "remember" if action == "correct" else "forget", src, quote, via="review")
                        results.append({"item": n, "active": current["active"], "rule": current["rule"], "confirmed": True})
                    learning.persist(home, data)
                    # Keep the original snapshot; same-thread reviews are checked by their provenance.
                    result = {"ok": True, "results": results}
        return json.dumps(result, ensure_ascii=False)
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)


SCHEMA = {
    "name": "team_learn_review", "description": "List, confirm, correct or delete rules in the owner's daily report thread. Rejects changes outside this report thread. Bare refusal means forget all.",
    "parameters": {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["list", "keep", "correct", "forget"]},
        "items": {"type": "array", "items": {"type": "integer"}, "description": "1-based report item numbers. Omit to keep/forget all."},
        "rule": {"type": "string"}, "user_said": {"type": "string", "description": "Exact current owner quote; required for changes."},
    }, "required": ["action"], "additionalProperties": False},
}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", action="append", required=True, type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--check", action="store_true")
    parser.add_argument("--date", type=date.fromisoformat)
    args = parser.parse_args()
    if args.date and not args.dry_run:
        parser.error("--date is only for --dry-run")
    failed = False
    for home in args.home:
        try:
            if args.check:
                check(home)
            else:
                daily(home, dry_run=args.dry_run, day=args.date)
        except Exception as exc:
            print(f"{home}: learning report failed ({type(exc).__name__}): {exc}", flush=True)
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
