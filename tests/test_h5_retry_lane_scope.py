"""H5 — retry_dispatch_failed must be lane-scoped to MODEL_VERSION.

Mirror of test_l3_input_filter: an unscoped dispatch_failed sweep picks up
foreign-lane rows (e.g. intraday spikes) and burns thesis's 5/day cap. Pin that
the query filters by model_version so only this lane's failures are retried.
"""
from __future__ import annotations

import thesis_agent


class _Resp:
    def __init__(self, rows): self._rows = rows; self.status_code = 200
    def json(self): return self._rows


def test_retry_query_is_scoped_to_model_version(monkeypatch):
    captured = {}

    def fake_get(url, headers=None, params=None, timeout=None):
        captured.update(params or {})
        return _Resp([])                       # no rows → retry returns 0

    monkeypatch.setattr(thesis_agent.requests, "get", fake_get)
    sent = thesis_agent.retry_dispatch_failed(cap_remaining=5)

    assert sent == 0
    assert captured.get("model_version") == f"eq.{thesis_agent.MODEL_VERSION}"
    assert captured.get("status_v2") == "eq.dispatch_failed"


def test_retry_noop_when_cap_exhausted(monkeypatch):
    # cap<=0 must not even query (no cross-lane read).
    called = {"n": 0}
    monkeypatch.setattr(thesis_agent.requests, "get",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1) or _Resp([]))
    assert thesis_agent.retry_dispatch_failed(cap_remaining=0) == 0
    assert called["n"] == 0
