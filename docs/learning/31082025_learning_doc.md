# Learning snapshot — end of 2025-08

_Generated 2026-06-03 from a historical replay of stock_event_paper_trades through 2025-08-31._

Window: realistic_loop_agent semantics applied retroactively to all closed h7d paper trades. $5,000 bankroll, $1,000 per position, max 5 concurrent, cash recycled on close, no leverage. Realized returns already net of 10 bps round-trip slippage (event_paper_agent convention).

## Hypothetical $5K loop state at month-end

| Metric | Value |
|---|---|
| Cash available | $0.00 |
| Positions open | 5 / 5 |
| Cumulative PnL | -$76.57 |
| Return % (vs $5K base) | -1.53% |
| High-water mark | $102.31 |
| Max drawdown | $426.74 (+8.53%) |
| Closed trades | 55 (24W / 31L, win-rate 43.6%) |
| Avg PnL per closed trade | -$1.39 |

## Month-over-month delta

| Metric | This month | Cumulative |
|---|---|---|
| PnL | $209.30 | -$76.57 |
| Opens | 22 | 60 |
| Closes | 22 | 55 |
| Win-rate (in month) | 54.5% | 43.6% |

## Top rule_keys by cumulative PnL (n ≥ 5)

| rule_key | n | wins | win-rate | cumulative PnL |
|---|---|---|---|---|
| `8k_material_event::h7d` | 35 | 16 | 45.7% | -$117.03 |
| `earnings_release:beat:h7d` | 15 | 5 | 33.3% | -$203.74 |

## Mature rules (n ≥ 30) as of 2025-08-31

| rule_key | n | win-rate | cumulative PnL |
|---|---|---|---|
| `8k_material_event::h7d` | 35 | 45.7% | -$117.03 |

## Top tickers by cumulative PnL (n ≥ 3)

| ticker | n | wins | win-rate | cumulative PnL |
|---|---|---|---|---|
| `MA` | 3 | 3 | 100.0% | $75.44 |
| `META` | 4 | 2 | 50.0% | $35.02 |
| `XOM` | 3 | 2 | 66.7% | $23.05 |
| `INTC` | 4 | 2 | 50.0% | -$12.37 |
| `NFLX` | 6 | 2 | 33.3% | -$37.02 |

## Worst tickers by cumulative PnL (n ≥ 3)

| ticker | n | wins | win-rate | cumulative PnL |
|---|---|---|---|---|
| `WMT` | 4 | 2 | 50.0% | -$65.76 |
| `JPM` | 4 | 1 | 25.0% | -$42.53 |
| `NFLX` | 6 | 2 | 33.3% | -$37.02 |
| `INTC` | 4 | 2 | 50.0% | -$12.37 |
| `XOM` | 3 | 2 | 66.7% | $23.05 |

## How to read this doc

This is a *historical replay*, not a live trading record. It answers the question: *had the realistic_loop_agent been active with a $5K bankroll and the discipline we ship today, what would it have made by 2025-08?* Numbers are bounded by the corpus available — h7d horizon, severity ≥ 2 events, ~150-ticker universe. They do not reflect intraday-spike alerts or the maturity-gated BUY/SELL vocabulary (which never triggered during this period — no rule reached the 90%/n≥30 adult gate).

Useful for a future agent reviewing time-indexed learning: load this and the prior month to see drift in rule_n, win-rate, and which tickers were accumulating edge or noise.
