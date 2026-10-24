"""Verified fresh-note reconstruction; never rewrite an attachment-bearing original."""
from collections import Counter
import html
import json
import re
from pathlib import Path
import subprocess
import time
from urllib.parse import unquote


def names(paths):
    return Counter(Path(unquote(str(p))).name for p in paths)


def text(value):
    return ''.join(str(value or '').replace('\ufffc', '').split())


def rebuild(td, *, note_id, folder, title, body, files, existing, before, args, runner, exported=False, append=False):
    if not folder or not before or not isinstance(before.get('plaintext'), str):
        return {'ok': False, 'error': 'APPLE_NOTES_REBUILD_SNAPSHOT_REQUIRED'}
    native = before.get('attachment_names')
    # Exported originals were copied out of this very note, so their names need not match.
    if not isinstance(native, list) or (not exported and (any(not n for n in native) or names(native) != names(existing))):
        return {'ok': False, 'error': 'APPLE_NOTES_REBUILD_ORIGINAL_FILES_REQUIRED'}
    if any(not Path(p).is_file() for p in existing):
        return {'ok': False, 'error': 'APPLE_NOTES_REBUILD_ORIGINAL_FILES_REQUIRED'}
    script = str(Path(td.__file__).with_name('apple_notes_mutation.applescript'))
    def mutate(action, identifier, content='', attachments=()):
        p = runner(['/usr/bin/osascript', script, action, identifier, folder, '', content, *attachments],
                   capture_output=True, text=True, timeout=60, check=False)
        if p.returncode:
            raise RuntimeError('APPLE_NOTES_REBUILD_NATIVE_FAILED')
        return str(p.stdout).strip().split('\t')[0]
    # Keep the full old plain text as well as the requested new rendering. Native
    # attachment placeholders are not text and are replaced by the actual files.
    old_text = before['plaintext'].replace('\ufffc', '')
    # Notes leaves table cells out of plaintext, so carry the old body HTML itself
    # (tables, formatting) minus the inline photos that are re-attached below.
    old_html = str(before.get('body_html') or '')
    old_tables = old_html.count('<table')
    carried = re.sub(r'<img\b[^>]*>', '', old_html) if old_html else html.escape(old_text).replace('\n', '<br>')
    # append keeps the note as it was and adds the new lines at the end.
    content = carried + body if append else body + '<div><br></div><div>이전 메모 내용</div>' + carried
    documents = [p for p in files if not td._is_note_image(p)]
    images = [p for p in files if td._is_note_image(p)]
    content += ''.join('<div><img src="' + html.escape(Path(p).as_uri(), quote=True) + '"></div>' for p in documents)
    file_map = [{'stored_path': p, 'attachment_name': Path(p).name} for p in files]
    new_id = None
    moved = False
    store = td._common_work_store()
    try:
        new_id = mutate('create', '', content)
        if not new_id or new_id == note_id:
            raise RuntimeError('APPLE_NOTES_REBUILD_ID_UNCONFIRMED')
        # Match the successful probe: settle document import before each native photo.
        if documents and td._note_missing_files(new_id, documents, runner):
            raise RuntimeError('APPLE_NOTES_REBUILD_DOCUMENTS_MISMATCH')
        attached = list(documents)
        for image in images:
            mutate('attach', new_id, '', [image])
            attached.append(image)
            if td._note_missing_files(new_id, attached, runner):
                raise RuntimeError('APPLE_NOTES_REBUILD_PHOTO_MISMATCH')
        if td._note_missing_files(new_id, files, runner):
            raise RuntimeError('APPLE_NOTES_REBUILD_ATTACHMENTS_MISMATCH')
        after = td._note_verification_snapshot(new_id, runner)
        if not after or text(old_text) not in text(after.get('plaintext')) or after.get('title') != title:
            raise RuntimeError('APPLE_NOTES_REBUILD_TEXT_MISMATCH')
        if str(after.get('body_html') or '').count('<table') < old_tables:
            raise RuntimeError('APPLE_NOTES_REBUILD_TABLES_MISSING')
        if sum(td._is_note_image(p) for p in files) > str(after.get('body_html') or '').count('data:image/'):
            raise RuntimeError('APPLE_NOTES_REBUILD_IMAGES_MISSING')
        latest = td._note_verification_snapshot(note_id, runner)
        if not latest or latest.get('body_html') != before.get('body_html') or latest.get('attachment_names') != native:
            raise RuntimeError('APPLE_NOTES_REBUILD_CONCURRENT_CHANGE')
        store.replace_note_source(old_id=note_id, new_id=new_id)
        moved = True
        td._remember_note_context(new_id, files, str(args.get('original_text') or ''))
    except Exception as exc:
        failed_snapshot = td._note_verification_snapshot(new_id, runner) if new_id else None
        cleanup = None
        if new_id and new_id != note_id:
            try:
                if moved: store.replace_note_source(old_id=new_id, new_id=note_id)
                mutate('delete', new_id); cleanup = True
            except Exception: cleanup = False
        return {'ok': False, 'error': str(exc), 'new_note_id': new_id, 'new_note_deleted': cleanup,
                'original_unchanged': True, 'retry_allowed': False, 'attachment_map': file_map,
                'verification': {'attachment_names': (failed_snapshot or {}).get('attachment_names'),
                                 'images': str((failed_snapshot or {}).get('body_html') or '').count('data:image/')}}
    record = {'old_id': note_id, 'new_id': new_id, 'folder': folder,
              'before': before, 'created_at': time.time()}
    # Persist recovery information before deleting the original, including for a
    # bundle interrupted before its normal completion journal can be saved.
    td._save_personal_rollback(title, 'notes-rebuild:'+new_id, [
        {'domain':'notes','action':'rollback_rebuild','arguments':{'reconstruction':record}}])
    try:
        mutate('delete', note_id)
    except Exception:
        # Deletion can be ambiguous. Keep the verified replacement and journal;
        # never delete both copies or automatically repeat the operation.
        return {'ok':False,'error':'APPLE_NOTES_REBUILD_DELETE_UNCONFIRMED',
                'result':{'note_id':new_id,'reconstruction':record},'retry_allowed':False}
    return {'ok':True,'authority':'Apple Notes','action':'update','target_id':new_id,
            'approved':True,'mutation_calls':2,'fallback_used':False,'tool_search_calls':0,
            'result':{'note_id':new_id,'title':title,'attachments':len(files),
                      'reconstruction':record,'attachment_map':file_map,'id_changed':True,'creation_date_changed':True,
                      'notice':'새 메모로 재구성하여 ID·생성일이 바뀜. 기존 메모는 최근 삭제됨에 있음.'}}


def rollback(td, record, runner):
    old_id, new_id = record['old_id'], record['new_id']
    td._common_work_store().replace_note_source(old_id=new_id, new_id=old_id)
    result = json.loads(td._apple_notes_direct_mutation(
        {'action':'delete','note_id':new_id,'approved':True,'_skip_common_work':True,'_rollback':True},runner=runner))
    result['old_note_id'] = old_id
    # A deleted note keeps its ID in Recently Deleted; the verified move action brings it back.
    script = str(Path(td.__file__).with_name('apple_notes_mutation.applescript'))
    try:
        p = runner(['/usr/bin/osascript', script, 'move', old_id, record['folder'], '', ''],
                   capture_output=True, text=True, timeout=60, check=False)
        restored = p.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        restored = False
    result['original_restored'] = restored
    if not restored:
        result['ok'] = False
        result['error'] = 'APPLE_NOTES_REBUILD_ORIGINAL_RESTORE_FAILED'
    return result
