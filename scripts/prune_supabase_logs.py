#!/usr/bin/env python3
"""Prune the append-only OPERATIONAL LOG tables that no archiver covers.

Why this exists: on 2026-09-23 the database hit 118% of the 0.5 GB free-tier limit with
the grace period expired, meaning requests can start returning 402. The three largest
objects were all logs:

    public.stock_job_runs          125.39 MB  (22.21%)
    public.stock_thesis_rejections 102.78 MB  (18.20%)
    public.stock_health_pulse       40.43 MB  ( 7.16%)
                                   --------
                                   268.60 MB  = 45% of the database

agents/archive_agent.py already implements archive-then-delete, but its TABLES list only
covers stock_normalized_events, stock_event_paper_trades, stock_signals, stock_raw_prices,
stock_raw_filings and stock_institutional_holdings_snapshot. These three were never in it.

WHAT THIS DELETES, AND WHAT IT REFUSES TO TOUCH
-----------------------------------------------
Only rows older than a per-table retention window, in tables that are pure diagnostics:

    stock_job_runs           90d   operational log; the learning state lives in snapshots/
    stock_thesis_rejections  60d   audit of clusters dropped before emit
    stock_health_pulse       30d   hourly pulse ledger; only "current" is ever read

It will NEVER touch stock_rule_calibration, stock_event_paper_trades, stock_signals,
stock_agent_weights, stock_normalized_events, or any table not in TABLES below — those
carry the forward record and are the archiver's business, not this script's.

SAFETY
------
- DRY RUN BY DEFAULT. --apply is required to delete anything.
- Refuses any retention window under MIN_RETENTION_DAYS.
- Counts rows before and after, and reports the delta, so the run is verifiable.
- Deletes in bounded batches with a hard cap per run.
- Never deletes a table's entire contents: if the age filter would match everything, it
  aborts, because that means the age column is wrong or the clock is off.

Usage:
    export SUPABASE_URL=... SUPABASE_SERVICE_KEY=...      # private shell only
    python scripts/prune_supabase_logs.py                  # dry run, shows what would go
    python scripts/prune_supabase_logs.py --apply          # actually delete
    python scripts/prune_supabase_logs.py --table stock_job_runs --apply
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import urllib.error
import urllib.parse
import urllib.request

MIN_RETENTION_DAYS = 14
MAX_DELETE_BATCHES = 2000         # hard cap per run, so a bad filter cannot run away
DELETE_CHUNK = 500                # ids per DELETE statement; keeps it under the timeout
HTTP_TIMEOUT = 45

# table -> (age column, retention days, what it is)
TABLES: dict[str, tuple[str, int, str]] = {
    "stock_job_runs":          ("started_at", 90, "operational log of every agent run"),
    # fired_at, NOT created_at — sql/0035_thesis_rejections.sql:27. Verified against the
    # migration, not guessed: the first version of this script guessed created_at and
    # PostgREST rejected it with 42703 while the run still exited 0.
    "stock_thesis_rejections": ("fired_at", 60, "audit of clusters dropped before emit"),
    "stock_health_pulse":      ("pulsed_at",  30, "hourly health pulse ledger"),
}

# Tables this script must never write to, even if someone adds them to TABLES by mistake.
FORBIDDEN = {
    "stock_rule_calibration", "stock_event_paper_trades", "stock_signals",
    "stock_agent_weights", "stock_normalized_events", "stock_trade_setups",
    "stock_risk_decisions", "stock_realistic_loop_positions", "stock_realistic_loop_state",
    "stock_symbols", "stock_watchlists", "stock_raw_prices", "stock_raw_filings",
}


def env() -> tuple[str, str]:
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        sys.exit("SUPABASE_URL and SUPABASE_SERVICE_KEY must be set (private shell only)")
    return url, key


def _req(method: str, url: str, key: str, extra: dict | None = None):
    req = urllib.request.Request(url, method=method)
    req.add_header("apikey", key)
    req.add_header("Authorization", f"Bearer {key}")
    for k, v in (extra or {}).items():
        req.add_header(k, v)
    return urllib.request.urlopen(req, timeout=HTTP_TIMEOUT)


def count(url: str, key: str, table: str, where: str = "") -> int | None:
    """Exact row count via PostgREST's Content-Range header."""
    q = f"{url}/rest/v1/{table}?select=id&limit=1" + (f"&{where}" if where else "")
    try:
        with _req("GET", q, key, {"Prefer": "count=exact", "Range": "0-0"}) as r:
            cr = r.headers.get("Content-Range", "")
            return int(cr.split("/")[-1]) if "/" in cr else None
    except urllib.error.HTTPError as e:
        body = e.read()[:200].decode("utf-8", "ignore")
        # 42703 = undefined_column. A wrong age column must be fatal, not a line that
        # scrolls past while the run still reports success for the other tables.
        if "42703" in body:
            sys.exit(f"\nFATAL: {table} has no column used by this script.\n  {body}\n"
                     f"  Check the CREATE TABLE in sql/ and fix TABLES[{table!r}].")
        print(f"  ! count {table}: {e.code} {body[:160]}")
        return None


