"""Pure engine for the local Paper Book portfolio (agents/_paper_book.py).

Mirrors realistic_loop's money math so the two are comparable:
  - close_position: direction-aware return net of round-trip slippage (5 bps/side).
  - recompute_state: derive cash/pnl/HWM/drawdown from the positions LEDGER
    (crash-safe, idempotent — never accumulate incrementally).
"""
from __future__ import annotations

import pytest

from _paper_book import admit_positions, close_position, recompute_state, size_position

SLIP = 0.0005  # 5 bps per side


def test_close_long_profit_net_of_round_trip_slippage():
    pct, pnl = close_position(open_price=100.0, close_price=110.0,
                              direction="long", notional=1000.0)
    assert pct == pytest.approx(0.10 - 2 * SLIP)   # +10% gross - 10bps
    assert pnl == pytest.approx((0.10 - 2 * SLIP) * 1000.0)


def test_close_short_profit_is_mirror_of_long():
    """A -10% move is a +10% gross win for a short."""
    pct, pnl = close_position(open_price=100.0, close_price=90.0,
                              direction="short", notional=1000.0)
    assert pct == pytest.approx(0.10 - 2 * SLIP)
    assert pnl == pytest.approx((0.10 - 2 * SLIP) * 1000.0)


def test_close_long_loss():
    pct, pnl = close_position(open_price=100.0, close_price=93.0,
                              direction="long", notional=1000.0)
    assert pct == pytest.approx(-0.07 - 2 * SLIP)
    assert pnl < 0


def test_size_position_caps_at_cash_and_per_size():
    # plenty of cash -> full per-position size
    assert size_position(cash_available=5000.0, per_size=1000.0) == 1000.0
    # low cash -> only what's left (no leverage / negative cash)
    assert size_position(cash_available=400.0, per_size=1000.0) == 400.0
    assert size_position(cash_available=0.0, per_size=1000.0) == 0.0


def test_recompute_state_cash_is_base_minus_open_notional():
    positions = [
        {"status": "open", "notional": 1000.0},
        {"status": "open", "notional": 1000.0},
        {"status": "closed", "notional": 1000.0, "realized_pnl": 50.0, "closed_at": "2026-06-10"},
    ]
    s = recompute_state(positions, capital_base=5000.0)
    assert s["cash_available"] == 3000.0      # 5000 - 2*1000 (closed notional freed)
    assert s["positions_open"] == 2
    assert s["cumulative_pnl"] == 50.0


def test_admit_under_cap_admits_all():
    cands = [{"setup_id": i, "entry_at": "2026-06-01", "exit_at": "2026-06-10"} for i in range(3)]
    assert len(admit_positions(cands, max_concurrent=5)) == 3


def test_admit_caps_concurrency_when_overlapping():
    # 6 positions all open 06-01..06-10 (overlapping); cap 5 -> only 5 get a slot.
    cands = [{"setup_id": i, "entry_at": "2026-06-01", "exit_at": "2026-06-10"} for i in range(6)]
    admitted = admit_positions(cands, max_concurrent=5)
    assert len(admitted) == 5
    assert {c["setup_id"] for c in admitted} == {0, 1, 2, 3, 4}  # first 5 by entry order


def test_admit_recycles_slot_after_exit():
    # cap 1, but A closes before B opens -> both fit sequentially.
    cands = [
        {"setup_id": "A", "entry_at": "2026-06-01", "exit_at": "2026-06-02"},
        {"setup_id": "B", "entry_at": "2026-06-03", "exit_at": "2026-06-04"},
    ]
    admitted = admit_positions(cands, max_concurrent=1)
    assert {c["setup_id"] for c in admitted} == {"A", "B"}


def test_admit_same_day_exit_and_entry_frees_slot():
    # exit_at == next entry_at: the slot is free (exit <= entry).
    cands = [
        {"setup_id": "A", "entry_at": "2026-06-01", "exit_at": "2026-06-03"},
        {"setup_id": "B", "entry_at": "2026-06-03", "exit_at": "2026-06-05"},
    ]
    assert len(admit_positions(cands, max_concurrent=1)) == 2


def test_admit_frozen_occupants_block_live_over_capacity():
    """Frozen closed trades must hold their historical capacity slots.

    MAX_CONC=1. A frozen trade occupies 06-01..06-10. A live candidate in the same
    window (06-02..06-09) must be BLOCKED because the slot is taken by the frozen
    trade. A second live candidate starting after the frozen closes (06-11..) must
    be ADMITTED because the slot is free by then.

    This is the Fix 1 regression lock: on the OLD code (frozen skipped before
    appending), both live candidates would have been admitted; on the NEW code
    (frozen-as-capacity-occupant), only the post-freeze live candidate is admitted.
    """
    candidates = [
        # frozen occupant: already in the ledger — carries only what admit_positions needs
        {"setup_id": "F1", "frozen": True,
         "entry_at": "2026-06-01", "exit_at": "2026-06-10"},
        # live candidate inside the frozen window -> must be BLOCKED
        {"setup_id": "L1", "frozen": False,
         "entry_at": "2026-06-02", "exit_at": "2026-06-09"},
        # live candidate starting after frozen closes -> must be ADMITTED
        {"setup_id": "L2", "frozen": False,
         "entry_at": "2026-06-11", "exit_at": "2026-06-20"},
    ]
    admitted = admit_positions(candidates, max_concurrent=1)
    admitted_ids = {c["setup_id"] for c in admitted}
    assert "F1" in admitted_ids, "frozen occupant must be admitted (counts for capacity)"
    assert "L1" not in admitted_ids, "live candidate inside frozen window must be BLOCKED"
    assert "L2" in admitted_ids, "live candidate after frozen closes must be admitted"


def test_recompute_state_drawdown_replays_closed_equity_in_order():
    positions = [
        {"status": "closed", "notional": 1000.0, "realized_pnl": 100.0, "closed_at": "2026-06-01"},
        {"status": "closed", "notional": 1000.0, "realized_pnl": -50.0, "closed_at": "2026-06-02"},
        {"status": "closed", "notional": 1000.0, "realized_pnl": 30.0,  "closed_at": "2026-06-03"},
    ]
    s = recompute_state(positions, capital_base=5000.0)
    # equity path: +100 (hwm 100), 50 (dd 50), 80 -> cumulative 80, hwm 100, maxdd 50
    assert s["cumulative_pnl"] == 80.0
    assert s["high_water_mark"] == 100.0
    assert s["max_drawdown"] == 50.0
