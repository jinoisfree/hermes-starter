"""Folder reach for a profile: read inside settings.files.folders, change only after the user approves.

Reading (read_file, search_files, file_list) is confined to the configured folders and to the
documents the user attached in chat. Changes (write_file, patch, file_move, folder_create,
file_trash, file_copy, doc_edit, pdf_pages, doc_convert, doc_fill) are confined to the configured folders, need a person
in the chat, and are asked one call at a time. Deleting means the macOS Trash.

Excel/Word/PDF edits run doc_worker.py under ~/.local/share/hermes-docs (openpyxl, python-docx,
pypdf), write a temporary file beside the target, and move it into place only when the worker
succeeded. doc_fill (mail merge) checks a template against a data table without approval and, after
one approval for the whole call, writes one new file per row into a new or empty folder.
"""
from __future__ import annotations

import ctypes
import errno
import hashlib
import fcntl
from contextlib import contextmanager
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import threading
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

READ_TOOLS = frozenset({"read_file", "search_files", "file_list", "doc_read", "pdf_render", "folder_scan", "file_link", "file_location"})
VIEW_TOOL = "vision_analyze"  # core tool: seeing an image; confined here to local files in reach
CORE_CHANGE_TOOLS = frozenset({"write_file", "patch"})  # asked through tool_policy
OWN_CHANGE_TOOLS = frozenset({"file_move", "folder_create", "file_trash", "file_copy", "doc_edit", "pdf_pages", "doc_convert", "doc_fill"})  # asked here
DOC_KINDS = {".xlsx": "xlsx", ".docx": "docx"}
READ_DOC_KINDS = {**DOC_KINDS, ".pdf": "pdf", ".hwp": "hwp", ".hwpx": "hwpx"}
CONVERT_PAIRS = {(".pdf", ".docx"), (".docx", ".pdf"), (".xlsx", ".pdf")}
MAX_CONVERT_BYTES = 100 * 1024 * 1024
MAX_OPERATIONS = 100
FILL_DATA_KINDS = frozenset({".xlsx", ".csv"})
MAX_FILL_ROWS = 200
MAX_PDF_SOURCES = 30
MAX_RENDER_PAGES = 5
SCAN_LIMIT = 3000
HASH_FILE_LIMIT = 200 * 1024 * 1024
HASH_TOTAL_LIMIT = 2 * 1024 * 1024 * 1024
IMAGE_TYPES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic"})
DOC_PYTHON = Path.home() / ".local/share/hermes-docs/bin/python"
TOOLS = READ_TOOLS | CORE_CHANGE_TOOLS | OWN_CHANGE_TOOLS | {VIEW_TOOL}
DELEGATED_TOOLS = frozenset({"read_file", "search_files", "file_list", "write_file", "patch", "file_trash", "file_link"})
MAX_LINK_BYTES = 50 * 1024 * 1024
MAX_MOVES = 50
LIST_LIMIT = 500
ACCESS_TIMEOUT = 10
_OPENED_ROOTS: set[Path] = set()

_TRASH_SCRIPT = (
    'ObjC.import("Foundation"); function run(argv) { return $.NSFileManager.defaultManager'
    '.trashItemAtURLResultingItemURLError($.NSURL.fileURLWithPath(argv[0]), null, null) ? "trashed" : "failed"; }'
)
# File-provider folders (Synology Drive and the like under ~/Library/CloudStorage) have no Trash of their own:
# macOS refuses with NSFeatureUnsupportedError (3328). Such an item is first moved out to a local folder.
CLOUD_STORAGE = Path.home() / "Library" / "CloudStorage"


def folders(config: Any) -> list[Path]:
    """The configured folders that exist, resolved. Empty when the feature is off."""
    if not isinstance(config, dict) or config.get("enabled") is not True:
        return []
    found: list[Path] = []
    for item in config.get("folders") or []:
        try:
            folder = Path(str(item)).expanduser()
            if folder.is_absolute() and folder.is_dir():
                found.append(folder.resolve())
        except (OSError, RuntimeError, ValueError):
            continue
    return found


def manages(config: Any, name: str) -> bool:
    """Extra-only profiles leave shared file tools to their existing plugin."""
    return name in TOOLS and not (isinstance(config, dict) and config.get("extra_only") is True and name in DELEGATED_TOOLS)


def available(config: Any, name: str) -> bool:
    if not manages(config, name):
        return False
    if name == "doc_convert":
        return isinstance(config, dict) and config.get("convert") is True
    if name == "doc_fill":  # the owner's main file profile only, not extra-only file tools
        return not (isinstance(config, dict) and config.get("extra_only") is True)
    configured = isinstance(config, dict) and ("folder_links" in config or _synology_enabled(config))
    return not (name == "file_link" and configured or name == "file_location" and not configured)


def _location_problem(path: Any, roots: list[Path]) -> str | None:
    target = inside(path, roots)
    if not isinstance(path, str) or not Path(path).is_absolute() or target is None:
        return "file_location needs an absolute path inside the configured folders"
    source = Path(path)
    if any(part.is_symlink() for part in (source, *source.parents)):
        return "file_location does not follow symbolic links"
    if not target.is_file() and not target.is_dir():
        return "file_location needs an existing file or folder"
    return None


_NAS_SESSIONS: dict = {}
_NAS_LOCK = threading.Lock()


def _synology_enabled(config: Any) -> bool:
    return isinstance(config, dict) and isinstance(config.get("synology"), dict) and config["synology"].get("enabled") is True


def _folder_url(url: Any) -> bool:
    if not isinstance(url, str):
        return False
    try:
        parsed = urllib.parse.urlsplit(url)
        return bool(parsed.scheme == "https" and (parsed.hostname or "").endswith(".example.com")
                    and re.fullmatch(r"/d/f/[A-Za-z0-9]+", parsed.path)
                    and not (parsed.username or parsed.password or parsed.port or parsed.query or parsed.fragment))
    except ValueError:
        return False


def _folder_state() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()) / "state/folder-links.json"


def _folder_cache(folder: Path, scope: str, url: str | None = None) -> str | None:
    target = _folder_state()
    temporary = target.with_name(f".{target.name}.{secrets.token_hex(8)}.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(target.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            try:
                records = json.loads(target.read_text())
                if not isinstance(records, dict):
                    records = {}
            except (OSError, ValueError):
                records = {}
            record = records.get(str(folder))
            if url is None:
                if ((folder.is_dir() or folder.is_file()) and isinstance(record, dict) and record.get("scope") == scope
                        and type(record.get("recorded_at")) in (int, float)
                        and 0 <= time.time() - record["recorded_at"] < 30 * 86400
                        and _folder_url(record.get("url"))):
                    return record["url"]
            else:
                records[str(folder)] = {"url": url, "recorded_at": time.time(), "scope": scope}
                records = {key: value for key, value in records.items()
                           if isinstance(value, dict) and type(value.get("recorded_at")) in (int, float)}
                records = dict(sorted(records.items(), key=lambda item: item[1]["recorded_at"], reverse=True)[:2000])
                with temporary.open("x", encoding="utf-8") as stream:
                    json.dump(records, stream, ensure_ascii=False)
                os.replace(temporary, target)
    except OSError:
        pass
    finally:
        temporary.unlink(missing_ok=True)
    return None


class _NasError(ValueError):
    def __init__(self, code: Any = None):
        super().__init__("Synology lookup failed")
        self.code = code if type(code) is int else None


class _NoNasRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


NAS_TOTAL_SECONDS = 55
NAS_LOGIN_SECONDS = 25


def _nas_api(base: str, body: dict, opener: Any, deadline: float) -> dict:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _NasError()
    request = urllib.request.Request(base + "/webapi/entry.cgi", data=urllib.parse.urlencode(body).encode())
    with opener(request, timeout=min(remaining, NAS_LOGIN_SECONDS if body.get("method") == "login" else 10)) as response:
        answer = json.load(response)
    if not isinstance(answer, dict) or answer.get("success") is not True:
        error = answer.get("error") if isinstance(answer, dict) else None
        raise _NasError(error.get("code") if isinstance(error, dict) else None)
    data = answer.get("data", {})
    if not isinstance(data, dict):
        raise _NasError()
    return data


def _nas_login_cooldown(exc):
    """Classify transport failures separately from authentication or invalid responses."""
    if isinstance(exc, urllib.error.HTTPError):
        return 60 if exc.code in (408, 429) or 500 <= exc.code < 600 else 1800
    return 60 if isinstance(exc, OSError) else 1800


def _nas_login(base, settings, home, session, opener, deadline):
    """Called under _NAS_LOCK; retry transport failures once within the shared deadline."""
    from dotenv import dotenv_values
    calls = 0
    try:
        env = dotenv_values(home / '.env', interpolate=False)
        user, password = env.get(settings['user']), env.get(settings['password'])
        if not user or not password:
            raise _NasError()
        body = {'api': 'SYNO.API.Auth', 'version': 6, 'method': 'login',
                'account': user, 'passwd': password, 'session': 'SynologyDrive', 'format': 'sid'}
        for attempt in range(2):
            try:
                calls += 1
                data = _nas_api(base, body, opener, deadline)
                if not isinstance(data.get('sid'), str) or not data['sid']:
                    raise _NasError()
                session['sid'] = data['sid']
                return calls
            except (OSError, ValueError, TypeError) as exc:
                if attempt == 0 and _nas_login_cooldown(exc) == 60 and deadline > time.monotonic():
                    continue
                raise
    except (OSError, ValueError, TypeError) as exc:
        session['cooldown'] = time.monotonic() + _nas_login_cooldown(exc)
        raise


def _synology_link(config: dict, folder: Path, opener: Any, deadline: float) -> tuple[str | None, str]:
    settings = config["synology"]
    try:
        base = settings["api_base"].rstrip("/")
        parsed = urllib.parse.urlsplit(base)
        host = settings["link_host"]
        sync_root = Path(settings["sync_root"])
        drive_root = settings["drive_root"]
        if (parsed.scheme != "https" or not (parsed.hostname or "").endswith(".example.com")
                or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password or parsed.port
                or not _folder_url(f"https://{host}/d/f/TEST") or not sync_root.is_absolute()
                or drive_root != "/mydrive" or not (folder.is_dir() or folder.is_file())
                or settings.get("user") != "SYNOLOGY_DRIVE_USER" or settings.get("password") != "SYNOLOGY_DRIVE_PASSWORD"):
            return None, "invalid_config"
        relative = folder.relative_to(sync_root.resolve())
        remote_path = drive_root + "/" + relative.as_posix()
        scope = hashlib.sha256(json.dumps([base, host, str(sync_root), drive_root]).encode()).hexdigest()
        if cached := _folder_cache(folder, scope):
            return cached, "cache"
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not _NAS_LOCK.acquire(timeout=max(0, remaining)):
            return None, "timeout"
        try:
            from hermes_constants import get_hermes_home
            home = Path(get_hermes_home())
            key = (str(home), scope)
            session = _NAS_SESSIONS.setdefault(key, {"sid": None, "cooldown": 0})
            if session["cooldown"] > time.monotonic():
                return None, "login_cooldown"
            open_url = opener or urllib.request.build_opener(_NoNasRedirect).open
            for attempt in range(2):
                if not session["sid"]:
                    try:
                        _nas_login(base, settings, home, session, open_url, deadline)
                    except (OSError, ValueError, TypeError):
                        return None, "login_failed"
                try:
                    data = _nas_api(base, {"api": "SYNO.SynologyDrive.Files", "version": 1, "method": "get",
                        "path": json.dumps(remote_path), "_sid": session["sid"]}, open_url, deadline)
                except _NasError as exc:
                    if exc.code in (106, 107, 119):
                        session["sid"] = None
                        if attempt == 0:
                            continue
                    raise
                identifier = data.get("permanent_link")
                if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9]+", identifier):
                    return None, "invalid_link"
                url = f"https://{host}/d/f/{identifier}"
                if not _folder_url(url):
                    return None, "invalid_link"
                _folder_cache(folder, scope, url)
                return url, "synology"
        finally:
            _NAS_LOCK.release()
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None, "lookup_failed"
    return None, "lookup_failed"


