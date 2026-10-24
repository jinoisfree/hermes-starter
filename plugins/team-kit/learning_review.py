"""Unified rule/skill review. Reject restores the report's baseline instead of forgetting."""
from __future__ import annotations

import copy
import difflib
from pathlib import Path
import time


def same_thread(entry, src):
    review = (entry or {}).get("review", {})
    source = review.get("source", {})
    return (entry and entry.get("via") == "review" and
            source.get("thread_id") == src["thread_id"] and source.get("chat_id") == src["chat_id"])


def diff_text(files, limit=1600):
    pieces = []
    for name, versions in files.items():
        diff = difflib.unified_diff((versions["before"] or "").splitlines(), (versions["after"] or "").splitlines(),
                                    fromfile=name + " (이전)", tofile=name + " (적용)", lineterm="", n=1)
        pieces.extend(diff)
    text = "\n".join(pieces)
    return text if len(text) <= limit else text[:limit] + "\n… 전체 문서는 해당 스킬에서 확인"


def skill_snapshot(data, start, end):
    if data.get("skill_events_dropped_through", 0) >= start:
        raise ValueError("Retained skill events do not cover this report day.")
    latest = {}
    for entry in data.get("skill_events", []):
        if not start <= entry["updated_at"] < end:
            continue
        name = entry["skill"]
        if entry["via"] != "conversation":
            latest.pop(name, None)
            continue
        old = latest.get(name)
        files = copy.deepcopy(old["files"] if old else {})
        for path, change in entry["files"].items():
            if path in files:
                files[path]["after"] = change["after"]
            else:
                files[path] = copy.deepcopy(change)
        summaries = list(dict.fromkeys([*((old or {}).get("summaries", [])), entry["rule"]]))
        teachings = copy.deepcopy((old or {}).get("teachings", []))
        teachings.append({key: copy.deepcopy(entry.get(key)) for key in
                          ("version", "source", "user_said", "origin", "evidence", "lesson", "rule")})
        latest[name] = {**entry, "files": files, "summaries": summaries[-8:],
                        "before_lesson": copy.deepcopy(old["before_lesson"] if old else entry.get("before_lesson", {})),
                        "teachings": teachings, "change": "스킬 수정", "rule": entry["rule"]}
    return [latest[name] for name in sorted(latest)
            if any(v["before"] != v["after"] for v in latest[name]["files"].values())]


def inspect_item(data, item):
    key = "skills" if item.get("kind") == "skill" else "rules"
    return data.get(key, {}).get(item.get("skill") if key == "skills" else item["topic"])


def public_current(entry):
    if entry is None:
        return None
    result = {key: entry[key] for key in ("kind", "topic", "skill", "rule", "active", "version", "lesson") if key in entry}
    if entry.get("review"):
        result["review"] = {key: entry["review"][key] for key in ("status", "at") if key in entry["review"]}
    return result


def keep_fingerprints(current, item, src, skills):
    """A keep acknowledges this thread's latest reviewed files, with legacy fallback."""
    files = item.get("files")
    if same_thread(current, src) and current.get("review", {}).get("files"):
        files = current["review"]["files"]
    if files:
        return {path: skills.digest(change["after"]) for path, change in files.items()}
    return current["fingerprints"]


def preflight(home, data, item, src, skills, *, action=None):
    current = inspect_item(data, item)
    if not current or (current["version"] != item["version"] and not same_thread(current, src)):
        raise ValueError(f"{item['topic']}은 보고 이후 바뀌었어. 현재 내용과 차이를 먼저 확인해야 해.")
    if item.get("kind") == "skill":
        skills.managed_name(home, item["skill"])
        fingerprints = keep_fingerprints(current, item, src, skills) if action == "keep" else current["fingerprints"]
        for path, fingerprint in fingerprints.items():
            if skills.digest(skills.read_document(home, path)) != fingerprint:
                raise ValueError(f"{item['skill']} 문서가 다른 곳에서 바뀌었어. 덮어쓰지 않았어.")
    return current


def historical_keep(home, data, item, src, skills):
    """An old approval acknowledges only that report, never a newer skill or file."""
    if item.get("kind") != "skill":
        return False
    current = inspect_item(data, item)
    if not current:
        return False
    skills.managed_name(home, item["skill"])
    if current["version"] != item["version"] and not same_thread(current, src):
        return True
    return any(skills.digest(skills.read_document(home, path)) != fingerprint
               for path, fingerprint in keep_fingerprints(current, item, src, skills).items())


def rule_change(data, item, current, action, args, src, learning):
    quote = args["user_said"]
    if action in {"keep", "defer"}:
        current["review"] = {"at": time.time(), "user_said": quote,
                             "source": {k: v for k, v in src.items() if k != "text"},
                             "status": "kept" if action == "keep" else "deferred"}
        return
    if action == "reject":
        if "before" not in item:
            raise ValueError("This older report has no rollback baseline; use correct after checking its history.")
        baseline = item["before"]
        rule = baseline["rule"] if baseline else ""
        op = "remember" if baseline and baseline.get("active") else "forget"
    else:
        rule, op = args.get("rule", ""), "remember" if action == "correct" else "forget"
    updated, _ = learning.change(data, item["topic"], rule, op, src, quote, via="review")
    updated["review"]["status"] = {"correct": "corrected", "reject": "reverted", "forget": "forgotten"}[action]


