# toss-trader — Claude Code 인수인계 문서

토스증권 Open API 기반 국내(KR)+미국(US) 주식 소액 자동매매 봇. 사용자 PC에서 `--watch`
무한루프로 상주. 사람용 설치·운영 가이드는 [README.md](README.md), 이 파일은 **개발 컨텍스트**다.

## 0. 대원칙 (코드보다 우선하는 규칙)

1. **LLM은 해석만, 결정은 규칙.** LLM(스카우트/뉴스필터/비서)은 감시 대상 추천과 매수 거부권만
   갖는다. 매수 방아쇠는 백테스트 검증을 통과한 가격 규칙만 당긴다.
2. **검증 게이트 없이는 실돈 없음.** 기준: OOS 거래 ≥30건, OOS 기대값 >0, 몬테카를로(3000회)
   손실확률 <30%. 어떤 전략/유니버스 변경도 이 게이트를 통과해야 실매수. 커트라인 완화 금지.
3. **기존 보유 불가침.** 봇은 자기 장부의 종목만 판다. 사용자가 직접 산 주식은 `--adopt`로
   명시 편입한 것 외엔 절대 건드리지 않는다.
4. **예산 상한.** 시장별(KR/US) 예산 분리, 실현손익 복리. 계좌에 돈이 더 있어도 초과 사용 불가.
5. **open-fail.** 모든 부가 기능(LLM/뉴스/공시/스트림/대시보드)은 죽어도 매매를 막지 않는다.
   단, 조용히 죽으면 안 되는 것은 텔레그램 경보를 보낸다 (크레딧 소진 등).
6. 주문 코드를 추가할 때는 안전장치(한도 체크)를 **같은 커밋**에 포함한다.
7. `.env`, `~/.toss_token_cache.json` 절대 커밋 금지. logs/·data/ 도 gitignore (계좌기록/캐시).
8. 기능 변경 시 문서를 **같은 턴에** 갱신한다: README.md(랜딩) + docs/setup·operation·strategy.md
   (사람용, 서로 링크) + 이 파일(개발용). 다이어그램은 mermaid (아스키 박스는 한글 폭으로 깨짐).

## 1. 아키텍처

```
run_dryrun.py (러너 = 관제탑, 드라이런/실전 공용)
 ├─ watch() 무한루프 → tick() 장중 8초(스트림 연결 시)/30초
 │   tick(): 세션판정 → 텔레그램 명령 → 하트비트 → 대사 → 대시보드 스냅샷
 │           → 스카우트(PRE 1회+장중 갱신) → DART 폴링 → 전환스캐너(15:20+)
 │           → 뉴스모니터 → 심볼별 process()
 ├─ process(심볼): 예약(pending) 체결 → 전략 on_open(장중 스탑/조건주문)
 │                → 전략 on_close(종가 판단, CLOSING_AUCTION/AFTER 1회)
 ├─ Portfolio: 장부 (positions/pending/예산/실현손익) — logs/live_state.json 원자적 저장
 └─ 내장 데몬 스레드: dashboard(웹), toss/stream(웹소켓 시세)

전략 strategy/ (base.py 인터페이스: on_open/on_close)
 └─ 백테스트 엔진(backtest/engine.py)과 러너가 **같은 메서드** 호출 (미래참조 방지 구조)

실주문은 broker.py LiveBroker가 유일한 통로 (RiskGuard 이중 검사).
toss/client.py의 place_order를 직접 호출하지 말 것.
```

파일 지도: README §5 참조. 상태 파일:
- `logs/live_state.json` 실전 장부 (positions/pending/budget_base/realized_pnl/tg_offset/manual_watch).
  손상 시 live 기동 거부. 테스트는 절대 이 파일을 건드리면 안 됨
