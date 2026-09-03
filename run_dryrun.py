"""토스 자동매매 러너 — 드라이런(기본) / 실전 소액(--live).

백테스트와 같은 전략 코드(on_open/on_close)를 그대로 호출한다.
드라이런과 실전의 차이는 '체결이 가상 장부냐 실제 주문이냐'뿐이고,
모든 매수는 동일하게 RiskGuard + LLM 뉴스필터를 통과해야 한다.

  python3 run_dryrun.py --watch                 # 드라이런 (실주문 없음)
  LIVE_TRADING=1 + python3 run_dryrun.py --watch --live   # ⚠️ 실전 소액
  python3 run_dryrun.py --status [--live]       # 포트폴리오 현황
  python3 run_dryrun.py --reset  [--live]       # 장부 초기화

실전 모드 이중 잠금: .env에 LIVE_TRADING=1 이 있고 --live 플래그를 같이 줘야만 켜진다.

텔레그램 명령 (봇에게 전송, 등록된 chat_id만 인정):
  /stop    신규 매수 전면 중지 (보유분 손절/청산은 계속 동작)
  /resume  매수 재개
  /flat    보유 전량 즉시 청산 + 매수 중지
  /status  현황 회신
  /budget  예산 확인 · /budget KR 100000 처럼 시장별 예산 변경 (즉시 적용, 영구 저장)

기록: logs/dryrun_*.{json,csv} (드라이런) / logs/live_*.{json,csv} (실전)
"""

import argparse
import csv
import json
import sys
import time
from dataclasses import asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path

import config
import notify
from backtest.engine import round_to_tick
from llm_filter import NewsFilter
from risk import RiskGuard
from scout import load_watchlist, run_scout
from strategy import Action, Fill, Position, build
from toss.client import TossClient
from toss.data import load_bars, bars_from_candles, merge, save_csv, cache_path

KST = timezone(timedelta(hours=9))
STATE = config.LOG_DIR / "dryrun_state.json"
SIGNALS = config.LOG_DIR / "dryrun_signals.csv"
TRADES = config.LOG_DIR / "dryrun_trades.csv"


def paths_for(live: bool) -> tuple[Path, Path, Path]:
    prefix = "live" if live else "dryrun"
    return (config.LOG_DIR / f"{prefix}_state.json",
            config.LOG_DIR / f"{prefix}_signals.csv",
            config.LOG_DIR / f"{prefix}_trades.csv")


def now_kst() -> datetime:
    return datetime.now(KST)


# ── 포트폴리오 장부 (드라이런: 가상 / 실전: 실체결 미러) ─────────

