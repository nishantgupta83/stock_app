-- 0028_trade_setups_target_source.sql
-- B1: trade_setup_agent now derives target_pct/stop_pct from each rule's
-- mean_mfe_pct / mean_mae_pct once the rule has ≥10 closed paper trades.
-- target_source records which logic produced the row so the dashboard and
-- audit_agent can tell at a glance whether a setup was sized from defaults
-- or from calibration data.
--
-- Values: 'default' (no calibration) | 'calibrated' (adaptive from MFE/MAE).

alter table stock_trade_setups
  add column if not exists target_source text;

comment on column stock_trade_setups.target_source is
  'How target_pct/stop_pct were derived: default | calibrated. Adaptive when rule_key has n_observations >= 10 and non-null mean_mfe_pct / mean_mae_pct.';

-- Backfill existing rows so the column isn't NULL on legacy data.
update stock_trade_setups
  set target_source = 'default'
  where target_source is null;