- `logs/risk_state.json` 일일 카운터 (risk_day = KST-6h 날짜 경계, 미장 자정 분절 방지)
- `logs/watchlist.json` 오늘의 스카우트 픽 / `logs/news_verdicts.json` 뉴스 판정 캐시
- `logs/strategy_validation.json` 검증 게이트 기록 — passed=true 없으면 --live 기동 거부
- `logs/dashboard_live.json`(+_dryrun) 대시보드 스냅샷 / `logs/scanner_shadow.csv` 스캐너 기록
- `logs/tg_history.json` 비서 대화 기억 (10문답/8천자)
- `logs/scout_picks.csv` AI 추천 원장 (date,symbol,name,price,thesis — _log_scout_picks가
  신규 픽만 기록, 마감 리포트의 7일 성적표 _scout_scorecard 근거. 9-08 도입 — 이후부터 축적)

안전장치 체인 (매수 1건): 후보 규칙필터 → LLM 선정(감시 추가만) → 전략 가격 시그널
→ RiskGuard(예산/일일손실/횟수/쿨다운/경고종목) → LLM 뉴스 거부권 → 주문 → 즉시 손절 등록.
그 외: --live+LIVE_TRADING=1 이중 잠금, TT_LIVE_INTENT(내부 플래그, main()만 세팅 —
테스트가 실계좌 만지는 사고 방지), flock 단일 인스턴스, 기동+1시간 대사(불일치 시 halted),
텔레그램 킬스위치(/stop /flat).

## 2. 개발 수칙 + 실사고 사례집 (전부 실제로 겪은 것)

- **자동 치환(sed/replace) 패치 금지에 가깝게 신중히.** 2026-09-01: str.replace 앵커 불일치가
  조용히 no-op → DART·스캐너 훅이 tick에 연결 안 된 채 "켜짐" 로그만 찍힘. 반드시 Edit 도구
  (불일치 시 실패) 사용 + 연결 후 **호출 검증 테스트** (tick을 세션 위장으로 돌려 훅 호출 확인)
- **테스트가 프로덕션을 오염시킨 사고 2회**: mock 테스트가 live_signals.csv에 가짜 행 기록 /
  가드 테스트가 실계좌에 진짜 조건주문 등록. → 테스트는 dryrun 상태 파일만, 끝나면 삭제.
  대시보드 스냅샷도 모드별 분리(dashboard_live vs _dryrun)가 그 재발 방지책
- tick()의 sessions 값은 `(세션문자열, info)` **튜플**. watch()에선 문자열로 변환됨 — 혼동 주의
- 조건주문 list는 `status="OPEN"` 파라미터 필수 — 누락 시 400→빈목록→중복 등록 (실계좌에
  스탑 6개 중복됐던 사고). _sync_exchange_stop이 '심볼당 정확히 1개' 보장
- 미국 소수점 주문은 정규장 마감 1시간 전(04:00 KST)까지만 접수 → US 주문 가능 구간을
  22:30~04:00으로 재정의 (03:50~04:00 종가판단, 04:00 이후 매도는 pending 전환).
  소수점 수량은 거래소 조건주문도 거부됨 → 봇 내 밴드 스탑이 유일한 방어선
- 거래소 백스톱 트리거 = min(평단×(1-8%), 밴드×0.97) — **밴드보다 아래인 재해용**.
  실질 손절선은 전략 밴드이고 전환 직후엔 -20~40% 아래일 수 있음
- pending(다음 시가 예약)이 예수금 부족(transient)으로 불발되면 삭제하지 말고 10분 백오프
  재시도 (예약 유실로 매수 놓친 실사고). pending 종목은 _ensure_position_symbols가 감시 유지
- 매수 체결 즉시 전략 밴드로 초기 손절선 세팅 (첫 종가판정까지 무방비 구간 방지)
- KR 종가청산은 broker.sell_at_close — 동시호가 중 취소 금지, 15:30 매칭 대기
- 잔고 API는 /api/v1/assets가 아니라 **/api/v1/holdings**
- 뉴스 자동청산은 치명 플래그(거래정지/상폐/분식/횡령)+헤드라인 3건 이상만. 감성 악재는 경보만
- Claude 크레딧 소진은 하루 1회 텔레그램 경보 (11시간 조용히 LLM 정지했던 실사고).
  소진 중에도 매매는 계속 (필터는 통과 처리)
