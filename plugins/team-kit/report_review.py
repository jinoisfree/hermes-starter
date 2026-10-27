"""Owner report decisions: deterministic button dispatch, session-verified corrections."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
import re
import time
from types import SimpleNamespace

NOTE = '''학습·스킬 보고 검토: 전체 버튼 모두 배우기=이 보고의 학습+스킬 전체 keep, 롤백=전체 reject,
일부만 배우기=항목별 버튼 안내만(변경 없음). 학습 N번 승인/거절=team_learn_review keep/reject items=[N],
스킬 N번 승인/거절=team_skill_review keep/reject items=[N]. 수정 버튼은 무엇을 고칠지 먼저 묻는다.
후속 수정 지시를 받으면 학습은 team_learn_review correct items=[N](rule/operations),
스킬은 team_skill_review correct items=[N] operations(기존 skill_manage의 정확한 patch)로 처리한다.
학습승인/학습거절/보류와 번호별 글 답장은 유지한다. 결정만 기록된 경우와 파일까지 바꾼 결과를 구분한다.
'''
LABEL = re.compile(r'^(학습|스킬) (\d+)번 (승인|거절|수정)$')
GLOBAL = {'모두 배우기': 'keep', '롤백': 'reject', '일부만 배우기': 'choose',
          '학습승인': 'keep', '학습거절': 'reject', '보류': 'defer'}


def stores(home, base):
    skill = base.skill_report()
    return [(base.path(home), base.load), (skill.path(home), skill.load)]


def find(home, src, base):
    if src.get('user_id') != base.owner(home) or not base.learning.owner_report_place(home, src):
        raise ValueError('소유자의 학습 채널 보고 또는 DM 스레드에서만 검토할 수 있어.')
    for store, loader in stores(home, base):
        for key, report in loader(home)['reports'].items():
            if (report.get('review_version') == 2 and report.get('thread_ts') == src.get('thread_id')
                    and report.get('channel') == src.get('chat_id') and report.get('owner') == src.get('user_id')):
                return store, loader, key, report
    raise ValueError('새 검토 버튼이 있는 원래 보고 스레드에서 사용해 줘.')


def parse(text):
    if text in GLOBAL:
        return 'all', None, GLOBAL[text]
    match = LABEL.fullmatch(text)
    if match:
        return ('learning' if match[1] == '학습' else 'skill', [int(match[2])],
                {'승인': 'keep', '거절': 'reject', '수정': 'ask'}[match[3]])
    return None


def pending(home, src, base):
    try:
        _, _, _, report = find(home, src, base)
        item = report.get('pending_correction')
        return (f"\n현재 이 보고의 {'학습' if item['scope'] == 'learning' else '스킬'} {item['number']}번 수정 내용을 기다리고 있어. "
                "이번 소유자 답변을 확인한 뒤 해당 correct 도구를 호출하고 실제 결과를 보고해."
                if item else '')
    except (ValueError, OSError, KeyError):
        return ''


def scoped(home, callback):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    token = set_hermes_home_override(home)
    try:
        return callback()
    finally:
        reset_hermes_home_override(token)


SHARED_PREFIX = 'secretary/'
SHARED_PROFILES = ('personal-secretary', 'work-assistant')


def shared_copies(home, relative):
    """Common secretary skills: the shared source and every secretary's copy (hermes-secretary-skills)."""
    home = Path(home)
    if not relative.startswith(SHARED_PREFIX) or home.parent.name != 'profiles' or home.name not in SHARED_PROFILES:
        return []
    root = home.parent.parent
    return [root / 'shared' / 'secretary-skills' / relative[len(SHARED_PREFIX):]] + [
        root / 'profiles' / name / 'skills' / relative for name in SHARED_PROFILES if name != home.name]


def spread_shared(home, file, relative, docs):
    """After a rollback or correction, give the shared source and the other copy the same text."""
    for target in shared_copies(home, relative):
        if target.is_symlink() or any(parent.is_symlink() for parent in target.parents):
            raise ValueError('공통 스킬 경로가 심링크라 맞추지 않았어.')
        docs.atomic_document(target, file.read_text(encoding='utf-8'))


