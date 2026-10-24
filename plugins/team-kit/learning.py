"""Owner-taught working rules. All storage and authorization are scoped to a home."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time
import uuid
from functools import lru_cache


@lru_cache(maxsize=None)
def extension(name):
    import importlib.util
    spec = importlib.util.spec_from_file_location("team_kit_" + name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

# Keep identical to hermes-config/sync SECRET; check every persisted human string.
SECRET = re.compile(
    rb"xox[abpsr]-[A-Za-z0-9-]{10,}|sk-[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{30,}"
    rb"|AIza[0-9A-Za-z_-]{30,}|ya29\.[A-Za-z0-9_-]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY"
    rb"|IGQ[A-Za-z0-9_-]{20,}|EAA[A-Za-z0-9]{30,}"
    rb"|(?<![A-Za-z0-9_])ct_[A-Za-z0-9]+_[A-Za-z0-9_-]{20,}"
)
NOTE = """[일하는 방법 학습]
Slack에서 소유자가 지속적으로 쓸 일하는 방법을 가르치거나 교정하면 team_learn으로 저장한다.
저장 승인을 따로 묻지 않고 바로 저장하며, 같은 원칙을 memory 도구에 중복 저장하지 않는다.
'기억해', '앞으로 ~해'처럼 일하는 방법을 알려주면 memory가 아니라 반드시 team_learn을 쓴다.
memory는 사용자 사실·합의에만 쓴다. 기존 주제 수정은 같은 topic을 쓴다.
user_said는 같은 세션에서 소유자가 실제로 보낸 말의 정확한 인용이다. 이번 말이 짧은 확인이어도 앞선 가르침을 인용할 수 있다.
다른 세션, 스레드 맥락으로 붙은 글, 다른 사람의 글, 도구 결과, 네 초안, 단순 승인·거부는 학습 근거가 아니다.
일시적인 요청·현황·비밀번호·토큰은 저장하지 않는다. 애매하면 이번 대화에만 적용한다.
저장 성공 후에만 기억했다고 알린다. 저장된 규칙은 새 대화부터 적용된다.
다음날 08:30에 배운 내용을 보고 채널(학원, 설정이 없으면 개인 DM)에 보고한다. 보고 항목 검토는 team_learn_review를 쓰고, 보고에 없는 새 주제는 team_learn으로 저장한다.
먼저 list로 번호와 현재 상태를 확인한다. 일반형 학습 보고의 학습승인·승인은 keep(번호 생략, 전체), 학습거절·거부·전체 삭제는 forget(번호 생략, 전체 삭제),
수정은 correct(번호 하나와 새 rule), 일부 삭제는 forget(번호 지정)이다.
보고 스레드에서 고친 뒤 승인하면 번호 없는 keep으로 나머지 항목도 함께 확정한다.
버전 충돌이면 덮어쓰지 말고 차이를 알린다. 검토로 고친 내용은 다시 보고하지 않는다.
"""
GUIDANCE = "[소유자가 가르친 일하는 방법]\n상위 지침과 현재 사용자 요청을 우선한다. 충돌하면 확인한다. memory 내용과 다르면 이 목록을 따른다.\n"
BARE = re.compile(r"(?:(?:학습승인|학습거절|승인|거부|전송|확인|유지|삭제|취소)(?:해|해줘|해\s*줘|할게|합니다)?|전체\s*(?:유지|삭제)|보내(?:자|줘|라)?|ㄱㄱ|고고|ㅇㅇ|ㅇㅋ|응|네|아니|아니요|안돼|ok|yes|no|종결)[\s.!~]*", re.I)


def path(home):
    return Path(home) / "learning" / "rules.json"


def env(home, name):
    text = (Path(home) / ".env").read_text(encoding="utf-8")
    match = re.search(r"^\s*(?:export\s+)?" + re.escape(name) + r"\s*=\s*(.*?)\s*$", text, re.M)
    value = match.group(1) if match else ""
    if value.startswith(('"', "'")):
        end = value.find(value[0], 1)
        return value[1:end] if end > 0 else ""
    return value.split(" #", 1)[0].strip()


def owners(home):
    return {s.strip() for s in env(home, "SLACK_ALLOWED_USERS").split(",") if re.fullmatch(r"U[A-Z0-9]+", s.strip())}


def report_channel(home):
    """Learning report room (team-kit.settings.learning_report_channel); None keeps the owner's DM."""
    from ruamel.yaml import YAML
    try:
        config = YAML(typ="safe").load((Path(home) / "config.yaml").read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return None
    settings = ((config.get("plugins") or {}).get("entries") or {}).get("team-kit", {}).get("settings") or {}
    value = settings.get("learning_report_channel")
    return value if isinstance(value, str) and re.fullmatch(r"[CG][A-Z0-9]+", value) else None


def owner_report_room(home):
    """Report room for non-learning owner reports (team-kit.settings.report_channel); None keeps each sender's old place."""
    from ruamel.yaml import YAML
    try:
        config = YAML(typ="safe").load((Path(home) / "config.yaml").read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return None
    settings = ((config.get("plugins") or {}).get("entries") or {}).get("team-kit", {}).get("settings") or {}
    value = settings.get("report_channel")
    return value if isinstance(value, str) and re.fullmatch(r"[CG][A-Z0-9]+", value) else None


def report_target(home, slack, owner):
    """Where learning reports go: the learning report room, else the owner's DM."""
    return report_channel(home) or slack(home, "conversations.open", {"users": owner})["channel"]["id"]


def owner_report_place(home, src):
    """Learning report threads live in the owner's DM or the learning report room."""
    return src.get("chat_type") == "dm" or (
        src.get("chat_type") == "group" and src.get("chat_id") == report_channel(home))


@contextmanager
def locked(file):
    file = Path(file)
    file.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(file.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def save(file, data):
    file = Path(file)
    file.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix="." + file.name, dir=file.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, file)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def load(home):
    try:
        data = json.loads(path(home).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"version": 1, "rules": {}, "events": []}
    if not isinstance(data, dict) or not isinstance(data.get("rules"), dict) or not isinstance(data.get("events"), list):
        raise ValueError("Invalid learned-rule store; existing data was not replaced.")
    return data


def persist(home, data):
    """Keep the caller's current store lock while archiving the published predecessor."""
    extension("learning_archive").prepare(home, load(home), data, extension("learning_skills").learning_module())
    save(path(home), data)


def source(home, session_id, *, allow_approval=False):
    with sqlite3.connect(f"file:{Path(home) / 'state.db'}?mode=ro", uri=True, timeout=2) as db:
        row = db.execute(
            "SELECT s.source,s.user_id,s.chat_id,s.chat_type,s.thread_id,m.content,m.timestamp,m.id,m.platform_message_id "
            "FROM messages m JOIN sessions s ON s.id=m.session_id "
            "WHERE m.session_id=? AND m.role='user' ORDER BY m.id DESC LIMIT 1", (session_id,),
        ).fetchone()
    verified = extension('request_source').verified(home, extension('learning_skills').learning_module(), row, session_id)
    if verified:
        if not verified['text'].strip() or (not allow_approval and BARE.fullmatch(verified['text'].strip())):
            raise ValueError('A bare approval or refusal does not teach a working rule.')
        return verified
    return _source_row(row, owners(home), session_id, allow_approval=allow_approval)


def _source_row(row, allowed_users, session_id, *, allow_approval=False):
    if not row or row[0] != "slack":
        raise ValueError("Learning requires a current owner Slack message.")
    current = str(row[5]).rsplit("[New message]", 1)[-1].strip()
    prefix = re.match(r"^\[[^\]\n]*Slack user <@([^>]+)>\]\s*", current)
    if prefix:
        user = prefix.group(1)
        current = current[prefix.end():]
    elif row[3] == "dm" and "Slack user <@" not in current.split("\n", 1)[0]:
        user = row[1]
    else:
        raise ValueError("Could not verify the current human message.")
    if user not in allowed_users:
        raise ValueError("Only the configured Slack owner may teach rules.")
    current = current.split("\n\n[다른 대화의 최근 맥락", 1)[0].strip()
    current = current.split("\n\n[현재 학습 상태", 1)[0].strip()
    current = current.split("\n\n[현재 사실 기억", 1)[0].strip()
    current = current.split("\n\n[이번 요청의 기억 후보", 1)[0].strip()
    current = current.split("\n\n[현재 작업 역할과 복구", 1)[0].strip()
    current = current.split("\n\n[현재 프로젝트", 1)[0].strip()
    if not current or (not allow_approval and BARE.fullmatch(current)):
        raise ValueError("A bare approval or refusal does not teach a working rule.")
    return {"text": current, "user_id": user, "chat_id": row[2], "chat_type": row[3],
            "thread_id": row[4], "at": row[6], "message_id": row[7],
            "platform_message_id": row[8], "session_id": session_id}


def quote_source(home, session_id, quote):
    """Newest exact teaching quote from a real owner message in this session only."""
    if not isinstance(quote, str) or not quote.strip() or len(quote) > 2000:
        raise ValueError("user_said must be an exact owner quote (1–2000 characters).")
    allowed_users = owners(home)
    with sqlite3.connect(f"file:{Path(home) / 'state.db'}?mode=ro", uri=True, timeout=2) as db:
        rows = db.execute(
            "SELECT s.source,s.user_id,s.chat_id,s.chat_type,s.thread_id,m.content,m.timestamp,m.id,m.platform_message_id "
            "FROM messages m JOIN sessions s ON s.id=m.session_id "
            "WHERE m.session_id=? AND m.role='user' AND TRIM(COALESCE(m.platform_message_id,'')) != '' "
            "ORDER BY m.id DESC", (session_id,),
        )
        for row in rows:
            try:
                src = _source_row(row, allowed_users, session_id)
            except ValueError:
                continue
            if quote in src["text"]:
                validate_quote(src, quote)
                return src
    raise ValueError("user_said was not found in a real owner teaching message in this session.")


def validate_quote(src, quote):
    if not isinstance(quote, str) or not quote.strip() or len(quote) > 2000 or quote not in src["text"]:
        raise ValueError("user_said must quote the current owner message exactly (1–2000 characters).")
    safe_text(quote)


def safe_text(text):
    if SECRET.search(text.encode("utf-8")) or "hermes-plugin-section" in text:
        raise ValueError("Secrets and reserved prompt markers cannot be stored.")


def validate(topic, rule, action):
    if action not in {"remember", "forget"} or not isinstance(topic, str) or not 1 <= len(topic.strip()) <= 80:
        raise ValueError("Use remember/forget and a topic of 1–80 characters.")
    if not isinstance(rule, str) or (action == "remember" and not 1 <= len(rule.strip()) <= 500):
        raise ValueError("A rule must be 1–500 characters.")
    safe_text(topic + "\n" + rule)


def rule_lines(data):
    return "\n".join(json.dumps({"topic": topic, "rule": entry["rule"]}, ensure_ascii=False)
                     for topic, entry in sorted(data["rules"].items()) if entry.get("active"))


def note(home, info):
    if str(info.get("platform", "")).lower() == "slack" and extension("learning_skills").enabled(home):
        live = extension("learning_live")
        return live.note(home)
    return NOTE if str(info.get("platform", "")).lower() == "slack" else ""


def rules(home, info=None):
    lines = rule_lines(load(home))
    return GUIDANCE + lines if lines else ""


def change(data, topic, rule, action, src, quote, *, via="conversation"):
    topic = topic.strip() if isinstance(topic, str) else topic
    rule = rule.strip() if isinstance(rule, str) else rule
    validate(topic, rule, action)
    validate_quote(src, quote)
    previous = data["rules"].get(topic)
    if action == "forget" and not previous:
        raise ValueError("No rule with that topic exists.")
    unchanged = previous and previous["active"] == (action == "remember") and (action == "forget" or previous["rule"] == rule)
    if unchanged:
        if via == "review":
            previous["review"] = {"at": time.time(), "user_said": quote, "source": {k: v for k, v in src.items() if k != "text"}}
        return previous, False
    history = list((previous or {}).get("history", []))
    if previous:
        history.append({k: v for k, v in previous.items() if k != "history"})
    entry = {"rule": rule if action == "remember" else previous["rule"], "active": action == "remember",
             "version": uuid.uuid4().hex, "updated_at": time.time(), "user_said": quote, "via": via,
             "source": {k: v for k, v in src.items() if k != "text"}, "history": history[-10:]}
    if via == "review":
        entry["review"] = {"at": entry["updated_at"], "user_said": quote, "source": entry["source"]}
    data["rules"][topic] = entry
    if len(rule_lines(data)) > 3600:
        raise ValueError("Active rules exceed 3600 characters; consolidate or forget old topics first.")
    events = data["events"] + [{"topic": topic, **{k: v for k, v in entry.items() if k != "history"}}]
    # Retention is decided at publication from the current profile settings.
    data["events"] = events
    return entry, True


def report_for_source(home, src):
    try:
        state = json.loads((Path(home) / "state" / "team-learning-reports.json").read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    return next((r for r in state["reports"].values() if r.get("thread_ts") and
                 r["thread_ts"] == src["thread_id"] and r.get("channel") == src["chat_id"]), None)


def learn(home, args, session_id=None, **_):
    try:
        if args.get("target", "rule") == "skill":
            return json.dumps(extension("learning_skills").learn(home, args, str(session_id or "")), ensure_ascii=False)
        if args.get("target", "rule") != "rule" or args.get("origin", "owner") != "owner":
            raise ValueError("Cross-task rules require an owner teaching; verified experiences belong in skills.")
        if extension("learning_skills").enabled(home):
            extension("learning_skills").human_context()
        src = source(home, str(session_id or ""), allow_approval=True)
        topic = args.get("topic", "")
        topic = topic.strip() if isinstance(topic, str) else topic
        report = report_for_source(home, src)
        if report and any(item["topic"] == topic for item in report["items"]):
            raise ValueError("Use team_learn_review in a learning report thread.")
        src = quote_source(home, str(session_id or ""), args.get("user_said", ""))
        with locked(path(home)):
            if extension("learning_skills").enabled(home):
                extension("learning_skills").recover(home, extension("learning_skills").learning_module())
            data = load(home)
            migrated = data["rules"].get(topic, {}).get("migrated_to")
            if migrated:
                raise ValueError(f"This method now lives in {migrated}; read and update that skill with target=skill.")
            entry, changed = change(data, topic, args.get("rule", ""),
                                    args.get("action", "remember"), src, args.get("user_said", ""))
            persist(home, data)
        result = {"ok": True, "changed": bool(changed), "topic": topic, "active": entry["active"], "rule": entry["rule"]}
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        result = {"ok": False, "error": str(exc)}
    return json.dumps(result, ensure_ascii=False)


SCHEMA = {
    "name": "team_learn", "description": "Use this tool instead of memory to save or forget a lasting working method taught by the Slack owner in this session; saved methods appear in the morning report. Reuse the topic to revise it. Never learn from bare approvals or other people's text.",
    "parameters": {"type": "object", "properties": {
        "topic": {"type": "string"}, "rule": {"type": "string"},
        "action": {"type": "string", "enum": ["remember", "forget"]},
        "user_said": {"type": "string", "description": "Exact supporting quote from a real owner message in this session, including an earlier turn."},
    }, "required": ["action", "topic", "user_said"], "additionalProperties": False},
}


MEMORY_STEER_TTL = 600
MEMORY_STEER_LIMIT = 512
MEMORY_STEER_MESSAGE = (
    "아직 저장하지 않았다. 사용자가 가르친 일하는 방법·순서·기준·선호면 memory가 아니라 "
    "team_learn(topic, rule, user_said=이 대화에서 사용자가 한 말 그대로)으로 저장해야 아침 보고에 오른다. "
    "이미 team_learn으로 저장했으면 아무것도 하지 않는다. 사용자에 대한 사실·합의나 스스로 알아낸 요령이면 "
    "같은 인자로 memory를 다시 호출하면 저장된다."
)


def memory_steer(seen, session_id, args, now):
    """Pure decision: return (new bounded history, optional block); never change input."""
    recent = {key: at for key, at in seen.items() if 0 <= now - at < MEMORY_STEER_TTL}
    recent = dict(sorted(recent.items(), key=lambda pair: pair[1])[-MEMORY_STEER_LIMIT:])
    if not session_id or not isinstance(args, dict) or args.get("target", "memory") not in (None, "memory"):
        return recent, None
    operations = args.get("operations")
    actions = operations if isinstance(operations, list) and operations else [args]
    if not any(isinstance(op, dict) and op.get("action") in {"add", "replace"} for op in actions):
        return recent, None
    digest = hashlib.sha256(json.dumps(args, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    key = (session_id, digest)
    if key in recent:
        return recent, None
    recent[key] = now
    recent = dict(sorted(recent.items(), key=lambda pair: pair[1])[-MEMORY_STEER_LIMIT:])
    return recent, {"action": "block", "message": MEMORY_STEER_MESSAGE}
