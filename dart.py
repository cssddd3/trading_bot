"""DART(전자공시) 모니터 — 뉴스 기사보다 빠른 '원천'을 직접 본다.

호재/악재의 시간 순서: 공시 접수(원천) → 기자가 기사 작성(수분~수십분) →
구글뉴스 RSS 인덱싱(또 수분~수십분). 이 모듈은 그 맨 앞단을 1~2분 주기로 폴링한다.

역할 경계: 공시는 매수 트리거가 아니다 — 감시/보유 종목에 새 공시가 뜨면
① 텔레그램 즉시 알림, ② 그 종목의 뉴스필터 캐시 무효화(다음 판단 때 재평가)만 한다.

설정 (없으면 자동으로 꺼짐 — open-fail):
  1. https://opendart.fss.or.kr 회원가입 → 인증키 신청 (무료, 즉시 발급)
  2. .env에 DART_API_KEY=발급키 추가
"""

import json
import os
import time
from datetime import datetime, timedelta, timezone

import requests

import config

KST = timezone(timedelta(hours=9))
LIST_URL = "https://opendart.fss.or.kr/api/list.json"
POLL_SECS = 120


class DartMonitor:
    def __init__(self):
        config.load_env()
        self.key = os.getenv("DART_API_KEY", "").strip()
        self._seen: set = set()          # rcept_no (접수번호) — 재알림 방지
        self._last_poll = 0.0
        self._primed = False             # 첫 폴링은 기존 공시 학습만 (알림 폭주 방지)

    @property
    def enabled(self) -> bool:
        return bool(self.key)

    def check(self, watch_symbols: set) -> list[dict]:
        """감시 종목의 새 공시 반환: [{symbol, corp_name, report, time}].

        실패는 조용히 빈 리스트 (공시 모니터 장애가 매매를 막지 않는다).
        """
        if not self.enabled or time.time() - self._last_poll < POLL_SECS:
            return []
        self._last_poll = time.time()
        today = datetime.now(KST).strftime("%Y%m%d")
        try:
            r = requests.get(LIST_URL, params={
                "crtfc_key": self.key, "bgn_de": today, "end_de": today,
                "page_count": 100}, timeout=10).json()
        except (requests.RequestException, ValueError):
            return []
        if r.get("status") not in ("000", "013"):    # 013 = 조회 결과 없음
            if r.get("status") == "020":              # 키 오류/한도
                print(f"  [DART] API 키 문제: {r.get('message')}")
            return []

        fresh = []
        for item in r.get("list", []):
            rcept = item.get("rcept_no", "")
            if not rcept or rcept in self._seen:
                continue
            self._seen.add(rcept)
            stock = (item.get("stock_code") or "").strip()
            if self._primed and stock and stock in watch_symbols:
                fresh.append({
                    "symbol": stock,
                    "corp_name": item.get("corp_name", ""),
                    "report": item.get("report_nm", ""),
                    "time": item.get("rcept_dt", ""),
                    "url": f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept}",
                })
        self._primed = True
        return fresh
