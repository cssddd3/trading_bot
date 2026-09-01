"""전략 공통 인터페이스.

설계 원칙 (미래참조 편향 방지):
  - `on_open(i)`  : i번째 봉의 **시가까지만** 알고 있는 시점. 여기서 장중 조건부
                    주문(스탑 매수/손절)을 낸다. 변동성 돌파 전략이 쓰는 자리.
  - `on_close(i)` : i번째 봉의 **종가가 확정된** 시점. 여기서 나온 시그널은
                    당일 종가 또는 익일 시가에 체결된다. 이동평균/추세 전략 자리.

즉 전략은 절대로 '아직 오지 않은 봉'의 고가·저가·종가를 볼 수 없다.
백테스트 엔진과 드라이런이 같은 메서드를 호출하므로 검증한 것과 돌리는 것이 같다.
"""

from dataclasses import dataclass, field
from enum import Enum


@dataclass(frozen=True)
class Bar:
    """일봉/분봉 하나."""
    date: str          # '2026-08-21' (일봉) / '2026-08-21T09:31:00+09:00' (분봉)
    open: float
    high: float
    low: float
    close: float
    volume: float


class Action(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class Fill(str, Enum):
    """체결 시점."""
    THIS_CLOSE = "THIS_CLOSE"   # 이 봉 종가에 체결 (실전: 15:20 동시호가 근처)
    NEXT_OPEN = "NEXT_OPEN"     # 다음 봉 시가에 체결 (가장 보수적·현실적)


@dataclass
class Signal:
    action: Action
    reason: str = ""
    fill: Fill = Fill.NEXT_OPEN
    weight: float = 1.0          # 진입 시 사용할 현금 비중 (0~1)


@dataclass
class StopOrder:
    """당일 장중에만 유효한 조건부 주문 (다음 봉으로 넘어가면 자동 취소)."""
    side: Action                 # BUY: 고가가 price 이상이면 체결 / SELL: 저가가 price 이하면 체결
    price: float
    reason: str = ""
    weight: float = 1.0


@dataclass
class Position:
    quantity: int = 0
    avg_price: float = 0.0
    entry_index: int = -1
    entry_date: str = ""
    highest_close: float = 0.0   # 진입 후 최고 종가 (트레일링 스탑용)
    stop_price: float | None = None
    meta: dict = field(default_factory=dict)

    @property
    def is_open(self) -> bool:
        return self.quantity > 0

    def unrealized_rate(self, price: float) -> float:
        if not self.is_open or self.avg_price <= 0:
            return 0.0
        return price / self.avg_price - 1.0


class Strategy:
    """전략 베이스. 하위 클래스는 prepare / on_open / on_close 를 필요한 만큼 구현한다."""

    name = "base"

    def __init__(self, **params):
        self.params = params
        for k, v in params.items():
            setattr(self, k, v)

    # 전체 봉을 받아 지표를 미리 계산해둔다 (엔진이 루프 전에 1회 호출).
    # 지표는 i번째 값이 i번째 봉까지만 사용하도록 계산돼야 한다 (indicators.py 보장).
    def prepare(self, bars: list[Bar]) -> None:
        self.bars = bars

    def warmup(self) -> int:
        """지표가 안정되기까지 필요한 봉 수. 이 인덱스 전에는 매매하지 않는다."""
        return 0

    def on_open(self, i: int, pos: Position) -> StopOrder | None:
        return None

    def on_close(self, i: int, pos: Position) -> Signal | None:
        return None

    def describe(self) -> str:
        ps = ", ".join(f"{k}={v}" for k, v in sorted(self.params.items()))
        return f"{self.name}({ps})"
