# Pre-registration — `qr_s1_v1`: dip-only quarterly rotation vs a same-universe random-pick null

**Status:** FROZEN at the commit that adds this file, before any result was computed. Parameters
live in `scripts/quarterly_rotation/experiment.py::ExperimentConfig`; `config_hash` covers them,
the universe, the calendar ticker and the experiment id. Any change = new version (`qr_s1_v2`),
never an edit. **Paper / research only. No capital.** Touches no Supabase table and no Telegram;
reads only yfinance daily bars. Isolated from `ai_humanoid_screen`, `paper_book` and every
`stock_*` table.

## Question (one)

> Does buying, at each quarter-end, the AI-universe names in the Bollinger dip band
> (%B < 0.20) beat buying the **same number of names drawn at random from the same universe**,
> net of costs, quarter after quarter?

This is the smallest experiment that can falsify the premise behind the quarterly-rotation
proposal (Strategies 0–6). Layers 200D / momentum / regime / two-sleeve portfolio are NOT built
and are not reachable unless this promotes.

## Method (frozen)

- **Calendar:** ONE reference calendar — the session dates of `QQQ` — for every asset. Never a
  per-asset date list. (`_market_calendar` only covers 2026–27, so `ai_humanoid_screen._trading_axis`
  cannot be reused for history; this is the documented substitute.)
- **Decision dates:** the last session of each **completed** calendar quarter
  (`methodology.rebalance_dates`; the current unfinished quarter is excluded).
- **Eligible name on decision date R:** has a bar on R, ≥ 252 closes strictly before R, all 20 calendar sessions ending R present (the %B window comes from the shared calendar), and open prices on
  both the entry and exit session. A name missing either is dropped from that cohort, never
  zero-filled.
- **Signal:** 20-day, 2σ, population-std %B on closes through R; dip = %B < 0.20. Fixed. No
  threshold grid, no other horizon.
- **Trade:** dip set, equal weight, entry at the **open of the session after R**, exit at the
  **open of the session after the next quarter-end**. Prices are yfinance auto-adjusted.
- **Costs:** 0.20% round trip (10 bps per side), charged to S1 and to every null draw alike.
- **Cohort:** one completed quarter with ≥ 8 eligible names and ≥ 1 dip name. Quarters with no
  dip name are reported (`no_signal_quarters`), not counted, and not filled with cash.
- **Quarter excess:** `mean(dip returns) − 0.20% − mean(all eligible returns)`.
- **Null:** for each signal quarter draw the same *number* of names uniformly without
  replacement from that quarter's eligible set; statistic = mean quarter excess; 2000 draws;
  `random.Random(20260924)`. `p = (1 + #draws ≥ observed) / (1 + 2000)`.
- **Independent unit:** the quarter (non-overlapping holds). Not the name-quarter.

## Primary metric (one)

Mean net quarter excess vs the same-universe equal-weight hold, judged by its percentile /
p-value against the permutation null.

## Promotion rule — ALL six must hold (numbers fixed before the run)

| # | check | threshold |
|---|---|---|
| P1 | permutation p-value | ≤ 0.05 |
| P2 | mean net excess per signal quarter | ≥ +1.0 pts |
| P3 | signal quarters | ≥ 20 |
| P4 | quarters with excess > 0 | ≥ 55% |
| P5 | mean excess with the single best quarter removed | > 0 |
| P6 | mean excess in each half of the signal quarters (chronological) | ≥ 0 |

If all hold: S1 is worth **one** further pre-registered layer, forward and paper. If any fails:
**no promotion** — the result is "no evidence the dip rule adds anything over random picks in
this universe", which is a legitimate outcome and ends this line. No re-run with a different
threshold, horizon, cost or universe under this id.

## What this can and cannot say

- **Hindsight universe.** The names are today's AI leaders, chosen knowing they won. Absolute
  returns here are meaningless. The null draws from the same names, which removes the
  level bias, but NOT the survivorship that is specific to a dip rule: names that dipped and
  never came back are absent by construction, so a positive result is biased upward for dip
  entries. A null result is unaffected; a promotion would still need forward confirmation.
- **Sample.** ~10 years ≈ 38 quarters, ≈ 25–35 with a dip. Modest power: a real +1 pt/quarter
  edge can be missed. Reported as such, not tuned around.
- **Trials.** Configurations tried under this id: **1**. Reported in the result.
- **Durability gate (≥ 80% positive 504-day windows, needs ≥ 2016 prior sessions) is NOT part of
  this backtest.** A 10-year download has 2,513 sessions, so the gate is point-in-time
  measurable only for the last ~2 years; computing it on the full sample would use future data.
  The result reports how many name-quarters were measurable (`durability_measurable_name_quarters`
  of `eligible_name_quarters`). The gate can be graded **forward only**, or with ≥ 20 years of
  history — separately pre-registered.
- **Taxes and fund pricing are out of scope** for this stock-only experiment: no
  short-term-gain drag (quarterly holds realise short-term gains in a taxable account) and no
  NAV-priced mutual-fund sleeve. A promotion here would still owe both before any use.
- **No regime, no momentum, no 200D layer.** Those are v2+ and require this to promote first.

## Outputs

`scripts/quarterly_rotation/results/qr_s1_v1.json` — verdict checks, per-quarter excess,
null mean / p95 / percentile, data end date, tickers loaded/missing. Reproduce:
`python3 scripts/quarterly_rotation/experiment.py` (data ends move with the run date; the
statistic is deterministic for a given dataset).
