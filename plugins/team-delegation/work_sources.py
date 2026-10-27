"""Company-only adapters for the owner's linked-work synchronization exception."""
from __future__ import annotations
import json
import re
import subprocess
from datetime import datetime, timedelta

import work_content as content
import deadline_time as dates
import source_lookup

TEXT_BLOCKS = {'paragraph', 'heading_1', 'heading_2', 'heading_3', 'bulleted_list_item',
               'numbered_list_item', 'quote', 'callout', 'toggle', 'code', 'table_row'}


def ordered_blocks(blocks):
    """NotionReader fetches descendants in batches; recover document order from their parents."""
    ids = {row['id'] for row in blocks}
    children = {}
    for row in blocks:
        parent = row.get('parent', {}).get('block_id')
        children.setdefault(parent if parent in ids else None, []).append(row)
    def visit(parent):
        for row in children.get(parent, []):
            yield row
            yield from visit(row['id'])
    return list(visit(None))


def body_text(block):
    if block['type'] == 'table_row':
        return '\t'.join(content.rich_text({'rich_text': cell}) for cell in block['table_row']['cells']).rstrip('\t')
    return content.rich_text(block[block['type']])


def rich(value):
    return [{'type': 'text', 'text': {'content': value[i:i+1900]}} for i in range(0, len(value), 1900)]


def stable_notion(value):
    """Notion returns a fresh request_id and freshly signed file URLs on every read.

    Drop the request_id and keep only the file path (its identity) so an unchanged page keeps
    one revision; otherwise every pre-write check sees a change and the write never runs.
    """
    if isinstance(value, dict):
        if set(value) == {'url', 'expiry_time'}:
            return {'url': str(value['url']).split('?', 1)[0]}
        return {key: stable_notion(item) for key, item in value.items() if key != 'request_id'}
    if isinstance(value, list):
        return [stable_notion(item) for item in value]
    return value


