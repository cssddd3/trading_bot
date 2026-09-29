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
ENTITIES_PATH = config.LOG_DIR / "tg_last_entities.json"   # 직전 답변에서 언급한 종목 (참조 기억)
HISTORY_MAX_MSGS = 20        # 최근 10문답
HISTORY_MAX_CHARS = 8000     # 히스토리 총량 상한 (비용/컨텍스트 통제)
CONTEXT_MAX_CHARS = 9000     # 상태 스냅샷 상한 — 초과 시 로그류부터 잘림 (핵심은 보존)

SYSTEM = """너는 'toss-trader' 자동매매 봇의 상태를 주인에게 설명하는 비서다.

규칙:
- 아래 제공되는 [봇 상태] 데이터만 근거로 답한다. 데이터에 없는 것은 모른다고 말한다.
  **종목코드의 회사명을 데이터에 없는데 아는 척 지어내지 마라** (예: "069500"을 실제로는
  KODEX 200인데 "카카오"라고 추측하는 식의 환각 금지) — 데이터에 이름이 없으면 코드만 말하라.
- 이전 대화가 이어진다. 단, [봇 상태]는 매 질문마다 최신으로 갱신되므로
  과거 답변 속 숫자와 다르면 항상 최신 [봇 상태]가 맞다.
- **종목은 항상 "코드 이름"으로 함께 말한다.** 코드↔이름 변환은 [종목 사전] 블록으로만 한다 —
  사전에 없으면 코드를 그대로 쓰고 "(이름 미확인)"이라고 붙인다. 사용자가 "종목명으로
  알려줘"라고 해도 사전에 없는 이름을 만들어내지 마라 — 모르는 건 모른다고 하는 게 정답이다.
- 사용자가 "그거/아까 그 종목/위 목록"처럼 직전 답변을 가리키면 [직전 답변에서 언급한 종목]
  블록이 정답이다 — 새로 추측하지 말고 그 목록을 그대로 변환·설명하라.
- 개수를 말할 때는 실제 나열한 개수와 반드시 일치시켜라 (헤더 "N개"와 목록 길이가 다르면 안 됨).
- 텔레그램 메시지이므로 짧고 명확하게 (보통 3~6문장, 필요하면 리스트).

- **너는 돈이 움직이는 어떤 것도 실행할 수 없다** — 매수/매도/중지/재개/예산변경은 절대
  네가 실행하지 못한다. 이런 요청이 오면 정확한 명령어만 안내한다(실행 금지):
  /stop(매수중지) /resume(재개) /flat(전량청산) /status(현황) /sync(장부-계좌 불일치 해소)
  /sell 종목코드(지정 매도) /budget(예산 변경) /restart(봇 재시작)
- **예외적으로 관심종목 등록/해제(watch_add, watch_remove)는 네가 직접 실행할 수 있다.**
  돈이 움직이지 않고(매수 방아쇠 아님 — 감시 대상에만 들어가고 실제 매수는 여전히 전략
  가격규칙이 결정), 스카우트 AI가 이미 같은 권한으로 감시종목을 추가하고 있어 새로운
  권한이 아니다. "이거 관심종목에 넣어줘", "아까 보여준 종목들 등록해줘" 같은 요청이
  명확한 종목코드/티커로 특정되면(직전 대화·직전 리포트에서 언급된 종목 포함해 문맥으로
  풀어도 됨) watch_add에 그 코드를 담아라. 애매하면(어떤 종목인지 특정 불가) 실행하지
  말고 reply에서 어떤 종목인지 되물어라 — 확신 없이 watch_add를 채우지 마라.
- 시황 해설은 데이터 범위 안에서만. 종목 추천/투자 조언은 하지 않는다.
- 한국어로 답한다."""

ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "reply": {"type": "string", "description": "사용자에게 보낼 답변 (한국어, 3~6문장)"},
        "watch_add": {
            "type": "array", "items": {"type": "string"},
            "description": "관심종목으로 등록할 종목코드/티커. 명확히 특정될 때만 채운다"
                          " (예: [\"005930\", \"AAPL\"]). 해당 없으면 빈 배열.",
        },
        "watch_remove": {
            "type": "array", "items": {"type": "string"},
            "description": "관심종목에서 해제할 종목코드/티커. 해당 없으면 빈 배열.",
        },
    },
    "required": ["reply", "watch_add", "watch_remove"],
    "additionalProperties": False,
}


