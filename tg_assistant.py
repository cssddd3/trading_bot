"""텔레그램 LLM 비서 — 봇 상태에 대한 자유 질문에 Claude가 답한다.

역할 경계 (중요):
  - **읽기·설명 전용.** LLM은 봇을 제어할 수 없다 — 매매 실행/중지/설정 변경 불가.
  - 제어는 결정적 명령(/stop /resume /flat /status)만 가능하고 코드가 직접 처리한다.
  - LLM 답변 실패는 조용히 무시된다 (open-fail — 비서가 죽어도 매매는 계속).

사용: 텔레그램 봇에게 "/"없이 아무 질문이나 보내면 된다.
  예) "지금 뭐 들고 있어?", "오늘 왜 안 샀어?", "어제랑 뭐가 달라?"
"""

import json

import config

HISTORY_PATH = config.LOG_DIR / "tg_history.json"
HISTORY_MAX_MSGS = 20        # 최근 10문답
HISTORY_MAX_CHARS = 8000     # 히스토리 총량 상한 (비용/컨텍스트 통제)

SYSTEM = """너는 'toss-trader' 자동매매 봇의 상태를 주인에게 설명하는 비서다.

규칙:
- 아래 제공되는 [봇 상태] 데이터만 근거로 답한다. 데이터에 없는 것은 모른다고 말한다.
- 이전 대화가 이어진다. 단, [봇 상태]는 매 질문마다 최신으로 갱신되므로
  과거 답변 속 숫자와 다르면 항상 최신 [봇 상태]가 맞다.
- 텔레그램 메시지이므로 짧고 명확하게 (보통 3~6문장, 필요하면 리스트).
- 너는 봇을 제어할 수 없다. 제어 요청(멈춰줘, 팔아줘 등)이 오면 실행하지 말고
  해당 명령어를 안내한다: /stop(매수중지) /resume(재개) /flat(전량청산) /status(현황)
- 시황 해설은 데이터 범위 안에서만. 종목 추천/투자 조언은 하지 않는다.
- 한국어로 답한다."""


def _tail_csv(path, n: int = 12) -> list[str]:
    if not path.exists():
        return []
    lines = path.read_text().strip().splitlines()
    return lines[:1] + lines[-n:] if len(lines) > n + 1 else lines


def build_context(dr) -> str:
    """러너(DryRun 인스턴스)에서 현재 상태 스냅샷을 문자열로 만든다."""
    from run_dryrun import now_kst

    parts = [f"[봇 상태] {now_kst():%Y-%m-%d %H:%M} KST",
             f"모드: {dr.tag} | 전략: {dr.key} | 매수중지(halted): {dr.pf.halted}"]
    try:
        session, _ = dr.market_session()
        parts.append(f"시장 세션: {session}")
    except Exception:                    # noqa: BLE001
        pass

    if dr.pf.positions:
        for s, p in dr.pf.positions.items():
            live = dr.last_price(s) or p["avg_price"]
            parts.append(f"보유: {s} {dr._names.get(s, '')} {p['quantity']}주 "
                         f"@{p['avg_price']:,.0f} → 현재 {live:,.0f} "
                         f"({live / p['avg_price'] - 1:+.2%}) 스탑 {p.get('stop_price') or '없음'}")
    else:
        parts.append("보유 포지션: 없음")
    if dr.live and dr.broker:
        try:
            parts.append(f"매수가능금액 {dr.broker.buying_power():,.0f}원 "
                         f"/ 봇 예산 {dr._budget_str()} — 예산은 실현손익만큼 복리로 변한다")
        except Exception:                # noqa: BLE001
            pass
    parts.append(dr.guard.summary())

    wl = config.LOG_DIR / "watchlist.json"
    if wl.exists():
        try:
            d = json.loads(wl.read_text())
            picks = ", ".join(f"{p['symbol']} {p['name']}" for p in d.get("picks", []))
            parts.append(f"오늘 워치리스트({d.get('date')}): {picks or '선정 없음'} "
                         f"| 시장메모: {d.get('market_note', '')}")
        except (json.JSONDecodeError, KeyError):
            pass

    sig = _tail_csv(dr.signals_path)
    if sig:
        parts.append("최근 시그널 로그(헤더+최근):\n" + "\n".join(sig))
    tr = _tail_csv(dr.trades_path, 6)
    if tr:
        parts.append("최근 체결 로그:\n" + "\n".join(tr))
    parts.append(f"감시 종목: {', '.join(dr.symbols)}")
    return "\n".join(parts)[:6000]


def _load_history() -> list[dict]:
    try:
        return json.loads(HISTORY_PATH.read_text())
    except (OSError, ValueError):
        return []


def _save_history(history: list[dict]) -> None:
    """최근 N문답, 총량 상한으로 잘라 저장 (오래된 것부터 자연 소멸)."""
    trimmed, total = [], 0
    for m in reversed(history[-HISTORY_MAX_MSGS:]):
        total += len(m["content"])
        if total > HISTORY_MAX_CHARS:
            break
        trimmed.append(m)
    trimmed.reverse()
    try:
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        HISTORY_PATH.write_text(json.dumps(trimmed, ensure_ascii=False))
    except OSError:
        pass


def answer(question: str, context: str) -> str | None:
    """질문에 답변. 이전 문답(파일 영속)이 이어지고, 봇 상태는 매번 최신으로 갱신.
    실패 시 None (호출부가 조용히 넘어감)."""
    try:
        from datetime import datetime, timedelta, timezone
        import anthropic
        client = anthropic.Anthropic()
        kwargs = {}
        oc = config.output_config_for(config.LLM_MODELS["assistant"], "low")
        if oc:
            kwargs["output_config"] = oc
        now = datetime.now(timezone(timedelta(hours=9)))
        history = _load_history()
        user_msg = f"[{now:%m-%d %H:%M}] {question}"
        resp = client.messages.create(
            model=config.LLM_MODELS["assistant"],
            max_tokens=2048,
            # 상태 스냅샷은 히스토리에 쌓지 않고 system에 매번 최신본만 싣는다
            system=f"{SYSTEM}\n\n{context}",
            **kwargs,
            messages=history + [{"role": "user", "content": user_msg}],
        )
        if resp.stop_reason == "refusal":
            return None
        text = next((b.text for b in resp.content if b.type == "text"), "").strip()
        if not text:
            return None
        text = text[:3800]
        history += [{"role": "user", "content": user_msg},
                    {"role": "assistant", "content": text}]
        _save_history(history)
        return text
    except Exception as e:               # noqa: BLE001 - 비서 실패가 매매를 막으면 안 됨
        print(f"  [비서] 응답 실패: {e}")
        return None
