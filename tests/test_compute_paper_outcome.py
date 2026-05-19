"""Regression tests for price_agent.compute_paper_outcome.

Direction-aware close-to-close return + MFE/MAE + target/stop audit. Long
and short must be perfectly symmetric — a +5% move on the underlying should
be +5% for long and -5% for short. The MFE/MAE/stop/target audit uses daily
high/low bars and must respect direction (long stop is below entry, short
stop is above).
"""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from price_agent import compute_paper_outcome


def _bars(entry_d: date, *, days: int = 5, base: float = 100.0,
          path: list[tuple[float, float, float]] | None = None) -> dict:
    """Build a {date: {high, low, close}} map.

    path: list of (high, low, close) over days (entry_d+1 .. entry_d+days).
    If omitted, builds a flat 100/100/100 path.
    """
    bars: dict = {entry_d: {"high": base, "low": base, "close": base}}
    if path is None:
        path = [(base, base, base)] * days
    for i, (h, lo, c) in enumerate(path, start=1):
        bars[entry_d + timedelta(days=i)] = {"high": h, "low": lo, "close": c}
    return bars


def _trade(direction: str, *, entry_d: date, entry_price: float = 100.0,
           horizon_days: int = 1, target_pct: float = 0.05,
           stop_pct: float = 0.03) -> dict:
    return {
        "entry_at":     entry_d.isoformat() + "T00:00:00+00:00",
        "entry_price":  entry_price,
        "direction":    direction,
        "horizon_days": horizon_days,
        "target_pct":   target_pct,
        "stop_pct":     stop_pct,
    }


# ---------- close-to-close return: long / short symmetry --------------------

def test_long_realized_return_positive_on_up_close():
    d0 = date(2026, 5, 1)
    bars = _bars(d0, path=[(105.0, 99.0, 105.0)])
    out = compute_paper_outcome(_trade("long", entry_d=d0, horizon_days=1), bars)
    assert out is not None
    assert out["realized_return"] == pytest.approx(0.05)
    assert out["correct"] is True


def test_short_realized_return_positive_on_down_close():
    """Same underlying move (+5%) for a short trade is a LOSS."""
    d0 = date(2026, 5, 1)
    bars = _bars(d0, path=[(105.0, 99.0, 105.0)])
    out = compute_paper_outcome(_trade("short", entry_d=d0, horizon_days=1), bars)
    assert out["realized_return"] == pytest.approx(-0.05)
    assert out["correct"] is False


def test_long_short_symmetric_signs():
    d0 = date(2026, 5, 1)
    bars = _bars(d0, path=[(102.0, 99.0, 97.0)])
    long_out = compute_paper_outcome(_trade("long", entry_d=d0), bars)
    short_out = compute_paper_outcome(_trade("short", entry_d=d0), bars)
    assert long_out["realized_return"] == pytest.approx(-short_out["realized_return"])


# ---------- target_hit / stop_hit are direction-aware ------------------------

def test_long_target_hit_when_high_breaches_up():
    d0 = date(2026, 5, 1)
    # On day +1 the daily high prints 106 (above entry 100 × 1.05 = 105)
    bars = _bars(d0, path=[(106.0, 99.5, 101.0)])
    out = compute_paper_outcome(_trade("long", entry_d=d0, target_pct=0.05), bars)
    assert out["target_hit"] is True
    assert out["stop_hit"] is False


def test_long_stop_hit_when_low_breaches_down():
    d0 = date(2026, 5, 1)
    # Daily low prints 96.5 (below entry 100 × 0.97 = 97)
    bars = _bars(d0, path=[(100.5, 96.5, 99.0)])
    out = compute_paper_outcome(_trade("long", entry_d=d0, stop_pct=0.03), bars)
    assert out["stop_hit"] is True
    assert out["target_hit"] is False


def test_short_target_hit_when_low_breaches_down():
    """For shorts, the favorable direction is DOWN — target_px = entry * (1 -
    target_pct). target_hit when daily LOW reaches it."""
    d0 = date(2026, 5, 1)
    bars = _bars(d0, path=[(101.0, 94.0, 95.0)])
    out = compute_paper_outcome(_trade("short", entry_d=d0, target_pct=0.05), bars)
    assert out["target_hit"] is True
    assert out["stop_hit"] is False


def test_short_stop_hit_when_high_breaches_up():
    """For shorts, the adverse direction is UP — stop_px = entry * (1 +
    stop_pct). stop_hit when daily HIGH reaches it."""
    d0 = date(2026, 5, 1)
    bars = _bars(d0, path=[(104.0, 99.5, 100.0)])
    out = compute_paper_outcome(_trade("short", entry_d=d0, stop_pct=0.03), bars)
    assert out["stop_hit"] is True
    assert out["target_hit"] is False


# ---------- MFE/MAE direction-aware ------------------------------------------

def test_long_mfe_positive_mae_negative():
    d0 = date(2026, 5, 1)
    # Day +1: high 103, low 98, close 101.
    bars = _bars(d0, path=[(103.0, 98.0, 101.0)])
    out = compute_paper_outcome(_trade("long", entry_d=d0), bars)
    # MFE = (103-100)/100 = +0.03; MAE = (98-100)/100 = -0.02
    assert out["mfe_pct"] == pytest.approx(0.03)
    assert out["mae_pct"] == pytest.approx(-0.02)


def test_short_mfe_positive_mae_negative_mirrored():
    """For shorts, MFE is when price goes DOWN (entry - low) / entry,
    MAE is when price goes UP. Both expressed direction-positive."""
    d0 = date(2026, 5, 1)
    bars = _bars(d0, path=[(103.0, 98.0, 101.0)])
    out = compute_paper_outcome(_trade("short", entry_d=d0), bars)
    # MFE = (100-98)/100 = +0.02; MAE = (100-103)/100 = -0.03
    assert out["mfe_pct"] == pytest.approx(0.02)
    assert out["mae_pct"] == pytest.approx(-0.03)


# ---------- returns None when data missing -----------------------------------

def test_returns_none_when_no_exit_bar():
    """No bar at or after entry_d + horizon — can't compute exit."""
    d0 = date(2026, 5, 1)
    bars = {d0: {"high": 100, "low": 100, "close": 100}}
    out = compute_paper_outcome(_trade("long", entry_d=d0, horizon_days=7), bars)
    assert out is None


def test_returns_none_on_bad_entry_price():
    d0 = date(2026, 5, 1)
    bars = _bars(d0)
    trade = _trade("long", entry_d=d0)
    trade["entry_price"] = 0
    assert compute_paper_outcome(trade, bars) is None
    trade["entry_price"] = "not-a-number"
    assert compute_paper_outcome(trade, bars) is None
