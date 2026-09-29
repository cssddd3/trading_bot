# Operation Guide

**🇰🇷 [한국어](operation.md)** · 🇺🇸 English · [← README](../README.en.md) · [Setup](setup.en.md) · [Strategy](strategy.en.md)

## Telegram (the only control surface)

| Command | Action |
|---|---|
| `/status` | Holdings (sorted by return, with entry date & signal reason) · pending orders · budgets · cash · watchlist · AI market note |
| `/stop` | Pause new buys (existing positions keep being managed) |
| `/resume` | Resume buying |
| `/flat` | Liquidate everything (kill switch) |
| `/sell 003350` | Sell one holding (market open: now / closed: queued for next open). Bot-ledger positions only |
| `/sync` | If you sold directly in the Toss app and the ledger no longer matches the real account, this reconciles it (plain `/resume` will just re-halt on the next reconcile) |
| `/restart` | Restart the bot (picks up new code) — one tap, no terminal needed |
| `/budget KR 100000` | Change the KR budget (persisted; same for US) |
| `/watch 005930` | Manually add a symbol to the watchlist (several at once: `/watch 005930,AAPL,TSLA`) |
| `/unwatch 005930` | Remove a manual watch |
| (free text) | Any non-"/" message goes to the AI assistant. It remembers recent conversation — follow-ups like "why did you buy that one earlier?" work. **It can directly add/remove watchlist symbols from chat** ("add this to my watchlist") — an exception because no money moves. Anything that moves money (buy/sell/budget/stop) is never executed from chat, only via the commands above |

Automatic alerts: buy/sell fills, blocked-buy reasons, bad-news warnings, new corporate
filings (📢), AI stock recommendations (🔭 news/market based — watch-only; buys come only
from validated price rules), scanner signals (🔍), drawdown briefing (📉), hourly heartbeat
(a one-liner "💓 running HH:MM" — if it stops, the bot is down, check the machine; shows 🛑
while buying is halted), end-of-day report, crashes.

The AI assistant **remembers the symbols it showed you last turn as data**, so follow-ups like
"give me those as company names" or "which of that list went up?" stay on topic. Names come
only from a dictionary the bot has actually verified — unknown symbols are reported as
"name unknown" rather than invented.

The end-of-day report includes a **7-day AI recommendation scorecard** — how each pick
has moved since it was recommended, graded automatically every day.

After the close, a **🧠 evening review** arrives — the AI grades today's trading (or
non-trading), checks whether each position's entry thesis still holds, and proposes
backtestable hypotheses. Reviews accumulate in a shared operating journal that the
morning scout reads the next day — the AI roles (scout, news veto, assistant, review)
share one memory and act like a single person. Hypotheses still must pass the
backtest gate before touching real money (the AI still cannot pull the buy trigger).

## Web dashboard (read-only)

Runs automatically while the bot is up — holdings, P&L, pending orders, watchlist, and
recent fills refresh every 10 seconds. Deliberately has no controls (anyone on your Wi-Fi
can open it; control stays behind the Telegram chat-id gate).

- On the bot's machine: http://localhost:8787
- From a phone on the same Wi-Fi: `http://<machine-ip>:8787`
- Port: set `DASHBOARD_PORT=8788` in `.env` (for a second bot instance on the same machine)

## Budgets vs. cash

- **Budget** = the ceiling the bot is allowed to use (`/budget`; compounds with realized P&L)
- **Cash (예수금)** = actual money in the brokerage account (Korean sale proceeds settle T+2)
- What the bot can actually spend = **min(cash, remaining budget)** — raising the budget
  without depositing cash means buys will fail

## Adopting positions you bought manually

```bash
python3 run_dryrun.py --adopt 005930,000660 --live
```

Only the listed symbols are adopted (the bot then manages their stops/news/exits).
Everything else you hold remains untouchable.

## Sharing intel between two bots (same machine, family accounts)

Two bots each run their own AI scout, so their picks differ and LLM cost doubles. This
setting **shares market intel while keeping account data separate**:

```ini
# both .env files point at the same folder
SHARED_INTEL_DIR=/Users/me/Documents/claude/shared-intel
# exactly one bot is the leader (runs the AI scout); the others only read
SCOUT_ROLE=leader        # first bot
SCOUT_ROLE=follower      # second bot
```

| Shared (SHARED_INTEL_DIR) | Kept per account |
|---|---|
| Scout watchlist + market note, AI recommendation ledger, news-verdict cache | Ledger, budgets, stops, pending orders, risk counters, operating journal, assistant memory, token cache |

Whenever the leader recommends a new symbol, the follower adds it to its watchlist with a
"🔭 [shared watchlist]" alert. **Watching only** — each bot still decides buys from its own
strategy signals, budget and cash, so the same pick may be bought by one bot, both, or neither
(a small-budget bot can't buy expensive shares).

## Sharing with friends (Telegram channel)

Create a Telegram channel → add the bot as an admin → add
`TELEGRAM_BROADCAST_CHAT_ID=@channelname` to `.env` → restart.
Scout picks (with reasoning), buys/sells (symbol · price · return), filings, and scanner
signals are then broadcast to the channel. Friends just subscribe — account figures
(cash, budgets, quantities) are never broadcast, and control remains private to the owner.

> ⚠️ Intended for free sharing with a few friends. Paid stock-picking for the general
> public may fall under investment-advisory regulations (Korea: 유사투자자문업) —
> consult a professional before widening the audience.

Next: **[Strategy Guide](strategy.en.md)**