- 대시보드/외부 도구는 토스 API를 직접 부르지 말 것 — 토큰 캐시 공유 시 재발급 경합
  (재발급하면 이전 토큰 즉시 무효). 러너가 쓴 스냅샷 파일만 읽기
- **9-09 HD현대 실사고**: 스캐너 픽 1주 값(25.5만)이 분할 상한(예산 50%≈25.4만)을 초과 →
  수량 0으로 하루 종일 10분마다 불발 + "예수금 부족(입금하면 가능)" 오진 안내. 수리:
  ① 스캐너가 예약 전에 고가 종목 제외+알림, ② virtual_buy 거부 사유가 실제 걸린 상한
  (분할 상한/현금버퍼 5%/예수금)을 정확히 표시, 분할 상한 초과는 permanent(예약 삭제).
  분할 상한 자체를 늘리는 것은 금지 (승격 전제)
- 매수 주문 예외/UNKNOWN = 재시도 금지 → halt+경보 (이중 매수 방지, 9-07). 체결인데 평단
  0/누락 → 재조회 후 UNKNOWN 승격. 조건주문 취소는 봇 소유 id 한정 (사용자 스탑 불가침).
  동시호가 잠정 전환의 NEXT_OPEN 예약은 AFTER(종가 확정) 후에만 확정.
  검수 잔여(2차): 대사 오분류(E1/E2), /flat 미완료 통지, 토큰 401 복구, 웹소켓 이상틱 교차확인

## 3. 전략·검증 현황

**가동 중**
- `st` Supertrend(ATR10×3, EMA200 필터, RSI<75) — 기본 전략. 검증: OOS 50건 +7.35%/건,
  MC 손실확률 0.7% ✅. 저회전(월 수 회)이 정상. 전환일 종가 확인 → 다음날 시가 진입,
  청산은 밴드 이탈/하락 전환. `run_backtest.py -t st --validate`로 게이트 기록 생성
- **전환 스캐너** (2026-08-31 승격 → 9-02 유니버스 200 확대, config.SCANNER):
  매일 15:20+ **60일 평균 거래대금 상위 200**에서 '오늘 전환'을 하루 최대 2건 다음날 시가
  매수 예약. ⚠️ 유니버스 정의는 60일 평균 (당일 거래대금 아님 — 당일 기준 spike는 기각 유니버스!).
  근거: 4년 OOS 423건 +2.49%/건, 1/2 분할 MC 손실확률 10.7% ✅ (상위 100은 13.6%).
  position_frac=0.5 분할이 승격 전제 — 제거 금지. 랭킹 API가 최대 100까지만 줘서
  유니버스 = 시드(data/scanner/universe_seed.json, 전 종목 캐시로 생성한 60일 평균 상위 300)
  ∪ 당일 랭킹 100 → 봉 데이터로 60일 평균 재랭킹 → 상위 200. 시드는 월 1회쯤 재생성 권장.
  첫 스캔은 봉 수집으로 ~4분, 이후 캐시로 ~1분. 장중 상시 스캔 안 하는 이유:
  신호가 일봉 종가 확정 기준 (미완성 캔들 = 가짜 신호).
  **바닥권 필터** (9-03, 사용자 질문에서 출발한 가설 검증): 과거 250일 고점의 70% 미만
  위치에서 뜨는 전환은 IS/OOS 모두 일관 손실(-3.7/-4.8%/건, 승률 14~20% — 데드캣) → 제외.
  적용 후 OOS +4.39%/건, 1/2분할 MC 4.1%. 반대로 '전고점 부근 진입'은 데이터상 문제없음
  (전고점권 OOS +4.1%/건) — 위치 개념 없는 Supertrend의 유일한 검증된 위치 필터

