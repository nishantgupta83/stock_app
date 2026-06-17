"""Pure money-math engine for the local Paper Book portfolio.

Storage-agnostic (no DB, no network) so it is trivially testable and shared. The
formulas MIRROR agents/realistic_loop_agent.py so the local SQLite book and the
Supabase shadow loop are directly comparable. Exit/stop/target detection is
delegated to price_agent.compute_paper_outcome (the stop_only logic) — not
reimplemented here — so the book marks positions the same way the calibration
pipeline grades paper trades.
"""
from __future__ import annotations

import heapq

SLIPPAGE_BPS = 5.0
SLIPPAGE_PER_SIDE = SLIPPAGE_BPS / 10_000  # 5 bps/side, 10 bps round-trip


def size_position(cash_available: float, per_size: float) -> float:
    """Notional to deploy for one position: the per-position size, capped at the
    cash on hand (no leverage, never negative)."""
    return round(max(0.0, min(per_size, cash_available)), 2)


def close_position(open_price: float, close_price: float, direction: str,
                   notional: float,
                   slippage_per_side: float = SLIPPAGE_PER_SIDE) -> tuple[float, float]:
    """Return (realized_pct, realized_pnl) for closing a position — direction-aware
    and net of round-trip slippage. Mirrors realistic_loop_agent._close_at."""
    direction_mult = 1.0 if direction == "long" else -1.0
    raw = (close_price - open_price) / open_price * direction_mult
    net = raw - 2 * slippage_per_side
    return round(net, 6), round(net * notional, 4)


def admit_positions(candidates: list[dict], max_concurrent: int) -> list[dict]:
    """Deterministic capacity gate for the event-sourced replay.

    Each candidate carries a determined `entry_at` and `exit_at` (ISO strings, both
    computed from event-time + bars — independent of when the local agent runs).
    Walk candidates in entry order; a position holds a slot from entry until exit;
    admit only while fewer than `max_concurrent` slots are held (a slot frees when
    its exit_at <= the next entry_at). Re-running reproduces the identical set, so
    the portfolio never depends on local run timing.
    """
    ordered = sorted(candidates, key=lambda c: (c["entry_at"], c.get("exit_at") or "9999"))
    held_exits: list[str] = []   # min-heap of exit_at of currently-held slots
    admitted: list[dict] = []
    for c in ordered:
        entry = c["entry_at"]
        exit_at = c.get("exit_at") or "9999-12-31"
        while held_exits and held_exits[0] <= entry:
            heapq.heappop(held_exits)
        if len(held_exits) < max_concurrent:
            admitted.append(c)
            heapq.heappush(held_exits, exit_at)
    return admitted


def recompute_state(positions: list[dict], capital_base: float) -> dict:
    """Derive portfolio state from the positions LEDGER (the source of truth) —
    crash-safe + idempotent (never accumulate incrementally; the realistic_loop
    M5 lesson). A run that dies mid-mark loses nothing: the next run re-derives.

    cash_available = capital_base - Σ(open notional); closed notional is freed.
    capital_base is static; PnL is tracked separately. high_water_mark and
    max_drawdown replay the closed-position equity path in close order.
    """
    open_notional = 0.0
    n_open = 0
    closed: list[dict] = []
    for p in positions:
        if p.get("status") == "open":
            n_open += 1
            open_notional += float(p.get("notional") or 0)
        elif p.get("status") == "closed":
            closed.append(p)
    closed.sort(key=lambda p: (p.get("closed_at") or ""))
    equity = hwm = maxdd = 0.0
    for p in closed:
        equity += float(p.get("realized_pnl") or 0)
        hwm = max(hwm, equity)
        maxdd = max(maxdd, hwm - equity)
    return {
        "cash_available":  round(capital_base - open_notional, 2),
        "positions_open":  n_open,
        "cumulative_pnl":  round(equity, 4),
        "high_water_mark": round(hwm, 4),
        "max_drawdown":    round(maxdd, 4),
    }
