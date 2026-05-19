"""Regression tests for thesis_agent.cluster_passes.

§15.3 cluster rule: 2 distinct source agents OR a single-source exception.
Each exception covers an event whose informational quality is high enough
that two-source confirmation is unnecessary. These tests pin each exception
so a refactor of source_agent_for or the exception list can't silently
drop a critical signal path.
"""
from __future__ import annotations

import pytest

from thesis_agent import cluster_passes


def _event(et: str, *, sub: str | None = None, severity: int = 2) -> dict:
    return {"event_type": et, "event_subtype": sub, "severity": severity}


# ---------- baseline rule: 2 distinct source agents --------------------------

def test_two_agents_passes():
    """A filing + a news article = two source agents → cluster passes."""
    events = [_event("8k_material_event"), _event("news_article")]
    ok, reason = cluster_passes(events)
    assert ok is True
    assert reason.startswith("cluster:")


def test_three_agents_passes():
    events = [
        _event("8k_material_event"),
        _event("news_article"),
        _event("truth_social_post"),
    ]
    ok, _ = cluster_passes(events)
    assert ok is True


def test_two_events_same_agent_fails_without_exception():
    """Two filings from the same source agent doesn't satisfy the cluster
    rule (need ≥ 2 distinct agents)."""
    events = [_event("8k_material_event", severity=2), _event("filing_s-3")]
    ok, reason = cluster_passes(events)
    # Both map to source_agent "filing" → 1 distinct agent → no exception
    assert ok is False
    assert reason == "single_source_no_exception"


# ---------- single-source exceptions -----------------------------------------

def test_filing_13d_is_exception():
    ok, reason = cluster_passes([_event("filing_13d")])
    assert ok is True
    assert reason == "exception:sc_13d"


def test_8k_subtype_13d_is_exception():
    """The §15.3 exception covers SC 13D filings emitted as 8-K subtype."""
    ok, reason = cluster_passes([_event("8k_material_event", sub="13D", severity=2)])
    assert ok is True
    assert reason == "exception:sc_13d"


def test_8k_sev3_is_exception():
    ok, reason = cluster_passes([_event("8k_material_event", severity=3)])
    assert ok is True
    assert reason == "exception:8k_sev3"


def test_8k_sev2_is_not_exception():
    """Below sev=3, an 8-K alone doesn't qualify."""
    ok, _ = cluster_passes([_event("8k_material_event", severity=2)])
    assert ok is False


def test_earnings_sev4_is_exception():
    ok, reason = cluster_passes([_event("earnings_release", severity=4)])
    assert ok is True
    assert reason == "exception:earnings_sev4"


def test_earnings_sev3_is_not_exception():
    """Below sev=4, an earnings alone isn't enough."""
    ok, _ = cluster_passes([_event("earnings_release", severity=3)])
    assert ok is False


def test_fda_pdufa_is_exception():
    ok, reason = cluster_passes([_event("fda_pdufa_decision")])
    assert ok is True
    assert reason == "exception:fda_pdufa"


def test_clinical_readout_sev3_is_exception():
    """Critical regression — audit 2026-05-18 noted every clinical_readout
    arrives at sev=3, so without this rule the biotech catalyst path was
    silently dropped."""
    ok, reason = cluster_passes([_event("clinical_readout", severity=3)])
    assert ok is True
    assert reason == "exception:clinical_sev3"


def test_clinical_readout_sev2_is_not_exception():
    ok, _ = cluster_passes([_event("clinical_readout", severity=2)])
    assert ok is False


def test_dod_contract_sev3_is_exception():
    ok, reason = cluster_passes([_event("dod_contract_award", severity=3)])
    assert ok is True
    assert reason == "exception:dod_sev3"


def test_nuclear_license_sev3_is_exception():
    ok, reason = cluster_passes([_event("nuclear_license_approval", severity=3)])
    assert ok is True
    assert reason == "exception:nuclear_sev3"


def test_insider_cluster_buy_is_exception():
    """Cohen/Lou 2012 academically-validated edge — single-source pass."""
    ok, reason = cluster_passes([_event("insider_cluster_buy")])
    assert ok is True
    assert reason == "exception:insider_cluster_buy"


# ---------- negative cases ---------------------------------------------------

def test_empty_cluster_fails():
    ok, reason = cluster_passes([])
    assert ok is False
    assert reason == "single_source_no_exception"


def test_unknown_event_alone_fails():
    """An event that doesn't match any exception and is the only source
    must fail — defensive against new event types being added without
    explicit single-source consideration."""
    ok, _ = cluster_passes([_event("some_speculative_new_event")])
    assert ok is False


def test_lone_truth_social_fails_without_exception():
    """Truth Social alone is not a single-source exception — needs confirmation."""
    ok, _ = cluster_passes([_event("truth_social_post")])
    assert ok is False