class Sources:
    def __init__(self, home, *, reader=None, notes=None, calendar=None):
        import notion_completion_sync as sync
        self.home = home
        self.reader = reader or sync.NotionReader(home)
        self.notes = notes or sync.Notes()
        self.calendar = calendar or sync.Calendar()
        self.guard = sync.company_scope_guard
        self.database = sync.DATABASE
        self.aliases = {}

    def references(self, item):
        refs = [('notion', r['external_id']) for r in item.get('knowledge_refs', []) if r['ref_type'] == 'notion_page' and r.get('metadata',{}).get('link_state',{}).get('status','ok')=='ok']
        refs += [(s['domain'], s['source_id']) for s in item.get('sources', []) if s['domain'] in {'notes', 'calendar'} and s.get('metadata',{}).get('link_state',{}).get('status','ok')=='ok']
        return refs

    def read(self, kind, identifier):
        identifier = self.aliases.get(identifier, identifier)
        if kind == 'notion':
            page = self.reader.page(identifier)
            if str(page.get('parent', {}).get('database_id', '')).replace('-', '').lower() != self.database:
                raise source_lookup.SourceReadError('out_of_scope', 'company_notion_database_required')
            from notion_completion_sync import page_title
            blocks = ordered_blocks(self.reader.blocks(identifier))
            tasks, lines = [], []
            for block in blocks:
                key = block.get('type')
                if block.get('archived') or block.get('in_trash'):
                    continue
                if key == 'to_do':
                    tasks.append(content.task(content.rich_text(block[key]), done=block[key].get('checked'), identifier=block['id']))
                elif key in TEXT_BLOCKS:
                    line = body_text(block)
                    if key != 'table_row' or line.strip():
                        lines.append(line)
            props = page.get('properties', {})
            status = props.get('상태', {})
            status_name = (status.get('status') or status.get('select') or {}).get('name')
            if status_name == 'Done':
                for row in tasks:
                    row['done'] = True
            due = (props.get('다음 마감', {}).get('date') or {}).get('start')
            if len(tasks) == 1 and not tasks[0].get('due') and due:
                tasks[0]['due'] = dates.canonical(due)
            closed = bool(page.get('in_trash') or page.get('archived') or status_name == 'Cancelled')
            result = {'content': {'title': page_title(page)[1], 'body': '\n'.join(lines).strip(), 'tasks': tasks},
                      'raw': {'page': page, 'blocks': blocks}, 'closed': closed}
        elif kind == 'notes':
            self.guard('notes', {'action': 'update', 'note_id': identifier}, strict=True)
            raw = self.notes.current(identifier)
            if not raw:
                raise source_lookup.SourceReadError('transient', 'note_read_unclassified')
            try:
                _, summary, _ = self.notes.tools()._linked_note_sections(raw['body_html'])
                lines = []
                for line in summary.splitlines():
                    hit = re.match(r'\s*- ([^:]+):\s*(.*?)\s*\|\s*(.*)', line)
                    if not hit:
                        raise ValueError('note_summary_incomplete')
                    section, label, value = hit.groups()
                    label = re.sub(r'^(\[[ xX]\]\s*)?\{fmt:[^}]+\}\s*', lambda m: m[1] or '', label)
                    if section == '할 일' and label.strip() == '다음 마감':
                        # A display row of the nearest deadline, not a task (2026-10-26 duplicate reminder).
                        lines.append(label+': '+value)
                    elif section == '할 일':
                        mark = re.match(r'^(\[[ xX]\])\s*(.*)', label)
                        lines.append((mark[1]+' '+mark[2] if mark else '[ ] '+label)+' | '+value)
                    else:
                        lines.append(value if section == '본문' else label+': '+value)
                parsed = content.from_text(raw['title'], '\n'.join(lines))
            except (KeyError, ValueError):
                text = str(raw.get('plaintext') or '')
                text = text.partition('\n')[2] if text.splitlines()[:1] == [raw.get('title')] else text
                # 진행도 (work_completion) is a status mark, not content: its own line or the end of 생성:.
                text = re.sub(r'\s*· 진행도: (진행중|완료)', '', text)
                text = '\n'.join(l for l in text.splitlines() if not re.fullmatch(r'진행도: (진행중|완료)', l.strip()))
                parsed = content.from_text(raw['title'], text)
            result = {'content': parsed, 'raw': raw, 'closed': False}
        elif kind == 'calendar':
            if identifier.startswith('google:'):
                raise source_lookup.SourceReadError('out_of_scope', 'google_calendar_source')
            if hasattr(self.calendar, 'lookup'):
                raw = source_lookup.require(self.calendar.lookup(identifier))
            else:
                raw = self.calendar.get(identifier)
                if not raw:
                    raise source_lookup.SourceReadError('transient', 'calendar_read_unclassified')
                if raw.get('recurring'):
                    raise source_lookup.SourceReadError('unsupported', 'recurring_calendar')
            self.guard('calendar', {'action': 'update', 'event_id': identifier, 'expected_start': raw['start']}, strict=True)
            from work_completion import strip_mark, split_progress
            parsed = content.from_text(strip_mark(raw['title']), split_progress(raw.get('notes') or '')[1])
            due = content.next_due(parsed)
            if due and dates.canonical(raw['start'], all_day=raw.get('isAllDay', raw.get('allDay')) is True) != due:
                # A calendar move changes only tasks that shared the earliest deadline.
                start = dates.canonical(raw['start'], all_day=raw.get('isAllDay', raw.get('allDay')) is True)
                for row in parsed['tasks']:
                    if row.get('due') == due:
                        row['due'] = start
            result = {'content': parsed, 'raw': raw, 'closed': False}
        else:
            raise source_lookup.SourceReadError('unsupported', 'unsupported_work_source')
        result['revision'] = content.digest(stable_notion(result['raw']) if kind == 'notion' else result['raw'])
        return result

    def structured(self, row):
        try:
            return content.structured_summary(self.notes.tools()._linked_note_sections(row['raw']['body_html'])[1])
        except (KeyError, ValueError, TypeError):
            return False

    def preflight(self, kind, identifier, before, desired):
        current = self.read(kind, identifier)
        if current['closed'] or current['revision'] != before['revision']:
            raise ValueError('work_original_changed_before_write')
        if kind == 'notes':
            # Existing structured summary/source and original attachment export are prerequisites.
            _, summary, _ = self.notes.tools()._linked_note_sections(before['raw']['body_html'])
            if content.structured_summary(summary) and (
                    desired['body'] != before['content']['body']
                    or [t['text'] for t in desired['tasks']] != [t['text'] for t in before['content']['tasks']]
                    or content.flip_marks(summary, desired['tasks']) is None):
                raise ValueError('note_structure_preserved')
        if any(row.get('ambiguous') for row in desired['tasks']):
            raise ValueError('ambiguous_task_deadline')

    def apply(self, kind, identifier, before, desired):
        self.preflight(kind, identifier, before, desired)
        if kind == 'notion':
            self._notion(identifier, before, desired)
        elif kind == 'calendar':
            self._calendar(identifier, before, desired)
        else:
            self._notes(identifier, before, desired)
        after = self.read(kind, identifier)
        if content.semantic(after['content']) != content.semantic(desired):
            raise RuntimeError('work_content_write_unverified')
        return after

    def _notion(self, identifier, before, desired):
        from notion_completion_sync import page_title
        name, old_title = page_title(before['raw']['page'])
        tasks_changed = content.semantic(before['content'])['tasks'] != desired['tasks']
        props = {}
        if tasks_changed:
            props['다음 마감'] = {'date': {'start': content.next_due(desired)} if content.next_due(desired) else None}
        if old_title != desired['title']:
            props[name] = {'title': rich(desired['title'])}
        if props:
            self.reader.request('PATCH', f'pages/{identifier}', {'properties': props})
        old_tasks = before['content']['tasks'] if tasks_changed else []
        desired_tasks = content.reconcile([dict(r, key=r['source_id']) for r in old_tasks], desired['tasks']) if tasks_changed else []
        wanted = {r['key'] for r in desired_tasks}
        for row in old_tasks:
            if row['source_id'] not in wanted:
                self.reader.request('DELETE', 'blocks/'+row['source_id'])
        append = []
        for row in desired_tasks:
            text = row['text'] + (' | '+row['due'] if row.get('due') else '')
            block = {'rich_text': rich(text), 'checked': row['done']}
            if any(r['source_id'] == row['key'] for r in old_tasks):
                self.reader.request('PATCH', 'blocks/'+row['key'], {'to_do': block})
            else:
                append.append({'object': 'block', 'type': 'to_do', 'to_do': block})
        if before['content']['body'] != desired['body']:
            old = [b for b in before['raw']['blocks'] if b.get('type') in TEXT_BLOCKS and not b.get('archived')
                   and (b['type'] != 'table_row' or body_text(b).strip())]
            lines = desired['body'].splitlines()
            for i, block in enumerate(old):
                if block['type'] == 'table_row':
                    width = len(block['table_row']['cells'])
                    cells = lines[i].split('\t') if i < len(lines) else []
                    if len(cells) > width:
                        cells = [lines[i]]
                    cells += [''] * (width-len(cells))
                    self.reader.request('PATCH', 'blocks/'+block['id'], {'table_row': {'cells': [rich(cell) for cell in cells]}})
                elif i < len(lines):
                    data = {'rich_text': rich(lines[i])}
                    if block['type'] == 'code':
                        data['language'] = block['code'].get('language', 'plain text')
                    self.reader.request('PATCH', 'blocks/'+block['id'], {block['type']: data})
                else:
                    self.reader.request('DELETE', 'blocks/'+block['id'])
            append += [{'object': 'block', 'type': 'paragraph', 'paragraph': {'rich_text': rich(line)}} for line in lines[len(old):]]
        for offset in range(0, len(append), 100):
            self.reader.request('PATCH', f'blocks/{identifier}/children', {'children': append[offset:offset+100]})

    def _calendar(self, identifier, before, desired):
        tools, event = self.calendar.tools(), before['raw']
        from work_completion import DONE_MARK, split_progress, with_progress
        mark = DONE_MARK if str(event['title']).startswith(DONE_MARK) else ''
        progress = split_progress(event.get('notes') or '')[0]
        args = {'action': 'update', 'event_id': identifier, 'expected_title': event['title'],
                'expected_start': event['start'], 'title': mark + desired['title'],
                'notes': with_progress(content.render(desired), progress), 'span': 'this'}
        if all(before['content'][field] == desired[field] for field in ('body', 'tasks')):
            args['notes'] = event.get('notes') or ''
        due = content.next_due(desired)
        if due:
            start = due if len(due) > 10 else due+'T00:00:00+09:00'
            delta = datetime.fromisoformat(event['end']) - datetime.fromisoformat(event['start'])
            args.update(start=start, end=(datetime.fromisoformat(start)+delta).isoformat(), all_day=len(due)==10)
        binary = tools._calendar_cache_root(tools.PERSONAL_PROFILE)/'eventkit_calendar_mutation-v1'
        result = subprocess.run([str(binary), json.dumps(args, ensure_ascii=False)], capture_output=True, text=True, timeout=30)
        # The EventKit helper reports {action, before, after}; it has no "ok" field. apply() re-reads to verify.
        try:
            after = json.loads(result.stdout).get('after') if not result.returncode else None
        except ValueError:
            after = None
        if not isinstance(after, dict):
            raise RuntimeError('calendar_work_write_uncertain')

    def _notes(self, identifier, before, desired):
        tools = self.notes.tools()
        preview, original_summary, original = tools._linked_note_sections(before['raw']['body_html'])
        summary = [f'- 본문: 줄 {i+1} | {line}' for i, line in enumerate(desired['body'].splitlines())]
        summary += ['- 할 일: '+('[x] ' if r['done'] else '[ ] ')+r['text']+' | '+(r.get('due') or '') for r in desired['tasks']]
        if not summary:
            summary = ['- 본문: 내용 | ']
        if all(before['content'][field] == desired[field] for field in ('body', 'tasks')):
            summary = original_summary.splitlines()
        elif content.structured_summary(original_summary):
            # Only completion marks change in a structured memo; preflight guaranteed they align.
            summary = content.flip_marks(original_summary, desired['tasks']).splitlines()
        result = json.loads(tools._apple_notes_direct_mutation({
            'action': 'update', 'note_id': identifier, 'title': desired['title'], 'rename_title': True,
            'preview_summary': preview, 'summary': '\n'.join(summary), 'approved': True,
            '_skip_common_work': True, 'mixed_mode': 'rebuild'},
            _linked_title_authority=tools._LINKED_TITLE_AUTHORITY, _linked_title_before=before['raw']))
        if not result.get('ok') or result.get('result', {}).get('content_loss'):
            raise RuntimeError('note_work_write_uncertain')
        actual_id = result.get('target_id') or result.get('result', {}).get('note_id') or identifier
        if actual_id != identifier:
            self.aliases[identifier] = actual_id
        after = self.notes.current(actual_id)
        if not after or sorted(after.get('attachment_names') or []) != sorted(before['raw'].get('attachment_names') or []):
            raise RuntimeError('note_attachment_preservation_unverified')
        if tools._linked_note_sections(after['body_html'])[2] != original:
            raise RuntimeError('note_original_preservation_unverified')
