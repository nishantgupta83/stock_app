"""M8 — signal reads must be lane-scoped to the thesis lane.

stock_signals is written by 9 producers (thesis, intraday, macro, consumer, ...).
price_agent.fetch_mature_signals graded via price bars but was lane-unscoped, so
it churned on foreign-lane placeholder-ticker rows (MACRO, INST_*) that can never
have bars — they stuck in status_v2='sent' since 5/12. site_generator.count_open_
signals likewise conflated all lanes. Both must filter by THESIS_MODEL_VERSION.
"""
from __future__ import annotations

import price_agent
import site_generator
from _lanes import THESIS_MODEL_VERSION


def test_fetch_mature_signals_is_lane_scoped(monkeypatch):
    captured = {}

    def fake_sb_get(table, params):
        captured.update(params)
        return []

    monkeypatch.setattr(price_agent, "sb_get", fake_sb_get)
    price_agent.fetch_mature_signals()
    assert captured.get("model_version") == f"eq.{THESIS_MODEL_VERSION}"


def test_count_open_signals_is_lane_scoped(monkeypatch):
    captured = {}

    def fake_sb_get(table, params):
        captured.update(params)
        return []

    monkeypatch.setattr(site_generator, "sb_get", fake_sb_get)
    site_generator.count_open_signals()
    assert captured.get("model_version") == f"eq.{THESIS_MODEL_VERSION}"
    assert captured.get("status_v2") == "eq.candidate"
