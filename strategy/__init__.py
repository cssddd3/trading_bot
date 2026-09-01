"""전략 레지스트리. CLI에서 --strategy <키> 로 고른다."""

from .base import Action, Bar, Fill, Position, Signal, StopOrder, Strategy
from .ma_cross import MaCross
from .supertrend import Supertrend
from .volatility_breakout import VolatilityBreakout

STRATEGIES: dict[str, type[Strategy]] = {
    "vb": VolatilityBreakout,
    "ma": MaCross,
    "st": Supertrend,
}


def build(key: str, **params) -> Strategy:
    if key not in STRATEGIES:
        raise KeyError(f"알 수 없는 전략 '{key}'. 사용 가능: {', '.join(STRATEGIES)}")
    return STRATEGIES[key](**params)


__all__ = ["Action", "Bar", "Fill", "Position", "Signal", "StopOrder", "Strategy",
           "STRATEGIES", "build", "MaCross", "Supertrend", "VolatilityBreakout"]