def _tail_rows(path, n: int) -> list[dict]:
    """CSV 마지막 n행을 dict로 (헤더 기준)."""
    import csv
    if not path.exists():
        return []
    with path.open() as f:
        rows = list(csv.DictReader(f))
    return rows[-n:]


# 마지막 build_context가 만든 종목 사전 — answer()가 답변 속 종목을 뽑아 기억할 때 쓴다
_NAMES_CACHE: dict[str, str] = {}


def _name_map(dr) -> dict[str, str]:
    """코드→종목명 통합 사전. 9-24 근본원인 수리: 이름이 보유/감시/시그널/스캐너 등 출처마다
    제각각 있거나 없어서, 비서가 코드를 이름으로 바꿔달라는 요청에 지어낸 이름을 냈다
    (25개 코드 → 카카오·네이버·GS칼텍스 등 무관한 목록). 봇이 실제로 확인한 이름을 한 곳에
    모아 항상 컨텍스트에 싣는다 — 사전에 없으면 '미확인'이라고 말하게 한다."""
    names: dict[str, str] = {}
    names.update({k: v for k, v in config.WHITELIST.items() if v})
    try:
        d = json.loads((config.INTEL_DIR / "watchlist.json").read_text())
        for p in d.get("picks", []):
            if p.get("name"):
                names[p["symbol"]] = p["name"]
    except (OSError, ValueError, KeyError):
        pass
    # 스캐너 CSV는 헤더 없는 (date,code,name,price) — 원문 파싱
    sc_path = config.LOG_DIR / config.SCANNER["shadow_csv"]
    if sc_path.exists():
        for line in sc_path.read_text().splitlines()[-200:]:
            cols = line.split(",")
            if len(cols) >= 3 and cols[2]:
                names[cols[1]] = cols[2]
    for r in _tail_rows(config.INTEL_DIR / "scout_picks.csv", 300):
        if r.get("name"):
            names[r["symbol"]] = r["name"]
    names.update({k: v for k, v in (dr.pf.manual_watch or {}).items() if v})
    names.update({k: v for k, v in (getattr(dr, "_names", {}) or {}).items() if v})
    return names


def _label(sym: str, names: dict[str, str]) -> str:
    return f"{sym} {names.get(sym) or '(종목명 미확인)'}"


