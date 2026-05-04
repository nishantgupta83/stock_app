-- Phase: editable keyword routing for news_agent + truth_social_agent.
-- Pre-existing hardcoded dicts in agents/news_agent.py and agents/truth_social_agent.py
-- are mirrored here so the user can add/edit/disable rules through SQL without a code
-- deploy. Each agent loads enabled rules at run start; on DB failure each agent falls
-- back to a small hardcoded safety-net so classification never silently breaks.

create table if not exists stock_keyword_rules (
  id              bigserial primary key,
  kind            text not null check (kind in ('news', 'truth_social')),
  keyword         text not null,                                   -- the trigger word/phrase or regex source
  match_type      text not null default 'icontains'                -- 'icontains' (case-insensitive substring) or 'regex'
                  check (match_type in ('icontains', 'regex')),
  direction_prior text not null default 'neutral'
                  check (direction_prior in ('long', 'short', 'neutral')),
  tickers         text[] default '{}',                             -- ticker basket; empty = sentiment-only (news)
  rule_label      text,                                            -- short trace label, e.g. tariff_general
  enabled         boolean not null default true,
  notes           text,
  created_at      timestamptz default now()
);

create index if not exists stock_keyword_rules_kind_idx
  on stock_keyword_rules (kind, enabled);

-- Same keyword can exist in both kinds; uniqueness is per-kind.
create unique index if not exists stock_keyword_rules_uniq_idx
  on stock_keyword_rules (kind, lower(keyword)) where enabled = true;

alter table stock_keyword_rules enable row level security;

-- ============================================================
-- Seed: Truth Social router (mirrors _RULES from truth_social_agent.py)
-- ============================================================
insert into stock_keyword_rules (kind, keyword, match_type, direction_prior, tickers, rule_label, notes) values
  ('truth_social', '\btariff(s)?\b',                      'regex',     'short',   '{XLI,XLB,XLY}',           'tariff_general',           'tariff threat → industrials/materials/EM short'),
  ('truth_social', '\bchina|xi\s+jinping|ccp\b',          'regex',     'short',   '{AAPL,NVDA,TSLA,FXI}',    'china',                    'China antagonism → US-China supply chain'),
  ('truth_social', '\b(fed|powell|interest\s+rate)\b',    'regex',     'long',    '{TLT,XLF}',               'rates_dovish_or_hawkish',  'rates context — direction read separately by sentiment in v2'),
  ('truth_social', '\b(oil|drill(ing)?|opec)\b',          'regex',     'long',    '{XLE,XOM}',               'oil',                      'oil bullish'),
  ('truth_social', '\b(crypto|bitcoin|btc)\b',            'regex',     'long',    '{COIN,MSTR}',             'crypto',                   'crypto positive default'),
  ('truth_social', '\b(djt|truth\s+social)\b',            'regex',     'long',    '{DJT}',                   'djt_self',                 'self-promotion');

-- Seed: Truth Social direct company name mentions (_COMPANY_MAP from truth_social_agent.py)
insert into stock_keyword_rules (kind, keyword, match_type, direction_prior, tickers, rule_label) values
  ('truth_social', 'apple',      'icontains', 'neutral', '{AAPL}',  'name_AAPL'),
  ('truth_social', 'nvidia',     'icontains', 'neutral', '{NVDA}',  'name_NVDA'),
  ('truth_social', 'microsoft',  'icontains', 'neutral', '{MSFT}',  'name_MSFT'),
  ('truth_social', 'amazon',     'icontains', 'neutral', '{AMZN}',  'name_AMZN'),
  ('truth_social', 'google',     'icontains', 'neutral', '{GOOGL}', 'name_GOOGL'),
  ('truth_social', 'alphabet',   'icontains', 'neutral', '{GOOGL}', 'name_GOOGL_alt'),
  ('truth_social', 'meta',       'icontains', 'neutral', '{META}',  'name_META'),
  ('truth_social', 'facebook',   'icontains', 'neutral', '{META}',  'name_META_alt'),
  ('truth_social', 'tesla',      'icontains', 'neutral', '{TSLA}',  'name_TSLA'),
  ('truth_social', 'berkshire',  'icontains', 'neutral', '{BRK.B}', 'name_BRKB'),
  ('truth_social', 'jpmorgan',   'icontains', 'neutral', '{JPM}',   'name_JPM'),
  ('truth_social', 'exxon',      'icontains', 'neutral', '{XOM}',   'name_XOM'),
  ('truth_social', 'walmart',    'icontains', 'neutral', '{WMT}',   'name_WMT'),
  ('truth_social', 'netflix',    'icontains', 'neutral', '{NFLX}',  'name_NFLX'),
  ('truth_social', 'costco',     'icontains', 'neutral', '{COST}',  'name_COST'),
  ('truth_social', 'visa',       'icontains', 'neutral', '{V}',     'name_V'),
  ('truth_social', 'mastercard', 'icontains', 'neutral', '{MA}',    'name_MA'),
  ('truth_social', 'amd',        'icontains', 'neutral', '{AMD}',   'name_AMD'),
  ('truth_social', 'coinbase',   'icontains', 'neutral', '{COIN}',  'name_COIN');