def skill_plan(home, item, action, previous, base):
    skill = base.skill_report()
    docs = base.learning.extension('learning_skills')
    file = docs.document(home, item['relative'])
    # A repeated click records no additional file action.
    if action in {'keep', 'reject'} and previous.get('status') == {'keep': 'confirmed', 'reject': 'rejected'}[action]:
        return None
    if action == 'reject' and item['change'] != '새로 만듦' and file.is_file():
        baseline = skill.checked_original(item.get('before'))
        # The other secretary already rolled this shared skill back: same original, nothing to overwrite.
        if baseline is not None and shared_copies(home, item['relative']) and \
                file.read_bytes() == baseline.encode():
            return file, baseline
    if action == 'reject' and item['change'] == '새로 만듦' and shared_copies(home, item['relative']):
        raise ValueError(f"{item['name']}은 두 비서가 함께 쓰는 새 공통 스킬이라 여기서 보관하지 않았어. "
                         "~/.hermes/shared/secretary-skills에서 정리해야 해.")
    expected = previous.get('current_sha256') or item.get('after', {}).get('sha256')
    if not expected or not file.is_file() or skill.hashlib.sha256(file.read_bytes()).hexdigest() != expected:
        raise ValueError(f"{item['name']}은 보고 이후 바뀌었거나 종료 원문이 없어. 덮어쓰지 않았어.")
    current = next((s for s in skill.skills(home) if s['relative'] == item['relative'] and s['name'] == item['name']), None)
    if not current or not current['custom']:
        raise ValueError('직접 만든 활성 스킬만 이 보고에서 고칠 수 있어.')
    if action == 'reject' and item['change'] != '새로 만듦':
        baseline = skill.checked_original(item.get('before'))
        if baseline is None:
            raise ValueError(f"{item['name']}은 대상일 자정 원문이 없어 롤백하지 않았어 (source=missing).")
        return file, baseline
    return file, None


