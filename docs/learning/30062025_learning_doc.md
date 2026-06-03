# Learning snapshot — end of 2025-06

_Generated 2026-06-03 from a historical replay of stock_event_paper_trades through 2025-06-30._

Window: realistic_loop_agent semantics applied retroactively to all closed h7d paper trades. $5,000 bankroll, $1,000 per position, max 5 concurrent, cash recycled on close, no leverage. Realized returns already net of 10 bps round-trip slippage (event_paper_agent convention).

## Hypothetical $5K loop state at month-end

| Metric | Value |
|---|---|
| Cash available | $0.00 |
| Positions open | 5 / 5 |
| Cumulative PnL | -$146.22 |
| Return % (vs $5K base) | -2.92% |
| High-water mark | $102.31 |
| Max drawdown | $248.52 (+4.97%) |
| Closed trades | 11 (4W / 7L, win-rate 36.4%) |
| Avg PnL per closed trade | -$13.29 |

## Top rule_keys by cumulative PnL (n ≥ 5)

| rule_key | n | wins | win-rate | cumulative PnL |
|---|---|---|---|---|
| `8k_material_event::h7d` | 10 | 4 | 40.0% | -$98.08 |

## How to read this doc

This is a *historical replay*, not a live trading record. It answers the question: *had the realistic_loop_agent been active with a $5K bankroll and the discipline we ship today, what would it have made by 2025-06?* Numbers are bounded by the corpus available — h7d horizon, severity ≥ 2 events, ~150-ticker universe. They do not reflect intraday-spike alerts or the maturity-gated BUY/SELL vocabulary (which never triggered during this period — no rule reached the 90%/n≥30 adult gate).

Useful for a future agent reviewing time-indexed learning: load this and the prior month to see drift in rule_n, win-rate, and which tickers were accumulating edge or noise.
