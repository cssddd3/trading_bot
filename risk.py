"""안전장치. 주문을 내기 전에 반드시 통과해야 하는 검사들.

지금은 드라이런(가상 주문)에만 걸려 있지만, 실주문 코드를 붙일 때
`RiskGuard.check()` 를 통과하지 못한 주문은 절대 API로 보내지 않는다는 것이
이 프로젝트의 규칙이다 (CLAUDE.md 참조).
"""

import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from config import LOG_DIR, RiskLimits

KST = __import__("datetime").timezone(timedelta(hours=9))


def risk_day() -> str:
    """리스크 카운터의 '하루' 경계 = KST 06:00 (미장 마감 05:00 직후).

    자정 기준이면 미장 세션(22:30~05:00) 한복판에서 일일 손실한도·주문횟수가
    리셋되는 구멍이 생긴다 (감사 H2). 06시 경계면 국장+그날 밤 미장이 한 '거래일'로 묶인다.
    """
    return (datetime.now(KST) - timedelta(hours=6)).date().isoformat()

BLOCKING_WARNINGS = {
    "LIQUIDATION_TRADING",   # 정리매매
    "OVERHEATED",            # 단기과열
    "INVESTMENT_WARNING",    # 투자경고
    "INVESTMENT_RISK",       # 투자위험
}


@dataclass
class DailyState:
    day: str = ""
    orders: int = 0
    realized_pnl: int = 0
    cooldown_until: dict[str, str] = field(default_factory=dict)  # symbol -> YYYY-MM-DD

    def roll(self, today: str) -> None:
        """날짜가 바뀌면 일일 카운터만 초기화한다 (쿨다운은 유지)."""
        if self.day != today:
            self.day, self.orders, self.realized_pnl = today, 0, 0


class RiskGuard:
    def __init__(self, limits: RiskLimits, state_path: Path | None = None):
        self.limits = limits
        self.path = state_path or (LOG_DIR / "risk_state.json")
        self.state = self._load()
        self.state.roll(risk_day())

    # ── 상태 저장 ────────────────────────────────────────────
    def _load(self) -> DailyState:
        if self.path.exists():
            try:
                return DailyState(**json.loads(self.path.read_text()))
            except (json.JSONDecodeError, TypeError):
                pass
        return DailyState(day=risk_day())

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(asdict(self.state), ensure_ascii=False, indent=2))

    # ── 검사 ────────────────────────────────────────────────
    def check_buy(self, symbol: str, price: float, quantity: int,
                  holdings_value: float = 0.0, total_exposure: float = 0.0,
                  warnings: list[dict] | None = None,
                  stock_info: dict | None = None) -> tuple[bool, str]:
        L = self.limits
        amount = price * quantity
        today = risk_day()
        self.state.roll(today)

        if symbol not in L.allow_symbols:
            return False, f"화이트리스트에 없는 종목({symbol})"
        if quantity <= 0:
            return False, (f"주문 가능 수량 0 — 1회 주문 한도({L.max_order_amount:,}원) "
                           f"또는 가상 현금으로 현재가 {price:,.0f}원 1주를 살 수 없음")
        if amount > L.max_order_amount:
            return False, f"1회 주문 한도 초과 {amount:,.0f} > {L.max_order_amount:,}"
        if holdings_value + amount > L.max_symbol_amount:
            return False, (f"종목당 보유 한도 초과 "
                           f"{holdings_value + amount:,.0f} > {L.max_symbol_amount:,}")
        if total_exposure + amount > L.max_total_exposure:
            return False, (f"전체 투자 한도 초과 "
                           f"{total_exposure + amount:,.0f} > {L.max_total_exposure:,}")
        if self.state.realized_pnl <= -L.daily_loss_limit:
            return False, (f"일일 손실 한도 도달 ({self.state.realized_pnl:,}원) "
                           f"— 당일 신규 매수 중단")
        if self.state.orders >= L.max_daily_orders:
            return False, f"일일 주문 횟수 한도 {L.max_daily_orders}회 도달"
        until = self.state.cooldown_until.get(symbol)
        if until and today <= until:
            return False, f"손절 쿨다운 중 ({until} 까지)"
        if stock_info:
            if stock_info.get("status") not in (None, "ACTIVE"):
                return False, f"거래 불가 상태({stock_info.get('status')})"
            kr = stock_info.get("koreanMarketDetail") or {}
            if kr.get("krxTradingSuspended") or kr.get("liquidationTrading"):
                return False, "거래정지/정리매매 종목"
        if L.block_warned_symbols and warnings:
            hit = [w.get("warningType") for w in warnings
                   if w.get("warningType") in BLOCKING_WARNINGS]
            if hit:
                return False, f"매수 유의 종목 {hit}"
        return True, "OK"

    def check_sell(self, symbol: str, quantity: int, held: int) -> tuple[bool, str]:
        if quantity <= 0:
            return False, "수량 0"
        if quantity > held:
            return False, f"보유 수량 초과 (보유 {held}주)"
        return True, "OK"

    # ── 기록 ────────────────────────────────────────────────
    def record_order(self) -> None:
        self.state.roll(risk_day())
        self.state.orders += 1
        self.save()

    def record_close(self, symbol: str, pnl: float, was_stop_loss: bool = False) -> None:
        self.state.roll(risk_day())
        self.state.realized_pnl += int(pnl)
        if was_stop_loss and self.limits.reentry_cooldown_days > 0:
            until = date.today() + timedelta(days=self.limits.reentry_cooldown_days)
            self.state.cooldown_until[symbol] = until.isoformat()
        self.save()

    def summary(self) -> str:
        self.state.roll(risk_day())   # 며칠씩 켜둬도 날짜/카운터 최신 유지
        s, L = self.state, self.limits
        return (f"[리스크] {s.day} 주문 {s.orders}/{L.max_daily_orders}회 · "
                f"실현손익 {s.realized_pnl:,}원 (한도 -{L.daily_loss_limit:,}) · "
                f"쿨다운 {len(s.cooldown_until)}종목")
