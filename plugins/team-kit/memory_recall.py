"""Rebuildable, profile-local recall of CURRENT facts, rules and passive work skills.

No model call, external memory provider, historical instruction replay, or writes to
the source documents. The on-disk index is diagnostic/rebuildable, never authority.
"""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import json
from pathlib import Path

# Load the standalone helper without relying on a previously cached sibling module.
import importlib.util as _import_util
_spec = _import_util.spec_from_file_location("team_learning_tasks", Path(__file__).with_name("learning_tasks.py"))
_TASKS = _import_util.module_from_spec(_spec)
_spec.loader.exec_module(_TASKS)
import re
import sqlite3
import threading
import time
import unicodedata

_CACHE = OrderedDict()
_CONFIG = OrderedDict()
_LOCK = threading.RLock()
MAX_RESULTS = 8
MAX_RESULT_CHARS = 6000
ALIASES = {
    "personal": ("일정", "약속", "미팅", "회의", "캘린더", "calendar", "메모", "노트", "notes", "미리알림", "미리 알림", "할일", "할 일", "리마인더", "reminder", "마감", "inbox"),
    "mail": ("메일", "이메일", "gmail", "email", "편지함", "수신자", "첨부파일", "첨부 파일"),
    "web": ("주차", "주차장", "parking", "검색", "조사", "비교", "장소", "방문", "식당", "맛집", "예약", "브라우저", "웹", "사이트", "browser"),
    "learn": ("학습", "가르", "교정", "배운", "배웠", "미검토", "학습 보고", "기준 변경"),
    "work": ("긴 작업", "뒷작업", "백그라운드", "산출물", "미완료", "진행 상황", "kanban", "카드", "실패 보고"),
}
STOP = {"알려줘", "보여줘", "찾아줘", "해주세요", "해줘", "확인해", "확인", "부탁해", "안녕", "고마워", "응", "이거", "그거", "저거", "어떻게", "이번", "지금", "내일", "오늘"}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def stamp(path):
    try:
        s = path.stat()
        return (str(path.resolve()), s.st_mtime_ns, s.st_ctime_ns, s.st_size, s.st_ino)
    except FileNotFoundError:
        return (str(path), None)


def config(home):
    from ruamel.yaml import YAML
    home = Path(home).resolve()
    signature = stamp(home / "config.yaml")
    with _LOCK:
        previous = _CONFIG.get(str(home))
        if previous and previous[0] == signature:
            return previous[1]
        file = home / "config.yaml"
        value = YAML(typ="safe").load(file.read_text()) if file.exists() else {}
        value = value or {}
        if not isinstance(value, dict):
            raise ValueError("Invalid recall configuration.")
        learning = value.get("plugins", {}).get("entries", {}).get("team-kit", {}).get("settings", {}).get("learning", {})
        result = {"learning": learning, "memory": value.get("memory", {}),
                  "projects": value.get("plugins", {}).get("entries", {}).get("team-kit", {}).get("settings", {}).get("projects", {}),
                  "diagnosis": value.get("plugins", {}).get("entries", {}).get("team-kit", {}).get("settings", {}).get("diagnosis", {}),
                  "work": value.get("plugins", {}).get("entries", {}).get("team-kit", {}).get("settings", {}).get("work", {}),
                  "location": value.get("plugins", {}).get("entries", {}).get("team-kit", {}).get("settings", {}).get("location", {}),
                  "news_watch": value.get("plugins", {}).get("entries", {}).get("team-kit", {}).get("settings", {}).get("news_watch", {})}
        _CONFIG[str(home)] = (signature, result)
        while len(_CONFIG) > 16:
            _CONFIG.popitem(last=False)
        return result


def options(home):
    learning = config(home)["learning"]
    value = learning.get("recall", {})
    if (learning.get("enabled") is not True or learning.get("skill_learning") is not True
            or not isinstance(value, dict) or value.get("enabled") is not True):
        return {}
    return value


def managed(home, name):
    cfg = config(home)["learning"]
    return name in cfg.get("managed_skills", []) or any(name.startswith(p) for p in cfg.get("managed_prefixes", []) if p)


def _safe_skill_path(root, path):
    return (not any(p.startswith(".") for p in path.relative_to(root).parts)
            and not any(p.is_symlink() for p in (path, *path.parents) if p == root or root in p.parents))


