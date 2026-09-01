"""전환 스캐너 백테스트 — st 전략을 '매일 거래대금 상위 100 전체'에 적용하면?

배경 (2026-08-31 진단): 스카우트는 이미 급등해 주목받는 종목을 골라오는데, st는
'전환 순간'만 산다 → 워치리스트 입성 시점엔 전환일이 지나 있어 매수가 거의 안 나감.
해결 가설: 워치리스트와 무관하게 상위 100 종목을 매일 종가에 스캔해 '오늘 전환'을 잡는다.

설계 원칙:
  - st의 검증된 파라미터/규칙 그대로 (ATR10×3.0, EMA200 위, RSI14<75, 다음날 시가 진입,
    밴드 손절/전환 청산). **튜닝할 그리드 없음** — 유니버스 확대의 효과만 측정한다.
  - 유니버스: 매일 거래대금(종가×거래량) 상위 100 + 가격 1,000~450,000원 (예산 50만)
    (생존편향 방지 — '그날의' 상위 100, DART 상장법인 전수의 일봉으로 자체 계산)
  - IS(4~6월)는 참고 수치, 게이트 판정은 OOS(7~8월) + 몬테카를로.

사용법: python3 research/scanner_backtest.py [events|fetch|run|all]
"""

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config                                    # noqa: E402
from strategy import indicators as ta            # noqa: E402
from strategy.base import Bar                    # noqa: E402
from toss.client import TossClient               # noqa: E402

KST = timezone(timedelta(hours=9))
DAILY_DIR = (config.DATA_DIR / "scanner" / "daily1000"
             if (len(sys.argv) > 2 and sys.argv[2] == "long")
             else config.DATA_DIR / "newsmom" / "daily")   # long=4년봉 / 기본=120봉
SC_DIR = config.DATA_DIR / "scanner"
LONG_DIR = (SC_DIR / "daily1000" if (len(sys.argv) > 2 and sys.argv[2] == "long")
            else SC_DIR / "daily400")                     # 이벤트 종목 상세 봉
MODE = "spike" if len(sys.argv) < 3 else sys.argv[2]   # spike=당일 거래대금 / stable=60일 평균
EVENTS_PATH = SC_DIR / f"flip_events_{MODE}.json"
RESULT_PATH = config.ROOT / "research" / f"scanner_result_{MODE}.json"

if MODE == "long":                      # 4년 검증: IS 2023~2025.8 / OOS 2025.9~2026.8
    WINDOW_START, WINDOW_END, IS_END = "2023-01-02", "2026-08-25", "2025-08-31"
else:
    WINDOW_START, WINDOW_END, IS_END = "2026-04-01", "2026-08-25", "2026-06-30"
TOP_N = 100
MIN_PRICE, MAX_PRICE = 1_000, 450_000
ATR_N, MULT, TREND, RSI_N, RSI_MAX = 10, 3.0, 200, 14, 75.0   # = config st 그대로
FEE, TAX, SLIP = 0.00015, 0.0015, 0.001


def tick_size(p):
    for lim, t in ((2000, 1), (5000, 5), (20000, 10), (50000, 50),
                   (200000, 100), (500000, 500)):
        if p < lim:
            return t
    return 1000


def rt(p):
    return int(p / tick_size(p)) * tick_size(p)


def load_bars(path: Path) -> list[Bar]:
    rows = json.loads(path.read_text())
    rows = sorted(rows, key=lambda r: r["timestamp"])
    return [Bar(date=r["timestamp"][:10], open=float(r["openPrice"]),
                high=float(r["highPrice"]), low=float(r["lowPrice"]),
                close=float(r["closePrice"]), volume=float(r["volume"]))
            for r in rows]


