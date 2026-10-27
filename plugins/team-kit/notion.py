"""Notion for a profile through an integration token: read freely, change in an identified chat.

The token (NOTION_API_KEY in the profile's .env) reaches only the pages its connection was given
in Notion, so that list is the scope. Reading: search, databases, schema, query, page, users.
Changes need a person in chat; configured create/update/append/edit can skip approval with rollback.
Deleting means Notion's trash.

settings.notion.writable narrows changes further: a list of database or page ids (or links). A
change is allowed only on one of them or on something below one. Notion grants a connection the
same capabilities on every page it can reach, so this narrowing lives here, not in Notion.
"""
from __future__ import annotations

import hashlib
import copy
import fcntl
import time
import threading
import types
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo
import importlib.util
import os
import stat
import uuid
import json
import mimetypes
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

API = "https://api.notion.com/v1/"
VERSION = "2022-06-28"
UPLOAD_VERSION = "2026-03-11"
_spec = importlib.util.spec_from_file_location("team_kit_notion_files", Path(__file__).with_name("files.py"))
_files = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(_files)
ATTACH_BLOCKS = frozenset({'image', 'file'})
IMAGE_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
              ".gif": "image/gif", ".webp": "image/webp", ".heic": "image/heic",
              ".tif": "image/tiff", ".tiff": "image/tiff"}
READ = frozenset({"search", "databases", "schema", "query", "page", "users"})
SINGLE_WRITE = frozenset({"create", "update", "append", "edit", "trash"})
WRITE = SINGLE_WRITE | {"batch", "rollback"}
MAX_ROWS = 50
MAX_BLOCKS = 100
MAX_PARAGRAPHS = 50
_ID = re.compile(r"[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}")
_SIMPLE = ("title", "rich_text", "number", "select", "status", "multi_select", "date", "checkbox", "people",
           "url", "email", "phone_number")


class NotionError(Exception):
    unknown = False
    code = None


class UnknownChange(NotionError):
    unknown = True


_API = None


def api_module():
    global _API
    if _API is None:
        spec = importlib.util.spec_from_file_location('team_kit_notion_api', Path(__file__).with_name('notion_api.py'))
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        _API = module
    return _API


def api_context():
    return types.SimpleNamespace(**globals())


def needs_snapshot(config, home, args):
    if args.get('action') == 'api': return api_module().is_write(args)
    if args.get('action') == 'rollback': return config.get('full_api') is True
    return args.get('action') in SINGLE_WRITE | {'batch'}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward the integration credential to a redirect destination.


def enabled(config: Any) -> bool:
    return isinstance(config, dict) and config.get("enabled") is True


def token(home: Path) -> str:
    try:
        for line in (Path(home) / ".env").read_text(encoding="utf-8").splitlines():
            match = re.match(r"\s*(?:export\s+)?NOTION_API_KEY\s*=\s*(.*)", line)
            if match:
                return match.group(1).strip().strip("\"'")
    except OSError:
        pass
    return ""


def object_id(value: Any) -> str:
    """The id inside a bare id or a Notion link (the last one in the path, not a view id)."""
    text = str(value or "").split("?")[0]
    found = _ID.findall(text)
    if not found:
        raise NotionError("needs a Notion id or link")
    return found[-1].replace("-", "").lower()


def _http_open(call, timeout):
    return urllib.request.build_opener(_NoRedirect()).open(call, timeout=timeout)


def request(home: Path, method: str, path: str, body: Any = None, *, opener: Any = None, timeout: int = 20, content_type: str | None = None, version: str | None = None) -> dict:
    secret = token(home)
    if not secret:
        raise NotionError("the Notion token is missing from this profile")
    call = urllib.request.Request(
        API + path, method=method, data=body if isinstance(body, bytes) else (None if body is None else json.dumps(body).encode("utf-8")),
        headers={"Authorization": "Bearer " + secret, "Notion-Version": version or (UPLOAD_VERSION if path.startswith("file_uploads") else VERSION),
                 "Content-Type": content_type or "application/json"})
    try:
        with (opener or _http_open)(call, timeout=timeout) as response:
            if getattr(response, "status", None) == 204: return {"object": "deleted", "deleted": True}
            result = json.load(response)
            if not isinstance(result, dict):
                raise ValueError("expected JSON object")
            return result
    except urllib.error.HTTPError as exc:
        try:
            detail = json.load(exc).get("message", "")
        except Exception:
            detail = ""
        error = NotionError(f"Notion refused ({exc.code}): {str(detail)[:300]}")
        error.code = exc.code
        error.unknown = exc.code >= 500 or exc.code == 408
        raise error from None
    except (ValueError, urllib.error.URLError, TimeoutError, OSError) as exc:
        # A timed-out change may or may not have landed: the caller must not repeat it blindly.
        raise UnknownChange(f"Notion did not answer ({type(exc).__name__}); whether a change landed is unknown") from None


def _plain(parts: Any) -> str:
    return "".join(str(part.get("plain_text", "")) for part in parts or [] if isinstance(part, dict))


def value(prop: dict) -> Any:
    """One property as a plain value."""
    kind = prop.get("type")
    data = prop.get(kind)
    if kind in ("title", "rich_text"):
        return _plain(data)
    if kind in ("select", "status"):
        return data.get("name") if data else None
    if kind == "multi_select":
        return [item.get("name") for item in data or []]
    if kind == "date":
        return None if not data else (data["start"] if not data.get("end") else {"start": data["start"], "end": data["end"]})
    if kind == "people":
        return [person.get("name") or person.get("id") for person in data or []]
    if kind in ("created_by", "last_edited_by"):
        return (data or {}).get("name") or (data or {}).get("id")
    if kind == "formula":
        return (data or {}).get((data or {}).get("type"))
    if kind == "relation":
        return [item.get("id") for item in data or []]
    if kind == "files":
        return [item.get("name") for item in data or []]
    return data


def row(page: dict) -> dict:
    return {"id": page.get("id"), "url": page.get("url"), "trashed": bool(page.get("archived") or page.get("in_trash")),
            "last_edited": page.get("last_edited_time"),
            "properties": {name: value(prop) for name, prop in (page.get("properties") or {}).items()}}


def _title_of(item: dict) -> str:
    if item.get("object") == "database":
        return _plain(item.get("title"))
    for prop in (item.get("properties") or {}).values():
        if prop.get("type") == "title":
            return _plain(prop.get("title"))
    return ""


def schema_of(database: dict) -> dict:
    fields = {}
    for name, prop in (database.get("properties") or {}).items():
        kind = prop.get("type")
        field: dict[str, Any] = {"type": kind, "writable": kind in _SIMPLE}
        if kind in ("select", "multi_select", "status"):
            field["options"] = [option.get("name") for option in (prop.get(kind) or {}).get("options", [])]
        fields[name] = field
    return fields


def _text(text: Any) -> list[dict]:
    text = "" if text is None else str(text)
    if len(text) > 200000:
        raise NotionError(f"text has {len(text)} characters; rich_text allows 100 objects of 2000 characters")
    return [{"type": "text", "text": {"content": text[i:i + 2000]}} for i in range(0, len(text), 2000)] or []


def encode(name: str, kind: str, given: Any, people: dict[str, str] | None = None) -> dict:
    """A plain value as the API's property payload. Raises NotionError on a value that does not fit."""
    if kind in ("title", "rich_text"):
        if isinstance(given, dict):
            raise NotionError(f"{name}: append/replace needs an existing text property (update)")
        return {kind: _text(given)}
    if kind == "number":
        if given is None:
            return {"number": None}
        if isinstance(given, bool) or not isinstance(given, (int, float)):
            raise NotionError(f"{name}: needs a number")
        return {"number": given}
    if kind in ("select", "status"):
        return {kind: None if given in (None, "") else {"name": str(given)}}
    if kind == "multi_select":
        if not isinstance(given, list):
            raise NotionError(f"{name}: needs a list of option names")
        return {"multi_select": [{"name": str(item)} for item in given]}
    if kind == "date":
        if given in (None, ""):
            return {"date": None}
        if isinstance(given, dict) and given.get("start"):
            return {"date": {"start": str(given["start"]), "end": str(given["end"]) if given.get("end") else None}}
        if isinstance(given, str):
            return {"date": {"start": given}}
        raise NotionError(f"{name}: needs a date like 2026-10-08 or {{start, end}}")
    if kind == "checkbox":
        if not isinstance(given, bool):
            raise NotionError(f"{name}: needs true or false")
        return {"checkbox": given}
    if kind == "people":
        if not isinstance(given, list):
            raise NotionError(f"{name}: needs a list of people (names or ids)")
        ids = []
        for person in given:
            person = str(person)
            if _ID.fullmatch(person):
                ids.append(person)
            elif people and person in people:
                ids.append(people[person])
            else:
                raise NotionError(f"{name}: no workspace member named {person!r} (use action 'users')")
        return {"people": [{"object": "user", "id": person} for person in ids]}
    if kind in ("url", "email", "phone_number"):
        return {kind: None if given in (None, "") else str(given)}
    raise NotionError(f"{name}: a {kind} property cannot be set here")


def _people(home: Path, opener: Any) -> dict[str, str]:
    found: dict[str, str] = {}
    cursor = None
    for _ in range(5):
        page = request(home, "GET", "users?page_size=100" + (f"&start_cursor={cursor}" if cursor else ""), opener=opener)
        for user in page.get("results", []):
            if user.get("type") == "person" and user.get("name"):
                found[user["name"]] = user["id"]
        cursor = page.get("next_cursor")
        if not page.get("has_more"):
            break
    return found


def _properties(home: Path, database: dict, given: Any, opener: Any) -> dict:
    if not isinstance(given, dict) or not given:
        raise NotionError("properties needs an object of property name to value")
    fields = database.get("properties") or {}
    people = None
    payload = {}
    for name, item in given.items():
        if name not in fields:
            raise NotionError(f"no property named {name!r}; properties: {', '.join(fields)}")
        kind = fields[name].get("type")
        if kind == "people" and people is None and any(not _ID.fullmatch(str(p)) for p in item or []):
            people = _people(home, opener)
        if kind in ("select", "status", "multi_select"):
            options = {option.get("name") for option in (fields[name].get(kind) or {}).get("options", [])}
            wanted = item if isinstance(item, list) else [item]
            unknown = [str(option) for option in wanted if option not in (None, "") and option not in options]
            if unknown:  # Schema changes cannot be rolled back by this tool.
                raise NotionError(f"{name}: {unknown[0]!r} is not an option ({', '.join(sorted(map(str, options)))})")
        payload[name] = encode(name, kind, item, people)
    return payload


TEXT_BLOCKS = frozenset({'paragraph', 'heading_1', 'heading_2', 'heading_3',
                         'bulleted_list_item', 'numbered_list_item', 'to_do', 'quote', 'callout', 'toggle'})
PARENT_BLOCKS = TEXT_BLOCKS - {'heading_1', 'heading_2', 'heading_3'}
COLORS = frozenset({'default'} | {c + suffix for c in
    ('gray', 'brown', 'orange', 'yellow', 'green', 'blue', 'purple', 'pink', 'red')
    for suffix in ('', '_background')})
ANNOTATIONS = {'bold': 'bold', 'italic': 'italic', 'underline': 'underline',
               'strike': 'strikethrough', 'code': 'code'}


def block_color(color):
    if not isinstance(color, str) or color not in COLORS:
        raise NotionError('잘못된 Notion 색: ' + str(color))
    return color