def skill_change(home, data, item, current, action, args, src, skills, learning):
    name = item["skill"]
    if (action == "reject" and same_thread(current, src) and current.get("review", {}).get("archived")
            and current["review"].get("status") == "reverted"):
        return {}
    reported_files = item.get("files", {}) if action == "keep" else item["files"]
    baseline = copy.deepcopy(current.get("review", {}).get("files", reported_files)
                             if same_thread(current, src) else reported_files)
    if action == "forget":
        raise ValueError("For a skill, use correct with an exact patch removing the method; reject reverts this change.")
    changes = {}
    archive = False
    if action == "correct":
        changes = skills.plan_documents(home, name, args.get("operations"), learning)
        learning.extension("learning_quality").check(home, name, changes, args.get("rule") or current["rule"], data, skills,
                                                     args.get("lesson") or current.get("lesson"))
        for path, value in changes.items():
            if path in baseline:
                baseline[path]["after"] = value["after"]
            else:
                baseline[path] = copy.deepcopy(value)
    elif action == "reject":
        root = next((path for path, value in baseline.items()
                     if path.endswith("/SKILL.md") and value["before"] is None), None)
        if root:
            archive = True
            folder = skills.document(home, root).parent
            for file in folder.rglob("*"):
                if (file.is_file() or file.is_symlink()) and file.relative_to(Path(home) / "skills").as_posix() not in baseline:
                    raise ValueError("새 스킬에 보고 이후 추가된 파일이 있어. 문서를 확인하기 전에는 스킬을 되돌리지 않았어.")
        changes = {path: {"before": skills.read_document(home, path), "after": value["before"]}
                   for path, value in baseline.items()
                   if skills.read_document(home, path) != value["before"]}
        for path, value in changes.items():
            baseline[path]["after"] = value["after"]
    lesson = (item.get("before_lesson", {}) if action == "reject" else
              learning.extension("learning_quality").lesson(args["lesson"], learning) if action == "correct" and "lesson" in args else None)
    if changes or (lesson is not None and lesson != current.get("lesson", {})):
        entry = skills.event(data, name, args.get("rule") or current["rule"], changes, src, args["user_said"], via="review", lesson=lesson)
        current = data["skills"][entry["skill"]]
    current["review"] = {"at": time.time(), "source": {k: v for k, v in src.items() if k != "text"},
                         "user_said": args["user_said"], "files": baseline,
                         "status": {"keep": "kept", "correct": "corrected", "reject": "reverted", "defer": "deferred"}[action]}
    if archive:
        from tools.skill_usage import archive_skill
        ok, message = archive_skill(name)
        if not ok:
            raise ValueError(message)
        # The core primitive moved the whole package and recorded its ledger entry.
        # Keep rollback events/fingerprints, but never unlink the archived originals.
        current["review"]["archived"] = True
        skills.invalidate_skills()
        return {}
    return changes


