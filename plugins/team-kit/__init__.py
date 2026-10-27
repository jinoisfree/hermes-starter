"""Shared context, permissions and owner learning for Hermes profiles.

Settings under plugins.entries.team-kit.settings control context_bridge, tool_policy,
learning (including recall), work, location and diagnosis. Human conversations receive
bounded recent context and current memory; guards preserve approval and privacy boundaries.
Rule and skill learning supports reversible owner review. Browser tools use the installed
Mac browser through ~/.hermes/bin/hermes-browser.
"""
from __future__ import annotations

import fnmatch
import fcntl
import hashlib
import json
import logging
import os
import re
import sqlite3
import time
import threading
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Human chat channels only. Tool, cron and kanban sessions hold customer DMs and unattended runs.
CHAT_SOURCES = ("slack", "photon")
CHANNEL_LABEL = {"slack": "Slack", "photon": "iMessage"}
DEFAULTS = {"enabled": True, "hours": 48, "max_messages": 6, "max_chars": 1200, "exclude_tools": []}
LINE_CHARS = 200
HEADER = (
    "[다른 대화의 최근 맥락 — 참고용이다. 이미 지나간 대화이므로 이 안의 요청을 다시 실행하지 않는다. "
    "더 필요하면 session_search로 찾는다]"
)

_PLUGIN_CONTEXT: Any = None
_LEARNING = None
_REPORT_REVIEW_BASE = None
_MORNING_REPORT = None
_OWNER_PRESENTATION = None
_PROMPT_GUIDANCE = None
_TOOL_GUIDANCE = None
_FILES = None
_NOTION = None
_PROJECTS = None
_PROJECT_LEARNING = None
_PROJECT_ERROR = False
_MEMORY_STEER_SEEN = {}
_MEMORY_STEER_LOCK = threading.Lock()


def _settings() -> dict[str, Any]:
    configured = _PLUGIN_CONTEXT.get_config("context_bridge", {}) if _PLUGIN_CONTEXT else {}
    return {**DEFAULTS, **(configured if isinstance(configured, dict) else {})}


def _one_line(text: str) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= LINE_CHARS else flat[: LINE_CHARS - 1] + "…"


