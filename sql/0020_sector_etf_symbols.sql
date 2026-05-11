-- Add missing sector ETFs to stock_symbols + stock_watchlists.
-- truth_social_agent fires tariff/macro posts with XLY, XLB, XLC, XLP, XLV,
-- XLU, XLRE tickers. These appeared in stock_watchlists.context (added manually)
-- but were absent from stock_symbols, so fetch_tradeable_kinds() JOIN silently
-- dropped them and no paper trades were ever opened for those events.

insert into stock_symbols (ticker, cik, name, sector, kind, is_etf) values
  ('XLY',  null, 'Consumer Discretionary Select Sector SPDR', 'Consumer Discretionary', 'etf', true),
  ('XLB',  null, 'Materials Select Sector SPDR Fund',         'Materials',              'etf', true),
  ('XLC',  null, 'Communication Services Select Sector SPDR', 'Communication Services', 'etf', true),
  ('XLP',  null, 'Consumer Staples Select Sector SPDR',       'Consumer Staples',       'etf', true),
  ('XLV',  null, 'Health Care Select Sector SPDR Fund',       'Health Care',            'etf', true),
  ('XLU',  null, 'Utilities Select Sector SPDR Fund',         'Utilities',              'etf', true),
  ('XLRE', null, 'Real Estate Select Sector SPDR Fund',       'Real Estate',            'etf', true)
on conflict (ticker) do update set kind = excluded.kind, is_etf = excluded.is_etf;

insert into stock_watchlists (name, ticker, weight)
values
  ('context', 'XLY',  0.5),
  ('context', 'XLB',  0.5),
  ('context', 'XLC',  0.5),
  ('context', 'XLP',  0.5),
  ('context', 'XLV',  0.5),
  ('context', 'XLU',  0.5),
  ('context', 'XLRE', 0.5)
on conflict (name, ticker) do nothing;
