"""새벽 기억 정리 (2026-10-27 사용자 결정: 자동 반영 후 보고).

Every night (launchd ``com.starter_user.hermes.memory-tidy``, 03:40, before the 04:30 settings commit) each
MEMORY.md and the shared USER.md is offered to the main model once, without tools, to merge
duplicates and replace facts that a later entry changed. The answer is checked before anything is
written:

  - every old entry must be accounted for in some new entry's ``from`` (nothing silently lost)
  - no growth: fewer or equal entries and characters, each entry and the file within the limit
  - entries the owner fixed (start with 📌 or contain [확인]) are passed through verbatim and never
    shown to the model as editable
  - the file is written under the memory store's own lock, only if it did not change while the
    model was thinking, as the clean ``\\n§\\n`` list the memory tool round-trips

Old texts of every changed entry go to ``memories/history.jsonl`` (kept by ./sync and git), and a
🟢 report lists the changes in 학습 채널. Running sessions keep their frozen prompt snapshot; new
sessions read the tidied file.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
DELIMITER = "\n§\n"
LIMITS = {"MEMORY.md": 6000, "USER.md": 3000}
PROFILES = ("default", "personal-secretary", "system-doctor", "work-assistant", "research-assistant", "sns-manager")
LABELS = {"default": "위키담당", "personal-secretary": "개인비서", "system-doctor": "진단담당", "work-assistant": "업무비서",
          "research-assistant": "연구담당", "sns-manager": "SNS담당"}
PROVIDER = "claude-subscription-directsdk-experimental"
MODEL = "opus"
LOCKED = re.compile(r"^📌|\[확인\]")

INSTRUCTIONS = """너는 AI 비서의 장기 기억 파일을 정리한다. 아래 번호 붙은 항목은 오래된 것부터 새 것 순서다.
할 일:
- 같은 내용을 말하는 항목은 하나로 합친다.
- 같은 대상의 사실이 바뀌었으면(번호가 클수록 최신) 최신 사실만 남긴 한 항목으로 만든다.
- 서로 관계없는 항목은 글자 하나 바꾸지 않고 그대로 둔다. 다듬기·요약·번역·말투 변경을 하지 않는다.
- 새 사실, 추측, 일반 상식을 더하지 않는다. 합칠 때는 원래 문장의 사실만 쓴다.
- 날짜·숫자·ID·이름·링크는 원문 그대로 둔다.
출력은 JSON 하나만 쓴다:
{"entries":[{"text":"항목 내용","from":[원래 번호,...]}],"changes":["바뀐 점 한 줄",...]}
모든 원래 번호는 정확히 한 entries의 from에 한 번만 들어가야 한다. 그대로 둔 항목은 from에 자기 번호 하나다.
바꿀 것이 없으면 {"entries":[],"changes":[]}만 쓴다."""


def root():
    return Path(os.environ.get("HERMES_ROOT") or Path.home() / ".hermes")


def targets(base=None):
    """(owner label, file path, limit) for every real memory file; the shared USER.md once."""
    base = Path(base or root())
    seen, found = set(), []
    for profile in PROFILES:
        home = base if profile == "default" else base / "profiles" / profile
        for name, limit in LIMITS.items():
            path = home / "memories" / name
            if not path.is_file():
                continue
            real = path.resolve()
            if real in seen:
                continue
            seen.add(real)
            label = "공유 USER.md" if name == "USER.md" else LABELS[profile] + " MEMORY.md"
            found.append((label, real, limit))
    return found


def parse(raw):
    return [e for e in (x.strip() for x in raw.split(DELIMITER)) if e]


def ask_model(entries):
    import hermes_bootstrap  # noqa: F401  launchd's base Python -> the installed environment
    from agent.auxiliary_client import call_llm
    listing = "\n".join(f"[{i}] {text}" for i, text in enumerate(entries))
    response = call_llm(provider=PROVIDER, model=MODEL, timeout=300, max_tokens=8000,
                        messages=[{"role": "system", "content": INSTRUCTIONS},
                                  {"role": "user", "content": "기억 항목:\n" + listing}])
    return response.choices[0].message.content


def decode(answer):
    text = str(answer or "").strip()
    fenced = re.search(r"\{[\s\S]*\}", text)
    if not fenced:
        raise ValueError("model_answer_not_json")
    data = json.loads(fenced.group(0))
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        raise ValueError("model_answer_shape")
    return data


def validate(old, data, limit, budget):
    """New unlocked entries in old order, or None when the model kept everything. Raises on any doubt."""
    entries = data["entries"]
    if not entries:
        return None
    used = []
    result = []
    for item in entries:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str) or not isinstance(item.get("from"), list):
            raise ValueError("entry_shape")
        text = item["text"].strip()
        sources = item["from"]
        if not text or "\n§\n" in f"\n{text}\n" or len(text) > limit:
            raise ValueError("entry_text_invalid")
        if not sources or any(not isinstance(i, int) or not 0 <= i < len(old) for i in sources):
            raise ValueError("entry_from_invalid")
        used += sources
        result.append((min(sources), text, sorted(sources)))
    if sorted(used) != list(range(len(old))):
        raise ValueError("every_old_entry_exactly_once")
    if len(result) > len(old):
        raise ValueError("more_entries_than_before")
    result.sort()
    new = [text for _, text, _ in result]
    if new == old:
        return None
    if sum(map(len, new)) > sum(map(len, old)) or sum(map(len, new)) > budget:
        raise ValueError("tidy_must_not_grow")
    return new, [(sources, text) for _, text, sources in result]


def assemble(raw_entries, locked_idx, mapping):
    """Locked entries keep their slots; each tidied entry takes the slot of its earliest source."""
    slots = [i for i in range(len(raw_entries)) if i not in locked_idx]  # editable index -> file index
    placed = [(i, raw_entries[i]) for i in locked_idx]
    placed += [(slots[min(sources)], text) for sources, text in mapping]
    return [text for _, text in sorted(placed)]


def history(path, record):
    log = path.parent / "history.jsonl"
    with log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def tidy(label, path, limit, *, model=ask_model, now=None, dry_run=False):
    """One file. Returns a result dict (status changed|unchanged|skipped|failed)."""
    from tools.memory_tool_store import MemoryStore
    now = time.time() if now is None else now
    raw = path.read_text(encoding="utf-8-sig")
    entries = parse(raw)
    if raw.strip() and raw.strip() != DELIMITER.join(entries):
        return {"label": label, "status": "skipped", "reason": "not_clean_list"}
    locked_idx = {i for i, e in enumerate(entries) if LOCKED.search(e)}
    editable = [e for i, e in enumerate(entries) if i not in locked_idx]
    if len(editable) < 2:
        return {"label": label, "status": "unchanged"}
    budget = limit - sum(len(entries[i]) for i in locked_idx) - len(DELIMITER) * max(len(entries) - 1, 0)
    try:
        data = decode(model(editable))
        checked = validate(editable, data, limit, budget)
    except Exception as exc:
        return {"label": label, "status": "failed", "reason": str(exc)[:120] if isinstance(exc, ValueError) else type(exc).__name__}
    if checked is None:
        return {"label": label, "status": "unchanged"}
    new_unlocked, mapping = checked
    final = assemble(entries, locked_idx, mapping)
    changes = [str(c)[:200] for c in data.get("changes") or [] if str(c).strip()][:10]
    merged = [{"before": [editable[i] for i in sources], "after": text}
              for sources, text in mapping if [editable[i] for i in sources] != [text]]
    result = {"label": label, "status": "changed", "before": len(entries), "after": len(final),
              "chars_before": len(raw), "chars_after": len(DELIMITER.join(final)), "changes": changes, "merged": merged}
    if dry_run:
        return result
    with MemoryStore._file_lock(path):
        if path.read_text(encoding="utf-8-sig") != raw:
            return {"label": label, "status": "skipped", "reason": "changed_while_tidying"}
        MemoryStore._write_file(path, final)
    history(path, {"at": datetime.fromtimestamp(now, KST).isoformat(timespec="seconds"), "file": path.name,
                   "merged": merged, "changes": changes})
    return result


def report_text(results, day):
    changed = [r for r in results if r["status"] == "changed"]
    failed = [r for r in results if r["status"] == "failed"]
    if not changed and not failed:
        return None
    lines = [f"*{'🟢' if not failed else '🟡'} 새벽 기억 정리 ({day})*", ""]
    for r in changed:
        lines.append(f"*{r['label']}* 항목 {r['before']}→{r['after']}개, {r['chars_before']}→{r['chars_after']}자")
        for change in r["changes"] or ["중복·바뀐 사실을 합쳤다."]:
            lines.append(f"    • {change}")
    for r in failed:
        lines.append(f"*{r['label']}* 정리하지 않음: `{r['reason']}` (파일은 그대로)")
    lines += ["", "_옛 문장은 각 memories/history.jsonl과 git 이력에 남아 있다. 📌로 시작하거나 [확인]이 붙은 항목은 건드리지 않는다._"]
    return "\n".join(lines)


def deliver(text):
    """learning_report.slack applies slack_blocks (상태색 제목·목록) before posting."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("memory_tidy_report", Path(__file__).with_name("learning_report.py"))
    R = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(R)
    home = root()
    channel = R.learning.report_channel(home) or R.slack(
        home, "conversations.open", {"users": R.owner(home)})["channel"]["id"]
    R.slack(home, "chat.postMessage", {"channel": channel, "text": text, "unfurl_links": False})


def main(argv=None):
    parser = argparse.ArgumentParser(description="새벽 기억 정리")
    parser.add_argument("--dry-run", action="store_true", help="모델 답과 검사 결과만 출력하고 쓰거나 보내지 않는다.")
    args = parser.parse_args(argv)
    results = []
    for label, path, limit in targets():
        try:
            results.append(tidy(label, path, limit, dry_run=args.dry_run))
        except Exception as exc:
            results.append({"label": label, "status": "failed", "reason": type(exc).__name__})
    day = datetime.now(KST).date().isoformat()
    text = report_text(results, day)
    print(json.dumps(results, ensure_ascii=False, indent=1))
    if text and not args.dry_run:
        try:
            deliver(text)
        except Exception as exc:
            print("MEMORY_TIDY_REPORT_FAILED:" + type(exc).__name__, file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