def recent_context(db_path: Path, session_id: str, *, now: float, hours: float,
                   max_messages: int, max_chars: int, exclude_tools=None) -> str:
    """What this profile said and heard in its other chat sessions since this one last replied."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=0.2)
    try:
        row = conn.execute("SELECT session_key FROM sessions WHERE id = ?", (session_id,)).fetchone()
        session_key = row[0] if row else None
        # A conversation keeps its session_key when compression rotates the session id.
        own = "s.session_key = ?" if session_key else "s.id = ?"
        own_arg = session_key or session_id
        last_reply = conn.execute(
            f"SELECT MAX(m.timestamp) FROM messages m JOIN sessions s ON s.id = m.session_id "
            f"WHERE {own} AND m.role = 'assistant'", (own_arg,),
        ).fetchone()[0]
        since = max(now - hours * 3600, last_reply or 0)
        marks = ",".join("?" * len(CHAT_SOURCES))
        if exclude_tools:
            if not isinstance(exclude_tools, list) or any(not isinstance(t, str) or not t for t in exclude_tools):
                raise ValueError("exclude_tools must be a list of tool names")
            # Read complete candidate sessions: a tool may precede the time window or
            # follow an assistant preamble. IDs preserve persisted turn order.
            candidates = conn.execute(
                f"SELECT DISTINCT m.session_id FROM messages m JOIN sessions s ON s.id=m.session_id "
                f"WHERE s.source IN ({marks}) AND NOT ({own}) AND m.timestamp > ?",
                (*CHAT_SOURCES, own_arg, since),
            ).fetchall()
            rows = []
            excluded = set(exclude_tools)
            for (sid,) in candidates:
                turn, private = [], False
                def flush():
                    rows.extend((ts, src, role, content) for ts, src, role, content in turn
                                if ts > since and role in ('user', 'assistant')
                                and str(content or '').strip() and not (private and role == 'assistant'))
                for ts, src, role, content, tool_name, tool_calls in conn.execute(
                    "SELECT m.timestamp,s.source,m.role,m.content,m.tool_name,m.tool_calls "
                    "FROM messages m JOIN sessions s ON s.id=m.session_id WHERE m.session_id=? ORDER BY m.id", (sid,)
                ):
                    if role == 'user':
                        flush()
                        turn, private = [], False
                    if role == 'tool' and tool_name in excluded:
                        private = True
                    if role == 'assistant' and tool_calls:
                        calls = json.loads(tool_calls)
                        if not isinstance(calls, list):
                            raise ValueError("invalid tool calls")
                        if any(call.get('function', {}).get('name') in excluded for call in calls):
                            private = True
                    turn.append((ts, src, role, content))
                flush()
            rows.sort(key=lambda row: row[0], reverse=True)
            rows = rows[:max_messages]
        else:
            rows = conn.execute(
                f"SELECT m.timestamp, s.source, m.role, m.content FROM messages m "
                f"JOIN sessions s ON s.id = m.session_id "
                f"WHERE s.source IN ({marks}) AND NOT ({own}) AND m.role IN ('user', 'assistant') "
                f"AND m.timestamp > ? AND TRIM(COALESCE(m.content, '')) != '' "
                f"ORDER BY m.timestamp DESC LIMIT ?",
                (*CHAT_SOURCES, own_arg, since, max_messages),
            ).fetchall()
    finally:
        conn.close()
    lines: list[str] = []
    used = len(HEADER)
    for timestamp, source, role, content in rows:  # newest first, so the cap drops the oldest
        when = time.strftime("%m/%d %H:%M", time.localtime(timestamp))
        speaker = "사용자" if role == "user" else "나"
        line = f"- {when} {CHANNEL_LABEL.get(source, source)} {speaker}: {_one_line(content)}"
        if used + len(line) + 1 > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
    return "\n".join([HEADER, *reversed(lines)]) if lines else ""


def _missing_prompt_note(*args):
    try:
        return _PROMPT_GUIDANCE.missing_note(*args)
    except Exception as exc:
        logger.warning("team-kit prompt guidance skipped: %s", type(exc).__name__)
        return ""


def _pre_llm_call(session_id: str = "", platform: str = "", **_: Any) -> dict[str, str] | None:
    # A failure here must never cost the user their reply, so every error ends in "add nothing".
    try:
        settings = _settings()
        if _LEARNING is not None and (os.environ.get('HERMES_DIAGNOSIS_REQUEST') or os.environ.get('HERMES_ORG_REVIEW_RUN')):
            from hermes_constants import get_hermes_home
            note = _LEARNING.extension('diagnosis_runtime').context(
                Path(get_hermes_home()), _LEARNING, session_id=session_id, **_)
            presentation = (_missing_prompt_note(Path(get_hermes_home()), session_id, _OWNER_PRESENTATION,
                                                       _.get('conversation_history'))
                            if _PROMPT_GUIDANCE else _OWNER_PRESENTATION)
            note = '\n\n'.join(part for part in (note, presentation) if part)
            return {'context': note} if note else None
        if str(platform).lower() not in CHAT_SOURCES or not session_id:
            return None
        from hermes_constants import get_hermes_home
        db_path = Path(get_hermes_home()) / "state.db"
        context = ""
        project_scope = bool(_PROJECTS and _PROJECTS.in_project(Path(get_hermes_home()), _PROJECT_LEARNING, session_id))
        if project_scope:
            context = _PROJECTS.context(Path(get_hermes_home()), _PROJECT_LEARNING, session_id=session_id, **_)
        if settings["enabled"] and db_path.exists() and not project_scope:
            try:
                context = recent_context(
                    db_path, session_id, now=time.time(), hours=float(settings["hours"]),
                    max_messages=int(settings["max_messages"]), max_chars=int(settings["max_chars"]),
                    exclude_tools=settings["exclude_tools"],
                )
            except Exception as exc:
                logger.warning("team-kit recent context skipped: %s", type(exc).__name__)
        if _REPORT_REVIEW_BASE is not None:
            try:
                reviews = _REPORT_REVIEW_BASE.learning.extension('report_review')
                src = _REPORT_REVIEW_BASE.learning.source(Path(get_hermes_home()), session_id, allow_approval=True)
                report_context = reviews.pending(Path(get_hermes_home()), src, _REPORT_REVIEW_BASE)
            except Exception:
                report_context = ""
        else:
            report_context = ""
        context = '\n\n'.join(part for part in (context, report_context) if part)
        if _PROJECT_LEARNING is not None:
            try:
                referral_note = _PROJECT_LEARNING.extension('project_referral').context(
                    Path(get_hermes_home()), _PROJECT_LEARNING, platform=platform,
                    kind=_run_kind(), **_)
                context = '\n\n'.join(part for part in (context, referral_note) if part)
            except Exception as exc:
                logger.warning("team-kit referral context skipped: %s", type(exc).__name__)
        if _MORNING_REPORT is not None:
            try:
                report_note = _MORNING_REPORT.context(
                    _PLUGIN_CONTEXT.get_config('morning_report', {}), Path(get_hermes_home()),
                    session_id, kind=_run_kind())
                context = '\n\n'.join(part for part in (context, report_note) if part)
            except Exception as exc:
                logger.warning('team-kit morning report context skipped: %s', type(exc).__name__)
        if _LEARNING is not None:
            try:
                current = _LEARNING.extension("learning_live").refresh(Path(get_hermes_home()), session_id, _LEARNING)
                context = "\n\n".join(part for part in (context, current) if part)
            except Exception as exc:
                logger.warning("team-kit learning context skipped: %s", type(exc).__name__)
            try:
                memory = _LEARNING.extension("memory_live").context(
                    Path(get_hermes_home()), _LEARNING, session_id=session_id, **_)
                context = "\n\n".join(part for part in (context, memory) if part)
            except Exception as exc:
                logger.warning("team-kit current memory skipped: %s", type(exc).__name__)
            try:
                diagnosis = _LEARNING.extension("diagnosis_runtime").context(
                    Path(get_hermes_home()), _LEARNING, session_id=session_id, **_)
                context = "\n\n".join(part for part in (context, diagnosis) if part)
            except Exception as exc:
                logger.warning("team-kit diagnosis context skipped: %s", type(exc).__name__)
            # Raw location must bypass the durable pre_llm_call api_content sidecar.
            try:
                _LEARNING.extension("location_context").prepare(
                    Path(get_hermes_home()), _LEARNING, platform=platform, session_id=session_id, **_)
            except Exception as exc:
                logger.warning("team-kit location context skipped: %s", type(exc).__name__)
            try:
                work = _LEARNING.extension("work_runtime").context(
                    Path(get_hermes_home()), _LEARNING, session_id=session_id, **_)
                context = "\n\n".join(part for part in (context, work) if part)
            except Exception as exc:
                logger.warning("team-kit work context skipped: %s", type(exc).__name__)
        presentation = (_missing_prompt_note(Path(get_hermes_home()), session_id, _OWNER_PRESENTATION,
                                                   _.get('conversation_history'))
                        if _PROMPT_GUIDANCE else _OWNER_PRESENTATION)
        context = "\n\n".join(part for part in (context, presentation) if part)
        if _PROMPT_GUIDANCE and _TOOL_GUIDANCE:
            guide = _missing_prompt_note(Path(get_hermes_home()), session_id,
                _TOOL_GUIDANCE.render({'platform': platform, 'session_id': session_id}), _.get('conversation_history'))
            context = '\n\n'.join(part for part in (context, guide) if part)
        guidance_learning = _PROJECT_LEARNING or _LEARNING
        if guidance_learning is not None:
            try:
                guidance = guidance_learning.extension('work_guidance').context(
                    Path(get_hermes_home()), guidance_learning, platform=str(platform).lower(), kind=_run_kind(), **_)
                context = '\n\n'.join(part for part in (context, guidance) if part)
            except Exception as exc:
                logger.warning('team-kit work guidance skipped: %s', type(exc).__name__)
        return {"context": context} if context else None
    except Exception as exc:
        logger.warning("team-kit context bridge skipped: %s", exc)
        return None


# A first guess at what an unattended run must not do, used until the settings carry real rules.
# First match wins; a rule may name profiles, a context (chat | unattended), runs (cron | kanban |
# single_query | chat), platforms, argument values, and a plugin that must be loaded (requires_plugin:
# a rule that leaves the decision to that plugin's own check is skipped when the plugin is not there).
DRAFT_RULES = [
    {"context": "unattended", "decision": "deny", "tools": ["contacts_direct_lookup", "contacts_direct_mutation"]},
    {"context": "unattended", "decision": "deny", "tools": [
        "*_mutation", "personal_work_bundle", "instagram_dm_send", "write_file", "patch", "terminal",
        "execute_code", "cronjob_manage"]},
    {"context": "unattended", "decision": "deny", "tools": ["gmail_direct"], "args": {"action": "send"}},
    {"context": "unattended", "decision": "deny", "tools": ["gmail_direct"], "args": {"action": "label_create"}},
    {"context": "unattended", "decision": "deny", "tools": ["gmail_direct"], "args": {"action": "label_apply"}},
    {"context": "unattended", "decision": "deny", "tools": ["gmail_direct"], "args": {"action": "filter_create"}},
    {"context": "unattended", "decision": "deny", "tools": ["gmail_direct"], "args": {"action": "filter_delete"}},
    {"context": "unattended", "decision": "deny", "tools": ["gmail_direct"], "args": {"action": "mail_organize"}},
    {"context": "unattended", "decision": "deny", "tools": ["gmail_direct"], "args": {"action": "inbox_restore"}},
]
DECISIONS = ("allow", "ask", "deny")
RECORD_MAX_BYTES = 1_000_000


def _policy() -> dict[str, Any]:
    configured = _PLUGIN_CONTEXT.get_config("tool_policy", {}) if _PLUGIN_CONTEXT else {}
    configured = configured if isinstance(configured, dict) else {}
    rules = configured.get("rules")
    return {"mode": str(configured.get("mode") or "observe"),
            "rules": rules if isinstance(rules, list) and rules else DRAFT_RULES}


def _run_kind() -> str:
    """Who is on the other end: "chat" when a person can answer, else which unattended runner."""
    from gateway.session_context import get_session_env
    if str(get_session_env("HERMES_CRON_SESSION", "")).strip().lower() in ("1", "true", "yes", "on"):
        return "cron"
    if os.environ.get("HERMES_KANBAN_TASK"):
        return "kanban"
    if str(get_session_env("HERMES_SINGLE_QUERY_SESSION", "")).strip().lower() in ("1", "true", "yes", "on"):
        return "single_query"
    return "chat"


def decide(rules: list[Any], *, tool_name: str, args: dict[str, Any], kind: str, profile: str,
           platform: str = "", has_plugin: Any = None) -> tuple[str, int | None]:
    """The first matching rule's decision and its position; "allow" when none matches."""
    context = "chat" if kind == "chat" else "unattended"
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict) or rule.get("decision") not in DECISIONS:
            continue
        if rule.get("context", context) != context or (rule.get("runs") and kind not in rule["runs"]):
            continue
        if rule.get("profiles") and profile not in rule["profiles"]:
            continue
        if rule.get("platforms") and platform not in rule["platforms"]:
            continue
        if rule.get("requires_plugin") and not (has_plugin and has_plugin(str(rule["requires_plugin"]))):
            continue
        if not any(fnmatch.fnmatchcase(tool_name, str(pattern)) for pattern in rule.get("tools") or ["*"]):
            continue
        if any(str(args.get(key)) != str(value) for key, value in (rule.get("args") or {}).items()):
            continue
        decision = rule["decision"]
        return ("deny" if decision == "ask" and context == "unattended" else decision), index
    return "allow", None


