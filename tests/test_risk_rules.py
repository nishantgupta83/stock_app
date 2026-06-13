"""Regression tests for risk_agent.evaluate_setup survival rules.

Each rule in the priority chain (self_skip → confidence_floor → drawdown
breaker → daily budget → concentration cap → stop sanity → maturity weight)
must fire only when it should. These tests pin the chain so a refactor of
ordering or threshold values can't silently degrade the safety net.
"""
from __future__ import annotations

import pytest

from risk_agent import (
    evaluate_setup,
    CONFIDENCE_FLOOR, MAX_DRAWDOWN_PCT, MAX_DAILY_RISK_PCT,
    MAX_SAME_RULE_OPEN, STOP_PCT_MIN, STOP_PCT_MAX,
    PORTFOLIO_NAV_BASELINE, RISK_PER_TRADE_PCT,
    MATURITY_MULTIPLIER,
)


def _setup(**overrides) -> dict:
    base = {
        "id":             123,
        "signal_id":      999,
        "ticker":         "FOO",
        "direction":      "long",
        "setup_type":     "next_open",
        "confidence":     0.80,
        "stop_pct":       0.03,
        "target_pct":     0.05,
        "horizon_days":   1,
        "rule_key":       "8k_material_event::h1d",
        "reason_to_skip": None,
    }
    base.update(overrides)
    return base


def _state(**overrides) -> dict:
    base = {
        "drawdown_pct":              0.0,
        "sum_return_30d":            0.0,
        "n_closed_30d":              0,
        "daily_risk_in_flight_pct":  0.0,
        "open_per_rule":             {},
    }
    base.update(overrides)
    return base


# ---------- rule 1: setup_self_skip ------------------------------------------

def test_self_skip_short_circuits_chain():
    setup = _setup(reason_to_skip="rule needs n=5+, has 2")
    decision = evaluate_setup(setup, cal={}, state=_state())
    assert decision["decision"] == "skip"
    assert "self-skipped" in decision["reason"]
    # Only the self_skip rule should be in the audit — nothing after.
    rule_names = [r["rule"] for r in decision["rules_applied"]]
    assert rule_names == ["setup_self_skip"]


# ---------- rule 2: confidence_floor -----------------------------------------

def test_confidence_below_floor_skips():
    setup = _setup(confidence=CONFIDENCE_FLOOR - 0.01)
    decision = evaluate_setup(setup, cal={}, state=_state())
    assert decision["decision"] == "skip"
    assert "below floor" in decision["reason"]


def test_confidence_at_floor_passes():
    setup = _setup(confidence=CONFIDENCE_FLOOR)
    decision = evaluate_setup(setup, cal={}, state=_state())
    assert decision["decision"] == "size"


# ---------- rule 3: drawdown_circuit_breaker ---------------------------------

def test_drawdown_at_threshold_skips():
    setup = _setup()
    decision = evaluate_setup(setup, cal={}, state=_state(drawdown_pct=-MAX_DRAWDOWN_PCT))
    assert decision["decision"] == "skip"
    assert "drawdown circuit breaker" in decision["reason"]


def test_drawdown_just_inside_passes():
    """Threshold is strict: dd > -MAX. Right at the line should trip; one
    basis-point above should pass."""
    setup = _setup()
    decision = evaluate_setup(setup, cal={},
                              state=_state(drawdown_pct=-MAX_DRAWDOWN_PCT + 0.001))
    assert decision["decision"] == "size"


def test_drawdown_deep_skips():
    setup = _setup()
    decision = evaluate_setup(setup, cal={}, state=_state(drawdown_pct=-0.25))
    assert decision["decision"] == "skip"


# ---------- rule 4: daily_risk_budget ----------------------------------------

def test_daily_budget_exhausted_skips():
    setup = _setup()
    decision = evaluate_setup(setup, cal={},
                              state=_state(daily_risk_in_flight_pct=MAX_DAILY_RISK_PCT))
    assert decision["decision"] == "skip"
    assert "daily risk budget" in decision["reason"]


def test_daily_budget_under_cap_passes():
    setup = _setup()
    decision = evaluate_setup(setup, cal={},
                              state=_state(daily_risk_in_flight_pct=MAX_DAILY_RISK_PCT - 0.001))
    assert decision["decision"] == "size"