**기각 이력 (재탕 금지 — 다중검정 카운트에 포함할 것)**
| 전략 | 결과 | 기제 |
|---|---|---|
| vb 변동성돌파 | OOS -0.96%/건, MC 100% | 급등 추격 = 꼭지 매수 |
| nm 뉴스모멘텀(분봉 급등 올라타기) | OOS -0.66%/건, MC 97.8% | 〃 (research/newsmom_backtest.py) |
| 공시 눌림목 (지인 제안) | OOS -1.62%/건, 승률 8% | 공급계약 팝 후 눌림 = 되돌림 시작 (pullback_backtest.py) |
| 스캐너 spike 유니버스(당일 거래대금) | OOS -4.5%/건, MC 100% | 당일 상위 = 어제 폭등 테마주 필터 |
| KODEX 인버스에 st (숏 대용) | IS -2.57%/건, OOS 0건 | 인버스는 구조적으로 녹는 자산 + 필터가 진입 차단 |
| 고정 익절 목표가 (문헌 기각) | Oxfordstrat 42선물 재현 순손실 | 추세계 이익의 원천인 우측 꼬리를 절단 |
| LLM 자율 매매 에이전트 (문헌 기각) | FINSABER: FinMem +23%→-22%, StockBench p>0.34 | 티커 기억 오염이 알파의 정체 (익명화하면 소멸) |

**연구 프로토콜 추가 (9-09)**: 모든 A/B 판독에 ①OOS 건수 ②기대값 ③maxDD ④MC ⑤**상위
5거래 손익 보존율** (필터가 대박 1건을 자르면 나머지가 다 좋아 보여도 기대값이 뒤집힘) +
기각 원장에 **트라이얼 카운터** (다중검정 보정용, Bailey & López de Prado SSRN 2460551).
LLM 판정의 과거 데이터 백테스트는 구조적 무효 (종목명/날짜 기억 오염) — LLM 피처는
forward-log(news_verdicts/scout_picks처럼 실시간 축적)로만 후보 가능.
9-09 리서치 트라이얼 대기열: 이슈 #13(출구 래칫) #14(변동성 사이징) #15(타임스탑)
#16(메타라벨링) #2(체제 게이트 구체화 코멘트) — 판 전체 상세는 이슈 본문이 정본.

**미검증 후보 대기열**: ① 스캐너 당일 종가 매수 vs 다음날 시가 비교, ② DART 유형별 익일
드리프트(자사주취득·임원 장내매수 — 학술 근거 있음, 일봉로 검증 가능), ③ 과매도 반등(평균회귀,
횡보장용). 백테스트 인프라: research/ 의 기존 스크립트 재사용 (유니버스는 생존편향 없이
'그날의' 랭킹으로 자체 구축 — data/newsmom/daily 120봉 캐시, data/scanner/daily1000 4년봉 83%)

**연구 방법론**: IS/OOS 시계열 분리(커닝 금지), 그리드는 IS에서만, 체결 가정 보수적
(수수료 0.015%+거래세 0.15%+슬리피지+호가단위+갭 반영), 시뮬에 실전 손절 구조 반영 필수.
공시 시각은 DART list에 없음 → KIND(kind.krx.co.kr)에서 확보, 정정공시만 제외(원본 유지 =
look-ahead 방지). LLM에 과거 종목명·날짜를 주면 백테스트 오염(사전지식) 주의

## 4. 서브시스템 메모

