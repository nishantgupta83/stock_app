"""H2 — after-hours entry-anchor leakage.

An event at/after the 16:00 ET close used to anchor to THAT SAME DAY's 16:00
close (a pre-event price), crediting the overnight gap to the trade. The fix
bumps the anchor to the next calendar day when the event is at/after 16:00 ET
(pick_entry_close then rolls weekends/holidays to the next real session). The
helper must convert to America/New_York BEFORE taking the date — a UTC timestamp
just after midnight is the PRIOR ET day, after-close.
"""
from __future__ import annotations

from event_paper_agent import _entry_anchor_from_ts


def test_during_hours_no_bump():
    # 14:00 UTC = 10:00 EDT (regular session) → same-day close is post-event.
    assert _entry_anchor_from_ts("2026-06-11T14:00:00+00:00") == "2026-06-11"


def test_after_close_bumps_one_day():
    # 20:17 UTC = 16:17 EDT (after the 16:00 close) → next day.
    assert _entry_anchor_from_ts("2026-06-11T20:17:04+00:00") == "2026-06-12"


def test_exactly_1600_et_bumps():
    # 20:00 UTC = 16:00 EDT — the close print is AT 16:00, so an event stamped
    # 16:00:00 is not strictly pre-close → bump (conservative, no leak).
    assert _entry_anchor_from_ts("2026-06-11T20:00:00+00:00") == "2026-06-12"


def test_1559_et_no_bump():
    assert _entry_anchor_from_ts("2026-06-11T19:59:59+00:00") == "2026-06-11"


def test_premarket_no_bump():
    # 11:00 UTC = 07:00 EDT (pre-market) → same-day 16:00 close is post-event.
    assert _entry_anchor_from_ts("2026-06-11T11:00:00+00:00") == "2026-06-11"


def test_midnight_utc_is_prior_et_day_after_close():
    # Codex case: 00:30 UTC Jun 12 = 20:30 EDT Jun 11 (after close) → anchor Jun 12,
    # NOT Jun 13. Converting to ET BEFORE taking the date is essential.
    assert _entry_anchor_from_ts("2026-06-12T00:30:00+00:00") == "2026-06-12"


def test_winter_est_after_close():
    # DST off (EST = UTC-5): 21:30 UTC = 16:30 EST → after close → bump.
    assert _entry_anchor_from_ts("2026-01-15T21:30:00+00:00") == "2026-01-16"
    # 20:30 UTC = 15:30 EST → before close → no bump.
    assert _entry_anchor_from_ts("2026-01-15T20:30:00+00:00") == "2026-01-15"


def test_naive_timestamp_assumed_utc():
    # No tzinfo → assume UTC (never the local machine tz). 20:17Z → 16:17 ET → bump.
    assert _entry_anchor_from_ts("2026-06-11T20:17:04") == "2026-06-12"


def test_garbage_returns_none():
    assert _entry_anchor_from_ts("not-a-date") is None
    assert _entry_anchor_from_ts(None) is None
