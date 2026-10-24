"""Drive the Notes app on screen to add text and files at the end of one note (owner's choice).

Used only when the owner picks it for a note that keeps photos and documents: scripting a body
rewrite would drop the documents, while pasting in the app keeps the same note, ID and sharing.
"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import time

# 2026-10-16 trial: keystrokes did not reach the note body and an existing PDF attachment was removed.
# Off until the focus problem is solved and re-tested; callers get a clear refusal instead.
ENABLED = False

SCRIPT = r'''
ObjC.import('AppKit');
function run(argv) {
  const [noteId, mode, payload] = argv;
  const notes = Application('Notes');
  notes.activate();
  notes.show(notes.notes.byId(noteId));
  delay(1.5);
  const se = Application('System Events');
  const pb = $.NSPasteboard.generalPasteboard;
  pb.clearContents;
  if (mode === 'text') {
    pb.setStringForType($(payload), $.NSPasteboardTypeString);
  } else {
    pb.writeObjects($([$.NSURL.fileURLWithPath(payload)]));
  }
  // Focus the note body, go to its very end, start a new line and paste.
  se.keystroke('', {});
  se.keyCode(125, {using: 'command down'});
  delay(0.3);
  se.keyCode(36);
  se.keystroke('v', {using: 'command down'});
  delay(mode === 'text' ? 1 : 4);
  return 'ok';
}
'''


def _paste(runner, note_id, mode, payload):
    proc = runner(['/usr/bin/osascript', '-l', 'JavaScript', '-e', SCRIPT, note_id, mode, payload],
                  capture_output=True, text=True, timeout=60, check=False)
    if proc.returncode != 0:
        detail = (proc.stderr or '').strip()
        if 'not allowed' in detail or '1002' in detail or 'assistive' in detail.lower():
            raise PermissionError('APPLE_NOTES_SCREEN_PERMISSION_REQUIRED')
        raise RuntimeError('APPLE_NOTES_SCREEN_FAILED')


def append(td, *, note_id, before, lines, files, runner=subprocess.run):
    """Paste lines and files at the end of the same note, then verify by note ID."""
    if not ENABLED:
        return {'ok': False, 'error': 'APPLE_NOTES_SCREEN_DISABLED', 'written': False,
                'reason': '화면 조작은 시험에서 기존 첨부가 사라져 사용을 멈췄다. 새로 만들기만 가능하다.'}
    if not lines and not files:
        return {'ok': False, 'error': 'APPLE_NOTES_SCREEN_NOTHING_TO_ADD'}
    names_before = [n for n in (before or {}).get('attachment_names') or [] if n]
    try:
        if lines:
            _paste(runner, note_id, 'text', '\n'.join(lines))
        for path in files:
            _paste(runner, note_id, 'file', str(Path(path)))
    except PermissionError as exc:
        return {'ok': False, 'error': str(exc), 'written': False,
                'instruction': '맥미니의 시스템 설정 > 개인정보 보호 및 보안 > 손쉬운 사용에서 Hermes 실행 프로그램을 허용해야 한다.'}
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        return {'ok': False, 'error': str(exc)}
    time.sleep(2)
    after = td._note_verification_snapshot(note_id, runner) or {}
    text = str(after.get('plaintext') or '')
    names_after = [n for n in after.get('attachment_names') or [] if n]
    missing_lines = [x for x in lines if x not in text]
    new_names = [n for n in names_after if n not in names_before]
    missing_files = [Path(p).name for p in files if not any(Path(p).stem.split('_')[0] in n for n in new_names)]
    kept = all(n in names_after for n in names_before)
    ok = not missing_lines and not missing_files and kept
    return {'ok': ok, 'authority': 'Apple Notes (screen)', 'action': 'update', 'target_id': note_id,
            'result': {'note_id': note_id, 'id_changed': False, 'lines_added': len(lines) - len(missing_lines),
                       'files_added': len(files) - len(missing_files), 'existing_attachments_kept': kept,
                       **({'missing_lines': missing_lines} if missing_lines else {}),
                       **({'missing_files': missing_files} if missing_files else {})}}