def styled_text(given):
    if isinstance(given, str): return _text(given)
    if not isinstance(given, list): raise NotionError('text needs a string or a list of spans')
    result = []
    for span in given:
        if not isinstance(span, dict) or set(span) - ({'text', 'color', 'link'} | set(ANNOTATIONS)):
            raise NotionError('unknown span key or invalid span')
        if not isinstance(span.get('text'), str): raise NotionError('span.text needs a string')
        annotations = {}
        for key, api_key in ANNOTATIONS.items():
            if key in span:
                if type(span[key]) is not bool: raise NotionError(key + ' needs a boolean')
                annotations[api_key] = span[key]
        if 'color' in span: annotations['color'] = block_color(span['color'])
        link = span.get('link')
        if 'link' in span:
            if not isinstance(link, str) or len(link) > 2000:
                raise NotionError('link needs an http/https URL of up to 2000 characters')
            parsed = urllib.parse.urlsplit(link)
            if parsed.scheme not in ('http', 'https') or not parsed.netloc:
                raise NotionError('link needs an http/https URL')
        for part in _text(span['text']):
            if annotations: part['annotations'] = annotations.copy()
            if link: part['text']['link'] = {'url': link}
            result.append(part)
        check_rich(result)
    return result


def _emoji(given):
    # Accept one common Unicode emoji sequence without adding a runtime dependency.
    base = r'[\U0001F300-\U0001FAFF\u2600-\u27BF\u2300-\u23FF\u2B00-\u2BFF©®‼⁉™ℹ〰〽㊗㊙]'
    unit = base + r'\ufe0f?[\U0001F3FB-\U0001F3FF]?'
    pattern = '(?:' + unit + r'(?:\u200d' + unit + r')*|[\U0001F1E6-\U0001F1FF]{2}|[0-9#*]\ufe0f?\u20e3)'
    if not isinstance(given, str) or not re.fullmatch(pattern, given):
        raise NotionError('callout.icon needs one emoji')
    return {'type': 'emoji', 'emoji': given}


def _paragraphs(given: Any, uploads=None) -> list[dict]:
    if given is None: return []
    if not isinstance(given, list) or len(given) > MAX_PARAGRAPHS:
        raise NotionError(f'paragraphs needs a list of up to {MAX_PARAGRAPHS} blocks; 나눠 보내세요.')
    count = 0; images = 0

    def build(items, depth):
        nonlocal count, images
        if not isinstance(items, list): raise NotionError('children needs a list')
        if items and depth > 2: raise NotionError('children 깊이는 최상위 아래 2단계까지입니다.')
        blocks = []
        for item in items:
            count += 1
            if count > 100: raise NotionError('하위 포함 전체 블록은 100개까지입니다. 나눠 보내세요.')
            if isinstance(item, str): item = {'text': item}
            if not isinstance(item, dict): raise NotionError('block needs text or an object')
            kind = item.get('type', 'paragraph')
            if not isinstance(kind, str) or kind not in TEXT_BLOCKS | ATTACH_BLOCKS | {'divider'}:
                raise NotionError(f'unknown block type {kind!r}')
            keys = {'type'}
            if kind in TEXT_BLOCKS: keys |= {'text', 'color'}
            if kind in PARENT_BLOCKS: keys.add('children')
            if kind == 'to_do': keys.add('checked')
            if kind == 'callout': keys.add('icon')
            if kind in ATTACH_BLOCKS: keys |= {'path', 'caption'}
            if set(item) - keys: raise NotionError(f'unknown {kind} block keys: {sorted(set(item) - keys)}')
            if kind in ATTACH_BLOCKS:
                if depth: raise NotionError(f'{kind}는 최상위에만 둡니다.')
                if not isinstance(item.get('path'), str) or not Path(item['path']).is_absolute():
                    raise NotionError(f'{kind} needs an absolute path')
                if not isinstance(item.get('caption', ''), str) or len(item.get('caption', '')) > 2000:
                    raise NotionError(f'{kind} caption must be a string of up to 2000 characters')
                images += 1
                if images > 10: raise NotionError('사진·파일은 호출당 10개까지 첨부합니다.')
                content = {'type': 'file_upload', 'file_upload': {'id': (uploads or {}).get(item['path'], '')},
                           'caption': _text(item.get('caption'))}
            elif kind == 'divider': content = {}
            else:
                content = {'rich_text': styled_text(item.get('text', ''))}
                if 'color' in item: content['color'] = block_color(item['color'])
                if kind == 'to_do':
                    if type(item.get('checked', False)) is not bool: raise NotionError('checked needs a boolean')
                    content['checked'] = item.get('checked', False)
                if kind == 'callout': content['icon'] = _emoji(item.get('icon', '💡'))
                if 'children' in item: content['children'] = build(item['children'], depth + 1)
            blocks.append({'object': 'block', 'type': kind, kind: content})
        return blocks
    return build(given, 0)


def block_total(blocks):
    return sum(1 + block_total(b[b['type']].get('children', [])) for b in blocks)


def paragraph_lines(paragraphs, depth=0):
    lines = []
    for item in paragraphs:
        if isinstance(item, str): item = {'text': item}
        if item.get('type') in ATTACH_BLOCKS: continue
        text = item.get('text', '')
        if isinstance(text, list): text = ''.join(span['text'] for span in text)
        lines.extend('  ' * depth + line for line in normalized_lines(text))
        lines.extend(paragraph_lines(item.get('children', []), depth + 1))
    return lines


def _body(home: Path, page_id: str, opener: Any) -> dict:
    budget = 300; more = False; visited = set()

    def read(parent, depth):
        nonlocal budget, more
        if parent in visited: more = True; return []
        visited.add(parent)
        data = request(home, 'GET', f'blocks/{parent}/children?page_size={min(MAX_BLOCKS, budget)}', opener=opener)
        if not isinstance(data.get('results'), list): raise NotionError('본문 목록을 읽지 못했습니다.')
        more |= bool(data.get('has_more'))
        lines = []
        blocks = data['results'][:budget]
        if len(blocks) < len(data['results']): more = True
        budget -= len(blocks)  # Reserve fetched siblings before spending the shared budget on children.
        for block in blocks:
            kind = block.get('type')
            content = block.get(kind) if isinstance(block.get(kind), dict) else {}
            item = {'id': block.get('id'), 'type': kind, 'text': _plain(content.get('rich_text'))}
            if kind in ('child_database', 'child_page'): item['title'] = content.get('title')
            if kind in MEDIA_TYPES:
                item['caption'] = _plain(content.get('caption'))
                if content.get('name'): item['name'] = content['name']
            if 'rich_text' in content and has_format(content['rich_text']):
                spans = []
                for part in content['rich_text']:
                    span = {'text': _plain([part])}
                    annotations = part.get('annotations') or {}
                    for key, api_key in ANNOTATIONS.items():
                        if annotations.get(api_key): span[key] = True
                    if annotations.get('color', 'default') != 'default': span['color'] = annotations['color']
                    link = (part.get('text', {}).get('link') or {}).get('url')
                    if link: span['link'] = link
                    spans.append(span)
                item['spans'] = spans
            if kind == 'to_do': item['checked'] = content.get('checked', False)
            if content.get('color', 'default') != 'default': item['color'] = content['color']
            if block.get('has_children'):
                if depth < 2 and budget > 0 and block.get('id'):
                    item['children'] = read(block['id'], depth + 1)
                else: more = True
            lines.append(item)
        return lines
    return {'blocks': read(page_id, 0), 'more': more}


def writable(config: Any) -> set[str] | None:
    """The ids changes are confined to; None when settings.notion.writable is absent (no narrowing)."""
    if not isinstance(config, dict) or "writable" not in config:
        return None
    allowed = set()
    for item in config.get("writable") or []:
        try:
            allowed.add(object_id(item))
        except NotionError:
            continue
    return allowed


def _parent_id(parent: Any) -> tuple[str, str] | None:
    kind = (parent or {}).get("type")
    if kind in ("database_id", "page_id", "block_id") and parent.get(kind):
        return {"database_id": "databases", "page_id": "pages", "block_id": "blocks"}[kind], str(parent[kind]).replace("-", "").lower()
    return None


def outside(config: Any, home: Path, args: dict[str, Any], *, opener: Any = None) -> str | None:
    """Why this change falls outside settings.notion.writable; None when it may go ahead. The
    target's parents are followed upward until a writable id or the workspace is reached."""
    allowed = writable(config)
    if allowed is None:
        return None
    if not allowed:
        return "이 프로필은 Notion을 보기만 할 수 있다."
    action = args.get("action")
    if action == "create" and args.get("database_id"):
        kind, current = "databases", object_id(args["database_id"])
    else:
        kind, current = "pages", object_id(args.get("parent_page_id") if action == "create" else args.get("page_id"))
    refusal = "이 Notion 위치는 보기만 할 수 있다. 만들거나 고칠 수 있는 곳은 설정에 지정된 표와 그 하위뿐이다."
    for _ in range(10):
        if current in allowed:
            return None
        try:
            item = request(home, "GET", f"{kind}/{current}", opener=opener, timeout=8)
        except NotionError as exc:
            return f"쓰기 범위를 확인하지 못했다: {exc}"
        step = _parent_id(item.get("parent"))
        if step is None:
            return refusal
        kind, current = step
    return refusal


def problem(args: dict[str, Any], config=None, files_config=None) -> str | None:
    """Why this call cannot run as written; None when its shape is fine."""
    action = str(args.get("action") or "")
    if action in ('api', 'api_help'):
        if not isinstance(config, dict) or config.get('full_api') is not True:
            return '이 프로필은 확장 Notion API가 꺼져 있습니다.'
        try:
            if action == 'api': api_module().route(api_context(), args)
        except (NotionError, KeyError, ValueError, TypeError) as exc: return str(exc)
        return None
    if action not in READ | WRITE:
        return f"notion needs action: one of {', '.join(sorted(READ | WRITE))}"
    try:
        if 'replace_all' in args and (type(args['replace_all']) is not bool or action != 'update'):
            return 'replace_all is a boolean for update only'
        if 'seen_last_edited' in args and (action not in ('update', 'append', 'edit', 'trash') or
                not isinstance(args['seen_last_edited'], str) or not args['seen_last_edited'].strip()):
            return 'seen_last_edited needs the last_edited string returned by page/query'
        if action == 'batch':
            if not isinstance(args.get('title'), str) or not args['title'].strip() or len(args['title']) > 200 or '\n' in args['title'] or '\r' in args['title']:
                return 'batch title needs a one-line description (1–200 characters)'
            ops = args.get('operations')
            if not isinstance(ops, list) or not 1 <= len(ops) <= 10:
                return 'batch operations needs 1–10 changes'
            for index, op in enumerate(ops, 1):
                if not isinstance(op, dict) or op.get('action') not in SINGLE_WRITE:
                    return f'{index}: batch permits create/update/append/edit/trash only'
                if reason := problem(op, config, files_config): return f'{index}: {reason}'
            if sum(block_total(_paragraphs(op.get('paragraphs'))) for op in ops) > 100: return '하위 포함 전체 블록은 호출당 100개까지입니다. 나눠 보내세요.'
            if sum(image_count(op) for op in ops) > 10: return '사진·파일은 batch 전체 호출당 10개까지 첨부합니다.'
            return None
        if action == 'rollback':
            if set(args) - {'action', 'key', '_approval_snapshot'} or ('key' in args and (not isinstance(args['key'], str) or not args['key'].strip())):
                return 'INVALID_ROLLBACK_ARGUMENTS'
            return None
        if action in ("schema", "query"):
            object_id(args.get("database_id"))
        if action in ("page", "update", "append", "edit", "trash"):
            object_id(args.get("page_id"))
        if action == "create":
            if bool(args.get("database_id")) == bool(args.get("parent_page_id")):
                return "create needs exactly one of database_id (a new row) or parent_page_id (a new sub-page)"
            object_id(args.get("database_id") or args.get("parent_page_id"))
            if args.get("database_id") and not isinstance(args.get("properties"), dict):
                return "create in a database needs properties"
            if args.get("parent_page_id") and not str(args.get("title") or "").strip():
                return "create under a page needs title"
        if action == "update" and not isinstance(args.get("properties"), dict):
            return "update needs properties"
        if action == "append" and not args.get("paragraphs"):
            return "append needs paragraphs"
        if 'after' in args:
            if action != 'append': return 'after is for append only'
            object_id(args['after'])
        if action == 'edit': validate_edits(args.get('blocks'))
        if action in ("create", "append"):
            _paragraphs(args.get("paragraphs"))
            image_files(args, config, files_config)
    except NotionError as exc:
        return f"{action}: {exc}"
    return None