def _drive_fragment(config: Any, url: str, target: Path) -> str:
    """Decorate only the returned link; never persist device paths in URLs/caches."""
    import unicodedata
    settings = config.get('synology')
    if not isinstance(settings, dict) or '#' in url:
        return url
    try:
        root = Path(settings['sync_root']).expanduser()
        if not root.is_absolute():
            return url
        relative = target.resolve().relative_to(root.resolve())
        if not relative.parts:
            return url
        encoded = '/'.join(urllib.parse.quote(unicodedata.normalize('NFC', part), safe='')
                           for part in relative.parts)
        return url + '#drive=' + encoded
    except (KeyError, TypeError, ValueError, OSError, RuntimeError):
        return url


def file_location(config: Any, args: dict[str, Any], opener: Any = None, **_: Any) -> str:
    if not available(config, "file_location"):
        return _error("No folder link lookup is configured for this profile")
    roots = folders(config)
    paths = args.get("paths")
    if not roots or not isinstance(paths, list) or not 1 <= len(paths) <= 10:
        return _error("file_location needs 1 to 10 paths and configured folders")
    configured = config.get("folder_links", {})
    if not isinstance(configured, dict):
        return _error("Invalid folder_links configuration")
    folders_by_link = {}
    try:
        for path, url in configured.items():
            if _location_problem(path, roots) or not Path(path).is_dir() or not isinstance(url, str):
                return _error("Invalid folder_links configuration")
            if not _folder_url(url):
                return _error("Invalid folder_links configuration")
            folders_by_link[Path(path).resolve()] = url
    except (OSError, ValueError, RuntimeError):
        return _error("Invalid folder_links configuration")
    links, unlinked, failed = [], [], []
    deadline = time.monotonic() + NAS_TOTAL_SECONDS
    for path in paths:
        target = None
        before_links, before_unlinked = len(links), len(unlinked)
        try:
            if reason := problem("file_location", {"paths": [path]}, roots):
                failed.append({"path": path, "error": reason})
                continue
            target = inside(path, roots)
            folder = target.parent if target.is_file() else target
            fallback = None
            if folder not in folders_by_link and _synology_enabled(config):
                url, source = _synology_link(config, folder, opener, deadline)
                if url:
                    links.append({"path": path, "name": target.name, "folder": folder.name,
                                  "url": _drive_fragment(config, url, folder), "exact": True, "source": source})
                    continue
                fallback = source
            candidates = [parent for parent in folders_by_link if parent == folder or parent in folder.parents]
            if not candidates:
                root = max((root for root in roots if root == folder or root in folder.parents),
                           key=lambda item: len(item.parts))
                unlinked.append({"path": path, "name": target.name, "folder_path": str(folder.relative_to(root)),
                                 "reason": "No Synology folder link is configured for this folder or its parents"})
                if fallback:
                    unlinked[-1]["fallback"] = fallback
                continue
            linked = max(candidates, key=lambda item: len(item.parts))
            item = {"path": path, "name": target.name, "folder": folder.name,
                    "url": _drive_fragment(config, folders_by_link[linked], linked), "exact": linked == folder}
            if fallback:
                item["fallback"] = fallback
            if linked != folder:
                item.update(linked_folder=linked.name, below=str(folder.relative_to(linked)))
            links.append(item)
        except (OSError, ValueError, RuntimeError) as exc:
            failed.append({"path": path, "error": f"file_location failed ({type(exc).__name__})"})
        finally:
            item = links[-1] if len(links) > before_links else unlinked[-1] if len(unlinked) > before_unlinked else None
            if item is not None and target is not None and target.is_file() and _synology_enabled(config):
                file_url, status = _synology_link(config, target, opener, deadline)
                if file_url:
                    item.update(file_url=_drive_fragment(config, file_url, target), file_link_status=status)
                else:
                    item["file_link_status"] = status
    # The gateway reads a "failed" key near the start of a result as a failed call, so it is left out when nothing failed.
    return json.dumps({"ok": not failed, "links": links, "unlinked": unlinked, **({"failed": failed} if failed else {})},
                      ensure_ascii=False)


def attachments() -> Path:
    """Where the gateway saves a document the user attached in chat."""
    from hermes_constants import get_hermes_dir
    return Path(get_hermes_dir("cache/documents", "document_cache")).resolve()



def image_attachments() -> Path:
    """Current gateway image cache; do not probe legacy directories before approval."""
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()) / "cache" / "images"


def renders() -> Path:
    """Where pdf_render leaves page images for vision_analyze. Cleared of anything older than a day."""
    from hermes_constants import get_hermes_dir
    return Path(get_hermes_dir("cache/doc-pages", "doc_pages")).resolve()


def inside(path: Any, roots: list[Path]) -> Path | None:
    """The resolved path when it is absolute and lies in one of ``roots``, else None. Links are
    followed first, so one that points out of a root does not pass."""
    try:
        target = Path(str(path or "")).expanduser()
        if not target.is_absolute():
            return None
        target = target.resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    return target if any(root == target or root in target.parents for root in roots) else None


def _named(roots: list[Path]) -> str:
    return "; ".join(str(root) for root in roots) or "none are set"


def _moves(args: dict[str, Any]) -> list[dict[str, Any]]:
    moves = args.get("moves")
    return [m for m in moves if isinstance(m, dict)] if isinstance(moves, list) else []


def _path_problem(tool_name: str, args: dict[str, Any], roots: list[Path]) -> str | None:
    """Why this file call may not run; None when it may."""
    named = _named(roots)
    if tool_name == "file_location":
        paths = args.get("paths")
        if not isinstance(paths, list) or not 1 <= len(paths) <= 10:
            return "file_location needs paths: 1 to 10 absolute file or folder paths"
        for path in paths:
            if reason := _location_problem(path, roots):
                return reason
        return None
    if tool_name == "file_link":
        paths = args.get("paths")
        if not isinstance(paths, list) or not 1 <= len(paths) <= 10:
            return "file_link needs paths: 1 to 10 absolute file paths"
        for path in paths:
            if reason := _link_problem(path, roots):
                return reason
        return None
    if tool_name == VIEW_TOOL:
        target = inside(args.get("image_url"), [*roots, attachments(), image_attachments(), renders()])
        if target is None or not target.is_file() or target.suffix.lower() not in IMAGE_TYPES:
            return (f"vision_analyze here opens only an image file by absolute path inside the readable folders ({named}), "
                    "an image the user attached in chat, or a page image made by pdf_render; web addresses are not opened")
        return None
    if tool_name in READ_TOOLS:
        if tool_name == "file_list" and not str(args.get("path") or "").strip():
            return None
        target = inside(args.get("path"), [*roots, attachments(), image_attachments()])
        if target is None:
            return (f"{tool_name} needs an absolute path inside the readable folders ({named}) "
                    "or a file the user attached in chat")
        if tool_name == "doc_read" and (not target.is_file() or target.suffix.lower() not in READ_DOC_KINDS):
            return "doc_read reads an existing .pdf, .xlsx, .docx, .hwp or .hwpx file (use read_file for other documents)"
        if tool_name == "pdf_render" and (not target.is_file() or target.suffix.lower() != ".pdf"):
            return "pdf_render needs an existing .pdf file"
        if tool_name == "folder_scan" and not target.is_dir():
            return "folder_scan needs a folder"
        return None
    if tool_name in CORE_CHANGE_TOOLS:
        target = inside(args.get("path"), roots)
        if target is None:
            return f"{tool_name} works only on an absolute path inside the folders ({named})"
        if tool_name == "patch" and (args.get("patch") or str(args.get("mode") or "replace") != "replace"):
            return "patch edits one file here: pass path, old_string and new_string"
        if tool_name == "patch" and not target.is_file():
            return "patch needs an existing file"
        if tool_name == "write_file" and target.is_dir():
            return "write_file needs a file path, not a folder"
        return None
    if tool_name == "folder_create":
        target = inside(args.get("path"), roots)
        if target is None or target in roots:
            return f"folder_create needs an absolute path below the folders ({named})"
        if target.exists():
            return "folder_create: that path already exists"
        return None
    if tool_name == "file_trash":
        target = inside(args.get("path"), roots)
        if target is None or target in roots or not target.exists():
            return f"file_trash needs an existing file or folder below the folders ({named}), not a folder itself"
        return None
    if tool_name == "file_copy":
        source, target = inside(args.get("from"), [*roots, attachments(), image_attachments()]), inside(args.get("to"), roots)
        if source is None or target is None or not source.is_file():
            return f"file_copy needs 'from' (an existing file) and 'to', absolute paths inside the folders ({named})"
        if target in roots or target.exists():
            return f"file_copy: {target} already exists; pick another name (nothing is overwritten)"
        return None
    if tool_name == "doc_convert":
        source = inside(args.get("from"), [*roots, attachments(), image_attachments()])
        target = inside(args.get("to"), roots)
        if source is None or not source.is_file():
            return "doc_convert: 'from' must be an existing file in configured folders or chat attachments"
        if target is None or target in roots or target.exists() or Path(args["to"]).is_symlink():
            return "doc_convert: 'to' must be a new path inside configured folders (nothing is overwritten)"
        if (source.suffix.lower(), target.suffix.lower()) not in CONVERT_PAIRS:
            return "doc_convert supports only .pdf → .docx, .docx → .pdf, .xlsx → .pdf"
        if any(key in args for key in ('fit', 'widen_columns')) and source.suffix.lower() != '.xlsx':
            return "doc_convert: fit and widen_columns are only supported for .xlsx sources"
        if args.get('fit', 'width') not in ('width', 'sheet', 'none'):
            return "doc_convert: fit must be width, sheet, or none"
        if type(args.get('widen_columns', False)) is not bool:
            return "doc_convert: widen_columns must be boolean"
        if source.stat().st_size > MAX_CONVERT_BYTES:
            return "doc_convert: source exceeds 100 MB"
        return None
    if tool_name == "doc_edit":
        target = inside(args.get("path"), roots)
        if target is None or target.suffix.lower() not in DOC_KINDS:
            return f"doc_edit works on .xlsx and .docx files inside the folders ({named})"
        operations = args.get("operations")
        if (not isinstance(operations, list) or not 1 <= len(operations) <= MAX_OPERATIONS
                or not all(isinstance(op, dict) and isinstance(op.get("op"), str) for op in operations)):
            return f"doc_edit needs operations: a list of 1 to {MAX_OPERATIONS} objects, each with 'op'"
        if args.get("create") is True:
            if target.exists() or args.get("save_as"):
                return "doc_edit: create makes a new file at path; it must not exist and save_as is not used"
            return None
        if not target.is_file():
            return "doc_edit: no file at path (pass create: true to make a new one)"
        if args.get("save_as"):
            saved = inside(args.get("save_as"), roots)
            if saved is None or saved.suffix.lower() != target.suffix.lower() or saved.exists():
                return "doc_edit: save_as must be a new path of the same type inside the folders"
        return None
    if tool_name == "doc_fill":
        return _fill_problem(args, roots)
    if tool_name == "pdf_pages":
        sources = args.get("sources")
        if (not isinstance(sources, list) or not 1 <= len(sources) <= MAX_PDF_SOURCES
                or not all(isinstance(item, dict) for item in sources)):
            return f"pdf_pages needs sources: a list of 1 to {MAX_PDF_SOURCES} objects with 'path'"
        for item in sources:
            source = inside(item.get("path"), [*roots, attachments(), image_attachments()])
            if source is None or not source.is_file() or source.suffix.lower() != ".pdf":
                return f"pdf_pages: each source must be an existing .pdf inside the folders ({named})"
        target = inside(args.get("to"), roots)
        if target is None or target.suffix.lower() != ".pdf" or target.exists():
            return "pdf_pages: 'to' must be a new .pdf path inside the folders (nothing is overwritten)"
        return None
    if tool_name == "file_move":
        moves = _moves(args)
        if not moves or len(moves) != len(args.get("moves") or []) or len(moves) > MAX_MOVES:
            return f"file_move needs moves: a list of 1 to {MAX_MOVES} objects with 'from' and 'to'"
        sources: set[Path] = set()
        targets: set[Path] = set()
        for move in moves:
            source, target = inside(move.get("from"), roots), inside(move.get("to"), roots)
            if source is None or target is None:
                return f"file_move: 'from' and 'to' must be absolute paths inside the folders ({named})"
            if source in roots or not source.exists():
                return f"file_move: nothing to move at {source}"
            if target in roots or target in targets or source in sources:
                return f"file_move: {target} is named twice or is a top folder"
            if target.exists():
                return f"file_move: {target} already exists; pick another name (nothing is overwritten)"
            if source == target or source in target.parents:
                return f"file_move: cannot move {source} into itself"
            sources.add(source)
            targets.add(target)
        return None
    return None


