"""Offline fingerprint maintenance; never write skill documents or change versions."""
from __future__ import annotations

import argparse
import copy
import importlib.util
import json
from pathlib import Path
import time


def learning_module():
    spec = importlib.util.spec_from_file_location("fingerprint_learning", Path(__file__).with_name("learning.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def homes(root):
    root = Path(root)
    return [home for home in [root, *sorted((root / "profiles").glob("*"))]
            if not home.name.startswith(".") or home == root
            if home.is_dir() and not home.is_symlink() and (home / "learning/rules.json").is_file()]


def fingerprints(home, data, skills):
    differences = []
    for name, entry in sorted(data.get("skills", {}).items()):
        for path, old in sorted(entry.get("fingerprints", {}).items()):
            content = skills.read_document(home, path)
            canonical = path.removeprefix("skills/")
            if canonical != path and content is None and skills.read_document(home, canonical) is not None:
                differences.append({"skill": name, "path": path, "before": old, "after": None,
                                    "action": "remove_prefix", "canonical": canonical})
            elif old != (new := skills.digest(content)):
                differences.append({"skill": name, "path": path, "before": old, "after": new,
                                    "action": "update"})
    return differences


def reported_item(reports, version, review, home, skills):
    source = review.get("source", {})
    for report in reports.get("reports", {}).values():
        if any(source.get(key) and source[key] != report.get(field)
               for key, field in (("chat_id", "channel"), ("thread_id", "thread_ts"))):
            continue
        for item in report.get("items", []):
            if (item.get("kind") == "skill" and item.get("version") == version and
                    item.get("skill") == review.get("skill") and item.get("files") and
                    all(skills.digest(skills.read_document(home, path)) == skills.digest(change["after"])
                        for path, change in item["files"].items())):
                return item
    return None


def plan(home, before, reports, skills, now):
    after = copy.deepcopy(before)
    repairs = []

    def record(name, path, old, new, action):
        repairs.append({"skill": name, "path": path, "before": old, "after": new, "at": now, "action": action})

    for row in fingerprints(home, before, skills):
        name, path = row["skill"], row["path"]
        values = after["skills"][name]["fingerprints"]
        if row["action"] == "remove_prefix":
            del values[path]
            # A prefix-only record must keep a fingerprint for its real document.
            canonical = row["canonical"]
            if canonical not in values:
                new = skills.digest(skills.read_document(home, canonical))
                values[canonical] = new
                record(name, canonical, None, new, "add_canonical")
        else:
            values[path] = row["after"]
        record(name, path, row["before"], row["after"], row["action"])

    for name, entry in after.get("skills", {}).items():
        if entry.get("via") in {"operator_applied", "operator", "maintenance"} and not entry.get("review"):
            entry["review"] = {"status": "operator_applied", "at": entry["updated_at"],
                               "source": {"platform": "paseo", "reason": "fingerprint-adopt"}}
            record(name, "review", skills.digest(None), skills.digest(entry["review"]), "operator_review")

    for version, review in after.get("report_reviews", {}).items():
        entry = after.get("skills", {}).get(review.get("skill"))
        if (review.get("status") != "kept_report_only" or review.get("promoted_at") is not None or
                not entry or review.get("current_version") != version or entry.get("version") != version):
            continue
        if entry.get("review", {}).get("at", 0) > review["at"]:
            continue  # A later explicit review takes precedence over an earlier approval.
        if reported_item(reports, version, review, home, skills) is None:
            continue
        old = skills.digest(entry.get("review"))
        entry["review"] = {key: copy.deepcopy(review[key]) for key in ("at", "user_said", "source")}
        entry["review"]["status"] = "kept"
        review["promoted_at"] = now
        record(entry["skill"], "review", old, skills.digest(entry["review"]), "promote_keep")

    if repairs:
        after.setdefault("fingerprint_repairs", []).extend(repairs)
    return after, repairs


def inspect(home, learning, *, adopt=False, dry_run=False):
    skills = learning.extension("learning_skills")
    if not adopt or dry_run:
        before = learning.load(home)
        if not adopt:
            return fingerprints(home, before, skills)
        reports = learning.extension("learning_report").load(home)
        return plan(home, before, reports, skills, time.time())[1]

    # Use the report -> store -> skill lock order shared by learning_review.review.
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    token = set_hermes_home_override(home)
    try:
        reports_module = learning.extension("learning_report")
        with learning.locked(reports_module.path(home)):
            with learning.locked(learning.path(home)):
                if skills.transaction_path(home).exists():
                    raise ValueError("An unfinished learning transaction needs review before fingerprint adoption.")
                before = learning.load(home)
                with skills.skill_locks(home, set(before.get("skills", {}))):
                    reports = reports_module.load(home)
                    after, repairs = plan(home, before, reports, skills, time.time())
                    if repairs:
                        learning.save(learning.path(home), after)
                    return repairs
    finally:
        reset_hermes_home_override(token)


def main(argv=None, *, root=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="List fingerprint drift without writing (default).")
    mode.add_argument("--adopt", action="store_true", help="Adopt current document fingerprints and repair reviews.")
    parser.add_argument("--dry-run", action="store_true", help="Preview adoption without writing any file.")
    args = parser.parse_args(argv)
    if args.check and args.dry_run:
        parser.error("Use --adopt --dry-run to preview repairs.")
    adopt = args.adopt or args.dry_run
    learning = learning_module()
    count = 0
    for home in homes(Path(root or Path.home() / ".hermes").resolve()):
        rows = inspect(home, learning, adopt=adopt, dry_run=args.dry_run)
        count += len(rows)
        for row in rows:
            print(json.dumps({"home": str(home), **row}, ensure_ascii=False))
        print(json.dumps({"home": str(home), "changes" if adopt else "mismatches": len(rows),
                          "mode": "dry-run" if args.dry_run else "adopt" if adopt else "check"}))
    print(f"Total {'repairs' if adopt else 'mismatches'}: {count}")
    return int(not adopt and bool(count))


if __name__ == "__main__":
    raise SystemExit(main())
