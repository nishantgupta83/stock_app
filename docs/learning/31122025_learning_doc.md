# Learning snapshot — end of 2025-12

_Generated 2026-06-03 from a historical replay of stock_event_paper_trades through 2025-12-31._

Window: realistic_loop_agent semantics applied retroactively to all closed h7d paper trades. $5,000 bankroll, $1,000 per position, max 5 concurrent, cash recycled on close, no leverage. Realized returns already net of 10 bps round-trip slippage (event_paper_agent convention).

## Hypothetical $5K loop state at month-end

| Metric | Value |
|---|---|
| Cash available | $2,000.00 |
| Positions open | 3 / 5 |
| Cumulative PnL | $430.38 |
| Return % (vs $5K base) | +8.61% |
| High-water mark | $851.14 |
| Max drawdown | $426.74 (+8.53%) |
| Closed trades | 128 (64W / 64L, win-rate 50.0%) |
| Avg PnL per closed trade | $3.36 |

## Month-over-month delta

| Metric | This month | Cumulative |
|---|---|---|
| PnL | -$232.37 | $430.38 |
| Opens | 18 | 131 |
| Closes | 15 | 128 |
| Win-rate (in month) | 33.3% | 50.0% |

## Top rule_keys by cumulative PnL (n ≥ 5)

| rule_key | n | wins | win-rate | cumulative PnL |
|---|---|---|---|---|
| `earnings_release:miss:h7d` | 8 | 6 | 75.0% | $439.39 |
| `8k_material_event::h7d` | 75 | 36 | 48.0% | -$13.98 |
| `earnings_release:beat:h7d` | 41 | 19 | 46.3% | -$14.16 |

## Mature rules (n ≥ 30) as of 2025-12-31

| rule_key | n | win-rate | cumulative PnL |
|---|---|---|---|
| `8k_material_event::h7d` | 75 | 48.0% | -$13.98 |
| `earnings_release:beat:h7d` | 41 | 46.3% | -$14.16 |

## Top tickers by cumulative PnL (n ≥ 3)

| ticker | n | wins | win-rate | cumulative PnL |
|---|---|---|---|---|
| `INTC` | 8 | 5 | 62.5% | $388.95 |
| `AMD` | 6 | 6 | 100.0% | $131.50 |
| `TSLA` | 3 | 2 | 66.7% | $61.95 |
| `META` | 7 | 3 | 42.9% | $48.13 |
| `JNJ` | 3 | 3 | 100.0% | $24.78 |

## Worst tickers by cumulative PnL (n ≥ 3)

| ticker | n | wins | win-rate | cumulative PnL |
|---|---|---|---|---|
| `DJT` | 5 | 1 | 20.0% | -$192.14 |
| `JPM` | 8 | 3 | 37.5% | -$99.37 |
| `NVDA` | 3 | 0 | 0.0% | -$90.53 |
| `COST` | 4 | 0 | 0.0% | -$66.44 |
| `NFLX` | 10 | 4 | 40.0% | -$51.33 |

## How to read this doc

This is a *historical replay*, not a live trading record. It answers the question: *had the realistic_loop_agent been active with a $5K bankroll and the discipline we ship today, what would it have made by 2025-12?* Numbers are bounded by the corpus available — h7d horizon, severity ≥ 2 events, ~150-ticker universe. They do not reflect intraday-spike alerts or the maturity-gated BUY/SELL vocabulary (which never triggered during this period — no rule reached the 90%/n≥30 adult gate).

Useful for a future agent reviewing time-indexed learning: load this and the prior month to see drift in rule_n, win-rate, and which tickers were accumulating edge or noise.
