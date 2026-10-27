"""Compact tool routing; keep learned rules and customer policies in their own sections."""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
import logging
import sqlite3

logger = logging.getLogger(__name__)

BRIEFS = {
    'morning_report': '학습·보고 채널의 아침 보고 스레드나 소유자 개인 DM에서 아침 보고를 다시 요청하면 morning_report로 저장된 보고를 읽는다(kind=all, view=summary). SNS담당는 학습·답장 보고를 함께 읽는다. 종류·새 서식을 되묻거나 재발송하지 않는다. 기록·전달 여부를 구분하고 상세·검토는 원래 보고 스레드에서 한다.',
    'x_read': 'x_posts는 소유자 대화에서 공개 X 글·검색·타래를 읽는다. 최신 공지는 timeline만 보고 없다고 하지 말고 search(from:계정 since:날짜)·thread로 확인한다. 받은 날짜·조회 시각·캐시/잘림·최신성 한계를 밝히고 비공개 글은 읽지 않는다.',
}


class ToolNotes:
    """Group short tool hints into one frame, without spending the learning budget on headings."""
    def __init__(self, ctx):
        self.ctx = ctx
        self.notes = {}

    def __getattr__(self, name):
        return getattr(self.ctx, name)

    def register_system_prompt_section(self, name, content, **kwargs):
        # Preserve the full permission/link/edit instructions and dynamic deadlines.
        if name in ('team-kit.files', 'team-kit.notion', 'team-kit.deadline_watch'):
            return self.ctx.register_system_prompt_section(name, content, **kwargs)
        self.notes[name] = (content, kwargs.get('max_chars', 4000))

    def render(self, info=None, **_):
        parts = []
        for name, (content, limit) in sorted(self.notes.items()):
            try:
                original = content(info or {}) if callable(content) else content
                if not isinstance(original, str) or len(original.strip()) > limit:
                    raise ValueError('invalid tool note')
                if original.strip():
                    parts.append(BRIEFS.get(name.removeprefix('team-kit.'), original.strip()))
            except (Exception, SystemExit) as exc:
                logger.warning('team-kit tool note %s skipped: %s', name, type(exc).__name__)
        return '\n'.join(parts)

    def flush(self):
        if self.notes:
            self.ctx.register_system_prompt_section('team-kit.tools', self.render, max_chars=2500)


def missing_note(home, session_id, note, history=None):
    """Only bridge older sessions; never rewrite a cached prefix or trust user text as guidance.

    Use the actual replay history, not database messages that may have been pruned
    from the request. User-supplied text cannot stand in for host-injected context.
    """
    if not note:
        return ''
    for row in history or ():
        if not isinstance(row, dict) or row.get('role') != 'user':
            continue
        api, content = row.get('api_content'), row.get('content')
        if isinstance(api, str) and note in api and note not in str(content or ''):
            return ''
    path = Path(home).resolve() / 'state.db'
    if not session_id or not path.exists():
        return note
    try:
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
            db.execute('PRAGMA query_only=ON')
            row = db.execute('SELECT COALESCE(p.prompt,s.system_prompt) FROM sessions s '
                             'LEFT JOIN system_prompts p ON p.hash=s.system_prompt_hash WHERE s.id=?',
                             (session_id,)).fetchone()
            if row and note in (row[0] or ''):
                return ''
    except (OSError, sqlite3.Error):
        # Unavailable/older storage cannot prove delivery: keep the guidance.
        pass
    return note


def soul_section(home, section_id, text):
    """Use the identical fixed policy already loaded by SOUL; drift falls back in full."""
    if Path(home).name != 'work-assistant' or section_id not in {'team-kit.files', 'team-kit.learning'}:
        return text
    try:
        soul = (Path(home) / 'SOUL.md').read_text()
    except OSError:
        return text
    marker = f'<!-- fixed-policy:{section_id} -->\n{text.strip()}\n<!-- /fixed-policy:{section_id} -->'
    if text.strip() and marker in soul:
        return f'[{section_id}] SOUL의 동일한 고정 지침을 따른다. 승인·안전·학습 규칙은 그대로 적용한다.'
    return text
