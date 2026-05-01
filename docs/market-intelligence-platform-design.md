# Market Intelligence Platform

Status: Draft v2 — Free-tier, Notification-first
Date: 2026-04-26
Owner: Nishant
Supersedes: v1 (paid-stack assumptions: Supabase Pro, Polygon, Next.js on Hostinger Node)

> **Current state (2026-05-01):** 9 GitHub Actions jobs + a one-time backfill helper, all live.
> Signal vocabulary: WATCH / RESEARCH / AVOID_CHASE / CHASE_RISK (the last is a downgrade
> when price already moved >5% since the cluster's earliest event — recorded but not pushed).
> Static dashboard auto-deploys to hub4apps.com/stock_app/ via FTPS every 15 min, with
> Chart.js vendored locally and a generated `.htaccess` CSP override for Hostinger LiteSpeed.
>
> Learning loop is closed end-to-end: `price_agent` (weekday EOD) audits matured signals
> against actual prices and writes per-agent EMA weights to `stock_agent_weights`;
> `thesis_agent` reads those weights on every run and applies them per source-agent so
> chronically-wrong agents are dampened and reliable ones amplified. Snapshots of the
> weights actually used are persisted in `stock_signals.weight_at_time` for audit.
>
> Recent additions worth noting (not yet woven into the v2 narrative below):
> - 8-K dilution detection via EDGAR's `primaryDocDescription` (PIPE / underwriting /
>   warrant / registered direct keywords) → emits a parallel `filing_dilution` event with
>   `direction_prior=short`
> - Recurring `earnings_agent` (weekly Sunday) keeps yfinance earnings dates fresh
>   in `stock_normalized_events`
> - Per-ticker chart pages render 180 days of price + filing/earnings event dots and a
>   "Big Moves" table (any |daily return| > 5% with the prior 2-day events as candidate causes)
>
> Sections of this doc that predate those changes (notably §7.5 Price-Action Agent and
> §7.7 Reconciliation Agent) describe future work; the live agent inventory is in `README.md`.
>
> **Roadmap parking lot:** hybrid keyword + LLM (Haiku) classifier for news/Truth Social
> sentiment routing — keeps the deterministic keyword path for known patterns and falls
> through to an LLM call for novel events (Iran/Hormuz/blockade-style topics that surface
> weekly). Bounded cost (~$0.10/month at current volume), zero ongoing keyword maintenance.

## 1. Executive Summary

You asked for a project that can help you recover financially over time after a major loss. The right way to build this is not a "guaranteed winner" bot and not an autonomous prediction engine that claims 90%+ accuracy. That target is not realistic for liquid U.S. equities, and designing around it would push the system toward overfitting, false confidence, and avoidable losses.

The reliable version of this project is a market-intelligence and decision-support platform with:

- a narrow, high-liquidity stock universe
- strong event detection around filings, earnings, insider activity, news, and Truth Social posts that move the tape
- explicit risk controls
- a human-in-the-loop decision process via Telegram alerts
- a learning loop that measures precision, calibration, drawdown, and risk-adjusted return instead of raw directional accuracy

**v2 is constrained to $0 recurring cost.** The product surface is **Telegram push alerts**; the website at `hub4apps.com/stock_app/` is a **static, read-only audit page** auto-deployed to Hostinger by FTPS. All compute runs on **GitHub Actions** (public repo = unlimited free minutes). All data lives in **Supabase Free** (500 MB DB).

This system should help you:

- avoid low-quality setups
- catch relevant filings, market-moving news, and Trump posts faster than the average retail trader
- rank opportunities by evidence and risk
- build a repeatable process
- reduce emotional decision-making after a large loss

It should not promise to recover losses on a schedule, and it should not claim to predict the market with certainty.

## 2. Hard Truths and Constraints

### 2.1 What this system can realistically do

- Surface high-signal events quickly to your phone
- Normalize SEC filings into structured signals
- Classify Truth Social posts by likely market impact
- Score setups across price action, fundamentals, and news
- Track whether the model's forecasts are improving
- Protect capital by filtering bad trades and enforcing risk limits

### 2.2 What this system should not claim

- "90%+ stock prediction accuracy"
- "Guaranteed returns"
- "Recover losses quickly"
- "AI can reliably beat the market every day"

These are exactly the claims regulators warn investors about, especially when AI is involved.

### 2.3 Better success criteria

Primary success metrics:

- precision of high-conviction alerts
- calibration of probability forecasts
- max drawdown
- Sharpe / Sortino of simulated strategy baskets
- average adverse excursion after entry
- profit factor
- percentage of trades that respected risk rules

Secondary metrics:

- headline-to-alert latency
- filing-to-alert latency
- Truth-Social-post-to-alert latency
- false positive alerts per day
- analyst review time saved

See §19 for the honest target numbers.

## 3. Why the Project Should Start With a Narrow Universe

For v1, do not scan all 500 names equally. Start with the names that move the index and attract the most institutional flow.

As of April 23, 2026, the largest SPY holdings were:

