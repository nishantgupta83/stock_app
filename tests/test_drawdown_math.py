"""Regression tests for A1: equity-curve max drawdown.

The pre-A1 implementation computed mean per-trade return and compared it
against MAX_DRAWDOWN_PCT (10%). For that to trip, the AVERAGE trade had to
lose 10% — essentially impossible. These tests confirm the new
peak-to-trough equity-curve drawdown trips on realistic loss patterns.
"""
from __future__ import annotations

import pytest

from agents.risk_agent import compute_equity_curve_drawdown


def _trade(realized_return: float) -> dict:
    return {"realized_return": realized_return}


def test_empty_window_returns_zero_drawdown():
    out = compute_equity_curve_drawdown([])
    assert out["drawdown_pct"] == 0.0
    assert out["n"] == 0


def test_all_winners_no_drawdown():
    trades = [_trade(0.02), _trade(0.03), _trade(0.01)]
    out = compute_equity_curve_drawdown(trades)
    assert out["drawdown_pct"] == 0.0
    assert out["sum_return"] == pytest.approx(0.06)
    assert out["peak_cumulative"] == pytest.approx(0.06)


def test_single_blowup_trips_breaker():
    """One -50% trade after some winners must trip the -10% threshold.

    This is the exact case the old mean-based code missed: 30 trades with
    one big loser have a manageable mean but a catastrophic drawdown."""
    trades = [_trade(0.02)] * 29 + [_trade(-0.50)]
    out = compute_equity_curve_drawdown(trades)
    # Peak after the 29 winners is +0.58. Cumulative after blowup is +0.08.
    # Drawdown is 0.08 - 0.58 = -0.50.
    assert out["drawdown_pct"] == pytest.approx(-0.50)
    assert out["drawdown_pct"] < -0.10   # would trip the circuit breaker


def test_pre_a1_mean_would_have_missed_this():
    """Same blowup as above. The old `sum / len` would have been (29*0.02 -
    0.50) / 30 = 0.0027, way above -0.10. The breaker never would have
    fired. The equity-curve approach sees -0.50."""
    trades = [_trade(0.02)] * 29 + [_trade(-0.50)]
    realized = [float(t["realized_return"]) for t in trades]
    old_mean = sum(realized) / len(realized)
    assert old_mean > -0.10   # old code: breaker did NOT trip
    out = compute_equity_curve_drawdown(trades)
    assert out["drawdown_pct"] < -0.10   # new code: breaker DOES trip


def test_recovery_does_not_reset_max_dd():
    """Once a drawdown occurs, a later recovery shouldn't erase the historical
    max. We watch peak-to-trough over the whole window — recovery is good but
    it doesn't refund the lesson."""
    # peak +5%, dip to -5% (DD = -10%), recover back to +3%
    trades = [_trade(0.05), _trade(-0.10), _trade(0.08)]
    out = compute_equity_curve_drawdown(trades)
    assert out["drawdown_pct"] == pytest.approx(-0.10)
    assert out["sum_return"] == pytest.approx(0.03)


def test_steady_losses_accumulate_to_drawdown():
    """Twenty consecutive -1% losses → cumulative -20%, drawdown -20%."""
    trades = [_trade(-0.01)] * 20
    out = compute_equity_curve_drawdown(trades)
    assert out["drawdown_pct"] == pytest.approx(-0.20)


def test_null_realized_return_treated_as_zero():
    trades = [{"realized_return": None}, _trade(-0.05), _trade(-0.03)]
    out = compute_equity_curve_drawdown(trades)
    assert out["drawdown_pct"] == pytest.approx(-0.08)
    assert out["n"] == 3


def test_drawdown_never_positive():
    """Drawdown_pct is defined as peak-to-trough — by construction ≤ 0."""
    trades = [_trade(0.10), _trade(0.10), _trade(0.10)]
    out = compute_equity_curve_drawdown(trades)
    assert out["drawdown_pct"] <= 0.0


def test_compute_portfolio_state_integration(monkeypatch):
    """End-to-end: monkeypatch sb_get so compute_portfolio_state runs against
    synthetic data, and confirm the drawdown_pct propagates."""
    from agents import risk_agent

    call_log = []

    def fake_sb_get(path, params):
        call_log.append((path, params.get("status")))
        if path == "stock_event_paper_trades" and params.get("status") == "eq.closed":
            return [_trade(0.05), _trade(-0.20), _trade(0.02)]
        return []

    monkeypatch.setattr(risk_agent, "sb_get", fake_sb_get)
    state = risk_agent.compute_portfolio_state()

    # Peak +0.05, trough -0.15, recovery to -0.13. Max DD = -0.20.
    assert state["drawdown_pct"] == pytest.approx(-0.20)
    assert state["sum_return_30d"] == pytest.approx(-0.13)
    assert state["n_closed_30d"] == 3
    # Threshold (-0.10) would be breached → breaker would fire.
    assert state["drawdown_pct"] < -risk_agent.MAX_DRAWDOWN_PCT
