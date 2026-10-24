"""Conservative tool-result classification; content is never treated as a success flag."""
from __future__ import annotations

import json
import re

TOOLS = frozenset({"contacts_direct_lookup", "contacts_direct_mutation", "browser_snapshot", "browser_click", "browser_type", "browser_press", "browser_navigate",
                   "web_search", "web_extract", "calendar_direct_lookup", "reminders_direct_lookup",
                   "apple_notes_direct_lookup", "personal_work_bundle", "read_file", "gmail_direct", "kanban_show"})
FAILURE = {"error", "failed", "failure", "denied", "blocked", "cancelled", "canceled", "timeout", "not_found"}
PARTIAL = {"partial", "partial_success", "incomplete", "pending", "needs_input", "awaiting_approval"}


def payload(tool, value):
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    prefix = f'<untrusted_tool_result source="{tool}">\n'
    if text.startswith(prefix) and text.endswith("\n</untrusted_tool_result>"):
        body = text[len(prefix):-len("\n</untrusted_tool_result>")]
        if body.startswith("The following content was retrieved from an external source."):
            _, separator, body = body.partition("\n\n")
            if not separator:
                return None
        text = body
    try:
        decoded = json.loads(text)
    except (ValueError, TypeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def _negative(data, depth=0):
    if depth > 8:
        return "unknown"
    status = str(data.get("status", "")).lower()
    if status in PARTIAL or data.get("partial") is True or data.get("failures"):
        return "partial"
    if (status in FAILURE or data.get("ok") is False or data.get("success") is False
            or data.get("error") or data.get("errors") or data.get("not_found") is True):
        return "failure"
    # Inspect result envelopes, never arbitrary text/content/DOM or customer records.
    for key in ("receipt", "response", "result", "data", "operations", "results"):
        child = data.get(key)
        children = child if isinstance(child, list) else [child]
        for item in children:
            if isinstance(item, dict):
                negative = _negative(item, depth + 1)
                if negative:
                    return "partial" if data.get("ok") is True or data.get("success") is True else negative
    return None


def contains_quote(value, quote):
    if isinstance(value, str):
        return quote in value
    if isinstance(value, dict):
        return quote in json.dumps(value, ensure_ascii=False) or any(contains_quote(v, quote) for v in value.values())
    if isinstance(value, list):
        return any(contains_quote(v, quote) for v in value)
    return False


def classify(tool, value):
    """success means this tool's result is supported, NOT that the user's task succeeded."""
    data = payload(tool, value)
    if data is None:
        if isinstance(value, str) and re.match(r"\s*(?:error|failed|failure|오류|실패)\s*[:：]", value, re.I):
            return "failure"
        return "unknown"
    negative = _negative(data)
    if negative:
        return negative
    if tool == "skill_view":
        return "success" if data.get("success") is True and not data.get("setup_needed") else "unknown"
    if tool not in TOOLS:
        return "unknown"
    if tool == "personal_work_bundle":
        return "success" if data.get("ok") is True and data.get("status") == "completed" else "unknown"
    if tool == "kanban_show":
        task = data.get("task", {})
        return "success" if isinstance(task, dict) and task.get("status") in {"done", "completed"} and task.get("result") else "unknown"
    if tool == "web_extract":
        results = data.get("results")
        return "success" if isinstance(results, list) and results and all(
            isinstance(item, dict) and item.get("url") and item.get("content") and not item.get("error")
            for item in results) else "unknown"
    if tool == "read_file":
        return "success" if isinstance(data.get("content"), str) and type(data.get("total_lines")) is int else "unknown"
    if tool.startswith("browser_") or tool == "web_search":
        return "success" if data.get("success") is True else "unknown"
    return "success" if data.get("ok") is True else "unknown"
