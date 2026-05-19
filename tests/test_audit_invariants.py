"""Regression tests for C2: audit_agent invariant checks.

Each invariant must return (True, detail) when the data is consistent and
(False, detail) when it's not. We mock sb_get to inject the synthetic
scenarios — the agent never hits the live DB.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import audit_agent


def test_invariants_list_has_five_entries():
    """Plan spec: 5 cross-table invariants. Locks the count."""
    assert len(audit_agent.INVARIANTS) == 5
    names = [n for n, _ in audit_agent.INVARIANTS]
    assert "sent_signals_have_dispatch_logs" in names
    assert "sized_decisions_have_live_signals" in names
    assert "calibration_count_consistency" in names
    assert "no_stale_open_paper_trades" in names
    assert "event_cardinality_not_dropping" in names


# ---------- calibration count consistency ------------------------------------

def test_calibration_count_pass(monkeypatch):
    rows = [
        {"rule_key": "a:b:h1d", "n_observations": 10, "n_correct": 7, "n_incorrect": 3},
        {"rule_key": "c::h7d",  "n_observations": 5,  "n_correct": 3, "n_incorrect": 2},
    ]
    monkeypatch.setattr(audit_agent, "sb_get", lambda path, params: rows)
    ok, detail = audit_agent.check_calibration_count_consistency()
    assert ok is True
    assert "2 rules" in detail


def test_calibration_count_fail(monkeypatch):
    rows = [
        {"rule_key": "broken:rule:h1d", "n_observations": 10, "n_correct": 5, "n_incorrect": 3},
        {"rule_key": "fine:rule:h1d",   "n_observations": 8,  "n_correct": 4, "n_incorrect": 4},
    ]
    monkeypatch.setattr(audit_agent, "sb_get", lambda path, params: rows)
    ok, detail = audit_agent.check_calibration_count_consistency()
    assert ok is False
    assert "broken:rule:h1d" in detail


# ---------- stale open paper trades ------------------------------------------

def test_no_stale_open_pass(monkeypatch):
    now = datetime.now(timezone.utc)
    rows = [
        {"id": 1, "horizon_days": 7, "entry_at": (now - timedelta(days=5)).isoformat()},
        {"id": 2, "horizon_days": 30, "entry_at": (now - timedelta(days=25)).isoformat()},
    ]
    monkeypatch.setattr(audit_agent, "sb_get", lambda path, params: rows)
    ok, _ = audit_agent.check_no_stale_open_paper_trades()
    assert ok is True


def test_stale_open_detected(monkeypatch):
    now = datetime.now(timezone.utc)
    rows = [
        {"id": 99, "horizon_days": 1, "entry_at": (now - timedelta(days=20)).isoformat()},
    ]
    monkeypatch.setattr(audit_agent, "sb_get", lambda path, params: rows)
    ok, detail = audit_agent.check_no_stale_open_paper_trades()
    assert ok is False
    assert "id=99" in detail


# ---------- sent signals have dispatch logs ---------------------------------

def test_sent_signals_logged_pass(monkeypatch):
    def fake_sb(path, params):
        if path == "stock_signals":
            return [{"id": 1, "fired_at": "x"}, {"id": 2, "fired_at": "y"}]
        if path == "stock_telegram_dispatch_log":
            return [{"signal_id": 1}, {"signal_id": 2}]
        return []
    monkeypatch.setattr(audit_agent, "sb_get", fake_sb)
    ok, _ = audit_agent.check_sent_signals_have_dispatch_logs()
    assert ok is True


def test_sent_signals_missing_log(monkeypatch):
    def fake_sb(path, params):
        if path == "stock_signals":
            return [{"id": 1}, {"id": 2}, {"id": 99}]
        if path == "stock_telegram_dispatch_log":
            return [{"signal_id": 1}, {"signal_id": 2}]
        return []
    monkeypatch.setattr(audit_agent, "sb_get", fake_sb)
    ok, detail = audit_agent.check_sent_signals_have_dispatch_logs()
    assert ok is False
    assert "99" in detail


# ---------- sized decisions have live signals --------------------------------

def test_sized_decisions_against_live(monkeypatch):
    """A sized decision must reference a setup whose signal valid_until was
    in the future at decision time."""
    now = datetime.now(timezone.utc)
    future = (now + timedelta(days=2)).isoformat()
    def fake_sb(path, params):
        if path == "stock_risk_decisions":
            return [{"setup_id": 1, "created_at": now.isoformat()}]
        if path == "stock_trade_setups":
            return [{"id": 1, "signal_id": 10, "valid_until": future}]
        return []
    monkeypatch.setattr(audit_agent, "sb_get", fake_sb)
    ok, _ = audit_agent.check_sized_decisions_have_live_signals()
    assert ok is True


def test_sized_decisions_on_expired_signal(monkeypatch):
    now = datetime.now(timezone.utc)
    past = (now - timedelta(days=1)).isoformat()
    def fake_sb(path, params):
        if path == "stock_risk_decisions":
            return [{"setup_id": 99, "created_at": now.isoformat()}]
        if path == "stock_trade_setups":
            return [{"id": 99, "signal_id": 10, "valid_until": past}]
        return []
    monkeypatch.setattr(audit_agent, "sb_get", fake_sb)
    ok, detail = audit_agent.check_sized_decisions_have_live_signals()
    assert ok is False
    assert "99" in detail
