"""몬테카를로 강건성 검증 — 백테스트 결과가 '운'인지 확인한다.

방법: 실제 발생한 거래 수익률을 복원추출(bootstrap)로 재배열해 수천 개의
가상 시나리오를 만들고, 최종 수익률과 최대낙폭(MDD)의 분포를 본다.

해석 기준:
  - p5 (하위 5%) 수익률이 크게 마이너스면 → 같은 전략이라도 순서 운이 나쁘면 깨진다
  - MDD p95 (최악 5%) 가 감내 범위를 넘으면 → 실전에서 그 낙폭을 만날 수 있다고 봐야 한다
  - 원 백테스트 성적이 분포의 꼬리(상위 몇 %)에 있으면 → 과최적화/운 의심
"""

import random
from dataclasses import dataclass

from .engine import Trade


@dataclass
class MCResult:
    n_sims: int
    n_trades: int
    ret_p5: float        # 최종 수익률 하위 5%
    ret_p50: float
    ret_p95: float
    mdd_p50: float
    mdd_p95: float       # 최악 5% 낙폭
    prob_loss: float     # 원금 손실로 끝날 확률
    original_rank: float # 원 백테스트 수익률이 분포에서 상위 몇 분위인가 (0~1, 높을수록 운 좋았던 것)


def _equity_path(rets: list[float]) -> tuple[float, float]:
    """거래 수익률 나열 → (최종 배수-1, 최대낙폭)."""
    eq, peak, mdd = 1.0, 1.0, 0.0
    for r in rets:
        eq *= (1.0 + r)
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1.0)
    return eq - 1.0, mdd


def run_monte_carlo(trades: list[Trade], n_sims: int = 3000,
                    seed: int = 42) -> MCResult | None:
    """거래 목록으로 부트스트랩 시뮬레이션. 거래 10건 미만이면 None (표본 부족)."""
    rets = [t.pnl_rate for t in trades]
    if len(rets) < 10:
        return None
    rng = random.Random(seed)
    n = len(rets)

    finals, mdds = [], []
    for _ in range(n_sims):
        sample = [rets[rng.randrange(n)] for _ in range(n)]
        f, m = _equity_path(sample)
        finals.append(f)
        mdds.append(m)
    finals.sort()
    mdds.sort()                      # 낙폭은 음수 — 앞쪽이 최악

    def pct(sorted_list, q):
        return sorted_list[min(len(sorted_list) - 1, int(q * len(sorted_list)))]

    original, _ = _equity_path(rets)
    rank = sum(1 for f in finals if f <= original) / len(finals)

    return MCResult(
        n_sims=n_sims, n_trades=n,
        ret_p5=pct(finals, 0.05), ret_p50=pct(finals, 0.50), ret_p95=pct(finals, 0.95),
        mdd_p50=pct(mdds, 0.50), mdd_p95=pct(mdds, 0.05),
        prob_loss=sum(1 for f in finals if f < 0) / len(finals),
        original_rank=rank,
    )


def format_mc(mc: MCResult) -> str:
    pct = lambda x: f"{x * 100:+.1f}%"
    warn = []
    if mc.prob_loss > 0.3:
        warn.append(f"⚠️ 손실 확률 {mc.prob_loss:.0%} — 전략 우위가 약함")
    if mc.original_rank > 0.9:
        warn.append(f"⚠️ 원 백테스트가 상위 {100 - mc.original_rank * 100:.0f}% 시나리오 — 운이 좋았을 가능성")
    lines = [
        "-" * 62,
        f" 몬테카를로 강건성 검증 ({mc.n_sims:,}회 부트스트랩, 거래 {mc.n_trades}건)",
        f"  최종 수익률 분포   p5 {pct(mc.ret_p5)} | 중앙값 {pct(mc.ret_p50)} | p95 {pct(mc.ret_p95)}",
        f"  최대낙폭(MDD)      중앙값 {pct(mc.mdd_p50)} | 최악5% {pct(mc.mdd_p95)}",
        f"  원금 손실 확률     {mc.prob_loss:.1%}",
        f"  원 결과의 위치     분포 상위 {100 - mc.original_rank * 100:.0f}% 지점",
    ]
    lines += [f"  {w}" for w in warn]
    if not warn:
        lines.append("  ✅ 특이 경고 없음 — 거래 순서가 바뀌어도 결과가 크게 흔들리지 않음")
    lines.append("-" * 62)
    return "\n".join(lines)
