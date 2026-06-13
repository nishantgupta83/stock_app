"""H6 — risk_agent portfolio state was computed on a truncated page.

PostgREST silently caps a single response at 2000 rows, so the drawdown
circuit-breaker (closed-30d read) and the concentration cap (open-trades read)
saw ~24-28% of the population (live: 2000 of 8,195 closed / 7,020 open). These
pin the paginate helper that reads the FULL population: assembles every page,
de-dupes by id, and stops at the short page.
"""
from __future__ import annotations

import risk_agent


def test_paginate_assembles_all_pages(monkeypatch) -> None:
    # 1000 + 1000 + 400 → 2400 rows across 3 pages (past the 2000 cap).
    pages = {0: [{"id": i} for i in range(1000)],
             1000: [{"id": i} for i in range(1000, 2000)],
             2000: [{"id": i} for i in range(2000, 2400)]}

    def fake_sb_get(table, params):
        return pages.get(int(params["offset"]), [])

    monkeypatch.setattr(risk_agent, "sb_get", fake_sb_get)
    out = risk_agent._paginate("stock_event_paper_trades", {"status": "eq.open"}, page=1000)
    assert len(out) == 2400
    assert out[0]["id"] == 0 and out[-1]["id"] == 2399


def test_paginate_dedupes_by_id(monkeypatch) -> None:
    # Overlapping id across a page boundary (race) must be counted once.
    pages = {0: [{"id": 1}, {"id": 2}, {"id": 3}] + [{"id": i} for i in range(4, 1001)],
             1000: [{"id": 3}, {"id": 1001}]}  # id=3 repeats

    def fake_sb_get(table, params):
        return pages.get(int(params["offset"]), [])

    monkeypatch.setattr(risk_agent, "sb_get", fake_sb_get)
    out = risk_agent._paginate("t", {}, page=1000)
    ids = [r["id"] for r in out]
    assert len(ids) == len(set(ids))           # no dupes
    assert 1001 in ids and ids.count(3) == 1


def test_paginate_stops_on_short_page(monkeypatch) -> None:
    calls = {"n": 0}

    def fake_sb_get(table, params):
        calls["n"] += 1
        return [{"id": i} for i in range(10)] if int(params["offset"]) == 0 else []

    monkeypatch.setattr(risk_agent, "sb_get", fake_sb_get)
    out = risk_agent._paginate("t", {}, page=1000)
    assert len(out) == 10
    assert calls["n"] == 1                      # short first page → no second fetch


def test_paginate_fails_loud_at_cap(monkeypatch) -> None:
    """Hitting the cap must raise (fail-closed), not silently truncate — a
    truncated risk-breaker population is the bug H6 exists to prevent."""
    import pytest

    def fake_sb_get(table, params):
        return [{"id": int(params["offset"]) + i} for i in range(1000)]  # never short

    monkeypatch.setattr(risk_agent, "sb_get", fake_sb_get)
    with pytest.raises(RuntimeError, match="cap"):
        risk_agent._paginate("t", {}, page=1000, cap=3000)
