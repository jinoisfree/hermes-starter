"""Skills the core writes on its own, recorded as reviewable self-learning.

Two core forks reach this path: the post-turn background review (inside an owner's Slack
conversation) and the weekly curator consolidation (no conversation). The core already limits
both to new skills and skills it created itself (``created_by: agent``); owner-taught skills stay
team_learn-only. Each write is checked here first and journaled after, so the morning report can
keep or revert it."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path, PurePosixPath
import re
import threading
import time


KST = timezone(timedelta(hours=9))
ACTIONS = {"create", "patch", "edit", "write_file"}
CURATOR_ACTIONS = ACTIONS | {"delete", "remove_file"}
REFERENCE = re.compile(r"references/[a-z0-9][a-z0-9._-]{0,100}\.md")
DAILY_CREATES = 3
_PENDING = {}
_LOCK = threading.Lock()


def settings(home, learning):
    config = learning.extension("learning_skills").settings(home).get("background_skills", {})
    return config if isinstance(config, dict) else {}


def enabled(home, learning):
    return learning.extension("learning_skills").enabled(home) and settings(home, learning).get("enabled") is True


def _block(message):
    return {"action": "block", "message": message}


def _created_today(data, now):
    start = datetime.fromtimestamp(now, KST).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    return sum(1 for entry in data.get("skill_events", [])
               if entry.get("origin") == "self" and entry.get("updated_at", 0) >= start
               and any(path.endswith("/SKILL.md") and change.get("before") is None
                       for path, change in entry.get("files", {}).items()))


def _curator():
    """The curator's consolidation fork runs off the housekeeping tick, outside any gateway
    conversation; a post-turn review always inherits the conversation's platform."""
    from gateway.session_context import get_session_env
    return not str(get_session_env("HERMES_SESSION_PLATFORM", "")).strip()


def _after(op, action, current):
    if action in {"delete", "remove_file"}:
        if current is None:
            raise ValueError("지울 문서가 없다.")
        return None
    if action == "create":
        if current is not None:
            raise ValueError("이미 있는 스킬이다. skill_view로 읽고 patch한다.")
        return op.get("content")
    if action == "write_file":
        return op.get("file_content")
    if current is None:
        raise ValueError("고칠 문서가 없다. 먼저 skill_view로 현재 문서를 확인한다.")
    if isinstance(op.get("content"), str) and not op.get("old_string"):
        return op["content"]
    old, new = op.get("old_string"), op.get("new_string")
    if not isinstance(old, str) or not old or not isinstance(new, str):
        raise ValueError("patch에는 old_string과 new_string이 필요하다.")
    count = current.count(old)
    if count == 0 or (count > 1 and not op.get("replace_all")):
        raise ValueError("old_string은 현재 문서의 한 구절과 정확히 같아야 한다. skill_view로 다시 읽고 복사한다.")
    return current.replace(old, new) if op.get("replace_all") else current.replace(old, new, 1)