# ---------- rule 5: rule_concentration ---------------------------------------

def test_concentration_at_cap_skips():
    setup = _setup(rule_key="foo:bar:h1d")
    state = _state(open_per_rule={"foo:bar:h1d": MAX_SAME_RULE_OPEN})
    decision = evaluate_setup(setup, cal={}, state=state)
    assert decision["decision"] == "skip"
    assert "too many open on rule" in decision["reason"]


def test_concentration_under_cap_passes():
    setup = _setup(rule_key="foo:bar:h1d")
    state = _state(open_per_rule={"foo:bar:h1d": MAX_SAME_RULE_OPEN - 1})
    decision = evaluate_setup(setup, cal={}, state=state)
    assert decision["decision"] == "size"


# ---------- rule 6: stop_sanity ----------------------------------------------

def test_stop_below_min_skips():
    setup = _setup(stop_pct=STOP_PCT_MIN / 2)
    decision = evaluate_setup(setup, cal={}, state=_state())
    assert decision["decision"] == "skip"
    assert "stop_pct" in decision["reason"]


def test_stop_above_max_skips():
    setup = _setup(stop_pct=STOP_PCT_MAX * 2)
    decision = evaluate_setup(setup, cal={}, state=_state())
    assert decision["decision"] == "skip"


def test_stop_at_min_boundary_passes():
    setup = _setup(stop_pct=STOP_PCT_MIN)
    decision = evaluate_setup(setup, cal={}, state=_state())
    assert decision["decision"] == "size"


# ---------- rule 7: maturity_weight ------------------------------------------

def test_immature_rule_gets_immature_multiplier():
    setup = _setup()
    cal = {}   # no calibration row
    decision = evaluate_setup(setup, cal=cal, state=_state())
    assert decision["decision"] == "size"
    expected_risk = PORTFOLIO_NAV_BASELINE * RISK_PER_TRADE_PCT * MATURITY_MULTIPLIER["immature"]
    assert decision["max_loss_dollars"] == pytest.approx(expected_risk, abs=0.01)


def test_production_mature_rule_gets_full_multiplier():
    setup = _setup(rule_key="mature:foo:h7d")
    # H1: risk reads the STORED tier (gated on effective-n by price_agent), not
    # raw stats — so supply tier='adult'. (Raw stats kept for realism.)
    cal = {"mature:foo:h7d": {"tier": "adult", "n_observations": 120,
                              "profit_factor": 2.5, "mean_realized_pct": 0.01}}
    decision = evaluate_setup(setup, cal=cal, state=_state())
    expected_risk = PORTFOLIO_NAV_BASELINE * RISK_PER_TRADE_PCT * MATURITY_MULTIPLIER["adult"]
    assert decision["max_loss_dollars"] == pytest.approx(expected_risk, abs=0.01)


def test_training_tier_gets_half_multiplier():
    setup = _setup(rule_key="training:foo:h7d")
    # H1: risk reads the STORED tier — supply tier='teen'. 0.50× multiplier.
    cal = {"training:foo:h7d": {"tier": "teen", "accuracy": 0.75,
                                "n_observations": 32, "mean_realized_pct": 0.01}}
    decision = evaluate_setup(setup, cal=cal, state=_state())
    expected_risk = PORTFOLIO_NAV_BASELINE * RISK_PER_TRADE_PCT * MATURITY_MULTIPLIER["teen"]
    assert decision["max_loss_dollars"] == pytest.approx(expected_risk, abs=0.01)


# ---------- size math sanity -------------------------------------------------

def test_size_dollars_inverse_of_stop_pct():
    """Tighter stop → larger size for the same risk budget. Van Tharp standard."""
    cal = {}
    d_tight = evaluate_setup(_setup(stop_pct=0.02), cal=cal, state=_state())
    d_wide  = evaluate_setup(_setup(stop_pct=0.06), cal=cal, state=_state())
    assert d_tight["decision"] == d_wide["decision"] == "size"
    assert d_tight["size_dollars_at_100k"] > d_wide["size_dollars_at_100k"]
    # Both should max-loss to the same dollar amount (that's the Van Tharp invariant).
    assert d_tight["max_loss_dollars"] == pytest.approx(d_wide["max_loss_dollars"], abs=0.01)
