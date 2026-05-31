-- 0032 — sector-aware calibration multiplier (view)
--
-- Backfill analysis on the 540d corpus (7,340 closed paper trades, May 2026)
-- showed material, reproducible dispersion of rule_key accuracy across sectors.
-- Example: 8k_material_event::h7d has 51% accuracy / PF 2.05 overall, but
-- Information Technology cell shows 71% / PF 4.37 (n=308) while Consumer
-- Staples shows 26% / PF 0.88 (n=121). thesis_agent currently scores both
-- identically — this view exposes a per-cell multiplier so future scoring
-- can amplify high-edge cells and dampen low-edge ones.
--
-- Design choices:
--   * View, not materialized view — auto-refreshes from underlying tables.
--     Cost is acceptable: ~7-10k closed trades JOIN ~150 symbols → <100ms.
--   * Multiplier combines accuracy ratio AND profit factor. Either alone is
--     misleading (high accuracy + thin PF = noise; high PF + low n = outlier).
--   * Min sample floor of n=30 per cell — below this, multiplier is 1.0 (no
--     effect). Prevents thin cells from gaining undue leverage.
--   * Bounded multipliers ([0.5, 1.3]) — caps both upside and downside so any
--     single cell can never dominate scoring.
--
-- Consumed by: agents/thesis_agent.py (behind SECTOR_CALIB_MULT_ENABLED flag).

create or replace view stock_rule_sector_multiplier as
with cell_stats as (
  select
    t.rule_key,
    coalesce(s.sector, 'Unknown')                       as sector,
    count(*)                                             as n,
    avg(case when t.correct then 1.0 else 0.0 end)::numeric(6,4)
                                                         as cell_accuracy,
    sum(case when t.realized_return > 0 then t.realized_return else 0 end)
                                                         as gross_wins,
    sum(case when t.realized_return <= 0 then abs(t.realized_return) else 0 end)
                                                         as gross_losses,
    avg(t.realized_return)::numeric(8,6)                 as mean_realized
  from stock_event_paper_trades t
  left join stock_symbols s on s.ticker = t.ticker
  where t.status = 'closed'
    and t.correct is not null
    and t.realized_return is not null
  group by t.rule_key, coalesce(s.sector, 'Unknown')
),
rule_stats as (
  select
    rule_key,
    sum(n)                                               as total_n,
    sum(n * cell_accuracy) / nullif(sum(n), 0)           as base_accuracy
  from cell_stats
  group by rule_key
)
select
  c.rule_key,
  c.sector,
  c.n,
  c.cell_accuracy,
  r.base_accuracy::numeric(6,4)                          as base_accuracy,
  case when r.base_accuracy > 0
       then (c.cell_accuracy / r.base_accuracy)::numeric(6,3)
       else null
  end                                                    as accuracy_ratio,
  case when c.gross_losses > 0
       then (c.gross_wins / c.gross_losses)::numeric(8,3)
       else null
  end                                                    as cell_profit_factor,
  c.mean_realized,
  -- Multiplier: only triggers when (a) cell has enough samples, (b) ratio is
  -- meaningfully off-baseline, AND (c) PF agrees. Returns 1.0 (no effect)
  -- in every other case.
  case
    when c.n < 30                                            then 1.0
    when r.base_accuracy <= 0                                then 1.0
    when c.gross_losses = 0                                  then 1.0
    when (c.cell_accuracy / r.base_accuracy) >= 1.5
         and (c.gross_wins / nullif(c.gross_losses, 0)) >= 1.5
                                                             then 1.3
    when (c.cell_accuracy / r.base_accuracy) <= 0.5
         or  (c.gross_wins / nullif(c.gross_losses, 0)) <= 0.5
                                                             then 0.5
    else 1.0
  end                                                    as multiplier
from cell_stats c
join rule_stats r on r.rule_key = c.rule_key;

comment on view stock_rule_sector_multiplier is
  'Per-(rule_key, sector) calibration multiplier for thesis_agent. '
  'Floors at n>=30 per cell. Bounded [0.5, 1.3]. Auto-refreshes from '
  'stock_event_paper_trades. Feature-flagged on the consumer side '
  '(SECTOR_CALIB_MULT_ENABLED).';
