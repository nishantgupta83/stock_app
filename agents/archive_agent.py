"""
Archive agent — tiered storage export (Phase 9).

Runs weekly. For each configured table, fetches rows older than the retention
threshold (archived_at IS NULL), serialises them to gzip-compressed JSONL,
uploads the file to Hostinger via FTPS, then (in live mode) marks rows with
archived_at and deletes them from Supabase.

DRY-RUN mode (env DRY_RUN=true): exports and uploads but skips the
archived_at stamp and the DELETE so we can verify archive integrity for
a week before enabling real deletions.

After processing all tables, updates archive/index.json on Hostinger with
cumulative rule_calibration counters (aggregated from stock_event_paper_trades)
and sends a Telegram digest.
"""
from __future__ import annotations

import gzip
import io
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from ftplib import FTP_TLS, error_perm

import requests

# ============================================================
# Environment
# ============================================================

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
BOT_TOKEN    = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID      = os.environ.get("TELEGRAM_CHAT_ID", "")

FTP_HOST = "ftp.hub4apps.com"
FTP_PORT = 21
FTP_USER = os.environ.get("HOSTINGER_FTP_USER", "")
FTP_PASS = os.environ.get("HOSTINGER_FTP_PASS", "")

DRY_RUN = os.environ.get("DRY_RUN", "false").strip().lower() == "true"

HEADERS_SB = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
    "Prefer":        "resolution=ignore-duplicates,return=minimal",
}

# ============================================================
# Retention rules
# ============================================================

# Each entry: (table, age_col, extra_filter_params, threshold_interval_sql_label)
# threshold_interval_sql_label is used only for readable output; the actual
# cutoff is computed in Python and passed as an ISO timestamp to the REST API.
#
# Rows that must NEVER be archived are excluded by extra_params passed to sb_fetch.
TABLES: list[dict] = [
    {
        "table":       "stock_normalized_events",
        "age_col":     "created_at",
        "days":        90,
        "extra_params": {},
    },
    {
        "table":       "stock_event_paper_trades",
        "age_col":     "exit_at",
        "days":        90,
        # Open trades (exit_at IS NULL) must never be archived — enforced by
        # requiring exit_at to be non-null AND older than threshold.
        "extra_params": {"exit_at": "not.is.null"},
    },
    {
        "table":       "stock_signals",
        "age_col":     "fired_at",
        "days":        90,
        # In-flight retryable signals must stay in the active tier.
        "extra_params": {"status_v2": "not.in.(candidate,sent,dispatch_failed)"},
    },
    {
        "table":       "stock_raw_prices",
        "age_col":     "ts",
        "days":        180,
        "extra_params": {},
    },
    {
        "table":       "stock_raw_filings",
        "age_col":     "filed_at",
        "days":        180,
        "extra_params": {},
    },
    {
        "table":       "stock_institutional_holdings_snapshot",
        "age_col":     "filed_at",
        "days":        90,
        "extra_params": {},
    },
]

PAGE_SIZE = 1000


# ============================================================
# Supabase helpers
# ============================================================

def sb_fetch_page(table: str, params: dict, offset: int) -> list[dict]:
    p = {**params, "offset": str(offset), "limit": str(PAGE_SIZE)}
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers=HEADERS_SB, params=p, timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"SB GET {table} offset={offset}: {r.status_code} {r.text[:200]}")
    return r.json()


def sb_fetch_all(table: str, age_col: str, threshold_iso: str,
                 extra_params: dict) -> list[dict]:
    """Paginate through all eligible rows for a table."""
    base_params: dict = {
        "archived_at": "is.null",
        age_col:       f"lt.{threshold_iso}",
        "select":      "*",
        **extra_params,
    }
    rows: list[dict] = []
    offset = 0
    while True:
        page = sb_fetch_page(table, base_params, offset)
        rows.extend(page)
        if len(page) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
    return rows


def sb_set_archived_at(table: str, ids: list, now_iso: str) -> bool:
    """Stamp archived_at on a batch of rows identified by their id column."""
    # Supabase URL length limits: chunk large id lists to avoid 414 errors.
    chunk = 200
    for i in range(0, len(ids), chunk):
        batch = ids[i:i + chunk]
        id_list = ",".join(str(x) for x in batch)
        r = requests.patch(
            f"{SUPABASE_URL}/rest/v1/{table}?id=in.({id_list})",
            headers={**HEADERS_SB, "Prefer": "return=minimal"},
            json={"archived_at": now_iso},
            timeout=30,
        )
        if r.status_code not in (200, 201, 204):
            print(f"  SB PATCH {table} ids chunk {i//chunk}: {r.status_code} {r.text[:200]}",
                  file=sys.stderr)
            return False
    return True


