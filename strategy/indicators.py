"""지표 계산 (순수 파이썬 — numpy/pandas 불필요).

모든 함수는 입력과 같은 길이의 리스트를 돌려주고, 계산이 불가능한 앞부분은 None 이다.
i번째 값은 **i번째 봉까지의 데이터만** 사용한다 (미래참조 없음).
"""

from .base import Bar

Series = list[float | None]


def sma(values: list[float], n: int) -> Series:
    out: Series = [None] * len(values)
    if n <= 0:
        return out
    total = 0.0
    for i, v in enumerate(values):
        total += v
        if i >= n:
            total -= values[i - n]
        if i >= n - 1:
            out[i] = total / n
    return out


def ema(values: list[float], n: int) -> Series:
    out: Series = [None] * len(values)
    if n <= 0 or len(values) < n:
        return out
    k = 2.0 / (n + 1)
    prev = sum(values[:n]) / n          # 초기값은 단순평균 (backtrader/TA-Lib 관행)
    out[n - 1] = prev
    for i in range(n, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def rma(values: list[float], n: int) -> Series:
    """Wilder 평활 (RSI/ATR 용). alpha = 1/n"""
    out: Series = [None] * len(values)
    if n <= 0 or len(values) < n:
        return out
    prev = sum(values[:n]) / n
    out[n - 1] = prev
    for i in range(n, len(values)):
        prev = (prev * (n - 1) + values[i]) / n
        out[i] = prev
    return out


def true_range(bars: list[Bar]) -> list[float]:
    out = []
    for i, b in enumerate(bars):
        if i == 0:
            out.append(b.high - b.low)
        else:
            pc = bars[i - 1].close
            out.append(max(b.high - b.low, abs(b.high - pc), abs(b.low - pc)))
    return out


def atr(bars: list[Bar], n: int = 14) -> Series:
    return rma(true_range(bars), n)


def rsi(values: list[float], n: int = 14) -> Series:
    gains, losses = [0.0], [0.0]
    for i in range(1, len(values)):
        d = values[i] - values[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag, al = rma(gains[1:], n), rma(losses[1:], n)
    out: Series = [None] * len(values)
    for i in range(len(ag)):
        if ag[i] is None:
            continue
        if al[i] == 0:
            out[i + 1] = 100.0
        else:
            rs = ag[i] / al[i]
            out[i + 1] = 100.0 - 100.0 / (1.0 + rs)
    return out


def stdev(values: list[float], n: int) -> Series:
    out: Series = [None] * len(values)
    for i in range(n - 1, len(values)):
        win = values[i - n + 1:i + 1]
        m = sum(win) / n
        out[i] = (sum((v - m) ** 2 for v in win) / n) ** 0.5
    return out


def highest(values: list[float], n: int) -> Series:
    out: Series = [None] * len(values)
    for i in range(n - 1, len(values)):
        out[i] = max(values[i - n + 1:i + 1])
    return out


def lowest(values: list[float], n: int) -> Series:
    out: Series = [None] * len(values)
    for i in range(n - 1, len(values)):
        out[i] = min(values[i - n + 1:i + 1])
    return out


def noise(bars: list[Bar], n: int = 20) -> Series:
    """노이즈 비율의 n일 평균. 1 - |종가-시가| / (고가-저가).

    0에 가까울수록 하루 움직임이 한 방향(추세적) → 돌파 신뢰도가 높다.
    변동성 돌파의 k값을 고정하지 않고 이 값으로 바꾸는 방식은
    국내 자동매매 커뮤니티에서 널리 쓰이는 '동적 k' 기법이다.
    """
    raw = []
    for b in bars:
        rng = b.high - b.low
        raw.append(1.0 - abs(b.close - b.open) / rng if rng > 0 else 1.0)
    return sma(raw, n)


def supertrend(bars: list[Bar], n: int = 10, mult: float = 3.0
               ) -> tuple[Series, list[int | None]]:
    """Supertrend 라인과 추세 방향(1=상승, -1=하락). freqtrade 인기 전략의 핵심 지표."""
    a = atr(bars, n)
    line: Series = [None] * len(bars)
    direction: list[int | None] = [None] * len(bars)
    up_prev = dn_prev = None
    dir_prev = 1
    for i, b in enumerate(bars):
        if a[i] is None:
            continue
        mid = (b.high + b.low) / 2
        up = mid - mult * a[i]      # 상승추세일 때의 지지선
        dn = mid + mult * a[i]      # 하락추세일 때의 저항선
        if up_prev is not None:
            up = max(up, up_prev) if bars[i - 1].close > up_prev else up
            dn = min(dn, dn_prev) if bars[i - 1].close < dn_prev else dn
            if b.close > dn_prev:
                d = 1
            elif b.close < up_prev:
                d = -1
            else:
                d = dir_prev
        else:
            d = 1
        line[i] = up if d == 1 else dn
        direction[i] = d
        up_prev, dn_prev, dir_prev = up, dn, d
    return line, direction


def crossed_up(fast: Series, slow: Series, i: int) -> bool:
    if i < 1 or None in (fast[i], slow[i], fast[i - 1], slow[i - 1]):
        return False
    return fast[i - 1] <= slow[i - 1] and fast[i] > slow[i]


def crossed_down(fast: Series, slow: Series, i: int) -> bool:
    if i < 1 or None in (fast[i], slow[i], fast[i - 1], slow[i - 1]):
        return False
    return fast[i - 1] >= slow[i - 1] and fast[i] < slow[i]
