"""Read-only routing correction statistics for the root home's morning report."""
from pathlib import Path
import sqlite3
import logging

from ruamel.yaml import YAML

logger = logging.getLogger(__name__)


def root_home():
    from hermes_cli.profiles import _get_default_hermes_home
    return _get_default_hermes_home()


def section(home, now):
    if Path(home).resolve() != Path(root_home()).resolve():
        return ''
    try:
        config = YAML(typ="safe").load((Path(home) / 'config.yaml').read_text()) or {}
        router = config.get('gateway', {}).get('common_router', {})
        switch = router.get('route_switch', {})
        if not switch.get('enabled') or not switch.get('learning', {}).get('enabled'):
            return ''
        path = Path(router['claim_database_path']).expanduser().resolve()
        db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True, timeout=2)
        try:
            since = now.timestamp() - 86400
            counts = dict(db.execute("SELECT status, COUNT(*) FROM route_switch_offers WHERE resolved_at>=? "
                "AND status IN ('accepted','switched') GROUP BY status", (since,)))
            patterns = list(db.execute('SELECT from_reason, COUNT(*) FROM route_corrections WHERE created_at>=? '
                'GROUP BY from_reason HAVING COUNT(*)>=2 ORDER BY COUNT(*) DESC LIMIT 5', (since,)))
        finally:
            db.close()
        accepted, switched = counts.get('accepted', 0), counts.get('switched', 0)
        total = accepted + switched
        if not total:
            return ''
        lines = ['*🔵 라우팅 배정 (지난 24시간)*',
                 f'> ✓ 확인된 배정 정확도 {accepted / total:.0%} ({accepted}/{total}) · 담당 바꿈 {switched}건',
                 '> 다음 글은 맞음, 담당 변경은 틀림으로 집계해. 미응답은 제외해.']
        if patterns:
            lines.append('*라우팅 규칙 고칠 곳*')
            lines.extend(f'> • `{reason}` 배정에서 {count}회 교정 — 사례를 확인한 뒤 규칙 수정 검토' for reason, count in patterns)
            lines.append('> 정규식 규칙은 자동으로 고치지 않아.')
        return '\n'.join(lines)
    except (OSError, sqlite3.Error, KeyError, ValueError):
        logger.warning('Routing report unavailable', exc_info=True)
        return ''
