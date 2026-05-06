# Phase 9 — Tiered Storage

Status: **planned, not built yet**
Date: 2026-05-06
Owner: Nishant

## Why

Supabase Free is 500 MB. The current 30-ticker watchlist with 1-day-only paper
trades is comfortably under it. After:

- multi-horizon paper trades (×4 per event, sql/0018) — already shipped
- adding INTC + EBAY (sql/0018) — already shipped
- the planned S&P 100 expansion (~70 more tickers) — roadmap

…we project ~700 MB of active Supabase storage within 3 months. Either we
pay Supabase Pro ($25/mo, breaks the $0 promise) or we tier storage so
**hot data lives in Supabase and cold history lives elsewhere**.

The user already pays for two things we can use:

- **Hostinger shared hosting** (25 GB, FTPS-writable from CI) — currently
  serves `dist/` static HTML
- **Local Mac** (TBs of free SSD) — currently runs nothing of this project

Goal: stay $0/mo on cloud spend indefinitely while supporting years of
training data and the eventual S&P 500.

## Architecture (three tiers)

```
┌────────────────────────────────────────────────────────────────────┐
│ ACTIVE TIER — Supabase Free (always-hot, every-N-min reads)        │
│                                                                    │
│ stock_normalized_events       last 90 days                         │
│ stock_event_paper_trades      open + closed last 90 days           │
│ stock_signals                 status_v2 in (candidate, sent)       │
│ stock_rule_calibration        full table (~500 rows max)           │
│ stock_agent_weights           full table (~daily rows × 6 agents)  │
│ stock_raw_prices              last 180 days (chart pages need it)  │
│ stock_institutional_…         current quarter only                 │
│ stock_keyword_rules           full table (~50 rows)                │
│                                                                    │
│ Sized to stay <300 MB even at S&P 500 scale.                       │
└────────────────────────────────────────────────────────────────────┘
                              │
                              │  archive_agent.py (weekly cron)
                              │   1. Export rows older than retention threshold
                              │   2. Gzip JSONL → FTPS upload to Hostinger
                              │   3. DELETE archived rows from active tier
                              ▼
┌────────────────────────────────────────────────────────────────────┐
│ PASSIVE TIER — Hostinger (CI-writable, web-readable)               │
│                                                                    │
│ /archive/2026/W18/closed_paper_trades.jsonl.gz                     │
│ /archive/2026/W18/normalized_events.jsonl.gz                       │
│ /archive/2026/W18/raw_prices.jsonl.gz                              │
│ /archive/2026/W18/holdings_snapshots.jsonl.gz                      │
│ /archive/index.json                                                 │
│                                                                    │
│ Immutable once published. Calibration cron HTTP-fetches archive    │
│ files when computing multi-year per-rule accuracy.                 │
│                                                                    │
│ Estimated growth at S&P 100 scale: ~6 MB/week = 300 MB/year.       │
│ 25 GB Hostinger holds ~80 years of history before pinching.        │
└────────────────────────────────────────────────────────────────────┘
                              │
                              │  ~/bin/stock_app_sync.sh (weekly, optional)
                              │   curl https://hub4apps.com/stock_app/archive/…
                              ▼
┌────────────────────────────────────────────────────────────────────┐
│ MAC LOCAL — offline analysis sandbox (optional)                    │
│                                                                    │
│ ~/stock_app_archive/2026/W18/*.jsonl.gz                             │
│                                                                    │
│ DuckDB or pandas for ad-hoc deep-dive queries on years of data.    │
│ System works without this — it's a convenience for the user.      │
└────────────────────────────────────────────────────────────────────┘
```

## Retention thresholds

| Table | Active retention | Why |
|---|---|---|
| `stock_normalized_events` | 90 days | thesis_agent looks back at most 180 min; site_generator displays last 200 events. 90d is generous. |
| `stock_event_paper_trades` | open + closed last 90 days | Calibration aggregates from in-memory union of active + archive when reading. 90d active = recent reconciliation latency. |
| `stock_signals` | open (candidate, sent) only | Closed signals (status=closed, expired, demoted, suppressed) → archive. backtest signals stay if recent. |
| `stock_raw_prices` | last 180 days | Ticker chart pages render 180 days. Beyond that = archive. |
| `stock_raw_filings` | last 180 days | filing_agent dedupes against accession_number; older filings only matter for backtest replay. |
| `stock_institutional_holdings_snapshot` | current quarter | Quarterly diff only needs current + previous; previous archived after each new filing. |

Tables NOT subject to retention (always full):
- `stock_rule_calibration` — small, hot, every read by thesis_agent
- `stock_agent_weights` — small, hot
- `stock_keyword_rules` — admin-edited, tiny

## Calibration must read both tiers

The maturity gate (≥90% accuracy, n≥30) needs to count **all closed paper
trades**, not just the active 90-day window. So the calibration extension
in `price_agent` will:

1. Compute per-rule deltas from active-tier trades closed today
2. Add the cumulative archive counts pre-computed and cached at archive
   time in `archive/index.json` (rule_key → n_observations, n_correct,
   sum_of_returns)