def _shown(given: Any) -> str:
    parts = []
    for name, item in (given or {}).items():
        text = " ".join((item if isinstance(item, str) else json.dumps(item, ensure_ascii=False, default=str)).split())
        parts.append(f"{name}: {text[:80]}{'…' if len(text) > 80 else ''}")
    return "\n".join(parts[:15]) + (f"\n… 외 {len(parts) - 15}개" if len(parts) > 15 else "")


def _no_approval(config):
    value = config.get('no_approval', []) if isinstance(config, dict) else []
    if not isinstance(value, list) or any(not isinstance(name, str) or name not in {'create', 'update', 'append', 'edit'} for name in value):
        return set(), 'notion.no_approval 설정 오류: create·update·append·edit 목록만 허용하므로 승인을 유지한다.'
    return set(value), ''


def approval_required(config, args):
    allowed, _ = _no_approval(config)
    ops = args['operations'] if args['action'] == 'batch' else [args]
    return not (all(op['action'] in allowed for op in ops)
                and config.get('rollback_enabled', True) is not False and conversation() is not None)


def guard(config: Any, home: Path, *, tool_name: str, args: Any, chat: bool, opener: Any = None, files_config=None, call_id="") -> dict[str, str] | None:
    if tool_name != "notion" or not enabled(config):
        return None
    call = args if isinstance(args, dict) else {}
    if call.get('action') in ('api', 'api_help'):
        return api_module().guard(api_context(), config, home, call, chat, files_config, opener, call_id)
    if call.get('action') == 'rollback' and config.get('full_api') is True:
        if not chat or conversation() is None or config.get('rollback_enabled', True) is False:
            return {'action': 'block', 'message': '되돌리기는 활성화된 같은 사용자 대화에서 실행합니다.'}
        if api_module().is_api_rollback(api_context(), home):
            return api_module().guard(api_context(), config, home, call, chat, files_config, opener, call_id)
        with journal(home) as (data, save):
            entry = api_module().latest(data, conversation())
            plan = {'legacy_rollback': entry['key'] if entry else None}
            api_module().remember(api_context(), home, call, plan, call_id)
            if entry and any(o['action'] in ('create', 'append') for o in entry['operations']):
                return {'action': 'approve', 'message': '승인하면 최근 생성한 Notion 페이지·본문을 삭제해 되돌립니다: ' + entry['title'],
                        'rule_key': 'team-kit-notion-undo-delete:' + entry['key']}
    if call.get("action") in WRITE and writable(config) == set():
        return {"action": "block", "message": "이 프로필은 Notion을 보기만 할 수 있다."}
    if reason := problem(call, config, files_config):
        return {"action": "block", "message": reason}
    if call["action"] in WRITE:
        if not chat:
            return {"action": "block", "message": "Notion changes need the user's approval in chat."}
        if call['action'] == 'rollback':
            if config.get('rollback_enabled', True) is False:
                return {'action': 'block', 'message': 'Notion rollback is disabled.'}
            return None  # The handler selects the journal entry and checks scope before restoring.
        try:
            plans = preflight(config, home, call, files_config, opener)
            if call['action'] == 'batch':
                lines = [f"승인하면 Notion {len(plans)}건을 순서대로 변경합니다: {call['title']}"]
                for index, (op, plan) in enumerate(zip(call['operations'], plans), 1):
                    lines.append(f"{index}. " + preview(op, plan))
                message = '\n'.join(lines)
            else:
                message = '승인하면 ' + preview(call, plans[0])
            digest = hashlib.sha256(json.dumps(call, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:12]
            remember_snapshot(home, call, plans, call_id)
            if not approval_required(config, call): return None
            _, error = _no_approval(config)
            if error: message = error + '\n' + message
            return {'action': 'approve', 'message': message, 'rule_key': f"team-kit-notion:{call['action']}:{digest}"}
        except (NotionError, OSError, ValueError, TypeError) as exc:
            return {'action': 'block', 'message': str(exc)}
    return None


def handle(config: Any, home: Path, args: dict[str, Any], opener: Any = None, files_config=None, chat=None, require_snapshot=False, **_: Any) -> str:
    def error(message: str) -> str:
        return json.dumps({"ok": False, "error": message}, ensure_ascii=False)

    if not enabled(config):
        return error("Notion is not enabled for this profile")
    if args.get('action') in ('api', 'api_help'):
        try:
            return json.dumps(api_module().handle(api_context(), config, home, args, files_config, opener, chat, require_snapshot), ensure_ascii=False)
        except (NotionError, OSError, ValueError, TypeError, KeyError) as exc: return error(str(exc))
    if args.get("action") in WRITE and writable(config) == set():
        return error("이 프로필은 Notion을 보기만 할 수 있다.")
    if reason := problem(args, config, files_config):
        return error(reason)
    action = args["action"]
    if action in WRITE:
        if require_snapshot and action != 'rollback' and not args.get('_approval_snapshot'):
            return error('승인 시점의 값 확인 정보가 없습니다. 다시 읽고 승인받으세요.')
        if chat is False:
            return error("Notion changes need the user's approval in chat.")
        try:
            if action == 'rollback':
                return json.dumps(rollback(config, home, args, opener, chat=chat, require_snapshot=require_snapshot), ensure_ascii=False)
            return json.dumps(change(config, home, args, files_config, opener), ensure_ascii=False)
        except (NotionError, OSError, ValueError, TypeError, KeyError) as exc:
            return error(str(exc))
    try:
        if action in ("search", "databases"):
            body: dict[str, Any] = {"page_size": min(int(args.get("page_size") or 20), MAX_ROWS)}
            if action == "databases":
                body["filter"] = {"property": "object", "value": "database"}
            elif str(args.get("query") or "").strip():
                body["query"] = str(args["query"])
            if args.get("cursor"):
                body["start_cursor"] = str(args["cursor"])
            data = request(home, "POST", "search", body, opener=opener)
            items = [{"object": item.get("object"), "id": item.get("id"), "title": _title_of(item), "url": item.get("url")}
                     for item in data.get("results", [])]
            return json.dumps({"ok": True, "results": items, "next_cursor": data.get("next_cursor")}, ensure_ascii=False)
        if action == "users":
            return json.dumps({"ok": True, "people": _people(home, opener)}, ensure_ascii=False)
        if action == "schema":
            database = request(home, "GET", "databases/" + object_id(args["database_id"]), opener=opener)
            return json.dumps({"ok": True, "id": database.get("id"), "title": _title_of(database), "url": database.get("url"),
                               "properties": schema_of(database)}, ensure_ascii=False)
        if action == "query":
            body = {"page_size": min(int(args.get("page_size") or 20), MAX_ROWS)}
            for key in ("filter", "sorts"):
                if args.get(key):
                    body[key] = args[key]
            if args.get("cursor"):
                body["start_cursor"] = str(args["cursor"])
            data = request(home, "POST", f"databases/{object_id(args['database_id'])}/query", body, opener=opener)
            return json.dumps({"ok": True, "rows": [row(page) for page in data.get("results", [])],
                               "next_cursor": data.get("next_cursor")}, ensure_ascii=False)
        if action == "page":
            page_id = object_id(args["page_id"])
            page = request(home, "GET", "pages/" + page_id, opener=opener)
            return json.dumps({"ok": True, **row(page), **_body(home, page_id, opener)}, ensure_ascii=False)
    except NotionError as exc:
        return error(str(exc))
    except (TypeError, ValueError, KeyError) as exc:
        return error(f"{action}: unexpected data ({type(exc).__name__})")


def note(config: Any) -> str:
    if not enabled(config):
        return ""
    if writable(config) == set():
        return ("[Notion] 이 프로필은 Notion을 보기만 할 수 있다. 연결에 지정된 페이지에만 접근한다. "
                "읽기: databases(목록), schema(열과 선택지), query(행 조회), page(한 항목의 속성과 본문), search, users. "
                "assistant-work-sources를 열어 조회 범위와 근거를 확인한다.")
    if config.get('full_api') is True:
        return ('[Notion] 연결에 공유된 모든 페이지·표에서 생성은 즉시, 수정은 먼저 실행하고 후보고, 삭제만 도구 승인을 받는다. '
                'api_help로 공식 문서 API 46개와 입력 스키마를 찾고 api(operation, params, body)로 실행한다. '
                'api_help(operation=이름)는 경로·쿼리 인수와 본문 형식, api_help(schema_ref=$ref)는 참조 형식을 보여준다. '
                '페이지·데이터베이스·데이터 소스·모든 열 종류·보기·블록·마크다운·이동·댓글·파일 업로드를 지원한다. '
                '열은 retrieve-database로 data_sources를 찾은 뒤 retrieve-a-data-source/update-a-data-source를 쓴다. '
                'upload-file(file_path, part_number?, offset?, length?)은 PDF/PPTX/XLSX 등 파일을 첨부한다. '
                '기존 databases/schema/query/page/search/users/create/update/append/edit/batch/trash도 사용 가능하다. '
                '수정 전 원본을 읽고 요청한 부분만 바꾼다. report·verification·rollback_available·limitations를 확인해 바뀐 항목과 복원 가능 여부를 보고한다. '
                'rollback은 같은 대화의 최근 변경을 30분 안 복원하며 이후 24시간 안에는 확인을 받는다. 복원이 삭제를 수반하면 승인을 받는다. '
                '동시 수정은 덮어쓰지 않고, 미확인 쓰기·비동기 작업은 성공으로 보고하거나 반복하지 않는다. '
                'API에서 복원 불가한 원본 첨부·댓글/보기 삭제 등은 제한을 보고한다. 예약·배경 쓰기는 차단한다. '
                'Notion 자체의 연결 공유·권한·요금제 제한은 API 오류를 근거로 알린다. office-notion 스킬을 참고한다.')
    limited = ("" if writable(config) is None else
               " 만들거나 고칠 수 있는 곳은 설정에 지정된 표와 그 하위 항목뿐이고, 나머지는 보기만 한다. 범위 밖 변경은 도구가 거부하니 시도하지 말고 사용자에게 알린다.")
    allowed, config_error = _no_approval(config)
    approval_note = ('·'.join(name for name in ('create', 'update', 'append', 'edit') if name in allowed) +
                     '는 소유자가 시킨 대화에서 승인 없이 바로 실행하고 결과의 report와 되돌리기 가능 여부를 답변에 적는다. '
                     '대화 식별·되돌리기 기록이 불가능하면 승인을 유지한다. trash와 그 밖의 변경은 호출마다 승인을 묻는다. ')
    if not allowed:
        approval_note = '변경: create(새 행·새 하위 페이지), update(행 속성), append(본문 덧붙이기), edit(본문 글·체크·색), trash(휴지통)는 호출마다 사용자 승인을 묻는다. '
    approval_note += config_error
    return limited.strip() + (' ' if limited else '') + (
        '[Notion] 읽기: databases, schema(열·선택지), query(filter·sorts), page(속성·본문 id·서식·children), search, users. '
        '쓰기 전 schema와 page를 읽는다. create(새 항목), update(속성), append(본문 추가), edit(기존 글·체크·색), trash(휴지통). '
        '여러 변경은 batch(title, operations 1~10개). ' + approval_note +
        'paragraphs의 text는 문자열 또는 [{text,bold,italic,underline,strike,code,color,link}] 조각 목록이다. '
        '글자 크기는 heading_2·heading_3으로, callout(icon 이모지)·toggle·quote·divider·to_do와 color·children(하위 2단계)로 정리한다. '
        'edit(page_id, blocks=[{id,text?,checked?,color?}])는 20개까지. append(after=같은 페이지 최상위 블록 id)로 위치를 정한다. '
        '사진은 {type:image,path:절대경로,caption:선택}, PDF·XLSX·DOCX·HWP 등 파일(메일 첨부 download 경로 포함)은 {type:file,path:절대경로,caption:선택}로 최상위 합계 10개까지(파일 20MB). '
        '기존 글·첨부를 보존하고 last_edited를 seen_last_edited로 넘긴다. 옛 메모의 마지막 ━ 5개 이상 구분선 아래 원문은 덧붙이기만 한다. '
        'update 글 칸은 {append:글}·{replace:{find,with}}를 쓴다. replace_all은 사용자가 그 건의 전체 재작성을 명시했을 때만 쓴다. '
        'report·verification·content_loss·attachment_loss를 보고하고 불명확한 쓰기는 재시도 전에 page로 확인한다. '
        'rollback은 같은 대화·스레드에서 30분 안 즉시, 24시간 안 확인받은 key로 복원한다. 예약·배경 변경은 거부한다.'
    )


SCHEMA = {
    "name": "notion",
    "description": (
        "Use batch(title, operations) for multiple changes and one tool approval; no separate prose approval. "
        "rollback(key?) restores this conversation's latest change: immediate within 30 minutes, confirmed key within "
        "24 hours. Preserve text and attachments, including original text below the last line with 5+ ━. "
        "replace_all=true requires an explicit full rewrite request. Pass page.last_edited as seen_last_edited for "
        "plaintext edits of nonempty text. update text accepts {append: text} or {replace: {find: text, with: text}}. "
        "Check verification and content_loss. Read and, after the user approves, change reachable Notion "
        "pages/databases. Reads: databases, search(query), schema(database_id), query(database_id, filter?, sorts?, "
        "page_size<=50, cursor?), page(page_id), users. Writes: create(database_id+properties or parent_page_id+title; "
        "paragraphs?), update(page_id+properties), append(page_id+paragraphs, after? top-level block id), "
        "edit(page_id+blocks, max20 text/checked/color changes), trash(page_id). IDs may be Notion links. Rich "
        "paragraphs, images/files (max10), nesting and spans: see paragraphs parameter. Use heading_2/heading_3 for "
        "text size. Properties: text, number, option name(s), YYYY-MM-DD or {start,end}, boolean, people names."),
    "parameters": {"type": "object", "properties": {
        "action": {"type": "string", "enum": sorted(READ | WRITE)},
        "database_id": {"type": "string"}, "page_id": {"type": "string"}, "parent_page_id": {"type": "string"},
        "query": {"type": "string"}, "title": {"type": "string"},
        "operations": {"type": "array", "minItems": 1, "maxItems": 10, "items": {"type": "object"}, "description": "Existing create/update/append/edit/trash arguments, in order; no nested batch."},
        "key": {"type": "string", "description": "Return the rollback preview key only after the user confirms."},
        "replace_all": {"type": "boolean", "description": "update only: explicit full rewrite authorized by the user for this item; never bypass a preservation refusal."},
        "seen_last_edited": {"type": "string", "description": "page/query last_edited; required for plaintext update of nonempty text, optional for partial update/append/edit/trash."},
        "properties": {"type": "object", "description": "Property name to plain value."},
        "after": {"type": "string", "description": "append only: insert after this top-level block of the same page."},
        "blocks": {"type": "array", "minItems": 1, "maxItems": 20, "items": {"type": "object"}, "description": "edit only: {id, text?: string or spans, checked?: boolean (to_do), color?: Notion color}. No deletion, type changes or moves."},
        "paragraphs": {"description": "Strings or {type,text: string or [{text,bold,italic,underline,strike,code,color,link}],color?,children?}. Types: paragraph, heading_1/2/3 (text size), bulleted_list_item, numbered_list_item, to_do(checked), quote, callout(icon emoji), toggle, divider(no text). Max 50 top-level, 100 total, 2 nested levels. Notion colors only; http/https links. Top-level {type: image|file, path: absolute local path, caption?: text}; up to 10 photos/files. Requires enabled files and allowed paths (mail attachment downloads included).", "type": "array", "maxItems": MAX_PARAGRAPHS, "items": {}},
        "filter": {"type": "object"}, "sorts": {"type": "array", "items": {"type": "object"}},
        "page_size": {"type": "integer", "minimum": 1, "maximum": MAX_ROWS}, "cursor": {"type": "string"},
    }, "required": ["action"]},
}


def schema(config: Any) -> dict:
    if writable(config) != set():
        allowed, _ = _no_approval(config)
        if not allowed and not (isinstance(config, dict) and config.get('full_api') is True): return SCHEMA
        result = copy.deepcopy(SCHEMA)
        result['description'] = result['description'].replace('and one tool approval', 'in one call').replace(
            'Read and, after the user approves, change', 'Read and change')
        result['description'] = ('·'.join(name for name in ('create', 'update', 'append', 'edit') if name in allowed) +
            '는 소유자가 시킨 대화에서 승인 없이 바로 실행하고 결과의 report와 되돌리기 가능 여부를 답변에 적는다. '
            '대화 식별·되돌리기 기록이 불가능하면 승인을 유지한다. trash는 호출마다 승인을 묻는다. ' + result['description'])
        result['parameters']['properties']['paragraphs']['description'] = result['parameters']['properties']['paragraphs']['description'].replace(', user approval', '')
        if isinstance(config, dict) and config.get('full_api') is True:
            result = api_module().extend_schema(result)
        return result
    result = copy.deepcopy(SCHEMA)
    result["description"] = "Read-only Notion: databases, search(query), schema(database_id), query(database_id, filter?, sorts?, page_size<=50, cursor?), page(page_id), users. Changes are unavailable."
    result["parameters"]["properties"]["action"]["enum"] = sorted(READ)
    for key in ("parent_page_id", "title", "properties", "paragraphs", "operations", "key", "replace_all", "seen_last_edited", "blocks", "after"):
        result["parameters"]["properties"].pop(key)
    return result


def image_count(args):
    """Photos and files attached by this call (both are uploads shown as attachments)."""
    return sum(isinstance(i, dict) and i.get('type') in ATTACH_BLOCKS for i in args.get('paragraphs', []) or [])


def attachment_label(args):
    files = any(isinstance(i, dict) and i.get('type') == 'file' for i in args.get('paragraphs', []) or [])
    count = len(attachment_names(args))
    return f'사진·파일 {count}개' if files else f'사진 {count}장'


def attachment_names(args):
    return [Path(i['path']).name for i in args.get('paragraphs', []) or []
            if isinstance(i, dict) and i.get('type') in ATTACH_BLOCKS and isinstance(i.get('path'), str)]


def _size_limit(config, key, default):
    maximum = (config or {}).get(key, default)
    if isinstance(maximum, bool) or not isinstance(maximum, (float, int)) or not 0 < maximum <= 20:
        raise NotionError(f'notion.{key}는 0 초과 20 이하이어야 합니다(single-part 한도).')
    return maximum, int(maximum * 1024 * 1024)


def file_mime(path):
    return IMAGE_MIME.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or 'application/octet-stream'


def image_files(args, config, files_config):
    pictures = [i for i in args.get('paragraphs', []) or [] if isinstance(i, dict) and i.get('type') in ATTACH_BLOCKS]
    if not pictures: return []
    if not isinstance(files_config, dict) or files_config.get('enabled') is not True:
        raise NotionError('사진·파일 첨부에는 files 설정이 켜져 있어야 합니다.')
    roots = _files.folders(files_config) + [_files.attachments(), _files.renders()]
    found = []
    for item in pictures:
        photo = item.get('type') == 'image'
        maximum, limit = _size_limit(config, 'max_image_mb', 5) if photo else _size_limit(config, 'max_file_mb', 20)
        label = '사진' if photo else '파일'
        path = _files.inside(item.get('path'), roots)
        if path is None: raise NotionError(f'{label} 경로가 허용된 파일 범위 밖입니다.')
        if photo and path.suffix.lower() not in IMAGE_MIME: raise NotionError('허용되지 않은 사진 확장자입니다.')
        try:
            info = path.stat()
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= limit:
                raise NotionError(f'{path.name}: {label} 크기 한도 {maximum}MB를 넘었거나 일반 파일이 아닙니다. '
                                  '더 큰 파일은 api upload-file(multi_part)로 나눠 올립니다.')
        except OSError: raise NotionError(f'{path.name}: {label} 파일을 읽지 못했습니다.') from None
        found.append((item, path, limit, roots))
    return found


def upload_images(home, args, config, files_config, opener):
    uploads = {}
    for item, path, limit, roots in image_files(args, config, files_config):
        try:
            # Recheck after approval, then read via directory descriptors to reject symlink swaps.
            fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                for part in path.parts[1:-1]:
                    child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                    os.close(fd); fd = child
                source = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            finally: os.close(fd)
            with os.fdopen(source, 'rb') as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode): raise NotionError('일반 파일이 아닙니다.')
                raw = stream.read(limit + 1)
            if not 0 < len(raw) <= limit: raise NotionError('첨부 크기가 바뀌어 한도를 넘었습니다.')
            mime = file_mime(path)
            created = request(home, 'POST', 'file_uploads', {'mode': 'single_part', 'filename': path.name, 'content_type': mime}, opener=opener)
            upload_id = object_id(created.get('id'))
            boundary = 'hermes' + uuid.uuid4().hex
            safe_name = path.name.replace('"', '_').replace('\r', '_').replace('\n', '_').replace('\\', '_')
            payload = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'
                       f'Content-Type: {mime}\r\n\r\n').encode() + raw + f'\r\n--{boundary}--\r\n'.encode()
            sent = request(home, 'POST', f'file_uploads/{upload_id}/send', payload, opener=opener,
                           content_type=f'multipart/form-data; boundary={boundary}')
            if sent.get('status') != 'uploaded': raise NotionError('업로드 완료 상태를 확인하지 못했습니다.')
            uploads[item['path']] = upload_id
        except (NotionError, OSError) as exc:
            error = NotionError(f'{path.name}: 첨부 업로드 실패 ({exc}). 본문은 보내지 않았습니다.')
            error.unknown = getattr(exc, 'unknown', False) or isinstance(exc, OSError)
            raise error from None
    return uploads


