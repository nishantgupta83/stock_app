# Learning snapshot — end of 2025-09

_Generated 2026-06-03 from a historical replay of stock_event_paper_trades through 2025-09-30._

Window: realistic_loop_agent semantics applied retroactively to all closed h7d paper trades. $5,000 bankroll, $1,000 per position, max 5 concurrent, cash recycled on close, no leverage. Realized returns already net of 10 bps round-trip slippage (event_paper_agent convention).

## Hypothetical $5K loop state at month-end

| Metric | Value |
|---|---|
| Cash available | $0.00 |
| Positions open | 5 / 5 |
| Cumulative PnL | $539.32 |
| Return % (vs $5K base) | +10.79% |
| High-water mark | $546.72 |
| Max drawdown | $426.74 (+8.53%) |
| Closed trades | 73 (37W / 36L, win-rate 50.7%) |
| Avg PnL per closed trade | $7.39 |

## Month-over-month delta

| Metric | This month | Cumulative |
|---|---|---|
| PnL | $615.88 | $539.32 |
| Opens | 18 | 78 |
| Closes | 18 | 73 |
| Win-rate (in month) | 72.2% | 50.7% |

## Top rule_keys by cumulative PnL (n ≥ 5)

| rule_key | n | wins | win-rate | cumulative PnL |
|---|---|---|---|---|
| `earnings_release:miss:h7d` | 6 | 4 | 66.7% | $257.39 |
| `8k_material_event::h7d` | 45 | 23 | 51.1% | $240.34 |
| `earnings_release:beat:h7d` | 20 | 9 | 45.0% | $98.80 |

## Mature rules (n ≥ 30) as of 2025-09-30

| rule_key | n | win-rate | cumulative PnL |
|---|---|---|---|
| `8k_material_event::h7d` | 45 | 51.1% | $240.34 |

## Rules approaching maturity (20 ≤ n < 30)

| rule_key | n | wins | win-rate |
|---|---|---|---|
| `earnings_release:beat:h7d` | 20 | 9 | 45.0% |

## Top tickers by cumulative PnL (n ≥ 3)

| ticker | n | wins | win-rate | cumulative PnL |
|---|---|---|---|---|
| `INTC` | 6 | 4 | 66.7% | $346.82 |
| `AVGO` | 3 | 2 | 66.7% | $106.48 |
| `MA` | 3 | 3 | 100.0% | $75.44 |
| `META` | 4 | 2 | 50.0% | $35.02 |
| `XOM` | 3 | 2 | 66.7% | $23.05 |

## Worst tickers by cumulative PnL (n ≥ 3)

| ticker | n | wins | win-rate | cumulative PnL |
|---|---|---|---|---|
| `WMT` | 6 | 4 | 66.7% | -$59.96 |
| `NFLX` | 6 | 2 | 33.3% | -$37.02 |
| `JPM` | 6 | 3 | 50.0% | -$21.61 |
| `XOM` | 3 | 2 | 66.7% | $23.05 |
| `META` | 4 | 2 | 50.0% | $35.02 |

## How to read this doc

This is a *historical replay*, not a live trading record. It answers the question: *had the realistic_loop_agent been active with a $5K bankroll and the discipline we ship today, what would it have made by 2025-09?* Numbers are bounded by the corpus available — h7d horizon, severity ≥ 2 events, ~150-ticker universe. They do not reflect intraday-spike alerts or the maturity-gated BUY/SELL vocabulary (which never triggered during this period — no rule reached the 90%/n≥30 adult gate).

Useful for a future agent reviewing time-indexed learning: load this and the prior month to see drift in rule_n, win-rate, and which tickers were accumulating edge or noise.
