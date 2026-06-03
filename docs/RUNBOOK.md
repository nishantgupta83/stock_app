# Operational Runbook

This document answers one question: **what keeps running when no human is watching?**

**Project scope:** solo-developer project; sole purpose is personal financial freedom.
Not a commercial product or service. There is no on-call rotation, no SLA, and no team —
the runbook below is a single-operator playbook designed for periodic check-ins, not
24/7 monitoring. Free-tier infrastructure only. See [`../README.md`](../README.md)
"Project scope" for the design constraints that follow from this.

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
| `0 4 * * *` | `audit_agent` | **Daily integrity check** — five cross-table invariants, Telegrams on failure |

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
| Hostinger | FTPS static hosting | Already paid | Dashboard stale but DB still has truth. Has intermittent control-socket timeouts — see §3 + §8. |
| cron-job.org | External cron backup pinger for 7 tight-cadence workflows | Free (50 jobs / 60s min) | GHA cron drift increases (still functional); no data loss. |
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
| `intraday_alert_agent` running at ~10% of cron schedule | GitHub Actions cron drift under load | Mitigated 2026-05-18 by cron-job.org external pinger (see §8). Pre-mitigation accept-and-rely-on-workflow_run-chain remains the fallback. |
| `thesis_agent` produces 0 signals for days | New domain agents emit single-source events; cluster rule required ≥2 distinct agents | Fixed in `a2e71e8` — added single-source exceptions for FDA / clinical / DoD / nuclear / insider cluster |
| `event_paper_agent` throws `offset-naive vs offset-aware` | Supabase returns timestamps without tz suffix in some rows | Fixed in `6e1c88f` — force tz-aware parsing in stale-price gate |
| `biotech_agent` crashed on first run | Missing `timedelta` import | Fixed in `1460788` |
| Dashboard shows inflated "X / Y healthy" | freshness view contains BOTH agent + workflow_agent rows | Fixed in `2ec0905` (dedupe) and `0e90534` (threshold formula) |
| Dashboard frozen >30 min despite GHA cron firing | Hostinger FTPS control-socket timeout — all 3 in-job retries failed within same outage window | Mitigated 2026-05-18 by `site_generator_retry.yml` (5-min delayed re-dispatch); see §8. |
| GHA cron silently dropped 5+ consecutive `*/15` firings overnight | GitHub Actions runner-pool best-effort scheduling | Mitigated 2026-05-18 by cron-job.org pingers on 7 tight-cadence workflows; see §8. |

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
- `site_generator` refreshes the dashboard every 15 min (or on workflow_run completion), regenerating all 11 tabs including the **Weekly** retrospective (perf · rule maturity · funnel)
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

# Force a dashboard refresh (regenerates all 11 tabs: Dashboard, Signals,
# Events, Setups, Risk, Agents, Backtest, Paper Trades, Calibration,
# Weekly, Learning + per-ticker + per-alert pages)
gh workflow run site_generator.yml --repo nishantgupta83/stock_app

# Run a backtest (manual only — produces stock_signals with status_v2='backtest')
gh workflow run backtester.yml --repo nishantgupta83/stock_app

# Apply a new SQL migration to live DB
cp sql/00XX_new.sql supabase/migrations/$(date -u +%Y%m%d%H%M%S)_new.sql
supabase db push --linked
```

## 7. Where to look first when something is wrong

1. **`status.json`** — `https://hub4apps.com/stock_app/status.json` gives a self-describing
   snapshot. Includes `git_sha`, `pipeline_version`, `agents.inventory_count`, per-layer
   counts (ingest / intelligence / trade_construction / risk / learning / presentation),
   platform vocabulary, maturity gates, agent freshness, recent failures, calibration summary.
   The `git_sha` field is the single source of truth for what's deployed — compare it against
   `git rev-parse HEAD` to confirm the FTPS upload completed.
2. **Dashboard** — `https://hub4apps.com/stock_app/agents.html` for per-agent health view.
   `trade_setups.html` and `risk_decisions.html` surface Layer 3 + 4 state.
3. **`audit_agent` Telegram** — daily at 04:00 UTC; alerts on five integrity invariants
   (dispatch_log match, sized-decision validity, calibration count consistency, no stale
   opens, event ingest cardinality). Silence = pass.
4. **`stock_job_runs`** — operational log; `status='failed'` is the canary.
5. **`stock_dead_letter_events`** — agents that hit unrecoverable errors record here.
6. **GitHub Actions runs** — `gh run list --repo nishantgupta83/stock_app --limit 30`.

If `status.json` itself is 404, the FTPS deploy or site_generator is the problem — start there.

---

## 8. External cron backup (`cron-job.org`) + delayed retry

