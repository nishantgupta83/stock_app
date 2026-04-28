-- Add explicit `kind` to stock_symbols so the pipeline can branch behavior
-- (stock vs etf vs mutual_fund vs institution) without overloading is_etf.

alter table stock_symbols
  add column if not exists kind text not null default 'stock'
    check (kind in ('stock','etf','mutual_fund','institution','index'));

-- Backfill from existing rows
update stock_symbols set kind = 'etf'   where is_etf = true and ticker not in ('VIX');
update stock_symbols set kind = 'index' where ticker = 'VIX';

-- ============================================================
-- Major mutual funds (passive index funds first — most-watched flows)
-- These have CIKs but use them as the *fund family series* CIK.
-- For Phase 0 we just register them; N-PORT parsing comes in Phase 1.
-- ============================================================
insert into stock_symbols (ticker, cik, name, sector, kind, is_etf) values
  ('FXAIX', '0000035315', 'Fidelity 500 Index Fund',         'Mutual Fund', 'mutual_fund', false),
  ('VTSAX', '0000036405', 'Vanguard Total Stock Market Idx', 'Mutual Fund', 'mutual_fund', false),
  ('VFIAX', '0000036405', 'Vanguard 500 Index Admiral',      'Mutual Fund', 'mutual_fund', false),
  ('SWPPX', '0000934647', 'Schwab S&P 500 Index Fund',       'Mutual Fund', 'mutual_fund', false)
on conflict (ticker) do update set kind = excluded.kind;

-- ============================================================
-- Major institutional 13F filers (positions disclose quarterly).
-- ticker col reused as a label (not tradable). CIKs drive EDGAR polling.
-- ============================================================
insert into stock_symbols (ticker, cik, name, sector, kind, is_etf) values
  ('INST_BRK',   '0001067983', 'Berkshire Hathaway (13F)',         'Institutional', 'institution', false),
  ('INST_BLK',   '0001364742', 'BlackRock Inc. (13F)',             'Institutional', 'institution', false),
  ('INST_VG',    '0000102909', 'Vanguard Group (13F)',             'Institutional', 'institution', false),
  ('INST_BRDGW', '0001350694', 'Bridgewater Associates (13F)',     'Institutional', 'institution', false),
  ('INST_SCION', '0001649339', 'Scion Asset Management (13F)',     'Institutional', 'institution', false),
  ('INST_PERSH', '0001336528', 'Pershing Square Capital (13F)',    'Institutional', 'institution', false)
on conflict (ticker) do update set kind = excluded.kind;

-- ============================================================
-- Watchlists for the new categories
-- ============================================================
insert into stock_watchlists (name, ticker, weight)
select 'mutual_funds', ticker, 0.3 from stock_symbols where kind = 'mutual_fund'
on conflict (name, ticker) do nothing;

insert into stock_watchlists (name, ticker, weight)
select 'institutions', ticker, 0.5 from stock_symbols where kind = 'institution'
on conflict (name, ticker) do nothing;
