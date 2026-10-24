"""Delete only work-assistant's recorded file_link uploads; never discover other Slack files."""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import time
import urllib.request

_spec = importlib.util.spec_from_file_location("team_kit_cleanup_files", Path(__file__).with_name("files.py"))
files = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(files)


def _allowed() -> bool:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()).resolve() == (Path.home() / ".hermes/profiles/work-assistant").resolve()


def candidates(records, cutoff):
    oldest = sorted(records, key=lambda key: records[key]["recorded_at"])
    excess = set(oldest[:max(0, len(oldest) - 500)])
    return [key for key in oldest if records[key]["recorded_at"] < cutoff or key in excess]


def cleanup(days=30, *, dry_run=False, now=None, opener=None):
    if not _allowed() or days <= 0:
        raise ValueError("cleanup scope refused")
    cutoff = (time.time() if now is None else now) - days * 86400
    with files._link_locked():
        snapshot = files._link_records()
    targets = candidates(snapshot, cutoff)
    if dry_run:
        return {"candidates": len(targets)}
    deleted = failed = 0
    if not targets:
        return {"deleted": 0, "kept": len(snapshot), "failed": 0}
    token = files._link_token()
    if not token:
        return {"deleted": 0, "kept": len(snapshot), "failed": len(targets)}
    for key in targets:
        # The same lock covers file_link's check/upload/timestamp update. Recheck after waiting.
        with files._link_locked():
            records = files._link_records()
            if records.get(key) != snapshot[key] or key not in candidates(records, cutoff):
                continue
            try:
                files._link_api(token, "files.delete", {"file": records[key]["file_id"]},
                                opener or urllib.request.urlopen)
            except files._LinkError as exc:
                if not exc.missing:
                    failed += 1
                    continue
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                failed += 1
                continue
            del records[key]
            files._save_link_records(records)
            deleted += 1
    with files._link_locked():
        kept = len(files._link_records())
    return {"deleted": deleted, "kept": kept, "failed": failed}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = cleanup(args.days, dry_run=args.dry_run)
    except (OSError, ValueError):
        result = {"deleted": 0, "kept": 0, "failed": 1}
    print(json.dumps(result))
    return 1 if result.get("failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
