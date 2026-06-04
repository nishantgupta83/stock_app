-- 0038 — round 2 truth_social expansion + water/uranium watchlist additions
--
-- Three coherent additions in one migration:
--
-- A. 6 new truth_social keyword rules surfaced by the 2026-06-04 pattern
--    sweep (docs/findings/2026-06-04_truth_social_pattern_sweep.md):
--      CEO aliases (Cook → AAPL, Pichai → GOOGL, Zuckerberg → META,
--      Dimon → JPM) + Powell → TLT/XLF + UAW/Teamsters → GM/F short.
--
-- B. 6 missing tickers added to stock_symbols (so they can become events
--    + paper trades):
--      LEU (Centrus Energy)        — uranium fuel pure-play
--      UEC (Uranium Energy Corp)   — uranium miner
--      URA (Global X Uranium ETF)  — uranium ETF
--      WTRG (Essential Utilities)  — water utility
--      XYL (Xylem)                 — water technology + data-center cooling
--      AWK (American Water Works)  — water utility
--
-- C. Two new watchlist categories so the dashboard + thesis_agent
--    intelligence layer can track them:
--      uranium_pureplay : LEU, UEC, URA, CCJ
--      water_datacenter : WTRG, XYL, AWK, VRT, ETN, GEV
--    (VRT/ETN/GEV are already in ai_power but their thesis is genuinely
--    dual-purpose — data-center cooling + power. Cross-listing is
--    informational; sector_cluster_bonus dedupes by ticker.)
--
-- Why ship these together: they all serve the same user-stated goal —
-- "make sure we have an eye on AI, GPU, and the whole pipeline including
-- nuclear energy, water datacenter." The keyword rules + watchlist
-- additions are the L1 + intelligence-layer coverage for that goal.
--
-- Idempotent. Reversal: rule_label LIKE 'kwd0038_%' for the keyword rules.

-- A. Keyword rules
insert into stock_keyword_rules (kind, enabled, keyword, match_type, direction_prior, tickers, rule_label, notes) values
  ('truth_social', true, 'tim cook',         'icontains', 'neutral', '{AAPL}',     'kwd0038_ceo_cook',   '0038: Tim Cook → AAPL'),
  ('truth_social', true, 'sundar pichai',    'icontains', 'neutral', '{GOOGL}',    'kwd0038_ceo_pichai', '0038: Sundar Pichai → GOOGL'),
  ('truth_social', true, 'zuckerberg',       'icontains', 'neutral', '{META}',     'kwd0038_ceo_zuck',   '0038: Zuckerberg → META'),
  ('truth_social', true, 'jamie dimon',      'icontains', 'neutral', '{JPM}',      'kwd0038_ceo_dimon',  '0038: Jamie Dimon → JPM'),
  ('truth_social', true, '\b(jerome\s+powell|chair\s+powell|powell\b)', 'regex', 'long', '{TLT,XLF}', 'kwd0038_powell_fed', '0038: Powell mentions → bond/fin sentiment'),
  ('truth_social', true, '\b(uaw|teamsters|auto\s+strike)',             'regex', 'short', '{GM,F}',   'kwd0038_union_auto', '0038: union threat → GM/F short');

-- B. Missing tickers added to stock_symbols (with sector + active flags
--    so thesis_agent's sector_cluster_bonus + sector multiplier can use them)
insert into stock_symbols (ticker, name, sector, is_etf, is_active, kind) values
  ('LEU',  'Centrus Energy Corp',           'Energy',    false, true, 'stock'),
  ('UEC',  'Uranium Energy Corp',           'Energy',    false, true, 'stock'),
  ('URA',  'Global X Uranium ETF',          'Energy',    true,  true, 'etf'),
  ('WTRG', 'Essential Utilities Inc',       'Utilities', false, true, 'stock'),
  ('XYL',  'Xylem Inc',                     'Industrials', false, true, 'stock'),
  ('AWK',  'American Water Works Co Inc',   'Utilities', false, true, 'stock')
on conflict (ticker) do nothing;

-- C. Watchlists — uranium pure-play + water/datacenter cooling
insert into stock_watchlists (name, ticker, weight) values
  ('uranium_pureplay', 'LEU',  1.0),
  ('uranium_pureplay', 'UEC',  1.0),
  ('uranium_pureplay', 'URA',  1.0),
  ('uranium_pureplay', 'CCJ',  1.0),
  ('water_datacenter', 'WTRG', 1.0),
  ('water_datacenter', 'XYL',  1.0),
  ('water_datacenter', 'AWK',  1.0),
  ('water_datacenter', 'VRT',  1.0),
  ('water_datacenter', 'ETN',  1.0),
  ('water_datacenter', 'GEV',  1.0)
on conflict do nothing;

-- Refresh the table comment for posterity
comment on table stock_keyword_rules is
  'News + truth_social classifier rules. As of 0038 (2026-06-04): '
  '70 news + 66 truth_social. Disable any single rule via PATCH on enabled. '
  'No deploy required — agents reload rules on each run.';
