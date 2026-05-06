-- Phase 8 follow-up — three things in one short migration:
--
-- 1. INTC + EBAY join the core watchlist. Direct ask from the user after the
--    May 5 semi jump (Intel news landed in our feed but couldn't surface as
--    its own ticker page) and the eBay M&A speculation last week.
--    CIKs validated against SEC EDGAR:
--      INTC = 0000050863  (Intel Corporation, since 1979)
--      EBAY = 0001065088  (eBay Inc., since 1998)
--
-- 2. Re-extend the unique index on stock_event_paper_trades so multi-horizon
--    paper trades coexist (one event → four trades at h=1d, 7d, 15d, 30d
--    with the same direction). Without horizon_days in the unique key, the
--    second through fourth trades collapse on conflict.
--
-- 3. (Implicit) thesis_agent CLUSTER_WINDOW_MIN widening from 5→30 happens in
--    the Python — listed here so the schema review reads as one consistent
--    change set.

-- ============================================================
-- 1. Add INTC + EBAY to symbols + core watchlist
-- ============================================================
-- Column order matches 0003's INSERT pattern: (ticker, cik, name, sector, kind, is_etf)
insert into stock_symbols (ticker, cik, name, sector, kind, is_etf) values
  ('INTC', '0000050863', 'Intel Corporation',          'Information Technology', 'stock', false),
  ('EBAY', '0001065088', 'eBay Inc.',                  'Consumer Discretionary', 'stock', false)
on conflict (ticker) do nothing;

insert into stock_watchlists (name, ticker, weight) values
  ('core', 'INTC', 1.0),
  ('core', 'EBAY', 1.0)
on conflict do nothing;

-- ============================================================
-- 2. Horizon-aware unique index for multi-horizon paper trades
-- ============================================================
drop index if exists stock_event_paper_trades_uniq;

create unique index if not exists stock_event_paper_trades_uniq
  on stock_event_paper_trades (event_id, ticker, direction, horizon_days);
