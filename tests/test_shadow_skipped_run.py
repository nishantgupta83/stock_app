"""Integration test for shadow_skipped.build_report — no network, pure store seeding.

Covers:
  - build_report returns the four expected top-level keys
  - instrument-priceable rows (CVX) appear in anomalies
  - unpriceable rows are NOT in anomalies
  - reason_distribution counts distinct skip reasons from outcomes
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agents"))
sys.path.insert(0, str(ROOT / "scripts"))

import _shadow_store as store  # noqa: E402
import shadow_skipped as orch  # noqa: E402


def _seed(conn) -> None:
    """Seed three frozen outcomes — resolved-payoff, unpriceable-payoff, resolved-instrument."""
    store.init(conn)

    # Setup 1: payoff-categorised, resolved — NVDA
    store.ingest_setup(
        conn, setup_id=1, ticker="NVDA", direction="long",
        created_at="2026-06-01T00:00:00+00:00",
        target_pct=0.10, stop_pct=-0.03, horizon_days=30,
        reason_to_skip="rule x profit_factor 0.76 < 1.0 (no payoff edge)",
        skip_category="payoff", raw="{}")
    store.freeze_outcome(
        conn, setup_id=1, ticker="NVDA", skip_category="payoff",
        reason_to_skip="rule x profit_factor 0.76 < 1.0 (no payoff edge)",
        priceable=True, status="resolved",
        entry_date="2026-06-02", entry_px=100.0, exit_date="2026-07-02", exit_px=110.0,
        return_pct=0.10, qqq_return_pct=0.04, excess_pct=0.06)

    # Setup 2: payoff-categorised, unpriceable ticker
    store.ingest_setup(
        conn, setup_id=2, ticker="BADTICKER", direction="long",
        created_at="2026-06-01T00:00:00+00:00",
        target_pct=0.10, stop_pct=-0.03, horizon_days=30,
        reason_to_skip="rule y profit_factor 0.50 < 1.0 (no payoff edge)",
        skip_category="payoff", raw="{}")
    store.freeze_outcome(
        conn, setup_id=2, ticker="BADTICKER", skip_category="payoff",
        reason_to_skip="rule y profit_factor 0.50 < 1.0 (no payoff edge)",
        priceable=False, status="unpriceable",
        entry_date=None, entry_px=None, exit_date=None, exit_px=None,
        return_pct=None, qqq_return_pct=None, excess_pct=None)

    # Setup 3: instrument-priceable anomaly — CVX
    store.ingest_setup(
        conn, setup_id=3, ticker="CVX", direction="long",
        created_at="2026-06-01T00:00:00+00:00",
        target_pct=0.08, stop_pct=-0.03, horizon_days=30,
        reason_to_skip="CVX not a tradeable instrument",
        skip_category="instrument", raw="{}")
    store.freeze_outcome(
        conn, setup_id=3, ticker="CVX", skip_category="instrument",
        reason_to_skip="CVX not a tradeable instrument",
        priceable=True, status="resolved",
        entry_date="2026-06-02", entry_px=150.0, exit_date="2026-07-02", exit_px=165.0,
        return_pct=0.10, qqq_return_pct=0.04, excess_pct=0.06)


def test_build_report_shape_and_anomaly(tmp_path, monkeypatch):
    monkeypatch.setenv("SHADOW_DB", str(tmp_path / "s.db"))

    conn = store.connect(tmp_path / "s.db")
    _seed(conn)

    report = orch.build_report(conn, sync_ok=True)

    # Top-level keys present
    assert "by_category" in report
    assert "anomalies" in report
    assert "reason_distribution" in report
    assert "captured_at" in report
    assert report["sync_ok"] is True

    # All five standard category keys present
    bc = report["by_category"]
    for cat in ("payoff", "vocabulary", "instrument", "other", "overall_priceable"):
        assert cat in bc, f"missing category key: {cat}"

    # CVX (instrument + priceable) must appear in anomalies
    anomaly_tickers = [a["ticker"] for a in report["anomalies"]]
    assert "CVX" in anomaly_tickers, f"CVX not in anomalies: {report['anomalies']}"

    # BADTICKER is unpriceable (payoff category) — must NOT be in anomalies
    assert "BADTICKER" not in anomaly_tickers

    # reason_distribution covers our three distinct skip reasons
    assert len(report["reason_distribution"]) == 3
