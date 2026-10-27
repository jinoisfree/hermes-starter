"""The owner's explicit task registration selection; no service calls or approval bypass."""
from pathlib import Path
import re
import sqlite3

DESTINATIONS = ('노션', '일정', '메모')
DIRECT = {'task_reminders_sync', 'notion', 'knowledge_ref_link', 'knowledge_ref_get', 'apple_notes_direct_lookup', 'reminders_direct_lookup'}
NOTE = """[업무비서 할 일 등록 — 소유자 결정 2026-10-24]
소유자가 할 일/업무 등록·추가·만들기·저장을 시키고 만들 곳을 말하지 않았으면, 제목·정확한 마감·세부 내용을 정리해 보여주고 마지막 줄에 [[체크: 전체 | 노션 | 일정 | 메모]]를 붙인다. 그 전에는 원본을 만들지 않는다. 이는 업무비서 등록 흐름 전용이고 다른 조직원에게 이 체크리스트를 붙이지 않는다. 이미 노션/일정/메모 중 만들 곳을 말했으면 다시 묻지 않고 지정한 곳만 진행한다.
Slack과 desktop-pet 모두 가로 선택 토글 버튼(☐/✅)과 작성·건너뛰기 버튼이다. 체크리스트 글에 소유자가 '건너뛰기'라고 답하면 선택 상태와 무관하게 아무것도 만들지 않으며 등록 승인으로 보지 않는다. 메일 알림이면 해당 알림의 메일 ID만 gmail_direct(action=ack)로 처리하고 report_status(done)로 초록 종결한다. 일반 할 일 등록이면 생성하지 않았다고 짧게 답하고 종결한다. 실패한 ack·종결은 완료로 보고하지 않는다. '선택: 노션, 일정', '노션 일정', '전체' 같은 글 답장도 같은 선택이다. 전체는 노션·일정·메모 전부다. 최근 같은 스레드의 등록 초안을 기준으로 선택한 곳만 한 번씩 만들며 제목·마감·세부가 없으면 먼저 확인한다. 확인하지 못한 시각을 만들지 않는다.
선택 후 A가 team_work(start) 없이 조회 → 묶음 작성 → 노션 작성 → 연결까지 같은 턴에 처리하고 보고한다. 선택한 일정·메모만 personal_work_bundle(task_capture=true)로 만들고 반환된 common_id를 재사용한다. 노션 작성·연결까지 마친 다음 task_reminders_sync(common_id=..., due=등록 초안의 마감)를 즉시 호출해 할 일마다 그 할 일 마감 당일 미리알림을 하나씩 만든다. 일정만 선택해 노션·메모가 없어도 같은 도구가 초안 마감(없으면 일정 날짜)으로 업무 미리알림 하나를 만든다. 마감이 없으면 due를 생략한다. 같은 등록 묶음에 todo create를 넣지 않아 중복 생성하지 않는다. 종일 일정 생성은 alarms=[]로 캘린더 기본 알림을 비운다. 메모 속 할 일이 세 개이고 마감이 각각이면 미리알림도 세 개다. 미리알림 due와 알림 시각은 실제 마감과 같고 날짜만 있으면 날짜만 쓴다. D-7/3/1 등 분할 준비 알림은 만들지 않는다. 기존 미리알림을 먼저 조회하고 완료된 항목도 중복 생성하지 않는다. 같은 공통 업무의 정확한 기존 미리알림은 common_id와 existing_sources로 재사용하고 선택한 다른 원본만 추가한다. 완료됐거나 미연결·다른 업무이면 중단해 보고한다. 전부 이미 있으면 다시 만들지 않고 보고한다.
노션을 골랐으면 기존 notion 도구로 전략기획실 업무 DB에 각 할 일을 to_do 블록으로, 각 마감은 그 할 일의 본문에 명시해 한 번 만들고 knowledge_ref_link(ref_type=notion_page, relation_type=documented_by, link_reason=verified_workflow_evidence, receipt_id=실제 생성 결과의 ID)로 같은 common_id에 연결한다. 노션만 골랐고 마감도 없어 묶음이 비었으면 knowledge_ref_link에 common_id를 생략하고 new_work_title=등록 제목으로 노션만 가진 공통 업무를 만든다. 선택하지 않은 메모·일정을 대신 만들지 않는다.
부분 성공이면 성공한 ID·링크를 보존하고 실패한 단계만 보고한다. 불명확한 쓰기는 조회로 확인하기 전 재실행하지 않는다. 승인·원본 범위 검사는 도구의 기존 경계를 그대로 따른다. 이 지침은 예약·배경 쓰기를 허용하지 않는다."""


def selection(text):
    value = str(text or '').strip().removeprefix('선택:').strip()
    pieces = [v for v in re.split(r'[,\s]+', value) if v]
    if not pieces or any(v not in {'전체', *DESTINATIONS} for v in pieces):
        return None
    return list(DESTINATIONS) if '전체' in pieces else [v for v in DESTINATIONS if v in pieces]


def active(home, text, session_id=''):
    if Path(home).name != 'work-assistant':
        return False
    if re.search(r'(?:할\s*일|업무).*(?:등록|추가|만들|저장)|(?:등록|추가|저장).*(?:할\s*일|업무)', str(text or '')):
        return True
    if selection(text) is None or not session_id:
        return False
    # A bare "전체" may answer any question. Only our current thread's capture draft unlocks this path.
    db = Path(home) / 'state.db'
    try:
        with sqlite3.connect(db.as_uri() + '?mode=ro', uri=True) as conn:
            row = conn.execute("SELECT content FROM messages WHERE session_id=? AND role='assistant' AND content IS NOT NULL ORDER BY id DESC LIMIT 1", (session_id,)).fetchone()
        return bool(row and '[[체크: 전체 | 노션 | 일정 | 메모]]' in row[0])
    except sqlite3.Error:
        return False


def allowed(tool, args):
    if tool == 'notion':
        return args.get('action') in {'search', 'databases', 'schema', 'query', 'page', 'users', 'api_help', 'create'}
    if tool == 'knowledge_ref_link':
        return args.get('ref_type') == 'notion_page'
    return True

NOTE += "\n연도 없는 M/D·M.D·M월 D일은 KST 올해다. 지난 날짜면 past_due로 보고하고 다음 해로 미루지 않는다. 7일 제한은 없다. 작성을 누르거나 같은 선택을 글로 답하면 이번 등록 계획의 승인이다. 생성 뒤 다시 승인받지 않는다. 기존 항목 변경·삭제·메일 발송이 같은 요청에 있으면 첫 승인에 request_plan 전체를 보여주고 계획 안 쓰기는 다시 묻지 않는다."