# Change preparation is read-only and shared by approval and execution.
def rich_payload(parts):
    result = []
    for part in parts or []:
        kind = part.get('type', 'text')
        if kind not in ('text', 'mention', 'equation'):
            raise NotionError(f'복원할 수 없는 rich_text 종류: {kind}')
        data = copy.deepcopy(part.get(kind))
        if data is None and kind == 'text' and 'plain_text' in part:
            data = {'content': part['plain_text']}  # Also supports minimal API test fixtures.
        if not isinstance(data, dict): raise NotionError('불완전한 rich_text는 수정하지 않습니다.')
        if kind == 'mention':
            mention_type = data.get('type')
            if mention_type not in ('user', 'page', 'database', 'date'):
                raise NotionError(f'복원할 수 없는 mention 종류: {mention_type}')
            if mention_type == 'user': data = {'type': 'user', 'user': {'id': data['user']['id']}}
        item = {'type': kind, kind: data}
        if part.get('annotations'): item['annotations'] = copy.deepcopy(part['annotations'])
        result.append(item)
    check_rich(result)
    return result


def check_rich(parts):
    chars = sum(len(p.get('text', {}).get('content', p.get('equation', {}).get('expression', ''))) for p in parts)
    if len(parts) > 100 or any(len(p.get('text', {}).get('content', '')) > 2000 for p in parts):
        raise NotionError(f'rich_text 한도 초과: {chars}자, 객체 {len(parts)}개 (객체당 2000자·배열 100개)')