Added 2026-05-18 after observing a 5-cycle GHA cron drop overnight and a separate
Hostinger FTPS timeout incident on the same day. These two mitigations are independent
of each other and address different failure modes.

### 8.1 Why external pingers exist

GitHub Actions `schedule:` triggers are documented as **best-effort**. Observed
behavior: under runner-pool load, scheduled workflows can be delayed 30–60 min or
silently dropped. On 2026-05-18 we observed `site_generator` (`*/15 * * * *`) sit
idle for ~90 min, leaving the dashboard frozen even though all upstream agents had
produced fresh data.

For the seven tightest-cadence workflows, this matters:

| Workflow | GHA cron | Why frequent runs matter |
|---|---|---|
| `filing_agent` | `*/5 * * * *` | EDGAR Form 4 / 8-K freshness for intraday alerts |
| `news_agent` | `*/5 * * * *` | Catalyst freshness — late news = late signal |
| `thesis_agent` | `*/5 * * * *` | Cluster scoring; downstream of all ingest agents |
| `truth_social_agent` | `*/5 * * * *` | DJT-driven momentum is minute-sensitive |
| `site_generator` | `*/15 * * * *` | Dashboard staleness is operator-visible |
| `paper_trade_agent` | `*/15 * * * *` | Forecast freshness |
| `intraday_alert_agent` | `*/15 13-21 * * 1-5` | Market-hours-only fast-twitch alerter |

For the remaining 18 workflows (hourly / daily / weekly cadences) GHA cron drift is
acceptable — a single dropped firing self-heals on the next slot.

### 8.2 Architecture

```
GitHub Actions cron     ─────┐
                              │ (best-effort, can drop)
                              ▼
                         Workflow runs ───────────┐
                              ▲                   │
                              │ POST dispatch     ▼
cron-job.org pinger     ─────┘            stock_job_runs
(7 jobs, staggered)                       (status: ok/failed)
                                                  │
                              ┌─── if failure ────┘
                              ▼
                       site_generator_retry.yml
                       (waits 5 min, re-dispatches once)
```

The pinger fires on a **staggered** schedule (off by 2–7 min from the GHA cron) so
that:
- When GHA cron is healthy, both fire; `concurrency: cancel-in-progress: true` in
  each workflow cancels the duplicate within ~1 second. Net cost: trivial.
- When GHA cron drops a slot, the pinger fills the gap within 2–7 min instead of
  waiting for the next GHA-scheduled slot 15 min later.

Staggering schedule per workflow:

| Workflow | GHA cron | Pinger cron |
|---|---|---|
| `filing_agent`, `news_agent`, `thesis_agent`, `truth_social_agent` | `:00,:05,:10,…,:55` | `:02,:07,:12,…,:57` |
| `site_generator`, `paper_trade_agent` | `:00,:15,:30,:45` | `:07,:22,:37,:52` |
| `intraday_alert_agent` | `:00,:15,:30,:45` (UTC 13-21 Mon-Fri) | `:07,:22,:37,:52` (same window) |

### 8.3 Bootstrap / rotation procedure

The seven cron-job.org jobs are provisioned by `scripts/bootstrap_cronjob_org.py`.
The script is **idempotent** — it PATCHes existing jobs whose title matches the
`stock_app:<workflow>` convention, and PUTs missing ones.

**One-time setup** (already done 2026-05-18):
1. Create a cron-job.org account; generate an API key from Settings → API Keys.
2. Create a fine-scoped GitHub Personal Access Token (PAT) at
   https://github.com/settings/personal-access-tokens with **Actions: Read and write**
   on `nishantgupta83/stock_app` only.
3. Run the script:
   ```bash
   export CRONJOB_API_KEY="..."
   export GH_DISPATCH_PAT="github_pat_..."
   python3 scripts/bootstrap_cronjob_org.py
   ```
   The script pre-checks the PAT (read the workflows list) before provisioning.

**Rotation** (when GitHub PAT expires — currently set to 2026-08-16):
- Generate a new PAT with identical scopes.
- Set `GH_DISPATCH_PAT` to the new value.
- Re-run `scripts/bootstrap_cronjob_org.py` — it will PATCH each job's header with
  the new bearer token. No cron-job.org-side changes needed.

**Extending to more workflows**: edit the `WORKFLOWS` dict at the top of
`scripts/bootstrap_cronjob_org.py` and re-run. No other changes.

### 8.4 Delayed retry for `site_generator` failures

A separate concern from cron drift: Hostinger's FTPS server (`ftp.hub4apps.com`)
has intermittent control-socket timeouts. The `site_generator` workflow already
performs 3 in-job retries with `continue-on-error`, but all three sit inside the
same ~10-min job window. A Hostinger outage longer than that burns every attempt.

