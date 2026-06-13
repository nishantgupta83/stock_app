"""M6.2 — flows prior-snapshot must not truncate a large 13F filing.

The old single capped query (order filed_at.desc, limit 5000) could cut off a
big filing's holdings; the missing tickers then looked like exits and
current-only tickers like new positions, fabricating 13F diff events. The fix
finds the latest filing then PAGES all of its holdings.
"""
from __future__ import annotations

import flows_agent


class _R:
    def __init__(self, rows): self._rows = rows; self.status_code = 200
    def json(self): return self._rows


def test_pages_full_latest_filing(monkeypatch):
    # Latest filing has 1500 holdings → must page (1000 + 500), not truncate.
    holdings = [{"ticker": f"T{i}", "shares": i} for i in range(1500)]
    calls = {"head": 0, "pages": []}

    def fake_get(url, headers=None, params=None, timeout=None):
        p = dict(params)
        if p.get("select") == "filed_at":                 # head query
            calls["head"] += 1
            return _R([{"filed_at": "2026-05-01T00:00:00+00:00"}])
        assert p["filed_at"] == "eq.2026-05-01T00:00:00+00:00"   # scoped to latest
        off = int(p["offset"])
        calls["pages"].append(off)
        return _R(holdings[off:off + 1000])

    monkeypatch.setattr(flows_agent.requests, "get", fake_get)
    snap = flows_agent.fetch_prior_snapshot("0001", "2026-06-01T00:00:00+00:00")
    assert len(snap) == 1500                              # nothing truncated
    assert snap["T1499"] == 1499
    assert calls["pages"] == [0, 1000]                    # paged through


def test_no_prior_filing_returns_empty(monkeypatch):
    monkeypatch.setattr(flows_agent.requests, "get",
                        lambda *a, **k: _R([]))
    assert flows_agent.fetch_prior_snapshot("0001", "2026-06-01T00:00:00+00:00") == {}
