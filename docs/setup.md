# 설치 가이드

[← README](../README.md) · [운영 가이드](operation.md) · [전략 가이드](strategy.md)

## 1. 필요한 것

- macOS / 리눅스 / **윈도우** 모두 지원, Python 3.11+
  (윈도우는 [python.org](https://www.python.org/downloads/)에서 설치 시
  **"Add python.exe to PATH" 체크 필수**)
- 토스증권 계좌 + Open API 키 (아래 발급법)
- (선택) Anthropic API 키 — AI 스카우트/뉴스필터. 없으면 규칙만으로 동작
- (선택) 텔레그램 봇 토큰 — 알림/원격제어. 없으면 콘솔 로그만
- (선택) DART API 키 — 전자공시 실시간 알림. 없으면 그 기능만 꺼짐

## 2. 키 발급 방법

### ① 토스증권 Open API (필수)

1. 토스증권 WTS(PC 웹) 접속 → 설정 → **Open API** 메뉴
2. 앱 등록 → `CLIENT_ID` / `CLIENT_SECRET` 발급
3. **허용 IP 등록 필수**: 설정 > Open API > 허용 IP 관리에 봇 돌릴 컴퓨터의 공인 IP 추가
   (미등록 IP는 403 에러. 집 IP가 바뀌면 여기서 갱신)
4. 공식 문서: https://openapi.tossinvest.com/openapi-docs/overview.md

> ⚠️ **같은 API 키를 두 컴퓨터에서 동시에 쓰면 안 된다** — 토큰 재발급 시 이전 토큰이
> 즉시 무효화되어 서로 죽인다. 각자 **본인 계좌·본인 키**로 발급받을 것.

### ② Anthropic API (선택 — AI 기능)

1. https://console.anthropic.com → API Keys → 발급
2. 유료 크레딧 필요. 역할별 모델 티어링(스카우트 Sonnet, 필터/비서 Haiku)과 15분 스로틀로
   하루 수백~수천 원 수준. 크레딧이 떨어지면 텔레그램 🚨 경보가 오고, 매매는 계속되며
   AI 감시망만 꺼진다

### ③ 텔레그램 봇 (선택 — 강력 추천)

1. 텔레그램에서 `@BotFather` 검색 → `/newbot` → 봇 이름 지정 → **토큰** 발급
2. 만든 봇에게 아무 메시지 1개 전송 (chat_id 확보용)
3. 브라우저에서 `https://api.telegram.org/bot<토큰>/getUpdates` 열어 `"chat":{"id":숫자}`
   확인 → 그 숫자가 `TELEGRAM_CHAT_ID`
4. 봇은 이 chat_id의 명령만 듣는다 (타인이 봇을 찾아도 제어 불가)

### ④ DART 전자공시 (선택)

1. https://opendart.fss.or.kr → 회원가입 → 인증키 신청 (무료, 즉시 발급)
2. 감시/보유 종목의 새 공시를 기사화 전에 텔레그램으로 받는다

## 3. 설치

**macOS / 리눅스 (터미널)**

```bash
git clone https://github.com/cssddd3/trading_bot.git toss-trader && cd toss-trader
pip3 install -r requirements.txt
cp .env.example .env
```

**윈도우 (PowerShell)** — 시작 메뉴에서 "PowerShell" 검색해 실행

```powershell
git clone https://github.com/cssddd3/trading_bot.git toss-trader; cd toss-trader
pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env        # 키 입력 후 저장
```

> 윈도우에서 `git`이 없다고 나오면 https://git-scm.com/download/win 설치.
> `.ps1` 스크립트가 차단되면 (최초 1회): `Set-ExecutionPolicy RemoteSigned -Scope CurrentUser`

`.env`를 열어 발급받은 키를 채운다:

```ini
TOSS_CLIENT_ID=발급받은_ID
TOSS_CLIENT_SECRET=발급받은_시크릿
LIVE_TRADING=0            # 실전은 1 (드라이런으로 먼저 확인 권장)
LIVE_BUDGET_KR=50000      # 시장별 초기 예산(원) — 이후 텔레그램 /budget으로 변경
LIVE_BUDGET_US=50000
ANTHROPIC_API_KEY=        # 선택
TELEGRAM_BOT_TOKEN=       # 선택
TELEGRAM_CHAT_ID=         # 선택
DART_API_KEY=             # 선택
```

> ⚠️ `.env`와 `~/.toss_token_cache.json`은 절대 공유/커밋 금지.

## 4. 검증 게이트 통과 (실전 전 필수, 컴퓨터마다 1회)

```bash
python3 run_backtest.py -t st --validate    # 윈도우는 python3 대신 python
```

`logs/strategy_validation.json`에 passed=true가 기록돼야 `--live`가 기동한다.

## 5. 실행

**macOS / 리눅스**

```bash
./start.sh          # 드라이런 (가상 체결 — 먼저 이걸로 관찰 권장)
./start.sh live     # 실전 (.env LIVE_TRADING=1 필요)
./stop.sh           # 정지
tail -f logs/watch.log   # 로그 확인
```

`start.sh`는 caffeinate(잠자기 방지)+nohup으로 돌린다. 맥 덮개를 닫아도 전원이 연결돼
있으면 계속 돈다. 재부팅 후 자동 시작을 원하면 `launchd/com.tosstrader.live.plist`의
경로를 본인 것으로 고친 뒤:

```bash
cp launchd/com.tosstrader.live.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.tosstrader.live.plist
```

**윈도우 (PowerShell)**

```powershell
.\start.ps1          # 드라이런
.\start.ps1 live     # 실전 (.env LIVE_TRADING=1 필요)
.\stop.ps1           # 정지
Get-Content logs\watch.log -Wait -Tail 30   # 로그 실시간 보기
```

`start.ps1`은 창을 닫아도 유지되는 숨김 프로세스로 돌린다. 유의사항:
- **절전 끄기 필수**: 설정 > 시스템 > 전원 > 화면·절전 → "전원 연결 시 절전 안 함"
  (절전에 들어가면 봇도 멈춘다 — 미장 밤 시간대 주의)
- 재부팅 후 자동 시작: 작업 스케줄러(Task Scheduler) → 기본 작업 만들기 →
  트리거 "로그온할 때" → 프로그램 `powershell`, 인수
  `-WindowStyle Hidden -File "C:\내경로\toss-trader\start.ps1" live`
- 명령 프롬프트(cmd)만 쓴다면: `python -u run_dryrun.py --watch --live >> logs\watch.log 2>&1`
  (단, 창을 닫으면 봇도 꺼진다 — PowerShell 스크립트 사용 권장)

## 6. 문제 해결

| 증상 | 원인/조치 |
|---|---|
| 403 에러 | IP 화이트리스트 미등록 — WTS에서 현재 IP 추가 |
| 401/토큰 에러 반복 | 다른 컴퓨터가 같은 키로 토큰 재발급 중 — 키를 한 곳에서만 사용 |
| 하트비트 끊김 | 봇 사망 — `tail logs/watch.log`로 크래시 확인 후 재시작 |
| "halted" 알림 | 장부↔계좌 불일치 — 수동 매매가 봇 장부와 겹쳤는지 확인 후 재시작 |
| 매수가 안 나감 | 정상일 확률 높음 (저회전 전략) — [전략 가이드](strategy.md) 참고 |
| 🚨 크레딧 소진 알림 | Anthropic 잔액 소진 — AI 감시망만 정지, 매매는 계속 |

다음: **[운영 가이드](operation.md)**
