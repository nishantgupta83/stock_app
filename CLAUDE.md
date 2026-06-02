# stock_app — Claude project context

Real-time multi-source market intelligence pipeline. 25 GitHub Actions agents → Supabase →
Telegram → static dashboard. Paper-trading until rule maturity (>=90% accuracy, n>=30)
unlocks BUY/SELL. See `README.md` for the full surface; this file is the fast-load context.

**Project scope — read first:** solo-developer project, sole purpose is personal financial
freedom. Not a commercial product, no clients, no team, no monetization. Free-tier
infrastructure only. Operational outputs are personal; the public repo exists for
transparency and to unlock GitHub Actions free minutes. See [`README.md`](README.md)
"Project scope" for the design constraints that follow from this.

## Critical rules — bugs we've hit, do not repeat

**1. `event_at` ≠ `created_at` in freshness queries.**
- `event_at` = real-world event date (SEC filing time, post time, earnings date) — can be days/weeks old
- `created_at` = when the row landed in our DB
- For "what landed recently" queries (event_paper_agent, intraday_alert_agent context), filter by `created_at`. We hit this bug twice in May 2026.

**2. PostgREST `?on_conflict=` fails on partial unique indexes (error 42P10).**
- Three migrations in this repo exist to fix this exact pattern (`sql/0013`, `sql/0015`).
- When inserting into `stock_event_paper_trades`, `stock_event_outcome_observations`, etc., either:
  (a) pre-filter duplicates and plain INSERT (current pattern in `event_paper_agent.write_paper_trades`), or
  (b) ensure the index is non-partial via migration.

**3. Verify Supabase state after claims.**
- "Alert sent" → confirm `stock_signals.status_v2 = 'sent'`.
- "Trades opened" → confirm rows in `stock_event_paper_trades`.
- A claim without verification is an intention.

**4. Mac-specific gotchas inherited from global CLAUDE.md.**
- Use `/usr/bin/python3` for network servers (Homebrew Python firewalled).
- BSD sed: `sed -i ''` (with empty string).
- See `~/.claude/CLAUDE.md` for the full list.

**5. Hostinger FTPS (`ftp.hub4apps.com`) has intermittent control-socket timeouts.**
- The `site_generator` workflow runs 3 in-line FTPS retries, but all 3 sit inside the
  same ~10-min window, so a Hostinger outage longer than that burns every attempt.
- Mitigation: `.github/workflows/site_generator_retry.yml` listens for `site_generator`
  failures triggered by `schedule` or `workflow_run`, sleeps 5 min (gives the FTP server
  time to recover), and re-dispatches `site_generator` via `workflow_dispatch`.
- The retry is single-shot: failures originating from `workflow_dispatch` are NOT
  retried, so a still-broken Hostinger surfaces as a hard failure after one attempt
  instead of cascading.
- External backup beyond that: `cron-job.org` pingers (see RUNBOOK §8) hit
  `site_generator` every 15 min staggered off the GHA cron, so even a busted retry
  will be picked up within ~7 min by the next external dispatch.

**6. GitHub Actions cron is best-effort, not a guarantee.**
- Documented behavior: `schedule:` triggers can be delayed or dropped under runner-pool
  load. We've observed >90-min gaps in `*/15` workflows.
- Mitigation: external pingers at `cron-job.org` re-dispatch the 7 tightest-cadence
  workflows (`site_generator`, `paper_trade_agent`, `intraday_alert_agent`,
  `filing_agent`, `news_agent`, `thesis_agent`, `truth_social_agent`) staggered off
  the GHA cron. Bootstrap script: `scripts/bootstrap_cronjob_org.py` (re-runnable,
  idempotent — see RUNBOOK §8).
- All seven workflows already have `concurrency: cancel-in-progress: true`, so a
  duplicate dispatch from GHA cron + pinger is harmless — one is cancelled within
  ~1 second.

## Project conventions

