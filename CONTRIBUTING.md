# 기여 가이드 / Contributing

🇰🇷 한국어 (English below)

## 원칙 하나만 기억하세요

**이 프로젝트의 유일한 심사 기준은 검증 게이트입니다.** 전략·필터·유니버스 등 돈이 걸리는
변경은 백테스트로 증명해야 합니다:

1. OOS(학습에 안 쓴 구간) 거래 **30건 이상**
2. OOS **기대값 > 0**
3. 몬테카를로 3,000회 **손실확률 < 30%**

"느낌상 좋을 것 같다"는 시험대에 올릴 수 없습니다 — 진입·청산·손절을 숫자로 적어주세요.
기각된 아이디어 목록([전략 가이드](docs/strategy.md#기각된-전략들-같은-아이디어-재탕-금지))을
먼저 확인해주세요. 같은 아이디어의 재검증 요청은 새 근거가 있을 때만 받습니다.

## 기여 방법

| 종류 | 경로 |
|---|---|
| 전략 아이디어 | Issue (전략 제안 템플릿) 또는 [Discussions](https://github.com/cssddd3/trading_bot/discussions) |
| 버그 리포트 | Issue (버그 템플릿) — 로그 첨부 시 **계좌 수치·API 키 가리기** |
| 코드 PR | 환영. 주문 관련 코드는 안전장치(한도 체크)를 같은 PR에 포함 필수 |
| 브로커/알림 채널 추가 | [#12](https://github.com/cssddd3/trading_bot/issues/12), [#6](https://github.com/cssddd3/trading_bot/issues/6) 참고 |

## 안전 규칙 (PR 공통)

- `.env`, 토큰 캐시, `logs/`, `data/` 절대 커밋 금지
- 테스트는 드라이런 상태 파일만 사용 (실계좌·live 상태 파일 접근 금지)
- 실주문 경로는 `broker.py` 하나 — 새 주문 경로를 만들지 마세요

모든 PR은 자동 검사(컴파일·비밀파일·게이트 우회·주문 경로·의존성 취약점)를 통과해야
합니다 — [SECURITY.md](SECURITY.md) 참조. 검사를 우회하려는 PR은 그 자체로 거절 사유입니다.

---

# 🇺🇸 English

## The one rule

**The validation gate is the only review standard.** Any change that touches money
(strategy, filter, universe) must prove itself in backtest: ≥30 OOS trades, positive OOS
expectancy, Monte-Carlo (3,000 runs) loss probability <30%.

"Feels promising" cannot be tested — write entry/exit/stop as numbers. Check the
[rejected-strategy list](docs/strategy.en.md) first; re-tests of rejected ideas need new evidence.

## How to contribute

- **Strategy ideas** → Issue (strategy template) or Discussions
- **Bugs** → Issue (bug template) — redact account figures & API keys from logs
- **Code PRs** → welcome; order-related code must include its safety checks in the same PR
- **New brokers / notification channels** → see #6 and #12

## Safety rules

- Never commit `.env`, token caches, `logs/`, `data/`
- Tests may only touch dry-run state files
- All real orders go through `broker.py` — do not create new order paths
