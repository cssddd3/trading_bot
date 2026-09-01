"""설정 한 곳. 여기 숫자만 바꾸면 백테스트/드라이런 동작이 바뀐다.

⚠️ 4단계(실전 소액) 로 넘어가기 전에 RISK 값을 반드시 본인 기준으로 다시 잡을 것.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
DATA_DIR = ROOT / "data"


def load_env(path: Path = ROOT / ".env") -> None:
    """python-dotenv가 없어도 동작하도록 자체 파서 폴백을 둔다."""
    try:
        from dotenv import load_dotenv
        load_dotenv(path)
        return
    except ImportError:
        pass
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def credentials() -> tuple[str, str]:
    load_env()
    cid, secret = os.getenv("TOSS_CLIENT_ID", ""), os.getenv("TOSS_CLIENT_SECRET", "")
    if not cid or not secret:
        raise SystemExit(
            "[!] .env 에 TOSS_CLIENT_ID / TOSS_CLIENT_SECRET 을 설정하세요.")
    return cid, secret


# ── 매매 대상 (화이트리스트 밖 종목은 어떤 경우에도 주문하지 않는다) ──────
WHITELIST: dict[str, str] = {
    "005930": "삼성전자",
    "000660": "SK하이닉스",
    "069500": "KODEX 200",
    # 미국 (티커 = 영문): 소수점 매수가 가능해 소액 예산에도 매수 가능
    "AAPL": "애플",
    "NVDA": "엔비디아",
}

# 운용할 시장. 미국장은 KST 밤 22:30(서머타임)~익일 05:00에 돌아간다.
MARKETS: list[str] = ["KR", "US"]


def market_of(symbol: str) -> str:
    """국내 6자리 코드(숫자로 시작) vs 미국 티커(영문으로 시작)."""
    return "KR" if symbol[:1].isdigit() else "US"


# 미국 주식 비용 (수수료는 /commissions 로 실제값 확인 가능. 거래세 없음)
US_FEE_RATE = 0.001
US_SELL_TAX_RATE = 0.0

# ── 기본 전략 ─────────────────────────────────────────────────
# 2026-08-27 감사 후 전환: vb는 검증 게이트 기각(OOS -0.96%/건, MC 손실확률 100%),
# st는 통과(OOS 50건 +7.35%/건, MC 손실확률 0.7%). st는 저회전 추세추종 —
# 몇 주 보유가 정상이며 매매 빈도가 낮다 (월 수 회 수준).
DEFAULT_STRATEGY = "st"          # vb=변동성돌파, ma=EMA교차, st=Supertrend
STRATEGY_PARAMS: dict[str, dict] = {
    "vb": {"k": 0.5, "dynamic_k": True, "ma_filter": 5, "stop_loss_rate": 0.03},
    "ma": {"fast": 20, "slow": 60, "trend": 200, "atr_mult": 2.5},
    "st": {"atr_n": 10, "mult": 3.0, "trend": 200},
}

# 거래소측 '재해 백스톱' 손절 비율 (전략별). 전략 자체의 청산 로직(밴드/트레일링)보다
# 넓게 잡는다 — 봇이 죽었을 때만 의미 있는 최후의 방어선이지, 전략 손절이 아니다.
BACKSTOP_STOP_RATE: dict = {"vb": 0.03, "st": 0.08, "ma": 0.08}

# ── 자본/비용 ─────────────────────────────────────────────────
INITIAL_CASH = 1_000_000         # 백테스트 초기자본 (실전과 같은 규모로 잡을 것)
POSITION_PCT = 0.95              # 종목당 투입 비중 (현금 여유 5%)
FEE_RATE = 0.00015               # 토스 국내주식 수수료 (실제값은 /commissions 로 확인)
SELL_TAX_RATE = 0.0015           # 증권거래세 (매도 시)
SLIPPAGE_RATE = 0.001            # 슬리피지 가정


@dataclass
class RiskLimits:
    """드라이런에서도 그대로 적용해, 실전 전에 한도 로직을 먼저 검증한다."""
    max_order_amount: int = 200_000        # 1회 주문 금액 상한
    max_symbol_amount: int = 400_000       # 종목당 보유 금액 상한
    max_total_exposure: int = 1_000_000    # 전체 투자 금액 상한
    daily_loss_limit: int = 50_000         # 일일 실현손실 한도 (넘으면 당일 매수 중단)
    max_daily_orders: int = 10             # 일일 주문 횟수 상한
    reentry_cooldown_days: int = 1         # 손절 후 같은 종목 재진입 금지 일수
    block_warned_symbols: bool = True      # 투자경고/위험/정리매매/단기과열 종목 차단
    allow_symbols: dict[str, str] = field(default_factory=lambda: dict(WHITELIST))


RISK = RiskLimits()

# 한도를 예산에 연동 (2026-08-26 사용자 결정: "하이닉스급 고가주도 살 수 있게").
# True면 위 RiskLimits의 금액 한도들은 기동 시/예산 변경 시 예산 비례로 재계산된다:
#   1회 주문 = 종목당 = 그 시장 예산 전액 / 전체 = 시장 예산 합 / 일일손실 = 총예산의 10%
RISK_FROM_BUDGET = True
DAILY_LOSS_PCT = 0.10


def effective_budget(market: str) -> int:
    """실효 초기예산: 텔레그램 /budget 설정(장부 저장) > .env > 기본값.

    스카우트 등 러너 밖 코드도 /budget 변경을 보게 하기 위한 헬퍼 (감사 지적 반영).
    """
    import json as _json
    try:
        bb = _json.loads((LOG_DIR / "live_state.json").read_text()).get("budget_base", {})
    except (OSError, ValueError):
        bb = {}
    return int(bb.get(market) or LIVE_BUDGET[market])

# 실시간 시세 스트림 (웹소켓): 감시 종목 체결가를 초 단위로 수신.
# 돌파/손절 반응이 30초 폴링 → 수 초로 단축된다. 연결 실패 시 REST 폴백 (open-fail).
STREAM: dict = {
    "enabled": True,
    "fresh_secs": 10,        # 이 나이(초) 이내의 스트림 가격만 신뢰, 넘으면 REST
    "fast_interval": 8,      # 스트림 연결 중 장중 틱 간격(초) — 기본 30초 대신
}

# 웹 대시보드 (읽기 전용): 봇이 매 틱 스냅샷을 쓰고 내장 서버가 보여준다.
# 같은 컴퓨터: http://localhost:8787 / 같은 와이파이 폰: http://<맥IP>:8787
DASHBOARD: dict = {"enabled": True, "port": 8787}

# 헬스체크(하트비트): 봇이 살아있다는 신호를 주기적으로 텔레그램에 보낸다.
# 이 메시지가 제때 안 오면 봇이 죽었다는 뜻 (전원/네트워크/크래시).
HEARTBEAT: dict = {
    "enabled": True,
    "interval_minutes": 60,          # 하트비트 주기 (24시간 내내 — 안 오면 봇이 죽은 것)
}

# ── 실전 소액 모드 ─────────────────────────────────────────────
# 봇이 실주문에 쓸 수 있는 총 예산 상한. 계좌에 돈이 더 있어도 이 이상 절대 못 쓴다.
# 봇은 자기가 산 종목만 팔 수 있으므로, 기존 보유 주식은 어떤 경우에도 건드리지 않는다.
# 켜는 법: .env에 LIVE_TRADING=1 추가 + 실행 시 --live 플래그 (이중 잠금)
# 시장별 초기 예산 (원). 실제 예산은 여기에 그 시장에서의 누적 실현손익이 더해져
# 복리로 굴러간다 (KR 5만→10만이 되면 KR 예산도 10만). 미실현 이익은 미포함.
# 국내 계좌 예수금은 KR 예산으로, 해외 계좌 예수금(USD)은 US 예산으로만 쓰인다.
# 돈을 더 넣었으면 .env에 LIVE_BUDGET_KR / LIVE_BUDGET_US 를 적고 재시작하면 된다.
load_env()
LIVE_BUDGET: dict = {"KR": int(os.getenv("LIVE_BUDGET_KR", "50000")),
                     "US": int(os.getenv("LIVE_BUDGET_US", "50000"))}

# 종목 하나가 그 시장 예산에서 차지할 수 있는 최대 비중.
# 사용자 결정(2026-08-26): 시장별 예산만 분리하고, 그 안에서는 한 종목이
# 전부 써도 된다 → 둘 다 1.0. 분산을 강제하고 싶어지면 US를 0.5 등으로 낮출 것.
MAX_POSITION_BUDGET_PCT: dict = {"KR": 1.0, "US": 1.0}

# ── LLM 모델 (역할별 티어링, 2026-08-27 사용자 결정) ───────────────
# 스카우트(종목 판단)는 Sonnet, 뉴스필터/비서(감성 분류·요약)는 Haiku.
# 비용: Opus $5/$25 > Sonnet $2/$10 > Haiku $1/$5 (입력/출력 100만 토큰당).
# .env로 역할별 오버라이드 가능: SCOUT_LLM_MODEL / NEWS_LLM_MODEL / ASSISTANT_LLM_MODEL
LLM_MODELS: dict = {
    "scout": os.getenv("SCOUT_LLM_MODEL", "claude-sonnet-5"),
    "filter": os.getenv("NEWS_LLM_MODEL", "claude-haiku-4-5"),
    "assistant": os.getenv("ASSISTANT_LLM_MODEL", "claude-haiku-4-5"),
}


def output_config_for(model: str, effort: str, schema: dict | None = None) -> dict | None:
    """모델별 output_config 구성. effort 파라미터는 Haiku 4.5 등 구모델에서 400 에러
    → 지원 모델(sonnet-5/opus/fable)에만 넣는다."""
    oc: dict = {}
    if schema:
        oc["format"] = {"type": "json_schema", "schema": schema}
    if not any(w in model for w in ("haiku", "sonnet-4-5")):
        oc["effort"] = effort
    return oc or None


# ── LLM 뉴스 필터 (선택 기능) ──────────────────────────────────
# .env에 ANTHROPIC_API_KEY가 있고 anthropic 패키지가 설치돼 있으면 동작.
# 없으면 자동으로 꺼지고 봇은 규칙 기반으로만 돌아간다 (open-fail).
# LLM은 매수 시그널을 만들지 않는다 — 악재 뉴스일 때 매수를 막는 거부권만 가진다.
NEWS_FILTER: dict = {
    "enabled": True,                 # False면 키가 있어도 완전히 끔
    "model": LLM_MODELS["filter"],
    "veto_sentiment": -0.4,          # 감성 <= 이 값이고
    "min_confidence": 0.6,           #   확신도 >= 이 값이면 그날 매수 차단
    "cache_hours": 3,                # 심볼당 판정 캐시 (장중 반전 대응 — 감사 반영)
}

# LLM 종목 스카우트: 규칙 후보군(거래대금 상위+필터) 안에서 LLM이 관심종목을 고른다.
# 뽑힌 종목은 그날 화이트리스트에 '추가'될 뿐, 매수는 전략+안전장치를 그대로 통과해야 한다.
SCOUT: dict = {
    "use_watchlist": True,           # 드라이런이 오늘자 watchlist.json을 화이트리스트에 추가
    "auto_run_premarket": True,      # --watch 중 장전(PRE)에 오늘자 워치리스트 없으면 자동 생성
    "refresh_minutes": 3,            # 장중 후보군 확인 주기 (0이면 장전 1회만).
                                     # 3분마다 랭킹만 싸게 확인하고(1콜), 새 후보가 등장했을 때만
                                     # LLM 스카우트를 실행한다 ("이상 없으면 그대로 간다").
                                     # 새 종목은 추가만 되고, 기존 종목은 그날 계속 감시한다
    "candidate_rank_type": "MARKET_TRADING_AMOUNT",
    "candidate_count": 100,          # 랭킹에서 가져올 수 (1콜은 동일 — 넓은 후보 풀)
    "trigger_count": 30,             # '새 후보 감지' 트리거는 상위 30만 본다
                                     # (31~100위는 순위 순환이 잦아 트리거로 쓰면 LLM 폭주)
    "max_candidates": 20,            # 규칙 필터 후 LLM에 보여줄 최대 후보
    "max_picks": 3,                  # LLM이 고를 최대 종목 수 (시장별)
    "llm_min_interval_minutes": 15,  # 풀 스카우트(LLM 호출) 최소 간격 — 후보확인(REST)은
                                     # 3분마다 하되, LLM 재평가는 시장당 15분에 1번까지만.
                                     # (8/27 실측: 스로틀 없이는 랭킹 순환 때문에 하루 150회+
                                     # 호출 — 새 얼굴은 모아뒀다가 다음 실행 때 함께 평가된다)
}

# 전환 스캐너 — 그림자(관찰) 모드 (2026-08-31): 매일 KR 마감 후 거래대금 상위
# top_n 종목을 스캔해 '오늘 Supertrend 상승 전환'을 기록+알림만 한다. 매수 없음.
# 5개월 백테스트는 기각(레짐 나쁨) — 장기 검증 통과 전까지 관찰 전용.
# 2026-08-31 자동매수 승격 (사용자 지시 + 분산 조건 재검증 통과): 4년 OOS +3.00%/건,
# 포지션당 예산 1/2 분할 가정 MC 손실확률 13.6%(<30% ✅). 단 '분할'이 승격 조건이므로
# 스캐너 매수는 position_frac을 반드시 유지할 것 (전액 몰빵이면 손실확률 31%로 기각 조건).
SCANNER: dict = {
    "enabled": True,
    "top_n": 100,                    # 매일 스캔할 거래대금 상위 수
    "shadow_csv": "scanner_shadow.csv",
    "auto_buy": True,                # 전환 감지 → 다음날 시가 매수 예약
    "position_frac": 0.5,            # 스캐너 매수 1건당 시장 예산의 최대 비중 (승격 조건!)
    "max_daily_picks": 2,            # 하루 최대 예약 수
}

# 장중 실시간 뉴스 모니터: 새 헤드라인이 감지되면 보유 종목 재평가.
# 치명적 악재(거래정지/분식회계 등) 또는 강한 악재면 가상 청산까지 수행.
NEWS_MONITOR: dict = {
    "enabled": True,
    "poll_minutes": 10,              # 헤드라인 확인 주기 (LLM 호출은 새 뉴스 있을 때만)
    "exit_sentiment": -0.7,          # 감성 <= 이 값 &
    "exit_confidence": 0.7,          #   확신도 >= 이 값이면 보유분 청산
}
