-- Allow CHASE_RISK as a first-class paper-trading action and keep the latest
-- status_v2 values in one constraint definition.
--
-- Apply after 0006. The thesis_agent also has a backward-compatible fallback,
-- so live runs do not fail before this migration is applied.

alter table stock_signals drop constraint if exists stock_signals_action_check;
alter table stock_signals
  add constraint stock_signals_action_check
  check (action in ('WATCH','RESEARCH','AVOID_CHASE','CHASE_RISK','BUY','SELL','TRIM'));

alter table stock_signals drop constraint if exists stock_signals_status_v2_check;
alter table stock_signals
  add constraint stock_signals_status_v2_check
  check (status_v2 in ('candidate','sent','suppressed','expired','demoted','backtest','closed'));