def _linked(path: Any, roots: list[Path]) -> bool:
    """A symbolic link anywhere between ``path`` and the root it lies in."""
    for part in (Path(str(path)), *Path(str(path)).parents):
        if part.is_symlink():
            return True
        if part.resolve() in roots:
            return False
    return False


def _fill_problem(args: dict[str, Any], roots: list[Path]) -> str | None:
    named = _named(roots)
    action = args.get("action")
    if action not in ("inspect", "fill"):
        return "doc_fill needs action: inspect or fill"
    readable = [*roots, attachments(), image_attachments()]
    template = inside(args.get("template"), readable)
    if (template is None or not template.is_file() or template.suffix.lower() not in DOC_KINDS
            or _linked(args["template"], readable)):
        return f"doc_fill: template must be an existing .docx or .xlsx inside the folders ({named}) or a chat attachment"
    if action == "fill" or args.get("data") is not None:
        data = inside(args.get("data"), readable)
        if data is None or not data.is_file() or data.suffix.lower() not in FILL_DATA_KINDS or _linked(args["data"], readable):
            return f"doc_fill: data must be an existing .xlsx or .csv inside the folders ({named}) or a chat attachment"
    columns = args.get("columns")
    if columns is not None and not (isinstance(columns, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in columns.items())):
        return "doc_fill: columns maps token names to column names, e.g. {\"기관\": \"기관명\"}"
    if args.get("start") is not None and (type(args["start"]) is not int or args["start"] < 1):
        return "doc_fill: start is the first data row to use, 1 or more"
    if args.get("limit") is not None and (type(args["limit"]) is not int or not 1 <= args["limit"] <= MAX_FILL_ROWS):
        return f"doc_fill: limit must be 1 to {MAX_FILL_ROWS}"
    if args.get("name_pattern") is not None and (not isinstance(args["name_pattern"], str) or not 1 <= len(args["name_pattern"]) <= 120):
        return "doc_fill: name_pattern is text like '공문_{기관명}' (at most 120 characters)"
    if action == "fill":
        out = inside(args.get("output_dir"), roots)
        if out is None or out in roots or _linked(args["output_dir"], roots):
            return f"doc_fill: output_dir must be a folder path below the folders ({named})"
        if out.exists() and (not out.is_dir() or any(p.name != ".DS_Store" for p in out.iterdir())):
            return f"doc_fill: {out} is not a new or empty folder; pick another (nothing is overwritten)"
    return None


