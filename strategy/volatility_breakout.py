"""변동성 돌파 (Larry Williams). 국내 자동매매 봇에서 가장 널리 복제된 전략.

  당일 목표가 = 당일 시가 + (전일 고가 - 전일 저가) x k
  장중 고가가 목표가를 넘으면 매수, 당일 종가(또는 익일 시가)에 청산.

원본 대비 추가한 것 (모두 실전 수익률보다 '덜 깨지는 것'에 초점):
  - 동적 k : k를 0.5로 고정하지 않고 최근 노이즈 평균을 쓴다. 추세적인 종목/구간에서
             k가 낮아져 빨리 올라타고, 횡보 구간에서는 k가 높아져 덜 속는다.
  - 이동평균 필터 : 당일 시가가 N일 이동평균 아래면 아예 진입하지 않는다 (하락장 회피).
  - 손절 : 진입가 대비 -stop_loss_rate 를 장중 스탑으로 건다. 원본에는 없지만
           갭하락 구간에서 손실 꼬리를 자른다.
"""

from . import indicators as ta
from .base import Action, Bar, Fill, Position, Signal, StopOrder, Strategy


class VolatilityBreakout(Strategy):
    name = "volatility_breakout"

    def __init__(self, k: float = 0.5, dynamic_k: bool = True,
                 noise_window: int = 20, ma_filter: int = 5,
                 stop_loss_rate: float = 0.03, exit_at_close: bool = True,
                 k_min: float = 0.3, k_max: float = 0.8):
        super().__init__(k=k, dynamic_k=dynamic_k, noise_window=noise_window,
                         ma_filter=ma_filter, stop_loss_rate=stop_loss_rate,
                         exit_at_close=exit_at_close, k_min=k_min, k_max=k_max)

    def prepare(self, bars: list[Bar]) -> None:
        self.bars = bars
        closes = [b.close for b in bars]
        self.ma = ta.sma(closes, self.ma_filter) if self.ma_filter else [None] * len(bars)
        self.noise = ta.noise(bars, self.noise_window) if self.dynamic_k else [None] * len(bars)

    def warmup(self) -> int:
        return max(self.ma_filter, self.noise_window if self.dynamic_k else 0) + 1

    def _k_for(self, i: int) -> float:
        """i번째 봉에 적용할 k. 전일(i-1)까지의 정보만 사용."""
        if not self.dynamic_k or self.noise[i - 1] is None:
            return self.k
        return min(max(self.noise[i - 1], self.k_min), self.k_max)

    def on_open(self, i: int, pos: Position) -> StopOrder | None:
        if i < self.warmup():
            return None
        today, prev = self.bars[i], self.bars[i - 1]

        # 보유 중이면 손절 스탑만 건다.
        if pos.is_open:
            if self.stop_loss_rate > 0:
                stop = pos.avg_price * (1 - self.stop_loss_rate)
                return StopOrder(Action.SELL, stop, reason=f"손절 -{self.stop_loss_rate:.1%}")
            return None

        # 이동평균 필터: 당일 시가가 전일까지의 MA 아래면 진입 금지.
        if self.ma_filter and (self.ma[i - 1] is None or today.open < self.ma[i - 1]):
            return None

        k = self._k_for(i)
        target = today.open + (prev.high - prev.low) * k
        return StopOrder(Action.BUY, target, reason=f"변동성돌파 k={k:.2f} 목표가={target:,.0f}")

    def on_close(self, i: int, pos: Position) -> Signal | None:
        if not pos.is_open:
            return None
        # 당일 진입분은 당일 종가에 청산 (오버나이트 리스크를 지지 않는 것이 원본 규칙).
        if self.exit_at_close:
            return Signal(Action.SELL, "종가 청산", fill=Fill.THIS_CLOSE)
        return Signal(Action.SELL, "익일 시가 청산", fill=Fill.NEXT_OPEN)
