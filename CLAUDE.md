# toss-trader 프로젝트 컨텍스트

토스증권 Open API 기반 국내+미국주식 소액 자동매매 봇. 사용자 PC에서 실행.

## 미장(US) 지원 메모
- 심볼 구분: 숫자 시작=KR, 영문 시작=US (`config.market_of`). `config.MARKETS`로 시장 on/off
- 세션: US 정규장 KST 22:30~익일 05:00 (`market-calendar/US`)이나, 토스가 **소수점 주문을
  마감 1시간 전(04:00)까지만** 받아서 (fractional-quantity-outside-regular-hours, 8/26 실발생)
  주문 가능 구간을 22:30~04:00으로 재정의: 03:50~04:00 종가판단 창, 04:00 이후 AFTER
  (매도는 pending 전환). 미장 거래일은 KST 날짜와 다를 수 있음 — trade_date 사용
- 주문: US 매수는 금액 기반 시장가(orderAmount, 소수점 취득) / 매도는 시장가(소수점은 시장가만 허용).
  KR은 기존 지정가 방식 유지. 호가: KR 호가단위, US $0.01
- 한도/예산 검사는 전부 KRW 기준 (환율 `/api/v1/exchange-rate`, baseCurrency/quoteCurrency 필수)
- 스카우트: 랭킹 상위 100 후보 풀(1콜) → 필터 → LLM에 20개 제시, 3분마다 후보 확인.
  '새 후보' 트리거는 상위 30만 (하위권 순위 순환 소음 방지), LLM 호출은 15분 스로틀.
  워치리스트는 `markets: {KR: {...}, US: {...}}` 구조로 시장별 병합

## 현재 상태 (2026-08-27 감사 후 대수술 완료)
- 6관점 감사(64건 확정, `research/audit_2026-08-27.md`) → 권고 반영 완료:
  · 검증 게이트: `--live`는 `logs/strategy_validation.json` passed=true인 전략만 기동
    (`run_backtest.py -t st --validate`로 생성. 우회는 .env LIVE_VALIDATION_OVERRIDE=1)
  · 기본 전략 vb→st 전환 (vb: OOS -0.96%/건·MC손실 100% 기각 / st: +7.35%/건·0.7% 통과)
  · 거래소측 손절(조건부주문 SINGLE/MARKET) — 매수 즉시 등록, 매도 전 취소 필수.
    ⚠️ 소수점 수량(미국 금액매수)은 조건주문 거부됨 → 봇 내 스탑만
  · 대사(reconcile): 기동 시+1시간마다 장부↔실계좌 비교. 스탑 발동 감지 시 장부 정리,
    원인불명 불일치 시 halted (stop_order_id 있던 포지션만 발동 추정)
  · 백스톱 스탑은 _sync_exchange_stop이 '심볼당 정확히 1개+목표 트리거'로 관리
    (조건주문 list는 status=OPEN 필수! 누락 시 400→빈목록→중복등록 사고가 8/26 실발생).
    목표 트리거 = min(평단×(1-백스톱율), 전략밴드×0.97) — 전략이 항상 먼저 발동
  · KR 종가청산은 broker.sell_at_close (동시호가 중 취소 금지, 15:30 매칭 대기)
  · 장외 세션 매도 → 즉시 주문 대신 pending 다음 시가 예약 (422 루프 방지)
  · live 인스턴스는 main() 경유만 생성 가능(TT_LIVE_INTENT 내부 플래그) + flock 단일 인스턴스
    + 장부 원자적 저장(tmp+rename), 손상 시 기동 거부 (테스트는 절대 live 경로 금지!)
  · 리스크 일자 경계 06:00 KST (미장 자정 리셋 버그 수정) — risk.risk_day()
  · 뉴스 자동청산은 치명 플래그(거래정지/상폐/분식/횡령)+헤드라인 3건 이상만,
    감성 악재는 텔레그램 경보만 (사람이 판단)
  · 스카우트: 레버리지/인버스 ETF·상장 60일 미만 제외, 뉴스필터 캐시 3h
  · launchd 자동재시작: launchd/com.tosstrader.live.plist (사용자가 load)
  · 봉 데이터 메모리 캐시 (틱당 400봉 재수집 제거 — 지연 수십초→1초 미만)

## 반응속도 (2026-08-26 저지연 작업)
- `toss/stream.py` 웹소켓 실시간 체결가 (declarative full-replace 구독, LOSSY, 계정당 2연결).
  러너 last_price()가 스트림(10초 신선도) 우선, 낡으면 REST 폴백. 연결 중 장중 틱 8초
  (`config.STREAM`). websocket-client 미설치/연결실패 시 자동 REST 폴백 (open-fail)
