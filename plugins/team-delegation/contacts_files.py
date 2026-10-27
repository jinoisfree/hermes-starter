"""Contact exports and recovery files, confined to work-assistant's configured file reach."""
from __future__ import annotations

import json
import os
from pathlib import Path


def roots() -> list[Path]:
    import hermes_yaml
    config = Path.home() / '.hermes/profiles/work-assistant/config.yaml'
    data = hermes_yaml.safe_load(config.read_text())
    settings = data['plugins']['entries']['team-kit']['settings']['files']
    if settings.get('enabled') is not True:
        raise ValueError('file tools disabled')
    # The same enabled, existing, absolute folders as team-kit.files.folders.
    return [p.resolve(strict=True) for item in settings.get('folders', [])
            if (p := Path(str(item)).expanduser()).is_absolute() and p.is_dir()]


def storage(*, backup: bool = False) -> Path:
    allowed = roots()
    candidates = [p if p.name == '999. work-assistant' else p / '999. work-assistant' for p in allowed]
    if not candidates:
        raise ValueError('no configured file folder')
    folder = candidates[0] / '연락처'
    if backup:
        folder /= '백업'
    resolved = folder.resolve()
    if not any(resolved.is_relative_to(root) for root in allowed):
        raise ValueError('output folder outside configured roots')
    folder.mkdir(parents=True, exist_ok=True)
    if folder.resolve(strict=True) != resolved:
        raise ValueError('output folder changed')
    return resolved


def filename(value: str, extension: str) -> str:
    # User supplied names never become paths. Reject instead of silently dropping traversal.
    if not value or value in {'.', '..'} or any(c in value for c in '/\\\x00'):
        raise ValueError('invalid file name')
    stem = value.removesuffix(extension).strip()[:160]
    if not stem or stem in {'.', '..'}:
        raise ValueError('invalid file name')
    return stem + extension


def write_unique(folder: Path, name: str, data: bytes) -> Path:
    """O_EXCL + O_NOFOLLOW prevents overwriting existing files, including dangling links."""
    for index in range(10000):
        initial = Path(name)
        path = folder / (name if index == 0 else f'{initial.stem}_{index}{initial.suffix}')
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            continue
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return path
    raise ValueError('too many filename collisions')


def import_path(value: str) -> Path:
    path = Path(value).expanduser().resolve(strict=True)
    if not path.is_file() or path.suffix.lower() != '.vcf' or not any(path.is_relative_to(r) for r in roots()):
        raise ValueError('input outside configured roots or not a vCard')
    if not 0 < path.stat().st_size <= 5 * 1024 * 1024:
        raise ValueError('vCard must be between 1 byte and 5 MiB')
    return path


def group_backup(group: dict) -> bytes:
    return json.dumps({'identifier': group['identifier'], 'name': group['name'],
                       'member_ids': sorted(group['member_ids'])}, ensure_ascii=False, indent=2).encode()
