"""Copy only the admitted event's files into the receiving profile's document cache.

This does not grant access to the sending profile's cache. Text, project goals and
old conversation paths are never attachment provenance. No file is executed.
"""
import dataclasses
import os
from pathlib import Path
import re
import stat
import uuid

MAX_FILES = 20
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_TOTAL_BYTES = 100 * 1024 * 1024
_CACHE_KINDS = frozenset({'images', 'documents', 'audio', 'videos'})


def _absolute(value):
    path = Path(str(value))
    if not path.is_absolute() or '..' in path.parts:
        raise ValueError('첨부 인계 경로를 확인하지 못했습니다.')
    return path


def _directory(path):
    """Open every component without following a symlink (including ancestors)."""
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in path.parts[1:]:
            child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _child(parent, name):
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent)
    except FileExistsError:
        pass
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)


def _snapshot(path, home):
    try:
        relative = path.relative_to(home / 'cache')
    except ValueError:
        raise ValueError('현재 요청의 원본 첨부 경로가 아닙니다.') from None
    if len(relative.parts) < 2 or relative.parts[0] not in _CACHE_KINDS:
        raise ValueError('현재 요청의 첨부 캐시 범위 밖입니다.')
    directory = _directory(path.parent)
    try:
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    finally:
        os.close(directory)
    with os.fdopen(fd, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode) or not 0 < before.st_size <= MAX_FILE_BYTES:
            raise ValueError('첨부 인계는 비어 있지 않은 20 MiB 이하 일반 파일만 지원합니다.')
        raw = stream.read(MAX_FILE_BYTES + 1)
        after = os.fstat(stream.fileno())
        identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        if len(raw) != before.st_size or identity(before) != identity(after):
            raise ValueError('읽는 동안 첨부가 바뀌어 인계를 중단했습니다.')
    return raw


def forward(event, source_home, target_home):
    """Return a new event with exact attachment paths rewritten; leave the source intact.

    Caller must have verified live owner admission and the receiving transport.
    All snapshots are validated before any destination file is created. Failure
    prevents the target handler from starting and removes this attempt's files.
    """
    urls = list(event.media_urls or [])
    if not urls:
        return event
    if len(urls) > MAX_FILES:
        raise ValueError('첨부는 한 요청에 20개까지만 인계합니다.')
    source, target = _absolute(source_home), _absolute(target_home)
    if source == target:
        raise ValueError('첨부 인계의 송신·수신 프로필이 같습니다.')
    snapshots = {}
    total = 0
    for raw in urls:
        if raw in snapshots:
            continue
        path = _absolute(raw)
        data = _snapshot(path, source)
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise ValueError('첨부 인계의 전체 크기는 100 MiB까지입니다.')
        snapshots[raw] = (path, data)
    # A supplied target home must already exist; never create another profile.
    fd = _directory(target)
    created = []
    mapping = {}
    try:
        for name in ('cache', 'documents'):
            child = _child(fd, name)
            os.close(fd)
            fd = child
        for raw, (path, data) in snapshots.items():
            suffix = path.suffix if re.fullmatch(r'\.[A-Za-z0-9]{1,12}', path.suffix) else '.bin'
            name = 'forward_' + uuid.uuid4().hex + suffix.lower()
            output = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd)
            created.append(name)
            with os.fdopen(output, 'wb') as stream:
                stream.write(data)
            mapping[raw] = str(target / 'cache' / 'documents' / name)
        # Only exact adapter-provided paths can change, including preprocessed
        # path notes. A similarly named old file or quoted path grants no access.
        expression = re.compile(r'(?<![\w/.-])(?:' + '|'.join(
            re.escape(raw) for raw in sorted(mapping, key=len, reverse=True)) + r')(?![\w/.-])')
        def rewrite(text):
            return expression.sub(lambda match: mapping[match.group()], text) if text else text
        return dataclasses.replace(event, media_urls=[mapping[raw] for raw in urls],
                                   text=rewrite(event.text), channel_context=rewrite(event.channel_context))
    except BaseException:
        for name in created:
            try:
                os.unlink(name, dir_fd=fd)
            except OSError:
                pass
        raise
    finally:
        os.close(fd)