def skill_decisions(home, args, src, base):
    L = base.learning
    store, loader, key, _ = find(home, src, base)
    action = args['action']
    if action not in {'list', 'keep', 'reject', 'correct', 'defer'}:
        raise ValueError('Use list/keep/reject/correct/defer.')
    L.extension('learning_skills').human_context()
    if action != 'list':
        L.validate_quote(src, args.get('user_said', ''))
    with L.locked(store):
        state = loader(home)
        report = state['reports'][key]
        items = report.get('skills', {}).get('items', [])
        numbers = args.get('items')
        if numbers is None:
            numbers = list(range(1, len(items) + 1))
        if (not isinstance(numbers, list) or not numbers or any(type(n) is not int or not 1 <= n <= len(items) for n in numbers)
                or action == 'correct' and len(numbers) != 1):
            raise ValueError('유효한 스킬 번호를 지정해 줘. 수정은 한 항목씩 해.')
        numbers = list(dict.fromkeys(numbers))
        if action == 'list':
            return {'ok': True, 'items': [{'item': n, 'reported': items[n-1],
                     'review': report.get('skill_review_state', {}).get(str(n), {})} for n in numbers]}
        results = []
        names = {items[n-1]['name'] for n in numbers}
        docs = L.extension('learning_skills')
        with docs.skill_locks(home, names):
            # Validate every file and every restore original before the first write.
            plans = {n: skill_plan(home, items[n-1], action,
                     report.get('skill_review_state', {}).get(str(n), {}), base) for n in numbers}
            for n in numbers:
                item, plan = items[n-1], plans[n]
                if plan and action == 'reject':
                    if item['change'] == '새로 만듦':
                        from tools.skill_usage import archive_skill
                        ok, message = scoped(home, lambda: archive_skill(item['name']))
                        if not ok:
                            raise ValueError(message)
                    else:
                        docs.atomic_document(plan[0], plan[1])
                        spread_shared(home, plan[0], item['relative'], docs)
                elif plan and action == 'correct':
                    operations = args.get('operations')
                    if (not isinstance(operations, list) or not operations or len(operations) > 8 or
                            any(not isinstance(op, dict) or op.get('action') != 'patch' or
                                op.get('name', item['name']) != item['name'] or
                                op.get('file_path', 'SKILL.md') != 'SKILL.md' for op in operations)):
                        raise ValueError('보고된 SKILL.md만 기존 skill_manage patch로 수정할 수 있어.')
                    from tools import skill_manager_tool as manager
                    # The owner already supplied the correction in this verified report thread.
                    # Bypass only the core's redundant staging gate, never its path/content guards.
                    def edit():
                        token = manager._skill_gate_bypass.set(True)
                        try:
                            return json.loads(manager.skill_manage(action='patch', name=item['name'],
                                              operations=operations, session_id=src.get('session_id', '')))
                        finally:
                            manager._skill_gate_bypass.reset(token)
                    result = scoped(home, edit)
                    if not result.get('success') or result.get('staged'):
                        raise ValueError(result.get('error') or result.get('message') or '스킬 수정을 완료하지 못했어.')
                    spread_shared(home, plan[0], item['relative'], docs)
                status = {'keep': 'confirmed', 'reject': 'rejected', 'correct': 'corrected', 'defer': 'deferred'}[action]
                decision = {'status': status, 'at': time.time(), 'user_said': args['user_said'],
                            'source': {k: v for k, v in src.items() if k != 'text'}}
                file = docs.document(home, item['relative'])
                if file.is_file():
                    decision['current_sha256'] = base.skill_report().hashlib.sha256(file.read_bytes()).hexdigest()
                report.setdefault('skill_review_state', {})[str(n)] = decision
                report.pop('pending_correction', None)
                L.save(store, state)
                results.append({'item': n, 'status': status})
        if action in {'reject', 'correct'}:
            docs.invalidate_skills()
    return {'ok': True, 'results': results, 'status_updated': base.update_status(home, src['thread_id'], store=store, loader=loader)}


def decide(home, args, src, base):
    store, loader, key, report = find(home, src, base)
    action, scope, numbers = args['action'], args.get('scope', 'all'), args.get('items')
    if action == 'choose':
        return {'ok': True, 'message': '아래 스레드의 학습·스킬 항목마다 승인·거절·수정 버튼을 눌러 줘. 아직 바꾼 내용은 없어.'}
    if action == 'ask':
        items = report.get('items', []) if scope == 'learning' else report.get('skills', {}).get('items', [])
        if not numbers or len(numbers) != 1 or not 1 <= numbers[0] <= len(items):
            raise ValueError('수정할 항목 번호를 확인해 줘.')
        with base.learning.locked(store):
            state = loader(home)
            state['reports'][key]['pending_correction'] = {'scope': scope, 'number': numbers[0]}
            base.learning.save(store, state)
        return {'ok': True, 'message': f"{'학습' if scope == 'learning' else '스킬'} {numbers[0]}번에서 무엇을 어떻게 고칠까? 바꿀 내용을 답해 줘."}
    results = []
    if scope in {'all', 'learning'} and report.get('items'):
        if store != base.path(home):
            raise ValueError('학습 보고를 확인하지 못했어.')
        learning_args = dict(args)
        learning_args.pop('scope', None)
        result = base.learning.extension('learning_review').review(home, learning_args, src.get('session_id', ''), base.learning,
                 SimpleNamespace(owner=base.owner, path=base.path, load=base.load,
                                 record_review=base.record_review, update_status=base.update_status), _source=src)
        results.append(result)
    if scope in {'all', 'skill'} and report.get('skills', {}).get('items'):
        results.append(skill_decisions(home, args, src, base))
    if not results:
        raise ValueError('검토할 항목이 없어.')
    return {'ok': True, 'results': results, 'message': '검토 결정을 반영했어. 원래 보고의 상태를 확인해 줘.'}


