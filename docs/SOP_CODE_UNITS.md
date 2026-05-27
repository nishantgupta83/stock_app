# SOP — Code Units Catalog

Canonical reference for every Python code unit in the `stock_app` pipeline.
Organized by the six-layer architecture in `CLAUDE.md`, plus operational and
helper modules. Each entry lists Purpose, Input, Processing, and Output so a
new contributor (or the user, six months from now) can reason about any agent
without re-reading source.

**Last updated:** 2026-05-26

## How to use this SOP

When debugging or extending, find the agent by layer below and read its four
fields top-to-bottom — they describe what the unit promises to do, where its
data comes from, the key algorithm, and where its output lands. The
**Workflow → Agent mapping** table at the bottom is the fastest way to go
from a YAML cron schedule back to a Python module. This document describes
what currently exists; it is not a backlog of proposed work.

## Table of contents

- [Layer 1 — Ingest](#layer-1--ingest)
  - [filing_agent.py](#filing_agentpy)
  - [news_agent.py](#news_agentpy)
  - [truth_social_agent.py](#truth_social_agentpy)
  - [earnings_agent.py](#earnings_agentpy)
  - [crypto_macro_agent.py](#crypto_macro_agentpy)
  - [flows_agent.py](#flows_agentpy)
  - [biotech_agent.py](#biotech_agentpy)
  - [defense_agent.py](#defense_agentpy)
  - [energy_transition_agent.py](#energy_transition_agentpy)
  - [activist_insider_agent.py](#activist_insider_agentpy)
  - [consumer_health_agent.py](#consumer_health_agentpy)
  - [macro_rates_agent.py](#macro_rates_agentpy)
  - [intraday_alert_agent.py](#intraday_alert_agentpy)
  - [market_scanner_agent.py](#market_scanner_agentpy)
- [Layer 2 — Intelligence](#layer-2--intelligence)
  - [thesis_agent.py](#thesis_agentpy)
- [Layer 3 — Trade Construction](#layer-3--trade-construction)
  - [trade_setup_agent.py](#trade_setup_agentpy)
- [Layer 4 — Risk / Capital Allocation](#layer-4--risk--capital-allocation)
  - [risk_agent.py](#risk_agentpy)
- [Layer 5 — Learning](#layer-5--learning)
  - [event_paper_agent.py](#event_paper_agentpy)
  - [price_agent.py](#price_agentpy)
  - [paper_trade_agent.py](#paper_trade_agentpy)
  - [backtester.py](#backtesterpy)
- [Layer 6 — Presentation](#layer-6--presentation)
  - [site_generator.py](#site_generatorpy)
  - [telegram_dispatcher.py](#telegram_dispatcherpy)
- [Operational](#operational)
  - [orchestrator_agent.py](#orchestrator_agentpy)
  - [audit_agent.py](#audit_agentpy)
  - [archive_agent.py](#archive_agentpy)
  - [source_review_agent.py](#source_review_agentpy)
  - [historical_ingest.py](#historical_ingestpy)
  - [ops_recorder.py](#ops_recorderpy)
  - [_catalyst_policy.py](#_catalyst_policypy)
  - [_market_calendar.py](#_market_calendarpy)
  - [_rule_key.py](#_rule_keypy)
- [Scripts](#scripts)
  - [learning_snapshot.py](#learning_snapshotpy)
  - [backfill_paper_trades.py](#backfill_paper_tradespy)
  - [bootstrap_cronjob_org.py](#bootstrap_cronjob_orgpy)
  - [bin/stock_app_sync.sh](#binstock_app_syncsh)
- [Workflow → Agent mapping](#workflow--agent-mapping)

---

## Layer 1 — Ingest

All Layer 1 agents share one invariant: they read from external sources, write
raw rows to source-specific tables (when present), and emit normalized rows to
`stock_normalized_events`. They never read from `stock_signals` or anything
above. Most reuse `filing_agent`'s `job_run_start` / `job_run_finish` /
`dead_letter` helpers, so a stock-symbol watchlist filter and the
`run_type='agent'` ops-log row are consistent across the layer.

### filing_agent.py

- **Name:** `agents/filing_agent.py` — EDGAR filing ingest.
- **Purpose:** Poll SEC EDGAR for new filings on the watchlist universe
  (operating companies, institutions, mutual funds) and emit each filing both
  as a raw row and a normalized event.
- **Input:**
  - Reads `stock_watchlists` joined to `stock_symbols` for `(ticker, cik, kind)`
    where the symbol has a CIK and the watchlist name is in
    `(core, institutions, mutual_funds)`.
  - Calls `https://data.sec.gov/submissions/CIK*.json` with the
    `EDGAR_USER_AGENT` env var (required by SEC fair-access policy).
  - Triggered by `filing_agent.yml` cron `*/5 * * * *`.
  - Env: `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `EDGAR_USER_AGENT`.
- **Processing:**
  - Per-CIK iterate the EDGAR `submissions` endpoint and keep only filings
    whose `form` is in the kind-specific `FORMS_BY_KIND` set (8-K, 10-Q,
    13D/G family, Form 4, S-3 for stocks; 13F-HR for institutions; N-PORT
    family for mutual funds).
  - Dedupe by `accession_number` (unique index on `stock_raw_filings`).
  - For each new filing, also emit a `stock_normalized_events` row whose
    `event_type` derives from the form (e.g. `8k_material_event`,
    `filing_13d`, `filing_4`, `filing_s-3`) with severity heuristics.
  - Exposes shared helpers used by every other Layer 1 agent: `job_run_start`,
    `job_run_finish`, `dead_letter`, plus the `HEADERS_SB` / `HEADERS_SEC`
    constants.
- **Output:**
  - Writes `stock_raw_filings` and `stock_normalized_events`.
  - Records lifecycle in `stock_job_runs` (run_type='agent').
  - Downstream: `thesis_agent`, `event_paper_agent`, `flows_agent` (13F-HR
    parser), `activist_insider_agent` (13D/Form 4 cluster detector).

### news_agent.py

- **Name:** `agents/news_agent.py` — Free-RSS news ingest + ticker/sentiment classifier.
- **Purpose:** Poll CNBC, MarketWatch, and Seeking Alpha RSS feeds, classify
  each article by the ticker(s) it mentions, and emit one normalized event per
  `(article, affected_ticker)` pair so 8-K + news clusters can satisfy the
  §15.3 cluster rule without requiring a Truth Social post.
- **Input:**
  - Three free RSS endpoints (`_FEEDS` constant).
  - PR1A/PR1B keyword classifier covers 265 company-name aliases and 144
    tickers (see commit `85fcaf7`).
  - Triggered by `news_agent.yml` cron `*/5 * * * *`.
- **Processing:**
  - Dedupe each article by SHA hash of `(feed_source, entry_id)`.
  - Match watchlist tickers via word-boundary regex AND company-name aliases
    (`_COMPANY_MAP`).
  - Apply bullish/bearish keyword regexes to derive `direction_prior`.
  - PR1B causal-keyword classifier (`_catalyst_policy.is_causal_headline`)
    gates whether the headline is even framed as a same-day catalyst — generic
    "discussed in roundup" mentions deliberately do not match.
- **Output:**
  - Writes `stock_raw_news` (one row per article).
  - Writes `stock_normalized_events` (one row per `(article, ticker)` pair,
    `event_type='news_article'`).
  - Downstream: `thesis_agent` (clustering + score), `event_paper_agent`.

### truth_social_agent.py

- **Name:** `agents/truth_social_agent.py` — Truth Social post ingest + DB-driven keyword router.
- **Purpose:** Poll the `trumpstruth.org` RSS mirror and emit one event per
  `(post, affected_ticker)` based on deterministic keyword rules that the user
  can edit in Supabase without redeploying.
- **Input:**
  - `TRUTH_SOCIAL_FEED` env (defaults to `https://trumpstruth.org/feed`).
  - Loads `stock_keyword_rules` where `kind='truth_social'` and `enabled=true`.
    Falls back to `_FALLBACK_RULES` (tariff / china / oil / crypto / DJT) if
    Supabase is unreachable, tagging `source_tag='keyword_fallback'` for
    audit.
  - Cron `*/5 * * * *`.
- **Processing:**
  - Each rule has `keyword`, `match_type` (icontains or regex), `tickers`,
    `direction_prior`, `rule_label`.
  - On match, emit one event per ticker in the rule's basket;
    `payload.direction_prior` carries the rule's bias.
- **Output:**
  - Writes `stock_raw_truth_posts` (raw).
  - Writes `stock_normalized_events` (`event_type='truth_social_post'`).

### earnings_agent.py

- **Name:** `agents/earnings_agent.py` — Weekly earnings calendar refresh.
- **Purpose:** Keep `stock_normalized_events` populated with both upcoming and
  recently-released earnings dates per tradeable ticker. `historical_ingest`
  did the one-time 6-month backfill; this is the recurring refresh.
- **Input:**
  - `kind='stock'` tickers from `stock_watchlists`.
  - `yfinance.Ticker(t).get_earnings_dates()` for each.
  - Cron `0 12 * * 0` (Sundays 12:00 UTC).
- **Processing:**
  - Window: 60 days back (catch revisions) + 14 days forward (announced
    upcomings).
  - Upserts on `dedupe_key=earnings_{ticker}_{date}` so a scheduled row later
    becomes a beat/miss/inline row in place.
- **Output:**
  - Writes `stock_normalized_events` (`event_type='earnings_release'` with
    subtype `beat|miss|inline|scheduled`).

### crypto_macro_agent.py

- **Name:** `agents/crypto_macro_agent.py` — Daily BTC/ETH macro-move emitter.
- **Purpose:** Close the largest coverage gap surfaced by the 90-day
  market_scanner backfill (75% of >3% moves on COIN/MSTR/IBIT had no tracked
  event — they move with BTC). Emit one event per crypto-correlated ticker
  when BTC or ETH closes ±2% or more.
- **Input:**
  - `yfinance` daily bars for `BTC-USD`, `ETH-USD`.
  - Cron `35 21 * * 1-5` (weekday 21:35 UTC, after price closes settle).
- **Processing:**
  - Threshold: `MOVE_THRESHOLD = 0.02`.
  - For each crypto ticker (currently COIN, MSTR) emit one
    `crypto_macro_move` event with `direction_prior` matching the move sign.
- **Output:**
  - Writes `stock_normalized_events` (`event_type='crypto_macro_move'`).

### flows_agent.py

- **Name:** `agents/flows_agent.py` — 13F-HR institutional holdings parser.
- **Purpose:** Bridge the gap where `filing_agent` ingests Berkshire /
  Bridgewater / Scion / BlackRock / Vanguard / Pershing 13F filings keyed to
  placeholder tickers but never propagates the actual affected stock tickers
  downstream.
- **Input:**
  - 13F-HR filings on tracked institution CIKs (`INSTITUTION_LABELS`).
  - Fetches each filing's `index.json` → `information_table.xml` from EDGAR.
  - Cron `0 14 * * 0` (Sundays 14:00 UTC, after weekend SEC catch-up).
- **Processing:**
  - Parse `<infoTable>` XML → name, cusip, shares, value per position.
  - Fuzzy-match issuer name to watchlist company names.
  - Diff vs prior-quarter snapshot in
    `stock_institutional_holdings_snapshot` to classify each position as new
    / exit / increase (>25% QoQ) / decrease (>25% QoQ).
  - Activist 5% threshold crossing emitted as `activist_5pct_crossed`.
  - `event_subtype` is suffixed with institution label (BRK / BLK / SCION /
    BRDGW / PERSH) so calibration tracks per-institution accuracy.
- **Output:**
  - Writes `stock_institutional_holdings_snapshot`.
  - Writes `stock_normalized_events` (`institutional_new_position`,
    `institutional_exit`, `institutional_increase`,
    `institutional_decrease`, `activist_5pct_crossed`).

### biotech_agent.py

- **Name:** `agents/biotech_agent.py` — FDA + clinical catalyst ingest.
- **Purpose:** Detect FDA approval / rejection news and Phase 3 clinical
  readouts on the biotech watchlist; both classes regularly produce
  severity-4 events.
- **Input:**
  - `clinicaltrials.gov/api/v2/studies` (Phase 3 trials, recent status
    changes).
  - FDA press releases RSS.
  - `BIOTECH_ALIASES` map sponsor names back to tickers.
  - Cron `0 14 * * 1-5`.
- **Processing:**
  - Approval / authorization keywords → bullish severity-4
    `fda_pdufa_decision` or `clinical_readout`.
  - Complete Response Letter / rejection keywords → bearish severity-4.
- **Output:**
  - Writes `stock_normalized_events`.
  - Immediate Telegram on severity-4 hits (FDA decision, Phase 3 readout).

### defense_agent.py

- **Name:** `agents/defense_agent.py` — DoD contract awards ingest.
- **Purpose:** Detect Department of Defense contract awards on tracked
  primes / drone makers / cyber-defense names and grade severity by award
  size.
- **Input:**
  - `defense.gov` contracts RSS.
  - `DEFENSE_ALIASES` (LMT/RTX/NOC/GD/BA/HII/LHX/AVAV/KTOS/RKLB/PANW/CRWD/
    FTNT/NET/ZS).
  - Cron `30 22 * * 1-5`.
- **Processing:**
  - Severity gradient: `>=$1B` → sev-4 + immediate Telegram; `$50M-$1B` →
    sev-3; `<$50M` → sev-2.
  - Award amount parsed via `CONTRACT_AMT_RE`.
- **Output:**
  - Writes `stock_normalized_events` (`event_type='dod_contract_award'`).
  - Direct Telegram on $1B+ hits (severity-4 bypass of MAX_ALERTS_PER_DAY).

### energy_transition_agent.py

- **Name:** `agents/energy_transition_agent.py` — Nuclear / EV / solar policy signal.
- **Purpose:** Catch NRC nuclear license actions plus boost severity on
  energy-transition tickers when policy keywords appear in upstream news.
- **Input:**
  - `nrc.gov` news RSS.
  - Sub-watchlist mappings: `ev_makers`, `solar`, `battery_storage`,
    `nuclear`, `charging_infra`.
  - Cron `45 13 * * 1-5`.
- **Processing:**
  - NRC RSS → `nuclear_license_approval` event when keywords like "license"
    / "construction permit" / "SMR" appear.
  - Severity-boost path for IRA / Section 45X / EV-mandate news on
    energy-tier tickers (uses `news_agent` output as upstream).
- **Output:**
  - Writes `stock_normalized_events` (`nuclear_license_approval` and
    severity-boosted overlays).
  - Telegram on NRC approval (bullish nuclear) or EV monthly miss >10%.

### activist_insider_agent.py

- **Name:** `agents/activist_insider_agent.py` — Activist 13D + insider cluster-buy detector.
- **Purpose:** Two highest signal-to-noise paths in equities — both reuse
  `filing_agent`'s existing 13D and Form 4 ingest with no new data source.
- **Input:**
  - `stock_normalized_events` rows with `event_type IN (filing_13d,
    filing_4)` from the last 7-30 days.
  - `TRACKED_ACTIVISTS` (Pershing / Icahn / Elliott / ValueAct / Trian /
    Starboard / Third Point / Scion / Berkshire / Bridgewater).
  - Cron `15 */2 * * *` (every 2 hours).
- **Processing:**
  - Activist 13D: when a tracked activist filer name matches an SC 13D issuer,
    emit `activist_initial_position` (severity-4).
  - Insider cluster buy: 3+ distinct Form 4 filers BUY-side on the same
    ticker within 7 days → `insider_cluster_buy`.
  - Severity-3 by default, escalated to severity-4 if price is within
    `52W_LOW_TOLERANCE_PCT` of 52-week low (Cohen/Lou, Jeng-Metrick-Zeckhauser).
- **Output:**
  - Writes `stock_normalized_events`.
  - Direct Telegram on either type.

### consumer_health_agent.py

- **Name:** `agents/consumer_health_agent.py` — Cycle-sentinel for retail / travel / discretionary.
- **Purpose:** Track three cycle proxies (TSA throughput, retail
  same-store-sales, UMICH sentiment) so we have a leading signal when the
  consumer cycle inflects.
- **Input:**
  - TSA passenger throughput from `tsa.gov`.
  - FRED series `UMCSENT` (UMICH consumer sentiment).
  - Cron `0 15 * * 1-5`.
- **Processing:**
  - TSA YoY ±5% → `traffic_data` event; ±15% → Telegram.
  - UMICH `<60` → panic; `<70` → stressed-consumer overlay; emitted as
    `consumer_sentiment`.
  - Same-store-sales: matches recent 8-K events on `retail_big_box` tickers
    via `news_agent`/`filing_agent` upstream.
- **Output:**
  - Writes `stock_normalized_events` (`traffic_data`, `consumer_sentiment`,
    `same_store_sales`).
  - Telegram on inflection thresholds.

### macro_rates_agent.py

- **Name:** `agents/macro_rates_agent.py` — FRED + VIX upstream regime detector.
- **Purpose:** Emit macro-wide events keyed to the sentinel ticker `MACRO`
  for FOMC / CPI / NFP / yields / VIX so `thesis_agent.is_risk_off()` can
  suppress bullish alerts during regime stress.
- **Input:**
  - FRED API: `DGS10`, `DGS2`, `CPIAUCSL`, `CPILFESL`, `PAYEMS`, `UNRATE`,
    `DFEDTARU`.
  - VIX from existing `stock_raw_prices` (context watchlist).
  - Env: `FRED_API_KEY`.
  - Crons: `0 13 * * 1-5` daily plus `30 18 * * 3` (FOMC announcement
    window).
- **Processing:**
  - `yield_milestone` when 10Y crosses 5% or 10Y-2Y inverts.
  - `cpi_release`, `nfp_release`, `fomc_decision` with severity graded by
    surprise magnitude (sev-2 through sev-4).
  - `vix_spike` at VIX >25 (sev-3) and >35 (sev-4).
- **Output:**
  - Writes `stock_normalized_events` (`ticker='MACRO'`).
  - Direct Telegram on regime changes.
  - Downstream: `thesis_agent.is_risk_off()` reads recent `vix_spike` and
    `yield_milestone` events to gate bullish alerts.

### intraday_alert_agent.py

- **Name:** `agents/intraday_alert_agent.py` — Fast-twitch intraday spike notifier.
- **Purpose:** Catch >=5% intraday moves on any tradeable ticker within
  15 minutes — `thesis_agent` runs every 5 minutes but only dispatches
  mature or clustered signals, so a lone +17% on a non-mature ticker would
  otherwise be invisible.
- **Input:**
  - `yfinance.download` batch over all `kind IN (stock, etf)` watchlist
    tickers.
  - Cron `*/15 13-21 * * 1-5` (US market hours, Mon-Fri).
- **Processing:**
  - `SPIKE_PCT = 0.05` triggers an alert.
  - `VOLUME_MULT = 2.0` is a parallel high-conviction trigger.
  - `ALERT_CAP = 25` per run as safety belt.
  - Dedupe: one alert per ticker per UTC day via
    `dedupe_key='intraday_spike_TICKER_YYYY-MM-DD'`.
  - Uses the shared `_catalyst_policy.split_events_by_role` to attach recent
    catalyst-eligible events as context (so the Telegram body explains WHY
    the move plausibly happened).
- **Output:**
  - Writes `stock_signals` (`action='WATCH'`,
    `model_version='intraday-spike-v1'`).
  - Hands off to `telegram_dispatcher.send_and_log`.

### market_scanner_agent.py

- **Name:** `agents/market_scanner_agent.py` — Daily >=3% move observer + coverage-gap finder.
- **Purpose:** Independent observer that ties every significant daily move
  back to prior 2-day events (if any). Observation-only — never adjusts
  scoring weights — but the aggregated observations tell us which event types
  reliably precede a move and which moves had NO tracked cause (coverage gap).
- **Input:**
  - `stock_raw_prices` and `stock_normalized_events` (already backfilled to
    180 days by `historical_ingest`).
  - Modes: default one-day pass; `--backfill-days N` replays the last N days.
  - Cron `30 21 * * 1-5`.
- **Processing:**
  - `JUMP_PCT = 0.03`, `LOOKBACK_DAYS = 2`.
  - For every ticker move >=3%, write one
    `stock_event_outcome_observations` row per prior event in the 2-day
    window, OR a single NULL-`prior_event_id` row if no event was tracked.
  - Unique index on `(ticker, observed_at, prior_event_id)` collapses
    re-inserts so backfill is idempotent.
- **Output:**
  - Writes `stock_event_outcome_observations`.

---

## Layer 2 — Intelligence

### thesis_agent.py

- **Name:** `agents/thesis_agent.py` — The scoring + clustering brain.
- **Purpose:** Read recent `stock_normalized_events`, apply the §17.7
  100-point rubric and §15.3 cluster rule, decide vocabulary tier
  (`CATALYST_WATCH` / `CATALYST_RESEARCH` / `MOMENTUM_ONLY` / `AVOID_CHASE` /
  `CHASE_RISK`, graduating to `BUY` / `SELL` only when the rule's calibration
  matures), write `stock_signals`, and dispatch via Telegram.
- **Input:**
  - `stock_normalized_events` filtered by `created_at` (not `event_at` —
    see CLAUDE.md rule #1) within `FRESHNESS_WINDOW_MIN = 180`.
  - `stock_rule_calibration` for maturity-tier gating (accuracy ≥0.90, n ≥30
    = production-mature; 0.70 / 30 = training-mature; else immature).
  - `stock_raw_news` directly per-ticker for PR1B catalyst-evidence wiring.
  - `_catalyst_policy.CATALYST_POLICY` for per-event-type role + max age.
  - Cron `*/5 * * * *`.
  - Env: `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`, `TELEGRAM_BOT_TOKEN`,
    `TELEGRAM_CHAT_ID`.
- **Processing:**
  - **Scoring rubric (§17.7, 100-point):** +25 new 8-K (operating company),
    +20 SC 13D, +10 SC 13G, +15 Truth Social mapping, +0..+20 filing
    severity uplift, +5..+40 earnings beat/miss magnitude, -10 staleness
    decay for short-lived social/news events.
  - **Cluster rule:** ≥2 distinct source agents within
    `CLUSTER_WINDOW_MIN=30` (widened from 5 min after the 2026-05-05 AMD
    miss). Single-source exceptions: SC 13D, severity-4 8-K.
  - **PR1A causal-attribution policy:** events partitioned into
    catalyst / context / background by `_catalyst_policy`. A signal with
    `catalyst_score = 0` cannot earn `CATALYST_WATCH` / `CATALYST_RESEARCH`
    — it routes to `MOMENTUM_ONLY` instead (the operator sees an explicit
    "no verified catalyst" tier).
  - **PR1B raw-news evidence:** `stock_raw_news` headlines run through
    `_catalyst_policy.is_causal_headline` (causal-keyword classifier) — when
    a real catalyst headline is found, it promotes the signal back into a
    catalyst tier and is attached as evidence.
  - **Alert governor:** `MAX_ALERTS_PER_DAY = 5` per ticker, 60-min dedupe.
    Severity-4 events bypass the cap (CLAUDE.md rule).
  - **Alpha decay:** `SIGNAL_TTL_HOURS[event_type]` produces a per-signal
    `valid_until` = `fired_at + max(TTL across cluster)`; news decays in
    hours, filings in days, binary catalysts (FDA / clinical / DoD) in
    weeks.
  - **Maturity gate:** Only rules with production-mature calibration can
    surface `BUY`/`SELL`. Everything else stays in the WATCH/RESEARCH/
    AVOID_CHASE/CHASE_RISK/MOMENTUM_ONLY vocabulary.
  - Imports `_rule_key.derive(...)` so its calibration lookup keys match
    what `event_paper_agent` writes — single source of truth fixes the
    historical drift bug (`event_type::h7d` vs `event_type:beat:h7d`).
- **Output:**
  - Writes `stock_signals` (action, score, confidence,
    `evidence_summary`, `score_breakdown`, `valid_until`,
    `model_version='rubric-v1.0'`).
  - Hands off to `telegram_dispatcher.send_and_log` in-process.
  - Downstream: `trade_setup_agent`, `paper_trade_agent`, `site_generator`,
    `event_paper_agent` (for trade open).

---

## Layer 3 — Trade Construction

### trade_setup_agent.py

- **Name:** `agents/trade_setup_agent.py` — Setup constructor.
- **Purpose:** For each fresh `stock_signals` row, decide HOW the trade would
  be entered (entry style, stop, target, validity window). Does NOT decide
  whether to trade — that is `risk_agent`'s job.
- **Input:**
  - `stock_signals` rows within `LOOKBACK_HOURS = 24` that don't already have
    a setup (`unique(signal_id)` constraint).
  - `stock_rule_calibration` for confidence + skip-decision inputs.
  - Cron `*/30 * * * *`.
- **Processing:**
  - Map `event_type → SETUP_TYPE_BY_EVENT`: fast-decay sentiment opens at
    `next_open`; position events use `limit_pullback`; momentum uses
    `breakout`; consumer-sentiment / yield-snapshot use `vwap_band`;
    structurally untradeable events get `manual_skip`.
  - `reason_to_skip` non-null when: signal past `valid_until`; action is
    `AVOID_CHASE`/`CHASE_RISK`; rule has `profit_factor < 1.0` AND not
    training-mature; rule sample `< 5` closed trades AND not mature.
  - Compute stop_pct / target_pct per event-type defaults (mirrors
    `event_paper_agent`'s 3% stop / 5% target unless rule calibration
    suggests otherwise).
- **Output:**
  - Writes `stock_trade_setups` (setup_type, stop_pct, target_pct,
    `valid_until`, confidence, `reason_to_skip`).
  - Downstream: `risk_agent`.

---

## Layer 4 — Risk / Capital Allocation

### risk_agent.py

- **Name:** `agents/risk_agent.py` — Survival layer (capital allocation).
- **Purpose:** For each tradeable `stock_trade_setups` row, decide either
  SIZE (with Van Tharp dollar-risk math) or SKIP, with a fully-auditable
  `rules_applied` JSON trail. The hardcoded survival rules can never be
  relaxed from upstream config.
- **Input:**
  - `stock_trade_setups` within `SETUP_AGE_FLOOR_DAYS = 14`.
  - `stock_event_paper_trades` (open + last-30-day-closed for drawdown +
    daily-risk-in-flight).
  - `stock_rule_calibration` for maturity-tier weighting.
  - Cron `*/30 * * * *`.
- **Processing (HARDCODED constants — never read from env or DB):**
  - **Van Tharp sizing:** `risk_dollars = NAV × RISK_PER_TRADE_PCT ×
    maturity_multiplier`; `size_dollars = risk_dollars / stop_distance_pct`;
    `max_loss_dollars = risk_dollars` (guaranteed by the stop).
  - **Maturity multiplier:** production=1.00, training=0.50, immature=0.25.
  - **Hardcoded constants:** `PORTFOLIO_NAV_BASELINE = 100_000`,
    `RISK_PER_TRADE_PCT = 0.01`, `MAX_DAILY_RISK_PCT = 0.03`,
    `MAX_DRAWDOWN_PCT = 0.10`, `CONFIDENCE_FLOOR = 0.30`,
    `MAX_SAME_RULE_OPEN = 3`, `STOP_PCT_MIN = 0.005`, `STOP_PCT_MAX = 0.20`.
  - **Evaluation order (SKIP wins as soon as one fires):**
    1. Setup self-skipped (`reason_to_skip` set upstream).
    2. Confidence below floor.
    3. Drawdown circuit breaker (last-30d realized losses ≥10%).
    4. Daily risk budget (sum of today's max_loss ≥3% NAV).
    5. Sector concentration (≥3 open trades on same `rule_key`).
    6. Stop-distance sanity (stop must be `(0, 0.20]`).
    7. Otherwise SIZE.
- **Output:**
  - Writes `stock_risk_decisions` (decision=`size`|`skip`, `size_dollars`,
    `max_loss_dollars`, `rules_applied` JSON).
  - This is the FINAL writable output — no agent above it acts on the
    decision automatically; the operator does.

---

## Layer 5 — Learning

### event_paper_agent.py

- **Name:** `agents/event_paper_agent.py` — Multi-horizon paper-trade opener.
- **Purpose:** Convert every significant fresh event into four parallel paper
  trades (1d / 7d / 15d / 30d horizons) so calibration can learn which
  horizon each event type actually rewards.
- **Input:**
  - `stock_normalized_events` filtered by `created_at` within
    `LOOKBACK_MIN = 150` (per CLAUDE.md rule #1).
  - `SEVERITY_FLOOR = 2` (ignore noise).
  - `kind IN (stock, etf)` only (mutual funds use NAV pricing — incompatible
    with next-session-close paper-trade contract).
  - `stock_raw_prices` for entry; `STALE_PRICE_MAX_AGE_DAYS = 3` gates entries
    against stale closes.
  - Cron `5 * * * *` (top of every hour, 5 min in to dodge cron-tower
    contention).
- **Processing:**
  - `HORIZONS = (1, 7, 15, 30)` — one trade per (event, ticker, direction,
    horizon).
  - `TARGET_PCT = 0.05`, `STOP_PCT = 0.03`.
  - Direction: from `payload.direction_prior` first; else
    `_DIRECTION_DEFAULT[event_type]` (filings ≈ long, dilution/10-K/S-3
    short, news/truth/momentum long).
  - `rule_key` derived via `_rule_key.derive(event_type, subtype,
    horizon_days)` — embedded subtype + horizon is what calibration buckets
    on.
  - Idempotent: `unique(event_id, ticker, direction)` collapses re-runs.
  - Uses the CLAUDE.md rule #2 pattern (pre-filter duplicates + plain INSERT)
    because `on_conflict=` fails on the partial unique index.
- **Output:**
  - Writes `stock_event_paper_trades` (`status='open'`, entry_price,
    `target_price`, `stop_price`, `rule_key`).
  - Downstream: `price_agent` closes them at EOD on entry_at + horizon.

### price_agent.py

- **Name:** `agents/price_agent.py` — EOD outcome reconciliation + calibration update.
- **Purpose:** End-of-day learning loop. For every expired-horizon open
  signal / paper trade, compute realized return + MFE/MAE, write outcome
  rows, update per-rule calibration and per-agent EMA weights, mark
  signals closed.
- **Input:**
  - `stock_signals` with `status_v2 IN (candidate, sent, suppressed)` whose
    horizon has expired.
  - `stock_event_paper_trades` with `status='open'` past horizon.
  - `yfinance` bars (browser-impersonated via `curl_cffi` to bypass
    Yahoo's GitHub-IP blocking).
  - Cron `30 21 * * 1-5` (weekday 21:30 UTC, after US close).
- **Processing:**
  - Fetch entry (next-session-open after `fired_at`) and exit (horizon
    close); compute realized return, direction-aware correctness, MFE/MAE,
    `target_hit` / `stop_hit`.
  - `SLIPPAGE_BPS = 5` per side.
  - Write `stock_forecast_audit` row per signal.
  - Close `stock_paper_forecasts` rows tied to the signal.
  - Update `stock_rule_calibration` (n_observations, n_correct,
    accuracy, profit_factor, avg_win, avg_loss, target_hit_rate,
    stop_hit_rate, mean_mfe_pct, mean_mae_pct, is_mature,
    accuracy_30d, brier_30d, n_closed_30d).
  - Update `stock_agent_weights` EMA per contributing agent
    (`EMA_ALPHA = 0.1`, same as backtester).
  - Mark `stock_signals.status_v2 = 'closed'`.
- **Output:**
  - Writes `stock_forecast_audit`, `stock_event_paper_trades` (closed),
    `stock_rule_calibration`, `stock_agent_weights`, `stock_paper_forecasts`,
    `stock_signals` (PATCH).
  - Telegram EOD digest.

### paper_trade_agent.py

- **Name:** `agents/paper_trade_agent.py` — Probability-calibrated paper forecast writer.
- **Purpose:** Convert live `stock_signals` into probability-calibrated paper
  forecasts. Explicitly NOT a BUY/SELL engine — writes paper-only actions
  (`PAPER_LONG`, `PAPER_WATCH`, `PAPER_AVOID`, `PAPER_CHASE_RISK`, `NO_TRADE`).
- **Input:**
  - Recent `stock_signals` rows.
  - Historical audited win-rates (from `stock_forecast_audit` aggregates).
  - Cron `*/15 * * * *`.
- **Processing:**
  - Empirical shrinkage calibration:
    `prob_win = (setup_wins + K * base_rate) / (setup_n + K)`, with
    `SHRINKAGE_K = 20` shrinking small samples toward the base rate.
  - Thresholds: `MIN_SETUP_N_FOR_LONG = 8`, `MIN_SETUP_N_FOR_WATCH = 4`.
  - `SetupFeatures` records which evidence flags (8-K, Form 4, earnings,
    momentum, news, truth, dilution) were present.
  - Modes: `live` (current signals) and `shadow_backtest` (replay).
- **Output:**
  - Writes `stock_paper_forecasts` (`model_version='paper-calibration-v1'`,
    `calibration_method='empirical_shrinkage_v1'`).

### backtester.py

- **Name:** `agents/backtester.py` — 6-month historical replay.
- **Purpose:** Replay the last 180 days of filings + earnings + momentum
  through the live scoring + cluster code (imported from `thesis_agent` so
  replay can't drift from production) and produce a summary of how the
  pipeline would have performed.
- **Input:**
  - `stock_raw_filings`, `stock_normalized_events`, `stock_raw_prices`.
  - `yfinance` bars (browser-impersonated).
  - Triggered manually: `gh workflow run backtester.yml`.
- **Processing:**
  - Signal sources: SEC filings (8-K, SC 13D, Form 4); earnings (pre-drift,
    release, post-PEAD); 20-day relative strength vs SPY (top decile = bullish
    momentum).
  - Reuses `thesis_agent.score_evidence`, `cluster_passes`, `action_for`,
    `signal_direction`, `source_agent_for`, `evidence_summary` — same code
    paths as live.
  - Entries at next-day open; slippage 5 bps/side; no commissions.
  - Honest caveats baked into the metrics output: survivorship bias,
    yfinance look-ahead, no fundamentals (P/E, FCF, short interest),
    Truth Social out of scope (RSS history limit).
  - Hedge-fund references: Bernard & Thomas (PEAD), Fama-French
    (momentum), Frazzini & Lamont (pre-earnings drift).
- **Output:**
  - Writes `stock_signals` (`model_version='rubric-v1.0-backtest'`,
    `status_v2='backtest'`).
  - Writes `stock_forecast_audit` (realized return per backtest signal).
  - Writes `stock_agent_weights` (per-day EMA evolution).
  - Writes `stock_backtest_runs` (summary metrics).

---

## Layer 6 — Presentation

### site_generator.py

- **Name:** `agents/site_generator.py` — Static dashboard renderer + Hostinger deployer.
- **Purpose:** Pull the pipeline's current state from Supabase, render
  Jinja2 templates into `dist/`, and let the workflow deploy via FTPS to
  `hub4apps.com/stock_app/`.
- **Input:**
  - Reads many tables: `stock_signals`, `stock_normalized_events`,
    `stock_event_paper_trades`, `stock_rule_calibration`,
    `stock_agent_weights`, `stock_job_runs`, `stock_trade_setups`,
    `stock_risk_decisions`, `stock_telegram_dispatch_log`, `stock_watchlists`.
  - `AGENT_INVENTORY` is the single source of truth for expected freshness
    SLA per agent — `expected_minutes` drives the "Agents" tab health view.
  - Cron `*/15 * * * *` (with `site_generator_retry.yml` listening for
    workflow_run failures and re-dispatching after 5 min — see CLAUDE.md
    rule #5).
- **Processing:**
  - Renders dashboard tabs: signals, trades, agents (with stale-row
    detection), rules (calibration grid), per-ticker pages, per-signal
    alert pages.
  - Emits `dist/status.json` as a machine-readable view of pipeline state.
  - Copies Chart.js + annotation plugin into `dist/vendor/` so the
    Hostinger pages can load JS locally (no CDN dependency).
  - **No purple** in any rendered template (project rule).
- **Output:**
  - `dist/` directory (HTML, CSS, JSON, JS vendor bundles).
  - Workflow ships `dist/` to Hostinger via 3-in-line FTPS retries.
  - `dist/status.json` consumed by cron-job.org pingers + external monitors.

### telegram_dispatcher.py

- **Name:** `agents/telegram_dispatcher.py` — Telegram payload formatter + dispatch logger.
- **Purpose:** Render a signal into the locked §17.3 alert format, post to
  the Telegram Bot API, and write a `stock_telegram_dispatch_log` row so
  `audit_agent` can prove every "sent" signal actually delivered.
- **Input:**
  - Called in-process from `thesis_agent`, `intraday_alert_agent`,
    `orchestrator_agent`, and direct-Telegram domain agents (biotech /
    defense / activist_insider / consumer_health / energy_transition).
  - Env: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
- **Processing:**
  - Emoji map covers legacy WATCH/RESEARCH/AVOID_CHASE plus new
    PR1A catalyst tiers (CATALYST_WATCH / CATALYST_RESEARCH / MOMENTUM_ONLY)
    plus maturity-gated BUY / SELL.
  - `MOMENTUM_ONLY` renders explicitly as "no verified catalyst (last 48h)"
    so the operator never reads it as catalyst-backed.
  - Inline keyboard (Researched / Acted / Skipped) is feature-flagged off
    (`TELEGRAM_CALLBACKS_ENABLED = False`) until a webhook handler lands +
    `stock_user_decisions` gains the necessary columns.
  - Body links to per-signal page on `hub4apps.com/stock_app/alert/{id}.html`.
- **Output:**
  - HTTP POST to Telegram Bot API.
  - Writes `stock_telegram_dispatch_log` (delivery_ok, attempt count,
    response).

---

## Operational

### orchestrator_agent.py

- **Name:** `agents/orchestrator_agent.py` — Daily-fire watchdog.
- **Purpose:** Once per day, check `stock_job_runs` for every expected
  agent's most recent successful run; if the gap exceeds the per-agent
  budget, fire ONE Telegram summary listing every stale agent.
- **Input:**
  - `stock_job_runs` (last successful per agent).
  - `EXPECTED` list of `AgentExpectation(name, cadence, max_gap_hours,
    trading_only)`.
  - `_market_calendar.is_trading_day` for trading-day gating.
  - Cron `30 4 * * *`.
  - Env: `GH_TOKEN` (optional, for dispatching).
- **Processing:**
  - For `trading_only=True` agents, extend gap budget by +24h per
    non-trading day since the last NYSE session — so weekend silence
    doesn't trip the watchdog.
  - `max_gap_hours` is padded generously per CLAUDE.md rule #6 (GHA cron
    is best-effort).
  - Routes through `telegram_dispatcher.send_and_log` so the watchdog's
    own Telegram is itself logged (audit_agent invariant #1 covers it).
- **Output:**
  - One Telegram digest per day listing stale agents (or no alert if
    everything fresh).
  - `stock_job_runs` lifecycle row.

### audit_agent.py

- **Name:** `agents/audit_agent.py` — Daily pipeline-invariant audit.
- **Purpose:** Run a fixed list of integrity invariants daily after the
  overnight learning cycle and Telegram-alert on any violation. Observation
  only — writes nothing to data tables.
- **Input:**
  - All Supabase tables involved in the invariants.
  - Cron `0 4 * * *`.
- **Processing — invariants (each independent):**
  1. Every `stock_signals.status_v2='sent'` has a matching
     `stock_telegram_dispatch_log` row with `delivery_ok=true` within
     `DISPATCH_WINDOW_HOURS = 2`.
  2. Every `stock_risk_decisions.decision='size'` ties to a setup whose
     signal's `valid_until` was `> decision_at` at decision time.
  3. `stock_rule_calibration.n_observations == n_correct + n_incorrect`.
  4. No `stock_event_paper_trades.status='open'` row older than
     `horizon_days + STALE_OPEN_GRACE_DAYS = 5` days.
  5. 24h `stock_normalized_events` count did not drop > 50% vs same DOW
     last week (`EVENT_DROP_THRESHOLD = 0.50`).
- **Output:**
  - No data writes (audit is read-only).
  - `stock_job_runs` lifecycle row.
  - Telegram alert on any invariant failure.

### archive_agent.py

- **Name:** `agents/archive_agent.py` — Weekly tiered-storage exporter.
- **Purpose:** Move rows older than each table's retention threshold to
  gzip-compressed JSONL files on Hostinger, then mark + delete from
  Supabase in live mode (DRY_RUN=true bypasses delete for verification).
- **Input:**
  - Tables: `stock_normalized_events` (90d), `stock_event_paper_trades`
    (90d, only `exit_at NOT NULL`), `stock_signals` (90d, only closed),
    and others per the `TABLES` config.
  - Cron `0 3 * * 0` (Sundays).
  - Env: `HOSTINGER_FTP_USER`, `HOSTINGER_FTP_PASS`, `DRY_RUN`.
- **Processing:**
  - Per-table fetch → serialize JSONL → gzip → FTPS upload to
    `archive/` on Hostinger.
  - In live mode: stamp `archived_at`, then delete from Supabase.
  - In DRY_RUN: skip the stamp and delete (so we can verify archive
    integrity before enabling deletions).
  - After all tables: update `archive/index.json` with cumulative
    rule_calibration counters aggregated from
    `stock_event_paper_trades`.
- **Output:**
  - Hostinger files at `archive/{table}_{date}.jsonl.gz`.
  - `archive/index.json` (consumed by `bin/stock_app_sync.sh` and `price_agent`).
  - Telegram digest.

### source_review_agent.py

- **Name:** `agents/source_review_agent.py` — Monthly architectural health.
- **Purpose:** Ping every registered alternative data source, compute
  per-agent success rates from the last 30 days of `stock_job_runs`, and
  Telegram-recommend (never auto-promote) promoting a fallback when its
  rolling success beats the primary by ≥10 percentage points.
- **Input:**
  - `stock_data_sources` registry.
  - `stock_job_runs` (last 30 days).
  - Cron `0 13 1 * *` (1st of month, 13:00 UTC).
- **Processing:**
  - Per-source `ping_source` strategy varies: yfinance via Yahoo chart
    API + `curl_cffi` browser impersonation; Stooq direct; EDGAR with
    proper UA; trumpstruth_rss feed reachability.
  - Updates each source's `health_*` fields.
  - `PROMOTION_DELTA_PP = 10` — fallback must beat primary by ≥10pp before
    a recommendation is generated.
- **Output:**
  - Writes `stock_data_sources` health fields.
  - Telegram digest with degradation alerts + promotion recommendations.

### historical_ingest.py

- **Name:** `agents/historical_ingest.py` — One-time 6-month bootstrap.
- **Purpose:** Backfill three data sources so the backtester + per-ticker
  pages have 180 days of foundation. Run manually after seeding the
  watchlist.
- **Input:**
  - `--filings` walks EDGAR per CIK for 180 days.
  - `--earnings` calls `yfinance.Ticker(t).get_earnings_dates()` per stock.
  - `--prices` does batched `yfinance.download` of daily bars.
  - `--all` runs all three.
  - Triggered via `historical_ingest.yml` (workflow_dispatch only).
- **Processing:**
  - Reuses `filing_agent.fetch_watchlist`, `fetch_recent_filings`,
    `upsert_filings`, `emit_normalized_events` so the bootstrap and live
    paths share dedupe logic.
  - All three subcommands idempotent.
- **Output:**
  - Writes `stock_raw_filings`, `stock_normalized_events`,
    `stock_raw_prices` (per subcommand).

### ops_recorder.py

- **Name:** `agents/ops_recorder.py` — Workflow-wrapper ops logger (stdlib only).
- **Purpose:** Record GitHub-Actions wrapper-level run rows in
  `stock_job_runs` BEFORE dependencies install, so workflow-level failures
  (pip install timeout, runner cancellation, deploy failure) are still
  visible in the dashboard even when the agent never started.
- **Input:**
  - CLI: `--phase start|finish --agent <name> [--status ...] [--error ...]`.
  - GHA env: `GITHUB_RUN_ID`, `GITHUB_RUN_ATTEMPT`, `GITHUB_WORKFLOW`,
    `GITHUB_JOB`, `GITHUB_REF`, `GITHUB_SHA`.
- **Processing:**
  - Writes `run_type='wrapper'` rows (distinct from agent-self-written
    `run_type='agent'` rows).
  - Stores the inserted ID in a temp file `.ops_run_id_<agent>` so the
    finish step can PATCH the right row.
  - Optional `--parent-run-id` and `--stage` for lineage.
- **Output:**
  - Writes `stock_job_runs` rows.
  - Every YAML in `.github/workflows/` invokes this in pre/post steps.

### _catalyst_policy.py

- **Name:** `agents/_catalyst_policy.py` — Per-event-type evidence role + age policy.
- **Purpose:** Single source of truth for which event types count as
  catalyst vs context vs background, plus the max age beyond which a
  catalyst-role event demotes to context. Imported by `thesis_agent`,
  `intraday_alert_agent`, and any code path that needs to honestly label
  causation.
- **Processing exports:**
  - `CATALYST_POLICY` dict — `{event_type: {role, max_age_hours}}`.
  - `policy_for(et)`, `is_catalyst_eligible(event, now)`,
    `split_events_by_role(events)`.
  - `CAUSAL_KEYWORDS` set (rating / corporate / regulatory / contract /
    operational / financing / activist) and
    `is_causal_headline(headline)` — the PR1B classifier used to gate
    `stock_raw_news` evidence promotion.

### _market_calendar.py

- **Name:** `agents/_market_calendar.py` — Hardcoded NYSE holiday calendar.
- **Purpose:** Offline `is_trading_day` / `previous_trading_day` /
  `next_trading_day` helpers, no pandas_market_calendars dependency on
  GHA runners.
- **Processing:**
  - Per-year frozensets `NYSE_HOLIDAYS_2026`, `NYSE_HOLIDAYS_2027`.
  - Each January a new year's set must be appended.
- **Used by:** `orchestrator_agent`, `archive_agent`.

### _rule_key.py

- **Name:** `agents/_rule_key.py` — Canonical rule_key derivation.
- **Purpose:** Single source of truth used by `event_paper_agent` (writes
  calibration rows), `trade_setup_agent` (looks up calibration for
  sizing), and `thesis_agent` (checks rule maturity for vocabulary gating).
- **Format:** `"{event_type}:{subtype}:h{horizon_days}d"`.
- **Why centralized:** prior to this, `event_paper_agent` wrote
  `earnings_release:beat:h7d` while `trade_setup_agent` looked up
  `earnings_release::h7d`, silently dropping adaptive sizing on every
  signal with a non-empty subtype.

---

## Scripts

### learning_snapshot.py

- **Name:** `scripts/learning_snapshot.py` — Weekly learning-state snapshot + diff.
- **Purpose:** Capture the three learning tables (`stock_rule_calibration`,
  `stock_agent_weights` latest date, closed-trade rollup from
  `stock_event_paper_trades`) into `snapshots/YYYY-MM-DD.json`, then diff
  two snapshots to surface "what the bot learned this week."
- **Input:**
  - CLI: `capture` or `diff <date1> <date2>`.
  - Env: `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`.
- **Processing:**
  - `capture()` dumps full calibration plus per-rule closed-trade rollup
    `(n, wins, sum_return, target_hits, stop_hits)`.
  - `diff()` prints a summary header (rules tracked, mature rules, closed
    trades delta), then calls `extract_meaningful_changes()` for the human
    digest.
  - `extract_meaningful_changes()` is currently a TODO stub — the user
    will implement which deltas matter (newly-mature rules, profit_factor
    swings, top weight gainers/losers).
- **Output:**
  - JSON files under `snapshots/`.
  - Stdout digest on `diff`.

### backfill_paper_trades.py

- **Name:** `scripts/backfill_paper_trades.py` — Historical paper-trade backfill.
- **Purpose:** Replay the last N days of `stock_normalized_events` through
  the same direction + rule_key + target/stop logic as live
  `event_paper_agent`, but write each event×horizon as a CLOSED row with
  all outcome fields populated from yfinance bars. Seeds calibration so
  the BUY/SELL maturity gate can actually engage.
- **Input:**
  - `--days N --commit` flags (`--commit` required to write).
  - Reuses `price_agent.fetch_bars` + `price_agent.compute_paper_outcome`
    so the backfill cannot drift from live outcome math.
  - Reuses `_rule_key.derive`.
- **Processing:**
  - Constants must match `event_paper_agent` exactly:
    `HORIZONS = (1, 7, 15, 30)`, `TARGET_PCT = 0.05`, `STOP_PCT = 0.03`.
  - Idempotent: skips events with an existing `(ticker, direction)` trade.
  - Tags every row with `notes=BACKFILL_TAG` so they can be filtered out
    later if needed.
  - After inserts, recomputes `stock_rule_calibration` from scratch for
    every touched `rule_key`.
- **Output:**
  - Writes `stock_event_paper_trades` (`status='closed'`).
  - Updates `stock_rule_calibration`.
  - Triggered by `backfill_paper_trades.yml` (workflow_dispatch input
    `backfill_days`).

### bootstrap_cronjob_org.py

- **Name:** `scripts/bootstrap_cronjob_org.py` — External pinger provisioner.
- **Purpose:** Idempotently provision cron-job.org backup pingers that call
  GitHub's `workflow_dispatch` API on a staggered cadence so a dropped GHA
  cron firing is covered within ~7 minutes (per CLAUDE.md rule #6).
- **Input:**
  - Env: `CRONJOB_API_KEY`, `GH_DISPATCH_PAT` (never logged).
  - `WORKFLOWS` dict — Pareto pick of the seven tightest-cadence
    workflows (`site_generator`, `paper_trade_agent`, `intraday_alert_agent`,
    `filing_agent`, `news_agent`, `thesis_agent`, `truth_social_agent`).
- **Processing:**
  - Schedules staggered 7 min off GHA cron so dropped slots get fast
    coverage without doubling work when GHA is healthy
    (`concurrency: cancel-in-progress` cancels duplicates within 1 sec).
  - Existing jobs matching the title convention `stock_app:<workflow>`
    are PATCHed; missing ones are PUT.
- **Output:**
  - Provisions cron-job.org jobs (HTTP API).

### bin/stock_app_sync.sh

- **Name:** `bin/stock_app_sync.sh` — Hostinger archive mirror.
- **Purpose:** Bash helper to mirror Hostinger archive JSONL.gz files to a
  local Mac for offline DuckDB / pandas analysis. Reads
  `archive/index.json` and fetches every referenced gzipped JSONL that
  isn't already on disk.
- **Input:**
  - `https://hub4apps.com/stock_app/archive/index.json`.
  - CLI: `[--dest <path>]` (default `$HOME/stock_app_archive`).
  - Requires `curl`, `jq`.
- **Processing:**
  - Fetches index, then loops every referenced file and downloads only the
    missing ones (idempotent).
- **Output:**
  - Files under `$DEST/`.
  - Intended to run as a weekly Mac cron Monday 04:00 local, after the
    Sunday `archive_agent` upload.

---

## Workflow → Agent mapping

Every workflow runs `agents/ops_recorder.py` in pre/post steps to record
wrapper-level lifecycle in `stock_job_runs`. The table below shows the
primary Python entry point + cron schedule.

| Workflow YAML | Python entry point | Cron (UTC) | Notes |
|---|---|---|---|
| `filing_agent.yml` | `agents/filing_agent.py` | `*/5 * * * *` | EDGAR poll, 24/7. |
| `news_agent.yml` | `agents/news_agent.py` | `*/5 * * * *` | RSS news, 24/7. |
| `truth_social_agent.yml` | `agents/truth_social_agent.py` | `*/5 * * * *` | Truth RSS, 24/7. |
| `thesis_agent.yml` | `agents/thesis_agent.py` | `*/5 * * * *` | Score + cluster + dispatch. |
| `intraday_alert_agent.yml` | `agents/intraday_alert_agent.py` | `*/15 13-21 * * 1-5` | US market hours only. |
| `paper_trade_agent.yml` | `agents/paper_trade_agent.py` | `*/15 * * * *` | Probability calibration. |
| `site_generator.yml` | `agents/site_generator.py` | `*/15 * * * *` | Render + FTPS deploy. |
| `site_generator_retry.yml` | — (re-dispatches site_generator) | `workflow_run` | Single-shot retry after 5-min sleep on schedule/workflow_run failures. |
| `trade_setup_agent.yml` | `agents/trade_setup_agent.py` | `*/30 * * * *` | Setup constructor. |
| `risk_agent.yml` | `agents/risk_agent.py` | `*/30 * * * *` | Van Tharp sizing. |
| `event_paper_agent.yml` | `agents/event_paper_agent.py` | `5 * * * *` | Hourly, 5 min in. |
| `activist_insider_agent.yml` | `agents/activist_insider_agent.py` | `15 */2 * * *` | Every 2h, 24/7. |
| `crypto_macro_agent.yml` | `agents/crypto_macro_agent.py` | `35 21 * * 1-5` | Weekday post-close. |
| `price_agent.yml` | `agents/price_agent.py` | `30 21 * * 1-5` | Weekday EOD. |
| `market_scanner_agent.yml` | `agents/market_scanner_agent.py` | `30 21 * * 1-5` | Weekday EOD. |
| `defense_agent.yml` | `agents/defense_agent.py` | `30 22 * * 1-5` | Weekday late. |
| `energy_transition_agent.yml` | `agents/energy_transition_agent.py` | `45 13 * * 1-5` | Weekday morning. |
| `biotech_agent.yml` | `agents/biotech_agent.py` | `0 14 * * 1-5` | Weekday morning. |
| `consumer_health_agent.yml` | `agents/consumer_health_agent.py` | `0 15 * * 1-5` | Weekday morning. |
| `macro_rates_agent.yml` | `agents/macro_rates_agent.py` | `0 13 * * 1-5` + `30 18 * * 3` | Daily + FOMC window. |
| `audit_agent.yml` | `agents/audit_agent.py` | `0 4 * * *` | Daily 04:00 UTC. |
| `orchestrator_agent.yml` | `agents/orchestrator_agent.py` | `30 4 * * *` | After audit. |
| `earnings_agent.yml` | `agents/earnings_agent.py` | `0 12 * * 0` | Sundays. |
| `flows_agent.yml` | `agents/flows_agent.py` | `0 14 * * 0` | Sundays. |
| `archive_agent.yml` | `agents/archive_agent.py` | `0 3 * * 0` | Sundays. |
| `source_review_agent.yml` | `agents/source_review_agent.py` | `0 13 1 * *` | Monthly. |
| `backtester.yml` | `agents/backtester.py` | manual | `workflow_dispatch` only. |
| `historical_ingest.yml` | `agents/historical_ingest.py` | manual | One-time bootstrap. |
| `backfill_paper_trades.yml` | `scripts/backfill_paper_trades.py` | manual | Takes `backfill_days` input. |
| `tests.yml` | pytest suite | on push | CI; no agent. |
