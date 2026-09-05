"""learning_snapshot must report tiers from RUNTIME truth — the stored `tier` /
`is_mature*` flags + effective_* stats — and must NEVER recompute the adult
(BUY/SELL) gate on RAW n, which over-counts 2-4x. Regression guard for the
2026-08 maturity-truth-alignment fix.

The load-bearing case is the REAL on-disk snapshot shape: every snapshots/*.json
today carries only raw columns + `is_mature` (no `tier`, no `effective_*`). An
earlier version of this fix fell back to running the effective adult gate on raw
n and fabricated 11 "→ adult" crossings for rules the DB marks is_mature=False.
"""
import glob
import json
import os
import sys
from pathlib import Path

# learning_snapshot reads these at import time; set dummies so import doesn't KeyError.
os.environ.setdefault("SUPABASE_URL", "http://localhost")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test-key")
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import learning_snapshot as ls  # noqa: E402


def _rule(**kw):
    base = {"rule_key": "r"}
    base.update(kw)
    return base


# --- stored tier (new snapshots) ------------------------------------------------

def test_tier_for_prefers_stored_tier():
    assert ls._tier_for(_rule(tier="adult", n_observations=5, accuracy=0.1)) == "adult"
    assert ls._tier_for(_rule(tier="child", n_observations=999, accuracy=0.99,
                              profit_factor=9.0, mean_realized_pct=0.05)) == "child"


def test_none_rule_is_child():
    assert ls._tier_for(None) == "child"


# --- effective stats present, no stored tier ------------------------------------

def test_tier_for_uses_effective_gate_when_present():
    adult = _rule(effective_n=120, effective_profit_factor=2.5,
                  effective_mean_realized_pct=0.01, effective_accuracy=0.55)
    assert ls._tier_for(adult) == "adult"          # no accuracy floor for adult
    # thin EFFECTIVE evidence is not adult even with fat raw stats present.
    thin = _rule(effective_n=40, effective_profit_factor=3.0,
                 effective_mean_realized_pct=0.02, effective_accuracy=0.95,
                 n_observations=200, accuracy=0.95, profit_factor=3.0)
    assert ls._tier_for(thin) != "adult"


# --- REAL old-snapshot shape: is_mature flag only, NO tier/effective_* -----------

def test_old_snapshot_uses_is_mature_flag_not_raw_recompute():
    # The 8k_material_event::h30d shape from snapshots/2026-07-13.json: fat raw n,
    # low accuracy, is_mature=False. Must NOT be recomputed to adult on raw n.
    eight_k = _rule(rule_key="8k_material_event::h30d", is_mature=False,
                    n_observations=1140, accuracy=0.4728,
                    profit_factor=2.0204, mean_realized_pct=0.0267)
    assert ls._tier_for(eight_k) == "child"        # was fabricated as "adult"
    # A genuinely-mature old-snapshot rule reports adult from the stored flag.
    assert ls._tier_for(_rule(is_mature=True, n_observations=1140)) == "adult"


def test_adult_shortfall_refuses_raw_only_rules():
    # No effective stats -> cannot assess the production gate -> None (not raw math).
    assert ls._adult_shortfall(_rule(is_mature=False, n_observations=1140,
                                     profit_factor=2.02, mean_realized_pct=0.0267)) is None
    # With effective stats, it lists the unmet criteria.
    unmet = ls._adult_shortfall(_rule(effective_n=80, effective_profit_factor=1.5,
                                      effective_mean_realized_pct=0.001))
    assert unmet is not None
    joined = " ".join(unmet)
    assert "eff_n=80" in joined and "need 100" in joined and "PF=1.50" in joined
    assert ls._adult_shortfall(_rule(tier="adult")) is None


def test_diff_on_old_snapshots_never_fabricates_adult():
    eight_k = _rule(rule_key="8k_material_event::h30d", is_mature=False,
                    n_observations=1140, accuracy=0.4728,
                    profit_factor=2.0204, mean_realized_pct=0.0267)
    text = "\n".join(ls.extract_meaningful_changes(
        {"calibration": [eight_k]}, {"calibration": [eight_k]}))
    assert "→ adult" not in text
    assert "CLOSEST TO ADULT" in text   # header still renders (empty body ok)


def test_stored_tier_promotion_renders():
    s1 = {"calibration": [_rule(rule_key="x", tier="teen")]}
    s2 = {"calibration": [_rule(rule_key="x", tier="young_adult",
                                effective_n=45, effective_profit_factor=1.6)]}
    text = "\n".join(ls.extract_meaningful_changes(s1, s2))
    assert "TIER PROMOTIONS" in text and "x: teen → young_adult" in text
    assert "→ adult" not in text


# --- integration: the actual committed snapshots (the bug was found here) --------

def test_real_snapshots_report_zero_adult_crossings():
    # PINNED to two committed historical snapshots. This test used to glob
    # `snapshots/*.json` and take `[-1]` — a directory a nightly cron appends
    # to — so it asserted a fact about last night's data, not about the code,
    # and went red the day the first real adult rules appeared (2026-09-04).
    # A regression guard must test frozen inputs.
    pinned = [REPO / "snapshots" / "2026-05-26.json",
              REPO / "snapshots" / "2026-05-30.json"]
    if not all(p.exists() for p in pinned):
        return   # fixture snapshots absent in this checkout
    s1 = json.loads(pinned[0].read_text())
    s2 = json.loads(pinned[1].read_text())
    # These snapshots predate the tier/effective_* columns and have 0 mature rules;
    # the report must not invent any adult crossing from raw n.
    assert sum(1 for r in s2["calibration"] if r.get("is_mature")) == 0
    text = "\n".join(ls.extract_meaningful_changes(s1, s2))
    assert "→ adult" not in text
