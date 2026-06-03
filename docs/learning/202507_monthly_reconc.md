# Monthly reconciliation — 2025-07

_Generated 2026-06-03._

Sequential learning replay: $500 weekly deposits, $500 base position size, max 10 concurrent. Each prior month's reconciliation produced rule-level flips, skips, and amplifiers that THIS month's trading respected.

## Month-end state

| Metric | This month | Cumulative |
|---|---|---|
| Deposits | $2,000.00 | $6,500.00 |
| Trading PnL | $87.18 | -$202.26 |
| Opens | 40 | 67 |
| Closes | 37 | 57 |
| Win-rate | 51.4% | 47.4% |
| Skipped (rule banned) | 0 | n/a |
| Flipped-direction trades opened | 0 | n/a |

| Snapshot | Value |
|---|---|
| Cash idle | $1,500.00 |
| Deployed in positions | $5,000.00 (10/10) |
| Cumulative PnL | -$202.26 |
| **Total equity** | **$6,297.74** |
| Return on deposits | -3.11% |
| Max drawdown | $337.94 |

## Reconciliation — learnings produced this month

### Newly mature (n crossed 30)

| rule_key | tier on maturity |
|---|---|
| `8k_material_event::h7d` | child |

## Active learning state (cumulative through this month)

These are ALL the rule-level decisions currently in effect — everything from this month plus carry-forward from prior months.

- **Direction flips active**: 0 rules
- **Structural skips active**: 0 rules
- **Amplified rules active**: 0 rules (×1.5)

## Tier population drift (vs prior month)

| Tier | Prior | Current | Δ |
|---|---|---|---|
| `n<30` | 3 | 3 | 0 |
| `child` | 0 | 1 | +1 |
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