Mitigation: `.github/workflows/site_generator_retry.yml`. It triggers on
`site_generator` `workflow_run` completion with `conclusion: failure`, waits 5 min
(typical Hostinger recovery is 2–10 min), and re-dispatches `site_generator` via
`workflow_dispatch`.

**Single-shot guarantee**: the retry filters out failures triggered by
`workflow_dispatch`. If the retry itself dispatches `site_generator` and that run
also fails, the retry workflow does NOT fire again — the second failure surfaces
as a hard error in `stock_job_runs` and cron-job.org's `stock_app:site_generator`
pinger picks it up at the next 15-min slot.

**Permissions**: the retry workflow uses the auto-issued `GITHUB_TOKEN` (which
includes `actions:write` for the same repo). No PAT is required.

### 8.5 Monitoring

- **cron-job.org dashboard**: https://console.cron-job.org/jobs — execution history
  per job, with HTTP response codes from GitHub. A successful dispatch returns 204.
- **Email notifications**: each job is configured with `onFailure: true,
  onFailureCount: 2` — two consecutive failures (e.g. expired PAT, GitHub API outage)
  trigger an email to the cron-job.org account holder.
- **Verifying a pinger actually triggered a GHA run**: filter
  `gh run list --workflow=<name>.yml --json event,createdAt` for
  `event: workflow_dispatch`. The pinger's dispatches will be on the
  staggered minutes (`:07/:22/:37/:52` etc.); GHA's own cron fires on the
  even ones (`:00/:15/:30/:45`).

### 8.6 What this does NOT solve

- Sustained Hostinger FTPS outage (>15 min): the single-shot retry will fail too;
  the cron-job.org pinger 7–15 min later will try again. If multiple consecutive
  pingers fail, you'll get a cron-job.org email — at that point Hostinger needs
  manual investigation (their FTP server may be in long maintenance).
- cron-job.org itself going down: GHA cron resumes its best-effort role.
- GitHub Actions outage: nothing helps. Wait it out.
- PAT revoked / expired: pinger fires return 401; cron-job.org emails after 2
  consecutive 401s; rotate via the procedure in §8.3.

## 9. Learning artifacts cadence (monthly / quarterly)

Added 2026-06-03. Three layers of time-indexed learning docs in
`docs/learning/`:

### 9.1 Monthly reconciliation

Run at end of each month (or on demand) to regenerate the full
sequential replay through the most recent data:

```bash
SUPABASE_URL="..." SUPABASE_SERVICE_KEY="..." \
  python3 scripts/sequential_monthly_replay.py
```

Writes / overwrites:
- `docs/learning/YYYYMM_monthly_reconc.md` — one per month, ~14 files.
- `docs/learning/YYYYQq_quarterly_review.md` — auto-emitted at quarter ends.
- `docs/learning/sequential_replay_summary_DDMMYYYY.md` — master roll-up.

Idempotent. Each run uses the current state of
`stock_event_paper_trades`, so re-running pulls in any new closed
trades since the last run.

### 9.2 Live quarterly consultant

The "independent consultant" — a deterministic rule-based reviewer
that reads live calibration + pulsecheck + recent monthly docs and
produces actionable recommendations.

```bash
SUPABASE_URL="..." SUPABASE_SERVICE_KEY="..." \
  python3 scripts/quarterly_consultant_review.py
```

Writes `docs/learning/YYYYQq_consultant_review.md` for the quarter
that just ended. Run quarterly (early in the next quarter), or on
demand whenever significant calibration changes have landed.

The doc lists concrete actions: rule flips, structural skips, sizing
amplifications. The operator decides which to ship — the consultant
does not auto-apply anything.

### 9.3 Pipeline-maturity scorecard

Per-agent / per-layer maturity audit:

```bash
SUPABASE_URL="..." SUPABASE_SERVICE_KEY="..." \
  python3 scripts/pipeline_maturity_audit.py
```

Writes `docs/pipeline-maturity-DDMMYYYY.md` showing operational %,
coverage volume, calibration depth, and actionable tier population.

### 9.4 What to do with the consultant's recommendations

1. Read the latest `YYYYQq_consultant_review.md`.
2. For each recommended flip, decide: does the evidence (n, acc, PF)
   justify reversing direction?
3. If yes: add the `rule_key` to a `STRUCTURAL_FLIP` set in
   `agents/thesis_agent.py`, gate behind a new env-flag (mirror the
   `SECTOR_CALIB_MULT_ENABLED` pattern), push, watch
   `pulsecheck_thesis.rejection_distribution` for 2 weeks.
4. Re-run the consultant after 2 weeks to confirm impact.

This is the closed-loop operator workflow. The consultant proposes
based on data; the operator decides; the pipeline learns from the
applied change.
