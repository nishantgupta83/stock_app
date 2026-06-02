#!/usr/bin/env python3
"""One-shot cleanup for paper trades stuck open past their horizon.

Why this exists:
  The 513-stuck-h1d incident (2026-06-02) traced to price_agent's silent
  drop of trades whose yfinance bars came back empty. The fix (committed
  same session) adds a stock_raw_prices fallback to fetch_bars + counters
  in stock_job_runs.meta. But that fix only helps FUTURE runs — the
  existing backlog needs a one-time push through the same logic.

What this script does:
  1. Identifies stuck open trades (status='open' AND entry_at + horizon
     days < now - 1 day buffer).
  2. For each, reuses agents/price_agent.fetch_bars (which already does
     yfinance -> stock_raw_prices fallback after the 2026-06-02 fix) and
     agents/price_agent.compute_paper_outcome (the canonical close
     logic).
  3. Closes trades that can be closed.
  4. For trades that CANNOT be closed (no bars in any source), marks
     status='closed' with close_reason='no_bars_unrecoverable' so they're
     surfaced in metrics rather than hanging open forever.

Modes:
  --dry-run  (default)   Reports what would happen. No writes.
  --commit               Performs the writes.
  --max=N                Cap to N trades per run (test mode).

Reuses existing utilities — does not duplicate the close path:
  * agents/price_agent.fetch_bars         (already has fallback after 6/2)
  * agents/price_agent.compute_paper_outcome  (canonical close logic)
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from datetime import datetime, timezone, timedelta

import requests

# Ensure agents/ is importable so we can reuse the canonical close logic.
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "agents"))

import price_agent       # noqa: E402  reuse fetch_bars (with fallback) + compute_paper_outcome


SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
HEADERS_SB = {
    "apikey": SUPABASE_SERVICE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=minimal",
}


def fetch_stuck() -> list[dict]:
    """All open paper trades whose horizon has clearly passed (+1d buffer).

    Paginates because PostgREST silently caps single-page responses at 2000
    rows — and stock_event_paper_trades currently has >5000 open trades
    across horizons. Without pagination, only the OLDEST 2000 were seen,
    which excluded most of the stuck h1d backlog (the h1d trades are newer
    than the h30d open queue).
    """
    now = datetime.now(timezone.utc)
    rows: list[dict] = []
    offset = 0
    page = 1000
    while True:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/stock_event_paper_trades",
            headers=HEADERS_SB,
            params={
                "status": "eq.open",
                "select": "id,ticker,direction,entry_at,entry_price,horizon_days,"
                          "target_pct,stop_pct,rule_key,event_type,event_subtype",
                "order":  "entry_at.asc",
                "limit":  str(page),
                "offset": str(offset),
            },
            timeout=30,
        )
        r.raise_for_status()
        chunk = r.json() or []
        if not chunk:
            break
        for t in chunk:
            try:
                entry = datetime.fromisoformat(t["entry_at"].replace("Z", "+00:00"))
            except Exception:
                continue
            horizon = int(t.get("horizon_days") or 1)
            # +1 day buffer past horizon — anything past entry+horizon+1 is "stuck"
            if now - entry >= timedelta(days=horizon + 1):
                rows.append(t)
        if len(chunk) < page:
            break
        offset += page
    return rows


def close_trade(t: dict, outcome: dict) -> None:
    requests.patch(
        f"{SUPABASE_URL}/rest/v1/stock_event_paper_trades?id=eq.{t['id']}",
        headers=HEADERS_SB,
        json={
            "status":          "closed",
            "exit_at":         outcome["exit_at"],
            "exit_price":      outcome["exit_price"],
            "realized_return": outcome["realized_return"],
            "correct":         outcome["correct"],
            "mfe_pct":         outcome.get("mfe_pct"),
            "mae_pct":         outcome.get("mae_pct"),
            "target_hit":      outcome.get("target_hit"),
            "stop_hit":        outcome.get("stop_hit"),
        },
        timeout=15,
    ).raise_for_status()


def force_close_unrecoverable(t: dict) -> None:
    """Mark a trade closed with close_reason='no_bars_unrecoverable'.

    Used when fetch_bars returns nothing even after the stock_raw_prices
    fallback. realized_return is 0 and correct is None — the row exits the
    'open' bucket but does NOT poison calibration metrics (downstream
    `correct is null` filters skip it).
    """
    requests.patch(
        f"{SUPABASE_URL}/rest/v1/stock_event_paper_trades?id=eq.{t['id']}",
        headers=HEADERS_SB,
        json={
            "status":          "closed",
            "exit_at":         datetime.now(timezone.utc).date().isoformat()
                               + "T00:00:00+00:00",
            "exit_price":      None,
            "realized_return": None,
            "correct":         None,
            "notes":           "no_bars_unrecoverable",
        },
        timeout=15,
    ).raise_for_status()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true", help="Actually write to DB.")
    ap.add_argument("--max", type=int, default=0, help="Cap to N trades (0 = no cap).")
    args = ap.parse_args()
    dry = not args.commit

    print(f"close_stuck_paper_trades: dry_run={dry} max={args.max or 'all'}")
    stuck = fetch_stuck()
    if args.max:
        stuck = stuck[: args.max]
    print(f"  found {len(stuck)} stuck open trades past horizon+1d")

    # Group by ticker so we fetch bars once per ticker (matches price_agent's pattern).
    by_ticker: dict[str, list[dict]] = {}
    for t in stuck:
        by_ticker.setdefault(t["ticker"], []).append(t)
    print(f"  {len(by_ticker)} distinct tickers")

    counters = Counter()
    bars_cache: dict[str, dict] = {}
    sample_unrecov: list[str] = []

    for ticker, trades in by_ticker.items():
        # Compute the widest entry → exit window across trades for this ticker.
        first_entry = min(
            datetime.fromisoformat(t["entry_at"].replace("Z", "+00:00")).date()
            for t in trades
        )
        last_exit = max(
            datetime.fromisoformat(t["entry_at"].replace("Z", "+00:00")).date()
            + timedelta(days=int(t.get("horizon_days") or 1) + 3)
            for t in trades
        )
        bars = price_agent.fetch_bars(ticker, first_entry, last_exit)
        bars_cache[ticker] = bars
        for t in trades:
            if not bars:
                counters["no_bars"] += 1
                if len(sample_unrecov) < 8:
                    sample_unrecov.append(f"{ticker} h={t['horizon_days']}")
                if not dry:
                    force_close_unrecoverable(t)
                continue
            outcome = price_agent.compute_paper_outcome(t, bars)
            if outcome is None:
                counters["no_outcome"] += 1
                if len(sample_unrecov) < 8:
                    sample_unrecov.append(f"{ticker} h={t['horizon_days']} (no_outcome)")
                if not dry:
                    force_close_unrecoverable(t)
                continue
            counters["closeable"] += 1
            if not dry:
                close_trade(t, outcome)

    print()
    print("=" * 60)
    print(f"Outcome ({'DRY RUN — no writes' if dry else 'COMMITTED'}):")
    print(f"  closeable          : {counters['closeable']}")
    print(f"  no bars (force)    : {counters['no_bars']}")
    print(f"  no outcome (force) : {counters['no_outcome']}")
    if sample_unrecov:
        print(f"  sample unrecov     : {', '.join(sample_unrecov)}")
    if dry:
        print()
        print("Re-run with --commit to actually close these trades.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
