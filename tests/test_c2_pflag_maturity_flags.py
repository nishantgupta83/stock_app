"""C2-pflag — the maturity gate must be evaluated on FRESH profit_factor.

Bug: upsert_calibration promoted to adult/young_adult using the PREVIOUS
batch's profit_factor (pf_for_gate = prev_pf), recompute_rule_payoff wrote the
fresh PF only afterwards. A rule could promote to adult on a stale PF and stay
until a manual recompute — and thesis (*/5) could read that false-adult and
license a premature BUY/SELL.

derive_maturity_flags() is the single pure gate (one source of truth for
upsert, recompute_rule_payoff, and the recompute_maturity_flags script). These
pin the adult gate (n≥100 AND PF≥2.0 AND mean≥0.5%) so the PF-lag can't
re-promote on a sub-2.0 PF.
"""
from __future__ import annotations

from price_agent import derive_maturity_flags


def test_adult_blocked_when_pf_just_under_2() -> None:
    """The exact PF-lag scenario: n/mean qualify, PF=1.96 < 2.0 → NOT adult."""
    f = derive_maturity_flags(n=120, pf=1.96, mean=0.03, accuracy=0.85)
    assert f["is_mature"] is False
    assert f["tier"] != "adult"


def test_adult_when_pf_crosses_2() -> None:
    f = derive_maturity_flags(n=120, pf=2.1, mean=0.03, accuracy=0.85)
    assert f["is_mature"] is True
    assert f["tier"] == "adult"


def test_adult_needs_n_100() -> None:
    f = derive_maturity_flags(n=99, pf=3.0, mean=0.03, accuracy=0.9)
    assert f["is_mature"] is False


def test_adult_needs_mean_floor() -> None:
    f = derive_maturity_flags(n=200, pf=3.0, mean=0.004, accuracy=0.9)
    assert f["is_mature"] is False


def test_none_pf_never_adult_or_young() -> None:
    """No payoff yet (PF=None) must never satisfy a PF-gated tier."""
    f = derive_maturity_flags(n=200, pf=None, mean=0.03, accuracy=0.9)
    assert f["is_mature"] is False
    assert f["is_mature_80"] is False


def test_teen_needs_no_pf() -> None:
    f = derive_maturity_flags(n=50, pf=None, mean=0.01, accuracy=0.72)
    assert f["is_mature_70"] is True
    assert f["tier"] == "teen"
