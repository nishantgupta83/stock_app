# Quarterly consultant review — 2026Q1

_Generated 2026-06-04. Window: 2026-01-01 → 2026-03-31._

**Independent automated review.** Reads the last 3 monthly reconciliations, the live `stock_rule_calibration` table, the pulsecheck dashboard, and closed paper trades for the quarter. Produces deterministic recommendations using the same thresholds the monthly reconciler uses. **Not LLM-generated — no judgment calls beyond the stated thresholds.**

Feeds back into the pipeline as: a list of concrete rule-level actions the operator can ship next week. These are NOT auto-applied — the consultant proposes; the operator decides.

## Quarter at a glance

| Metric | Value |
|---|---|
| Closed trades in window | 776 |
| Wins / Losses | 354 / 422 |
| Aggregate win-rate | 45.6% |
| Sum of realized return % across all trades | -367.2% |
| Distinct rule_keys producing trades | 28 |

## Pipeline health snapshot

| Status | Count |
|---|---|
| ok | 24 |
| warning | 3 |

### Active warnings/criticals

| Agent | Check | Status | Detail |
|---|---|---|---|
| `pulsecheck_news` | `classifier_neutrality` | warning | 24h neutral share: 214/248 (86%) |
| `pulsecheck_realistic_loop` | `input_starvation` | warning | null-reason setups in last 5d: 0 |
| `pulsecheck_thesis` | `rejection_distribution` | warning | 24h rejections: 8522 total, dominant=cluster_passes (98 |

## Recommended actions (data-driven)

### Direction flips (5 rules cross PF<1.0 AND acc<50% at n≥30)

Adding these to a STRUCTURAL_FLIP set in `agents/thesis_agent.py` and feature-flagging would invert their direction. The evidence is they are losing money in the original direction with enough sample to trust the verdict.

| rule_key | n | acc | PF |
|---|---|---|---|
| `filing_13g::h1d` | 95 | 33.7% | 0.34 |
| `earnings_release:miss:h30d` | 124 | 46.8% | 0.57 |
| `earnings_release:miss:h15d` | 145 | 47.6% | 0.65 |
| `8k_material_event::h1d` | 1168 | 45.3% | 0.67 |
| `earnings_release:beat:h1d` | 474 | 43.2% | 0.77 |

### Structural skips (1 rules cross acc<30% at n≥30)

These rule_keys have severely low accuracy with significant sample. Both directions are losing — better to NOT emit signals on these at all.

| rule_key | n | acc | PF |
|---|---|---|---|
| `filing_13d::h30d` | 30 | 13.3% | 4.14 |

### Amplifications (8 rules cross PF≥2.0 AND acc≥60% at n≥30)

Consider raising live position size on these in `agents/risk_agent.py` (or letting them through dedupe more freely). Profit factor ≥ 2 with reasonable accuracy means wins are durable.

| rule_key | n | acc | PF |
|---|---|---|---|
| `clinical_readout:active_not_recruiting:h15d` | 33 | 90.9% | 9.33 |
| `truth_social_post:tariff_general:h15d` | 30 | 70.0% | 7.80 |
| `truth_social_post:tariff_general:h7d` | 42 | 73.8% | 4.40 |
| `clinical_readout:active_not_recruiting:h1d` | 248 | 69.8% | 3.14 |
| `8k_material_event::h15d` | 1155 | 68.8% | 2.81 |
| `filing_13g::h15d` | 89 | 66.3% | 2.37 |
| `truth_social_post:tariff_general:h1d` | 42 | 69.0% | 2.24 |
| `news_article:negative:h1d` | 31 | 71.0% | 2.07 |

## Top rule_keys by quarterly activity (n ≥ 5 in window)

| rule_key | n (in quarter) | wins | win-rate | sum realized return % |
|---|---|---|---|---|
| `filing_13g::h15d` | 15 | 14 | 93.3% | 67.0% |
| `8k_material_event::h7d` | 101 | 53 | 52.5% | 47.9% |
| `8k_material_event::h1d` | 101 | 63 | 62.4% | 17.0% |
| `filing_13d::h1d` | 13 | 10 | 76.9% | 4.3% |
| `filing_13g::h7d` | 15 | 10 | 66.7% | -0.8% |
| `earnings_release:beat:h7d` | 44 | 23 | 52.3% | -7.9% |
| `filing_13g::h30d` | 15 | 5 | 33.3% | -11.2% |
| `earnings_release:miss:h7d` | 16 | 7 | 43.8% | -14.5% |
| `earnings_release:miss:h30d` | 16 | 8 | 50.0% | -15.0% |
| `earnings_release:miss:h15d` | 15 | 7 | 46.7% | -15.2% |

## Source chronology

Monthly docs read for this review (most recent first):

- [`202606_monthly_reconc.md`](202606_monthly_reconc.md)
- [`202605_monthly_reconc.md`](202605_monthly_reconc.md)
- [`202604_monthly_reconc.md`](202604_monthly_reconc.md)

## How to action this

1. Pick one recommended action from above (start with flips — highest ROI per code change).
2. Add the rule_key to the relevant set in `agents/thesis_agent.py` or `agents/risk_agent.py`.
3. Gate behind a feature flag (e.g., `STRUCTURAL_FLIP_ENABLED`).
4. Push, set the secret to `true`, watch the relevant pulsecheck.
5. Re-run this consultant in 2 weeks to confirm impact.

**This consultant runs deterministically** — same inputs produce the same outputs. There's no surprise. Re-run any time:

```bash
python3 scripts/quarterly_consultant_review.py
```
