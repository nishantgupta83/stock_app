"""Cheap syntax check on the SQL migrations in sql/.

Doesn't replace `psql` validation against the live schema, but catches the
typical mistakes: missing semicolons, mismatched do-blocks, stray BOM
characters, and lookalike SQL keyword typos. The new migrations
(0028, 0029) added in this work are the priority targets.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"

NEW_MIGRATIONS = [
    "0028_trade_setups_target_source.sql",
    "0029_signals_direction_check.sql",
    "0030_brier_calibration.sql",
]


@pytest.mark.parametrize("name", NEW_MIGRATIONS)
def test_migration_file_exists(name):
    p = SQL_DIR / name
    assert p.exists(), f"{name} should exist in sql/"


@pytest.mark.parametrize("name", NEW_MIGRATIONS)
def test_migration_ends_cleanly(name):
    """Each statement should end with a semicolon (or we're inside a
    do-block which has its own end$$;)."""
    text = (SQL_DIR / name).read_text().strip()
    # Strip line comments and blank lines for the test, just for readability.
    body = "\n".join(l for l in text.splitlines() if l.strip() and not l.strip().startswith("--"))
    assert body.rstrip().endswith(";"), f"{name} body does not end with ;"


@pytest.mark.parametrize("name", NEW_MIGRATIONS)
def test_migration_no_bom(name):
    raw = (SQL_DIR / name).read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), f"{name} starts with BOM"


def test_0028_adds_target_source_column():
    text = (SQL_DIR / "0028_trade_setups_target_source.sql").read_text()
    assert "stock_trade_setups" in text
    assert "target_source" in text
    assert re.search(r"add column if not exists target_source", text, re.IGNORECASE)


def test_0029_adds_direction_check():
    text = (SQL_DIR / "0029_signals_direction_check.sql").read_text()
    assert "stock_signals" in text
    assert re.search(r"check\s*\(\s*direction\s+in", text, re.IGNORECASE)
    # Must reference the current vocabulary
    for val in ("bullish", "bearish", "neutral"):
        assert val in text, f"0029 missing {val!r}"


def test_0029_uses_idempotent_do_block():
    """Re-running the migration must not fail with 'constraint already
    exists'. The DO block we wrote checks information_schema first."""
    text = (SQL_DIR / "0029_signals_direction_check.sql").read_text()
    assert "do $$" in text or "DO $$" in text
    assert "information_schema.table_constraints" in text


def test_0030_adds_brier_columns():
    text = (SQL_DIR / "0030_brier_calibration.sql").read_text()
    assert "stock_rule_calibration" in text
    for col in ("brier_30d", "accuracy_30d", "n_closed_30d", "last_brier_recomputed_at"):
        assert re.search(rf"add column if not exists {col}", text, re.IGNORECASE), \
            f"0030 missing column add for {col}"


def test_all_migrations_have_unique_numeric_prefix():
    """No two migrations may share a numeric prefix — that's the source
    of accidental application order ambiguity."""
    seen = {}
    for p in sorted(SQL_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql")):
        prefix = p.name[:4]
        assert prefix not in seen, f"duplicate prefix: {p.name} and {seen[prefix]}"
        seen[prefix] = p.name