def sb_delete_archived(table: str, age_col: str, threshold_iso: str) -> bool:
    """Delete rows that have been stamped with archived_at for this table."""
    r = requests.delete(
        f"{SUPABASE_URL}/rest/v1/{table}",
        headers={**HEADERS_SB, "Prefer": "return=minimal"},
        params={
            "archived_at": "not.is.null",
            age_col:       f"lt.{threshold_iso}",
        },
        timeout=60,
    )
    if r.status_code not in (200, 201, 204):
        print(f"  SB DELETE {table}: {r.status_code} {r.text[:200]}", file=sys.stderr)
        return False
    return True


# ============================================================
# JSONL.gz serialisation
# ============================================================

def rows_to_gzip(rows: list[dict]) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        for row in rows:
            gz.write((json.dumps(row, default=str) + "\n").encode())
    return buf.getvalue()


# ============================================================
# FTPS helpers
# ============================================================

def _ftp_connect() -> FTP_TLS:
    ftp = FTP_TLS()
    ftp.connect(FTP_HOST, FTP_PORT, timeout=30)
    ftp.auth()          # upgrade to explicit TLS
    ftp.login(FTP_USER, FTP_PASS)
    ftp.prot_p()        # protect data channel
    return ftp


def _ftp_makedirs(ftp: FTP_TLS, path: str) -> None:
    """Recursively create remote directories, ignoring already-exists errors."""
    parts = path.strip("/").split("/")
    cur = ""
    for part in parts:
        cur = f"{cur}/{part}" if cur else part
        try:
            ftp.mkd(cur)
        except error_perm:
            # Directory already exists — safe to continue.
            pass


def ftp_upload_bytes(remote_path: str, data: bytes) -> None:
    """Upload raw bytes to remote_path. Creates parent directories as needed."""
    ftp = _ftp_connect()
    try:
        parent = "/".join(remote_path.split("/")[:-1])
        if parent:
            _ftp_makedirs(ftp, parent)
        ftp.storbinary(f"STOR {remote_path}", io.BytesIO(data))
    finally:
        try:
            ftp.quit()
        except Exception:
            ftp.close()


def ftp_download_bytes(remote_path: str) -> bytes | None:
    """Download a remote file; returns None if it does not exist."""
    ftp = _ftp_connect()
    try:
        buf = io.BytesIO()
        try:
            ftp.retrbinary(f"RETR {remote_path}", buf.write)
        except error_perm:
            return None
        return buf.getvalue()
    finally:
        try:
            ftp.quit()
        except Exception:
            ftp.close()


# ============================================================
# archive/index.json management
# ============================================================

def load_index() -> dict:
    raw = ftp_download_bytes("archive/index.json")
    if raw:
        try:
            return json.loads(raw.decode())
        except json.JSONDecodeError:
            pass
    return {"last_updated": "", "weeks": [], "rule_calibration": {}}


def merge_calibration(index: dict, paper_trade_rows: list[dict]) -> None:
    """Accumulate rule_key counters from newly archived paper trade rows."""
    cal: dict = index.setdefault("rule_calibration", {})
    for row in paper_trade_rows:
        rk = row.get("rule_key")
        if not rk:
            continue
        correct = row.get("correct")
        ret = row.get("realized_return")
        if correct is None or ret is None:
            continue
        entry = cal.setdefault(rk, {
            "n_observations": 0,
            "n_correct": 0,
            "sum_of_returns": 0.0,
        })
        entry["n_observations"] += 1
        entry["n_correct"] += 1 if correct else 0
        entry["sum_of_returns"] = round(
            float(entry["sum_of_returns"]) + float(ret), 6
        )


def save_index(index: dict, week_label: str, now_iso: str) -> None:
    index["last_updated"] = now_iso
    if week_label not in index.get("weeks", []):
        index.setdefault("weeks", []).insert(0, week_label)
    ftp_upload_bytes("archive/index.json",
                     json.dumps(index, indent=2).encode())


# ============================================================
# Telegram digest
# ============================================================

def send_telegram(table_results: list[dict], total_bytes: int, now_iso: str) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        return

    mode_label = "DRY RUN" if DRY_RUN else "archived"
    lines = [f"<b>📦 archive_agent — {mode_label}</b>",
             f"<i>{now_iso[:10]}</i>\n"]

    for tr in table_results:
        kb = tr["bytes"] / 1024
        unit = f"{kb:.1f} KB" if kb < 1024 else f"{kb/1024:.2f} MB"
        lines.append(f"• <b>{tr['table']}</b>: {tr['rows']:,} rows · {unit}")

    total_kb = total_bytes / 1024
    total_str = f"{total_kb:.1f} KB" if total_kb < 1024 else f"{total_kb/1024:.2f} MB"
    lines.append(f"\nTotal uploaded: <b>{total_str}</b>")
    lines.append("Archive: hub4apps.com/stock_app/archive/")

    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": "\n".join(lines), "parse_mode": "HTML"},
            timeout=15,
        )
    except Exception as e:
        print(f"  Telegram digest failed: {e}", file=sys.stderr)


