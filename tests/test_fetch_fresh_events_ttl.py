"""Regression tests for E2: per-event-type TTL filter.

fetch_fresh_events filters on created_at (matches CLAUDE.md rule #1) and
then drops rows whose real-world event_at is older than the event_type's
TTL. Validates the in-Python TTL filter directly so we can test it
without mocking the HTTP layer.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from thesis_agent import (
    _event_within_real_ttl,
    EVENT_REAL_TTL_HOURS,
    EVENT_REAL_TTL_DEFAULT_HOURS,
)


def _event(et: str, event_at_hours_ago: float | None) -> dict:
    if event_at_hours_ago is None:
        return {"event_type": et, "event_at": None}
    now = datetime.now(timezone.utc)
    ea = (now - timedelta(hours=event_at_hours_ago)).isoformat()
    return {"event_type": et, "event_at": ea}


def test_recent_event_passes():
    now = datetime.now(timezone.utc)
    e = _event("earnings_release", event_at_hours_ago=12)
    assert _event_within_real_ttl(e, now) is True


def test_event_just_inside_ttl_passes():
    """Earnings TTL is 72h — 71h ago passes."""
    now = datetime.now(timezone.utc)
    e = _event("earnings_release", event_at_hours_ago=71)
    assert _event_within_real_ttl(e, now) is True


def test_event_outside_ttl_dropped():
    """Earnings TTL is 72h — 96h ago dropped."""
    now = datetime.now(timezone.utc)
    e = _event("earnings_release", event_at_hours_ago=96)
    assert _event_within_real_ttl(e, now) is False


def test_unknown_event_type_uses_default_ttl():
    now = datetime.now(timezone.utc)
    e = _event("some_new_event_type", event_at_hours_ago=EVENT_REAL_TTL_DEFAULT_HOURS - 1)
    assert _event_within_real_ttl(e, now) is True
    e2 = _event("some_new_event_type", event_at_hours_ago=EVENT_REAL_TTL_DEFAULT_HOURS + 1)
    assert _event_within_real_ttl(e2, now) is False


def test_13f_long_ttl_allows_late_arriving_quarterly_data():
    """13F-derived events have a 30-day TTL because the filing arrives weeks
    after the position date. A 25-day-old 13F position is still valid."""
    now = datetime.now(timezone.utc)
    e = _event("institutional_new_position", event_at_hours_ago=25 * 24)
    assert _event_within_real_ttl(e, now) is True


def test_truth_social_short_ttl_drops_day_old():
    """Truth Social TTL is 24h. A 26-hour-old post is stale."""
    now = datetime.now(timezone.utc)
    e = _event("truth_social_post", event_at_hours_ago=26)
    assert _event_within_real_ttl(e, now) is False


def test_missing_event_at_passes():
    """Defensive: never drop a row solely because event_at is missing."""
    now = datetime.now(timezone.utc)
    e = _event("earnings_release", event_at_hours_ago=None)
    assert _event_within_real_ttl(e, now) is True


def test_unparseable_event_at_passes():
    now = datetime.now(timezone.utc)
    e = {"event_type": "earnings_release", "event_at": "not-an-iso-date"}
    assert _event_within_real_ttl(e, now) is True


def test_every_documented_event_type_has_ttl_or_uses_default():
    """The map and the default together should cover every event_type the
    pipeline emits — but we don't enforce a hard list (new event types
    inherit the default). This test just checks the TTL map is non-empty
    and has the major SEC-derived types."""
    for et in ("earnings_release", "8k_material_event", "filing_13d",
               "truth_social_post", "news_article"):
        assert et in EVENT_REAL_TTL_HOURS, f"{et} should have an explicit TTL"