-- ============================================================
-- Seed: News company-name → ticker map (mirrors _COMPANY_MAP from news_agent.py)
-- ============================================================
insert into stock_keyword_rules (kind, keyword, match_type, direction_prior, tickers, rule_label) values
  ('news', 'nvidia',        'icontains', 'neutral', '{NVDA}',  'name_NVDA'),
  ('news', 'apple',         'icontains', 'neutral', '{AAPL}',  'name_AAPL'),
  ('news', 'microsoft',     'icontains', 'neutral', '{MSFT}',  'name_MSFT'),
  ('news', 'amazon',        'icontains', 'neutral', '{AMZN}',  'name_AMZN'),
  ('news', 'broadcom',      'icontains', 'neutral', '{AVGO}',  'name_AVGO'),
  ('news', 'alphabet',      'icontains', 'neutral', '{GOOGL}', 'name_GOOGL'),
  ('news', 'google',        'icontains', 'neutral', '{GOOGL}', 'name_GOOGL_alt'),
  ('news', 'meta',          'icontains', 'neutral', '{META}',  'name_META'),
  ('news', 'facebook',      'icontains', 'neutral', '{META}',  'name_META_alt'),
  ('news', 'tesla',         'icontains', 'neutral', '{TSLA}',  'name_TSLA'),
  ('news', 'berkshire',     'icontains', 'neutral', '{BRK.B}', 'name_BRKB'),
  ('news', 'jpmorgan',      'icontains', 'neutral', '{JPM}',   'name_JPM'),
  ('news', 'j.p. morgan',   'icontains', 'neutral', '{JPM}',   'name_JPM_alt'),
  ('news', 'eli lilly',     'icontains', 'neutral', '{LLY}',   'name_LLY'),
  ('news', 'exxon',         'icontains', 'neutral', '{XOM}',   'name_XOM'),
  ('news', 'johnson',       'icontains', 'neutral', '{JNJ}',   'name_JNJ'),
  ('news', 'walmart',       'icontains', 'neutral', '{WMT}',   'name_WMT'),
  ('news', 'netflix',       'icontains', 'neutral', '{NFLX}',  'name_NFLX'),
  ('news', 'costco',        'icontains', 'neutral', '{COST}',  'name_COST'),
  ('news', 'mastercard',    'icontains', 'neutral', '{MA}',    'name_MA'),
  ('news', 'coinbase',      'icontains', 'neutral', '{COIN}',  'name_COIN'),
  ('news', 'microstrategy', 'icontains', 'neutral', '{MSTR}',  'name_MSTR');

-- ============================================================
-- Seed: News sentiment lexicon (mirrors _BULLISH_RE / _BEARISH_RE from news_agent.py)
-- Empty tickers[] = sentiment-only rule, applied to all matched tickers.
-- ============================================================
insert into stock_keyword_rules (kind, keyword, match_type, direction_prior, rule_label) values
  -- Bullish
  ('news', '\b(beat(s)?|jumps?|rally|surge(s|d)?|raises?\s+guidance|buyback|acquisition|upgrade(s|d)?|record\s+(high|profit|revenue)|dividend|strong\s+(earnings|results)|outperform)\b', 'regex', 'long', 'sentiment_bullish'),
  -- Bearish
  ('news', '\b(miss(es|ed)?|fall(s|ing)?|drop(s|ped)?|cut(s)?\s+guidance|layoff|investigation|lawsuit|recall|downgrade(s|d)?|disappointing|warning|loss(es)?|below\s+expectations)\b', 'regex', 'short', 'sentiment_bearish');
