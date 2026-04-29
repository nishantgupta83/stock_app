-- Add 'closed' to stock_signals.status_v2 check constraint.
-- price_agent marks signals closed after EOD reconciliation.

alter table stock_signals drop constraint if exists stock_signals_status_v2_check;
alter table stock_signals
  add constraint stock_signals_status_v2_check
  check (status_v2 in ('candidate','sent','suppressed','expired','demoted','backtest','closed'));
