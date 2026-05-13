-- 0022_run_lineage.sql
--
-- Adds parent_run_id / run_type / stage columns to stock_job_runs so the
-- dashboard can distinguish wrapper rows (the GH Actions YAML scaffold)
-- from agent rows (the Python code's own heartbeat) and trace lineage
-- between them.
--
-- Stage 1B of the trading-pipeline backlog. All columns are nullable with
-- safe defaults so existing INSERTs that don't include them continue to
-- work — no agent code changes are forced by this migration.

alter table stock_job_runs
  add column if not exists parent_run_id bigint references stock_job_runs(id) on delete set null,
  add column if not exists run_type      text not null default 'agent',
  add column if not exists stage         text;

-- Backfill: rows previously created by ops_recorder.start() (workflow
-- wrappers) have agent names prefixed with "workflow_". Tag those as
-- run_type='wrapper' so the dashboard dedupe can prefer non-wrapper rows
-- when both exist for the same canonical agent.
update stock_job_runs
   set run_type = 'wrapper'
 where run_type = 'agent'                                   -- only the default-tagged rows
   and agent like 'workflow\_%' escape '\';                 -- literal underscore match

-- Helpful indexes:
--   parent lookup chains (audit who-spawned-whom)
--   filter by run_type when computing dashboard truth
create index if not exists stock_job_runs_parent_idx
  on stock_job_runs (parent_run_id)
  where parent_run_id is not null;

create index if not exists stock_job_runs_type_started_idx
  on stock_job_runs (run_type, started_at desc);

-- Constrain run_type to known values so a typo doesn't silently pollute
-- the dedupe logic. Add as a soft check (not null + value list).
alter table stock_job_runs
  drop constraint if exists stock_job_runs_run_type_check;

alter table stock_job_runs
  add constraint stock_job_runs_run_type_check
  check (run_type in ('agent', 'wrapper', 'sub_task'));
