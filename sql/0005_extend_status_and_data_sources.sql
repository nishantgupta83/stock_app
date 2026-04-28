-- Extend status_v2 enum to include 'backtest' (signals fired during historical
-- replay, never sent live). And add a data_sources table so the review agent
-- has a canonical place to record per-source health + recommended fallback chain.

-- 1. Drop + recreate the check constraint to add 'backtest'
alter table stock_signals drop constraint if exists stock_signals_status_v2_check;
alter table stock_signals
  add constraint stock_signals_status_v2_check
  check (status_v2 in ('candidate','sent','suppressed','expired','demoted','backtest'));

-- 2. Data source registry — canonical list of every external dep with primary/
-- fallback chain. Updated by source_review_agent.
create table if not exists stock_data_sources (
  id              bigserial primary key,
  name            text unique not null,           -- 'yfinance', 'stooq', 'edgar', 'trumpstruth_rss', etc.
  category        text not null,                  -- 'price', 'filing', 'news', 'social', 'notification'
  url             text,
  is_primary      boolean default false,          -- exactly one true per category in healthy state
  fallback_for    text,                           -- name of the primary this is a fallback for (null if itself primary)
  notes           text,
  created_at      timestamptz default now(),
  last_health_check_at  timestamptz,
  last_health_check_ok  boolean,
  consecutive_failures  integer default 0
);

-- Seed the registry with current sources + known free alternatives
insert into stock_data_sources (name, category, url, is_primary, fallback_for, notes) values
  -- Prices
  ('yfinance',         'price',        'https://github.com/ranaroussi/yfinance',     true,  null,        'Primary. Yahoo blocks GitHub IPs without curl_cffi browser impersonation.'),
  ('stooq',            'price',        'https://stooq.com/',                          false, 'yfinance',  'EU-based mirror, no rate limit, no IP blocks. Daily bars only.'),
  ('finnhub_free',     'price',        'https://finnhub.io/docs/api',                 false, 'yfinance',  'Real-time quotes, 60 req/min free tier. Requires API key.'),
  ('tiingo_free',      'price',        'https://api.tiingo.com/documentation',        false, 'yfinance',  '500 req/hr, 1yr history free tier. Requires API key.'),
  -- Filings
  ('edgar',            'filing',       'https://www.sec.gov/edgar',                   true,  null,        'Source of truth. No real fallback. 10 req/sec, requires User-Agent.'),
  ('sec_api_io',       'filing',       'https://sec-api.io/',                         false, 'edgar',     'Paid mirror with cleaner JSON. Last resort if EDGAR rate-limits us hard.'),
  -- News
  ('finnhub_news',     'news',         'https://finnhub.io/docs/api/company-news',    true,  null,        'Per-ticker company news, 60 req/min free.'),
  ('rss_reuters',      'news',         'https://www.reuters.com/tools/rss',           false, 'finnhub_news', 'Reuters RSS, no API key, headlines only.'),
  ('rss_cnbc',         'news',         'https://www.cnbc.com/rss-feeds/',             false, 'finnhub_news', 'CNBC RSS, no API key.'),
  -- Social
  ('trumpstruth_rss',  'social',       'https://trumpstruth.org/feed',                true,  null,        'Third-party Trump archive. Single point of failure.'),
  ('truthbrush',       'social',       'https://github.com/stanfordio/truthbrush',    false, 'trumpstruth_rss', 'Direct Truth Social scraper, gray ToS. Backup only.'),
  -- Notifications
  ('telegram',         'notification', 'https://core.telegram.org/bots/api',          true,  null,        'Free, instant. Bot tokens are revocable.'),
  ('email_smtp',       'notification', 'rfc5321',                                     false, 'telegram',  'Fallback if Telegram bans bot. Use Gmail SMTP with app password.'),
  ('discord_webhook',  'notification', 'https://discord.com/developers/docs/resources/webhook', false, 'telegram', 'Free, no rate limits in practice. Backup channel.')
on conflict (name) do nothing;

-- View: sources by category showing primary + fallbacks ordered for each
create or replace view stock_source_chain as
select
  category,
  name,
  is_primary,
  fallback_for,
  consecutive_failures,
  last_health_check_at,
  last_health_check_ok,
  url
from stock_data_sources
order by category, is_primary desc, name;