# ============================================================
# Per-table archive routine
# ============================================================

def archive_table(cfg: dict, week_path: str, now_iso: str) -> dict | None:
    """
    Returns {"table": str, "rows": int, "bytes": int} on success.
    Returns None if the table had a fatal error (upload failed).
    Returns {"table": str, "rows": 0, "bytes": 0} if no eligible rows (skipped).
    """
    table    = cfg["table"]
    age_col  = cfg["age_col"]
    days     = cfg["days"]
    extra    = cfg["extra_params"]

    cutoff = datetime.now(timezone.utc)
    # Compute ISO threshold by subtracting days — avoids importing dateutil.
    threshold_iso = (cutoff - timedelta(days=days)).isoformat()

    print(f"[{table}] fetching rows with {age_col} < {threshold_iso[:10]} ...")
    try:
        rows = sb_fetch_all(table, age_col, threshold_iso, extra)
    except Exception as e:
        print(f"  [{table}] fetch failed: {e}", file=sys.stderr)
        return None

    if not rows:
        print(f"  [{table}] 0 eligible rows — skipping")
        return {"table": table, "rows": 0, "bytes": 0, "_rows": None}

    print(f"  [{table}] {len(rows)} rows to archive")

    gz_data = rows_to_gzip(rows)
    remote_path = f"{week_path}/{table}.jsonl.gz"

    try:
        ftp_upload_bytes(remote_path, gz_data)
    except Exception as e:
        print(f"  [{table}] FTPS upload failed: {e}", file=sys.stderr)
        # Data safety: do not stamp or delete if upload failed.
        return None

    file_size = len(gz_data)
    print(f"  [{table}] uploaded {file_size:,} bytes → {remote_path}")

    if DRY_RUN:
        print(f"  DRY_RUN: skipping delete for {table}")
        return {"table": table, "rows": len(rows), "bytes": file_size,
                "_rows": rows if table == "stock_event_paper_trades" else None}

    # Live mode: stamp archived_at then delete.
    ids = [r["id"] for r in rows if r.get("id") is not None]
    if ids:
        ok = sb_set_archived_at(table, ids, now_iso)
        if not ok:
            print(f"  [{table}] archived_at stamp failed — skipping delete for safety",
                  file=sys.stderr)
            return None

    ok = sb_delete_archived(table, age_col, threshold_iso)
    if not ok:
        print(f"  [{table}] DELETE failed — rows stamped but not removed", file=sys.stderr)
        # Don't return None here: upload succeeded and rows are stamped, so
        # report success to avoid masking the Telegram digest. The next run
        # will skip stamped rows because they already have archived_at set,
        # but operator should investigate the delete failure.

    return {"table": table, "rows": len(rows), "bytes": file_size,
            "_rows": rows if table == "stock_event_paper_trades" else None}


# ============================================================
# Main
# ============================================================

def main() -> int:
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()
    iso_year, iso_week, _ = now.isocalendar()
    week_label = f"{iso_year}/W{iso_week:02d}"
    week_path  = f"archive/{iso_year}/W{iso_week:02d}"

    mode = "DRY RUN" if DRY_RUN else "LIVE"
    print(f"archive_agent starting — {mode} — week {week_label}")

    table_results: list[dict] = []
    paper_trade_rows: list[dict] = []
    had_error = False

    for cfg in TABLES:
        result = archive_table(cfg, week_path, now_iso)
        if result is None:
            had_error = True
            continue
        if result["rows"] > 0:
            table_results.append(result)
        # Collect paper trade rows returned by archive_table (before they were deleted).
        if result.get("_rows"):
            paper_trade_rows.extend(result["_rows"])

    # Update the Hostinger index JSON.
    try:
        index = load_index()
        merge_calibration(index, paper_trade_rows)
        save_index(index, week_label, now_iso)
        print(f"archive/index.json updated — {len(index.get('rule_calibration', {}))} rules")
    except Exception as e:
        print(f"  index update failed (non-fatal): {e}", file=sys.stderr)

    total_bytes = sum(tr["bytes"] for tr in table_results)
    if table_results:
        send_telegram(table_results, total_bytes, now_iso)

    nonempty = [tr for tr in table_results if tr["rows"] > 0]
    print(f"archive_agent done — {len(nonempty)} tables exported, "
          f"{sum(tr['rows'] for tr in nonempty):,} rows total, "
          f"{total_bytes:,} bytes")

    return 1 if had_error else 0


if __name__ == "__main__":
    sys.exit(main())
