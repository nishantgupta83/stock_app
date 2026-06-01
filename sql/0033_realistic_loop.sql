-- 0033 — realistic shadow paper-trade loop ($5K bankroll, capital-deployed sizing).
--
-- Why a separate table family (not stock_event_paper_trades):
--   * Loop isolation memory: stock_rule_calibration is owned by event_paper_agent
--     only. The realistic loop must not feed calibration — its accounting model
--     (capped concurrency, capital-deployed sizing, single-horizon execution) is
--     deliberately different from the multi-horizon calibration replay.
--   * NOTIONAL not RISK sizing: per the project memory on $X/day budgets, this
--     loop sizes by capital deployed ($1,000 per position by default), not by
--     Van Tharp risk units. The latter would produce 10-50x larger positions.
--
-- Loop semantics (default loop_name = 'shadow_5k'):
--   * Total capital: $5,000 (capital_base).
--   * Max concurrent positions: 5 (max_concurrent).
--   * Per-position notional: $1,000 (per_position_size = capital_base / max_concurrent).
--   * Cash recycles: when a position closes, its notional returns to cash_available.
--     cumulative_pnl is tracked separately; capital_base does NOT grow with PnL.
--   * Input: stock_trade_setups where reason_to_skip IS NULL (i.e., the "tradeable"
--     subset that the pipeline already filtered). One position per setup.

create table if not exists stock_realistic_loop_positions (
  id                bigserial primary key,
  loop_name         text not null default 'shadow_5k',
  setup_id          bigint references stock_trade_setups(id) on delete set null,
  signal_id         bigint references stock_signals(id)      on delete set null,
  ticker            text not null,
  direction         text not null check (direction in ('long', 'short')),
  opened_at         timestamptz not null default now(),
  open_price        numeric(14,4) not null,
  notional          numeric(14,2) not null,
  shares            numeric(14,6) not null,
  target_pct        numeric(8,5) not null,
  stop_pct          numeric(8,5) not null,
  target_price      numeric(14,4) not null,
  stop_price        numeric(14,4) not null,
  horizon_days      integer not null,
  exit_target_date  date,
  valid_until       timestamptz,
  status            text not null default 'open' check (status in ('open', 'closed')),
  closed_at         timestamptz,
  close_price       numeric(14,4),
  close_reason      text check (close_reason in
                       ('target_hit', 'stop_hit', 'horizon_expired',
                        'valid_until_expired', 'force_close')),
  realized_pct      numeric(10,6),
  realized_pnl      numeric(14,4),
  mfe_pct           numeric(10,6),
  mae_pct           numeric(10,6),
  meta              jsonb not null default '{}'::jsonb,
  unique(loop_name, setup_id)
);

create index if not exists idx_realistic_loop_positions_open
  on stock_realistic_loop_positions (loop_name, status)
  where status = 'open';

create index if not exists idx_realistic_loop_positions_closed_recent
  on stock_realistic_loop_positions (loop_name, closed_at desc)
  where status = 'closed';

comment on table stock_realistic_loop_positions is
  'Realistic shadow paper-trade ledger. Capital-deployed sizing, capped '
  'concurrency, single-horizon execution. Isolated from stock_rule_calibration.';


-- Per-loop state (cash bookkeeping + cumulative PnL/drawdown tracking).
create table if not exists stock_realistic_loop_state (
  loop_name           text primary key,
  capital_base        numeric(14,2) not null,
  cash_available      numeric(14,2) not null,
  positions_open      integer not null default 0,
  max_concurrent      integer not null default 5,
  per_position_size   numeric(14,2) not null,
  cumulative_pnl      numeric(14,4) not null default 0,
  high_water_mark     numeric(14,4) not null default 0,
  max_drawdown        numeric(14,4) not null default 0,
  last_open_scan_at   timestamptz,
  last_mark_at        timestamptz,
  meta                jsonb not null default '{}'::jsonb,
  updated_at          timestamptz not null default now()
);

comment on table stock_realistic_loop_state is
  'Per-loop cash + PnL bookkeeping. One row per loop_name. Cash recycles on '
  'position close; cumulative_pnl tracked separately so capital_base stays static.';


-- Seed the default loop row (idempotent).
insert into stock_realistic_loop_state
  (loop_name, capital_base, cash_available, max_concurrent, per_position_size)
values
  ('shadow_5k', 5000, 5000, 5, 1000)
on conflict (loop_name) do nothing;


-- Read-only summary view (joins state + open-position aggregates).
create or replace view stock_realistic_loop_summary as
select
  s.loop_name,
  s.capital_base,
  s.cash_available,
  s.positions_open,
  s.max_concurrent,
  s.per_position_size,
  s.cumulative_pnl,
  s.high_water_mark,
  s.max_drawdown,
  case when s.capital_base > 0
       then round((s.cumulative_pnl / s.capital_base) * 100, 2)
       else null
  end as return_pct,
  (select count(*) from stock_realistic_loop_positions p
    where p.loop_name = s.loop_name and p.status = 'closed') as closed_count,
  (select count(*) from stock_realistic_loop_positions p
    where p.loop_name = s.loop_name and p.status = 'closed' and p.realized_pnl > 0) as wins,
  s.last_open_scan_at,
  s.last_mark_at,
  s.updated_at
from stock_realistic_loop_state s;

comment on view stock_realistic_loop_summary is
  'Operational summary for the realistic loop. Read-only.';
