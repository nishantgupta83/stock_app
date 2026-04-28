-- Market Intelligence Platform — initial schema
-- All tables prefixed `stock_` to namespace within the user's Supabase project.
-- Apply in Supabase SQL editor (https://app.supabase.com → SQL → new query).

-- ============================================================
-- Reference data
-- ============================================================

create table if not exists stock_symbols (
  ticker        text primary key,
  cik           text,                          -- SEC Central Index Key, zero-padded 10 chars
  name          text,
  sector        text,
  is_etf        boolean default false,
  is_active     boolean default true,
  created_at    timestamptz default now()
);

create table if not exists stock_watchlists (
  id            bigserial primary key,
  name          text not null,                 -- e.g. 'core', 'context'
  ticker        text not null references stock_symbols(ticker),
  weight        numeric default 1.0,           -- index weight or priority
  created_at    timestamptz default now(),
  unique (name, ticker)
);

-- ============================================================
-- Raw ingestion (append-only, vendor payloads preserved)
-- ============================================================

create table if not exists stock_raw_filings (
  id                bigserial primary key,
  accession_number  text unique not null,      -- EDGAR's natural key, dedupes retries
  cik               text not null,
  ticker            text,                       -- denormalized for fast lookup
  form_type         text not null,             -- 8-K, 10-Q, 10-K, 4, 13D, 13G, S-3...
  filed_at          timestamptz not null,
  primary_doc_url   text,
  raw_payload       jsonb not null,
  ingested_at       timestamptz default now()
);
create index if not exists stock_raw_filings_ticker_filed_idx on stock_raw_filings (ticker, filed_at desc);
create index if not exists stock_raw_filings_form_idx on stock_raw_filings (form_type);

create table if not exists stock_raw_news (
  id            bigserial primary key,
  source        text not null,                 -- 'reuters', 'cnbc', 'finnhub', ...
  external_id   text,                           -- vendor article id or url hash
  ticker        text,
  headline      text not null,
  url           text,
  published_at  timestamptz,
  raw_payload   jsonb,
  ingested_at   timestamptz default now(),
  unique (source, external_id)
);
create index if not exists stock_raw_news_ticker_pub_idx on stock_raw_news (ticker, published_at desc);

create table if not exists stock_raw_prices (
  id            bigserial primary key,
  ticker        text not null,
  ts            timestamptz not null,
  open          numeric, high numeric, low numeric, close numeric,
  volume        bigint,
  source        text not null,                 -- 'finnhub', 'yfinance'
  unique (ticker, ts, source)
);
create index if not exists stock_raw_prices_ticker_ts_idx on stock_raw_prices (ticker, ts desc);

create table if not exists stock_raw_truth_posts (
  id            bigserial primary key,
  post_id       text unique not null,          -- truthsocial post id or rss guid
  posted_at     timestamptz not null,
  content       text not null,
  url           text,
  source        text default 'trumpstruth_rss',
  ingested_at   timestamptz default now()
);
create index if not exists stock_raw_truth_posts_posted_idx on stock_raw_truth_posts (posted_at desc);

-- ============================================================
-- Normalized events (pipeline output, one row per real-world event)
-- ============================================================

create table if not exists stock_normalized_events (
  id            bigserial primary key,
  event_type    text not null,                 -- 'earnings_release', '8k_material_event', 'insider_sell_cluster', 'truth_social_post', 'price_gap', ...
  ticker        text,
  event_at      timestamptz not null,
  severity      smallint default 0,            -- 0=info, 1=low, 2=medium, 3=high, 4=critical
  source_table  text,                          -- 'stock_raw_filings', etc.
  source_id     bigint,
  payload       jsonb,                         -- event-specific extracted fields
  created_at    timestamptz default now()
);
create index if not exists stock_normalized_events_ticker_event_idx on stock_normalized_events (ticker, event_at desc);
create index if not exists stock_normalized_events_type_idx on stock_normalized_events (event_type, event_at desc);

-- ============================================================
-- Feature store
-- ============================================================

create table if not exists stock_features_daily (
  ticker        text not null,
  date          date not null,
  features      jsonb not null,                -- { "ret_1d": 0.012, "rs_vs_spy_5d": -0.003, ... }
  primary key (ticker, date)
);

create table if not exists stock_features_intraday (
  ticker        text not null,
  ts            timestamptz not null,
  features      jsonb not null,
  primary key (ticker, ts)
);

-- ============================================================
-- Signals & evidence
-- ============================================================

create table if not exists stock_signals (
  id                  bigserial primary key,
  ticker              text not null,
  fired_at            timestamptz not null default now(),
  direction           text not null,           -- 'BUY', 'SELL', 'TRIM'
  confidence          numeric not null,        -- 0..1
  horizon_days        smallint default 1,
  stop_price          numeric,
  target_price        numeric,
  thesis_summary      text,
  model_version       text,
  weight_at_time      jsonb,                   -- snapshot of agent_weights for reproducibility
  status              text default 'open'      -- 'open', 'filled', 'expired', 'invalidated'
);
create index if not exists stock_signals_ticker_fired_idx on stock_signals (ticker, fired_at desc);

create table if not exists stock_signal_evidence (
  id            bigserial primary key,
  signal_id     bigint not null references stock_signals(id) on delete cascade,
  agent         text not null,                 -- 'filing', 'truth_social', 'news', 'price_action', ...
  event_id      bigint references stock_normalized_events(id),
  strength      numeric default 1.0,
  detail        text
);

-- ============================================================
-- Learning loop
-- ============================================================

create table if not exists stock_forecast_audit (
  id              bigserial primary key,
  signal_id       bigint not null references stock_signals(id) on delete cascade,
  horizon_days    smallint not null,
  realized_return numeric,
  realized_at     timestamptz,
  correct         boolean,
  computed_at     timestamptz default now()
);
create index if not exists stock_forecast_audit_signal_idx on stock_forecast_audit (signal_id);

create table if not exists stock_agent_weights (
  agent         text not null,
  date          date not null,
  accuracy_ema  numeric not null default 0.5,  -- EMA of `correct`, alpha=0.1
  weight        numeric not null default 1.0,  -- clip(accuracy_ema / 0.5, 0.1, 2.0)
  n_signals     integer default 0,
  primary key (agent, date)
);

create table if not exists stock_telegram_dispatch_log (
  id            bigserial primary key,
  signal_id     bigint references stock_signals(id) on delete set null,
  sent_at       timestamptz default now(),
  payload       text not null,
  delivery_ok   boolean,
  telegram_msg_id bigint,
  error         text
);

create table if not exists stock_paper_trades (
  id            bigserial primary key,
  signal_id     bigint not null references stock_signals(id) on delete cascade,
  user_action   text not null,                 -- 'bought', 'sold', 'skipped'
  responded_at  timestamptz default now(),
  notes         text
);

-- ============================================================
-- Backtests & model registry
-- ============================================================

create table if not exists stock_backtest_runs (
  id            bigserial primary key,
  model_version text not null,
  started_at    timestamptz default now(),
  finished_at   timestamptz,
  config        jsonb,
  metrics       jsonb                          -- { "sharpe": 1.1, "max_dd": 0.12, "precision_at_5": 0.58, ... }
);

create table if not exists stock_backtest_trades (
  id            bigserial primary key,
  run_id        bigint not null references stock_backtest_runs(id) on delete cascade,
  ticker        text not null,
  entered_at    timestamptz not null,
  exited_at     timestamptz,
  pnl           numeric
);

create table if not exists stock_model_registry (
  id            bigserial primary key,
  model_version text unique not null,
  trained_at    timestamptz default now(),
  is_champion   boolean default false,
  metrics       jsonb,
  artifact_url  text                            -- supabase storage path to .pkl/.txt
);

-- ============================================================
-- User journal (manual notes outside paper_trades)
-- ============================================================

create table if not exists stock_user_decisions (
  id            bigserial primary key,
  ticker        text,
  decided_at    timestamptz default now(),
  action        text,
  rationale     text
);

create table if not exists stock_journal_entries (
  id            bigserial primary key,
  written_at    timestamptz default now(),
  body          text not null,
  tags          text[]
);

-- ============================================================
-- RLS: enable on all tables; policies added in 0003 once auth roles exist.
-- For Phase 0 the service_role key is the only writer/reader.
-- ============================================================

alter table stock_symbols                enable row level security;
alter table stock_watchlists             enable row level security;
alter table stock_raw_filings            enable row level security;
alter table stock_raw_news               enable row level security;
alter table stock_raw_prices             enable row level security;
alter table stock_raw_truth_posts        enable row level security;
alter table stock_normalized_events      enable row level security;
alter table stock_features_daily         enable row level security;
alter table stock_features_intraday      enable row level security;
alter table stock_signals                enable row level security;
alter table stock_signal_evidence        enable row level security;
alter table stock_forecast_audit         enable row level security;
alter table stock_agent_weights          enable row level security;
alter table stock_telegram_dispatch_log  enable row level security;
alter table stock_paper_trades           enable row level security;
alter table stock_backtest_runs          enable row level security;
alter table stock_backtest_trades        enable row level security;
alter table stock_model_registry         enable row level security;
alter table stock_user_decisions         enable row level security;
alter table stock_journal_entries        enable row level security;
