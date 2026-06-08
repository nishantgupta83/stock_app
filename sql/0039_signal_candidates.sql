-- 0039 — Layer 2.a candidate ledger  (meta-labeling funnel, PR-A.1)
--
-- ⚠️ DRAFT — NOT YET APPLIED. Gated on:
--   (1) the Layer 2→5 foundation correctness audit coming back clean, and
--   (2) two independent Codex reviews (schema + idempotency) being folded in.
-- See docs/design/layer2-metalabeling-funnel.md §7 (PR-A.1).
--
-- Why this exists:
--   We're splitting the monolithic thesis_agent into a funnel:
--     2.a candidate generation (high recall) → 2.b precision gate (per
--     rule_key×horizon expectancy) → orchestrator (one compact alert).
--   2.a writes ONE row here per cluster that clears the LOOSE recall floor —
--   EVERY candidate, whether or not 2.b later acts on it. This is the seam
--   that makes 2.a and 2.b independently testable, and the input the PR-B0
--   cluster-replay coverage measurement reads.
--
--   Score is RETAINED (a column, not a gate): per the empirical finding +
--   independent review, profitability tracks horizon more than score, but
--   score still matters for candidate ranking / dedup priority / within-cell
--   ordering — so 2.a keeps it, it just stops being the precision decision.
--
-- Lifecycle:
--   - 2.a (candidate generation) INSERTs rows; it pre-filters duplicates
--     against dedup_key and does a plain INSERT (NOT ?on_conflict= — partial
--     unique indexes break PostgREST with 42P10, CLAUDE.md rule #2).
--   - gate_decision + emitted_signal_id are NULL until PR-C: 2.b fills
--     gate_decision (per-horizon act/pass), the orchestrator sets
--     emitted_signal_id if it emits to stock_signals. Whether 2.b mutates
--     this row vs. writes a separate decision table is a PR-C decision; the
--     columns are reserved here at zero cost (nullable).
--   - Append-only otherwise; a future prune job can drop rows older than N
--     days (free-tier storage is fine for months at ~41 candidates/day).

create table if not exists stock_signal_candidates (
  id                bigserial primary key,
  candidate_run_id  bigint,
  created_at        timestamptz not null default now(),  -- when it LANDED (CLAUDE.md rule #1)
  fired_at          timestamptz not null default now(),  -- cluster event time
  ticker            text        not null,
  cluster_bucket    text,
  direction         text,
  score             numeric(8,3) not null,               -- retained for ranking, NOT the gate
  recall_floor      numeric(8,3) not null,               -- floor in effect (provenance)
  catalyst_score    numeric(8,3),
  context_score     numeric(8,3),
  background_score  numeric(8,3),
  n_events          integer     not null default 0,
  source_agents     text[]      not null default '{}',
  rule_keys         text[]      not null default '{}',   -- constituent rule_key::horizon — 2.b gates on these
  dedup_key         text        not null,                -- e.g. thesis_{ticker}_{bucket}_{yyyymmddHHMM}; idempotency
  breakdown         jsonb       not null default '[]'::jsonb,
  -- reserved for PR-C (2.b / orchestrator); NULL until then:
  gate_decision     jsonb,                               -- per-horizon {act|pass|failopen, pf, n}
  emitted_signal_id bigint,
  meta              jsonb       not null default '{}'::jsonb
);

-- Recent-first scans (pulsecheck, coverage replay, dashboards).
create index if not exists idx_signal_candidates_recent
  on stock_signal_candidates (created_at desc);

-- Per-ticker history.
create index if not exists idx_signal_candidates_ticker
  on stock_signal_candidates (ticker, created_at desc);

-- 2.b reads candidates by the cells they touch (gate lookup); GIN on the array.
create index if not exists idx_signal_candidates_rule_keys
  on stock_signal_candidates using gin (rule_keys);

-- Idempotency support: the agent pre-filters on dedup_key, but a non-unique
-- index keeps that lookup cheap. (Deliberately NOT a unique index: a partial/
-- unique index would tempt a PostgREST ?on_conflict= insert, which fails 42P10
-- — CLAUDE.md rule #2. Dedup stays in the agent, plain INSERT here.)
create index if not exists idx_signal_candidates_dedup
  on stock_signal_candidates (dedup_key, created_at desc);

comment on table stock_signal_candidates is
  'Layer 2.a candidate ledger: one row per cluster clearing the loose recall '
  'floor. Input to the 2.b precision gate. score retained for ranking, not '
  'gating. gate_decision/emitted_signal_id filled by PR-C. See '
  'docs/design/layer2-metalabeling-funnel.md.';
