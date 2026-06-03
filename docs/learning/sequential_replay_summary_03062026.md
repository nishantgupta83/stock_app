# Sequential monthly replay — summary (2025-05-01 → 2026-05-31)

_Generated 2026-06-03._

Each month's reconciliation produced flip/skip/amplify decisions that the NEXT month's trading respected. This is the closed-loop version of the historical replay.

## Headline

| Metric | Value |
|---|---|
| Months simulated | 14 |
| Total deposits | $28,000.00 |
| Cumulative trading PnL | $309.33 |
| **Total equity** | **$28,309.33** |
| Effective return on deposits | +1.10% |
| Max drawdown | $575.73 |
| Closed trades | 391 (199W / 192L, win-rate 50.9%) |

## Equity curve by month-end

| Month | Deposits cum | Trading PnL | Equity | Return on dep |
|---|---|---|---|---|
| 2025-05 | $2,000.00 | $47.26 | $2,047.26 | +2.36% |
| 2025-06 | $4,500.00 | -$289.44 | $4,210.56 | -6.43% |
| 2025-07 | $6,500.00 | -$202.26 | $6,297.74 | -3.11% |
| 2025-08 | $8,500.00 | -$98.43 | $8,401.57 | -1.16% |
| 2025-09 | $11,000.00 | -$191.25 | $10,808.75 | -1.74% |
| 2025-10 | $13,000.00 | -$184.21 | $12,815.79 | -1.42% |
| 2025-11 | $15,000.00 | -$301.49 | $14,698.51 | -2.01% |
| 2025-12 | $17,500.00 | -$259.37 | $17,240.63 | -1.48% |
| 2026-01 | $19,500.00 | -$188.82 | $19,311.18 | -0.97% |
| 2026-02 | $21,500.00 | -$338.04 | $21,161.96 | -1.57% |
| 2026-03 | $24,000.00 | -$336.94 | $23,663.06 | -1.40% |
| 2026-04 | $26,000.00 | $228.35 | $26,228.35 | +0.88% |
| 2026-05 | $28,000.00 | $380.33 | $28,380.33 | +1.36% |
| 2026-06 | $28,000.00 | $309.33 | $28,309.33 | +1.10% |

## All reconciliation decisions over the period

### Direction flips applied

| rule_key | flipped at month |
|---|---|
| `earnings_release:beat:h7d` | 2025-08 |

### Structural skips applied

_None._

### Amplifications applied

_None._

## Tier population trajectory

| Month | n<30 | child | teen | young | adult |
|---|---|---|---|---|---|
| 2025-05 | 3 | 0 | 0 | 0 | 0 |
| 2025-06 | 3 | 0 | 0 | 0 | 0 |
| 2025-07 | 3 | 1 | 0 | 0 | 0 |
| 2025-08 | 3 | 2 | 0 | 0 | 0 |
| 2025-09 | 3 | 2 | 0 | 0 | 0 |
| 2025-10 | 3 | 2 | 0 | 0 | 0 |
| 2025-11 | 4 | 2 | 0 | 0 | 0 |
| 2025-12 | 4 | 2 | 0 | 0 | 0 |
| 2026-01 | 6 | 2 | 0 | 0 | 0 |
| 2026-02 | 6 | 2 | 0 | 0 | 0 |
| 2026-03 | 6 | 2 | 0 | 0 | 0 |
| 2026-04 | 6 | 2 | 0 | 0 | 0 |
| 2026-05 | 15 | 2 | 0 | 0 | 0 |
| 2026-06 | 16 | 2 | 0 | 0 | 0 |

## Method

- Window: 2025-05-01 → 2026-05-31
- Weekly deposit: $500 every Monday from 2025-05-05
- Per-position base size: $500, max 10 concurrent
- Flip threshold: PF < 1.0 AND acc < 50% at n ≥ 30
- Skip threshold: acc < 30% at n ≥ 30
- Amplify threshold: PF ≥ 2.0 AND acc ≥ 60% at n ≥ 30, scale ×1.5
- Slippage: already in `realized_return` (10 bps round-trip)
- Horizon: h7d (matches the single-pass DCA replay for comparability)

Re-runnable: `python3 scripts/sequential_monthly_replay.py` (idempotent — overwrites prior docs).
