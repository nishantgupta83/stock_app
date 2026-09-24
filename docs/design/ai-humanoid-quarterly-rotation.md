# AI/Humanoid Quarterly Rotation — isolated research branch

**Branch:** `feat/ai-humanoid-quarterly-rotation`  
**Scope:** Reverse-engineer only the methodology behind `ai_humanoid_screen`; do not modify the existing screen, dashboard, Supabase pipeline, or other agents.

## Objective

Turn the existing AI/humanoid screen into a **quarterly rotation research model** that can compare:

1. the current screen methodology as the immutable baseline;
2. simplified variants of each signal;
3. a regime-aware quarterly model;
4. a mixed universe of Fidelity-usable funds/ETFs and individual stocks.

The purpose of this branch is measurement first. No result is treated as a recommendation until it survives out-of-sample testing.

## Existing methodology to preserve as Strategy 0

The current screen uses:

- 20-day Bollinger Bands, 2 standard deviations;
- `%B < 0.20` as the dip condition;
- `%B > 1.00` as the extended condition;
- price vs 200-day SMA as descriptive context;
- 504-session (~2-year) forward-history statistics;
- at least 4 independent 504-session windows for the historical hold gate;
- historical positive-window rate >= 80%;
- historical 10th percentile 2-year return >= -10%;
- $2M median daily dollar-volume liquidity gate;
- separate handling for daily-reset leveraged/inverse products;
- next-session entry for forward-return measurement.

The existing `ai_humanoid_screen.py` remains untouched.

## Research questions

### R1 — Does the dip signal actually add value?

Compare:

- `%B < 0.20`
- `%B < 0.10`
- `%B < 0.00`
- no dip filter
- random/periodic quarterly entry

Horizons: 20, 60, 90, 126 and 252 trading days.

### R2 — Does the 200-day condition add value?

Test the dip condition split by:

- above 200D;
- below 200D;
- distance from 200D buckets.

Do not assume that below 200D is better simply because the original screen reports a stronger historical bucket.

### R3 — Does the historical hold gate add value?

Test the screen with:

- no hold gate;
- positive-window >= 70%, 80%, 90%;
- P10 >= -20%, -10%, 0%;
- combinations.

The purpose is to discover whether this is a useful predictor or merely a volatility/quality filter.

### R4 — Does momentum hurt or help?

Test 12-month momentum as:

- excluded;
- confirmation;
- penalty for extreme momentum;
- independent rank.

This directly tests the original screen's claim that momentum chasing underperformed its null.

### R5 — Does regime change the result?

Define the regime only from information available at the rebalance date. Initial regime candidates:

- SPY vs 200D;
- QQQ vs 200D;
- 20D/200D slope;
- market breadth if a stable historical breadth series is available;
- volatility proxy.

No future data may enter the regime label.

### R6 — Can the methodology work on a quarterly schedule?

At each quarter-end:

1. calculate signals using data available through that date;
2. rank eligible assets;
3. form the portfolio;
4. hold until the next scheduled rebalance;
5. charge modeled transaction costs/slippage;
6. record cash if fewer than the target number of assets qualify.

There are no intra-quarter signal changes in the core quarterly strategy.

## Universe design

Keep universes separate so the model cannot hide a category effect.

### Fund/ETF sleeve

A controlled list of Fidelity-usable funds/ETFs, including Fidelity funds and liquid ETFs available through Fidelity.

### Individual-stock sleeve

The existing AI/humanoid universe from the screen, plus explicitly defined benchmark/control names.

### Benchmark sleeve

At minimum:

- SPY
- QQQ
- VTI
- SMH

Benchmarks are controls, not candidates that are silently mixed into the stock ranking.

### Leveraged/inverse products

SOXL/SOXS and similar daily-reset products are excluded from the ordinary signal model unless a separate leveraged-product study proves that the same signal has meaning there.

## Backtest integrity rules

Every experiment must enforce:

- no look-ahead;
- next-session execution;
- point-in-time universe where feasible;
- no use of today's holdings to represent historical ETF composition without disclosure;
- split/dividend-adjusted prices consistently;
- explicit transaction costs;
- delisted/failed names handled where data permits;
- quarterly decision dates;
- train/test separation;
- walk-forward or expanding-window evaluation;
- untouched final test period;
- raw observations and independent quarterly cohorts reported separately.

Overlapping daily observations must not be presented as independent evidence.

## Planned experiment matrix

### Strategy 0 — Existing screen

Exact reproduction.

### Strategy 1 — Dip-only

Only `%B` condition.

### Strategy 2 — Dip + hold durability

Current screen's historical gate.

### Strategy 3 — Dip + trend

Dip + 200D relationship.

### Strategy 4 — Dip + momentum

Dip + 12M momentum confirmation.

### Strategy 5 — Regime-aware

Dip + durability + market regime.

### Strategy 6 — Quarterly portfolio

Regime + signal ranking + portfolio construction, with fund and stock sleeves.

No strategy is allowed to inherit thresholds from a later strategy during comparison. Parameters are frozen per experiment.

## Metrics

For every strategy:

- CAGR / annualized return;
- cumulative return;
- maximum drawdown;
- quarterly win rate;
- worst quarter;
- median quarter;
- volatility;
- Sharpe;
- Sortino;
- profit factor;
- turnover;
- transaction-cost drag;
- percentage of quarters invested;
- excess return vs SPY;
- excess return vs QQQ;
- excess return vs a simple quarterly buy-and-hold control.

Also report the number of independent quarterly cohorts.

## Promotion rule

A change is retained only when it improves out-of-sample behavior without creating an unacceptable increase in drawdown/turnover.

The system must be able to conclude:

> "The original rule was better; keep it."

This is a research branch, not an optimization exercise.

## Implementation boundary

New code belongs under:

`scripts/quarterly_rotation/`  
`tests/test_quarterly_rotation_*.py`  
`docs/design/`

Do **not** edit:

- `scripts/ai_humanoid_screen.py`
- `scripts/ai_humanoid_render.py`
- `ai_humanoid/`
- existing agents;
- existing Supabase migrations;
- existing production workflows.

A future UI/deployment workflow should be added only after the research engine has reproducible results.