def raw_property(prop):
    """Keep writable API fields, never flatten formatting when making a snapshot."""
    kind = prop.get('type') or next((k for k in _SIMPLE if k in prop), None)
    if kind not in _SIMPLE or kind not in prop:
        raise NotionError(f'복원할 수 없는 속성 종류 또는 불완전한 값: {kind}')
    data = copy.deepcopy(prop[kind])
    if prop.get('has_more'):
        raise NotionError('일부만 읽은 속성은 안전하게 복원할 수 없어 수정하지 않습니다.')
    if kind in ('title', 'rich_text'):
        # Retrieve-page may truncate inline references at 25; refuse instead of losing them.
        if sum(p.get('type') == 'mention' for p in data) >= 25:
            raise NotionError('멘션 25개 이상은 전체 원본을 보장할 수 없어 수정하지 않습니다.')
        data = rich_payload(data)
    elif kind == 'people':
        if len(data) >= 25: raise NotionError('사람 25명 이상은 전체 원본을 보장할 수 없어 수정하지 않습니다.')
        data = [{'object': 'user', 'id': p['id']} for p in data]
    elif kind in ('select', 'status'):
        data = None if data is None else ({'id': data['id']} if data.get('id') else {'name': data['name']})
    elif kind == 'multi_select':
        data = [({'id': p['id']} if p.get('id') else {'name': p['name']}) for p in data]
    return {kind: data}


def comparable(prop):
    """Compare semantic API values, ignoring read-only and default decoration."""
    kind = prop.get('type') or next((k for k in _SIMPLE if k in prop), None)
    data = prop.get(kind)
    if kind in ('title', 'rich_text'):
        parts = rich_payload(data)
        for part in parts:
            annotations = {k: v for k, v in part.get('annotations', {}).items() if v not in (False, 'default', None)}
            part.pop('annotations', None)
            if annotations: part['annotations'] = annotations
            if part['type'] == 'text' and not part['text'].get('link'): part['text'].pop('link', None)
        return {kind: parts}
    if kind in ('select', 'status'):
        return {kind: None if data is None else data.get('name', data.get('id'))}
    if kind == 'multi_select': return {kind: sorted(p.get('name', p.get('id', '')) for p in data or [])}
    if kind == 'people': return {kind: sorted(p['id'] for p in data or [])}
    if kind == 'date' and data is not None:
        data = {k: v for k, v in data.items() if v is not None}
    return {kind: data}


def text_of(parts):
    return ''.join(p.get('plain_text', p.get('text', {}).get('content', p.get('equation', {}).get('expression', ''))) for p in parts or [])


def has_format(parts):
    return any(p.get('type', 'text') != 'text' or p.get('text', {}).get('link') or
               any(v not in (False, None, 'default') for v in p.get('annotations', {}).values()) for p in parts)


def validate_edits(blocks):
    if not isinstance(blocks, list) or not 1 <= len(blocks) <= 20:
        raise NotionError('edit.blocks needs 1–20 blocks')
    ids = set()
    for block in blocks:
        if not isinstance(block, dict) or set(block) - {'id', 'text', 'checked', 'color'} or not set(block) - {'id'}:
            raise NotionError('edit block needs id and text/checked/color only')
        ident = object_id(block.get('id'))
        if ident in ids: raise NotionError('edit block IDs must be unique')
        ids.add(ident)
        if 'text' in block: styled_text(block['text'])
        if 'checked' in block and type(block['checked']) is not bool: raise NotionError('checked needs a boolean')
        if 'color' in block: block_color(block['color'])


def checked_block(home, page_id, ident, opener, *, top_level=False):
    block = request(home, 'GET', 'blocks/' + ident, opener=opener)
    current = block; seen = {ident}
    for _ in range(10):
        if current.get('archived') or current.get('in_trash'):
            raise NotionError('휴지통 블록은 고치거나 삽입 위치로 쓸 수 없습니다.')
        parent = _parent_id(current.get('parent'))
        if parent == ('pages', page_id): return block
        if top_level or parent is None or parent[0] != 'blocks' or parent[1] in seen:
            break
        seen.add(parent[1])
        current = request(home, 'GET', 'blocks/' + parent[1], opener=opener)
    raise NotionError('블록이 지정한 page_id의 하위인지 확인하지 못했습니다.')


def block_value(block):
    kind = block.get('type'); content = block.get(kind, {})
    if kind not in TEXT_BLOCKS or not isinstance(content, dict) or 'rich_text' not in content:
        raise NotionError('edit는 rich_text가 있는 글 블록만 고칩니다.')
    result = {'rich_text': rich_payload(content['rich_text']), 'color': content.get('color', 'default')}
    if kind == 'to_do': result['checked'] = content.get('checked', False)
    return result


def comparable_block(content):
    return {**content, **comparable({'rich_text': content['rich_text']})}


def block_fingerprint(block):
    data = {'type': block.get('type'), 'value': comparable_block(block_value(block)),
            'parent': _parent_id(block.get('parent')), 'last_edited_time': block.get('last_edited_time'),
            'archived': bool(block.get('archived') or block.get('in_trash'))}
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def prepare_edits(home, page_id, blocks, opener, cache):
    edits = []
    for given in blocks:
        ident = object_id(given['id'])
        block = checked_block(home, page_id, ident, opener)
        # Earlier operations in a batch are simulated for validation; snapshots keep the first value.
        block = copy.deepcopy(cache.get('block:' + ident, block))
        kind = block.get('type'); before = block_value(block)
        if 'checked' in given and kind != 'to_do': raise NotionError('checked는 to_do만 받습니다.')
        patch = {}
        if 'text' in given: patch['rich_text'] = styled_text(given['text'])
        if 'checked' in given: patch['checked'] = given['checked']
        if 'color' in given: patch['color'] = given['color']
        after = {**copy.deepcopy(before), **patch}
        warnings = []
        if 'text' in given and has_format(before['rich_text']) and comparable_block(before) != comparable_block(after):
            warnings.append(f'{ident}: 글 교체로 기존 링크·멘션·수식·서식이 사라질 수 있습니다. 이전 서식은 되돌리기 기록에 보관합니다.')
        edits.append({'id': ident, 'type': kind, 'before': before, 'after': after, 'patch': patch,
                      'snapshot': block_fingerprint(block), 'warnings': warnings,
                      'missing': missing_lines(text_of(before['rich_text']), text_of(after['rich_text']))})
        block[kind].update(copy.deepcopy(patch)); cache['block:' + ident] = block
    return edits


def edit_report(plan, outcomes=None):
    lines = []; states = {b['id']: b['status'] for b in outcomes or []}
    for edit in plan.get('edits', []):
        ident = edit['id']; old = edit['before']; new = edit['after']
        status = f" [{states.get(ident, 'skipped')}]" if outcomes is not None else ''
        lines.append(f"블록 {ident}{status}: {text_of(old['rich_text'])[:240]} → {text_of(new['rich_text'])[:240]}")
        if old.get('checked') != new.get('checked'): lines.append(f"체크: {old.get('checked')} → {new.get('checked')}")
        if old['color'] != new['color']: lines.append(f"색: {old['color']} → {new['color']}")
        if edit['missing']: lines.append('빠진 기존 줄:\n' + loss_preview(edit['missing']))
    return lines


def snapshot_values(plans):
    values = {}
    for plan in plans:
        if 'snapshot' in plan: values.setdefault(plan['page_id'], plan['snapshot'])
        for edit in plan.get('edits', []): values.setdefault('block:' + edit['id'], edit['snapshot'])
    return values


def execute_edits(home, plan, opener, persist):
    inverse = {'action': 'edit', 'page_id': plan['page_id'], 'blocks': []}
    outcomes = []; halted = False
    for edit in plan['edits']:
        ident = edit['id']
        if halted:
            outcomes.append({'id': ident, 'status': 'skipped'}); continue
        saved = None
        try:
            # Check each block again immediately before its PATCH, even within one edit call.
            current = checked_block(home, plan['page_id'], ident, opener)
            if block_fingerprint(current) != edit['snapshot']:
                raise NotionError('실행 직전 본문 블록이 바뀌었다. 다시 읽고 합친다.')
            saved = {k: copy.deepcopy(edit[k]) for k in ('id', 'type', 'before', 'after')}
            saved['status'] = 'unknown'; inverse['blocks'].append(saved)
            persist(inverse)
            response = request(home, 'PATCH', 'blocks/' + ident, {edit['type']: edit['patch']}, opener=opener)
            if not response.get('id') or object_id(response['id']) != ident:
                raise UnknownChange('블록 변경 응답 ID를 확인하지 못했습니다.')
            saved['status'] = 'done'; persist(inverse)
            outcomes.append({'id': ident, 'status': 'done'})
        except (NotionError, OSError, ValueError, KeyError, TypeError) as exc:
            unknown = getattr(exc, 'unknown', False) or (isinstance(exc, OSError) and saved is not None)
            if saved is not None and not unknown:
                inverse['blocks'].remove(saved); persist(inverse)
            outcomes.append({'id': ident, 'status': 'unknown' if unknown else 'failed', 'error': str(exc)})
            halted = True
    return {'ok': not halted, 'status': ('unknown' if any(b['status'] == 'unknown' for b in outcomes) else 'failed') if halted else 'done',
            'edited': outcomes, 'content_loss': plan['loss']}, inverse, plan['page_id'], []


def verify_edits(home, plan, outcomes, opener):
    verified = {}; snapshots = {}
    states = {b['id']: b['status'] for b in outcomes}
    for edit in plan['edits']:
        ident = edit['id']; item = {'matches': None, 'status': states[ident]}
        if states[ident] in ('done', 'unknown'):
            try:
                current = checked_block(home, plan['page_id'], ident, opener)
                actual = block_value(current)
                item.update(text=text_of(actual['rich_text']), color=actual['color'])
                if 'checked' in actual: item['checked'] = actual['checked']
                item['matches'] = current['type'] == edit['type'] and comparable_block(actual) == comparable_block(edit['after'])
                snapshots['block:' + ident] = block_fingerprint(current)
            except (NotionError, KeyError, TypeError) as exc: item['error'] = str(exc)
        verified[ident] = item
    return verified, snapshots


