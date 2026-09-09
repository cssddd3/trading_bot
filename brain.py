"""공유 두뇌 — LLM 역할들(스카우트/뉴스거부권/비서/저녁리뷰)이 함께 읽고 쓰는 운용 일지.

'한 사람처럼' 동작하기 위한 공유 기억이다:
  아침 스카우트가 어제까지의 일지를 읽고 → 장중 매매가 일지에 기록되고 →
  저녁 리뷰가 하루를 평가하고 가설을 남기고 → 다음날 아침 스카우트로 이어진다.

역할 경계는 불변 (대원칙 1): 일지는 기억일 뿐, 매수 방아쇠는 여전히 검증된 가격 규칙만.
LLM이 남긴 가설은 logs/hypotheses.md 에 쌓이고, 백테스트 게이트를 통과해야만 실전 반영.
"""

import json
from datetime import datetime, timedelta, timezone

import config

KST = timezone(timedelta(hours=9))
JOURNAL_PATH = config.LOG_DIR / "brain_journal.json"
HYPOTHESES_PATH = config.LOG_DIR / "hypotheses.md"
MAX_ENTRIES = 60
MAX_CHARS = 15_000


def journal_append(kind: str, text: str) -> None:
    """일지에 한 줄 추가. kind: 매매/리뷰/사건/가설 등. 실패해도 조용히 (open-fail)."""
    try:
        entries = _load()
        entries.append({"ts": datetime.now(KST).isoformat(timespec="minutes"),
                        "kind": kind, "text": text.strip()})
        # 오래된 것부터 자연 소멸 (개수 + 총량 상한)
        entries = entries[-MAX_ENTRIES:]
        while sum(len(e["text"]) for e in entries) > MAX_CHARS and len(entries) > 1:
            entries.pop(0)
        JOURNAL_PATH.write_text(json.dumps(entries, ensure_ascii=False, indent=0))
    except OSError:
        pass


def digest(max_chars: int = 2200, days: int = 10) -> str:
    """최근 일지를 LLM 프롬프트용 요약 문자열로. 없으면 빈 문자열."""
    entries = _load()
    if not entries:
        return ""
    cutoff = (datetime.now(KST) - timedelta(days=days)).isoformat()
    lines = [f"{e['ts'][5:16]} [{e['kind']}] {e['text']}"
             for e in entries if e["ts"] >= cutoff]
    out = "\n".join(lines)
    while len(out) > max_chars and lines:
        lines.pop(0)
        out = "\n".join(lines)
    return out


def add_hypothesis(text: str, source: str = "저녁 리뷰") -> None:
    """검증 대기 가설 기록 — 게이트 통과 전에는 절대 실전 반영 금지."""
    try:
        stamp = datetime.now(KST).strftime("%Y-%m-%d")
        line = f"- [ ] {stamp} ({source}) {text.strip()}\n"
        header = ("# 검증 대기 가설 (백테스트 게이트 통과 전 실전 반영 금지)\n\n"
                  if not HYPOTHESES_PATH.exists() else "")
        with HYPOTHESES_PATH.open("a") as f:
            f.write(header + line)
    except OSError:
        pass


def ask(prompt: str, system: str, role: str = "scout",
        effort: str = "medium", max_tokens: int = 2048) -> str | None:
    """공용 LLM 호출 (텍스트 응답). 실패 시 None (open-fail — 매매를 막지 않는다)."""
    try:
        import anthropic
        model = config.LLM_MODELS.get(role, config.LLM_MODELS["scout"])
        client = anthropic.Anthropic()
        resp = client.messages.create(
            model=model, max_tokens=max_tokens, system=system,
            output_config=config.output_config_for(model, effort, None),
            messages=[{"role": "user", "content": prompt}])
        if resp.stop_reason == "refusal":
            return None
        return next((b.text for b in resp.content if b.type == "text"), None)
    except Exception:                       # noqa: BLE001
        return None


def _load() -> list[dict]:
    try:
        return json.loads(JOURNAL_PATH.read_text())
    except (OSError, ValueError):
        return []