def build_context(dr) -> str:
    """러너(DryRun 인스턴스)에서 현재 상태 스냅샷을 문자열로 만든다.

    9-24 구조 재설계 (근본원인: 산문 기억 + 사전 부재 + 뒤에서부터 잘림 + 약한 모델):
      - 모든 종목은 `코드 이름`으로만 등장 (통합 사전 _name_map), [종목 사전] 블록 별도 제공
      - [직전 답변에서 언급한 종목]을 넣어 "그거/아까 그 종목" 참조가 데이터로 이어지게
      - 핵심(보유·예약·감시·사전·기억)을 앞에, 로그(시그널·체결·일지)를 뒤에 두고
        글자 상한에 걸리면 로그부터 잘라낸다 — 핵심은 절대 잘리지 않는다
    """
    global _NAMES_CACHE
    from run_dryrun import now_kst
    names = _name_map(dr)
    _NAMES_CACHE = names

    core = [f"[봇 상태] {now_kst():%Y-%m-%d %H:%M} KST",
            f"모드: {dr.tag} | 전략: {dr.key} | 매수중지(halted): {dr.pf.halted}"]
    mism = getattr(dr, "_sync_mismatch", None)
    if dr.pf.halted and mism:
        core.append(f"매수중지 원인: 장부-계좌 불일치 ({', '.join(sorted(mism))}) — "
                    f"토스 앱에서 직접 매도한 경우 /sync 명령으로 해결 (그냥 /resume은 "
                    f"불일치가 안 풀려서 다시 멈춤). 사용자가 '왜 멈췄어' 류로 물으면 이걸로 답하라.")
    elif dr.pf.halted:
        core.append("매수중지 원인: /stop 또는 /flat 등 수동 정지 (또는 매수 주문 결과 불명 "
                    "안전정지) — 원인이 불확실하면 계좌를 직접 확인하라고 안내하라.")
    try:
        session, _ = dr.market_session()
        core.append(f"시장 세션: {session}")
    except Exception:                    # noqa: BLE001
        pass

    seen: list[str] = []                 # 컨텍스트에 등장한 종목 (사전 블록용)

    def note(sym: str) -> None:
        if sym not in seen:
            seen.append(sym)

    if dr.pf.positions:
        core.append(f"보유 포지션 총 {len(dr.pf.positions)}개:")
        for s, p in dr.pf.positions.items():
            note(s)
            live = dr.last_price(s) or p["avg_price"]
            core.append(f"  · {_label(s, names)} {p['quantity']}주 @{p['avg_price']:,.0f} → 현재 "
                        f"{live:,.0f} ({live / p['avg_price'] - 1:+.2%}) 스탑 {p.get('stop_price') or '없음'}")
    else:
        core.append("보유 포지션: 없음")
    if dr.pf.pending:
        core.append(f"다음 시가 예약(pending) 총 {len(dr.pf.pending)}개:")
        for s, pend in dr.pf.pending.items():
            note(s)
            src = "전환 스캐너" if pend.get("frac") else "기본 전략(st)"
            core.append(f"  · {_label(s, names)} {pend.get('action')} — 신호: {pend.get('reason')} "
                        f"({pend.get('date')} 종가 확정, 출처: {src})")
    watch = [s for s in dr.symbols if s not in dr.pf.positions]
    kr = [s for s in watch if config.market_of(s) == "KR"]
    us = [s for s in watch if config.market_of(s) != "KR"]
    for s in watch:
        note(s)
    core.append(f"감시 종목 총 {len(watch)}개 (KR {len(kr)} / US {len(us)}) — 보유 제외:")
    if kr:
        core.append("  KR: " + ", ".join(_label(s, names) for s in kr))
    if us:
        core.append("  US: " + ", ".join(_label(s, names) for s in us))

    ent = _load_entities()
    if ent:
        core.append(f"[직전 답변에서 언급한 종목 {len(ent)}개 — 사용자가 '그거/아까 그 종목/위 목록'"
                    f"이라고 하면 이것을 가리킨다] "
                    + ", ".join(f"{s} {n}" for s, n in ent.items()))
        for s in ent:
            note(s)

    known = [s for s in seen if names.get(s)]
    unknown = [s for s in seen if not names.get(s)]
    core.append("[종목 사전 — 코드↔이름 변환은 반드시 여기서만. 여기 없는 이름을 지어내지 말 것] "
                + ", ".join(f"{s}={names[s]}" for s in known)
                + (f" | 이름 미확인: {', '.join(unknown)}" if unknown else ""))

    core.append("참고: 모든 매수 신호는 가격 기술 규칙(Supertrend 상승 전환 등)이며 "
                "뉴스·호재 기반이 아니다. 뉴스는 매수 차단(거부권)에만 쓰인다.")
    if dr.live and dr.broker:
        try:
            core.append(f"매수가능금액 {dr.broker.buying_power():,.0f}원 "
                        f"/ 봇 예산 {dr._budget_str()} — 예산은 실현손익만큼 복리로 변한다")
        except Exception:                # noqa: BLE001
            pass
    core.append(dr.guard.summary())
    try:
        d = json.loads((config.INTEL_DIR / "watchlist.json").read_text())
        core.append(f"오늘 AI 워치리스트({d.get('date')}): "
                    + (", ".join(_label(p["symbol"], names) for p in d.get("picks", [])) or "선정 없음")
                    + f" | 시장메모: {d.get('market_note', '')}")
    except (OSError, ValueError, KeyError):
        pass

    # ── 로그류 (상한 초과 시 여기부터 잘린다) ──
    tail: list[str] = []
    sc_path = config.LOG_DIR / config.SCANNER["shadow_csv"]
    if sc_path.exists():
        rows = sc_path.read_text().strip().splitlines()[-8:]
        tail.append("전환 스캐너 최근 포착(날짜,코드,이름,신호가):\n" + "\n".join(rows))
    import brain
    memo = brain.digest(max_chars=1200)
    if memo:
        tail.append("운용 일지(공유 두뇌 — 매매 기록·저녁 리뷰·논지):\n" + memo)
    sig = _tail_rows(dr.signals_path, 12)
    if sig:
        tail.append("최근 시그널(시각 종목 액션 체결여부 사유):\n" + "\n".join(
            f"  {r['time'][5:16]} {_label(r['symbol'], names)} {r['action']} "
            f"{'체결' if r.get('executed') == 'True' else '미체결'} {r.get('reason', '')[:70]}"
            for r in sig))
    tr = _tail_rows(dr.trades_path, 6)
    if tr:
        tail.append("최근 체결(청산 완료):\n" + "\n".join(
            f"  {_label(r['symbol'], names)} {r['entry_date']}→{r['exit_date']} "
            f"{float(r['pnl_rate']):+.2%} {float(r['pnl']):+,.0f}원 {r.get('exit_reason', '')[:50]}"
            for r in tr))

    core_txt = "\n".join(core)
    budget = max(0, CONTEXT_MAX_CHARS - len(core_txt) - 1)
    tail_txt = "\n".join(tail)[:budget]
    return core_txt + ("\n" + tail_txt if tail_txt else "")


