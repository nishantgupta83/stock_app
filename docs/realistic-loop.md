# Realistic Loop — `$5K` shadow paper-trade portfolio

A capital-deployed, capped-concurrency paper-trade ledger that shadows
the live pipeline. The goal is one measurable answer: **"if I'd actually
traded every tradeable setup the pipeline emitted, with a static `$5K`
bankroll, what's my realized Sharpe / drawdown / hit-rate?"**

This is the design implementation reviewed before shipping; ongoing
operational notes live in `RUNBOOK.md`.

## Scope decisions (the four choices)

| Choice | Decision | Why |
|---|---|---|
| What is the loop *for*? | Realistic shadow portfolio — open every `reason_to_skip IS NULL` setup | Tells you the **deployed** pipeline's after-friction P&L, not a strategy in a vacuum |
| Signal source | `stock_trade_setups` (every `reason_to_skip IS NULL`) | The pipeline has already filtered. Trading every setup matches "what an alert subscriber would do." Setups also carry `direction/stop_pct/target_pct/horizon_days` so we don't re-derive |
| Position lifecycle | Stop/target-driven, falls back to horizon then `valid_until` | Matches `event_paper_agent` convention so behavior is comparable |
| `$5K/wk` semantics | **Static `$5K` bankroll, 5 concurrent positions of `$1,000` each, cash recycles on close, PnL tracked separately** | Closest to "what a solo trader with `$5K` to risk would actually do." The `$5K` is the **risk capital**, not a weekly top-up |

## What this is NOT

- **Not a calibration source.** Writes to `stock_realistic_loop_positions`, never to
  `stock_rule_calibration`. The canonical multi-horizon calibration replay
  is owned by `event_paper_agent` + `price_agent` and stays under the
  maturity-gate discipline.
- **Not Van Tharp risk-sized.** Positions are dollar-equal (`$1,000` each),
  not risk-equal. This matches the project memory on paper-trade
  budgeting (notional, not risk units).
- **Not for live execution.** Outputs feed `stock_realistic_loop_summary`
  for measurement only — no broker, no Telegram dispatch.

## Architecture

```
            ┌─────────────────────────────┐
            │  stock_trade_setups         │
            │  reason_to_skip IS NULL     │
            └──────────────┬──────────────┘
                           │ (every hour, "open" mode)
                           ▼
            ┌─────────────────────────────┐
            │  realistic_loop_agent.py    │
            │  --mode=open                │
            │   • cap at 5 concurrent     │
            │   • $1K each                │
            │   • entry price = latest    │
            │     stock_raw_prices close  │
            │   • +5 bps entry slippage   │
            └──────────────┬──────────────┘
                           ▼
            ┌─────────────────────────────┐
            │ stock_realistic_loop_       │
            │ positions  (status='open')  │
            └──────────────┬──────────────┘
                           │ (daily 21:30 UTC, "mark" mode)
                           ▼
            ┌─────────────────────────────┐
            │  realistic_loop_agent.py    │
            │  --mode=mark                │
            │   • walk stock_raw_prices   │
            │     from opened_at to now   │
            │   • detect target/stop/     │
            │     horizon/valid_until     │
            │   • -5 bps exit slippage    │
            │   • update state            │
            └──────────────┬──────────────┘
                           ▼
            ┌─────────────────────────────┐
            │ stock_realistic_loop_       │
            │ positions (status='closed') │
            │  realized_pct, realized_pnl │
            │  mfe_pct, mae_pct           │
            │                             │
            │ stock_realistic_loop_state  │
            │  cash, cumulative_pnl,      │
            │  high_water_mark, drawdown  │
            └──────────────┬──────────────┘
                           ▼
            ┌─────────────────────────────┐
            │  stock_realistic_loop_      │
            │  summary  (view)            │
            └─────────────────────────────┘
```

## Tables

`sql/0033_realistic_loop.sql` creates:

- `stock_realistic_loop_positions` — one row per opened position. Carries
  setup_id (FK), signal_id (FK), open/close prices, target/stop, MFE/MAE,
  close_reason. `unique(loop_name, setup_id)` prevents duplicate opens.
- `stock_realistic_loop_state` — one row per loop (default `shadow_5k`).
  Tracks cash, positions open, cumulative PnL, high-water mark, max
  drawdown. Seeded automatically.
- `stock_realistic_loop_summary` (view) — read-only aggregate for
  dashboards / quick queries.

## Agent (`agents/realistic_loop_agent.py`)

Three modes via `--mode`:

