"""Send one requested inability report to the lead's configured Slack report channel (보고 채널)."""
from __future__ import annotations

import json
from typing import Any, Callable

# In sending order, each under its label. The user asked for these fixed labels (2026-10-06) as an
# exception to the no-fixed-headings premise; the text under a label stays the lead's own wording.
LABELS = {"undone": "못한 일", "reason": "왜", "user_can": "네가 해줄 것", "system_fix": "시스템에서 바꿀 것",
          "tools": "필요한 도구"}
FIELDS = {
    "undone": "What was not done, separated from what was done.",
    "reason": "Why, grounded in the error or refusal text actually seen; mark an unverified cause as a guess.",
    "user_can": "What the user can do right now to make it possible; say there is nothing when there is nothing.",
    "system_fix": "What has to change in the system to make it possible; say there is nothing when there is nothing.",
    "tools": ("The tool this needs: only names seen in your tool list or in the error/refusal text, otherwise "
              "describe the missing ability without inventing a name; say so when it is a permission, login, "
              "or setting problem rather than a missing tool."),
}
TITLE = "*🔴 못한 일 보고*"
MAX_CHARS = 2000
SCHEMA = {
    "name": "limit_report",
    "description": ("Report an unfinished request to the configured Slack report channel (보고 채널) once. Every field is required and "
                    "written in your own voice without repeating its label; they are sent in order, each under "
                    "its fixed label (못한 일, 왜, 네가 해줄 것, 시스템에서 바꿀 것, 필요한 도구). In that channel, write "
                    "the same five labelled points in your reply instead. Never retry a failed or uncertain send."),
    "parameters": {
        "type": "object",
        "properties": {name: {"type": "string", "minLength": 1, "description": text} for name, text in FIELDS.items()},
        "required": list(FIELDS),
        "additionalProperties": False,
    },
}
NUDGE = (
    "[Unfinished work] In this request these tool calls ended in a failure: {tools}. If the request ends up "
    "not done or only partly done, report it as the 못한 일 보고 section of your instructions says. "
    "If a retry or another way finished it, do not report."
)


def call(args: dict[str, Any], settings: dict[str, Any], *, platform: str, chat_id: str, sender=None) -> str:
    def failure(message: str) -> str:
        return json.dumps({"status": "failed", "sent": False, "error": message,
                           "instruction": "재시도하지 말고 현재 답변에 보고 내용을 모두 담고 보고 채널 전송을 확인하지 못했다고 말하세요."}, ensure_ascii=False)

    if not isinstance(args, dict) or set(args) != set(FIELDS):
        return failure("Exactly these arguments are required: " + ", ".join(FIELDS))
    if any(not isinstance(args[name], str) or not args[name].strip() for name in FIELDS):
        return failure("Every argument must be nonempty text; write that there is nothing when there is nothing")
    message = "\n\n".join([TITLE] + [f"**{LABELS[name]}**: {args[name].strip()}" for name in FIELDS])
    if len(message) > MAX_CHARS:
        return failure(f"The report must be at most {MAX_CHARS} characters in total")
    report_chat_id = settings.get("chat_id")
    if not isinstance(report_chat_id, str) or not report_chat_id.strip():
        return failure("limit_report.chat_id is not configured")
    report_chat_id = report_chat_id.strip()
    if platform.strip().lower() == "slack" and chat_id.strip() == report_chat_id:
        return json.dumps({"status": "ok", "sent": False,
                           "instruction": "이미 지정된 보고 채널입니다. 보고 내용을 현재 답변에 담으세요."}, ensure_ascii=False)
    try:
        if sender is None:
            from tools.send_message_tool import send_message_tool
            sender = send_message_tool
        result = sender({"target": f"slack:{report_chat_id}", "message": message})
        if isinstance(result, str):
            result = json.loads(result)
        if (not isinstance(result, dict) or result.get("success") is not True
                or not isinstance(result.get("message_id"), str) or not result["message_id"].strip()
                or result.get("error")):
            return failure("Report delivery was not confirmed by a successful message receipt")
    except Exception:
        return failure("Report delivery raised an exception or returned an invalid receipt")
    return json.dumps({"status": "ok", "sent": True, "message_id": result["message_id"]}, ensure_ascii=False)


def _failed(texts: list[str]) -> bool:
    """A tool result that says of itself that it failed: this plugin's and the core's error objects."""
    for text in texts:
        try:
            result = json.loads(text)
        except (TypeError, ValueError):
            continue
        if not isinstance(result, dict):
            continue
        if result.get("ok") is False or result.get("success") is False or result.get("status") == "failed":
            return True
        if result.get("error") and result.get("ok") is not True and result.get("success") is not True:
            return True
    return False


def nudge(messages: Any, strings: Callable[[Any], list[str]]) -> str:
    """The reminder for a request whose tools failed since the user last spoke, or "" when none applies.

    A tool counts by its last result, so a retry that worked clears it. Once limit_report was called
    in the same stretch there is nothing left to remind of. Anything unreadable ends in ""."""
    try:
        if not isinstance(messages, list):
            return ""
        start = max((index for index, message in enumerate(messages)
                     if isinstance(message, dict) and message.get("role") == "user"), default=-1)
        names: dict[str, str] = {}
        failed: dict[str, bool] = {}
        for message in messages[start + 1:]:
            if not isinstance(message, dict):
                continue
            if message.get("role") == "assistant":
                for made in message.get("tool_calls") or []:
                    function = made.get("function") if isinstance(made, dict) else None
                    if not isinstance(function, dict):
                        continue
                    name = str(function.get("name") or "")
                    if name == "limit_report":
                        return ""
                    if made.get("id"):
                        names[str(made["id"])] = name
            elif message.get("role") == "tool":
                name = names.get(str(message.get("tool_call_id") or ""), "")
                if name:
                    failed[name] = _failed(strings(message.get("content")))
        tools = sorted(name for name, bad in failed.items() if bad)
        return NUDGE.format(tools=", ".join(tools)) if tools else ""
    except Exception:
        return ""
