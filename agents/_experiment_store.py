"""Isolated SQLite store for the forward provisional-long edge experiments.

Mirrors agents/_shadow_store.py, but every row is namespaced by `experiment_id`
so the two horizon experiments (`fwd_prov_long_h1d`, `fwd_prov_long_h7d`) can NEVER
share a cohort, position, or watermark — even if pointed at the same DB file. In
production each experiment also gets its OWN db file (EXP_DB) + committed state
JSON (EXP_STATE_JSON), so there are two isolation layers.

Three tables — entirely separate from book_setups / book_positions / shadow_*:
  exp_candidates  — one row per ADMITTED candidate (deduped on (experiment_id, candidate_id))
  exp_positions   — one frozen outcome per admitted candidate; CLOSED rows are IMMUTABLE
                    (only still-open rows re-grade), deduped on (experiment_id, candidate_id)
  exp_state       — one row per experiment: cursor watermark, forward_epoch, config_hash

Pattern mirrors _shadow_store.py: conn.row_factory = sqlite3.Row, dict(r) returns
dicts, INSERT OR IGNORE for idempotency. Does NOT import or modify any production
module, table, or the paper_book / shadow ledgers.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS exp_candidates (
    experiment_id   TEXT    NOT NULL,
    candidate_id    INTEGER NOT NULL,
    created_at      TEXT,
    ticker          TEXT,
    direction       TEXT,
    score           REAL,
    rule_keys       TEXT,          -- JSON list
    source_agents   TEXT,          -- JSON list
    raw             TEXT,          -- JSON blob
    PRIMARY KEY (experiment_id, candidate_id)
);
CREATE TABLE IF NOT EXISTS exp_positions (
    experiment_id   TEXT    NOT NULL,
    candidate_id    INTEGER NOT NULL,
    ticker          TEXT,
    direction       TEXT,          -- always 'long' here (bullish -> long)
    horizon_days    INTEGER,
    created_at      TEXT,          -- candidate created_at (drives forward/replay split)
    entry_session   TEXT,          -- ISO date of the entry bar
    entry_px        REAL,
    exit_session    TEXT,          -- ISO date of the exit bar
    exit_px         REAL,
    exit_reason     TEXT,          -- 'stop' | 'horizon'
    net_return      REAL,          -- candidate net-of-slippage return (single locus)
    qqq_return      REAL,          -- matched-window QQQ return (gross)
    excess          REAL,          -- net_return - qqq_return
    status          TEXT,          -- 'open' | 'closed'
    PRIMARY KEY (experiment_id, candidate_id)
);
CREATE TABLE IF NOT EXISTS exp_state (
    experiment_id   TEXT PRIMARY KEY,
    cursor          TEXT,
    forward_epoch   TEXT,
    config_hash     TEXT
);
"""

_CANDIDATE_COLS = (
    "experiment_id", "candidate_id", "created_at", "ticker", "direction",
    "score", "rule_keys", "source_agents", "raw",
)

_POSITION_COLS = (
    "experiment_id", "candidate_id", "ticker", "direction", "horizon_days",
    "created_at", "entry_session", "entry_px", "exit_session", "exit_px",
    "exit_reason", "net_return", "qqq_return", "excess", "status",
)

_STATE_COLS = ("experiment_id", "cursor", "forward_epoch", "config_hash")


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
# Per-experiment state (cursor, forward_epoch, config_hash)
# ---------------------------------------------------------------------------

def ensure_state(conn, experiment_id: str) -> None:
    """Ensure a state row exists for this experiment (no-op if present)."""
    conn.execute(
        "INSERT OR IGNORE INTO exp_state(experiment_id) VALUES (?)", (experiment_id,))
    conn.commit()


def get_state(conn, experiment_id: str) -> dict:
    """Return the state row as a dict, or {} if the experiment is unknown."""
    row = conn.execute(
        "SELECT * FROM exp_state WHERE experiment_id=?", (experiment_id,)).fetchone()
    return dict(row) if row else {}


def set_cursor(conn, experiment_id: str, iso: str) -> None:
    """Advance the incremental sync watermark."""
    ensure_state(conn, experiment_id)
    conn.execute(
        "UPDATE exp_state SET cursor=? WHERE experiment_id=?", (iso, experiment_id))
    conn.commit()


def set_forward_epoch(conn, experiment_id: str, epoch: str) -> None:
    """Stamp the forward_epoch (set ONCE, on the first run)."""
    ensure_state(conn, experiment_id)
    conn.execute(
        "UPDATE exp_state SET forward_epoch=? WHERE experiment_id=?",
        (epoch, experiment_id))
    conn.commit()


def check_config_hash(conn, experiment_id: str, computed: str) -> None:
    """Persist the frozen-params hash on first run; raise if it ever drifts.

    This enforces the pre-registration contract: a parameter change WITHOUT a
    new experiment version (which changes the id + hash) is a protocol violation.
    """
    ensure_state(conn, experiment_id)
    stored = get_state(conn, experiment_id).get("config_hash")
    if stored is None:
        conn.execute(
            "UPDATE exp_state SET config_hash=? WHERE experiment_id=?",
            (computed, experiment_id))
        conn.commit()
        return
    if stored != computed:
        raise ValueError(
            f"config_hash drift for {experiment_id!r}: stored {stored[:12]}… != "
            f"computed {computed[:12]}…. Frozen params changed — bump the experiment "
            f"version + forward_epoch instead of mutating params in place.")


