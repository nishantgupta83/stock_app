"""Regression tests for B1: adaptive target/stop from calibration.

trade_setup_agent.compute_target_and_stop must:
  - Fall back to defaults when n < 10 or MFE/MAE missing
  - Adapt target_pct toward 70% of mean_mfe_pct, stop_pct toward 50% of
    |mean_mae_pct|, clipped into sane bounds
  - Tag the source field so audits can distinguish default vs calibrated
"""
from __future__ import annotations

import pytest

from trade_setup_agent import (
    compute_target_and_stop,
    DEFAULT_TARGET_PCT, DEFAULT_STOP_PCT,
    TARGET_PCT_MIN, TARGET_PCT_MAX,
    STOP_PCT_MIN, STOP_PCT_MAX,
)


def test_empty_calibration_uses_defaults():
    target, stop, source = compute_target_and_stop({})
    assert target == DEFAULT_TARGET_PCT
    assert stop == DEFAULT_STOP_PCT
    assert source == "default"


def test_below_min_n_uses_defaults():
    cal = {"n_observations": 9, "mean_mfe_pct": 0.07, "mean_mae_pct": -0.04}
    target, stop, source = compute_target_and_stop(cal)
    assert source == "default"
    assert target == DEFAULT_TARGET_PCT
    assert stop == DEFAULT_STOP_PCT


def test_missing_mfe_uses_defaults():
    cal = {"n_observations": 50, "mean_mfe_pct": None, "mean_mae_pct": -0.04}
    _, _, source = compute_target_and_stop(cal)
    assert source == "default"


def test_missing_mae_uses_defaults():
    cal = {"n_observations": 50, "mean_mfe_pct": 0.06, "mean_mae_pct": None}
    _, _, source = compute_target_and_stop(cal)
    assert source == "default"


def test_typical_calibrated_values():
    """MFE 6%, MAE -4%, n=25 → target = 0.6*0.7 = 0.042, stop = 0.04*0.5 = 0.02."""
    cal = {"n_observations": 25, "mean_mfe_pct": 0.06, "mean_mae_pct": -0.04}
    target, stop, source = compute_target_and_stop(cal)
    assert source == "calibrated"
    assert target == pytest.approx(0.042, abs=1e-4)
    assert stop == pytest.approx(0.02, abs=1e-4)


def test_extreme_mfe_clipped_to_max():
    """Outlier rule with 25% mean_mfe shouldn't produce a 17.5% target."""
    cal = {"n_observations": 50, "mean_mfe_pct": 0.25, "mean_mae_pct": -0.04}
    target, _, _ = compute_target_and_stop(cal)
    assert target == TARGET_PCT_MAX


def test_tiny_mfe_clipped_to_min():
    """If MFE is below the floor, target gets the floor — we don't want a
    1-cent target. Same with stop floor."""
    cal = {"n_observations": 50, "mean_mfe_pct": 0.001, "mean_mae_pct": -0.001}
    target, stop, _ = compute_target_and_stop(cal)
    assert target == TARGET_PCT_MIN
    assert stop == STOP_PCT_MIN


def test_extreme_mae_clipped_to_max():
    """Wide adverse excursion shouldn't produce a 50% stop."""
    cal = {"n_observations": 50, "mean_mfe_pct": 0.05, "mean_mae_pct": -0.50}
    _, stop, _ = compute_target_and_stop(cal)
    assert stop == STOP_PCT_MAX


def test_positive_mae_treated_as_abs():
    """mean_mae_pct should be negative, but defensive abs() handles a
    positive value the same way."""
    cal = {"n_observations": 50, "mean_mfe_pct": 0.05, "mean_mae_pct": 0.04}
    _, stop_neg, _ = compute_target_and_stop({**cal, "mean_mae_pct": -0.04})
    _, stop_pos, _ = compute_target_and_stop(cal)
    assert stop_neg == stop_pos


def test_exactly_min_n_is_calibrated():
    """Boundary: n == ADAPTIVE_MIN_N triggers calibration."""
    cal = {"n_observations": 10, "mean_mfe_pct": 0.05, "mean_mae_pct": -0.03}
    _, _, source = compute_target_and_stop(cal)
    assert source == "calibrated"
