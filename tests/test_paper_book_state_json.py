import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "agents"))
import _paper_book_store as store


def _seed(conn):
    store.init_state(conn, loop_name="t", capital_base=5000.0, max_concurrent=5, per_size=1000.0)
    store.ingest_setup(conn, setup_id=1, signal_id=10, ticker="AAA", direction="long",
                       created_at="2026-06-20T13:00:00+00:00", target_pct=0.1, stop_pct=-0.03,
                       horizon_days=30, valid_until=None, raw={"x": 1})
    store.open_position(conn, setup_id=1, signal_id=10, ticker="AAA", direction="long",
                        opened_at="2026-06-21T00:00:00+00:00", open_price=100.0, notional=1000.0,
                        target_pct=0.1, stop_pct=-0.03, horizon_days=30)
    pid = next(p["id"] for p in store.all_positions(conn) if p["setup_id"] == 1)
    store.close_position(conn, pid, closed_at="2026-06-25T00:00:00+00:00", close_price=110.0,
                         close_reason="horizon", realized_pct=0.0995, realized_pnl=99.5)
    store.set_forward_epoch(conn, "t", "2026-06-19")


def test_export_import_roundtrip_and_freeze(tmp_path):
    a = store.connect(tmp_path / "a.db"); _seed(a)
    snap = store.export_state(a, "t")
    assert snap["book_state"]["forward_epoch"] == "2026-06-19"
    assert len(snap["book_setups"]) == 1
    assert len(snap["book_positions_closed"]) == 1
    assert store.closed_setup_ids(a) == {1}

    b = store.connect(tmp_path / "b.db")
    store.import_state(b, snap)
    assert store.config(b, "t")["forward_epoch"] == "2026-06-19"
    assert store.all_setups(b)[0]["ticker"] == "AAA"
    closed = [p for p in store.all_positions(b) if p["status"] == "closed"]
    assert closed[0]["realized_pnl"] == 99.5
    assert store.closed_setup_ids(b) == {1}
