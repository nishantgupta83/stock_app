-- Operational tables — heartbeat, dead-letter, and signal-table enrichment.
-- Addresses external review findings: no job_runs, no dead_letter, no
-- parser_confidence, no event_subtype, no status enum on signals.

-- ============================================================
-- stock_job_runs — every agent invocation logs start/end here.
-- Lets the dashboard answer: "is the filing agent still alive?"
-- and the EOD reconciler ignore signals from runs that crashed mid-flight.
-- ============================================================
create table if not exists stock_job_runs (
  id            bigserial primary key,
  agent         text not null,                 -- 'filing_agent', 'truth_social_agent', ...
  started_at    timestamptz not null default now(),
  finished_at   timestamptz,
  status        text not null default 'running'  -- 'running' | 'ok' | 'failed' | 'partial'
                check (status in ('running','ok','failed','partial')),
  rows_in       integer default 0,
  rows_out      integer default 0,
  error_text    text,
  meta          jsonb default '{}'::jsonb
);
create index if not exists stock_job_runs_agent_started_idx on stock_job_runs (agent, started_at desc);

-- ============================================================
-- stock_dead_letter_events — anything that failed to parse goes here.
-- Lets us debug without losing data, and bound the size of normalized tables.
-- ============================================================
create table if not exists stock_dead_letter_events (
  id            bigserial primary key,
  occurred_at   timestamptz not null default now(),
  agent         text not null,
  source_table  text,
  source_id     bigint,
  reason        text not null,                 -- short tag: 'xml_parse_error', 'schema_mismatch', ...
  detail        text,                          -- full traceback or vendor payload excerpt
  payload       jsonb
);
create index if not exists stock_dead_letter_events_agent_idx on stock_dead_letter_events (agent, occurred_at desc);

-- ============================================================
-- Enrich stock_normalized_events
-- ============================================================
alter table stock_normalized_events
  add column if not exists event_subtype     text,            -- e.g. '8k_item_1_01'
  add column if not exists parser_confidence numeric(5,4) default 1.0,
  add column if not exists dedupe_key        text;

-- partial unique index — only enforce when dedupe_key is provided
create unique index if not exists stock_normalized_events_dedupe_idx
  on stock_normalized_events (dedupe_key)
  where dedupe_key is not null;

-- ============================================================
-- Enrich stock_signals — add status state machine + dedupe + score breakdown
-- ============================================================
alter table stock_signals
  add column if not exists action          text default 'WATCH'
    check (action in ('WATCH','RESEARCH','AVOID_CHASE','BUY','SELL','TRIM')),
  add column if not exists score           numeric(6,2),       -- 0-100 from §17.7 rubric
  add column if not exists score_breakdown jsonb,              -- per-rule contribution
  add column if not exists evidence_summary text,              -- ≤80 chars, the Telegram body
  add column if not exists dedupe_key      text,
  add column if not exists status_v2       text default 'candidate'
    check (status_v2 in ('candidate','sent','suppressed','expired','demoted'));

create unique index if not exists stock_signals_dedupe_idx
  on stock_signals (dedupe_key)
  where dedupe_key is not null;

-- ============================================================
-- Enrich stock_telegram_dispatch_log with the reviewer's `dedupe_key`
-- to prevent fan-out duplicates (e.g. polling re-fires same signal).
-- ============================================================
alter table stock_telegram_dispatch_log
  add column if not exists dedupe_key text;

create unique index if not exists stock_telegram_dispatch_idx
  on stock_telegram_dispatch_log (dedupe_key)
  where dedupe_key is not null;

-- ============================================================
-- View: latest agent heartbeat — used by the static site freshness panel
-- ============================================================
create or replace view stock_agent_freshness as
select
  agent,
  max(started_at)  as last_seen,
  max(finished_at) as last_finished,
  count(*) filter (where status = 'failed' and started_at > now() - interval '1 hour') as failures_last_hour
from stock_job_runs
group by agent;
