"""H2 — dashboard must not fold intraday-spike (L1) signals into the Layer-2
count. Pre-2026-06-09 count_alerts_today summed all lanes and overstated L2
~4-6x. These assert the count queries filter by the thesis lane.
"""
from __future__ import annotations

import site_generator
from _lanes import THESIS_MODEL_VERSION


def _capture_params(monkeypatch):
    seen = {}
    def fake_sb_get(table, params):
        seen["table"] = table
        seen["params"] = params
        return []
    monkeypatch.setattr(site_generator, "sb_get", fake_sb_get)
    return seen


def test_count_alerts_today_is_thesis_scoped(monkeypatch):
    seen = _capture_params(monkeypatch)
    site_generator.count_alerts_today()
    assert seen["params"].get("model_version") == f"eq.{THESIS_MODEL_VERSION}"
    assert seen["params"].get("status_v2") == "eq.sent"


def test_count_alerts_split_is_thesis_scoped(monkeypatch):
    seen = _capture_params(monkeypatch)
    site_generator.count_alerts_today_split()
    assert seen["params"].get("model_version") == f"eq.{THESIS_MODEL_VERSION}"


def test_non_thesis_count_derived_no_extra_query():
    # Egress: derived from the already-fetched signals list — adds NO new read.
    sigs = [
        {"model_version": THESIS_MODEL_VERSION, "status_v2": "sent",
         "fired_at": __import__("datetime").datetime.now(
             __import__("datetime").timezone.utc).date().isoformat() + "T10:00:00Z"},
        {"model_version": "intraday-spike-v1", "status_v2": "sent",
         "fired_at": __import__("datetime").datetime.now(
             __import__("datetime").timezone.utc).date().isoformat() + "T10:00:00Z"},
    ]
    assert site_generator.count_non_thesis_today(sigs) == 1   # only the intraday one
