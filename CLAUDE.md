# stock_app — Claude project context

Real-time multi-source market intelligence pipeline. 14 GitHub Actions agents → Supabase →
Telegram. Paper-trading until rule maturity (>=90% accuracy, n>=30) unlocks BUY/SELL.
See `README.md` for the full surface; this file is the fast-load context.

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

```
Inputs:      EDGAR (filing) · RSS (news) · Truth Social · yfinance (price/earnings) · 13F-HR
   ↓
Ingest:      filing_agent · news_agent · truth_social_agent · earnings_agent · crypto_macro_agent · flows_agent
   ↓
Event bus:   stock_normalized_events  ← every agent writes here
   ↓
Score:       thesis_agent (5-min cluster, 100-pt rubric, intelligence layer bonuses)
   ↓
Paper trade: event_paper_agent (4 horizons/event: 1d/7d/15d/30d)
   ↓
Reconcile:   price_agent (EOD close, EMA weight update, rule calibration)
   ↓
Notify:      Telegram (thesis_agent dispatch + intraday_alert_agent spikes)
   ↓
Surface:     site_generator → Hostinger FTPS → hub4apps.com/stock_app/
```

**Key tables:**
- `stock_normalized_events` — universal event bus (all agents write)
- `stock_event_paper_trades` — open trades (4 horizons each)
- `stock_rule_calibration` — per-rule accuracy (maturity gate)
- `stock_signals` — fired signals (Telegram alerts)
- `stock_agent_weights` — per-agent EMA learned weights
- `stock_watchlists` — categorized ticker baskets (`core`, `context`, `ai_compute`, `ai_optical`, …)

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

## Hooks active in this repo

`.claude/hookify.*.local.md` — three rules that prevent the bug classes above. Review them
if you see a "⚠️" warning fire during tool use.
