"""LLM 뉴스 필터 — Claude가 종목 뉴스를 읽고 매수 거부권(veto)을 행사한다.

역할을 엄격히 제한한다:
  - 전략(매수 시그널)은 규칙 기반 코드가 만든다. LLM은 시그널을 만들지 않는다.
  - LLM은 최근 헤드라인을 읽고 {감성점수, 확신도, 리스크 플래그}만 출력한다.
  - 뚜렷한 악재(감성 <= veto_sentiment && 확신도 >= min_confidence, 또는
    치명적 리스크 플래그)일 때만 그날 해당 종목 매수를 막는다.
  - LLM 호출 실패/키 없음 → 필터는 조용히 통과(open-fail). 봇은 LLM 없이도 완전 동작.

판정은 심볼당 하루 cache_hours 시간 캐시되어 API 비용을 제한한다.
"""

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path

import config
from news import fetch_headlines

KST = timezone(timedelta(hours=9))
CACHE_PATH = config.LOG_DIR / "news_verdicts.json"

# 러너가 등록하는 API 오류 콜백 (크레딧 소진 경보용). 없으면 무시.
on_api_error = None

# 이 플래그가 하나라도 뜨면 감성점수와 무관하게 매수 차단
CRITICAL_FLAGS = {"trading_halt", "delisting", "accounting_fraud", "embezzlement"}

SCHEMA = {
    "type": "object",
    "properties": {
        "sentiment": {
            "type": "number",
            "description": "-1(강한 악재) ~ 0(중립) ~ 1(강한 호재). 주가에 미칠 단기 영향 기준.",
        },
        "confidence": {
            "type": "number",
            "description": "판단 확신도 0~1. 헤드라인이 적거나 모호하면 낮게.",
        },
        "risk_flags": {
            "type": "array",
            "items": {
                "type": "string",
                "enum": ["trading_halt", "delisting", "accounting_fraud",
                         "embezzlement", "lawsuit", "regulatory", "dilution",
                         "management_risk", "earnings_shock", "none"],
            },
            "description": "헤드라인에서 확인되는 구조적 리스크. 없으면 [\"none\"].",
        },
        "summary": {
            "type": "string",
            "description": "판단 근거 한두 문장 (한국어).",
        },
    },
    "required": ["sentiment", "confidence", "risk_flags", "summary"],
    "additionalProperties": False,
}

SYSTEM = """너는 자동매매 봇의 뉴스 리스크 필터다. 매수 추천을 하는 것이 아니라,
규칙 기반 전략이 이미 낸 매수 시그널을 '오늘 뉴스 때문에 막아야 하는가'만 판단한다.

기준:
- sentiment는 헤드라인들이 향후 1~5거래일 주가에 미칠 영향의 방향과 강도다.
- 광고성/시황 나열/유튜브성 헤드라인은 무시하고, 사실 기반 뉴스에 가중치를 둔다.
- 거래정지·상장폐지·분식회계·횡령 정황은 감성과 무관하게 risk_flags에 반드시 표기한다.
- 헤드라인만으로 판단이 어려우면 confidence를 낮춰라. 과잉 확신은 금물이다."""


@dataclass
class Verdict:
    symbol: str
    sentiment: float
    confidence: float
    risk_flags: list
    summary: str
    headlines_used: int
    model: str
    checked_at: str
    headlines_hash: str = ""

    @property
    def blocks_buy(self) -> bool:
        if set(self.risk_flags) & CRITICAL_FLAGS:
            return True
        return (self.sentiment <= config.NEWS_FILTER["veto_sentiment"]
                and self.confidence >= config.NEWS_FILTER["min_confidence"])

    @property
    def urgent_exit(self) -> bool:
        """자동 청산 발동 기준 — 치명적 플래그(거래정지/상폐/분식/횡령)가 헤드라인
        3건 이상에서 확인될 때만. (감사 C6: 검증 없는 헤드라인이 시장가 청산을 직접
        발동하는 것은 과도한 권한 — 감성 악재는 alert_exit로 사람에게 알리기만 한다)"""
        return bool(set(self.risk_flags) & CRITICAL_FLAGS) and self.headlines_used >= 3

    @property
    def alert_exit(self) -> bool:
        """자동 청산은 아니지만 사람이 봐야 할 강한 감성 악재."""
        return (self.sentiment <= config.NEWS_MONITOR["exit_sentiment"]
                and self.confidence >= config.NEWS_MONITOR["exit_confidence"])

    def reason(self) -> str:
        flags = [f for f in self.risk_flags if f != "none"]
        tag = f" 플래그={flags}" if flags else ""
        return (f"뉴스 감성 {self.sentiment:+.2f} (확신도 {self.confidence:.2f}){tag}"
                f" — {self.summary}")


