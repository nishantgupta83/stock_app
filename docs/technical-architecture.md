# Technical Architecture

Current as of 2026-05-02. This system is a paper-only market intelligence
pipeline. It does not place trades and does not use BUY/SELL language.

## Runtime Topology

```mermaid
flowchart TB
  subgraph Sources["External Sources"]
    EDGAR["SEC EDGAR submissions"]
    News["CNBC / MarketWatch / AP RSS"]
    Truth["Truth Social RSS"]
    Yahoo["yfinance prices + earnings"]
  end

  subgraph Actions["GitHub Actions"]
    Filing["filing_agent<br/>every 5 min"]
    NewsAgent["news_agent<br/>every 5 min"]
    TruthAgent["truth_social_agent<br/>every 5 min"]
    Earnings["earnings_agent<br/>weekly"]
    Thesis["thesis_agent<br/>every 5 min<br/>180 min freshness window"]
    Paper["paper_trade_agent<br/>every 15 min live<br/>manual shadow_30d replay"]
    Price["price_agent<br/>weekday EOD"]
    Backtester["backtester<br/>manual / weekly"]
    Site["site_generator<br/>every 15 min"]
    SourceReview["source_review_agent<br/>monthly"]
  end

  subgraph Supabase["Supabase Free"]
    RawFilings["stock_raw_filings"]
    RawPrices["stock_raw_prices"]
    Events["stock_normalized_events"]
    Signals["stock_signals"]
    Evidence["stock_signal_evidence"]
    Audit["stock_forecast_audit"]
    Weights["stock_agent_weights"]
    PaperForecasts["stock_paper_forecasts"]
    BacktestRuns["stock_backtest_runs"]
    Ops["stock_job_runs<br/>stock_dead_letter_events<br/>stock_agent_freshness"]
  end

  subgraph Outputs["Outputs"]
    Telegram["Telegram alerts"]
    Hostinger["hub4apps.com/stock_app<br/>static dashboard"]
  end

  EDGAR --> Filing --> RawFilings --> Events
  News --> NewsAgent --> Events
  Truth --> TruthAgent --> Events
  Yahoo --> Earnings --> Events
  Yahoo --> Price
  Yahoo --> Backtester
  Yahoo --> RawPrices

  Events --> Thesis --> Signals
  Thesis --> Evidence
  Thesis --> Telegram
  Weights --> Thesis

  Signals --> Paper --> PaperForecasts
  Audit --> Paper
  RawPrices --> Paper

  Signals --> Price --> Audit
  Price --> Weights
  Price --> PaperForecasts

  RawFilings --> Backtester --> Signals
  Backtester --> Audit
  Backtester --> Weights
  Backtester --> BacktestRuns

  SourceReview --> Ops
  Filing --> Ops
  NewsAgent --> Ops
  TruthAgent --> Ops
  Earnings --> Ops
  Thesis --> Ops
  Paper --> Ops
  Price --> Ops
  Backtester --> Ops
  Site --> Ops

  Events --> Site
  Signals --> Site
  Audit --> Site
  Weights --> Site
  PaperForecasts --> Site
  BacktestRuns --> Site
  RawPrices --> Site
  Site --> Hostinger
```

## Live Signal Path

```mermaid
sequenceDiagram
  autonumber
  participant Source as External source
  participant Agent as Ingestion agent
  participant DB as Supabase
  participant Thesis as thesis_agent
  participant Paper as paper_trade_agent
  participant Price as price_agent
  participant Site as site_generator
  participant User as User

  Source->>Agent: New filing/news/post/earnings data
  Agent->>DB: Upsert raw rows and normalized events
  Thesis->>DB: Read recent normalized events
  Thesis->>DB: Read latest agent EMA weights
  Thesis->>DB: Write candidate/sent/suppressed signal
  Thesis-->>User: Telegram alert for WATCH / AVOID_CHASE when dispatch rules pass
  Paper->>DB: Read live eligible signals
  Paper->>DB: Read audited historical outcomes
  Paper->>DB: Write live paper forecast with probability, EV, target, stop
  Price->>DB: Close matured signals and paper forecasts from EOD prices
  Price->>DB: Update per-agent EMA weights
  Site->>DB: Render dashboard, charts, learning, and paper forecast pages
  Site-->>User: Deploy static HTML to Hostinger by FTPS
```