def source_paths(home):
    """Only the explicit USER symlink may leave this profile; never follow skill links."""
    home = Path(home).resolve()
    result = []
    mem = config(home)["memory"]
    shared = home.parent.parent / "memories/USER.md" if home.parent.name == "profiles" else home / "memories/USER.md"
    for name, enabled in (("USER.md", mem.get("user_profile_enabled", True)), ("MEMORY.md", mem.get("memory_enabled", True))):
        path = home / "memories" / name
        if not enabled or not path.exists():
            continue
        if path.parent.is_symlink() or (path.is_symlink() and (
                name != "USER.md" or path.resolve() != shared.resolve() or shared.is_symlink() or shared.parent.is_symlink())):
            raise ValueError("Memory paths must be local except the shared USER.md link.")
        result.append(path)
    manifest = home / "skills/.bundled_manifest"
    if manifest.is_symlink():
        raise ValueError("Recall does not follow a symlinked skill manifest.")
    bundled = {x.split(":", 1)[0] for x in _read(manifest).splitlines()}
    root = home / "skills"
    if root.is_symlink():
        raise ValueError("Recall does not follow a symlinked skills directory.")
    names = set()
    for path in sorted(root.rglob("SKILL.md")):
        name = path.parent.name
        if not managed(home, name) or name in bundled or not _safe_skill_path(root, path):
            continue
        if name in names:
            raise ValueError("Duplicate managed skill names must be resolved before recall.")
        names.add(name)
        if len(names) > 256:
            raise ValueError("Recall supports at most 256 managed skills.")
        result.append(path)
        refs = [p for p in sorted((path.parent / "references").glob("*.md")) if _safe_skill_path(root, p)]
        if len(refs) > 23:
            raise ValueError("Consolidate this skill's references before indexing.")
        result.extend(refs)
    result.extend([home / "learning/rules.json", manifest, home / "state/team-memory-provenance.json", home / "learning/facts.json"])
    for path in result:
        if path.name == "USER.md":
            continue
        if any(p.is_symlink() for p in (path, *path.parents) if p != home and home in p.parents):
            raise ValueError("Recall sources must remain within the profile.")
    return result


def provenance_path(home):
    home = Path(home)
    durable = home / "learning/facts.json"
    return durable if durable.exists() or config(home)["learning"].get("archive") is True else home / "state/team-memory-provenance.json"


def provenance(home):
    path = provenance_path(home)
    if not path.exists():
        path = Path(home) / "state/team-memory-provenance.json"
    value = json.loads(_read(path) or "{}")
    return value if isinstance(value, dict) else {}


def _read(path):
    if not path.exists():
        return ""
    if path.stat().st_size > (20_000_000 if path.name == "rules.json" else 200_000):
        raise ValueError("Recall source is too large; existing documents were not changed.")
    return path.read_text(encoding="utf-8")


def norm(text):
    return unicodedata.normalize("NFKC", text).casefold()


def query_terms(query):
    terms = {t for t in re.findall(r"[\w가-힣]+", norm(query)) if len(t) > 1 and t not in STOP}
    # Korean particles otherwise hide exact noun matches in short user requests.
    for term in list(terms):
        for suffix in ("에서는", "으로", "에서", "까지", "부터", "에는", "은", "는", "을", "를", "의", "에"):
            if term.endswith(suffix) and len(term) > len(suffix) + 1:
                terms.add(term[:-len(suffix)])
                break
    return terms


def domains(query, task_skills=None):
    q = norm(query)
    roles = _TASKS.DEFAULTS if task_skills is None else task_skills
    return {roles[role] for role, words in ALIASES.items() if role in roles and any(w in q for w in words)}


def _safe(text, learning, *, fact=False):
    learning.safe_text(text)
    if fact:
        from tools.threat_patterns import scan_for_threats
        if scan_for_threats(text, scope="strict"):
            return "[기억의 지시 삽입 패턴이 차단됨. 이 항목은 지침으로 적용하지 않는다.]"
    return text


