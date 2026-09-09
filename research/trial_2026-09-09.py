"""9-09 리서치 트라이얼 — 이슈 #13(출구 래칫) #15(타임스탑) #14(변동성 사이징) 검증.

베이스라인 = 현재 프로덕션 스캐너 (st 파라미터 고정 + EMA200 + 바닥권 필터(250일 고점 70%)
+ 유니버스 200, 4년 장기창). 이벤트는 data/scanner/flip_events_long200.json 재사용
(TOP_N=200, RSI<75 사전 필터 — find_events()와 동일 로직으로 이미 생성됨).

트라이얼은 전부 베이스라인과 '같은 진입'을 공유하고 청산/사이징만 바꾼다 — 그래야
차이가 온전히 그 변경 하나의 효과다 (다중검정 최소화).

사용법: python3 research/trial_2026-09-09.py [baseline|ratchet|timestop|volsize|fetch_index|all]
"""

import json
import sys
import time
from datetime import timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config                                     # noqa: E402
from strategy import indicators as ta             # noqa: E402
from strategy.base import Bar                     # noqa: E402
from backtest.montecarlo import run_monte_carlo    # noqa: E402

KST = timezone(timedelta(hours=9))
SC_DIR = config.DATA_DIR / "scanner"
LONG_DIR = SC_DIR / "daily1000"
EVENTS_PATH = SC_DIR / "flip_events_long200.json"
RESULT_PATH = config.ROOT / "research" / "trial_2026-09-09_result.json"

WINDOW_START, WINDOW_END, IS_END = "2022-09-01", "2026-08-25", "2025-08-31"
ATR_N, MULT, TREND, RSI_N, RSI_MAX = 10, 3.0, 200, 14, 75.0
FEE, TAX, SLIP = 0.00015, 0.0015, 0.001
POSITION_FRAC = 0.5   # 스캐너 승격 전제 — 전부 이 분할가정으로 MC 채점


def tick_size(p):
    for lim, t in ((2000, 1), (5000, 5), (20000, 10), (50000, 50),
                   (200000, 100), (500000, 500)):
        if p < lim:
            return t
    return 1000


def rt(p):
    return int(p / tick_size(p)) * tick_size(p)


def load_bars(path: Path) -> list[Bar]:
    rows = sorted(json.loads(path.read_text()), key=lambda r: r["timestamp"])
    return [Bar(date=r["timestamp"][:10], open=float(r["openPrice"]),
                high=float(r["highPrice"]), low=float(r["lowPrice"]),
                close=float(r["closePrice"]), volume=float(r["volume"]))
            for r in rows]


