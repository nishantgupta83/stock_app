-- Phase 6A: probability-calibrated paper forecasts.
--
-- `stock_paper_trades` remains the manual Telegram/user-response journal.
-- This table stores model-generated paper-trade forecasts: probability,
-- expected value, risk/reward, entry/exit levels, and eventual outcome.

create table if not exists stock_paper_forecasts (
  id                  bigserial primary key,
  signal_id           bigint not null references stock_signals(id) on delete cascade,
  ticker              text not null,
  created_at          timestamptz not null default now(),
  updated_at          timestamptz not null default now(),
  fired_at            timestamptz not null,

  horizon_days        smallint not null default 1,
  direction           text not null
    check (direction in ('bullish', 'bearish', 'neutral')),
  source_action       text not null,
  paper_action        text not null
    check (paper_action in (
      'PAPER_LONG',
      'PAPER_SHORT',
      'PAPER_WATCH',
      'PAPER_AVOID',
      'PAPER_CHASE_RISK',
      'NO_TRADE'
    )),

  prob_win            numeric not null check (prob_win >= 0 and prob_win <= 1),
  base_rate           numeric check (base_rate is null or (base_rate >= 0 and base_rate <= 1)),
  setup_hit_rate      numeric check (setup_hit_rate is null or (setup_hit_rate >= 0 and setup_hit_rate <= 1)),
  sample_size         integer not null default 0,
  score_bucket        text,

  avg_win             numeric,
  avg_loss            numeric,
  expected_value      numeric,
  risk_reward         numeric,

  entry_price         numeric,
  target_price        numeric,
  stop_price          numeric,

  status              text not null default 'open'
    check (status in ('open', 'closed', 'expired', 'skipped')),
  exit_price          numeric,
  realized_return     numeric,
  realized_at         timestamptz,
  correct             boolean,

  features_json       jsonb not null default '{}'::jsonb,
  calibration_method  text not null default 'empirical_shrinkage_v1',
  reason_summary      text,
  dedupe_key          text not null,

  unique (dedupe_key)
);

create index if not exists stock_paper_forecasts_signal_idx
  on stock_paper_forecasts (signal_id);

create index if not exists stock_paper_forecasts_ticker_created_idx
  on stock_paper_forecasts (ticker, created_at desc);

create index if not exists stock_paper_forecasts_status_idx
  on stock_paper_forecasts (status, created_at desc);

create index if not exists stock_paper_forecasts_action_idx
  on stock_paper_forecasts (paper_action, created_at desc);

alter table stock_paper_forecasts enable row level security;
