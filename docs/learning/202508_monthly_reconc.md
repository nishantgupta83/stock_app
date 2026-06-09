# Monthly reconciliation — 2025-08

_Generated 2026-06-04._

Sequential learning replay: $500 weekly deposits, $500 base position size, max 10 concurrent. Each prior month's reconciliation produced rule-level flips, skips, and amplifiers that THIS month's trading respected.

## Month-end state

| Metric | This month | Cumulative |
|---|---|---|
| Deposits | $2,000.00 | $8,500.00 |
| Trading PnL | $103.83 | -$98.43 |
| Opens | 37 | 104 |
| Closes | 38 | 95 |
| Win-rate | 50.0% | 48.4% |
| Skipped (rule banned) | 0 | n/a |
| Flipped-direction trades opened | 0 | n/a |

| Snapshot | Value |
|---|---|
| Cash idle | $4,000.00 |
| Deployed in positions | $4,500.00 (9/10) |
| Cumulative PnL | -$98.43 |
| **Total equity** | **$8,401.57** |
| Return on deposits | -1.16% |
| Max drawdown | $337.94 |

## Reconciliation — learnings produced this month

### Newly mature (n crossed 30)

| rule_key | tier on maturity |
|---|---|
| `earnings_release:beat:h7d` | child |

### Direction flips applied (PF < 1.0 AND acc < 50%)

From next month onward, trades on these rule_keys will be opened with INVERTED direction. Evidence at flip time:

| rule_key | n | acc | PF | cum PnL |
|---|---|---|---|---|
| `earnings_release:beat:h7d` | 31 | 41.9% | 0.80 | -$71.83 |

## Active learning state (cumulative through this month)

These are ALL the rule-level decisions currently in effect — everything from this month plus carry-forward from prior months.

- **Direction flips active**: 1 rules
  - `earnings_release:beat:h7d`
- **Structural skips active**: 0 rules
- **Amplified rules active**: 0 rules (×1.5)

## Tier population drift (vs prior month)

| Tier | Prior | Current | Δ |
|---|---|---|---|
| `n<30` | 3 | 3 | 0 |
| `child` | 1 | 2 | +1 |
| `teen` | 0 | 0 | 0 |
| `young` | 0 | 0 | 0 |
| `adult` | 0 | 0 | 0 |

## Top rules by cumulative PnL (n ≥ 10)

| rule_key | n | win-rate | PF | cum PnL |
|---|---|---|---|---|
| `8k_material_event::h7d` | 216 | 51.9% | 1.34 | $577.57 |
| `earnings_release:beat:h7d` | 97 | 51.5% | 1.15 | $164.20 |
| `earnings_release:miss:h7d` | 29 | 48.3% | 0.56 | -$305.56 |

## How to read this doc

This snapshot was produced AT the end of the month named in the title. The 'Reconciliation' section shows the new decisions made that night; those decisions then governed the NEXT month's trading. The 'Active learning state' section shows the full carry-forward set including all prior months' decisions.
