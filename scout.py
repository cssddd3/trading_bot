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

**추적 결과 경고 (9-17)**: 이 봇의 과거 추천 91건을 사후 채점한 결과 73%가 하락했고
(평균 -3.4%, 지수 하락폭의 2~8배), 절반 가까이가 스스로 "급등"을 근거로 들었다.
후보 목록 자체가 '오늘 거래대금 급증'(=오늘 이미 크게 움직인) 종목 위주라 어쩔 수 없이
편향돼 있다 — 이 봇의 검증된 백테스트에서 "당일 급등 추격 매수"는 이미 두 번(변동성돌파,
뉴스모멘텀) 손실로 기각된 패턴과 정확히 같다. 너는 매수를 하지 않지만, 관심종목으로
올리는 것 자체가 사용자에게 "이게 좋아 보인다"는 신호로 읽힌다 — 신중하라.

원칙:
- **전일대비 등락률을 반드시 확인하고, 당일 이미 +8% 이상 오른 종목은 원칙적으로 감점.**
  선정하려면 "왜 지금부터도 남았는지"를 재료로 명시해야 한다 (단순히 "급등, 거래대금 실림"
  만으로는 근거 부족 — 그 자체가 경고 신호다)
- 후보에는 두 종류 태그가 있다: **🔥오늘 거래대금 상위**(오늘 이미 크게 움직였을 위험,
  위 규칙 적용) / **🐢안정유니버스**(평소에도 유동성 있지만 오늘 급등은 아님 — 신선한
  재료가 있다면 오히려 이쪽을 우선 고려하라. '아직 안 움직였다'는 게 약점이 아니다)
- 명확한 재료(실적/수주/신제품 등 사실 기반)가 있고, 아직 초기 반응 단계인 종목을 우선
- 뉴스가 이미 다 반영된 급등 피로 종목, 테마성 급등락(정치·풍문)은 제외
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


def _quality_ok(sym: str, info: dict, client: TossClient) -> bool:
    """레버리지/신규상장/거래정지/경고종목 공통 필터 (spike·steady 후보 공용)."""
    if info.get("status") not in (None, "ACTIVE"):
        return False
    # 감사 H1: 레버리지/인버스 ETF 제외 (일변동 ±10%에 -3% 손절은 노이즈 안쪽)
    name_all = (str(info.get("name", "")) + " " + str(info.get("englishName", ""))).upper()
    if any(w in name_all for w in LEVERAGE_WORDS):
        return False
    # 신규상장 60일 미만 제외 (워밍업 봉 부족 + 변동성 비정상)
    list_date = info.get("listDate") or ""
    if list_date and (datetime.now(KST).date()
                      - datetime.fromisoformat(list_date).date()).days < 60:
        return False
    kr = info.get("koreanMarketDetail") or {}
    if kr.get("krxTradingSuspended") or kr.get("liquidationTrading"):
        return False
    try:
        warns = {w.get("warningType") for w in client.get_warnings(sym)}
    except Exception:                   # noqa: BLE001
        warns = set()
    if warns & {"LIQUIDATION_TRADING", "INVESTMENT_WARNING", "INVESTMENT_RISK", "OVERHEATED"}:
        return False
    return True


