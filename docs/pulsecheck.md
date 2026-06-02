# Pulsecheck framework — proactive health monitoring

## Intent

Catch issues at "feverish" rather than "ICU." The 5/22–6/2 thesis-agent
silence (where intraday alerts ate the shared cap budget for 10 days
without anyone noticing) is the canonical motivating example: every
workflow was green, every cron was firing, but the system's central
scoring pipeline was emit-silent the whole time. A pulse heartbeat per
workflow would have surfaced it on day 1.

## Design rules

1. **One pulsecheck per workflow.** Scoped narrowly. No pulsecheck
   queries another's bucket. This is the *non-intersection* rule —
   it's what keeps the system from drowning in duplicate alerts.

2. **Shared facts have a single owner.** "Is Supabase reachable" is
   not a fact every pulsecheck should re-derive; it's owned by
   `pulsecheck_foundation`. Others depend on it via `depends_on`.

3. **Dependency-aware skipping.** When a foundational check has
   already flagged a problem (e.g., Supabase unreachable, bars stale),
   downstream pulsechecks report `status='precondition_failed'`
   instead of generating their own (probably-related) alerts. No
   cascading false alarms.

4. **Cron-driven, not on-demand.** Pulses run hourly via
   `.github/workflows/pulsecheck.yml`. The scheduler is its own
   accountability check: if the pulsecheck workflow stops, the absence
   of new pulses shows up directly in `stock_health_pulse_current.age_seconds`.

5. **Three statuses + two skips.** `ok | warning | critical` for
   meaningful states; `skipped | precondition_failed` for inapplicable
   ones. Skip "alarm" — too many levels invite numbness.

6. **Workflow exits 0 even on non-ok pulses.** A workflow failure
   means the pulsecheck itself broke. Non-ok status is information for
   the dashboard, not a CI/CD signal.

## Schema

`sql/0034_health_pulse.sql` creates:

- **`stock_health_pulse`** — append-only ledger. Rows: `(agent,
  check_name, status, detail, observed, threshold, meta, pulsed_at)`.
- **`stock_health_pulse_current`** — view, most recent pulse per
  `(agent, check_name)`. Includes `age_seconds` so you can spot
  stalled pulsechecks.
- **`stock_health_pulse_recent_alerts`** — view, all warning/critical
  pulses in the last 24h. Source for a daily digest.

## Codebase layout

```
agents/pulsecheck/
├── _pulse.py                # shared helpers — Check dataclass, run_checks runner
├── foundation.py            # owns: supabase_reachable, site_freshness, recent_bars
├── thesis.py                # owns: recent_runs, cap_consumption, candidate_dryness
├── event_paper.py           # owns: recent_runs, open_trade_age, horizon_balance, h1d_close_lag
├── realistic_loop.py        # owns: recent_runs, input_starvation, position_lifecycle, pnl_drawdown
└── news.py                  # owns: recent_runs, ingest_volume, classifier_neutrality, watchlist_coverage
```

The workflow `.github/workflows/pulsecheck.yml` runs each script as a
separate job, with `needs: foundation` on every non-foundational job.

## What each pulsecheck owns

| Pulsecheck | Workflow watched | Checks |
|---|---|---|
| `foundation` | (none — system-wide) | supabase_reachable, site_freshness, recent_bars |
| `thesis` | thesis_agent.yml | recent_runs, cap_consumption, **candidate_dryness** ← would have caught the 5/22 silence |
| `event_paper` | event_paper_agent.yml | recent_runs, open_trade_age, horizon_balance, h1d_close_lag |
| `realistic_loop` | realistic_loop_agent.yml | recent_runs, **input_starvation**, position_lifecycle, pnl_drawdown |
| `news` | news_agent.yml | recent_runs, ingest_volume, **classifier_neutrality** ← would have caught the Computex miss |

Bold checks are the ones designed to catch failure modes that have
already bitten this project. Their thresholds are calibrated to the
specific incidents.

## Extending — adding a pulsecheck for another workflow

Per the design rule, every existing workflow eventually deserves its
own pulsecheck. Pattern:

1. Create `agents/pulsecheck/<workflow_name>.py`. Define `AGENT =
   "pulsecheck_<workflow_name>"`, a list of `Check(name, fn, depends_on=[…])`,
   and a `main()` that calls `run_checks(AGENT, CHECKS)`.

2. Each check function returns a `CheckResult(status, detail,
   observed, threshold, meta)`. Always set `observed` and `threshold`
   when applicable — it lets you see *how close* to the line you are,
   not just whether you crossed it.

3. Add a job to `.github/workflows/pulsecheck.yml` mirroring the
   existing pattern, with `needs: foundation`.

4. Define checks **narrowly**. If the fact you're checking is "is
   Supabase up," don't — that's already `pulsecheck_foundation`'s
   job. Depend on it.

5. Calibrate thresholds against past incidents. A threshold pulled
   from intuition will produce alert fatigue; one calibrated to a real
   failure mode will produce signal.

## What this framework does NOT do (yet)

- **Telegram alerting.** Pulses land in Supabase. A daily digest agent
  reading `stock_health_pulse_recent_alerts` would close the loop, but
  hasn't been built. The current value is "I can query when something
  feels off" — proactive notification is a follow-up.
- **Coverage for the other 27 workflows.** Shipped: foundation, thesis,
  event_paper, realistic_loop, news. Remaining: ~27 workflows. Stamp
  them out as the failure modes become known — pure-config workflows
  like `tests.yml` may never need one.
- **Per-pulse cost accounting.** Each pulsecheck is 1 GHA job × 1
  minute compute. 5 pulsechecks × 24/day = ~120 GHA min/day. Within
  free tier on a public repo but worth watching if the count grows.

## Operational queries

Current health snapshot:

```bash
curl -s "${SUPABASE_URL}/rest/v1/stock_health_pulse_current?select=agent,check_name,status,detail,observed,threshold,age_seconds&order=agent,check_name" \
  -H "apikey: ${SUPABASE_SERVICE_KEY}" -H "Authorization: Bearer ${SUPABASE_SERVICE_KEY}"
```

Recent warnings/criticals:

```bash
curl -s "${SUPABASE_URL}/rest/v1/stock_health_pulse_recent_alerts" \
  -H "apikey: ${SUPABASE_SERVICE_KEY}" -H "Authorization: Bearer ${SUPABASE_SERVICE_KEY}"
```

History for one check:

```bash
curl -s "${SUPABASE_URL}/rest/v1/stock_health_pulse?agent=eq.pulsecheck_thesis&check_name=eq.candidate_dryness&order=pulsed_at.desc&limit=24" \
  -H "apikey: ${SUPABASE_SERVICE_KEY}" -H "Authorization: Bearer ${SUPABASE_SERVICE_KEY}"
```
