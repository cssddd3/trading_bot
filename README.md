# toss-trader — 토스증권 소액 자동매매 봇

토스증권 Open API로 국내(KR)+미국(US) 주식을 소액으로 자동매매하는 개인용 봇.
사용자 PC(맥)에서 돌고, 텔레그램으로 알림/원격제어한다.

> ## ⚠️ 투자 책임 고지 (Disclaimer)
> 이 소프트웨어는 **개인 연구·학습 목적**으로 만들어졌으며, 어떠한 투자 수익도 보장하지
> 않습니다. **이 봇을 사용해 발생하는 모든 투자 손실과 결과는 전적으로 사용자 본인의
> 책임입니다.** 제작자는 이 소프트웨어의 사용으로 인한 어떠한 금전적 손실에 대해서도
> 책임지지 않습니다 (MIT 라이선스 — [LICENSE](LICENSE) 참조).
> 자동매매는 버그·네트워크 장애·시장 급변으로 예상치 못한 손실을 낼 수 있습니다.
> **반드시 잃어도 되는 소액으로만 운용하고**, 검증 게이트를 우회하지 마세요.
> 이 문서의 어떤 내용도 투자 자문이 아닙니다.

---

## 1. 아키텍처 한눈에

> 그림으로 보려면: [docs/architecture.html](docs/architecture.html) — 전체 지도·매수 파이프라인·
> 검증 공장(몬테카를로) 다이어그램과 부품별 분업표. 브라우저로 열면 된다.

```
                    ┌──────────────────────────────────────────────┐
                    │                run_dryrun.py                  │
                    │            (러너 — 관제탑, --watch 루프)        │
                    └──┬────────┬─────────┬─────────┬──────────┬───┘
      시세·주문         │        │ 종목선정   │ 뉴스판단  │ 원격제어   │ 공시
  ┌────────────────┐   │   ┌─────────┐ ┌──────────┐ ┌─────────┐ ┌────────┐
  │ toss/client.py │◄──┘   │scout.py │ │llm_filter│ │notify.py│ │dart.py │
  │ (REST API)     │       │(LLM     │ │(LLM      │ │(텔레그램)│ │(전자   │
  │ toss/stream.py │       │ 스카우트) │ │ 거부권)   │ │tg_assist│ │ 공시)  │
  │ (웹소켓 실시간)  │       └─────────┘ └──────────┘ └─────────┘ └────────┘
  └───────┬────────┘
          │ 실주문은 broker.py(LiveBroker)만 통과 — RiskGuard(risk.py) 이중검사
          ▼
   토스증권 Open API (openapi.tossinvest.com)
```

**매수 한 건이 나가는 경로** (전부 통과해야 체결):

1. **종목 선정** — 거래대금 랭킹 → 규칙 필터(레버리지ETF/신규상장/경고종목 제외) → LLM이 후보 안에서 관심종목 선택 → 워치리스트
2. **매수 시그널** — 전략(기본 st=Supertrend)의 **가격 규칙**이 충족될 때만. 뉴스·LLM은 매수 시그널을 만들지 못한다
3. **최종 관문** — RiskGuard 한도 검사(예산/일일손실/횟수/쿨다운) + LLM 뉴스 거부권(악재면 차단)

**대원칙**: LLM은 해석만, 결정은 규칙. 백테스트 검증 게이트(OOS 30건+, 기대값 양수,
몬테카를로 손실확률 30% 미만)를 통과한 전략만 실돈을 쓴다.

### 안전장치 목록

| 장치 | 내용 |
|---|---|
| 이중 잠금 | `--live` 플래그 + `.env LIVE_TRADING=1` 둘 다 있어야 실주문 |
| 검증 게이트 | `logs/strategy_validation.json` passed=true인 전략만 live 기동 |
| 예산 상한 | 시장별(KR/US) 분리, 실현손익 복리. 계좌에 돈이 더 있어도 초과 사용 불가 |
| 기존 보유 보호 | 봇 장부에 없는 종목은 절대 매도 불가 (`--adopt`로 명시 편입한 것만 관리) |
| 거래소측 손절 | 매수 즉시 조건부주문(스탑) 등록 — 봇이 죽어도 방어선 유지 |
| 대사(reconcile) | 기동 시+1시간마다 장부↔실계좌 비교, 원인불명 불일치면 매매 정지 |
| 킬 스위치 | 텔레그램 `/stop`(정지) `/flat`(전량청산) |
| 단일 인스턴스 | flock 잠금 — 봇 2개가 동시에 도는 사고 방지 |

