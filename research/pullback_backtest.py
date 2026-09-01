"""'공시 확인 후 눌림목' 전략 백테스트 — 지인 제안(2026-08-27) 검증.

전략 스펙 (제안 원문):
  신호: 장중 DART '단일판매·공급계약체결' 공시 (정정공시 제외)
  대기: 공시 직후 30분은 따라가지 않는다
  진입: 30분 후~90분 사이, 공시 후 고점 대비 -2% 눌림 && 공시 직전가 대비 +3% 이상 유지
        → 다음 분봉 시가 매수 (원문은 '30분 뒤 1회 확인'이나, 눌림은 30~90분 사이
          아무 때나 올 수 있어 롤링 창으로 해석. 결과에 병기)
  청산: +5% 익절 / -3% 손절 / 공시 직전가 회귀 시 즉시 손절 / 2거래일 후 종가 시간청산
        (보유기간 미명시 → 최대 2박 3일로 가정. 익절·손절 수치는 IS에서만 그리드 탐색)

데이터:
  공시 시각: KIND(kind.krx.co.kr) 일별 공시목록 — 시각(HH:MM)과 발행사코드가 행에 있음
  분봉: 토스 1분봉 (공시일 + 이후 2거래일), newsmom과 같은 캐시 디렉터리 재사용

체결 가정 (보수적): 진입 슬리피지 0.1%, 손절은 갭 반영(시가가 더 나쁘면 시가 체결),
같은 봉에서 익절·손절 겹치면 손절 우선, 수수료 0.015%×2 + 거래세 0.15% + 호가단위 반올림.

사용법:
  python3 research/pullback_backtest.py events   # 1단계: KIND에서 공시 이벤트 수집
  python3 research/pullback_backtest.py fetch    # 2단계: 분봉 수집 (캐시)
  python3 research/pullback_backtest.py run      # 3단계: IS 그리드 → OOS → MC → 게이트
  python3 research/pullback_backtest.py all
"""

import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config                                    # noqa: E402
from toss.client import TossClient               # noqa: E402

KST = timezone(timedelta(hours=9))
PB_DIR = config.DATA_DIR / "pullback"
EVENTS_PATH = PB_DIR / "events.json"
INTRA_DIR = config.DATA_DIR / "newsmom" / "intraday"     # newsmom 캐시 재사용
RESULT_PATH = config.ROOT / "research" / "pullback_result.json"
UNIVERSE_PATH = config.DATA_DIR / "newsmom" / "universe.json"  # 거래일 달력 재사용

WINDOW_START, WINDOW_END, IS_END = "2026-04-01", "2026-08-25", "2026-06-30"
TITLE_PAT = "공급계약"
EXCLUDE_PAT = ("정정",)                 # [정정]/기재정정 — 새 정보 아님
EVENT_TIME_MIN, EVENT_TIME_MAX = "09:05", "14:00"   # 장중 + 청산 여유
MIN_PRICE, MAX_PRICE = 1_000, 100_000   # 예산 내 정수 주 매수 가능 범위
WAIT_MIN, ENTRY_WINDOW_MIN = 30, 90     # 공시 후 대기 / 진입 탐색 마감(분)
PULLBACK, MIN_ABOVE = 0.02, 0.03        # 고점 대비 눌림 / 직전가 대비 유지
HOLD_DAYS = 2                           # 시간청산: 공시일 + 2거래일 종가
FEE, TAX, SLIP = 0.00015, 0.0015, 0.001
GRID = {"tp": [0.04, 0.05, 0.07], "sl": [0.02, 0.03]}

KIND_URL = "https://kind.krx.co.kr/disclosure/todaydisclosure.do"
ROW_RE = re.compile(
    r'<td class="first txc">(\d{2}:\d{2})</td>.*?'
    r"companysummary_open\('(\d+)'\).*?"
    r'openDisclsViewer\([^)]*\)"'
    r" title='([^']*)'>(.*?)</a>", re.S)


def tick_size(p: float) -> int:
    for lim, t in ((2000, 1), (5000, 5), (20000, 10), (50000, 50),
                   (200000, 100), (500000, 500)):
        if p < lim:
            return t
    return 1000


