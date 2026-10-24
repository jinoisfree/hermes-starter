"""Explicit lesson boundaries and exact duplicate checks, without extra model calls."""
from __future__ import annotations

import difflib
from pathlib import Path
import re
import unicodedata

FIELDS = {"scope": "언제 적용하는가", "exceptions": "언제 적용하지 않는가; 없으면 없음",
          "check": "어떤 결과를 확인해야 하는가", "reason": "무엇을 왜 고쳤는가"}
SCHEMA = {"type": "object", "properties": {key: {"type": "string", "description": value, "maxLength": 300}
          for key, value in FIELDS.items()}, "required": list(FIELDS), "additionalProperties": False}


def lesson(value, learning):
    if not isinstance(value, dict) or set(value) != set(FIELDS):
        raise ValueError("Provide lesson with scope, exceptions, check and reason. Do not invent owner preferences.")
    result = {}
    for key, text in value.items():
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 300:
            raise ValueError("Each lesson field must contain 1–300 characters.")
        learning.safe_text(text)
        result[key] = text.strip()
    return result


def normalized(text):
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", text).casefold())


def body(text):
    if text.startswith("---"):
        parts = text.split("---", 2)
        text = parts[2] if len(parts) == 3 else text
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def check(home, name, changes, summary, data, skills, lesson=None):
    """Block exact repetition; fuzzy matches are advisory, never silently merged."""
    needle = normalized(summary)
    for other, entry in data.get("skills", {}).items():
        if (other != name and entry.get("active") and len(needle) >= 12 and normalized(entry["rule"]) == needle
                and normalized(entry.get("lesson", {}).get("scope", "")) == normalized((lesson or {}).get("scope", ""))):
            raise ValueError(f"The same method is already in {other}; read and update its original skill.")
    candidates = []
    for relative, change in changes.items():
        content = change["after"]
        if content is None:
            continue
        old = normalized(change["before"] or "")
        paragraphs = [normalized(p) for p in body(content).split("\n\n")]
        for part in set(paragraphs):
            if len(part) >= 24 and paragraphs.count(part) > 1 and old.count(part) < paragraphs.count(part):
                raise ValueError("This patch repeats an existing method; replace its original passage instead.")
        if change["before"] is not None or not relative.endswith("/SKILL.md"):
            continue
        wanted = normalized(body(content))
        for file in (Path(home) / "skills").rglob("SKILL.md"):
            if any(p.startswith(".") for p in file.relative_to(Path(home) / "skills").parts):
                continue
            try:
                text = skills.read_document(home, file.relative_to(Path(home) / "skills").as_posix())
            except (OSError, ValueError):
                continue
            other = normalized(body(text or ""))
            if len(wanted) >= 24 and wanted == other:
                raise ValueError(f"The same procedure already exists in {file.parent.name}; update that skill.")
            if len(wanted) >= 24 and difflib.SequenceMatcher(None, wanted, other).ratio() >= 0.75:
                candidates.append(file.parent.name)
    return sorted(set(candidates))[:5]
