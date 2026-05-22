-- 0030_brier_calibration.sql
--
-- Adds probabilistic-calibration metrics to stock_rule_calibration so the
-- learning loop can tell "this rule is 70% accurate" apart from "this rule
-- claims 70% accuracy and the outcomes match that claim." Accuracy alone
-- can't surface the second question — a rule that always predicts the same
-- side will have a meaningful accuracy number that says nothing about
-- whether its confidence is honest.
--
-- brier_30d = mean((accuracy - outcome)^2) across the rule's closed paper
-- trades in the last 30 days. Lower is better. Compared against the
-- floor of accuracy*(1-accuracy) — Brier should approach that floor for
-- a well-calibrated rule. Brier above the floor indicates the rule's
-- confidence overstates its true probability.
--
-- n_closed_30d powers the drift detector — gap between rolling 30d
-- accuracy and lifetime accuracy is the trend signal that static
-- maturity gates can't see.
--
-- Both columns are nullable so rule_calibration rows from before this
-- migration remain readable; price_agent recomputes them on every
-- close-out cycle.

alter table stock_rule_calibration
  add column if not exists brier_30d                numeric,
  add column if not exists accuracy_30d             numeric,
  add column if not exists n_closed_30d             integer,
  add column if not exists last_brier_recomputed_at timestamptz;

comment on column stock_rule_calibration.brier_30d is
  'Mean Brier score over the last 30 days of closed paper trades for this rule. predicted_prob = current accuracy; outcome ∈ {0,1}. Lower is better; floor is accuracy*(1-accuracy). NULL until n_closed_30d >= 5.';

comment on column stock_rule_calibration.accuracy_30d is
  'Rolling 30-day accuracy. Gap from lifetime accuracy is the drift signal — large gap means the rule is in a different regime than its history.';

comment on column stock_rule_calibration.n_closed_30d is
  'Count of closed paper trades for this rule in the last 30 days. Used to gate brier_30d (below n=5 the metric is noise).';

comment on column stock_rule_calibration.last_brier_recomputed_at is
  'Stamped by price_agent after each batch close-out so the calibration UI can show staleness when the daily recompute hasn''t run.';