def guard(home, learning, *, args, session_id="", tool_call_id="", **_):
    """Check a review-fork skill_manage call before the core runs it; None or modify lets it run."""
    curator = _curator()
    if not enabled(home, learning):
        return _block("이 조직원은 배경 스킬 학습이 꺼져 있다. 이번 돌아보기에서는 스킬을 바꾸지 않는다.")
    skills = learning.extension("learning_skills")
    config = skills.settings(home)
    folder_name = config.get("skill_folder", "jarvis")
    prefix = next((p for p in config.get("managed_prefixes", []) if p), "")
    args = args if isinstance(args, dict) else {}
    batch = isinstance(args.get("operations"), list)
    ops = args["operations"] if batch else [args]
    if not 1 <= len(ops) <= (24 if curator else 8):
        return _block("한 번에 처리할 작업 수가 너무 많다. 나눠서 호출한다.")
    if curator:
        src = {"session_id": session_id, "trigger": "curator"}
    else:
        try:
            src = learning.source(home, session_id, allow_approval=True)
        except ValueError:
            return _block("소유자 Slack 대화에서만 스스로 스킬을 배운다. 이번 돌아보기에서는 스킬을 바꾸지 않는다.")
    plan, rewritten, creates, absorbed = {}, [], 0, {}
    for op in ops:
        if not isinstance(op, dict) or op.get("action") not in (CURATOR_ACTIONS if curator else ACTIONS):
            return _block("배경 스킬 학습은 만들기와 고치기만 한다. 삭제와 파일 제거는 소유자가 정한다.")
        action = op["action"]
        name = PurePosixPath(str(op.get("name") or args.get("name") or "")).name
        try:
            skills.managed_name(home, name)
        except ValueError:
            return _block(f"스스로 만드는 스킬 이름은 `{prefix}`로 시작해야 한다(예: `{prefix}task-name`). "
                          "이름을 바꿔 다시 호출한다. 이 조직원의 기본·공통 스킬은 고치지 않는다.")
        folder = skills.locate(home, name)
        if action == "delete":
            # Consolidation only: the whole skill moves into its umbrella and the core archives it.
            target = PurePosixPath(str(op.get("absorbed_into") or "")).name
            try:
                skills.managed_name(home, target)
            except ValueError:
                return _block(f"합칠 대상 스킬도 `{prefix}`로 시작해야 한다. absorbed_into를 확인한다.")
            files = [p for p in folder.rglob("*") if p.is_file() or p.is_symlink()]
            if not files or any(p.suffix != ".md" or p.is_symlink() for p in files):
                return _block("문서만 있는 스킬만 합쳐서 보관한다.")
            for file in files:
                relative = file.relative_to(Path(home) / "skills").as_posix()
                current = plan[relative]["after"] if relative in plan else skills.read_document(home, relative)
                plan.setdefault(relative, {"before": current, "skill": name})["after"] = None
            absorbed[name] = target
            rewritten.append(op)
            continue
        path = "SKILL.md" if action == "create" else op.get("file_path") or "SKILL.md"
        if path != "SKILL.md" and not REFERENCE.fullmatch(str(path)):
            return _block("배경 스킬 학습은 SKILL.md와 references/<이름>.md만 쓴다. 스크립트·템플릿은 만들지 않는다.")
        relative = (folder / path).relative_to(Path(home) / "skills").as_posix()
        current = plan[relative]["after"] if relative in plan else skills.read_document(home, relative)
        try:
            if action == "remove_file" and path == "SKILL.md":
                raise ValueError("SKILL.md는 따로 지우지 않는다.")
            after = _after(op, action, current)
            if after is not None:
                skills.validate_text(after, name, path, learning)
        except ValueError as exc:
            return _block(f"스킬 문서 검사에 걸렸다: {exc}")
        plan.setdefault(relative, {"before": current, "skill": name})["after"] = after
        if action == "create":
            creates += 1
            op = {**op, "category": folder_name}
        rewritten.append(op)
    if creates and not curator:
        limit = settings(home, learning).get("daily_creates", DAILY_CREATES)
        if _created_today(learning.load(home), time.time()) + creates > limit:
            return _block(f"오늘 스스로 만든 스킬이 하루 한도({limit}개)에 닿았다. 기존 스킬을 고치거나 다음에 만든다.")
    with _LOCK:
        now = time.time()
        for key in [key for key, value in _PENDING.items() if now - value["at"] > 900]:
            _PENDING.pop(key, None)
        _PENDING[(str(home), session_id, tool_call_id)] = {"plan": plan, "src": src, "at": now,
                                                            "curator": curator, "absorbed": absorbed}
    if rewritten != ops:
        return {"action": "modify", "args": {"operations": rewritten} if batch else rewritten[0]}
    return None


def _summary(skills, home, name, files, curator=False, absorbed_into=""):
    if absorbed_into:
        return f"스킬 정리: `{absorbed_into}`에 합쳐 보관함"
    created = any(path.endswith("/SKILL.md") and change["before"] is None for path, change in files.items())
    description = ""
    text = skills.read_document(home, (skills.locate(home, name) / "SKILL.md").relative_to(Path(home) / "skills").as_posix())
    match = re.search(r"^description:\s*(.+)$", text or "", re.M)
    if match:
        description = match.group(1).strip().strip("'\"")
    head = ("스킬 정리로 만든 스킬" if created else "스킬 정리로 합친 스킬") if curator else (
        "스스로 만든 스킬" if created else "스스로 고친 스킬")
    return (f"{head}: {description}" if description else f"{head}: {name}")[:500]


def observe(home, learning, *, tool_name="", args=None, result=None, session_id="", tool_call_id="", **_):
    """Journal the change the core just made so the morning report can keep or revert it."""
    if tool_name != "skill_manage":
        return
    from tools.skill_provenance import is_background_review
    if not is_background_review():
        return
    with _LOCK:
        pending = _PENDING.pop((str(home), session_id, tool_call_id), None)
    if not pending:
        return
    try:
        outcome = json.loads(result) if isinstance(result, str) else result
    except ValueError:
        return
    if not isinstance(outcome, dict) or not outcome.get("success") or outcome.get("staged"):
        return
    skills = learning.extension("learning_skills")
    changed = {}
    for relative, item in pending["plan"].items():
        actual = skills.read_document(home, relative)
        if actual != item["before"]:
            changed.setdefault(item["skill"], {})[relative] = {"before": item["before"], "after": actual}
    if not changed:
        return
    src, curator = pending["src"], pending.get("curator", False)
    quote = ("주간 스킬 정리(큐레이터)가 스스로 만든 스킬을 합침" if curator else
             "배경 돌아보기 · 대화: " + " ".join(str(src.get("text", "")).split())[:280])
    with learning.locked(learning.path(home)):
        before = learning.load(home)
        after = copy.deepcopy(before)
        for name, files in sorted(changed.items()):
            summary = _summary(skills, home, name, files, curator, pending.get("absorbed", {}).get(name, ""))
            skills.event(after, name, summary, files, src, quote, origin="curator" if curator else "self",
                         evidence={"trigger": "curator" if curator else "background_review",
                                   "session_id": session_id, "tool_call_id": tool_call_id})
        skills.commit(home, before, after, {}, set(), learning)
