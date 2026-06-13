-- 0041 — effective-n (H1): independent-evidence calibration stats
--
-- Calibration n over-counts: one market move fans into many (ticker, entry-day)
-- paper trades for the same rule (measured 2.2-3.8x on the adult rules). The
-- maturity gate (which licenses BUY/SELL) must run on EFFECTIVE evidence — one
-- observation per (ticker, entry-day) cluster, the cluster's representative
-- outcome = the MEAN of its trades' realized_return.
--
-- These columns persist the collapsed stats so EVERY gate path (live
-- recompute_rule_payoff, the recompute_maturity_flags script, the risk_agent
-- tier fallback, the dashboard) reads the same effective numbers instead of raw
-- n. is_mature/is_mature_70/is_mature_80/tier are derived from these once
-- populated. Raw n_observations / accuracy / mean_realized_pct / profit_factor
-- are kept unchanged for reference + audit.
--
-- All columns are NULLABLE so the upsert path is backward-compatible until the
-- agent code that writes them is deployed (the writer guards its effective-*
-- write so a pre-migration run never fails).

ALTER TABLE stock_rule_calibration
    ADD COLUMN IF NOT EXISTS effective_n                 integer,
    ADD COLUMN IF NOT EXISTS effective_n_correct         integer,
    ADD COLUMN IF NOT EXISTS effective_accuracy          double precision,
    ADD COLUMN IF NOT EXISTS effective_mean_realized_pct double precision,
    ADD COLUMN IF NOT EXISTS effective_profit_factor     double precision;

COMMENT ON COLUMN stock_rule_calibration.effective_n IS
    'H1: count of distinct (ticker, entry-day) clusters — independent-evidence n. '
    'The maturity gate runs on this, not n_observations (which over-counts 2-4x).';
