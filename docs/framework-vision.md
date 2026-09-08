# 프레임워크 비전 — "누구나 조립하는 AI 트레이딩 봇"

🇰🇷 한국어 · **🇺🇸 [English](framework-vision.en.md)** · [← README](../README.md) · [전략 가이드](strategy.md) · [운영 가이드](operation.md)

> 장기 로드맵 문서다. 현 봇의 운용과는 무관하며, 여기 내용은 아직 구현되지 않았다.

## 목표

지금의 toss-trader를 **그래프 기반 조립형 프레임워크**로 발전시킨다:

- 사용자는 **노드를 연결**해 자기만의 봇을 만든다 — 코딩 불필요
- 조립 결과는 **YAML 한 파일**로 정의된다 (사람이 읽을 수 있고, 공유·버전관리 가능)
- **비주얼 에디터**(웹)에서 드래그앤드롭으로 그리면 YAML이 생성되고, 그 반대도 된다
- AI API·증권사 API·뉴스 소스·백테스트는 전부 **갈아끼울 수 있는 부품**

## 무엇이 노드인가

```mermaid
flowchart LR
    subgraph 데이터 소스
        D1[증권 API<br/>토스/KIS/알파카...]
        D2[뉴스 RSS/검색]
        D3[전자공시 DART]
    end
    subgraph 해석 · AI
        A1[AI 스카우트<br/>Claude/GPT/로컬...]
        A2[AI 뉴스 필터]
    end
    subgraph 신호 · 규칙
        S1[전략 노드<br/>Supertrend/이평/커스텀]
        S2[유니버스 필터<br/>거래대금/바닥권...]
    end
    subgraph 집행
        R1[리스크 가드]
        B1[브로커<br/>드라이런/실전]
        N1[알림<br/>텔레그램/슬랙...]
    end
    D1 --> S1 & S2
    D2 & D3 --> A1 & A2
    A1 -.감시 추가만.-> S1
    S2 --> S1
    S1 --> R1
    A2 -.거부권.-> R1
    R1 --> B1 --> N1
```

노드 6종: **DataSource**(시세/뉴스/공시) · **Interpreter**(AI — 해석만) ·
**Signal**(가격 규칙 — 유일한 매수 방아쇠) · **Risk**(한도) · **Broker**(집행) ·
**Notifier**(알림). 엣지의 방향과 종류(데이터/거부권/감시)가 곧 권한이다.

## YAML 스케치 (설계 초안)

```yaml
bot:
  name: my-first-bot
  markets: [KR]

nodes:
  quotes:      { type: datasource.toss,       keys_env: TOSS }
  news:        { type: datasource.rss,        feeds: [google-news, investing] }
  scout:       { type: interpreter.claude,    model: sonnet, role: scout, max_picks: 3 }
  veto:        { type: interpreter.claude,    model: haiku, role: news_veto }
  supertrend:  { type: signal.supertrend,     atr: 10, mult: 3.0, ema_filter: 200 }
  guard:       { type: risk.default,          budget: 500000, daily_loss: -50000 }
  broker:      { type: broker.toss,           mode: dryrun }   # live는 게이트 통과 후에만
  telegram:    { type: notifier.telegram }

edges:
  - quotes -> supertrend
  - news -> scout
  - scout -watch-> supertrend        # 감시 추가만 가능 (매수 불가)
  - supertrend -> guard
  - veto -veto-> guard               # 거부권만 가능
  - guard -> broker -> telegram

validation:                          # 프레임워크가 강제 — 우회 불가
  gate: { oos_trades: 30, oos_expectancy: ">0", mc_loss_prob: "<0.30" }
```

## 무엇을 절대 양보하지 않는가 (프레임워크의 헌법)

컴맹이 조립해도 위험해지지 않으려면, 자유는 조립에만 주고 안전은 구조로 강제한다:

1. **AI 노드는 Signal 노드에 연결될 수 없다** — 엣지 타입이 `watch`(감시 추가)와
   `veto`(거부권)뿐. "AI가 사라고 해서 샀다"가 그래프 문법상 불가능
2. **검증 게이트는 그래프 단위로 강제** — 어떤 조립이든 백테스트 게이트를 통과해야
   `mode: live`가 열린다. 게이트 기준 완화는 YAML로 불가
3. **Broker는 항상 Risk 뒤에만** 연결 가능 (문법 강제)
4. 드라이런이 기본값. 실전은 이중 잠금 유지

## 단계별 로드맵

| 단계 | 내용 | 판정 기준 |
|---|---|---|
| **P0 (지금)** | 경계 정리 — 새 코드는 항상 노드 경계(데이터/해석/신호/리스크/집행)를 침범하지 않게 작성 | 매 수정에서 준수 |
| **P1** | config.py → `bot.yaml` 로더 + 스키마 검증. 코드는 그대로, 조립 정의만 YAML로 | 현 봇이 YAML로 완전 재현 |
| **P2** | 노드 레지스트리 — 6종 인터페이스 확정, 기존 부품(토스/Claude/Supertrend/스캐너)을 노드로 포장 | 부품 교체가 YAML 1줄 |
| **P3** | 그래프 실행기 — 백테스트와 라이브가 같은 그래프 실행, 게이트가 그래프 해시 단위 기록 | 임의 조립 백테스트 가능 |
| **P4** | 비주얼 에디터 — 웹에서 드래그앤드롭 ↔ YAML 양방향, 검증 결과를 그래프 위에 표시 | 컴맹 사용자 테스트 통과 |

P1부터는 현 봇의 실운용과 병행 — 실계좌를 실험대에 올리지 않는다.

## 국외 확장 기틀

한국 밖 사용자·시장을 처음부터 염두에 둔다:

- **이미 된 것**: 문서 전부 한/영 병행, KR/US 두 시장 동시 운용, 시장별 예산·수수료·세금 분리
- **브로커 추상화** (P2와 동일 작업): 토스는 broker 노드 구현 중 하나일 뿐 — KIS·Alpaca·IBKR
  등은 구현체 추가만으로 지원. 호가단위·세금·거래시간은 "시장 프로파일"로 외부화
- **메시지 카탈로그**: 알림·로그 문자열을 ko/en 카탈로그로 분리 (YAML `locale:` 한 줄로 전환)
- **커뮤니티**: 계획·논의는 GitHub [Issues](https://github.com/cssddd3/trading_bot/issues)와
  [Discussions](https://github.com/cssddd3/trading_bot/discussions)에서 공개 진행

## 진행 추적

로드맵 전체: [Discussion #11](https://github.com/cssddd3/trading_bot/discussions/11) ·
단계별 이슈: [roadmap 라벨](https://github.com/cssddd3/trading_bot/issues?q=label%3Aroadmap)