# ── 1단계: 상위 100 유니버스에서 전환 이벤트 탐지 (120봉 캐시) ──
def find_events() -> None:
    # 일별 거래대금 상위 100 구성
    day_rows: dict[str, list] = {}
    files = list(DAILY_DIR.glob("*.json"))
    print(f"일봉 캐시 {len(files)}종목 스캔")
    series: dict[str, list[Bar]] = {}
    for f in files:
        try:
            bars = load_bars(f)
        except (ValueError, KeyError):
            continue
        if len(bars) < 60:
            continue
        sym = f.stem
        series[sym] = bars
        vals = [b.close * b.volume for b in bars]
        for k, b in enumerate(bars):
            if not (WINDOW_START <= b.date <= WINDOW_END):
                continue
            if not (MIN_PRICE <= b.close <= MAX_PRICE):
                continue
            if MODE in ("stable", "long"):
                # 최근 60일 평균 거래대금 (과거 방향만 — 미래참조 없음).
                # '어제 폭등' 스파이크가 아니라 꾸준히 유동성 있는 종목을 뽑는다
                lo = max(0, k - 59)
                v = sum(vals[lo:k + 1]) / (k + 1 - lo)
                if k + 1 - lo < 40:
                    continue
            else:
                v = vals[k]
            day_rows.setdefault(b.date, []).append((v, sym))
    universe = {d: {s for _, s in sorted(v, reverse=True)[:TOP_N]}
                for d, v in day_rows.items()}

    # 종목별 supertrend/RSI 1회 계산 → 상위 100에 든 날의 전환만 이벤트로
    events = []
    for sym, bars in series.items():
        line, dirs = ta.supertrend(bars, ATR_N, MULT)
        closes = [b.close for b in bars]
        rsi = ta.rsi(closes, RSI_N)
        for i in range(31, len(bars) - 1):        # 워밍업 30봉 + 다음날 시가 필요
            d = bars[i].date
            if not (WINDOW_START <= d <= WINDOW_END):
                continue
            if dirs[i] == 1 and dirs[i - 1] == -1 and sym in universe.get(d, ()):
                if rsi[i] and rsi[i] > RSI_MAX:
                    continue                       # 과열 전환 제외 (st 규칙)
                events.append({"symbol": sym, "date": d, "i": i})
    SC_DIR.mkdir(parents=True, exist_ok=True)
    EVENTS_PATH.write_text(json.dumps(events, ensure_ascii=False))
    print(f"전환 이벤트(EMA 필터 전): {len(events)}건, "
          f"고유 종목 {len({e['symbol'] for e in events})}개 → {EVENTS_PATH}")


# ── 2단계: 이벤트 종목만 400봉 확보 (EMA200 필터용) ──
def fetch_long() -> None:
    events = json.loads(EVENTS_PATH.read_text())
    syms = sorted({e["symbol"] for e in events})
    LONG_DIR.mkdir(parents=True, exist_ok=True)
    todo = [s for s in syms if not (LONG_DIR / f"{s}.json").exists()]
    print(f"400봉 수집: {len(syms)}종목 중 신규 {len(todo)}종목")
    client = TossClient(*config.credentials())
    for k, sym in enumerate(todo):
        if k % 50 == 0 and k:
            print(f"  ...{k}/{len(todo)}")
        rows, before = [], None
        for _ in range(2):
            try:
                r = client.get_candles(sym, interval="1d", count=200, before=before)
            except Exception:               # noqa: BLE001
                break
            rows += r.get("candles", [])
            before = r.get("nextBefore")
            if not before:
                break
            time.sleep(0.06)
        (LONG_DIR / f"{sym}.json").write_text(json.dumps(rows))
    print("수집 완료")


