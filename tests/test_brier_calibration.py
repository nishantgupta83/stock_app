"""Regression tests for compute_brier_30d (price_agent).

Brier here measures how well a rule's claimed accuracy matches its recent
outcomes. predicted_prob = rule's lifetime accuracy; outcome ∈ {0, 1}.
For a well-calibrated rule the Brier approaches the floor accuracy*(1-accuracy).
Wildly miscalibrated rules sit far above the floor.
"""
from __future__ import annotations

import pytest

from price_agent import compute_brier_30d


def test_returns_none_below_minimum_sample():
    """Anything under n=5 is noise and should suppress to None."""
    for n in range(0, 5):
        assert compute_brier_30d(0.7, [True] * n) is None


def test_perfectly_calibrated_70_pct_rule():
    """Rule claims 0.7, outcomes are 7/10 wins → Brier = floor for that acc.
    Floor = 0.7 * 0.3 = 0.21."""
    outcomes = [True] * 7 + [False] * 3
    brier = compute_brier_30d(0.7, outcomes)
    assert brier == pytest.approx(0.21)


def test_overclaiming_rule_brier_above_floor():
    """Rule CLAIMS 0.9 but actual recent is only 0.5 (5/10 wins).
    Brier = mean((0.9-1)^2 for wins, (0.9-0)^2 for losses)
          = (5 * 0.01 + 5 * 0.81) / 10 = 0.41 — well above floor."""
    outcomes = [True] * 5 + [False] * 5
    brier = compute_brier_30d(0.9, outcomes)
    assert brier == pytest.approx(0.41)
    # Floor for the CLAIM of 0.9 is 0.09 — Brier of 0.41 is 4.6x the floor.
    assert brier > (0.9 * 0.1) * 4


def test_perfect_rule_brier_zero():
    """100% accurate claim that always hits → Brier = 0."""
    assert compute_brier_30d(1.0, [True] * 10) == pytest.approx(0.0)


def test_inverted_rule_high_brier():
    """A rule that claims 0.7 but consistently misses (all 10 outcomes False)
    should produce a high Brier, surfacing the inversion that accuracy alone
    can hide if the rule has only recently started misfiring."""
    brier = compute_brier_30d(0.7, [False] * 10)
    assert brier == pytest.approx(0.49)  # mean((0.7-0)^2) = 0.49


def test_brier_hand_calc_22_wins_8_losses_at_pred_07():
    """Worked example from the migration plan: 22 wins / 8 losses, claim=0.7.
    Per outcome: win contributes (0.7-1)^2 = 0.09, loss contributes 0.49.
    Total = 22*0.09 + 8*0.49 = 1.98 + 3.92 = 5.90; /30 = 0.196667."""
    outcomes = [True] * 22 + [False] * 8
    assert compute_brier_30d(0.7, outcomes) == pytest.approx(0.196667, abs=1e-4)