def fetch_ids(url: str, key: str, table: str, where: str, limit: int) -> list:
    """The oldest `limit` ids matching the filter, ascending."""
    q = (f"{url}/rest/v1/{table}?{where}&select=id&order=id.asc&limit={limit}")
    with _req("GET", q, key) as r:
        body = r.read().decode("utf-8", "ignore")
    return [int(m) for m in re.findall(r'"id"\s*:\s*(\d+)', body)]


def delete_ids(url: str, key: str, table: str, ids: list) -> int:
    """DELETE exactly these ids, nothing else.

    A filtered PostgREST DELETE removes EVERY matching row -- the Range header bounds the
    RESPONSE, not the statement. The first version of this relied on Range to chunk, so
    each call tried to delete all 156,965 stock_job_runs rows at once AND serialize them
    back through `return=representation`; Postgres returned 500 (statement timeout) after
    stock_health_pulse had already succeeded. Deleting an explicit id list is the only
    way to make the statement genuinely bounded.
    """
    if not ids:
        return 0
    q = f"{url}/rest/v1/{table}?id=in.({','.join(str(i) for i in ids)})"
    _req("DELETE", q, key, {"Prefer": "return=minimal"}).close()
    return len(ids)


def prune(url: str, key: str, table: str, apply: bool, days_override: int | None = None) -> dict:
    age_col, days, what = TABLES[table]
    if days_override is not None:
        days = days_override
    if table in FORBIDDEN:
        sys.exit(f"refusing: {table} is on the FORBIDDEN list")
    if days < MIN_RETENTION_DAYS:
        sys.exit(f"refusing: retention {days}d for {table} is under the {MIN_RETENTION_DAYS}d floor")

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    where = f"{age_col}=lt.{urllib.parse.quote(cutoff)}"
    total = count(url, key, table)
    old = count(url, key, table, where)
    if total is None or old is None:
        return {"table": table, "error": "count failed"}

    print(f"\n{table}  ({what})")
    print(f"  retention {days}d  ·  cutoff {cutoff[:19]}Z  ·  age column {age_col}")
    print(f"  total rows {total:,}  ·  older than cutoff {old:,}  ·  would keep {total - old:,}")

    if old == 0:
        print("  nothing to prune")
        return {"table": table, "total": total, "old": 0, "deleted": 0}
    if old == total:
        print("  ABORT: the filter matches EVERY row — wrong age column or a clock problem")
        return {"table": table, "total": total, "old": old, "deleted": 0, "error": "matches all"}
    if not apply:
        print(f"  DRY RUN — would delete {old:,} rows. Re-run with --apply to do it.")
        return {"table": table, "total": total, "old": old, "deleted": 0, "dry_run": True}

    removed = 0
    for i in range(MAX_DELETE_BATCHES):
        try:
            ids = fetch_ids(url, key, table, where, DELETE_CHUNK)
        except urllib.error.HTTPError as e:
            print(f"    id fetch failed ({e.code}) — stopping with {removed:,} deleted")
            break
        if not ids:
            break
        for attempt in range(4):
            try:
                removed += delete_ids(url, key, table, ids)
                break
            except urllib.error.HTTPError as e:
                if attempt == 3:
                    print(f"    DELETE failed after 4 tries ({e.code}); "
                          f"stopping with {removed:,} deleted. Re-run to continue.")
                    ids = None
                    break
                time.sleep(2 ** attempt)
        if ids is None:
            break
        if removed % (DELETE_CHUNK * 20) == 0:
            print(f"    deleted {removed:,} ...")
    after = count(url, key, table)
    print(f"  DELETED {removed:,}  ·  rows now {after:,} (was {total:,})")
    return {"table": table, "total": total, "old": old, "deleted": removed, "after": after}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="actually delete (default is a dry run)")
    ap.add_argument("--table", choices=sorted(TABLES), help="prune only this table")
    ap.add_argument("--days", type=int, default=None,
                    help=f"override the retention window for every selected table "
                         f"(floor {MIN_RETENTION_DAYS}d). Current defaults leave ~67 MB of "
                         f"headroom, about 37 days at the observed refill rate; 45/30/14 "
                         f"roughly doubles that.")
    a = ap.parse_args()
    url, key = env()

    print("=" * 78)
    print("PRUNE SUPABASE OPERATIONAL LOGS" + ("  [APPLY]" if a.apply else "  [DRY RUN]"))
    print("=" * 78)
    print("Touches only diagnostic log tables. Calibration, paper trades, signals and the")
    print("frozen experiments are on the FORBIDDEN list and are never read or written here.")

    results = [prune(url, key, t, a.apply, a.days)
               for t in ([a.table] if a.table else sorted(TABLES))]
    failed = [r for r in results if r.get("error")]
    total_del = sum(r.get("deleted", 0) for r in results)
    would = sum(r.get("old", 0) for r in results)
    print("\n" + "=" * 78)
    if a.apply:
        print(f"deleted {total_del:,} rows across {len(results)} tables")
        print("Supabase reports Database Size on a delay of up to an hour, and Postgres does")
        print("not return the space to the OS until a VACUUM. Run this in the SQL editor:")
        for t in ([a.table] if a.table else sorted(TABLES)):
            print(f"    VACUUM (ANALYZE) public.{t};")
    else:
        print(f"dry run: {would:,} rows are eligible. Re-run with --apply.")
    if failed:
        for r in failed:
            print(f"::error::{r['table']}: {r['error']}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
