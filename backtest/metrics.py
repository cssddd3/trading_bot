"""성과 지표. 백테스트 결과를 '믿을 만한가' 판단하는 데 필요한 것만."""

from dataclasses import dataclass, asdict
from datetime import date as _date

from .engine import BacktestResult

TRADING_DAYS = 252


def _to_date(s: str) -> _date | None:
    try:
        return _date.fromisoformat(s[:10])
    except ValueError:
        return None


@dataclass
class Metrics:
    total_return: float      # 총 수익률
    cagr: float              # 연평균 복리 수익률
    mdd: float               # 최대 낙폭 (음수)
    mdd_days: int            # 최대 낙폭 회복까지 걸린 봉 수
    sharpe: float
    sortino: float
    volatility: float        # 연율화 변동성
    trades: int
    win_rate: float
    profit_factor: float     # 총이익 / 총손실
    avg_win: float
    avg_loss: float
    expectancy: float        # 1거래당 기대 손익률
    avg_holding_bars: float
    exposure: float          # 포지션 보유 봉 비율
    total_cost: float
    cost_drag: float         # 비용이 초기자본 대비 갉아먹은 비율
    buy_and_hold: float      # 같은 기간 단순 보유 수익률

    def as_dict(self) -> dict:
        return asdict(self)


def _drawdown(equity: list[float]) -> tuple[float, int]:
    peak, mdd, peak_i, worst_len = equity[0], 0.0, 0, 0
    for i, v in enumerate(equity):
        if v > peak:
            peak, peak_i = v, i
        dd = v / peak - 1.0
        if dd < mdd:
            mdd, worst_len = dd, i - peak_i
    return mdd, worst_len


def compute(res: BacktestResult) -> Metrics:
    eq = res.equity or [res.initial_cash]
    rets = [eq[i] / eq[i - 1] - 1.0 for i in range(1, len(eq)) if eq[i - 1] > 0]
    n = len(rets) or 1
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / n
    sd = var ** 0.5
    downside = [r for r in rets if r < 0]
    dsd = (sum(r * r for r in downside) / len(downside)) ** 0.5 if downside else 0.0

    total_return = eq[-1] / res.initial_cash - 1.0
    d0, d1 = _to_date(res.dates[0]), _to_date(res.dates[-1])
    years = ((d1 - d0).days / 365.25) if (d0 and d1 and d1 > d0) else len(eq) / TRADING_DAYS
    years = max(years, 1e-9)
    cagr = (eq[-1] / res.initial_cash) ** (1 / years) - 1.0 if eq[-1] > 0 else -1.0

    wins = [t for t in res.trades if t.pnl > 0]
    losses = [t for t in res.trades if t.pnl <= 0]
    gross_win = sum(t.pnl for t in wins)
    gross_loss = -sum(t.pnl for t in losses)
    held = sum(1 for t in res.trades) and sum(
        max(1, _bars_between(res.dates, t.entry_date, t.exit_date)) for t in res.trades)

    mdd, mdd_days = _drawdown(eq)
    bh = res.bars[-1].close / res.bars[0].open - 1.0 if res.bars else 0.0

    return Metrics(
        total_return=total_return,
        cagr=cagr,
        mdd=mdd,
        mdd_days=mdd_days,
        sharpe=(mean / sd * TRADING_DAYS ** 0.5) if sd > 0 else 0.0,
        sortino=(mean / dsd * TRADING_DAYS ** 0.5) if dsd > 0 else 0.0,
        volatility=sd * TRADING_DAYS ** 0.5,
        trades=len(res.trades),
        win_rate=len(wins) / len(res.trades) if res.trades else 0.0,
        profit_factor=(gross_win / gross_loss) if gross_loss > 0 else float("inf") if gross_win else 0.0,
        avg_win=(sum(t.pnl_rate for t in wins) / len(wins)) if wins else 0.0,
        avg_loss=(sum(t.pnl_rate for t in losses) / len(losses)) if losses else 0.0,
        expectancy=(sum(t.pnl_rate for t in res.trades) / len(res.trades)) if res.trades else 0.0,
        avg_holding_bars=(held / len(res.trades)) if res.trades else 0.0,
        exposure=(held / len(eq)) if res.trades else 0.0,
        total_cost=res.total_cost,
        cost_drag=res.total_cost / res.initial_cash,
        buy_and_hold=bh,
    )


def _bars_between(dates: list[str], a: str, b: str) -> int:
    try:
        return dates.index(b) - dates.index(a)
    except ValueError:
        return 1


def format_report(res: BacktestResult, m: Metrics) -> str:
    pct = lambda x: f"{x * 100:>8.2f}%"
    lines = [
        "=" * 62,
        f" 종목 {res.symbol} | 전략 {res.strategy}",
        f" 기간 {res.dates[0][:10]} ~ {res.dates[-1][:10]}  ({len(res.dates)}봉)",
        "=" * 62,
        f"  총 수익률      {pct(m.total_return)}     단순보유 {pct(m.buy_and_hold)}",
        f"  CAGR           {pct(m.cagr)}     변동성   {pct(m.volatility)}",
        f"  MDD            {pct(m.mdd)}     회복소요 {m.mdd_days}봉",
        f"  Sharpe         {m.sharpe:>8.2f}      Sortino  {m.sortino:.2f}",
        "-" * 62,
        f"  거래 횟수      {m.trades:>8}       승률     {pct(m.win_rate)}",
        f"  손익비(PF)     {m.profit_factor:>8.2f}      기대손익 {pct(m.expectancy)}",
        f"  평균 수익      {pct(m.avg_win)}     평균 손실 {pct(m.avg_loss)}",
        f"  평균 보유      {m.avg_holding_bars:>8.1f}봉     노출도   {pct(m.exposure)}",
        f"  총 비용        {m.total_cost:>8,.0f}원    자본대비 {pct(m.cost_drag)}",
        "=" * 62,
    ]
    return "\n".join(lines)
