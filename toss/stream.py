"""실시간 체결 스트림 (토스 웹소켓) — 시세 반응을 30초 폴링에서 초 단위로.

- 감시 종목의 체결가를 실시간 수신해 메모리에 캐시한다. 러너의 last_price()가
  이 캐시를 우선 사용하고, 낡았거나 연결이 죽었으면 REST로 폴백한다 (open-fail).
- 시세 채널은 LOSSY(최신 우선)라 '마지막 체결가'로만 쓴다 — 체결 집계 용도 아님.
- 연결이 끊기면 5초 후 자동 재연결. 계정당 2연결 한도라 봇 전체에서 1개만 쓴다.

의존성: pip3 install websocket-client (없으면 자동으로 꺼지고 REST 폴링으로 동작)
"""

import json
import threading
import time

WS_URL = "wss://openapi-ws.tossinvest.com/ws/v1"
MAX_CODES = 45          # 연결당 구독 100건 한도 — 시장당 45개로 여유


class PriceStream:
    def __init__(self, token_fn):
        self._token_fn = token_fn       # 만료 대응: 재연결 때마다 새 토큰
        self._latest: dict = {}         # symbol -> (price, monotonic_ts)
        self._want: set = set()
        self._ws = None
        self._connected = False
        self._started = False
        self._lock = threading.Lock()

    # ── 공개 API ────────────────────────────────────────────
    def start(self) -> None:
        if self._started:
            return
        self._started = True
        threading.Thread(target=self._run, daemon=True, name="price-stream").start()

    def set_symbols(self, symbols) -> None:
        """구독 대상 갱신 (변화 있을 때만 재선언 — 선언은 full-replace)."""
        want = set(symbols)
        with self._lock:
            if want == self._want:
                return
            self._want = want
        self._declare()

    def price(self, symbol: str, fresh_secs: float = 10.0) -> float | None:
        v = self._latest.get(symbol)
        if v and time.monotonic() - v[1] <= fresh_secs:
            return v[0]
        return None

    @property
    def connected(self) -> bool:
        return self._connected

    # ── 내부 ────────────────────────────────────────────────
    def _declare(self) -> None:
        ws = self._ws
        if not (ws and self._connected):
            return
        with self._lock:
            kr = sorted(s for s in self._want if s[:1].isdigit())[:MAX_CODES]
            us = sorted(s for s in self._want if not s[:1].isdigit())[:MAX_CODES]
        decl = []
        if kr:
            decl.append({"type": "trade:kr", "codes": kr})
        if us:
            decl.append({"type": "trade:us", "codes": us})
        try:
            ws.send(json.dumps(decl))
        except Exception:               # noqa: BLE001 - 재연결 루프가 처리
            pass

    def _run(self) -> None:
        try:
            import websocket
        except ImportError:
            print("  [스트림] websocket-client 미설치 — REST 폴링으로 동작")
            return

        def on_open(ws):
            self._connected = True
            self._declare()

        def on_message(ws, msg):
            try:
                f = json.loads(msg)
            except ValueError:
                return
            if f.get("type") != "message":
                return
            sym = f.get("topic", "").rsplit(":", 1)[-1]
            try:
                px = float((f.get("data") or {}).get("price"))
            except (TypeError, ValueError):
                return
            self._latest[sym] = (px, time.monotonic())

        def on_end(ws, *a):
            self._connected = False

        while True:
            try:
                ws = websocket.WebSocketApp(
                    WS_URL,
                    header={"Authorization": f"Bearer {self._token_fn()}"},
                    on_open=on_open, on_message=on_message,
                    on_close=on_end, on_error=on_end)
                self._ws = ws
                # 서버는 180초 무수신 시 종료 → 50초 간격 표준 ping (문서상 지원)
                ws.run_forever(ping_interval=50, ping_timeout=10)
            except Exception:           # noqa: BLE001
                pass
            self._connected = False
            time.sleep(5)               # 재연결 백오프
