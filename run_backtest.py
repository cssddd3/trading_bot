"""2단계: 백테스트 CLI.

  python3 run_backtest.py --symbol 005930 --strategy vb --count 1000
  python3 run_backtest.py --symbol 005930 --strategy ma --split 0.7      # 학습/검증 분리
  python3 run_backtest.py --symbol 005930 --strategy vb --sweep          # 파라미터 탐색
  python3 run_backtest.py --symbol 005930 --source pykrx --no-cache
  python3 run_backtest.py --symbol 005930 --strategy vb --param k=0.6 --param ma_filter=10

과최적화 주의: --sweep 상위 결과는 '그 구간에 가장 잘 맞은 값'일 뿐이다.
반드시 --split 으로 검증 구간(OOS) 성적을 같이 보고, 두 구간에서 모두 준수한 값만 쓴다.
"""

import argparse
import csv
import itertools
import sys

import config
from backtest import Backtester, Costs, compute, format_report
from strategy import STRATEGIES, build
from toss.client import TossClient
from toss.data import load_bars

SWEEP_GRID = {
    "vb": {"k": [0.3, 0.4, 0.5, 0.6, 0.7], "ma_filter": [0, 3, 5, 10],
           "dynamic_k": [True, False]},
    "ma": {"fast": [5, 10, 20], "slow": [40, 60, 120], "atr_mult": [2.0, 2.5, 3.5]},
    "st": {"atr_n": [7, 10, 14], "mult": [2.0, 3.0, 4.0]},
}


def parse_params(pairs: list[str]) -> dict:
    out = {}
    for p in pairs:
        k, _, v = p.partition("=")
        if v.lower() in ("true", "false"):
            out[k] = v.lower() == "true"
        else:
            try:
                out[k] = int(v) if v.isdigit() or (v[:1] == "-" and v[1:].isdigit()) else float(v)
            except ValueError:
                out[k] = v
    return out


def make_tester(cash: float) -> Backtester:
    return Backtester(
        costs=Costs(config.FEE_RATE, config.SELL_TAX_RATE, config.SLIPPAGE_RATE),
        initial_cash=cash, position_pct=config.POSITION_PCT)


def run_once(bars, key, params, cash, symbol, start_index: int = 0):
    res = make_tester(cash).run(bars, build(key, **params), symbol=symbol,
                                start_index=start_index)
    return res, compute(res)


def print_trades(res, limit: int = 20):
    print(f"\n[거래 내역] 총 {len(res.trades)}건 (최근 {min(limit, len(res.trades))}건)")
    print(f"  {'진입일':<12}{'청산일':<12}{'수량':>5}{'진입가':>10}{'청산가':>10}"
          f"{'손익':>12}{'수익률':>9}  사유")
    for t in res.trades[-limit:]:
        print(f"  {t.entry_date:<12}{t.exit_date:<12}{t.quantity:>5}"
              f"{t.entry_price:>10,.0f}{t.exit_price:>10,.0f}"
              f"{t.pnl:>12,.0f}{t.pnl_rate * 100:>8.2f}%  {t.exit_reason}")


def save_trades(res, path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["symbol", "entry_date", "entry_price", "exit_date", "exit_price",
                    "quantity", "pnl", "pnl_rate", "entry_reason", "exit_reason", "cost"])
        for t in res.trades:
            w.writerow([t.symbol, t.entry_date, f"{t.entry_price:.0f}", t.exit_date,
                        f"{t.exit_price:.0f}", t.quantity, f"{t.pnl:.0f}",
                        f"{t.pnl_rate:.4f}", t.entry_reason, t.exit_reason, f"{t.cost:.0f}"])
    print(f"  거래 내역 저장: {path}")


