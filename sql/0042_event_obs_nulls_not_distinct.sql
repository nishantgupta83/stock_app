-- 0042 — M6.3: collapse NULL-prior_event_id duplicates in
-- stock_event_outcome_observations + make the dedup index NULLS NOT DISTINCT.
--
-- market_scanner_agent inserts one observation per (ticker, observed_at). For
-- "no tracked event" rows prior_event_id is NULL. Postgres treats NULLs as
-- DISTINCT in a unique index by default (sql/0013), so the on_conflict
-- (ticker, observed_at, prior_event_id) never collapsed the NULL-prior rows —
-- every rerun re-inserted them. Measured: 369 NULL-prior rows, 86 duplicates.
--
-- PG17 supports NULLS NOT DISTINCT. We (1) dedup keeping the lowest id per
-- (ticker, observed_at) — touches ONLY NULL-prior rows, no FK references this
-- table — then (2) recreate the unique index with NULLS NOT DISTINCT so future
-- on_conflict upserts collapse NULL-prior re-inserts correctly. The on_conflict
-- columns are unchanged, so no agent code change is needed.

-- (1) dedup NULL-prior duplicates (keep lowest id per ticker+observed_at)
DELETE FROM stock_event_outcome_observations a
USING stock_event_outcome_observations b
WHERE a.prior_event_id IS NULL
  AND b.prior_event_id IS NULL
  AND a.ticker = b.ticker
  AND a.observed_at = b.observed_at
  AND a.id > b.id;

-- (2) recreate the unique index with NULLS NOT DISTINCT
DROP INDEX IF EXISTS stock_event_outcome_observations_uniq;
CREATE UNIQUE INDEX stock_event_outcome_observations_uniq
  ON stock_event_outcome_observations (ticker, observed_at, prior_event_id)
  NULLS NOT DISTINCT;