def snapshot(home, learning):
    """Stat cache, bounded sources, stable read check. Never trust a saved index as input."""
    home = Path(home).resolve()
    key = str(home)
    with _LOCK:
        for attempt in range(2):
            paths = source_paths(home)
            signature = [stamp(home / "config.yaml"), *[stamp(p) for p in paths]]
            cached = _CACHE.get(key)
            if cached and cached[0] == signature:
                _CACHE.move_to_end(key)
                return cached[1]
            _read(home / "learning/rules.json")  # Bound the store before parsing it.
            data = learning.load(home)
            try:
                fact_sources = provenance(home)
            except (ValueError, OSError):
                fact_sources = {}  # Optional provenance cannot make current facts unavailable.
            rows, facts, skills, warnings = [], {"memories/USER.md": [], "memories/MEMORY.md": []}, {}, []
            for path in paths:
                if path.suffix != ".md":
                    continue
                relative = path.relative_to(home).as_posix()
                text = _read(path)
                revision = digest(text)
                base = {"path": relative, "revision": revision, "modified_at": path.stat().st_mtime, "source": "current_file"}
                if relative.startswith("memories/"):
                    entries = list(dict.fromkeys(t.strip() for t in text.split("\n§\n") if t.strip()))
                    facts[relative] = []
                    for entry in entries:
                        ref = digest([relative, entry])[:24]
                        try:
                            content = _safe(entry, learning, fact=True)
                        except ValueError:
                            content = "[인증정보 패턴이 있어 이 기억은 검색·전달하지 않는다.]"
                        item = {**base, "id": ref, "kind": "fact", "title": path.stem, "text": content,
                                "provenance": fact_sources.get(ref, {"origin": "existing_file", "verified_owner_source": False})}
                        rows.append(item)
                        facts[relative].append(item)
                    continue
                name = path.parent.name if path.name == "SKILL.md" else path.parent.parent.name
                current = data.get("skills", {}).get(name, {})
                if current.get("active") is False:
                    continue
                try:
                    _safe(text, learning)
                except ValueError:
                    warnings.append(relative)
                    continue
                file_name = "SKILL.md" if path.name == "SKILL.md" else "references/" + path.name
                # The source revision is always the actual document. Lesson metadata is
                # included only if that document still matches the recorded learning.
                recorded = current.get("fingerprints", {}).get(relative.removeprefix("skills/"))
                lesson = current.get("lesson", {}) if recorded == revision else {}
                source = {**base, "skill": name, "file": file_name,
                          "version": current.get("version", revision) if recorded == revision else revision}
                skills.setdefault(name, {})[file_name] = {**source, "text": text}
                parts = [p.strip() for p in text.split("\n\n") if p.strip() and not p.startswith("---\n")]
                for number, paragraph in enumerate(parts):
                    rows.append({**source, "id": digest([relative, revision, number])[:24], "kind": "skill",
                                 "title": name, "text": paragraph, "lesson": lesson})
            for topic, entry in data.get("rules", {}).items():
                if not entry.get("active") or entry.get("migrated_to"):
                    continue
                try:
                    text = _safe(entry["rule"], learning)
                except ValueError:
                    warnings.append("learning/rules.json:" + topic)
                    continue
                rows.append({"id": digest([topic, entry["version"]])[:24], "kind": "rule", "title": topic,
                             "path": "learning/rules.json", "revision": entry["version"], "text": text,
                             "source": entry.get("source", {}), "modified_at": entry.get("updated_at")})
            if signature != [stamp(home / "config.yaml"), *[stamp(p) for p in source_paths(home)]]:
                continue
            result = {"version": 1, "signature": digest(signature), "rows": rows, "facts": facts,
                      "skills": skills, "task_skills": _TASKS.mapping(config(home)["learning"]), "warnings": warnings, "built_at": time.time()}
            _CACHE[key] = (signature, result)
            while len(_CACHE) > 16:
                _CACHE.popitem(last=False)
            # A failed cache write must not stop a correct read from the originals.
            try:
                learning.save(home / "state/team-memory-index.json", result)
            except OSError:
                pass
            return result
    raise ValueError("Recall sources changed while reading; retry against current originals.")


def search(index, query, *, limit=5, kind="all"):
    terms = query_terms(query)
    hints = domains(query, index.get("task_skills"))
    if not terms and not hints:
        return []
    ranked = []
    for row in index["rows"]:
        if kind != "all" and row["kind"] != kind:
            continue
        text = norm(row["text"] + " " + row["title"])
        matched = [term for term in terms if term in text]
        score = sum(3 + min(len(t), 8) / 8 for t in matched)
        if row.get("skill") in hints:
            score += 2
        if not matched and not (row.get("skill") in hints and row.get("file") == "SKILL.md"):
            continue
        if row["kind"] in {"fact", "rule"} and matched:
            score += 2
        ranked.append((score, row))
    ranked.sort(key=lambda x: (-x[0], x[1]["id"]))
    results, seen = [], set()
    for score, row in ranked:
        # One excerpt per skill document; every fact/rule remains independently selectable.
        group = row["path"] if row["kind"] == "skill" else row["id"]
        if group in seen:
            continue
        seen.add(group)
        item = {k: v for k, v in row.items() if k != "text"}
        locations = [norm(row["text"]).find(t) for t in terms if t in norm(row["text"])]
        start = max(0, min(locations, default=0) - 120)
        excerpt = ("…" if start else "") + row["text"][start:start + 650]
        item.update({"excerpt": excerpt, "excerpt_only": True, "score": round(score, 2)})
        results.append(item)
        if len(results) >= limit:
            break
    return results