def round_tick(p: float) -> float:
    return int(p / tick_size(p)) * tick_size(p)


def trade_days() -> list[str]:
    return sorted(json.loads(UNIVERSE_PATH.read_text()))


# ── 1단계: KIND 공시 이벤트 수집 ─────────────────────────────
def _kind_day(day: str) -> list[dict]:
    """하루치 공시 전체를 페이지네이션으로 훑어 공급계약 이벤트만 반환."""
    out, page = [], 1
    while page <= 15:
        try:
            r = requests.post(KIND_URL, timeout=15,
                              headers={"User-Agent": "Mozilla/5.0"},
                              data={"method": "searchTodayDisclosureSub",
                                    "currentPageSize": 100, "pageIndex": page,
                                    "orderMode": 0, "orderStat": "A",
                                    "forward": "todaydisclosure_sub",
                                    "chose": "S", "todayFlag": "N", "selDate": day})
        except requests.RequestException:
            break
        rows = re.split(r"<tr[ >]", r.text)[1:]
        for row in rows:
            m = ROW_RE.search(row)
            if not m:
                continue
            hhmm, issuer, title, inner = m.groups()
            if TITLE_PAT not in title:
                continue
            # 진짜 정정공시([정정]/[기재정정] 제목)만 제외.
            # '이후에 정정된 보고서 있음' 아이콘의 원본 공시는 유지 —
            # 원본 제외는 미래 정보를 쓰는 것(look-ahead)이라 편향이다.
            if "[정정]" in inner or title.startswith("[기재정정]"):
                continue
            if not (EVENT_TIME_MIN <= hhmm <= EVENT_TIME_MAX):
                continue
            sym = f"{int(issuer) * 10:06d}"       # 발행사코드 → 보통주 종목코드
            out.append({"date": day.replace("-", "")[:4] + "-"
                                + day.replace("-", "")[4:6] + "-"
                                + day.replace("-", "")[6:8]
                        if "-" not in day else day,
                        "time": hhmm, "symbol": sym, "title": title})
        if len(rows) < 100:
            break
        page += 1
        time.sleep(0.25)
    return out


def collect_events() -> None:
    days = trade_days()
    PB_DIR.mkdir(parents=True, exist_ok=True)
    prev = json.loads(EVENTS_PATH.read_text()) if EVENTS_PATH.exists() else {}
    for i, d in enumerate(days):
        if d in prev:
            continue
        prev[d] = _kind_day(d)
        if i % 10 == 0:
            print(f"  {d}: 누적 {sum(len(v) for v in prev.values())}건")
            EVENTS_PATH.write_text(json.dumps(prev, ensure_ascii=False))
        time.sleep(0.3)
    # 같은 날 같은 종목은 첫 공시만
    for d, evs in prev.items():
        seen, ded = set(), []
        for e in sorted(evs, key=lambda x: x["time"]):
            if e["symbol"] in seen:
                continue
            seen.add(e["symbol"])
            ded.append(e)
        prev[d] = ded
    EVENTS_PATH.write_text(json.dumps(prev, ensure_ascii=False, indent=1))
    n = sum(len(v) for v in prev.values())
    print(f"이벤트 수집 완료: {len(prev)}거래일, 공급계약 공시 {n}건 → {EVENTS_PATH}")


# ── 2단계: 분봉 수집 (newsmom 인프라 재사용) ──────────────────
def fetch_intraday() -> None:
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "nm", Path(__file__).parent / "newsmom_backtest.py")
    nm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(nm)

    events = json.loads(EVENTS_PATH.read_text())
    days = trade_days()
    need = set()
    for d, evs in events.items():
        if d not in days:
            continue
        i = days.index(d)
        for e in evs:
            for j in range(HOLD_DAYS + 1):
                if i + j < len(days):
                    need.add((days[i + j], e["symbol"]))
    todo = [(d, s) for d, s in sorted(need)
            if not (INTRA_DIR / f"{s}_{d}.json").exists()]
    print(f"분봉 수집: {len(need)}쌍 중 신규 {len(todo)}쌍")
    client = TossClient(*config.credentials())
    for i, (d, sym) in enumerate(todo):
        if i % 100 == 0 and i:
            print(f"  ...{i}/{len(todo)}")
        nm._fetch_day_1m(client, sym, d)
    print("수집 완료")