| Mode | Purpose |
|------|---------|
| `open` | Scan `stock_trade_setups` since `last_open_scan_at`, open positions up to `max_concurrent`, decrement `cash_available` |
| `mark` | For each open position, walk `stock_raw_prices` from `opened_at`, detect target/stop/horizon/valid_until, close + update state |
| `both` | Open then mark (default for `workflow_dispatch`) |

Key invariants:

- **Idempotent opens.** `unique(loop_name, setup_id)` plus
  `Prefer: resolution=ignore-duplicates` mean rerunning `open` won't
  double-trade.
- **Cold-start guard.** When `last_open_scan_at` is `NULL`, only setups
  from the last `LOOP_COLD_START_LOOKBACK_HOURS` (default 24) are
  considered — prevents retroactively trading the backlog.
- **Cash recycle, not refill.** `capital_base` stays at `$5,000`.
  `cumulative_pnl` accumulates separately. Closed-position notional
  returns to `cash_available`.
- **Slippage parity.** 5 bps per side (10 bps round-trip), same as
  `event_paper_agent` / `price_agent` so the two loops are comparable.

## Workflow (`.github/workflows/realistic_loop_agent.yml`)

- `*/15 * * * *`-style hourly `open` runs (cron `15 * * * *`).
- Daily `mark` at `30 21 * * *` UTC — ~30 min after US market close,
  bars are typically settled by then.
- `workflow_dispatch` supports manual `open|mark|both` for backfills /
  ops work.
- 5-minute timeout (well above measured worst case).
- Same `ops_recorder` start/finish bracket as other agents.

## Operational queries

Current state:

```bash
curl -s "${SUPABASE_URL}/rest/v1/stock_realistic_loop_summary" \
  -H "apikey: ${SUPABASE_SERVICE_KEY}" -H "Authorization: Bearer ${SUPABASE_SERVICE_KEY}"
```

Open positions:

```bash
curl -s "${SUPABASE_URL}/rest/v1/stock_realistic_loop_positions?status=eq.open&select=ticker,direction,opened_at,open_price,target_price,stop_price,exit_target_date" \
  -H "apikey: ${SUPABASE_SERVICE_KEY}" -H "Authorization: Bearer ${SUPABASE_SERVICE_KEY}"
```

Closed positions, last 7 days, with PnL:

```bash
curl -s "${SUPABASE_URL}/rest/v1/stock_realistic_loop_positions?status=eq.closed&closed_at=gte.$(date -u -v-7d +%Y-%m-%dT%H:%M:%SZ)&order=closed_at.desc&select=ticker,direction,opened_at,closed_at,close_reason,realized_pct,realized_pnl" \
  -H "apikey: ${SUPABASE_SERVICE_KEY}" -H "Authorization: Bearer ${SUPABASE_SERVICE_KEY}"
```

Force-reset state (operator action):

```bash
curl -s -X PATCH "${SUPABASE_URL}/rest/v1/stock_realistic_loop_state?loop_name=eq.shadow_5k" \
  -H "apikey: ${SUPABASE_SERVICE_KEY}" -H "Authorization: Bearer ${SUPABASE_SERVICE_KEY}" \
  -H "Content-Type: application/json" -H "Prefer: return=minimal" \
  -d '{"cash_available":5000,"positions_open":0,"cumulative_pnl":0,"high_water_mark":0,"max_drawdown":0,"last_open_scan_at":null,"last_mark_at":null}'
```

## What success looks like (review after 30 days)

1. **Volume** — closed positions per week. Expect ~5-15 once the pipeline
   stabilizes (matches setup volume).
2. **Hit rate** — wins / closed_count. Useful as a sanity check; not the
   primary metric.
3. **Cumulative PnL / capital_base** — annualized return is the headline.
4. **Max drawdown** — the discipline check. If drawdown > `$500` (10% of
   bankroll), the pipeline is not yet trustworthy for live capital.
5. **Sharpe** — daily PnL series, mean/stdev × √252. Only meaningful after
   ~40 closed positions.

## Future loops on the same infra

The schema keys everything by `loop_name`. To add an A/B test (e.g.,
`shadow_5k_no_mults` to measure the sector multiplier's effect):

```sql
insert into stock_realistic_loop_state
  (loop_name, capital_base, cash_available, max_concurrent, per_position_size)
values
  ('shadow_5k_no_mults', 5000, 5000, 5, 1000);
```

…and run the agent with `REALISTIC_LOOP_NAME=shadow_5k_no_mults`. The two
loops are fully isolated (separate state, separate ledger).
