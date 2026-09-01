"""봉 단위 백테스트 엔진.

체결 규칙 (실제보다 불리하게 잡는 것이 원칙 — 백테스트가 예뻐 보이는 게 목적이 아니다):
  1. 전 봉 종가에 나온 NEXT_OPEN 시그널  → 이 봉 시가에 체결 (+슬리피지)
  2. on_open() 의 조건부 주문
       - 매수 스탑: 고가 >= 목표가 → max(목표가, 시가) 에 체결 (갭 상승이면 시가)
       - 매도 스탑: 저가 <= 손절가 → min(손절가, 시가) 에 체결 (갭 하락이면 시가)
       - 같은 봉에서 매수 후 손절가도 닿았다면 **둘 다 체결된 것으로 본다** (최악 가정)
  3. on_close() 의 THIS_CLOSE 시그널 → 이 봉 종가에 체결

비용: 매수 수수료 / 매도 수수료+증권거래세 / 양방향 슬리피지 / 호가단위 반올림.
"""

from dataclasses import dataclass, field

from strategy.base import Action, Bar, Fill, Position, Strategy


@dataclass
class Costs:
    """기본값은 토스증권 국내주식 기준 (GET /api/v1/commissions 로 실제값 확인 가능)."""
    fee_rate: float = 0.00015      # 매매 수수료 (매수/매도 각각)
    sell_tax_rate: float = 0.0015  # 증권거래세 (매도 시에만, 코스피/코스닥 0.15%)
    slippage_rate: float = 0.001   # 체결 미끄러짐 (소액이어도 보수적으로 0.1%)


@dataclass
class Trade:
    symbol: str
    entry_date: str
    entry_price: float
    exit_date: str = ""
    exit_price: float = 0.0
    quantity: int = 0
    entry_reason: str = ""
    exit_reason: str = ""
    cost: float = 0.0              # 왕복 수수료+세금 합계

    @property
    def pnl(self) -> float:
        return (self.exit_price - self.entry_price) * self.quantity - self.cost

    @property
    def pnl_rate(self) -> float:
        base = self.entry_price * self.quantity
        return self.pnl / base if base else 0.0


@dataclass
class BacktestResult:
    symbol: str
    strategy: str
    bars: list[Bar]
    equity: list[float] = field(default_factory=list)      # 봉마다의 평가자산
    dates: list[str] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    initial_cash: float = 0.0
    total_cost: float = 0.0


TICKS = [(2_000, 1), (5_000, 5), (20_000, 10), (50_000, 50),
         (200_000, 100), (500_000, 500)]


