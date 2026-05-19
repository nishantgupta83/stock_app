"""Regression tests for A3: signal_direction dilution tie-break.

Pre-A3, a dilution 8-K emitted both an 8k_material_event (+1 bull) and a
filing_dilution event (+1 bear). bear > bull was false → fell through to
bull > 0 → returned "bullish". Clearly-bearish PIPE/ATM filings routed to
the WATCH bucket instead of being flagged as bearish setups.

Also covers the broader signal_direction surface: every event_type in the
function must produce its documented tilt. This is the kind of test that
would have caught A3 the first time.
"""
from __future__ import annotations

import pytest

from thesis_agent import signal_direction


def _event(et: str, *, sub: str | None = None, direction_prior: str = "neutral",
           accession: str | None = None, rel_strength_pct: float | None = None) -> dict:
    payload: dict = {"direction_prior": direction_prior}
    if accession is not None:
        payload["accession_number"] = accession
    if rel_strength_pct is not None:
        payload["rel_strength_pct"] = rel_strength_pct
    return {"event_type": et, "event_subtype": sub, "payload": payload}


# ---------- the bug A3 fixes -------------------------------------------------

def test_dilution_with_parent_8k_routes_bearish():
    """Same-accession 8-K + filing_dilution must come out bearish.

    This is the exact case that pre-A3 returned bullish."""
    events = [
        _event("8k_material_event", accession="0001234-25-000001"),
        _event("filing_dilution", direction_prior="short",
               accession="0001234-25-000001"),
    ]
    assert signal_direction(events) == "bearish"


def test_dilution_without_parent_8k_routes_bearish():
    """Direct filing_dilution (no parent 8-K in cluster) gets double weight
    so a single-source dilution event still produces a bearish signal."""
    events = [
        _event("filing_dilution", direction_prior="short", accession="acc-99"),
    ]
    assert signal_direction(events) == "bearish"


def test_dilution_with_other_bullish_still_bearish_when_clear():
    """Two bull events vs. a dilution-with-parent-8K. Bull side has 8K (suppressed
    because it's the dilution's parent) + a separate truth_social_post long.
    Net: 1 bull (truth) vs. 1 bear (dilution). Falls through bull > 0 → bullish.
    Documents the tie behavior."""
    events = [
        _event("8k_material_event", accession="acc-1"),
        _event("filing_dilution", direction_prior="short", accession="acc-1"),
        _event("truth_social_post", direction_prior="long"),
    ]
    # 8-K suppressed, dilution +1 bear (parent present), truth_social +1 bull
    # tie 1/1 → bull > 0 → bullish
    assert signal_direction(events) == "bullish"


def test_dilution_without_parent_overpowers_one_bull():
    """Standalone dilution (+2) vs. one bullish event (+1) → bearish."""
    events = [
        _event("filing_dilution", direction_prior="short", accession="acc-x"),
        _event("truth_social_post", direction_prior="long"),
    ]
    # dilution=+2 bear (no parent), truth=+1 bull → bear > bull → bearish
    assert signal_direction(events) == "bearish"


# ---------- non-dilution 8-K still bullish -----------------------------------

def test_plain_8k_no_dilution_is_bullish():
    """An 8-K that is NOT a dilution (no matching filing_dilution event)
    should still count as bullish — A3 must not break the normal case."""
    events = [_event("8k_material_event", accession="acc-77")]
    assert signal_direction(events) == "bullish"


# ---------- event_type coverage (the broader regression) ---------------------

@pytest.mark.parametrize("et", ["filing_13d", "filing_s-3", "filing_s-3/a"])
def test_directional_filings(et):
    events = [_event(et)]
    expected = "bullish" if et == "filing_13d" else "bearish"
    assert signal_direction(events) == expected


@pytest.mark.parametrize("sub,expected", [("beat", "bullish"), ("miss", "bearish")])
def test_earnings_release_by_subtype(sub, expected):
    events = [_event("earnings_release", sub=sub)]
    assert signal_direction(events) == expected


@pytest.mark.parametrize("direction_prior,expected", [
    ("long", "bullish"), ("short", "bearish"),
])
def test_truth_social_by_direction_prior(direction_prior, expected):
    events = [_event("truth_social_post", direction_prior=direction_prior)]
    assert signal_direction(events) == expected


@pytest.mark.parametrize("rs,expected", [
    (10.0, "bullish"), (-10.0, "bearish"),
])
def test_momentum_by_rel_strength(rs, expected):
    events = [_event("momentum", rel_strength_pct=rs)]
    assert signal_direction(events) == expected


def test_momentum_below_threshold_is_neutral():
    """rs in (-5, +5) doesn't move direction."""
    events = [_event("momentum", rel_strength_pct=2.0)]
    assert signal_direction(events) == "neutral"


@pytest.mark.parametrize("et", [
    "institutional_new_position", "institutional_increase", "activist_5pct_crossed",
])
def test_institutional_in_is_bullish(et):
    assert signal_direction([_event(et)]) == "bullish"


@pytest.mark.parametrize("et", ["institutional_exit", "institutional_decrease"])
def test_institutional_out_is_bearish(et):
    assert signal_direction([_event(et)]) == "bearish"


def test_empty_cluster_is_neutral():
    assert signal_direction([]) == "neutral"


def test_unknown_event_type_does_not_contribute():
    events = [_event("some_new_event_type")]
    assert signal_direction(events) == "neutral"


# ---------- mixed cluster sanity ---------------------------------------------

def test_clear_bullish_cluster():
    events = [
        _event("8k_material_event", accession="acc-a"),
        _event("filing_13d"),
        _event("news_article", direction_prior="long"),
    ]
    assert signal_direction(events) == "bullish"


def test_clear_bearish_cluster():
    events = [
        _event("earnings_release", sub="miss"),
        _event("filing_s-3"),
        _event("news_article", direction_prior="short"),
    ]
    assert signal_direction(events) == "bearish"
