"""risk_agent.maturity_tier — stored-tier contract (H1).

The stored `tier` column is now gated on EFFECTIVE-n by
price_agent.recompute_rule_payoff (independent ticker-days, not raw trade
count). maturity_tier therefore TRUSTS the stored tier and, when it is absent,
FAILS TO CHILD — it must NOT recompute a tier from raw n_observations (which
over-counts pseudo-replication 2-4x and would size a rule with no independent
evidence). This pins that contract so a future edit can't reintroduce a raw-n
fallback (the M1 fix had already corrected the gate values; H1 removes the
raw-n path entirely).
"""
from __future__ import annotations

from risk_agent import maturity_tier
import price_agent


def _cal(**kw):
    base = {"tier": None}  # no stored tier → fallback path
    base.update(kw)
    return base


def test_null_tier_fails_to_child_even_with_strong_raw_stats():
    # Raw n=120/PF=2.5/positive mean would look adult on RAW stats, but raw n is
    # pseudo-replicated — with no stored (effective-gated) tier, fail to child.
    assert maturity_tier(_cal(accuracy=0.9, n_observations=120,
                              profit_factor=2.5, mean_realized_pct=0.01)) == "child"


def test_null_tier_with_no_stats_is_child():
    assert maturity_tier(_cal()) == "child"
    assert maturity_tier(None) == "child"


def test_stored_tier_is_authoritative():
    # price_agent writes the tier (gated on effective-n) — use it verbatim.
    assert maturity_tier({"tier": "adult"}) == "adult"
    assert maturity_tier({"tier": "young_adult"}) == "young_adult"
    assert maturity_tier({"tier": "teen"}) == "teen"
    # A stored 'child' wins even over (raw) stats that look mature.
    assert maturity_tier({"tier": "child", "n_observations": 999,
                          "profit_factor": 9, "mean_realized_pct": 1}) == "child"


def test_canonical_adult_constants_unchanged():
    # The canonical gate values still live in the shared source.
    assert price_agent.ADULT_MIN_N == 100
    assert price_agent.ADULT_MIN_PF == 2.0
    assert price_agent.ADULT_MIN_MEAN == 0.005
