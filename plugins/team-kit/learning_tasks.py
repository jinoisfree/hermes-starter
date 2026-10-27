"""Optional profile-local task skill routing; absent settings preserve assistant behavior."""
from pathlib import Path
from collections import OrderedDict
import threading

_CONFIG = OrderedDict()
_LOCK = threading.RLock()

DEFAULTS = {"personal": "assistant-personal-work", "web": "assistant-web-research",
            "mail": "assistant-mail", "work": "assistant-task-workflow", "learn": "assistant-learning"}


def settings(home):
    from ruamel.yaml import YAML
    path = Path(home).resolve() / "config.yaml"
    with _LOCK:
        try:
            stat = path.stat()
            signature = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size, stat.st_ino)
        except FileNotFoundError:
            signature = None
        previous = _CONFIG.get(path)
        if previous is not None and previous[0] == signature:
            _CONFIG.move_to_end(path)
            return previous[1]
        config = (YAML(typ="safe").load(path.read_text()) or {}) if signature is not None else {}
        result = config.get("plugins", {}).get("entries", {}).get("team-kit", {}).get("settings", {}).get("learning", {})
        _CONFIG[path] = (signature, result)
        while len(_CONFIG) > 16:
            _CONFIG.popitem(last=False)
        return result


def mapping(config=None):
    config = config or {}
    if "task_skills" not in config:
        return dict(DEFAULTS)
    value = config["task_skills"]
    if not isinstance(value, dict):
        raise ValueError("learning.task_skills must be a role-to-skill map.")
    if any(role not in DEFAULTS or not isinstance(name, str) or not name.strip()
           for role, name in value.items()):
        raise ValueError("learning.task_skills needs known roles and nonempty skill names.")
    return dict(value)