def _steady_candidates(client: TossClient, exclude: set, n: int,
                       infos: dict, price_cap: float | None) -> list[dict]:
    """9-17: '오늘 급등' 랭킹이 아니라 스캐너가 이미 검증한 60일-평균 유니버스에서
    후보를 뽑는다 (스카우트 추천 73% 하락 사후채점 — 원인은 '오늘 급등' 편향된 후보 풀).
    조용한 순(당일 등락 작은 순)으로 골라 진짜 대안이 되게 한다."""
    seed_path = config.DATA_DIR / "scanner" / "universe_seed.json"
    try:
        seed = json.loads(seed_path.read_text()).get("symbols", [])
    except (OSError, ValueError):
        return []
    import random
    rng = random.Random(datetime.now(KST).date().toordinal())   # 날짜별 고정 샘플
    pool = [s for s in seed if s not in exclude]
    rng.shuffle(pool)
    pool = pool[: n * 4]                 # 후보군을 넉넉히 뽑아 필터링 후 n개로 압축
    out = []
    for sym in pool:
        try:
            r = client.get_candles(sym, interval="1d", count=2)
            bars = sorted(r.get("candles", []), key=lambda b: b["timestamp"])
            if len(bars) < 2:
                continue
            price = float(bars[-1]["closePrice"])
            change = price / float(bars[-2]["closePrice"]) - 1
        except Exception:                # noqa: BLE001
            continue
        if price_cap and price > price_cap:
            continue
        info = infos.get(sym)
        if info is None:
            try:
                info = client.get_stocks([sym])[0]
            except Exception:            # noqa: BLE001
                continue
        if not _quality_ok(sym, info, client):
            continue
        out.append({"symbol": sym, "name": info.get("name", sym), "last_price": price,
                    "change_rate": change, "trading_amount": 0.0,
                    "market": info.get("market", ""), "source": "steady"})
        time.sleep(0.05)
        if len(out) >= n:
            break
    return out


def build_candidates(client: TossClient, market: str = "KR",
                     verbose: bool = True) -> list[dict]:
    """규칙 기반 후보군: 거래대금 상위(spike) + 60일평균 유니버스(steady) → 필터."""
    import os
    S = config.SCOUT
    rows = _fetch_rankings(client, market)
    price_cap = None
    # KR: 1주 가격이 예산 이내인 것만 (미국은 금액 기반 소수점 매수가 가능해 가격 제한 불필요)
    if market == "KR":
        config.load_env()
        price_cap = config.effective_budget("KR") if os.getenv("LIVE_TRADING") == "1" \
            else config.RISK.max_order_amount
        rows = [r for r in rows if float(r["price"]["lastPrice"]) <= price_cap]
    symbols = [r["symbol"] for r in rows]
    if not symbols:
        return []

    infos = {i["symbol"]: i for i in client.get_stocks(symbols)}
    steady_slots = S.get("steady_slots", 0) if market == "KR" else 0
    spike_slots = max(1, S["max_candidates"] - steady_slots)
    out = []
    for r in rows:
        sym = r["symbol"]
        info = infos.get(sym, {})
        if not _quality_ok(sym, info, client):
            continue
        out.append({
            "symbol": sym,
            "name": info.get("name", sym),
            "last_price": float(r["price"]["lastPrice"]),
            "change_rate": float(r["price"]["changeRate"]),
            "trading_amount": float(r["tradingAmount"]),
            "market": info.get("market", ""),
            "source": "spike",
        })
        time.sleep(0.1)                  # warnings 호출 rate limit 여유
        if len(out) >= spike_slots:
            break
    if steady_slots:
        out += _steady_candidates(client, exclude={c["symbol"] for c in out},
                                  n=steady_slots, infos=infos, price_cap=price_cap)
    if verbose:
        n_steady = sum(1 for c in out if c.get("source") == "steady")
        print(f"규칙 필터 통과 후보: {len(out)}개 (오늘상위 {len(out)-n_steady} / "
              f"안정유니버스 {n_steady})")
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
        warn = " ⚠️당일 급등(추격매수 위험)" if c["change_rate"] >= 0.08 else ""
        tag = "🐢안정유니버스(오늘 급등 아님)" if c.get("source") == "steady" else "🔥오늘 거래대금 상위"
        blocks.append(
            f"[{c['symbol']}] {c['name']} ({c['market']}) {tag} — "
            f"전일대비 {c['change_rate']:+.2%}{warn}, {px}\n{hl}")

    # 공유 두뇌: 어제까지의 운용 일지를 읽고 이어서 판단한다 (기억의 연속성)
    import brain
    memo = brain.digest(max_chars=1800)
    memo_block = (f"\n\n[운용 일지 — 최근 매매·리뷰·보유 논지. 이 맥락을 이어서 판단하라]\n"
                  f"{memo}\n" if memo else "")
    prompt = (f"오늘 날짜: {datetime.now(KST):%Y-%m-%d (%a)} | 시장: {market}"
              + memo_block
              + f"\n후보 {len(candidates)}개 (거래대금 상위 → 규칙 필터 통과):\n\n"
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