def _access(root: Path, *, runner: Any = None) -> str | None:
    """Probe in a child: a macOS file-provider approval can stall directory open."""
    if root in _OPENED_ROOTS:
        return None
    try:
        result = (runner or subprocess.run)(
            [sys.executable, "-c", "import os,sys; os.scandir(sys.argv[1]).close()", str(root)],
            capture_output=True, text=True, timeout=ACCESS_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        # subprocess.run kills and reaps the child before raising.
        return (f"the folder {root} did not open within {ACCESS_TIMEOUT} seconds. "
                "macOS is likely waiting for file access approval on the Mac mini screen "
                "(allow osascript to access files managed by a provider such as Synology Drive). "
                "Ask the owner to click Allow on the Mac mini screen; do not retry this call before approval")
    except OSError as exc:
        return f"folder access check failed for {root}: {type(exc).__name__}; ask the owner to check file access permissions"
    if result.returncode != 0:
        if "PermissionError" in result.stderr:
            return f"folder access denied for {root}: PermissionError; ask the owner to allow file access on the Mac mini"
        return f"folder access check failed for {root} (exit {result.returncode}); ask the owner to check folder availability and permissions"
    _OPENED_ROOTS.add(root)
    return None


def problem(tool_name: str, args: dict[str, Any], roots: list[Path], *, runner: Any = None) -> str | None:
    if reason := _path_problem(tool_name, args, roots):
        return reason
    if tool_name not in TOOLS or (tool_name == "file_list" and not str(args.get("path") or "").strip()):
        return None
    if tool_name in ("file_link", "file_location"):
        paths = args["paths"]
    elif tool_name == "file_move":
        paths = [move.get(key) for move in _moves(args) for key in ("from", "to")]
    elif tool_name == "pdf_pages":
        paths = [item.get("path") for item in args["sources"]] + [args.get("to")]
    elif tool_name in ("file_copy", "doc_convert"):
        paths = [args.get("from"), args.get("to")]
    elif tool_name == "doc_edit":
        paths = [args.get("path"), args.get("save_as")]
    elif tool_name == "doc_fill":
        paths = [args.get("template"), args.get("data"), args.get("output_dir")]
    else:
        paths = [args.get("image_url" if tool_name == VIEW_TOOL else "path")]
    for path in paths:
        target = inside(path, roots)
        if target is not None:
            root = next(root for root in roots if root == target or root in target.parents)
            if reason := _access(root, runner=runner):
                return reason
    return None


def _short(path: Any, roots: list[Path]) -> str:
    target = inside(path, roots)
    if target is None:
        return str(path)
    for root in roots:
        if root == target or root in target.parents:
            return str(Path(root.name) / target.relative_to(root)) if target != root else root.name
    return str(target)


def approval(tool_name: str, args: dict[str, Any], roots: list[Path], plan: dict[str, Any] | None = None) -> dict[str, str]:
    """The question shown before one of this module's own changes. "승인하면" is the wording the
    desktop-pet chat puts its buttons under."""
    if tool_name == "file_move":
        moves = _moves(args)
        lines = [f"{_short(m.get('from'), roots)} → {_short(m.get('to'), roots)}" for m in moves]
        shown = "\n".join(lines[:20]) + (f"\n… 외 {len(lines) - 20}건" if len(lines) > 20 else "")
        message = f"승인하면 {len(moves)}건을 옮기거나 이름을 바꿉니다:\n{shown}"
    elif tool_name == "folder_create":
        message = f"승인하면 폴더를 만듭니다: {_short(args.get('path'), roots)}"
    elif tool_name == "file_copy":
        message = f"승인하면 복사합니다: {_short(args.get('from'), roots)} → {_short(args.get('to'), roots)}"
    elif tool_name == "doc_convert":
        message = (f"승인하면 변환해 새 파일을 만듭니다: {_short(args.get('from'), [*roots, attachments(), image_attachments()])} → "
                   f"{_short(args.get('to'), roots)} (원본은 그대로)")
        if args.get('fit', 'width') != 'width':
            message += " / 인쇄: " + {'sheet': '시트당 한 쪽', 'none': '원본 인쇄 설정'}.get(args['fit'], str(args['fit']))
        if args.get('widen_columns') is True:
            message += " / 글자 길이에 맞춰 열 넓히기"
    elif tool_name == "doc_edit":
        kinds: dict[str, int] = {}
        for op in args.get("operations") or []:
            kinds[str(op.get("op"))] = kinds.get(str(op.get("op")), 0) + 1
        summary = ", ".join(f"{name} {count}건" for name, count in kinds.items())
        if args.get("create") is True:
            where = f"새 문서를 만듭니다: {_short(args.get('path'), roots)}"
        elif args.get("save_as"):
            where = (f"문서를 고쳐 새 파일로 저장합니다: {_short(args.get('path'), roots)} → "
                     f"{_short(args.get('save_as'), roots)} (원본은 그대로)")
        else:
            where = f"문서를 고쳐 원본에 덮어씁니다: {_short(args.get('path'), roots)}"
        message = f"승인하면 {where}\n작업: {summary}"
    elif tool_name == "pdf_pages":
        parts = [f"{_short(item.get('path'), [*roots, attachments(), image_attachments()])}"
                 + (f" [{item.get('pages')}쪽]" if item.get("pages") else "")
                 + (f" (회전 {item.get('rotate')}°)" if item.get("rotate") else "")
                 for item in args.get("sources") or []]
        shown = "\n".join(parts[:20]) + (f"\n… 외 {len(parts) - 20}건" if len(parts) > 20 else "")
        message = f"승인하면 새 PDF를 만듭니다: {_short(args.get('to'), roots)}\n{shown}"
    elif tool_name == "doc_fill":
        plan, readable = plan or {}, [*roots, attachments(), image_attachments()]
        first = plan.get("start", 1)
        out = inside(args.get("output_dir"), roots)
        message = (f"승인하면 양식을 채워 새 파일 {plan.get('selected', '?')}개를 만듭니다 (양식·데이터 원본은 그대로)\n"
                   f"양식: {_short(args.get('template'), readable)}\n"
                   f"데이터: {_short(args.get('data'), readable)} ({first}~{first + plan.get('selected', 1) - 1}행 / 전체 {plan.get('rows', '?')}행)\n"
                   f"출력 폴더: {_short(args.get('output_dir'), roots)} ({'빈 폴더' if out is not None and out.exists() else '새 폴더'})")
        if plan.get("sample_names"):
            message += "\n파일 이름 예: " + ", ".join(plan["sample_names"])
        flagged = [f"{t}(열 없음)" for t in plan.get("unmapped_tokens") or []]
        flagged += [f"{t}({n}행 빈 값)" for t, n in (plan.get("blank_values") or {}).items()]
        if flagged:
            message += "\n«누락» 표시될 값: " + ", ".join(flagged[:10]) + (f" 외 {len(flagged) - 10}개" if len(flagged) > 10 else "")
    else:
        message = f"승인하면 휴지통으로 옮깁니다: {_short(args.get('path'), roots)}"
    call = json.dumps(args, ensure_ascii=False, sort_keys=True, default=str)
    # The key covers this one call, so an "always" answer cannot wave later ones through.
    return {"action": "approve", "message": message,
            "rule_key": f"team-kit-files:{tool_name}:{hashlib.sha256(call.encode('utf-8')).hexdigest()[:12]}"}


NO_APPROVAL_TOOLS = frozenset({'folder_create', 'file_copy'})


def _no_approval(config):
    value = config.get('no_approval', []) if isinstance(config, dict) else []
    if not isinstance(value, list) or any(not isinstance(name, str) or name not in NO_APPROVAL_TOOLS for name in value):
        return set(), 'files.no_approval 설정 오류: folder_create·file_copy 목록만 허용하므로 승인을 유지한다.'
    return set(value), ''


def schema(config, name):
    result = dict(SCHEMAS[name])
    allowed, error = _no_approval(config)
    if name in allowed:
        result['description'] = result['description'].replace('after the user approves',
            'immediately when the owner explicitly asks in chat, without another approval')
    return result


def guard(config: Any, *, tool_name: str, args: Any, chat: bool) -> dict[str, str] | None:
    """Block a file call outside the folders; ask before this module's own changes."""
    if not manages(config, tool_name):
        return None
    if not available(config, tool_name):
        return {"action": "block", "message": f"{tool_name} is disabled for this profile."}
    if tool_name == "file_link" and not chat:
        return {"action": "block", "message": "file_link is available only in a Slack chat, not in scheduled or background runs."}
    roots = folders(config)
    if not roots:
        if isinstance(config, dict) and config.get("extra_only") is True:
            return {"action": "block", "message": "No folders are configured or accessible for these extra tools."}
        return None if tool_name not in OWN_CHANGE_TOOLS | {"file_list", "file_link", "file_location"} else {
            "action": "block", "message": "No folders are configured for this profile."}
    call = args if isinstance(args, dict) else {}
    if reason := problem(tool_name, call, roots):
        return {"action": "block", "message": reason}
    if tool_name == "doc_fill" and call.get("action") == "inspect":
        return None  # reading only, like doc_read
    if tool_name == "doc_fill" and chat:
        plan = json.loads(_ask(_fill_job("fill_inspect", call, roots)))
        if plan.get("ok") is not True:
            return {"action": "block", "message": f"doc_fill: {plan.get('error') or 'could not check the template and data'}"}
        if not plan.get("tokens") or not plan.get("selected") or plan["selected"] > MAX_FILL_ROWS:
            return {"action": "block", "message": "doc_fill: " + (
                "the template has no {{tokens}}" if not plan.get("tokens") else "no data rows selected" if not plan.get("selected")
                else f"{plan['selected']} rows selected; at most {MAX_FILL_ROWS} per call (use start and limit)")}
        return approval(tool_name, call, roots, plan)
    if tool_name in OWN_CHANGE_TOOLS:
        if not chat:
            return {"action": "block", "message": f"{tool_name} needs the user's approval in chat."}
        allowed, error = _no_approval(config)
        if tool_name in allowed:
            return None
        result = approval(tool_name, call, roots)
        if error:
            result['message'] = error + ' ' + result['message']
        return result
    return None


def note(config: Any) -> str:
    roots = folders(config)
    if not roots:
        return ""
    convert_note = ("doc_convert(from, to)는 PDF→DOCX·DOCX→PDF·XLSX→PDF를 승인받아 새 파일로 만든다. "
                    if available(config, "doc_convert") else "")
    if isinstance(config, dict) and config.get("extra_only") is True:
        return (convert_note + f"[추가 파일 도구] 대상 폴더: {_named(roots)}. folder_scan은 하위 목록·중복, doc_read는 문서, "
                "pdf_render는 PDF 쪽 그림, vision_analyze는 지정 폴더·대화 첨부·쪽 그림의 이미지만 읽는다(웹 주소 거부). "
                "file_location은 지정 폴더의 NAS 내부 링크를 돌려준다. 받은 링크만 쓰고 없으면 받지 못했다고 알린다. "
                "file_move·file_copy·folder_create·doc_edit·pdf_pages는 이 폴더에서만 변경하며 대화에서 호출마다 승인받는다. "
                "예약·배경 변경은 거부한다. 옮기기·복사·새 파일은 덮어쓰지 않으며 doc_edit의 원본 덮어쓰기는 먼저 알린다. "
                "기존 읽기·쓰기·휴지통 도구의 범위와 승인은 별도 파일 안내를 따른다. 절차는 jarvis-files를 연다.")
    allowed, config_error = _no_approval(config)
    approval_note = ('소유자가 분명히 시킨 ' + '·'.join(sorted(allowed)) +
                     '는 승인 없이 바로 실행한다. 나머지 변경은 호출마다 사용자 승인을 묻는다. ')
    if not allowed:
        approval_note = '변경은 호출마다 사용자 승인을 묻는다. '
    approval_note += config_error
    location_note = (
        "위치·링크는 file_location(paths=[절대 경로])에 묶어 조회한다. [파일 이름](file_url)·[폴더 이름](url)로 쓰고 file_url이 없으면 폴더 링크·파일 이름과 미확인을 알린다. "
        "exact=false면 linked_folder와 below를, unlinked면 링크 없음과 폴더 위치를 알린다. 반복 실패는 기존 진단 절차를 따른다. "
        "반환된 url만 쓰고 Mac 경로·예상 주소·Slack 업로드로 링크를 대신하지 않는다. 사진을 보여 달라는 요청만 MEDIA로 첨부한다. "
    ) if available(config, "file_location") else (
        "파일 링크를 줄 때마다(다시 달라는 요청 포함) 그 턴에서 file_link(paths=[절대 경로])를 호출한다. 대화의 예전 주소를 재사용하지 않고 받은 url을 한 글자도 바꾸지 않아 [파일명](주소)로 적는다. "
        "파일은 이 대화 스레드에 올라가고 주소를 돌려준다. 같은 방에 공유된 파일만 재사용하며 MEDIA로 다시 첨부하지 않는다. 링크는 마지막 요청 30일 뒤 지워지며, 열리지 않는다고 하면 file_link로 새 주소를 준다. 받은 주소만 쓰고 Mac 경로나 지어낸 주소는 링크로 쓰지 않는다. 예약·배경 실행에서는 쓸 수 없다. "
    )
    return (
        convert_note + (approval_note if allowed or config_error else "") +
        f"[파일] 이 Mac에서 열 수 있는 곳은 다음 폴더 안뿐이다: {_named(roots)}. "
        "file_list는 한 단계 목록, folder_scan은 하위 목록·개수·중복, search_files는 검색, read_file은 일반 파일 읽기다. 허용 폴더의 절대 경로를 쓴다. "
        "PDF·Word(.docx)·Excel(.xlsx)·한글(.hwp·.hwpx)은 doc_read로 읽는다. PDF는 5쪽씩 pages·next_page로 이어 읽고 textless_pages·잘림은 pdf_render→vision_analyze로 확인한다. 구형 .doc·.xls와 다른 문서는 read_file을 쓴다. 암호·배포용 한글은 PDF를 요청한다. "
        "사진은 vision_analyze(image_url=절대 경로)로 본다. 사용자 첨부는 메시지의 경로 그대로 읽는다. "
        + location_note +
        "옮기기·이름 바꾸기는 file_move(moves=[{from, to}], 한 번에 50건까지), 복사는 file_copy, 새 폴더는 folder_create, "
        "삭제는 file_trash(휴지통 이동)다. 글 파일 내용은 write_file·patch로, Excel(.xlsx)·Word(.docx) 내용은 doc_edit로 고치고, "
        + ("같은 양식 여러 부는 doc_fill(inspect 먼저), " if available(config, "doc_fill") else "") +
        "PDF 합치기·쪽 뽑기·회전은 pdf_pages로 새 PDF를 만든다. "
        + ("" if allowed or config_error else approval_note) + "여러 파일을 정리할 때는 "
        "먼저 계획을 보여 주고 한 번의 file_move로 묶는다. 옮기기·복사·새 파일은 기존 파일을 덮어쓰지 않는다. "
        "doc_edit는 save_as가 없으면 원본을 덮어쓰므로 그 사실을 미리 말한다. 결과는 도구가 돌려준 대로만 말한다."
    )


def _error(message: str) -> str:
    return json.dumps({"ok": False, "error": message}, ensure_ascii=False)


def _link_problem(path: Any, roots: list[Path]) -> str | None:
    target = inside(path, [*roots, attachments(), image_attachments(), renders()])
    if not isinstance(path, str) or not Path(path).is_absolute() or target is None:
        return "file_link needs an absolute file path inside the readable folders, chat attachments, or rendered pages"
    try:
        source = Path(path)
        if any(part.is_symlink() for part in (source, *source.parents)):
            return "file_link does not upload symbolic links"
        if not target.is_file():
            return "file_link needs an existing file"
        if target.stat().st_size > MAX_LINK_BYTES:
            return "file_link accepts at most 50 MB per file"
    except OSError:
        return "file_link could not inspect the file"
    return None


def _link_token() -> str:
    from dotenv import dotenv_values
    from hermes_constants import get_hermes_home
    return str(dotenv_values(Path(get_hermes_home()) / ".env", interpolate=False).get("SLACK_BOT_TOKEN") or "")


def _link_destination() -> tuple[str, str]:
    from gateway.session_context import get_session_env
    return get_session_env("HERMES_SESSION_CHAT_ID"), get_session_env("HERMES_SESSION_THREAD_ID")


def _link_shared(info: dict, channel: str) -> bool:
    file = info.get("file")
    if not isinstance(file, dict) or not any(isinstance(file.get(key), list) for key in ("channels", "groups", "ims")):
        raise ValueError("Slack returned no sharing information")
    return any(isinstance(file.get(key), list) and channel in file[key] for key in ("channels", "groups", "ims"))


def _link_session() -> bool:
    from gateway.session_context import get_session_env
    unattended = any(str(get_session_env(key, "")).strip().lower() in ("1", "true", "yes", "on")
                     for key in ("HERMES_CRON_SESSION", "HERMES_SINGLE_QUERY_SESSION"))
    return (get_session_env("HERMES_SESSION_PLATFORM") == "slack"
            and bool(get_session_env("HERMES_SESSION_CHAT_ID"))
            and not unattended and not os.environ.get("HERMES_KANBAN_TASK"))


class _LinkError(ValueError):
    def __init__(self, code: Any):
        super().__init__("Slack refused the file request")
        self.missing = code in ("file_not_found", "file_deleted")


def _link_state() -> Path:
    from hermes_constants import get_hermes_home
    return Path(get_hermes_home()) / "state/file-links.json"


@contextmanager
def _link_locked():
    target = _link_state().with_suffix(".lock")
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _link_records() -> dict:
    try:
        records = json.loads(_link_state().read_text(encoding="utf-8"))
        if not isinstance(records, dict):
            return {}
        return {key: {field: value[field] for field in ("file_id", "permalink", "recorded_at")}
                for key, value in records.items()
                if isinstance(value, dict) and isinstance(value.get("file_id"), str)
                and isinstance(value.get("permalink"), str) and type(value.get("recorded_at")) in (int, float)}
    except (OSError, ValueError):
        return {}


def _save_link_records(records: dict) -> None:
    target = _link_state()
    temporary = target.with_name(f".{target.name}.{secrets.token_hex(8)}.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(records, stream, ensure_ascii=False)
        os.replace(temporary, target)
    except OSError:
        pass  # A local bookkeeping failure does not undo a successful Slack upload.
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _link_permalink(info: dict) -> str:
    permalink = info["file"]["permalink"]
    parsed = urllib.parse.urlsplit(permalink)
    if parsed.scheme != "https" or not (parsed.hostname or "").endswith(".slack.com"):
        raise ValueError("Slack returned no valid permalink")
    file = info["file"]
    if not all(isinstance(file.get(key), str) and re.fullmatch(r"[A-Z0-9]+", file[key])
               for key in ("user", "id")):
        raise ValueError("Slack returned invalid file identifiers")
    return f"https://{parsed.hostname}/files/{file['user']}/{file['id']}"


def _link_api(token: str, method: str, body: dict, opener: Any) -> dict:
    request = urllib.request.Request("https://slack.com/api/" + method,
                                     data=urllib.parse.urlencode(body).encode(),
                                     headers={"Authorization": "Bearer " + token})
    with opener(request, timeout=30) as response:
        answer = json.load(response)
    if not isinstance(answer, dict) or answer.get("ok") is not True:
        raise _LinkError(answer.get("error") if isinstance(answer, dict) else None)
    return answer


class _LinkSharingPending(ValueError):
    pass


def _wait_link_shared(token: str, file_id: str, channel: str, info: dict, opener: Any, sleep: Any) -> str:
    for attempt in range(21):
        file = info.get("file")
        if not isinstance(file, dict):
            raise ValueError("Slack returned no file object")
        permalink = _link_permalink(info) if file.get("permalink") else None
        has_sharing = any(isinstance(file.get(key), list) for key in ("channels", "groups", "ims"))
        if permalink and has_sharing and _link_shared(info, channel):
            return permalink
        if attempt == 20:
            raise _LinkSharingPending()
        sleep(0.5)
        info = _link_api(token, "files.info", {"file": file_id}, opener)
    raise _LinkSharingPending()


def file_link(config: Any, args: dict[str, Any], opener: Any = None, sleep: Any = None, **_: Any) -> str:
    if not available(config, "file_link"):
        return _error("file_link is disabled; use file_location for Synology folder links")
    try:
        with _link_locked():
            return _file_link(config, args, opener=opener, sleep=sleep or time.sleep)
    except OSError:
        return _error("Could not lock file link records; no upload attempted")


def _file_link(config: Any, args: dict[str, Any], opener: Any = None, sleep: Any = time.sleep) -> str:
    """Upload into the current thread and return only a channel-shared permalink."""
    if not _link_session():
        return _error("file_link is available only in a Slack chat, not in scheduled or background runs")
    channel, thread = _link_destination()
    if not channel or not thread:
        return _error("file_link needs the current Slack channel and thread; no upload attempted")
    roots = folders(config)
    if not roots:
        return _error("No folders are configured for this profile.")
    if reason := problem("file_link", args, roots):
        return _error(reason)
    try:
        token = _link_token()
    except OSError:
        return _error("Could not read this profile's .env")
    if not token:
        return _error("SLACK_BOT_TOKEN is missing from this profile's .env")
    open_url = opener or urllib.request.urlopen
    links, failed = [], []
    records = _link_records()
    for path in args["paths"]:
        try:
            if reason := _link_problem(path, roots):
                raise ValueError(reason)
            target = inside(path, [*roots, attachments(), image_attachments(), renders()])
            with target.open("rb") as source:
                data = source.read(MAX_LINK_BYTES + 1)
            if len(data) > MAX_LINK_BYTES:
                raise ValueError("file exceeds 50 MB")
            key = hashlib.sha256(data).hexdigest() + ":" + target.name + ":" + channel
            if cached := records.get(key):
                try:
                    info = _link_api(token, "files.info", {"file": cached["file_id"]}, open_url)
                except _LinkError as exc:
                    if not exc.missing:
                        raise
                    del records[key]
                    _save_link_records(records)
                else:
                    permalink = _wait_link_shared(token, cached["file_id"], channel, info, open_url, sleep)
                    cached.update(permalink=permalink, recorded_at=time.time())
                    _save_link_records(records)
                    links.append({"path": path, "name": target.name, "url": permalink, "reused": True})
                    continue
            upload = _link_api(token, "files.getUploadURLExternal", {"filename": target.name, "length": len(data)}, open_url)
            url = urllib.parse.urlsplit(upload["upload_url"])
            if url.scheme != "https" or url.hostname != "files.slack.com":
                raise ValueError("unexpected Slack upload URL")
            with open_url(urllib.request.Request(upload["upload_url"], data=data,
                          headers={"Content-Type": "application/octet-stream"}), timeout=60) as response:
                if response.status != 200:
                    raise ValueError("Slack upload failed")
            _link_api(token, "files.completeUploadExternal",
                      {"files": json.dumps([{"id": upload["file_id"], "title": target.name}]),
                       "channel_id": channel, "thread_ts": thread}, open_url)
            info = _link_api(token, "files.info", {"file": upload["file_id"]}, open_url)
            file = info.get("file")
            if not isinstance(file, dict):
                raise ValueError("Slack returned no file object")
            permalink = _link_permalink(info) if file.get("permalink") else ""
            records[key] = {"file_id": upload["file_id"], "permalink": permalink, "recorded_at": time.time()}
            _save_link_records(records)
            permalink = _wait_link_shared(token, upload["file_id"], channel, info, open_url, sleep)
            records[key]["permalink"] = permalink
            _save_link_records(records)
            links.append({"path": path, "name": target.name, "url": permalink})
        except _LinkSharingPending:
            saved = _link_records().get(key)
            if saved and saved == records.get(key):
                message = ("file_link sharing confirmation is delayed; the upload is recorded. "
                           "Call file_link once more with the same path after a short wait; "
                           "it will check the existing file without uploading again.")
            else:
                message = "file_link sharing confirmation is delayed and its record could not be saved. Do not retry."
            failed.append({"path": path, "error": message})
            break
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
            # No response bodies, upload URLs or tokens in errors; an uncertain upload is never repeated.
            failed.append({"path": path, "error": f"file_link failed ({type(exc).__name__}); upload may have completed. Do not retry."})
            break
    not_attempted = args["paths"][len(links) + len(failed):]
    return json.dumps({"ok": not failed, "links": links, **({"failed": failed} if failed else {}),
                       "not_attempted": not_attempted}, ensure_ascii=False)


def file_list(config: Any, args: dict[str, Any], runner: Any = None, **_: Any) -> str:
    roots = folders(config)
    if not str(args.get("path") or "").strip():
        return json.dumps({"ok": True, "readable_folders": [str(root) for root in roots]}, ensure_ascii=False)
    if reason := problem("file_list", args, roots, runner=runner):
        return _error(reason)
    target = inside(args.get("path"), [*roots, attachments(), image_attachments()])
    if target is None or not target.is_dir():
        return _error(f"file_list needs the absolute path of a folder inside the readable folders ({_named(roots)})")
    try:
        entries = sorted(target.iterdir(), key=lambda entry: entry.name.casefold())
    except OSError as exc:
        return json.dumps({"ok": False, "complete": False, "error": f"could not list the folder: {type(exc).__name__}",
                           "unreadable_folders": [{"path": ".", "error": type(exc).__name__, "errno": exc.errno}]})
    items = []
    unreadable = 0
    for entry in entries[:LIST_LIMIT]:
        try:
            stat = entry.stat()
            items.append({"name": entry.name, "type": "folder" if entry.is_dir() else "file",
                          "size": None if entry.is_dir() else stat.st_size, "modified": int(stat.st_mtime)})
        except OSError as exc:
            unreadable += 1
            items.append({"name": entry.name, "type": "unreadable", "error": type(exc).__name__, "errno": exc.errno})
    return json.dumps({"ok": True, "path": str(target), "entries": items, "total": len(entries),
                       "truncated": len(entries) > LIST_LIMIT, "unreadable_entries": unreadable,
                       "complete": not unreadable and len(entries) <= LIST_LIMIT}, ensure_ascii=False)


def folder_create(config: Any, args: dict[str, Any], **_: Any) -> str:
    roots = folders(config)
    if reason := problem("folder_create", args, roots):
        return _error(reason)
    target = inside(args.get("path"), roots)
    try:
        target.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        return _error(f"could not create the folder: {type(exc).__name__}")
    return json.dumps({"ok": True, "created": str(target)}, ensure_ascii=False)


def _rename_exclusive(source, target):
    """Publish a file or directory without replacing a concurrently created name."""
    if sys.platform == "darwin":
        rename = ctypes.CDLL(None, use_errno=True).renamex_np
        rename.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
        rename.restype = ctypes.c_int
        if rename(os.fsencode(source), os.fsencode(target), 0x00000004):  # RENAME_EXCL
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code), str(target))
    else:
        # POSIX links cannot atomically move directories; fail closed there.
        os.link(source, target, follow_symlinks=False)
        os.unlink(source)


