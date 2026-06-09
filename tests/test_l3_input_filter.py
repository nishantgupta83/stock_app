"""H1 — Layer-3 boundary fix: trade_setup_agent must consume ONLY the Layer-2
lane (rubric) and non-suppressed statuses, not the whole stock_signals table.

Verified leak (2026-06-09): fetch_recent_signals filtered only fired_at, so
intraday-spike (L1) + suppressed thesis signals flowed into L3 setup
construction. These tests lock the allowlist, pin the lane to thesis_agent's
MODEL_VERSION (so a version bump can't silently starve L3), and assert the
starvation telemetry that catches that failure.
"""
from __future__ import annotations

import trade_setup_agent
import thesis_agent
from _lanes import THESIS_MODEL_VERSION, L3_INPUT_STATUSES


def test_lane_constant_matches_thesis_producer():
    # If thesis bumps MODEL_VERSION without updating _lanes, L3 would filter to
    # a dead string and silently produce 0 setups. This pins them together.
    assert THESIS_MODEL_VERSION == thesis_agent.MODEL_VERSION


def test_allowlist_excludes_suppressed():
    assert "suppressed" not in L3_INPUT_STATUSES
    assert set(L3_INPUT_STATUSES) == {"candidate", "sent"}


def _sig(sid, model_version, status_v2):
    return {"id": sid, "ticker": "ETN", "direction": "bullish", "action": "CATALYST_RESEARCH",
            "score": 42, "fired_at": "2026-06-09T14:00:00Z", "valid_until": None,
            "horizon_days": 1, "score_breakdown": [], "weight_at_time": {},
            "model_version": model_version, "status_v2": status_v2}


class TestFetchRecentSignalsFilter:
    def _patch(self, monkeypatch, rows):
        monkeypatch.setattr(trade_setup_agent, "sb_get", lambda *a, **k: rows)

    def test_keeps_only_rubric_candidate_and_sent(self, monkeypatch):
        rows = [
            _sig(1, "rubric-v1.1", "sent"),          # keep
            _sig(2, "rubric-v1.1", "candidate"),     # keep
            _sig(3, "rubric-v1.1", "suppressed"),    # drop (L2 said don't emit)
            _sig(4, "intraday-spike-v1", "sent"),    # drop (L1 lane)
            _sig(5, "rubric-v1.1", "closed"),        # drop (not an emission)
        ]
        self._patch(monkeypatch, rows)
        kept = trade_setup_agent.fetch_recent_signals()
        assert sorted(s["id"] for s in kept) == [1, 2]

    def test_starvation_telemetry(self, monkeypatch):
        # All rows ineligible (intraday + suppressed) → eligible empty → the
        # probe (same stub) finds signals in-window → starved. Egress-minimal:
        # the probe only fires because eligible was empty.
        rows = [_sig(4, "intraday-spike-v1", "sent"),
                _sig(3, "rubric-v1.1", "suppressed")]
        self._patch(monkeypatch, rows)
        kept = trade_setup_agent.fetch_recent_signals()
        assert kept == []
        stats = trade_setup_agent.LAST_INPUT_STATS
        assert stats["n_eligible"] == 0
        assert stats["starved"] is True