- **공유 두뇌** (brain.py, 9-09 "LLM과 알고리즘이 한 사람처럼" 사용자 지시): 모든 LLM 역할이
  같은 운용 일지(logs/brain_journal.json, 60건/15k자 롤링)를 읽고 쓴다.
  사이클: 아침 스카우트가 일지를 읽음(ask_claude에 digest 주입) → 매매가 일지 기록
  (virtual_buy/sell) → 매수 근거가 뉴스 거부권에 전달(news.check context=) → 비서도 같은
  일지를 읽음 → 마감 후 _evening_review가 하루 평가+가설을 일지에 남김(🧠 텔레그램).
  가설은 logs/hypotheses.md 적재 — **게이트 통과 전 실전 반영 금지** (대원칙 1·2 불변:
  일지는 기억일 뿐 매수 방아쇠 아님). brain.ask()는 공용 텍스트 LLM 헬퍼 (open-fail)
- **LLM 티어링** (config.LLM_MODELS): 스카우트=sonnet-5(15분 스로틀), 뉴스필터/비서=haiku-4-5.
  effort 파라미터는 haiku에서 400 에러 → config.output_config_for()가 처리.
  스로틀 없인 랭킹 순환으로 하루 150회+ 호출 실측됨
- **스카우트**: 랭킹 상위 100 풀(1콜) → 규칙 필터(레버리지/인버스·신규상장 60일·경고 제외)
  → LLM에 20개 제시 → 최대 3픽/시장. '새 후보' 트리거는 상위 30만(하위권 순환 소음).
  픽은 감시 추가만 — 매수는 전략 시그널로만
- **뉴스**: 구글뉴스 검색(넓고 느림) + 인베스팅닷컴 공개 RSS(분 단위, 종목명 토큰 매칭) 병합.
  인베스팅 Pro 구독은 API가 없어 연동 불가. DART 2분 폴링(기사보다 빠름) — 알림+재평가만
- **시세**: toss/stream.py 웹소켓(declarative full-replace 구독, LOSSY, 계정당 2연결,
  100구독/연결). last_price() 우선순위: 스트림(10초 신선도) → 틱 배치 맵(_refresh_prices,
  틱당 1콜·90초 신선도, 9-02 검수 효율 수리 — 이전엔 틱당 25~30콜) → 개별 REST
- **텔레그램**: /stop /resume /flat /restart /status /budget /watch /unwatch + 자유질문(비서, 대화 기억).
  /restart는 save 후 os.execv 자기재실행 (새 코드 반영 — 사용자 UX 개선 3종 중 하나, 9-08:
  ① 마감 리포트가 무거래 사유·내일 예약·평가손익 합계를 선제 설명, ② /restart 원터치,
  ③ _drawdown_alert 급락 브리핑 — 당일 평가손익이 기준선 대비 max(투자금 3%, 1만원) 악화 시
  하루 1회 워스트 3종목+손절선 거리 통지).
  비서 컨텍스트(tg_assistant.build_context)에 pending 예약·스캐너 CSV 포함 (9-08: HD현대 예약을
  비서가 부인한 동문서답 사고 수리 — 새 상태를 추가하면 비서 컨텍스트에도 넣을 것).
  /status는 _status_text()로 분리(테스트 가능) — 수익률순 정렬, 포지션별 entry_reason/entry_src
  (virtual_buy가 저장, 9-08 이전 포지션은 없어서 'Supertrend 상승 전환' 폴백), AI 시황 메모
  (_market_note = watchlist.json market_note, 당일 것만) 포함.
  chat_id 게이트. 공유 채널 notify.broadcast() — 발신 전용, 계좌 수치 제외
- **대시보드**: dashboard.py 러너 내장 스레드 :8787, 읽기 전용, 스냅샷 파일만 서빙.
  포트 점유 시 60초마다 재시도 (좀비 프로세스 실사고)
- **무인 운용**: ./start.sh live (caffeinate+nohup, 5MB 로그 로테이션) / ./stop.sh /
  launchd plist(자동재시작, 경로는 플레이스홀더 — 사용자별 수정)

## 5. 토스 Open API 핵심

- Base `https://openapi.tossinvest.com` / OAuth client_credentials —
  **재발급 시 이전 토큰 즉시 무효** → toss/auth.py가 ~/.toss_token_cache.json 캐시 (86400s).
  같은 키를 두 곳에서 쓰면 서로 죽인다