def _move_exclusive(source, target):
    try:
        _rename_exclusive(source, target)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    # Copy on the destination volume, then publish with the same no-replace rule.
    with tempfile.TemporaryDirectory(prefix=".hermes-move-", dir=target.parent) as temporary:
        staged = Path(temporary) / "payload"
        if source.is_dir():
            shutil.copytree(source, staged, symlinks=True)
        else:
            shutil.copy2(source, staged, follow_symlinks=False)
        _rename_exclusive(staged, target)
    if source.is_dir():
        shutil.rmtree(source)
    else:
        source.unlink()


def file_move(config: Any, args: dict[str, Any], **_: Any) -> str:
    """Move in order and stop at the first failure, reporting what moved and what did not."""
    roots = folders(config)
    if reason := problem("file_move", args, roots):
        return _error(reason)
    pairs = [(inside(m.get("from"), roots), inside(m.get("to"), roots)) for m in _moves(args)]
    moved: list[dict[str, str]] = []
    failed = None
    for index, (source, target) in enumerate(pairs):
        try:
            if target.exists():
                raise FileExistsError(str(target))
            target.parent.mkdir(parents=True, exist_ok=True)
            _move_exclusive(source, target)
            moved.append({"from": str(source), "to": str(target)})
        except (OSError, shutil.Error) as exc:
            failed = {"from": str(source), "to": str(target), "error": type(exc).__name__, "index": index}
            break
    not_attempted = [{"from": str(s), "to": str(t)} for s, t in pairs[len(moved) + (1 if failed else 0):]]
    return json.dumps({"ok": failed is None, "moved": moved, **({"failed": failed} if failed else {}), "not_attempted": not_attempted},
                      ensure_ascii=False)


