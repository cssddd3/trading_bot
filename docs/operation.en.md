# Operation Guide

**🇰🇷 [한국어](operation.md)** · 🇺🇸 English · [← README](../README.en.md) · [Setup](setup.en.md) · [Strategy](strategy.en.md)

## Telegram (the only control surface)

| Command | Action |
|---|---|
| `/status` | Holdings (live P&L, stop levels) · pending orders · watchlist · budgets · cash |
| `/stop` | Pause new buys (existing positions keep being managed) |
| `/resume` | Resume buying |
| `/flat` | Liquidate everything (kill switch) |
| `/budget KR 100000` | Change the KR budget (persisted; same for US) |
| `/watch 005930` | Manually add a symbol to the watchlist |
| `/unwatch 005930` | Remove a manual watch |
| (free text) | Any non-"/" message goes to the read-only AI assistant. It remembers recent conversation — follow-ups like "why did you buy that one earlier?" work |

Automatic alerts: buy/sell fills, blocked-buy reasons, bad-news warnings, new corporate
filings (📢), scanner signals (🔍), hourly heartbeat (if it stops, the bot is down —
check the machine), end-of-day report, crashes.

## Web dashboard (read-only)

Runs automatically while the bot is up — holdings, P&L, pending orders, watchlist, and
recent fills refresh every 10 seconds. Deliberately has no controls (anyone on your Wi-Fi
can open it; control stays behind the Telegram chat-id gate).

- On the bot's machine: http://localhost:8787
- From a phone on the same Wi-Fi: `http://<machine-ip>:8787`
- Port: `DASHBOARD` in `config.py`

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
