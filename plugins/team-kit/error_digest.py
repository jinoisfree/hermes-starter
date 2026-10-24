"""Daily new gateway warning/error types; confirmed Slack delivery, without an agent."""
from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import re
import stat
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

KST = timezone(timedelta(hours=9))
MAX_BYTES = 4 * 1024 * 1024
MAX_SIGNATURES = 2000
STATE_VERSION = 2
RENOTICE_SECONDS = 14 * 86400
STAMP_LEVEL = re.compile(
    r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:,\d{3})?)\s+"
    r"(WARNING|ERROR|CRITICAL)\s+(.*)$"
)
# Tool error results returned to the model are ordinary work results, not system failures.
TOOL_RETURN_ERROR = re.compile(r"^agent\.tool_executor:\s+Tool [\w.-]+ returned error\b")


def safe_path(path):
    path = Path(path).absolute()
    if path.resolve() != path:
        raise ValueError("symlink path")
    return path


def read_tail(path, limit=MAX_BYTES):
    path = safe_path(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("nonregular file")
        start = max(0, info.st_size - limit)
        stream.seek(start)
        raw = stream.read(limit)
    if start:
        raw = raw.partition(b"\n")[2]  # The first retained line may be partial.
    return raw.decode("utf-8", errors="replace")


def redact(text):
    # Redact before clipping, including credentials beyond the first 160 characters.
    text = re.sub(r"(?i)\bxox[a-z]-[A-Za-z0-9-]+", "[REDACTED]", text)
    text = re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[REDACTED]", text)
    text = re.sub(
        r'(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password|authorization)["\s]*[:=]["\s]*)([^\s,"}]+)',
        r"\1[REDACTED]", text,
    )
    text = re.sub(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b", "<email>", text)
    text = re.sub(
        r"(?i)\b(?:content|body|prompt|dm|email|location|latitude|longitude)\s*[:=].*",
        "[내용 생략]", text,
    )
    text = re.sub(
        r"(?<![A-Za-z0-9+/=_-])[A-Za-z0-9+/=_-]{60,}(?![A-Za-z0-9+/=_-])",
        lambda m: m[0] if re.fullmatch(r"[0-9a-fA-F]+", m[0]) else "[REDACTED]", text,
    )
    for pattern in (r'"[^"\n]{41,}"', r"'[^'\n]{41,}'", r"“[^”\n]{41,}”", r"‘[^’\n]{41,}’"):
        text = re.sub(pattern, lambda m: m[0][0] + "…" + m[0][-1]
                      if re.search(r"[A-Za-z가-힣]", m[0]) else m[0], text)
    return text


def normalize(text):
    text = redact(text)
    text = re.sub(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:[+-]\d{2}:\d{2}|Z)?",
                  "<time>", text)
    text = re.sub(r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
                  "<uuid>", text)
    text = re.sub(r"(?i)([/\\])[0-9a-f]{8,}(?=[/\\.\s]|$)", r"\1<hash>", text)
    text = re.sub(r"\b[CDGUW][A-Z0-9]{8,}\b", "<slack-id>", text)
    text = re.sub(r"\b\d{10,}\.\d{6}\b", "<slack-ts>", text)
    text = re.sub(r"(?i)\b[0-9a-f]{16,}\b", "<hash>", text)
    text = re.sub(r"\b\d{6,}\b", "<number>", text)
    return " ".join(text.split())[:160]


def make_signature(level, summary):
    # Logger and tool names identify the source; numbers in error details vary per run.
    logger, separator, message = summary.partition(": ")
    if not separator or not re.fullmatch(r"[\w.-]+", logger):
        logger, separator, message = "", "", summary
    tool = re.match(r"Tool ([\w.-]+) returned error\b", message)
    if tool:
        message = tool[0]
    else:
        message = re.sub(r"\d+(?:\.\d+)?", "<n>", message)
    return f"{level} {logger}{separator}{message}"[:160]


def collect(text, since, now):
    """Only timestamped WARNING/ERROR/CRITICAL lines, not traceback continuations."""
    groups = {}
    for line in text.splitlines():
        match = STAMP_LEVEL.match(line)
        if not match:
            continue
        if TOOL_RETURN_ERROR.match(match[3]):
            continue
        try:
            stamp = datetime.fromisoformat(match[1].replace(",", ".")).replace(tzinfo=KST).timestamp()
        except ValueError:
            continue
        if not since <= stamp <= now:
            continue
        level, summary = match[2], normalize(match[3])
        signature = make_signature(level, summary)
        item = groups.setdefault(signature, {
            "count": 0, "first_at": stamp, "last_seen": stamp, "level": level, "summary": summary,
        })
        item["count"] += 1
        item["first_at"] = min(item["first_at"], stamp)
        item["last_seen"] = max(item["last_seen"], stamp)
    return groups


def load_state(path):
    path = safe_path(path)
    if not path.exists():
        return {"version": STATE_VERSION, "initialized": False, "signatures": {}}
    data = json.loads(read_tail(path))
    if not isinstance(data, dict) or not isinstance(data.get("initialized"), bool):
        raise ValueError("invalid state")
    signatures = data.get("signatures")
    if not isinstance(signatures, dict):
        raise ValueError("invalid signatures")
    for key, item in signatures.items():
        value = item.get("last_seen") if isinstance(item, dict) else None
        if (len(key) > 160 or isinstance(value, bool) or not isinstance(value, (int, float))
                or not math.isfinite(value)):
            raise ValueError("invalid last_seen")
    if type(data.get("version")) is not int or data["version"] != STATE_VERSION:
        return {"version": STATE_VERSION, "initialized": False, "signatures": {}}
    return data


def save_state(path, data):
    path = safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".error-digest-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(data, stream, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        safe_path(path)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def explain(summary):
    """Explain only recognizable log facts; do not infer recovery or user impact."""
    message = summary.lower()
    if "failed to connect" in message and "slack.com" in message:
        return "슬랙 서버 접속 실패", "슬랙 서버에 연결하지 못했다는 기록이다. 연결 실패의 원인과 메시지 손실 여부는 이 줄만으로 알 수 없다. 같은 시각의 재연결·전송 기록을 확인해야 한다."
    if "system prompt section" in message and "exceeded" in message and "budget" in message:
        return "답변 지침 일부 누락", "답변에 넣을 안내문이 글자 수 한도를 넘어 일부 빠졌다는 기록이다. 해당 안내를 답변에 반영하지 못했을 수 있다."
    if "stored system prompt" in message and "is null" in message:
        return "저장된 답변 지침을 다시 준비함", "저장해 둔 답변 지침이 비어 있어 이번 답변용 지침을 다시 준비하려 했다는 기록이다. 대화 내용이 지워졌다는 뜻은 아니다."
    if "msg_too_long" in message:
        return "슬랙 글 길이 초과", "글이 너무 길어 슬랙이 해당 전송이나 수정을 거절했다는 기록이다. 전달하려던 내용이 전부 보이는지는 확인이 필요하다."
    if "socket mode unhealthy" in message and "reconnecting" in message:
        return "슬랙 연결을 다시 시도함", "슬랙과의 실시간 연결에 문제가 생겨 다시 연결하려 했다는 기록이다. 그동안 답변이 늦어졌을 수 있으며, 연결 성공 여부는 이 줄만으로 알 수 없다."
    if "error analyzing image" in message and "client is closed" in message:
        return "이미지 읽기 실패", "이미지를 읽는 서비스와의 연결이 닫혀 해당 분석을 끝내지 못했다는 기록이다. 이후 다시 읽기에 성공했는지는 확인이 필요하다."
    if "failed to run a middleware function" in message:
        return "요청 처리의 중간 단계 실패", "요청을 처리하는 중간 단계에서 문제가 생겼다는 기록이다. 어느 답변에 영향이 있었는지와 원인은 추가 확인이 필요하다."
    # conversation_loop logs the failed attempt, then the retry; the final outcome is a later line.
    if ("api call failed (attempt" in message or "retrying api call in" in message) and "/" in message:
        return "AI 호출 실패 후 자동 재시도", "AI 모델 서버 호출이 한 번 실패해 자동으로 다시 시도했다는 기록이다. 재시도에 성공하면 답변은 정상으로 나간다. 마지막 시도까지 실패했는지는 같은 시각의 이후 기록을 봐야 안다."
    if "ddgs" in message and "no results found" in message:
        return "웹 검색 결과 없음", "무료 웹 검색(DuckDuckGo)이 그 검색어로 결과를 찾지 못했다는 기록이다. 프로그램 고장이 아니며, 조직원이 다른 검색어나 다른 방법으로 이어 갈 수 있다."
    if ("photon upstream stream degraded" in message
            or "fatal photon adapter error (upstream_stream_degraded)" in message):
        return "iMessage(Photon) 연결 끊김 후 재연결", "아이메시지(Photon)의 수신 연결에 문제가 생겨 자동 재연결을 시도한다는 기록이다. 재연결 성공 여부는 이 줄만으로는 알 수 없고, gateway.log의 'photon reconnected successfully' 기록으로 확인한다."
    if "mirror: no session found for slack:" in message:
        return "보낸 글의 대화 기록 연결 실패", "보낸 글을 대화 기록에도 남기려 했으나 연결할 대화를 찾지 못해 기록을 덧붙이지 않았다는 기록이다. 실제 글 전송의 성공·실패는 이 줄만으로는 알 수 없다."
    if ("agent.conversation_compression_reply_anchor:" in message
            and "compression: engine folded away the just-delivered assistant reply" in message):
        return "대화 압축 중 최근 답변 복원 생략", "대화 요약에서 방금 전달한 답변이 최근 대화 목록에서 빠졌고, 위치나 발언 순서 문제로 다시 넣지 않았다는 기록이다. 이 줄만으로는 화면 표시나 다음 답변에 생긴 영향을 알 수 없다."
    if "common router ignored expired pending approval route" in message:
        return "지난 승인 대기 무시", "시간이 지난 승인 대기 정보를 버리고 평소 담당자 선택 경로로 처리했다는 정상 기록이다. 승인 대상 작업을 실행했다는 뜻은 아니다."
    if "plugin catalog" in message and "tier must be one of" in message:
        return "외부 플러그인 목록 자료 경고", "Hermes 공개 플러그인 목록의 항목 값이 이 버전이 모르는 형식이라 그 항목을 건너뛰었다는 기록이다. 조직원 동작에는 영향이 없다."
    if "timed out" in message or "timeouterror" in message:
        return "처리 대기 시간 초과", "정해진 시간 안에 처리가 끝나지 않았다는 기록이다. 결과가 늦게 도착했는지, 끝내 실패했는지는 확인이 필요하다."
    if "missing_scope" in message:
        return "슬랙 사용 권한 부족", "요청한 기능에 필요한 슬랙 권한이 없어 해당 작업이 거절됐다는 기록이다. 어떤 기능의 권한인지 확인이 필요하다."
    if "not_in_channel" in message:
        return "슬랙 대화방 접근 불가", "해당 봇이 대화방에 들어가 있지 않아 요청한 작업을 하지 못했다는 기록이다."
    if "nodename nor servname" in message or "clientconnectordnserror" in message:
        return "인터넷 주소 찾기 실패", "슬랙 같은 바깥 서버의 주소를 찾지 못해 연결하지 못했다는 기록이다. 그 순간 인터넷 연결이 끊겼을 수 있으며, 해당 전송이 빠졌는지는 확인이 필요하다."
    if "notification was not delivered" in message:
        return "시스템 알림 전달 실패", "재시작·종료 같은 시스템 알림을 채널에 보내지 못했다는 기록이다. 게이트웨이 동작 자체의 실패는 아니며, 같은 시각의 다른 기록으로 원인을 확인한다."
    if "shutdown context" in message and "signal=" in message:
        return "게이트웨이 종료 신호 기록", "재시작이나 종료 명령으로 게이트웨이가 멈출 때 받은 신호를 남긴 기록이다. 이것만으로는 오류가 아니다."
    # The only emitter is cronjob_prompt_scan; normalize() hides the text after "prompt:".
    if "cron skill-assembled" in message:
        return "예약 지침의 보이지 않는 글자 제거", "예약 작업에 붙는 스킬 본문에서 보이지 않는 글자를 지우고 실행했다는 기록이다. 예약은 그대로 진행된다."
    return "뜻을 더 확인해야 하는 새 기록", "프로그램이 새 경고나 오류를 남겼다. 이 문구만으로 원인과 사용자에게 생긴 영향을 확정할 수 없어 추가 확인이 필요하다."


def format_digest(groups):
    if not groups:
        return ""
    ordered = sorted(groups.items(), key=lambda pair: (-pair[1]["count"], pair[1]["first_at"], pair[0]))
    lines = [f"*🟡 지난 24시간 새 오류 {len(ordered)}종*", "Hermes의 경고·오류 기록을 확인했다. 과거 기록이므로 지금도 문제가 계속되는지는 별도 확인이 필요하다."]
    for _, item in ordered[:10]:
        first = datetime.fromtimestamp(item["first_at"], KST).strftime("%m월 %d일 %H:%M")
        level = {"WARNING": "경고", "ERROR": "오류", "CRITICAL": "심각한 오류"}[item["level"]]
        title, meaning = explain(item["summary"])
        lines.append(f'- *{title} · {level} · {item["count"]}건*\n  {meaning}\n  이 조회 기간의 첫 발생: {first} (한국시간)\n  확인용 기록 원문: {item["summary"]}')
    if len(ordered) > 10:
        lines.append(f"외 {len(ordered) - 10}종")
    lines.append("니가 지금 해결할 필요가 있는지 알고 싶으면 starter_user-world에서 진단담당에게 이 보고의 현재 상태를 확인하라고 해라. 근거는 시스템 오류 기록 파일(gateway.error.log)에 있다.")
    return "\n\n".join(lines)


def deliver(home, text):
    spec = importlib.util.spec_from_file_location("error_digest_report", Path(__file__).with_name("learning_report.py"))
    R = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(R)
    return R.slack(home, 'chat.postMessage', {'channel': 'REPLACE_SLACK_ID', 'text': text,
                                            'unfurl_links': False, 'unfurl_media': False})


def run(root, home, *, hours=24, dry_run=False, now=None):
    now = time.time() if now is None else now
    state_path = Path(home) / "state/error-digest.json"
    state = load_state(state_path)
    text = read_tail(Path(root) / "logs/gateway.error.log")
    initialized = state["initialized"]
    window = hours * 3600 if initialized else RENOTICE_SECONDS
    groups = collect(text, now - window, now)
    seen = state["signatures"]
    new = {key: item for key, item in groups.items()
           if key not in seen or now - seen[key]["last_seen"] > RENOTICE_SECONDS} if initialized else {}
    pending = state.get('pending', {})
    if not isinstance(pending, dict):
        raise ValueError('invalid pending digest')
    # Keep unacknowledged reports even when their log lines leave today's window.
    new = {**pending, **new}
    if len(new) > MAX_SIGNATURES:
        raise ValueError('pending digest capacity exceeded')
    result = format_digest(new)
    if hours != 24:
        result = result.replace("지난 24시간", f"지난 {hours}시간", 1)
    if dry_run:
        return result
    if result:
        state['pending'] = new
        save_state(state_path, state)
        receipt = deliver(home, result)
        if not (isinstance(receipt, dict) and receipt.get('ok') is True and receipt.get('ts')
                and receipt.get('channel') == 'REPLACE_SLACK_ID'):
            raise ValueError('digest delivery unconfirmed')
    for key, item in {**groups, **new}.items():
        seen[key] = {"last_seen": max(item["last_seen"], seen.get(key, {}).get("last_seen", 0))}
    state.pop('pending', None)
    state.update(version=STATE_VERSION, initialized=True, signatures=dict(sorted(
        seen.items(), key=lambda pair: (-pair[1]["last_seen"], pair[0]))[:MAX_SIGNATURES]))
    save_state(state_path, state)
    # Cron's deliver target remains configured; empty stdout prevents a second post.
    return ""


def main(argv=None, *, root=None, home=None, now=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="상태를 바꾸지 않고 출력만 확인")
    parser.add_argument("--hours", type=int, default=24, help="조회 시간 (기본 24)")
    args = parser.parse_args(argv)
    if args.hours <= 0:
        parser.error("--hours는 양수여야 합니다")
    root = Path(root) if root is not None else Path.home() / ".hermes"
    home = Path(home) if home is not None else root / "profiles/system-doctor"
    try:
        result = run(root, home, hours=args.hours, dry_run=args.dry_run, now=now)
    except (OSError, ValueError, TypeError, RuntimeError) as exc:
        print(f"새 오류 요약 실패: 로그·상태 처리 또는 전달을 확인하지 못했습니다 ({type(exc).__name__}).",
              file=sys.stderr)
        return 1
    if result:
        print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