- 시세 `GET /api/v1/prices?symbols=` (최대 200) / 캔들 `GET /api/v1/candles` (count≤200,
  before/nextBefore 페이지네이션, adjusted=true 필수) / 잔고 `GET /api/v1/holdings`
  / 가용금 get_buying_power(currency) / 랭킹 get_rankings / 환율 exchange-rate
  (baseCurrency·quoteCurrency 필수) / 캘린더 market-calendar/KR(integrated)·US(regularMarket,
  세션이 KST 자정 넘음 — previousBusinessDay도 봐야 함)
- 주문: KR 지정가+0.3% 매수(미체결 시 시장가 재시도) / US 매수는 orderAmount 시장가(소수점),
  매도는 시장가. 조건주문 SINGLE/MARKET(스탑), list엔 status=OPEN 필수
- 웹소켓 `wss://openapi-ws.tossinvest.com/ws/v1` (Bearer 헤더, 180초 무수신 종료 → ping 50s)
- Rate limit: AUTH 5/s, ACCOUNT 1/s, MARKET_DATA 15/s, CHART 20/s. 429 시 Retry-After 준수
  (client.py 구현). IP 화이트리스트 미등록 = 403
- 문서: https://openapi.tossinvest.com/openapi-docs/overview.md (모의투자 없음 → 드라이런으로 대체)

## 6. 장기 로드맵 — 프레임워크화 (2026-09-08 사용자 지시)

봇을 **그래프 조립형 프레임워크**로 발전시킨다 (노드 조립 → YAML 정의 → 비주얼 에디터,
컴맹도 자기 봇 제작 가능). 설계·헌법·단계(P0~P4)는 [docs/framework-vision.md](docs/framework-vision.md).
**지금 할 일은 P0 하나**: 새 코드를 쓸 때 노드 경계(데이터소스/AI해석/신호/리스크/집행/알림)를
침범하지 않게 유지한다 — 예: AI 코드에 매수 로직 넣지 않기, broker 외 주문 경로 만들지 않기
(이미 대원칙과 일치). P1(YAML 로더)부터는 사용자와 시점 합의 후 착수. 실운용과 병행 원칙.

**저장소 가드레일** (9-08): secret scanning+push protection, Dependabot, CodeQL, main
강제푸시·삭제 금지, 비공개 취약점 제보 ON. PR CI(.github/workflows/pr-check.yml)가 컴파일·
비밀파일·게이트 우회(strategy_validation 쓰기)·broker 외 place_order·의존성 취약점을 차단.
PR 머지는 소유자만 — 머지 전 broker.py/toss/·.github 변경은 반드시 직접 검토.

**계획은 GitHub에 공개 게시** (사용자 지시): 새 계획·연구 후보·검수 항목이 생기면 이슈로
(라벨: roadmap/strategy-research/audit/global), 큰 방향은 Discussions로. 로드맵 허브는
Discussion #11. 완료 시 이슈 닫고, 기각된 전략도 근거와 함께 이슈에 기록 (재탕 방지 공개 원장).
국외 확장 기틀은 이슈 #6 + framework-vision.md '국외 확장' 절 참조.

## 7. 사용자(운용자) 컨텍스트

- 소액 실험 계정 (예산 KR/US 각 50만원 수준). 공격적 전략보다 검증·안전장치 우선이 방침이나,
  거래 빈도가 너무 낮은 것에 대한 불만이 반복됨 — 해소는 항상 "검증 통과한 엔진 추가"로
- 5분 넘는 작업은 시작 전에 소요시간 고지+동의 (30분 수집을 중단시킨 적 있음)
- 재시작(./stop.sh && ./start.sh live)은 사용자가 누르는 것이 기본 (명시 요청 시 대행 가능)
- 텔레그램 지인 공유 채널 운영 중일 수 있음 — 계좌 수치는 채널에 내보내지 않는 것 유지
