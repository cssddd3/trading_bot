"""LLM 종목 스카우트 — 오늘의 관심종목(워치리스트)을 뽑는다.

역할 경계 (중요):
  1. 후보군은 **규칙**이 만든다: 거래대금 상위 랭킹 → 1주 가격이 주문 한도 이내 →
     거래정지/투자경고 아님 → 상장 정상(ACTIVE).
  2. LLM은 **후보 안에서 고르기만** 한다. 후보에 없는 종목을 지어내면 버려진다.
  3. 뽑힌 종목도 매수하려면 여전히 전략 시그널 + RiskGuard + 뉴스필터를 통과해야 한다.

산출물: logs/watchlist.json (당일 날짜 포함). 드라이런이 config.SCOUT["use_watchlist"]=True면
이 파일의 종목을 그날의 화이트리스트에 추가한다. 사용 예:

  python3 scout.py            # 오늘의 워치리스트 생성
  python3 scout.py --show     # 저장된 워치리스트 보기
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone, timedelta

import config
from news import fetch_headlines
from toss.client import TossClient

KST = timezone(timedelta(hours=9))
WATCHLIST_PATH = config.LOG_DIR / "watchlist.json"
# 감사 H1: 레버리지/인버스 상품 제외 (스카우트·스캐너 공용 — 단일 정의)
LEVERAGE_WORDS = ("레버리지", "인버스", "2X", "3X", "BULL", "BEAR", "ULTRA", "곱버")

SCHEMA = {
    "type": "object",
    "properties": {
        "picks": {
            "type": "array",
            "description": "매매 관심종목. 근거가 강한 순서로, 최대 max_picks개. 확신 없으면 빈 배열.",
            "items": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "후보 목록에 있는 6자리 코드만"},
                    "score": {"type": "number", "description": "매력도 0~1"},
                    "thesis": {"type": "string", "description": "선정 근거 1~2문장 (한국어)"},
                },
                "required": ["symbol", "score", "thesis"],
                "additionalProperties": False,
            },
        },
        "market_note": {"type": "string", "description": "오늘 시장 분위기 한 줄 요약"},
    },
    "required": ["picks", "market_note"],
    "additionalProperties": False,
}

SYSTEM = """너는 자동매매 봇의 종목 스카우트다. 제시된 후보 목록 **안에서만** 골라라.
봇의 전략은 추세추종/변동성돌파 계열이므로, 다음을 우선한다:
- 명확한 재료(실적/수주/신제품 등 사실 기반)가 있고 거래대금이 실린 종목
- 뉴스가 이미 다 반영된 급등 피로 종목, 테마성 급등락(정치·풍문)은 감점
- 유동성이 낮거나 뉴스가 거의 없는 종목은 고르지 않는다
확신이 없으면 적게 고르거나 아예 고르지 마라. 고르지 않는 것도 유효한 답이다."""


def _fetch_rankings(client: TossClient, market: str,
                    count: int | None = None) -> list[dict]:
    """거래대금 상위 랭킹. 장중엔 실시간, 비장중엔 1d 폴백."""
    n = count or config.SCOUT["candidate_count"]
    rows = []
    try:
        rows = client.get_rankings(type=config.SCOUT["candidate_rank_type"],
                                   market_country=market, duration="realtime",
                                   count=n, exclude_caution=True)
    except Exception:                    # noqa: BLE001 - realtime 미지원/오류 시 폴백
        pass
    if not rows:
        rows = client.get_rankings(type=config.SCOUT["candidate_rank_type"],
                                   market_country=market, duration="1d",
                                   count=n, exclude_caution=True)
    return rows


def quick_candidate_symbols(client: TossClient, market: str = "KR") -> set[str]:
    """3분 주기용 저비용 확인 — 랭킹 1콜로 후보 심볼만 뽑는다 (LLM/경고조회 없음).

    러너가 이 집합을 직전 값과 비교해서, 새 얼굴이 나타났을 때만 전체 스카우트를 돌린다.
    트리거는 상위 trigger_count(30)만 본다 — 하위권은 순위 순환이 잦아 소음이라서.
    """
    import os
    rows = _fetch_rankings(client, market)[: config.SCOUT.get("trigger_count", 30)]
    if market == "KR":
        config.load_env()
        cap = config.effective_budget("KR") if os.getenv("LIVE_TRADING") == "1" \
            else config.RISK.max_order_amount
        rows = [r for r in rows if float(r["price"]["lastPrice"]) <= cap]
    return {r["symbol"] for r in rows}


def build_candidates(client: TossClient, market: str = "KR",
                     verbose: bool = True) -> list[dict]:
    """규칙 기반 후보군: 거래대금 상위 → 가격/경고/상태 필터."""
    import os
    S = config.SCOUT
    rows = _fetch_rankings(client, market)
    # KR: 1주 가격이 예산 이내인 것만 (미국은 금액 기반 소수점 매수가 가능해 가격 제한 불필요)
    if market == "KR":
        config.load_env()
        cap = config.effective_budget("KR") if os.getenv("LIVE_TRADING") == "1" \
            else config.RISK.max_order_amount
        rows = [r for r in rows if float(r["price"]["lastPrice"]) <= cap]
    symbols = [r["symbol"] for r in rows]
    if not symbols:
        return []

    infos = {i["symbol"]: i for i in client.get_stocks(symbols)}
    out = []
    for r in rows:
        sym = r["symbol"]
        info = infos.get(sym, {})
        if info.get("status") not in (None, "ACTIVE"):
            continue
        # 감사 H1: 레버리지/인버스 ETF 제외 (일변동 ±10%에 -3% 손절은 노이즈 안쪽)
        name_all = (str(info.get("name", "")) + " " + str(info.get("englishName", ""))).upper()
        if any(w in name_all for w in LEVERAGE_WORDS):
            continue
        # 신규상장 60일 미만 제외 (워밍업 봉 부족 + 변동성 비정상)
        list_date = info.get("listDate") or ""
        if list_date and (datetime.now(KST).date()
                          - datetime.fromisoformat(list_date).date()).days < 60:
            continue
        kr = info.get("koreanMarketDetail") or {}
        if kr.get("krxTradingSuspended") or kr.get("liquidationTrading"):
            continue
        try:
            warns = {w.get("warningType") for w in client.get_warnings(sym)}
        except Exception:               # noqa: BLE001
            warns = set()
        if warns & {"LIQUIDATION_TRADING", "INVESTMENT_WARNING", "INVESTMENT_RISK",
                    "OVERHEATED"}:
            continue
        out.append({
            "symbol": sym,
            "name": info.get("name", sym),
            "last_price": float(r["price"]["lastPrice"]),
            "change_rate": float(r["price"]["changeRate"]),
            "trading_amount": float(r["tradingAmount"]),
            "market": info.get("market", ""),
        })
        time.sleep(0.1)                  # warnings 호출 rate limit 여유
        if len(out) >= config.SCOUT["max_candidates"]:
            break
    if verbose:
        print(f"규칙 필터 통과 후보: {len(out)}개")
    return out


def ask_claude(candidates: list[dict], market: str = "KR") -> dict | None:
    import anthropic

    S = config.SCOUT
    blocks = []
    for c in candidates:
        try:
            heads = fetch_headlines(f"{c['name']} 주식", max_items=6)
        except Exception:               # noqa: BLE001
            heads = []
        c["_news_count"] = len(heads)
        hl = "\n".join(f"    - {h.title}" for h in heads) or "    (최근 48시간 뉴스 없음)"
        if market == "US":
            px = f"현재가 ${c['last_price']:,.2f}, 거래대금 ${c['trading_amount'] / 1e6:,.0f}M"
        else:
            px = f"현재가 {c['last_price']:,.0f}원, 거래대금 {c['trading_amount'] / 1e8:,.0f}억"
        blocks.append(
            f"[{c['symbol']}] {c['name']} ({c['market']}) — {px}, "
            f"전일대비 {c['change_rate']:+.2%}\n{hl}")

    prompt = (f"오늘 날짜: {datetime.now(KST):%Y-%m-%d (%a)} | 시장: {market}\n"
              f"후보 {len(candidates)}개 (거래대금 상위 → 규칙 필터 통과):\n\n"
              + "\n\n".join(blocks)
              + f"\n\n이 중 최대 {S['max_picks']}개를 골라라.")

    client = anthropic.Anthropic()
    model = config.LLM_MODELS["scout"]
    resp = client.messages.create(
        model=model,
        max_tokens=4096,
        system=SYSTEM,
        output_config=config.output_config_for(model, "medium", SCHEMA),
        messages=[{"role": "user", "content": prompt}],
    )
    if resp.stop_reason == "refusal":
        return None
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None


def run_scout(client: TossClient | None = None, market: str = "KR",
              verbose: bool = True) -> dict | None:
    """한 시장의 스카우트를 실행하고, 그 시장의 picks만 갈아끼운다 (다른 시장 것은 유지)."""
    from llm_filter import NewsFilter
    ok, why = NewsFilter.available()
    if not ok:
        print(f"[스카우트] LLM 사용 불가 ({why}) — 건너뜀")
        return None

    client = client or TossClient(*config.credentials())
    candidates = build_candidates(client, market=market, verbose=verbose)
    if not candidates:
        print(f"[스카우트/{market}] 후보 없음")
        return None
    result = ask_claude(candidates, market=market)
    if result is None:
        print(f"[스카우트/{market}] LLM 판정 실패")
        return None

    valid = {c["symbol"]: c for c in candidates}
    picks = []
    for p in result.get("picks", [])[: config.SCOUT["max_picks"]]:
        if p["symbol"] not in valid:     # 후보에 없는 종목을 지어냈으면 폐기
            continue
        picks.append({"symbol": p["symbol"], "name": valid[p["symbol"]]["name"],
                      "score": max(0.0, min(1.0, float(p["score"]))),
                      "thesis": str(p["thesis"])[:300], "market": market})

    today = datetime.now(KST).date().isoformat()
    prev = load_watchlist() or {}
    markets = prev.get("markets", {}) if prev.get("date") == today else {}
    markets[market] = {
        "generated_at": datetime.now(KST).isoformat(timespec="seconds"),
        "market_note": str(result.get("market_note", ""))[:300],
        "picks": picks,
    }
    merged = [p for m in markets.values() for p in m["picks"]]
    data = {"date": today,
            "generated_at": datetime.now(KST).isoformat(timespec="seconds"),
            "market_note": markets[market]["market_note"],
            "model": config.LLM_MODELS["scout"],
            "markets": markets,
            "picks": merged}
    WATCHLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    WATCHLIST_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    if verbose:
        print(f"\n시장 요약: {data['market_note']}")
        for p in picks:
            print(f"  ✓ {p['symbol']} {p['name']} (점수 {p['score']:.2f}) — {p['thesis']}")
        if not picks:
            print("  (오늘은 고를 만한 종목 없음 — 이것도 유효한 판단)")
        print(f"저장: {WATCHLIST_PATH}")
    return data


def load_watchlist(today_only: bool = True) -> dict | None:
    """저장된 워치리스트. today_only면 오늘 날짜가 아닐 때 None."""
    if not WATCHLIST_PATH.exists():
        return None
    try:
        data = json.loads(WATCHLIST_PATH.read_text())
    except json.JSONDecodeError:
        return None
    if today_only and data.get("date") != datetime.now(KST).date().isoformat():
        return None
    return data


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true", help="저장된 워치리스트 출력")
    ap.add_argument("--market", default="KR", choices=["KR", "US"])
    args = ap.parse_args()
    if args.show:
        data = load_watchlist(today_only=False)
        print(json.dumps(data, ensure_ascii=False, indent=2) if data else "(없음)")
        sys.exit(0)
    run_scout(market=args.market)
