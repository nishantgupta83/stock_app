"""Isolated SQLite store for the shadow-skipped forward-return audit.

Three tables — entirely separate from book_setups / book_positions:
  shadow_setups    — one row per skipped trade-setup (deduped on setup_id)
  shadow_outcomes  — one frozen outcome row per setup (deduped on setup_id)
  shadow_state     — single-row cursor (ISO timestamp watermark)

Pattern mirrors _paper_book_store.py: conn.row_factory = sqlite3.Row,
dict(r) return dicts, INSERT OR IGNORE for idempotency.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS shadow_setups (
    setup_id        INTEGER PRIMARY KEY,
    signal_id       INTEGER,
    ticker          TEXT,
    direction       TEXT,
    created_at      TEXT,
    target_pct      REAL,
    stop_pct        REAL,
    horizon_days    INTEGER,
    valid_until     TEXT,
    reason_to_skip  TEXT,
    skip_category   TEXT,
    raw             TEXT
);
CREATE TABLE IF NOT EXISTS shadow_outcomes (
    setup_id        INTEGER PRIMARY KEY,
    ticker          TEXT,
    skip_category   TEXT,
    reason_to_skip  TEXT,
    priceable       INTEGER,
    status          TEXT,
    entry_date      TEXT,
    entry_px        REAL,
    exit_date       TEXT,
    exit_px         REAL,
    return_pct      REAL,
    qqq_return_pct  REAL,
    excess_pct      REAL
);
CREATE TABLE IF NOT EXISTS shadow_state (
    id      INTEGER PRIMARY KEY CHECK(id=1),
    cursor  TEXT
);
"""

_SETUP_COLS = (
    "setup_id", "signal_id", "ticker", "direction", "created_at",
    "target_pct", "stop_pct", "horizon_days", "valid_until",
    "reason_to_skip", "skip_category", "raw",
)

_OUTCOME_COLS = (
    "setup_id", "ticker", "skip_category", "reason_to_skip",
    "priceable", "status", "entry_date", "entry_px",
    "exit_date", "exit_px", "return_pct", "qqq_return_pct", "excess_pct",
)


# ---------------------------------------------------------------------------
# Connection + schema
# ---------------------------------------------------------------------------

def connect(path) -> sqlite3.Connection:
    """Open (or create) a SQLite database at path. Does NOT create tables."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def init(conn) -> None:
    """Create all three tables if they do not already exist."""
    conn.executescript(_SCHEMA)
    conn.commit()


# ---------------------------------------------------------------------------
# Cursor (single-row watermark)
# ---------------------------------------------------------------------------

def get_cursor(conn) -> str | None:
    """Return the stored ISO cursor string, or None if never set."""
    row = conn.execute("SELECT cursor FROM shadow_state WHERE id=1").fetchone()
    return row["cursor"] if row else None


def set_cursor(conn, iso: str) -> None:
    """Upsert the single-row cursor to iso."""
    conn.execute(
        "INSERT OR REPLACE INTO shadow_state(id, cursor) VALUES(1, ?)", (iso,))
    conn.commit()


# ---------------------------------------------------------------------------
# Setups
# ---------------------------------------------------------------------------

def ingest_setup(conn, *, setup_id, ticker, direction, created_at,
                 target_pct=None, stop_pct=None, horizon_days=None,
                 valid_until=None, signal_id=None,
                 reason_to_skip=None, skip_category=None, raw=None) -> bool:
    """Durably store a skipped setup event. Idempotent on setup_id.

    Returns True if the row was newly inserted, False if it already existed.
    """
    cur = conn.execute(
        "INSERT OR IGNORE INTO shadow_setups "
        "(setup_id, signal_id, ticker, direction, created_at, target_pct, stop_pct, "
        " horizon_days, valid_until, reason_to_skip, skip_category, raw) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (setup_id, signal_id, ticker, direction, created_at,
         target_pct, stop_pct, horizon_days, valid_until,
         reason_to_skip, skip_category, raw))
    conn.commit()
    return cur.rowcount > 0


def all_setups(conn) -> list[dict]:
    """Return all setup rows ordered by created_at, setup_id."""
    return [dict(r) for r in conn.execute(
        "SELECT * FROM shadow_setups ORDER BY created_at, setup_id")]


# ---------------------------------------------------------------------------
# Outcomes (frozen forward-return records)
# ---------------------------------------------------------------------------

def freeze_outcome(conn, *, setup_id, ticker, skip_category, reason_to_skip,
                   priceable, status, entry_date, entry_px,
                   exit_date, exit_px, return_pct, qqq_return_pct, excess_pct) -> bool:
    """Freeze a forward-return outcome for a skipped setup. Idempotent on setup_id.

    priceable is stored as INTEGER (True→1, False→0).
    Returns True if newly inserted, False if already frozen.
    """
    cur = conn.execute(
        "INSERT OR IGNORE INTO shadow_outcomes "
        "(setup_id, ticker, skip_category, reason_to_skip, priceable, status, "
        " entry_date, entry_px, exit_date, exit_px, return_pct, qqq_return_pct, excess_pct) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (setup_id, ticker, skip_category, reason_to_skip,
         int(priceable) if isinstance(priceable, bool) else priceable,
         status, entry_date, entry_px, exit_date, exit_px,
         return_pct, qqq_return_pct, excess_pct))
    conn.commit()
    return cur.rowcount > 0


def all_outcomes(conn) -> list[dict]:
    """Return all outcome rows ordered by setup_id."""
    return [dict(r) for r in conn.execute(
        "SELECT * FROM shadow_outcomes ORDER BY setup_id")]


def resolved_setup_ids(conn) -> set[int]:
    """Return the set of setup_ids that have a frozen outcome row."""
    return {r["setup_id"] for r in conn.execute(
        "SELECT setup_id FROM shadow_outcomes")}


# ---------------------------------------------------------------------------
# State export / import (round-trip identity)
# ---------------------------------------------------------------------------

def export_state(conn) -> dict:
    """Snapshot all three tables into a plain dict.

    Shape: {cursor: str|None, setups: [...], outcomes: [...]}
    """
    return {
        "cursor": get_cursor(conn),
        "setups": all_setups(conn),
        "outcomes": all_outcomes(conn),
    }


def import_state(conn, state: dict) -> None:
    """Restore a snapshot produced by export_state into conn.

    Idempotent: existing rows are silently skipped (INSERT OR IGNORE).
    The cursor is only written if the snapshot contains a non-None value.
    """
    cursor = state.get("cursor")
    if cursor is not None:
        set_cursor(conn, cursor)

    for s in state.get("setups", []):
        conn.execute(
            f"INSERT OR IGNORE INTO shadow_setups ({','.join(_SETUP_COLS)}) "
            f"VALUES ({','.join('?' * len(_SETUP_COLS))})",
            tuple(s.get(c) for c in _SETUP_COLS))

    for o in state.get("outcomes", []):
        conn.execute(
            f"INSERT OR IGNORE INTO shadow_outcomes ({','.join(_OUTCOME_COLS)}) "
            f"VALUES ({','.join('?' * len(_OUTCOME_COLS))})",
            tuple(o.get(c) for c in _OUTCOME_COLS))

    conn.commit()
