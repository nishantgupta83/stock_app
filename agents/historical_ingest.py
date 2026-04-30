"""
One-time 6-month historical bootstrap.

Backfills three data sources so the backtester + ticker pages have a real
180-day foundation to learn from and visualize:

  --filings   EDGAR submissions per CIK → stock_raw_filings + stock_normalized_events
  --earnings  yfinance earnings dates per ticker → stock_normalized_events
  --prices    yfinance daily bars (batched) → stock_raw_prices
  --all       Run all three in order (default)

All three are idempotent — safe to re-run. They use the same dedupe paths as
the live agents (accession_number for filings, dedupe_key for earnings,
unique(ticker,ts,source) for prices), so duplicates collapse silently.

Trigger via .github/workflows/historical_ingest.yml (workflow_dispatch).

After this finishes, run:
    gh workflow run backtester.yml
to fill stock_agent_weights with 180 days of EMA evolution.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import requests
import yfinance as yf

# Reuse filing_agent helpers — single source of truth for EDGAR + Supabase logic.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from filing_agent import (   # type: ignore
    fetch_watchlist, fetch_recent_filings, upsert_filings,
    emit_normalized_events, already_seen_accessions,
    job_run_start, job_run_finish, dead_letter,
    SUPABASE_URL, HEADERS_SB,
)

LOOKBACK_DAYS = 180
EDGAR_SLEEP   = 0.12      # 10 req/sec ceiling
YF_SLEEP      = 0.20


# ============================================================
# Watchlist helpers
# ============================================================

def fetch_all_watchlist_tickers() -> list[str]:
    """Every distinct ticker on any watchlist, including ETFs/indices without a CIK.
    Used by --earnings and --prices, which don't need EDGAR access."""
    rows = requests.get(
        f"{SUPABASE_URL}/rest/v1/stock_watchlists",
        headers=HEADERS_SB,
        params={"select": "ticker"},
        timeout=30,
    ).json()
    return sorted({r["ticker"] for r in rows if r.get("ticker")})


# ============================================================
# Subcommand: filings
# ============================================================

def ingest_filings() -> tuple[int, int, int]:
    """Walk EDGAR for each CIK in watchlist, keep filings within 180-day window.
    Returns (tickers_processed, filings_inserted, events_emitted)."""
    cutoff_iso = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).isoformat()
    watchlist  = fetch_watchlist()
    print(f"[filings] {len(watchlist)} symbols with CIKs, cutoff {cutoff_iso[:10]}")

    n_processed = n_filings = n_events = 0
    for sym in watchlist:
        ticker, cik, kind = sym["ticker"], sym["cik"], sym["kind"]
        try:
            recent   = fetch_recent_filings(cik, kind)
            in_win   = [r for r in recent if r["filed_at"] >= cutoff_iso]
            if not in_win:
                print(f"  {ticker}: 0 in window")
                n_processed += 1
                time.sleep(EDGAR_SLEEP)
                continue
            seen = already_seen_accessions([r["accession_number"] for r in in_win])
            new  = [r for r in in_win if r["accession_number"] not in seen]
            if new:
                f_inserted = upsert_filings(new, ticker)
                e_emitted  = emit_normalized_events(new, ticker)
                n_filings += f_inserted
                n_events  += e_emitted
                print(f"  {ticker}: {len(in_win)} in window, +{f_inserted} filings, +{e_emitted} events")
            else:
                print(f"  {ticker}: {len(in_win)} in window, all already ingested")
            n_processed += 1
        except Exception as e:  # noqa: BLE001
            dead_letter("historical_ingest", "stock_symbols", None,
                        "filings_failure", f"{ticker}/{cik}: {e}",
                        {"ticker": ticker, "cik": cik})
            print(f"  {ticker}: FAILED ({e})", file=sys.stderr)
        time.sleep(EDGAR_SLEEP)

    print(f"[filings] DONE — {n_processed}/{len(watchlist)} tickers, "
          f"+{n_filings} filings, +{n_events} normalized events")
    return n_processed, n_filings, n_events


# ============================================================
# Subcommand: earnings
# ============================================================

def ingest_earnings() -> tuple[int, int]:
    """Fetch earnings dates per ticker via yfinance, write each as one
    earnings_release event with dedupe_key=earnings_{ticker}_{date}."""
    cutoff_dt   = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    upper_dt    = datetime.now(timezone.utc) + timedelta(days=14)   # include scheduled near-future
    tickers     = fetch_all_watchlist_tickers()
    print(f"[earnings] {len(tickers)} tickers, window {cutoff_dt.date()} → {upper_dt.date()}")

    rows: list[dict] = []
    n_with_data = 0

    for ticker in tickers:
        try:
            ed = None
            try:
                ed = yf.Ticker(ticker).get_earnings_dates(limit=8)   # ~2 yrs back
            except Exception:
                ed = None
            if ed is None or ed.empty:
                continue
            n_with_data += 1
            for ts, row in ed.iterrows():
                # Normalize timestamp to UTC
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
        except Exception as e:  # noqa: BLE001
            print(f"  {ticker}: earnings fetch failed ({e})", file=sys.stderr)
        time.sleep(YF_SLEEP)

    if not rows:
        print(f"[earnings] no rows to insert ({n_with_data}/{len(tickers)} returned data)")
        return n_with_data, 0

    inserted = _bulk_insert("stock_normalized_events", rows)
    print(f"[earnings] DONE — {n_with_data}/{len(tickers)} tickers had data, "
          f"{len(rows)} rows submitted, {inserted} new (dups ignored)")
    return n_with_data, inserted


