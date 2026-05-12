-- Phase 10 perf prep: index hot-path filters added after the May 2026
-- event_at → created_at fix and the multi-domain expansion plan.
--
-- Why these specific indexes?
--   event_paper_agent.fetch_recent_events filters by created_at + severity
--   thesis_agent.fetch_recent_events_window filters by created_at (intelligence layer)
--   intraday_alert_agent.recent_events_context_batch filters ticker IN (...) + event_at
--
-- These were CPU-cheap at 33K rows but will degrade at ~300K+ rows when six
-- new domain agents (defense / biotech / energy / macro / activist / consumer)
-- start writing. CREATE INDEX CONCURRENTLY runs without an ACCESS EXCLUSIVE
-- lock so live traffic isn't blocked.

create index concurrently if not exists stock_normalized_events_created_at_idx
  on stock_normalized_events (created_at desc);

-- Composite for the event_paper_agent hot path (created_at + severity + ticker not null)
-- Postgres uses leading column + can range-scan severity within the same plan.
create index concurrently if not exists stock_normalized_events_freshness_idx
  on stock_normalized_events (created_at desc, severity)
  where ticker is not null;

-- intraday_alert_agent recent_events_context_batch uses ticker IN (...) + event_at gte
-- — keep the existing (ticker, event_at) index but ensure DESC for our query.
create index concurrently if not exists stock_normalized_events_ticker_eventat_idx
  on stock_normalized_events (ticker, event_at desc);

-- stock_signals dedupe_key lookups (intraday_alert_agent already_alerted_today_batch)
-- and the hot dispatch path. dedupe_key already has unique constraint from 0001.
-- Adding a partial index for active candidate signals to speed retry_dispatch_failed.
create index concurrently if not exists stock_signals_dispatch_failed_idx
  on stock_signals (fired_at)
  where status_v2 = 'dispatch_failed';

-- stock_event_paper_trades — fetch_already_traded_event_ids uses event_id IN (...).
-- event_id is the leading column of the current unique index (event_id, ticker,
-- direction, horizon_days) from sql/0018, which supports our IN lookups by
-- leading-column scan. No new index needed.
