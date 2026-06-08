"""Regression tests for thesis_agent signal-insert-failure surfacing.

Incident (2026-06-08, commit 974d967): a stock_signals.action CHECK constraint
silently rejected the entire post-PR1A vocabulary for ~13 days. write_signal
swallowed every 400 and the run finished status='ok' rows_out=0 — invisible in
stock_job_runs. These tests lock in that the NEXT such drift is recorded in
job meta, flips the run to 'partial', and is caught by pulsecheck over a
lookback WINDOW (not just the latest run, which a clean follow-up run hides).
"""
from __future__ import annotations

import pytest

import thesis_agent
from thesis_agent import emit_run_status, write_signal, _INSERT_FAILURES

# pulsecheck lives under agents/pulsecheck/; conftest puts agents/ on path.
import pulsecheck.thesis as pt
from pulsecheck.thesis import classify_emit, worst_emit


# ---------------------------------------------------------------------------
# emit_run_status — the pure run-status decision (Codex #3: ANY failure = partial)
# ---------------------------------------------------------------------------
class TestEmitRunStatus:
    def test_all_inserts_rejected_is_partial(self):
        assert emit_run_status(n_attempted=6, n_emitted=0, n_insert_failed=6) == "partial"

    def test_some_failed_some_emitted_is_partial(self):
        # A run that lost even one signal to a DB rejection did not fully succeed.
        assert emit_run_status(n_attempted=6, n_emitted=5, n_insert_failed=1) == "partial"

    def test_clean_run_is_ok(self):
        assert emit_run_status(n_attempted=6, n_emitted=6, n_insert_failed=0) == "ok"

    def test_no_candidates_is_ok(self):
        assert emit_run_status(n_attempted=0, n_emitted=0, n_insert_failed=0) == "ok"


# ---------------------------------------------------------------------------
# classify_emit — pulsecheck per-run severity (Codex #2: use attempted, not candidates)
# ---------------------------------------------------------------------------
class TestClassifyEmit:
    def test_all_attempted_rejected_is_critical(self):
        status, _ = classify_emit({"n_attempted": 6, "n_emitted": 0, "n_insert_failed": 6})
        assert status == "critical"

    def test_partial_failure_is_warning(self):
        status, _ = classify_emit({"n_attempted": 6, "n_emitted": 5, "n_insert_failed": 1})
        assert status == "warning"

    def test_clean_emit_is_ok(self):
        status, _ = classify_emit({"n_attempted": 6, "n_emitted": 6, "n_insert_failed": 0})
        assert status == "ok"

    def test_empty_emit_is_ok(self):
        status, _ = classify_emit({})
        assert status == "ok"

    def test_dedupe_only_no_attempts_is_ok(self):
        # candidates existed but all were recently_dispatched -> 0 attempted, 0 failed.
        status, _ = classify_emit({"n_candidates": 4, "n_attempted": 0,
                                   "n_emitted": 0, "n_insert_failed": 0})
        assert status == "ok"


# ---------------------------------------------------------------------------
# worst_emit — Codex #1: scan a window, surface the worst run (cadence mismatch:
# thesis runs ~*/5min, pulsecheck hourly; a clean run must not hide a prior fail)
# ---------------------------------------------------------------------------
class TestWorstEmit:
    def test_picks_critical_over_later_clean_run(self):
        runs = [
            {"meta": {"emit": {"n_attempted": 0, "n_emitted": 0, "n_insert_failed": 0}}},  # newest, clean
            {"meta": {"emit": {"n_attempted": 6, "n_emitted": 0, "n_insert_failed": 6}}},  # older, all-rejected
        ]
        status, _ = worst_emit(runs)
        assert status == "critical"

    def test_all_clean_window_is_ok(self):
        runs = [
            {"meta": {"emit": {"n_attempted": 3, "n_emitted": 3, "n_insert_failed": 0}}},
            {"meta": {"emit": {"n_attempted": 0, "n_emitted": 0, "n_insert_failed": 0}}},
        ]
        status, _ = worst_emit(runs)
        assert status == "ok"

    def test_no_emit_meta_is_ok(self):
        status, _ = worst_emit([{"meta": {}}, {"meta": None}])
        assert status == "ok"


# ---------------------------------------------------------------------------
# insert_failures() — end-to-end query path (stubbed sb_get): a window with an
# all-rejected run must report critical even when a later run is clean.
# ---------------------------------------------------------------------------
class TestInsertFailuresCheck:
    def test_window_with_all_rejected_run_is_critical(self, monkeypatch):
        rows = [
            {"status": "ok", "meta": {"emit": {"n_attempted": 0, "n_emitted": 0, "n_insert_failed": 0}}},
            {"status": "partial", "meta": {"emit": {"n_attempted": 6, "n_emitted": 0, "n_insert_failed": 6}}},
        ]
        monkeypatch.setattr(pt, "sb_get", lambda *a, **k: rows)
        result = pt.insert_failures()
        assert result.status == "critical"
        assert result.observed == 6.0   # total failed across window

    def test_clean_window_is_ok(self, monkeypatch):
        rows = [{"meta": {"emit": {"n_attempted": 3, "n_emitted": 3, "n_insert_failed": 0}}}]
        monkeypatch.setattr(pt, "sb_get", lambda *a, **k: rows)
        assert pt.insert_failures().status == "ok"

    def test_no_runs_in_window_is_warning(self, monkeypatch):
        monkeypatch.setattr(pt, "sb_get", lambda *a, **k: [])
        assert pt.insert_failures().status == "warning"


# ---------------------------------------------------------------------------
# write_signal — records the failure instead of silently swallowing it
# ---------------------------------------------------------------------------
class _FakeResp:
    def __init__(self, status_code, body):
        self.status_code = status_code
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


def _evt():
    return {"id": 1, "event_type": "8k_material_event",
            "event_subtype": "earnings_results", "severity": 3}


class TestWriteSignalRecordsFailure:
    def test_rejected_insert_is_recorded_and_returns_none(self, monkeypatch):
        _INSERT_FAILURES.clear()
        monkeypatch.setattr(
            thesis_agent.requests, "post",
            lambda *a, **k: _FakeResp(400, {"code": "23514",
                                            "message": "violates check constraint "
                                                       "stock_signals_action_check"}),
        )
        sig_id = write_signal(
            ticker="ETN", score=62.4, action="CATALYST_RESEARCH", direction="bullish",
            breakdown=[], events=[_evt()], dedupe_key="ETN:8k:test",
        )
        assert sig_id is None
        assert len(_INSERT_FAILURES) == 1
        rec = _INSERT_FAILURES[0]
        assert rec["ticker"] == "ETN"
        assert rec["action"] == "CATALYST_RESEARCH"   # attempted action
        assert rec["code"] == 400

    def test_successful_insert_records_nothing(self, monkeypatch):
        _INSERT_FAILURES.clear()
        monkeypatch.setattr(
            thesis_agent.requests, "post",
            lambda *a, **k: _FakeResp(201, [{"id": 999}]),
        )
        # write_signal_evidence also POSTs; the same stub returns a 201 list — fine.
        sig_id = write_signal(
            ticker="ETN", score=62.4, action="CATALYST_RESEARCH", direction="bullish",
            breakdown=[], events=[_evt()], dedupe_key="ETN:8k:test2",
        )
        assert sig_id == 999
        assert _INSERT_FAILURES == []
