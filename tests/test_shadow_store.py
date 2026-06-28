# tests/test_shadow_store.py
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "agents"))
import _shadow_store as store


def test_ingest_freeze_roundtrip(tmp_path):
    c = store.connect(tmp_path / "s.db"); store.init(c)
    assert store.ingest_setup(c, setup_id=1, ticker="CVX", direction="long",
        created_at="2026-06-22T00:00:00+00:00", target_pct=0.1, stop_pct=-0.03,
        horizon_days=30, reason_to_skip="CVX not a tradeable instrument", skip_category="instrument", raw="{}")
    assert store.ingest_setup(c, setup_id=1, ticker="CVX", direction="long", created_at="x",
        target_pct=None, stop_pct=None, horizon_days=None, reason_to_skip="r", skip_category="instrument", raw="{}") is False
    store.freeze_outcome(c, setup_id=1, ticker="CVX", skip_category="instrument",
        reason_to_skip="CVX not a tradeable instrument", priceable=True, status="resolved",
        entry_date="2026-06-23", entry_px=150.0, exit_date="2026-07-23", exit_px=165.0,
        return_pct=0.10, qqq_return_pct=0.04, excess_pct=0.06)
    assert store.resolved_setup_ids(c) == {1}
    snap = store.export_state(c)
    d = store.connect(tmp_path / "d.db"); store.init(d); store.import_state(d, snap)
    outs = store.all_outcomes(d)
    assert outs[0]["excess_pct"] == 0.06 and store.resolved_setup_ids(d) == {1}
