# Learning docs

Three layers of time-indexed learning artifacts. Each is regeneratable,
none manually edited — when the underlying data changes, re-run the
relevant script and the docs refresh.

## Layer 1 — Monthly reconciliations

Closed-loop sequential replay. Each month's reconciliation produces
rule-level decisions (flips, skips, amplifications) that **carry into
the next month's simulation**. This models how the discipline would
have evolved if we'd been actively learning monthly instead of running
a fixed strategy across the whole window.

| Month | Doc | Source |
|---|---|---|
| 2025-05 | [`202505_monthly_reconc.md`](202505_monthly_reconc.md) | `scripts/sequential_monthly_replay.py` |
| 2025-06 | [`202506_monthly_reconc.md`](202506_monthly_reconc.md) | " |
| 2025-07 | [`202507_monthly_reconc.md`](202507_monthly_reconc.md) | " |
| 2025-08 | [`202508_monthly_reconc.md`](202508_monthly_reconc.md) | " |
| 2025-09 | [`202509_monthly_reconc.md`](202509_monthly_reconc.md) | " |
| 2025-10 | [`202510_monthly_reconc.md`](202510_monthly_reconc.md) | " |
| 2025-11 | [`202511_monthly_reconc.md`](202511_monthly_reconc.md) | " |
| 2025-12 | [`202512_monthly_reconc.md`](202512_monthly_reconc.md) | " |
| 2026-01 | [`202601_monthly_reconc.md`](202601_monthly_reconc.md) | " |
| 2026-02 | [`202602_monthly_reconc.md`](202602_monthly_reconc.md) | " |
| 2026-03 | [`202603_monthly_reconc.md`](202603_monthly_reconc.md) | " |
| 2026-04 | [`202604_monthly_reconc.md`](202604_monthly_reconc.md) | " |
| 2026-05 | [`202605_monthly_reconc.md`](202605_monthly_reconc.md) | " |
| 2026-06 | [`202606_monthly_reconc.md`](202606_monthly_reconc.md) | " |

Each doc captures: end-of-month state, reconciliation decisions made
that night, active carry-forward learning state, tier-population drift.

**Regenerate:** `python3 scripts/sequential_monthly_replay.py`

## Layer 2 — Quarterly reviews (historical)

Quarter-end roll-ups of the monthly reconc data. Show Δ across the
quarter, aggregate of all flips/skips/amplifies, tier-drift trajectory,
top contributing rules.

| Quarter | Doc |
|---|---|
| 2025Q2 | [`2025Q2_quarterly_review.md`](2025Q2_quarterly_review.md) |
| 2025Q3 | [`2025Q3_quarterly_review.md`](2025Q3_quarterly_review.md) |
| 2025Q4 | [`2025Q4_quarterly_review.md`](2025Q4_quarterly_review.md) |
| 2026Q1 | [`2026Q1_quarterly_review.md`](2026Q1_quarterly_review.md) |
| 2026Q2 | [`2026Q2_quarterly_review.md`](2026Q2_quarterly_review.md) |

Auto-generated alongside the monthly docs by the same replay script.

## Layer 3 — Live operational consultant

Independent rule-based analytical agent. Reads:
- The last 3 monthly reconciliations
- Live `stock_rule_calibration`
- Live `stock_health_pulse_current`
- Quarter's closed paper trades

Produces deterministic recommendations (NOT LLM judgments) for what the
operator should ship next: flips to add to a `STRUCTURAL_FLIP` set in
`thesis_agent`, sizing amplifications, etc.

| Quarter | Doc |
|---|---|
| 2026Q1 | [`2026Q1_consultant_review.md`](2026Q1_consultant_review.md) |

**Run on demand:** `python3 scripts/quarterly_consultant_review.py`

## Roll-up / summary

| Doc | What it covers |
|---|---|
| [`sequential_replay_summary_03062026.md`](sequential_replay_summary_03062026.md) | Master roll-up of the 14-month sequential replay: equity curve, all reconciliation decisions, tier-pop trajectory |
| [`dca599_03062026_doc.md`](dca599_03062026_doc.md) | Earlier single-pass DCA replay (no flips/skips applied) — kept for comparison vs the sequential closed-loop version |

## Reading order (for an agent or operator)

1. Start with the **summary** to see the headline (cumulative PnL,
   tier-population end state, total flip/skip/amplify count).
2. Walk the **monthly reconciliations** in chronological order to see
   how each month's decisions changed the next month's behavior.
3. Read the **quarterly reviews** for the "step back" perspective —
   observations + recommendations that wouldn't be visible in one month.
4. Run the **live consultant** to see what the most recent quarter's
   data is telling you to ship next.

## Conventions used across docs

- All amounts are USD, 10 bps round-trip slippage already netted in
  `realized_return`.
- "tier" follows `sql/0031_tiered_maturity.sql` definitions:
  child (n≥30, acc<70%) → teen (acc≥70%) → young (acc≥80%) →
  **adult** (acc≥90% AND PF>1.5).
- Adult is the only tier that unlocks BUY/SELL vocabulary in
  `thesis_agent`. Everything else stays paper-tier (WATCH/RESEARCH/
  AVOID_CHASE/CHASE_RISK).
- All docs are timestamped on generation; re-running the source script
  overwrites with current data.

## What these docs are NOT

- Not advice. The pipeline is a personal-financial-freedom learning
  vehicle, not a managed product.
- Not a backtest. The simulation respects current discipline (max
  concurrent, slippage, maturity gate). Real-money execution would
  introduce friction (broker fees, fills, taxes) we don't model.
- Not LLM-generated. Every threshold (flip, skip, amplify) is a
  deterministic numeric rule defined in the script source. Same inputs
  always produce same outputs.
