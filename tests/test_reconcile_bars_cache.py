"""Regression test for the reconcile bars_cache window bug.

The bug: bars_cache was sized by the FIRST trade encountered per ticker.
A later trade with a larger horizon for the same ticker would see bars
too narrow to reach its exit target — close_on_or_after returned None,
compute_paper_outcome returned None, and the trade silently stayed open
across every reconcile run. The May 1 2026 bulk backfill exposed this:
42 trades per closeable horizon (h=1, 7, 15) were stuck for 19 days
because the same-ticker h=1 trade always came first and pinned the
cache window to ~May 12 — too narrow for h=15 exit on May 16.

The fix uses _max_end_by_ticker so the cache window is always the widest
horizon for that ticker.
"""
from __future__ import annotations

from datetime import date, timedelta

from price_agent import _max_end_by_ticker


def _trade(ticker: str, entry: date, h: int) -> dict:
    return {
        "ticker":       ticker,
        "entry_at":     entry.isoformat() + "T00:00:00+00:00",
        "horizon_days": h,
    }


def test_single_trade_picks_its_own_horizon():
    entry = date(2026, 5, 1)
    out = _max_end_by_ticker([_trade("V", entry, 7)])
    assert out == {"V": entry + timedelta(days=10)}   # 7 + 3 buffer


def test_widest_horizon_wins_per_ticker():
    """The bug scenario: h=1 first, h=30 last for the same ticker."""
    entry = date(2026, 5, 1)
    trades = [
        _trade("V", entry, 1),
        _trade("V", entry, 7),
        _trade("V", entry, 15),
        _trade("V", entry, 30),
    ]
    out = _max_end_by_ticker(trades)
    assert out == {"V": entry + timedelta(days=33)}   # 30 + 3 buffer


def test_widest_horizon_per_ticker_independent():
    entry = date(2026, 5, 1)
    trades = [
        _trade("V",    entry, 1),
        _trade("V",    entry, 30),
        _trade("AAPL", entry, 7),
        _trade("AAPL", entry, 15),
    ]
    out = _max_end_by_ticker(trades)
    assert out["V"]    == entry + timedelta(days=33)
    assert out["AAPL"] == entry + timedelta(days=18)


def test_skips_unparseable_entry_at():
    """A row with a bad entry_at must not crash _max_end_by_ticker and must
    not contribute to any ticker's window. (Mirrors the silent-skip in the
    main reconcile loop.)"""
    entry = date(2026, 5, 1)
    trades = [
        {"ticker": "BAD", "entry_at": "not-a-date", "horizon_days": 7},
        _trade("V", entry, 1),
    ]
    out = _max_end_by_ticker(trades)
    assert "BAD" not in out
    assert out["V"] == entry + timedelta(days=4)


def test_empty_trades_returns_empty_map():
    assert _max_end_by_ticker([]) == {}


def test_different_entry_dates_picks_latest_window():
    """A ticker can appear with different entry dates in one batch (e.g.
    backfill spanning multiple weeks). Widest end_date (entry + horizon + 3)
    wins, not just widest horizon."""
    old_entry = date(2026, 5, 1)
    new_entry = date(2026, 5, 10)
    trades = [
        _trade("V", old_entry, 30),    # ends May 1 + 33 = Jun 3
        _trade("V", new_entry, 7),     # ends May 10 + 10 = May 20
    ]
    # Jun 3 is later than May 20 → old_entry+h30 wins
    out = _max_end_by_ticker(trades)
    assert out["V"] == old_entry + timedelta(days=33)

    # Flip: a fresher entry with a longer horizon should win.
    trades_flipped = [
        _trade("V", old_entry, 1),     # ends May 1 + 4 = May 5
        _trade("V", new_entry, 30),    # ends May 10 + 33 = Jun 12
    ]
    out2 = _max_end_by_ticker(trades_flipped)
    assert out2["V"] == new_entry + timedelta(days=33)