- NVDA 7.97%
- AAPL 6.59%
- MSFT 5.07%
- AMZN 4.09%
- AVGO 3.27%
- GOOGL 3.24%
- GOOG 2.59%
- META 2.37%
- TSLA 1.73%
- BRK.B 1.42%

A reliable monitoring platform should begin with:

- top 10-25 S&P 500 names by index weight
- top sector ETFs
- index proxies: SPY, QQQ, IWM
- rates and macro proxies: TLT, DXY, VIX, 2Y and 10Y yields

### 3.1 High-level review of the last 1-2 years

- The last 1-2 years were dominated by mega-cap technology, AI infrastructure, and earnings resilience.
- Market leadership was concentrated, not broad-based.
- Event risk around earnings, AI capex commentary, regulation, insider selling, guidance changes, and political/tariff posts became more important than generic technical patterns.

As of March 31, 2026, SPY's benchmark showed:

- calendar 2024: ~25%
- calendar 2025: ~18%
- YTD 2026: -4.33%
- 1-year: 17.80%
- 3-year annualized: 18.32%

Even after strong trailing returns, the market enters drawdown phases. The system must handle regime shifts, not just trending conditions.

## 4. Product Vision

### 4.1 Product name

Working name: `Hub4Apps Market Intelligence`

### 4.2 Product goal

A Telegram-first private intelligence assistant that watches market-moving equities, filings, Trump posts, and news; pushes ranked alerts to your phone; tracks model confidence; and reconciles model behavior at end of day so the system learns from misses and false signals.

### 4.3 Product positioning

- **Primary UI:** Telegram alerts on your phone.
- **Secondary UI:** Static HTML audit page at `hub4apps.com/stock_app/` (auto-deployed by FTPS).
- **Not a public "AI picks" website.**

**The alert payload is the product.** Everything upstream exists to produce a 5-line, lock-screen-actionable message. The website exists only to prove the audit trail.

This distinction matters because:

- it reduces legal and reputation risk
- it keeps the first release focused
- it avoids public claims about performance
- it lets you iterate on signal quality before exposing the system externally

## 5. Recommended Scope for V1

V1 should do five things well:

1. Monitor a controlled universe of liquid U.S. equities.
2. Ingest filings, earnings, insider transactions, high-quality news, and Truth Social posts.
3. Generate ranked Telegram alerts with evidence and risk context.
4. Maintain a decision journal and forecast audit trail (paper trades).
5. Reconcile predictions vs outcomes at end of day and retrain on a schedule.

V1 should not:

- auto-place real trades
- scan penny stocks or OTC names
- act on social hype without verification
- use LLM output directly as trade execution logic
- market itself as a recovery machine

## 6. System Architecture

```
GitHub Actions (cron schedules)
  ├─ filing_agent        every 5 min
  ├─ news_agent          every 5 min
  ├─ truth_social_agent  every 5 min
  ├─ thesis_agent        every 5 min
  ├─ earnings_agent      weekly
  ├─ price_agent         weekday EOD
  ├─ backtester          weekly/manual
  ├─ source_review_agent monthly
  └─ site_generator      every 15 min
                │
                ▼
         Supabase Free
   (raw + normalized + signals + weights + audit)
                │
                ▼
         Thesis Agent → Risk + Dedupe + Daily-cap gate
                │
                ▼
         Telegram Bot API → your phone
                │
        ┌───────┴────────┐
        ▼                ▼
  Tap deep link     Inline buttons
        │           Bought/Sold/Skipped
        ▼                │
  Static page on         ▼
  hub4apps.com     paper_trades table
                         │
                         ▼
                EOD Reconciliation
                → agent weight update
                         │
                         ▼
                Static HTML regenerated
                → deployed to Hostinger by FTPS
```

### 6.1 Architecture layers

**A. Ingestion** — pull market data, filings, news, earnings, insider activity, macro calendar, Truth Social posts.

**B. Raw storage** — append-only, never overwrite vendor payloads, timestamp every receipt, dedupe keys.

**C. Normalization** — turn payloads into clean event objects: `earnings_release`, `guidance_change`, `insider_sell_cluster`, `8k_material_event`, `price_gap`, `volume_anomaly`, `volatility_spike`, `truth_social_post`.

**D. Feature store** — returns/volatility windows, relative strength vs SPY/sector, pre/post event drift, volume expansion, gap size, realized vol, filing sentiment, insider intensity, earnings revision, Truth Social classification.

**E. Signal engine** — directional score, confidence, risk score, event class, expected holding window, invalidation level.

**F. Telegram dispatcher** — formats payload, applies risk + dedupe + daily-cap gate, posts to user's chat.

**G. Static site generator** — renders the full dashboard from Supabase and deploys to Hostinger via FTPS.

## 7. Multi-Agent Design

Each agent is **one Python script + one `.github/workflows/*.yml`**. They share state via Supabase tables. No long-running servers.

### 7.1 Universe Agent
Maintain the active watchlist; rank symbols by index weight, liquidity, event density, current volatility.

