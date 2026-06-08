"""Unit tests for PR-B0 replay_cluster_coverage pure helpers.

Lock in the two non-obvious behaviors: (1) reconstruct_clusters trims each
(ticker, event_at-bucket) cluster to a SINGLE production-run visibility window
via created_at, so a long replay can't invent clusters production never saw
(Codex finding #1); (2) candidate_rule_keys mirrors _record_candidates exactly
(set over events x horizons) so coverage is measured on the cells 2.b gates on.
"""
from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Load the script module directly (scripts/ is not a package).
_SPEC = importlib.util.spec_from_file_location(
    "replay_cluster_coverage",
    Path(__file__).resolve().parents[1] / "scripts" / "replay_cluster_coverage.py",
)
rcc = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(rcc)

NOW = datetime(2026, 6, 1, 15, 0, tzinfo=timezone.utc)


def _evt(event_id, ticker, event_type, *, event_at, created_at, subtype=""):
    return {"id": event_id, "ticker": ticker, "event_type": event_type,
            "event_subtype": subtype,
            "event_at": event_at.isoformat(), "created_at": created_at.isoformat()}


class TestReconstructClusters:
    def test_same_ticker_same_bucket_one_cluster(self):
        e1 = _evt(1, "ETN", "8k_material_event", event_at=NOW, created_at=NOW)
        e2 = _evt(2, "ETN", "8k_material_event",
                  event_at=NOW + timedelta(minutes=5), created_at=NOW)
        out = rcc.reconstruct_clusters([e1, e2], freshness_min=180, cluster_window_min=30)
        assert len(out) == 1
        assert len(out[0]["events"]) == 2

    def test_created_far_apart_trims_to_one_run_window(self):
        # Same event_at bucket, but created_at 10h apart (> 180m freshness). The
        # earlier-created event would have been a DIFFERENT production run, so it
        # must be trimmed from the run_at=max(created) window.
        e_old = _evt(1, "ETN", "8k_material_event",
                     event_at=NOW, created_at=NOW - timedelta(hours=10))
        e_new = _evt(2, "ETN", "8k_material_event",
                     event_at=NOW + timedelta(minutes=2), created_at=NOW)
        out = rcc.reconstruct_clusters([e_old, e_new], freshness_min=180, cluster_window_min=30)
        assert len(out) == 1
        assert len(out[0]["events"]) == 1
        assert out[0]["events"][0]["id"] == 2

    def test_different_buckets_are_separate(self):
        e1 = _evt(1, "ETN", "8k_material_event", event_at=NOW, created_at=NOW)
        e2 = _evt(2, "ETN", "8k_material_event",
                  event_at=NOW + timedelta(hours=2), created_at=NOW)
        out = rcc.reconstruct_clusters([e1, e2], freshness_min=180, cluster_window_min=30)
        assert len(out) == 2


class TestCandidateRuleKeys:
    def test_keys_span_events_and_all_horizons(self):
        e = _evt(1, "ETN", "8k_material_event", event_at=NOW, created_at=NOW)
        keys = rcc.candidate_rule_keys([e], horizons=(1, 7, 15, 30))
        assert len(keys) == 4          # one event x four horizons
        assert all("8k_material_event" in k for k in keys)

    def test_empty_event_type_skipped(self):
        e = _evt(1, "ETN", "", event_at=NOW, created_at=NOW)
        assert rcc.candidate_rule_keys([e]) == set()

    def test_by_horizon_partitions_keys(self):
        # candidate×horizon: one event yields exactly one cell per horizon, and
        # the union matches candidate_rule_keys (the per-horizon metric is what
        # 2.b gates on, so partitioning must be exact).
        e = _evt(1, "ETN", "8k_material_event", event_at=NOW, created_at=NOW)
        by_h = rcc.candidate_rule_keys_by_horizon(e and [e], horizons=(1, 7, 15, 30))
        assert set(by_h) == {1, 7, 15, 30}
        assert all(len(v) == 1 for v in by_h.values())
        union = set().union(*by_h.values())
        assert union == rcc.candidate_rule_keys([e], horizons=(1, 7, 15, 30))


class TestClassifyCandidate:
    def test_strict_wins_over_prov(self):
        assert rcc.classify_candidate({"a", "b"}, strict={"b"}, prov={"a"}) == "strict"

    def test_prov_when_no_strict(self):
        assert rcc.classify_candidate({"a"}, strict={"z"}, prov={"a"}) == "prov"

    def test_thin_when_uncalibrated(self):
        assert rcc.classify_candidate({"a"}, strict=set(), prov=set()) == "thin"


class TestCoverageVerdict:
    def test_commit_at_70(self):
        assert rcc.coverage_verdict(50.0, 20.0).startswith("COMMIT")

    def test_narrow_between_60_and_70(self):
        assert rcc.coverage_verdict(40.0, 25.0).startswith("NARROW")

    def test_subthreshold_is_inconclusive_not_defer(self):
        v = rcc.coverage_verdict(30.0, 10.0)
        assert "INCONCLUSIVE" in v and "DEFER" not in v.split("re-measure")[0]
