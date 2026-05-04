-- Phase 4 follow-up: Postgres rejects ON CONFLICT against a PARTIAL unique
-- index ("42P10: no unique or exclusion constraint matching the ON CONFLICT
-- specification"). market_scanner_agent's bulk insert needs a regular unique
-- index. Postgres treats NULLs as DISTINCT by default in unique indexes,
-- so the no_tracked_event rows (prior_event_id NULL) still get to coexist
-- under the same (ticker, observed_at).

drop index if exists stock_event_outcome_observations_uniq;

create unique index if not exists stock_event_outcome_observations_uniq
  on stock_event_outcome_observations (ticker, observed_at, prior_event_id);