class Portfolio:
    def __init__(self, path: Path = STATE):
        self._path = path
        self.cash = float(config.INITIAL_CASH)
        self.positions: dict[str, dict] = {}
        self.pending: dict[str, dict] = {}     # 다음 시가 체결 예약
        self.done_today: dict[str, str] = {}   # symbol -> 마지막 처리 날짜 (중복 방지)
        self.halted = False                    # 킬 스위치: True면 신규 매수 금지
        self.tg_offset = 0                     # 텔레그램 명령 커서
        self.realized_pnl = {"KR": 0.0, "US": 0.0}   # 시장별 누적 실현손익(KRW) — 예산 복리 근거
        self.budget_base: dict = {}            # /budget 명령으로 설정한 초기예산 (없으면 config 기본값)
        self.manual_watch: dict = {}           # /watch 명령으로 추가한 감시 종목 {symbol: name}

    @classmethod
    def load(cls, path: Path = STATE) -> "Portfolio":
        p = cls(path)
        if path.exists():
            try:
                d = json.loads(path.read_text())
            except json.JSONDecodeError:
                # 감사 C3: 실전 장부 손상을 '조용히 빈 장부'로 만들면 킬스위치까지 풀린다.
                if "live" in path.name:
                    notify.send("🚨 [실전] 장부 파일 손상 감지 — 봇 기동 거부. "
                                "logs/live_state.json 을 확인하세요.")
                    raise SystemExit(f"[!] 장부 손상: {path} — 수동 확인 필요 (자동 초기화 금지)")
                return p
            try:
                p.cash = d.get("cash", p.cash)
                p.positions = d.get("positions", {})
                p.pending = d.get("pending", {})
                p.done_today = d.get("done_today", {})
                p.halted = d.get("halted", False)
                p.tg_offset = d.get("tg_offset", 0)
                rp = d.get("realized_pnl", {})
                if isinstance(rp, dict):
                    p.realized_pnl = {"KR": rp.get("KR", 0.0), "US": rp.get("US", 0.0)}
                else:                          # 구버전(float) 마이그레이션
                    p.realized_pnl = {"KR": float(rp), "US": 0.0}
                p.budget_base = d.get("budget_base", {})
                p.manual_watch = d.get("manual_watch", {})
            except (KeyError, TypeError, ValueError):
                if "live" in path.name:
                    raise SystemExit(f"[!] 장부 필드 손상: {path} — 수동 확인 필요")
            # done_today 무한 증가 방지: 이틀 지난 항목 정리 (감사 지적)
            cutoff = (now_kst().date() - timedelta(days=2)).isoformat()
            p.done_today = {k: v for k, v in p.done_today.items() if v >= cutoff}
        return p

    def save(self) -> None:
        """원자적 저장: tmp에 쓴 뒤 rename — 저장 도중 크래시로 장부가 깨지지 않는다."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"cash": self.cash, "positions": self.positions,
             "pending": self.pending, "done_today": self.done_today,
             "halted": self.halted, "tg_offset": self.tg_offset,
             "realized_pnl": self.realized_pnl,
             "budget_base": self.budget_base,
             "manual_watch": self.manual_watch,
             "updated_at": now_kst().isoformat()},
            ensure_ascii=False, indent=2)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(payload)
        import os
        os.replace(tmp, self._path)

    def position_of(self, symbol: str) -> Position:
        d = self.positions.get(symbol)
        if not d:
            return Position()
        pos = Position(quantity=d["quantity"], avg_price=d["avg_price"],
                       entry_date=d.get("entry_date", ""),
                       highest_close=d.get("highest_close", 0.0),
                       stop_price=d.get("stop_price"))
        return pos

    def exposure(self, prices: dict[str, float]) -> float:
        return sum(p["quantity"] * prices.get(s, p["avg_price"])
                   for s, p in self.positions.items())


def log_signal(row: dict, path: Path = SIGNALS) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["time", "symbol", "strategy", "action",
                                          "price", "quantity", "executed", "reason"])
        if new:
            w.writeheader()
        w.writerow(row)


def log_trade(row: dict, path: Path = TRADES) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["symbol", "entry_date", "entry_price",
                                          "exit_date", "exit_price", "quantity",
                                          "pnl", "pnl_rate", "exit_reason"])
        if new:
            w.writeheader()
        w.writerow(row)


# ── 드라이런 엔진 ─────────────────────────────────────────────

class DryRun:
    def __init__(self, strategy_key: str, params: dict, live: bool = False):
        self.client = TossClient(*config.credentials())
        self.key = strategy_key
        self.params = params
        self.live = live
        self.tag = "실전" if live else "드라이런"
        self.state_path, self.signals_path, self.trades_path = paths_for(live)
        if live:
            # 감사 C3/H: 실전 생성은 main() 경유만 허용 — TT_LIVE_INTENT는 main()이
            # 게이트 통과 후 직접 세우는 내부 플래그라 .env로는 절대 세워지지 않는다.
            # (테스트/외부 코드가 DryRun(live=True)을 직접 만들면 여기서 죽는다)
            import os
            if os.environ.get("TT_LIVE_INTENT") != "yes":
                raise SystemExit("[!] live 인스턴스는 run_dryrun.py main()을 통해서만 생성 가능 "
                                 "(테스트는 드라이런 경로를 쓸 것)")
            # 단일 인스턴스 잠금 — 이중 실행은 장부를 파괴한다.
            # macOS/리눅스는 fcntl.flock, 윈도우는 msvcrt.locking (fcntl 없음)
            self._lock_file = open(config.LOG_DIR / "live.lock", "w")
            try:
                try:
                    import fcntl
                    fcntl.flock(self._lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except ImportError:                  # Windows
                    import msvcrt
                    msvcrt.locking(self._lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                raise SystemExit("[!] 실전 봇이 이미 실행 중입니다 (live.lock 점유). "
                                 "이중 실행은 장부를 파괴합니다.")
        self.pf = Portfolio.load(self.state_path)
        self.guard = RiskGuard(config.RISK)
        self.symbols = list(config.RISK.allow_symbols)
        self.broker = None
        if live:
            from broker import LiveBroker
            self.broker = LiveBroker(self.client, self.guard)
            power = self.broker.buying_power()
            print("⚠️ 실전 모드 — 실제 주문이 나갑니다!")
            print(f"   계좌 {self.broker.account_no} | 매수가능 {power:,.0f}원 | "
                  f"봇 예산 {self._budget_str()}")
            print(f"   봇은 자기가 산 종목만 팔 수 있습니다 (기존 보유 종목은 건드리지 않음)")
            notify.send(f"⚠️ [실전] 봇 시작\n계좌 {self.broker.account_no}\n"
                        f"매수가능 {power:,.0f}원 · 봇 예산 {self._budget_str()}\n"
                        f"명령: /stop /flat /status /budget")
        ok, why = NewsFilter.available()
        self.news = NewsFilter() if ok else None
        if self.news:
            import llm_filter
            llm_filter.on_api_error = self._llm_error_alert
        print(f"LLM 뉴스 필터: {'켜짐 (' + config.NEWS_FILTER['model'] + ')' if ok else '꺼짐 — ' + why}")
        self._last_news_poll = 0.0
        self._last_heartbeat = 0.0     # 시작 직후 첫 하트비트가 바로 나간다
        self._names = dict(config.WHITELIST)
        self._fx_cache, self._fx_at = 0.0, 0.0
        self._seen_candidates: dict[str, set] = {}   # 시장별 랭킹 후보 (10분 확인용)
        self._seen_date = ""
        self._last_scout_check = 0.0
        self._last_reconcile = 0.0
        self.dart = None
        try:
            from dart import DartMonitor
            d = DartMonitor()
            if d.enabled:
                self.dart = d
                print("DART 공시 모니터: 켜짐 (2분 주기)")
            else:
                print("DART 공시 모니터: 꺼짐 (.env DART_API_KEY 없음 — opendart.fss.or.kr 무료 발급)")
        except Exception as e:              # noqa: BLE001
            print(f"DART 공시 모니터: 꺼짐 ({e})")
        self.dashboard = False
        self._dash_retry = 0.0
        self._try_dashboard()
        self.stream = None
        if config.STREAM["enabled"]:
            try:
                from toss.auth import get_access_token
                from toss.stream import PriceStream
                self.stream = PriceStream(
                    lambda: get_access_token(*config.credentials()))
                self.stream.start()
                print("실시간 시세 스트림: 켜짐 (웹소켓, 폴백 REST)")
            except Exception as e:          # noqa: BLE001 - 스트림 실패는 매매를 막지 않는다
                print(f"실시간 시세 스트림: 꺼짐 ({e})")
        self._scale_risk_limits()
        self._apply_watchlist()
        self._apply_manual_watch()
        self._ensure_position_symbols()
        if live:
            self.reconcile(startup=True)

    # ── 대사 (감사 C3: 장부 ↔ 실계좌를 주기적으로 맞춰본다) ─────────
    def reconcile(self, startup: bool = False) -> None:
        """장부와 실계좌 보유를 비교한다. 기동 시 + 1시간마다.

        - 장부 수량 > 실보유: 거래소측 스탑이 발동해 팔렸거나(정상 — 장부 정리),
          알 수 없는 유출(비정상 — 매수 중지 + 경보).
        - 실보유에 있지만 장부에 없는 것: 사용자 소유 — 건드리지 않음 (--adopt로만 편입).
        - 스탑이 없는 장부 포지션: 거래소측 스탑 재등록.
        """
        if not (self.live and self.broker):
            return
        if not startup and time.time() - self._last_reconcile < 3600:
            return
        self._last_reconcile = time.time()
        try:
            holdings = self.client.get_holdings(self.broker.account_seq)
            held = {i["symbol"]: float(i["quantity"])
                    for i in holdings.get("items", [])}
            stop_syms = self.broker.active_stop_symbols()
        except Exception as e:              # noqa: BLE001 - 대사 실패가 매매를 막지 않는다
            print(f"  [대사] 조회 실패: {e}")
            return
        for sym in list(self.pf.positions):
            book_qty = self.pf.positions[sym]["quantity"]
            real_qty = held.get(sym, 0.0)
            if real_qty >= book_qty - 1e-6:
                self._sync_exchange_stop(sym)   # 보유 정상 — 스탑을 '정확히 1개, 올바른 트리거'로
                continue
            had_stop = bool(self.pf.positions[sym].get("stop_order_id"))
            if not had_stop or sym in stop_syms:
                # 스탑 발동으로 설명 안 되는 수량 감소 — 원인 불명 유출 → 안전 우선 정지
                self.pf.halted = True
                notify.send(f"🚨 [실전] 장부-계좌 불일치: {sym} 장부 {book_qty:g}주 "
                            f"vs 실보유 {real_qty:g}주 — 원인 불명. 매수 중지(/resume로 해제). "
                            f"토스 앱에서 확인 필요")
            else:
                # 봇이 등록한 스탑이 사라짐 + 수량 감소 = 거래소측 손절 발동 → 장부 정리
                live_px = self.last_price(sym) or self.pf.positions[sym]["avg_price"]
                notify.send(f"🛑 [실전] {sym} 거래소측 손절 발동 감지 (장부 정리, "
                            f"추정가 {live_px:g}) — 상세는 토스 앱 체결내역 확인")
                pnl_krw = self.to_krw(sym, (live_px - self.pf.positions[sym]["avg_price"])
                                      * book_qty)
                self.pf.realized_pnl[config.market_of(sym)] += pnl_krw
                self.guard.record_close(sym, pnl_krw, was_stop_loss=True)
                del self.pf.positions[sym]
        self.pf.save()

    def _notify_reject(self, symbol: str, why: str) -> None:
        """거부 알림 — 같은 종목·같은 사유 계열은 하루 1회만 (transient 재시도 스팸 방지)."""
        key = f"{symbol}:{now_kst().date()}:{why[:14]}"
        cache = getattr(self, "_reject_notified", None)
        if cache is None:
            cache = self._reject_notified = set()
        if key in cache:
            return
        cache.add(key)
        notify.send(f"⛔ [{self.tag}] 매수 차단\n{symbol} {self._names.get(symbol, '')}\n{why}")

    def _desired_backstop(self, sym: str, p: dict) -> float:
        """거래소측 백스톱의 목표 트리거가격.

        원칙: 전략 자체의 스탑(supertrend 밴드 등)보다 '아래'에 둔다 — 전략이 먼저
        발동하고, 백스톱은 봇이 죽었을 때만 의미 있는 최후 방어선 (감사 C2 정합성).
        """
        rate = self.params.get("stop_loss_rate") or \
            config.BACKSTOP_STOP_RATE.get(self.key, 0.05)
        base = p["avg_price"] * (1 - rate)
        band = p.get("stop_price")
        if band and 0 < band < p["avg_price"]:
            base = min(base, band * 0.97)      # 밴드보다 3% 아래
        return self.round_px(base, config.market_of(sym))

    def _sync_exchange_stop(self, sym: str) -> None:
        """해당 포지션의 거래소측 스탑을 '정확히 1개 + 목표 트리거'로 맞춘다.

        (조회 실패로 중복 등록되던 사고 재발 방지 — 2026-08-26 실계좌에 심볼당 3개 발견)
        """
        p = self.pf.positions[sym]
        if float(p["quantity"]) != int(float(p["quantity"])):
            return                              # 소수점 수량은 조건주문 미지원
        desired = self._desired_backstop(sym, p)
        stops = self.broker.list_stops_for(sym)
        good = [s for s in stops if desired > 0 and abs(s[1] - desired) / desired <= 0.02]
        if len(stops) == 1 and len(good) == 1:
            p["stop_order_id"] = stops[0][0]
            return                              # 정확히 1개 + 트리거 일치 — 그대로
        for cid, trig in stops:                 # 중복/불일치 전부 정리 후 1개 재등록
            try:
                self.client.cancel_conditional_order(self.broker.account_seq, cid)
                print(f"  [대사] {sym} 조건주문 정리 (trigger {trig:g})")
            except Exception as e:              # noqa: BLE001
                print(f"  [대사] {sym} 조건주문 취소 실패: {e}")
        cid = self.broker.set_stop(sym, p["quantity"], desired)
        if cid:
            p["stop_order_id"] = cid
            p["backstop_price"] = desired
            print(f"  [대사] {sym} 거래소측 백스톱 @{desired:g} (1개로 정리)")

    def _apply_manual_watch(self) -> None:
        for sym, name in self.pf.manual_watch.items():
            self._names.setdefault(sym, name)
            self.guard.limits.allow_symbols.setdefault(sym, name)
            if sym not in self.symbols:
                self.symbols.append(sym)
        if self.pf.manual_watch:
            print(f"수동 감시 종목: {', '.join(self.pf.manual_watch)}")

    def _ensure_position_symbols(self) -> None:
        """장부에 있는 종목은 워치리스트에서 빠져도 계속 감시한다.

        (워치리스트는 날짜가 바뀌면 갱신되므로, 이 보정이 없으면 어제 산 종목의
        손절/청산 관리가 끊긴다 — 실제로 V(비자)가 이 버그로 하루 방치됐었다)"""
        unknown = [s for s in self.pf.positions if s not in self._names]
        if unknown:
            try:
                for i in self.client.get_stocks(unknown):
                    self._names[i["symbol"]] = i.get("name", i["symbol"])
            except Exception:               # noqa: BLE001
                pass
        for s in set(self.pf.positions) | set(self.pf.pending):
            self.guard.limits.allow_symbols.setdefault(s, self._names.get(s, s))
            if s not in self.symbols:
                self.symbols.append(s)
                print(f"보유/예약 종목 감시 복원: {s} {self._names.get(s, '')}")

    def adopt(self, symbols: list[str]) -> None:
        """사용자가 직접 산 보유 종목을 봇 장부로 편입한다 (--adopt, 실전 전용).

        편입되면 봇이 산 것과 동일하게 관리된다: 손절/트레일링 스탑, 뉴스 모니터,
        전략 청산 규칙, /flat. 편입 즉시 그 시장 예산에서 평가액만큼 차지한다.
        명시적으로 지정한 종목만 편입한다 — 나머지 보유분은 여전히 건드리지 않는다."""
        holdings = self.client.get_holdings(self.broker.account_seq)
        items = {i["symbol"]: i for i in holdings.get("items", [])}
        adopted = []
        for sym in [s.strip() for s in symbols if s.strip()]:
            if sym in self.pf.positions:
                print(f"  {sym}: 이미 봇 장부에 있음 — 건너뜀")
                continue
            it = items.get(sym)
            if not it:
                print(f"  {sym}: 계좌에 보유 내역이 없음 — 건너뜀")
                continue
            qty = float(it["quantity"])
            avg = float(it["averagePurchasePrice"])
            name = it.get("name", sym)
            self._names[sym] = name
            self.pf.positions[sym] = {
                "quantity": qty, "avg_price": avg,
                "entry_date": now_kst().date().isoformat(),
                "highest_close": avg, "stop_price": None}
            unit = "$" if config.market_of(sym) == "US" else "원"
            adopted.append(f"{sym} {name} {qty:g}주 @ {avg:,.2f}{unit}")
            print(f"  ✓ 편입: {adopted[-1]}")
        if adopted:
            self._ensure_position_symbols()
            self.pf.save()
            notify.send("📥 [실전] 보유 종목 편입 — 이제 봇이 관리합니다\n"
                        + "\n".join(adopted)
                        + f"\n예산 차지분 포함 현황: {self._budget_str()}")
        else:
            print("편입된 종목 없음")

    # ── 환율/통화 헬퍼 ──────────────────────────────────────
    def fx(self) -> float:
        """USD→KRW 환율 (5분 캐시). 조회 실패 시 마지막 값 유지."""
        if time.time() - self._fx_at > 300:
            try:
                self._fx_cache = self.client.get_exchange_rate()
                self._fx_at = time.time()
            except Exception:               # noqa: BLE001
                if not self._fx_cache:
                    self._fx_cache = 1400.0  # 최후 폴백 (보수적으로 높게)
        return self._fx_cache

    def to_krw(self, symbol: str, price: float) -> float:
        return price * self.fx() if config.market_of(symbol) == "US" else price

    def exposure_krw(self, market: str | None = None) -> float:
        """봇 보유 포지션의 KRW 환산 평가액. market 지정 시 그 시장 것만."""
        total = 0.0
        for s, p in self.pf.positions.items():
            if market and config.market_of(s) != market:
                continue
            live = self.last_price(s) or p["avg_price"]
            total += p["quantity"] * self.to_krw(s, live)
        return total

    def budget_total(self, market: str) -> float:
        """그 시장의 현재 예산 = 초기 예산 + 그 시장 누적 실현손익 (복리).

        KR 5만이 10만 되면 KR 예산도 10만. 시장 간 예산은 섞이지 않는다
        (국내 계좌 예수금 ↔ 해외 계좌 예수금이 분리돼 있는 것과 일치).
        미실현 이익은 포함하지 않는다 — 팔아서 확정된 것만 예산이 된다.
        초기예산: 텔레그램 /budget 설정값 > .env(LIVE_BUDGET_*) > 기본 5만원."""
        base = self.pf.budget_base.get(market) or config.LIVE_BUDGET[market]
        return max(0.0, base + self.pf.realized_pnl[market])

    def _budget_str(self) -> str:
        return " · ".join(
            f"{m} {self.budget_total(m):,.0f}원({self.pf.realized_pnl[m]:+,.0f})"
            for m in config.MARKETS)

    def _set_budget(self, market: str, amount: int) -> str:
        self.pf.budget_base[market] = amount
        self.pf.save()
        self._scale_risk_limits()               # 한도도 즉시 새 예산에 연동
        L = self.guard.limits
        return (f"💰 [{self.tag}] {market} 초기예산 → {amount:,}원 (즉시 적용·재시작에도 유지)\n"
                f"현재 예산: {self._budget_str()}\n"
                f"연동 한도: 1회/종목당 {L.max_order_amount:,}원 · 일일손실 -{L.daily_loss_limit:,}원")

    def _scale_risk_limits(self) -> None:
        """RISK_FROM_BUDGET=True면 금액 한도를 현재 예산에 연동한다.

        예: /budget KR 2000000 → 하이닉스(176만원) 1주 매수 가능해짐.
        일일 손실 한도 = 총예산의 DAILY_LOSS_PCT (최소 5만원 유지)."""
        if not config.RISK_FROM_BUDGET:
            return
        per_market = {m: int(self.budget_total(m)) for m in config.MARKETS}
        L = self.guard.limits
        L.max_order_amount = max(per_market.values())
        L.max_symbol_amount = max(per_market.values())
        L.max_total_exposure = sum(per_market.values())
        L.daily_loss_limit = max(50_000, int(sum(per_market.values()) * config.DAILY_LOSS_PCT))
        print(f"리스크 한도(예산 연동): 1회/종목당 {L.max_order_amount:,}원 · "
              f"전체 {L.max_total_exposure:,}원 · 일일손실 -{L.daily_loss_limit:,}원")

    def _budget_parts(self, market: str) -> tuple[float, float]:
        """(실제 예수금 KRW환산, 예산 잔여) — 거부 사유를 정확히 말해주기 위한 분해."""
        remaining = max(0.0, self.budget_total(market) - self.exposure_krw(market))
        if not self.live:
            return self.pf.cash, remaining
        if market == "US":
            power = self.broker.buying_power("USD") * self.fx()
        else:
            power = self.broker.buying_power("KRW")
        return power, remaining

    def _budget_cash(self, market: str) -> float:
        """이번 매수에 쓸 수 있는 현금(KRW). 실전: min(그 계좌 매수가능금액, 그 시장 예산 잔여)."""
        if not self.live:
            return self.pf.cash
        remaining = max(0.0, self.budget_total(market) - self.exposure_krw(market))
        if market == "US":
            power = self.broker.buying_power("USD") * self.fx()
        else:
            power = self.broker.buying_power("KRW")
        return min(power, remaining)

    def _apply_watchlist(self) -> None:
        """오늘자 LLM 워치리스트를 그날의 화이트리스트에 추가한다 (기존 종목 유지)."""
        if not config.SCOUT["use_watchlist"]:
            return
        data = load_watchlist()
        if not data or not data.get("picks"):
            return
        added = []
        for p in data["picks"]:
            sym = p["symbol"]
            self._names[sym] = p.get("name", sym)
            if sym not in self.guard.limits.allow_symbols:
                self.guard.limits.allow_symbols[sym] = p.get("name", sym)
            if sym not in self.symbols:
                self.symbols.append(sym)
                added.append(f"{sym} {p.get('name','')}")
        if added:
            print(f"오늘의 LLM 워치리스트 반영: {', '.join(added)}")

    def _run_scout_market(self, market: str, label: str) -> None:
        t = getattr(self, "_last_scout_llm", {})
        t[market] = time.time()
        self._last_scout_llm = t
        before = set(self.symbols)
        try:
            data = run_scout(client=self.client, market=market, verbose=False)
        except Exception as e:              # noqa: BLE001 - 스카우트 실패는 매매를 막지 않는다
            print(f"[스카우트/{market}] 실패: {e}")
            self._llm_error_alert(e)
            return
        self._apply_watchlist()
        if not data:
            return
        mkt = data.get("markets", {}).get(market, {})
        new = [s for s in self.symbols if s not in before]
        picks = {p["symbol"]: p for p in mkt.get("picks", [])}
        if new:
            lines = [f"· {s} {picks[s]['name']} ({picks[s]['score']:.2f}) — {picks[s]['thesis']}"
                     for s in new if s in picks]
            msg = f"🔭 [{label}/{market}] {mkt.get('market_note', '')}\n" + "\n".join(lines)
            notify.send(msg)
            notify.broadcast(msg + "\n\n(자동매매 봇의 관심종목 — 투자 판단·책임은 각자에게)")
        elif label == "스카우트":            # 장전 첫 실행은 '선정 없음'도 알림
            notify.send(f"🔭 [스카우트/{market}] {mkt.get('market_note', '')}\n(선정 종목 없음)")

    def _maybe_scout(self, market: str) -> None:
        """장전(PRE)에 그 시장의 오늘자 워치리스트가 없으면 한 번 생성."""
        if not (config.SCOUT["auto_run_premarket"] and config.SCOUT["use_watchlist"]
                and self.news):
            return
        data = load_watchlist()
        if data and market in data.get("markets", {}):
            return
        print(f"[스카우트/{market}] 오늘자 워치리스트 없음 → 생성")
        self._run_scout_market(market, "스카우트")

    def _refresh_scout(self, market: str) -> None:
        """장중 10분마다 랭킹만 싸게 확인 → 새 후보가 등장했을 때만 전체 스카우트.

        "이상 없으면 그대로 간다": 후보군이 그대로면 LLM 호출도, 종목 변경도 없다.
        기존 종목은 제거하지 않는다 (보유/스탑 관리가 끊기면 안 되므로).
        """
        mins = config.SCOUT.get("refresh_minutes", 0)
        if not (config.SCOUT["use_watchlist"] and mins and self.news):
            return
        if time.time() - self._last_scout_check < mins * 60:
            return
        self._last_scout_check = time.time()

        today = now_kst().date().isoformat()
        if self._seen_date != today:
            self._seen_date, self._seen_candidates = today, {}
        try:
            from scout import quick_candidate_symbols
            current = quick_candidate_symbols(self.client, market)
        except Exception as e:              # noqa: BLE001
            print(f"[스카우트/{market}] 후보 확인 실패: {e}")
            return
        seen = self._seen_candidates.setdefault(market, set())
        fresh = current - seen
        seen |= current
        if not fresh:
            return                           # 새 얼굴 없음 → 그대로 간다
        min_gap = config.SCOUT.get("llm_min_interval_minutes", 15) * 60
        last = getattr(self, "_last_scout_llm", {}).get(market, 0)
        if time.time() - last < min_gap:     # 스로틀: 새 얼굴은 seen에 쌓였다가
            print(f"[스카우트/{market}] 새 후보 {sorted(fresh)} — LLM 스로틀 중 (다음 주기에 평가)")
            return                           # 다음 실행 때 함께 평가된다 (호출 폭주 방지)
        print(f"[스카우트/{market}] 새 후보 감지 {sorted(fresh)} → 재평가")
        self._run_scout_market(market, "스카우트 갱신")

    def _news_monitor(self) -> None:
        """장중: 새 헤드라인 감지 시 보유 종목 재평가, 치명적 악재면 가상 청산."""
        if not (config.NEWS_MONITOR["enabled"] and self.news and self.pf.positions):
            return
        if time.time() - self._last_news_poll < config.NEWS_MONITOR["poll_minutes"] * 60:
            return
        self._last_news_poll = time.time()
        for sym in list(self.pf.positions):
            v = self.news.check(sym, name=self._names.get(sym), refresh=True)
            if not v:
                continue
            if v.urgent_exit:
                # LLM 플래그만으로 자동청산하지 않는다 — 거래소 데이터로 객관 검증.
                # (9/3 실발생: 남의 회사 기사로 trading_halt 플래그 → 멀쩡한 씨피시스템
                #  자동 매도. 운좋게 +8.5% 익절이었지만 오작동은 오작동)
                confirmed = False
                try:
                    info = (self.client.get_stocks([sym]) or [{}])[0]
                    krd = info.get("koreanMarketDetail") or {}
                    confirmed = bool(krd.get("krxTradingSuspended")
                                     or krd.get("liquidationTrading")
                                     or info.get("status") not in (None, "ACTIVE"))
                except Exception:           # noqa: BLE001 - 확인 불가면 보수적으로 보류
                    pass
                live = self.last_price(sym)
                if confirmed and live:
                    print(f"  [뉴스 모니터] {sym} 치명 악재(거래소 확인됨) → 청산: {v.reason()}")
                    self.virtual_sell(sym, live, f"뉴스 리스크 청산 — {v.reason()}",
                                      was_stop=True)
                elif v.headlines_hash != self.pf.done_today.get(f"{sym}:newsalert"):
                    self.pf.done_today[f"{sym}:newsalert"] = v.headlines_hash
                    notify.send(f"🚨 [{self.tag}] {sym} {self._names.get(sym, '')} "
                                f"치명 플래그 감지 — 단, 거래소 확인 결과 정지/이상 아님 "
                                f"→ 자동청산 보류, 직접 판단 요망\n{v.reason()}")
            elif v.alert_exit and not v.headlines_hash == self.pf.done_today.get(f"{sym}:newsalert"):
                # 감성 악재는 자동 매도 대신 경보 — 판단은 사람이 (/flat 또는 개별 대응)
                self.pf.done_today[f"{sym}:newsalert"] = v.headlines_hash
                notify.send(f"⚠️ [{self.tag}] {sym} {self._names.get(sym, '')} 악재 뉴스 감지"
                            f" (자동 청산 안 함)\n{v.reason()}\n"
                            f"직접 판단: /flat(전량) 또는 유지")

    def _try_dashboard(self) -> None:
        """대시보드 서버 기동 (실패 시 60초마다 재시도 — 포트를 좀비가 물고 있다
        풀리는 경우가 실발생: 8/28)."""
        if self.dashboard or not config.DASHBOARD["enabled"] \
                or time.time() - self._dash_retry < 60:
            return
        self._dash_retry = time.time()
        try:
            import dashboard
            self.dashboard = dashboard.start_server(config.DASHBOARD["port"],
                                                    live=self.live)
            if self.dashboard:
                print(f"웹 대시보드: http://localhost:{config.DASHBOARD['port']} (읽기 전용)")
        except Exception as e:              # noqa: BLE001 - 대시보드 실패는 매매 무관
            print(f"웹 대시보드: 꺼짐 ({e})")

    def _write_dashboard(self) -> None:
        self._try_dashboard()
        if not self.dashboard:
            return
        self._refresh_prices(max_age=60)       # 유휴 루프에서도 시세 신선도 유지
        try:
            import dashboard
            dashboard.write_snapshot(self)
        except Exception:                   # noqa: BLE001
            pass

    def _llm_error_alert(self, err) -> None:
        """Claude API 크레딧 소진/과금 오류를 하루 1회 텔레그램으로 알린다.

        (8/27 실발생: 01:23~12:50 크레딧 소진으로 스카우트·뉴스필터가 11시간
        조용히 죽어 있었다 — 로그에만 64회 남고 사용자는 몰랐다)"""
        msg = str(err).lower()
        if not any(w in msg for w in ("credit balance", "billing", "quota")):
            return
        day = now_kst().date().isoformat()
        if self.pf.done_today.get("llm_credit_alert") == day:
            return
        self.pf.done_today["llm_credit_alert"] = day
        notify.send(f"🚨 [{self.tag}] Claude API 크레딧 소진 — 스카우트/뉴스필터 정지!\n"
                    f"매매(전략+안전장치)는 계속되지만 LLM 감시망이 꺼졌습니다.\n"
                    f"충전: console.anthropic.com → Plans & Billing")

    def _shadow_scan(self) -> None:
        """전환 스캐너: KR 15:20+ 1회 — '60일 평균 거래대금 상위 top_n'에서 오늘 전환 탐지.

        ⚠️ 유니버스 정의 = 60일 평균 거래대금 (당일 거래대금 아님!) — 4년 검증이
        이 정의로 통과했고, 당일 기준(spike)은 기각됐다 (어제 폭등 테마주 필터가 됨).
        실시간 랭킹 상위 pool_n을 후보풀로 받아 일봉으로 60일 평균을 계산해 재랭킹한다."""
        if not config.SCANNER["enabled"]:
            return
        today = now_kst().date().isoformat()
        if self.pf.done_today.get("scanner") == today:
            return
        try:
            from strategy import indicators as ta
            from scout import _fetch_rankings, LEVERAGE_WORDS
            # 후보풀 = 시드(전 종목 캐시로 만든 60일 평균 상위 300) ∪ 오늘 실시간 상위 100.
            # 랭킹 API 상한이 100이라 시드가 몸통, 랭킹은 '새로 뜨는 종목' 유입 통로다.
            pool = _fetch_rankings(self.client, "KR", count=100)
            syms = [r["symbol"] for r in pool
                    if 1000 <= float(r["price"]["lastPrice"]) <= 450_000]
            seed_path = config.DATA_DIR / "scanner" / "universe_seed.json"
            if seed_path.exists():
                import json as _json
                for s_ in _json.loads(seed_path.read_text()).get("symbols", []):
                    if s_ not in syms:
                        syms.append(s_)
            infos = {}
            for k in range(0, len(syms), 100):   # get_stocks 배치 상한 대응
                infos.update({i["symbol"]: i
                              for i in self.client.get_stocks(syms[k:k + 100])})
            ranked, bars_of = [], {}
            for sym in syms:
                name = infos.get(sym, {}).get("name", sym)
                if any(w in name.upper() for w in LEVERAGE_WORDS):
                    continue
                try:
                    bars = self.bars_with_today(sym, today)[0]
                except Exception:           # noqa: BLE001
                    continue
                if len(bars) < 202:
                    continue
                avg_val = sum(b.close * b.volume for b in bars[-60:]) / min(60, len(bars))
                ranked.append((avg_val, sym, name))
                bars_of[sym] = bars
            ranked.sort(reverse=True)
            flips = []
            for _, sym, name in ranked[: config.SCANNER["top_n"]]:
                bars = bars_of[sym]
                line, dirs = ta.supertrend(bars, 10, 3.0)
                closes = [b.close for b in bars]
                if not (dirs[-1] == 1 and dirs[-2] == -1):
                    continue
                ema = ta.ema(closes, 200)
                rsi = ta.rsi(closes, 14)
                if ema[-1] is None or closes[-1] < ema[-1] or (rsi[-1] or 0) > 75:
                    continue
                flips.append((sym, name, closes[-1]))
                with open(config.LOG_DIR / config.SCANNER["shadow_csv"], "a") as f:
                    f.write(f"{today},{sym},{name},{closes[-1]:.0f}\n")
            queued = []
            if flips and config.SCANNER["auto_buy"]:
                for sym, name, close in flips[: config.SCANNER["max_daily_picks"]]:
                    if sym in self.pf.positions or sym in self.pf.pending:
                        continue
                    self._names[sym] = name
                    self.guard.limits.allow_symbols.setdefault(sym, name)
                    if sym not in self.symbols:
                        self.symbols.append(sym)
                    self.pf.pending[sym] = {
                        "action": "BUY", "reason": "전환 스캐너: Supertrend 상승 전환",
                        "date": today, "frac": config.SCANNER["position_frac"]}
                    queued.append(f"· {sym} {name} (종가 {close:,.0f})")
                self.pf.save()
            if queued:
                msg = ("🔍 [전환 스캐너] 오늘 상승 전환 → 내일 시가 매수 예약 "
                       f"(1건당 예산의 {config.SCANNER['position_frac']:.0%} 이내, "
                       f"뉴스 거부권·한도 검사 후 체결)\n" + "\n".join(queued))
                notify.send(msg)
                notify.broadcast(msg)
            elif flips:
                lines = [f"· {s} {n} (종가 {c:,.0f})" for s, n, c in flips]
                notify.send("🔍 [전환 스캐너] 오늘 상승 전환 감지 (예약 없음 — 보유/중복)\n"
                            + "\n".join(lines))
            print(f"  [스캐너] 전환 {len(flips)}건, 예약 {len(queued)}건")
            self.pf.done_today["scanner"] = today   # 성공했을 때만 완료 처리 (9/2 수리)
        except Exception as e:              # noqa: BLE001 - 스캐너 장애는 매매 무관
            print(f"  [스캐너] 실패: {e} — 다음 틱에 재시도")
            if self.pf.done_today.get("scanner_fail_alert") != today:
                self.pf.done_today["scanner_fail_alert"] = today
                notify.send(f"⚠️ [{self.tag}] 전환 스캐너 오류 (재시도 중): {e}")

    def _check_dart(self) -> None:
        """감시/보유 종목의 새 전자공시 → 즉시 알림. 보유종목이면 뉴스 재평가도 앞당긴다.

        공시는 기사보다 수분~수십분 빠른 원천이지만, 매수 트리거는 아니다 (알림+재평가만).
        """
        watch = set(self.symbols) | set(self.pf.positions)
        try:
            fresh = self.dart.check(watch)
        except Exception:                    # noqa: BLE001 - 공시 모니터 장애는 매매 무관
            return
        for d in fresh:
            held = d["symbol"] in self.pf.positions
            print(f"  [DART] {d['corp_name']}({d['symbol']}) 새 공시: {d['report']}")
            notify.send(f"📢 [{self.tag}] 새 공시 — {d['corp_name']}({d['symbol']})"
                        f"{' ★보유중' if held else ''}\n{d['report']}\n{d['url']}")
            notify.broadcast(f"📢 새 공시 — {d['corp_name']}({d['symbol']})\n"
                             f"{d['report']}\n{d['url']}")
            if held:
                self._last_news_poll = 0.0   # 다음 틱에 뉴스 모니터 즉시 재평가

    # ── 시장 시간 ───────────────────────────────────────────
    def market_session(self, country: str = "KR") -> tuple[str, dict]:
        """세션 판정. KR/US 공통 — 반환 info에 trade_date(그 시장의 거래일)를 담는다.

        미국장은 KST 밤~새벽에 걸치므로 trade_date가 KST 오늘과 다를 수 있다.
        캐시(60초)로 캘린더 호출을 아낀다.
        """
        cache = getattr(self, "_cal_cache", {})
        hit = cache.get(country)
        if hit and time.time() - hit[0] < 60:
            cal = hit[1]
        else:
            cal = self.client.get_market_calendar(country)
            cache[country] = (time.time(), cal)
            self._cal_cache = cache
        today = cal.get("today", {})
        now = now_kst()

        if country == "KR":
            reg = (today.get("integrated") or {}).get("regularMarket")
            if not reg:
                nxt = ((cal.get("nextBusinessDay") or {}).get("integrated") or {})
                return "CLOSED", {"nextOpen": (nxt.get("regularMarket") or {}).get("startTime")}
            start = datetime.fromisoformat(reg["startTime"])
            end = datetime.fromisoformat(reg["endTime"])
            info = dict(reg)
            info["trade_date"] = today.get("date", now.date().isoformat())
            if now < start:
                return "PRE", info
            if now >= end:
                return "AFTER", info
            auction = datetime.fromisoformat(reg["singlePriceAuctionStartTime"])
            if now >= auction:
                return "CLOSING_AUCTION", info   # 15:20~15:30 동시호가
            return "OPEN", info

        # 미국: 세션이 자정을 넘기므로 KST 날짜가 바뀌면 캘린더의 today가 다음 거래일로
        # 넘어간다. 진행 중인 세션은 previousBusinessDay 쪽에 있을 수 있어 둘 다 본다.
        # (이걸 안 보면 00:00~05:00 구간을 '장전'으로 오판해 미장 감시가 멈춘다)
        for day in (today, cal.get("previousBusinessDay") or {}):
            reg = day.get("regularMarket")
            if not reg:
                continue
            start = datetime.fromisoformat(reg["startTime"])
            end = datetime.fromisoformat(reg["endTime"])
            info = dict(reg)
            info["trade_date"] = day.get("date", now.date().isoformat())
            if start <= now < end:
                # 토스는 미국 소수점 수량 주문을 '정규장 마감 1시간 전'까지만 받는다
                # (fractional-quantity-outside-regular-hours, 8/26 실발생).
                # 우리 미국 포지션은 금액매수라 전부 소수점 → 주문 가능 구간을
                # 22:30~04:00으로 재정의한다. 04:00~05:00 매도는 pending으로 넘어간다.
                cutoff = end - timedelta(hours=1)
                if now >= cutoff:
                    return "AFTER", info
                if (cutoff - now).total_seconds() <= 600:
                    return "CLOSING_AUCTION", info   # 03:50~04:00 = 종가 판단 창
                return "OPEN", info
            if end <= now < end + timedelta(hours=2):
                return "AFTER", info                 # 마감 직후 (종가 판단 마무리 창)
        reg = today.get("regularMarket")
        if reg and now < datetime.fromisoformat(reg["startTime"]):
            info = dict(reg)
            info["trade_date"] = today.get("date", now.date().isoformat())
            return "PRE", info
        nxt = (cal.get("nextBusinessDay") or {}).get("regularMarket") or {}
        return "CLOSED", {"nextOpen": nxt.get("startTime") or (reg or {}).get("startTime")}

    # ── 데이터 ─────────────────────────────────────────────
    def bars_with_today(self, symbol: str, trade_date: str | None = None):
        """일봉 + 오늘 봉 포함 여부. 감사 H4 대응: 과거 봉은 하루 1회만 수집하고
        메모리에 캐시, 매 틱에는 최신 2봉만 갱신한다 (틱 지연 수십 초 → 1초 미만).
        """
        target = trade_date or now_kst().date().isoformat()
        cache = getattr(self, "_bars_cache", None)
        if cache is None:
            cache = self._bars_cache = {}
        hist, hist_day = cache.get(symbol, (None, ""))
        if hist is None or hist_day != now_kst().date().isoformat():
            hist = load_bars(symbol, "1d", 400, source="toss", client=self.client,
                             verbose=False)
            cache[symbol] = (hist, now_kst().date().isoformat())
            save_csv(cache_path(symbol, "1d"), hist)
        page = self.client.get_candles(symbol, "1d", count=2)
        fresh = bars_from_candles(page.get("candles", []), "1d")
        bars = merge(hist, fresh)
        cache[symbol] = (bars, now_kst().date().isoformat())
        has_today = bool(bars) and bars[-1].date == target
        return bars, has_today

    @staticmethod
    def round_px(price: float, market: str) -> float:
        """호가 반올림 — KR: 호가단위, US: $0.01."""
        return round(price, 2) if market == "US" else round_to_tick(price)

    def _refresh_prices(self, max_age: float = 0.0) -> None:
        """감시+보유 전 종목 시세를 1콜로 배치 조회해 틱 맵에 저장.

        기존엔 심볼마다 REST를 따로 쳐서 틱당 25~30콜(지연 수초)이 들었다 — 검수 효율 항목."""
        now = time.time()
        if max_age and now - getattr(self, "_px_ts", 0) < max_age:
            return
        syms = sorted(set(self.symbols) | set(self.pf.positions))[:200]
        if not syms:
            return
        try:
            rows = self.client.get_prices(syms)
            self._px_map = {r["symbol"]: float(r["lastPrice"]) for r in rows}
            self._px_ts = now
        except Exception:                       # noqa: BLE001 - 이전 맵 유지
            pass

    def last_price(self, symbol: str) -> float | None:
        if self.stream:
            px = self.stream.price(symbol, config.STREAM["fresh_secs"])
            if px:
                return px                        # 1순위: 웹소켓 실시간 체결가
        if time.time() - getattr(self, "_px_ts", 0) < 90:
            px = getattr(self, "_px_map", {}).get(symbol)
            if px:
                return px                        # 2순위: 틱 배치 시세 (틱마다 갱신)
        try:                                     # 3순위: 개별 REST (감시 밖 종목 등)
            rows = self.client.get_prices([symbol])
            return float(rows[0]["lastPrice"]) if rows else None
        except Exception:                       # noqa: BLE001
            return None

    # ── 체결 (드라이런: 가상 장부 / 실전: 실주문) ─────────────────
    def virtual_buy(self, symbol: str, price: float, reason: str,
                    max_frac: float | None = None) -> None:
        market = config.market_of(symbol)
        fee_rate = config.US_FEE_RATE if market == "US" else config.FEE_RATE
        price = self.round_px(price * (1 + config.SLIPPAGE_RATE), market)
        budget_krw = min(self._budget_cash(market) * config.POSITION_PCT,
                         config.RISK.max_order_amount,
                         self.budget_total(market) * config.MAX_POSITION_BUDGET_PCT[market])
        if max_frac:                         # 스캐너 매수: 분할 상한 (승격 조건)
            budget_krw = min(budget_krw, self.budget_total(market) * max_frac)
        amount_usd = 0.0
        if market == "US":
            amount_usd = budget_krw / self.fx() / (1 + fee_rate)
            qty = round(amount_usd / price, 6)   # 소수점 주식 (실전은 체결 후 확정)
        else:
            qty = int(budget_krw // (price * (1 + fee_rate)))
        pos_now = self.pf.positions.get(symbol, {})
        held_value_krw = self.to_krw(symbol, pos_now.get("quantity", 0) * price)
        exposure_krw = self.exposure_krw()

        # 여력 부족은 '왜'가 중요 — 예수금 부족(입금 필요)인지 예산 소진(청산 대기)인지 구분
        if (market == "KR" and qty <= 0) or (market == "US" and amount_usd < 2):
            power, remaining = self._budget_parts(market)
            # KR: 1주 가격 / US: 최소 주문 $2 (소수점 매수라 1주 전체가 필요하지 않음)
            need = self.to_krw(symbol, price) if market == "KR" else 2 * self.fx()
            if power < remaining:
                why = (f"{market} 예수금 부족 — 계좌 가용 {power:,.0f}원 < 필요 {need:,.0f}원. "
                       f"(예산 잔여는 {remaining:,.0f}원 — 입금하면 살 수 있음)")
            else:
                why = (f"{market} 예산 소진 — 잔여 {remaining:,.0f}원 < 필요 {need:,.0f}원. "
                       f"(보유 종목이 예산 점유 중 — 청산되면 재개, /budget으로 증액 가능)")
            log_signal({"time": now_kst().isoformat(timespec="seconds"), "symbol": symbol,
                        "strategy": self.key, "action": "BUY",
                        "price": f"{price:.2f}" if market == "US" else f"{price:.0f}",
                        "quantity": 0, "executed": False, "reason": f"거부: {why}"},
                       self.signals_path)
            print(f"  [매수 거부] {symbol} {why}")
            self._notify_reject(symbol, why)
            return "transient"

        warnings = []
        stock_info = None
        try:
            warnings = self.client.get_warnings(symbol)
            infos = self.client.get_stocks([symbol])
            stock_info = infos[0] if infos else None
        except Exception:                       # noqa: BLE001 - 참조 실패는 차단 사유 아님
            pass

        # 한도 검사는 전부 KRW 기준. 미국은 금액 기반이라 (환산액, 1주)로 검사한다.
        if market == "US":
            ok, why = self.guard.check_buy(
                symbol, amount_usd * self.fx(), 1 if amount_usd >= 2 else 0,
                holdings_value=held_value_krw, total_exposure=exposure_krw,
                warnings=warnings, stock_info=stock_info)
        else:
            ok, why = self.guard.check_buy(
                symbol, price, qty, holdings_value=held_value_krw,
                total_exposure=exposure_krw,
                warnings=warnings, stock_info=stock_info)

        if ok and self.pf.halted:
            ok, why = False, "킬 스위치(/stop) 상태 — 매수 중지 중 (/resume 으로 해제)"

        # 마지막 관문: LLM 뉴스 필터 (악재 뉴스 거부권)
        if ok and self.news:
            verdict = self.news.check(symbol, name=self._names.get(symbol))
            if verdict and verdict.blocks_buy:
                ok, why = False, f"뉴스 필터 — {verdict.reason()}"

        # 실전: 실제 주문 → 실체결 가격/수량으로 기록
        # (KR: 지정가+0.3% / US: 금액 기반 시장가 — 소수점 취득)
        if ok and self.live:
            fill = self.broker.buy(symbol, qty, price,
                                   holdings_value=held_value_krw,
                                   total_exposure=exposure_krw,
                                   halted=self.pf.halted,
                                   amount_usd=amount_usd if market == "US" else None)
            if not fill:
                ok, why = False, "실주문 미체결/거부"
            else:
                price, qty = fill["avg_price"], fill["filled"]

        px_fmt = f"{price:.2f}" if market == "US" else f"{price:.0f}"
        log_signal({"time": now_kst().isoformat(timespec="seconds"), "symbol": symbol,
                    "strategy": self.key, "action": "BUY", "price": px_fmt,
                    "quantity": f"{qty:g}", "executed": ok,
                    "reason": reason if ok else f"거부: {why}"},
                   self.signals_path)
        if not ok:
            print(f"  [매수 거부] {symbol} {why}")
            if why.startswith("뉴스 필터") or self.live:
                self._notify_reject(symbol, why)
            # 감사 H3: '그날 다시 안 바뀌는 사유'만 permanent — 나머지는 재시도 허용
            permanent = ("뉴스 필터" in why or "화이트리스트" in why or "쿨다운" in why
                         or "유의 종목" in why or "거래정지" in why or "킬 스위치" in why
                         or "일일" in why)
            return "permanent" if permanent else "transient"

        if not self.live:
            fee = price * qty * fee_rate
            self.pf.cash -= self.to_krw(symbol, price * qty + fee)
        self.pf.positions[symbol] = {
            "quantity": qty, "avg_price": price,
            "entry_date": now_kst().date().isoformat(),
            "highest_close": price, "stop_price": None}
        if not self.live:
            self.guard.record_order()           # 실전은 broker가 이미 기록
        # 감사 C5: 실전 매수 즉시 거래소측 백스톱 등록 (봇이 죽어도 보호)
        if self.live:
            trig = self._desired_backstop(symbol, self.pf.positions[symbol])
            cid = self.broker.set_stop(symbol, qty, trig)
            if cid:
                self.pf.positions[symbol]["stop_order_id"] = cid
                self.pf.positions[symbol]["backstop_price"] = trig
        unit = "$" if config.market_of(symbol) == "US" else "원"
        px_disp = f"{price:,.2f}" if unit == "$" else f"{price:,.0f}"
        stop_note = (f"\n거래소측 손절 @{self.pf.positions[symbol].get('stop_price') or '-'}"
                     if self.live else "")
        print(f"  [{self.tag} 매수] {symbol} {qty:g}주 @ {px_disp}{unit} — {reason}")
        notify.broadcast(f"🟢 매수: {symbol} {self._names.get(symbol, '')} @ {px_disp}"
                         f"{'$' if market == 'US' else '원'} — {reason}")
        notify.send(f"🟢 [{self.tag}] 매수\n{symbol} {self._names.get(symbol, '')} "
                    f"{qty:g}주 @ {px_disp}{unit}\n{reason}{stop_note}")
        return "filled"

    def virtual_sell(self, symbol: str, price: float, reason: str,
                     was_stop: bool = False) -> None:
        pos = self.pf.positions.get(symbol)
        if not pos:
            return                               # 봇 장부에 없는 종목(기존 보유분)은 절대 팔지 않는다
        market = config.market_of(symbol)
        fee_rate = config.US_FEE_RATE if market == "US" else config.FEE_RATE
        tax_rate = config.US_SELL_TAX_RATE if market == "US" else config.SELL_TAX_RATE
        price = self.round_px(price * (1 - config.SLIPPAGE_RATE), market)
        qty = pos["quantity"]
        ok, why = self.guard.check_sell(symbol, qty, qty)
        if not ok:
            print(f"  [매도 거부] {symbol} {why}")
            return

        if self.live:
            # 감사 C4/422루프: 주문 가능한 세션에서만 매도. 불가 세션이면 다음 시가 예약
            sess, sinfo = self.market_session(market)
            if sess not in ("OPEN", "CLOSING_AUCTION"):
                if self.pf.pending.get(symbol, {}).get("action") != "SELL":
                    self.pf.pending[symbol] = {"action": "SELL",
                                               "reason": f"(장외이월) {reason}", "date": ""}
                    notify.send(f"⏸ [실전] {symbol} 매도 불가 세션({sess}) — "
                                f"다음 장 시가 매도 예약\n사유: {reason}")
                return
            if market == "KR" and sess == "CLOSING_AUCTION":
                # 동시호가: 취소 없이 15:30 단일가 매칭까지 대기하는 전용 경로
                deadline = datetime.fromisoformat(sinfo["endTime"]).timestamp() + 90
                fill = self.broker.sell_at_close(symbol, qty, deadline)
            else:
                fill = self.broker.sell(symbol, qty, price)
            if not fill:
                print(f"  [실주문 매도 실패] {symbol} — 다음 시가 매도 예약")
                self.pf.pending[symbol] = {"action": "SELL",
                                           "reason": f"(재시도) {reason}", "date": ""}
                notify.send(f"⚠️ [실전] 매도 미체결 — {symbol} 다음 장 시가 예약\n사유: {reason}")
                return
            if fill.get("status") == "UNKNOWN":
                self.pf.halted = True
                notify.send(f"🚨 [실전] {symbol} 매도 주문 상태 불명 — 매수 중지. "
                            f"토스 앱 확인 후 /resume")
            price, qty = fill["avg_price"], fill["filled"]

        gross = price * qty
        fee = gross * (fee_rate + tax_rate)
        if not self.live:
            self.pf.cash += self.to_krw(symbol, gross - fee)
        pnl_krw = self.to_krw(symbol, (price - pos["avg_price"]) * qty - fee)
        rate = price / pos["avg_price"] - 1
        px_fmt = f"{price:.2f}" if market == "US" else f"{price:.0f}"
        en_fmt = f"{pos['avg_price']:.2f}" if market == "US" else f"{pos['avg_price']:.0f}"
        log_signal({"time": now_kst().isoformat(timespec="seconds"), "symbol": symbol,
                    "strategy": self.key, "action": "SELL", "price": px_fmt,
                    "quantity": f"{qty:g}", "executed": True, "reason": reason},
                   self.signals_path)
        log_trade({"symbol": symbol, "entry_date": pos["entry_date"],
                   "entry_price": en_fmt,
                   "exit_date": now_kst().date().isoformat(),
                   "exit_price": px_fmt, "quantity": f"{qty:g}",
                   "pnl": f"{pnl_krw:.0f}", "pnl_rate": f"{rate:.4f}",
                   "exit_reason": reason}, self.trades_path)
        remaining = pos["quantity"] - qty
        if remaining > 1e-9:                     # 부분 체결 → 잔량은 장부에 유지
            self.pf.positions[symbol]["quantity"] = remaining
        else:
            del self.pf.positions[symbol]
        if not self.live:
            self.guard.record_order()            # 실전은 broker가 이미 기록
        self.pf.realized_pnl[market] += pnl_krw  # 예산 복리: 그 시장 예산에 실현손익 반영
        self.guard.record_close(symbol, pnl_krw, was_stop_loss=was_stop)
        unit = "$" if market == "US" else "원"
        px_disp = f"{price:,.2f}" if market == "US" else f"{price:,.0f}"
        print(f"  [{self.tag} 매도] {symbol} {qty:g}주 @ {px_disp}{unit} "
              f"(손익 {pnl_krw:+,.0f}원, {rate:+.2%}) — {reason}")
        emoji = "🔴" if pnl_krw < 0 else "🔵"
        notify.broadcast(f"{emoji} 매도: {symbol} {self._names.get(symbol, '')} "
                         f"@ {px_disp}{'$' if market == 'US' else '원'} ({rate:+.2%}) — {reason}")
        notify.send(f"{emoji} [{self.tag}] 매도\n{symbol} {self._names.get(symbol, '')} "
                    f"{qty:g}주 @ {px_disp}{unit}\n손익 {pnl_krw:+,.0f}원 ({rate:+.2%})\n{reason}")

    # ── 텔레그램 킬 스위치 + LLM 비서 ─────────────────────────
    def _handle_commands(self) -> None:
        self.pf.tg_offset, commands, questions = notify.poll_commands(self.pf.tg_offset)
        if questions:
            self.pf.save()               # offset 먼저 저장 (재시작 시 중복 응답 방지)
            import tg_assistant
            reply = tg_assistant.answer("\n".join(questions),
                                        tg_assistant.build_context(self))
            if reply:
                notify.send(f"🤖 {reply}")
            else:
                notify.send("🤖 (LLM 응답 실패 — /status 로 기본 현황은 확인할 수 있어요)")
        for cmd in commands:
          try:                              # 감사 H8: 명령 처리 예외가 봇을 죽이면 안 됨
            base = cmd.split()[0]
            if base == "/budget":
                toks = cmd.upper().split()
                if len(toks) == 3 and toks[1] in ("KR", "US") and toks[2].isdigit():
                    amount = int(toks[2])
                    if not 1_000 <= amount <= 10_000_000:
                        notify.send("⚠️ 예산은 1,000원 ~ 10,000,000원 사이로 설정하세요.")
                    else:
                        notify.send(self._set_budget(toks[1], amount))
                else:
                    notify.send(f"현재 예산: {self._budget_str()}\n"
                                "변경: /budget KR 100000  또는  /budget US 70000")
            elif base == "/watch":
                toks = cmd.split()
                if len(toks) != 2:
                    cur = ", ".join(f"{s_} {n}" for s_, n in self.pf.manual_watch.items()) or "(없음)"
                    notify.send(f"수동 감시: {cur}\n추가: /watch MRNA · 해제: /unwatch MRNA")
                else:
                    sym = toks[1].upper()
                    try:
                        infos = self.client.get_stocks([sym])
                    except Exception:           # noqa: BLE001
                        infos = []
                    if not infos:
                        notify.send(f"⚠️ {sym}: 종목을 찾을 수 없습니다 (KR 6자리 코드 / US 티커)")
                    else:
                        name = infos[0].get("name", sym)
                        self.pf.manual_watch[sym] = name
                        self._apply_manual_watch()
                        notify.send(f"👁 감시 추가: {sym} {name}\n"
                                    f"전략 시그널이 나오면 매수 대상이 됩니다 "
                                    f"(안전장치·예산 검사는 동일 적용)")
            elif base == "/unwatch":
                toks = cmd.split()
                sym = toks[1].upper() if len(toks) == 2 else ""
                if sym in self.pf.manual_watch:
                    name = self.pf.manual_watch.pop(sym)
                    if sym not in self.pf.positions and sym in self.symbols:
                        self.symbols.remove(sym)
                    notify.send(f"👁 감시 해제: {sym} {name}"
                                + (" (보유 중이라 관리는 계속됨)" if sym in self.pf.positions else ""))
                else:
                    notify.send(f"⚠️ {sym or '?'}: 수동 감시 목록에 없습니다")
            elif cmd == "/stop":
                self.pf.halted = True
                notify.send(f"🛑 [{self.tag}] 신규 매수 중지. 보유분 손절/청산은 계속 동작. /resume 으로 재개")
            elif cmd == "/resume":
                self.pf.halted = False
                notify.send(f"▶️ [{self.tag}] 매수 재개")
            elif cmd == "/flat":
                self.pf.halted = True
                if not self.pf.positions:
                    notify.send(f"🛑 [{self.tag}] 보유 없음 · 매수 중지 완료")
                for sym in list(self.pf.positions):
                    px = self.last_price(sym)
                    if px:
                        self.virtual_sell(sym, px, "킬 스위치(/flat) 전량 청산", was_stop=True)
            elif cmd == "/status":
                lines = [f"[{self.tag}] {'🛑매수중지' if self.pf.halted else '▶️가동중'}"]
                unreal = 0.0
                for s, p in self.pf.positions.items():
                    lines.append("· " + self._position_line(s, p))
                    live = self.last_price(s) or p["avg_price"]
                    unreal += self.to_krw(s, (live - p["avg_price"]) * p["quantity"])
                if self.pf.positions:
                    lines.append(f"평가손익 합계 {unreal:+,.0f}원")
                else:
                    lines.append("보유 없음")
                for sym, pd in self.pf.pending.items():
                    lines.append(f"📅 예약: {sym} {self._names.get(sym, '')} "
                                 f"{pd['action']} (다음 시가) — {pd['reason']}")
                if self.live and self.broker:
                    lines.append(f"예산 {self._budget_str()}")
                    try:
                        lines.append(f"예수금 KR {self.broker.buying_power('KRW'):,.0f}원"
                                     f" · US ${self.broker.buying_power('USD'):,.2f}")
                    except Exception:       # noqa: BLE001
                        pass
                watch = [f"{s} {self._names.get(s, '')}" for s in self.symbols
                         if s not in self.pf.positions]
                if watch:
                    lines.append(f"감시 {len(watch)}종목: " + ", ".join(watch[:8])
                                 + (" 외" if len(watch) > 8 else ""))
                lines.append(self.guard.summary())
                feats = [f"시세 {'실시간' if self.stream and self.stream.connected else 'REST'}",
                         f"공시 {'ON' if self.dart else 'OFF'}",
                         f"LLM {config.LLM_MODELS['scout'].split('-')[1]}"
                         f"/{config.LLM_MODELS['filter'].split('-')[1]}"]
                lines.append(" · ".join(feats))
                notify.send("\n".join(lines))
          except Exception as e:            # noqa: BLE001
            print(f"  [!] 명령({cmd}) 처리 실패: {e}")
            notify.send(f"⚠️ 명령 처리 오류: {cmd} — {type(e).__name__}")
          finally:
            self.pf.save()

    # ── 심볼 1개 처리 ───────────────────────────────────────
    def process(self, symbol: str, session: str, trade_date: str | None = None) -> None:
        bars, has_today = self.bars_with_today(symbol, trade_date)
        strat = build(self.key, **self.params)
        strat.prepare(bars)
        i = len(bars) - 1
        if i < strat.warmup():
            print(f"  {symbol}: 봉 부족 ({len(bars)} < {strat.warmup()})")
            return
        pos = self.pf.position_of(symbol)
        live = self.last_price(symbol)
        today = trade_date or now_kst().date().isoformat()

        # 0) 예약된 시가 주문 체결 (오늘 봉이 열렸으면 오늘 시가로)
        pend = self.pf.pending.get(symbol)
        if pend and has_today and session in ("OPEN", "CLOSING_AUCTION") \
                and pend.get("date") != today:
            retry_at = getattr(self, "_retry_after", {})
            if time.time() >= retry_at.get(f"pend:{symbol}", 0):
                fill = bars[-1].open
                result = None
                if pend["action"] == "BUY" and not pos.is_open:
                    live_px = self.last_price(symbol) or fill
                    result = self.virtual_buy(symbol, max(fill, live_px),
                                              f"시가 체결: {pend['reason']}",
                                              max_frac=pend.get("frac"))
                    # 매수 직후 첫 종가판정 전까지 손절선 공백 방지 —
                    # 현 시점의 전략 밴드를 즉시 심는다 (CRWD 무방비 구간, 8/29 발견)
                    if result == "filled" and symbol in self.pf.positions \
                            and not self.pf.positions[symbol].get("stop_price"):
                        band = getattr(strat, "line", None)
                        if band and band[i]:
                            self.pf.positions[symbol]["stop_price"] = band[i]
                            print(f"  {symbol}: 초기 손절선 {band[i]:,.2f} (전략 밴드)")
                elif pend["action"] == "SELL" and pos.is_open:
                    self.virtual_sell(symbol, fill, f"시가 체결: {pend['reason']}")
                if result == "transient":
                    # 예수금 부족 등 일시 사유 → 예약을 지우지 않고 10분 후 재시도
                    # (8/28 실발생: 아침 예수금 부족 → 예약 삭제 → 낮에 입금해도 미실행)
                    retry_at[f"pend:{symbol}"] = time.time() + 600
                    self._retry_after = retry_at
                elif self.pf.pending.get(symbol) is pend:
                    # 실행했던 그 예약일 때만 삭제 — virtual_sell이 실패 시 재등록한
                    # '(재시도)' 예약을 지우던 실버그 수리 (9/2, 검수 C2)
                    del self.pf.pending[symbol]
            pos = self.pf.position_of(symbol)

        # 1) 장중: 조건부 주문 (변동성 돌파 목표가 / 손절·트레일링 스탑)
        if session in ("OPEN", "CLOSING_AUCTION") and has_today and live:
            # 저장된 트레일링 스탑을 Position 에 복원한 상태로 on_open 호출
            order = strat.on_open(i, pos)
            if order:
                if order.side == Action.BUY and not pos.is_open and live >= order.price:
                    retry_at = getattr(self, "_retry_after", {})
                    if self.pf.done_today.get(f"{symbol}:buy") != today \
                            and time.time() >= retry_at.get(symbol, 0):
                        result = self.virtual_buy(symbol, max(order.price, live), order.reason)
                        if result != "transient":     # 일시 사유(여력부족·미체결)는 재시도 허용
                            self.pf.done_today[f"{symbol}:buy"] = today
                        else:                          # 단, 10분 백오프 (30초마다 재시도 낭비 방지)
                            retry_at[symbol] = time.time() + 600
                            self._retry_after = retry_at
                elif order.side == Action.SELL and pos.is_open and live <= order.price:
                    self.virtual_sell(symbol, min(order.price, live), order.reason,
                                      was_stop=True)
                elif pos.is_open:
                    print(f"  {symbol}: 스탑 대기 {order.price:,.2f} (현재 {live:,.2f})")
                else:
                    print(f"  {symbol}: 매수 대기 목표가 {order.price:,.2f} (현재 {live:,.2f})")

        # 2) 종가 판단 — 동시호가/마감 직전 또는 장 종료 후 1회
        if session in ("CLOSING_AUCTION", "AFTER") and has_today \
                and self.pf.done_today.get(f"{symbol}:close") != today:
            pos = self.pf.position_of(symbol)
            sig = strat.on_close(i, pos)
            # on_close 가 갱신한 트레일링 스탑을 저장
            if pos.is_open and symbol in self.pf.positions:
                self.pf.positions[symbol]["highest_close"] = pos.highest_close
                self.pf.positions[symbol]["stop_price"] = pos.stop_price
            if sig and sig.action != Action.HOLD:
                price_now = live or bars[-1].close
                if sig.fill == Fill.THIS_CLOSE:
                    if sig.action == Action.SELL and pos.is_open:
                        self.virtual_sell(symbol, price_now, sig.reason)
                    elif sig.action == Action.BUY and not pos.is_open:
                        self.virtual_buy(symbol, price_now, sig.reason)
                else:
                    prev = self.pf.pending.get(symbol)
                    new_pend = {"action": sig.action.value,
                                "reason": sig.reason, "date": today}
                    # 스캐너가 먼저 건 예약의 분할 상한(frac)은 보존 — 승격 전제 조건.
                    # (같은 틱에 on_close가 같은 전환을 감지해 덮어쓰던 실버그, 9/2 수리)
                    if prev and prev.get("frac") and prev.get("action") == sig.action.value:
                        new_pend["frac"] = prev["frac"]
                    self.pf.pending[symbol] = new_pend
                    print(f"  {symbol}: 다음 시가 {sig.action.value} 예약 — {sig.reason}")
                    if not prev or prev.get("action") != sig.action.value:
                        act = "매수" if sig.action == Action.BUY else "매도"
                        notify.send(f"📅 [{self.tag}] 다음 시가 {act} 예약\n"
                                    f"{symbol} {self._names.get(symbol, '')} — {sig.reason}")
                    log_signal({"time": now_kst().isoformat(timespec="seconds"),
                                "symbol": symbol, "strategy": self.key,
                                "action": f"PENDING_{sig.action.value}",
                                "price": f"{price_now:.2f}", "quantity": 0,
                                "executed": False, "reason": sig.reason},
                               self.signals_path)
            if session == "AFTER":
                self.pf.done_today[f"{symbol}:close"] = today

    # ── 실행 ────────────────────────────────────────────────
    def _heartbeat(self, session: str) -> None:
        """살아있다는 신호. 이 메시지가 주기마다 안 오면 봇이 죽은 것이다."""
        if not config.HEARTBEAT["enabled"]:
            return
        if time.time() - self._last_heartbeat < config.HEARTBEAT["interval_minutes"] * 60:
            return
        self._last_heartbeat = time.time()
        if self.pf.positions:
            unreal = sum(self.to_krw(s_, ((self.last_price(s_) or p["avg_price"])
                                          - p["avg_price"]) * p["quantity"])
                         for s_, p in self.pf.positions.items())
            pos = f"보유 {len(self.pf.positions)}종목 평가 {unreal:+,.0f}원"
        else:
            pos = "보유 없음"
        pend = f" · 예약 {len(self.pf.pending)}건" if self.pf.pending else ""
        halt = " · 🛑매수중지" if self.pf.halted else ""
        notify.send(f"💓 [{self.tag}] 정상 가동 {now_kst():%H:%M} · 세션 {session} · "
                    f"{pos}{pend}{halt} · 궁금한 건 자유롭게 질문 (예: 오늘 뭐했어?)")

    def tick(self) -> dict:
        """1회 순회. 반환: {시장: 세션} — 시장별(KR 주간 / US 야간)로 따로 처리한다."""
        sessions: dict[str, tuple] = {}
        for m in config.MARKETS:
            try:
                sessions[m] = self.market_session(m)
            except Exception as e:              # noqa: BLE001 - 캘린더 실패 = 그 시장만 쉼
                print(f"  [!] {m} 캘린더 조회 실패: {e}")
                sessions[m] = ("CLOSED", {})
        sess_str = " ".join(f"{m}={s[0]}" for m, s in sessions.items())
        halt = " 🛑매수중지" if self.pf.halted else ""
        print(f"\n[{now_kst():%Y-%m-%d %H:%M:%S}] [{self.tag}]{halt} {sess_str}  {self.guard.summary()}")
        self._handle_commands()
        self._heartbeat(sess_str)
        self.reconcile()

        active_any = any(s[0] in ("OPEN", "CLOSING_AUCTION") for s in sessions.values())
        if not active_any and all(s[0] == "CLOSED" for s in sessions.values()):
            nxt = next((s[1].get("nextOpen") for s in sessions.values()
                        if s[1].get("nextOpen")), None)
            print(f"  모든 시장 휴장. 다음 장 시작: {nxt or '(조회 불가)'}")

        if self.stream:                        # 감시 종목 + 보유 종목 실시간 구독
            self.stream.set_symbols(set(self.symbols) | set(self.pf.positions))
        self._refresh_prices()                 # 틱당 1콜 배치 시세 (개별 REST 대체)
        self._write_dashboard()
        for m, (sess, _info) in sessions.items():
            if sess == "PRE":
                self._maybe_scout(m)
            elif sess == "OPEN":
                self._refresh_scout(m)
        kr_sess = sessions.get("KR", ("CLOSED", {}))[0]
        if self.dart and kr_sess != "CLOSED":
            self._check_dart()                 # 전자공시 2분 폴링
        if kr_sess in ("CLOSING_AUCTION", "AFTER"):
            self._shadow_scan()                # 15:20 동시호가부터 전환 스캔 (하루 1회)
        if active_any:
            self._news_monitor()

        for sym in self.symbols:
            m = config.market_of(sym)
            if m not in sessions:
                continue
            sess, info = sessions[m]
            if sess in ("CLOSED", "PRE"):    # 장전엔 판단할 게 없다 (스카우트만 돎)
                continue
            try:
                self.process(sym, sess, info.get("trade_date"))
            except Exception as e:              # noqa: BLE001 - 한 종목 실패가 루프를 죽이면 안 됨
                print(f"  [!] {sym} 처리 실패: {e}")
        self.pf.save()
        return {m: s[0] for m, s in sessions.items()}

    def daily_report(self) -> str:
        """오늘의 시그널/체결/포트폴리오 요약."""
        today = now_kst().date().isoformat()

        def today_rows(path):
            if not path.exists():
                return []
            with path.open() as f:
                return [r for r in csv.DictReader(f)
                        if (r.get("time", "") or r.get("exit_date", "")).startswith(today)]

        sigs = today_rows(self.signals_path)
        trades = today_rows(self.trades_path)
        pnl = sum(float(t["pnl"]) for t in trades)
        expo = self.exposure_krw()
        if self.live:
            asset_line = f"보유평가 {expo:,.0f}원 / 봇 예산 {self._budget_str()}"
        else:
            asset_line = f"평가자산 {self.pf.cash + expo:,.0f}원 (초기 {config.INITIAL_CASH:,})"
        lines = [f"📋 [{self.tag} 마감] {today}",
                 f"시그널 {len(sigs)}건 · 체결 {len(trades)}건 · 실현손익 {pnl:+,.0f}원",
                 asset_line]
        for sym, p in self.pf.positions.items():
            lines.append("보유: " + self._position_line(sym, p))
        if not self.pf.positions:
            lines.append("보유 포지션 없음")
        lines.append(self.guard.summary())
        report = "\n".join(lines)
        print(report)
        notify.send(report)
        return report

    def _idle_wait(self, seconds: float) -> None:
        """긴 대기 중에도 30초마다 텔레그램 명령/질문을 확인한다 (밤에도 응답 가능)."""
        deadline = time.time() + seconds
        last_snap = 0.0
        while time.time() < deadline:
            time.sleep(min(30.0, max(1.0, deadline - time.time())))
            try:
                self._handle_commands()
            except Exception as e:          # noqa: BLE001 - 대기 중 오류가 루프를 죽이면 안 됨
                print(f"  [!] 명령 처리 실패: {e}")
            if time.time() - last_snap >= 60:   # 휴장 중에도 대시보드는 살아있게
                last_snap = time.time()
                self._write_dashboard()

    def watch(self, interval: int) -> None:
        print(f"{self.tag} 감시 시작 (간격 {interval}초, 시장 {'/'.join(config.MARKETS)}, "
              f"Ctrl+C 로 종료)")
        print(f"텔레그램 알림: {'켜짐' if notify.enabled() else '꺼짐 (notify.py 참고)'}")
        reported = None   # 국장 마감 리포트를 보낸 날짜
        try:
            while True:
                sessions = self.tick()
                # 국장 마감 후 하루 1회 리포트 (미장 체결분은 다음날 리포트에 포함)
                if sessions.get("KR") == "AFTER":
                    today = now_kst().date().isoformat()
                    if reported != today:
                        print("국장 종료 — 일일 리포트 발송.")
                        self.daily_report()
                        reported = today
                statuses = set(sessions.values())
                if statuses & {"OPEN", "CLOSING_AUCTION"}:
                    fast = (config.STREAM["fast_interval"]
                            if self.stream and self.stream.connected else interval)
                    time.sleep(min(interval, fast))  # 스트림 연결 중엔 더 촘촘히
                elif "PRE" in statuses:
                    self._idle_wait(max(interval, 300))
                else:
                    self._idle_wait(1800)            # 전부 휴장/마감 — 텔레그램만 30초 폴링
        except KeyboardInterrupt:
            print("\n수동 종료.")
        except Exception as e:              # noqa: BLE001 - 무인 운용: 죽기 전에 알린다
            notify.send(f"🚨 [{self.tag}] 봇 비정상 종료!\n{type(e).__name__}: {e}\n"
                        f"맥북에서 재시작이 필요합니다.")
            raise

    def _position_line(self, s: str, p: dict) -> str:
        live = self.last_price(s) or p["avg_price"]
        r = live / p["avg_price"] - 1
        if config.market_of(s) == "US":
            px = f"@ ${p['avg_price']:,.2f} → ${live:,.2f}"
        else:
            px = f"@ {p['avg_price']:,.0f} → {live:,.0f}"
        return (f"{s} {self._names.get(s, '')}: {p['quantity']:g}주 {px} ({r:+.2%}) "
                f"스탑 {p.get('stop_price') or '-'}")

    def status(self) -> None:
        expo = self.exposure_krw()
        if self.live and self.broker:
            print(f"[실전] 매수가능 {self.broker.buying_power():,.0f}원 | "
                  f"봇 보유평가 {expo:,.0f}원 | 봇 예산 {self._budget_str()} | "
                  f"{'🛑매수중지' if self.pf.halted else '▶️가동중'}")
        else:
            print(f"가상 현금: {self.pf.cash:,.0f}원 | 평가금액: {expo:,.0f}원 "
                  f"| 합계: {self.pf.cash + expo:,.0f}원 (초기 {config.INITIAL_CASH:,})")
        for s, p in self.pf.positions.items():
            print("  " + self._position_line(s, p))
        if self.pf.pending:
            print(f"  예약 주문: {self.pf.pending}")
        print(self.guard.summary())


def main():
    ap = argparse.ArgumentParser(description="토스 자동매매 러너 (기본: 드라이런)")
    ap.add_argument("--strategy", "-t", default=config.DEFAULT_STRATEGY)
    ap.add_argument("--interval", type=int, default=30, help="폴링 간격(초)")
    ap.add_argument("--live", action="store_true",
                    help="⚠️ 실전 주문 모드 (.env에 LIVE_TRADING=1 도 필요)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="1회 판단 후 종료")
    mode.add_argument("--watch", action="store_true", help="장중 폴링 루프")
    mode.add_argument("--status", action="store_true", help="포트폴리오 현황")
    mode.add_argument("--reset", action="store_true", help="장부 초기화")
    mode.add_argument("--adopt", metavar="SYMBOLS",
                      help="직접 산 보유 종목을 봇 장부로 편입 (쉼표구분, --live 전용)")
    args = ap.parse_args()

    if args.adopt and not args.live:
        sys.exit("[!] --adopt 는 실전(--live) 전용입니다. 실제 보유 내역에서 편입합니다.")

    if args.live:
        import os
        config.load_env()
        if os.getenv("LIVE_TRADING") != "1":
            sys.exit("[!] 실전 모드 이중 잠금: .env에 LIVE_TRADING=1 을 추가해야 켜집니다.\n"
                     "    (실수로 --live 를 붙이는 것을 막기 위한 안전장치)")
        # 감사 게이트(R10): 검증을 통과한 전략만 실전에 붙는다
        gate = config.LOG_DIR / "strategy_validation.json"
        gate_ok = False
        if gate.exists():
            try:
                rec = json.loads(gate.read_text())
                gate_ok = rec.get("passed") and rec.get("strategy") == args.strategy
            except json.JSONDecodeError:
                pass
        if not gate_ok and os.getenv("LIVE_VALIDATION_OVERRIDE") != "1":
            sys.exit(f"[!] 전략 '{args.strategy}' 은 검증 게이트를 통과하지 못했습니다.\n"
                     f"    검증 실행: python3 run_backtest.py -t {args.strategy} --validate\n"
                     f"    (기준: OOS 30건+, OOS 기대손익>0, MC 손실확률<30%)\n"
                     f"    본인 책임 우회: .env에 LIVE_VALIDATION_OVERRIDE=1")

    if args.live:
        import os
        os.environ["TT_LIVE_INTENT"] = "yes"   # 생성자 가드 해제 (main 경유 증명)

    state_path, _, _ = paths_for(args.live)
    if args.reset:
        Path(state_path).unlink(missing_ok=True)
        print(f"{'실전' if args.live else '가상'} 장부를 초기화했습니다 (시그널/거래 로그는 보존).")
        return 0

    params = config.STRATEGY_PARAMS.get(args.strategy, {})
    dr = DryRun(args.strategy, params, live=args.live)
    if args.adopt:
        dr.adopt(args.adopt.split(","))
    elif args.status:
        dr.status()
    elif args.watch:
        dr.watch(args.interval)
    else:
        dr.tick()
    return 0


if __name__ == "__main__":
    sys.exit(main())
