"""뉴스모멘텀(nm) 전략 연구 백테스트 — "뉴스가 남긴 가격 발자국"을 사고판다.

가설: 호재 뉴스/공시가 나오면 분봉에 '급등 + 거래량 폭증 + 신고가'라는 서명이 남는다.
      그 서명을 규칙으로 감지해 올라타고, 트레일링/손절/장마감으로 나온다.
      (뉴스 원문은 과거 데이터가 없어 백테스트 불가 — 가격 서명이 검증 가능한 대리 신호.
       실전에서는 여기에 LLM 뉴스 거부권이 추가로 붙는다.)

생존편향 방지: 유니버스는 '그날그날의' 거래대금 상위 종목 (DART 전체 상장법인
→ 토스 일봉으로 일별 거래대금 계산). 오늘의 스타 종목만 모아 테스트하지 않는다.

체결 가정 (보수적):
  진입 = 신호 다음 분봉 시가 + 슬리피지 0.15% (모멘텀 추격은 항상 불리한 체결)
  하드스탑 = 봉 저가가 닿으면 체결 (시가가 더 낮으면 시가로 — 갭하락 반영)
  같은 봉에서 스탑과 트레일링이 겹치면 나쁜 쪽(하드스탑) 우선
  비용 = 수수료 0.015%×2 + 거래세 0.15%(매도) + 슬리피지 0.15%×2 + KRX 호가단위 반올림

사용법:
  python3 research/newsmom_backtest.py universe   # 1단계: 일별 거래대금 상위 유니버스
  python3 research/newsmom_backtest.py fetch      # 2단계: 1분봉 수집 (캐시, 오래 걸림)
  python3 research/newsmom_backtest.py run        # 3단계: IS 그리드 → OOS 검증 → MC
  python3 research/newsmom_backtest.py all
"""

import io
import json
import os
import sys
import time
import zipfile
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config                                    # noqa: E402
from toss.client import TossClient               # noqa: E402

KST = timezone(timedelta(hours=9))
NM_DIR = config.DATA_DIR / "newsmom"
DAILY_DIR = NM_DIR / "daily"
INTRA_DIR = NM_DIR / "intraday"
UNIVERSE_PATH = NM_DIR / "universe.json"
RESULT_PATH = config.ROOT / "research" / "newsmom_result.json"

# ── 설정 ─────────────────────────────────────────────────────
WINDOW_START = "2026-04-01"          # 1분봉 확보 가능 범위 (탐침: 3월 말까지 확인)
WINDOW_END = "2026-08-25"            # 어제까지 (오늘 제외 — 미완성 하루)
IS_END = "2026-06-30"                # IS(그리드 탐색) / OOS(검증) 경계
TOP_N = 20                           # 일별 거래대금 상위 N
MAX_PRICE = 100_000                  # 1주 가격 상한 (KR 예산 10만원 — 정수 주만 가능)
MIN_PRICE = 1_000                    # 동전주 제외
MIN_VALUE = 30e9                     # 일 거래대금 300억 미만 제외 (유동성)
EXCLUDE_NAME = ("기업인수목적", "스팩", "SPAC", "리츠")

FEE = 0.00015
TAX = 0.0015
SLIP = 0.0015                        # 편도 슬리피지 (모멘텀 추격 가정 — 일봉 전략의 1.5배)

# 그리드 (IS에서만 탐색 — 12조합. OOS는 최종 1조합만 평가)
GRID = {
    "up_th": [0.02, 0.03, 0.04],     # 5분 수익률 임계
    "vol_mult": [5.0, 10.0],         # 5분 거래량 / 전일 평균 5분 거래량 배수
    "trail": [0.02, 0.03],           # 트레일링 스탑 (당일 피크 종가 대비)
}
LOOKBACK = 5                         # 신호 창(분)
HARD_STOP = 0.03                     # 진입가 대비 하드스탑
ENTRY_START, ENTRY_END = "09:05", "14:30"   # 신호 인정 시간대
TIME_EXIT = "15:15"                  # 미청산분 시간 청산 (동시호가 전)


def tick_size(p: float) -> int:
    for lim, t in ((2000, 1), (5000, 5), (20000, 10), (50000, 50),
                   (200000, 100), (500000, 500)):
        if p < lim:
            return t
    return 1000


def round_tick(p: float) -> float:
    t = tick_size(p)
    return int(p / t) * t