### 7.2 Filing Agent
Watch EDGAR for new filings (8-K, 10-Q, 10-K, Form 4, 13D/G, S-3). Extract events, severity score, key entities/numbers.

### 7.3 News Agent
Ingest breaking company and macro news via free RSS feeds. Dedupe syndicated articles, map to symbols, classify catalyst type, estimate novelty.

### 7.4 Price-Action Agent
Monitor gaps, breakouts, failed breakouts, volatility spikes, relative strength changes. Compare move vs sector and index. Flag dislocations without explanatory news.

### 7.5 Risk Agent
Block low-quality or dangerous setups: no illiquid names, no fraud-investigated names without "watch only" mode, no signals during unresolved halts, no averaging-down logic, daily risk budget cap.

### 7.6 Thesis Agent
Combine evidence from filings, news, market action, fundamentals, and Truth Social into a single scorecard: thesis summary, bull/bear case, key evidence, invalidation signals, confidence band.

Fires only when the cluster rule passes: at least two distinct source agents in the window, or a narrow high-severity exception. This step is deterministic in v1; no LLM is used for probability.

### 7.7 Reconciliation Agent
Compare forecast vs realized outcome at end of day. Identify false positives, false negatives, late alerts, overconfident predictions. Outputs: calibration report, drift report, retraining candidate set.

### 7.8 Telegram Dispatcher (replaces v1 "Website Content Agent")
Formats the locked payload (§17), applies the alert-fatigue governor (§15), posts to your Telegram chat, logs dispatch in `telegram_dispatch_log`.

### 7.9 Truth Social Agent (NEW)

Trump posts have measurable, repeatable impact on tariff-exposed sectors, defense names, China-exposed mega-caps, Fed-rate proxies, crypto names, and his own DJT ticker.

**Source priority:**
1. **Primary:** `trumpstruth.org` RSS feed — third-party mirror, ToS-safe, ~30-60s lag.
2. **Fallback:** `truthbrush` Python library — direct scrape, ToS gray area, use only if RSS goes down.

**Polling cadence (free tier reality):**
- GitHub Actions: every 5 minutes (cron minimum). This is the Phase 1 default.
- Optional upgrade: Supabase scheduled Edge Function (pg_cron) at every 1 minute during US market hours. Only do this if 5-min latency on Trump posts is measurably costing alpha.

**Classifier:** rule-based keyword router. No LLM cost. Deterministic and auditable.

| Trigger keyword(s) | Affected universe | Direction prior |
|---|---|---|
| `tariff`, `tariffs` + country name | XLI, XLB, XLY, country ADRs (FXI, EWZ, EWJ) | short |
| `Fed`, `Powell`, `interest rate` | TLT, IEF, XLF | depends on dovish/hawkish lexicon |
| `China`, `Xi`, `CCP` | AAPL, NVDA, TSLA, FXI | short |
| `oil`, `drill`, `OPEC` | XLE, XOM, CVX | long |
| `crypto`, `bitcoin`, `BTC` | COIN, MSTR, IBIT | long if positive, short if negative |
| `DJT`, `Truth Social` | DJT | long |
| explicit S&P 500 company name (regex) | that ticker | sentiment-driven |
| default | none | log only, no signal |

**Latency target:** post → Telegram in **<6 minutes** on GitHub Actions cron (5-min poll + ~30s thesis + ~30s dispatch). Sub-90s requires the Edge Function upgrade above.

**ToS caveat:** Truth Social's ToS prohibits unauthorized scraping. The RSS mirror is not operated by Truth Social, so it shifts the legal posture. For personal/research use this is generally low-risk; do not redistribute the data publicly.

### 7.10 Fund Flows Agent (Phase 1)

ETFs and mutual funds belong in the pipeline as a **positioning / flow signal**, not as event-driven alerts. They rarely move on their own filings; what matters is what the largest holders are doing.

**Sources (all via EDGAR, ingested by `filing_agent.py` — reused, no second polling loop):**

| Filer kind | Form | Cadence | Signal |
|---|---|---|---|
| Institution (Berkshire, BlackRock, Bridgewater, Pershing Square, Scion, Vanguard) | 13F-HR | quarterly, 45-day lag | Position changes — net buys/sells, new positions, exits |
| Institution (activist) | SC 13D | event-driven | Activist crossing 5% threshold |
| Mutual fund / ETF | N-PORT | monthly, 60-day lag | Fund holdings drift — sector rotation read |
| Mutual fund / ETF | N-CSR | semi-annual | Manager commentary |

**Phase 1 scope:** add `agents/flows_agent.py` that parses raw 13F/N-PORT XML payloads already in `stock_raw_filings` (form_type filter), extracts per-holding rows into a new `stock_fund_holdings` table, computes quarter-over-quarter deltas, and emits a `position_change` normalized event when a famous fund significantly changes a position in our core universe.

**Why a separate agent (not in `filing_agent.py`):** XML parsing of 13F is non-trivial and shouldn't share the polling loop's tight timeout. The agent runs daily off whatever the polling loop has fetched.