- 뉴스 소스 2개 병합 (`news.py`): 구글뉴스 검색(넓지만 수분~수십분 지연) +
  인베스팅닷컴 공개 RSS(분 단위 신선, 종목명 토큰 매칭, 피드캐시 120초).
  ⚠️ 인베스팅 Pro 구독은 API가 없어 봇 연동 불가 — 공개 피드만 사용
- `dart.py` DART 전자공시 모니터 (기사보다 빠른 원천, 2분 폴링): 감시/보유 종목 새 공시 →
  텔레그램 알림 + 보유종목이면 뉴스 재평가 즉시 트리거. 매수 트리거 아님.
  .env DART_API_KEY 필요 (opendart.fss.or.kr 무료) — 없으면 자동 꺼짐
- 스카우트 후보확인 10분→3분 (랭킹 1콜, 새 얼굴 있을 때만 LLM, LLM은 15분 스로틀)
- 원칙 불변: 뉴스/공시는 매수 트리거가 아니다 (거부권+알림+재평가만). 매수는 검증된
  전략 시그널로만 — "뉴스 보고 올라타기"는 검증 게이트 통과한 전략이 생기기 전엔 금지
- 뉴스모멘텀(nm) 전략 검증 시도 (2026-08-27, `research/newsmom_backtest.py`) → **기각**:
  분봉 급등+거래량폭증+신고가 진입, 일별 거래대금 상위20 유니버스(생존편향 방지,
  DART 상장법인+토스 일봉으로 자체 구축), 99거래일 1분봉 2,807파일.
  IS 최적 +0.46%/건이 OOS -0.66%/건·승률 33%·MC 손실확률 97.8%로 붕괴
  (`research/newsmom_result.json`). 급등 추격은 vb에 이어 2번째 데이터 기각 —
  재도전 시 다른 신호(공시 직후 반응 등)로, 같은 그리드 재탕 금지 (다중검정 편향)
- 공시눌림목(pullback) 전략 검증 (2026-08-27, 지인 제안, `research/pullback_backtest.py`)
  → **기각**: 장중 공급계약 공시 400건(KIND에서 공시 시각 확보 — DART list엔 시각 없음,
  KIND 정정공시만 제외·원본은 유지=look-ahead 방지) 중 진입 23%.
  IS 전 조합(익절4~7%/손절2~3%) 마이너스, OOS -1.62%/건·승률 8%·MC 손실 100%.
  기제: 공급계약 팝 후 -2% 눌림은 눌림목이 아니라 전체 되돌림의 시작(92% 손절 종결).
  강한 종목은 눌리지 않아 진입 자체가 약한 케이스만 선별함. 익절/손절 튜닝으로 구제 불가.
  다음 후보: DART 유형별 익일 드리프트(자사주취득·임원 장내매수 등, 일봉로 검증 가능)
- 전환 스캐너 검증 (2026-08-31, `research/scanner_backtest.py`) → **2가지 유니버스 모두 기각**:
  st 규칙 그대로(튜닝 없음) 유니버스만 확대. ①당일 거래대금 상위100: OOS -4.5%/건·MC 100%
  (급등 테마주 필터가 됨 — 전환일=폭등일, 익일 갭 매수 후 즉사, vb/nm과 동일 패턴).
  ②60일 평균 거래대금 상위100(대형주): OOS -5.0%/건·승률 18%·MC 100%.
  시사점: 2026년 4~8월 KR장에서 '신규 상승 전환' 신호 자체가 레짐상 손실 구간
  (KODEX·삼전도 7월부터 하락추세). 봇의 저활동은 결함이 아니라 레짐 반영일 가능성.
  ⚠️ 정정: 거래소 백스톱 트리거는 min(평단-8%, 밴드×0.97) — 밴드보다 항상 아래인 재해용.
  실질 손절선은 전략 밴드이며 전환 직후엔 -20~40% 아래일 수 있음 (US 소수점은 봇 내 밴드가 유일).
  스캐너 시뮬의 '-8% 캡'은 실제보다 후한 가정이었고 그래도 기각 — 결론 불변.
  ⚠️ 스캐너 재도전 시 이 2회 검정을 다중검정 카운트에 포함할 것
