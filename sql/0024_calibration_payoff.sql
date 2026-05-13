-- 0024_calibration_payoff.sql
--
-- Extends the calibration layer beyond accuracy. Today, stock_rule_calibration
-- knows ONLY how often a rule is "right" (accuracy). It cannot distinguish a
-- 55% rule with 5:1 win/loss ratio from a 55% rule with 1:5 — the first prints
-- money, the second drains it. This migration adds payoff metrics so the
-- learning layer reflects tradeable edge, not just hit rate.
--
-- Also adds per-trade MFE/MAE + stop/target hit booleans on
-- stock_event_paper_trades. price_agent.reconcile_event_paper_trades populates
-- these from daily H/L bars during reconciliation — the "daily-HL audit"
-- approximation of the intraday stop/target check (item #9 in the backlog;
-- a full intraday audit requires 1-min or 5-min bars which we don't ingest yet).
--
-- Stage 4 of the trading-pipeline backlog. All columns nullable so previously
-- closed trades and their existing calibration rows remain readable.

-- Per-trade payoff record + intraday-ish audit columns
alter table stock_event_paper_trades
  add column if not exists mfe_pct      numeric,
  add column if not exists mae_pct      numeric,
  add column if not exists target_hit   boolean,
  add column if not exists stop_hit     boolean;

comment on column stock_event_paper_trades.mfe_pct is
  'Max Favorable Excursion as a fraction of entry price. Positive: how far in our favor the price moved at its best between entry and exit.';

comment on column stock_event_paper_trades.mae_pct is
  'Max Adverse Excursion as a fraction of entry price. Negative: how far against us the price moved at its worst between entry and exit.';

comment on column stock_event_paper_trades.target_hit is
  'True if at any session between entry and exit the daily High (long) / Low (short) breached the target_pct band. Approximation of the intraday-audit answer using daily bars.';

comment on column stock_event_paper_trades.stop_hit is
  'True if at any session between entry and exit the daily Low (long) / High (short) breached the stop_pct band. Approximation of the intraday-audit answer using daily bars.';

-- Per-rule payoff aggregates. profit_factor and avg_win/loss let the maturity
-- gate distinguish "right often" from "tradeable." Future risk_agent reads
-- these to size positions.
alter table stock_rule_calibration
  add column if not exists median_return_pct  numeric,
  add column if not exists avg_win_pct        numeric,
  add column if not exists avg_loss_pct       numeric,
  add column if not exists profit_factor      numeric,
  add column if not exists target_hit_rate    numeric,
  add column if not exists stop_hit_rate      numeric,
  add column if not exists mean_mfe_pct       numeric,
  add column if not exists mean_mae_pct       numeric,
  add column if not exists last_payoff_recomputed_at timestamptz;

comment on column stock_rule_calibration.profit_factor is
  'sum(winning_returns) / abs(sum(losing_returns)). >1 means the rule prints money even with losses; ~1 means break-even; <1 means it bleeds. NULL until enough closed trades exist.';

comment on column stock_rule_calibration.avg_win_pct is
  'Mean realized_return across CORRECT closed trades. Pairs with avg_loss_pct to give the payoff shape.';

comment on column stock_rule_calibration.avg_loss_pct is
  'Mean realized_return across INCORRECT closed trades (typically negative).';

comment on column stock_rule_calibration.target_hit_rate is
  'Fraction of closed trades whose target_pct band was touched intra-holding. Validates whether targets are realistically reachable.';

comment on column stock_rule_calibration.stop_hit_rate is
  'Fraction of closed trades whose stop_pct band was touched intra-holding. Validates whether stops are too tight (high rate = whipsaw risk).';
