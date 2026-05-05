"""
Market scanner agent — S&P 500 jump verifier + event-outcome observer.

Two modes:
  default          One-day pass for the latest closing date (cron 30 21 * * 1-5).
  --backfill-days N  Replays the last N calendar days using stock_raw_prices +
                     stock_normalized_events (both already backfilled to 6mo by
                     historical_ingest). Idempotent — the unique index on
                     (ticker, observed_at, prior_event_id) collapses re-inserts.

Output: stock_event_outcome_observations rows tying every >=3% daily move to
prior 2-day events as candidate causes, OR a NULL-prior_event row when nothing
was tracked nearby (= a coverage gap to investigate later).

Observation-only: writing here never changes scoring weights. Aggregating these
observations over time tells us which event types most reliably precede a
meaningful move; that data can later feed thesis_agent's per-rule calibration.

Run via .github/workflows/market_scanner_agent.yml (cron + workflow_dispatch
input for backfill_days).
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

# Reuse filing_agent helpers — single source of truth for Supabase wiring + ops logging.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from filing_agent import (   # type: ignore
    job_run_start, job_run_finish, dead_letter,
    SUPABASE_URL, HEADERS_SB,
)

# Threshold for "significant" move; observation-only, conservative.
# 3% is well-established in academic literature (Fama on event-day returns)
# as outside the daily noise floor for large-cap names.
JUMP_PCT = 0.03

# How far back to scan for prior events that may have caused the move.
# 2 trading days = 1 calendar day's overnight + intraday + previous session.
LOOKBACK_DAYS = 2

# Bulk insert chunk size — keeps each POST under PostgREST's payload cap and
# lets us see incremental progress when backfilling.
INSERT_CHUNK = 500


# ============================================================
# Data loaders
# ============================================================

def fetch_tradeable_tickers() -> list[str]:
    """kind='stock' tickers from any watchlist (mirrors earnings_agent helper)."""
    url = (
        f"{SUPABASE_URL}/rest/v1/stock_watchlists"
        f"?select=ticker,stock_symbols!inner(kind)"
        f"&stock_symbols.kind=eq.stock"
    )
    r = requests.get(url, headers=HEADERS_SB, timeout=30)
    if r.status_code != 200:
        return []
    return sorted({row["ticker"] for row in r.json() if row.get("ticker")})


def fetch_recent_closes(tickers: list[str], days_back: int) -> dict[str, list[dict]]:
    """{ticker: [{ts, close}]} ordered ascending. Pages through Supabase if the
    response would exceed the per-request cap (e.g. 90 days × 25 tickers ≈
    2.2k rows fits, 180 × 50 ≈ 4.5k still fits, but 365+ might not)."""
    if not tickers:
        return {}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).date().isoformat()
    in_list = ",".join(f'"{t}"' for t in tickers)
    by_t: dict[str, list[dict]] = {}
    offset = 0
    page_size = 5000
    while True:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/stock_raw_prices",
            headers=HEADERS_SB,
            params={
                "ticker": f"in.({in_list})",
                "ts":     f"gte.{cutoff}",
                "select": "ticker,ts,close",
                "order":  "ts.asc",
                "limit":  str(page_size),
                "offset": str(offset),
            },
            timeout=30,
        )
        if r.status_code != 200:
            print(f"  fetch_recent_closes: {r.status_code} {r.text[:200]}", file=sys.stderr)
            break
        page = r.json()
        if not page:
            break
        for row in page:
            if row.get("close") is None:
                continue
            by_t.setdefault(row["ticker"], []).append(row)
        if len(page) < page_size:
            break
        offset += page_size
    return by_t


def fetch_events_window(tickers: list[str], days_back: int) -> dict[str, list[dict]]:
    """All normalized events for `tickers` in the last (days_back + LOOKBACK_DAYS)
    days, indexed by ticker. Used by the backfill loop instead of one-call-per-day
    to avoid 100s of round-trips."""
    if not tickers:
        return {}
    cutoff = (datetime.now(timezone.utc)
              - timedelta(days=days_back + LOOKBACK_DAYS + 1)).isoformat()
    in_list = ",".join(f'"{t}"' for t in tickers)
    by_t: dict[str, list[dict]] = defaultdict(list)
    offset = 0
    page_size = 5000
    while True:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/stock_normalized_events",
            headers=HEADERS_SB,
            params={
                "ticker":   f"in.({in_list})",
                "event_at": f"gte.{cutoff}",
                "select":   "id,event_type,event_subtype,event_at,severity,ticker",
                "order":    "event_at.asc",
                "limit":    str(page_size),
                "offset":   str(offset),
            },
            timeout=30,
        )
        if r.status_code != 200:
            print(f"  fetch_events_window: {r.status_code} {r.text[:200]}", file=sys.stderr)
            break
        page = r.json()
        if not page:
            break
        for row in page:
            t = row.get("ticker")
            if t:
                by_t[t].append(row)
        if len(page) < page_size:
            break
        offset += page_size
    return dict(by_t)


# ============================================================
# Core observation builder — used by both today-only and backfill paths
# ============================================================

def build_observations_for_jump(
    ticker: str,
    today_ts: str,
    daily_return: float,
    events_for_ticker: list[dict],
) -> list[dict]:
    """Given a single (ticker, day) jump and the candidate events, build the
    observation rows. `events_for_ticker` should be the ticker's events sorted
    asc by event_at; this function filters to the LOOKBACK_DAYS window."""
    today_dt = datetime.fromisoformat(today_ts.replace("Z", "+00:00"))
    window_start = today_dt - timedelta(days=LOOKBACK_DAYS)
    candidates = [
        e for e in events_for_ticker
        if window_start <= datetime.fromisoformat(e["event_at"].replace("Z", "+00:00")) <= today_dt
    ]
    if not candidates:
        return [{
            "observed_at":           today_ts,
            "ticker":                ticker,
            "daily_return_pct":      round(daily_return, 6),
            "prior_event_id":        None,
            "prior_event_type":      "no_tracked_event",
            "prior_event_subtype":   None,
            "prior_event_severity":  None,
            "prior_event_age_hours": 0,
            "source":                "market_scanner",
            "notes":                 f"|move|={abs(daily_return)*100:.2f}% with no event in last {LOOKBACK_DAYS} days",
        }]
    rows = []
    for e in candidates:
        e_dt = datetime.fromisoformat(e["event_at"].replace("Z", "+00:00"))
        age_hours = max(0, (today_dt - e_dt).total_seconds() / 3600.0)
        rows.append({
            "observed_at":           today_ts,
            "ticker":                ticker,
            "daily_return_pct":      round(daily_return, 6),
            "prior_event_id":        e["id"],
            "prior_event_type":      e["event_type"],
            "prior_event_subtype":   e.get("event_subtype"),
            "prior_event_severity":  e.get("severity"),
            "prior_event_age_hours": round(age_hours, 2),
            "source":                "market_scanner",
            # Must match key set of the no_tracked_event branch — PostgREST
            # rejects mixed key sets on bulk insert (PGRST102).
            "notes":                 None,
        })
    return rows


def write_observations(rows: list[dict]) -> int:
    """Bulk insert into stock_event_outcome_observations with on_conflict so
    re-runs are idempotent. Chunks to keep payload bounded."""
    if not rows:
        return 0
    url = (
        f"{SUPABASE_URL}/rest/v1/stock_event_outcome_observations"
        f"?on_conflict=ticker,observed_at,prior_event_id"
    )
    headers = {**HEADERS_SB, "Prefer": "resolution=ignore-duplicates,return=minimal"}
    total = 0
    for i in range(0, len(rows), INSERT_CHUNK):
        batch = rows[i:i+INSERT_CHUNK]
        r = requests.post(url, headers=headers, json=batch, timeout=30)
        if r.status_code in (200, 201, 204):
            total += len(batch)
        else:
            print(f"  observation insert chunk {i//INSERT_CHUNK} {r.status_code}: {r.text[:300]}",
                  file=sys.stderr)
    return total


# ============================================================
# Mode: today-only (cron path)
# ============================================================

def run_today() -> tuple[int, int, int]:
    tickers = fetch_tradeable_tickers()
    n_tickers = len(tickers)
    print(f"Scanning {n_tickers} tickers (today only), jump threshold {JUMP_PCT*100:.1f}%")
    closes_map = fetch_recent_closes(tickers, days_back=5)
    events_map = fetch_events_window(tickers, days_back=2)

    rows: list[dict] = []
    n_jumps = 0
    for ticker in tickers:
        bars = closes_map.get(ticker) or []
        if len(bars) < 2:
            continue
        today, prior = bars[-1], bars[-2]
        try:
            t_close, p_close = float(today["close"]), float(prior["close"])
        except (TypeError, ValueError):
            continue
        if p_close <= 0:
            continue
        ret = (t_close - p_close) / p_close
        if abs(ret) < JUMP_PCT:
            continue
        n_jumps += 1
        rows.extend(build_observations_for_jump(
            ticker, today["ts"], ret, events_map.get(ticker, [])
        ))

    n_written = write_observations(rows)
    return n_tickers, n_jumps, n_written


# ============================================================
# Mode: backfill (workflow_dispatch path)
# ============================================================

def run_backfill(days: int) -> tuple[int, int, int]:
    """Replay every consecutive-trading-day pair in the last `days` calendar
    days. Reads everything in two bulk pulls; no per-day round-trips."""
    tickers = fetch_tradeable_tickers()
    n_tickers = len(tickers)
    print(f"Backfilling {days} days for {n_tickers} tickers, jump threshold {JUMP_PCT*100:.1f}%")
    # Need a couple extra days of price history so the FIRST day in the window
    # has a prior bar to diff against.
    closes_map = fetch_recent_closes(tickers, days_back=days + 5)
    events_map = fetch_events_window(tickers, days_back=days)

    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=days)
    rows: list[dict] = []
    n_jumps = 0
    n_pairs = 0
    for ticker in tickers:
        bars = closes_map.get(ticker) or []
        if len(bars) < 2:
            continue
        # Walk every adjacent pair (bar[i-1], bar[i]); only count those whose
        # observation date falls inside the requested backfill window.
        for i in range(1, len(bars)):
            today, prior = bars[i], bars[i-1]
            try:
                today_dt = datetime.fromisoformat(today["ts"].replace("Z", "+00:00"))
            except Exception:
                continue
            if today_dt < cutoff_dt:
                continue
            n_pairs += 1
            try:
                t_close, p_close = float(today["close"]), float(prior["close"])
            except (TypeError, ValueError):
                continue
            if p_close <= 0:
                continue
            ret = (t_close - p_close) / p_close
            if abs(ret) < JUMP_PCT:
                continue
            n_jumps += 1
            rows.extend(build_observations_for_jump(
                ticker, today["ts"], ret, events_map.get(ticker, [])
            ))

    print(f"  evaluated {n_pairs} (ticker, day) pairs, {n_jumps} qualified as jumps")
    n_written = write_observations(rows)
    return n_tickers, n_jumps, n_written


# ============================================================
# Entry
# ============================================================

def main() -> int:
    ap = argparse.ArgumentParser(description="Market scanner — daily jump observer")
    ap.add_argument(
        "--backfill-days", type=int, default=0,
        help="If >0: replay this many days of historical bars instead of just today. "
             "Idempotent — safe to re-run.",
    )
    args = ap.parse_args()

    started = time.time()
    run_id = job_run_start("market_scanner_agent")
    n_tickers = n_jumps = n_observations = 0
    try:
        if args.backfill_days > 0:
            n_tickers, n_jumps, n_observations = run_backfill(args.backfill_days)
        else:
            n_tickers, n_jumps, n_observations = run_today()
        elapsed = time.time() - started
        mode = f"backfill-{args.backfill_days}d" if args.backfill_days > 0 else "today"
        print(f"DONE in {elapsed:.1f}s [{mode}] — {n_jumps} jumps from {n_tickers} tickers, "
              f"{n_observations} observations written")
        job_run_finish(run_id, "ok", n_tickers, n_observations)
        return 0
    except Exception as e:  # noqa: BLE001
        import traceback
        tb = traceback.format_exc()
        dead_letter("market_scanner_agent", None, None, "top_level_failure", tb)
        job_run_finish(run_id, "failed", n_tickers, n_observations, err=str(e))
        print(f"FATAL: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
