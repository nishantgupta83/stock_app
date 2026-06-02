-- 0034 — pulsecheck telemetry table
--
-- Stores per-(agent, check_name) health observations from the pulsecheck
-- agents. Designed so each pulsecheck owns a defined scope and never
-- writes outside its bucket — preventing the "everyone tracks everything"
-- duplication that makes alert fatigue inevitable.
--
-- Granularity: one row per (agent, check_name, pulsed_at). Old rows are
-- not pruned automatically (free-tier storage is fine for years of
-- hourly checks); a daily aggregate view shows current state.
--
-- The "critical findings should be caught early" intent is realized by:
--   1. cron-driven pulse — runs hourly, not on-demand
--   2. status levels: ok | warning | critical (skip "alarm")
--   3. threshold + observed pair so you can see HOW close to the line
--   4. dependency check via meta.depends_on so chained failures don't
--      generate confusing alerts (e.g., if Supabase is unreachable, the
--      thesis pulsecheck reports "precondition_failed" not "thesis broke")

create table if not exists stock_health_pulse (
  id            bigserial primary key,
  agent         text not null,       -- pulsecheck_thesis, pulsecheck_foundation, ...
  check_name    text not null,       -- emit_rate, cap_consumption, supabase_up, ...
  status        text not null check (status in
                  ('ok', 'warning', 'critical', 'skipped', 'precondition_failed')),
  detail        text,
  observed      numeric,
  threshold     numeric,
  meta          jsonb not null default '{}'::jsonb,
  pulsed_at     timestamptz not null default now()
);

create index if not exists idx_health_pulse_recent
  on stock_health_pulse (agent, check_name, pulsed_at desc);

create index if not exists idx_health_pulse_alerts
  on stock_health_pulse (pulsed_at desc, status)
  where status in ('warning', 'critical');

comment on table stock_health_pulse is
  'Pulsecheck telemetry. One row per (agent, check_name, pulsed_at). '
  'Read via stock_health_pulse_current view for latest state.';


-- Current state view — most recent pulse per (agent, check_name).
create or replace view stock_health_pulse_current as
select distinct on (agent, check_name)
  agent,
  check_name,
  status,
  detail,
  observed,
  threshold,
  meta,
  pulsed_at,
  -- Age in seconds — useful for spotting stalled pulsechecks.
  extract(epoch from (now() - pulsed_at))::integer as age_seconds
from stock_health_pulse
order by agent, check_name, pulsed_at desc;

comment on view stock_health_pulse_current is
  'Most recent pulse per (agent, check_name). Use for "what is broken right now".';


-- Recent alerts view — anything that crossed warning/critical in the last 24h.
create or replace view stock_health_pulse_recent_alerts as
select
  agent,
  check_name,
  status,
  detail,
  observed,
  threshold,
  pulsed_at
from stock_health_pulse
where status in ('warning', 'critical')
  and pulsed_at >= now() - interval '24 hours'
order by pulsed_at desc;

comment on view stock_health_pulse_recent_alerts is
  '24h rolling alert feed. Source for the daily health digest.';
