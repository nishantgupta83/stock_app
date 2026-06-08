-- 0040 — extend stock_signals.action CHECK to the post-PR1A vocabulary
--
-- ROOT-CAUSE FIX (2026-06-08). thesis_agent.action_for has returned
-- CATALYST_WATCH / CATALYST_RESEARCH / MOMENTUM_ONLY since PR1A (2026-05-22,
-- the causal-attribution policy), but stock_signals_action_check only allowed
-- the legacy set (WATCH/RESEARCH/AVOID_CHASE/CHASE_RISK/BUY/SELL/TRIM). Every
-- signal carrying a new-vocabulary action was SILENTLY rejected by the DB:
-- write_signal() does `if status not in (200,201): return None` with a fallback
-- only for CHASE_RISK, so the insert failure was swallowed and Layer 2 emitted
-- 0 rows for those actions. Evidence: 0 CATALYST_*/MOMENTUM_ONLY rows have ever
-- existed in stock_signals; only old-vocab actions (and maturity-gated SELL)
-- ever landed. This was the binding cause of the Layer-2 "silence" that
-- survived the recall-floor + cluster-override fixes (those were upstream of
-- this DB gate).
--
-- Widening a CHECK constraint is safe: it only ADDS permitted values; every
-- existing row already satisfies the narrower set, so no row can violate the
-- wider one. Idempotent: drop-if-exists then add.

alter table stock_signals
  drop constraint if exists stock_signals_action_check;

alter table stock_signals
  add constraint stock_signals_action_check
  check (action = any (array[
    -- legacy vocabulary (pre-PR1A)
    'WATCH', 'RESEARCH', 'AVOID_CHASE', 'CHASE_RISK', 'BUY', 'SELL', 'TRIM',
    -- causal-attribution vocabulary (PR1A, 2026-05-22) — emitted by action_for
    'CATALYST_WATCH', 'CATALYST_RESEARCH', 'MOMENTUM_ONLY'
  ]::text[]));

comment on constraint stock_signals_action_check on stock_signals is
  'Allowed signal actions. Must stay in sync with thesis_agent.action_for + the '
  'chase-risk downgrade. Extended 2026-06-08 (0040) to add the PR1A '
  'CATALYST_*/MOMENTUM_ONLY vocabulary that was being silently rejected.';