# ── 3단계: 시뮬레이션 ────────────────────────────────────────
def _bars(sym: str, d: str) -> list:
    p = INTRA_DIR / f"{sym}_{d}.json"
    return json.loads(p.read_text()) if p.exists() else []


def _minutes(t: str) -> int:
    return int(t[:2]) * 60 + int(t[3:5])


def simulate_event(ev: dict, days: list[str], tp: float, sl: float) -> dict | None:
    d0 = ev["date"]
    bars0 = _bars(ev["symbol"], d0)
    if len(bars0) < 60:
        return None
    t_ev = _minutes(ev["time"])
    pre = [b for b in bars0 if _minutes(b["t"]) < t_ev]
    if not pre:
        return None
    p0 = pre[-1]["c"]                            # 공시 직전가
    if not (MIN_PRICE <= p0 <= MAX_PRICE):
        return None

    # 진입 탐색: 공시 +30분 ~ +90분, 공시 후 누적 고점 대비 -2% && p0 대비 +3%
    hi, entry_i = 0.0, None
    for i, b in enumerate(bars0):
        tm = _minutes(b["t"])
        if tm <= t_ev:
            continue
        hi = max(hi, b["h"])
        if tm < t_ev + WAIT_MIN or tm > t_ev + ENTRY_WINDOW_MIN:
            continue
        if hi >= p0 * (1 + MIN_ABOVE) and b["c"] <= hi * (1 - PULLBACK) \
                and b["c"] >= p0 * (1 + MIN_ABOVE) and i + 1 < len(bars0):
            entry_i = i + 1
            break
    if entry_i is None:
        return None

    entry = round_tick(bars0[entry_i]["o"] * (1 + SLIP))
    if entry <= 0:
        return None
    tgt, stp = entry * (1 + tp), min(entry * (1 - sl), p0)  # 손절 vs p0회귀 중 위쪽 먼저
    stop_px = max(entry * (1 - sl), p0)          # 더 가까운(높은) 방어선이 먼저 발동
    i0 = days.index(d0)
    seq = [(d0, bars0[entry_i:])] + [
        (days[i0 + j], _bars(ev["symbol"], days[i0 + j]))
        for j in range(1, HOLD_DAYS + 1) if i0 + j < len(days)]

    for di, (d, bars) in enumerate(seq):
        for b in bars:
            if b["o"] <= stop_px:                # 갭/즉시 하회
                return _close(ev, entry, b["o"], d, b["t"], "stop_gap")
            if b["l"] <= stop_px:
                return _close(ev, entry, min(stop_px, b["o"]), d, b["t"], "stop")
            if b["h"] >= tgt:
                return _close(ev, entry, tgt, d, b["t"], "target")
        if di == len(seq) - 1 and bars:
            return _close(ev, entry, bars[-1]["c"], d, bars[-1]["t"], "time")
    return None


def _close(ev, entry, exit_px, d, t, reason) -> dict:
    sell = round_tick(exit_px * (1 - SLIP))
    pnl = sell * (1 - FEE - TAX) / (entry * (1 + FEE)) - 1
    return {"symbol": ev["symbol"], "date": ev["date"], "ev_time": ev["time"],
            "entry": entry, "exit": sell, "exit_date": d, "exit_t": t,
            "pnl_pct": pnl, "reason": reason}


def run_combo(events: dict, days: list[str], subset: list[str],
              tp: float, sl: float) -> list[dict]:
    out = []
    for d in subset:
        for ev in events.get(d, []):
            r = simulate_event(ev, days, tp, sl)
            if r:
                out.append(r)
    return out


def stats(trades: list[dict]) -> dict:
    if not trades:
        return {"n": 0}
    rets = [t["pnl_pct"] for t in trades]
    wins = [r for r in rets if r > 0]
    return {"n": len(rets), "expectancy_pct": sum(rets) / len(rets) * 100,
            "win_rate": len(wins) / len(rets), "total_pct": sum(rets) * 100,
            "worst": min(rets) * 100, "best": max(rets) * 100}


