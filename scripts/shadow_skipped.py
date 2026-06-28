#!/usr/bin/env python3
"""Shadow-Skipped Forward-Return Audit

Syncs all SKIPPED trade setups (reason_to_skip set), grades each one with a
capacity-free forward stop_only return vs a matched QQQ window, and reports
per-category performance — answering: which gate (if any) over-filters real edge.

Consumes (read-only):
  agents/_shadow_skipped.py — pure categorise/aggregate/anomaly functions
  agents/_shadow_store.py   — isolated SQLite store (shadow_setups + shadow_outcomes)
  price_agent.compute_paper_outcome — the pipeline's stop_only grader

Writes:
  paper_book/shadow/shadow.db   (gitignored) — durable per-setup ledger
  paper_book/shadow/report.json (committed)  — per-category forward-return audit
  paper_book/shadow/state.json  (committed, CI only via SHADOW_STATE_JSON) — cursor snapshot

Does NOT import or modify paper_book.py or any _paper_book*.py module.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agents"))

# Keep import-safe when Supabase creds are absent (matches paper_book.py pattern).
os.environ.setdefault("SUPABASE_URL", "http://offline.local")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "offline")

import yfinance as yf                           # noqa: E402
import _shadow_store as store                   # noqa: E402
from _shadow_skipped import (                   # noqa: E402
    categorize_skip,
    by_category,
    anomaly_audit,
    reason_distribution,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DB = Path(
    os.environ.get("SHADOW_DB")
    or str(ROOT / "paper_book" / "shadow" / "shadow.db"))
STATE_JSON = os.environ.get("SHADOW_STATE_JSON")   # unset locally; set in CI
REPORT = ROOT / "paper_book" / "shadow" / "report.json"
BENCH = "QQQ"
COLD_START_HOURS = 720                             # 30d cold-start on first run


# ---------------------------------------------------------------------------
# Paginated Supabase GET (replicates paper_book._sb — do NOT import it)
# ---------------------------------------------------------------------------

def _sb(path: str) -> list[dict]:
    url = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ["SUPABASE_SERVICE_KEY"]
    rows, off = [], 0
    while True:
        req = urllib.request.Request(
            f"{url}/rest/v1/{path}",
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Range": f"{off}-{off+999}",
            })
        page = json.load(urllib.request.urlopen(req))
        rows += page
        if len(page) < 1000:
            return rows
        off += 1000


# ---------------------------------------------------------------------------
# Price bars — yfinance, per-ticker cache, returns {} on failure
# (replicates paper_book.bars_for — do NOT import it)
# ---------------------------------------------------------------------------

_BARS: dict[str, dict] = {}


def bars_for(ticker: str, start: dt.date, end: dt.date) -> dict:
    if ticker in _BARS:
        return _BARS[ticker]
    out: dict[dt.date, dict] = {}
    try:
        df = yf.Ticker(ticker).history(
            start=start.isoformat(),
            end=(end + dt.timedelta(days=4)).isoformat(),
            auto_adjust=True)
        for ts, row in df.iterrows():
            d = ts.date() if hasattr(ts, "date") else ts.to_pydatetime().date()
            out[d] = {
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
            }
    except Exception as e:  # noqa: BLE001
        print(f"  {ticker}: bar fetch failed — {e}", file=sys.stderr)
    _BARS[ticker] = out
    return out


def _next_session(bars: dict, on_or_after: dt.date):
    for d in sorted(bars):
        if d >= on_or_after:
            return d, bars[d]
    return None


# ---------------------------------------------------------------------------
# Sync — pull skipped setups incrementally from stock_trade_setups
# ---------------------------------------------------------------------------

def sync(conn) -> int:
    cur = store.get_cursor(conn)
    if not cur:
        cur = (dt.datetime.now(dt.timezone.utc)
               - dt.timedelta(hours=COLD_START_HOURS)).isoformat()
    cur_q = urllib.parse.quote(cur, safe="")
    rows = _sb(
        "stock_trade_setups?reason_to_skip=not.is.null"
        f"&created_at=gt.{cur_q}&order=created_at.asc"
        "&select=id,signal_id,ticker,direction,created_at,"
        "target_pct,stop_pct,horizon_days,valid_until,reason_to_skip")
    n_new = 0
    newest = cur
    for r in rows:
        if not r.get("ticker") or not r.get("direction"):
            continue
        if store.ingest_setup(
                conn,
                setup_id=r["id"],
                signal_id=r.get("signal_id"),
                ticker=r["ticker"],
                direction=r["direction"],
                created_at=r["created_at"],
                target_pct=r.get("target_pct"),
                stop_pct=r.get("stop_pct"),
                horizon_days=r.get("horizon_days"),
                valid_until=r.get("valid_until"),
                reason_to_skip=r["reason_to_skip"],
                skip_category=categorize_skip(r["reason_to_skip"]),
                raw=json.dumps(r)):
            n_new += 1
        newest = max(newest, r["created_at"])
    store.set_cursor(conn, newest)
    print(f"[sync] {len(rows)} skipped setups since cursor, {n_new} new"
          f" (cursor → {newest[:19]})")
    return n_new


# ---------------------------------------------------------------------------
# Grade — per-setup forward stop_only return vs matched QQQ window
# ---------------------------------------------------------------------------

def grade(conn) -> None:
    from price_agent import compute_paper_outcome  # deferred; avoids heavy import at test time

    today = dt.datetime.now(dt.timezone.utc).date()
    setups = store.all_setups(conn)
    resolved = store.resolved_setup_ids(conn)
    unresolved = [s for s in setups if s["setup_id"] not in resolved]

    # Pre-warm the QQQ cache over the FULL window so per-setup matched-QQQ lookups
    # (bars_for caches by ticker) don't lock to the first narrow range fetched.
    if unresolved:
        earliest = min(dt.date.fromisoformat(s["created_at"][:10]) for s in unresolved)
        bars_for(BENCH, earliest, today)   # one wide fetch populates _BARS[BENCH]

    for s in unresolved:
        sid = s["setup_id"]
        ticker = s["ticker"]
        created_date = dt.date.fromisoformat(s["created_at"][:10])
        direction = s.get("direction") or "long"

        bars = bars_for(ticker, created_date, today)
        if not bars:
            # Unpriceable — quarantine but do not crash.
            store.freeze_outcome(
                conn, setup_id=sid, ticker=ticker,
                skip_category=s.get("skip_category"),
                reason_to_skip=s.get("reason_to_skip"),
                priceable=False, status="unpriceable",
                entry_date=None, entry_px=None,
                exit_date=None, exit_px=None,
                return_pct=None, qqq_return_pct=None, excess_pct=None)
            continue

        # Entry: first session on/after created_at + 1 day (act the next morning).
        entry = _next_session(bars, created_date + dt.timedelta(days=1))
        if not entry:
            continue  # too fresh; no session yet — revisit next run

        entry_date, entry_bar = entry
        entry_open = entry_bar["open"]
        trade = {
            "entry_at": entry_date.isoformat() + "T00:00:00+00:00",
            "entry_price": entry_open,
            "direction": direction,
            "horizon_days": int(s.get("horizon_days") or 30),
            "target_pct": s.get("target_pct"),
            "stop_pct": s.get("stop_pct"),
        }
        o = compute_paper_outcome(trade, bars, exit_policy="stop_only")
        if o is None:
            continue  # too fresh; bars don't cover the horizon yet — revisit next run

        exit_date = dt.date.fromisoformat(o["exit_at"][:10])
        exit_px = o["exit_price"]
        dir_mult = 1 if direction == "long" else -1
        # gross return (slippage excluded) — the EXCESS vs gross QQQ is the diagnostic signal
        setup_ret = (exit_px - entry_open) / entry_open * dir_mult

        # Matched QQQ window return (gross, always long on QQQ).
        qbars = bars_for(BENCH, entry_date, exit_date)
        q_entry_px = q_exit_px = None
        if qbars:
            # Exact date preferred; fall back to nearest on/after.
            if entry_date in qbars:
                q_entry_px = qbars[entry_date]["close"]
            else:
                for d in sorted(qbars):
                    if d >= entry_date:
                        q_entry_px = qbars[d]["close"]
                        break
            if exit_date in qbars:
                q_exit_px = qbars[exit_date]["close"]
            else:
                for d in sorted(qbars):
                    if d >= exit_date:
                        q_exit_px = qbars[d]["close"]
                        break
        qqq_ret = (
            (q_exit_px - q_entry_px) / q_entry_px
            if q_entry_px and q_exit_px else 0.0)

        store.freeze_outcome(
            conn, setup_id=sid, ticker=ticker,
            skip_category=s.get("skip_category"),
            reason_to_skip=s.get("reason_to_skip"),
            priceable=True, status="resolved",
            entry_date=entry_date.isoformat(),
            entry_px=round(entry_open, 4),
            exit_date=exit_date.isoformat(),
            exit_px=round(exit_px, 4),
            return_pct=round(setup_ret, 6),
            qqq_return_pct=round(qqq_ret, 6),
            excess_pct=round(setup_ret - qqq_ret, 6))


# ---------------------------------------------------------------------------
# Report builder — pure: reads outcomes, writes report.json, returns dict
# ---------------------------------------------------------------------------

def build_report(conn, sync_ok: bool) -> dict:
    # n_setups counts OUTCOME rows (resolved+unpriceable); setups not yet graded are excluded until resolved
    rows = store.all_outcomes(conn)
    report = {
        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "sync_ok": sync_ok,
        "by_category": by_category(rows),
        "anomalies": anomaly_audit(rows),
        "reason_distribution": reason_distribution(rows),
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, indent=2, default=str))
    return report


# ---------------------------------------------------------------------------
# State round-trip — CI durability via committed state.json
# ---------------------------------------------------------------------------

def load_state(conn) -> None:
    if not STATE_JSON:
        return
    p = Path(STATE_JSON)
    if p.exists():
        store.import_state(conn, json.loads(p.read_text()))


def dump_state(conn) -> None:
    if not STATE_JSON:
        return
    p = Path(STATE_JSON)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(store.export_state(conn), indent=0, default=str))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    conn = store.connect(DB)
    store.init(conn)
    load_state(conn)

    sync_ok = True
    try:
        sync(conn)
    except Exception as e:  # noqa: BLE001
        sync_ok = False
        print(f"[sync] FAILED (non-fatal): {e}", file=sys.stderr)

    grade(conn)
    report = build_report(conn, sync_ok)
    dump_state(conn)

    # One-line summary.
    n_all = sum(
        report["by_category"].get(cat, {}).get("n_setups", 0)
        for cat in ("payoff", "vocabulary", "instrument", "other"))
    n_resolved = sum(
        report["by_category"].get(cat, {}).get("n_resolved", 0)
        for cat in ("payoff", "vocabulary", "instrument", "other"))
    n_anomalies = len(report.get("anomalies", []))
    print(
        f"[shadow] {n_all} setups | {n_resolved} resolved | "
        f"{n_anomalies} anomalies | sync_ok={sync_ok} → {REPORT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
