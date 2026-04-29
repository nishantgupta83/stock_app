# Hub4Apps Market Intelligence

Free-tier, notification-first market intelligence platform.
Telegram alerts on filings, news, and Trump posts that move the tape.

**Design doc:** [`docs/market-intelligence-platform-design.md`](docs/market-intelligence-platform-design.md)
**Phase 0 setup:** [`docs/PHASE0_CHECKLIST.md`](docs/PHASE0_CHECKLIST.md)

## Stack

| Layer | Service | Cost |
|---|---|---|
| Compute | GitHub Actions (public repo, unlimited minutes) | $0 |
| DB + auth | Supabase Free | $0 |
| Push notifications | Telegram Bot API | $0 |
| Static frontend | Hostinger shared hosting (manual upload) | already paid |
| Domain | hub4apps.com | already paid |

## Layout

```
.
├── docs/                       Design doc + Phase 0 checklist
├── sql/                        Supabase migrations (run in order)
├── agents/                     One Python file per agent
├── .github/workflows/          One YAML per agent (cron-scheduled)
├── templates/                  Jinja2 for static-site generator (Phase 2)
└── dist/                       Generated HTML to upload to Hostinger (Phase 2)
```

## Current status

### Phase 0 — EDGAR ingestion ✅ live
- Filing agent polls EDGAR every 5 min, writes to `stock_raw_filings` + `stock_normalized_events`
- 12,800+ filings ingested

### Phase 1 — Intelligence pipeline ✅ live
- `truth_social_agent` — Trump posts via RSS → keyword classifier → ticker events
- `thesis_agent` — joins filing + Truth Social evidence, fires WATCH/RESEARCH signals at score ≥ 70
- `telegram_dispatcher` — push alerts to @Hub4apps_market_intel_bot

### Phase 2 — Dashboard + backtest ✅ deployed
- Static HTML dashboard (5 tabs) generated every 15 min → `dist` branch → FTP to `market.hub4apps.com`
- 6-month backtester (filings + earnings + momentum) with yfinance → stooq fallback
- Monthly source review agent pings all data sources, sends health report via Telegram

### SQL migrations (run in order in Supabase SQL Editor)
- `sql/0001_initial_schema.sql`
- `sql/0002_seed_universe.sql`
- `sql/0003_add_kind_and_funds.sql`
- `sql/0004_ops_tables.sql`
- `sql/0005_extend_status_and_data_sources.sql` — status_v2='backtest' + data sources registry

### Agents
| Agent | Schedule | Purpose |
|---|---|---|
| `filing_agent` | `*/5 * * * *` | EDGAR filings → normalized events |
| `truth_social_agent` | `*/5 * * * *` | Trump RSS → ticker events |
| `thesis_agent` | `*/5 * * * *` | Scoring + cluster rule + Telegram dispatch |
| `site_generator` | `*/15 * * * *` | HTML dashboard → dist branch |
| `backtester` | manual | 6-month historical replay |
| `source_review_agent` | `0 13 1 * *` | Monthly source health check |

### Security note
`agents/` use secrets only via GitHub Actions `secrets.*` — never committed.
Local Claude Code tool permissions are stored in `.claude/settings.local.json` (git-ignored).
Do not commit that file; it may contain local API keys used for interactive verification.
