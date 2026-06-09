# Monthly reconciliation — 2025-12

_Generated 2026-06-04._

Sequential learning replay: $500 weekly deposits, $500 base position size, max 10 concurrent. Each prior month's reconciliation produced rule-level flips, skips, and amplifiers that THIS month's trading respected.

## Month-end state

| Metric | This month | Cumulative |
|---|---|---|
| Deposits | $2,500.00 | $17,500.00 |
| Trading PnL | $42.12 | -$259.37 |
| Opens | 27 | 228 |
| Closes | 28 | 225 |
| Win-rate | 42.9% | 49.3% |
| Skipped (rule banned) | 0 | n/a |
| Flipped-direction trades opened | 5 | n/a |

| Snapshot | Value |
|---|---|
| Cash idle | $16,000.00 |
| Deployed in positions | $1,500.00 (3/10) |
| Cumulative PnL | -$259.37 |
| **Total equity** | **$17,240.63** |
| Return on deposits | -1.48% |
| Max drawdown | $564.89 |

## Reconciliation — learnings produced this month

_No new mature rules this month. Existing carry-forward decisions remain in effect (see 'Active learning state' below)._

## Active learning state (cumulative through this month)

These are ALL the rule-level decisions currently in effect — everything from this month plus carry-forward from prior months.

- **Direction flips active**: 1 rules
  - `earnings_release:beat:h7d`
- **Structural skips active**: 0 rules
- **Amplified rules active**: 0 rules (×1.5)

## Tier population drift (vs prior month)

| Tier | Prior | Current | Δ |
|---|---|---|---|
| `n<30` | 4 | 4 | 0 |
| `child` | 2 | 2 | 0 |
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
