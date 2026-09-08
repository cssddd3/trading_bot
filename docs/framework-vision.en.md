# Framework Vision — "An AI Trading Bot Anyone Can Assemble"

**🇰🇷 [한국어](framework-vision.md)** · 🇺🇸 English · [← README](../README.en.md) · [Strategy Guide](strategy.en.md) · [Operation Guide](operation.en.md)

> Long-term roadmap. Nothing here is implemented yet; the current bot is unaffected.

## Goal

Evolve toss-trader into a **graph-based, composable framework**:

- Users **connect nodes** to build their own bot — no coding required
- The assembly is defined in **a single YAML file** (human-readable, shareable, version-controlled)
- A **visual editor** (web) renders drag-and-drop ↔ YAML in both directions
- AI APIs, broker APIs, news sources, and backtesting are all **swappable parts**

## What is a node

```mermaid
flowchart LR
    subgraph Data sources
        D1[Broker API<br/>Toss/KIS/Alpaca...]
        D2[News RSS/search]
        D3[Corporate filings]
    end
    subgraph Interpretation · AI
        A1[AI scout<br/>Claude/GPT/local...]
        A2[AI news filter]
    end
    subgraph Signals · rules
        S1[Strategy node<br/>Supertrend/MA/custom]
        S2[Universe filter]
    end
    subgraph Execution
        R1[Risk guard]
        B1[Broker<br/>dry-run/live]
        N1[Notifier<br/>Telegram/Slack...]
    end
    D1 --> S1 & S2
    D2 & D3 --> A1 & A2
    A1 -.watch only.-> S1
    S2 --> S1
    S1 --> R1
    A2 -.veto only.-> R1
    R1 --> B1 --> N1
```

Six node kinds: **DataSource** · **Interpreter** (AI — interprets only) ·
**Signal** (price rules — the only buy trigger) · **Risk** · **Broker** · **Notifier**.
The direction and type of each edge is itself the permission model.

## YAML sketch (design draft)

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
  broker:      { type: broker.toss,           mode: dryrun }   # live only after the gate
  telegram:    { type: notifier.telegram }

edges:
  - quotes -> supertrend
  - news -> scout
  - scout -watch-> supertrend        # may only add to the watchlist (cannot buy)
  - supertrend -> guard
  - veto -veto-> guard               # veto power only
  - guard -> broker -> telegram

validation:                          # enforced by the framework — cannot be bypassed
  gate: { oos_trades: 30, oos_expectancy: ">0", mc_loss_prob: "<0.30" }
```

## Non-negotiables (the framework's constitution)

Freedom applies to assembly; safety is enforced by structure:

1. **AI nodes cannot connect to Signal nodes** — only `watch` and `veto` edge types exist.
   "The AI said buy, so it bought" is grammatically impossible
2. **The validation gate is enforced per graph** — any assembly must pass the backtest gate
   before `mode: live` unlocks. Gate thresholds cannot be relaxed via YAML
3. **Broker only connects downstream of Risk** (enforced by grammar)
4. Dry-run is the default; live keeps the double lock

## Roadmap

| Phase | Scope | Done when |
|---|---|---|
| **P0 (now)** | Boundary discipline — all new code respects node boundaries (data/AI/signal/risk/execution) | every change complies |
| **P1** | config.py → `bot.yaml` loader + schema validation. Code unchanged; only assembly moves to YAML | current bot fully reproduced from YAML |
| **P2** | Node registry — finalize the 6 interfaces, wrap existing parts (Toss/Claude/Supertrend/scanner) as nodes | swapping a part is one YAML line |
| **P3** | Graph runner — backtest and live execute the same graph; gate records keyed by graph hash | arbitrary assemblies are backtestable |
| **P4** | Visual editor — web drag-and-drop ↔ YAML, validation results shown on the graph | non-developer user test passes |

From P1 onward, work runs alongside live operation — the real account is never the test bench.

## Global expansion groundwork

Built for users and markets beyond Korea from the start:

- **Already in place**: fully bilingual docs (KR/EN), simultaneous KR/US market operation,
  per-market budgets/fees/taxes
- **Broker abstraction** (same work as P2): Toss is just one broker-node implementation —
  KIS, Alpaca, IBKR etc. are one implementation away. Tick sizes, taxes, and trading hours
  externalized as "market profiles"
- **Message catalog**: notification/log strings split into ko/en catalogs (`locale:` one-liner)
- **Community**: plans and discussion happen in the open via GitHub
  [Issues](https://github.com/cssddd3/trading_bot/issues) and
  [Discussions](https://github.com/cssddd3/trading_bot/discussions)

## Tracking

Full roadmap: [Discussion #11](https://github.com/cssddd3/trading_bot/discussions/11) ·
Phase issues: [roadmap label](https://github.com/cssddd3/trading_bot/issues?q=label%3Aroadmap)