def prepare(home, op, opener, pages=None):
    pages = {} if pages is None else pages
    action = op['action']; plan = {'warnings': [], 'before': {}, 'before_raw': {}, 'loss': {}, 'text_checks': {}, 'transitions': {}}
    page_id = object_id(op.get('page_id') or op.get('parent_page_id') or op.get('database_id'))
    plan['page_id'] = page_id
    if action == 'create' and op.get('database_id'):
        database = request(home, 'GET', 'databases/' + page_id, opener=opener)
        plan['name'] = _title_of(database) or page_id
        plan['body'] = {'parent': {'database_id': page_id}, 'properties': _properties(home, database, op['properties'], opener)}
    else:
        if page_id not in pages: pages[page_id] = request(home, 'GET', 'pages/' + page_id, opener=opener)
        page = pages[page_id]
        plan['snapshot'] = page_fingerprint(page)
        plan['last_edited'] = page.get('last_edited_time')
        if op.get('seen_last_edited') is not None and op['seen_last_edited'] != page.get('last_edited_time'):
            raise NotionError('그 사이 Notion에서 바뀌었다. 다시 읽고 합친다 (seen_last_edited 불일치).')
        plan['name'] = _title_of(page) or page_id; plan['url'] = page.get('url')
        if action == 'create':
            plan['body'] = {'parent': {'page_id': page_id}, 'properties': {'title': {'title': _text(op['title'])}}}
        elif action == 'update':
            parent = page.get('parent') or {}
            if parent.get('type') != 'database_id':
                raise NotionError('update changes the properties of a database row; this page is not one')
            database = request(home, 'GET', 'databases/' + object_id(parent['database_id']), opener=opener)
            given = op['properties']; simple = {}; partial = {}
            for name, val in given.items():
                kind = database.get('properties', {}).get(name, {}).get('type')
                if kind in ('title', 'rich_text') and isinstance(val, dict): partial[name] = (kind, val)
                else: simple[name] = val
            payload = _properties(home, database, simple, opener) if simple else {}
            for name, (kind, val) in partial.items():
                old_prop = page.get('properties', {}).get(name)
                if old_prop is None: raise NotionError(f'{name}: 현재 값을 읽지 못했습니다.')
                old = raw_property(old_prop)[kind]
                if set(val) == {'append'} and isinstance(val['append'], str):
                    new = old + _text(val['append'])
                elif set(val) == {'replace'} and isinstance(val['replace'], dict) and set(val['replace']) == {'find', 'with'}:
                    find, replacement = val['replace']['find'], val['replace']['with']
                    current = text_of(old_prop[kind])
                    if not isinstance(find, str) or not find or not isinstance(replacement, str) or current.count(find) != 1:
                        raise NotionError(f'{name}: replace.find는 현재 글에 정확히 한 번 있어야 합니다.')
                    new = _text(current.replace(find, replacement, 1))
                else: raise NotionError(f'{name}: use {{append: text}} or {{replace: {{find, with}}}}')
                check_rich(new); payload[name] = {kind: new}
            if not payload: raise NotionError('properties needs a nonempty object')
            for name, target in payload.items():
                original = page.get('properties', {}).get(name)
                if original is None: raise NotionError(f'{name}: 변경 전 원본 속성이 없어 복원할 수 없습니다.')
                plan['before'][name] = raw_property(original)
                plan['before_raw'][name] = copy.deepcopy(original)
                kind = next(iter(target))
                if kind in ('title', 'rich_text'):
                    check_rich(target[kind])
                    preserving = isinstance(given[name], dict) and 'append' in given[name]
                    if has_format(original[kind]) and not preserving:
                        plan['warnings'].append(f'{name}: 평문 교체로 기존 링크·멘션·수식·서식이 사라집니다.')
                    before, after = text_of(original[kind]), text_of(target[kind])
                    if before.strip() and not isinstance(given[name], dict) and not op.get('seen_last_edited'):
                        raise NotionError(f'{name}: 현재 page/query를 읽고 last_edited를 seen_last_edited로 넘겨야 합니다.')
                    checked = text_change(before, after, allow_all=op.get('replace_all') is True, appending=preserving)
                    plan['text_checks'][name] = checked
                    if checked['missing']:
                        plan['loss'][name] = {'missing_lines': len(checked['missing']), 'preview': checked['missing'][:15]}
                else:
                    plan['transitions'][name] = f"{value(original)} → {value({'type': kind, **target})}"
            plan['body'] = {'properties': payload}
            # Simulate earlier updates for validation of later partial edits in this batch.
            pages[page_id] = copy.deepcopy(page)
            for name, prop in payload.items():
                pages[page_id]['properties'][name] = {'type': next(iter(prop)), **prop}
        elif action == 'edit':
            if page.get('archived') or page.get('in_trash'):
                raise NotionError('이미 휴지통인 페이지는 변경하지 않습니다.')
            plan['edits'] = prepare_edits(home, page_id, op['blocks'], opener, pages)
            for edit in plan['edits']:
                ident = edit['id']
                if edit['missing']: plan['loss'][ident] = {'missing_lines': len(edit['missing']), 'preview': edit['missing'][:15]}
                plan['warnings'].extend(edit['warnings'])
        elif action == 'trash':
            if page.get('archived') or page.get('in_trash'):
                raise NotionError('이미 휴지통인 페이지는 변경하지 않습니다.')
            plan['body'] = {'archived': True}
    if action == 'append' and 'after' in op:
        checked_block(home, page_id, object_id(op['after']), opener, top_level=True)
    if action in ('update', 'append', 'edit', 'trash'):
        plan['attachments_before'] = attachment_count(home, page_id, opener)
    return plan


def preflight(config, home, args, files_config, opener):
    ops = args['operations'] if args['action'] == 'batch' else [args]
    plans = []; pages = {}
    for index, op in enumerate(ops, 1):
        try:
            if reason := problem(op, config, files_config): raise NotionError(reason)
            if reason := outside(config, home, op, opener=opener): raise NotionError(reason)
            plan = prepare(home, op, opener, pages)
            body = copy.deepcopy(plan.get('body', {}))
            if op.get('paragraphs'): body['children'] = _paragraphs(op['paragraphs'])
            if op['action'] == 'edit': body = {e['id']: {e['type']: e['patch']} for e in plan['edits']}
            if len(json.dumps(body).encode('utf-8')) > 500 * 1024:
                raise NotionError('Notion 요청 본문 500KiB 한도를 넘습니다. 변경을 나눠 주세요.')
            plans.append(plan)
        except (NotionError, ValueError, KeyError, TypeError) as exc:
            raise NotionError(f'{index}: {exc}') from None
    return plans


def preview(op, plan):
    action = op['action']; target = f"Notion '{plan['name']}'"
    if action == 'create': line = f"{target}에 새 항목을 만듭니다: {op.get('title', '')}\n{_shown(op.get('properties'))}"
    elif action == 'update':
        prefix = '기존 내용을 지우고 새로 씁니다. ' if op.get('replace_all') else ''
        line = f"{prefix}{target}을 고칩니다:\n{_shown(op.get('properties'))}"
    elif action == 'append': line = f"{target}에 본문 {len(op['paragraphs'])}개를 덧붙입니다."
    elif action == 'edit': line = f"{target}의 본문을 고칩니다:\n" + '\n'.join(edit_report(plan))
    else:
        count = plan.get('attachments_before')
        shown = str(count) + '개' if count is not None else '개수 미확인'
        line = f"{target}: 사진·파일 {shown}가 있는 항목을 휴지통으로 옮깁니다.\n휴지통은 복구 가능하며 rollback 대상입니다."
    if action == 'create' and op.get('paragraphs'): line += f"\n본문 {len(op['paragraphs'])}개 포함"
    images = attachment_names(op)
    line = line[:700] + ("…" if len(line) > 700 else "")
    for name, change in plan.get('text_checks', {}).items():
        if change['missing']:
            line += f"\n{name}: 빠지는 기존 내용 {len(change['missing'])}줄\n" + loss_preview(change['missing'])
        else: line += f'\n{name}: 기존 내용을 모두 유지합니다'
    for name, transition in plan.get('transitions', {}).items():
        line += f'\n{name}: {transition[:240]}'
    if body_lines := paragraph_lines(op.get('paragraphs') or []): line += '\n추가한 본문:\n' + loss_preview(body_lines)
    if images: line += f"\n{attachment_label(op)}: " + ', '.join(images)
    if plan.get('url'): line += '\n' + plan['url']
    return line + ('\n' + '\n'.join(plan['warnings']) if plan['warnings'] else '')


def report(op, plan, result):
    action = op['action']
    page = result.get('created') or result.get('updated') or {}
    name = plan['name']
    if action == 'create':
        name = next((text_of(prop[kind]) for prop in plan['body']['properties'].values()
                     for kind in ('title',) if kind in prop), name)
    lines = [f"{action}: Notion '{name}'"]
    url = page.get('url') or result.get('verification', {}).get('url')
    if not url and action != 'create': url = plan.get('url')
    if url: lines.append(url)
    if op.get('replace_all'): lines.append('전체 재작성')
    for name, prop in plan.get('body', {}).get('properties', {}).items():
        kind = next(iter(prop))
        if kind in ('title', 'rich_text'):
            before = text_of(plan.get('before_raw', {}).get(name, {}).get(kind, []))
            after = text_of(prop[kind])
            added = missing_lines(after, before)
            removed = plan.get('text_checks', {}).get(name, {}).get('missing', [])
            lines.append(f'{name}: 추가된 줄 {len(added)}개' + ('\n' + loss_preview(added) if added else ''))
            lines.append(f'{name}: 빠진 줄 {len(removed)}개\n' + loss_preview(removed) if removed else f'{name}: 기존 내용을 모두 유지했습니다')
        else:
            transition = plan.get('transitions', {}).get(name, f"없음 → {value({'type': kind, **prop})}")
            lines.append(f'{name}: {transition}')
    paragraphs = op.get('paragraphs') or []
    images = attachment_names(op)
    lines.append(f'덧붙인 본문 블록 {len(paragraphs)}개 · {attachment_label(op)}' + (': ' + ', '.join(images) if images else ''))
    body_lines = paragraph_lines(paragraphs)
    if action == 'edit': lines.extend(edit_report(plan, result.get('edited', [])))
    if body_lines: lines.append('추가한 본문:\n' + loss_preview(body_lines))
    return '\n'.join(lines + plan['warnings'])


def conversation():
    try:
        from gateway.session_context import get_session_env as env
        if any(str(env(k, '')).lower() not in ('', '0', 'false') for k in ('HERMES_CRON_SESSION', 'HERMES_SINGLE_QUERY_SESSION', 'HERMES_KANBAN_TASK')) or os.getenv('HERMES_KANBAN_TASK'):
            return None
        keys = {'platform': 'PLATFORM', 'scope_id': 'SCOPE_ID', 'chat_id': 'CHAT_ID', 'user_id': 'USER_ID', 'thread_id': 'THREAD_ID'}
        result = {k: env('HERMES_SESSION_' + v, '').strip() for k, v in keys.items()}
        if result['platform'] != 'slack' or not result['chat_id'] or not result['user_id']: return None
        return result
    except (ImportError, AttributeError): return None


_JOURNAL_LOCK = threading.RLock()


@contextmanager
def journal(home):
    # Serialize thread-local descriptor setup too; flock supplies the cross-process lock.
    with _JOURNAL_LOCK:
        with _journal_locked(home) as state:
            yield state


@contextmanager
def _journal_locked(home):
    """Hold a cross-process lock through a change; publish snapshots with atomic 0600 writes."""
    home_fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        try: os.mkdir('state', 0o700, dir_fd=home_fd)
        except FileExistsError: pass
        directory = os.open('state', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=home_fd)
    finally: os.close(home_fd)
    lock = None
    try:
        lock = os.open('notion-rollback.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=directory)
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            source = os.open('notion-rollback.json', os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        except FileNotFoundError: data = {'version': 1, 'entries': []}
        else:
            with os.fdopen(source, 'r') as stream: data = json.load(stream)
        if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('entries'), list):
            raise NotionError('되돌리기 기록 형식 오류: 변경하지 않습니다.')
        data['entries'] = [e for e in data['entries'] if 0 <= time.time() - e['created_at'] < 86400][-100:]

        def save():
            data['entries'] = data['entries'][-100:]
            temp = 'notion-rollback.tmp-' + uuid.uuid4().hex
            try:
                fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=directory)
                with os.fdopen(fd, 'w') as stream:
                    json.dump(data, stream, ensure_ascii=False); stream.flush(); os.fsync(stream.fileno())
                os.replace(temp, 'notion-rollback.json', src_dir_fd=directory, dst_dir_fd=directory)
                os.fsync(directory)
            finally:
                try: os.unlink(temp, dir_fd=directory)
                except FileNotFoundError: pass
        yield data, save
    finally:
        if lock is not None: os.close(lock)
        os.close(directory)


