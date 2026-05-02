-- Phase 6B: separate true live paper forecasts from historical shadow replay.
--
-- live             = generated from live candidate/sent/suppressed signals.
-- shadow_backtest  = generated from audited backtest signals for UI validation
--                    and calibration review. Shadow rows must not be counted as
--                    real live paper-trading performance.

alter table stock_paper_forecasts
  add column if not exists forecast_mode text not null default 'live'
    check (forecast_mode in ('live', 'shadow_backtest'));

create index if not exists stock_paper_forecasts_mode_created_idx
  on stock_paper_forecasts (forecast_mode, created_at desc);

create index if not exists stock_paper_forecasts_mode_status_idx
  on stock_paper_forecasts (forecast_mode, status, created_at desc);