def _load_entities() -> dict[str, str]:
    try:
        d = json.loads(ENTITIES_PATH.read_text())
        return dict(d.get("entities", {}))
    except (OSError, ValueError):
        return {}


def _extract_entities(reply: str, names: dict[str, str]) -> dict[str, str]:
    """답변 본문에 등장한 종목(코드 또는 이름)을 사전 기준으로 뽑는다 — 다음 턴의 참조 기억."""
    import re
    found: dict[str, str] = {}
    for sym, name in names.items():
        hit = re.search(rf"(?<![A-Za-z0-9]){re.escape(sym)}(?![A-Za-z0-9])", reply) is not None
        if not hit and name and len(name) >= 2 and name in reply:
            hit = True
        if hit:
            found[sym] = name
        if len(found) >= 80:
            break
    return found


def _save_entities(entities: dict[str, str]) -> None:
    try:
        ENTITIES_PATH.parent.mkdir(parents=True, exist_ok=True)
        ENTITIES_PATH.write_text(json.dumps({"entities": entities}, ensure_ascii=False))
    except OSError:
        pass


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


def answer(question: str, context: str) -> dict | None:
    """질문에 답변. 이전 문답(파일 영속)이 이어지고, 봇 상태는 매번 최신으로 갱신.
    반환: {"reply": str, "watch_add": [...], "watch_remove": [...]} | None (실패 시).
    watch_add/remove 실행은 호출부(run_dryrun) 책임 — 여기선 의도만 뽑는다."""
    try:
        from datetime import datetime, timedelta, timezone
        import anthropic
        client = anthropic.Anthropic()
        kwargs = {}
        oc = config.output_config_for(config.LLM_MODELS["assistant"], "low", ANSWER_SCHEMA)
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
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = {"reply": text, "watch_add": [], "watch_remove": []}
        reply = str(data.get("reply", ""))[:3800]
        if not reply:
            return None
        history += [{"role": "user", "content": user_msg},
                    {"role": "assistant", "content": reply}]
        _save_history(history)
        # 답변에 등장한 종목을 데이터로 기억 — 다음 턴의 "그거/아까 그 종목" 참조가
        # 산문이 아니라 코드+이름 목록으로 이어진다 (9-24 근본원인 수리)
        if _NAMES_CACHE:
            _save_entities(_extract_entities(reply, _NAMES_CACHE))
        return {"reply": reply,
                "watch_add": [str(s).upper() for s in data.get("watch_add") or []][:10],
                "watch_remove": [str(s).upper() for s in data.get("watch_remove") or []][:10]}
    except Exception as e:               # noqa: BLE001 - 비서 실패가 매매를 막으면 안 됨
        print(f"  [비서] 응답 실패: {e}")
        return None