class NewsFilter:
    """사용법: NewsFilter().check('005930') → Verdict | None (None이면 판단 불가→통과)"""

    def __init__(self):
        self._client = None
        self._cache = self._load_cache()

    # ── 가용성 ──────────────────────────────────────────────
    @staticmethod
    def available() -> tuple[bool, str]:
        if not config.NEWS_FILTER["enabled"]:
            return False, "config.NEWS_FILTER['enabled']=False"
        config.load_env()
        if not os.getenv("ANTHROPIC_API_KEY"):
            return False, ".env에 ANTHROPIC_API_KEY 없음"
        try:
            import anthropic  # noqa: F401
        except ImportError:
            return False, "anthropic 패키지 미설치 (pip3 install anthropic)"
        return True, "OK"

    # ── 캐시 ────────────────────────────────────────────────
    def _load_cache(self) -> dict:
        if CACHE_PATH.exists():
            try:
                return json.loads(CACHE_PATH.read_text())
            except json.JSONDecodeError:
                pass
        return {}

    def _save_cache(self) -> None:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(json.dumps(self._cache, ensure_ascii=False, indent=2))

    def _cached(self, symbol: str) -> Verdict | None:
        d = self._cache.get(symbol)
        if not d:
            return None
        age = datetime.now(KST) - datetime.fromisoformat(d["checked_at"])
        if age > timedelta(hours=config.NEWS_FILTER["cache_hours"]):
            return None
        return Verdict(**d)

    # ── LLM 호출 ────────────────────────────────────────────
    def _ask_claude(self, symbol: str, name: str, headlines) -> Verdict | None:
        import anthropic

        if self._client is None:
            self._client = anthropic.Anthropic()

        lines = "\n".join(
            f"- [{h.published[:16]}] {h.title}" + (f" ({h.source})" if h.source else "")
            for h in headlines)
        prompt = (f"종목: {name}({symbol})\n최근 48시간 헤드라인 {len(headlines)}건:\n"
                  f"{lines}\n\n이 종목의 오늘 매수를 뉴스 리스크 관점에서 평가하라.")
        try:
            resp = self._client.messages.create(
                model=config.NEWS_FILTER["model"],
                max_tokens=2048,
                system=SYSTEM,
                output_config=config.output_config_for(
                    config.NEWS_FILTER["model"], "low", SCHEMA),
                messages=[{"role": "user", "content": prompt}],
            )
        except anthropic.APIStatusError as e:
            print(f"  [뉴스필터] API 오류({e.status_code}) → 필터 통과 처리")
            if on_api_error:
                try:
                    on_api_error(e)
                except Exception:               # noqa: BLE001
                    pass
            return None
        except anthropic.APIConnectionError:
            print("  [뉴스필터] 네트워크 오류 → 필터 통과 처리")
            return None

        if resp.stop_reason == "refusal":
            return None
        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return None
        return Verdict(
            symbol=symbol,
            sentiment=max(-1.0, min(1.0, float(data["sentiment"]))),
            confidence=max(0.0, min(1.0, float(data["confidence"]))),
            risk_flags=data.get("risk_flags", []),
            summary=str(data.get("summary", ""))[:300],
            headlines_used=len(headlines),
            model=config.NEWS_FILTER["model"],
            checked_at=datetime.now(KST).isoformat(timespec="seconds"),
        )

    # ── 공개 API ────────────────────────────────────────────
    @staticmethod
    def _hash(headlines) -> str:
        import hashlib
        return hashlib.sha1("|".join(h.title for h in headlines).encode()).hexdigest()[:16]

    def check(self, symbol: str, name: str | None = None,
              refresh: bool = False) -> Verdict | None:
        """refresh=True면 캐시가 있어도 헤드라인이 바뀌었을 때 재평가한다.
        (LLM 호출은 새 뉴스가 있을 때만 — 같은 헤드라인이면 캐시 반환)"""
        ok, why = self.available()
        if not ok:
            return None
        cached = self._cached(symbol)
        if cached and not refresh:
            return cached
        name = name or config.WHITELIST.get(symbol, symbol)
        try:
            headlines = fetch_headlines(f"{name} 주가")
        except Exception as e:                  # noqa: BLE001 - 뉴스 실패는 매매를 막지 않는다
            print(f"  [뉴스필터] 헤드라인 수집 실패({e}) → 필터 통과 처리")
            return cached
        if len(headlines) < 3:
            return cached                        # 표본 부족 → 기존 판정 유지
        h = self._hash(headlines)
        if cached and cached.headlines_hash == h:
            return cached                        # 새 뉴스 없음 → LLM 호출 생략
        verdict = self._ask_claude(symbol, name, headlines)
        if verdict:
            verdict.headlines_hash = h
            self._cache[symbol] = asdict(verdict)
            self._save_cache()
        return verdict or cached


if __name__ == "__main__":
    import sys
    sym = sys.argv[1] if len(sys.argv) > 1 else "005930"
    ok, why = NewsFilter.available()
    print(f"필터 가용: {ok} ({why})")
    f = NewsFilter()
    v = f.check(sym)
    if v is None:
        print("판정 없음 (필터 비활성/표본 부족/오류) → 매수 통과")
    else:
        print(f"매수 차단: {v.blocks_buy}")
        print(v.reason())