---

## 2. 새 컴퓨터에 설치하기

### 2-1. 필요한 것

- macOS (launchd 자동재시작 사용 시) 또는 리눅스, Python 3.11+
- 토스증권 계좌 + Open API 키 (아래 발급법)
- (선택) Anthropic API 키 — LLM 스카우트/뉴스필터. 없으면 규칙만으로 동작
- (선택) 텔레그램 봇 토큰 — 알림/원격제어. 없으면 콘솔 로그만
- (선택) DART API 키 — 전자공시 실시간 알림. 없으면 그 기능만 꺼짐

### 2-2. 키 발급 방법

**① 토스증권 Open API** (필수)
1. 토스증권 WTS(PC 웹) 접속 → 설정 → **Open API** 메뉴
2. 앱 등록 → `CLIENT_ID` / `CLIENT_SECRET` 발급
3. **허용 IP 등록 필수**: 설정 > Open API > 허용 IP 관리에 봇 돌릴 컴퓨터의 공인 IP 추가
   (미등록 IP는 403. 집 IP가 바뀌면 여기서 갱신해야 한다)
4. 문서: https://openapi.tossinvest.com/openapi-docs/overview.md

**② Anthropic API** (선택 — LLM 기능)
1. https://console.anthropic.com → API Keys → 발급
2. 유료 크레딧 필요. 역할별 모델 티어링(스카우트 Sonnet, 필터/비서 Haiku)과
   15분 스로틀로 하루 수백~수천 원 수준. 크레딧 소진 시 텔레그램 🚨 경보가 오고,
   매매는 계속되며 LLM 감시망만 꺼진다 (open-fail)

**③ 텔레그램 봇** (선택 — 강력 추천)
1. 텔레그램에서 `@BotFather` 검색 → `/newbot` → 봇 이름 지정 → **토큰** 발급
2. 만든 봇에게 아무 메시지 1개 전송 (chat_id 확보용)
3. `https://api.telegram.org/bot<토큰>/getUpdates` 열어서 `"chat":{"id":숫자}` 확인 → 그 숫자가 `TELEGRAM_CHAT_ID`
4. ⚠️ 봇은 이 chat_id의 명령만 듣는다 (타인이 봇을 찾아도 제어 불가)

**④ DART 전자공시** (선택)
1. https://opendart.fss.or.kr → 회원가입 → 인증키 신청 (무료, 즉시 발급)
2. 감시/보유 종목의 새 공시를 기사화 전에 텔레그램으로 받는다

### 2-3. 설치

```bash
git clone <이 저장소> toss-trader && cd toss-trader   # 또는 폴더 통째로 복사
pip3 install -r requirements.txt
cp .env.example .env
# .env 열어서 키 채우기 (아래 참고)
```

`.env` 내용:

```ini
TOSS_CLIENT_ID=발급받은_ID
TOSS_CLIENT_SECRET=발급받은_시크릿
LIVE_TRADING=0            # 실전은 1 (드라이런으로 먼저 확인 권장)
LIVE_BUDGET_KR=50000      # 시장별 초기 예산(원) — 이후 텔레그램 /budget으로 변경 가능
LIVE_BUDGET_US=50000
ANTHROPIC_API_KEY=        # 선택
TELEGRAM_BOT_TOKEN=       # 선택
TELEGRAM_CHAT_ID=         # 선택
DART_API_KEY=             # 선택
```

⚠️ `.env`와 `~/.toss_token_cache.json`은 절대 공유/커밋 금지.
⚠️ **같은 API 키를 두 컴퓨터에서 동시에 쓰면 안 된다** — 토큰 재발급 시 이전 토큰이
즉시 무효화되어 서로 죽인다. 지인에게 줄 때는 **본인 계좌·본인 키**로 새로 발급받게 할 것.

### 2-4. 검증 게이트 통과 (실전 전 필수, 컴퓨터마다 1회)

```bash
python3 run_backtest.py -t st --validate
```

`logs/strategy_validation.json`에 passed=true가 기록돼야 `--live`가 기동한다.
(우회는 `.env LIVE_VALIDATION_OVERRIDE=1` — 권장하지 않음)

### 2-5. 실행

```bash
./start.sh          # 드라이런 (가상 체결 — 먼저 이걸로 관찰 권장)
./start.sh live     # 실전 (.env LIVE_TRADING=1 필요)
./stop.sh           # 정지
tail -f logs/watch.log   # 로그 확인
```