# ---------------------------------------------------------------------------
# Admitted candidates
# ---------------------------------------------------------------------------

def ingest_candidate(conn, experiment_id: str, *, candidate_id, created_at,
                     ticker, direction, score=None, rule_keys=None,
                     source_agents=None, raw=None) -> bool:
    """Durably store an admitted candidate. Idempotent on (experiment_id, candidate_id).

    Returns True if newly inserted, False if it already existed.
    """
    cur = conn.execute(
        "INSERT OR IGNORE INTO exp_candidates "
        "(experiment_id, candidate_id, created_at, ticker, direction, score, "
        " rule_keys, source_agents, raw) VALUES (?,?,?,?,?,?,?,?,?)",
        (experiment_id, candidate_id, created_at, ticker, direction, score,
         rule_keys, source_agents, raw))
    conn.commit()
    return cur.rowcount > 0


def all_candidates(conn, experiment_id: str) -> list[dict]:
    """All admitted candidates for this experiment, ordered by created_at."""
    return [dict(r) for r in conn.execute(
        "SELECT * FROM exp_candidates WHERE experiment_id=? "
        "ORDER BY created_at, candidate_id", (experiment_id,))]


# ---------------------------------------------------------------------------
# Positions (frozen forward outcomes — CLOSED rows are immutable)
# ---------------------------------------------------------------------------

def get_position(conn, experiment_id: str, candidate_id) -> dict | None:
    row = conn.execute(
        "SELECT * FROM exp_positions WHERE experiment_id=? AND candidate_id=?",
        (experiment_id, candidate_id)).fetchone()
    return dict(row) if row else None


def save_position(conn, experiment_id: str, *, candidate_id, ticker, direction,
                  horizon_days, created_at, entry_session, entry_px,
                  exit_session, exit_px, exit_reason,
                  net_return, qqq_return, excess, status) -> bool:
    """Insert or update a position.

    IMMUTABILITY: once a position row is 'closed' it is frozen — this is a no-op
    and returns False. Still-open positions are overwritten (INSERT OR REPLACE) so
    they re-grade each run until they close. This is the ONLY writer of outcomes.
    """
    existing = get_position(conn, experiment_id, candidate_id)
    if existing and existing.get("status") == "closed":
        return False  # frozen — never rewrite a closed outcome
    conn.execute(
        "INSERT OR REPLACE INTO exp_positions "
        "(experiment_id, candidate_id, ticker, direction, horizon_days, created_at, "
        " entry_session, entry_px, exit_session, exit_px, exit_reason, "
        " net_return, qqq_return, excess, status) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (experiment_id, candidate_id, ticker, direction, horizon_days, created_at,
         entry_session, entry_px, exit_session, exit_px, exit_reason,
         net_return, qqq_return, excess, status))
    conn.commit()
    return True


def all_positions(conn, experiment_id: str) -> list[dict]:
    return [dict(r) for r in conn.execute(
        "SELECT * FROM exp_positions WHERE experiment_id=? ORDER BY candidate_id",
        (experiment_id,))]


def closed_candidate_ids(conn, experiment_id: str) -> set[int]:
    """candidate_ids whose position is already frozen closed (skip re-grade)."""
    return {r["candidate_id"] for r in conn.execute(
        "SELECT candidate_id FROM exp_positions "
        "WHERE experiment_id=? AND status='closed'", (experiment_id,))}


# ---------------------------------------------------------------------------
# State export / import (round-trip identity for CI durability)
# ---------------------------------------------------------------------------

def export_state(conn, experiment_id: str) -> dict:
    """Snapshot everything for this experiment into a plain JSON-safe dict.

    Shape: {experiment_id, cursor, forward_epoch, config_hash,
            candidates: [...], positions: [...]}
    """
    st = get_state(conn, experiment_id)
    return {
        "experiment_id": experiment_id,
        "cursor": st.get("cursor"),
        "forward_epoch": st.get("forward_epoch"),
        "config_hash": st.get("config_hash"),
        "candidates": all_candidates(conn, experiment_id),
        "positions": all_positions(conn, experiment_id),
    }


def import_state(conn, state: dict) -> None:
    """Restore a snapshot produced by export_state.

    Idempotent: existing rows are silently skipped (INSERT OR IGNORE), so a frozen
    closed position round-trips unchanged. The state row is upserted.
    """
    experiment_id = state.get("experiment_id")
    if not experiment_id:
        return
    ensure_state(conn, experiment_id)
    conn.execute(
        "INSERT OR REPLACE INTO exp_state (experiment_id, cursor, forward_epoch, config_hash) "
        "VALUES (?,?,?,?)",
        (experiment_id, state.get("cursor"), state.get("forward_epoch"),
         state.get("config_hash")))

    for c in state.get("candidates", []):
        conn.execute(
            f"INSERT OR IGNORE INTO exp_candidates ({','.join(_CANDIDATE_COLS)}) "
            f"VALUES ({','.join('?' * len(_CANDIDATE_COLS))})",
            tuple(c.get(col) for col in _CANDIDATE_COLS))

    for p in state.get("positions", []):
        conn.execute(
            f"INSERT OR IGNORE INTO exp_positions ({','.join(_POSITION_COLS)}) "
            f"VALUES ({','.join('?' * len(_POSITION_COLS))})",
            tuple(p.get(col) for col in _POSITION_COLS))

    conn.commit()