- **Vocabulary:** Bot uses WATCH / RESEARCH / AVOID_CHASE / CHASE_RISK until a rule's
  paper-trade accuracy crosses 90% with n>=30. Only then does it graduate to BUY / SELL.
  Never hardcode BUY/SELL outside the maturity gate.
- **Scoring:** §17.7 100-point rubric in `agents/thesis_agent.py:score_evidence()`. Intelligence
  layer adds sector cluster bonus, hyperscaler echo, power scarcity, risk-off filter on top.
- **Severity-4 events** bypass `MAX_ALERTS_PER_DAY = 5` cap (LITE-style critical alerts).
- **No purple in UI** (use teal/coral/amber/sage/sky-blue pastels per global pref).
- **No AI/assistant branding** in user-facing strings.

## Common commands

**Query the live DB (set once per shell):**

> ⚠️ **Run only in a private shell.** `SUPABASE_SERVICE_KEY` is the
> service_role token — full DB write access including bypass of RLS.
> Never paste the value into a chat, screenshot, paste-bin, or shared
> terminal. The command below stores it in the current shell only; the
> key never touches stdout. After your session, `unset SUPABASE_SERVICE_KEY`
> if you're worried about shell-history leakage.

```bash
export SUPABASE_URL="https://wlfwdtdtiedlcczfoslt.supabase.co"
export SUPABASE_SERVICE_KEY="$(supabase projects api-keys --project-ref wlfwdtdtiedlcczfoslt | awk '/service_role/{print $NF}')"

# Latest job runs
curl -s "${SUPABASE_URL}/rest/v1/stock_job_runs?order=started_at.desc&limit=10&select=agent,status,started_at,rows_in,rows_out" \
  -H "apikey: ${SUPABASE_SERVICE_KEY}" -H "Authorization: Bearer ${SUPABASE_SERVICE_KEY}"
```

**Trigger an agent manually:**

```bash
gh workflow run <agent>.yml --repo nishantgupta83/stock_app
gh run watch <run_id> --repo nishantgupta83/stock_app
```

**Backfill paper trades from historical events:**

```bash
gh workflow run event_paper_agent.yml --repo nishantgupta83/stock_app -f backfill_days=14
```

**Apply SQL migration to remote DB:** `supabase db push --linked` (CLI tracks `supabase/migrations/`,
NOT `sql/`). Migrations in `sql/` must be applied via Supabase Management API or psql with
DB password. The local `supabase db query` requires CLI v2.79+; we have older. Workaround:
PostgREST `execute_sql` via MCP, or direct INSERTs for DML.

## Architecture quick map

Six-layer pipeline with strict boundaries — each layer reads from layers
below and writes only to its own output table. Bugs in one layer cannot
corrupt the layers above.

```
Layer 1 — INGEST
  in:  external (EDGAR, RSS, Truth Social, FRED, FDA, ctgov, yfinance, 13F-HR)
  out: stock_normalized_events
  agents: filing_agent, news_agent, truth_social_agent, earnings_agent,
          crypto_macro_agent, flows_agent, biotech_agent, defense_agent,
          energy_transition_agent, activist_insider_agent, consumer_health_agent,
          macro_rates_agent, intraday_alert_agent, market_scanner_agent

Layer 2 — INTELLIGENCE
  in:  stock_normalized_events + stock_rule_calibration
  out: stock_signals (vocabulary-gated by maturity tier; valid_until per signal)
  agent: thesis_agent (cluster + 100-pt rubric + intelligence bonuses)

Layer 3 — TRADE CONSTRUCTION                                     (added 2026-05-18)
  in:  stock_signals
  out: stock_trade_setups (entry style, stop/target, valid_until, confidence, reason_to_skip)
  agent: trade_setup_agent

Layer 4 — RISK / CAPITAL ALLOCATION                              (added 2026-05-18)
  in:  stock_trade_setups + stock_event_paper_trades + stock_rule_calibration
  out: stock_risk_decisions (sized or skip, with rules_applied audit trail)
  agent: risk_agent (HARDCODED rules: Van Tharp sizing, drawdown circuit
         breaker, daily risk budget, rule-concentration cap, stop sanity)

Layer 5 — LEARNING
  in:  stock_signals + stock_event_paper_trades + price data
  out: stock_rule_calibration, stock_agent_weights
  agents: event_paper_agent (opens 4 horizons/event), price_agent (EOD
          reconcile + MFE/MAE + payoff metrics), paper_trade_agent,
          backtester

Layer 6 — PRESENTATION (read-only)
  in:  everything
  out: hub4apps.com/stock_app/{dashboard, status.json}, Telegram alerts
  agents: site_generator, telegram_dispatcher
```

