"""Owner-only watch inspection. Never runs the watcher or changes delivery state."""
import json
from pathlib import Path
import time

SCHEMA = {'name': 'news_watch_read', 'description': 'Read configured news sources, watch health and recent feed/X items without sending or scheduling anything.',
          'parameters': {'type': 'object', 'properties': {'action': {'type': 'string', 'enum': ['status', 'sources', 'recent']},
                         'source': {'type': 'string'}, 'limit': {'type': 'integer', 'minimum': 1, 'maximum': 20}},
                         'required': ['action'], 'additionalProperties': False}}


def handle(home, learning, args, session_id='', **_):
    try:
        learning.extension('learning_skills').human_context()
        learning.source(home, session_id, allow_approval=True)
        cfg = learning.extension('memory_recall').config(home).get('news_watch', {})
        if cfg.get('enabled') is not True:
            raise ValueError('소식 감시가 설정되지 않은 담당자다.')
        watch = learning.extension('news_watch')
        sources = cfg.get('sources', [])
        action = args.get('action')
        if action == 'sources':
            result = [{'id': watch.source_key(s), **{k: s.get(k) for k in ('type', 'name', 'account', 'url', 'interval_minutes')}} for s in sources]
        elif action == 'status':
            path = Path(home) / 'state/news-watch.json'
            state = json.loads(path.read_text()) if path.is_file() else {}
            health = {key: {k: value.get(k) for k in ('last_check', 'failures', 'alerted', 'error')}
                      for key, value in state.get('sources', {}).items() if isinstance(value, dict)}
            result = {'checked_at': time.time(), 'schedule': watch.schedule_status(cfg, time.time()),
                      'sources': health, 'notified_links': state.get('notified_links', [])[-30:],
                      'inflight_count': len(state.get('inflight', [])), 'notice': '저장된 감시 상태이며 실시간 연결 성공을 보장하지 않는다.'}
        elif action == 'recent':
            wanted = str(args.get('source') or '').removeprefix('@').lower()
            selected = wanted and next((s for s in sources if wanted in {watch.source_key(s).lower(), str(s.get('name') or '').lower(), str(s.get('account') or '').lower()}), None)
            if not selected:
                raise ValueError('sources에 표시된 설정 소스를 하나 지정해야 한다.')
            items = watch.fetch_x(Path(home), selected) if selected.get('type') == 'x' else watch.fetch(selected)
            result = [{k: row.get(k) for k in ('id', 'title', 'content_text', 'date_published', 'url')} for row in items[:max(1, min(int(args.get('limit', 5)), 20))]]
        else:
            raise ValueError('지원하지 않는 조회다.')
        return json.dumps({'ok': True, 'result': result, 'readonly': True}, ensure_ascii=False)
    except Exception as exc:
        return json.dumps({'ok': False, 'error': str(exc)[:300] if isinstance(exc, ValueError) else type(exc).__name__}, ensure_ascii=False)
