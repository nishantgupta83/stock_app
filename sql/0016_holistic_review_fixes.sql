-- Holistic schema review fixes — three issues surfaced when reviewing all 15
-- migrations together:
--
-- 1. RLS gap on 3 operational tables created in 0004 / 0005. They don't
--    expose user data but RLS-disabled means an anon client with the URL
--    can read job logs / dead letters / data-source health. Defense in
--    depth — every other table has RLS on; close the gap.
--
-- 2. Hot-path index missing on stock_signals.status_v2. Confirmed used by
--    thesis_agent (5+ queries) + paper_trade_agent + site_generator with
--    ?status_v2=eq.{candidate,sent,backtest,dispatch_failed}. Currently
--    full-scans 150 rows — fine — but won't scale.
--
-- 3. The action-allow-list constraint in 0014 dropped 'TRIM' (which was
--    in the 0004 / 0007 lists). 'TRIM' is unused in code today so this
--    isn't an active bug, but restoring it keeps the constraint history
--    additive. If we later wire up a position-trim action we won't need
--    another constraint migration.

-- ============================================================
-- 1. RLS for ops tables
-- ============================================================
alter table stock_job_runs              enable row level security;
alter table stock_dead_letter_events    enable row level security;
alter table stock_data_sources          enable row level security;

-- ============================================================
-- 2. Hot-path index on stock_signals.status_v2
-- ============================================================
create index if not exists stock_signals_status_v2_fired_idx
  on stock_signals (status_v2, fired_at desc);

-- ============================================================
-- 3. Re-add 'TRIM' to the action allow-list
-- ============================================================
alter table stock_signals drop constraint if exists stock_signals_action_check;
alter table stock_signals add constraint stock_signals_action_check
  check (action in ('WATCH','RESEARCH','AVOID_CHASE','CHASE_RISK','BUY','SELL','TRIM'));
