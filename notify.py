"""텔레그램 알림 (선택 기능).

.env에 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 가 있으면 주요 이벤트를 폰으로 보낸다.
없으면 조용히 아무것도 안 한다 (open-fail — 알림 실패가 매매를 막지 않는다).

설정 방법 (5분):
  1. 텔레그램 앱에서 @BotFather 검색 → /newbot → 봇 이름 정하면 토큰을 준다
  2. 만든 봇에게 아무 메시지나 한 번 보낸다
  3. 브라우저에서 https://api.telegram.org/bot<토큰>/getUpdates 열면
     "chat":{"id": 123456789 ...} 가 보인다 → 그게 chat_id
  4. .env에 추가:
       TELEGRAM_BOT_TOKEN=123456:ABC-...
       TELEGRAM_CHAT_ID=123456789
  5. 테스트: python3 notify.py "테스트"
"""

import os

import requests

import config


def _creds() -> tuple[str, str] | None:
    config.load_env()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    return (token, chat) if token and chat else None


def enabled() -> bool:
    return _creds() is not None


def send(text: str) -> bool:
    """알림 전송. 실패해도 예외를 올리지 않는다."""
    creds = _creds()
    if not creds:
        return False
    token, chat = creds
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text[:4000],
                  "disable_web_page_preview": True},
            timeout=10,
        )
        return r.status_code == 200
    except requests.RequestException:
        return False


def broadcast(text: str) -> bool:
    """공유 채널(지인 구독용) 전송. .env TELEGRAM_BROADCAST_CHAT_ID 없으면 no-op.

    채널 만들기: 텔레그램 새 채널 생성 → 봇을 관리자로 추가 →
    공개 채널이면 TELEGRAM_BROADCAST_CHAT_ID=@채널이름 (비공개면 숫자 id).
    ⚠️ 명령 수신은 여전히 개인 chat_id만 — 채널은 발신 전용이다.
    ⚠️ 계좌 수치(예수금/예산/수량)는 보내지 않는다 — 종목·가격·사유·수익률만."""
    config.load_env()
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.getenv("TELEGRAM_BROADCAST_CHAT_ID", "").strip()
    if not token or not chat:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text[:4000],
                  "disable_web_page_preview": True},
            timeout=10,
        )
        return r.status_code == 200
    except requests.RequestException:
        return False


def poll_commands(offset: int = 0) -> tuple[int, list[str], list[str]]:
    """봇에게 온 메시지를 가져온다. 등록된 chat_id만 인정한다.

    반환: (다음 offset, /명령 리스트, 자유 질문 리스트)
      - "/"로 시작하면 명령(/stop 등) — 코드가 결정적으로 처리
      - 그 외 텍스트는 질문 — LLM 비서가 답변 (읽기·설명 전용)
    """
    creds = _creds()
    if not creds:
        return offset, [], []
    token, chat = creds
    try:
        r = requests.get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            params={"offset": offset, "timeout": 0}, timeout=10).json()
    except (requests.RequestException, ValueError):
        return offset, [], []
    commands, questions, new_offset = [], [], offset
    for u in r.get("result", []):
        new_offset = max(new_offset, u["update_id"] + 1)
        msg = u.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != str(chat):
            continue                        # 등록된 chat_id 외 무시
        raw = (msg.get("text") or "").strip()
        if not raw:
            continue
        if raw.startswith("/"):
            parts = raw.split()
            base = parts[0].lower().split("@")[0]      # "/budget@bot" → "/budget"
            commands.append(" ".join([base] + parts[1:])[:100])
        else:
            questions.append(raw[:500])
    return new_offset, commands, questions


if __name__ == "__main__":
    import sys
    msg = " ".join(sys.argv[1:]) or "toss-trader 알림 테스트 ✅"
    if not enabled():
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 가 .env에 없습니다.")
        print(__doc__)
    else:
        print("전송 성공" if send(msg) else "전송 실패 (토큰/chat_id 확인)")
