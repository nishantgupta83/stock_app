"""M1 — the backtester must not contaminate live stock_agent_weights.

stock_agent_weights is the LIVE learning table thesis reads (latest-date) + the
dashboard displays. The backtester upserted on_conflict=(agent,date), so a
backtest run overwrote the live-learned weight for any in-window date. There's
no source column to tell them apart. Loop-isolation principle (cf. realistic_loop
must not write calibration): the backtester does NOT write live weights unless
explicitly opted in.
"""
from __future__ import annotations

import backtester


def test_no_live_write_by_default(monkeypatch):
    posts = []
    monkeypatch.setattr(backtester.requests, "post",
                        lambda *a, **k: posts.append(k.get("json")) or _ok())
    monkeypatch.delenv("BACKTEST_WRITE_LIVE_WEIGHTS", raising=False)
    backtester.persist_agent_weights([(_d(), {"news_agent": {"acc": 0.6, "n": 10}})])
    assert posts == []          # default: no live-table write


def test_opt_in_writes(monkeypatch):
    posts = []
    monkeypatch.setattr(backtester.requests, "post",
                        lambda *a, **k: posts.append(k.get("json")) or _ok())
    monkeypatch.setenv("BACKTEST_WRITE_LIVE_WEIGHTS", "true")
    backtester.persist_agent_weights([(_d(), {"news_agent": {"acc": 0.6, "n": 10}})])
    assert posts and posts[0][0]["agent"] == "news_agent"


def _ok():
    class R:
        status_code = 201
        text = ""
    return R()


def _d():
    from datetime import date
    return date(2026, 6, 4)
