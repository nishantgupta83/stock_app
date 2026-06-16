"""STOP-ONLY exit policy for price_agent.compute_paper_outcome.

The bug this fixes: realized_return was naked close-to-close at the horizon-day
close, ignoring the declared stop. So a trade whose stop was hit on day 3 was
held all 30 days (a real INTC trade booked +122% instead of being stopped).

STOP-ONLY semantics ("cut losers, let winners run"):
  - exit at the stop price the FIRST day the stop is breached (direction-aware,
    daily high/low), conservative GAP-FILL AT OPEN when the bar gaps through the
    stop (fill no better than the open).
  - if the stop is never hit, RIDE to the horizon close (winners are NOT capped;
    there is no take-profit under this policy).
  - target_hit/stop_hit remain audit flags; a new `exit_reason` records why the
    trade closed ("stop" | "horizon").
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from price_agent import compute_paper_outcome

SLIP = 0.001  # 2 sides x 5 bps round-trip


def _bars(entry_d: date, path: list[tuple[float, float, float, float]],
          base: float = 100.0) -> dict:
    """{date: {open, high, low, close}} — path is (open, high, low, close) per day."""
    bars: dict = {entry_d: {"open": base, "high": base, "low": base, "close": base}}
    for i, (o, h, lo, c) in enumerate(path, start=1):
        bars[entry_d + timedelta(days=i)] = {"open": o, "high": h, "low": lo, "close": c}
    return bars


def _trade(direction: str, *, entry_d: date, entry_price: float = 100.0,
           horizon_days: int = 5, target_pct: float = 0.05,
           stop_pct: float = 0.03) -> dict:
    return {
        "entry_at": entry_d.isoformat() + "T00:00:00+00:00",
        "entry_price": entry_price, "direction": direction,
        "horizon_days": horizon_days, "target_pct": target_pct, "stop_pct": stop_pct,
    }


def test_stop_only_exits_at_stop_price_not_horizon_close():
    """Long stop_px=97 touched intraday day+1 (open 100 above stop) -> fill at 97,
    NOT the day-5 horizon close."""
    d0 = date(2026, 5, 1)
    bars = _bars(d0, [(100.0, 100.5, 96.5, 99.0), (99, 99, 98, 98),
                      (98, 99, 97.5, 98.5), (98, 99, 98, 98), (98, 100, 98, 99)])
    out = compute_paper_outcome(_trade("long", entry_d=d0, stop_pct=0.03), bars,
                                exit_policy="stop_only")
    assert out["exit_reason"] == "stop"
    assert out["realized_return"] == pytest.approx(-0.03 - SLIP)
    assert out["exit_at"].startswith((d0 + timedelta(days=1)).isoformat())


def test_stop_only_gap_through_stop_fills_at_open():
    """Bar gaps DOWN through the stop (open 95 < stop_px 97) -> fill at the open
    (95), worse than the stop price."""
    d0 = date(2026, 5, 1)
    bars = _bars(d0, [(95.0, 96.0, 94.0, 95.5)] + [(95, 96, 94, 95)] * 4)
    out = compute_paper_outcome(_trade("long", entry_d=d0, stop_pct=0.03), bars,
                                exit_policy="stop_only")
    assert out["exit_reason"] == "stop"
    assert out["realized_return"] == pytest.approx(-0.05 - SLIP)


def test_stop_only_stops_out_before_a_later_rally():
    """Stopped day+1; a +30% rally on day+2/3 must NOT be captured (the bug)."""
    d0 = date(2026, 5, 1)
    bars = _bars(d0, [(100, 100.5, 96.5, 98), (110, 131, 109, 130), (130, 140, 129, 138)])
    out = compute_paper_outcome(_trade("long", entry_d=d0, horizon_days=3, stop_pct=0.03),
                                bars, exit_policy="stop_only")
    assert out["exit_reason"] == "stop"
    assert out["realized_return"] == pytest.approx(-0.03 - SLIP)


def test_stop_only_rides_winner_to_horizon_uncapped():
    """Stop never hit; target (105) crossed but NOT capped -> ride to day-2 close 120."""
    d0 = date(2026, 5, 1)
    bars = _bars(d0, [(101, 110, 100.5, 108), (108, 125, 107, 120)])
    out = compute_paper_outcome(_trade("long", entry_d=d0, horizon_days=2,
                                       target_pct=0.05, stop_pct=0.03),
                                bars, exit_policy="stop_only")
    assert out["exit_reason"] == "horizon"
    assert out["realized_return"] == pytest.approx(0.20 - SLIP)
    assert out["target_hit"] is True  # audit still records the target was touched


def test_stop_only_short_exits_at_stop():
    """Short stop_px=103 touched intraday (high 104, open 101 below stop) -> fill 103."""
    d0 = date(2026, 5, 1)
    bars = _bars(d0, [(101, 104, 100, 102), (102, 103, 101, 102), (102, 103, 101, 102)])
    out = compute_paper_outcome(_trade("short", entry_d=d0, horizon_days=3, stop_pct=0.03),
                                bars, exit_policy="stop_only")
    assert out["exit_reason"] == "stop"
    assert out["realized_return"] == pytest.approx(-0.03 - SLIP)


def test_stop_only_closes_on_stop_before_horizon_bar_exists():
    """A stop hit on day+2 must close AT the stop immediately, even when the h30
    horizon bar does not exist yet (Codex: no delayed close / backdated exit_at)."""
    d0 = date(2026, 5, 1)
    # horizon 30, but bars only run through day+3 — not matured to the horizon.
    bars = _bars(d0, [(100, 101, 99, 100), (100, 100.5, 96.0, 97.5), (97, 98, 96, 97)])
    out = compute_paper_outcome(_trade("long", entry_d=d0, horizon_days=30, stop_pct=0.03),
                                bars, exit_policy="stop_only")
    assert out is not None
    assert out["exit_reason"] == "stop"
    assert out["exit_at"].startswith((d0 + timedelta(days=2)).isoformat())
    assert out["realized_return"] == pytest.approx(-0.03 - SLIP)


def test_stop_only_returns_none_when_not_stopped_and_not_matured():
    """No stop yet AND horizon not reached -> still open (None), don't close early."""
    d0 = date(2026, 5, 1)
    bars = _bars(d0, [(100, 101, 99, 100), (100, 101, 99.5, 100.5)])
    out = compute_paper_outcome(_trade("long", entry_d=d0, horizon_days=30, stop_pct=0.03),
                                bars, exit_policy="stop_only")
    assert out is None


def test_hold_policy_preserves_legacy_horizon_close():
    """exit_policy='hold' must reproduce the old naked hold-to-horizon return."""
    d0 = date(2026, 5, 1)
    # stop (97) is breached on day+1, but 'hold' ignores it and rides to the
    # day-5 horizon close of 99.
    bars = _bars(d0, [(100, 100.5, 96.5, 99.0)] + [(99, 99.5, 98, 99)] * 4)
    out = compute_paper_outcome(_trade("long", entry_d=d0, stop_pct=0.03), bars,
                                exit_policy="hold")
    assert out["exit_reason"] == "horizon"
    # day-5 close 99 -> -1% - slip (NOT stopped at 97)
    assert out["realized_return"] == pytest.approx(-0.01 - SLIP)
