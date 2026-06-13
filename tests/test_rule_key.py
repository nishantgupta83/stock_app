"""Regression tests for A2: unified rule_key.

Before this fix, event_paper_agent wrote `earnings_release:beat:h7d` while
trade_setup_agent looked up `earnings_release::h7d` — every signal with a
non-empty event subtype silently fell back to default confidence. These tests
confirm both agents now produce the same rule_key for the same logical event.
"""
from __future__ import annotations

import pytest

import _rule_key
from event_paper_agent import derive_rule_key as paper_derive
from trade_setup_agent import (
    derive_primary_event_type,
    derive_primary_event_subtype,
    derive_rule_key as setup_derive,
)


# ---------- canonical _rule_key.derive ---------------------------------------

def test_canonical_format_with_subtype():
    assert _rule_key.derive("earnings_release", "beat", 7) == "earnings_release:beat:h7d"


def test_canonical_format_without_subtype():
    assert _rule_key.derive("8k_material_event", None, 1) == "8k_material_event::h1d"


def test_canonical_strips_whitespace_from_subtype():
    assert _rule_key.derive("foo", "  bar  ", 30) == "foo:bar:h30d"


def test_canonical_empty_string_subtype_becomes_empty_middle():
    assert _rule_key.derive("foo", "", 15) == "foo::h15d"


def test_canonical_horizon_coerced_to_int():
    # Defensive: callers may pass a float
    assert _rule_key.derive("foo", "bar", 7.0) == "foo:bar:h7d"


# ---------- event_paper_agent and trade_setup_agent produce the same key -----

def _event(et: str, sub: str | None = None) -> dict:
    return {"event_type": et, "event_subtype": sub}


def _signal(primary_event_type: str, primary_event_subtype: str | None,
            horizon_days: int = 1) -> dict:
    return {
        "horizon_days": horizon_days,
        "weight_at_time": {
            "primary_event_types": [primary_event_type],
            "primary_event_subtype": primary_event_subtype,
        },
    }


def test_paper_and_setup_agree_on_subtyped_event_h1():
    """The exact case the review called out as silent calibration loss."""
    event = _event("earnings_release", "beat")
    signal = _signal("earnings_release", "beat", horizon_days=1)
    assert paper_derive(event, 1) == setup_derive(signal) == "earnings_release:beat:h1d"


def test_paper_and_setup_agree_on_subtyped_event_h7():
    event = _event("earnings_release", "miss")
    # signal.horizon_days = 0 → trade_setup maps to h7d
    signal = _signal("earnings_release", "miss", horizon_days=0)
    assert paper_derive(event, 7) == setup_derive(signal) == "earnings_release:miss:h7d"


def test_paper_and_setup_agree_on_subtypeless_event():
    event = _event("8k_material_event", None)
    signal = _signal("8k_material_event", None, horizon_days=1)
    assert paper_derive(event, 1) == setup_derive(signal) == "8k_material_event::h1d"


# ---------- pre-A2 mismatch regression ---------------------------------------

def test_pre_a2_mismatch_no_longer_happens():
    """Before A2: setup_derive would produce '...::...' and never match the
    'earnings_release:beat:...' rows event_paper_agent wrote. After A2 they
    match."""
    event = _event("earnings_release", "beat")
    signal = _signal("earnings_release", "beat", horizon_days=1)
    paper_key = paper_derive(event, 1)
    setup_key = setup_derive(signal)
    assert paper_key == setup_key
    # And the new setup_key is NOT the old broken subtype-less form
    assert setup_key != "earnings_release::h1d"


# ---------- backward compatibility with older signals ------------------------

def test_signal_without_subtype_field_falls_back_to_subtypeless():
    """Signals fired before A2 don't have weight_at_time.primary_event_subtype.
    They should derive to the subtype-less key — won't match granular calibration
    rows, but won't crash. Old behavior preserved for legacy data."""
    signal_legacy = {
        "horizon_days": 1,
        "weight_at_time": {"primary_event_types": ["earnings_release"]},
    }
    assert setup_derive(signal_legacy) == "earnings_release::h1d"


def test_signal_with_no_weight_at_time_returns_none():
    assert setup_derive({"horizon_days": 1}) is None
    assert setup_derive({"horizon_days": 1, "weight_at_time": {}}) is None


# ---------- thesis_agent.cluster_has_mature_rule uses canonical too ----------

def test_cluster_has_mature_rule_uses_canonical():
    """Regression: thesis_agent's maturity check must produce the same keys
    event_paper_agent writes, otherwise mature rules never unlock vocabulary.

    C2: maturity is checked at the EMITTED horizon only — so the h7d key is
    matched when we ask for horizon_days=7, but NOT at the default emitted
    horizon (h1d). See tests/test_c2_maturity_gate_scope.py for the scope fix.
    """
    from thesis_agent import cluster_has_mature_rule

    events = [_event("earnings_release", "beat")]
    # Calibration row keyed exactly as event_paper_agent would have written it
    calibration = {"earnings_release:beat:h7d": {"is_mature": True}}
    assert cluster_has_mature_rule(events, calibration, horizon_days=7) is True
    # C2: an h7d-only mature rule must NOT license the emitted h1d horizon.
    assert cluster_has_mature_rule(events, calibration) is False


def test_cluster_has_mature_rule_negative():
    from thesis_agent import cluster_has_mature_rule
    events = [_event("earnings_release", "beat")]
    calibration = {"earnings_release:beat:h7d": {"is_mature": False}}
    assert cluster_has_mature_rule(events, calibration) is False


def test_cluster_has_mature_rule_no_match():
    from thesis_agent import cluster_has_mature_rule
    events = [_event("8k_material_event", None)]
    # Wrong key in calibration — no horizon variant matches
    calibration = {"earnings_release:beat:h7d": {"is_mature": True}}
    assert cluster_has_mature_rule(events, calibration) is False
