-- 0035 — thesis_agent rejection audit
--
-- Why this exists:
--   thesis_agent processes 60-120 events per 5-min run but emitted 0 rubric
--   signals between 5/22 and 6/2. The pulsecheck framework caught the
--   silence (candidate_dryness check) but couldn't say WHICH gate was
--   binding. Three independent gates can suppress: cluster_passes (most
--   common failure label is `single_source_no_exception`), score<50
--   (action_for returns ""), and catalyst_score==0 (which downgrades
--   bullish actions to MOMENTUM_ONLY).
--
--   This table records one row per rejected cluster so we can measure
--   the distribution and pick the highest-ROI intervention with data.
--
-- Lifecycle:
--   thesis_agent writes one row per cluster that gets dropped at line
--   1711 (filter: cluster_ok AND non-empty action). A successful cluster
--   does NOT write here — it becomes a stock_signals row instead.
--
--   Rows are append-only. A future cleanup job can prune rows older
--   than N days, but free-tier storage is fine for months of hourly
--   data.

create table if not exists stock_thesis_rejections (
  id               bigserial primary key,
  thesis_run_id    bigint,
  fired_at         timestamptz not null default now(),
  cluster_ticker   text not null,
  cluster_bucket   text,
  n_events         integer not null default 0,
  source_agents    text[] not null default '{}',
  direction        text,
  score            numeric(8,3),
  catalyst_score   numeric(8,3),
  context_score    numeric(8,3),
  background_score numeric(8,3),
  fail_reason      text not null,
  cluster_label    text,
  action           text,
  breakdown_sample jsonb not null default '[]'::jsonb,
  meta             jsonb not null default '{}'::jsonb
);

create index if not exists idx_thesis_rejections_recent
  on stock_thesis_rejections (fired_at desc);

create index if not exists idx_thesis_rejections_reason
  on stock_thesis_rejections (fail_reason, fired_at desc);

comment on table stock_thesis_rejections is
  'Append-only audit of thesis_agent clusters dropped before emit. '
  'Used to measure which gate (cluster_passes / score<50 / catalyst_score==0) '
  'is binding so we can pick the highest-ROI intervention.';


-- Convenience view: rolling 24h rejection-mix by fail_reason.
create or replace view stock_thesis_rejection_mix as
select
  fail_reason,
  count(*)                              as n,
  count(distinct cluster_ticker)        as distinct_tickers,
  round(avg(score)::numeric, 2)         as avg_score,
  round(avg(catalyst_score)::numeric, 2) as avg_catalyst_score,
  min(fired_at)                         as first_seen,
  max(fired_at)                         as last_seen
from stock_thesis_rejections
where fired_at >= now() - interval '24 hours'
group by fail_reason
order by n desc;

comment on view stock_thesis_rejection_mix is
  'Rolling 24h rejection mix. Consumed by pulsecheck_thesis.rejection_distribution.';
