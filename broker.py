"""실전 주문 브로커 — 실주문이 나가는 유일한 통로.

CLAUDE.md 규칙: 주문 코드는 안전장치와 한 몸이다.
  - 모든 주문은 이 클래스를 거치고, 여기서 RiskGuard 최종 검사를 한 번 더 한다
    (호출부가 이미 검사했더라도 — 이중 방어).
  - 매수는 '지정가(현재가+0.3%)'로 슬리피지를 상한하고, 미체결분은 취소한다.
  - 매도(손절 포함)는 반드시 나가야 하므로: 지정가(현재가-0.5%) 시도 후
    미체결 시 취소하고 시장가로 1회 재시도한다.
  - 킬 스위치(halted)가 켜져 있으면 매수는 전면 거부된다 (매도는 허용 — 탈출은 항상 가능).
"""

import time
import uuid

import config
from backtest.engine import round_to_tick
from risk import RiskGuard
from toss.client import TossApiError, TossClient

FILL_WAIT_SEC = 12          # 지정가 체결 대기
POLL_INTERVAL = 1.5


class BrokerError(Exception):
    pass


class LiveBroker:
    def __init__(self, client: TossClient, guard: RiskGuard):
        self.client = client
        self.guard = guard
        accounts = client.get_accounts()
        if not accounts:
            raise BrokerError("토스 계좌를 찾을 수 없습니다 (WTS에서 계좌 확인)")
        self.account_seq = str(accounts[0]["accountSeq"])
        self.account_no = accounts[0].get("accountNo", "?")
        self._fx, self._fx_at = 0.0, 0.0

    # ── 조회 ────────────────────────────────────────────────
    def buying_power(self, currency: str = "KRW") -> float:
        r = self.client.get_buying_power(self.account_seq, currency)
        return float(r.get("cashBuyingPower", 0))

    def sellable(self, symbol: str) -> float:
        return self.client.get_sellable_quantity(self.account_seq, symbol)

    def fx(self) -> float:
        """USD→KRW 환율 (5분 캐시). 0/비정상 값은 채택하지 않는다 (감사: fx=0이면 한도검사 무력화)."""
        if time.time() - self._fx_at > 300:
            try:
                rate = self.client.get_exchange_rate()
                if rate > 500:                 # sanity: 원달러가 500 밑일 수 없다
                    self._fx = rate
                    self._fx_at = time.time()
            except TossApiError:
                pass
        if self._fx <= 500:
            raise BrokerError("환율 조회 불가 — US 주문 보류 (한도검사 무력화 방지)")
        return self._fx

    # ── 내부: 주문 1건 실행 + 체결 대기 ─────────────────────────
    def _execute(self, symbol: str, side: str, qty: float | None,
                 limit_price: float | None,
                 order_amount: float | None = None) -> dict:
        """주문 → 체결 폴링 → 미체결분 취소. 반환: {filled, avg_price, order_id}"""
        order_type = "LIMIT" if limit_price else "MARKET"
        order = self.client.place_order(
            self.account_seq, symbol, side, order_type, qty,
            price=limit_price, order_amount=order_amount,
            client_order_id=f"tt-{uuid.uuid4().hex[:20]}")
        order_id = order.get("orderId", "")

        deadline = time.time() + FILL_WAIT_SEC
        status, filled, avg = "", 0.0, 0.0
        while time.time() < deadline:
            time.sleep(POLL_INTERVAL)
            try:
                o = self.client.get_order(self.account_seq, order_id)
            except TossApiError:
                continue
            status = o.get("status", "")
            ex = o.get("execution") or {}
            filled = float(ex.get("filledQuantity") or 0)
            avg = float(ex.get("averageFilledPrice") or 0)
            if status in ("FILLED", "CANCELED", "REJECTED"):
                break

        if status not in ("FILLED", "CANCELED", "REJECTED"):
            # 미체결/부분체결 → 잔량 취소. 취소-체결 race 대비: 취소 후 최종 상태를
            # 최대 3회 재확인해 '취소 요청 직전에 체결된 수량'을 놓치지 않는다 (감사 H5).
            try:
                self.client.cancel_order(self.account_seq, order_id)
            except TossApiError as e:
                print(f"  [주문] 취소 실패({e}) — 최종 상태 재확인으로 판별")
            for _ in range(3):
                time.sleep(POLL_INTERVAL)
                try:
                    o = self.client.get_order(self.account_seq, order_id)
                    status = o.get("status", status)
                    ex = o.get("execution") or {}
                    filled = float(ex.get("filledQuantity") or 0)
                    avg = float(ex.get("averageFilledPrice") or 0)
                    if status in ("FILLED", "CANCELED", "REJECTED"):
                        break
                except TossApiError:
                    continue
            else:
                # 상태를 확정하지 못함 — 살아있는 주문일 수 있다. 호출부가 대사(reconcile)로 잡도록 표식
                status = "UNKNOWN"
        return {"filled": filled, "avg_price": avg, "order_id": order_id,
                "status": status}

    # ── 거래소측 손절 (조건부주문 — 봇이 죽어도 토스 서버에서 발동) ──

    def set_stop(self, symbol: str, quantity: float, trigger_price: float) -> str | None:
        """손절 조건주문 등록. 반환: conditionalOrderId (실패 시 None — 봇 내 스탑이 백업).

        소수점 수량(미국 금액매수 취득분)은 조건주문이 거부되므로 시도하지 않는다 —
        해당 포지션은 봇 내 스탑 + launchd 자동재시작 + 당일 청산 규칙으로 보호.
        """
        if float(quantity) != int(float(quantity)):
            return None
        from datetime import datetime, timedelta, timezone
        expire = (datetime.now(timezone(timedelta(hours=9))) + timedelta(days=30)).date().isoformat()
        try:
            r = self.client.place_stop_loss(self.account_seq, symbol, quantity,
                                            trigger_price, expire)
            return r.get("conditionalOrderId") or r.get("id")
        except TossApiError as e:
            print(f"  [스탑] 거래소측 손절 등록 실패({symbol}): {e} — 봇 내 스탑만 유지")
            return None

    def cancel_stops_for(self, symbol: str) -> None:
        """해당 종목의 조건주문 전부 취소 — 매도 직전 반드시 호출 (이중 매도 방지)."""
        try:
            for c in self.client.list_conditional_orders(self.account_seq):
                if c.get("symbol") == symbol:
                    cid = c.get("conditionalOrderId") or c.get("id")
                    if cid:
                        self.client.cancel_conditional_order(self.account_seq, cid)
        except TossApiError as e:
            print(f"  [스탑] 조건주문 취소 실패({symbol}): {e}")

    def active_stop_symbols(self) -> set:
        """활성 조건주문이 걸린 심볼 집합 (대사·재등록 판단용)."""
        try:
            return {c.get("symbol") for c in
                    self.client.list_conditional_orders(self.account_seq)}
        except TossApiError:
            return set()

    def list_stops_for(self, symbol: str) -> list[tuple[str, float]]:
        """해당 종목의 활성 조건주문 [(id, trigger)] — 중복/트리거 불일치 정리용."""
        out = []
        try:
            for c in self.client.list_conditional_orders(self.account_seq):
                if c.get("symbol") != symbol:
                    continue
                cid = c.get("conditionalOrderId") or c.get("id")
                trig = float((c.get("first") or {}).get("triggerPrice") or 0)
                if cid:
                    out.append((cid, trig))
        except TossApiError:
            pass
        return out

    def sell_at_close(self, symbol: str, qty: float, deadline_ts: float) -> dict | None:
        """국내 동시호가(15:20~15:30) 전용 청산 — 주문을 내고 15:30 매칭까지 취소 없이 대기.

        감사 C4 대응: 기존 12초 자가취소는 단일가 메커니즘과 양립 불가였다.
        deadline_ts: 이 시각(epoch, 대략 15:31)까지 폴링하며 기다린다.
        """
        avail = self.sellable(symbol)
        qty = min(int(qty), int(avail))
        ok, why = self.guard.check_sell(symbol, qty, avail)
        if not ok:
            print(f"  [종가청산 거부] {symbol} {why}")
            return None
        self.cancel_stops_for(symbol)          # 스탑과 이중 매도 방지
        order = self.client.place_order(
            self.account_seq, symbol, "SELL", "MARKET", qty,
            client_order_id=f"tt-cls-{uuid.uuid4().hex[:16]}")
        order_id = order.get("orderId", "")
        while time.time() < deadline_ts:
            time.sleep(3)
            try:
                o = self.client.get_order(self.account_seq, order_id)
            except TossApiError:
                continue
            ex = o.get("execution") or {}
            filled = float(ex.get("filledQuantity") or 0)
            if o.get("status") == "FILLED":
                self.guard.record_order()
                return {"filled": filled,
                        "avg_price": float(ex.get("averageFilledPrice") or 0),
                        "order_id": order_id, "status": "FILLED"}
        # 마감까지 미체결 — 취소 후 결과 반환 (부분체결 반영)
        r = self._execute_status_after_cancel(order_id)
        if r["filled"] > 0:
            self.guard.record_order()
        return r if r["filled"] > 0 else None

    def _execute_status_after_cancel(self, order_id: str) -> dict:
        try:
            self.client.cancel_order(self.account_seq, order_id)
        except TossApiError:
            pass
        time.sleep(POLL_INTERVAL)
        try:
            o = self.client.get_order(self.account_seq, order_id)
            ex = o.get("execution") or {}
            return {"filled": float(ex.get("filledQuantity") or 0),
                    "avg_price": float(ex.get("averageFilledPrice") or 0),
                    "order_id": order_id, "status": o.get("status", "UNKNOWN")}
        except TossApiError:
            return {"filled": 0.0, "avg_price": 0.0, "order_id": order_id,
                    "status": "UNKNOWN"}

    # ── 공개: 매수/매도 ──────────────────────────────────────
    def buy(self, symbol: str, qty: float, ref_price: float,
            holdings_value: float = 0.0, total_exposure: float = 0.0,
            halted: bool = False, amount_usd: float | None = None) -> dict | None:
        """검증 → 매수. 체결 없으면 None.

        KR: 지정가(현재가+0.3%) qty주. / US: 금액 기반 시장가(orderAmount, 소수점 취득).
        holdings_value/total_exposure/한도 비교는 전부 KRW 기준.
        """
        if halted:
            print("  [실주문 거부] 킬 스위치(halted) 상태 — 매수 금지")
            return None
        market = config.market_of(symbol)

        if market == "US":
            if not amount_usd or amount_usd <= 0:
                return None
            krw_amount = amount_usd * self.fx()
            ok, why = self.guard.check_buy(
                symbol, krw_amount, 1,          # 금액 기반: KRW 환산액으로 한도 검사
                holdings_value=holdings_value, total_exposure=total_exposure)
            if not ok:
                print(f"  [실주문 거부] {symbol} {why}")
                return None
            power = self.buying_power("USD")
            if amount_usd > power:
                print(f"  [실주문 거부] USD 매수가능금액 부족 (${power:,.2f})")
                return None
            r = self._execute(symbol, "BUY", None, None, order_amount=amount_usd)
        else:
            limit_price = round_to_tick(ref_price * 1.003)
            ok, why = self.guard.check_buy(
                symbol, limit_price, int(qty),
                holdings_value=holdings_value, total_exposure=total_exposure)
            if not ok:
                print(f"  [실주문 거부] {symbol} {why}")
                return None
            power = self.buying_power("KRW")
            if limit_price * qty > power:
                print(f"  [실주문 거부] 매수가능금액 부족 ({power:,.0f}원)")
                return None
            r = self._execute(symbol, "BUY", int(qty), limit_price)

        if r["filled"] > 0:
            self.guard.record_order()
        return r if r["filled"] > 0 else None

    def sell(self, symbol: str, qty: float, ref_price: float) -> dict | None:
        """매도 — 반드시 나가야 한다 (손절 포함).

        KR: 지정가(-0.5%) → 미체결 시 시장가 재시도.
        US: 곧바로 시장가 (소수점 수량은 시장가 매도만 허용되는 스펙).
        """
        avail = self.sellable(symbol)
        qty = min(qty, avail)
        ok, why = self.guard.check_sell(symbol, qty, avail)
        if not ok:
            print(f"  [실주문 거부] {symbol} {why}")
            return None
        market = config.market_of(symbol)
        self.cancel_stops_for(symbol)          # 거래소측 스탑과 이중 매도 방지

        if market == "US":
            r = self._execute(symbol, "SELL", qty, None)          # MARKET
        else:
            qty = int(qty)
            r = self._execute(symbol, "SELL", qty, round_to_tick(ref_price * 0.995))
            remaining = qty - r["filled"]
            if remaining > 0:
                print(f"  [매도 미체결 {remaining:g}주] 시장가 재시도")
                r2 = self._execute(symbol, "SELL", remaining, None)   # MARKET
                total = r["filled"] + r2["filled"]
                if total > 0:
                    avg = ((r["avg_price"] * r["filled"] + r2["avg_price"] * r2["filled"])
                           / total)
                    r = {"filled": total, "avg_price": avg,
                         "order_id": r["order_id"], "status": "FILLED"}
        if r["filled"] > 0:
            self.guard.record_order()
        return r if r["filled"] > 0 else None
