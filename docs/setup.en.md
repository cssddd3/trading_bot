# Setup Guide

**🇰🇷 [한국어](setup.md)** · 🇺🇸 English · [← README](../README.en.md) · [Operation](operation.en.md) · [Strategy](strategy.en.md)

## 1. What you need

- macOS / Linux / **Windows**, Python 3.11+
  (on Windows, install from [python.org](https://www.python.org/downloads/) and
  **check "Add python.exe to PATH"**)
- A Toss Securities account + Open API keys (instructions below)
- (Optional) Anthropic API key — AI scout / news veto. Without it the bot runs rules-only
- (Optional) Telegram bot token — alerts and remote control
- (Optional) DART API key — real-time corporate-filing alerts (Korean market)

## 2. Obtaining the keys

### ① Toss Securities Open API (required)

1. Log into Toss Securities WTS (desktop web) → Settings → **Open API**
2. Register an app → you receive `CLIENT_ID` / `CLIENT_SECRET`
3. **IP allowlisting is mandatory**: Settings > Open API > Allowed IPs — add the public IP
   of the machine running the bot (unlisted IPs get 403; update it when your home IP changes)
4. Docs: https://openapi.tossinvest.com/openapi-docs/overview.md

> ⚠️ **Never use the same API key on two machines** — issuing a token immediately
> invalidates the previous one, so the two bots kill each other. Each person must use
> **their own account and their own keys**.

### ② Anthropic API (optional — AI features)

1. https://console.anthropic.com → API Keys
2. Paid credits required. With per-role model tiering (scout: Sonnet, veto/assistant: Haiku)
   and a 15-minute throttle, usage is modest. If credits run out you get a 🚨 Telegram
   alert; trading continues with the AI layer off (fail-open).

### ③ Telegram bot (optional — strongly recommended)

1. In Telegram, find `@BotFather` → `/newbot` → name your bot → you receive a **token**
2. Send your new bot any single message (needed to discover your chat id)
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and find
   `"chat":{"id":NUMBER}` → that number is `TELEGRAM_CHAT_ID`
4. The bot only obeys commands from this chat id.

### ④ DART corporate filings (optional)

1. https://opendart.fss.or.kr → sign up → request an API key (free, instant)
2. New filings on watched/held stocks reach Telegram before news articles do.

## 3. Install

**macOS / Linux (terminal)**

```bash
git clone https://github.com/cssddd3/trading_bot.git toss-trader && cd toss-trader
pip3 install -r requirements.txt
cp .env.example .env
```

**Windows (PowerShell)**

```powershell
git clone https://github.com/cssddd3/trading_bot.git toss-trader; cd toss-trader
pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env        # fill in your keys and save
```

> No `git` on Windows? Install from https://git-scm.com/download/win.
> If `.ps1` scripts are blocked (one-time): `Set-ExecutionPolicy RemoteSigned -Scope CurrentUser`

`.env` contents:

```ini
TOSS_CLIENT_ID=your_client_id
TOSS_CLIENT_SECRET=your_client_secret
LIVE_TRADING=0            # set 1 for live (start with a dry run first!)
LIVE_BUDGET_KR=50000      # initial per-market budget in KRW — later adjustable via /budget
LIVE_BUDGET_US=50000
ANTHROPIC_API_KEY=        # optional
TELEGRAM_BOT_TOKEN=       # optional
TELEGRAM_CHAT_ID=         # optional
DART_API_KEY=             # optional
```

> ⚠️ Never share or commit `.env` or `~/.toss_token_cache.json`.

## 4. Pass the validation gate (required before live, once per machine)

```bash
python3 run_backtest.py -t st --validate    # Windows: python
```

Live mode refuses to start unless `logs/strategy_validation.json` records passed=true.

## 5. Run

**macOS / Linux**

```bash
./start.sh          # dry run (paper fills — watch it first)
./start.sh live     # live (.env LIVE_TRADING=1 required)
./stop.sh           # stop
tail -f logs/watch.log
```

`start.sh` uses caffeinate (no sleep) + nohup. For auto-restart after reboot, edit the
paths in `launchd/com.tosstrader.live.plist` to your own, then:

```bash
cp launchd/com.tosstrader.live.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.tosstrader.live.plist
```

**Windows (PowerShell)**

```powershell
.\start.ps1          # dry run
.\start.ps1 live     # live (.env LIVE_TRADING=1 required)
.\stop.ps1           # stop
Get-Content logs\watch.log -Wait -Tail 30
```

Notes for Windows:
- **Disable sleep** (Settings > System > Power): if the PC sleeps, the bot stops —
  this matters for overnight US sessions
- Auto-start on login: Task Scheduler → Create Basic Task → trigger "At log on" →
  program `powershell`, arguments `-WindowStyle Hidden -File "C:\path\to\toss-trader\start.ps1" live`

## 6. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| 403 errors | IP not allowlisted — add your current IP in WTS |
| Repeated 401/token errors | Another machine is using the same key — one machine per key |
| Heartbeat stops | Bot died — check `logs/watch.log` for the crash, then restart |
| "halted" alert | Ledger vs. account mismatch — check whether manual trades overlapped the bot's book, then restart |
| No buys happening | Usually normal (low-turnover strategy) — see the [Strategy Guide](strategy.en.md) |
| 🚨 credit-exhausted alert | Anthropic balance empty — only the AI layer stops; trading continues |

Next: **[Operation Guide](operation.en.md)**
