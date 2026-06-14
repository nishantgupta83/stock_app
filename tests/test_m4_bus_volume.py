"""M4 — event-bus volume pulse.

The ingest freshness checks pass when an agent RAN ok, but an EDGAR all-429 sweep
(or any bus-write failure) records ok while landing ZERO events — the layer reads
healthy but ingests nothing. classify_bus_volume warns when total events landed
in the window fall below the floor DURING market hours (it's coarse: a total
ingest collapse, not a single sporadic source going quiet).
"""
from __future__ import annotations

from pulsecheck.ingest_agents import classify_bus_volume, BUS_VOLUME_FLOOR


def test_zero_during_market_hours_warns():
    status, detail = classify_bus_volume(0, market_hours=True)
    assert status == "warning" and "stalled" in detail


def test_normal_volume_ok():
    status, _ = classify_bus_volume(25, market_hours=True)
    assert status == "ok"


def test_floor_boundary():
    assert classify_bus_volume(BUS_VOLUME_FLOOR, market_hours=True)[0] == "ok"
    assert classify_bus_volume(BUS_VOLUME_FLOOR - 1, market_hours=True)[0] == "warning"


def test_outside_market_hours_never_warns():
    # Low/zero volume off-hours is normal — must not fire.
    assert classify_bus_volume(0, market_hours=False)[0] == "ok"