# ── 3단계: EMA200 필터 + 체결 시뮬레이션 ──
def simulate() -> None:
    events = json.loads(EVENTS_PATH.read_text())
    trades, skipped_ema, open_end = [], 0, 0
    for ev in events:
        sym = ev["symbol"]
        lp = LONG_DIR / f"{sym}.json"
        if not lp.exists():
            continue
        bars = load_bars(lp)
        idx = {b.date: k for k, b in enumerate(bars)}
        if ev["date"] not in idx:
            continue
        i = idx[ev["date"]]
        if i + 1 >= len(bars) or i < TREND:
            if i < TREND:
                skipped_ema += 1              # 상장 200일 미만 → 봇도 봉부족으로 스킵
            continue
        closes = [b.close for b in bars]
        ema = ta.ema(closes, TREND)
        if ema[i] is None or closes[i] < ema[i]:
            skipped_ema += 1
            continue
        line, dirs = ta.supertrend(bars, ATR_N, MULT)

        entry = rt(bars[i + 1].open * (1 + SLIP))
        if entry <= 0:
            continue
        # 실전과 동일: 매수 즉시 거래소 백스톱(-8%, config.BACKSTOP_STOP_RATE['st']).
        # 전환 직후 밴드는 -20~40% 아래일 수 있어 — 실전 최대손실은 백스톱이 결정한다
        backstop = entry * (1 - config.BACKSTOP_STOP_RATE["st"])
        exit_px = exit_d = reason = None
        for j in range(i + 1, len(bars)):
            stop = max(line[j - 1] or 0, backstop)   # 밴드 vs 백스톱 중 높은 쪽 먼저
            if stop and bars[j].open <= stop:
                exit_px, exit_d, reason = bars[j].open, bars[j].date, "stop_gap"
                break
            if stop and bars[j].low <= stop:
                exit_px, exit_d, reason = min(stop, bars[j].open), bars[j].date, "stop"
                break
            if dirs[j] == -1:                 # 종가 전환 → 다음날 시가 청산
                if j + 1 < len(bars):
                    exit_px, exit_d, reason = bars[j + 1].open, bars[j + 1].date, "flip"
                else:
                    exit_px, exit_d, reason = bars[j].close, bars[j].date, "flip_eod"
                break
        if exit_px is None:                   # 창 끝까지 보유 중 → 마지막 종가 평가
            exit_px, exit_d, reason = bars[-1].close, bars[-1].date, "open_end"
            open_end += 1
        sell = rt(exit_px * (1 - SLIP))
        pnl = sell * (1 - FEE - TAX) / (entry * (1 + FEE)) - 1
        trades.append({"symbol": sym, "date": ev["date"], "entry": entry,
                       "exit": sell, "exit_date": exit_d, "pnl_pct": pnl,
                       "reason": reason})

    def stats(ts):
        if not ts:
            return {"n": 0}
        r = [t["pnl_pct"] for t in ts]
        w = [x for x in r if x > 0]
        return {"n": len(r), "expectancy_pct": sum(r) / len(r) * 100,
                "win_rate": len(w) / len(r), "total_pct": sum(r) * 100,
                "best": max(r) * 100, "worst": min(r) * 100}

    is_t = [t for t in trades if t["date"] <= IS_END]
    oos_t = [t for t in trades if t["date"] > IS_END]
    s_is, s_oos = stats(is_t), stats(oos_t)
    print(f"EMA200 필터 탈락/봉부족: {skipped_ema}건 | 미청산(창 끝 평가): {open_end}건")
    print(f"\nIS  (4~6월): {s_is.get('n', 0)}건, 기대값 {s_is.get('expectancy_pct', 0):+.2f}%/건, "
          f"승률 {s_is.get('win_rate', 0):.0%}")
    print(f"OOS (7~8월): {s_oos.get('n', 0)}건, 기대값 {s_oos.get('expectancy_pct', 0):+.2f}%/건, "
          f"승률 {s_oos.get('win_rate', 0):.0%}, 합계 {s_oos.get('total_pct', 0):+.1f}%")

    mc = None
    if s_oos.get("n", 0) >= 10:
        from types import SimpleNamespace
        from backtest.montecarlo import run_monte_carlo
        mc = run_monte_carlo([SimpleNamespace(pnl_rate=t["pnl_pct"]) for t in oos_t],
                             n_sims=3000)
        print(f"MC(3000회): 손실확률 {mc.prob_loss:.1%}, 최악5% {mc.ret_p5:+.1%}, "
              f"중앙값 {mc.ret_p50:+.1%}, MDD최악5% {mc.mdd_p95:+.1%}")

    gate = {"oos_trades_ge_30": s_oos.get("n", 0) >= 30,
            "oos_expectancy_pos": s_oos.get("expectancy_pct", -1) > 0,
            "mc_loss_lt_30": bool(mc and mc.prob_loss < 0.30)}
    passed = all(gate.values())
    print(f"\n검증 게이트: {'✅ 통과' if passed else '❌ 기각'} — {gate}")
    RESULT_PATH.write_text(json.dumps({
        "strategy": "st-scanner(전환 스캐너)", "window": [WINDOW_START, WINDOW_END],
        "generated_at": datetime.now(KST).isoformat(timespec="seconds"),
        "universe": f"일별 거래대금 상위 {TOP_N} (가격 {MIN_PRICE}~{MAX_PRICE})",
        "params": "st 검증 파라미터 고정 (튜닝 없음)",
        "is": s_is, "oos": s_oos,
        "mc": ({"prob_loss": mc.prob_loss, "ret_p5": mc.ret_p5, "ret_p50": mc.ret_p50,
                "mdd_p95": mc.mdd_p95} if mc else None),
        "gate": gate, "passed": passed, "open_end": open_end,
        "oos_trades": oos_t,
    }, ensure_ascii=False, indent=1))
    print(f"결과 저장: {RESULT_PATH}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    if cmd in ("events", "all"):
        find_events()
    if cmd in ("fetch", "all"):
        fetch_long()
    if cmd in ("run", "all"):
        simulate()
