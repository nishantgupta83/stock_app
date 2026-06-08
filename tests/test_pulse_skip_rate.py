"""Regression tests for pulsecheck_price_agent.reconcile_skip_rate volume guard.

Incident (2026-06-08, Monday): 6 Saturday-exit paper trades were pending
Monday's not-yet-ingested EOD bar. With that run's total reconcile volume also
small, 6/total crossed the 20% CRITICAL threshold — a false alarm. The skip
RATE is volume-sensitive; a tiny absolute count looks catastrophic at low
volume. An absolute-skip floor suppresses that while still firing on the
513-stuck-h1d regression (large absolute count).
"""
from __future__ import annotations

from pulsecheck.price_agent import classify_skip_rate, MIN_SKIP_ABS


class TestClassifySkipRate:
    def test_few_pending_weekend_trades_do_not_page(self):
        # 6 pending no_bars, tiny total → 75% rate but below absolute floor.
        # The floor must drop this off the CRITICAL page (the false-alarm we're
        # fixing); it self-clears once Monday's EOD bar lands. A warning is
        # acceptable — it doesn't page and isn't the reported problem.
        status, rate = classify_skip_rate(closed=2, no_bars=6, no_outcome=0)
        assert status != "critical"
        assert rate > 0.5  # the raw rate IS high — the floor is what de-pages it

    def test_low_volume_total_failure_still_warns(self):
        # Codex blind-spot guard: a small but PERSISTENT 100%-skip failure must
        # not be silenced just because it's below the absolute floor.
        status, rate = classify_skip_rate(closed=0, no_bars=19, no_outcome=0)
        assert status == "warning"
        assert rate == 1.0

    def test_large_stuck_backlog_is_critical(self):
        # The 513-stuck regression: large absolute count, high rate.
        status, _ = classify_skip_rate(closed=100, no_bars=513, no_outcome=0)
        assert status == "critical"

    def test_moderate_skip_above_floor_warns(self):
        status, _ = classify_skip_rate(closed=200, no_bars=30, no_outcome=0)
        assert status == "warning"

    def test_clean_reconcile_is_ok(self):
        status, rate = classify_skip_rate(closed=300, no_bars=0, no_outcome=0)
        assert status == "ok"
        assert rate == 0.0

    def test_nothing_to_reconcile_is_ok(self):
        status, rate = classify_skip_rate(closed=0, no_bars=0, no_outcome=0)
        assert status == "ok"
        assert rate == 0.0

    def test_floor_is_meaningfully_above_a_handful(self):
        # Guard against a future edit silencing real regressions: the floor must
        # exceed a small weekend batch but stay well below the 513 incident.
        assert 10 <= MIN_SKIP_ABS <= 100
