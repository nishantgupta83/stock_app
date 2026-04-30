"""
Earnings agent — keeps stock_normalized_events fresh with upcoming + recently-released
earnings dates per watchlist stock.

historical_ingest.py was a one-time 6-month backfill. This recurring agent runs
weekly (Sunday 12:00 UTC) to:
  1. Pick up newly-announced earnings dates (companies often confirm 4-8 weeks ahead)
  2. Refresh actual EPS numbers for releases that already happened
  3. Be safe to re-run — dedupe_key=earnings_{ticker}_{date} collapses on conflict

Run via .github/workflows/earnings_agent.yml (cron 0 12 * * 0).
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests
import yfinance as yf

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from filing_agent import (   # type: ignore
    job_run_start, job_run_finish, dead_letter,
    SUPABASE_URL, HEADERS_SB,
)

# Window: ~2 months back (catch revisions to recent earnings) + 14 days forward (announced upcomings)
LOOKBACK_DAYS = 60
LOOKAHEAD_DAYS = 14
YF_SLEEP = 0.20


def fetch_tradeable_tickers() -> list[str]:
    """kind='stock' tickers only — ETFs/mutual funds/indices have no earnings."""
    url = (
        f"{SUPABASE_URL}/rest/v1/stock_watchlists"
        f"?select=ticker,stock_symbols!inner(kind)"
        f"&stock_symbols.kind=eq.stock"
    )
    rows = requests.get(url, headers=HEADERS_SB, timeout=30).json()
    return sorted({r["ticker"] for r in rows if r.get("ticker")})


def emit_earnings(rows: list[dict]) -> int:
    """Bulk insert with on_conflict=dedupe_key so re-runs silently dedupe."""
    if not rows:
        return 0
    url = f"{SUPABASE_URL}/rest/v1/stock_normalized_events?on_conflict=dedupe_key"
    inserted = 0
    chunk = 500
    for i in range(0, len(rows), chunk):
        batch = rows[i:i+chunk]
        r = requests.post(url, headers=HEADERS_SB, json=batch, timeout=30)
        if r.status_code in (200, 201, 204):
            inserted += len(batch)
        else:
            print(f"  events insert chunk {i//chunk} {r.status_code}: {r.text[:300]}", file=sys.stderr)
    return inserted


def main() -> int:
    started = time.time()
    run_id = job_run_start("earnings_agent")
    rows: list[dict] = []
    n_with_data = 0
    tickers = []

    try:
        tickers = fetch_tradeable_tickers()
        cutoff_dt = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
        upper_dt  = datetime.now(timezone.utc) + timedelta(days=LOOKAHEAD_DAYS)
        print(f"Earnings window: {cutoff_dt.date()} → {upper_dt.date()} ({len(tickers)} tickers)")

        for ticker in tickers:
            try:
                ed = None
                try:
                    ed = yf.Ticker(ticker).get_earnings_dates(limit=8)
                except Exception:
                    ed = None
                if ed is None or ed.empty:
                    continue
                n_with_data += 1
                for ts, row in ed.iterrows():
                    d = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
                    if not isinstance(d, datetime):
                        continue
                    d = d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)
                    if d < cutoff_dt or d > upper_dt:
                        continue

                    actual = row.get("Reported EPS")
                    est    = row.get("EPS Estimate")
                    surp   = row.get("Surprise(%)")
                    actual_ok = actual is not None and not pd.isna(actual)
                    est_ok    = est    is not None and not pd.isna(est)
                    surp_ok   = surp   is not None and not pd.isna(surp)

                    if actual_ok and est_ok:
                        if   actual > est: subtype = "beat"
                        elif actual < est: subtype = "miss"
                        else:              subtype = "inline"
                        sev = 4 if (surp_ok and abs(surp) > 10) else 3 if (surp_ok and abs(surp) > 3) else 2
                    else:
                        subtype = "scheduled"
                        sev     = 2

                    rows.append({
                        "event_type":   "earnings_release",
                        "event_subtype": subtype,
                        "ticker":       ticker,
                        "event_at":     d.isoformat(),
                        "severity":     sev,
                        "source_table": "yfinance_earnings",
                        "parser_confidence": 1.0 if actual_ok else 0.5,
                        "dedupe_key":   f"earnings_{ticker}_{d.date().isoformat()}",
                        "payload": {
                            "actual_eps":     float(actual) if actual_ok else None,
                            "estimated_eps":  float(est)    if est_ok    else None,
                            "surprise_pct":   float(surp)   if surp_ok   else None,
                        },
                    })
            except Exception as e:  # noqa: BLE001 — never let one ticker abort the run
                print(f"  {ticker}: earnings fetch failed ({e})", file=sys.stderr)
            time.sleep(YF_SLEEP)

        inserted = emit_earnings(rows)
        elapsed = time.time() - started
        print(f"DONE in {elapsed:.1f}s — {n_with_data}/{len(tickers)} tickers had data, "
              f"{len(rows)} rows submitted, {inserted} new (dups ignored)")
        job_run_finish(run_id, "ok", len(tickers), inserted)
        return 0

    except Exception as e:  # noqa: BLE001
        import traceback
        tb = traceback.format_exc()
        dead_letter("earnings_agent", None, None, "top_level_failure", tb)
        job_run_finish(run_id, "failed", len(tickers), 0, err=str(e))
        print(f"FATAL: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
