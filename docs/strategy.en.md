# Strategy Guide

**🇰🇷 [한국어](strategy.md)** · 🇺🇸 English · [← README](../README.en.md) · [Setup](setup.en.md) · [Operation](operation.en.md)

## The core principle: the validation gate

Before any strategy touches real money it must pass three tests:

1. **≥30 out-of-sample (OOS) trades** — otherwise the sample proves nothing
2. **Positive OOS expectancy** — measured on data the strategy has never seen
3. **Monte-Carlo (3,000 runs) loss probability <30%** — trade order is reshuffled to
   measure how much of the result is luck

The in-sample/out-of-sample split prevents curve-fitting ("no cheating on the exam");
Monte Carlo measures the luck hidden behind an average. Fill assumptions are always
conservative (fees + Korean transaction tax + slippage + tick rounding + gap handling).

```bash
python3 run_backtest.py -t st            # backtest
python3 run_backtest.py -t st --mc       # + Monte Carlo
python3 run_backtest.py -t st --validate # writes the gate record required for live mode
```

## Live strategies

### Risk overlays (added 9-09, validated)

Two safety layers apply on top of every entry, adjusting size and timing only — the
underlying strategy is unchanged:

- **Volatility-regime sizing**: when 20-day realized volatility of KOSPI (KODEX200) /
  S&P 500 (SPY) is in the top 20% of the trailing 500 trading days, new entries get half
  size. Sizing, not skipping — trade frequency is unaffected, only risk is. Result:
  Monte-Carlo loss probability 2.4%→0.9%, worst-5% drawdown -85.8%→-67.7%
- **Time stop**: if a position hasn't gained at least 1x its entry-time ATR after 15
  trading days, it's closed at the next open. A band-break stop always takes priority.
  The structural fix for "a short-term-style entry drifting with no context"

Rejected alternative: an **ATR stop ratchet** (tightening the stop only as new highs
form) was tested at four cap settings — all made both expectancy and Monte-Carlo results
worse, concentrating returns onto a single big winner instead of protecting give-back.

### st — Supertrend trend following (default)

- Buys the day a stock's trend is **confirmed to have flipped from down to up** (daily
  close basis), at the next day's open; sells when the trend breaks (close below the band).
  There is no take-profit target — winners are ridden until the trend ends
- **A few trades per month is normal.** "Why didn't it buy anything today?" is usually
  the system working as designed
- Validation: 50 OOS trades, +7.35%/trade, Monte-Carlo loss probability 0.7% ✅
- Why daily closes: intraday candles are unfinished — provisional flips can vanish by
  the close. The close (set by the closing auction) is the day's most reliable price

### Flip scanner (promoted 2026-08-31)

Every day from 15:20 KST it scans the **top 200 by 60-day average trading value** for
"flipped today" stocks and reserves up to 2 next-open buys.

- Validation: 4-year backtest, OOS +4.39%/trade with the bottom-zone filter,
  Monte-Carlo loss probability 4.1% under half-budget position sizing ✅
- **Position sizing of ≤50% of the market budget per trade is the promotion condition**
  (full-size allocation fails the gate at 31% loss probability) — do not remove
  `position_frac` in `config.SCANNER`
- Consequently, **stocks whose single share costs more than 50% of the budget are skipped**
  even when they signal (you get a notification). Raising the cap would violate the
  validated premise — increase `/budget` instead
- **Bottom-zone filter**: flips occurring below 70% of the 250-day high are excluded —
  in both test periods these "dead-cat bounce" entries lost consistently
  (-3.7%/-4.8% per trade, 14–20% win rate)
- News veto, risk limits, and stop registration apply exactly as for any other buy

## What news and AI are for (never buy triggers)

- **Scout**: reads the news and picks what to *watch* (candidate pool is rule-generated)
- **News veto**: blocks a buy at the last gate if strong bad news is found
- **News monitor**: auto-liquidates a holding only for critical, exchange-confirmed events
  (actual trading halt / delisting); sentiment-only bad news raises an alert for the human
- **DART filings**: alerts + triggers re-evaluation — faster than news articles
- AI can never say "buy" — AI judgments are not reproducible, so they can't pass the gate

## Rejected strategies (do not re-test the same idea)

All were tested on real data with survivorship-bias-free universes, and the data said no:

| Strategy | OOS result | Why it loses |
|---|---|---|
| Volatility breakout (chasing same-day surges) | -0.96%/trade, 100% loss prob. | Chasing spikes = buying tops |
| News momentum (riding 1-minute surges) | -0.66%/trade, 33% win rate | Same — a +2%/5min print is a short-term top |
| Filing pullback (buy the dip after supply-contract news) | -1.62%/trade, 8% win rate | The dip after the pop is the start of the full retracement |
| Scanner on same-day trading-value universe | -4.5%/trade | Same-day top volume = yesterday's pump filter |
| st on inverse ETFs (short substitute) | 0 OOS entries / IS -2.6%/trade | Inverse ETFs decay structurally |
| Fixed profit targets | net-negative in a 42-futures replication | amputates the right tail that funds trend systems |
| LLM making trade decisions | edge vanishes under contamination-free eval | fake alpha from the model remembering ticker history |

Full data and reproduction code live in `research/`.

## Proposing a new strategy idea

An idea qualifies for testing when it has all four:

1. **Expressible in numbers** ("buy when it feels right" cannot be tested)
2. **Entry, exit, and stop-loss all specified**
3. **Reproducible on historical data**
4. Passes the **three gate criteria** above before going live

Strategies implement the `on_open`/`on_close` interface in `strategy/base.py`, so the
backtest and the live runner execute **the same code** (no look-ahead by construction).
