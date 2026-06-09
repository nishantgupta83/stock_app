"""Unit tests for PR-B validate_metalabel_gate pure helpers.

primary_event must match write_signal's attribution (alphabetically-first
event_type) AND return that event's id; match_label must use the EXACT
(event_id, horizon) paper trade so the backtest label is the candidate's own
outcome, never a neighbour's (Codex review).
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "validate_metalabel_gate",
    Path(__file__).resolve().parents[1] / "scripts" / "validate_metalabel_gate.py",
)
vmg = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(vmg)


def _evt(et, sub="", eid=1):
    return {"id": eid, "event_type": et, "event_subtype": sub, "ticker": "ETN"}


class TestPrimaryEvent:
    def test_alphabetically_first_event_type_wins(self):
        et, sub, eid = vmg.primary_event([_evt("news_article", "positive", 9),
                                          _evt("8k_material_event", "earnings", 3)])
        assert et == "8k_material_event"      # '8' sorts before 'n'
        assert sub == "earnings"
        assert eid == 3                        # id of the primary event, for label

    def test_empty_cluster(self):
        assert vmg.primary_event([]) == (None, "", None)


class TestLabelMatching:
    def _idx(self, *rows):
        # rows: (event_id, horizon_days, ret, correct)
        return vmg.build_label_index([
            {"event_id": eid, "horizon_days": h, "realized_return": ret, "correct": cor}
            for eid, h, ret, cor in rows
        ])

    def test_exact_event_and_horizon(self):
        idx = self._idx((3, 15, 0.05, True), (3, 7, -0.02, False))
        assert vmg.match_label(idx, 3, 15) == (0.05, True)
        assert vmg.match_label(idx, 3, 7) == (-0.02, False)

    def test_none_when_event_absent(self):
        idx = self._idx((3, 15, 0.05, True))
        assert vmg.match_label(idx, 99, 15) is None

    def test_none_when_horizon_absent(self):
        idx = self._idx((3, 15, 0.05, True))
        assert vmg.match_label(idx, 3, 30) is None

    def test_none_for_missing_event_id(self):
        idx = self._idx((3, 15, 0.05, True))
        assert vmg.match_label(idx, None, 15) is None