def sweep(bars, key, cash, symbol, split: float, top: int = 10):
    grid = SWEEP_GRID[key]
    keys = list(grid)
    combos = list(itertools.product(*(grid[k] for k in keys)))
    cut = int(len(bars) * split)
    train = bars[:cut]
    print(f"\n[파라미터 탐색] {len(combos)}개 조합 | 학습 {bars[0].date}~{bars[cut - 1].date} "
          f"/ 검증 {bars[cut].date}~{bars[-1].date}")

    rows, seen = [], set()
    for combo in combos:
        params = dict(zip(keys, combo))
        # 동적 k 를 쓰면 k 값은 무시되므로 같은 조합을 중복 평가하지 않는다.
        fingerprint = tuple(sorted(
            (k, v) for k, v in params.items() if not (params.get("dynamic_k") and k == "k")))
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        try:
            _, mtr = run_once(train, key, params, cash, symbol)
            # 검증구간은 전체 봉을 주되 cut 이후부터만 매매/집계 (지표 워밍업 확보)
            _, mte = run_once(bars, key, params, cash, symbol, start_index=cut)
        except Exception:                       # noqa: BLE001 - 조합이 깨지면 건너뛴다
            continue
        rows.append((params, mtr, mte))

    rows.sort(key=lambda r: r[1].cagr, reverse=True)
    head = f"  {'파라미터':<44}{'학습CAGR':>9}{'학습MDD':>9}{'검증CAGR':>9}{'검증MDD':>9}{'검증거래':>7}"
    print(head)
    print("  " + "-" * (len(head) - 2))
    for params, mtr, mte in rows[:top]:
        ps = ",".join(f"{k}={v}" for k, v in params.items())
        print(f"  {ps:<44}{mtr.cagr * 100:>8.1f}%{mtr.mdd * 100:>8.1f}%"
              f"{mte.cagr * 100:>8.1f}%{mte.mdd * 100:>8.1f}%{mte.trades:>7}")
    print("\n  ※ 학습 성적만 좋고 검증 성적이 무너지는 조합은 과최적화다. 버릴 것.")
    return rows


def main():
    ap = argparse.ArgumentParser(description="토스 캔들 기반 백테스트")
    ap.add_argument("--symbol", "-s", action="append", default=None,
                    help="종목코드 (여러 번 지정 가능, 기본: 화이트리스트 전체)")
    ap.add_argument("--strategy", "-t", default=config.DEFAULT_STRATEGY,
                    choices=list(STRATEGIES))
    ap.add_argument("--count", "-n", type=int, default=1000, help="사용할 봉 개수")
    ap.add_argument("--interval", default="1d", choices=["1d", "1m"])
    ap.add_argument("--source", default="auto", choices=["auto", "toss", "pykrx", "csv"])
    ap.add_argument("--cash", type=float, default=config.INITIAL_CASH)
    ap.add_argument("--param", "-p", action="append", default=[], help="예: -p k=0.6")
    ap.add_argument("--split", type=float, default=0.0,
                    help="0.7 이면 앞 70%%=학습 / 뒤 30%%=검증 으로 나눠 각각 리포트")
    ap.add_argument("--sweep", action="store_true", help="파라미터 그리드 탐색")
    ap.add_argument("--trades", action="store_true", help="거래 내역 출력")
    ap.add_argument("--mc", action="store_true",
                    help="몬테카를로 강건성 검증 (거래 순서 부트스트랩 3000회)")
    ap.add_argument("--validate", action="store_true",
                    help="실전 게이트용 종합 검증: 전 종목 OOS(70/30)+MC 집계 → "
                         "logs/strategy_validation.json 기록. 통과해야 --live 기동 가능")
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args()

    symbols = args.symbol or list(config.WHITELIST)
    params = {**config.STRATEGY_PARAMS.get(args.strategy, {}), **parse_params(args.param)}

    client = None
    if args.source in ("auto", "toss"):
        try:
            client = TossClient(*config.credentials())
        except SystemExit as e:
            print(f"  [!] {e} → 캐시/pykrx 로만 진행합니다.")

    if args.validate:
        return run_validation(symbols, args, params, client)

    for sym in symbols:
        print(f"\n■ {sym} {config.WHITELIST.get(sym, '')}")
        try:
            bars = load_bars(sym, args.interval, args.count, source=args.source,
                             client=client, use_cache=not args.no_cache)
        except Exception as e:                  # noqa: BLE001
            print(f"  [!] 데이터 로드 실패: {e}")
            continue

        strat = build(args.strategy, **params)
        if len(bars) <= strat.warmup() + 10:
            print(f"  [!] 봉이 부족합니다 ({len(bars)}개, 최소 {strat.warmup() + 10}개 필요)")
            continue

        if args.sweep:
            sweep(bars, args.strategy, args.cash, sym, args.split or 0.7)
            continue

        if args.split:
            cut = int(len(bars) * args.split)
            for label, seg, start in (("학습구간", bars[:cut], 0),
                                      ("검증구간(OOS)", bars, cut)):
                res, m = run_once(seg, args.strategy, params, args.cash, sym, start)
                print(f"\n<{label}>")
                print(format_report(res, m))
            continue

        res, m = run_once(bars, args.strategy, params, args.cash, sym)
        print(format_report(res, m))
        if args.mc:
            from backtest.montecarlo import format_mc, run_monte_carlo
            mc = run_monte_carlo(res.trades)
            print(format_mc(mc) if mc else "  [MC] 거래 10건 미만 — 표본 부족으로 생략")
        if args.trades:
            print_trades(res)
        save_trades(res, config.LOG_DIR / f"trades_{sym}_{args.strategy}.csv")


