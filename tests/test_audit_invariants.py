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

def test_calibration_count_pass_valid_counts(monkeypatch):
    """0 <= n_correct <= n_observations on every row is the contract."""
    rows = [
        {"rule_key": "a:b:h1d", "n_observations": 10, "n_correct": 7},
        {"rule_key": "c::h7d",  "n_observations": 5,  "n_correct": 5},
        {"rule_key": "d::h1d",  "n_observations": 0,  "n_correct": 0},
    ]
    monkeypatch.setattr(audit_agent, "sb_get", lambda path, params: rows)
    ok, detail = audit_agent.check_calibration_count_consistency()
    assert ok is True
    assert "3 rules" in detail


def test_calibration_count_fail_negative_correct(monkeypatch):
    rows = [
        {"rule_key": "broken:rule:h1d", "n_observations": 5, "n_correct": -1},
    ]
    monkeypatch.setattr(audit_agent, "sb_get", lambda path, params: rows)
    ok, detail = audit_agent.check_calibration_count_consistency()
    assert ok is False
    assert "broken:rule:h1d" in detail


def test_calibration_count_fail_correct_exceeds_observations(monkeypatch):
    rows = [
        {"rule_key": "broken:rule:h1d", "n_observations": 10, "n_correct": 12},
        {"rule_key": "fine:rule:h1d",   "n_observations": 8,  "n_correct": 4},
    ]
    monkeypatch.setattr(audit_agent, "sb_get", lambda path, params: rows)
    ok, detail = audit_agent.check_calibration_count_consistency()
    assert ok is False
    assert "broken:rule:h1d" in detail
    assert "fine:rule:h1d" not in detail


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

def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def test_sent_signals_pass_within_window(monkeypatch):
    """Log sent_at within DISPATCH_WINDOW_HOURS of fired_at → ok."""
    fired = datetime.now(timezone.utc)
    sent_at = fired + timedelta(minutes=5)

    def fake_sb(path, params):
        if path == "stock_signals":
            return [{"id": 1, "fired_at": _iso(fired)}]
        if path == "stock_telegram_dispatch_log":
            return [{"signal_id": 1, "sent_at": _iso(sent_at), "delivery_ok": True}]
        return []
    monkeypatch.setattr(audit_agent, "sb_get", fake_sb)
    ok, _ = audit_agent.check_sent_signals_have_dispatch_logs()
    assert ok is True


def test_sent_signals_fail_outside_window(monkeypatch):
    """Log exists with delivery_ok=true but 3h after fired_at → fail."""
    fired = datetime.now(timezone.utc)
    sent_at = fired + timedelta(hours=3)

    def fake_sb(path, params):
        if path == "stock_signals":
            return [{"id": 7, "fired_at": _iso(fired)}]
        if path == "stock_telegram_dispatch_log":
            return [{"signal_id": 7, "sent_at": _iso(sent_at), "delivery_ok": True}]
        return []
    monkeypatch.setattr(audit_agent, "sb_get", fake_sb)
    ok, detail = audit_agent.check_sent_signals_have_dispatch_logs()
    assert ok is False
    assert "outside_window" in detail
    assert "sig=7" in detail


def test_sent_signals_pass_with_resend_closest_wins(monkeypatch):
    """Two delivery_ok=true logs: one 6h late, one 4 min late. The closer one
    wins; signal is in-window → ok. Locks the min() selection rule."""
    fired = datetime.now(timezone.utc)
    late = fired + timedelta(hours=6)
    close = fired + timedelta(minutes=4)

    def fake_sb(path, params):
        if path == "stock_signals":
            return [{"id": 3, "fired_at": _iso(fired)}]
        if path == "stock_telegram_dispatch_log":
            return [
                {"signal_id": 3, "sent_at": _iso(late),  "delivery_ok": True},
                {"signal_id": 3, "sent_at": _iso(close), "delivery_ok": True},
            ]
        return []
    monkeypatch.setattr(audit_agent, "sb_get", fake_sb)
    ok, _ = audit_agent.check_sent_signals_have_dispatch_logs()
    assert ok is True


def test_sent_signals_fail_missing_log(monkeypatch):
    """Sent signal with no dispatch_log row → missing_log bucket."""
    fired = datetime.now(timezone.utc)

    def fake_sb(path, params):
        if path == "stock_signals":
            return [{"id": 99, "fired_at": _iso(fired)}]
        if path == "stock_telegram_dispatch_log":
            return []
        return []
    monkeypatch.setattr(audit_agent, "sb_get", fake_sb)
    ok, detail = audit_agent.check_sent_signals_have_dispatch_logs()
    assert ok is False
    assert "missing_log" in detail
    assert "99" in detail


def test_sent_signals_fail_all_delivery_false(monkeypatch):
    """Two logs for one signal, both delivery_ok=false → missing_log bucket
    (operationally identical to having no log at all)."""
    fired = datetime.now(timezone.utc)

    def fake_sb(path, params):
        if path == "stock_signals":
            return [{"id": 5, "fired_at": _iso(fired)}]
        if path == "stock_telegram_dispatch_log":
            return [
                {"signal_id": 5, "sent_at": _iso(fired + timedelta(minutes=1)), "delivery_ok": False},
                {"signal_id": 5, "sent_at": _iso(fired + timedelta(minutes=2)), "delivery_ok": False},
            ]
        return []
    monkeypatch.setattr(audit_agent, "sb_get", fake_sb)
    ok, detail = audit_agent.check_sent_signals_have_dispatch_logs()
    assert ok is False
    assert "missing_log" in detail
    assert "5" in detail


def test_sent_signals_fail_parse_bad_sent_at(monkeypatch):
    """Log row with malformed sent_at — distinct bucket, not 'missing'."""
    fired = datetime.now(timezone.utc)

    def fake_sb(path, params):
        if path == "stock_signals":
            return [{"id": 11, "fired_at": _iso(fired)}]
        if path == "stock_telegram_dispatch_log":
            return [{"signal_id": 11, "sent_at": "not-a-timestamp", "delivery_ok": True}]
        return []
    monkeypatch.setattr(audit_agent, "sb_get", fake_sb)
    ok, detail = audit_agent.check_sent_signals_have_dispatch_logs()
    assert ok is False
    assert "parse_failed_sent" in detail


def test_sent_signals_fail_parse_bad_fired_at(monkeypatch):
    """Signal with malformed fired_at — distinct bucket from log-parse fails."""
    def fake_sb(path, params):
        if path == "stock_signals":
            return [{"id": 22, "fired_at": "bogus"}]
        if path == "stock_telegram_dispatch_log":
            return []
        return []
    monkeypatch.setattr(audit_agent, "sb_get", fake_sb)
    ok, detail = audit_agent.check_sent_signals_have_dispatch_logs()
    assert ok is False
    assert "parse_failed_fired" in detail
    assert "22" in detail


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
