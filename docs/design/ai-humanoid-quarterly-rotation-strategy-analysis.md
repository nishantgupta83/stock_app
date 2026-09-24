# AI/Humanoid Quarterly Rotation — Strategy 0–6 Analysis

Branch: feat/ai-humanoid-quarterly-rotation

## Purpose

The current AI/Humanoid screen is the immutable control. We will test seven progressively more complex strategies rather than assuming complexity improves results.

| Strategy | Name | Core question |
|---|---|---|
| 0 | Existing AI/Humanoid Screen | Can the current methodology be reproduced and validated? |
| 1 | Dip Only | Does Bollinger %B alone create an edge? |
| 2 | Dip + Historical Durability | Does the 2-year historical gate add value? |
| 3 | Dip + Trend | Does the 200D relationship improve dip entries? |
| 4 | Dip + Momentum | Does momentum confirmation help or create chase risk? |
| 5 | Regime-Aware | Does market regime change the value of a dip? |
| 6 | Full Quarterly Rotation | Can the surviving rules become a practical quarterly portfolio? |

## Strategy 0 — Existing AI/Humanoid Screen

Reproduce the current screen exactly: 20-day Bollinger Bands, 2 standard deviations, %B below 0.20 as the dip signal, %B above 1.00 as extended, 200D SMA context, 504-session historical horizon, minimum four independent windows, >=80% positive historical windows, P10 >= -10%, $2M liquidity gate, next-session execution, and special handling for leveraged/inverse products.

The existing research reports roughly +21.42% for dip observations versus +17.95% for owning the complex and +25.43% for downtrend plus dip over its stated 60-session study. These are hypotheses to reproduce, not validated conclusions.

Questions: does the result survive quarterly decisions, transaction costs, walk-forward testing and appropriate benchmarks? Controls are SPY, QQQ, VTI and SMH where appropriate.

## Strategy 1 — Dip Only

Hypothesis: most useful information is simply mean reversion from an unusually weak recent price position.

Signal: %B < 0.20. Remove historical durability, 200D and momentum.

Test thresholds %B < 0.30, 0.20, 0.10 and 0.00, with threshold selection restricted to training periods.

If Strategy 1 is close to Strategy 0, much of the original complexity may be unnecessary. If it materially underperforms, the removed filters may contain useful information.

Risk: a Bollinger dip can be either a temporary pullback or a genuine breakdown.

## Strategy 2 — Dip + Historical Durability

Hypothesis: a dip is more attractive when the asset has demonstrated favorable historical two-year behavior.

Keep the dip signal plus the 504-session history, four-independent-window minimum, 80% positive-window requirement and -10% P10 floor.

Test alternate thresholds inside training data: positive-window floors of 70%, 80% and 90%; P10 floors of -20%, -10% and 0%.

Overlapping observations are not independent experiments. Report raw observations and independent windows separately.

The filter is useful if it improves drawdown, downside, consistency or risk-adjusted return without excessive turnover; higher raw return is not required.

## Strategy 3 — Dip + Trend

Hypothesis: the reported strength of downtrend plus dip is genuine.

Primary test: %B < 0.20 AND close < 200D SMA.

Also measure distance from 200D: above +10%, +5% to +10%, 0% to +5%, 0% to -5%, -5% to -10%, below -10%.

Key question: is the edge coming from healthy assets temporarily below trend, or deeply damaged assets experiencing mean reversion?

Measure maximum drawdown, recovery time, and percentage of entries still below entry after 60/90/126 sessions.

Compare dip plus below-200D with dip plus above-200D rather than assuming either is superior.

## Strategy 4 — Dip + Momentum

Hypothesis: momentum may either confirm a dip or create chase risk.

Test 3-, 6- and 12-month momentum and relative strength versus SPY, QQQ and SMH where relevant.

4A — confirmation: accept dips only when momentum is above the universe median.

4B — ranking: accept all dips but increase rank for stronger momentum.

4C — extreme filter: avoid highly extended momentum.

The existing screen reports that top-quartile 12-month momentum underperformed simply owning the complex. Strategy 4 therefore tests momentum as a possible risk-control variable rather than assuming it is a buy signal.

## Strategy 5 — Regime-Aware

Hypothesis: the meaning of a dip changes with the broader market environment.

Initial inputs: QQQ versus 200D, SPY versus 200D, 20D/200D slope, stable historical breadth if available, and a volatility proxy.

