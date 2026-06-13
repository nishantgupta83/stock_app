"""C2 — maturity-gate scope. Two independent leaks let premature BUY/SELL out:

  (a) cluster_has_mature_rule checked is_mature at ANY horizon (1,7,15,30) while
      the signal is graded at the EMITTED horizon (h1d). An 8-K mature at h15d
      wrongly licensed an h1d BUY/SELL.
  (b) No instrument-class guard — BUY/SELL fired on index mutual funds (VTSAX,
      VFIAX) and placeholder tickers (INST_*), which are not tradeable vehicles.

These pin the emitted-horizon-only maturity check and the tradeable-instrument
gate so a future refactor can't silently re-open either leak.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import _rule_key
from thesis_agent import (action_for, cluster_has_mature_rule,
                          emitted_horizon_days, score_cluster)

NOW = datetime(2026, 6, 1, 15, 0, tzinfo=timezone.utc)


def _ev(et: str, sub: str | None = None) -> dict:
    return {"event_type": et, "event_subtype": sub}


def _evt(et="8k_material_event", *, event_id=1, ticker="ETN", hours_ago=1.0,
         subtype="", severity=3) -> dict:
    at = (NOW - timedelta(hours=hours_ago)).isoformat()
    return {"id": event_id, "event_type": et, "event_subtype": subtype,
            "ticker": ticker, "event_at": at, "created_at": at,
            "severity": severity, "source_table": "test",
            "parser_confidence": 1.0, "payload": {"direction_prior": "long"}}


# ---------- (a) maturity is checked ONLY at the emitted horizon --------------

def test_mature_at_h15d_does_not_license_h1d() -> None:
    """8-K mature at h15d but NOT at h1d → no maturity at the emitted h1d horizon."""
    cal = {
        _rule_key.derive("8k_material_event", None, 15): {"is_mature": True},
        _rule_key.derive("8k_material_event", None, 1):  {"is_mature": False},
    }
    events = [_ev("8k_material_event")]
    assert cluster_has_mature_rule(events, cal, horizon_days=1) is False
    # sanity: the h15d cell really is mature when asked for that horizon
    assert cluster_has_mature_rule(events, cal, horizon_days=15) is True


def test_mature_at_h1d_licenses_h1d() -> None:
    """A genuinely h1d-mature rule (clinical readout) still licenses h1d."""
    cal = {_rule_key.derive("clinical_readout", "completed", 1): {"is_mature": True}}
    events = [_ev("clinical_readout", "completed")]
    assert cluster_has_mature_rule(events, cal, horizon_days=1) is True


# ---------- (b) BUY/SELL requires a tradeable instrument --------------------

def test_buy_requires_tradeable_instrument() -> None:
    """Mature + bullish + high score, but non-tradeable ticker → must NOT BUY."""
    blocked = action_for(80, "bullish", has_mature_rule=True, tradeable=False)
    assert blocked != "BUY"
    allowed = action_for(80, "bullish", has_mature_rule=True, tradeable=True)
    assert allowed == "BUY"


def test_sell_requires_tradeable_instrument() -> None:
    """Mature + bearish, but non-tradeable ticker → must NOT SELL."""
    blocked = action_for(60, "bearish", has_mature_rule=True, tradeable=False)
    assert blocked != "SELL"
    allowed = action_for(60, "bearish", has_mature_rule=True, tradeable=True)
    assert allowed == "SELL"


def test_non_tradeable_falls_back_not_suppressed() -> None:
    """Blocking BUY/SELL must fall back to the normal action, not suppress."""
    # bullish, score in the WATCH band, mature but non-tradeable → still a signal
    fb = action_for(80, "bullish", has_mature_rule=True, tradeable=False,
                    catalyst_score=5.0)
    assert fb in ("CATALYST_WATCH", "MOMENTUM_ONLY")


# ---------- emitted-horizon helper -----------------------------------------

def test_emitted_horizon_days_parses_robustly() -> None:
    assert emitted_horizon_days([_evt()]) == 1


# ---------- integration: score_cluster threads the guard -------------------

def _mature_h1d_cal() -> dict:
    return {_rule_key.derive("8k_material_event", "", 1): {"is_mature": True}}


def test_score_cluster_h1d_mature_is_licensed_at_emitted_horizon() -> None:
    evs = [_evt(event_id=1), _evt(event_id=2)]
    s = score_cluster(evs, rule_calibration=_mature_h1d_cal(), now=NOW,
                      tradeable_tickers={"ETN"})
    assert s["has_mature_rule"] is True


def test_score_cluster_h15d_only_not_licensed_at_h1d() -> None:
    """An h15d-only mature rule must NOT mark the emitted-h1d cluster mature."""
    cal15 = {_rule_key.derive("8k_material_event", "", 15): {"is_mature": True}}
    s = score_cluster([_evt(event_id=1), _evt(event_id=2)],
                      rule_calibration=cal15, now=NOW, tradeable_tickers={"ETN"})
    assert s["has_mature_rule"] is False


def test_score_cluster_empty_set_blocks_buy_sell() -> None:
    """tradeable_tickers=set() (fail-closed) → never a BUY/SELL action; None
    (guard off) may yield one. Robust to the exact score."""
    evs = [_evt(event_id=1), _evt(event_id=2)]
    cal = _mature_h1d_cal()
    blocked = score_cluster(evs, rule_calibration=cal, now=NOW, tradeable_tickers=set())
    assert blocked["action"] not in ("BUY", "SELL")
    licensed = score_cluster(evs, rule_calibration=cal, now=NOW, tradeable_tickers={"ETN"})
    if licensed["action"] in ("BUY", "SELL"):
        assert blocked["action"] != licensed["action"]
