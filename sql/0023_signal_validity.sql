-- 0023_signal_validity.sql
--
-- Adds valid_until to stock_signals. Implements the "alpha decay TTL"
-- gap surfaced by the validation review: a 5-day-old activist filing
-- has different decay than a 30-minute-old news article. Telegram and
-- the dashboard can filter / fade signals past valid_until.
--
-- Stage 2 of the trading-pipeline backlog. Column is nullable so
-- pre-migration signals remain valid (NULL = never expires under any
-- downstream filter).

alter table stock_signals
  add column if not exists valid_until timestamptz;

-- Index for the common "give me signals still valid right now" filter.
-- Partial: only signals with a TTL are indexed, since legacy NULL rows
-- always pass any downstream "valid_until > now()" check.
create index if not exists stock_signals_valid_until_idx
  on stock_signals (valid_until)
  where valid_until is not null;

comment on column stock_signals.valid_until is
  'Alpha-decay expiry. Beyond this timestamp the signal is considered stale for trading/display purposes. NULL = no expiry set (legacy or pre-decay-config signals).';
