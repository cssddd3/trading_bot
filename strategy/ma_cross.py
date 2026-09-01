"""EMA 골든/데드크로스 + ATR 트레일링 스탑.

freqtrade 계열 전략에서 반복적으로 등장하는 조합을 그대로 옮겼다:
  진입 = 단기 EMA가 장기 EMA를 상향 돌파 + 장기 추세 필터(EMA200) 위
  청산 = 데드크로스  또는  ATR 기반 트레일링 스탑 이탈

교차만으로 진입하면 트레일링 스탑에 한 번 털린 뒤 추세가 계속돼도 재크로스가 없어
다시 못 올라탄다 (실제로 삼성전자 2024-07 이후 구간에서 그렇게 됐다).
그래서 freqtrade 계열 전략처럼 '상태 조건' 재진입을 둔다:
정배열(EMA단기>EMA장기) + 추세 필터 위 + 종가가 단기 EMA를 다시 상향 돌파 → 눌림목 재진입.

트레일링 스탑은 '진입 후 최고 종가 - ATR x mult' 로 계속 올라가기만 한다 (내려가지 않음).
추세추종은 이기는 거래를 오래 끌고 가는 것이 전부라, 익절 목표를 두지 않는다.
"""

from . import indicators as ta
from .base import Action, Bar, Fill, Position, Signal, StopOrder, Strategy


class MaCross(Strategy):
    name = "ma_cross"

    def __init__(self, fast: int = 20, slow: int = 60, trend: int = 200,
                 atr_n: int = 14, atr_mult: float = 2.5, use_trailing: bool = True,
                 pullback_reentry: bool = True):
        super().__init__(fast=fast, slow=slow, trend=trend, atr_n=atr_n,
                         atr_mult=atr_mult, use_trailing=use_trailing,
                         pullback_reentry=pullback_reentry)

    def prepare(self, bars: list[Bar]) -> None:
        self.bars = bars
        closes = [b.close for b in bars]
        self.ema_fast = ta.ema(closes, self.fast)
        self.ema_slow = ta.ema(closes, self.slow)
        self.ema_trend = ta.ema(closes, self.trend) if self.trend else [None] * len(bars)
        self.atr = ta.atr(bars, self.atr_n)

    def warmup(self) -> int:
        return max(self.slow, self.trend or 0, self.atr_n) + 1

    def _pullback(self, i: int) -> bool:
        """정배열 상태에서 종가가 단기 EMA를 다시 상향 돌파하는 순간."""
        f, s = self.ema_fast[i], self.ema_slow[i]
        if f is None or s is None or f <= s:
            return False
        prev, cur = self.bars[i - 1].close, self.bars[i].close
        pf = self.ema_fast[i - 1]
        return pf is not None and prev <= pf and cur > f

    def on_open(self, i: int, pos: Position) -> StopOrder | None:
        # 트레일링 스탑은 장중에 걸어둔다 (종가까지 기다리면 하루치 더 밀린다).
        if pos.is_open and pos.stop_price:
            return StopOrder(Action.SELL, pos.stop_price, reason="트레일링 스탑")
        return None

    def on_close(self, i: int, pos: Position) -> Signal | None:
        if i < self.warmup():
            return None
        c = self.bars[i].close

        if not pos.is_open:
            if self.trend and (self.ema_trend[i] is None or c < self.ema_trend[i]):
                return None  # 장기 하락추세에서는 어떤 진입 신호도 믿지 않는다
            if ta.crossed_up(self.ema_fast, self.ema_slow, i):
                return Signal(Action.BUY, f"골든크로스 EMA{self.fast}>EMA{self.slow}",
                              fill=Fill.NEXT_OPEN)
            if self.pullback_reentry and self._pullback(i):
                return Signal(Action.BUY, f"눌림목 재진입 (정배열 + 종가>EMA{self.fast})",
                              fill=Fill.NEXT_OPEN)
            return None

        # 보유 중: 트레일링 스탑 갱신 (다음 봉 on_open 에서 사용)
        if self.use_trailing and self.atr[i]:
            pos.highest_close = max(pos.highest_close, c)
            new_stop = pos.highest_close - self.atr[i] * self.atr_mult
            pos.stop_price = max(pos.stop_price or 0.0, new_stop)

        if ta.crossed_down(self.ema_fast, self.ema_slow, i):
            return Signal(Action.SELL, "데드크로스", fill=Fill.NEXT_OPEN)
        return None
