"""Tests for the 2.b meta-label gate core (agents/_metalabel_gate.py).

This is the gate PR-C will wire LIVE to SUPPRESS low-expectancy candidates, so
its decision logic + the walk-forward (leakage-purged) calibration read it
depends on are locked here. Policy (Codex-reviewed):
  n>=MIN_N AND pf>=PF_BAR AND expectancy>0 -> act
  n>=MIN_N AND not profitable              -> watch (suppressed_low_pf)
  n< MIN_N or missing                      -> watch (fail_open_thin)
Walk-forward: a candidate at run_at may only see trades whose outcome was KNOWN
(realized_at) before run_at — this purges future regime info AND the candidate's
own not-yet-closed trade.
"""
from __future__ import annotations

from datetime import datetime, timezone

from _metalabel_gate import gate_decision, walkforward_stats, expectancy_stats

AS_OF = datetime(2026, 4, 1, tzinfo=timezone.utc)


class TestGateDecision:
    def test_calibrated_profitable_acts(self):
        action, reason = gate_decision({"n": 150, "pf": 2.0, "expectancy": 0.03},
                                       pf_bar=1.5, min_n=100)
        assert action == "act"
        assert reason == "calibrated_profitable"

    def test_calibrated_low_pf_is_suppressed(self):
        action, reason = gate_decision({"n": 150, "pf": 1.1, "expectancy": 0.02},
                                       pf_bar=1.5, min_n=100)
        assert action == "watch"
        assert reason == "suppressed_low_pf"

    def test_high_pf_but_negative_expectancy_suppressed(self):
        # PF can look fine on a distorted tail; expectancy>0 is the hard sanity.
        action, reason = gate_decision({"n": 150, "pf": 2.0, "expectancy": -0.01},
                                       pf_bar=1.5, min_n=100)
        assert action == "watch"
        assert reason == "suppressed_low_pf"

    def test_thin_cell_fails_open(self):
        action, reason = gate_decision({"n": 12, "pf": 3.0, "expectancy": 0.05},
                                       pf_bar=1.5, min_n=100)
        assert action == "watch"
        assert reason == "fail_open_thin"

    def test_missing_cell_fails_open(self):
        action, reason = gate_decision(None, pf_bar=1.5, min_n=100)
        assert action == "watch"
        assert reason == "fail_open_thin"


def _trade(rule_key, ret, correct, realized_at, created_at="anchor"):
    # created_at defaults to the same instant as realized_at unless overridden,
    # so the backfill guard is only exercised by the dedicated test.
    ca = realized_at if created_at == "anchor" else created_at
    return {"rule_key": rule_key, "realized_return": ret, "correct": correct,
            "realized_at": realized_at.isoformat() if realized_at else None,
            "created_at": ca.isoformat() if ca else None}


class TestWalkforwardStats:
    def test_only_counts_trades_closed_before_as_of(self):
        # Two wins known before AS_OF, one win known AFTER (must be purged).
        before = datetime(2026, 3, 1, tzinfo=timezone.utc)
        after = datetime(2026, 5, 1, tzinfo=timezone.utc)
        trades = [
            _trade("8k::h15d", 0.04, True, before),
            _trade("8k::h15d", 0.02, True, before),
            _trade("8k::h15d", 0.50, True, after),   # future → excluded
        ]
        st = walkforward_stats(trades, "8k::h15d", AS_OF)
        assert st["n"] == 2          # the future win is purged

    def test_filters_by_rule_key(self):
        before = datetime(2026, 3, 1, tzinfo=timezone.utc)
        trades = [
            _trade("8k::h15d", 0.04, True, before),
            _trade("news::h7d", -0.03, False, before),
        ]
        st = walkforward_stats(trades, "8k::h15d", AS_OF)
        assert st["n"] == 1

    def test_missing_realized_at_excluded(self):
        # Unknown close time → cannot prove it predates as_of → exclude (no leak).
        trades = [_trade("8k::h15d", 0.04, True, None)]
        st = walkforward_stats(trades, "8k::h15d", AS_OF)
        assert st["n"] == 0

    def test_backfilled_trade_excluded_by_created_at(self):
        # Historical realized_at (before AS_OF) but the ROW landed AFTER AS_OF
        # (a backfill) → was not in calibration at as_of → must be purged.
        old_close = datetime(2026, 3, 1, tzinfo=timezone.utc)
        backfilled_row = datetime(2026, 5, 1, tzinfo=timezone.utc)
        trades = [_trade("8k::h15d", 0.9, True, old_close, created_at=backfilled_row)]
        st = walkforward_stats(trades, "8k::h15d", AS_OF)
        assert st["n"] == 0


def test_expectancy_stats_basic():
    st = expectancy_stats([0.04, -0.02, 0.06], [True, False, True])
    assert st["n"] == 3
    assert round(st["win_rate"], 4) == round(2 / 3, 4)
    assert st["pf"] == (0.10 / 0.02)
    assert round(st["expectancy"], 4) == round(0.08 / 3, 4)
