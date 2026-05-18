# Operational Runbook

This document answers one question: **what keeps running when no human is watching?**

The pipeline is designed for solo operation. Every component below executes on a schedule with
no daily human intervention. This runbook enumerates what is autonomous, what depends on
external infrastructure, and what to do when something breaks.

---

## 1. The autonomous pipeline

### GitHub Actions cron schedule (UTC)

| Cadence | Workflows | Purpose |
|---|---|---|
| `*/5 * * * *` | `filing_agent`, `news_agent`, `truth_social_agent`, `thesis_agent` | Hot path: ingest + score |
| `5 * * * *` | `event_paper_agent` | Open 4 paper trades per fresh event |
| `*/15 * * * *` | `site_generator`, `paper_trade_agent` | Refresh dashboard + Codex paper path |
| `*/15 13-21 * * 1-5` | `intraday_alert_agent` | Fast-twitch spike detection during US market hours |
| `*/30 * * * *` | `trade_setup_agent`, `risk_agent` | Layer 3+4 — signal → setup → risk decision |
| `15 */2 * * *` | `activist_insider_agent` | 13D + Form 4 cluster detection |
| `0 13 * * 1-5` | `macro_rates_agent` | Pre-market macro pulse (FRED) |
| `0 14 * * 1-5` | `biotech_agent` | FDA RSS + clinicaltrials.gov Phase 3 events |
| `0 15 * * 1-5` | `consumer_health_agent` | Consumer-discretionary signal |
| `45 13 * * 1-5` | `energy_transition_agent` | Energy/uranium/EV catalysts |
| `0 12 * * 0` | `earnings_agent` | Weekly Sunday — populate week-ahead earnings dates |
| `0 14 * * 0` | `flows_agent` | Weekly Sunday — 13F-HR institutional position diff |
| `30 21 * * 1-5` | `price_agent`, `market_scanner_agent` | **EOD: close paper trades + update calibration** |
| `30 22 * * 1-5` | `defense_agent` | After-close DoD contract scan |
| `35 21 * * 1-5` | `crypto_macro_agent` | After-close BTC/ETH probe |

**The critical learning step is `price_agent` at 21:30 UTC weekdays.** This is where matured
paper trades get closed, `stock_rule_calibration` updates, agent EMA weights step, and
`recompute_rule_payoff` extends the payoff metrics. Without this, the system stops learning.

### workflow_run chain (event-driven, mitigates cron drift)

`site_generator` and downstream layers also fire on completion of upstream agents:

```
intraday_alert_agent ─┐
thesis_agent ─────────┤
news_agent ───────────┤
truth_social_agent ───┼─→ site_generator (refresh status.json + dashboard)
paper_trade_agent ────┤
filing_agent ─────────┤
event_paper_agent ────┘

thesis_agent ─────────→ trade_setup_agent ─→ risk_agent
```

The chain exists because GitHub Actions cron is best-effort and can drop cycles during high
load. Event-driven triggers add resilience for the most important refresh paths.

### The three Claude Code routines (paid Anthropic API)

These consume your Claude Pro plan; the GitHub Actions pipeline does NOT depend on them.

| Schedule | Routine | What it does |
|---|---|---|
| Daily 11:00 UTC (7 AM ET) | premarket prep digest | Fetch `status.json` → 400-word morning summary |
| Mon-Fri 13:20 UTC (9:20 AM ET) | opening read digest | Fast-twitch open-bell summary |
| Mon-Fri 20:00 UTC (4 PM ET) | market close digest | Today's closed trades + tier movements + interpretation |

**These are summary readers, not pipeline writers.** If your Claude Pro tokens run out, the
GitHub Actions pipeline keeps running and writing to Supabase; you just stop receiving the
written digests. The dashboard, Telegram alerts, and `status.json` continue.

---

## 2. External dependencies (and what breaks if they fail)

| Service | What we use | Free-tier limit | Failure impact |
|---|---|---|---|
| GitHub Actions | Cron-scheduled workflows | Unlimited for public repos | Pipeline halts. Severe. |
| Supabase | Postgres + PostgREST | Free tier (500 MB, 50k MAU) | Pipeline halts. Severe. |
| Hostinger | FTPS static hosting | Already paid | Dashboard stale but DB still has truth |
| Telegram Bot API | Alert delivery | Free | Alerts stop; pipeline still learns |
| yfinance (Yahoo) | Price bars (`event_paper_agent`, `price_agent`, `activist_insider_agent`) | Rate-limited but generous | EOD reconciliation may skip a day; auto-recovers next run |
| EDGAR | SEC filings RSS | Free with User-Agent rule | `filing_agent` halts; agent's gaps backfill via `historical_ingest.py` |
| FRED | Fed data API | 1000 calls/day | `macro_rates_agent` halts |
| FDA RSS, ClinicalTrials.gov | Biotech catalysts | Free | `biotech_agent` halts |