def file_trash(config: Any, args: dict[str, Any], runner: Any = None, **_: Any) -> str:
    roots = folders(config)
    if reason := problem("file_trash", args, roots):
        return _error(reason)
    target = inside(args.get("path"), roots)
    run = runner or subprocess.run

    def trash(item: Path) -> bool:
        result = run(["/usr/bin/osascript", "-l", "JavaScript", "-e", _TRASH_SCRIPT, str(item)],
                     capture_output=True, text=True, timeout=30)
        return result.returncode == 0 and result.stdout.strip() == "trashed" and not item.exists()

    try:
        if trash(target):
            return json.dumps({"ok": True, "trashed": str(target)}, ensure_ascii=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return _error(f"could not reach the Trash: {type(exc).__name__}")
    if CLOUD_STORAGE not in target.parents or not target.exists():
        return _error("the item was not moved to the Trash")
    # The item keeps its name in the Trash; the folder around it is only a holding place.
    holding = Path(tempfile.mkdtemp(prefix="team-kit-trash-"))
    staged = holding / target.name
    try:
        shutil.move(str(target), str(staged))
    except (OSError, shutil.Error) as exc:
        if target.exists():  # a copy that stopped halfway leaves the original whole
            shutil.rmtree(holding, ignore_errors=True)
        return _error(f"the item was not moved to the Trash (could not take it out of the synced folder: {type(exc).__name__})")
    try:
        trashed = trash(staged)
    except (OSError, subprocess.SubprocessError):
        trashed = False
    if trashed:
        holding.rmdir()
        return json.dumps({"ok": True, "trashed": str(target),
                           "note": "synced folder without its own Trash: moved out to this Mac's Trash"}, ensure_ascii=False)
    try:
        shutil.move(str(staged), str(target))
        holding.rmdir()
    except (OSError, shutil.Error):
        return _error(f"the item was not moved to the Trash and could not be put back; it is now at {staged}")
    return _error("the item was not moved to the Trash")


def _ask(job: dict[str, Any], *, runner: Any = None, timeout: int = 120) -> str:
    """Run a read-only worker job and return its JSON as it is."""
    if not DOC_PYTHON.exists():
        return _error("the document environment (~/.local/share/hermes-docs) is missing; "
                      "it has to be rebuilt in the Paseo hermes-config project")
    try:
        result = (runner or subprocess.run)(
            [str(DOC_PYTHON), str(Path(__file__).with_name("doc_worker.py"))],
            input=json.dumps(job, ensure_ascii=False), capture_output=True, text=True, timeout=timeout)
        answer = json.loads(result.stdout.strip().splitlines()[-1])
    except (OSError, subprocess.SubprocessError, ValueError, IndexError) as exc:
        return _error(f"could not read the document: {type(exc).__name__}")
    return json.dumps(answer if isinstance(answer, dict) else {"ok": False, "error": "no result"}, ensure_ascii=False)


def doc_read(config: Any, args: dict[str, Any], runner: Any = None, **_: Any) -> str:
    roots = folders(config)
    if reason := problem("doc_read", args, roots):
        return _error(reason)
    path = inside(args.get("path"), [*roots, attachments(), image_attachments()])
    job = {"kind": "read_" + READ_DOC_KINDS[path.suffix.lower()], "source": str(path),
           "sheet": args.get("sheet"), "range": args.get("range"), "offset": args.get("offset"), "pages": args.get("pages")}
    return _ask(job, runner=runner)


def pdf_render(config: Any, args: dict[str, Any], runner: Any = None, **_: Any) -> str:
    roots = folders(config)
    if reason := problem("pdf_render", args, roots):
        return _error(reason)
    path = inside(args.get("path"), [*roots, attachments(), image_attachments()])
    cache = renders()
    try:
        cache.mkdir(parents=True, exist_ok=True)
        cutoff = time.time() - 86400
        for old in cache.iterdir():  # yesterday's page images are not kept
            if old.is_dir() and old.stat().st_mtime < cutoff:
                shutil.rmtree(old, ignore_errors=True)
    except OSError as exc:
        return _error(f"could not prepare the page image folder: {type(exc).__name__}")
    stat = path.stat()
    key = hashlib.sha256(f"{path}|{stat.st_mtime_ns}|{stat.st_size}".encode("utf-8")).hexdigest()[:16]
    return _ask({"kind": "render", "source": str(path), "pages": args.get("pages"), "limit": MAX_RENDER_PAGES,
                 "output_dir": str(cache / key)}, runner=runner)


NAS_SCAN_SECONDS = NAS_TOTAL_SECONDS
NAS_REPORT_LIMIT = 100
NAS_FILES_LIMIT = 200


def _nas_inventory(config, top, opener=None):
    """Metadata only: verified Drive list v1, paginated per folder. No file GET/download."""
    from concurrent.futures import ThreadPoolExecutor
    import unicodedata
    settings = config['synology']
    base = settings['api_base'].rstrip('/')
    parsed = urllib.parse.urlsplit(base)
    sync_root = Path(settings['sync_root'])
    host, drive_root = settings['link_host'], settings['drive_root']
    if (parsed.scheme != 'https' or not (parsed.hostname or '').endswith('.example.com')
            or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password or parsed.port
            or not _folder_url(f'https://{host}/d/f/TEST') or not sync_root.is_absolute()
            or drive_root != '/mydrive' or settings.get('user') != 'SYNOLOGY_DRIVE_USER'
            or settings.get('password') != 'SYNOLOGY_DRIVE_PASSWORD'):
        raise ValueError('invalid NAS config')
    relative = top.relative_to(sync_root.resolve()).as_posix()
    remote = drive_root + ('/' + relative if relative != '.' else '')
    started = time.monotonic()
    deadline = started + NAS_SCAN_SECONDS
    scope = hashlib.sha256(json.dumps([base, host, str(sync_root), drive_root]).encode()).hexdigest()
    from hermes_constants import get_hermes_home
    home = Path(get_hermes_home())
    open_url = opener or urllib.request.build_opener(_NoNasRedirect).open
    calls = 0
    if not _NAS_LOCK.acquire(timeout=max(0, deadline - time.monotonic())):
        raise TimeoutError()
    try:
        session = _NAS_SESSIONS.setdefault((str(home), scope), {'sid': None, 'cooldown': 0})
        if session['cooldown'] > time.monotonic():
            raise _NasError()
        if not session['sid']:
            calls += _nas_login(base, settings, home, session, open_url, deadline)
        sid = session['sid']
    finally:
        _NAS_LOCK.release()
    def listing(item):
        rel, path = item
        rows, offset, requests = [], 0, 0
        while True:
            if time.monotonic() >= deadline:
                raise TimeoutError()
            data = _nas_api(base, {'api': 'SYNO.SynologyDrive.Files', 'version': 1, 'method': 'list',
                'path': json.dumps(path), 'offset': offset, 'limit': 500, '_sid': sid}, open_url, deadline)
            requests += 1
            page, total = data.get('items'), data.get('total')
            if not isinstance(page, list) or type(total) is not int or total < 0 or total > 10000:
                raise ValueError('invalid NAS page')
            rows.extend(page)
            offset += len(page)
            if offset >= total:
                if offset != total:
                    raise ValueError('NAS listing changed')
                return rel, path, rows, requests
            if not page or requests >= 100:
                raise ValueError('incomplete NAS page')
    inventory = {}
    pending = [('.', remote)]
    count = 0
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            while pending:
                next_level = []
                for rel, path, rows, requests in pool.map(listing, pending):
                    calls += requests
                    names, dirs, file_rows = set(), set(), {}
                    for row in rows:
                        if not isinstance(row, dict) or not isinstance(row.get('name'), str):
                            raise ValueError('invalid NAS item')
                        raw = row['name']
                        name = unicodedata.normalize('NFC', raw)
                        if not name or '/' in name or '\x00' in name or name in ('.', '..'):
                            raise ValueError('invalid NAS name')
                        if name.startswith('.'):
                            continue
                        if name in names:
                            raise ValueError('ambiguous NAS names')
                        names.add(name)
                        if row.get('type') == 'dir':
                            dirs.add(name)
                            child = name if rel == '.' else rel + '/' + name
                            next_level.append((child, path + '/' + raw))
                        elif row.get('type') == 'file':
                            if type(row.get('size')) is not int or type(row.get('modified_time')) is not int:
                                raise ValueError('invalid NAS metadata')
                            file_rows[name] = {'size': row['size'], 'modified': row['modified_time']}
                        else:
                            raise ValueError('unknown NAS item type')
                        count += 1
                    inventory[rel] = {'dirs': dirs, 'files': file_rows}
                    if count > 10000 or len(inventory) + len(next_level) > 2000 or calls > 1000:
                        raise ValueError('NAS listing limit')
                pending = next_level
    except _NasError as exc:
        if exc.code in (106, 107, 119):
            with _NAS_LOCK:
                if session['sid'] == sid:
                    session['sid'] = None
        raise
    return inventory, calls, round(time.monotonic() - started, 3)


def _refresh_scan_folder(path, timeout):
    # Isolate File Provider stalls. Only scandir/stat; no file content is opened.
    code = '''import os,sys,json,stat
rows=[]
with os.scandir(sys.argv[1]) as items:
 for e in items:
  if e.name.startswith('.'): continue
  s=e.stat(follow_symlinks=False)
  rows.append([e.name,stat.S_ISDIR(s.st_mode),s.st_size,int(s.st_mtime)])
print(json.dumps(rows))'''
    proc = subprocess.run([sys.executable, '-c', code, str(path)], capture_output=True,
                          text=True, timeout=timeout, check=False)
    if proc.returncode:
        raise OSError('local recheck failed')
    return json.loads(proc.stdout)


def _compare_nas(config, top, local, local_paths, opener=None):
    import unicodedata
    settings = config.get('synology')
    if not isinstance(settings, dict) or settings.get('enabled') is not True:
        return {'status': 'skipped', 'reason': 'not_configured' if settings is None else 'disabled'}, []
    try:
        if not top.is_relative_to(Path(settings['sync_root']).resolve()):
            return {'status': 'skipped', 'reason': 'outside_sync_root'}, []
        remote, calls, elapsed = _nas_inventory(config, top, opener)
        def different(key):
            a, b = local.get(key), remote.get(key)
            return a is None or b is None or a['dirs'] != b['dirs'] or set(a['files']) != set(b['files'])
        keys = sorted(set(local) | set(remote))
        deadline = time.monotonic() + 3
        recheck_failed = 0
        for key in keys:
            if not different(key):
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                recheck_failed += 1
                continue
            path = local_paths.get(key, top / key)
            try:
                rows = _refresh_scan_folder(path, min(0.5, remaining))
                dirs, file_rows = set(), {}
                for name, is_dir, size, modified in rows:
                    normalized = unicodedata.normalize('NFC', name)
                    if is_dir:
                        dirs.add(normalized)
                    else:
                        file_rows[normalized] = {'size': size, 'modified': modified, 'path': path / name}
                local[key] = {'dirs': dirs, 'files': file_rows}
            except (OSError, ValueError, subprocess.TimeoutExpired):
                recheck_failed += 1
        mismatched, missing = [], []
        for key in keys:
            a, b = local.get(key, {'files': {}, 'dirs': set()}), remote.get(key, {'files': {}, 'dirs': set()})
            if different(key):
                mismatched.append({'path': key, 'nas_files': len(b['files']), 'local_files': len(a['files'])})
            for name in sorted(set(b['files']) - set(a['files'])):
                missing.append({'path': name if key == '.' else key + '/' + name, **b['files'][name]})
        return {'status': 'compared', 'files': sum(len(v['files']) for v in remote.values()),
                'folders': len(remote) - 1, 'calls': calls, 'seconds': elapsed,
                'mismatched_folders': mismatched[:NAS_REPORT_LIMIT],
                'mismatched_folders_omitted': max(0, len(mismatched) - NAS_REPORT_LIMIT),
                'not_local_count': len(missing), 'local_recheck_failed': recheck_failed}, missing
    except Exception as exc:
        # Do not expose URLs, account names, session IDs or exception text.
        return {'status': 'failed', 'reason': 'timeout' if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)) else 'lookup_failed'}, []