# ── 1단계: 유니버스 ───────────────────────────────────────────
def load_corps() -> dict:
    cache = NM_DIR / "corps.json"
    if cache.exists():
        return json.loads(cache.read_text())
    config.load_env()
    r = requests.get("https://opendart.fss.or.kr/api/corpCode.xml",
                     params={"crtfc_key": os.getenv("DART_API_KEY")}, timeout=60)
    z = zipfile.ZipFile(io.BytesIO(r.content))
    root = ET.fromstring(z.read(z.namelist()[0]))
    corps = {}
    for el in root.findall(".//list"):
        code = (el.findtext("stock_code") or "").strip()
        name = (el.findtext("corp_name") or "").strip()
        if len(code) == 6 and not any(w in name for w in EXCLUDE_NAME):
            corps[code] = name
    NM_DIR.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(corps, ensure_ascii=False))
    return corps


def build_universe() -> None:
    corps = load_corps()
    print(f"상장법인 {len(corps)}개 일봉 스캔 (캐시 재사용, 처음엔 ~5분)")
    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    client = TossClient(*config.credentials())
    day_rows: dict[str, list] = {}
    done = 0
    for sym, name in corps.items():
        done += 1
        if done % 400 == 0:
            print(f"  ...{done}/{len(corps)}")
        cache = DAILY_DIR / f"{sym}.json"
        if cache.exists():
            candles = json.loads(cache.read_text())
        else:
            try:
                candles = client.get_candles(sym, interval="1d", count=120).get(
                    "candles", [])
            except Exception:            # noqa: BLE001 - 상폐/조회불가 종목
                candles = []
            cache.write_text(json.dumps(candles))
            time.sleep(0.055)            # CHART 20/s 여유
        for c in candles:
            d = c["timestamp"][:10]
            if not (WINDOW_START <= d <= WINDOW_END):
                continue
            close = float(c["closePrice"])
            value = float(c["volume"]) * close      # 근사 거래대금 (종가×거래량)
            if not (MIN_PRICE <= close <= MAX_PRICE) or value < MIN_VALUE:
                continue
            day_rows.setdefault(d, []).append((value, sym, name, close))
    universe = {}
    for d in sorted(day_rows):
        top = sorted(day_rows[d], reverse=True)[:TOP_N]
        universe[d] = [{"symbol": s, "name": n, "value": v, "close": c}
                       for v, s, n, c in top]
    UNIVERSE_PATH.write_text(json.dumps(universe, ensure_ascii=False, indent=1))
    pairs = sum(len(v) for v in universe.values())
    print(f"유니버스: {len(universe)}거래일 × 상위{TOP_N} = {pairs}쌍 → {UNIVERSE_PATH}")


# ── 2단계: 1분봉 수집 ────────────────────────────────────────
def _intra_path(sym: str, d: str) -> Path:
    return INTRA_DIR / f"{sym}_{d}.json"


def _fetch_day_1m(client: TossClient, sym: str, d: str) -> list:
    """d일의 정규장(09:00~15:30) 1분봉. 연장장 봉은 버린다."""
    p = _intra_path(sym, d)
    if p.exists():
        return json.loads(p.read_text())
    out, before = [], f"{d}T23:59:00+09:00"
    for _ in range(12):                  # 연장장 포함 최대 ~720봉 + 여유
        try:
            r = client.get_candles(sym, interval="1m", count=200, before=before)
        except Exception:                # noqa: BLE001
            break
        rows = r.get("candles", [])
        if not rows:
            break
        stop = False
        for c in rows:
            ts = c["timestamp"]
            if ts[:10] < d:
                stop = True
                break
            hm = ts[11:16]
            if ts[:10] == d and "09:00" <= hm <= "15:30":
                out.append({"t": hm, "o": float(c["openPrice"]),
                            "h": float(c["highPrice"]), "l": float(c["lowPrice"]),
                            "c": float(c["closePrice"]), "v": float(c["volume"])})
        before = r.get("nextBefore")
        if stop or not before:
            break
        time.sleep(0.055)
    out.sort(key=lambda x: x["t"])
    p.write_text(json.dumps(out))
    return out


def prev_trade_day(universe: dict, d: str) -> str | None:
    days = sorted(universe)
    i = days.index(d)
    return days[i - 1] if i > 0 else None


