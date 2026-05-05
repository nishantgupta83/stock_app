-- Phase 7 — closed learning loop:
--
--   event lands → event_paper_agent opens a paper trade (next-session-open or
--   current close as entry depending on what's available) → price_agent EOD
--   reconciles outcome → updates stock_rule_calibration (per event_type
--   accuracy + sample size) → thesis_agent reads calibration on every run and
--   applies learned weight per rule. When a rule's accuracy crosses 0.90 with
--   ≥ MATURITY_MIN_N observations, is_mature flips true and clusters anchored
--   on it can fire BUY/SELL instead of WATCH/AVOID_CHASE.
--
-- All paper-only. Real BUY/SELL semantics are still subject to the existing
-- §17.6 graduation gate at the human/portfolio level — this just unlocks the
-- vocabulary the bot is allowed to use when its rules have empirically earned it.

-- ============================================================
-- 1. Per-event paper trades (one row per significant event)
-- ============================================================
create table if not exists stock_event_paper_trades (
  id                  bigserial primary key,
  event_id            bigint references stock_normalized_events(id) on delete set null,
  event_type          text not null,
  event_subtype       text,
  ticker              text not null,
  vehicle_type        text not null default 'stock'
                      check (vehicle_type in ('stock','etf','mutual_fund','index')),
  direction           text not null check (direction in ('long','short')),
  entry_at            timestamptz not null,            -- bar timestamp the entry references
  entry_price         numeric not null,
  horizon_days        smallint not null default 1,     -- exit at entry_at + horizon_days session close
  target_pct          numeric default 0.05,            -- display only; not enforced intraday yet
  stop_pct            numeric default 0.03,
  exit_at             timestamptz,
  exit_price          numeric,
  realized_return     numeric,                         -- direction-aware: positive when correct
  correct             boolean,                         -- realized_return > 0 (long) or < 0 (short)
  status              text not null default 'open'
                      check (status in ('open','closed','expired','skipped')),
  rule_key            text not null,                   -- matches stock_rule_calibration.rule_key
  notes               text,
  created_at          timestamptz default now()
);

create index if not exists stock_event_paper_trades_open_idx
  on stock_event_paper_trades (status, entry_at desc);
create index if not exists stock_event_paper_trades_rule_idx
  on stock_event_paper_trades (rule_key, status);
create index if not exists stock_event_paper_trades_ticker_idx
  on stock_event_paper_trades (ticker, entry_at desc);

-- One open trade per (event, ticker, direction) — re-runs collapse safely.
create unique index if not exists stock_event_paper_trades_uniq
  on stock_event_paper_trades (event_id, ticker, direction)
  where event_id is not null;

alter table stock_event_paper_trades enable row level security;

-- ============================================================
-- 2. Per-rule calibration (running tally of paper-trade accuracy per event_type)
-- ============================================================
create table if not exists stock_rule_calibration (
  id                  bigserial primary key,
  rule_key            text not null unique,            -- "event_type" or "event_type:subtype"
  n_observations      integer not null default 0,
  n_correct           integer not null default 0,
  accuracy            numeric not null default 0.5,    -- updated by reconciler, NOT a generated col
                                                       -- (Postgres generated cols can't divide and
                                                       --  guard for n_observations=0 in one expr)
  mean_realized_pct   numeric,                          -- avg realized return when this rule fires
  is_mature           boolean not null default false,   -- accuracy >= 0.90 AND n_observations >= 30
  matured_at          timestamptz,
  last_updated        timestamptz default now(),
  notes               text
);

create index if not exists stock_rule_calibration_mature_idx
  on stock_rule_calibration (is_mature, accuracy desc);

alter table stock_rule_calibration enable row level security;

-- ============================================================
-- 3. Allow BUY / SELL actions in stock_signals (graduated vocabulary)
-- ============================================================
alter table stock_signals drop constraint if exists stock_signals_action_check;
alter table stock_signals add constraint stock_signals_action_check
  check (action in ('WATCH','RESEARCH','AVOID_CHASE','CHASE_RISK','BUY','SELL'));
