-- Phase 9: Tiered storage — add archived_at columns and indexes to enable safe deletion
--
-- The project now exports old rows to FTPS archive before deleting from Supabase Free.
-- To make deletion safe (delete only after confirmed upload), each archivable table
-- gets an archived_at timestamptz column. A per-table composite index on (archived_at, age_col DESC)
-- makes the weekly archive scan fast. Since archived rows are deleted from the active tier after
-- upload, the index stays small long-term even without a WHERE clause.
--
-- All 6 tables already have RLS enabled from their creation migrations.
-- This migration only adds the column and the index.

-- ============================================================
-- stock_normalized_events (age column: created_at)
-- ============================================================
alter table stock_normalized_events
  add column if not exists archived_at timestamptz;

create index if not exists stock_normalized_events_archive_idx
  on stock_normalized_events (archived_at, created_at desc);

-- ============================================================
-- stock_event_paper_trades (age column: exit_at)
-- ============================================================
alter table stock_event_paper_trades
  add column if not exists archived_at timestamptz;

create index if not exists stock_event_paper_trades_archive_idx
  on stock_event_paper_trades (archived_at, exit_at desc);

-- ============================================================
-- stock_signals (age column: fired_at)
-- ============================================================
alter table stock_signals
  add column if not exists archived_at timestamptz;

create index if not exists stock_signals_archive_idx
  on stock_signals (archived_at, fired_at desc);

-- ============================================================
-- stock_raw_prices (age column: ts)
-- ============================================================
alter table stock_raw_prices
  add column if not exists archived_at timestamptz;

create index if not exists stock_raw_prices_archive_idx
  on stock_raw_prices (archived_at, ts desc);

-- ============================================================
-- stock_raw_filings (age column: filed_at)
-- ============================================================
alter table stock_raw_filings
  add column if not exists archived_at timestamptz;

create index if not exists stock_raw_filings_archive_idx
  on stock_raw_filings (archived_at, filed_at desc);

-- ============================================================
-- stock_institutional_holdings_snapshot (age column: filed_at)
-- ============================================================
alter table stock_institutional_holdings_snapshot
  add column if not exists archived_at timestamptz;

create index if not exists stock_institutional_holdings_snapshot_archive_idx
  on stock_institutional_holdings_snapshot (archived_at, filed_at desc);
