"""C1 — stop the calibration-counter ratchet.

Verified bug (2026-06-12): archive_agent runs DRY_RUN (never deletes), yet still
merges the same >90d rows into a cumulative public index every run (ratchet),
and price_agent.enrich_cal_from_archive FLOORS live n_observations at that
inflated index — so calibration n was 1.5-2.5x the true closed-trade count and
re-inflated after any repair.

Fix: (a) DRY_RUN doesn't merge/save the index; (b) the index is versioned —
enrich applies the floor ONLY for the current schema, and a loaded UNVERSIONED
index has its poisoned rule_calibration DROPPED (not carried forward / re-blessed).
"""
from __future__ import annotations

import price_agent
import archive_agent


def test_schema_constants_match():
    # Mirrored in two agents — a test pins them equal so they can't drift.
    assert price_agent.ARCHIVE_INDEX_SCHEMA == archive_agent.ARCHIVE_INDEX_SCHEMA


class TestEnrichVersionGate:
    def test_unversioned_index_does_not_floor(self):
        cal = {"8k::h15d": {"n_observations": 1162, "n_correct": 600}}
        poisoned = {"rule_calibration": {"8k::h15d": {"n_observations": 2285, "n_correct": 1100}}}
        price_agent.enrich_cal_from_archive(cal, poisoned)   # no schema_version
        assert cal["8k::h15d"]["n_observations"] == 1162      # floor NOT applied

    def test_versioned_index_applies_floor(self):
        cal = {"r::h1d": {"n_observations": 50, "n_correct": 25}}
        idx = {"schema_version": archive_agent.ARCHIVE_INDEX_SCHEMA,
               "rule_calibration": {"r::h1d": {"n_observations": 80, "n_correct": 40}}}
        price_agent.enrich_cal_from_archive(cal, idx)
        assert cal["r::h1d"]["n_observations"] == 80          # legit floor when versioned


class TestSanitizeIndex:
    def test_unversioned_index_drops_calibration(self):
        poisoned = {"weeks": ["w1"], "rule_calibration": {"a": {"n_observations": 999}}}
        out = archive_agent.sanitize_index(poisoned)
        assert out["rule_calibration"] == {}                  # poison dropped
        assert out.get("schema_version") == archive_agent.ARCHIVE_INDEX_SCHEMA
        assert out["weeks"] == ["w1"]                          # other sections kept

    def test_versioned_index_kept(self):
        good = {"schema_version": archive_agent.ARCHIVE_INDEX_SCHEMA,
                "rule_calibration": {"a": {"n_observations": 10}}}
        out = archive_agent.sanitize_index(good)
        assert out["rule_calibration"] == {"a": {"n_observations": 10}}