**Key tables:**
- `stock_normalized_events` — universal event bus (Layer 1 output)
- `stock_signals` — scored intelligence (Layer 2 output; has valid_until + structured score_breakdown)
- `stock_trade_setups` — tradable proposals (Layer 3 output)
- `stock_risk_decisions` — sized or skipped (Layer 4 output; has rules_applied audit)
- `stock_event_paper_trades` — open/closed paper trades with MFE/MAE/target_hit/stop_hit
- `stock_rule_calibration` — per-rule accuracy + payoff (profit_factor, avg_win/loss, hit rates)
- `stock_agent_weights` — per-agent EMA learned weights
- `stock_job_runs` — operational log; has run_type ('agent' vs 'wrapper') + parent_run_id lineage
- `stock_watchlists` — categorized ticker baskets

**Views:**
- `stock_rule_sector_multiplier` — per-(rule_key, sector) calibration multiplier,
  floored at n>=30 per cell and bounded [0.5, 1.3]. Auto-refreshes from
  `stock_event_paper_trades` + `stock_symbols`. Consumed by `thesis_agent` only
  when `SECTOR_CALIB_MULT_ENABLED=true` (default off). Added 2026-05-31; see
  `sql/0032_rule_sector_multiplier_view.sql` and `docs/findings/` for the rationale.
- `stock_realistic_loop_summary` — read-only aggregate of the $5K shadow
  portfolio (state + open/closed position counts). Added 2026-05-31; full
  design in `docs/realistic-loop.md`.

**Isolated loops (do NOT write to stock_rule_calibration):**
- `stock_realistic_loop_positions` / `stock_realistic_loop_state` — capital-deployed
  shadow ledger keyed by `loop_name`. Default loop `shadow_5k`: $5K bankroll,
  5 concurrent $1K positions, cash recycles on close. Owned by
  `realistic_loop_agent`. See `docs/realistic-loop.md`.

**Health monitoring (added 2026-06-02):**
- `stock_health_pulse` — append-only ledger of (agent, check_name, status,
  observed, threshold, pulsed_at). Written hourly by per-workflow
  pulsecheck agents in `agents/pulsecheck/`. Read via
  `stock_health_pulse_current` (latest pulse per check) or
  `stock_health_pulse_recent_alerts` (24h warning/critical feed).
  Design + extension pattern: `docs/pulsecheck.md`. Each pulsecheck owns
  a defined scope; shared facts have a single owner with `depends_on`
  declarations to prevent cascading false alarms.

**Thesis rejection audit (added 2026-06-02):**
- `stock_thesis_rejections` — append-only audit of clusters dropped by
  thesis_agent before emit. Records fail_reason (cluster_passes vs
  action_empty_low_score), score, catalyst_score, breakdown sample.
  Used to measure WHICH gate is binding so the next thesis fix is
  data-driven.
- `stock_thesis_rejection_mix` (view) — rolling 24h fail_reason
  distribution. Consumed by `pulsecheck_thesis.rejection_distribution`.