def review(home, args, session_id, learning, reports, *, _source=None):
    skills = learning.extension("learning_skills")
    skills.human_context()
    if args.get("action") in {"pending", "status", "assess", "feedback"}:
        return learning.extension("learning_usage").handle(home, args, session_id, learning)
    src = _source if _source is not None else learning.source(home, session_id, allow_approval=True)
    if not learning.owner_report_place(home, src) or src["user_id"] != reports.owner(home):
        raise ValueError("Reviews require the owner's report thread (DM or report room).")
    action = args.get("action")
    if action not in {"list", "keep", "correct", "forget", "reject", "defer"}:
        raise ValueError("Use list/keep/correct/forget/reject/defer.")
    with learning.locked(reports.path(home)):
        report_state = reports.load(home)
        report = next((r for r in report_state["reports"].values() if r.get("thread_ts") and
                       r["thread_ts"] == src["thread_id"] and r.get("channel") == src["chat_id"] and
                       r.get("owner") == src["user_id"]), None)
        if report is None:
            raise ValueError("Use this tool inside the daily learning report thread.")
        with learning.locked(learning.path(home)):
            skills.recover(home, learning)
            before = learning.load(home)
            if action == "list":
                offset = args.get("offset", 0)
                numbers = args.get("items") or list(range(1, len(report["items"]) + 1))
                if (type(offset) is not int or offset < 0 or not isinstance(numbers, list) or
                        any(type(n) is not int or not 1 <= n <= len(report["items"]) for n in numbers)):
                    raise ValueError("Specify valid item numbers and a nonnegative offset.")
                return {"ok": True, "items": [
                    {"item": n, "reported": {k: v for k, v in item.items() if k not in {"files", "fingerprints", "teachings"}},
                     "diff": diff_text(item.get("files", {})), "current": public_current(inspect_item(before, item)),
                     "report_review": before.get("report_reviews", {}).get(item.get("version")),
                     "teachings": item.get("teachings", [])[offset:offset + 8],
                     "next_offset": offset + 8 if len(item.get("teachings", [])) > offset + 8 else None}
                    for n in dict.fromkeys(numbers) for item in [report["items"][n - 1]]]}
            learning.validate_quote(src, args.get("user_said", ""))
            numbers = args.get("items")
            if numbers is None and action in {"keep", "reject", "forget", "defer"}:
                numbers = list(range(1, len(report["items"]) + 1))
            if (not isinstance(numbers, list) or not numbers or
                    any(type(n) is not int or not 1 <= n <= len(report["items"]) for n in numbers) or
                    (action == "correct" and len(numbers) != 1)):
                raise ValueError("Specify report item numbers; correct exactly one item.")
            selected = [report["items"][n - 1] for n in dict.fromkeys(numbers)]
            names = {item["skill"] for item in selected if item.get("kind") == "skill"}
            with skills.skill_locks(home, names):
                after = copy.deepcopy(before)
                historical = {item["version"] for item in selected
                              if action == "keep" and historical_keep(home, before, item, src, skills)}
                # Validate the whole selection before changing either documents or the store.
                for item in selected:
                    if item["version"] not in historical:
                        preflight(home, before, item, src, skills, action=action)
                files = {}
                for item in selected:
                    current = inspect_item(after, item)
                    if item["version"] in historical:
                        after.setdefault("report_reviews", {})[item["version"]] = {
                            "status": "kept_report_only", "at": time.time(), "skill": item["skill"],
                            "user_said": args["user_said"],
                            "source": {k: v for k, v in src.items() if k != "text"},
                            "current_version": current["version"], "current_confirmed": False,
                        }
                    elif item.get("kind") == "skill":
                        files.update(skill_change(home, after, item, current, action, args, src, skills, learning))
                    else:
                        rule_change(after, item, current, action, args, src, learning)
                skills.commit(home, before, after, files, names, learning, session_id)
        results = [
            {"item": n, "current": public_current(inspect_item(after, report["items"][n - 1])),
             "confirmed": action != "defer" and report["items"][n - 1]["version"] not in historical,
             "reported_confirmed": action == "keep",
             "report_only": report["items"][n - 1]["version"] in historical,
             "message": ("보고 당시 학습승인만 기록했어. 최신 스킬과 문서는 그대로이며 최신 내용까지 승인한 것은 아니야."
                         if report["items"][n - 1]["version"] in historical else "")}
            for n in dict.fromkeys(numbers)]
        if action in {"keep", "reject", "defer", "correct"}:
            reports.record_review(report, action, results)
            learning.save(reports.path(home), report_state)
    result = {"ok": True, "results": results}
    if action in {"keep", "reject", "defer", "correct"}:
        result["status_updated"] = reports.update_status(home, report["thread_ts"])
    return result


def schema(base, operations):
    result = copy.deepcopy(base)
    result["description"] = ("Review the owner's daily learning report. 학습 N번 승인=keep items=[N], 학습 N번 거절=reject items=[N]. 학습 N번 수정 asks for the correction first; then correct items=[N]. List first. keep confirms the reported learning, checking only "
                             "the report's files or their latest same-thread review snapshot. Without a file snapshot, legacy reports check all "
                             "fingerprints. A newer skill version outside this thread or changed snapshot file records only the "
                             "reported approval (report_only=true, confirmed=false), without approving "
                             "or changing the latest skill. reject/correct/forget/defer still check all current fingerprints. "
                             "reject restores the report baseline; "
                             "defer leaves it applied but unreviewed. correct changes one rule or skill. "
                             "forget removes a rule; to remove a method from a skill use correct with an exact patch. "
                             "Owner Slack anywhere: pending lists unreviewed learning, status shows observed skill cases; "
                             "assess links current task evidence to a viewed skill (not a success claim); "
                             "feedback records an explicit owner's confirmed/needs_correction outcome for a case.")
    props = result["parameters"]["properties"]
    props["action"]["enum"] += ["reject", "defer", "pending", "status", "assess", "feedback"]
    props["operations"] = operations
    from importlib.util import spec_from_file_location, module_from_spec
    spec = spec_from_file_location("review_quality_schema", Path(__file__).with_name("learning_quality.py"))
    quality = module_from_spec(spec)
    spec.loader.exec_module(quality)
    props.update({"lesson": quality.SCHEMA, "skill": {"type": "string"}, "case_id": {"type": "string"},
                  "offset": {"type": "integer", "minimum": 0}, "check": {"type": "string"},
                  "observation": {"type": "string", "enum": ["failure", "partial", "unknown"], "description": "assess only: observed non-success when no success exists; requires assess_prompt setting; omit evidence."},
                  "evidence": {"type": "string"}, "outcome": {"type": "string", "enum": ["confirmed", "needs_correction"]}})
    return result
