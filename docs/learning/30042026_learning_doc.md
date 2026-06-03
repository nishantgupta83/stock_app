# Learning snapshot — end of 2026-04

_Generated 2026-06-03 from a historical replay of stock_event_paper_trades through 2026-04-30._

Window: realistic_loop_agent semantics applied retroactively to all closed h7d paper trades. $5,000 bankroll, $1,000 per position, max 5 concurrent, cash recycled on close, no leverage. Realized returns already net of 10 bps round-trip slippage (event_paper_agent convention).

## Hypothetical $5K loop state at month-end

| Metric | Value |
|---|---|
| Cash available | $0.00 |
| Positions open | 5 / 5 |
| Cumulative PnL | $885.75 |
| Return % (vs $5K base) | +17.71% |
| High-water mark | $906.28 |
| Max drawdown | $580.19 (+11.60%) |
| Closed trades | 199 (102W / 97L, win-rate 51.3%) |
| Avg PnL per closed trade | $4.45 |

## Month-over-month delta

| Metric | This month | Cumulative |
|---|---|---|
| PnL | $614.81 | $885.75 |
| Opens | 21 | 204 |
| Closes | 19 | 199 |
| Win-rate (in month) | 68.4% | 51.3% |

## Top rule_keys by cumulative PnL (n ≥ 5)

| rule_key | n | wins | win-rate | cumulative PnL |
|---|---|---|---|---|
| `8k_material_event::h7d` | 118 | 63 | 53.4% | $809.09 |
| `earnings_release:miss:h7d` | 13 | 7 | 53.8% | $263.59 |
| `earnings_release:beat:h7d` | 56 | 26 | 46.4% | -$74.01 |

## Mature rules (n ≥ 30) as of 2026-04-30

| rule_key | n | win-rate | cumulative PnL |
|---|---|---|---|
| `8k_material_event::h7d` | 118 | 53.4% | $809.09 |
| `earnings_release:beat:h7d` | 56 | 46.4% | -$74.01 |

## Top tickers by cumulative PnL (n ≥ 3)

| ticker | n | wins | win-rate | cumulative PnL |
|---|---|---|---|---|
| `INTC` | 11 | 8 | 72.7% | $546.13 |
| `AMD` | 12 | 12 | 100.0% | $396.15 |
| `AVGO` | 11 | 5 | 45.5% | $304.22 |
| `TSLA` | 4 | 3 | 75.0% | $82.11 |
| `V` | 4 | 3 | 75.0% | $49.23 |

## Worst tickers by cumulative PnL (n ≥ 3)

| ticker | n | wins | win-rate | cumulative PnL |
|---|---|---|---|---|
| `DJT` | 5 | 1 | 20.0% | -$192.14 |
| `JPM` | 13 | 5 | 38.5% | -$152.27 |
| `META` | 9 | 3 | 33.3% | -$136.35 |
| `NVDA` | 3 | 0 | 0.0% | -$90.53 |
| `AAPL` | 10 | 3 | 30.0% | -$70.02 |

## How to read this doc

This is a *historical replay*, not a live trading record. It answers the question: *had the realistic_loop_agent been active with a $5K bankroll and the discipline we ship today, what would it have made by 2026-04?* Numbers are bounded by the corpus available — h7d horizon, severity ≥ 2 events, ~150-ticker universe. They do not reflect intraday-spike alerts or the maturity-gated BUY/SELL vocabulary (which never triggered during this period — no rule reached the 90%/n≥30 adult gate).

Useful for a future agent reviewing time-indexed learning: load this and the prior month to see drift in rule_n, win-rate, and which tickers were accumulating edge or noise.