def fetch_intraday() -> None:
    universe = json.loads(UNIVERSE_PATH.read_text())
    INTRA_DIR.mkdir(parents=True, exist_ok=True)
    client = TossClient(*config.credentials())
    pairs = [(d, e["symbol"]) for d in sorted(universe) for e in universe[d]]
    # 전일 봉(거래량 기준선)도 필요
    need = set()
    for d, sym in pairs:
        need.add((d, sym))
        pd = prev_trade_day(universe, d)
        if pd:
            need.add((pd, sym))
    todo = [(d, s) for d, s in sorted(need) if not _intra_path(s, d).exists()]
    print(f"1분봉 수집: 총 {len(need)}쌍 중 신규 {len(todo)}쌍 (쌍당 2~8콜)")
    for i, (d, sym) in enumerate(todo):
        if i % 100 == 0 and i:
            print(f"  ...{i}/{len(todo)}")
        _fetch_day_1m(client, sym, d)
    print("수집 완료")


# ── 3단계: 이벤트 백테스트 ───────────────────────────────────
def simulate_day(bars: list, prev_bars: list, up_th: float, vol_mult: float,
                 trail: float) -> dict | None:
    """하루치 1분봉에서 신호 감지 → 1회 매매 시뮬레이션. 없으면 None."""
    if len(bars) < LOOKBACK + 2 or len(prev_bars) < 60:
        return None
    prev_vol_5m = sum(b["v"] for b in prev_bars) / (len(prev_bars) / 5)
    if prev_vol_5m <= 0:
        return None
    day_high = 0.0
    sig_i = None
    for i in range(LOOKBACK, len(bars) - 1):
        b = bars[i]
        day_high = max(day_high, b["h"])
        if not (ENTRY_START <= b["t"] <= ENTRY_END):
            continue
        base = bars[i - LOOKBACK]["c"]
        if base <= 0:
            continue
        ret = b["c"] / base - 1
        vol5 = sum(x["v"] for x in bars[i - LOOKBACK + 1: i + 1])
        if (ret >= up_th and vol5 >= vol_mult * prev_vol_5m
                and b["c"] >= day_high * 0.999):
            sig_i = i
            break
    if sig_i is None:
        return None

    entry_bar = bars[sig_i + 1]
    entry = round_tick(entry_bar["o"] * (1 + SLIP))
    if entry <= 0:
        return None
    stop_px = entry * (1 - HARD_STOP)
    peak = entry
    exit_px, exit_t, reason = None, None, None
    for j in range(sig_i + 1, len(bars)):
        b = bars[j]
        if j > sig_i + 1 and b["o"] <= stop_px:          # 갭으로 스탑 하회 시가
            exit_px, exit_t, reason = b["o"], b["t"], "stop_gap"
            break
        if b["l"] <= stop_px:                            # 봉 안에서 스탑 터치
            exit_px, exit_t, reason = min(stop_px, b["o"]), b["t"], "stop"
            break
        peak = max(peak, b["c"])
        if b["c"] <= peak * (1 - trail):                 # 트레일링 → 다음 봉 시가
            if j + 1 < len(bars):
                exit_px, exit_t, reason = bars[j + 1]["o"], bars[j + 1]["t"], "trail"
            else:
                exit_px, exit_t, reason = b["c"], b["t"], "trail_close"
            break
        if b["t"] >= TIME_EXIT:                          # 시간 청산
            exit_px, exit_t, reason = b["c"], b["t"], "time"
            break
    if exit_px is None:
        last = bars[-1]
        exit_px, exit_t, reason = last["c"], last["t"], "eod"

    sell = round_tick(exit_px * (1 - SLIP))
    cost = entry * (1 + FEE)
    recv = sell * (1 - FEE - TAX)
    pnl_pct = recv / cost - 1
    return {"entry_t": entry_bar["t"], "entry": entry, "exit_t": exit_t,
            "exit": sell, "pnl_pct": pnl_pct, "reason": reason}


def run_combo(universe: dict, days: list, up_th, vol_mult, trail) -> list[dict]:
    trades = []
    for d in days:
        pd = prev_trade_day(universe, d)
        if not pd:
            continue
        for e in universe[d]:
            sym = e["symbol"]
            p1, p0 = _intra_path(sym, d), _intra_path(sym, pd)
            if not (p1.exists() and p0.exists()):
                continue
            bars = json.loads(p1.read_text())
            prev = json.loads(p0.read_text())
            t = simulate_day(bars, prev, up_th, vol_mult, trail)
            if t:
                t.update(symbol=sym, name=e["name"], date=d)
                trades.append(t)
    return trades


