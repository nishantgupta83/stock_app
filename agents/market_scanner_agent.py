"""
Market scanner agent — S&P 500 jump verifier + event-outcome observer.

Runs after market close on weekdays (cron 30 21 * * 1-5). For every tracked
stock with |daily return| >= JUMP_PCT, looks back 2 trading days for
normalized events and records one row per (ticker, day, prior event) into
stock_event_outcome_observations.

Output is observation-only: it does NOT change scoring weights. Aggregating
these observations over time tells us which event types most reliably
precede a meaningful move; that signal can later feed thesis_agent's per-rule
calibration.

Why this exists (per user request):
  "make sure there is an agent that verifies the stock jumps of top S&P 500
   ... that way the various parameters are added, adjusted in the earning
   pipeline."

Run via .github/workflows/market_scanner_agent.yml.
"""
from __future__ import annotations

import os
import sys
import time
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


def fetch_recent_closes(tickers: list[str], days_back: int = 5) -> dict[str, list[dict]]:
    """{ticker: [{ts, close}]} ordered ascending. Reads from stock_raw_prices,
    which historical_ingest backfilled and price_agent + site_generator keep
    fresh. We need at least 2 distinct trading days per ticker to compute
    daily return."""
    if not tickers:
        return {}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days_back)).date().isoformat()
    in_list = ",".join(f'"{t}"' for t in tickers)
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/stock_raw_prices",
        headers=HEADERS_SB,
        params={
            "ticker": f"in.({in_list})",
            "ts":     f"gte.{cutoff}",
            "select": "ticker,ts,close",
            "order":  "ts.asc",
            "limit":  "5000",
        },
        timeout=20,
    )
    if r.status_code != 200:
        print(f"  fetch_recent_closes: {r.status_code} {r.text[:200]}", file=sys.stderr)
        return {}
    by_t: dict[str, list[dict]] = {}
    for row in r.json():
        if row.get("close") is None:
            continue
        by_t.setdefault(row["ticker"], []).append(row)
    return by_t


def fetch_prior_events(ticker: str, end_iso: str, lookback_days: int) -> list[dict]:
    """Events for `ticker` between (end - lookback_days) and end. Includes the
    end day itself so a same-day filing/earnings is captured as a candidate cause."""
    start_iso = (datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
                 - timedelta(days=lookback_days)).isoformat()
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/stock_normalized_events",
        headers=HEADERS_SB,
        params={
            "ticker":   f"eq.{ticker}",
            "event_at": f"gte.{start_iso}",
            "select":   "id,event_type,event_subtype,event_at,severity",
            "order":    "event_at.desc",
            "limit":    "20",
        },
        timeout=15,
    )
    if r.status_code != 200:
        return []
    end_dt = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
    return [e for e in r.json()
            if datetime.fromisoformat(e["event_at"].replace("Z", "+00:00")) <= end_dt]


def write_observations(rows: list[dict]) -> int:
    """Bulk insert into stock_event_outcome_observations with on_conflict so re-runs are idempotent."""
    if not rows:
        return 0
    url = (
        f"{SUPABASE_URL}/rest/v1/stock_event_outcome_observations"
        f"?on_conflict=ticker,observed_at,prior_event_id"
    )
    headers = {**HEADERS_SB, "Prefer": "resolution=ignore-duplicates,return=minimal"}
    r = requests.post(url, headers=headers, json=rows, timeout=30)
    if r.status_code not in (200, 201, 204):
        print(f"  observation insert {r.status_code}: {r.text[:300]}", file=sys.stderr)
        return 0
    return len(rows)


def main() -> int:
    started = time.time()
    run_id = job_run_start("market_scanner_agent")
    n_tickers = n_jumps = n_observations = 0

    try:
        tickers = fetch_tradeable_tickers()
        n_tickers = len(tickers)
        print(f"Scanning {n_tickers} tickers, jump threshold {JUMP_PCT*100:.1f}%")

        closes_map = fetch_recent_closes(tickers, days_back=5)

        rows_to_write: list[dict] = []

        for ticker in tickers:
            bars = closes_map.get(ticker) or []
            if len(bars) < 2:
                continue
            today = bars[-1]
            prior = bars[-2]
            try:
                t_close = float(today["close"])
                p_close = float(prior["close"])
            except (TypeError, ValueError):
                continue
            if p_close <= 0:
                continue
            ret = (t_close - p_close) / p_close
            if abs(ret) < JUMP_PCT:
                continue
            n_jumps += 1

            events = fetch_prior_events(ticker, today["ts"], LOOKBACK_DAYS)
            if not events:
                # Still record the jump with a NULL prior_event_id so we can
                # see "big moves with no tracked event" — useful gap signal.
                rows_to_write.append({
                    "observed_at":           today["ts"],
                    "ticker":                ticker,
                    "daily_return_pct":      round(ret, 6),
                    "prior_event_id":        None,
                    "prior_event_type":      "no_tracked_event",
                    "prior_event_subtype":   None,
                    "prior_event_severity":  None,
                    "prior_event_age_hours": 0,
                    "source":                "market_scanner",
                    "notes":                 f"|move|={abs(ret)*100:.2f}% with no event in last {LOOKBACK_DAYS} days",
                })
                continue

            today_dt = datetime.fromisoformat(today["ts"].replace("Z", "+00:00"))
            for e in events:
                e_dt = datetime.fromisoformat(e["event_at"].replace("Z", "+00:00"))
                age_hours = max(0, (today_dt - e_dt).total_seconds() / 3600.0)
                rows_to_write.append({
                    "observed_at":           today["ts"],
                    "ticker":                ticker,
                    "daily_return_pct":      round(ret, 6),
                    "prior_event_id":        e["id"],
                    "prior_event_type":      e["event_type"],
                    "prior_event_subtype":   e.get("event_subtype"),
                    "prior_event_severity":  e.get("severity"),
                    "prior_event_age_hours": round(age_hours, 2),
                    "source":                "market_scanner",
                })

        n_observations = write_observations(rows_to_write)
        elapsed = time.time() - started
        print(f"DONE in {elapsed:.1f}s — {n_jumps} jumps from {n_tickers} tickers, "
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
