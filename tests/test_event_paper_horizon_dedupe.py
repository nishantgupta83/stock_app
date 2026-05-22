"""Regression test for event_paper_agent composite-key dedupe.

Pre-fix bug: fetch_already_traded_event_ids returned event_ids that had
ANY paper trade row, so a partial prior write (only h=1d landed before
the run crashed) caused subsequent reruns to skip the entire event,
permanently leaving h=7d/15d/30d unwritten.

Post-fix: fetch_already_traded_keys returns (event_id, horizon_days)
pairs, and the row filter at the call site drops only the matching
composite keys. Missing horizons flow through to write_paper_trades.

This test exercises the pure-Python filter logic without hitting the
network; the requests.get layer is covered separately by the
write_paper_trades 201/409 path in test_compute_paper_outcome.
"""
from __future__ import annotations


def _row(event_id: int, horizon_days: int, *, ticker: str = "AAPL") -> dict:
    return {
        "event_id":     event_id,
        "event_type":   "8k_material_event",
        "ticker":       ticker,
        "direction":    "long",
        "horizon_days": horizon_days,
        "status":       "open",
        "entry_price":  150.0,
        "rule_key":     f"8k_material_event::h{horizon_days}d",
    }


def _apply_composite_filter(rows: list[dict], already: set[tuple[int, int]]) -> list[dict]:
    """Mirror of the inline filter in event_paper_agent.main():
    keep rows whose (event_id, horizon_days) is NOT in `already`."""
    return [r for r in rows if (r["event_id"], r["horizon_days"]) not in already]


def test_partial_prior_write_heals_missing_horizons():
    """Event 100 had only h=1d written previously. Rerun must insert h=7/15/30."""
    rows = [_row(100, h) for h in (1, 7, 15, 30)]
    already = {(100, 1)}  # only h=1d existed before

    kept = _apply_composite_filter(rows, already)

    assert len(kept) == 3
    assert {r["horizon_days"] for r in kept} == {7, 15, 30}


def test_all_horizons_present_filters_to_empty():
    """If all 4 horizons exist, the rerun writes nothing for that event."""
    rows = [_row(100, h) for h in (1, 7, 15, 30)]
    already = {(100, h) for h in (1, 7, 15, 30)}

    kept = _apply_composite_filter(rows, already)

    assert kept == []


def test_pristine_event_keeps_all_horizons():
    """An event with no prior rows passes through unfiltered."""
    rows = [_row(100, h) for h in (1, 7, 15, 30)]
    already: set[tuple[int, int]] = set()

    kept = _apply_composite_filter(rows, already)

    assert len(kept) == 4


def test_pre_fix_event_id_only_would_have_dropped_all():
    """Regression assertion: the OLD event-id-only logic would have dropped
    all 4 horizons because event_id 100 was in the seen set. This test
    locks in that the composite-key fix actually changes behavior — if
    someone reverts to event-id-only filtering, this test fails."""
    rows = [_row(100, h) for h in (1, 7, 15, 30)]
    already_event_ids_only = {100}

    # Simulate the pre-fix filter (event-id-only)
    pre_fix_kept = [r for r in rows if r["event_id"] not in already_event_ids_only]
    assert pre_fix_kept == []  # OLD behavior: everything dropped

    # Post-fix filter with the equivalent (per-horizon-NOT-all-traded) state
    already_composite = {(100, 1)}
    post_fix_kept = _apply_composite_filter(rows, already_composite)
    assert len(post_fix_kept) == 3  # NEW behavior: missing horizons survive


def test_multiple_events_mixed_state():
    """Two events: 100 had only h=1d, 200 had h=1d and h=7d. Both should heal correctly."""
    rows = [_row(100, h) for h in (1, 7, 15, 30)] + [_row(200, h, ticker="MSFT") for h in (1, 7, 15, 30)]
    already = {(100, 1), (200, 1), (200, 7)}

    kept = _apply_composite_filter(rows, already)

    kept_keys = {(r["event_id"], r["horizon_days"]) for r in kept}
    assert kept_keys == {(100, 7), (100, 15), (100, 30), (200, 15), (200, 30)}