def stats(trades: list[dict]) -> dict:
    if not trades:
        return {"n": 0}
    rets = [t["pnl_pct"] for t in trades]
    wins = [r for r in rets if r > 0]
    return {"n": len(rets),
            "expectancy_pct": sum(rets) / len(rets) * 100,
            "win_rate": len(wins) / len(rets),
            "avg_win": (sum(wins) / len(wins) * 100) if wins else 0.0,
            "avg_loss": (sum(r for r in rets if r <= 0)
                         / max(1, len(rets) - len(wins)) * 100),
            "total_pct": (sum(rets)) * 100,
            "worst": min(rets) * 100, "best": max(rets) * 100}


def run_backtest() -> None:
    universe = json.loads(UNIVERSE_PATH.read_text())
    days = sorted(universe)
    is_days = [d for d in days if d <= IS_END]
    oos_days = [d for d in days if d > IS_END]
    print(f"IS {len(is_days)}일 ({is_days[0]}~{is_days[-1]}) / "
          f"OOS {len(oos_days)}일 ({oos_days[0]}~{oos_days[-1]})\n")

    print("── IS 그리드 탐색 ──")
    results = []
    for up in GRID["up_th"]:
        for vm in GRID["vol_mult"]:
            for tr in GRID["trail"]:
                s = stats(run_combo(universe, is_days, up, vm, tr))
                results.append(((up, vm, tr), s))
                print(f"  up{up:.0%} vol{vm:.0f}x trail{tr:.0%}: "
                      f"{s.get('n', 0)}건, 기대값 {s.get('expectancy_pct', 0):+.2f}%/건, "
                      f"승률 {s.get('win_rate', 0):.0%}")
    viable = [(p, s) for p, s in results if s.get("n", 0) >= 30]
    if not viable:
        print("\nIS에서 30건 이상 나온 조합 없음 → 기각")
        return
    best_p, best_s = max(viable, key=lambda x: x[1]["expectancy_pct"])
    up, vm, tr = best_p
    print(f"\nIS 최적: up{up:.0%} vol{vm:.0f}x trail{tr:.0%} "
          f"(기대값 {best_s['expectancy_pct']:+.2f}%/건, {best_s['n']}건)")

    print("\n── OOS 검증 (최적 조합 1개만 — 커닝 금지) ──")
    oos_trades = run_combo(universe, oos_days, up, vm, tr)
    oos = stats(oos_trades)
    print(f"  OOS: {oos.get('n', 0)}건, 기대값 {oos.get('expectancy_pct', 0):+.2f}%/건, "
          f"승률 {oos.get('win_rate', 0):.0%}, 합계 {oos.get('total_pct', 0):+.1f}%")

    mc = None
    if oos.get("n", 0) >= 10:
        from types import SimpleNamespace
        from backtest.montecarlo import run_monte_carlo
        fake = [SimpleNamespace(pnl_rate=t["pnl_pct"]) for t in oos_trades]
        mc = run_monte_carlo(fake, n_sims=3000)
        print(f"  MC(3000회): 손실확률 {mc.prob_loss:.1%}, "
              f"최악5% {mc.ret_p5:+.1%}, 중앙값 {mc.ret_p50:+.1%}, "
              f"MDD 최악5% {mc.mdd_p95:+.1%}")

    gate = {"oos_trades_ge_30": oos.get("n", 0) >= 30,
            "oos_expectancy_pos": oos.get("expectancy_pct", -1) > 0,
            "mc_loss_lt_30": bool(mc and mc.prob_loss < 0.30)}
    passed = all(gate.values())
    print(f"\n검증 게이트: {'✅ 통과' if passed else '❌ 기각'} — {gate}")

    RESULT_PATH.write_text(json.dumps({
        "strategy": "nm", "generated_at": datetime.now(KST).isoformat(timespec="seconds"),
        "window": [WINDOW_START, WINDOW_END], "is_end": IS_END,
        "params": {"up_th": up, "vol_mult": vm, "trail": tr,
                   "lookback": LOOKBACK, "hard_stop": HARD_STOP},
        "grid_is": [{"params": p, **s} for p, s in results],
        "oos": oos,
        "mc": ({"prob_loss": mc.prob_loss, "ret_p5": mc.ret_p5, "ret_p50": mc.ret_p50,
                "mdd_p95": mc.mdd_p95} if mc else None),
        "gate": gate, "passed": passed,
        "oos_trades": oos_trades,
    }, ensure_ascii=False, indent=1))
    print(f"결과 저장: {RESULT_PATH}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    if cmd in ("universe", "all"):
        build_universe()
    if cmd in ("fetch", "all"):
        fetch_intraday()
    if cmd in ("run", "all"):
        run_backtest()
