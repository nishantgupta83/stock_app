"""C2 (downstream) — Layer-3 instrument guard.

Stopping BUY/SELL at Layer 2 is not enough: a non-tradeable signal that gets
downgraded to WATCH can still become a trade_setup and be opened by the
realistic loop. trade_setup_agent must refuse to build an actionable setup for
a non-tradeable instrument (index mutual funds, INST_* placeholders), recording
a reason_to_skip so risk/loop layers leave it alone.
"""
from __future__ import annotations

from trade_setup_agent import compute_setup, derive_rule_key


def _sig(ticker: str) -> dict:
    return {
        "id":             "sig-1",
        "ticker":         ticker,
        "direction":      "bullish",
        "action":         "BUY",
        "valid_until":    None,
        "horizon_days":   1,
        "fired_at":       "2026-06-12T00:00:00Z",
        "weight_at_time": {"primary_event_types": ["8k_material_event"]},
    }


def _mature_cal(sig: dict) -> dict:
    """Calibration that passes every NON-instrument gate (mature, high n/PF)."""
    rk = derive_rule_key(sig)
    return {rk: {"is_mature": True, "n_observations": 200,
                 "accuracy": 0.9, "profit_factor": 3.0,
                 "mean_mfe_pct": 0.04, "mean_mae_pct": -0.02}}


def test_tradeable_ticker_not_skipped() -> None:
    sig = _sig("AAPL")
    out = compute_setup(sig, _mature_cal(sig), tradeable_tickers={"AAPL"})
    assert out["reason_to_skip"] is None


def test_mutual_fund_skipped() -> None:
    sig = _sig("VTSAX")
    out = compute_setup(sig, _mature_cal(sig), tradeable_tickers={"AAPL"})
    assert out["reason_to_skip"] is not None
    assert "tradeable" in out["reason_to_skip"].lower()


def test_inst_placeholder_skipped_even_if_in_set() -> None:
    sig = _sig("INST_VG")
    out = compute_setup(sig, _mature_cal(sig), tradeable_tickers={"INST_VG"})
    assert out["reason_to_skip"] is not None


def test_guard_disabled_when_set_is_none() -> None:
    """tradeable_tickers=None (replay/tests) → guard off; no instrument skip."""
    sig = _sig("VTSAX")
    out = compute_setup(sig, _mature_cal(sig))
    assert out["reason_to_skip"] is None