- 전환 스캐너 4년 검증 (long 모드, 3,327종목 83% 커버리지): IS(23~25.8) +0.23%/건 441건,
  OOS(25.9~26.8) **+3.00%/건** 231건 승률 30% — 메커니즘 자체는 장기 양의 기대값.
  단 MC 손실확률 30.5% > 30% 커트라인으로 **기각(아슬)**. 우상단 꼬리가 두꺼운 분포
  (최악5% -95%) — 전액 순차 복리 가정의 MC라 연속 손절에 취약. 실매수 승격 보류,
  그림자 모드(`config.SCANNER`, KR 마감 후 상위100 전환 기록+알림)로 실증 수집 중.
  → 2026-08-31 분산 재검증: 포지션당 예산 1/2 가정 MC 손실확률 13.6%(<30% ✅), 1/3이면 9.0%.
  **사용자 지시로 자동매수 승격** (config.SCANNER auto_buy=True): 15:20 동시호가 스캔 →
  하루 최대 2건 다음날 시가 매수 예약, 1건당 예산의 position_frac(0.5) 상한 — 이 분할이
  승격의 전제이므로 frac 제거 금지 (전액이면 손실확률 31%로 기각 조건). 뉴스 거부권·
  리스크가드·백스톱은 기존 매수와 동일 적용

## 로드맵
1. ~~읽기 전용 연동~~ ✅
2. ~~전략 3종(변동성돌파/EMA교차/Supertrend) + 백테스트~~ ✅ `run_backtest.py`
3. ~~드라이런: 가상 체결 + 시그널 로그 + 안전장치 적용~~ ✅ `run_dryrun.py`
4. ~~소액 실전: 주문 API + 안전장치 같은 커밋~~ ✅ — 사용자 결정으로 드라이런 관찰 없이 조기 진입.
   `--live` + .env LIVE_TRADING=1 이중 잠금, LIVE_BUDGET(5만원) 예산 상한,
   봇 장부 밖 종목 매도 불가(기존 보유 보호), 텔레그램 킬 스위치(/stop /flat)

## 구현 메모
- 전략은 `strategy/base.py`의 on_open(장중 조건부주문)/on_close(종가 판단) 인터페이스.
  백테스트 엔진과 드라이런이 **같은 메서드**를 호출한다 (미래참조 방지 구조)
- 백테스트 체결 가정은 보수적: 갭 돌파는 시가 체결, 같은 봉 매수+손절이면 둘 다 체결,
  수수료 0.015%+거래세 0.15%+슬리피지 0.1%+호가단위 반올림 (`backtest/engine.py`)
- 캔들은 `before`/`nextBefore` 페이지네이션으로 수년치 수집, `data/*.csv` 캐시 (`toss/data.py`)
- 리스크 한도·화이트리스트는 `config.py`의 RISK, 검사 로직은 `risk.py`의 RiskGuard.
  드라이런 상태는 `logs/dryrun_state.json`, 일일 카운터는 `logs/risk_state.json`
- LLM 모델 역할별 티어링 (2026-08-27, `config.LLM_MODELS`): 스카우트=sonnet-5,
  뉴스필터/비서=haiku-4-5 (감성분류·요약엔 충분, 비용 1/8~1/10). effort 파라미터는
  haiku에서 400 → `config.output_config_for()`가 모델별로 자동 처리.
  스카우트 LLM 호출은 시장당 최소 15분 간격 스로틀 (후보확인 REST는 3분 유지 —
  스로틀 없인 랭킹 순환으로 하루 150회+ 호출 실측). .env SCOUT_LLM_MODEL 등으로 오버라이드
- LLM 통합 4종 (전부 open-fail — 키 없으면 자동 꺼짐, 설계 원칙: **LLM은 해석만, 결정은 규칙**):
  1. `scout.py` 종목 스카우트 — 랭킹 후보군(규칙 필터) 안에서 관심종목 선택 → `logs/watchlist.json`.
     장전 1회 + 장중 `refresh_minutes`(기본 120분)마다 갱신, 실시간 랭킹 우선(비장중 1d 폴백).
     갱신 시 새 종목은 추가만, 기존 종목은 그날 계속 감시 (`config.SCOUT`)
  2. `llm_filter.py` 매수 거부권 — 헤드라인 감성 악재면 매수 차단 (`config.NEWS_FILTER`)
  3. 뉴스 모니터 (run_dryrun 내) — 장중 새 헤드라인 감지 시 보유종목 재평가 (`config.NEWS_MONITOR`)
  4. `tg_assistant.py` 텔레그램 비서 — "/"없는 자유 질문에 봇 상태를 읽고 답변.
     **읽기·설명 전용** (제어는 /stop /resume /flat /status 만, 코드가 결정적 처리).
     대화 히스토리 `logs/tg_history.json` 영속 (최근 10문답·8천자 상한, 재시작에도 유지.
     상태 스냅샷은 히스토리에 안 쌓고 system에 매번 최신본만 — 2026-08-27 "맥락 끊김" 수정).
     긴 대기 중에도 `_idle_wait`가 30초마다 텔레그램 폴링 → 밤에도 응답
  판정 캐시 `logs/news_verdicts.json` (헤드라인 해시로 중복 호출 방지)
