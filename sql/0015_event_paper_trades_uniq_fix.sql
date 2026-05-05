-- Phase 7 follow-up: same lesson as sql/0013. Postgres rejects ON CONFLICT
-- against PARTIAL unique indexes ("42P10: no unique or exclusion constraint
-- matching the ON CONFLICT specification"). The original
-- stock_event_paper_trades_uniq from 0014 had WHERE event_id IS NOT NULL,
-- which broke event_paper_agent's bulk upsert.
--
-- NULLS-DISTINCT default means NULL event_id rows still don't collide with
-- each other, so we can drop the partial predicate without losing the
-- "manual paper trade with no source event" use case.

drop index if exists stock_event_paper_trades_uniq;

create unique index if not exists stock_event_paper_trades_uniq
  on stock_event_paper_trades (event_id, ticker, direction);