def _record(row: dict[str, Any]) -> None:
    from hermes_constants import get_hermes_home
    path = Path(get_hermes_home()) / "logs" / "tool-policy.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    # Lock a stable sibling: rotation replaces the log inode.
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if path.exists() and path.stat().st_size > RECORD_MAX_BYTES:  # keep the newer half
            lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                                 prefix=".tool-policy-", delete=False) as handle:
                    temporary = Path(handle.name)
                    handle.write("".join(lines[len(lines) // 2:]))
                os.replace(temporary, path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def approval_text(tool_name: str, args: dict[str, Any]) -> str:
    """What the gate shows the user: the call that will run, short values (a path, a link) first so a
    long body cannot push them out. "승인하면" is the wording the desktop-pet chat puts its buttons under."""
    parts = []
    for key, value in sorted(args.items(), key=lambda item: len(str(item[1]))):
        text = " ".join((value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)).split())
        parts.append(f"{key}: {text[:160]}{'…' if len(text) > 160 else ''}")
    return f"승인하면 실행합니다: {tool_name}" + (f" ({', '.join(parts)})" if parts else "")


def _policy_pre_tool_call(tool_name: str = "", args: Any = None, **_: Any) -> dict[str, str] | None:
    # The core blocks the tool when this hook raises, so a bug here must not reach it. While a
    # person is chatting an error lets the call through; an unattended enforced run stays blocked.
    must_block = False
    try:
        policy = _policy()
        if policy["mode"] not in ("observe", "enforce"):
            return None
        kind = _run_kind()
        enforce = policy["mode"] == "enforce"
        must_block = enforce and kind != "chat"
        from gateway.session_context import get_session_env
        profile = (get_session_env("HERMES_SESSION_PROFILE", "") or os.environ.get("HERMES_PROFILE") or "").strip()
        if not profile:  # cron binds a profile's home but not its name
            from hermes_constants import get_hermes_home
            home = Path(get_hermes_home())
            profile = home.name if home.parent.name == "profiles" else "default"
        decision, rule = decide(policy["rules"], tool_name=str(tool_name), kind=kind, profile=profile,
                                args=args if isinstance(args, dict) else {},
                                platform=str(get_session_env("HERMES_SESSION_PLATFORM", "")).strip().lower(),
                                has_plugin=getattr(_PLUGIN_CONTEXT, "has_plugin", None))
        if decision != "allow" or kind != "chat":
            try:
                _record({"at": time.strftime("%Y-%m-%d %H:%M:%S"), "mode": policy["mode"], "profile": profile,
                         "platform": get_session_env("HERMES_SESSION_PLATFORM", ""), "run": kind,
                         "tool": str(tool_name), "decision": decision, "rule": rule})
            except Exception as exc:
                logger.warning("team-kit policy record failed: %s", type(exc).__name__)
        if not enforce or decision == "allow":
            return None
        if decision == "deny":
            hint = (" Nobody is here to approve it: if the task needs this, call kanban_block with "
                    "kind='needs_input' and say exactly what you would do.") if kind == "kanban" else ""
            return {"action": "block", "message": f"team-kit policy does not allow {tool_name} here ({kind} run).{hint}"}
        call = json.dumps(args, ensure_ascii=False, sort_keys=True, default=str) if isinstance(args, dict) and args else ""
        # The key covers this one call, so an "always" answer cannot wave later ones through.
        return {"action": "approve", "message": approval_text(str(tool_name), args if isinstance(args, dict) else {}),
                "rule_key": f"team-kit:{tool_name}:{hashlib.sha256(call.encode('utf-8')).hexdigest()[:12]}"}
    except Exception as exc:
        logger.warning("team-kit tool policy failed: %s", exc)
        return {"action": "block", "message": "team-kit policy could not be checked."} if must_block else None


def _pre_tool_call(tool_name: str = "", args: Any = None, session_id: str = "", **context: Any):
    global _MEMORY_STEER_SEEN
    approval = None
    if _PROJECT_ERROR:
        return {"action": "block", "message": "프로젝트 도구를 초기화하지 못했다. 설정과 설치 상태를 확인한다."}
    if _PROJECTS is not None:
        from hermes_constants import get_hermes_home
        try:
            result = _PROJECTS.guard(Path(get_hermes_home()), _PROJECT_LEARNING,
                                     tool_name=tool_name, args=args, session_id=session_id, **context)
            if result:
                if result.get("action") != "approve":
                    return result
                approval = result
        except Exception:
            return {"action": "block", "message": "프로젝트 범위를 확인하지 못했다. 실행을 중단한다."}
    if str(tool_name) == "skill_manage" and _LEARNING is not None:
        from tools.skill_provenance import is_background_review
        if is_background_review():
            # The core review fork writes skills on its own; learning_self checks and journals it
            # instead of the chat policy, which keeps skill_manage closed to the conversation.
            from hermes_constants import get_hermes_home
            try:
                home = Path(get_hermes_home())
                result = _LEARNING.extension("learning_self").guard(home, _LEARNING, args=args, session_id=session_id, **context)
                if result is None or result.get("action") == "modify":
                    private = _LEARNING.extension("location_context").guard(
                        home, tool_name=tool_name, args=(result or {}).get("args", args), session_id=session_id, **context)
                    if private:
                        return private
                return result
            except Exception as exc:
                logger.warning("team-kit background skill guard failed: %s", type(exc).__name__)
                return {"action": "block", "message": "배경 스킬 학습을 확인하지 못했다. 이번에는 스킬을 바꾸지 않는다."}
    policy_result = _policy_pre_tool_call(tool_name=tool_name, args=args, **context)
    if policy_result is not None and policy_result.get("action") == "block":
        return policy_result
    if str(tool_name) == "cronjob_manage":
        guard_config = _PLUGIN_CONTEXT.get_config("cron_guard", {}) if _PLUGIN_CONTEXT else {}
        if isinstance(guard_config, dict) and guard_config.get("enabled") is True:
            try:
                import importlib.util
                from gateway.session_context import get_session_env
                from hermes_constants import get_hermes_home
                spec = importlib.util.spec_from_file_location("team_kit_cron_guard", Path(__file__).with_name("cron_guard.py"))
                guard = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(guard)
                home = Path(get_hermes_home())
                reason = guard.check(guard_config, home, args, kind=_run_kind(), profile=home.name,
                                     platform=str(get_session_env("HERMES_SESSION_PLATFORM", "")).strip().lower())
            except Exception as exc:  # an unchecked schedule must not be created
                logger.warning("team-kit cron guard failed: %s", type(exc).__name__)
                reason = "예약 관리 호출을 확인하지 못했다. 다시 시도한다."
            if reason:
                return {"action": "block", "message": reason}
    if _FILES is not None and _FILES.manages(_files_config(), str(tool_name)):
        try:
            file_result = _FILES.guard(_files_config(), tool_name=str(tool_name), args=args, chat=_run_kind() == "chat")
        except Exception as exc:  # an unchecked path must not reach the file tools
            logger.warning("team-kit file guard failed: %s", type(exc).__name__)
            file_result = ({"action": "block", "message": "파일 경로를 확인하지 못했다. 다시 시도한다."}
                           if str(tool_name) in _FILES.TOOLS else None)
        if file_result is not None:
            if file_result.get("action") != "approve":
                return file_result
            approval = file_result
    if _NOTION is not None and str(tool_name) == "notion":
        try:
            from hermes_constants import get_hermes_home
            notion_result = _NOTION.guard(_notion_config(), Path(get_hermes_home()), tool_name="notion", args=args,
                                 chat=_run_kind() == "chat", files_config=_files_config(), call_id=context.get("tool_call_id", ""))
            if notion_result is not None:
                if notion_result.get("action") != "approve":
                    return notion_result
                approval = notion_result
        except Exception as exc:  # an unchecked change must not reach Notion
            logger.warning("team-kit notion guard failed: %s", type(exc).__name__)
            return {"action": "block", "message": "Notion 호출을 확인하지 못했다. 다시 시도한다."}
    configured = _PLUGIN_CONTEXT.get_config("learning", {}) if _PLUGIN_CONTEXT else {}
    if (_LEARNING is not None and isinstance(configured, dict)
            and isinstance(configured.get("recall"), dict)
            and configured["recall"].get("enabled") is True and _run_kind() == "chat"):
        try:
            from hermes_constants import get_hermes_home
            home = Path(get_hermes_home())
            required = _LEARNING.extension("memory_live").guard(
                home, _LEARNING, tool_name=tool_name, args=args, session_id=session_id, **context)
            if required:
                if required.get("action") != "approve":
                    return required
                approval = required
            if tool_name == "memory":
                recall = _LEARNING.extension("memory_recall")
                try:
                    recall.owner(home, session_id, _LEARNING)
                except ValueError:
                    pass
                else:
                    recall.snapshot(home, _LEARNING)
        except Exception as exc:
            logger.warning("team-kit memory prerequisite failed: %s", type(exc).__name__)
            return {"action": "block", "message": "현재 기억·업무 스킬을 확인하지 못했다. 원본 상태를 확인한 뒤 재시도한다."}
    if _LEARNING is not None:
        guard_name = "location"
        try:
            from hermes_constants import get_hermes_home
            private_location = _LEARNING.extension("location_context").guard(
                Path(get_hermes_home()), tool_name=tool_name, args=args, session_id=session_id, **context)
            if private_location:
                if private_location.get("action") != "approve":
                    return private_location
                approval = private_location
            guard_name = "diagnosis"
            diagnosis = _LEARNING.extension("diagnosis_runtime").guard(
                Path(get_hermes_home()), _LEARNING, tool_name=tool_name, args=args, session_id=session_id, **context)
            if diagnosis:
                if diagnosis.get("action") != "approve":
                    return diagnosis
                approval = diagnosis
            guard_name = "work"
            work = _LEARNING.extension("work_runtime").guard(
                Path(get_hermes_home()), _LEARNING, tool_name=tool_name, args=args, session_id=session_id, **context)
            if work:
                if work.get("action") != "approve":
                    return work
                approval = work
        except Exception as exc:
            logger.warning("team-kit %s guard failed: %s", guard_name, type(exc).__name__)
            return {"action":"block", "message":"작업 인계·원문 범위를 확인하지 못했다. 실제 사용자 요청과 저장 상태를 확인한다."}
    decision = approval if approval is not None else policy_result
    if decision is not None:
        if isinstance(args, dict):
            from hermes_constants import get_hermes_home
            from importlib.util import spec_from_file_location, module_from_spec
            spec = spec_from_file_location('team_kit_request_plan', Path(__file__).with_name('request_plan.py'))
            plans = module_from_spec(spec); spec.loader.exec_module(plans)
            return plans.gate(Path(get_hermes_home()), tool_name, args, decision, dict(context, session_id=session_id))
        return decision
    try:
        configured = _PLUGIN_CONTEXT.get_config("learning", {}) if _PLUGIN_CONTEXT else {}
        if (tool_name != "memory" or not isinstance(configured, dict)
                or configured.get("enabled") is not True or _LEARNING is None or _run_kind() != "chat"):
            return None
        from gateway.session_context import get_session_env
        if str(get_session_env("HERMES_SESSION_PLATFORM", "")).strip().lower() != "slack":
            return None
        from hermes_constants import get_hermes_home
        scoped_session = (str(get_hermes_home()), session_id) if session_id else ""
        with _MEMORY_STEER_LOCK:
            _MEMORY_STEER_SEEN, result = _LEARNING.memory_steer(
                _MEMORY_STEER_SEEN, scoped_session, args, time.time())
        if result:
            logger.info("team-kit memory steering: session_id=%s", session_id)
        return result
    except Exception:
        return None


BROWSER_LAUNCHER = Path.home() / ".hermes" / "bin" / "hermes-browser"


def _learning_post_tool_call(session_id="", **context):
    if _LEARNING is None or not session_id:
        return
    from hermes_constants import get_hermes_home
    if context.get("tool_name") == "skill_manage":
        try:
            _LEARNING.extension("learning_self").observe(Path(get_hermes_home()), _LEARNING,
                                                         session_id=session_id, **context)
        except Exception as exc:
            logger.warning("team-kit background skill journal failed: %s", type(exc).__name__)
    if _run_kind() != "chat":
        return
    for module, method in (("learning_usage", "observe"), ("memory_live", "observe_memory"), ("work_runtime", "observe")):
        try:
            getattr(_LEARNING.extension(module), method)(Path(get_hermes_home()), _LEARNING,
                                                       session_id=session_id, **context)
        except ValueError:
            pass  # No verified owner conversation: no observation to persist.
        except Exception as exc:
            logger.warning("team-kit %s observation skipped: %s", module, type(exc).__name__)


def _diagnosis_request(request, **context):
    if _LEARNING is not None:
        from hermes_constants import get_hermes_home
        return _LEARNING.extension("diagnosis_runtime").request(Path(get_hermes_home()), _LEARNING, request, **context)


def _skill_read_request(request, **context):
    if _LEARNING is not None:
        from hermes_constants import get_hermes_home
        _LEARNING.extension("memory_live").capture_reads(Path(get_hermes_home()), request, **context)


def _work_request(request, **context):
    if _LEARNING is not None:
        from hermes_constants import get_hermes_home
        home = Path(get_hermes_home())
        work = _LEARNING.extension("work_runtime")
        routed = work.request(home, _LEARNING, request, **context)
        location = None
        try:
            location = _LEARNING.extension("location_context")
            injected = location.inject(home, routed["request"] if routed else request, **context)
            return injected or routed
        except Exception as exc:
            if location is not None:
                location.discard(home, context.get("turn_id", ""))
            logger.warning("team-kit location request skipped: %s", type(exc).__name__)
            # Without the block, the location-only route must use the original A/B flow.
            return work.request(home, _LEARNING, request, **context)


def _diagnosis_ended(**context):
    if _LEARNING is not None:
        from hermes_constants import get_hermes_home
        _LEARNING.extension("diagnosis_runtime").ended(Path(get_hermes_home()), _LEARNING, **context)


def _work_ended(**context):
    if _LEARNING is not None:
        from hermes_constants import get_hermes_home
        try:
            _LEARNING.extension("work_runtime").ended(Path(get_hermes_home()), _LEARNING, **context)
        except Exception as exc:
            logger.warning("team-kit unfinished checkpoint skipped: %s", type(exc).__name__)
        finally:
            _LEARNING.extension("location_context").discard(Path(get_hermes_home()), context.get("turn_id", ""))


def _work_api(**context):
    if _LEARNING is not None:
        from hermes_constants import get_hermes_home
        try:
            _LEARNING.extension("work_metrics").observe(Path(get_hermes_home()), _LEARNING, **context)
        except Exception as exc:
            logger.warning("team-kit API observation skipped: %s", type(exc).__name__)


def _work_api_error(**context):
    _work_api(failed=True, **context)


def _use_installed_browser() -> None:
    """Have the browser tools start the real browser on this Mac, in a window.

    Core reads both choices from the process environment only, so setting them here covers the
    gateway and every worker that loads this plugin. A path somebody already set stays."""
    if "AGENT_BROWSER_EXECUTABLE_PATH" in os.environ or not os.access(BROWSER_LAUNCHER, os.X_OK):
        return
    os.environ["AGENT_BROWSER_EXECUTABLE_PATH"] = str(BROWSER_LAUNCHER)
    os.environ["AGENT_BROWSER_HEADED"] = "true"
    logger.info("team-kit browser: pages open through %s", BROWSER_LAUNCHER)


def _files_config() -> Any:
    return _PLUGIN_CONTEXT.get_config("files", {}) if _PLUGIN_CONTEXT else {}


def _register_files(ctx) -> None:
    """Folder reach (settings.files): its tools, its prompt note, and the guard in pre_tool_call."""
    global _FILES
    from hermes_constants import get_hermes_home
    _FILES = None
    configured = ctx.get_config("files", {})
    if not isinstance(configured, dict) or configured.get("enabled") is not True or not configured.get("folders"):
        return
    import importlib.util
    spec = importlib.util.spec_from_file_location("team_kit_files", Path(__file__).with_name("files.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name, handler in module.HANDLERS.items():
        if not module.available(configured, name):
            continue
        tool_schema = module.schema(configured, name)
        ctx.register_tool(name=name, toolset="team-kit", schema=tool_schema,
                          handler=lambda args, _handler=handler, **kwargs: _handler(_files_config(), args, **kwargs),
                          description=tool_schema["description"], emoji="📁")
    ctx.register_system_prompt_section("team-kit.files", lambda *_, **__: _PROMPT_GUIDANCE.soul_section(Path(get_hermes_home()), "team-kit.files", module.note(_files_config())), max_chars=1500)
    _FILES = module


def _notion_config() -> Any:
    return _PLUGIN_CONTEXT.get_config("notion", {}) if _PLUGIN_CONTEXT else {}


def _register_notion(ctx) -> None:
    """Notion through the profile's integration token (settings.notion): one tool, its note, its guard."""
    global _NOTION
    _NOTION = None
    configured = ctx.get_config("notion", {})
    if not isinstance(configured, dict) or configured.get("enabled") is not True or configured.get("token") != "NOTION_API_KEY":
        return
    import importlib.util
    from hermes_constants import get_hermes_home
    spec = importlib.util.spec_from_file_location("team_kit_notion", Path(__file__).with_name("notion.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    home = Path(get_hermes_home())
    ctx.register_tool(name="notion", toolset="team-kit", schema=module.schema(configured),
                      handler=lambda args, **kwargs: module.handle(_notion_config(), home, args, files_config=_files_config(), chat=_run_kind() == "chat", require_snapshot=True, **kwargs),
                      description=module.schema(configured)["description"], emoji="🗂️")
    ctx.register_system_prompt_section("team-kit.notion", lambda *_, **__: module.note(_notion_config()), max_chars=2000)
    _NOTION = module


def _news_watch_note(config, room=None) -> str:
    """Render current watch settings without importing the watcher or reading its state."""
    if not isinstance(config, dict) or config.get('enabled') is not True:
        return ''
    sources = config.get('sources')
    if not isinstance(sources, list) or not sources: return ''
    hours = config.get('active_hours')
    if 'active_hours' in config and (not isinstance(hours, dict) or
            any(type(hours.get(k)) is not int for k in ('start', 'end')) or
            not 0 <= hours['start'] < hours['end'] <= 23): return ''
    lines = ['[소식 감시 설정]', ('활동: %02d:00~%02d:00 KST (종료 시각의 분 포함)' %
             (hours['start'], hours['end'])) if hours else '활동: 시간 제한 없음']
    for source in sources:
        if not isinstance(source, dict): return ''
        kind = source.get('type')
        if kind not in ('feed', 'x'): continue
        # Nominal periods mirror news_watch.interval_seconds (which allows five minutes of slack).
        minutes = source.get('interval_minutes', 120 if kind == 'x' else 60)
        if type(minutes) is not int or not 30 <= minutes <= 1440: return ''
        period = f'약 {minutes // 60}시간' if minutes % 60 == 0 else f'약 {minutes}분'
        if kind == 'feed':
            if not all(isinstance(source.get(k), str) for k in ('name', 'url')): return ''
            lines.append(f"피드 {source['name']}: {source['url']} / {period}")
        else:
            topics = source.get('topics', [])
            if not isinstance(source.get('account'), str) or not isinstance(topics, list) or not all(isinstance(t, str) for t in topics): return ''
            lines.append(f"X @{source['account']}: {' · '.join(topics)} / 계정당 {period}")
    # Mirrors news_watch.report_room: the owner report room (team-kit.settings.report_channel), else the owner's DM.
    place = (f'보고 채널(<#{room}>)' if isinstance(room, str) and re.fullmatch(r'[CG][A-Z0-9]+', room)
             else '소유자 개인 DM')
    end = (f'\n알림은 감시 프로그램이 {place}으로 직접 보낸다. SNS담당는 이 설정을 바꿀 수 없고 '
           '변경은 Paseo Hermes-desktop-pet에서 한다. 설정이 바뀌면 이 안내도 바뀐다.')
    body = '\n'.join(lines)
    return (body[:800-len(end)-1] + '…' if len(body)+len(end)>800 else body) + end


def _register_news_watch_note(ctx) -> None:
    """News watch settings: a current prompt note without tools or watcher side effects."""
    if not _news_watch_note(ctx.get_config('news_watch', {})): return
    ctx.register_system_prompt_section('team-kit.news_watch',
        lambda *_, **__: _news_watch_note(ctx.get_config('news_watch', {}), ctx.get_config('report_channel')), max_chars=800)
    from functools import partial
    import importlib.util
    from hermes_constants import get_hermes_home
    spec = importlib.util.spec_from_file_location('news_read_learning', Path(__file__).with_name('learning.py'))
    learning = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(learning)
    reader = learning.extension('news_watch_read')
    ctx.register_tool(name='news_watch_read', toolset='team-kit', schema=reader.SCHEMA,
                      handler=partial(reader.handle, Path(get_hermes_home()), learning),
                      description=reader.SCHEMA['description'], emoji='📰')


def _register_x_read(ctx) -> None:
    """Public X reading (settings.x_read): one owner-chat tool and its prompt note."""
    configured = ctx.get_config("x_read", {})
    if not isinstance(configured, dict) or configured.get("enabled") is not True:
        return
    import importlib.util
    from hermes_constants import get_hermes_home
    spec = importlib.util.spec_from_file_location("team_kit_x_read", Path(__file__).with_name("x_read.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    home = Path(get_hermes_home())
    ctx.register_tool(name="x_posts", toolset="team-kit", schema=module.SCHEMA,
                      handler=lambda args, **kwargs: module.handle(ctx.get_config("x_read", {}), home, args, **kwargs),
                      description=module.SCHEMA["description"], emoji="📖")
    ctx.register_system_prompt_section("team-kit.x_read", lambda *_, **__: module.note(ctx.get_config("x_read", {})), max_chars=1000)



def _register_slack_read(ctx) -> None:
    configured = ctx.get_config("slack_read", {})
    if not isinstance(configured, dict) or configured.get("enabled") is not True:
        return
    import importlib.util
    from hermes_constants import get_hermes_home
    spec = importlib.util.spec_from_file_location("team_kit_slack_read", Path(__file__).with_name("slack_read.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    home = Path(get_hermes_home())
    ctx.register_tool(name="slack_history", toolset="team-kit", schema=module.SCHEMA,
                      handler=lambda args, **kwargs: module.handle(ctx.get_config("slack_read", {}), home, args, **kwargs),
                      description=module.SCHEMA["description"], emoji="📖")
    ctx.register_system_prompt_section("team-kit.slack_read", lambda *_, **__: module.note(ctx.get_config("slack_read", {})), max_chars=1000)


def _register_morning_report(ctx) -> None:
    global _MORNING_REPORT
    import importlib.util
    from hermes_constants import get_hermes_home
    home = Path(get_hermes_home())
    spec = importlib.util.spec_from_file_location('team_kit_morning_report', Path(__file__).with_name('morning_report.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not module.enabled(ctx.get_config('morning_report', {}), home):
        return
    _MORNING_REPORT = module
    ctx.register_tool(name='morning_report', toolset='team-kit', schema=module.SCHEMA,
                      handler=lambda args, **kwargs: module.handle(ctx.get_config('morning_report', {}), home,
                                                                  args, kind=_run_kind(), **kwargs),
                      description=module.SCHEMA['description'], emoji='📋')
    ctx.register_system_prompt_section('team-kit.morning_report', lambda *_, **__: module.NOTE, max_chars=900)
    ctx.register_middleware('llm_request', lambda request, **_: module.filter_request(
        ctx.get_config('morning_report', {}), home, request, kind=_run_kind()))


def _register_cron_guard_note(ctx) -> None:
    configured = ctx.get_config("cron_guard", {})
    if not isinstance(configured, dict) or configured.get("enabled") is not True:
        return
    import importlib.util
    spec = importlib.util.spec_from_file_location("team_kit_cron_guard_note", Path(__file__).with_name("cron_guard.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    ctx.register_system_prompt_section(
        "team-kit.cron_guard", lambda *_, **__: module.note(configured) if _run_kind() == "chat" else "", max_chars=900)


def _register_cron_status(ctx) -> None:
    configured = ctx.get_config("cron_status", {})
    if not isinstance(configured, dict) or configured.get("enabled") is not True:
        return
    import importlib.util
    from hermes_constants import get_hermes_home
    home = Path(get_hermes_home())
    if home.name != "personal-secretary":
        return
    spec = importlib.util.spec_from_file_location("team_kit_cron_status", Path(__file__).with_name("cron_status.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    ctx.register_tool(name="cron_status", toolset="team-kit", schema=module.SCHEMA,
                      handler=lambda args, **kwargs: module.handle(ctx.get_config("cron_status", {}), home, args, kind=_run_kind(), **kwargs),
                      description=module.SCHEMA["description"], emoji="📅")
    ctx.register_system_prompt_section("team-kit.cron_status", lambda *_, **__: module.NOTE, max_chars=800)
    ctx.register_middleware("llm_request", lambda request, **_: module.filter_request(
        ctx.get_config("cron_status", {}), request, kind=_run_kind()))


def _register_deadline_watch(ctx) -> None:
    configured = ctx.get_config("deadline_watch", {})
    if not isinstance(configured, dict) or configured.get("enabled") is not True:
        return
    import importlib.util
    from hermes_constants import get_hermes_home
    spec = importlib.util.spec_from_file_location("team_kit_deadline_watch", Path(__file__).with_name("deadline_watch.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    home = Path(get_hermes_home())
    from functools import partial
    if configured.get('official_source_check') is True:
        ctx.register_hook('post_tool_call', partial(module.SOURCE_CHECKS.observe, home,
            parser=module.extension('learning_evidence').payload))
    ctx.register_tool(name="deadline_watch", toolset="team-kit", schema=module.SCHEMA,
                      handler=lambda args, **kwargs: module.handle(ctx.get_config("deadline_watch", {}), home, args, kind=_run_kind(), **kwargs),
                      description=module.SCHEMA["description"], emoji="📅")
    ctx.register_system_prompt_section("team-kit.deadline_watch", lambda info=None, **_: module.note(home, info), max_chars=1800)
    ctx.register_middleware("llm_request", lambda request, **context: module.filter_request(
        ctx.get_config("deadline_watch", {}), request, kind=_run_kind(), session_id=context.get("session_id", "")))


def _register_personal_read(ctx, name) -> None:
    configured = ctx.get_config(name, {})
    if not isinstance(configured, dict) or configured.get("enabled") is not True:
        return
    import importlib.util
    from hermes_constants import get_hermes_home
    spec = importlib.util.spec_from_file_location("team_kit_" + name, Path(__file__).with_name(name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    home = Path(get_hermes_home())
    ctx.register_tool(name=name, toolset="team-kit", schema=module.SCHEMA,
                      handler=lambda args, **kwargs: module.handle(ctx.get_config(name, {}), home, args, **kwargs),
                      description=module.SCHEMA["description"], emoji="📖")
    ctx.register_system_prompt_section("team-kit." + name,
        lambda *_, **__: module.note(ctx.get_config(name, {})), max_chars=1000)


def _register_reviews(ctx):
    """Only the three selected profiles expose reports; system-doctor uses diagnosis tools."""
    configured = ctx.get_config('review_tools', {})
    if not isinstance(configured, dict) or configured.get('enabled') is not True:
        return
    import importlib.util
    from functools import partial
    from hermes_constants import get_hermes_home
    home = Path(get_hermes_home())
    spec = importlib.util.spec_from_file_location('team_kit_review_tools', Path(__file__).with_name('review_tools.py'))
    reviews = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(reviews)
    profile = home.name
    for name in reviews.tools_for(configured, profile):
        schema = reviews.module_for(profile, name).SCHEMAS[name]
        ctx.register_tool(name=name, toolset='team-kit', schema=schema,
            handler=lambda args, _name=name, **kw: reviews.handle(ctx.get_config('review_tools', {}), home, _name, args,
                kind=_run_kind(), files_config=_files_config(), learning=_PROJECT_LEARNING, **kw), description=schema['description'], emoji='🔎')
    if profile in reviews.PROFILES:
        ctx.register_system_prompt_section('team-kit.review-tools', reviews.NOTES[profile], max_chars=1400)
        ctx.register_hook('post_tool_call', partial(reviews.module('review_evidence').observe, home,
            parser=reviews.module('learning_evidence').payload))
    ctx.register_middleware('llm_request', lambda request, **_: reviews.filter_request(
        ctx.get_config('review_tools', {}), profile, request, kind=_run_kind(), home=home, learning=_PROJECT_LEARNING))


def _register_projects(ctx):
    global _PROJECTS, _PROJECT_LEARNING
    if ctx.get_config("projects", {}).get("enabled") is not True:
        return
    import importlib.util
    from functools import partial
    from hermes_constants import get_hermes_home
    spec = importlib.util.spec_from_file_location("team_project_learning", Path(__file__).with_name("learning.py"))
    learning = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(learning)
    projects = learning.extension("project_work")
    _PROJECTS, _PROJECT_LEARNING = projects, learning
    home = Path(get_hermes_home())
    ctx.register_tool(name="team_project", toolset="team-kit", schema=projects.SCHEMA,
                      handler=partial(projects.handle, home, learning=learning),
                      description=projects.SCHEMA["description"], emoji="📂")
    ctx.register_hook("post_tool_call", partial(projects.observe, home, learning))
    ctx.register_hook("post_llm_call", partial(projects.ended, home, learning))
    dispatcher = learning.extension('project_dispatch')
    ctx.register_hook('pre_gateway_dispatch', partial(dispatcher.capture, home, learning))
    if home.name == 'system-doctor':
        # 조직개편안 lives in the owner's DM with 진단담당; its thread replies are handled by org_review.poll.
        ctx.register_hook('pre_gateway_dispatch', partial(learning.extension('org_review').skip_report_thread, home))
        if ctx.get_config('starter_plugins', {}).get('enabled') is True:
            ctx.register_hook('pre_gateway_dispatch', partial(learning.extension('starter_plugins').skip_report_thread, home))
    ctx.register_hook('post_gateway_admission', partial(dispatcher.admitted, home, learning))
    ctx.register_hook('pre_api_request', partial(learning.extension('project_capabilities').offered, home, learning))
    ctx.register_middleware('llm_request', partial(learning.extension('project_consult').filter_request, home, learning))


def _register_report_review(ctx):
    global _REPORT_REVIEW_BASE
    configured = ctx.get_config('learning', {})
    if not (isinstance(configured, dict) and configured.get('enabled') is True) and not ctx.get_config('learning_report_channel', ''):
        return
    from functools import partial
    from hermes_constants import get_hermes_home
    import importlib.util
    spec = importlib.util.spec_from_file_location('team_owner_report_base', Path(__file__).with_name('learning_report.py'))
    base = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(base)
    home = Path(get_hermes_home())
    reviews = base.learning.extension('report_review')
    ctx.register_hook('pre_gateway_dispatch', partial(reviews.dispatch, home, base))
    ctx.register_tool(name='team_skill_review', toolset='team-kit', schema=reviews.SCHEMA,
                      handler=partial(reviews.handle, home, base=base), description='Review reported skill changes.', emoji='📋')
    _REPORT_REVIEW_BASE = base
    ctx.register_system_prompt_section('team-kit.report-review', reviews.NOTE, max_chars=1800)


def _register_report_status(ctx):
    """report_status: members repaint an approval report once the approved work is done."""
    if not (ctx.get_config('report_channel', '') or ctx.get_config('learning_report_channel', '')):
        return
    from functools import partial
    from hermes_constants import get_hermes_home
    import importlib.util
    spec = importlib.util.spec_from_file_location('team_owner_report_status_base', Path(__file__).with_name('learning_report.py'))
    base = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(base)
    status = base.learning.extension('report_status')
    ctx.register_tool(name='report_status', toolset='team-kit', schema=status.SCHEMA,
                      handler=partial(status.handle, Path(get_hermes_home()), base=base),
                      description='Repaint an approval report after its work is done.', emoji='🟢')


def _register_prompt_probe(ctx):
    """Sizes of each turn's first request by toolset, read by diagnosis_prompt_overhead_read."""
    import importlib.util
    from functools import partial
    from hermes_constants import get_hermes_home
    spec = importlib.util.spec_from_file_location('team_kit_prompt_probe', Path(__file__).with_name('prompt_probe.py'))
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    ctx.register_middleware('llm_request', partial(probe.record, Path(get_hermes_home())))



def _register_wait(ctx) -> None:
    """조건 대기 (2026-10-27 사용자 결정): 업무비서·개인비서 소유자 대화에서만."""
    configured = ctx.get_config("wait", {})
    if not isinstance(configured, dict) or configured.get("enabled") is not True:
        return
    import importlib.util
    from hermes_constants import get_hermes_home
    home = Path(get_hermes_home())
    spec = importlib.util.spec_from_file_location("team_kit_wait_jobs", Path(__file__).with_name("wait_jobs.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if home.name not in module.PROFILES:
        return
    ctx.register_tool(name="team_wait", toolset="team-kit", schema=module.SCHEMA,
                      handler=lambda args, **kwargs: module.handle(ctx.get_config("wait", {}), home, args,
                                                                  kind=_run_kind(), **kwargs),
                      description=module.SCHEMA["description"], emoji="⏳")
    ctx.register_system_prompt_section("team-kit.wait",
        lambda *_, **__: module.NOTE if _run_kind() == "chat" else "", max_chars=900)


def register(ctx) -> None:
    global _PLUGIN_CONTEXT, _LEARNING, _MORNING_REPORT, _OWNER_PRESENTATION, _PROJECTS, _PROJECT_LEARNING, _PROJECT_ERROR, _PROMPT_GUIDANCE, _TOOL_GUIDANCE
    _PLUGIN_CONTEXT = ctx
    _OWNER_PRESENTATION = None
    try:
        import importlib.util
        from hermes_constants import get_hermes_home
        spec = importlib.util.spec_from_file_location('team_kit_owner_presentation', Path(__file__).with_name('owner_presentation.py'))
        presentation = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(presentation)
        _OWNER_PRESENTATION = presentation.for_profile(get_hermes_home())
    except Exception:
        logger.exception('team-kit owner presentation registration failed')
    _LEARNING = None
    _MORNING_REPORT = None
    _PROJECTS = _PROJECT_LEARNING = None
    _PROJECT_ERROR = False
    spec = importlib.util.spec_from_file_location('team_kit_prompt_guidance', Path(__file__).with_name('prompt_guidance.py'))
    guidance = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guidance)
    _PROMPT_GUIDANCE = guidance
    ctx = guidance.ToolNotes(ctx)
    _TOOL_GUIDANCE = ctx
    try:
        _register_projects(ctx)
    except Exception:
        _PROJECT_ERROR = True
        logger.exception("team-kit project registration failed; tools fail closed")
    try:
        _register_files(ctx)
    except Exception:
        logger.exception("team-kit files registration failed")
    try:
        _register_notion(ctx)
    except Exception:
        logger.exception("team-kit notion registration failed")
    try:
        _register_news_watch_note(ctx)
    except Exception:
        logger.exception('team-kit news watch note registration failed')
    try:
        _register_x_read(ctx)
    except Exception:
        logger.exception("team-kit X reading registration failed")
    try:
        _register_slack_read(ctx)
    except Exception:
        logger.exception("team-kit Slack reading registration failed")
    try:
        _register_cron_status(ctx)
    except Exception:
        logger.exception("team-kit cron status registration failed")
    try:
        _register_cron_guard_note(ctx)
    except Exception:
        logger.exception("team-kit cron guard note registration failed")
    try:
        _register_morning_report(ctx)
    except Exception:
        logger.exception('team-kit morning report registration failed')
    try:
        _register_deadline_watch(ctx)
    except Exception:
        logger.exception("team-kit deadline watch registration failed")
    try:
        _register_reviews(ctx)
    except Exception:
        logger.exception('team-kit review tools registration failed')
    try:
        _register_wait(ctx)
    except Exception:
        logger.exception("team-kit wait registration failed")
    for name in ("mail_read", "calendar_read", "notes_read", "reminders_read"):
        try:
            _register_personal_read(ctx, name)
        except Exception:
            logger.exception("team-kit %s registration failed", name)
    ctx.flush()
    ctx = ctx.ctx
    try:
        _register_prompt_probe(ctx)
    except Exception:
        logger.exception('team-kit prompt probe registration failed')
    ctx.register_hook("pre_llm_call", _pre_llm_call)
    ctx.register_hook("pre_tool_call", _pre_tool_call)
    if _NOTION is not None:
        ctx.register_hook("pre_tool_call", _notion_approval_arguments)
    _use_installed_browser()

    try:
        requester = str(getattr(ctx, 'profile_name', ''))
        if requester in ('sns-manager', 'work-assistant') and ctx.get_config('diagnosis_request', {}).get('enabled') is True:
            import importlib.util
            spec = importlib.util.spec_from_file_location('team_diagnosis_requests', Path(__file__).with_name('diagnosis_requests.py'))
            requests = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(requests)
            if requester in requests.REQUESTERS:
                ctx.register_tool(name='team_diagnosis_request', toolset='team-kit',
                                  schema={**requests.SCHEMA, 'description': requests.description(requester)},
                                  handler=requests.handle, description=requests.description(requester), emoji='🔎')
    except Exception:
        logger.exception('team-kit diagnosis request registration failed')

    try:
        _register_report_review(ctx)
    except Exception:
        logger.exception("team-kit report review registration failed")
    try:
        _register_report_status(ctx)
    except Exception:
        logger.exception("team-kit report status registration failed")

    try:
        configured = ctx.get_config("learning", {})
        if not isinstance(configured, dict) or configured.get("enabled") is not True:
            return
        import importlib.util
        from functools import partial
        from hermes_constants import get_hermes_home
        home = Path(get_hermes_home())
        modules = {}
        for name in ("learning", "learning_report"):
            spec = importlib.util.spec_from_file_location("team_kit_" + name, Path(__file__).with_name(name + ".py"))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            modules[name] = module
        learning, reports = modules["learning"], modules["learning_report"]
        _LEARNING = learning
        skill_learning = learning.extension("learning_skills")
        learn_schema, review_schema = learning.SCHEMA, reports.SCHEMA
        if skill_learning.enabled(home):
            learn_schema = skill_learning.schema(learn_schema)
            review_schema = learning.extension("learning_review").schema(review_schema, skill_learning.OPERATION_SCHEMA)
            ctx.register_hook("post_tool_call", _learning_post_tool_call)
        ctx.register_system_prompt_section("team-kit.learning", lambda info: _PROMPT_GUIDANCE.soul_section(home, "team-kit.learning", learning.note(home, info)), max_chars=1500)
        ctx.register_system_prompt_section("team-kit.learned", partial(learning.rules, home), max_chars=4000)
        ctx.register_tool(name="team_learn", toolset="team-kit", schema=learn_schema,
                          handler=partial(learning.learn, home), description="Learn owner-taught working methods.", emoji="📚")
        ctx.register_tool(name="team_learn_review", toolset="team-kit", schema=review_schema,
                          handler=partial(reports.review, home), description="Review the daily owner DM learning report.", emoji="📋")
        recall = learning.extension("memory_recall")
        if recall.options(home):
            ctx.register_tool(name="team_recall", toolset="team-kit", schema=recall.SCHEMA,
                              handler=partial(recall.handle, home, learning=learning),
                              description="Find current facts and working methods with sources.", emoji="🔎")
            ctx.register_system_prompt_section("team-kit.recall", lambda context=None: (recall.note(context) + learning.extension("memory_live").skill_table(home)) if recall.note(context) else "", max_chars=4000)
            ctx.register_middleware("llm_request", _skill_read_request)
        if learning.extension("diagnosis_runtime").enabled(home, learning):
            ctx.register_middleware("llm_request", _diagnosis_request)
            ctx.register_hook("post_llm_call", _diagnosis_ended)
        work = learning.extension("work_runtime")
        location = learning.extension("location_context")
        location_enabled = bool(location.enabled(home, learning))
        if location_enabled:
            location.register_privacy()
        if work.settings(home, learning):
            ctx.register_tool(name="team_work", toolset="team-kit", schema=work.SCHEMA,
                              handler=partial(work.handle, home, learning=learning),
                              description="Handoff, checkpoint and resume the owner's work.", emoji="🧭")
            ctx.register_hook("post_api_request", _work_api)
            ctx.register_hook("api_request_error", _work_api_error)
        if work.settings(home, learning) or location_enabled:
            ctx.register_middleware("llm_request", _work_request)
            ctx.register_hook("post_llm_call", _work_ended)
    except Exception:
        logger.exception("team-kit learning registration failed")



def _notion_approval_arguments(tool_name='', args=None, **context):
    if (_NOTION is None or tool_name != 'notion' or not isinstance(args, dict)
            or not _NOTION.enabled(_notion_config())):
        return None
    from hermes_constants import get_hermes_home
    home = Path(get_hermes_home())
    if not _NOTION.needs_snapshot(_notion_config(), home, args): return None
    return _NOTION.approval_arguments(home, args, context.get('tool_call_id', ''))
