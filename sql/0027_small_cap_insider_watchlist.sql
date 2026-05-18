-- 0027_small_cap_insider_watchlist.sql
--
-- Starter small-cap watchlist for the "insider buying near 52w low" edge
-- (Cohen/Lou 2012; Jeng/Metrick/Zeckhauser 2003). Strategic feedback flagged
-- this as the retail trader's structural edge: small-cap firms with high
-- information asymmetry give outsized abnormal returns to insider cluster
-- buys, especially near multi-year lows.
--
-- These 8 names span sectors (regional bank, biotech, specialty industrials,
-- consumer cyclical, energy) and all have history of meaningful Form 4
-- activity. The list is starter material — curate further once enough
-- closed paper trades exist on the small_cap_insider watchlist to evaluate
-- which sub-sectors actually carry the insider edge.
--
-- Stage 8 (post-stages addition). Not part of the original 7-stage backlog;
-- shipped 2026-05-18 alongside the activist_insider 52w-low escalation.

-- 1. Add symbols (idempotent — skips on conflict)
insert into stock_symbols (ticker, name, sector, kind, is_etf, is_active) values
  ('HMST', 'HomeStreet Inc',         'Financials',           'stock', false, true),
  ('SAVA', 'Cassava Sciences Inc',   'Health Care',          'stock', false, true),
  ('AGX',  'Argan Inc',              'Industrials',          'stock', false, true),
  ('BJRI', 'BJ''s Restaurants Inc',  'Consumer Cyclical',    'stock', false, true),
  ('AROC', 'Archrock Inc',           'Energy',               'stock', false, true),
  ('KRYS', 'Krystal Biotech Inc',    'Health Care',          'stock', false, true),
  ('BOOT', 'Boot Barn Holdings Inc', 'Consumer Cyclical',    'stock', false, true),
  ('IMVT', 'Immunovant Inc',         'Health Care',          'stock', false, true)
on conflict (ticker) do nothing;

-- 2. Add to small_cap_insider watchlist (idempotent — drop+reinsert)
delete from stock_watchlists where name = 'small_cap_insider';
insert into stock_watchlists (name, ticker, weight) values
  ('small_cap_insider', 'HMST', 1.0),
  ('small_cap_insider', 'SAVA', 1.0),
  ('small_cap_insider', 'AGX',  1.0),
  ('small_cap_insider', 'BJRI', 1.0),
  ('small_cap_insider', 'AROC', 1.0),
  ('small_cap_insider', 'KRYS', 1.0),
  ('small_cap_insider', 'BOOT', 1.0),
  ('small_cap_insider', 'IMVT', 1.0);