def owner(home, session_id, learning):
    if not options(home):
        raise ValueError("Owner recall is not enabled for this profile.")
    learning.extension("learning_skills").human_context()
    return learning.source(home, session_id, allow_approval=True)


def handle(home, args, session_id="", *, learning, **_):
    try:
        owner(home, session_id, learning)
        if args.get("action") == "history":
            query, limit, offset = args.get("query", ""), args.get("limit", 5), args.get("offset", 0)
            if args.get("ref_id"):
                if type(offset) is not int or offset < 0:
                    raise ValueError("History offset must be nonnegative.")
                return json.dumps(learning.extension("learning_archive").read_history(home, args["ref_id"], learning, offset=offset), ensure_ascii=False)
            if (not isinstance(query, str) or not 1 <= len(query.strip()) <= 500 or type(limit) is not int
                    or not 1 <= limit <= MAX_RESULTS or type(offset) is not int or offset < 0):
                raise ValueError("History needs query, limit 1–8 and a nonnegative offset.")
            return json.dumps(learning.extension("learning_archive").history(home, query, learning, limit=limit, offset=offset), ensure_ascii=False)
        index = snapshot(home, learning)
        action = args.get("action", "search")
        if action == "read":
            row = next((r for r in index["rows"] if r["id"] == args.get("ref_id")), None)
            if not row:
                raise ValueError("This recall reference is absent or changed; search again.")
            item = dict(row)
            if len(item["text"]) > 5000:
                item["text"] = item["text"][:5000]
                item["truncated"] = True
            result = {"ok": True, "item": item, "current": True,
                      "hint": "Use skill_view for the complete skill document; recall does not satisfy the skill-read requirement."}
        elif action == "search":
            query, limit, kind = args.get("query", ""), args.get("limit", 5), args.get("kind", "all")
            if not isinstance(query, str) or not 1 <= len(query.strip()) <= 500:
                raise ValueError("Provide a focused recall query of 1–500 characters.")
            if type(limit) is not int or not 1 <= limit <= MAX_RESULTS or kind not in {"all", "fact", "rule", "skill"}:
                raise ValueError("Invalid recall limit or kind.")
            matches = search(index, query, limit=limit, kind=kind)
            while matches and len(json.dumps(matches, ensure_ascii=False)) > MAX_RESULT_CHARS:
                matches.pop()
            result = {"ok": True, "matches": matches, "warnings": index["warnings"],
                      "hint": "Excerpts are discovery aids. Open the named skill/file with skill_view before work. "
                              "For past conversations use session_search; verify current Calendar/Notes/Mail in the original tools."}
        else:
            raise ValueError("Recall supports search, read or historical-only history.")
        return json.dumps(result, ensure_ascii=False)
    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error) as exc:
        return json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False)


def note(context=None):
    if isinstance(context, dict) and context.get("platform") != "slack":
        return ""
    return ("[현재 기억과 업무 방법 찾기]\n관련 기억은 team_recall로 찾는다. 현재 파일의 사실·활성 규칙·업무 스킬만 검색하며 "
            "출처와 버전을 확인한다. 검색 후보나 발췌는 사실 확정이나 스킬 읽기 완료가 아니다. 업무 시작 전 해당 skill_view를 연다. "
            "같은 세션에서 읽은 파일이 그대로이고 전체 본문이 현재 도구 결과에 남아 있으면 재조회하지 않아도 된다. 파일 수정·압축·본문 제거 뒤에는 다시 확인한다. "
            "USER.md·MEMORY.md의 최신 목록이 전달되면 이전 스냅샷을 갱신한다. 일하는 방법은 최신 학습 규칙·스킬을 따른다. "
            "옛 대화 원문은 session_search, 현재 일정·메모·메일은 해당 원본 도구에서 확인한다. "
            "출처가 existing_file인 기억은 과거 원문까지 검증된 것으로 말하지 않는다.")


SCHEMA = {"name": "team_recall", "description": "Find current owner facts, active rules and managed work skills. Read-only; does not search historical conversations or verify current app state.",
          "parameters": {"type": "object", "properties": {
              "action": {"type": "string", "enum": ["search", "read", "history"]},
              "offset": {"type": "integer", "minimum": 0, "description": "Historical lookup pagination only."},
              "query": {"type": "string", "maxLength": 500},
              "ref_id": {"type": "string", "description": "Exact id from search for action=read; historical ref_id for action=history. History offsets then count characters."},
              "kind": {"type": "string", "enum": ["all", "fact", "rule", "skill"]},
              "limit": {"type": "integer", "minimum": 1, "maximum": MAX_RESULTS}}, "additionalProperties": False}}
