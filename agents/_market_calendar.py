"""NYSE trading calendar — full holidays only.

Hardcoded per-year sets keep this offline (no `pandas_market_calendars`
dependency on the GHA runner). Each January, a new year's set must be
appended (NYSE publishes ~2 years out — see
https://www.nyse.com/markets/hours-calendars).

Early-close days (day after Thanksgiving, Christmas Eve, July 3 when the
4th falls on Saturday) are *trading* days for our purposes — the agents
that run intraday don't care whether close is 13:00 ET or 16:00 ET, only
that the session existed. So they're intentionally not in this list.

Usage:
    from _market_calendar import is_trading_day, previous_trading_day
    if is_trading_day(d): ...
"""
from __future__ import annotations

from datetime import date, timedelta

# NYSE full-day closures. Source: nyse.com/markets/hours-calendars.
NYSE_HOLIDAYS_2026: frozenset[date] = frozenset({
    date(2026, 1,  1),   # New Year's Day (Thu)
    date(2026, 1, 19),   # Martin Luther King Jr. Day (Mon)
    date(2026, 2, 16),   # Presidents' Day (Mon)
    date(2026, 4,  3),   # Good Friday
    date(2026, 5, 25),   # Memorial Day (Mon)
    date(2026, 6, 19),   # Juneteenth (Fri)
    date(2026, 7,  3),   # Independence Day observed (Jul 4 is Saturday)
    date(2026, 9,  7),   # Labor Day (Mon)
    date(2026, 11, 26),  # Thanksgiving (Thu)
    date(2026, 12, 25),  # Christmas (Fri)
})

NYSE_HOLIDAYS_2027: frozenset[date] = frozenset({
    date(2027, 1,  1),   # New Year's Day (Fri)
    date(2027, 1, 18),   # MLK Day (Mon)
    date(2027, 2, 15),   # Presidents' Day (Mon)
    date(2027, 3, 26),   # Good Friday
    date(2027, 5, 31),   # Memorial Day (Mon)
    date(2027, 6, 18),   # Juneteenth observed (Jun 19 is Saturday)
    date(2027, 7,  5),   # Independence Day observed (Jul 4 is Sunday)
    date(2027, 9,  6),   # Labor Day (Mon)
    date(2027, 11, 25),  # Thanksgiving (Thu)
    date(2027, 12, 24),  # Christmas observed (Dec 25 is Saturday)
})

ALL_HOLIDAYS: frozenset[date] = NYSE_HOLIDAYS_2026 | NYSE_HOLIDAYS_2027


def is_trading_day(d: date) -> bool:
    """True if NYSE has a regular trading session that day."""
    if d.weekday() >= 5:           # Saturday=5, Sunday=6
        return False
    return d not in ALL_HOLIDAYS


def previous_trading_day(d: date) -> date:
    """Last trading day strictly before d. Useful for 'yesterday' in
    schedule-gap checks that should jump back across weekends/holidays."""
    d = d - timedelta(days=1)
    while not is_trading_day(d):
        d -= timedelta(days=1)
    return d


def next_trading_day(d: date) -> date:
    """First trading day strictly after d. Symmetric to previous_trading_day."""
    d = d + timedelta(days=1)
    while not is_trading_day(d):
        d += timedelta(days=1)
    return d
