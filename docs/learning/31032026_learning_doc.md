# Learning snapshot — end of 2026-03

_Generated 2026-06-03 from a historical replay of stock_event_paper_trades through 2026-03-31._

Window: realistic_loop_agent semantics applied retroactively to all closed h7d paper trades. $5,000 bankroll, $1,000 per position, max 5 concurrent, cash recycled on close, no leverage. Realized returns already net of 10 bps round-trip slippage (event_paper_agent convention).

## Hypothetical $5K loop state at month-end

| Metric | Value |
|---|---|
| Cash available | $2,000.00 |
| Positions open | 3 / 5 |
| Cumulative PnL | $270.95 |
| Return % (vs $5K base) | +5.42% |
| High-water mark | $851.14 |
| Max drawdown | $580.19 (+11.60%) |
| Closed trades | 180 (89W / 91L, win-rate 49.4%) |
| Avg PnL per closed trade | $1.51 |

## Month-over-month delta

| Metric | This month | Cumulative |
|---|---|---|
| PnL | -$269.65 | $270.95 |
| Opens | 16 | 183 |
| Closes | 16 | 180 |
| Win-rate (in month) | 25.0% | 49.4% |

## Top rule_keys by cumulative PnL (n ≥ 5)

| rule_key | n | wins | win-rate | cumulative PnL |
|---|---|---|---|---|
| `earnings_release:miss:h7d` | 13 | 7 | 53.8% | $263.59 |
| `8k_material_event::h7d` | 104 | 53 | 51.0% | $203.63 |
| `earnings_release:beat:h7d` | 52 | 24 | 46.2% | -$76.89 |

## Mature rules (n ≥ 30) as of 2026-03-31

| rule_key | n | win-rate | cumulative PnL |
|---|---|---|---|
| `8k_material_event::h7d` | 104 | 51.0% | $203.63 |
| `earnings_release:beat:h7d` | 52 | 46.2% | -$76.89 |

## Top tickers by cumulative PnL (n ≥ 3)

| ticker | n | wins | win-rate | cumulative PnL |
|---|---|---|---|---|
| `INTC` | 11 | 8 | 72.7% | $546.13 |
| `AMD` | 12 | 12 | 100.0% | $396.15 |
| `XOM` | 7 | 4 | 57.1% | $84.50 |
| `TSLA` | 3 | 2 | 66.7% | $61.95 |
| `V` | 4 | 3 | 75.0% | $49.23 |

## Worst tickers by cumulative PnL (n ≥ 3)

| ticker | n | wins | win-rate | cumulative PnL |
|---|---|---|---|---|
| `DJT` | 5 | 1 | 20.0% | -$192.14 |
| `JPM` | 12 | 4 | 33.3% | -$158.74 |
| `GOOG` | 3 | 0 | 0.0% | -$137.75 |
| `META` | 9 | 3 | 33.3% | -$136.35 |
| `AAPL` | 8 | 1 | 12.5% | -$104.13 |

## How to read this doc

This is a *historical replay*, not a live trading record. It answers the question: *had the realistic_loop_agent been active with a $5K bankroll and the discipline we ship today, what would it have made by 2026-03?* Numbers are bounded by the corpus available — h7d horizon, severity ≥ 2 events, ~150-ticker universe. They do not reflect intraday-spike alerts or the maturity-gated BUY/SELL vocabulary (which never triggered during this period — no rule reached the 90%/n≥30 adult gate).

Useful for a future agent reviewing time-indexed learning: load this and the prior month to see drift in rule_n, win-rate, and which tickers were accumulating edge or noise.