def run_validation(symbols, args, params, client):
    """실전 기동 게이트 검증 (감사 권고 R10).

    기준 — 전 종목 합산으로:
      · 검증구간(OOS, 뒤 30%) 거래 합계 >= 30건 (표본)
      · OOS 거래당 기대손익 > 0
      · 전체 거래 몬테카를로 손실확률 < 30%
    통과 시 logs/strategy_validation.json 에 기록 → run_dryrun --live 가 이를 요구한다.
    """
    import json
    from datetime import datetime, timezone, timedelta
    from backtest.montecarlo import run_monte_carlo, format_mc

    all_trades, oos_trades = [], []
    used = []
    for sym in symbols:
        try:
            bars = load_bars(sym, args.interval, args.count, source=args.source,
                             client=client, use_cache=not args.no_cache)
        except Exception as e:              # noqa: BLE001
            print(f"  [!] {sym} 데이터 실패: {e}")
            continue
        strat = build(args.strategy, **params)
        if len(bars) <= strat.warmup() + 40:
            print(f"  [!] {sym} 봉 부족 — 제외")
            continue
        cut = int(len(bars) * 0.7)
        res_all, _ = run_once(bars, args.strategy, params, args.cash, sym)
        res_oos, _ = run_once(bars, args.strategy, params, args.cash, sym, start_index=cut)
        all_trades += res_all.trades
        oos_trades += res_oos.trades
        used.append(sym)
        print(f"  {sym}: 전체 {len(res_all.trades)}건 / OOS {len(res_oos.trades)}건")

    if not all_trades:
        sys.exit("[검증 실패] 거래 표본 없음")
    oos_exp = (sum(t.pnl_rate for t in oos_trades) / len(oos_trades)) if oos_trades else -1
    mc = run_monte_carlo(all_trades)
    mc_loss = mc.prob_loss if mc else 1.0
    ok = len(oos_trades) >= 30 and oos_exp > 0 and mc_loss < 0.30

    record = {
        "strategy": args.strategy, "params": params, "symbols": used,
        "validated_at": datetime.now(timezone(timedelta(hours=9))).isoformat(timespec="seconds"),
        "total_trades": len(all_trades), "oos_trades": len(oos_trades),
        "oos_expectancy": round(oos_exp, 5), "mc_loss_prob": round(mc_loss, 4),
        "criteria": "oos_trades>=30 & oos_expectancy>0 & mc_loss_prob<0.30",
        "passed": ok,
    }
    out = config.LOG_DIR / "strategy_validation.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, ensure_ascii=False, indent=2))

    print("\n" + "=" * 62)
    print(f" 검증 결과: {'✅ 통과' if ok else '❌ 불합격'}  (전략 {args.strategy})")
    print(f"  OOS 거래 {len(oos_trades)}건 (기준 ≥30) | OOS 기대손익 {oos_exp:+.3%} (기준 >0)")
    print(f"  MC 손실확률 {mc_loss:.1%} (기준 <30%)")
    if mc:
        print(format_mc(mc))
    print(f"  기록: {out}")
    if not ok:
        print("  → 이 전략으로는 --live 기동이 거부됩니다 (감사 게이트)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
