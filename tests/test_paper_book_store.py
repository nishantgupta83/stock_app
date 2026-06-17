"""Local SQLite store for the Paper Book (agents/_paper_book_store.py).

Zero-egress local ledger. Idempotent opens (one position per setup_id) so a
re-run of the local agent can't double-trade — the same invariant the Supabase
realistic_loop gets from its unique(loop_name, setup_id) index.
"""
from __future__ import annotations

import _paper_book_store as store


def _conn(tmp_path):
    c = store.connect(tmp_path / "book.db")
    store.init_state(c, loop_name="paper_book_5k", capital_base=5000.0,
                     max_concurrent=5, per_size=1000.0)
    return c


def _open(c, setup_id=1, ticker="NVDA", direction="long"):
    return store.open_position(c, setup_id=setup_id, signal_id=10, ticker=ticker,
                               direction=direction, opened_at="2026-06-16T20:00:00+00:00",
                               open_price=100.0, notional=1000.0, target_price=112.0,
                               stop_price=93.0, target_pct=0.12, stop_pct=0.07,
                               horizon_days=30, exit_target_date="2026-07-16",
                               valid_until="2026-07-16T00:00:00+00:00")


def test_open_position_is_idempotent_on_setup_id(tmp_path):
    c = _conn(tmp_path)
    assert _open(c, setup_id=1) is True       # first insert
    assert _open(c, setup_id=1) is False      # duplicate setup_id ignored
    assert len(store.all_positions(c)) == 1


def test_open_then_read_back_fields(tmp_path):
    c = _conn(tmp_path)
    _open(c, setup_id=7, ticker="AMD", direction="long")
    rows = store.all_positions(c)
    assert rows[0]["ticker"] == "AMD"
    assert rows[0]["status"] == "open"
    assert rows[0]["notional"] == 1000.0


def test_open_setup_ids_for_dedupe(tmp_path):
    c = _conn(tmp_path)
    _open(c, setup_id=1); _open(c, setup_id=2)
    assert store.open_setup_ids(c) == {1, 2}


def test_close_position_sets_closed_fields(tmp_path):
    c = _conn(tmp_path)
    _open(c, setup_id=1)
    pid = store.all_positions(c)[0]["id"]
    store.close_position(c, pid, closed_at="2026-06-20T20:00:00+00:00",
                         close_price=112.0, close_reason="target",
                         realized_pct=0.119, realized_pnl=119.0,
                         mfe_pct=0.12, mae_pct=-0.01)
    row = store.all_positions(c)[0]
    assert row["status"] == "closed"
    assert row["close_reason"] == "target"
    assert row["realized_pnl"] == 119.0


def test_config_and_marks_roundtrip(tmp_path):
    c = _conn(tmp_path)
    cfg = store.config(c, "paper_book_5k")
    assert cfg["capital_base"] == 5000.0
    assert cfg["max_concurrent"] == 5
    store.set_marks(c, "paper_book_5k", last_open_scan_at="2026-06-16T20:00:00+00:00")
    assert store.config(c, "paper_book_5k")["last_open_scan_at"] == "2026-06-16T20:00:00+00:00"
