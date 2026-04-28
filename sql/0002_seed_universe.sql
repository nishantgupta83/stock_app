-- Seed the v1 universe (§20 of design doc).
-- CIKs from https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany
-- Format: zero-padded 10 chars, used directly in EDGAR URLs.

insert into stock_symbols (ticker, cik, name, sector, is_etf) values
  ('NVDA',  '0001045810', 'NVIDIA Corporation',           'Information Technology', false),
  ('AAPL',  '0000320193', 'Apple Inc.',                   'Information Technology', false),
  ('MSFT',  '0000789019', 'Microsoft Corporation',        'Information Technology', false),
  ('AMZN',  '0001018724', 'Amazon.com, Inc.',             'Consumer Discretionary', false),
  ('AVGO',  '0001730168', 'Broadcom Inc.',                'Information Technology', false),
  ('GOOGL', '0001652044', 'Alphabet Inc. Class A',        'Communication Services', false),
  ('GOOG',  '0001652044', 'Alphabet Inc. Class C',        'Communication Services', false),
  ('META',  '0001326801', 'Meta Platforms, Inc.',         'Communication Services', false),
  ('TSLA',  '0001318605', 'Tesla, Inc.',                  'Consumer Discretionary', false),
  ('BRK.B', '0001067983', 'Berkshire Hathaway Inc.',      'Financials',             false),
  ('JPM',   '0000019617', 'JPMorgan Chase & Co.',         'Financials',             false),
  ('LLY',   '0000059478', 'Eli Lilly and Company',        'Health Care',            false),
  ('XOM',   '0000034088', 'Exxon Mobil Corporation',      'Energy',                 false),
  ('JNJ',   '0000200406', 'Johnson & Johnson',            'Health Care',            false),
  ('WMT',   '0000104169', 'Walmart Inc.',                 'Consumer Staples',       false),
  ('V',     '0001403161', 'Visa Inc.',                    'Financials',             false),
  ('NFLX',  '0001065280', 'Netflix, Inc.',                'Communication Services', false),
  ('COST',  '0000909832', 'Costco Wholesale Corporation', 'Consumer Staples',       false),
  ('MA',    '0001141391', 'Mastercard Incorporated',      'Financials',             false),
  ('AMD',   '0000002488', 'Advanced Micro Devices, Inc.', 'Information Technology', false),
  -- Trump-related ticker
  ('DJT',   '0001849056', 'Trump Media & Technology Group', 'Communication Services', false),
  -- Context ETFs (no CIK needed for filings; included for price/regime tracking)
  ('SPY',   null, 'SPDR S&P 500 ETF Trust',          'ETF',  true),
  ('QQQ',   null, 'Invesco QQQ Trust',                'ETF',  true),
  ('XLK',   null, 'Technology Select Sector SPDR',    'ETF',  true),
  ('XLF',   null, 'Financial Select Sector SPDR',     'ETF',  true),
  ('XLE',   null, 'Energy Select Sector SPDR',        'ETF',  true),
  ('XLI',   null, 'Industrial Select Sector SPDR',    'ETF',  true),
  ('TLT',   null, '20+ Year Treasury Bond ETF',       'ETF',  true),
  ('VIX',   null, 'CBOE Volatility Index',            'Index',true),
  ('FXI',   null, 'iShares China Large-Cap ETF',      'ETF',  true),
  ('COIN',  '0001679788', 'Coinbase Global, Inc.',    'Financials',             false),
  ('MSTR',  '0001050446', 'MicroStrategy Incorporated','Information Technology', false)
on conflict (ticker) do nothing;

insert into stock_watchlists (name, ticker, weight)
select 'core', ticker, 1.0 from stock_symbols
where ticker in ('NVDA','AAPL','MSFT','AMZN','AVGO','GOOGL','GOOG','META','TSLA','BRK.B',
                 'JPM','LLY','XOM','JNJ','WMT','V','NFLX','COST','MA','AMD','DJT')
on conflict (name, ticker) do nothing;

insert into stock_watchlists (name, ticker, weight)
select 'context', ticker, 0.5 from stock_symbols
where ticker in ('SPY','QQQ','XLK','XLF','XLE','XLI','TLT','VIX','FXI','COIN','MSTR')
on conflict (name, ticker) do nothing;