Initial states: Risk-on, Neutral, Risk-off and Recovery.

Potential behavior: normal dip acceptance in Risk-on; stronger evidence in Neutral; reduced exposure or restricted entries in Risk-off; gradual restoration in Recovery.

This is not a market prediction model. It tests whether signal acceptance or position sizing should depend on observed market state. Regime rules must be frozen before final testing and evaluated on unseen periods.

## Strategy 6 — Full Quarterly Rotation

Objective: convert only the components that survive Strategies 0–5 into a practical quarterly portfolio.

Quarterly workflow:

1. Freeze information at quarter-end.
2. Establish market regime.
3. Screen stocks and Fidelity-usable funds/ETFs separately.
4. Calculate only validated signals: %B, 200D, durability, momentum, volatility, liquidity and relative strength.
5. Rank candidates using only components that demonstrated out-of-sample value.
6. Construct the portfolio.
7. Hold until the next scheduled rebalance.
8. Measure return, drawdown, turnover, costs and benchmark-relative performance.

Initial research range: 5–10 individual stocks and 2–5 funds/ETFs with position limits. Leveraged/inverse products stay outside the standard model unless separately validated.

Fund sleeve: controlled Fidelity-compatible universe covering broad market, technology/semiconductors, AI/robotics, defensive and diversifying exposures.

Stock sleeve: retain existing AI/humanoid categories so results can be decomposed into compute, memory, equipment, power/grid, networking, platforms and humanoid supply chain.

An individual-stock signal is not assumed to transfer directly to an ETF; both sleeves are tested separately.

## Backtest Integrity

Every strategy must enforce no look-ahead, first available session after the decision date for execution, point-in-time universe where feasible, explicit survivorship/delisting treatment, consistent adjusted prices, transaction costs/slippage, quarterly decision dates, train/test separation, walk-forward evaluation, untouched final test period, and independent quarterly cohorts reported separately from overlapping daily observations.

## Walk-Forward Design

Use TRAIN → FREEZE RULES → TEST → MOVE FORWARD.

Illustrative structure: 2018–2021 train / 2022 test; 2019–2022 train / 2023 test; 2020–2023 train / 2024 test; 2021–2024 train / 2025 test; 2022–2025 train / 2026 current test. Actual dates depend on clean data availability.

## Metrics for all strategies

Return: cumulative return, annualized return, quarterly mean and median.

Risk: maximum drawdown, volatility, downside deviation, Sharpe, Sortino, worst quarter, longest recovery.

Consistency: quarterly win rate, positive-year rate, profit factor, median winner and loser.

Efficiency: turnover, transaction-cost drag, percentage of quarters invested.

Benchmark-relative: excess versus SPY, QQQ, VTI and SMH where appropriate.

Evidence quality: number of quarters, independent quarterly cohorts, number of names, sector concentration, top-five contribution, return excluding best quarter, and return excluding best three trades.

## Comparison

| Strategy | Main question | Return | Max DD | Sharpe | Win % | Turnover | Benchmark excess |
|---|---|---:|---:|---:|---:|---:|---:|
| 0 — Existing screen | Reproduce current method | TBD | TBD | TBD | TBD | TBD | TBD |
| 1 — Dip only | Does the dip work? | TBD | TBD | TBD | TBD | TBD | TBD |
| 2 — Durability | Does history add information? | TBD | TBD | TBD | TBD | TBD | TBD |
| 3 — Trend | Does 200D add information? | TBD | TBD | TBD | TBD | TBD | TBD |
| 4 — Momentum | Does momentum help or hurt? | TBD | TBD | TBD | TBD | TBD | TBD |
| 5 — Regime | Does market context help? | TBD | TBD | TBD | TBD | TBD | TBD |
| 6 — Quarterly rotation | Can it become a usable portfolio? | TBD | TBD | TBD | TBD | TBD | TBD |

TBD is intentional until the historical engine produces reproducible results.

## Promotion Criteria

A strategy advances only if it shows positive out-of-sample evidence, improvement versus its immediate simpler predecessor, acceptable drawdown, reasonable turnover, robustness after costs, no single stock or quarter explaining most of the result, performance across multiple market regimes, and no look-ahead or survivorship leakage.

A higher raw return alone is insufficient.

Core principle: every additional layer must prove that it adds information. Complexity is not an advantage by itself.
