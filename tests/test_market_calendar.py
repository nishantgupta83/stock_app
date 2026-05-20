"""Regression tests for the NYSE trading calendar.

Each entry in NYSE_HOLIDAYS_* is a date the orchestrator MUST NOT flag
agents as missing for. Buggy or stale calendars create false positives.
"""
from __future__ import annotations

from datetime import date

import pytest

from _market_calendar import (
    is_trading_day,
    previous_trading_day,
    next_trading_day,
    NYSE_HOLIDAYS_2026,
    NYSE_HOLIDAYS_2027,
)


# ---------- is_trading_day ---------------------------------------------------

def test_weekends_are_not_trading_days():
    # Saturday and Sunday in May 2026
    assert is_trading_day(date(2026, 5, 23)) is False   # Sat
    assert is_trading_day(date(2026, 5, 24)) is False   # Sun


def test_normal_weekdays_are_trading_days():
    # Tue 2026-05-19, Wed 2026-05-20 (not holidays)
    assert is_trading_day(date(2026, 5, 19)) is True
    assert is_trading_day(date(2026, 5, 20)) is True


def test_2026_holidays_are_not_trading_days():
    for d in NYSE_HOLIDAYS_2026:
        assert is_trading_day(d) is False, f"{d} should be closed"


def test_2027_holidays_are_not_trading_days():
    for d in NYSE_HOLIDAYS_2027:
        assert is_trading_day(d) is False, f"{d} should be closed"


def test_juneteenth_2026_is_closed():
    """Spot check — Juneteenth is recent (added 2022) and a likely
    source of false positives if forgotten."""
    assert is_trading_day(date(2026, 6, 19)) is False


def test_thanksgiving_friday_2026_is_open():
    """Black Friday is a half-day, but still a trading day."""
    assert is_trading_day(date(2026, 11, 27)) is True


def test_july_3_2026_observed_holiday():
    """Jul 4 2026 is Saturday → observed Friday Jul 3 is the NYSE closure."""
    assert is_trading_day(date(2026, 7, 3)) is False
    assert is_trading_day(date(2026, 7, 6)) is True   # next Mon


# ---------- previous_trading_day --------------------------------------------

def test_previous_trading_day_skips_weekend():
    """Mon 2026-05-18 → previous trading day is Fri 2026-05-15."""
    assert previous_trading_day(date(2026, 5, 18)) == date(2026, 5, 15)


def test_previous_trading_day_skips_holiday():
    """Wed 2026-07-08 → previous trading day jumps over Jul 3 (Fri holiday)
    and the weekend in between is non-trivial; expect Tue 2026-07-07."""
    assert previous_trading_day(date(2026, 7, 8)) == date(2026, 7, 7)


def test_previous_trading_day_from_a_holiday_itself():
    """Memorial Day 2026 (Mon May 25) → previous trading day is Fri May 22."""
    assert previous_trading_day(date(2026, 5, 25)) == date(2026, 5, 22)


def test_previous_trading_day_strictly_before():
    """The function returns a day strictly before its argument, never the
    argument itself — important for 'yesterday' semantics."""
    d = date(2026, 5, 20)
    assert previous_trading_day(d) != d
    assert previous_trading_day(d) < d


# ---------- next_trading_day ------------------------------------------------

def test_next_trading_day_skips_weekend():
    """Fri 2026-05-15 → next trading day is Mon 2026-05-18."""
    assert next_trading_day(date(2026, 5, 15)) == date(2026, 5, 18)


def test_next_trading_day_skips_holiday():
    """Day before Memorial Day (Fri 2026-05-22) → next is Tue May 26
    (skipping Sat/Sun + Memorial Day Mon)."""
    assert next_trading_day(date(2026, 5, 22)) == date(2026, 5, 26)
