"""M5 — realistic_loop state must be recomputed from the ledger, not mutated
incrementally.

The mark loop closed positions one-by-one (sb_patch) then aggregated cash/pnl
into the state at the END. A crash mid-loop persisted the closes but lost their
cash/pnl forever (state never self-heals). recompute_state derives the whole
state from the positions ledger (the source of truth) so it's idempotent +
crash-safe: cash = capital_base - Σ(open notional), pnl = Σ(closed pnl), and
HWM/drawdown replay the closed-position equity path in close order.
"""
from __future__ import annotations

from realistic_loop_agent import recompute_state


def _p(status, notional, pnl=None, closed_at=None):
    return {"status": status, "notional": notional, "realized_pnl": pnl, "closed_at": closed_at}


def test_cash_and_pnl_from_ledger():
    positions = [
        _p("open", 1000),
        _p("open", 1000),
        _p("closed", 1000, 50.0, "2026-06-01T20:00:00+00:00"),
        _p("closed", 1000, -30.0, "2026-06-02T20:00:00+00:00"),
        _p("closed", 1000, 100.0, "2026-06-03T20:00:00+00:00"),
    ]
    st = recompute_state(positions, capital_base=5000.0)
    assert st["positions_open"] == 2
    assert st["cash_available"] == 3000.0          # 5000 - 2*1000 open
    assert st["cumulative_pnl"] == 120.0           # 50 - 30 + 100


def test_hwm_and_drawdown_replay_in_close_order():
    positions = [
        _p("closed", 1000, 50.0, "2026-06-01T20:00:00+00:00"),
        _p("closed", 1000, -30.0, "2026-06-02T20:00:00+00:00"),
        _p("closed", 1000, 100.0, "2026-06-03T20:00:00+00:00"),
    ]
    st = recompute_state(positions, capital_base=5000.0)
    # equity path 50 -> 20 -> 120; HWM=120, max drawdown = 50-20 = 30
    assert st["high_water_mark"] == 120.0
    assert st["max_drawdown"] == 30.0


def test_crash_mid_close_self_heals():
    # Simulates a crash: a position was closed (status=closed, pnl set) but the
    # state's cash was never credited. recompute ignores the (lost) old state and
    # derives the truth: the closed position is not open → its notional is freed.
    positions = [_p("open", 1000), _p("closed", 1000, 40.0, "2026-06-01T20:00:00+00:00")]
    st = recompute_state(positions, capital_base=5000.0)
    assert st["cash_available"] == 4000.0          # only the 1 open deploys capital
    assert st["positions_open"] == 1
    assert st["cumulative_pnl"] == 40.0


def test_empty_ledger_is_full_bankroll():
    st = recompute_state([], capital_base=5000.0)
    assert st["cash_available"] == 5000.0 and st["positions_open"] == 0
    assert st["cumulative_pnl"] == 0.0 and st["high_water_mark"] == 0.0
