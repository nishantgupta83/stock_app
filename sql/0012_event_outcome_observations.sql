-- Phase 4: market_scanner_agent observations.
-- Daily after market close, the scanner records each significant price move
-- (|daily return| >= threshold) joined with prior-1-2-day normalized events.
-- This data lets us aggregate "what kinds of events most reliably preceded
-- a big move?" — feeds future calibration of thesis_agent's per-event weights.
--
-- This is observation-only: writing here never changes scoring. The pipeline
-- decides if/when to consume aggregates. Keeps the loop auditable.

create table if not exists stock_event_outcome_observations (
  id                       bigserial primary key,
  observed_at              timestamptz not null,
  ticker                   text not null,
  daily_return_pct         numeric not null,                -- e.g. 0.0381 for +3.81%
  abs_return_pct           numeric generated always as (abs(daily_return_pct)) stored,
  prior_event_id           bigint  references stock_normalized_events(id) on delete set null,
  prior_event_type         text    not null,
  prior_event_subtype      text,
  prior_event_severity     smallint,
  prior_event_age_hours    numeric not null,                -- positive = event happened before observation
  source                   text default 'market_scanner',
  notes                    text
);

-- Idempotency: re-running the scanner for the same day shouldn't create dup rows.
-- (ticker, observed_at, prior_event_id) is unique. NULL prior_event_id is allowed
-- repeatedly only when the event has been deleted; Postgres treats NULL != NULL,
-- which is fine for our use.
create unique index if not exists stock_event_outcome_observations_uniq
  on stock_event_outcome_observations (ticker, observed_at, prior_event_id)
  where prior_event_id is not null;

create index if not exists stock_event_outcome_observations_type_idx
  on stock_event_outcome_observations (prior_event_type, observed_at desc);

create index if not exists stock_event_outcome_observations_ticker_idx
  on stock_event_outcome_observations (ticker, observed_at desc);

alter table stock_event_outcome_observations enable row level security;
