-- 0043 — Layer 2.b gate-decision history (append-only, versioned)
--
-- STATUS: DRAFT — apply OUT-OF-BAND (Supabase Management API / psql), NOT via the
-- CLI `supabase db push`. The CLI ledger (supabase/migrations/) is ~30 migrations
-- stale; pushing it would mis-apply against a much newer live schema. See
-- docs/findings/2026-08-02_schema-reconciliation.md.
--
-- Why this exists:
--   sql/0039.stock_signal_candidates has a SINGLE mutable `gate_decision` column.
--   When the 2.b precision gate (_metalabel_gate) goes live and is re-versioned
--   (the plan switches an experiment's input only under a NEW immutable gate
--   version), overwriting that one field would destroy the audit trail of what an
--   earlier gate version decided and on what evidence. This table records EVERY
--   decision, append-only, so a re-version APPENDS rather than clobbers.
--
--   The candidate row's `gate_decision` may keep the latest for convenience, but
--   THIS table is the source of truth for the gate audit + the paper go/no-go.
--
-- Statistics discipline (CLAUDE.md + feedback_forward_experiment_isolation):
--   the recorded stats are EFFECTIVE (collapsed independent ticker-entry-day)
--   evidence per (rule_key × direction × horizon) on NET returns — never raw n.

create table if not exists stock_candidate_gate_decisions (
  id                bigserial primary key,
  candidate_id      bigint      not null,   -- stock_signal_candidates.id decided on
  gate_version      text        not null,   -- immutable version tag of the 2.b gate
  decided_at        timestamptz not null default now(),
  -- the primary cell the decision keyed on:
  rule_key          text,
  horizon_days      integer,
  direction         text,                   -- long | short (never pooled in one PF)
  -- the decision + its reason (mirrors _metalabel_gate GATE_REASONS):
  decision          text        not null,   -- act | watch | fail_open
  reason            text,                   -- calibrated_profitable | suppressed_low_pf | fail_open_thin
  -- input statistics at decision time (EFFECTIVE evidence, not raw n):
  eff_n             integer,
  eff_profit_factor numeric(12,4),
  eff_expectancy    numeric(12,6),          -- mean NET return per independent obs
  -- hypothesis-specific source-health at decision time (so "blocked because a
  -- source was down" is distinguishable from "acted"; see the plan's Step 4):
  source_health     jsonb       not null default '{}'::jsonb,
  meta              jsonb       not null default '{}'::jsonb
);

-- Append-only reads: latest decisions per candidate (audit), per gate version
-- (compare versions), per cell (coverage / expectancy replay).
create index if not exists idx_gate_decisions_candidate
  on stock_candidate_gate_decisions (candidate_id, decided_at desc);
create index if not exists idx_gate_decisions_version
  on stock_candidate_gate_decisions (gate_version, decided_at desc);
create index if not exists idx_gate_decisions_cell
  on stock_candidate_gate_decisions (rule_key, horizon_days, direction, decided_at desc);

-- Idempotency support (NON-unique on purpose — a unique/partial index would tempt
-- a PostgREST ?on_conflict= insert, which 42P10's on partial indexes; CLAUDE.md
-- rule #2). Dedup, if ever needed, stays in the agent with a plain INSERT.
create index if not exists idx_gate_decisions_dedup
  on stock_candidate_gate_decisions (candidate_id, gate_version);

comment on table stock_candidate_gate_decisions is
  'Layer 2.b append-only, versioned gate-decision history. One row per (candidate, '
  'gate_version) decision with EFFECTIVE input stats (eff_n / eff_profit_factor / '
  'eff_expectancy) + source-health at decision time. Source of truth for the gate '
  'audit + paper go/no-go; stock_signal_candidates.gate_decision may hold the latest '
  'for convenience. See docs/design/layer2-metalabeling-funnel.md + '
  'docs/findings/2026-08-02_schema-reconciliation.md.';
