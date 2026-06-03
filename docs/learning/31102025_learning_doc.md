# Learning snapshot — end of 2025-10

_Generated 2026-06-03 from a historical replay of stock_event_paper_trades through 2025-10-31._

Window: realistic_loop_agent semantics applied retroactively to all closed h7d paper trades. $5,000 bankroll, $1,000 per position, max 5 concurrent, cash recycled on close, no leverage. Realized returns already net of 10 bps round-trip slippage (event_paper_agent convention).

## Hypothetical $5K loop state at month-end

| Metric | Value |
|---|---|
| Cash available | $0.00 |
| Positions open | 5 / 5 |
| Cumulative PnL | $770.40 |
| Return % (vs $5K base) | +15.41% |
| High-water mark | $851.14 |
| Max drawdown | $426.74 (+8.53%) |
| Closed trades | 93 (49W / 44L, win-rate 52.7%) |
| Avg PnL per closed trade | $8.28 |

## Month-over-month delta

| Metric | This month | Cumulative |
|---|---|---|
| PnL | $231.08 | $770.40 |
| Opens | 20 | 98 |
| Closes | 20 | 93 |
| Win-rate (in month) | 60.0% | 52.7% |

## Top rule_keys by cumulative PnL (n ≥ 5)

| rule_key | n | wins | win-rate | cumulative PnL |
|---|---|---|---|---|
| `8k_material_event::h7d` | 58 | 31 | 53.4% | $350.65 |
| `earnings_release:miss:h7d` | 7 | 5 | 71.4% | $270.68 |
| `earnings_release:beat:h7d` | 26 | 12 | 46.2% | $206.28 |

## Mature rules (n ≥ 30) as of 2025-10-31

| rule_key | n | win-rate | cumulative PnL |
|---|---|---|---|
| `8k_material_event::h7d` | 58 | 53.4% | $350.65 |

## Rules approaching maturity (20 ≤ n < 30)

| rule_key | n | wins | win-rate |
|---|---|---|---|
| `earnings_release:beat:h7d` | 26 | 12 | 46.2% |

## Top tickers by cumulative PnL (n ≥ 3)

| ticker | n | wins | win-rate | cumulative PnL |
|---|---|---|---|---|
| `INTC` | 7 | 5 | 71.4% | $453.72 |
| `AVGO` | 4 | 3 | 75.0% | $125.18 |
| `AMD` | 3 | 3 | 100.0% | $104.54 |
| `MA` | 3 | 3 | 100.0% | $75.44 |
| `TSLA` | 3 | 2 | 66.7% | $61.95 |

## Worst tickers by cumulative PnL (n ≥ 3)

| ticker | n | wins | win-rate | cumulative PnL |
|---|---|---|---|---|
| `JPM` | 8 | 3 | 37.5% | -$99.37 |
| `WMT` | 6 | 4 | 66.7% | -$59.96 |
| `NFLX` | 9 | 3 | 33.3% | -$54.32 |
| `COST` | 3 | 0 | 0.0% | -$33.82 |
| `XOM` | 4 | 2 | 50.0% | $4.81 |

## How to read this doc

This is a *historical replay*, not a live trading record. It answers the question: *had the realistic_loop_agent been active with a $5K bankroll and the discipline we ship today, what would it have made by 2025-10?* Numbers are bounded by the corpus available — h7d horizon, severity ≥ 2 events, ~150-ticker universe. They do not reflect intraday-spike alerts or the maturity-gated BUY/SELL vocabulary (which never triggered during this period — no rule reached the 90%/n≥30 adult gate).

Useful for a future agent reviewing time-indexed learning: load this and the prior month to see drift in rule_n, win-rate, and which tickers were accumulating edge or noise.
