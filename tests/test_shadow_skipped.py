import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "agents"))
import _shadow_skipped as s

def test_categorize_skip():
    assert s.categorize_skip("rule 8k_material_event::h1d profit_factor 0.76 < 1.0 (no payoff edge)") == "payoff"
    assert s.categorize_skip("intelligence flagged AVOID_CHASE") == "vocabulary"
    assert s.categorize_skip("CVX not a tradeable instrument (fund/placeholder)") == "instrument"
    assert s.categorize_skip("some new reason") == "other"
    assert s.categorize_skip(None) == "other"

def _row(cat, ret, exc, priceable=True, status="resolved", ticker="X", reason="r"):
    return {"ticker": ticker, "reason_to_skip": reason, "skip_category": cat,
            "priceable": priceable, "status": status, "return_pct": ret, "excess_pct": exc}

def test_aggregate_and_win_rate():
    rows = [_row("payoff", 0.05, 0.02), _row("payoff", -0.03, -0.04)]
    a = s.aggregate(rows)
    assert a["n_resolved"] == 2 and a["win_rate"] == 0.5
    assert a["mean_excess_vs_qqq_pct"] == round((0.02 - 0.04)/2, 4)

def test_aggregate_insufficient():
    assert s.aggregate([_row("payoff", 0, 0, status="unpriceable")])["status"] == "insufficient"

def test_by_category_and_anomaly():
    rows = [_row("payoff", 0.05, 0.02),
            _row("instrument", 0.10, 0.06, ticker="CVX", reason="CVX not a tradeable instrument")]
    bc = s.by_category(rows)
    assert bc["payoff"]["n_resolved"] == 1 and bc["instrument"]["n_resolved"] == 1
    anomalies = s.anomaly_audit(rows)
    assert len(anomalies) == 1 and anomalies[0]["ticker"] == "CVX"