The single point of failure is **Supabase**. Everything writes there. If that bucket fills or
free tier hits, the pipeline silently fails. Audit `Settings → Usage` in the Supabase dashboard
periodically.

---

## 3. Failure modes seen in practice

| Symptom | Diagnosed cause | Fix |
|---|---|---|
| `intraday_alert_agent` running at ~10% of cron schedule | GitHub Actions cron drift under load | Accept; the workflow_run chain on site_generator handles freshness propagation |
| `thesis_agent` produces 0 signals for days | New domain agents emit single-source events; cluster rule required ≥2 distinct agents | Fixed in `a2e71e8` — added single-source exceptions for FDA / clinical / DoD / nuclear / insider cluster |
| `event_paper_agent` throws `offset-naive vs offset-aware` | Supabase returns timestamps without tz suffix in some rows | Fixed in `6e1c88f` — force tz-aware parsing in stale-price gate |
| `biotech_agent` crashed on first run | Missing `timedelta` import | Fixed in `1460788` |
| Dashboard shows inflated "X / Y healthy" | freshness view contains BOTH agent + workflow_agent rows | Fixed in `2ec0905` (dedupe) and `0e90534` (threshold formula) |

When you encounter something not on this list, the diagnostic sequence is:
1. `gh run list --workflow=<agent>.yml --limit 5` — did it run?
2. Query `stock_job_runs WHERE agent = '<name>' ORDER BY started_at DESC LIMIT 3` — did it succeed?
3. `status.json` → `recent_failures[]` — did it land in dead letters?

---

## 4. What survives a 30-day "no human" window

If you stop touching this project for 30 days, here's what continues:

**Continues running:**
- All 25 GitHub Actions agents on their cron schedules (with normal drift)
- `event_paper_agent` opens 4 paper trades per fresh event
- `price_agent` closes matured trades + updates calibration each weekday EOD
- `site_generator` refreshes the dashboard every 15 min (or on workflow_run completion)
- Telegram alerts continue
- `status.json` stays current

**Degrades gracefully:**
- If a single agent fails (e.g., upstream API timeout), the other 24 keep running
- The workflow_run chain ensures site_generator refreshes even when its own cron drifts
- Concentration caps in `risk_agent` prevent runaway position-buildup on a single rule

**Will silently fail (worth a monthly check):**
- Supabase free-tier exhaustion (rare with our scale; periodically check)
- Hostinger FTPS password expiry (manual rotation)
- yfinance throttling (auto-recovers when limits reset)

**Will require manual intervention:**
- Schema migrations (new SQL files in `sql/`) — apply with `supabase db push --linked`
- Telegram bot token rotation (Anthropic Pro is independent of this)
- New ticker additions to `stock_symbols`

---

## 5. The learning loop is self-cycling

```
   Event ingested → stock_normalized_events
                ↓
   event_paper_agent opens 4 paper trades (1d/7d/15d/30d horizons)
                ↓
   stock_event_paper_trades (status=open)
                ↓
   price_agent runs weekday 21:30 UTC
                ↓
   matured trades closed → realized_return + MFE/MAE/target_hit/stop_hit
                ↓
   stock_rule_calibration updated (accuracy, profit_factor, payoff metrics)
                ↓
   recompute_rule_payoff() reduces all closed trades for that rule
                ↓
   stock_agent_weights EMA steps (alpha=0.1)
                ↓
   Next thesis_agent run reads the updated weights and calibration
                ↓
   Stronger rules score higher; weak rules naturally demoted
                ↓ (repeat indefinitely)
```

No tuning required. The system gets better as it sees more outcomes. The only manual
intervention the loop needs is data: as new tickers, agents, or watchlists are added, the
calibration starts at zero for them.

---

## 6. Manual triggers (if you ever need them)

```bash
# Re-run a stuck agent
gh workflow run <agent>.yml --repo nishantgupta83/stock_app

# Force a dashboard refresh
gh workflow run site_generator.yml --repo nishantgupta83/stock_app

# Run a backtest (manual only — produces stock_signals with status_v2='backtest')
gh workflow run backtester.yml --repo nishantgupta83/stock_app

# Apply a new SQL migration to live DB
cp sql/00XX_new.sql supabase/migrations/$(date -u +%Y%m%d%H%M%S)_new.sql
supabase db push --linked
```

## 7. Where to look first when something is wrong

1. **`status.json`** — `https://hub4apps.com/stock_app/status.json` gives a self-describing
   snapshot. Includes platform vocabulary, maturity gates, agent inventory + freshness, recent
   failures, and calibration summary.
2. **Dashboard** — `https://hub4apps.com/stock_app/agents.html` for the per-agent health view.
3. **`stock_job_runs`** — operational log; `status='failed'` is the canary.
4. **`stock_dead_letter_events`** — agents that hit unrecoverable errors record here.
5. **GitHub Actions runs** — `gh run list --repo nishantgupta83/stock_app --limit 30`.

If `status.json` itself is 404, the FTPS deploy or site_generator is the problem — start there.