**Limitation acknowledged:** 13F's 45-day lag means this is **never a fast signal** — by the time Berkshire's Q1 holdings appear, Q1 is two months old. Use it for thesis confirmation, not for entries.

## 8. Data Sources (free-tier)

| Source | Free quota | Use |
|---|---|---|
| SEC EDGAR | 10 req/sec, requires User-Agent header | Filings (8-K, 10-Q, Form 4, 13D/G) |
| yfinance | unofficial, throttled | Daily bars, earnings dates, backtest prices |
| RSS (CNBC, MarketWatch, Seeking Alpha market currents) | unlimited | Headline news |
| trumpstruth.org RSS | unlimited | Trump posts (primary) |
| truthbrush | gray ToS | Trump posts (fallback) |

**Removed from v1:** Polygon Stocks ($29/mo), Alpha Vantage paid tiers, NewsAPI paid. These can return if budget allows, but v2 must stand on its own at $0.

### 8.1 Trust signals

For protection and due diligence, maintain:
- SEC / FINRA enforcement watchlists where accessible
- registration verification workflows for any outside adviser or promoter
- symbol blocklists for suspicious micro-cap patterns

**Do not rely on:** anonymous Discord/Telegram/WhatsApp groups, unverified X posts, screenshot-based tips.

## 9. Machine Learning Design

### 9.1 Core principle

Use ML to **rank event quality and estimate probabilities**. Do not use an LLM as the final prediction engine.

- LLMs (only Thesis Agent) for extraction, summarization, classification — and only sparingly given $0 budget.
- Statistical / tabular models for probability estimation.

### 9.2 Candidate models

Start with:
- regularized logistic regression
- gradient-boosted trees (LightGBM)

Runs on GitHub Actions runner (7 GB RAM, 2 cores) — plenty for tabular models on 20-30 symbols.

Later:
- temporal sequence model for richer intraday event streams (defer until v2 of model layer).

### 9.3 Labels

Multiple horizons: 15 min, 1 trading day, 3 trading days, 5 trading days.

Predict: direction, move magnitude bucket, volatility expansion, stop-out risk.

### 9.4 Features

Price/return windows, intraday range expansion, realized vol, relative volume, gap context, sector-relative strength, implied-vol proxy if available, filing type and extracted facts, earnings surprise/revision, insider sell intensity, news novelty, Truth Social post classification, macro regime flags.

### 9.5 Evaluation

Do not optimize only for accuracy. Use: precision@top-k, recall on major moves, ROC-AUC, PR-AUC, Brier score, calibration curve, walk-forward backtest PnL, max drawdown, turnover, slippage-aware returns.

### 9.6 Retraining loop

- daily reconciliation
- weekly feature drift review + champion/challenger comparison
- monthly model retraining
- quarterly strategy review

Rules: walk-forward validation only, no random shuffles across time, no leakage from future filings/revised data, champion/challenger registry mandatory.

### 9.7 Adaptive Weight Update (NEW)

Each signal stores: `source_agent`, `features`, `predicted_direction`, `confidence`, `weight_at_time`.

EOD job:

```
for each signal fired today:
  realized_return = close_price - entry_price (long) or inverse (short)
  correct = sign(realized_return) == predicted_direction
  acc_new = α·correct + (1-α)·acc_old        # α = 0.1, EMA
  weight  = clip(acc_new / 0.5, 0.1, 2.0)    # 50% acc → weight 1.0; 70% → 1.4; 30% → 0.6
```

Thesis Agent then aggregates:

```
thesis_score = Σ (agent_weight × evidence_strength)
```

This is intentionally simple. It can be debugged by reading one row. If a source agent's accuracy drifts below 40%, it's effectively muted (weight ≤ 0.8) until it recovers.

## 10. End-of-Day Reconciliation

The most important learning loop in the system.

For every alert, store: symbol, timestamp, event type, model version, probability, predicted direction, expected move range, stop level, invalidation criteria.

At end of day, compute: actual move at each horizon, was alert timely, was event already priced in, was confidence too high or too low, which source family contributed most.

Outputs: false positive clusters, feature drift warnings, source quality ranking, prompts for human review.

### 10.1 Weekly Macro Regression (NEW)

Sunday 09:00 ET job runs a logistic regression on the week's signals with macro features added:
- VIX level + change
- 10Y yield change
- Fed event flag (FOMC meeting day, Powell speech day)
- Truth Social post count + sector-impact tally
- Sector rotation indicator (XLK vs XLE vs XLF)
- Dollar (DXY) change

A new macro feature is promoted into the live feature set only if:
1. Walk-forward AUC on the trailing 60 days improves by ≥0.01, AND
2. Calibration error does not worsen.

Champion/challenger comparison logged to `model_registry`.

## 11. Database Design in Supabase

### 11.1 Why Supabase Free

Postgres + auth + storage + edge functions + RLS, all in the free tier:
- 500 MB database
- 1 GB file storage
- 50K monthly active users
- Unlimited API requests

Sufficient for personal use with disciplined retention.

### 11.2 Core tables

