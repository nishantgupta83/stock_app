"""Direct unit tests for the shared _instruments gate (C2 safety contract).

is_tradeable decides whether a matured rule may license BUY/SELL / an actionable
setup. It's the single source of truth for L2 + L3, so it gets its own tests
(beyond the indirect C2 coverage).
"""
from __future__ import annotations

import _instruments


def test_is_tradeable_stock_in_set():
    assert _instruments.is_tradeable("AAPL", {"AAPL", "MSFT"}) is True


def test_not_in_set_blocked():
    assert _instruments.is_tradeable("VTSAX", {"AAPL"}) is False


def test_inst_placeholder_blocked_even_if_in_set():
    # INST_* are institutional placeholders — never tradeable, even if present.
    assert _instruments.is_tradeable("INST_VG", {"INST_VG"}) is False


def test_empty_or_none_ticker_blocked():
    assert _instruments.is_tradeable("", {"AAPL"}) is False
    assert _instruments.is_tradeable(None, {"AAPL"}) is False


def test_fetch_truncation_guard_returns_none(monkeypatch):
    # A full page (== LIMIT) may be truncated → return None (caller fails closed),
    # never a silently-capped set.
    class _R:
        status_code = 200
        def json(self): return [{"ticker": f"T{i}"} for i in range(_instruments.LIMIT)]
    monkeypatch.setattr(_instruments.requests, "get", lambda *a, **k: _R())
    assert _instruments.fetch_tradeable_tickers("http://x", {}) is None


def test_fetch_failure_returns_none(monkeypatch):
    class _R:
        status_code = 500
        text = "err"
        def json(self): return []
    monkeypatch.setattr(_instruments.requests, "get", lambda *a, **k: _R())
    assert _instruments.fetch_tradeable_tickers("http://x", {}) is None