# ============================================================
# Subcommand: prices
# ============================================================

def ingest_prices() -> tuple[int, int]:
    """Single batched yfinance.download() for all watchlist tickers, 6mo of daily bars.
    Inserts into stock_raw_prices; (ticker, ts, source) uniqueness handles dup rows."""
    tickers = fetch_all_watchlist_tickers()
    if not tickers:
        print("[prices] empty watchlist, nothing to do")
        return 0, 0

    print(f"[prices] downloading 6mo of daily bars for {len(tickers)} tickers (single yf.download call)")

    try:
        df = yf.download(
            tickers, period="6mo", interval="1d",
            auto_adjust=False, group_by="ticker", progress=False, threads=True,
        )
    except Exception as e:  # noqa: BLE001
        print(f"[prices] yf.download failed: {e}", file=sys.stderr)
        return 0, 0

    if df is None or df.empty:
        print("[prices] empty dataframe returned")
        return 0, 0

    rows: list[dict] = []
    n_with_data = 0

    for ticker in tickers:
        try:
            sub = df[ticker] if isinstance(df.columns, pd.MultiIndex) else df
            sub = sub.dropna(subset=["Close"])
            if sub.empty:
                continue
            n_with_data += 1
            for ts, r in sub.iterrows():
                # Store as midnight UTC marker for the trading day; yfinance daily bars
                # are date-keyed (US/Eastern session aggregate). 00:00 UTC keeps the date intact
                # and matches what site_generator displays.
                date_iso = ts.strftime("%Y-%m-%d")
                rows.append({
                    "ticker": ticker,
                    "ts":     f"{date_iso}T00:00:00+00:00",
                    "open":   _safe_float(r.get("Open")),
                    "high":   _safe_float(r.get("High")),
                    "low":    _safe_float(r.get("Low")),
                    "close":  _safe_float(r.get("Close")),
                    "volume": _safe_int(r.get("Volume")),
                    "source": "yfinance",
                })
        except (KeyError, AttributeError) as e:
            print(f"  {ticker}: not in batch result ({e})", file=sys.stderr)

    if not rows:
        print("[prices] no rows assembled")
        return n_with_data, 0

    # Bulk insert in chunks of 1000 — Supabase REST has a payload size cap.
    inserted = _bulk_insert("stock_raw_prices", rows, chunk=1000)
    print(f"[prices] DONE — {n_with_data}/{len(tickers)} tickers had data, "
          f"{len(rows)} rows submitted, {inserted} new (dups ignored)")
    return n_with_data, inserted


# ============================================================
# Helpers
# ============================================================

def _safe_float(v) -> float | None:
    if v is None or pd.isna(v):
        return None
    return round(float(v), 4)


def _safe_int(v) -> int | None:
    if v is None or pd.isna(v):
        return None
    return int(v)


def _bulk_insert(table: str, rows: list[dict], chunk: int = 500) -> int:
    """POST in chunks to avoid Supabase REST payload limit. Returns total submitted
    (Supabase doesn't tell us how many were skipped as duplicates with ignore-duplicates)."""
    total = 0
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    for i in range(0, len(rows), chunk):
        batch = rows[i:i+chunk]
        r = requests.post(url, headers=HEADERS_SB, json=batch, timeout=60)
        if r.status_code in (200, 201, 204):
            total += len(batch)
        else:
            print(f"  bulk insert {table} chunk {i//chunk} {r.status_code}: {r.text[:300]}", file=sys.stderr)
    return total


# ============================================================
# Main
# ============================================================

def main() -> int:
    ap = argparse.ArgumentParser(description="One-time 6-month historical backfill")
    ap.add_argument("--filings",  action="store_true", help="EDGAR filings per CIK → stock_raw_filings + events")
    ap.add_argument("--earnings", action="store_true", help="yfinance earnings → stock_normalized_events")
    ap.add_argument("--prices",   action="store_true", help="yfinance daily bars → stock_raw_prices")
    ap.add_argument("--all",      action="store_true", help="Run all three in order (default if no flags)")
    args = ap.parse_args()

    do_all = args.all or not (args.filings or args.earnings or args.prices)
    run_filings  = args.filings  or do_all
    run_earnings = args.earnings or do_all
    run_prices   = args.prices   or do_all

    started = time.time()
    run_id = job_run_start("historical_ingest")
    total_in = total_out = 0

    try:
        if run_filings:
            tp, fi, ev = ingest_filings()
            total_in  += tp
            total_out += fi + ev
        if run_earnings:
            td, ie = ingest_earnings()
            total_in  += td
            total_out += ie
        if run_prices:
            tp, ip = ingest_prices()
            total_in  += tp
            total_out += ip

        elapsed = time.time() - started
        print(f"DONE in {elapsed:.1f}s — total_in={total_in}, total_out={total_out}")
        job_run_finish(run_id, "ok", total_in, total_out)
        return 0

    except Exception as e:  # noqa: BLE001
        import traceback
        tb = traceback.format_exc()
        dead_letter("historical_ingest", None, None, "top_level_failure", tb)
        job_run_finish(run_id, "failed", total_in, total_out, err=str(e))
        print(f"FATAL: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
