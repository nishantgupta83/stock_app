-- 0031_tiered_maturity.sql
--
-- Stage-gate maturity: extends stock_rule_calibration from a single 90%
-- maturity gate into three tiers (teen 70%, young_adult 80%, adult 90%).
-- The existing is_mature column keeps its current 90% semantics — it stays
-- the canonical BUY/SELL gate (thesis_agent.py:1132-1136 reads it for
-- vocabulary unlocking). The new columns sit alongside.
--
-- v1 promotion gates require accuracy + payoff sanity:
--   teen        : n >= 30  AND accuracy >= 0.70  AND mean_realized_pct > 0
--   young_adult : n >= 30  AND accuracy >= 0.80  AND profit_factor > 1.2
--   adult       : n >= 30  AND accuracy >= 0.90  AND profit_factor > 1.5
--                              (additional avg_loss_pct constraint deferred to Phase 4)
--
-- Why payoff on top of accuracy: a rule can be 82% accurate and still lose
-- money if the 18% wrong are large losses. Accuracy alone overstates trust.
-- The v1 gate names sit in scripts/learning_snapshot.py:TIER_GATES so the
-- Phase 3 application code can import the same constants.
--
-- This migration is purely additive: new columns default to safe values,
-- backfill UPDATE runs once in the same transaction, no existing column
-- is renamed/dropped. Consumers that SELECT is_mature continue to work
-- unchanged. The new tier column is derived; the only writer is
-- price_agent.upsert_calibration() (after Phase 3).
--
-- Schema risk audit:
--   • No partial indexes added (CLAUDE.md rule #2 against PostgREST 42P10)
--   • No unique constraints added
--   • New columns are NULL or have a safe default
--   • The backfill UPDATE touches every row but runs INSIDE this transaction
--     so it's atomic with the column additions

begin;

alter table stock_rule_calibration
  add column if not exists is_mature_70   boolean      default false,
  add column if not exists is_mature_80   boolean      default false,
  add column if not exists matured_70_at  timestamptz,
  add column if not exists matured_80_at  timestamptz,
  add column if not exists tier           text;

comment on column stock_rule_calibration.is_mature is
  'Canonical 90% production maturity flag (accuracy >= 0.90 AND n_observations >= 30); '
  'retained for backward compatibility. See is_mature_70 / is_mature_80 for the tiered '
  'gates added 2026-05-26. Under v1 gates, also requires profit_factor > 1.5 — see '
  'scripts/learning_snapshot.py:TIER_GATES for the canonical thresholds.';

comment on column stock_rule_calibration.is_mature_70 is
  'Teen-tier promotion flag. v1: n >= 30 AND accuracy >= 0.70 AND mean_realized_pct > 0. '
  'Does NOT unlock BUY/SELL — that remains gated on is_mature (90%). Affects sizing '
  'multiplier in risk_agent (0.5x) and eligibility for the realistic-sizing parallel '
  'loop (sql/0032).';

comment on column stock_rule_calibration.is_mature_80 is
  'Young-adult tier promotion flag. v1: n >= 30 AND accuracy >= 0.80 AND '
  'profit_factor > 1.2. Affects risk_agent sizing multiplier (0.75x) and realistic-loop '
  'tier budget split.';

comment on column stock_rule_calibration.matured_70_at is
  'First crossing of the teen threshold. Self-heals on subsequent upserts if NULL '
  'while is_mature_70 = true (matching the matured_at pattern in price_agent.py:512-518).';

comment on column stock_rule_calibration.matured_80_at is
  'First crossing of the young_adult threshold. Same self-heal pattern as matured_70_at.';

comment on column stock_rule_calibration.tier is
  'Derived label: child | teen | young_adult | adult. Computed by '
  'price_agent.upsert_calibration() on every reconciliation. Audit invariant: tier '
  'must equal the highest-passed gate among (is_mature, is_mature_80, is_mature_70). '
  'Drift check runs in audit_agent (Phase 5).';

-- Backfill existing rows using v1 gates. NULL profit_factor / mean_realized_pct
-- count as failing the payoff requirement (conservative — better to label a rule
-- 'child' until it has enough closed trades to compute its payoff metrics).
update stock_rule_calibration
   set is_mature_70 = (
         coalesce(n_observations, 0) >= 30
         and coalesce(accuracy, 0) >= 0.70
         and mean_realized_pct is not null
         and mean_realized_pct > 0
       ),
       is_mature_80 = (
         coalesce(n_observations, 0) >= 30
         and coalesce(accuracy, 0) >= 0.80
         and profit_factor is not null
         and profit_factor > 1.2
       );

-- Stamp matured_at timestamps for any row that backfilled to true.
update stock_rule_calibration
   set matured_70_at = coalesce(matured_70_at, now())
 where is_mature_70 = true and matured_70_at is null;

update stock_rule_calibration
   set matured_80_at = coalesce(matured_80_at, now())
 where is_mature_80 = true and matured_80_at is null;

-- Derive tier from the flags. Note: under v1 gates the existing is_mature
-- flag may be true while the v1 adult definition (which also requires
-- profit_factor > 1.5) is false. We honor the existing is_mature here so
-- the BUY/SELL gate behavior is unchanged by this migration. Phase 3 will
-- tighten is_mature itself to include the payoff sanity.
update stock_rule_calibration
   set tier = case
                when is_mature                                        then 'adult'
                when is_mature_80                                     then 'young_adult'
                when is_mature_70                                     then 'teen'
                else                                                       'child'
              end;

commit;
