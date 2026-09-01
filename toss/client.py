"""토스증권 Open API 읽기 전용 클라이언트.

시세/캔들/계좌/잔고/종목정보/장운영시간 조회만 포함한다. 주문 API는 의도적으로 없다.
(전략 검증이 끝나기 전에는 주문 코드를 아예 두지 않는 것이 소액 운용의 안전장치다.)
"""

import time

import requests

from .auth import BASE_URL, get_access_token


class TossApiError(Exception):
    pass


class TossClient:
    def __init__(self, client_id: str, client_secret: str):
        self._client_id = client_id
        self._client_secret = client_secret

    # ── 내부 공통 ──────────────────────────────────────────────

    def _headers(self, account_seq: str | None = None) -> dict:
        token = get_access_token(self._client_id, self._client_secret)
        headers = {"Authorization": f"Bearer {token}"}
        if account_seq:
            headers["X-Tossinvest-Account"] = str(account_seq)
        return headers

    def _get(self, path: str, params: dict | None = None,
             account_seq: str | None = None, _retries: int = 3):
        """GET 요청. 429(호출 한도)면 Retry-After만큼 기다렸다 재시도."""
        for attempt in range(_retries):
            resp = requests.get(
                f"{BASE_URL}{path}",
                params=params,
                headers=self._headers(account_seq),
                timeout=10,
            )
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 1)) + 0.5
                time.sleep(wait)
                continue
            if resp.status_code != 200:
                raise TossApiError(
                    f"GET {path} 실패 (HTTP {resp.status_code}): {resp.text[:300]}"
                )
            return resp.json()
        raise TossApiError(f"GET {path}: 호출 한도 초과가 계속됨 (429)")

    # ── 시세 (토큰만 필요) ─────────────────────────────────────

    def get_prices(self, symbols: list[str]) -> list[dict]:
        """현재가 조회. 국내는 6자리 코드('005930'), 미국은 티커('AAPL'). 최대 200개."""
        data = self._get("/api/v1/prices", params={"symbols": ",".join(symbols)})
        return data.get("result", [])

    def get_candles(self, symbol: str, interval: str = "1d", count: int = 100,
                    before: str | None = None, adjusted: bool = True) -> dict:
        """캔들 한 페이지 조회 (최신 → 과거 순, 최대 200개).

        before: ISO8601. 이 시각 이전 봉만 반환 — 직전 응답의 nextBefore 를 그대로 넘긴다.
        adjusted: 수정주가 적용 (백테스트는 반드시 True).
        반환: {"candles": [...], "nextBefore": "..." | None}
        """
        params = {"symbol": symbol, "interval": interval, "count": count,
                  "adjusted": str(adjusted).lower()}
        if before:
            params["before"] = before
        return self._get("/api/v1/candles", params=params).get("result", {})

    # ── 계좌 (토큰 + 계좌 헤더) ────────────────────────────────

    def get_accounts(self) -> list[dict]:
        """내 계좌 목록."""
        data = self._get("/api/v1/accounts")
        return data.get("result", [])

    def get_holdings(self, account_seq: str, symbol: str | None = None) -> dict:
        """보유 주식(잔고). account_seq는 get_accounts() 결과의 accountSeq."""
        params = {"symbol": symbol} if symbol else None
        data = self._get("/api/v1/holdings", params=params, account_seq=account_seq)
        return data.get("result", {})

    def get_buying_power(self, account_seq: str, currency: str = "KRW") -> dict:
        """매수 가능 금액 (미수 제외 현금 기준)."""
        data = self._get("/api/v1/buying-power", params={"currency": currency},
                         account_seq=account_seq)
        return data.get("result", {})

    def get_commissions(self, account_seq: str) -> list[dict]:
        """내 계좌의 시장별 수수료율. 백테스트 비용 모델을 실제값에 맞출 때 쓴다."""
        data = self._get("/api/v1/commissions", account_seq=account_seq)
        return data.get("result", [])

    # ── 주문 (실전 모드 전용 — 반드시 broker.py의 RiskGuard 검사를 거쳐 호출) ──

    def _post(self, path: str, body: dict, account_seq: str,
              _retries: int = 3) -> dict:
        """주문 계열 POST. 429는 Retry-After 대기 후 재시도 (감사 지적 반영)."""
        for _ in range(_retries):
            resp = requests.post(f"{BASE_URL}{path}", json=body,
                                 headers=self._headers(account_seq), timeout=10)
            if resp.status_code == 429:
                time.sleep(int(resp.headers.get("Retry-After", 1)) + 0.5)
                continue
            if resp.status_code != 200:
                raise TossApiError(
                    f"POST {path} 실패 (HTTP {resp.status_code}): {resp.text[:300]}")
            return resp.json().get("result", {})
        raise TossApiError(f"POST {path}: 429 한도 초과 지속")

    def place_order(self, account_seq: str, symbol: str, side: str,
                    order_type: str, quantity: float | None = None,
                    price: float | None = None,
                    order_amount: float | None = None,
                    client_order_id: str | None = None) -> dict:
        """주문 생성. side: BUY|SELL, order_type: LIMIT|MARKET.

        - quantity: 수량 기반 주문. 국내는 정수만, 미국 시장가 매도는 소수점 허용.
        - order_amount: 금액 기반 주문 (미국 시장가 매수 — 소수점 주식 취득).
        ⚠️ 이 메서드를 broker.LiveBroker 밖에서 직접 호출하지 말 것.
        (한도/화이트리스트 검사를 우회하게 된다 — CLAUDE.md 규칙)
        """
        body: dict = {"symbol": symbol, "side": side, "orderType": order_type}
        if order_amount is not None:
            body["orderAmount"] = f"{order_amount:.2f}"
        elif quantity is not None:
            q = float(quantity)
            body["quantity"] = str(int(q)) if q == int(q) else f"{q:.6f}".rstrip("0")
        else:
            raise ValueError("quantity 또는 order_amount 중 하나가 필요합니다")
        if order_type == "LIMIT":
            if price is None:
                raise ValueError("LIMIT 주문에는 price가 필요합니다")
            body["price"] = (str(int(price)) if symbol[:1].isdigit()
                             else f"{price:.2f}")
        if client_order_id:
            body["clientOrderId"] = client_order_id
        return self._post("/api/v1/orders", body, account_seq)

    # ── 조건부 주문 (거래소측 손절 — 봇 프로세스가 죽어도 살아있는 보호장치) ──

    def place_stop_loss(self, account_seq: str, symbol: str, quantity: float,
                        trigger_price: float, expire_date: str) -> dict:
        """SINGLE 조건주문: 가격이 trigger 이하로 내려오면 시장가 매도.

        감사 C5 대응 — 스탑이 봇 메모리가 아니라 토스 서버에 등록된다.
        """
        q = float(quantity)
        body = {
            "symbol": symbol, "type": "SINGLE", "orderType": "MARKET",
            "quantity": str(int(q)) if q == int(q) else f"{q:.6f}".rstrip("0"),
            "expireDate": expire_date,
            "first": {"orderSide": "SELL",
                      "triggerPrice": (str(int(trigger_price)) if symbol[:1].isdigit()
                                       else f"{trigger_price:.2f}")},
        }
        return self._post("/api/v1/conditional-orders", body, account_seq)

    def list_conditional_orders(self, account_seq: str,
                                status: str = "OPEN") -> list[dict]:
        """진행 중(OPEN) 조건주문 목록. status는 필수 파라미터 (OPEN|CLOSED)."""
        data = self._get("/api/v1/conditional-orders",
                         params={"status": status, "limit": 100},
                         account_seq=account_seq)
        result = data.get("result", [])
        return result if isinstance(result, list) else result.get("conditionalOrders", [])

    def cancel_conditional_order(self, account_seq: str, cond_id: str) -> dict:
        resp = requests.delete(
            f"{BASE_URL}/api/v1/conditional-orders/{cond_id}",
            headers=self._headers(account_seq), timeout=10)
        if resp.status_code != 200:
            raise TossApiError(
                f"조건주문 취소 실패 (HTTP {resp.status_code}): {resp.text[:300]}")
        return resp.json().get("result", {})

    def get_order(self, account_seq: str, order_id: str) -> dict:
        """주문 상세 (status: SUBMITTED|PARTIAL_FILLED|FILLED|CANCELED..., execution 포함)."""
        data = self._get(f"/api/v1/orders/{order_id}", account_seq=account_seq)
        return data.get("result", {})

    def cancel_order(self, account_seq: str, order_id: str) -> dict:
        return self._post(f"/api/v1/orders/{order_id}/cancel", {}, account_seq)

    def get_sellable_quantity(self, account_seq: str, symbol: str) -> float:
        data = self._get("/api/v1/sellable-quantity", params={"symbol": symbol},
                         account_seq=account_seq)
        return float(data.get("result", {}).get("sellableQuantity", 0))

    # ── 종목 참조 정보 (안전장치용) ────────────────────────────

    def get_stocks(self, symbols: list[str]) -> list[dict]:
        """종목 기본정보. 상장 상태(status)·거래정지 여부 확인용. 최대 200개."""
        data = self._get("/api/v1/stocks", params={"symbols": ",".join(symbols)})
        return data.get("result", [])

    def get_warnings(self, symbol: str) -> list[dict]:
        """매수 유의사항 (정리매매/단기과열/투자경고·위험/VI). 비어 있으면 정상."""
        data = self._get(f"/api/v1/stocks/{symbol}/warnings")
        return data.get("result", [])

    def get_rankings(self, type: str = "MARKET_TRADING_AMOUNT",
                     market_country: str = "KR", duration: str = "1d",
                     count: int = 30, exclude_caution: bool = True) -> list[dict]:
        """주식 랭킹 (상위 100까지). 종목 스카우트의 후보군 소스.

        type: MARKET_TRADING_AMOUNT | MARKET_TRADING_VOLUME | TOP_GAINERS | TOP_LOSERS ...
        duration: realtime | 1d | 1w | 1mo ... (TOP_GAINERS/LOSERS는 realtime 미지원)
        """
        data = self._get("/api/v1/rankings", params={
            "type": type, "marketCountry": market_country, "duration": duration,
            "count": count, "excludeInvestmentCaution": str(exclude_caution).lower()})
        return data.get("result", {}).get("rankings", [])

    def get_exchange_rate(self) -> float:
        """USD→KRW 환율 (토스 고시 환율)."""
        data = self._get("/api/v1/exchange-rate",
                         params={"baseCurrency": "USD", "quoteCurrency": "KRW"})
        return float(data.get("result", {}).get("rate", 0))

    def get_market_calendar(self, country: str = "KR", date: str | None = None) -> dict:
        """장 운영 시간 (전일/당일/익일). 드라이런이 장중인지 판단할 때 쓴다."""
        params = {"date": date} if date else None
        data = self._get(f"/api/v1/market-calendar/{country}", params=params)
        return data.get("result", {})