def stats(rets: list[float]) -> dict:
    if not rets:
        return {"n": 0}
    w = [x for x in rets if x > 0]
    top5 = sorted(rets, reverse=True)[:max(1, len(rets) // 20)]
    total = sum(rets)
    return {"n": len(rets), "expectancy_pct": sum(rets) / len(rets) * 100,
            "win_rate": len(w) / len(rets), "total_pct": total * 100,
            "top5pct_retention": (sum(top5) / total) if total else None,
            "best": max(rets) * 100, "worst": min(rets) * 100}


def gate_check(oos_rets: list[float]) -> dict:
    mc = run_monte_carlo(
        [SimpleNamespace(pnl_rate=r * POSITION_FRAC) for r in oos_rets], n_sims=3000)
    g = {"oos_trades_ge_30": len(oos_rets) >= 30,
         "oos_expectancy_pos": (sum(oos_rets) / len(oos_rets) > 0) if oos_rets else False,
         "mc_loss_lt_30": bool(mc and mc.prob_loss < 0.30)}
    return {"gate": g, "passed": all(g.values()),
            "mc": ({"prob_loss": mc.prob_loss, "ret_p5": mc.ret_p5, "ret_p50": mc.ret_p50,
                    "mdd_p95": mc.mdd_p95} if mc else None)}


# ── 진입 후보 구성: EMA200 + 바닥권(250일 고점 70%) 필터를 통과한 이벤트만 ──
def build_entries() -> list[dict]:
    events = json.loads(EVENTS_PATH.read_text())
    entries = []
    for ev in events:
        sym = ev["symbol"]
        p = LONG_DIR / f"{sym}.json"
        if not p.exists():
            continue
        bars = load_bars(p)
        idx = {b.date: k for k, b in enumerate(bars)}
        if ev["date"] not in idx:
            continue
        i = idx[ev["date"]]
        if i + 1 >= len(bars) or i < max(TREND, 250):
            continue
        if not (WINDOW_START <= bars[i].date <= WINDOW_END):
            continue
        closes = [b.close for b in bars]
        ema = ta.ema(closes, TREND)
        if ema[i] is None or closes[i] < ema[i]:
            continue
        prior_high = max(b.high for b in bars[i - 250:i])
        if closes[i] < 0.70 * prior_high:
            continue                          # 바닥권 필터 (검증된 규칙)
        line, dirs = ta.supertrend(bars, ATR_N, MULT)
        atr = ta.atr(bars, ATR_N) if hasattr(ta, "atr") else None
        entries.append({"symbol": sym, "date": ev["date"], "i": i, "bars": bars,
                        "line": line, "dirs": dirs, "atr": atr})
    print(f"진입 후보(EMA200+바닥권 통과): {len(entries)}건")
    return entries


def _atr_series(bars: list[Bar], n: int) -> list[float]:
    trs = [bars[0].high - bars[0].low]
    for k in range(1, len(bars)):
        trs.append(max(bars[k].high - bars[k].low,
                       abs(bars[k].high - bars[k - 1].close),
                       abs(bars[k].low - bars[k - 1].close)))
    out, buf = [], []
    for t in trs:
        buf.append(t)
        if len(buf) > n:
            buf.pop(0)
        out.append(sum(buf) / len(buf))
    return out


# ── 베이스라인: 현재 프로덕션 청산 (밴드 vs 백스톱, 전환 시 다음날 시가) ──
def run_baseline(entries: list[dict]) -> list[dict]:
    trades = []
    for e in entries:
        bars, line, dirs, i, sym = e["bars"], e["line"], e["dirs"], e["i"], e["symbol"]
        entry = rt(bars[i + 1].open * (1 + SLIP))
        if entry <= 0:
            continue
        backstop = entry * (1 - config.BACKSTOP_STOP_RATE["st"])
        exit_px = exit_d = None
        for j in range(i + 1, len(bars)):
            stop = max(line[j - 1] or 0, backstop)
            if stop and bars[j].open <= stop:
                exit_px, exit_d = bars[j].open, bars[j].date; break
            if stop and bars[j].low <= stop:
                exit_px, exit_d = min(stop, bars[j].open), bars[j].date; break
            if dirs[j] == -1:
                if j + 1 < len(bars):
                    exit_px, exit_d = bars[j + 1].open, bars[j + 1].date
                else:
                    exit_px, exit_d = bars[j].close, bars[j].date
                break
        if exit_px is None:
            exit_px, exit_d = bars[-1].close, bars[-1].date
        sell = rt(exit_px * (1 - SLIP))
        pnl = sell * (1 - FEE - TAX) / (entry * (1 + FEE)) - 1
        trades.append({"symbol": sym, "date": e["date"], "exit_date": exit_d, "pnl": pnl})
    return trades


# ── 트라이얼 A: ATR 래칫 (거리-래칫 변형 — 손절 거리는 줄어들 수만 있음) ──
def run_ratchet(entries: list[dict], cap_mult: float = 1.5) -> list[dict]:
    trades = []
    for e in entries:
        bars, dirs, i, sym = e["bars"], e["dirs"], e["i"], e["symbol"]
        atr = _atr_series(bars, ATR_N)
        entry = rt(bars[i + 1].open * (1 + SLIP))
        if entry <= 0:
            continue
        backstop = entry * (1 - config.BACKSTOP_STOP_RATE["st"])
        entry_atr = atr[i]
        highest_close = bars[i + 1].open
        stop = entry - min(atr[i + 1], cap_mult * entry_atr) * MULT if entry_atr else backstop
        exit_px = exit_d = None
        for j in range(i + 1, len(bars)):
            highest_close = max(highest_close, bars[j].close)
            dist = min(atr[j], cap_mult * entry_atr) * MULT if entry_atr else 0
            new_stop = highest_close - dist
            stop = max(stop, new_stop, backstop)   # 거리 축소만 허용 (래칫 — 절대 완화 안 함)
            if bars[j].open <= stop:
                exit_px, exit_d = bars[j].open, bars[j].date; break
            if bars[j].low <= stop:
                exit_px, exit_d = min(stop, bars[j].open), bars[j].date; break
            if dirs[j] == -1:
                if j + 1 < len(bars):
                    exit_px, exit_d = bars[j + 1].open, bars[j + 1].date
                else:
                    exit_px, exit_d = bars[j].close, bars[j].date
                break
        if exit_px is None:
            exit_px, exit_d = bars[-1].close, bars[-1].date
        sell = rt(exit_px * (1 - SLIP))
        pnl = sell * (1 - FEE - TAX) / (entry * (1 + FEE)) - 1
        trades.append({"symbol": sym, "date": e["date"], "exit_date": exit_d, "pnl": pnl})
    return trades


# ── 트라이얼 B: 타임스탑 (N거래일 후 +threshold*ATR 미만이면 익일 시가 청산) ──
def run_timestop(entries: list[dict], base_trades_by_key: dict,
                 n_days: int = 15, thr_mult: float = 1.0) -> list[dict]:
    trades = []
    by_key = {(t["symbol"], t["date"]): t for t in base_trades_by_key}
    for e in entries:
        bars, i, sym = e["bars"], e["i"], e["symbol"]
        base = by_key.get((sym, e["date"]))
        if not base:
            continue
        atr = _atr_series(bars, ATR_N)
        entry = rt(bars[i + 1].open * (1 + SLIP))
        entry_atr = atr[i] or 0
        base_exit_idx = None
        idx_map = {b.date: k for k, b in enumerate(bars)}
        base_exit_idx = idx_map.get(base["exit_date"], len(bars) - 1)
        cutoff_idx = i + 1 + n_days
        if cutoff_idx < base_exit_idx and cutoff_idx < len(bars):
            close_at_cut = bars[cutoff_idx].close
            if close_at_cut - entry < thr_mult * entry_atr:
                exit_j = min(cutoff_idx + 1, len(bars) - 1)
                exit_px = bars[exit_j].open
                sell = rt(exit_px * (1 - SLIP))
                pnl = sell * (1 - FEE - TAX) / (entry * (1 + FEE)) - 1
                trades.append({"symbol": sym, "date": e["date"],
                              "exit_date": bars[exit_j].date, "pnl": pnl})
                continue
        trades.append({"symbol": sym, "date": e["date"],
                       "exit_date": base["exit_date"], "pnl": base["pnl"]})
    return trades


# ── 트라이얼 C: 변동성 체제 사이징 (KODEX200 20일 실현변동성 상위 20%면 frac 절반) ──
def fetch_index_bars(sym: str = "069500") -> None:
    from toss.client import TossClient
    client = TossClient(*config.credentials())
    rows, before = [], None
    for _ in range(8):
        try:
            r = client.get_candles(sym, interval="1d", count=200, before=before)
        except Exception as e:                # noqa: BLE001
            print("fetch 실패:", e); break
        rows += r.get("candles", [])
        before = r.get("nextBefore")
        if not before:
            break
        time.sleep(0.1)
    (SC_DIR / f"{sym}_index.json").write_text(json.dumps(rows))
    print(f"{sym} 지수 캔들 {len(rows)}건 저장")


def _vol_regime_map(sym: str = "069500") -> dict:
    p = SC_DIR / f"{sym}_index.json"
    bars = load_bars(p)
    closes = [b.close for b in bars]
    import math
    rets = [math.log(closes[k] / closes[k - 1]) for k in range(1, len(closes))]
    vol20 = [None] * len(closes)
    for k in range(20, len(closes)):
        seg = rets[k - 20:k]
        mean = sum(seg) / len(seg)
        var = sum((x - mean) ** 2 for x in seg) / len(seg)
        vol20[k] = (var ** 0.5) * (252 ** 0.5)
    # 2년 롤링 80퍼센타일 (미래참조 없음 — 그 시점까지의 과거 분포만 사용)
    thresh = [None] * len(closes)
    for k in range(260, len(closes)):
        window = [v for v in vol20[max(0, k - 500):k] if v is not None]
        if len(window) >= 60:
            window.sort()
            thresh[k] = window[int(0.8 * len(window))]
    return {bars[k].date: (vol20[k], thresh[k]) for k in range(len(bars))
            if vol20[k] is not None and thresh[k] is not None}


def run_volsize(entries: list[dict], base_trades: list[dict]) -> list[dict]:
    vmap = _vol_regime_map("069500")
    if not vmap:
        print("  [경고] 지수 데이터 없음 — fetch_index 먼저 실행 필요")
        return []
    dates = sorted(vmap.keys())
    trades = []
    for e, base in zip(entries, base_trades):
        entry_date = e["date"]
        # 진입일 이전 가장 가까운 지수 거래일의 변동성 사용
        d = max((x for x in dates if x <= entry_date), default=None)
        high_vol = bool(d and vmap[d][0] > vmap[d][1])
        frac = (POSITION_FRAC * 0.5) if high_vol else POSITION_FRAC
        trades.append({**base, "frac": frac, "high_vol": high_vol})
    return trades


def report(name: str, trades: list[dict], scaled: bool = False) -> dict:
    is_r = [t["pnl"] for t in trades if t["date"] <= IS_END]
    oos = [t for t in trades if t["date"] > IS_END]
    oos_r = [t["pnl"] * t.get("frac", POSITION_FRAC) / POSITION_FRAC if scaled else t["pnl"]
             for t in oos]
    s_is, s_oos = stats(is_r), stats(oos_r)
    g = gate_check(oos_r)
    print(f"\n=== {name} ===")
    print(f"IS  {s_is.get('n', 0)}건 기대값 {s_is.get('expectancy_pct', 0):+.2f}%/건")
    print(f"OOS {s_oos.get('n', 0)}건 기대값 {s_oos.get('expectancy_pct', 0):+.2f}%/건 "
          f"승률 {s_oos.get('win_rate', 0):.0%} 상위5%보존율 "
          f"{s_oos.get('top5pct_retention') and s_oos['top5pct_retention']:.0%}")
    if g["mc"]:
        print(f"MC(1/2분할) 손실확률 {g['mc']['prob_loss']:.1%} p5 {g['mc']['ret_p5']:+.1%} "
              f"MDD최악5% {g['mc']['mdd_p95']:+.1%}")
    print(f"게이트: {'✅ 통과' if g['passed'] else '❌ 기각'} {g['gate']}")
    return {"is": s_is, "oos": s_oos, **g}


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    results = {}

    if cmd == "fetch_index":
        fetch_index_bars("069500")
        sys.exit(0)

    entries = build_entries()

    base_trades = run_baseline(entries)
    results["baseline"] = report("베이스라인 (현 프로덕션: EMA200+바닥권, 1/2분할)", base_trades)

    if cmd in ("ratchet", "all"):
        for cap in (1.25, 1.5, 2.0):
            rt_trades = run_ratchet(entries, cap_mult=cap)
            results[f"ratchet_cap{cap}"] = report(f"트라이얼A: ATR래칫 cap={cap}x진입ATR", rt_trades)

    if cmd in ("timestop", "all"):
        for n_days, thr in ((15, 1.0), (15, 0.5), (20, 1.0)):
            ts_trades = run_timestop(entries, base_trades, n_days=n_days, thr_mult=thr)
            results[f"timestop_{n_days}d_{thr}atr"] = report(
                f"트라이얼B: 타임스탑 {n_days}일/+{thr}ATR", ts_trades)

    if cmd in ("volsize", "all"):
        idx_path = SC_DIR / "069500_index.json"
        if not idx_path.exists():
            print("지수 캔들 없음 — fetch_index_bars 실행")
            fetch_index_bars("069500")
        vs_trades = run_volsize(entries, base_trades)
        if vs_trades:
            results["volsize"] = report("트라이얼C: 변동성체제 사이징 (고변동성시 frac 절반)",
                                        vs_trades, scaled=True)
            n_high = sum(1 for t in vs_trades if t.get("high_vol"))
            print(f"  고변동성 구간 진입: {n_high}/{len(vs_trades)}건 "
                  f"({n_high/len(vs_trades):.0%})")

    RESULT_PATH.write_text(json.dumps(results, ensure_ascii=False, indent=1))
    print(f"\n결과 저장: {RESULT_PATH}")
