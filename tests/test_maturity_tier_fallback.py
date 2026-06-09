"""M1 — risk_agent.maturity_tier fallback must match the CANONICAL adult gate
(price_agent ADULT_MIN_N/PF/MEAN), not the old acc≥0.90/n≥30/PF>1.5 copy.

The fallback fires only when a calibration row lacks a stored `tier`. Pre-fix it
would have sized a high-accuracy / low-n / negative-expectancy rule as adult —
exactly the payoff-blind mistake the canonical gate was redefined to prevent.
"""
from __future__ import annotations

from risk_agent import maturity_tier
import price_agent


def _cal(**kw):
    base = {"tier": None}  # force the fallback live-computation path
    base.update(kw)
    return base


def test_fallback_adult_requires_canonical_n_and_payoff():
    # Old gate (acc≥0.90, n≥30, PF>1.5) would call this adult; canonical (n≥100)
    # must NOT — n is only 50.
    assert maturity_tier(_cal(accuracy=0.95, n_observations=50,
                              profit_factor=9.0, mean_realized_pct=0.10)) != "adult"


def test_fallback_adult_has_no_accuracy_floor():
    # Payoff-first: high n + PF + positive expectancy = adult even at low acc.
    assert maturity_tier(_cal(accuracy=0.0, n_observations=120,
                              profit_factor=2.5, mean_realized_pct=0.01)) == "adult"


def test_fallback_adult_rejects_negative_expectancy():
    # High acc + high PF but NEGATIVE mean realized → not adult (the exact
    # acc-only trap the canonical gate closes).
    assert maturity_tier(_cal(accuracy=0.91, n_observations=200,
                              profit_factor=9.3, mean_realized_pct=-0.0012)) != "adult"


def test_fallback_boundary_matches_price_agent_constants():
    # Pin the fallback's numbers to the canonical source so they can't drift.
    assert price_agent.ADULT_MIN_N == 100
    assert price_agent.ADULT_MIN_PF == 2.0
    assert price_agent.ADULT_MIN_MEAN == 0.005


def test_stored_tier_is_preferred_over_fallback():
    # When price_agent has written a tier, use it verbatim (fallback is only a net).
    assert maturity_tier({"tier": "adult"}) == "adult"
    assert maturity_tier({"tier": "child", "n_observations": 999,
                          "profit_factor": 9, "mean_realized_pct": 1}) == "child"
