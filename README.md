# Hub4Apps Market Intelligence

A six-layer event-driven trading intelligence pipeline that runs **without a human** on free-tier
infrastructure. Ingests SEC filings, market news, Truth Social posts, FDA/clinical readouts,
DoD contract awards, insider Form 4 filings, and macro releases; clusters multi-source evidence
into scored signals; converts signals into trade setups; gates capital with hardcoded risk rules;
reconciles paper trades against actual price reality; and learns from outcomes via per-agent EMA
weights + per-rule payoff calibration.

**Live dashboard:** https://hub4apps.com/stock_app/  · `status.json` machine-readable feed at
[`/status.json`](https://hub4apps.com/stock_app/status.json)
**Live count:** 25 GitHub Actions agents, 6 architectural layers, 27 SQL migrations applied.
**Read the architecture first:** [`CLAUDE.md`](CLAUDE.md) has the six-layer diagram and table boundaries.
**Active roadmap & decisions log:** [`docs/next-phases-roadmap.md`](docs/next-phases-roadmap.md) — Phase 11.6
documents every stage shipped 2026-05-12 through 2026-05-18 with commit hashes.

> **Paper-trading vocabulary only.** The bot says **WATCH / RESEARCH / AVOID_CHASE / CHASE_RISK** —
> never "BUY" or "SELL" — until a rule's calibration crosses the **production maturity gate**
> (`accuracy ≥ 0.90 AND n_observations ≥ 30`). A **training tier** at `accuracy ≥ 0.70` exists as
> a parallel surface for visibility but does not unlock BUY/SELL emission. Educational use only;
> not financial advice.

## Why this exists

A solopreneur cannot beat institutional HFT desks on speed or capital. What a solopreneur CAN
beat them on is **discipline + niche discovery + capital preservation**. This system encodes
those three constraints in code:

- **Layer 4 (`risk_agent`) has hardcoded survival rules.** Van Tharp position sizing
  (Capital × Risk% / Stop Distance), drawdown circuit breaker at −10% 30-day mean realized,
  daily risk budget at 3% NAV in flight, concentration cap at 3 open positions per rule_key.
  Constants are module-level Python — not configurable from env or DB — so a misconfigured
  env can never relax them.
- **Maturity gate prevents premature BUY/SELL.** The vocabulary stays in paper terms until
  outcomes prove edge.
- **Calibration tracks payoff, not just accuracy.** A 55% rule with 5:1 win/loss beats a 90%
  rule with 1:5 win/loss. `stock_rule_calibration` records `profit_factor`, `mfe_pct`,
  `mae_pct`, `target_hit_rate`, `stop_hit_rate`, `avg_win_pct`, `avg_loss_pct`.
- **Backtester has out-of-sample validation built in.** Chronological 70/30 split with an
  explicit `OVERFIT_RISK_HIGH` verdict if test-half performance drops by ≥10 pts.
- **Insider edge follows Cohen/Lou (2012).** `activist_insider_agent` escalates severity
  3 → 4 when a 3+ Form-4-buyer cluster fires within 15% of the ticker's 52-week low — the
  single largest documented retail edge in the academic literature.

## Detailed design docs

- [`docs/market-intelligence-platform-design.md`](docs/market-intelligence-platform-design.md) — original platform design
- [`docs/technical-architecture.md`](docs/technical-architecture.md) — runtime topology, sequence diagrams
- [`docs/PHASE0_CHECKLIST.md`](docs/PHASE0_CHECKLIST.md) — initial setup
- [`docs/phase9-tiered-storage.md`](docs/phase9-tiered-storage.md) — tiered-storage plan
- [`docs/multi-domain-roadmap.md`](docs/multi-domain-roadmap.md) — six domain-agent expansion
- [`docs/next-phases-roadmap.md`](docs/next-phases-roadmap.md) — phase ledger + open backlog
- [`docs/RUNBOOK.md`](docs/RUNBOOK.md) — autonomous operation, failure modes, recovery

## Stack

| Layer | Service | Cost |
|---|---|---|
| Compute | GitHub Actions (public repo, unlimited minutes) | $0 |
| DB + auth | Supabase Free | $0 |
| Push notifications | Telegram Bot API | $0 |
| Static frontend | Hostinger shared hosting (FTPS auto-deploy) | already paid |
| Domain | hub4apps.com | already paid |

## Architecture

The current technical diagram and operational runbook live in
[`docs/technical-architecture.md`](docs/technical-architecture.md). It covers:

- runtime topology across GitHub Actions, Supabase, Telegram, and Hostinger
- live signal and paper forecast sequence
- historical backfill, backtest, and `shadow_backtest` replay path
- core table responsibilities and forecast modes
- current verification snapshot from the Phase 6B rollout

## Pipeline (25 GitHub Actions jobs across 6 layers)

`ls .github/workflows/*.yml | wc -l` for the live count. Layers are isolated:
ingest → intelligence → trade construction → risk → learning → presentation.
Each layer reads from layers below and writes only to its own output table.

**Layer 3+4 added 2026-05-18:** `trade_setup_agent` (signal → tradable setup)
and `risk_agent` (Van Tharp position sizing + drawdown circuit breaker +
hardcoded daily risk budget). All decisions are paper-only; a broker
adapter is explicitly NOT in this cycle.

| Agent | Schedule | What it does |
|---|---|---|
| `intraday_alert_agent`| `*/15 13-21 * * 1-5` | **Fast-twitch spike detector** — scans every tradeable ticker every 15 min during US market hours. Detects intraday ≥5% moves (or ≥2% with 2× volume) via yfinance, pulls recent normalized-event context (real `event_at` date), and sends an immediate Telegram alert with the catalyst summary. LITE-style +17% moves now ping within 15 min instead of 3 hours late. Dedupes one alert per ticker per UTC day. |
| `filing_agent`        | `*/5 * * * *`     | EDGAR → 8-K (with item parsing), 10-K/Q, Form 4, 13D/G, S-3 → `stock_raw_filings` + `stock_normalized_events` |
| `news_agent`          | `*/5 * * * *`     | CNBC / MarketWatch / Seeking Alpha RSS → `stock_raw_news` + DB-loaded keyword rules → `stock_normalized_events` |
| `truth_social_agent`  | `*/5 * * * *`     | Trump Truth Social RSS → DB-loaded keyword rules → `stock_normalized_events` |
| `thesis_agent`        | `*/5 * * * *`     | Cluster rule (≥2 agents, 30-min bucket) → 100-pt weighted score → action. Reads `stock_agent_weights` (per-agent EMA) AND `stock_rule_calibration` (per-rule paper-trade accuracy). **Intelligence layer** applies four cross-rule bonuses: `sector_cluster_bonus` (+20 when 2+ same-sector watchlist peers fire same direction), `hyperscaler_capex_echo` (+12 when MSFT/GOOG/META/AMZN/ORCL 8-K mentions capex → fans out to mapped suppliers), `power_scarcity_active` (+15 when CEG/VST/TLN/NRG file sev-4 8-K → boosts AI compute/servers/optical), and `risk_off` filter (VIX > 25 tightens bullish thresholds +10 pts). Severity-4 events bypass the 5-alerts/day cap. When a cluster contains a **mature rule** (accuracy ≥ 0.90, n ≥ 30), action escalates from WATCH → **BUY** or AVOID_CHASE → **SELL**. |
| `earnings_agent`      | weekly Sun 12:00 UTC | yfinance earnings dates per stock → `stock_normalized_events` |
| `event_paper_agent`   | hourly (5 min in) | Every event with severity ≥ 2 (150-min lookback) becomes a paper trade in `stock_event_paper_trades` with status=open, entry_price from `stock_raw_prices` (yfinance fallback writes back to DB self-healing), horizon=1d/7d/15d/30d. Filters by `created_at` (ingest time) not `event_at` so historical filings ingested today are picked up correctly. Pre-filters event_ids that already have trades for idempotency (PostgREST partial-index workaround). Supports `--backfill-days N` to replay events from the last N days. |
| `price_agent`         | weekday 21:30 UTC | yfinance EOD closes → close mature signals + EMA weight update + close mature paper trades + update `stock_rule_calibration`. Flags rules that crossed the maturity gate. |
| `market_scanner_agent`| weekday 21:30 UTC | Tracked-stock daily scan: any \|move\|≥3% → `stock_event_outcome_observations` (correlation rows for future calibration QA) |
| `crypto_macro_agent`  | weekday 21:35 UTC | BTC/ETH daily probe → emits `crypto_macro_move` events for COIN/MSTR when \|move\|≥2% (lowered from 5% so signals actually fire). |
| `flows_agent`         | weekly Sun 14:00 UTC | Parses 13F-HR information_table.xml from Berkshire / BlackRock / Vanguard / Bridgewater / Scion / Pershing Square. Diffs vs prior quarter snapshot → `institutional_new_position` / `exit` / `increase` / `decrease` events keyed to the affected ticker. |
| `paper_trade_agent`   | `*/15 * * * *`    | Codex's earlier path: signal-driven calibrated forecasts in `stock_paper_forecasts` (separate table from `stock_event_paper_trades`). Both run in parallel. |
| `trade_setup_agent`   | `*/30 * * * *` + workflow_run on `thesis_agent` | **Layer 3 — trade construction.** Reads recent `stock_signals`, derives a tradable proposal per signal (entry style: next_open / limit_pullback / breakout / vwap_band; stop_pct, target_pct, valid_until, confidence). Writes `stock_trade_setups`. Populates `reason_to_skip` when expired signal, AVOID_CHASE/CHASE_RISK action, neutral direction, or rule lacks edge — those rows still get written for audit but the risk_agent skips them. |
| `risk_agent`          | `*/30 * * * *` + workflow_run on `trade_setup_agent` | **Layer 4 — capital allocation / survival.** Reads `stock_trade_setups`, applies HARDCODED rules (Van Tharp position sizing, drawdown circuit breaker at -10% 30d mean, daily risk budget at 3% NAV, concentration cap at 3 open per rule_key, stop sanity at [0.5%, 20%]). Writes `stock_risk_decisions` with `decision in ('size','skip')`, `size_pct_portfolio`, `max_loss_dollars`, and a `rules_applied` JSONB audit trail of every gate that ran. Paper-only / advisory; broker adapter not connected. |
| `backtester`          | manual only       | 180-day replay → precision/calibration metrics |
| `site_generator`      | `*/15 * * * *` + workflow_run on 7 upstream agents | Supabase → Jinja2 HTML → FTPS auto-deploy to Hostinger. workflow_run chain reduces cron drift impact (cron is best-effort on GHA). Emits machine-readable `status.json` v1.1 with tiered maturity. |
| `source_review_agent` | `0 13 1 * *`      | Monthly health check on every external feed → Telegram digest |

### One-time helper

| Script | When | Purpose |
|---|---|---|
| `historical_ingest.py` | Run once via `historical_ingest.yml` workflow_dispatch | 6-month backfill of EDGAR filings + earnings + daily prices so the backtester has a real history to learn from |

## Watchlist universe (Phase 10 — AI cluster expansion)

The pipeline tracks 56 stocks + 15 ETFs across categorized watchlists. The AI cluster
was added 2026-05-11; each subdomain is a separate watchlist so cross-domain signals
(sector cluster bonus, hyperscaler echo) can identify peer co-movement.

| Watchlist | Tickers |
|---|---|
| `core` | AAPL, AMD, AMZN, AVGO, BRK.B, COST, DJT, EBAY, GOOG, GOOGL, INTC, JNJ, JPM, LLY, MA, META, MSFT, NFLX, NVDA, TSLA, V, WMT, XOM |
| `context` | COIN, FXI, MSTR, QQQ, SPY, TLT, VIX, XLB, XLC, XLE, XLF, XLI, XLK, XLP, XLRE, XLU, XLV, XLY |
| `ai_compute`  | MU, MRVL, TSM, ASML, ALAB |
| `ai_optical`  | LITE, COHR, CIEN, ANET, GLW, FN, CRDO |
| `ai_servers`  | SMCI, DELL, HPE, VRT, NVT, MOD |
| `ai_power`    | CEG, VST, GEV, ETN, TLN, NRG |
| `ai_software` | ORCL, PLTR, CRWD, NOW, SNOW, AI, PATH |
| `ai_neocloud` | IREN, CRWV |
| `institutions` | INST_BLK, INST_BRDGW, INST_BRK, INST_PERSH, INST_SCION, INST_VG (13F holders, not tradeable) |
| `mutual_funds` | FXAIX, SWPPX, VFIAX, VTSAX |

Multi-domain expansion (defense, biotech, energy_transition, macro_rates, activist_insider,
consumer_health) is mapped in [`docs/multi-domain-roadmap.md`](docs/multi-domain-roadmap.md).

## Signal vocabulary

Paper-only (default — what the bot uses on day 1):
- **WATCH**       — score ≥70, bullish cluster (≥2 agents agree)
- **RESEARCH**    — score ≥50, single strong signal or weaker cluster
- **AVOID_CHASE** — score ≥50, bearish cluster (S-3, 8-K dilution, earnings miss, downgrade, bearish news)
- **CHASE_RISK**  — would-be WATCH/RESEARCH on a ticker that already moved >5% since the cluster's earliest event. Recorded for review, not dispatched.

Graduated vocabulary (Phase 7 maturity gate):
- **BUY**  — score ≥70 bullish AND the cluster contains a rule whose paper-trade accuracy crossed ≥90% with n≥30 closed observations (per `stock_rule_calibration`)
- **SELL** — score ≥50 bearish AND the cluster contains a mature rule

The system stays on the paper-only vocabulary until a rule earns BUY/SELL through measured accuracy. See the **Calibration** dashboard tab for live status.

## Paper forecast vocabulary

- **PAPER_LONG** — calibrated probability and expected value are positive with enough similar historical samples
- **PAPER_WATCH** — interesting setup, but sample size or edge is not strong enough
- **PAPER_AVOID** — negative expected value, bearish setup, or poor historical follow-through
- **PAPER_CHASE_RISK** — signal exists, but the move is likely already priced in
- **NO_TRADE** — insufficient comparable history for a realistic paper forecast

Forecast modes:

- **live** — generated only from live `candidate` / `sent` / `suppressed` signals; these count toward real paper-trading review
- **shadow_backtest** — generated from already-audited historical backtest signals, replayed day-by-day so each day only learns from outcomes computed before that replay day; useful for validation, not counted as live paper-trading performance

Probability caveats:

- Forecast outcomes use the paper-only contract `next_session_open_to_horizon_close` with 5 bps per-side slippage.
- `target_price` and `stop_price` are display assumptions until intraday high/low target-stop auditing is added.
- Probabilities are shown with setup sample sizes; sparse setups are learning examples, not calibrated confidence.

## Dashboard tabs

`Dashboard · Signals · Events · Agents · Backtest · Paper Trades · Calibration · Learning` — plus
per-ticker chart pages under `/ticker/{TICKER}.html` (180-day price + filing/earnings overlay +
"Big Moves" explanations) and per-alert detail pages under `/alert/{id}.html` for the link in
every Telegram message. The new **Calibration** tab surfaces per-rule paper-trade accuracy and
flags any rule that's crossed the BUY/SELL maturity gate.

## Layout

```
.
├── docs/                       Design doc + Phase 0 checklist
│   └── technical-architecture.md  Current diagrams + runbook
├── sql/                        Supabase migrations (run in order)
├── agents/                     One Python file per agent
├── .github/workflows/          One YAML per agent (cron-scheduled)
├── templates/                  Jinja2 for static-site generator
└── dist/                       Generated HTML (deployed by FTPS; not committed)
```

## SQL migrations (run in order in Supabase SQL Editor)

- `sql/0001_initial_schema.sql`
- `sql/0002_seed_universe.sql`
- `sql/0003_add_kind_and_funds.sql`
- `sql/0004_ops_tables.sql`
- `sql/0005_extend_status_and_data_sources.sql` — `status_v2='backtest'` + data sources registry
- `sql/0006_add_closed_status.sql` — `status_v2='closed'` for matured signals (price_agent loop)
- `sql/0007_allow_chase_risk.sql` — `action='CHASE_RISK'` plus latest signal status constraint
- `sql/0008_paper_forecasts.sql` — Phase 6A calibrated paper forecast table
- `sql/0009_paper_forecast_modes.sql` — separates live paper forecasts from historical `shadow_backtest` replay rows
- `sql/0010_reliability_and_calibration.sql` — audit/evidence uniqueness, dispatch retry status, outcome-price fields, source registry refresh, calibration summary view
- `sql/0011_keyword_rules.sql` — DB-editable keyword routing for news + Truth Social (`stock_keyword_rules`); seeds with the existing hardcoded rules so behavior is identical day-1
- `sql/0012_event_outcome_observations.sql` — per-day per-ticker (move %, prior event) rows from `market_scanner_agent` for future scoring calibration
- `sql/0013_event_outcome_observations_uniq_fix.sql` — replaces the partial unique index from 0012 with a non-partial one so PostgREST's `ON CONFLICT` upsert resolves correctly
- `sql/0014_event_paper_trades_and_calibration.sql` — Phase 7 closed learning loop: `stock_event_paper_trades`, `stock_rule_calibration`, plus BUY/SELL allowed in `stock_signals.action`
- `sql/0015_event_paper_trades_uniq_fix.sql` — non-partial unique index on `(event_id, ticker, direction)` so `ON CONFLICT` works (same lesson as `0013`)
- `sql/0016_holistic_review_fixes.sql` — RLS for `stock_job_runs` / `stock_dead_letter_events` / `stock_data_sources`, `stock_signals(status_v2, fired_at)` index for hot path, restore `TRIM` to the action allow-list
- `sql/0017_institutional_holdings_snapshot.sql` — Phase 8 `stock_institutional_holdings_snapshot` for `flows_agent` per-quarter 13F snapshots
- `sql/0018_intc_ebay_and_horizon_index.sql` — adds INTC + EBAY to core watchlist; widens unique index on `stock_event_paper_trades` to include `horizon_days` so the new multi-horizon paper trades coexist
- `sql/0019_retention_columns.sql` — tiered storage retention markers (Phase 9 prep)
- `sql/0020_sector_etf_symbols.sql` — adds XLY, XLB, XLC, XLP, XLV, XLU, XLRE to `stock_symbols` so `truth_social_agent` tariff/macro posts on sector ETFs generate paper trades (previously dropped by the watchlist JOIN). Direct DB inserts already applied; this file is the canonical migration.
- `sql/0021_normalized_events_perf_indexes.sql` — performance indexes on `stock_normalized_events` for hot freshness/window queries.
- `sql/0022_run_lineage.sql` — adds `parent_run_id`, `run_type` (`'agent'`/`'wrapper'`), `stage` to `stock_job_runs` so the dashboard health metric distinguishes agent code rows from yaml wrapper rows. Backfills historical `workflow_*` rows.
- `sql/0023_signal_validity.sql` — adds `valid_until` to `stock_signals` for alpha-decay TTL. `thesis_agent` populates per event type (news 4h, filings 7-14d, FDA/clinical 14d).
- `sql/0024_calibration_payoff.sql` — extends `stock_rule_calibration` with `median_return_pct`, `avg_win_pct`, `avg_loss_pct`, `profit_factor`, `target_hit_rate`, `stop_hit_rate`, `mean_mfe_pct`, `mean_mae_pct`. Adds `mfe_pct`, `mae_pct`, `target_hit`, `stop_hit` per-trade fields. `price_agent.recompute_rule_payoff` populates aggregates each reconciliation pass.
- `sql/0025_trade_setups.sql` — **NEW LAYER 3 table** `stock_trade_setups` (Stage 5, 2026-05-18). One setup per signal, idempotent via `unique(signal_id)`. Written by `trade_setup_agent`.
- `sql/0026_risk_decisions.sql` — **NEW LAYER 4 table** `stock_risk_decisions` (Stage 6, 2026-05-18). One decision per setup, idempotent via `unique(setup_id)`. Written by `risk_agent` with hardcoded survival rules and a `rules_applied` JSONB audit trail.

## GitHub Actions secrets

| Secret | Used by | Notes |
|---|---|---|
| `SUPABASE_URL`         | every agent | project URL, e.g. `https://abc.supabase.co` |
| `SUPABASE_SERVICE_KEY` | every agent | service-role key (bypasses RLS) |
| `EDGAR_USER_AGENT`     | filing_agent, historical_ingest | required by SEC fair-access policy |
| `TELEGRAM_BOT_TOKEN`   | thesis_agent, price_agent | bot via @BotFather |
| `TELEGRAM_CHAT_ID`     | thesis_agent, price_agent | private chat id |
| `HOSTINGER_FTP_USER`   | site_generator | FTPS user — see deployment notes below |
| `HOSTINGER_FTP_PASS`   | site_generator | FTPS password |

## Deployment notes — Hostinger FTPS

Pinned in `.github/workflows/site_generator.yml`:

- **server:** `ftp.hub4apps.com`
- **port:** `21` with `protocol: ftps` (Hostinger requires explicit TLS; plain FTP returns 530)
- **timeout:** `120000` ms because Hostinger can take longer than the action's default control-socket timeout
- **server-dir:** `./` because the `u832160935.stock_app` FTP user is chrooted directly at `/public_html/stock_app/`
- **dangerous-clean-slate:** `false` (never wipe the deploy target)

### Choosing an FTP user

The deploy needs **write permission to `/public_html/stock_app/`**. Two options:

| Option | User | Pros | Cons |
|---|---|---|---|
| Sub-account | `u832160935.stock_app` | Least-privilege, scoped chroot directly at the served folder | Must keep `server-dir: ./` because this user already lands inside `/public_html/stock_app/`. |
| Main account | `u832160935` | Always has write access everywhere | Wider blast radius if the deploy ever misbehaves; some Hostinger plans disable FTPS on the main account |

We currently run on the scoped `u832160935.stock_app` sub-account.

### CSP gotcha — vendor JS locally

Hostinger LiteSpeed sends a default `Content-Security-Policy: ... script-src 'self'` header.
External CDNs and inline chart-binding scripts are blocked unless overridden, so the dashboard
vendors Chart.js in `templates/vendor/` and writes a generated `.htaccess` that restricts assets
to `self` while allowing the current inline scripts. JSON embedded in pages is emitted with
Jinja `tojson`, not raw `|safe`.

## Bootstrap order (cold start)

1. Apply all `sql/*.sql` migrations in Supabase
2. Add the secrets above
3. In Hostinger: create FTP sub-account, then `chmod 775 /public_html/stock_app/`
   (or just use the main FTP account — see deployment notes above)
4. Push to `main` — every cron-scheduled workflow starts running
5. Run `historical_ingest.yml` once with `sections=all` (6-month backfill)
6. Run `backtester.yml` once to populate `stock_agent_weights` and `stock_forecast_audit`
7. Run `paper_trade_agent.yml` once with `mode=shadow_30d` to fill 30 days of closed historical shadow forecasts
8. Run `paper_trade_agent.yml` once with `mode=live` to seed `stock_paper_forecasts` from current live signals, if any exist
9. Wait one `*/15` cycle for `site_generator` to render and deploy
10. After a few weeks of live signals, `price_agent`'s EMA loop populates per-agent weights
   that `thesis_agent` then uses to amplify reliable agents

## Future roadmap

The big-picture **multi-domain expansion** plan (six new domain agents: macro_rates,
defense, biotech, energy_transition, activist_insider, consumer_health) is in
[`docs/multi-domain-roadmap.md`](docs/multi-domain-roadmap.md). Build order, ticker
baskets, event types, catalysts, and cross-domain leverage examples are documented there.

The items below are smaller bounded enhancements that don't require a new agent.
Each is bounded effort; none requires a paid API.

| Item | Why deferred | Estimated effort |
|---|---|---|
| **Form 4 buy/sell transaction split** | Form 4 currently lumps insider buys, sells, and option exercises into a single event. Empirical data (n=218) shows the aggregate is bearish on average — but BUYS and SELLS likely have opposite signs. Splitting requires fetching + parsing the Form 4 XML doc (transaction code + shares). Would let `thesis_agent` score insider buys as bullish, sells as bearish, instead of treating both the same. | 3-4h |
| **Intraday "beat-but-falling" detector** | The META / TSLA case: earnings beat fires bullish signal, stock falls anyway because of capex / guidance. Would need a 30-min after-open price check post-earnings; if down despite beat → emit `beat_but_weak` with `direction_prior=short`. Requires intraday price data (yfinance 1-min or 15-min bars). | 2h once intraday data path exists |
| **Hybrid keyword + LLM classifier** | Truth Social / news keyword router goes stale (Iran/Hormuz this week, something else next week). Hybrid: keyword hits use deterministic path; misses fall through to a single Anthropic Haiku call (~$0.10/month at current volume). Deferred per "no API spend" constraint — re-open if you want to revisit. | 2h |
| **Auto-tune scoring weights from observations** | Today, `thesis_agent` rule weights are static constants. With 90 days of `stock_event_outcome_observations` data we can derive empirical weights weekly: `weight = clip(2 × win_rate, 0.1, 2.0)`. Closes the loop at the rubric level, not just per-agent. | 3h |
| **Mutual fund NAV-based paper trades** | `event_paper_agent` skips kind=mutual_fund (NAV pricing, T+1 settlement). Would need a separate paper-trade contract: entry at next NAV, exit at horizon NAV. Fits if you ever start trading VFIAX / SWPPX / FXAIX directly. | 3h |
| **Expanded crypto-correlated tickers** | `crypto_macro_agent` currently emits events for COIN + MSTR only. Adding MARA, RIOT, IBIT, FBTC would cover more crypto-correlated coverage gaps. Requires adding them to the watchlist first. | 30min once tickers are in watchlist |
| **OpenFIGI CUSIP → ticker integration** | `flows_agent` matches 13F holdings by company name, which works for our mega-cap watchlist but misses anything where the SEC name doesn't normalize cleanly. OpenFIGI's free API (25 req/sec) gives accurate CUSIP→ticker mapping. Would let `flows_agent` cover Berkshire's smaller positions too. | 2h |
| **Activist 13D activity tracker** | When Pershing Square / Scion file an SC 13D on a NEW target (one we don't yet track), it might be worth adding that ticker dynamically to the watchlist. Currently their 13Ds on non-watchlist tickers get logged but produce no events. | 2h |
| **Real BUY/SELL graduation digest** | When a rule crosses the maturity gate (>=90% accuracy, n>=30) and `is_mature` flips true, send a Telegram alert: "Rule X has graduated to BUY/SELL. Next signal containing this rule will fire as BUY/SELL instead of WATCH/AVOID_CHASE." Currently surfaces in the Calibration tab only. | 30min |
| **Calibration → SQL nightly aggregation cron** | Replace ad-hoc SQL with a nightly view-refresh that pre-computes accuracy, mean return, sample size per `(rule_key, lookback_window)`. Removes load from `thesis_agent` reads. | 1h |
| **Tiered storage — passive history off Supabase Free** *(Phase 9, **v1 design locked**, full design in [`docs/phase9-tiered-storage.md`](docs/phase9-tiered-storage.md))* | Closed paper trades, prices > 90 days, and 13F snapshots > 1 year live in passive storage (Hostinger 25 GB FTPS-archived JSONL.gz at `hub4apps.com/stock_app/archive/`; Mac local sync via `bin/stock_app_sync.sh`). Active tables (open trades, recent events, agent_weights) stay in Supabase. Lets the system carry years of training data without ever paying Supabase Pro. Calibration cron reads BOTH active + archive when computing per-rule accuracy. v1 also ships a weekly Telegram archive digest (rows archived, % free-tier headroom). | ~7.5h, ships incrementally |
| **S&P 100 watchlist expansion (~70 more tickers)** | Track the next tier of liquid names beyond our current 30 mega-caps + ETFs. Catches more earnings drama, more 13F positions matching, more news mentions. Storage impact: ~3× event volume, ~3× paper trades. Pairs naturally with the tiered storage item above. CIKs need fetching from SEC's `company_tickers.json`. | 2h once tiered storage is in place |
| **Auto-add M&A targets dynamically** | When `news_article` mentions an "acquisition" + a non-watchlist ticker, add that ticker to a temporary `mna_watchlist` so subsequent news/filings get tracked. Useful for catching eBay-style M&A moves we currently miss. | 2h |

## Security note

Agents read secrets only via GitHub Actions `secrets.*` — never committed to the repo.
Local Claude Code tool permissions live in `.claude/settings.local.json` (git-ignored).
Do not commit that file; it may contain local API keys used for interactive verification.
