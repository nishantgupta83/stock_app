"""H8 — daily-risk budget must bind ACROSS a batch.

`daily_risk_in_flight_pct` was computed once (from already-persisted decisions)
and reused for every evaluate_setup in the same run, so N individually-sub-cap
sizes could collectively blow past MAX_DAILY_RISK_PCT. evaluate_batch accumulates
each sized trade's max_loss into the in-flight so the cap binds mid-batch.
"""
from __future__ import annotations

from risk_agent import (evaluate_batch, MAX_DAILY_RISK_PCT, RISK_PER_TRADE_PCT,
                        PORTFOLIO_NAV_BASELINE, MATURITY_MULTIPLIER)


def _setup(i):
    return {"id": i, "signal_id": i, "ticker": "FOO", "direction": "long",
            "setup_type": "next_open", "confidence": 0.80, "stop_pct": 0.03,
            "target_pct": 0.05, "horizon_days": 1,
            "rule_key": "8k_material_event::h1d", "reason_to_skip": None}


def _state():
    return {"drawdown_pct": 0.0, "sum_return_30d": 0.0, "n_closed_30d": 0,
            "daily_risk_in_flight_pct": 0.0, "open_per_rule": {}}


def test_batch_stops_sizing_at_the_daily_cap():
    # adult tier → full multiplier → 1% NAV risk per trade; cap 3% → only 3 size.
    cal = {"8k_material_event::h1d": {"tier": "adult"}}
    per_trade = RISK_PER_TRADE_PCT * MATURITY_MULTIPLIER["adult"]   # 0.01
    n_should_size = int(MAX_DAILY_RISK_PCT / per_trade)             # 3
    decisions = evaluate_batch([_setup(i) for i in range(n_should_size + 2)], cal, _state())
    sized = [d for d in decisions if d["decision"] == "size"]
    budget_skips = [d for d in decisions
                    if d["decision"] == "skip" and "daily risk budget" in (d.get("reason") or "")]
    assert len(sized) == n_should_size            # cap reached exactly
    assert len(budget_skips) == 2                 # the rest blocked by the cap
    total_risk = sum(d["max_loss_dollars"] for d in sized) / PORTFOLIO_NAV_BASELINE
    assert total_risk <= MAX_DAILY_RISK_PCT + 1e-9


def test_batch_preserves_single_setup_behavior():
    cal = {"8k_material_event::h1d": {"tier": "adult"}}
    decisions = evaluate_batch([_setup(1)], cal, _state())
    assert len(decisions) == 1 and decisions[0]["decision"] == "size"


def test_prospective_cap_no_single_trade_overshoot():
    """A trade that would PUSH OVER the cap is skipped, not sized to overshoot
    (Codex: 2.5% in-flight + 1% trade must not size to 3.5%)."""
    cal = {"8k_material_event::h1d": {"tier": "adult"}}        # 1% candidate risk
    st = _state(); st["daily_risk_in_flight_pct"] = 0.025      # 2.5% already in flight
    d = evaluate_batch([_setup(1)], cal, st)[0]
    assert d["decision"] == "skip" and "daily risk budget" in d["reason"]


def test_decision_snapshots_state_at_decision_time():
    """Each decision's portfolio_state reflects in-flight AT its own evaluation,
    not the final post-batch value (shared-mutable serialization bug)."""
    cal = {"8k_material_event::h1d": {"tier": "adult"}}
    decisions = evaluate_batch([_setup(i) for i in range(3)], cal, _state())
    inflights = [d["portfolio_state"]["daily_risk_in_flight_pct"] for d in decisions]
    assert inflights == [0.0, 0.01, 0.02]                     # monotonic per-decision snapshot