**8. price_agent now has a stock_raw_prices fallback (2026-06-02).**
- Prior bug: `fetch_bars` was yfinance-only. On transient failures it
  returned `{}` and reconcile silently skipped the trade with `if not
  bars: continue` — no log, no counter. The 513-stuck-h1d incident
  traced to this. Trades stayed open across every subsequent run.
- Fix: `fetch_bars` tries yfinance, falls back to `stock_raw_prices`
  via `_bars_from_raw_prices`. Skip counters land in
  `stock_job_runs.meta.reconcile.{n_skipped_no_bars, n_skipped_no_outcome,
  skipped_tickers}`. `pulsecheck_price_agent.reconcile_skip_rate` reads
  that meta and warns at >5% skip.
- Cron bumped from `30 21 * * 1-5` (once daily) to `0 */2 * * 1-5`
  (every 2h weekday) so transient yfinance hiccups recover within hours
  rather than days.
- One-shot cleanup: `scripts/close_stuck_paper_trades.py` resolved the
  existing 513-trade backlog on 2026-06-02.

**Feature flags (env vars):**
- `SECTOR_CALIB_MULT_ENABLED` — toggles sector-aware scoring in `thesis_agent`.
  Default off. When on, score_evidence multiplies event-tied rule points by the
  cell's multiplier from `stock_rule_sector_multiplier`. Effect appears in
  `stock_signals.score_breakdown[].sector_mult`.

**7. `MAX_ALERTS_PER_DAY` in `thesis_agent` is now per-lane, not global.**
- Prior bug: `alerts_sent_today()` queried `stock_signals` without filtering
  by `model_version`, so `intraday_alert_agent`'s daily volume (10-20+ spike
  alerts) silently consumed thesis_agent's 5/day cap. Thesis was emit-silent
  for the entire 5/22–6/2 window without anyone noticing.
- Fix (2026-06-02): `alerts_sent_today(model_version=MODEL_VERSION)` scopes
  the count to rubric-v1.1 only. Intraday continues to use its own per-run
  `ALERT_CAP=25` and does not consume thesis's daily budget.
- Effect: two independent budgets — thesis 5/day rubric + intraday 25/run
  spike. Watch for any future agent that calls `alerts_sent_today()` — must
  pass its own model_version or it will count cross-lane traffic again.

**Watchlists** (Phase 10 AI cluster, May 2026):
`core`, `context`, `ai_compute`, `ai_optical`, `ai_servers`, `ai_power`, `ai_software`, `ai_neocloud`,
`institutions`, `mutual_funds`. Multi-domain expansion (defense/biotech/energy/macro/activist/consumer)
planned in `docs/multi-domain-roadmap.md`.

## When extending

- **New agent**: copy `agents/filing_agent.py` skeleton + add YAML workflow. Reuse
  `ops_recorder.py` for workflow-level health tracking.
- **New event type**: add to `_DIRECTION_DEFAULT` in `event_paper_agent.py` so paper trades
  get the right direction; add scoring rule in `thesis_agent.score_evidence()`; verify
  filter in `event_paper_agent.fetch_recent_events` uses `created_at` not `event_at`.
- **New ticker**: insert into `stock_symbols` (with CIK if SEC-tracked) + `stock_watchlists`.
  Trigger `historical_ingest.yml` and `filing_agent.yml` to backfill.
- **Telegram-level change**: `agents/telegram_dispatcher.py` formats payload; `thesis_agent`
  governs cap + dedupe; `intraday_alert_agent` is the fast-twitch path with its own dedupe key.

## Deferred-action findings

`docs/findings/` — observations whose action is deferred (refactor cost,
insufficient data, etc.). Each doc states "what would change our mind" so a
future reviewer can decide whether to act without re-deriving the observation.
Index in `docs/findings/README.md`.

## Hooks active in this repo

`.claude/hookify.*.local.md` — three rules that prevent the bug classes above. Review them
if you see a "⚠️" warning fire during tool use.
