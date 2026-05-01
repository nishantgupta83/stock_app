# Hub4Apps Market Intelligence

Real-time stock event triage. Monitors SEC filings, market news, and Trump posts. Clusters
multi-source evidence, fires Telegram alerts, reconciles each signal against actual price reality,
and learns from outcomes via per-agent EMA weights.

**Live dashboard:** https://hub4apps.com/stock_app/
**Design doc:** [`docs/market-intelligence-platform-design.md`](docs/market-intelligence-platform-design.md)
**Phase 0 setup:** [`docs/PHASE0_CHECKLIST.md`](docs/PHASE0_CHECKLIST.md)

> **Paper-trading vocabulary only.** The bot says **WATCH / RESEARCH / AVOID_CHASE / CHASE_RISK** — never
> "BUY" or "SELL" — until 60 days of paper trading hits the §17.6 graduation thresholds.
> Educational use; not financial advice.

## Stack

| Layer | Service | Cost |
|---|---|---|
| Compute | GitHub Actions (public repo, unlimited minutes) | $0 |
| DB + auth | Supabase Free | $0 |
| Push notifications | Telegram Bot API | $0 |
| Static frontend | Hostinger shared hosting (FTPS auto-deploy) | already paid |
| Domain | hub4apps.com | already paid |

## Pipeline (9 GitHub Actions jobs)

| Agent | Schedule | What it does |
|---|---|---|
| `filing_agent`        | `*/5 * * * *`     | EDGAR → 8-K (with item parsing), 10-K/Q, Form 4, 13D/G, S-3 → `stock_raw_filings` + `stock_normalized_events` |
| `news_agent`          | `*/5 * * * *`     | CNBC / MarketWatch / AP RSS → ticker mention + sentiment classifier → `stock_normalized_events` |
| `truth_social_agent`  | `*/5 * * * *`     | Trump Truth Social RSS → keyword router → `stock_normalized_events` |
| `thesis_agent`        | `*/5 * * * *`     | Cluster rule (≥2 distinct agents within 5-minute bucket, with narrow high-severity exceptions) → 100-pt weighted score → action → Telegram dispatch. Reads live `stock_agent_weights` to amplify reliable agents and dampen chronically-wrong ones. Includes chase-risk downgrade if price already moved >5% since cluster start. |
| `earnings_agent`      | weekly Sun 12:00 UTC | yfinance earnings dates per stock → upcoming + recently-released into `stock_normalized_events` |
| `price_agent`         | weekday 21:30 UTC | yfinance EOD closes → outcome audit → EMA weight update per agent → digest |
| `backtester`          | manual / weekly   | 180-day replay (filings + earnings + momentum) → Sharpe, precision, calibration metrics |
| `site_generator`      | `*/15 * * * *`    | Supabase → Jinja2 HTML → FTPS auto-deploy to Hostinger |
| `source_review_agent` | `0 13 1 * *`      | Monthly health check on every external feed → Telegram digest |

### One-time helper

| Script | When | Purpose |
|---|---|---|
| `historical_ingest.py` | Run once via `historical_ingest.yml` workflow_dispatch | 6-month backfill of EDGAR filings + earnings + daily prices so the backtester has a real history to learn from |

## Signal vocabulary

- **WATCH**       — score ≥70, bullish cluster (≥2 agents agree)
- **RESEARCH**    — score ≥50, single strong signal or weaker cluster
- **AVOID_CHASE** — score ≥50, bearish cluster (S-3, 8-K dilution flagged via primaryDocDescription, earnings miss, downgrade, bearish news)
- **CHASE_RISK**  — would-be WATCH/RESEARCH on a ticker that already moved >5% since the cluster's earliest event. Recorded for review, not dispatched to Telegram.

## Dashboard tabs

`Dashboard · Signals · Events · Agents · Backtest · Learning` — plus per-ticker chart pages
under `/ticker/{TICKER}.html` (180-day price + filing + earnings overlay + "Big Moves" explanation
table) and per-alert detail pages under `/alert/{id}.html` for the link in every Telegram message.

## Layout

```
.
├── docs/                       Design doc + Phase 0 checklist
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
7. Wait one `*/15` cycle for `site_generator` to render and deploy
8. After a few weeks of live signals, `price_agent`'s EMA loop populates per-agent weights
   that `thesis_agent` then uses to amplify reliable agents

## Security note

Agents read secrets only via GitHub Actions `secrets.*` — never committed to the repo.
Local Claude Code tool permissions live in `.claude/settings.local.json` (git-ignored).
Do not commit that file; it may contain local API keys used for interactive verification.
