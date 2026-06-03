# Learning snapshot — end of 2026-01

_Generated 2026-06-03 from a historical replay of stock_event_paper_trades through 2026-01-31._

Window: realistic_loop_agent semantics applied retroactively to all closed h7d paper trades. $5,000 bankroll, $1,000 per position, max 5 concurrent, cash recycled on close, no leverage. Realized returns already net of 10 bps round-trip slippage (event_paper_agent convention).

## Hypothetical $5K loop state at month-end

| Metric | Value |
|---|---|
| Cash available | $0.00 |
| Positions open | 5 / 5 |
| Cumulative PnL | $574.49 |
| Return % (vs $5K base) | +11.49% |
| High-water mark | $851.14 |
| Max drawdown | $426.74 (+8.53%) |
| Closed trades | 144 (74W / 70L, win-rate 51.4%) |
| Avg PnL per closed trade | $3.99 |

## Month-over-month delta

| Metric | This month | Cumulative |
|---|---|---|
| PnL | $144.11 | $574.49 |
| Opens | 18 | 149 |
| Closes | 16 | 144 |
| Win-rate (in month) | 62.5% | 51.4% |

## Top rule_keys by cumulative PnL (n ≥ 5)

| rule_key | n | wins | win-rate | cumulative PnL |
|---|---|---|---|---|
| `earnings_release:miss:h7d` | 9 | 7 | 77.8% | $458.32 |
| `8k_material_event::h7d` | 86 | 44 | 51.2% | $164.57 |
| `earnings_release:beat:h7d` | 41 | 19 | 46.3% | -$14.16 |

## Mature rules (n ≥ 30) as of 2026-01-31

| rule_key | n | win-rate | cumulative PnL |
|---|---|---|---|
| `8k_material_event::h7d` | 86 | 51.2% | $164.57 |
| `earnings_release:beat:h7d` | 41 | 46.3% | -$14.16 |

## Top tickers by cumulative PnL (n ≥ 3)

| ticker | n | wins | win-rate | cumulative PnL |
|---|---|---|---|---|
| `INTC` | 10 | 7 | 70.0% | $493.47 |
| `AMD` | 8 | 8 | 100.0% | $155.04 |
| `XOM` | 7 | 4 | 57.1% | $84.50 |
| `TSLA` | 3 | 2 | 66.7% | $61.95 |
| `META` | 7 | 3 | 42.9% | $48.13 |

## Worst tickers by cumulative PnL (n ≥ 3)

| ticker | n | wins | win-rate | cumulative PnL |
|---|---|---|---|---|
| `DJT` | 5 | 1 | 20.0% | -$192.14 |
| `JPM` | 12 | 4 | 33.3% | -$158.74 |
| `AAPL` | 5 | 0 | 0.0% | -$90.56 |
| `NVDA` | 3 | 0 | 0.0% | -$90.53 |
| `AVGO` | 8 | 3 | 37.5% | -$70.57 |

## How to read this doc

This is a *historical replay*, not a live trading record. It answers the question: *had the realistic_loop_agent been active with a $5K bankroll and the discipline we ship today, what would it have made by 2026-01?* Numbers are bounded by the corpus available — h7d horizon, severity ≥ 2 events, ~150-ticker universe. They do not reflect intraday-spike alerts or the maturity-gated BUY/SELL vocabulary (which never triggered during this period — no rule reached the 90%/n≥30 adult gate).

Useful for a future agent reviewing time-indexed learning: load this and the prior month to see drift in rule_n, win-rate, and which tickers were accumulating edge or noise.
