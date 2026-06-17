"""Local SQLite store for the Paper Book — zero Supabase egress for the ledger.

Event-sourced (Codex): `book_setups` durably mirrors every trade-setup pulled from
the pipeline (the cursor advances after THIS write, decoupled from open decisions);
`book_positions` is the replayed portfolio (idempotent — one position per setup_id,
so a re-run can't double-trade). Portfolio cash/pnl/drawdown are NOT stored — they
are derived from the ledger via _paper_book.recompute_state.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS book_setups (
  setup_id     INTEGER PRIMARY KEY,
  signal_id    INTEGER,
  ticker       TEXT,
  direction    TEXT,
  created_at   TEXT,
  target_pct   REAL,
  stop_pct     REAL,
  horizon_days INTEGER,
  valid_until  TEXT,
  raw          TEXT
);
CREATE TABLE IF NOT EXISTS book_positions (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  setup_id      INTEGER UNIQUE,
  signal_id     INTEGER,
  ticker        TEXT,
  direction     TEXT,
  opened_at     TEXT,
  open_price    REAL,
  notional      REAL,
  target_price  REAL,
  stop_price    REAL,
  target_pct    REAL,
  stop_pct      REAL,
  horizon_days  INTEGER,
  exit_target_date TEXT,
  valid_until   TEXT,
  status        TEXT DEFAULT 'open',
  closed_at     TEXT,
  close_price   REAL,
  close_reason  TEXT,
  realized_pct  REAL,
  realized_pnl  REAL,
  mfe_pct       REAL,
  mae_pct       REAL
);
CREATE TABLE IF NOT EXISTS book_state (
  loop_name         TEXT PRIMARY KEY,
  capital_base      REAL,
  max_concurrent    INTEGER,
  per_position_size REAL,
  last_open_scan_at TEXT,
  last_mark_at      TEXT,
  setup_cursor      TEXT
);
"""


def connect(db_path) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def init_state(conn, *, loop_name, capital_base, max_concurrent, per_size) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO book_state "
        "(loop_name, capital_base, max_concurrent, per_position_size) VALUES (?,?,?,?)",
        (loop_name, capital_base, max_concurrent, per_size))
    conn.commit()


# --- event-sourced setup ingest -------------------------------------------------

def ingest_setup(conn, *, setup_id, signal_id, ticker, direction, created_at,
                 target_pct=None, stop_pct=None, horizon_days=None, valid_until=None,
                 raw=None) -> bool:
    """Durably store a setup event. Idempotent on setup_id. Returns True if new."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO book_setups "
        "(setup_id, signal_id, ticker, direction, created_at, target_pct, stop_pct, "
        " horizon_days, valid_until, raw) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (setup_id, signal_id, ticker, direction, created_at, target_pct, stop_pct,
         horizon_days, valid_until, json.dumps(raw) if raw is not None else None))
    conn.commit()
    return cur.rowcount > 0


def all_setups(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM book_setups ORDER BY created_at, setup_id")]


# --- replayed positions ---------------------------------------------------------

def open_position(conn, *, setup_id, signal_id, ticker, direction, opened_at, open_price,
                  notional, target_price=None, stop_price=None, target_pct=None,
                  stop_pct=None, horizon_days=None, exit_target_date=None,
                  valid_until=None) -> bool:
    """Open a position. Idempotent on setup_id (re-run can't double-trade)."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO book_positions "
        "(setup_id, signal_id, ticker, direction, opened_at, open_price, notional, "
        " target_price, stop_price, target_pct, stop_pct, horizon_days, "
        " exit_target_date, valid_until, status) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'open')",
        (setup_id, signal_id, ticker, direction, opened_at, open_price, notional,
         target_price, stop_price, target_pct, stop_pct, horizon_days,
         exit_target_date, valid_until))
    conn.commit()
    return cur.rowcount > 0


def close_position(conn, position_id, *, closed_at, close_price, close_reason,
                   realized_pct, realized_pnl, mfe_pct=None, mae_pct=None) -> None:
    conn.execute(
        "UPDATE book_positions SET status='closed', closed_at=?, close_price=?, "
        "close_reason=?, realized_pct=?, realized_pnl=?, mfe_pct=?, mae_pct=? WHERE id=?",
        (closed_at, close_price, close_reason, realized_pct, realized_pnl,
         mfe_pct, mae_pct, position_id))
    conn.commit()


def all_positions(conn) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM book_positions ORDER BY id")]


def open_setup_ids(conn) -> set[int]:
    return {r["setup_id"] for r in conn.execute(
        "SELECT setup_id FROM book_positions WHERE setup_id IS NOT NULL")}


# --- config / cursors -----------------------------------------------------------

def config(conn, loop_name) -> dict:
    r = conn.execute("SELECT * FROM book_state WHERE loop_name=?", (loop_name,)).fetchone()
    return dict(r) if r else {}


def set_marks(conn, loop_name, *, last_open_scan_at=None, last_mark_at=None,
              setup_cursor=None) -> None:
    sets, vals = [], []
    for col, val in (("last_open_scan_at", last_open_scan_at),
                     ("last_mark_at", last_mark_at),
                     ("setup_cursor", setup_cursor)):
        if val is not None:
            sets.append(f"{col}=?")
            vals.append(val)
    if sets:
        vals.append(loop_name)
        conn.execute(f"UPDATE book_state SET {', '.join(sets)} WHERE loop_name=?", vals)
        conn.commit()