def round_to_tick(price: float) -> float:
    """국내 주식 호가단위로 내림 정렬 (2023.1 개편 기준)."""
    for limit, tick in TICKS:
        if price < limit:
            return (price // tick) * tick
    return (price // 1000) * 1000


class Backtester:
    def __init__(self, costs: Costs | None = None, initial_cash: float = 1_000_000,
                 position_pct: float = 1.0, allow_fractional_cash: bool = True):
        self.costs = costs or Costs()
        self.initial_cash = initial_cash
        self.position_pct = position_pct   # 종목당 투입할 현금 비중

    # ── 체결 헬퍼 ────────────────────────────────────────────

    def _buy_price(self, raw: float) -> float:
        return round_to_tick(raw * (1 + self.costs.slippage_rate))

    def _sell_price(self, raw: float) -> float:
        return round_to_tick(raw * (1 - self.costs.slippage_rate))

    def run(self, bars: list[Bar], strategy: Strategy, symbol: str = "",
            start_index: int = 0) -> BacktestResult:
        """start_index 이전 봉은 지표 계산에만 쓰고 매매/성과 집계에서는 제외한다.

        검증구간(OOS) 백테스트에서 지표 워밍업 때문에 초반이 통째로 날아가는 것을 막는다.
        """
        strategy.prepare(bars)
        res = BacktestResult(symbol=symbol, strategy=strategy.describe(),
                             bars=bars[start_index:],
                             initial_cash=self.initial_cash)
        cash = self.initial_cash
        pos = Position()
        pending = None       # (Action, reason, weight) — 다음 봉 시가 체결 예약
        open_trade: Trade | None = None

        def do_buy(price: float, reason: str, date: str, i: int, weight: float):
            nonlocal cash, pos, open_trade
            budget = cash * min(self.position_pct * weight, 1.0)
            qty = int(budget // (price * (1 + self.costs.fee_rate)))
            if qty <= 0:
                return
            gross = price * qty
            fee = gross * self.costs.fee_rate
            cash -= gross + fee
            pos = Position(quantity=qty, avg_price=price, entry_index=i,
                           entry_date=date, highest_close=bars[i].close)
            open_trade = Trade(symbol=symbol, entry_date=date, entry_price=price,
                               quantity=qty, entry_reason=reason, cost=fee)
            res.total_cost += fee

        def do_sell(price: float, reason: str, date: str):
            nonlocal cash, pos, open_trade
            if not pos.is_open or open_trade is None:
                return
            gross = price * pos.quantity
            fee = gross * self.costs.fee_rate + gross * self.costs.sell_tax_rate
            cash += gross - fee
            open_trade.exit_date = date
            open_trade.exit_price = price
            open_trade.exit_reason = reason
            open_trade.cost += fee
            res.total_cost += fee
            res.trades.append(open_trade)
            open_trade = None
            pos = Position()

        for i, bar in enumerate(bars):
            if i < start_index:
                continue
            # 1) 전 봉에서 예약된 시가 주문
            if pending:
                action, reason, weight = pending
                pending = None
                if action == Action.BUY and not pos.is_open:
                    do_buy(self._buy_price(bar.open), reason, bar.date, i, weight)
                elif action == Action.SELL and pos.is_open:
                    do_sell(self._sell_price(bar.open), reason, bar.date)

            # 2) 장중 조건부 주문
            order = strategy.on_open(i, pos)
            if order:
                if order.side == Action.BUY and not pos.is_open and bar.high >= order.price:
                    fill = max(order.price, bar.open)          # 갭 상승이면 시가에 체결
                    do_buy(self._buy_price(fill), order.reason, bar.date, i, order.weight)
                    # 매수 직후 같은 봉에서 손절선이 닿았는지 최악 가정으로 재확인
                    protect = strategy.on_open(i, pos)
                    if (protect and protect.side == Action.SELL and pos.is_open
                            and bar.low <= protect.price):
                        do_sell(self._sell_price(protect.price),
                                protect.reason + " (당일)", bar.date)
                elif order.side == Action.SELL and pos.is_open and bar.low <= order.price:
                    fill = min(order.price, bar.open)          # 갭 하락이면 시가에 체결
                    do_sell(self._sell_price(fill), order.reason, bar.date)

            # 3) 종가 확정 후 판단
            sig = strategy.on_close(i, pos)
            if sig and sig.action != Action.HOLD:
                if sig.fill == Fill.THIS_CLOSE:
                    if sig.action == Action.BUY and not pos.is_open:
                        do_buy(self._buy_price(bar.close), sig.reason, bar.date, i, sig.weight)
                    elif sig.action == Action.SELL and pos.is_open:
                        do_sell(self._sell_price(bar.close), sig.reason, bar.date)
                elif i + 1 < len(bars):
                    pending = (sig.action, sig.reason, sig.weight)

            res.equity.append(cash + pos.quantity * bar.close)
            res.dates.append(bar.date)

        # 마지막 봉에서 열린 포지션은 종가로 강제 청산해 성과를 확정한다.
        if pos.is_open:
            do_sell(self._sell_price(bars[-1].close), "백테스트 종료 청산", bars[-1].date)
            res.equity[-1] = cash

        return res