def folder_scan(config: Any, args: dict[str, Any], runner: Any = None, opener: Any = None, **_: Any) -> str:
    """Everything below a folder in one answer: entries, counts by type, and files with identical content."""
    roots = folders(config)
    if reason := problem("folder_scan", args, roots, runner=runner):
        return _error(reason)
    top = inside(args.get("path"), [*roots, attachments(), image_attachments()])
    import unicodedata
    local, local_paths = {}, {}
    entries: list[dict[str, Any]] = []
    by_type: dict[str, int] = {}
    by_size: dict[int, list[Path]] = {}
    total_files = total_folders = total_bytes = 0
    unreadable_folders = []
    unreadable_folders_omitted = unreadable_files = 0
    def onerror(exc):
        nonlocal unreadable_folders_omitted
        if len(unreadable_folders) >= 100:
            unreadable_folders_omitted += 1
            return
        try:
            relative = str(Path(exc.filename).relative_to(top))
        except (TypeError, ValueError):
            relative = "."
        unreadable_folders.append({"path": relative, "error": type(exc).__name__, "errno": exc.errno})
    for current, names, files_here in os.walk(top, onerror=onerror):
        names[:] = sorted(name for name in names if not name.startswith("."))
        key = unicodedata.normalize('NFC', str(Path(current).relative_to(top)))
        local_paths[key] = Path(current)
        local[key] = {'dirs': {unicodedata.normalize('NFC', n) for n in names}, 'files': {}}
        for name in sorted(files_here):
            if name.startswith("."):
                continue
            path = Path(current) / name
            try:
                stat = path.stat()
            except OSError:
                unreadable_files += 1
                continue
            local[key]['files'][unicodedata.normalize('NFC', name)] = {
                'size': stat.st_size, 'modified': int(stat.st_mtime), 'path': path}
    nas_check, not_local = _compare_nas(config, top, local, local_paths, opener)
    for folder in local.values():
        total_folders += len(folder['dirs'])
        for info in folder['files'].values():
            path, size = info['path'], info['size']
            total_files += 1
            total_bytes += size
            kind = path.suffix.lower() or "(없음)"
            by_type[kind] = by_type.get(kind, 0) + 1
            by_size.setdefault(size, []).append(path)
            if len(entries) < SCAN_LIMIT:
                entries.append({'path': str(path.relative_to(top)), 'size': size, 'modified': info['modified']})
    result: dict[str, Any] = {"ok": True, "path": str(top), "files": total_files, "folders": total_folders,
                              "bytes": total_bytes, "by_type": dict(sorted(by_type.items(), key=lambda item: -item[1])),
                              "entries": entries, "truncated": total_files > len(entries)}
    if args.get("duplicates") is True:
        groups, hashed, skipped = [], 0, 0
        for size, paths in by_size.items():
            if len(paths) < 2 or size == 0:
                continue
            if size > HASH_FILE_LIMIT or hashed + size * len(paths) > HASH_TOTAL_LIMIT:
                skipped += len(paths)
                continue
            digests: dict[str, list[str]] = {}
            for path in paths:
                try:
                    digest = hashlib.sha256()
                    with open(path, "rb") as handle:
                        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                            digest.update(chunk)
                    digests.setdefault(digest.hexdigest(), []).append(str(path.relative_to(top)))
                    hashed += size
                except OSError:
                    skipped += 1
                    unreadable_files += 1
            groups.extend({"size": size, "files": sorted(same)} for same in digests.values() if len(same) > 1)
        result["duplicates"] = sorted(groups, key=lambda group: -group["size"])[:200]
        result["duplicates_not_compared"] = skipped
    result.update(unreadable_folders=unreadable_folders,
                  unreadable_folders_omitted=unreadable_folders_omitted, unreadable_files=unreadable_files,
                  complete=not (unreadable_folders or unreadable_folders_omitted or unreadable_files
                                or result['truncated'] or result.get('duplicates_not_compared', 0)))
    result.update(nas_check=nas_check, not_local=not_local[:NAS_FILES_LIMIT],
                  not_local_omitted=max(0, len(not_local) - NAS_FILES_LIMIT))
    if args.get('duplicates') is True:
        result['duplicates_not_local'] = len(not_local)
    # Unconfigured profiles retain their local-only contract. Explicitly disabled
    # or failed NAS checks on configured profiles cannot claim completeness.
    if config.get('synology') is not None and (nas_check['status'] != 'compared'
            or nas_check.get('mismatched_folders') or nas_check.get('mismatched_folders_omitted')):
        result['complete'] = False
    return json.dumps(result, ensure_ascii=False)


def file_copy(config: Any, args: dict[str, Any], **_: Any) -> str:
    roots = folders(config)
    if reason := problem("file_copy", args, roots):
        return _error(reason)
    source, target = inside(args.get("from"), [*roots, attachments(), image_attachments()]), inside(args.get("to"), roots)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(source, "rb") as reader, open(target, "xb") as writer:  # "x": never over an existing file
            shutil.copyfileobj(reader, writer)
        shutil.copystat(source, target)
    except OSError as exc:
        return _error(f"could not copy: {type(exc).__name__}")
    return json.dumps({"ok": True, "copied": {"from": str(source), "to": str(target)}}, ensure_ascii=False)