def verification(home, page_id, expected, block_ids, opener, *, new_page=False, block_count=0, images=0):
    result = {'properties': {name: {'value': None, 'matches': None} for name in expected},
              'blocks': None, 'images': None, 'blocks_match': None, 'images_match': None}
    try:
        page = request(home, 'GET', 'pages/' + page_id, opener=opener)
        result['url'] = page.get('url')
        result['_snapshot'] = page_fingerprint(page)
        result['_last_edited'] = page.get('last_edited_time')
        for name, prop in expected.items():
            actual = page.get('properties', {}).get(name)
            if actual is not None:
                try: matches = comparable(actual) == comparable(prop)
                except (NotionError, KeyError, TypeError): matches = None
                result['properties'][name] = {'value': value(actual), 'matches': matches}
        if block_count:
            if new_page:
                response = request(home, 'GET', f'blocks/{page_id}/children?page_size=100', opener=opener)
                blocks = response.get('results', [])
                if response.get('has_more'): return result
            elif len(block_ids) == block_count:
                blocks = [request(home, 'GET', 'blocks/' + ident, opener=opener) for ident in block_ids]
                if any(b.get('archived') or b.get('in_trash') or not b.get('type') for b in blocks): return result
            else: return result
            result.update(blocks=len(blocks), images=sum(b.get('type') in ATTACH_BLOCKS for b in blocks))
            result.update(blocks_match=result['blocks'] == block_count, images_match=result['images'] == images)
    except (NotionError, KeyError, TypeError) as exc: result['error'] = str(exc)
    return result


def execute_change(config, home, op, plan, files_config, opener, persist=lambda inverse: None):
    action = op['action']; page_id = plan['page_id']; ids = []
    if action in ('create', 'append'):
        uploads = upload_images(home, op, config, files_config, opener)
        blocks = _paragraphs(op.get('paragraphs'), uploads)
    if action == 'edit': return execute_edits(home, plan, opener, persist)
    if action == 'create':
        body = copy.deepcopy(plan['body'])
        if blocks: body['children'] = blocks
        response = request(home, 'POST', 'pages', body, opener=opener)
        if not response.get('id'): raise UnknownChange('Created page ID missing; whether a change landed is unknown')
        page_id = object_id(response['id'])
        result = {'ok': True, 'created': row(response), 'images_attached': image_count(op)}
        inverse = {'action': 'create', 'page_id': page_id}
    elif action == 'update':
        response = request(home, 'PATCH', 'pages/' + page_id, plan['body'], opener=opener)
        if not response.get('id'): raise UnknownChange('Update response ID missing; whether a change landed is unknown')
        result = {'ok': True, 'updated': row(response), 'content_loss': plan['loss']}
        inverse = {'action': 'update', 'page_id': page_id, 'before': plan['before'], 'before_raw': plan['before_raw'],
                   'after': plan['body']['properties']}
    elif action == 'append':
        response = request(home, 'PATCH', f'blocks/{page_id}/children', {'children': blocks, **({'after': object_id(op['after'])} if 'after' in op else {})}, opener=opener)
        returned = response.get('results', [])
        ids = [object_id(b['id']) for b in returned if b.get('id')]
        result = {'ok': True, 'appended': len(returned), 'images_attached': image_count(op)}
        inverse = {'action': 'append', 'page_id': page_id, 'block_ids': ids}
        if len(ids) != len(blocks):
            inverse['incomplete'] = True
            result.update(ok=False, status='unknown', error='추가 블록 ID 응답이 불완전합니다. 자동 재시도하지 마세요.')
    else:
        response = request(home, 'PATCH', 'pages/' + page_id, {'archived': True}, opener=opener)
        if not response.get('id'): raise UnknownChange('Trash response ID missing; whether a change landed is unknown')
        result = {'ok': True, 'trashed': row(response)}
        inverse = {'action': 'trash', 'page_id': page_id}
    return result, inverse, page_id, ids


def change(config, home, args, files_config, opener):
    # A batch gets no mutation at all unless every operation passes its preflight.
    approved = consume_snapshot(home, args)
    plans = preflight(config, home, args, files_config, opener)
    initial = snapshot_values(plans)
    if approved is not None and initial != approved:
        raise NotionError('승인 뒤 그 사이 Notion에서 바뀌었다. 다시 읽고 합친다.')
    required = approval_required(config, args)
    owned = {}; owned_blocks = {}
    ops = args['operations'] if args['action'] == 'batch' else [args]
    who = conversation() if config.get('rollback_enabled', True) is not False else None
    with journal(home) as (data, save):
        entry = {'conversation': who, 'created_at': time.time(), 'title': args.get('title') or ops[0]['action'],
                 'key': hashlib.sha256((json.dumps(args, sort_keys=True) + uuid.uuid4().hex).encode()).hexdigest(),
                 'consumed': False, 'operations': []}
        results = []; halted = False
        for index, op in enumerate(ops, 1):
            if halted:
                results.append({'index': index, 'status': 'skipped'}); continue
            pending = None
            try:
                # Re-read immediately before mutation; snapshots include earlier successful operations.
                if reason := outside(config, home, op, opener=opener): raise NotionError(reason)
                effective = op
                ident = object_id(op.get('page_id') or op.get('parent_page_id') or op.get('database_id'))
                if ident in owned and op.get('seen_last_edited'):
                    effective = {**op, 'seen_last_edited': owned[ident][1]}
                plan = prepare(home, effective, opener)
                expected = owned[ident][0] if ident in owned else initial.get(ident)
                if 'snapshot' in plan and expected != plan['snapshot']:
                    raise NotionError('실행 직전 그 사이 Notion에서 바뀌었다. 다시 읽고 합친다.')
                for edit in plan.get('edits', []):
                    key = 'block:' + edit['id']
                    if edit['snapshot'] != owned_blocks.get(key, initial.get(key)):
                        raise NotionError('승인 뒤 본문 블록이 바뀌었다. 다시 읽고 합친다.')
                pending = {'action': 'unknown', 'page_id': plan['page_id'], 'original_action': op['action'], 'index': index}
                if who:
                    if entry not in data['entries']: data['entries'].append(entry)
                    entry['operations'].append(pending); save()
                def persist(inverse):
                    if who:
                        entry['operations'][-1] = {**copy.deepcopy(inverse), 'index': index}; save()
                result, inverse, page_id, ids = execute_change(config, home, op, plan, files_config, opener, persist)
                inverse['index'] = index
                if who:
                    entry['operations'][-1] = inverse
                    if inverse['action'] == 'edit' and not inverse['blocks']: entry['operations'].pop()
                    save()
                result['verification'] = verification(home, page_id, plan.get('body', {}).get('properties', {}), ids, opener,
                    new_page=op['action'] == 'create', block_count=len(op.get('paragraphs') or []), images=image_count(op))
                if op['action'] == 'edit':
                    verified, fingerprints = verify_edits(home, plan, result['edited'], opener)
                    result['verification']['edited_blocks'] = verified
                    owned_blocks.update(fingerprints)
                fingerprint = result['verification'].pop('_snapshot', None)
                edited = result['verification'].pop('_last_edited', None)
                owned[page_id] = (fingerprint, edited)
                if op['action'] in ('update', 'append', 'edit'):
                    attachments_after = attachment_count(home, page_id, opener)
                    attachments_before = plan.get('attachments_before')
                    known = attachments_before is not None and attachments_after is not None
                    lost = attachments_after < attachments_before if known else None
                    result['verification'].update(attachments_before=attachments_before, attachments_after=attachments_after,
                        attachments_expected_increase=image_count(op) if op['action'] == 'append' else 0,
                        attachments_increase_matches=(attachments_after - attachments_before == image_count(op)) if known else None)
                    result['attachment_loss'] = lost
                    if lost: result['warnings'] = ['기존 사진·파일 첨부 수가 줄었습니다. 내용을 확인하고 되돌릴지 물어보세요.']
                if op['action'] == 'update':
                    losses = {}
                    for name, old in plan['before_raw'].items():
                        if old.get('type') not in ('title', 'rich_text'): continue
                        observed = result['verification']['properties'][name]['value']
                        if observed is None:
                            if name in plan['loss']: losses[name] = {**plan['loss'][name], 'verified': False}
                            continue
                        before = text_of(old[old['type']])
                        missing = missing_lines(before, observed)
                        if missing:
                            losses[name] = {'missing_lines': len(missing), 'preview': [line[:160] for line in missing[:5]], 'verified': True}
                    result['content_loss'] = losses
                result['report'] = report(op, plan, result)
                results.append({'index': index, 'status': 'done', **result})
                halted = result.get('ok') is not True
            except (NotionError, OSError, ValueError, KeyError, TypeError) as exc:
                unknown = getattr(exc, 'unknown', False)
                # A journal error after a mutation may also leave a durable pending record; never replay blindly.
                if isinstance(exc, OSError) and pending is not None: unknown = True
                if who and pending in entry['operations'] and not unknown:
                    entry['operations'].remove(pending); save()
                results.append({'index': index, 'status': 'unknown' if unknown else 'failed', 'ok': False, 'error': str(exc)})
                halted = True
        if who:
            if entry in data['entries'] and not entry['operations']: data['entries'].remove(entry)
            save()
        available = bool(who and any(o['action'] != 'unknown' and (o['action'] != 'edit' or any(b['status'] == 'done' for b in o['blocks'])) for o in entry['operations']))
        meta = {'approval_required': required, 'rollback_available': available, 'rollback_key': entry['key'] if available else None}
        if args['action'] != 'batch': return {**results[0], **meta}
        return {'ok': not halted, 'title': args['title'], 'operations': results, **meta}