- 백테스트 `--mc`: 몬테카를로 부트스트랩(3000회)으로 강건성 검증 (`backtest/montecarlo.py`)
- 웹 대시보드 `dashboard.py` (2026-08-28): 러너 내장 데몬 스레드, 포트 8787 (config.DASHBOARD).
  러너가 매 틱 `logs/dashboard.json` 스냅샷을 원자적으로 쓰고 서버는 그 파일만 서빙
  (토스 API 직접 호출 금지 — 토큰/레이트리밋 충돌 방지). 읽기 전용, 제어는 텔레그램만.
  단독 실행 가능(`python3 dashboard.py` — 마지막 스냅샷 표시), 포트 점유 시 조용히 skip
- 공유 채널 `notify.broadcast()` (2026-08-28): .env TELEGRAM_BROADCAST_CHAT_ID 설정 시
  스카우트 픽/매수/매도/공시를 텔레그램 채널로도 발신 (발신 전용, 계좌 수치 제외,
  명령 수신은 개인 chat_id만). 미설정이면 no-op
- 무인 운용: `./start.sh [live]`(caffeinate+nohup) / `./stop.sh`, 텔레그램 알림 `notify.py`
  (매수/매도/차단/마감리포트/크래시). watch 루프는 마감 후에도 다음 영업일까지 대기
- 편입: `--adopt 심볼,심볼 --live` 로 사용자가 직접 산 종목을 봇 장부에 넣으면
  봇이 산 것과 동일하게 관리(스탑/뉴스/청산). 명시한 종목만 — 나머지 보유분은 불가침
- 예산: 텔레그램 `/budget KR 100000` 으로 변경 (장부 파일에 영구 저장, 우선순위:
  /budget > .env LIVE_BUDGET_* > 기본 5만원). 시장별 분리+실현손익 복리
- 장부의 종목 + pending 예약 종목은 워치리스트에서 빠져도 `_ensure_position_symbols`가
  감시를 유지한다 (없으면 자정에 어제 산 종목/예약이 방치 — V로 실제 발생했었음)
- Claude API 크레딧 소진 시 텔레그램 경보 1회/일 (`_llm_error_alert`, 8/27 실발생:
  11시간 조용히 LLM 전기능 정지 — 스카우트 실패+뉴스필터 APIStatusError 훅에 연결).
  크레딧 소진 중에도 매매는 계속 (open-fail: 필터 통과 처리, 스카우트만 정지)
- 실주문 경로: `broker.py` LiveBroker가 유일한 통로 (RiskGuard 이중 검사, 지정가+0.3% 매수/
  매도 미체결 시 시장가 재시도). `toss/client.py`의 place_order를 직접 호출하지 말 것
- 잔고 API는 `/api/v1/assets`가 아니라 **`/api/v1/holdings`** (스펙 확인됨)

## 토스 Open API 핵심 정보
- Base: `https://openapi.tossinvest.com`
- 인증: `POST /oauth2/token` (client_credentials). **재발급 시 이전 토큰 즉시 무효화** → `toss/auth.py`가 `~/.toss_token_cache.json`에 캐시 (expires_in 86400초)
- 시세: `GET /api/v1/prices?symbols=005930,000660` (국내 6자리, 미국 티커, 최대 200개)
- 캔들: `GET /api/v1/candles?symbol=005930&interval=1d|1m&count=200`
- 계좌: `GET /api/v1/accounts` / 잔고: `GET /api/v1/assets` (+ `X-Tossinvest-Account: {accountSeq}` 헤더)
- 주문(추후): 생성/정정/취소 + 조건주문(SINGLE/OCO/OTO), ORDER 그룹 초당 10회
- 웹소켓: `wss://openapi-ws.tossinvest.com/ws/v1` (체결/호가/주문이벤트, 계정당 2연결, 60초 PING 권장)
- Rate limit: AUTH 5/s, ACCOUNT 1/s, MARKET_DATA 15/s, CHART 20/s. 429 시 Retry-After 준수 (client.py에 구현됨)
- IP 화이트리스트: WTS > 설정 > Open API > 허용 IP 관리. 미등록 IP는 403
- 공식 문서: https://openapi.tossinvest.com/openapi-docs/overview.md , OpenAPI 스펙: https://openapi.tossinvest.com/openapi-docs/latest/openapi.json
- 모의투자/샌드박스 없음 → 드라이런 모드로 대체

## 규칙
- `.env`, `~/.toss_token_cache.json`은 절대 커밋 금지
- 주문 코드를 추가할 때는 반드시 안전장치(한도 체크)를 같은 커밋에 포함
- 사용자는 소액 운용이 목표 — 공격적 전략보다 검증과 안전장치 우선
