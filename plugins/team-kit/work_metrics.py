"""Bounded native API observations, without prompts, answers or business payloads."""
from collections import Counter
import json
import math
from pathlib import Path
import time

LIMIT = 500
TOKENS = {"prompt_tokens", "completion_tokens", "total_tokens", "input_tokens", "output_tokens",
          "cache_read_input_tokens", "cache_creation_input_tokens", "reasoning_tokens"}


def file(home):
    result = Path(home) / "state/team-work-metrics.json"
    if result.is_symlink() or result.parent.is_symlink():
        raise ValueError("Work observations cannot follow links.")
    return result


def load(home):
    path = file(home)
    if not path.exists():
        return {"requests": [], "dropped": 0}
    if path.stat().st_size > 2_000_000:
        raise ValueError("Work observations exceed their bound.")
    return json.loads(path.read_text())


def observe(home, learning, *, turn_id="", api_request_id="", api_duration=None, response_model="", started_at=None,
            usage=None, failed=False, **_):
    work = learning.extension("work_runtime")
    turn = work.binding(home, turn_id)
    if not work.settings(home, learning) or not turn or not turn.get("last_route") or not api_request_id:
        return
    route = turn["last_route"]
    row = {"request_id": str(api_request_id), "turn_id": str(turn_id), "at": time.time(),
           "role": route["role"], "requested_model": route["model"], "effort": route["effort"],
           "response_model": str(response_model)[:120], "status": "failed" if failed else "returned"}
    if isinstance(started_at, (int, float)) and math.isfinite(started_at):
        row["started_at"] = started_at
    if isinstance(api_duration, (int, float)) and math.isfinite(api_duration) and api_duration >= 0:
        row["seconds"] = round(api_duration, 4)
    row["usage"] = {k: v for k, v in (usage or {}).items() if k in TOKENS and type(v) is int and v >= 0}
    with learning.locked(file(home)):
        data = load(home)
        # A retry can share an API request id. Preserve each failed attempt and the
        # final returned response, but ignore a duplicate notification.
        previous = data["requests"][-1] if data["requests"] else {}
        if all(previous.get(k) == row.get(k) for k in ("request_id", "status", "started_at")):
            return
        data["requests"].append(row)
        if len(data["requests"]) > LIMIT:
            data["dropped"] += len(data["requests"]) - LIMIT
            data["requests"] = data["requests"][-LIMIT:]
        learning.save(file(home), data)


def summary(home):
    data = load(home)
    rows = data["requests"]
    groups = {}
    for role in sorted({r["role"] for r in rows}):
        selected = [r for r in rows if r["role"] == role]
        durations = sorted(r["seconds"] for r in selected if "seconds" in r and r["status"] == "returned")
        groups[role] = {"attempts": len(selected), "status": dict(Counter(r["status"] for r in selected)),
                        "p50_seconds": durations[len(durations) // 2] if durations else None,
                        "tokens": {key: sum(r["usage"].get(key, 0) for r in selected) for key in
                                   sorted({key for r in selected for key in r["usage"]})}}
    return {"ok": True, "observed_requests": len(rows), "dropped": data["dropped"], "roles": groups,
            "note": "최근 최대 500개 API 호출 관찰이다. 응답 수신·도구 성공·업무 완료·사용자 만족은 별개다. 실제 과금액이나 전체 기간 성공률이 아니다."}