def rollback(config, home, args, opener, *, chat=None, require_snapshot=False):
    who = conversation()
    if chat is False or who is None:
        raise NotionError('되돌리기는 식별 가능한 같은 Slack 대화·스레드에서만 실행합니다.')
    if config.get('rollback_enabled', True) is False: raise NotionError('Notion rollback is disabled.')
    legacy_receipt = None
    if config.get('full_api') is True:
        if api_module().is_api_rollback(api_context(), home):
            return api_module().rollback(api_context(), config, home, args, opener, require_snapshot)
        legacy_receipt = api_module().consume(api_context(), home, args, require_snapshot)
    with journal(home) as (data, save):
        candidates = [e for e in data['entries'] if e['conversation'] == who and not e['consumed']]
        if not candidates:
            save(); return {'ok': False, 'error': 'NO_RECENT_NOTION_ROLLBACK', 'mutation_calls': 0}
        entry = candidates[-1]
        if legacy_receipt is not None and legacy_receipt.get('legacy_rollback') != entry['key']:
            raise NotionError('되돌릴 기록이 바뀌었습니다. 다시 확인하세요.')
        if 'key' in args and args['key'] != entry['key']:
            return {'ok': False, 'error': 'ROLLBACK_KEY_NOT_LATEST', 'mutation_calls': 0}
        if time.time() - entry['created_at'] >= 1800 and 'key' not in args:
            return {'ok': False, 'error': 'OLDER_CHANGE_NEEDS_CONFIRMATION', 'mutation_calls': 0,
                    'key': entry['key'], 'title': entry['title'],
                    'changed_at': datetime.fromtimestamp(entry['created_at'], ZoneInfo('Asia/Seoul')).isoformat(),
                    'targets': [{'action': o['action'], 'page_id': o['page_id']} for o in entry['operations']]}
        # Reject the whole restoration if any target is now outside the writable boundary.
        for op in entry['operations']:
            if reason := outside(config, home, {'action': 'update', 'page_id': op['page_id']}, opener=opener):
                return {'ok': False, 'error': reason, 'mutation_calls': 0}
        results = []; blocked_pages = set(); mutation_calls = 0
        for op in list(reversed(entry['operations'])):
            page_id = op['page_id']; action = op['action']; details = []
            if page_id in blocked_pages:
                results.append({'index': op['index'], 'status': 'skipped', 'reason': '뒤 변경의 복원이 남아 있어 이전 변경을 건너뜁니다.'})
                continue
            try:
                if action == 'unknown':
                    details.append({'status': 'skipped', 'reason': '반영 여부를 알 수 없는 변경입니다. 현재 상태를 먼저 확인해야 합니다.'})
                elif action == 'update':
                    current = request(home, 'GET', 'pages/' + page_id, opener=opener)
                    for name in list(op['before']):
                        actual = current.get('properties', {}).get(name)
                        before = op['before_raw'][name]
                        if actual is not None and comparable(actual) == comparable(before):
                            details.append({'property': name, 'status': 'skipped', 'reason': '이미 복원된 값입니다.'})
                            del op['before'][name]; continue
                        if actual is None or comparable(actual) != comparable(op['after'][name]):
                            details.append({'property': name, 'status': 'skipped', 'reason': '그 사이 Notion에서 바뀌었다. 현재 값을 유지합니다.'})
                            continue
                        try:
                            mutation_calls += 1
                            request(home, 'PATCH', 'pages/' + page_id, {'properties': {name: op['before'][name]}}, opener=opener)
                            checked = request(home, 'GET', 'pages/' + page_id, opener=opener).get('properties', {}).get(name)
                            if checked is None or comparable(checked) != comparable(before):
                                raise NotionError('복원 뒤 값이 일치하지 않거나 확인하지 못했습니다.')
                            del op['before'][name]
                            details.append({'property': name, 'status': 'restored'})
                        except NotionError as exc:
                            details.append({'property': name, 'status': 'failed', 'reason': str(exc)})
                        save()
                elif action == 'edit':
                    for saved in list(reversed(op['blocks'])):
                        ident = saved['id']
                        if saved['status'] == 'unknown':
                            details.append({'block_id': ident, 'status': 'skipped', 'reason': '반영 여부를 모르는 블록은 자동 복원하지 않습니다.'})
                            continue
                        try:
                            current = checked_block(home, page_id, ident, opener)
                            actual = block_value(current)
                            if current['type'] != saved['type']: raise NotionError('블록 종류가 바뀌었습니다.')
                            if comparable_block(actual) == comparable_block(saved['before']):
                                op['blocks'].remove(saved)
                                details.append({'block_id': ident, 'status': 'skipped', 'reason': '이미 복원된 값입니다.'})
                            elif comparable_block(actual) != comparable_block(saved['after']):
                                details.append({'block_id': ident, 'status': 'skipped', 'reason': '그 사이 Notion에서 바뀌었다. 현재 값을 유지합니다.'})
                            else:
                                mutation_calls += 1
                                request(home, 'PATCH', 'blocks/' + ident, {saved['type']: saved['before']}, opener=opener)
                                restored = checked_block(home, page_id, ident, opener)
                                if restored['type'] != saved['type'] or comparable_block(block_value(restored)) != comparable_block(saved['before']):
                                    raise NotionError('복원 뒤 블록 값이 일치하지 않거나 확인하지 못했습니다.')
                                op['blocks'].remove(saved)
                                details.append({'block_id': ident, 'status': 'restored'})
                        except (NotionError, KeyError, TypeError) as exc:
                            details.append({'block_id': ident, 'status': 'failed', 'reason': str(exc)})
                        save()
                elif action == 'append':
                    for ident in list(reversed(op['block_ids'])):
                        try:
                            try: block = request(home, 'GET', 'blocks/' + ident, opener=opener)
                            except NotionError as exc:
                                if exc.code != 404: raise
                                block = {'archived': True}
                            if block.get('archived') or block.get('in_trash'):
                                details.append({'block_id': ident, 'status': 'skipped', 'reason': '이미 없는 블록입니다.'})
                            else:
                                mutation_calls += 1
                                removed = request(home, 'DELETE', 'blocks/' + ident, opener=opener)
                                if not (removed.get('archived') or removed.get('in_trash')):
                                    raise NotionError('추가 블록의 삭제 상태를 확인하지 못했습니다.')
                                details.append({'block_id': ident, 'status': 'restored'})
                            op['block_ids'].remove(ident)
                        except NotionError as exc:
                            details.append({'block_id': ident, 'status': 'failed', 'reason': str(exc)})
                        save()
                    if op.get('incomplete'):
                        details.append({'status': 'failed', 'reason': '추가 블록 ID 응답이 불완전하여 나머지는 자동 복원할 수 없습니다.'})
                else:
                    current = request(home, 'GET', 'pages/' + page_id, opener=opener)
                    desired = action == 'create'
                    if bool(current.get('archived') or current.get('in_trash')) == desired:
                        details.append({'status': 'skipped', 'reason': '이미 복원 상태입니다.'})
                    else:
                        mutation_calls += 1
                        response = request(home, 'PATCH', 'pages/' + page_id, {'archived': desired}, opener=opener)
                        if bool(response.get('archived') or response.get('in_trash')) != desired:
                            raise NotionError('페이지 복원 상태를 확인하지 못했습니다.')
                        details.append({'status': 'restored'})
                    op['restored'] = True
            except (NotionError, KeyError, TypeError) as exc:
                details.append({'status': 'failed', 'reason': str(exc)})
            done = ((action == 'update' and not op['before']) or (action == 'edit' and not op['blocks']) or
                    (action == 'append' and not op['block_ids'] and not op.get('incomplete')) or op.get('restored'))
            if done: entry['operations'].remove(op)
            else: blocked_pages.add(page_id)
            status = 'failed' if any(d['status'] == 'failed' for d in details) else ('restored' if done else 'skipped')
            results.append({'index': op['index'], 'action': action, 'status': status, 'details': details})
            save()
        entry['consumed'] = not entry['operations']; save()
        return {'ok': entry['consumed'], 'key': entry['key'], 'title': entry['title'], 'operations': results,
                'remaining_operations': len(entry['operations']),
                'remaining': [{'index': o['index'], 'action': o['action'], 'page_id': o['page_id'],
                               'properties': list(o.get('before', {})), 'block_ids': [b['id'] for b in o['blocks']] if o['action'] == 'edit' else o.get('block_ids', [])}
                              for o in entry['operations']],
                'mutation_calls': mutation_calls, 'approval_calls': 0}


MEDIA_TYPES = frozenset({'image', 'file', 'pdf', 'video', 'audio'})


def normalized_lines(text):
    return [line.strip() for line in text.splitlines() if line.strip()]


def missing_lines(before, after):
    present = set(normalized_lines(after))
    return [line for line in normalized_lines(before) if line not in present]


def loss_preview(lines):
    shown = '\n'.join(line[:120] for line in lines[:15])
    return shown + (f'\n… 외 {len(lines) - 15}줄' if len(lines) > 15 else '')


def text_change(before, after, *, allow_all=False, appending=False):
    missing = missing_lines(before, after)
    # Appending directly to the last line changes line identity but cannot erase old text.
    if appending: return {'missing': [], 'original_missing': []}
    lines = normalized_lines(before)
    separators = [i for i, line in enumerate(lines) if re.search('━{5,}', line)]
    original_missing = []
    if separators:
        after_lines = normalized_lines(after)
        new_separators = [i for i, line in enumerate(after_lines) if re.search('━{5,}', line)]
        original = lines[separators[-1] + 1:]
        new_original = after_lines[new_separators[-1] + 1:] if new_separators else []
        original_missing = [line for line in original if line not in set(new_original)]
        if not allow_all and (not new_separators or original_missing):
            reason = '원문 구분선이 사라집니다.' if not new_separators else '빠지는 원문 줄이 있어 거부합니다.'
            raise NotionError(reason + '\n' + loss_preview(original_missing))
    if lines and not allow_all and (not after.strip() or len(missing) > len(lines) / 2):
        raise NotionError(f'빈 값 또는 기존 줄의 절반 초과 삭제를 거부합니다. 빠지는 줄 {len(missing)}개:\n' + loss_preview(missing))
    return {'missing': missing, 'original_missing': original_missing}


def attachment_count(home, page_id, opener):
    """Count media in the page's block tree; incomplete traversal yields unknown, never zero."""
    pending = [(page_id, None)]; visited = set(); total = 0; scanned = 0
    try:
        while pending:
            parent, cursor = pending.pop()
            key = (parent, cursor)
            if key in visited or scanned >= 500 or len(visited) >= 500: return None
            visited.add(key)
            path = f'blocks/{parent}/children?page_size=100'
            if cursor: path += '&start_cursor=' + urllib.parse.quote(cursor, safe='')
            response = request(home, 'GET', path, opener=opener)
            blocks = response.get('results')
            if not isinstance(blocks, list) or scanned + len(blocks) > 500: return None
            scanned += len(blocks)
            for block in blocks:
                if not isinstance(block, dict) or not block.get('type'): return None
                if block.get('archived') or block.get('in_trash'): continue
                total += block['type'] in MEDIA_TYPES
                if block.get('has_children'):
                    if not block.get('id'): return None
                    pending.append((block['id'], None))
            if response.get('has_more'):
                if not isinstance(response.get('next_cursor'), str) or not response['next_cursor']: return None
                pending.append((parent, response['next_cursor']))
        return total
    except (NotionError, TypeError, ValueError): return None


_SNAPSHOT_LOCK = threading.RLock()
_SNAPSHOTS = {}
_SNAPSHOT_CALLS = {}


def call_digest(args):
    return hashlib.sha256(json.dumps({k: v for k, v in args.items() if k != '_approval_snapshot'},
                                     ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def page_fingerprint(page):
    data = {'properties': page.get('properties'), 'last_edited_time': page.get('last_edited_time'),
            'archived': page.get('archived', False), 'in_trash': page.get('in_trash', False)}
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def remember_snapshot(home, args, plans, call_id):
    # The second pre-tool hook transports this opaque receipt via the existing modify directive.
    with _SNAPSHOT_LOCK:
        for token, receipt in list(_SNAPSHOTS.items()):
            if time.time() - receipt['time'] > 3600: del _SNAPSHOTS[token]
        while len(_SNAPSHOTS) >= 256: del _SNAPSHOTS[next(iter(_SNAPSHOTS))]
        for key, token in list(_SNAPSHOT_CALLS.items()):
            if token not in _SNAPSHOTS: del _SNAPSHOT_CALLS[key]
        token = uuid.uuid4().hex; pages = snapshot_values(plans)
        _SNAPSHOTS[token] = {'home': str(home), 'digest': call_digest(args), 'pages': pages, 'time': time.time()}
        _SNAPSHOT_CALLS[(str(home), call_id or id(args))] = token


def approval_arguments(home, args, call_id=''):
    if args.get('action') in ('api', 'rollback'):
        return api_module().approval_arguments(api_context(), home, args, call_id)
    with _SNAPSHOT_LOCK:
        token = _SNAPSHOT_CALLS.pop((str(home), call_id or id(args)), None)
        receipt = _SNAPSHOTS.get(token)
        if receipt is None or receipt['digest'] != call_digest(args):
            return {'action': 'block', 'message': '승인 값 확인 정보를 전달하지 못했습니다. 다시 호출하세요.'}
        return {'action': 'modify', 'args': {'_approval_snapshot': token}}


def consume_snapshot(home, args):
    token = args.get('_approval_snapshot')
    if token is None: return None  # Direct unit/API calls still get execution-time checks.
    with _SNAPSHOT_LOCK:
        receipt = _SNAPSHOTS.pop(token, None) if isinstance(token, str) else None
        if (receipt is None or receipt['home'] != str(home) or receipt['digest'] != call_digest(args)
                or time.time() - receipt['time'] > 3600):
            raise NotionError('승인 값 확인 정보가 만료되었거나 일치하지 않습니다. 다시 읽고 승인받으세요.')
        return receipt['pages']
