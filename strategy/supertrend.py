"""Supertrend 추세추종 (ATR 밴드 전환).

freqtrade / jesse 커뮤니티 전략 저장소에서 단독으로 가장 많이 쓰이는 지표 기반 전략.
  진입 = 추세가 하락(-1) → 상승(1) 으로 전환 + 장기 EMA 위 + RSI 과열 아님
  청산 = 추세가 상승 → 하락 전환 (밴드가 곧 손절선 역할을 한다)
"""

from . import indicators as ta
from .base import Action, Bar, Fill, Position, Signal, StopOrder, Strategy


class Supertrend(Strategy):
    name = "supertrend"

    def __init__(self, atr_n: int = 10, mult: float = 3.0, trend: int = 200,
                 rsi_n: int = 14, rsi_max: float = 75.0, use_band_stop: bool = True):
        super().__init__(atr_n=atr_n, mult=mult, trend=trend,
                         rsi_n=rsi_n, rsi_max=rsi_max, use_band_stop=use_band_stop)

    def prepare(self, bars: list[Bar]) -> None:
        self.bars = bars
        closes = [b.close for b in bars]
        self.line, self.dir = ta.supertrend(bars, self.atr_n, self.mult)
        self.ema_trend = ta.ema(closes, self.trend) if self.trend else [None] * len(bars)
        self.rsi = ta.rsi(closes, self.rsi_n)

    def warmup(self) -> int:
        return max(self.atr_n * 3, self.trend or 0, self.rsi_n) + 1

    def on_open(self, i: int, pos: Position) -> StopOrder | None:
        if pos.is_open and self.use_band_stop and pos.stop_price:
            return StopOrder(Action.SELL, pos.stop_price, reason="Supertrend 밴드 이탈")
        return None

    def on_close(self, i: int, pos: Position) -> Signal | None:
        if i < self.warmup() or self.dir[i] is None:
            return None
        c = self.bars[i].close

        if not pos.is_open:
            flipped_up = self.dir[i] == 1 and self.dir[i - 1] == -1
            if not flipped_up:
                return None
            if self.trend and (self.ema_trend[i] is None or c < self.ema_trend[i]):
                return None
            if self.rsi[i] and self.rsi[i] > self.rsi_max:
                return None  # 이미 과열된 뒤에 따라붙지 않는다
            return Signal(Action.BUY, "Supertrend 상승 전환", fill=Fill.NEXT_OPEN)

        pos.stop_price = self.line[i]   # 밴드 자체가 손절선
        if self.dir[i] == -1:
            return Signal(Action.SELL, "Supertrend 하락 전환", fill=Fill.NEXT_OPEN)
        return None
