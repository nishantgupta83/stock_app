# Learning snapshot — end of 2025-11

_Generated 2026-06-03 from a historical replay of stock_event_paper_trades through 2025-11-30._

Window: realistic_loop_agent semantics applied retroactively to all closed h7d paper trades. $5,000 bankroll, $1,000 per position, max 5 concurrent, cash recycled on close, no leverage. Realized returns already net of 10 bps round-trip slippage (event_paper_agent convention).

## Hypothetical $5K loop state at month-end

| Metric | Value |
|---|---|
| Cash available | $5,000.00 |
| Positions open | 0 / 5 |
| Cumulative PnL | $662.75 |
| Return % (vs $5K base) | +13.26% |
| High-water mark | $851.14 |
| Max drawdown | $426.74 (+8.53%) |
| Closed trades | 113 (59W / 54L, win-rate 52.2%) |
| Avg PnL per closed trade | $5.87 |

## Month-over-month delta

| Metric | This month | Cumulative |
|---|---|---|
| PnL | -$107.65 | $662.75 |
| Opens | 15 | 113 |
| Closes | 20 | 113 |
| Win-rate (in month) | 50.0% | 52.2% |

## Top rule_keys by cumulative PnL (n ≥ 5)

| rule_key | n | wins | win-rate | cumulative PnL |
|---|---|---|---|---|
| `earnings_release:miss:h7d` | 7 | 5 | 71.4% | $270.68 |
| `8k_material_event::h7d` | 66 | 34 | 51.5% | $217.27 |
| `earnings_release:beat:h7d` | 36 | 17 | 47.2% | $155.67 |

## Mature rules (n ≥ 30) as of 2025-11-30

| rule_key | n | win-rate | cumulative PnL |
|---|---|---|---|
| `8k_material_event::h7d` | 66 | 51.5% | $217.27 |
| `earnings_release:beat:h7d` | 36 | 47.2% | $155.67 |

## Top tickers by cumulative PnL (n ≥ 3)

| ticker | n | wins | win-rate | cumulative PnL |
|---|---|---|---|---|
| `INTC` | 8 | 5 | 62.5% | $388.95 |
| `AMD` | 6 | 6 | 100.0% | $131.50 |
| `AVGO` | 4 | 3 | 75.0% | $125.18 |
| `TSLA` | 3 | 2 | 66.7% | $61.95 |
| `META` | 4 | 2 | 50.0% | $35.02 |

## Worst tickers by cumulative PnL (n ≥ 3)

| ticker | n | wins | win-rate | cumulative PnL |
|---|---|---|---|---|
| `JPM` | 8 | 3 | 37.5% | -$99.37 |
| `NVDA` | 3 | 0 | 0.0% | -$90.53 |
| `DJT` | 3 | 1 | 33.3% | -$67.14 |
| `NFLX` | 9 | 3 | 33.3% | -$54.32 |
| `COST` | 3 | 0 | 0.0% | -$33.82 |

## How to read this doc

This is a *historical replay*, not a live trading record. It answers the question: *had the realistic_loop_agent been active with a $5K bankroll and the discipline we ship today, what would it have made by 2025-11?* Numbers are bounded by the corpus available — h7d horizon, severity ≥ 2 events, ~150-ticker universe. They do not reflect intraday-spike alerts or the maturity-gated BUY/SELL vocabulary (which never triggered during this period — no rule reached the 90%/n≥30 adult gate).

Useful for a future agent reviewing time-indexed learning: load this and the prior month to see drift in rule_n, win-rate, and which tickers were accumulating edge or noise.
