"""백테스트/드라이런용 캔들 데이터 로더.

토스 캔들 API는 한 번에 200봉이지만 `before`/`nextBefore` 페이지네이션을 지원하므로
과거로 계속 거슬러 올라가 수년치 일봉을 모을 수 있다 (수정주가 adjusted=true).
받은 데이터는 data/ 아래 CSV로 캐시해 두고, 다음 실행 때는 없는 구간만 새로 받는다.

우선순위(source='auto'): CSV 캐시 → 토스 API → pykrx(설치돼 있으면)
"""

import csv
import time
from pathlib import Path

from strategy.base import Bar

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CHART_SLEEP = 0.06          # CHART 그룹 20 req/s → 여유 있게


# ── 변환 ────────────────────────────────────────────────────

def _norm_date(ts: str, interval: str) -> str:
    return ts[:10] if interval == "1d" else ts


def bars_from_candles(candles: list[dict], interval: str = "1d") -> list[Bar]:
    """API 응답(최신순, 값은 문자열) → 오래된 순 Bar 리스트."""
    out = []
    for c in candles:
        try:
            out.append(Bar(
                date=_norm_date(c["timestamp"], interval),
                open=float(c["openPrice"]),
                high=float(c["highPrice"]),
                low=float(c["lowPrice"]),
                close=float(c["closePrice"]),
                volume=float(c.get("volume") or 0),
            ))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(out, key=lambda b: b.date)


def merge(*groups: list[Bar]) -> list[Bar]:
    """여러 소스를 날짜 기준으로 합치고 중복 제거 (뒤에 온 것이 우선)."""
    table: dict[str, Bar] = {}
    for g in groups:
        for b in g:
            table[b.date] = b
    return [table[k] for k in sorted(table)]


# ── CSV 캐시 ────────────────────────────────────────────────

def cache_path(symbol: str, interval: str = "1d") -> Path:
    return DATA_DIR / f"{symbol}_{interval}.csv"


def load_csv(path: Path) -> list[Bar]:
    if not path.exists():
        return []
    with path.open(newline="") as f:
        return sorted(
            (Bar(r["date"], float(r["open"]), float(r["high"]), float(r["low"]),
                 float(r["close"]), float(r["volume"])) for r in csv.DictReader(f)),
            key=lambda b: b.date)


def save_csv(path: Path, bars: list[Bar]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["date", "open", "high", "low", "close", "volume"])
        for b in bars:
            w.writerow([b.date, b.open, b.high, b.low, b.close, b.volume])


# ── 소스별 수집 ──────────────────────────────────────────────

def fetch_toss(client, symbol: str, interval: str = "1d", max_bars: int = 1200,
               until_date: str | None = None, verbose: bool = True) -> list[Bar]:
    """토스 API를 페이지네이션으로 거슬러 올라가며 캔들 수집."""
    collected: list[Bar] = []
    before = None
    while len(collected) < max_bars:
        page = client.get_candles(symbol, interval=interval, count=200,
                                  before=before, adjusted=True)
        batch = bars_from_candles(page.get("candles", []), interval)
        if not batch:
            break
        collected = merge(batch, collected)
        if verbose:
            print(f"    {symbol}: {len(collected)}봉 수집 ({collected[0].date} ~)", end="\r")
        before = page.get("nextBefore")
        if not before:
            break
        if until_date and collected[0].date <= until_date:
            break
        time.sleep(CHART_SLEEP)
    if verbose and collected:
        print(f"    {symbol}: {len(collected)}봉 수집 완료 "
              f"({collected[0].date} ~ {collected[-1].date})")
    return collected[-max_bars:]


def fetch_pykrx(symbol: str, days: int = 1200) -> list[Bar]:
    """pykrx 폴백 (pip3 install pykrx). 토스 IP 화이트리스트에 막혔을 때 유용."""
    from datetime import datetime, timedelta

    from pykrx import stock  # type: ignore

    end = datetime.now()
    start = end - timedelta(days=int(days * 1.6) + 30)   # 휴장일 감안
    df = stock.get_market_ohlcv(start.strftime("%Y%m%d"), end.strftime("%Y%m%d"),
                                symbol, adjusted=True)
    bars = [Bar(str(idx.date()), float(r["시가"]), float(r["고가"]), float(r["저가"]),
                float(r["종가"]), float(r["거래량"]))
            for idx, r in df.iterrows() if float(r["시가"]) > 0]
    return bars[-days:]


def load_bars(symbol: str, interval: str = "1d", count: int = 1200,
              source: str = "auto", client=None, use_cache: bool = True,
              verbose: bool = True) -> list[Bar]:
    """캔들 로드. source: auto | toss | pykrx | csv"""
    path = cache_path(symbol, interval)
    cached = load_csv(path) if use_cache else []

    if source == "csv":
        if not cached:
            raise FileNotFoundError(f"캐시 없음: {path}")
        return cached[-count:]

    fresh: list[Bar] = []
    if source in ("auto", "toss") and client is not None:
        try:
            fresh = fetch_toss(client, symbol, interval, max_bars=count, verbose=verbose)
        except Exception as e:                      # noqa: BLE001 - 폴백을 위해 광범위 캐치
            if source == "toss":
                raise
            print(f"  [!] 토스 캔들 조회 실패 → pykrx/캐시로 폴백: {e}")

    if not fresh and source in ("auto", "pykrx"):
        try:
            fresh = fetch_pykrx(symbol, count)
        except ImportError:
            if source == "pykrx":
                raise
        except Exception as e:                      # noqa: BLE001
            if source == "pykrx":
                raise
            print(f"  [!] pykrx 조회 실패: {e}")

    bars = merge(cached, fresh)
    if use_cache and fresh:
        save_csv(path, bars)
    if not bars:
        raise RuntimeError(
            f"{symbol}: 캔들을 가져오지 못했습니다. .env 키/허용 IP를 확인하거나 "
            f"`pip3 install pykrx` 후 --source pykrx 로 시도하세요.")
    return bars[-count:]