`start.sh`는 caffeinate(잠자기 방지)+nohup으로 돌린다. 맥 덮개를 닫아도 전원이 연결돼
있으면 계속 돈다. 재부팅 후 자동 시작을 원하면:

```bash
cp launchd/com.tosstrader.live.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.tosstrader.live.plist
```

---

## 3. 운영 방법

### 웹 대시보드 (읽기 전용)

봇이 켜져 있으면 내장 대시보드가 함께 뜬다 — 보유·평가손익·예약·감시종목·최근 체결을
10초마다 갱신해 보여준다. 제어 기능은 없다 (제어는 텔레그램만).

- 봇 돌리는 컴퓨터에서: http://localhost:8787
- 같은 와이파이의 폰에서: `http://<컴퓨터IP>:8787` (맥 IP는 시스템 설정 > Wi-Fi에서 확인)
- 포트 변경: `config.py`의 `DASHBOARD`

### 텔레그램

| 명령 | 동작 |
|---|---|
| `/status` | 보유/예산/오늘 성적 요약 |
| `/stop` | 신규 매수 정지 (보유분 관리는 계속) |
| `/resume` | 재개 |
| `/flat` | 전량 청산 (킬 스위치) |
| `/budget KR 100000` | KR 예산을 10만원으로 (장부에 영구 저장, US도 동일) |
| `/watch 005930` | 종목 수동 감시 추가 |
| `/unwatch 005930` | 수동 감시 해제 |
| (자유 질문) | "/" 없는 메시지는 LLM 비서가 답변 (읽기 전용). 대화가 이어진다 — "아까 그 종목 왜 샀어?" 같은 후속 질문 가능 (최근 10문답 기억, 재시작에도 유지) |

`/status`는 보유(실시간 평가손익·스탑 포함), 예약 주문, 감시 종목, 예산, 리스크 카운터,
기능 상태(시세/공시/LLM)를 한 번에 보여준다. 하트비트에도 평가손익·예약 건수가 실린다.

자동으로 오는 알림: 매수/매도 체결, 매수 차단 사유, 악재 경보, 새 공시(📢),
1시간 하트비트(안 오면 봇 사망 = 전원/네트워크 확인), 마감 리포트, 크래시.

**돈을 더 넣었을 때**: 계좌 입금 → `/budget KR 200000` 처럼 상향 → 끝. 재시작 불필요.

**지인과 공유하기 (텔레그램 채널)**: 텔레그램에서 새 채널 생성 → 봇을 관리자로 추가 →
`.env`에 `TELEGRAM_BROADCAST_CHAT_ID=@채널이름` 추가 → 재시작. 이후 스카우트 픽(선정 근거),
매수/매도(종목·가격·수익률), 새 공시가 채널로도 나간다. 지인은 채널만 구독하면 된다.
계좌 수치(예수금·예산·수량)는 채널에 나가지 않고, 봇 제어는 여전히 주인 개인 챗만 가능.
⚠️ 소수 지인과의 무료 공유 용도 — 불특정 다수 대상 유료 리딩은 유사투자자문업 규제 대상이
될 수 있으니 범위를 넓히기 전에 전문가와 상의할 것.

**직접 산 종목을 봇에 맡기기**:

```bash
python3 run_dryrun.py --adopt 005930,000660 --live
```

명시한 종목만 편입되고, 나머지 기존 보유분은 봇이 절대 건드리지 않는다.

---

## 4. 전략 운영법

### 현재 기본 전략: st (Supertrend 추세추종)

- **일봉 기반 저회전** — 추세가 서면 올라타고 밴드 이탈 시 청산. 몇 주 보유가 정상,
  매매는 월 수 회 수준. "왜 오늘 아무것도 안 사?"가 정상 동작이다
- 검증 성적: OOS 50건 +7.35%/건, 몬테카를로 손실확률 0.7% (통과)
- 참고: 급등 추격형은 2번 검증해서 2번 다 **기각**됨 —
  변동성돌파(vb): OOS -0.96%/건, 손실확률 100% /
  뉴스모멘텀(nm, 분봉 급등 올라타기): OOS -0.66%/건, 승률 33%, 손실확률 97.8%
  (`research/newsmom_result.json`). "호재 보고 바로 사기"가 금지인 것은 취향이 아니라
  데이터의 결론이다

### 전략 검증/변경 절차