def _work(job: dict[str, Any], target: Path, *, replace: bool, runner: Any = None, timeout: int = 120) -> str:
    """Run the worker into a temporary file beside ``target`` and move it into place on success."""
    if not DOC_PYTHON.exists():
        return _error("the document editing environment (~/.local/share/hermes-docs) is missing; "
                      "it has to be rebuilt in the Paseo hermes-config project")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return _error(f"could not create the destination folder: {type(exc).__name__}")
    temporary = target.with_name(f".~{secrets.token_hex(6)}{target.suffix}")
    try:
        result = (runner or subprocess.run)(
            [str(DOC_PYTHON), str(Path(__file__).with_name("doc_worker.py"))],
            input=json.dumps({**job, "output": str(temporary)}, ensure_ascii=False),
            capture_output=True, text=True, timeout=timeout,
        )
        try:
            answer = json.loads(result.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            answer = {"ok": False, "error": "the document worker gave no result"}
        if result.returncode != 0 or answer.get("ok") is not True or not temporary.is_file() or temporary.stat().st_size == 0:
            return _error(str(answer.get("error") or "the document was not written"))
        if not replace and target.exists():
            return _error(f"{target} already exists (nothing is overwritten)")
        if replace:
            os.replace(temporary, target)
        else:
            os.link(temporary, target)  # atomic creation: a concurrent writer cannot be overwritten
        return json.dumps({"ok": True, "saved": str(target), "done": answer.get("done", [])}, ensure_ascii=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return _error(f"could not edit the document: {type(exc).__name__}")
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def doc_edit(config: Any, args: dict[str, Any], runner: Any = None, **_: Any) -> str:
    roots = folders(config)
    if reason := problem("doc_edit", args, roots):
        return _error(reason)
    path = inside(args.get("path"), roots)
    create = args.get("create") is True
    target = inside(args.get("save_as"), roots) if args.get("save_as") else path
    job = {"kind": DOC_KINDS[path.suffix.lower()], "source": None if create else str(path),
           "operations": args["operations"]}
    return _work(job, target, replace=target == path and not create, runner=runner)


def _fill_job(kind: str, args: dict[str, Any], roots: list[Path]) -> dict[str, Any]:
    readable = [*roots, attachments(), image_attachments()]
    data = inside(args.get("data"), readable) if args.get("data") is not None else None
    return {"kind": kind, "template": str(inside(args.get("template"), readable)), "data": str(data) if data else None,
            "columns": args.get("columns"), "start": args.get("start"), "limit": args.get("limit"),
            "name_pattern": args.get("name_pattern"), "max_rows": MAX_FILL_ROWS}


def doc_fill(config: Any, args: dict[str, Any], runner: Any = None, **_: Any) -> str:
    roots = folders(config)
    if not roots:
        return _error("No folders are configured for this profile")
    if reason := problem("doc_fill", args, roots):
        return _error(reason)
    if args["action"] == "inspect":
        return _ask(_fill_job("fill_inspect", args, roots), runner=runner)
    if not DOC_PYTHON.exists():
        return _error("the document editing environment (~/.local/share/hermes-docs) is missing; "
                      "it has to be rebuilt in the Paseo hermes-config project")
    out = inside(args.get("output_dir"), roots)
    made, created = not out.exists(), []
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        staging = out.parent / f".~fill-{secrets.token_hex(6)}"
        staging.mkdir()
    except OSError as exc:
        return _error(f"could not prepare the output folder: {type(exc).__name__}")
    try:
        result = (runner or subprocess.run)(
            [str(DOC_PYTHON), str(Path(__file__).with_name("doc_worker.py"))],
            input=json.dumps({**_fill_job("fill", args, roots), "staging": str(staging)}, ensure_ascii=False),
            capture_output=True, text=True, timeout=300)
        try:
            answer = json.loads(result.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            answer = {"ok": False, "error": "the document worker gave no result"}
        written = answer.get("files") if isinstance(answer.get("files"), list) else []
        if (result.returncode != 0 or answer.get("ok") is not True or not written
                or any(not (staging / item["name"]).is_file() or Path(item["name"]).name != item["name"] for item in written)):
            return _error(str(answer.get("error") or "the files were not written"))
        out.mkdir(exist_ok=True)
        for item in written:
            os.link(staging / item["name"], out / item["name"])  # atomic creation: an existing file is never replaced
            created.append(out / item["name"])
    except (OSError, subprocess.SubprocessError, KeyError, TypeError) as exc:
        for path in created:  # all or nothing
            path.unlink(missing_ok=True)
        if made:
            try:
                out.rmdir()
            except OSError:
                pass
        reason = "a file with that name already exists (nothing is overwritten)" if isinstance(exc, FileExistsError) else type(exc).__name__
        return _error(f"could not fill the template: {reason}")
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    files = [{"file": str(out / item["name"]), "row": item["row"], "missing": item["missing"]} for item in written]
    return json.dumps({"ok": True, "output_dir": str(out), "created": [f["file"] for f in files], "files": files,
                       "total": len(files), "with_missing": sum(1 for f in files if f["missing"]),
                       "unmapped_tokens": answer.get("unmapped_tokens", []), "mapping": answer.get("mapping", {}),
                       "rows": answer.get("rows"), "start": answer.get("start"),
                       "note": "빈 값은 문서에 «누락: 토큰»으로 보이게 표시했다. 보내거나 서명하지 않았다."}, ensure_ascii=False)


def pdf_pages(config: Any, args: dict[str, Any], runner: Any = None, **_: Any) -> str:
    roots = folders(config)
    if reason := problem("pdf_pages", args, roots):
        return _error(reason)
    sources = [{"path": str(inside(item.get("path"), [*roots, attachments(), image_attachments()])),
                "pages": item.get("pages"), "rotate": item.get("rotate")} for item in args["sources"]]
    return _work({"kind": "pdf", "sources": sources}, inside(args.get("to"), roots), replace=False, runner=runner)


def doc_convert(config: Any, args: dict[str, Any], runner: Any = None, **_: Any) -> str:
    if not available(config, "doc_convert"):
        return _error("doc_convert is disabled for this profile")
    roots = folders(config)
    if not roots:
        return _error("No folders are configured for this profile")
    if reason := problem("doc_convert", args, roots):
        return _error(reason)
    source = inside(args.get("from"), [*roots, attachments(), image_attachments()])
    target = inside(args.get("to"), roots)
    return _work({"kind": "convert", "source": str(source), "format": target.suffix.lower()[1:],
                  **{key: args[key] for key in ("fit", "widen_columns") if key in args}},
                 target, replace=False, runner=runner, timeout=300)


_PATH = {"type": "string", "description": "Absolute path inside the readable folders."}
SCHEMAS = {
    "doc_convert": {
        "name": "doc_convert",
        "description": "Convert only PDF to DOCX, DOCX to PDF, or XLSX to PDF after the user approves in chat. Source: configured folders or chat attachments; destination: a new file in configured folders. Never overwrite. Max 100 MB, PDF max 300 pages; no encrypted PDFs. Fully scanned PDFs: local Korean/English OCR up to 20 pages; OCR may have typos and does not reconstruct tables. Mixed PDFs are not OCRed. Inspect the result with doc_read/pdf_render and report layout/font/page-break limitations.",
        "parameters": {"type": "object", "properties": {"from": _PATH, "to": _PATH,
            "fit": {"type": "string", "enum": ["width", "sheet", "none"], "description": "XLSX only. Default width: one page wide; sheet: one page per sheet; none: original print settings."},
            "widen_columns": {"type": "boolean", "description": "XLSX only. Default false; true widens columns approximately to fit text without changing the source."}}, "required": ["from", "to"]},
    },
    "file_location": {
        "name": "file_location",
        "description": "Return configured or automatically queried Synology folder links for 1 to 10 existing files or folders. Batch multiple paths in one call. If file_url is present, write [filename](file_url) plus [folder](url), using returned URLs unchanged. 주소는 받은 그대로 쓴다. #drive= 뒤는 앱이 로컬 파일을 찾는 표시이므로 지우거나 고치지 않는다. Otherwise keep the folder link and filename and say the file link could not be retrieved. If exact is false, link linked_folder and include below. For unlinked, say the link could not be retrieved and give only the relative folder path; follow existing diagnosis rules for repeated unexpected failures. Never link Mac paths or invent URLs. No Slack upload; MEDIA only when asked to show a photo. No approval; usable in background tasks.",
        "parameters": {"type": "object", "properties": {"paths": {
            "type": "array", "minItems": 1, "maxItems": 10, "items": _PATH}}, "required": ["paths"]},
    },
    "file_link": {
        "name": "file_link",
        "description": ("Upload readable files into this Slack thread and return confirmed shared permalinks for [filename](url). Do not attach the same file again via MEDIA. "
                        "Call file_link in the current turn every time you provide a file link, including repeat requests. Never reuse URLs from chat history; copy the returned url exactly without changing any character. Only in Slack chat, without approval; 1 to 10 files, at most 50 MB each. "
                        "Reuse identical content and filename only when shared in this channel. Links expire 30 days after the last request; call file_link again when the user reports a dead link. Use returned URLs only. Only when sharing confirmation is delayed, call once more with the same path; do not repeat other failed or uncertain uploads."),
        "parameters": {"type": "object", "properties": {"paths": {
            "type": "array", "minItems": 1, "maxItems": 10, "items": _PATH}}, "required": ["paths"]},
    },
    "file_list": {
        "name": "file_list",
        "description": "List one folder inside the readable folders (one level). Without a path, name the readable folders. complete=false means partial results; report unreadable entries instead of assuming they are empty.",
        "parameters": {"type": "object", "properties": {"path": _PATH}, "required": []},
    },
    "file_move": {
        "name": "file_move",
        "description": ("Move or rename files and folders inside the readable folders, after the user approves. "
                        "Missing destination folders are created. Nothing is overwritten."),
        "parameters": {"type": "object", "properties": {"moves": {
            "type": "array", "minItems": 1, "maxItems": MAX_MOVES,
            "items": {"type": "object", "properties": {"from": _PATH, "to": _PATH}, "required": ["from", "to"]},
        }}, "required": ["moves"]},
    },
    "folder_create": {
        "name": "folder_create",
        "description": "Create an empty folder inside the readable folders, after the user approves.",
        "parameters": {"type": "object", "properties": {"path": _PATH}, "required": ["path"]},
    },
    "file_trash": {
        "name": "file_trash",
        "description": "Move one file or folder inside the readable folders to the macOS Trash, after the user approves.",
        "parameters": {"type": "object", "properties": {"path": _PATH}, "required": ["path"]},
    },
    "file_copy": {
        "name": "file_copy",
        "description": "For chat images use the original filename supplied in the attachment notice as the destination basename; if unavailable ask for a name instead of inventing one. Copy one file to a new path inside the readable folders, after the user approves. Nothing is overwritten.",
        "parameters": {"type": "object", "properties": {"from": _PATH, "to": _PATH}, "required": ["from", "to"]},
    },
    "doc_edit": {
        "name": "doc_edit",
        "description": (
            "Edit an Excel (.xlsx) or Word (.docx) file inside the readable folders, after the user approves. "
            "Without save_as the original is overwritten; with save_as the result goes to a new file and the original "
            "stays. create: true makes a new file at path. Excel operations: "
            "{op:'set_cells', sheet?, cells:{'A1': value}}, {op:'append_rows', sheet?, rows:[[...]]}, "
            "{op:'insert_rows', sheet?, start, count}, {op:'delete_rows', sheet?, start, count}, "
            "{op:'replace_text', find, replace, sheet?}, {op:'add_sheet', name}, {op:'rename_sheet', sheet, name}, "
            "{op:'delete_sheet', sheet}. A value starting with '=' is a formula. Word operations: "
            "{op:'replace_text', find, replace}, {op:'set_paragraph', index, text}, {op:'insert_paragraph', index, text, style?} "
            "(before index), {op:'delete_paragraph', index}, {op:'set_table_cell', table, row, col, text}, "
            "{op:'append_paragraphs', paragraphs:[{text, style?}]}, {op:'append_table', rows:[[...]]}. Indexes come from "
            "doc_read and shift after an insert or delete: put such operations last-to-first or re-read. Charts, images and "
            "pivot tables in an Excel file may not survive a save: use save_as for such a workbook. Read the file first with doc_read."),
        "parameters": {"type": "object", "properties": {
            "path": _PATH,
            "operations": {"type": "array", "minItems": 1, "maxItems": MAX_OPERATIONS, "items": {"type": "object"}},
            "save_as": {"type": "string", "description": "New path of the same type; the original is left as it is."},
            "create": {"type": "boolean", "description": "Make a new file at path instead of editing one."},
        }, "required": ["path", "operations"]},
    },
    "doc_fill": {
        "name": "doc_fill",
        "description": (
            "Mail merge: fill ONE Word (.docx) or Excel (.xlsx) template whose varying parts are {{token}} with each row of "
            "a data table (.xlsx first sheet or .csv, header row = column names) and make one new file per row. Use for the "
            "same form/letter to many people or organisations; use doc_edit for one document. action 'inspect' (no approval) "
            "lists the template tokens, data columns, token→column mapping (same name, case-insensitive; override with "
            "columns {token: column}), unmapped_tokens, unused_columns, blank_values, row count and sample file names: show "
            "it to the user before fill. To make a template, first put {{token}} into a copy with doc_edit save_as. action "
            f"'fill' asks the user once for the whole call, in chat only, and writes into output_dir, which must be a new or "
            f"empty folder (nothing is overwritten); at most {MAX_FILL_ROWS} rows per call, choose with start (1-based data "
            "row) and limit. name_pattern like '공문_{기관명}' names files from columns. A missing or blank value is written "
            "into the file as «누락: token» and listed per file in missing: report every one. Dates become YYYY-MM-DD and "
            "other values go in as stored. It only makes files; it never sends, posts or signs anything."),
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": ["inspect", "fill"]},
            "template": {"type": "string", "description": "Absolute path of the .docx/.xlsx template with {{token}}s."},
            "data": {"type": "string", "description": "Absolute path of the .xlsx/.csv data table (required for fill)."},
            "output_dir": {"type": "string", "description": "fill: a new or empty folder inside the readable folders."},
            "columns": {"type": "object", "additionalProperties": {"type": "string"},
                        "description": "Optional {token: column} where the names differ."},
            "name_pattern": {"type": "string", "description": "Optional file name like '공문_{기관명}'; default <template>_<row>."},
            "start": {"type": "integer", "minimum": 1, "description": "First data row to use (default 1)."},
            "limit": {"type": "integer", "minimum": 1, "maximum": MAX_FILL_ROWS, "description": "Rows to use from start."},
        }, "required": ["action", "template"]},
    },
    "pdf_pages": {
        "name": "pdf_pages",
        "description": ("Build a new PDF from pages of existing PDFs inside the readable folders (merge, extract, "
                        "reorder, rotate), after the user approves. pages is like '1-3,5'; omitted means all."),
        "parameters": {"type": "object", "properties": {
            "sources": {"type": "array", "minItems": 1, "maxItems": MAX_PDF_SOURCES, "items": {
                "type": "object", "properties": {"path": _PATH, "pages": {"type": "string"},
                                                 "rotate": {"type": "integer", "enum": [0, 90, 180, 270]}},
                "required": ["path"]}},
            "to": {"type": "string", "description": "New .pdf path inside the readable folders."},
        }, "required": ["sources", "to"]},
    },
    "doc_read": {
        "name": "doc_read",
        "description": ("Read PDF (.pdf), Excel (.xlsx), Word (.docx), or Hangul (.hwp/.hwpx, read-only) documents. PDF returns text by page, at most 5 pages per call (pages='1-3', default first 5); next_page continues. Empty/scanned pages need pdf_render then vision_analyze; embedded pictures are not read. Legacy .doc/.xls and PowerPoint use read_file; photos use vision_analyze. Hangul returns numbered paragraphs including table text, 200 from offset; encrypted/distribution documents are rejected. For Excel/Word: for Excel the sheets and the cells "
                        "of one sheet by address (formulas shown as formulas; default A1 to 30 columns x 200 rows, or pass "
                        "range like 'A1:H80', at most 50 columns x 500 rows); for Word the paragraphs with index and style "
                        "(200 from offset) and the tables. Use before doc_edit."),
        "parameters": {"type": "object", "properties": {
            "path": _PATH, "sheet": {"type": "string"}, "range": {"type": "string"},
            "pages": {"type": "string", "description": "PDF: 1-based pages, e.g. '1-3,7', at most 5 per call."},
            "offset": {"type": "integer", "minimum": 0, "description": "Word/Hangul: first paragraph index to show."},
        }, "required": ["path"]},
    },
    "pdf_render": {
        "name": "pdf_render",
        "description": (f"Turn pages of a PDF into images (at most {MAX_RENDER_PAGES} per call, pages like '1-3,7', default '1') "
                        "so a scanned PDF can be seen: pass a returned image path to vision_analyze."),
        "parameters": {"type": "object", "properties": {"path": _PATH, "pages": {"type": "string"}}, "required": ["path"]},
    },
    "folder_scan": {
        "name": "folder_scan",
        "description": (f"List everything below a folder at once (up to {SCAN_LIMIT} files, with size and time), with counts by "
                        "file type. duplicates: true also groups files whose content is identical. complete=false means partial results: report unreadable_folders and unreadable_files, never assume unreadable folders are empty. complete also accounts for truncated entries and incomplete duplicate comparison. With Synology enabled, compare NAS metadata without downloading content. On mismatch local counts are partial: use nas_check totals and not_local to describe the missing items; failed/skipped means not verified against NAS."),
        "parameters": {"type": "object", "properties": {"path": _PATH, "duplicates": {"type": "boolean"}}, "required": ["path"]},
    },
}
HANDLERS = {"file_location": file_location, "file_link": file_link, "file_list": file_list, "file_move": file_move, "folder_create": folder_create, "file_trash": file_trash,
            "file_copy": file_copy, "doc_edit": doc_edit, "pdf_pages": pdf_pages, "doc_convert": doc_convert, "doc_fill": doc_fill,
            "doc_read": doc_read, "pdf_render": pdf_render, "folder_scan": folder_scan}
