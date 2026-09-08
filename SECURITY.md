# 보안 정책 / Security Policy

## 이 저장소의 가드레일

| 장치 | 내용 |
|---|---|
| 머지 승인 | 외부인은 push 불가 — PR 제안만 가능하고, 머지는 소유자 승인 필수 |
| PR 자동 검사 | 컴파일 · 비밀파일 커밋 차단 · 검증 게이트 우회 시도 차단 · broker 외 주문 경로 차단 · 의존성 취약점(pip-audit) · 신규 의존성 심사 |
| CodeQL | 코드 취약점 자동 스캔 (매 push/PR) |
| Secret scanning | API 키·토큰이 커밋되면 푸시 자체가 차단됨 |
| Dependabot | 의존성 취약점 알림 + 자동 수정 PR |
| 브랜치 보호 | main 강제 푸시·삭제 금지 |

## 사용자(받아가는 분)를 위한 안전 수칙

- 이 봇은 **본인 키·본인 계좌**로만 돕니다. 남의 fork를 돌리기 전에 반드시 diff를 확인하세요
  — 특히 `broker.py`, `toss/`, `.github/`의 변경
- `git pull` 후에는 변경 내역을 훑어보고 재시작하세요 (원본 repo도 마찬가지)
- `.env`는 절대 공유 금지. 키가 유출되면 토스 WTS에서 즉시 재발급

## 취약점 제보 / Reporting a vulnerability

주문·자금과 관련된 취약점은 공개 이슈 대신
[GitHub 비공개 제보](https://github.com/cssddd3/trading_bot/security/advisories/new)로 알려주세요.
Report money-path vulnerabilities privately via GitHub Security Advisories, not public issues.