## Historical Learning Path

```mermaid
flowchart LR
  Ingest["historical_ingest.yml<br/>filings + earnings + prices"] --> Raw["stock_raw_filings<br/>stock_normalized_events<br/>stock_raw_prices"]
  Raw --> Backtest["backtester.yml<br/>180 day replay"]
  Backtest --> HistSignals["stock_signals<br/>status_v2 = backtest"]
  Backtest --> Audit["stock_forecast_audit"]
  Backtest --> Weights["stock_agent_weights"]
  Backtest --> Runs["stock_backtest_runs"]
  HistSignals --> Shadow["paper_trade_agent.yml<br/>mode = shadow_30d"]
  Audit --> Shadow
  Weights --> Shadow
  Shadow --> PaperRows["stock_paper_forecasts<br/>forecast_mode = shadow_backtest<br/>status = closed"]
  PaperRows --> Site["Paper Trades page<br/>shadow rows separated from live rows"]
```

The `shadow_backtest` mode is intentionally not counted as live paper-trading
performance. It exists to validate the UI, calibration logic, and replay process
using already-audited historical signals. Scheduled `paper_trade_agent` runs stay
strictly live-only.

## Core Tables

| Table | Purpose |
|---|---|
| `stock_watchlists` | Active ticker universe. |
| `stock_symbols` | Symbol metadata, CIK, asset kind. |
| `stock_raw_filings` | EDGAR raw filing metadata. |
| `stock_raw_prices` | Daily OHLCV bars used by charts, chase-risk checks, and paper entries. |
| `stock_normalized_events` | Cross-source event stream consumed by thesis and site. |
| `stock_signals` | Thesis outputs: live signals plus backtest replay signals. |
| `stock_signal_evidence` | Links signals back to supporting normalized events. |
| `stock_forecast_audit` | Realized outcomes for matured live/backtest signals. |
| `stock_agent_weights` | Per-agent EMA accuracy and scoring weight. |
| `stock_paper_forecasts` | Probability-calibrated paper forecasts, split by `forecast_mode`. |
| `stock_backtest_runs` | Backtest metrics and calibration summaries. |
| `stock_job_runs` | Operational run history per agent. |
| `stock_dead_letter_events` | Failed parses/fetches with redacted diagnostics. |

## Forecast Modes

| Mode | Source rows | Status behavior | Counts as live paper trading? |
|---|---|---|---|
| `live` | `stock_signals.status_v2 in (candidate,sent,suppressed)` | Opens when generated, closes through `price_agent` | Yes |
| `shadow_backtest` | Audited backtest signals from recent history | Written already closed from historical audit | No |

## Operational Runbook

Cold start order:

1. Apply `sql/*.sql` in order through `sql/0009_paper_forecast_modes.sql`.
2. Run `historical_ingest.yml` with `sections=all`.
3. Run `backtester.yml`.
4. Run `paper_trade_agent.yml` with `mode=shadow_30d`.
5. Run `paper_trade_agent.yml` with `mode=live`.
6. Run `site_generator.yml`.

Normal operation:

- Ingestion, thesis, paper forecast, and site generation run on cron.
- `price_agent` closes mature signals and forecasts at weekday EOD.
- `backtester` remains manual/weekly so historical replay is deliberate.
- `source_review_agent` runs monthly to catch feed drift.

## Current Verification Snapshot

Last verified on 2026-05-02:

- Shadow replay wrote 27 closed `shadow_backtest` paper forecasts.
- Live paper forecast pass wrote 0 rows because no live eligible signals existed.
- Manual thesis run saw 4 fresh events in the 180-minute window and produced 0 candidates.
- Site generation succeeded after increasing Hostinger FTPS timeout to 120 seconds.
- Live Paper Trades page rendered `Shadow 30d = 27` and `Shadow Hit Rate = 48%`.