3. Update `stock_rule_calibration` with the merged totals

This means the active-tier `stock_rule_calibration` row IS the global running
total — but the math behind it draws from both tiers. Mac users running deep
analysis can verify by joining archive JSONL files directly.

## Components to build

| File | What | Effort |
|---|---|---|
| `agents/archive_agent.py` | Weekly cron: select rows past retention → JSONL.gz → FTPS upload to `/archive/{year}/W{week}/{table}.jsonl.gz` → update `archive/index.json` (with rule_key cumulative counts) → DELETE archived rows from active tier inside a single transaction so a half-failure doesn't drop data. | 3h |
| `.github/workflows/archive_agent.yml` | Weekly cron Sun 03:00 UTC, after Saturday's filing flow but before Sunday's flows_agent. workflow_dispatch input `dry_run=true` for testing. | 15min |
| `sql/0019_retention_columns.sql` | Add `archived_at timestamptz` to the 6 affected tables so we can DELETE WHERE archived_at IS NOT NULL after upload confirms. Index on `(archived_at IS NULL, created_at DESC)` for the partition scan during archive. | 30min |
| `agents/price_agent.py` extension | When updating `stock_rule_calibration`, fetch `archive/index.json` once at the top of the run, merge cumulative counts into the active `n_observations` / `n_correct` totals before applying today's delta. Falls back gracefully if archive is unreachable (system keeps running on active-only). | 2h |
| `agents/site_generator.py` extension | Calibration tab gets a sub-line on each rule row: "active 60 / archived 240 / total 300". Makes the tiering visible. | 30min |
| `bin/stock_app_sync.sh` (Mac) | One-line cron: `curl -sN https://hub4apps.com/stock_app/archive/index.json | jq … | xargs -I@ curl -O https://...@`. User installs once via `crontab -e`. | 30min |
| `docs/phase9-tiered-storage.md` | This file. | done |

**Total build estimate: ~7 hours over 2-3 sessions.**

## Build order (incremental, each step ships standalone)

1. Schema (sql/0019) — additive only, doesn't affect any current behavior
2. archive_agent in DRY-RUN mode — uploads to Hostinger but doesn't DELETE
3. After 1 week of dry-run: verify archive files are correct, flip to real mode
4. price_agent extension to read archive index — backwards-compatible (degrades to active-only if archive missing)
5. site_generator UI sub-line — display only
6. Mac sync — optional, last

Each step is ship-and-verify. No big-bang migration; we never have a moment
where data is in flight between tiers and unreadable.

## Failure modes and graceful degradation

| Failure | Effect | Recovery |
|---|---|---|
| FTPS upload fails | archive_agent retries (existing 3-attempt deploy pattern from `site_generator`); on persistent failure, no rows are DELETEd from active. Active tier grows for one extra week. Telegram digest flags it. | Retry next Sunday |
| Archive index.json corrupted | Calibration cron logs warning, falls back to active-only counts. Maturity gate may pause one cycle. | Re-publish from a previous good snapshot |
| Hostinger 25 GB exhausted (years away at this scale) | archive_agent fails upload, no DELETE, active tier grows | Archive to a second target (S3 free tier? next Hostinger account?) |
| Mac local sync stale | Pure UX issue — the user's offline analysis is missing recent weeks. CI calibration unaffected. | Just re-run sync |

Every layer is allowed to fail without breaking the next one. That's the
point of tiering.

## Cost trajectory

| Scenario | Today | After Phase 9 + S&P 100 |
|---|---|---|
| Supabase Free | $0/mo (300 MB) | $0/mo (~250 MB hot) |
| Hostinger | already paid | already paid (~300 MB/yr archive) |
| Mac storage | $0 | $0 |
| GitHub Actions | $0 (public repo) | $0 |
| **Cloud total** | **$0/mo** | **$0/mo** |

Comparing: without Phase 9, the same expansion forces Supabase Pro ($25/mo)
or aggressive truncation (lose training history → calibration regresses).

## Open questions (for the user before code starts)

1. **Path on Hostinger**: confirm `/archive/` under `/public_html/stock_app/`
   (publicly readable but obscure). Safe? Or should it sit alongside but
   outside `/stock_app/` so it isn't linked from the dashboard?
2. **Mac sync — yes or skip for v1**? Optional component; defer if you'd
   rather not run a cron on the Mac.
3. **Telegram digest from archive_agent**: weekly summary of what was
   archived (e.g., "archived 2,400 closed paper trades, 18,000 prices,
   1,200 events. Active tier now 230 MB / 500 MB")? Adds 30 min to build.

## Deferred / NOT in Phase 9

- LLM hybrid classifier (still in roadmap, separate decision)
- Form 4 buy/sell split (separate)
- Multi-horizon expansion beyond 30d (60d PEAD window) — pending data
- Auto-tune thesis_agent weights from observation aggregates — needs Phase 9
  archive index to span enough history first

---

Once approved, I'll ship in the build order above, one piece per session.
First piece (schema + dry-run archive_agent) lands without disturbing anything.
