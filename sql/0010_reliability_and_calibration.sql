-- Phase 7: reliability, lineage, and calibration contracts.
--
-- This migration is additive except for check-constraint refreshes. It keeps the
-- system paper-only while making reruns and replay calibration auditable.

-- Forecast audits are one outcome per signal/horizon. Agents should upsert on
-- this key and use reruns to heal dependent forecast/signal rows.
alter table stock_forecast_audit
  add column if not exists entry_price numeric,
  add column if not exists exit_price numeric,
  add column if not exists entry_at timestamptz,
  add column if not exists exit_at timestamptz,
  add column if not exists outcome_method text not null default 'next_session_open_to_horizon_close'
    check (outcome_method in ('next_session_open_to_horizon_close'));

delete from stock_forecast_audit a
using stock_forecast_audit b
where a.signal_id = b.signal_id
  and a.horizon_days = b.horizon_days
  and (
    coalesce(a.computed_at, '-infinity'::timestamptz),
    a.id
  ) < (
    coalesce(b.computed_at, '-infinity'::timestamptz),
    b.id
  );

create unique index if not exists stock_forecast_audit_signal_horizon_idx
  on stock_forecast_audit (signal_id, horizon_days);

-- Evidence rows are idempotent too. Null event_id evidence remains allowed for
-- synthetic/manual evidence, but real normalized events must not duplicate.
delete from stock_signal_evidence a
using stock_signal_evidence b
where a.signal_id = b.signal_id
  and a.event_id = b.event_id
  and a.event_id is not null
  and a.id < b.id;

create unique index if not exists stock_signal_evidence_signal_event_idx
  on stock_signal_evidence (signal_id, event_id)
  where event_id is not null;

-- PostgREST upserts target plain column lists via `on_conflict=...`. Plain
-- unique indexes on nullable columns still allow multiple null values in
-- PostgreSQL, while giving rerun-healing writes a non-partial conflict target.
create unique index if not exists stock_normalized_events_dedupe_unique_idx
  on stock_normalized_events (dedupe_key);

create unique index if not exists stock_signal_evidence_signal_event_unique_idx
  on stock_signal_evidence (signal_id, event_id);

create unique index if not exists stock_telegram_dispatch_dedupe_unique_idx
  on stock_telegram_dispatch_log (dedupe_key);

-- Freshness should reflect the latest run status, not only max(started_at).
create or replace view stock_agent_freshness as
with latest as (
  select distinct on (agent)
    agent,
    started_at,
    finished_at,
    status
  from stock_job_runs
  order by agent, started_at desc
),
agg as (
  select
    agent,
    count(*) filter (where status = 'failed' and started_at > now() - interval '1 hour') as failures_last_hour,
    count(*) filter (where status = 'running' and started_at < now() - interval '30 minutes') as stale_running
  from stock_job_runs
  group by agent
)
select
  latest.agent,
  latest.started_at as last_seen,
  latest.finished_at as last_finished,
  latest.status as last_status,
  coalesce(agg.failures_last_hour, 0) as failures_last_hour,
  coalesce(agg.stale_running, 0) as stale_running
from latest
left join agg using (agent);

-- A failed Telegram delivery is retryable state, not a terminal silent skip.
alter table stock_signals drop constraint if exists stock_signals_status_v2_check;
alter table stock_signals
  add constraint stock_signals_status_v2_check
  check (status_v2 in (
    'candidate',
    'sent',
    'suppressed',
    'expired',
    'demoted',
    'backtest',
    'closed',
    'dispatch_failed'
  ));

-- Source registry now reflects the live RSS feeds actually polled by news_agent.
update stock_data_sources
set is_primary = false
where category = 'news';

insert into stock_data_sources (name, category, url, is_primary, fallback_for, notes) values
  ('cnbc_markets',  'news', 'https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=15839069', true,  null,          'Live RSS feed polled by news_agent.'),
  ('marketwatch',   'news', 'https://feeds.marketwatch.com/marketwatch/topstories/',                              false, 'cnbc_markets', 'Live RSS feed polled by news_agent.'),
  ('seeking_alpha', 'news', 'https://seekingalpha.com/market_currents.xml',                                        false, 'cnbc_markets', 'Live RSS feed polled by news_agent.'),
  ('rss_reuters',   'news', 'https://www.reutersagency.com/feed/?best-topics=business-finance',                   false, 'cnbc_markets', 'Fallback probe only; not currently polled by news_agent.')
on conflict (name) do update
set
  category = excluded.category,
  url = excluded.url,
  is_primary = excluded.is_primary,
  fallback_for = excluded.fallback_for,
  notes = excluded.notes;

-- Dashboard/reporting view: keeps live and shadow calibration separate and
-- exposes the setup bucket behind each probability.
create or replace view stock_paper_calibration_summary as
select
  forecast_mode,
  ticker,
  horizon_days,
  score_bucket,
  source_action,
  paper_action,
  coalesce(features_json->>'setup_key', source_action) as setup_key,
  count(*) as n_forecasts,
  count(*) filter (where status = 'closed') as n_closed,
  count(*) filter (where status = 'closed' and correct is true) as n_correct,
  avg(prob_win) as avg_prob_win,
  avg(expected_value) as avg_expected_value,
  case
    when count(*) filter (where status = 'closed' and correct is not null) = 0 then null
    else
      (count(*) filter (where status = 'closed' and correct is true)::numeric /
       count(*) filter (where status = 'closed' and correct is not null))
  end as hit_rate
from stock_paper_forecasts
group by
  forecast_mode,
  ticker,
  horizon_days,
  score_bucket,
  source_action,
  paper_action,
  coalesce(features_json->>'setup_key', source_action);
