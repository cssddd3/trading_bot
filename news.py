"""종목 관련 뉴스 헤드라인 수집 (구글뉴스 + 인베스팅닷컴 RSS — API 키 불필요).

LLM 필터(llm_filter.py)의 입력이 된다. 소스 2개를 합친다:
  - 구글뉴스 RSS: 종목명 검색 — 커버리지 넓지만 인덱싱 지연 수분~수십분
  - 인베스팅닷컴 RSS: 최신 금융기사 피어호스 — 분 단위로 신선, 종목명 매칭으로 필터
    (Pro 구독과 무관한 공개 피드 — 구독 콘텐츠는 API가 없어 봇 연동 불가)
"""

import time as _time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

INVESTING_FEEDS = [
    "https://kr.investing.com/rss/news_25.rss",   # 주식시장 (한국어)
    "https://kr.investing.com/rss/news.rss",      # 전체 (한국어)
    "https://www.investing.com/rss/news_25.rss",  # stock market (영문 — 미국 티커용)
]
_INV_CACHE: dict = {"ts": 0.0, "items": []}
_INV_TTL = 120          # 피드 캐시(초) — 후보 여러 개를 연달아 조회해도 3콜만


@dataclass
class Headline:
    title: str
    source: str
    published: str          # ISO8601


def _parse_items(xml: bytes, cutoff: datetime) -> list[tuple[str, str, datetime]]:
    out = []
    for item in ET.fromstring(xml).findall(".//item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        raw = item.findtext("pubDate") or ""
        try:                                 # RFC822 (구글) / ISO (인베스팅) 둘 다 처리
            pub = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            try:
                pub = datetime.fromisoformat(raw).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
        if pub < cutoff:
            continue
        src = item.find("source")           # 구글뉴스 <source>는 네임스페이스 없음
        source = (src.text or "").strip() if src is not None else ""
        out.append((title, source, pub))
    return out


def _investing_items(cutoff: datetime) -> list[tuple[str, str, datetime]]:
    """인베스팅닷컴 피드 전체 기사 (캐시 공유 — 여러 종목 조회 시 재사용)."""
    if _time.time() - _INV_CACHE["ts"] < _INV_TTL:
        items = _INV_CACHE["items"]
    else:
        items = []
        for url in INVESTING_FEEDS:
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
                items += _parse_items(urllib.request.urlopen(req, timeout=10).read(),
                                      datetime.now(timezone.utc) - timedelta(hours=48))
            except Exception:               # noqa: BLE001 - 보조 소스 장애는 무시
                continue
        _INV_CACHE.update(ts=_time.time(), items=items)
    return [(t, s, p) for t, s, p in items if p >= cutoff]


def fetch_headlines(query: str, max_items: int = 12,
                    within_hours: int = 48) -> list[Headline]:
    """구글뉴스 검색 + 인베스팅닷컴 피드에서 종목 관련 헤드라인 수집 (최신순)."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=within_hours)
    rows: list[tuple[str, str, datetime]] = []

    url = ("https://news.google.com/rss/search?"
           f"q={urllib.parse.quote(query)}&hl=ko&gl=KR&ceid=KR:ko")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        rows += _parse_items(urllib.request.urlopen(req, timeout=10).read(), cutoff)
    except Exception:                       # noqa: BLE001
        pass

    # 인베스팅닷컴은 검색이 아니라 최신기사 스트림 → 종목명 토큰이 제목에 있으면 매칭
    tokens = [w for w in query.replace("주식", "").replace("stock", "").split()
              if len(w) >= 2]
    if tokens:
        for title, source, pub in _investing_items(cutoff):
            if any(tok.lower() in title.lower() for tok in tokens):
                rows.append((title, source or "Investing.com", pub))

    out, seen = [], set()
    for title, source, pub in rows:
        if source and title.endswith(f" - {source}"):
            title = title[: -len(source) - 3].strip()   # 구글뉴스 제목 꼬리 정리
        key = title.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(Headline(title=title, source=source, published=pub.isoformat()))
    out.sort(key=lambda h: h.published, reverse=True)
    return out[:max_items]


if __name__ == "__main__":
    import sys
    q = sys.argv[1] if len(sys.argv) > 1 else "삼성전자 주가"
    for h in fetch_headlines(q):
        print(f"[{h.published[:16]}] {h.title}  ({h.source})")