```bash
python3 run_backtest.py -t st            # 백테스트
python3 run_backtest.py -t st --mc       # + 몬테카를로 강건성
python3 run_backtest.py -t st --validate # 검증 게이트 기록 (live 기동 조건)
```

전략 추가는 `strategy/base.py`의 on_open/on_close 인터페이스 구현 →
백테스트와 실전이 **같은 코드**를 호출한다 (미래참조 방지).
`config.py`의 `DEFAULT_STRATEGY`로 교체하되, 반드시 `--validate` 통과 후에.

### 전환 스캐너 (2026-08-31 승격)

매일 15:20(동시호가) 거래대금 상위 100 전체를 스캔해 "오늘 추세 상승 전환"한 종목을
잡고, 하루 최대 2건을 **다음날 시가 매수 예약**한다 (1건당 시장 예산의 50% 이내 분할 —
이 분할 조건으로 4년 검증을 통과했으므로 변경 금지). 뉴스 거부권·리스크 한도는 동일 적용.
기록은 `logs/scanner_shadow.csv`.

### 뉴스·LLM의 역할 (매수 트리거 아님)

- 스카우트: 뉴스를 읽고 **어떤 종목을 지켜볼지** 고른다 (후보군은 규칙이 생성)
- 뉴스필터: 매수 직전 악재면 **차단** (거부권)
- 뉴스모니터: 보유 종목에 치명 악재(거래정지/상폐/분식/횡령 헤드라인 3건+)면 자동 청산,
  일반 악재는 텔레그램 경보만 (판단은 사람)
- DART 공시: 알림 + 뉴스 재평가 트리거

### 반응 속도

- 시세: 웹소켓 실시간 (`toss/stream.py`), 끊기면 REST 30초 폴백
- 뉴스: 구글뉴스 검색 + 인베스팅닷컴 공개 RSS 병합 (분 단위 신선)
- 공시: DART 2분 폴링 (기사보다 빠른 원천)
- 스카우트 후보 확인: 3분 주기 (새 후보 등장 시에만 LLM 실행)

---

## 5. 파일 지도

```
config.py            모든 설정 (예산/리스크/전략/LLM/스트림) — 숫자는 여기서만 바꾼다
run_dryrun.py        러너 (드라이런+실전 공용) — --watch 무한루프
run_backtest.py      백테스트 + --validate 검증 게이트
broker.py            실주문 유일 통로 (LiveBroker + 거래소측 스탑 관리)
risk.py              RiskGuard 한도 검사 (일자 경계 06:00 KST)
scout.py             LLM 종목 스카우트 → logs/watchlist.json
llm_filter.py        LLM 뉴스 거부권 (판정 캐시 logs/news_verdicts.json)
news.py              헤드라인 수집 (구글뉴스+인베스팅 RSS)
dart.py              DART 전자공시 모니터
notify.py            텔레그램 송수신 / tg_assistant.py 자유질문 LLM 비서
dashboard.py         웹 대시보드 (localhost:8787, 읽기 전용)
toss/                API 클라이언트 (client.py REST / stream.py 웹소켓 / auth.py 토큰캐시)
strategy/            전략 구현 (base.py 인터페이스)
backtest/            백테스트 엔진 + 몬테카를로
research/            감사 보고서, 전략 연구 (newsmom 등)
logs/live_state.json 실전 장부 (포지션/예산/실현손익) — 백업 대상
logs/watch.log       메인 로그
```

## 6. 문제 해결

| 증상 | 원인/조치 |
|---|---|
| 403 에러 | IP 화이트리스트 미등록 — WTS에서 현재 IP 추가 |
| 401/토큰 에러 반복 | 다른 컴퓨터가 같은 키로 토큰 재발급 중 — 키를 한 곳에서만 사용 |
| 하트비트 끊김 | 봇 사망 — `tail logs/watch.log`로 크래시 확인 후 `./start.sh live` |
| "halted" 알림 | 장부↔계좌 불일치 — 수동 매매가 봇 장부와 겹쳤는지 확인 후 재시작 |
| 매수가 안 나감 | 정상일 확률 높음 (st는 저회전) — 차단 알림에 사유가 명시됨 |
| 🚨 크레딧 소진 알림 | Claude API 잔액 소진 — LLM 감시망만 정지, 매매는 계속. console.anthropic.com에서 충전 |
| 기동 거부 (검증) | `python3 run_backtest.py -t st --validate` 재실행 |
```
