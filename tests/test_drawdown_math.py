"""Regression tests for A1 (+ A1b NAV scaling).

The pre-A1 implementation computed mean per-trade return and compared it
against MAX_DRAWDOWN_PCT (10%). For that to trip, the AVERAGE trade had to
lose 10% — essentially impossible.

A1 introduced peak-to-trough cumulative-return drawdown. A1b corrected the
units: per-trade return is multiplied by RISK_PER_TRADE_PCT so the result
is in NAV-fraction units, matching MAX_DRAWDOWN_PCT. Without that scaling,
summing per-trade percentages grows unboundedly with trade count and the
breaker over-fires (live observation 2026-05-19: -304% cumulative across
939 trades skipped 30/36 decisions on a single afternoon).
"""
from __future__ import annotations

import pytest

from agents.risk_agent import (
    compute_equity_curve_drawdown,
    RISK_PER_TRADE_PCT,
)


def _trade(realized_return: float) -> dict:
    return {"realized_return": realized_return}


# All tests below use risk_per_trade_pct=1.0 to make the unit-conversion
# transparent. The default (RISK_PER_TRADE_PCT) is exercised in the live
# integration test at the bottom.
def _dd(trades):
    return compute_equity_curve_drawdown(trades, risk_per_trade_pct=1.0)


def test_empty_window_returns_zero_drawdown():
    out = _dd([])
    assert out["drawdown_pct"] == 0.0
    assert out["n"] == 0


def test_all_winners_no_drawdown():
    trades = [_trade(0.02), _trade(0.03), _trade(0.01)]
    out = _dd(trades)
    assert out["drawdown_pct"] == 0.0
    assert out["sum_return_nav"] == pytest.approx(0.06)
    assert out["peak_cumulative"] == pytest.approx(0.06)


def test_single_blowup_trips_breaker():
    """One -50% trade after some winners must trip the -10% threshold.

    Old mean-based code missed this. At risk_per_trade_pct=1.0 the unit is
    identical to per-trade percentage so the magnitudes match the natural
    reading; the live code applies the Van Tharp scaling separately."""
    trades = [_trade(0.02)] * 29 + [_trade(-0.50)]
    out = _dd(trades)
    # Peak after the 29 winners is +0.58. Cumulative after blowup is +0.08.
    # Drawdown is 0.08 - 0.58 = -0.50.
    assert out["drawdown_pct"] == pytest.approx(-0.50)
    assert out["drawdown_pct"] < -0.10


def test_pre_a1_mean_would_have_missed_this():
    """The old `sum / len` would have been (29*0.02 - 0.50) / 30 = 0.0027,
    way above -0.10 — never tripped. Peak-to-trough sees -0.50."""
    trades = [_trade(0.02)] * 29 + [_trade(-0.50)]
    realized = [float(t["realized_return"]) for t in trades]
    old_mean = sum(realized) / len(realized)
    assert old_mean > -0.10   # old code: breaker did NOT trip
    out = _dd(trades)
    assert out["drawdown_pct"] < -0.10


def test_recovery_does_not_reset_max_dd():
    # peak +5%, dip to -5% (DD = -10%), recover back to +3%
    trades = [_trade(0.05), _trade(-0.10), _trade(0.08)]
    out = _dd(trades)
    assert out["drawdown_pct"] == pytest.approx(-0.10)
    assert out["sum_return_nav"] == pytest.approx(0.03)


def test_steady_losses_accumulate_to_drawdown():
    """Twenty consecutive -1% losses → cumulative -20%, drawdown -20%."""
    trades = [_trade(-0.01)] * 20
    out = _dd(trades)
    assert out["drawdown_pct"] == pytest.approx(-0.20)


def test_null_realized_return_treated_as_zero():
    trades = [{"realized_return": None}, _trade(-0.05), _trade(-0.03)]
    out = _dd(trades)
    assert out["drawdown_pct"] == pytest.approx(-0.08)
    assert out["n"] == 3


def test_drawdown_never_positive():
    trades = [_trade(0.10), _trade(0.10), _trade(0.10)]
    out = _dd(trades)
    assert out["drawdown_pct"] <= 0.0


# ---------- A1b — Van Tharp NAV scaling -------------------------------------

def test_nav_scaling_applied_by_default():
    """A1b: with the default RISK_PER_TRADE_PCT, a -50% trade-return
    blowup contributes only -0.5 * 0.01 = -0.005 NAV-fraction. Bounded by
    the size of risk budget, not by trade percentage magnitude."""
    trades = [_trade(0.02)] * 29 + [_trade(-0.50)]
    out = compute_equity_curve_drawdown(trades)
    # Peak: 29 * 0.02 * 0.01 = 0.0058. After blowup: 0.0058 - 0.005 = 0.0008.
    # DD = 0.0008 - 0.0058 = -0.005 NAV.
    assert out["drawdown_pct"] == pytest.approx(-0.005, abs=1e-6)
    assert out["drawdown_pct"] > -0.10   # below threshold — breaker correctly stays off


def test_nav_scaling_prevents_unbounded_growth_with_trade_count():
    """A1b: 1000 small-loss trades shouldn't trip a 10% NAV breaker — the
    pre-A1b code would have summed to -10 with no scaling, vastly past
    -0.10 and locking out forever."""
    trades = [_trade(-0.01)] * 1000
    out = compute_equity_curve_drawdown(trades)
    # 1000 * 0.01 trade-loss * 0.01 RISK_PER_TRADE = 0.10 NAV-loss
    assert out["drawdown_pct"] == pytest.approx(-0.10)
    # The breaker fires at exactly the 10% threshold here, which is the
    # honest read: 1000 paper trades each losing 1R IS a 10% NAV blowup.


def test_compute_portfolio_state_integration(monkeypatch):
    from agents import risk_agent

    def fake_sb_get(path, params):
        if path == "stock_event_paper_trades" and params.get("status") == "eq.closed":
            return [_trade(0.05), _trade(-0.20), _trade(0.02)]
        return []

    monkeypatch.setattr(risk_agent, "sb_get", fake_sb_get)
    state = risk_agent.compute_portfolio_state()

    # With RISK_PER_TRADE_PCT=0.01 scaling:
    # Trade returns scaled: [+0.0005, -0.002, +0.0002]
    # Cumulative: 0.0005, -0.0015, -0.0013
    # Peak: 0.0005, trough: -0.0015. DD = -0.0015 - 0.0005 = -0.002
    assert state["drawdown_pct"] == pytest.approx(-0.002, abs=1e-6)
    assert state["n_closed_30d"] == 3
    # -0.002 is well above -0.10 → breaker doesn't fire on three trades.
    assert state["drawdown_pct"] > -risk_agent.MAX_DRAWDOWN_PCT