def run_backtest() -> None:
    events = json.loads(EVENTS_PATH.read_text())
    days = trade_days()
    is_days = [d for d in days if d <= IS_END]
    oos_days = [d for d in days if d > IS_END]
    n_ev = sum(len(events.get(d, [])) for d in days)
    print(f"이벤트 {n_ev}건 | IS {len(is_days)}일 / OOS {len(oos_days)}일\n")

    print("── IS 그리드 (익절/손절만 탐색 — 진입 조건은 제안 원문 고정) ──")
    results = []
    for tp in GRID["tp"]:
        for sl in GRID["sl"]:
            s = stats(run_combo(events, days, is_days, tp, sl))
            results.append(((tp, sl), s))
            print(f"  익절+{tp:.0%} 손절-{sl:.0%}: {s.get('n', 0)}건, "
                  f"기대값 {s.get('expectancy_pct', 0):+.2f}%/건, "
                  f"승률 {s.get('win_rate', 0):.0%}")
    viable = [(p, s) for p, s in results if s.get("n", 0) >= 20]
    if not viable:
        print("\nIS 표본 부족 → 판정 불가")
        return
    (tp, sl), best = max(viable, key=lambda x: x[1]["expectancy_pct"])
    print(f"\nIS 최적: 익절+{tp:.0%}/손절-{sl:.0%} "
          f"({best['n']}건, {best['expectancy_pct']:+.2f}%/건)")

    print("\n── OOS 검증 (최적 1조합만) ──")
    oos_trades = run_combo(events, days, oos_days, tp, sl)
    oos = stats(oos_trades)
    print(f"  OOS: {oos.get('n', 0)}건, 기대값 {oos.get('expectancy_pct', 0):+.2f}%/건, "
          f"승률 {oos.get('win_rate', 0):.0%}, 합계 {oos.get('total_pct', 0):+.1f}%")

    mc = None
    if oos.get("n", 0) >= 10:
        from types import SimpleNamespace
        from backtest.montecarlo import run_monte_carlo
        mc = run_monte_carlo([SimpleNamespace(pnl_rate=t["pnl_pct"])
                              for t in oos_trades], n_sims=3000)
        print(f"  MC(3000회): 손실확률 {mc.prob_loss:.1%}, 최악5% {mc.ret_p5:+.1%}, "
              f"중앙값 {mc.ret_p50:+.1%}")

    gate = {"oos_trades_ge_30": oos.get("n", 0) >= 30,
            "oos_expectancy_pos": oos.get("expectancy_pct", -1) > 0,
            "mc_loss_lt_30": bool(mc and mc.prob_loss < 0.30)}
    passed = all(gate.values())
    print(f"\n검증 게이트: {'✅ 통과' if passed else '❌ 기각'} — {gate}")
    RESULT_PATH.write_text(json.dumps({
        "strategy": "pullback(공시 후 눌림목)", "window": [WINDOW_START, WINDOW_END],
        "generated_at": datetime.now(KST).isoformat(timespec="seconds"),
        "spec": {"wait_min": WAIT_MIN, "entry_window_min": ENTRY_WINDOW_MIN,
                 "pullback": PULLBACK, "min_above": MIN_ABOVE,
                 "hold_days": HOLD_DAYS, "tp": tp, "sl": sl},
        "n_events": n_ev,
        "grid_is": [{"tp": p[0], "sl": p[1], **s} for p, s in results],
        "oos": oos,
        "mc": ({"prob_loss": mc.prob_loss, "ret_p5": mc.ret_p5,
                "ret_p50": mc.ret_p50} if mc else None),
        "gate": gate, "passed": passed, "oos_trades": oos_trades,
    }, ensure_ascii=False, indent=1))
    print(f"결과 저장: {RESULT_PATH}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    if cmd in ("events", "all"):
        collect_events()
    if cmd in ("fetch", "all"):
        fetch_intraday()
    if cmd in ("run", "all"):
        run_backtest()
