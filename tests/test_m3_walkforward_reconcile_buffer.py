"""M3 — walk-forward gate must not count outcomes that weren't reconciled yet.

exit_at is stamped MIDNIGHT UTC of the close date, but the outcome isn't
reconciled until that day's EOD price_agent run (+ 2h-cadence latency). A raw
`exit_at < as_of` therefore leaks a same-day, not-yet-known outcome into the
walk-forward window (optimistic). The fix requires the close to be >= 1 trading
day before as_of so the reconcile definitely ran.
"""
from __future__ import annotations

from datetime import datetime, timezone

from _metalabel_gate import walkforward_stats


def _t(exit_day, ret=0.02, created="2026-05-01T00:00:00+00:00"):
    return {"rule_key": "r::h1d", "realized_return": ret, "correct": ret > 0,
            "exit_at": f"{exit_day}T00:00:00+00:00", "created_at": created}


AS_OF = datetime(2026, 6, 10, 20, 0, tzinfo=timezone.utc)   # Wed 2026-06-10 20:00


def test_same_day_close_is_purged():
    # Close on 2026-06-10 (Wed) midnight; as_of same day 20:00 — its EOD reconcile
    # may not have run by as_of, so it must NOT count (was leaking).
    stats = walkforward_stats([_t("2026-06-10")], "r::h1d", AS_OF)
    assert stats["n"] == 0


def test_prior_settled_close_counts():
    # Close 2026-06-05 (Fri); next trading day Mon 06-08 < as_of 06-10 → settled, counts.
    stats = walkforward_stats([_t("2026-06-05")], "r::h1d", AS_OF)
    assert stats["n"] == 1


def test_mix_keeps_only_settled():
    trades = [_t("2026-06-10"), _t("2026-06-05"), _t("2026-06-04")]
    stats = walkforward_stats(trades, "r::h1d", AS_OF)
    assert stats["n"] == 2          # only the two settled (06-04, 06-05); 06-10 purged


def test_backfill_created_at_guard_still_applies():
    # Settled exit but created_at AFTER as_of (backfilled later) → still excluded.
    t = _t("2026-06-05", created="2026-06-30T00:00:00+00:00")
    assert walkforward_stats([t], "r::h1d", AS_OF)["n"] == 0