def handle(home, args, base, session_id='', **_):
    try:
        src = base.learning.source(home, session_id, allow_approval=True)
        return json.dumps(skill_decisions(home, args, src, base), ensure_ascii=False)
    except Exception as exc:
        return json.dumps({'ok': False, 'error': str(exc) if isinstance(exc, ValueError) else type(exc).__name__}, ensure_ascii=False)


async def dispatch(home, base, event=None, **_):
    source = getattr(event, 'source', None)
    if str(getattr(getattr(source, 'platform', None), 'value', '')) != 'slack':
        return None
    text = str(getattr(event, 'text', '') or '').strip()
    command = parse(text)
    if not command:
        return None
    src = {'text': text, 'user_id': getattr(source, 'user_id', ''), 'chat_id': getattr(source, 'chat_id', ''),
           'chat_type': 'dm' if str(getattr(source, 'chat_id', '')).startswith('D') else 'group',
           'thread_id': getattr(source, 'thread_id', ''), 'platform_message_id': getattr(event, 'message_id', ''), 'at': time.time()}
    try:
        store, loader, _, report = find(home, src, base)
    except (ValueError, OSError, KeyError):
        return None
    # A synthetic click must originate in this report's own head or matching item part.
    button = (getattr(event, 'raw_message', None) or {}).get('_hermes_button')
    if button:
        part = next((p for p in report['parts'] if p.get('ts') == button.get('message_ts')), None)
        kind = '학습' if command[0] == 'learning' else '스킬'
        if (not part or button.get('label') != text or
                (command[0] == 'all' and button.get('message_ts') != report['thread_ts']) or
                (command[0] != 'all' and (part.get('review_kind') != kind or part.get('item') != command[1][0]))):
            return {'action': 'skip', 'reason': 'report button source mismatch'}
    scope, numbers, action = command
    try:
        result = await asyncio.to_thread(decide, home, {'scope': scope, 'items': numbers,
                           'action': action, 'user_said': text}, src, base)
        answer = result['message']
    except Exception as exc:
        answer = '검토를 끝내지 못했어. ' + (str(exc) if isinstance(exc, ValueError) else type(exc).__name__) + ' 반영된 항목은 원래 보고에 남아 있어.'
        # The click already marked the report answered; show its real state (and buttons) again.
        try:
            await asyncio.to_thread(lambda: base.update_status(home, src['thread_id'], store=store, loader=loader))
        except Exception:
            pass
    try:
        await asyncio.to_thread(lambda: base.slack(home, 'chat.postMessage', {'channel': src['chat_id'], 'thread_ts': src['thread_id'],
                               'text': '*🟡 보고 검토*\n' + answer}))
    except Exception as exc:
        logging.getLogger(__name__).warning('Report decision reply failed (%s)', type(exc).__name__)
    return {'action': 'skip', 'reason': 'owner report decision handled by plugin'}


SCHEMA = {'name': 'team_skill_review', 'description': NOTE + '이 도구는 스킬 번호만 받는다. list로 원문·현재 검토 상태를 확인한다. 고친 스킬 거절은 자정 원문 복원, 새 스킬 거절은 보관이다. 이후 변경이 있으면 거부한다.',
          'parameters': {'type': 'object', 'properties': {
              'action': {'type': 'string', 'enum': ['list', 'keep', 'reject', 'correct', 'defer']},
              'items': {'type': 'array', 'items': {'type': 'integer'}, 'description': '스킬 N번의 1-based 번호. 생략은 전체 스킬.'},
              'user_said': {'type': 'string', 'description': '현재 소유자의 정확한 수정·검토 지시 인용.'},
              'operations': {'type': 'array', 'items': {'type': 'object', 'properties': {
                  'action': {'type': 'string', 'enum': ['patch']}, 'old_string': {'type': 'string'},
                  'new_string': {'type': 'string'}, 'file_path': {'type': 'string', 'enum': ['SKILL.md']}},
                  'required': ['action', 'old_string', 'new_string'], 'additionalProperties': False}}},
              'required': ['action'], 'additionalProperties': False}}
