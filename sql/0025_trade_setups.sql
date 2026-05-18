-- 0025_trade_setups.sql
--
-- New table for the trade-construction layer. A signal answers "is this
-- interesting?"; a trade setup answers "how would you actually enter this?".
-- The setup IS NOT a BUY/SELL recommendation — it's the tradable shape of
-- the proposal: entry style, entry reference price, stop logic, target logic,
-- and a reason-to-skip if any pre-flight check failed.
--
-- The risk_agent (Stage 6) reads stock_trade_setups and decides whether to
-- size capital. A setup with non-NULL reason_to_skip automatically becomes
-- decision='skip' at the risk layer.
--
-- Stage 5 of the trading-pipeline backlog.

create table if not exists stock_trade_setups (
  id                  bigserial primary key,
  signal_id           bigint      not null references stock_signals(id) on delete cascade,
  ticker              text        not null,
  direction           text        not null check (direction in ('long', 'short')),
  setup_type          text        not null check (setup_type in (
    'next_open', 'limit_pullback', 'breakout', 'vwap_band', 'manual_skip'
  )),
  entry_reference     text,
  entry_ref_price     numeric,
  stop_pct            numeric,
  target_pct          numeric,
  horizon_days        integer,
  valid_until         timestamptz,
  confidence          numeric,
  reason_to_skip      text,
  rule_key            text,
  created_at          timestamptz not null default now(),
  unique (signal_id)
);

create index if not exists stock_trade_setups_ticker_created_idx
  on stock_trade_setups (ticker, created_at desc);

create index if not exists stock_trade_setups_valid_idx
  on stock_trade_setups (valid_until)
  where valid_until is not null;

create index if not exists stock_trade_setups_actionable_idx
  on stock_trade_setups (created_at desc)
  where reason_to_skip is null;

alter table stock_trade_setups enable row level security;

comment on table stock_trade_setups is
  'Trade-construction layer: each row is a tradable proposal derived from a stock_signals row. Layer boundary: this table is written by trade_setup_agent only; risk_agent reads from here and writes to stock_risk_decisions.';
