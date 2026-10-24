"""Read the current profile's persisted scheduler state without running or changing jobs."""
from __future__ import annotations

import importlib.util
import json
from datetime import datetime
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
FIELDS = ("id", "name", "enabled", "state", "schedule", "schedule_display", "repeat",
          "paused_at", "paused_reason", "next_run_at", "last_run_at", "last_status",
          "last_error", "last_delivery_error", "last_delivery_unverified", "failure_streak",
          "deliver", "failure_deliver")
NOTE = ("[예약 상태 조회] 소유자가 예약 등록 여부·활성 상태·다음 실행·최근 결과를 물으면 "
        "cron_status(action='list') 또는 cron_status(action='get', job_id='예약 ID')로 현재 상태를 읽는다. "
        "닥터아이 정기회의 초안은 업무비서 예약(bbf498cc15a0)이라 이 도구로 보이지 않는다. 1a932448f6da는 2026-10-17 이관 뒤 멈춘 옛 예약이다. 과거 대화·문서만 보고 확인을 Paseo에 떠넘기지 않는다. "
        "next_run_at_kst는 저장된 예정 시각이며 실제 실행·전달 성공을 보장하지 않는다. "
        "last_run_at가 없으면 아직 실행 이력이 없다고 말한다. 새 예약·변경·수동 실행은 이 도구의 범위가 아니다.")


def enabled(config):
    return isinstance(config, dict) and config.get("enabled") is True


def owner(home, session_id):
    spec = importlib.util.spec_from_file_location("cron_status_learning", Path(__file__).with_name("learning.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.source(home, session_id, allow_approval=True)


def snapshot(home, args):
    if not isinstance(args, dict) or set(args) - {"action", "job_id"}:
        raise ValueError("unexpected_arguments")
    action = args.get("action")
    job_id = args.get("job_id")
    if action not in {"list", "get"}:
        raise ValueError("read_only_actions_list_or_get")
    if action == "get" and (not isinstance(job_id, str) or not job_id.strip()):
        raise ValueError("job_id_required")
    if action == "list" and "job_id" in args:
        raise ValueError("use_get_with_job_id")
    path = Path(home) / "cron/jobs.json"
    if path.is_symlink() or path.parent.is_symlink():
        raise ValueError("cron_store_must_be_profile_local")
    data = json.loads(path.read_text(encoding="utf-8"))
    jobs = data.get("jobs") if isinstance(data, dict) else None
    if (not isinstance(jobs, list) or any(not isinstance(j, dict) or not isinstance(j.get("id"), str)
                                         for j in jobs)):
        raise ValueError("invalid_cron_store")
    if action == "get":
        jobs = [j for j in jobs if j["id"] == job_id]
        if len(jobs) != 1:
            raise ValueError("job_not_found_or_duplicated")
    result = []
    for job in jobs:
        item = {key: job.get(key) for key in FIELDS}
        # Same pause gates as cron.jobs.is_job_runnable, plus terminal-state exclusion.
        item["schedule_active"] = (bool(job.get("enabled", True))
                                   and job.get("state") not in {"paused", "completed", "error"}
                                   and not job.get("paused_at"))
        for key in ("next_run_at", "last_run_at"):
            value = job.get(key)
            if value is None:
                item[key + "_kst"] = None
            else:
                dt = datetime.fromisoformat(value)
                if dt.tzinfo is None:
                    raise ValueError("cron_timestamp_timezone_missing")
                item[key + "_kst"] = dt.astimezone(KST).isoformat(timespec="seconds")
        result.append(item)
    return {"ok": True, "jobs": result, "checked_at_kst": datetime.now(KST).isoformat(timespec="seconds"),
            "source": "profile cron/jobs.json", "note": "저장된 예약 상태. 실행·전달 성공이나 게이트웨이 생존을 보장하지 않음."}


def handle(config, home, args, *, kind, session_id="", **_):
    try:
        if not enabled(config) or Path(home).name != "personal-secretary":
            raise ValueError("cron_status_disabled_or_invalid_profile")
        if kind != "chat":
            raise ValueError("cron_status_requires_owner_chat")
        owner(home, session_id)
        return json.dumps(snapshot(home, args), ensure_ascii=False)
    except (ValueError, TypeError, OSError, sqlite3.Error) as exc:
        return json.dumps({"ok": False, "error": str(exc) if isinstance(exc, ValueError)
                           and not isinstance(exc, json.JSONDecodeError) else type(exc).__name__}, ensure_ascii=False)


def filter_request(config, request, *, kind):
    if enabled(config) and kind == "chat":
        return None
    tools = request.get("tools")
    if not isinstance(tools, list):
        return None
    filtered = [t for t in tools if t.get("function", {}).get("name", t.get("name")) != "cron_status"]
    return {"request": dict(request, tools=filtered)} if len(filtered) != len(tools) else None


SCHEMA = {"name": "cron_status", "description": "Read current profile's actual scheduled jobs, including paused jobs, next run and last execution/delivery result. Owner chat only. Read-only; cannot create, change or run jobs.",
          "parameters": {"type": "object", "properties": {
              "action": {"type": "string", "enum": ["list", "get"]},
              "job_id": {"type": "string", "description": "Exact job ID; required for get."}},
              "required": ["action"], "additionalProperties": False}}