- `symbols`
- `watchlists`
- `raw_filings`
- `raw_news`
- `raw_prices`
- `raw_truth_posts` (NEW)
- `normalized_events`
- `features_daily`
- `features_intraday`
- `signals`
- `signal_evidence`
- `forecast_audit`
- `agent_weights` (NEW — one row per agent per day with `accuracy_ema`, `weight`)
- `telegram_dispatch_log` (NEW — every push attempt with delivery status)
- `paper_trades` (NEW — user's Bought/Sold/Skipped responses)
- `backtest_runs`
- `backtest_trades`
- `model_registry`
- `user_decisions`
- `journal_entries`

### 11.3 Free-tier retention

- Raw tables (`raw_filings`, `raw_news`, `raw_prices`, `raw_truth_posts`) keep 90 days.
- Older raw rows archived to Supabase Storage as compressed JSON, then deleted from DB.
- Normalized events and signals retained indefinitely (small).
- Weekly cron job enforces retention.

### 11.4 Security model

Roles: `admin`, `analyst`, `viewer`. Private by default. Service keys only in GitHub Actions secrets. RLS enabled on all user-facing tables.

## 12. Web Application Design (Static HTML)

### 12.1 Deployment target

- Domain: `https://hub4apps.com/stock_app/`.
- Posture: static read-only audit dashboard. Generated pages are sanitized before publish; secrets and raw dead-letter payloads must never be rendered.
- **Hostinger plan does not support JS/Node.** No Next.js, no React, no SSR.

### 12.2 Generation

- `site_generator.yml` GitHub Action runs every 15 minutes.
- Python + Jinja2 templates render:
  - `index.html` — recent signals, agent health, pre-signal candidates
  - `signals.html` — filterable signal table
  - `events.html` — sanitized normalized event stream
  - `agents.html` — agent weights and redacted failure log
  - `backtest.html` — latest replay metrics
  - `learning.html` — agent-weight timeline and forecast audit
  - `ticker/<ticker>.html` — 180-day price chart with filing/earnings/news overlays
  - `alert/<id>.html` — full thesis page per Telegram alert
- Output is deployed to Hostinger via FTPS. It is no longer force-pushed to a public `dist` branch.

### 12.3 Auto-refresh

`<meta http-equiv="refresh" content="900">` on every page via the shared layout.

### 12.4 Hostinger FTPS publish

`site_generator.yml` deploys `./dist/` with `SamKirkland/FTP-Deploy-Action`.

- Server: `ftp.hub4apps.com`
- Protocol: explicit FTPS on port `21`
- Server directory: `./` because the scoped FTP user is chrooted directly at `/public_html/stock_app/`
- `dangerous-clean-slate: false`

### 12.5 Pages

- **Dashboard:** recent signals, agent health, evidence building toward a signal.
- **Signals:** full filterable signal history.
- **Events:** sanitized event stream from filings, news, Truth Social, earnings, and momentum.
- **Agents:** current learned weights, heartbeats, redacted failure log.
- **Backtest:** latest 180-day replay metrics.
- **Learning:** agent-weight evolution and live/backtest audit rows.
- **Ticker pages:** 180-day price with event overlays and "Big Moves" candidate-cause table.

## 13. Hosting

| Layer | Service | Cost |
|---|---|---|
| Compute (all agents, training, site generation) | GitHub Actions, public repo | $0 |
| Database + auth + storage | Supabase Free | $0 |
| Static frontend | Hostinger shared hosting (existing) | already paid |
| Domain | hub4apps.com (existing) | already paid |
| Push notifications | Telegram Bot API | $0 |

**Total recurring cost: $0.**

Do not introduce a paid component until §19 calibration targets are met for 60 consecutive days of paper trading.

## 14. Scheduling and Orchestration (GitHub Actions)

**GitHub Actions cron minimum interval is 5 minutes.** Earlier drafts of this doc listed `*/1`, `*/2`, `*/3` schedules — those will silently fail to deploy. The table below is the deployable reality.

| Workflow | Cron | Purpose |
|---|---|---|
| `truth_social_agent.yml` | `*/5 * * * *` | Poll Trump posts (RSS) — see note below for sub-5-min option |
| `filing_agent.yml` | `*/5 * * * *` | EDGAR poll |
| `news_agent.yml` | `*/5 * * * *` | CNBC / MarketWatch / AP RSS classifier |
| `earnings_agent.yml` | `0 12 * * 0` | Refresh upcoming/recent earnings events |
| `thesis_agent.yml` | `*/5 * * * *` | Join evidence, score, dispatch Telegram |
| `price_agent.yml` | `30 21 * * 1-5` UTC | EOD close audit + EMA weight update |
| `backtester.yml` | weekly/manual | 180-day replay + calibration metrics |
| `site_generator.yml` | `*/15 * * * *` | Regenerate HTML and deploy by FTPS |
| `source_review_agent.yml` | `0 13 1 * *` | Monthly external-feed health check |
| `historical_ingest.yml` | manual only | One-time EDGAR/earnings/prices backfill |

**Sub-5-min polling option (Truth Social only):** if 5 minutes proves too slow for tariff/Fed posts, move `truth_social_agent` to a **Supabase scheduled Edge Function** (pg_cron + pg_net), which supports minute-level scheduling on the free tier. Keep the rest on GitHub Actions for simplicity. Phase 1 ships everything on GitHub at `*/5`; the migration to Edge Functions happens only if measured latency on Trump posts is the bottleneck.

**Best-effort caveat:** GitHub Actions cron is best-effort. Jobs may delay 5-15 min under platform load and are paused after 60 days of repository inactivity. Acceptable for filings and news. **Not** acceptable for sub-minute price reaction — we don't promise that.

Constraints:
- Keep each workflow short and idempotent.
- Store all secrets (Supabase service key, Telegram bot token, Hostinger FTPS password) as GitHub Actions secrets.
- Use concurrency groups to prevent overlapping runs of the same agent.

## 15. Reliability and Risk Controls

### 15.1 Capital protection rules

- position sizing caps
- max daily risk budget
- max number of concurrent ideas
- no trade on low-confidence alerts
- no trade when data freshness is stale
- no trade when filing parser confidence is low
- no trade when spread / liquidity is poor

### 15.2 Alert Fatigue Governor (NEW)

- Max **5 alerts/day** total.
- Dedupe within a **60-minute window** per `(symbol, event_type)`.
- Confidence floor: **0.65**.
- If >5 candidates qualify, keep top 5 by `confidence × agent_weight`.

This rule is non-negotiable. Without it, the user mutes the bot within a week.

### 15.3 Cluster Rule (NEW — no single-source alerts)

**A single agent firing alone is not an alert.** A new 8-K alone is information, not signal. The thesis_agent must see corroboration from **at least 2 distinct source agents** within a 5-minute window for a candidate signal to fire.

Valid clusters:
- filing + price/volume anomaly
- filing + news article
- filing + Truth Social mention
- news + price/volume anomaly
- Truth Social + price/volume anomaly
- Form 4 cluster (≥3 insider trades) — counts as its own cluster, can fire alone

**Narrow exceptions (single-source allowed):**
- SC 13D filing — activist disclosure is rare and self-validating
- 8-K with Item 1.01 (Material Definitive Agreement), Item 2.01 (Acquisition), or Item 5.02-material (CEO/CFO departure) — content-classified as severity 4 by the parser

Why: most filings are routine (Item 5.02 director appointments, Item 7.01 Reg FD disclosures of slides) and don't move price. Only when **independent observers see something happen at the same time** does the joint probability of a real event clear the alert threshold.

This rule pairs with §17.7 — even with a high rubric score, the cluster check is a hard gate.

### 15.4 Model governance

Every signal records the model version. Every prediction is reproducible. Every manual override is logged. Every retrain produces comparison metrics vs current model.

### 15.5 Operational reliability

Heartbeat checks for each agent. Source freshness dashboard. Dead-letter queue for failed parsing. Idempotency keys for ingestion. Alert dedupe.

## 16. What "Reliable" Means in This Context

Reliable does not mean "it always makes money."

Reliable means:
- it does not hallucinate data
- it does not make impossible claims
- it preserves audit trails
- it fails safely
- it separates extraction from prediction
- it measures whether the model is truly improving
- it helps you avoid impulsive, unstructured decisions

## 17. Notification Design (Telegram)

### 17.1 Why Telegram

- Free, no rate limits at this volume
- Instant on iOS, no Apple Developer enrollment needed
- Inline buttons supported (Bought/Sold/Skipped)
- Bot token + chat ID stored as GitHub Actions secrets

### 17.2 Setup (one-time)

1. Open Telegram, message `@BotFather`, run `/newbot`, save the token.
2. Message your bot once to start the chat.
3. Visit `https://api.telegram.org/bot<TOKEN>/getUpdates` to find your `chat_id`.
4. Add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` to GitHub Actions secrets.

### 17.3 Alert payload (locked format) — v1 epistemic vocabulary

The bot **does not say BUY or SELL** until the system has earned that vocabulary (see §17.6 graduation rule). v1 uses four epistemic actions:

- 🟢 `WATCH` — score ≥ 70. Multiple agents agree something material happened. Do your own research before acting.
- 🟡 `RESEARCH` — score 50-69. One strong signal but uncorroborated, or strong signal already partially priced in.
- 🔴 `AVOID_CHASE` — bearish score ≥ 50 from dilution, miss, downgrade, bearish news, or similar evidence. Treat as a caution/research item, not a short recommendation.
- 🔴 `CHASE_RISK` — would-be WATCH/RESEARCH where price already moved >5% since the cluster's earliest event. Recorded and audited, but not pushed to Telegram.

```
🟢 NVDA · WATCH · score 74/100
New 8-K detected 3m ago: buyback authorization disclosed.
Confidence: 0.78 · Horizon: 1d
Tap for thesis →  https://hub4apps.com/stock_app/alert/<id>.html
```

Required fields (alert does not fire if any missing): `action`, `score`, `confidence`, `evidence_summary` (≤80 chars), `horizon`, `signal_id`.

Stop / target prices are **deliberately omitted** in v1 — those are trade-execution levels, and v1 doesn't recommend trades. They appear only after graduation.

### 17.4 Inline buttons

Each alert ships with three buttons (semantics matched to v1 vocabulary):
- `🔍 Researched`
- `💰 Acted`
- `⏭ Skipped`

User taps → Telegram callback → `stock_paper_trades` row written via Supabase REST. Default to "Skipped" if no response within 60 minutes (so reconciliation has a complete log).

### 17.5 Handling button callbacks without a server

GitHub Actions can't receive webhooks (no public endpoint). Two free options:
1. **Polling**: a `*/5 * * * *` workflow calls `getUpdates` and processes button taps. Simple, slightly delayed.
2. **Supabase Edge Function** as the webhook target (free tier supports this). Cleaner; use this if polling proves laggy.

Start with polling. Switch to Edge Function if needed.

### 17.6 Graduation rule — when the bot earns BUY/SELL

The bot graduates from `WATCH/RESEARCH/AVOID_CHASE/CHASE_RISK` to `BUY/SELL/TRIM` only when **all** of the following are true over a 60-day rolling paper-trading window:

- Precision@5 daily alerts ≥ 55%
- Brier score ≤ 0.22
- Calibration error ≤ 10%
- Max drawdown of paper portfolio ≤ 15%
- ≥ 30 alerts have been issued (sample size)

If any metric regresses below threshold for 14 consecutive days post-graduation, the bot **demotes itself** back to WATCH vocabulary automatically. This is a reversible state machine, not a one-way ratchet — and it removes the temptation to hand-tune the rubric to keep the BUY label.

### 17.7 Scoring rubric (rule-based, 100-point scale)

This rubric is the v1 thesis function. It is fully transparent — every point on the score is traceable to a row in `stock_signal_evidence`. No ML is invoked at this stage.

| Evidence | Rule | Points | Phase 1? |
|---|---|---|---|
| New 8-K (operating company) | Detected within 5 min of filing | +25 | yes |
| New SC 13D | Activist crossing 5% threshold | +20 | yes |
| New SC 13G | Passive holder crossing 5% | +10 | yes |
| Form 4 cluster | ≥3 insider trades same direction in 5 trading days | +12 (buy) / -12 (sell) | later (Phase 2) |
| Filing severity uplift | Parser flags Item 1.01, 2.01, 5.02-material, 7.01 | +0 to +20 | yes |
| Confirming news | Same symbol gets credible RSS article within 30 min | +12 | yes |
| Earnings beat/miss | Reported EPS vs estimate, scored by surprise magnitude | +15 to +50 | yes |
| S-3 / dilution signal | Shelf or financing-flavored filing | +12 evidence strength, bearish direction | yes |
| Price/volume confirmation | Relative volume > 2× 20-day avg, or unexplained gap > 1% | +15 | later |
| Sector confirmation | Symbol outperforms its sector ETF by ≥ 1% on the day | +10 | later |
| Truth Social mapping | Post explicitly maps to this ticker (per §7.9 table) | +15 | yes |
| Staleness penalty | Event detected > 15 min after public time | -10 | yes |
| Chase-risk downgrade | Bullish WATCH/RESEARCH already moved >5% since earliest event | CHASE_RISK | yes |
| Liquidity penalty | Wide spread, halt, or ADV < $10M | -20 | later |

**Thresholds:**

| Score | Action | Telegram |
|---|---|---|
| ≥ 70 | `WATCH` | sent |
| 50-69 | `RESEARCH` | sent (subject to alert-fatigue governor) |
| < 50 | suppress | logged only, no push |

**Cluster requirement** (from §15.3): even if score ≥ 70, the alert does not fire unless evidence comes from at least 2 distinct source agents. The narrow exceptions are SC 13D, high-severity 8-K, and high-severity earnings release — those can fire alone because the event itself is rare or self-validating.

**Phase 1 active rules:** the rows marked "yes" above are live. The "later" rows turn on as their source agents come online.

## 18. Delivery Roadmap

### Phase 0 — Foundation (1-2 days)
- Initialize public GitHub repo at `stock_app/`.
- Create Supabase Free project; apply schema migration with all tables in §11.2.
- Create Telegram bot via @BotFather; store token + chat ID as Actions secrets.
- First workflow: `filing_agent.yml` polling EDGAR for the 20-name universe → write to `raw_filings`.

### Phase 1 — First Alert End-to-End (3-5 days)
- Add `truth_social_agent.yml` (RSS source) → `raw_truth_posts`.
- Add `thesis_agent.yml` with rule-based scoring (no ML yet).
- Add Telegram dispatch step with locked payload + alert-fatigue governor.
- First real Telegram alert from a live filing or Trump post.

### Phase 2 — Signal Scoring & Static Site (1-2 weeks)
- Event normalization for filings + Truth Social.
- Baseline LightGBM model trained on synthetic + first 2 weeks of live data.
- Risk engine + confidence scoring.
- `site_generator.yml` generates the full static dashboard and deploys it to Hostinger by FTPS.

### Phase 3 — Reconciliation, Backtesting, Adaptive Weights (2 weeks)
- `price_agent.yml` computes EOD outcomes and updates `stock_agent_weights`.
- Forecast audit tables.
- `backtester.yml` runs the 180-day replay and calibration summary.
- Inline buttons → `paper_trades`.

### Phase 4 — Weekly Macro Review (1 week)
- `source_review_agent.yml` checks feed health monthly.
- Optional future macro/champion-challenger work stays gated behind paper-trading results.

### Phase 5 — Hardening (ongoing)
- Monitoring, dedupe tuning, source expansion, model governance, performance tuning.

## 19. Honest Accuracy Targets

After 60 days of paper trading, expect:

| Metric | v1 target | Excellent (rare) |
|---|---|---|
| Direction accuracy (1-day, news-conditioned) | 55–58% | 62% |
| Precision@5 daily alerts | 55% | 65% |
| Brier score | < 0.22 | < 0.18 |
| Calibration error | < 10% | < 5% |
| Paper-portfolio Sharpe (90d) | > 0.8 | > 1.5 |
| Max drawdown | < 15% | < 8% |

**The job of the reconciliation loop is to prove the model is calibrated, not to chase 90%.** If after 90 days the model is well-calibrated at 56%, that is a real edge worth keeping. If it is not, the right action is to narrow the universe further or stop — not to spend money on more data.

## 20. Recommended Initial Universe

20-30 names max.

**Core:** NVDA, AAPL, MSFT, AMZN, AVGO, GOOGL, GOOG, META, TSLA, BRK.B, JPM, LLY, XOM, JNJ, WMT, V, NFLX, COST, MA, AMD.

**Context tickers:** SPY, QQQ, XLK, XLF, XLC, XLY, XLE, XLI, XLB, FXI, EWZ, EWJ, TLT, IEF, DXY, VIX, COIN, MSTR, IBIT, DJT.

## 21. Final Recommendation

Build this as a disciplined intelligence platform, not a promise machine.

The right first release is:
- private
- event-driven
- filing + Truth-Social aware
- risk-constrained
- measurable
- auditable
- **$0 recurring cost**

If you want the best chance of long-term value, the system should help you do **fewer, better, more explainable actions** rather than trying to predict every market move.

v2 is built to prove the loop works at $0 cost before any spend is justified. If after 60 days of paper trading the calibration targets in §19 are not met, the right action is to narrow the universe further or stop — not to spend money on more data.

## 22. Legal and Practical Fraud-Recovery Layer

Because the loss involved stock fraud, your true recovery path may have two separate tracks:
- investment process recovery (this project)
- fraud complaint / restitution recovery (legal track)

This project only addresses the first. For the second, separately preserve:
- statements
- wire details
- messages
- trade confirmations
- names of brokers / promoters / advisers
- websites and domains used
- screenshots

Review formal reporting avenues:
- SEC TCR complaint process
- FINRA investor complaint process
- BrokerCheck / investment professional verification
- attorney review if loss involved a registered broker, unauthorized trades, or misrepresentation

Outside the app build, but important enough to state explicitly.

## 23. Sources

- SPY holdings and benchmark context:
  - https://www.ssga.com/us/en/intermediary/etfs/state-street-spdr-sp-500-etf-trust-spy
- S&P 500 / ETF performance context:
  - https://www.blackrock.com/us/financial-professionals/products/239726/ishares-core-sp-500-etf
- SEC EDGAR APIs:
  - https://www.sec.gov/edgar/sec-api-documentation
  - https://www.sec.gov/os/accessing-edgar-data
- Supabase Free:
  - https://supabase.com/pricing
  - https://supabase.com/docs/guides/functions
  - https://supabase.com/docs/guides/auth/quickstarts/nextjs
- GitHub Actions:
  - https://docs.github.com/en/actions/using-workflows/events-that-trigger-workflows#schedule
  - https://docs.github.com/en/billing/managing-billing-for-github-actions/about-billing-for-github-actions
- Telegram Bot API:
  - https://core.telegram.org/bots/api
  - https://core.telegram.org/bots/features#botfather
- Truth Social:
  - https://trumpstruth.org/
  - https://github.com/stanfordio/truthbrush
- Free market data:
  - https://finnhub.io/docs/api
  - https://github.com/ranaroussi/yfinance
  - https://fred.stlouisfed.org/docs/api/fred/
- Investor protection / fraud reporting:
  - https://www.investor.gov/introduction-investing/general-resources/news-alerts/alerts-bulletins/investor-alerts/artificial-intelligence-fraud
  - https://www.investor.gov/introduction-investing/getting-started/working-investment-professional/check-out-your-investment-professional
  - https://www.finra.org/investors/need-help/file-a-complaint
  - https://www.sec.gov/tcr
- Future, if budget allows:
  - https://polygon.io/docs/stocks/ws_getting-started
  - https://www.alphavantage.co/documentation/
