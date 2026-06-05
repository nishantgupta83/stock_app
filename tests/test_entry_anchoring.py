"""FIX-2 (2026-06-05): paper-trade entry must be anchored to the close ON/AFTER
the event's own date — never a pre-event close (which leaked event-day moves
into calibration and flattered profit_factor).

These lock pick_entry_close / _event_anchor_date so a refactor can't silently
restore the latest-close-as-entry leakage.
"""
from __future__ import annotations

from event_paper_agent import pick_entry_close, _event_anchor_date


def _ev(event_at: str | None, created_at: str | None = None) -> dict:
    return {"event_at": event_at, "created_at": created_at, "ticker": "X"}


def _close(ts: str, close: float) -> dict:
    return {"ticker": "X", "ts": ts, "close": close}


def test_anchor_date_uses_event_at_not_created_at():
    # Backfilled event: event happened months ago, landed today. Anchor must be
    # the EVENT date so the entry is the historical event-day close, not today's.
    ev = _ev(event_at="2026-01-15T14:30:00+00:00", created_at="2026-06-05T10:00:00+00:00")
    assert _event_anchor_date(ev) == "2026-01-15"


def test_picks_first_close_on_or_after_event_date():
    ev = _ev("2026-06-03T10:00:00+00:00")
    closes = [
        _close("2026-06-01T00:00:00+00:00", 100.0),  # before event — must NOT be chosen
        _close("2026-06-02T00:00:00+00:00", 101.0),  # before event
        _close("2026-06-03T00:00:00+00:00", 105.0),  # event day — the correct entry
        _close("2026-06-04T00:00:00+00:00", 110.0),
    ]
    assert pick_entry_close(ev, closes)["close"] == 105.0


def test_defers_when_only_pre_event_closes_exist():
    # Intraday event whose same-day close hasn't been ingested yet → None (defer),
    # NOT a fill at yesterday's pre-event close. This is the core leakage guard.
    ev = _ev("2026-06-05T10:00:00+00:00")
    closes = [
        _close("2026-06-03T00:00:00+00:00", 100.0),
        _close("2026-06-04T00:00:00+00:00", 101.0),  # latest available is pre-event
    ]
    assert pick_entry_close(ev, closes) is None


def test_skips_nonpositive_close_and_takes_next_valid():
    ev = _ev("2026-06-03T10:00:00+00:00")
    closes = [
        _close("2026-06-03T00:00:00+00:00", 0.0),    # invalid
        _close("2026-06-04T00:00:00+00:00", 108.0),  # next valid on/after anchor
    ]
    assert pick_entry_close(ev, closes)["close"] == 108.0


def test_live_floor_raises_anchor_to_created_at():
    # Late-ingested LIVE row: event_at is old, created_at is recent. With the
    # live floor we must NOT enter at the old event-day close (hindsight) —
    # anchor is raised to created_at.
    ev = _ev(event_at="2026-06-01T10:00:00+00:00", created_at="2026-06-04T09:00:00+00:00")
    assert _event_anchor_date(ev, floor_created_at=True) == "2026-06-04"
    assert _event_anchor_date(ev, floor_created_at=False) == "2026-06-01"
    closes = [
        _close("2026-06-01T00:00:00+00:00", 100.0),  # event day — hindsight, must NOT pick
        _close("2026-06-04T00:00:00+00:00", 120.0),  # >= created_at — the safe live entry
    ]
    assert pick_entry_close(ev, closes, floor_created_at=True)["close"] == 120.0
    assert pick_entry_close(ev, closes, floor_created_at=False)["close"] == 100.0


def test_none_inputs_defer_safely():
    assert pick_entry_close(_ev(None), [_close("2026-06-03T00:00:00+00:00", 100.0)]) is None
    assert pick_entry_close(_ev("2026-06-03T10:00:00+00:00"), []) is None
    assert _event_anchor_date(_ev("not-a-date")) is None
