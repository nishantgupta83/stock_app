"""Regression tests for D1: count_alerts_today_split.

Pre-D1 the dashboard subtracted alerts_today from 5 and showed the
result, producing "-26 remaining" when sev-4 bypasses pushed the count
above the cap. count_alerts_today_split now classifies each sent signal
by inspecting score_breakdown for the severity_uplift_sev4 rule.
"""
from __future__ import annotations

import pytest

from site_generator import count_alerts_today_split


def _signal(*, has_sev4: bool) -> dict:
    breakdown = []
    if has_sev4:
        breakdown.append({"rule": "severity_uplift_sev4", "points": 15})
    breakdown.append({"rule": "some_other_rule", "points": 5})
    return {"id": 1, "score_breakdown": breakdown}


def test_no_signals_returns_zero_zero(monkeypatch):
    from site_generator import sb_get as real_sb_get  # noqa: F401
    import site_generator
    monkeypatch.setattr(site_generator, "sb_get", lambda path, params: [])
    cap, bypass = count_alerts_today_split()
    assert cap == 0
    assert bypass == 0


def test_all_normal_signals_no_bypass(monkeypatch):
    import site_generator
    rows = [_signal(has_sev4=False) for _ in range(3)]
    monkeypatch.setattr(site_generator, "sb_get", lambda path, params: rows)
    cap, bypass = count_alerts_today_split()
    assert cap == 3
    assert bypass == 0


def test_all_bypass_signals(monkeypatch):
    import site_generator
    rows = [_signal(has_sev4=True) for _ in range(7)]
    monkeypatch.setattr(site_generator, "sb_get", lambda path, params: rows)
    cap, bypass = count_alerts_today_split()
    assert cap == 0
    assert bypass == 7


def test_mixed_returns_correct_split(monkeypatch):
    """The exact case from the review: many sends, some via bypass."""
    import site_generator
    rows = [_signal(has_sev4=False) for _ in range(5)] + \
           [_signal(has_sev4=True) for _ in range(26)]
    monkeypatch.setattr(site_generator, "sb_get", lambda path, params: rows)
    cap, bypass = count_alerts_today_split()
    assert cap == 5
    assert bypass == 26
    # Pre-D1 the dashboard showed `5 - 31 = -26`. Post-fix:
    # "5 / 5 cap used · 26 severity-4 bypass" — never negative.


def test_signal_with_missing_breakdown_is_cap(monkeypatch):
    import site_generator
    monkeypatch.setattr(site_generator, "sb_get",
                        lambda path, params: [{"id": 1, "score_breakdown": None},
                                              {"id": 2}])
    cap, bypass = count_alerts_today_split()
    assert cap == 2
    assert bypass == 0


def test_signal_with_non_list_breakdown_is_cap(monkeypatch):
    """Defensive: if score_breakdown is a string or dict somehow, don't crash."""
    import site_generator
    monkeypatch.setattr(site_generator, "sb_get",
                        lambda path, params: [{"id": 1, "score_breakdown": "broken"},
                                              {"id": 2, "score_breakdown": {}}])
    cap, bypass = count_alerts_today_split()
    assert cap == 2
    assert bypass == 0
