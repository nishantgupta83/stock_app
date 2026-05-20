"""Regression tests for orchestrator_agent.

Pin the two pieces that determine whether an alert fires:
  effective_max_gap_hours — trading-day-aware budget extension
  check_agent — actual stale detection given a mocked fetch_last_run
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest


os.environ.setdefault("SUPABASE_URL", "https://test.local")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "test")

import orchestrator_agent as oa  # noqa: E402


# ---------- effective_max_gap_hours ------------------------------------------

def test_non_trading_only_agent_unchanged_on_weekend():
    """always-on agent gets the same budget regardless of weekday."""
    spec = oa.AgentExpectation("filing_agent", "every 5 min", 1.0, False)
    sat = datetime(2026, 5, 23, 12, 0, tzinfo=timezone.utc)   # Saturday
    mon = datetime(2026, 5, 18, 12, 0, tzinfo=timezone.utc)   # Monday
    assert oa.effective_max_gap_hours(spec, sat) == 1.0
    assert oa.effective_max_gap_hours(spec, mon) == 1.0


def test_trading_only_agent_unchanged_on_trading_day():
    """On a normal weekday, trading_only agents get their base budget."""
    spec = oa.AgentExpectation("biotech_agent", "daily weekdays", 28.0, True)
    wed = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    assert oa.effective_max_gap_hours(spec, wed) == 28.0


def test_trading_only_agent_gets_extra_day_on_saturday():
    """Saturday: +24h slack (last trading day was Friday)."""
    spec = oa.AgentExpectation("biotech_agent", "daily weekdays", 28.0, True)
    sat = datetime(2026, 5, 23, 12, 0, tzinfo=timezone.utc)
    # Sat 23 → last trading day Fri 22 → 1 non-trading day → +24h
    assert oa.effective_max_gap_hours(spec, sat) == 52.0


def test_trading_only_agent_gets_two_extra_days_on_sunday():
    """Sunday: +48h slack."""
    spec = oa.AgentExpectation("biotech_agent", "daily weekdays", 28.0, True)
    sun = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    assert oa.effective_max_gap_hours(spec, sun) == 76.0


def test_trading_only_agent_extra_slack_across_long_weekend():
    """Memorial Day 2026 is Mon May 25 (closed) — so Sun May 24 carries
    only +48h (since Fri 22) but Mon May 25 (still non-trading) gets +72h."""
    spec = oa.AgentExpectation("biotech_agent", "daily weekdays", 28.0, True)
    mon_holiday = datetime(2026, 5, 25, 12, 0, tzinfo=timezone.utc)
    assert oa.effective_max_gap_hours(spec, mon_holiday) == 28.0 + 72.0


# ---------- check_agent ------------------------------------------------------

def test_check_agent_pass_within_budget(monkeypatch):
    spec = oa.AgentExpectation("any_agent", "5min", 1.0, False)
    now = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    last = now - timedelta(minutes=30)
    monkeypatch.setattr(oa, "fetch_last_run", lambda name: last)

    ok, detail, age = oa.check_agent(spec, now)
    assert ok is True
    assert age is not None
    assert age < 1.0
    assert "ago" in detail


def test_check_agent_fail_outside_budget(monkeypatch):
    spec = oa.AgentExpectation("any_agent", "5min", 1.0, False)
    now = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    last = now - timedelta(hours=3)
    monkeypatch.setattr(oa, "fetch_last_run", lambda name: last)

    ok, detail, age = oa.check_agent(spec, now)
    assert ok is False
    assert age == pytest.approx(3.0, rel=0.01)
    assert "budget 1.0h" in detail


def test_check_agent_no_run_ever(monkeypatch):
    spec = oa.AgentExpectation("brand_new_agent", "daily", 26.0, False)
    now = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(oa, "fetch_last_run", lambda name: None)

    ok, detail, age = oa.check_agent(spec, now)
    assert ok is False
    assert age is None
    assert "no run ever" in detail


def test_check_agent_trading_only_passes_on_sunday(monkeypatch):
    """A trading-only agent that last ran Friday is still healthy on Sunday
    because effective_max_gap_hours absorbs the +48h."""
    spec = oa.AgentExpectation("biotech_agent", "daily weekdays", 28.0, True)
    sun = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)   # Sunday
    fri = datetime(2026, 5, 22, 14, 0, tzinfo=timezone.utc)   # Fri 14:00 UTC
    monkeypatch.setattr(oa, "fetch_last_run", lambda name: fri)

    ok, _, _ = oa.check_agent(spec, sun)
    assert ok is True


def test_check_agent_trading_only_fails_when_truly_stale(monkeypatch):
    """Same agent, but last run was a full week earlier — even with the
    weekend slack it's outside budget."""
    spec = oa.AgentExpectation("biotech_agent", "daily weekdays", 28.0, True)
    sun = datetime(2026, 5, 24, 12, 0, tzinfo=timezone.utc)
    last_week = sun - timedelta(days=8)
    monkeypatch.setattr(oa, "fetch_last_run", lambda name: last_week)

    ok, detail, _ = oa.check_agent(spec, sun)
    assert ok is False
    assert "budget" in detail


# ---------- format_summary --------------------------------------------------

def test_format_summary_empty_when_all_healthy():
    now = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    results = [
        (oa.AgentExpectation("a", "5min", 1.0, False), True, "ok", 0.5),
        (oa.AgentExpectation("b", "5min", 1.0, False), True, "ok", 0.6),
    ]
    assert oa.format_summary(results, now) == ""


def test_format_summary_names_stale_agents():
    now = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)
    results = [
        (oa.AgentExpectation("good", "5min", 1.0, False), True, "ok", 0.5),
        (oa.AgentExpectation("stale_one", "5min", 1.0, False), False, "last run 3.0h ago (budget 1.0h)", 3.0),
        (oa.AgentExpectation("stale_two", "5min", 1.0, True),  False, "last run 30.0h ago (budget 1.0h)", 30.0),
    ]
    summary = oa.format_summary(results, now)
    assert "stale_one" in summary
    assert "stale_two" in summary
    assert "(trading_only)" in summary    # the True one is annotated
    assert "good" not in summary
    assert "2/3 agents flagged" in summary
