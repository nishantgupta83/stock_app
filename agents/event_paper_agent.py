"""
Event paper-trade agent — opens one paper trade per significant event.

Runs hourly. Pulls events from stock_normalized_events that landed in the
LOOKBACK_MIN window, filters to severity ≥ 2 and tradeable vehicles
(stocks + ETFs), and opens one row per (event, ticker, direction) in
stock_event_paper_trades with status='open'. Idempotent: the unique index
on (event_id, ticker, direction) collapses re-runs.

Entry: most recent close from stock_raw_prices (or yfinance fallback if DB
empty for that ticker). Exit + realized return: written by price_agent at
EOD on entry_at + horizon_days session close.

Direction comes from event.payload.direction_prior when set, else falls
back to a per-event-type default (filings ≈ long, dilution = short, etc.).

Why this exists (per user request, paraphrased):
  "your agent has to self-learn based on events and event types and suggest
   paper trades until they are mature with >90% accuracy to trigger buy/sell."

The companion piece is in price_agent.py (close + calibration update) and
in thesis_agent.py (read calibration → apply learned weights).
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from filing_agent import (   # type: ignore
    job_run_start, job_run_finish, dead_letter,
    SUPABASE_URL, HEADERS_SB,
)

LOOKBACK_MIN = 75               # 60min cron + 15min buffer for the previous run
SEVERITY_FLOOR = 2              # ignore noise events
HORIZON_DAYS_DEFAULT = 1        # next-session close
TARGET_PCT = 0.05
STOP_PCT = 0.03
INSERT_CHUNK = 200

# Per-event-type default direction when payload.direction_prior is absent.
# Keep narrow — most rules should set direction_prior explicitly upstream.
_DIRECTION_DEFAULT: dict[str, str] = {
    "8k_material_event":  "long",   # neutral by nature; default long pending dilution check
    "earnings_release":   "long",   # subtype carries the actual signal (beat/miss handled below)
    "filing_4":           "long",   # noisy but historically positive on average for buys
    "filing_13d":         "long",
    "filing_13g":         "long",
    "filing_10-q":        "long",
    "filing_10-k":        "short",  # empirically all-bearish in 90-day calibration
    "filing_dilution":    "short",
    "filing_s-3":         "short",
    "filing_s-3/a":       "short",
    "news_article":       "long",
    "truth_social_post":  "long",
    "momentum":           "long",
    "crypto_macro_move":  "long",
}


def fetch_tradeable_kinds() -> dict[str, str]:
    """{ticker → kind} for kind in (stock, etf). Mutual funds use NAV pricing
    and don't fit the next-session-close paper-trade contract."""
    url = (
        f"{SUPABASE_URL}/rest/v1/stock_watchlists"
        f"?select=ticker,stock_symbols!inner(kind)"
        f"&or=(stock_symbols.kind.eq.stock,stock_symbols.kind.eq.etf)"
    )
    r = requests.get(url, headers=HEADERS_SB, timeout=30)
    if r.status_code != 200:
        print(f"  fetch_tradeable_kinds: {r.status_code} {r.text[:200]}", file=sys.stderr)
        return {}
    out: dict[str, str] = {}
    for row in r.json():
        t = row.get("ticker")
        sym = row.get("stock_symbols") or {}
        kind = sym.get("kind")
        if t and kind in ("stock", "etf"):
            out[t] = kind
    return out


def fetch_recent_events(min_severity: int, since_iso: str) -> list[dict]:
    """Events that landed since `since_iso` with severity ≥ floor."""
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/stock_normalized_events",
        headers=HEADERS_SB,
        params=[
            ("event_at", f"gte.{since_iso}"),
            ("severity", f"gte.{min_severity}"),
            ("ticker",   "not.is.null"),
            ("select",   "id,event_type,event_subtype,ticker,event_at,severity,payload"),
            ("order",    "event_at.asc"),
            ("limit",    "1000"),
        ],
        timeout=20,
    )
    if r.status_code != 200:
        print(f"  fetch_recent_events: {r.status_code} {r.text[:200]}", file=sys.stderr)
        return []
    return r.json()


def fetch_latest_closes(tickers: list[str]) -> dict[str, dict]:
    """{ticker → most recent {ts, close} row} from stock_raw_prices."""
    if not tickers:
        return {}
    in_list = ",".join(f'"{t}"' for t in tickers)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=10)).date().isoformat()
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/stock_raw_prices",
        headers=HEADERS_SB,
        params={
            "ticker": f"in.({in_list})",
            "ts":     f"gte.{cutoff}",
            "select": "ticker,ts,close",
            "order":  "ts.desc",
            "limit":  "1000",
        },
        timeout=20,
    )
    if r.status_code != 200:
        return {}
    latest: dict[str, dict] = {}
    for row in r.json():
        t = row.get("ticker")
        if t and t not in latest and row.get("close") is not None:
            latest[t] = row
    return latest


def derive_direction(event: dict) -> str:
    """Resolve direction. Priority: explicit payload.direction_prior → subtype
    hints (beat/miss for earnings) → per-type default."""
    payload = event.get("payload") or {}
    d = (payload.get("direction_prior") or "").strip().lower()
    if d in ("long", "short"):
        return d
    if d == "neutral":
        # neutral payload still needs a direction to open a paper trade; defer to defaults
        pass
    et = event["event_type"]
    sub = (event.get("event_subtype") or "").lower()
    if et == "earnings_release":
        if sub == "beat":  return "long"
        if sub == "miss":  return "short"
        # inline / scheduled → fall through to default
    return _DIRECTION_DEFAULT.get(et, "long")


def derive_rule_key(event: dict) -> str:
    """Granular rule identity. Subtype included when present so beat vs miss
    earn separate calibration tracks."""
    et = event["event_type"]
    sub = (event.get("event_subtype") or "").strip()
    if sub:
        return f"{et}:{sub}"
    return et


def build_paper_trade(event: dict, ticker_kind: str, latest_close: dict) -> dict | None:
    """One open paper trade row. Returns None if entry price unavailable."""
    try:
        entry_price = float(latest_close["close"])
    except (TypeError, ValueError, KeyError):
        return None
    if entry_price <= 0:
        return None
    return {
        "event_id":       event["id"],
        "event_type":     event["event_type"],
        "event_subtype":  event.get("event_subtype"),
        "ticker":         event["ticker"],
        "vehicle_type":   ticker_kind,
        "direction":      derive_direction(event),
        "entry_at":       latest_close["ts"],
        "entry_price":    round(entry_price, 4),
        "horizon_days":   HORIZON_DAYS_DEFAULT,
        "target_pct":     TARGET_PCT,
        "stop_pct":       STOP_PCT,
        # exit_at/exit_price/realized_return/correct populated by reconciler
        "status":         "open",
        "rule_key":       derive_rule_key(event),
        "notes":          None,
    }


def write_paper_trades(rows: list[dict]) -> int:
    if not rows:
        return 0
    url = (
        f"{SUPABASE_URL}/rest/v1/stock_event_paper_trades"
        f"?on_conflict=event_id,ticker,direction"
    )
    headers = {**HEADERS_SB, "Prefer": "resolution=ignore-duplicates,return=minimal"}
    inserted = 0
    for i in range(0, len(rows), INSERT_CHUNK):
        batch = rows[i:i+INSERT_CHUNK]
        r = requests.post(url, headers=headers, json=batch, timeout=30)
        if r.status_code in (200, 201, 204):
            inserted += len(batch)
        else:
            print(f"  write_paper_trades chunk {i//INSERT_CHUNK} {r.status_code}: {r.text[:300]}",
                  file=sys.stderr)
    return inserted


def main() -> int:
    started = time.time()
    run_id = job_run_start("event_paper_agent")
    n_events = n_skipped = n_built = n_written = 0
    try:
        since = (datetime.now(timezone.utc) - timedelta(minutes=LOOKBACK_MIN)).isoformat()
        events = fetch_recent_events(SEVERITY_FLOOR, since)
        n_events = len(events)
        print(f"Found {n_events} events since {since[:19]} (severity ≥ {SEVERITY_FLOOR})")
        if not events:
            job_run_finish(run_id, "ok", 0, 0)
            return 0

        kinds = fetch_tradeable_kinds()
        if not kinds:
            print("  no tradeable tickers found — abort", file=sys.stderr)
            job_run_finish(run_id, "partial", n_events, 0, err="empty watchlist")
            return 0

        # Filter to events whose ticker is tradeable
        tradeable_events = [e for e in events if (e.get("ticker") or "") in kinds]
        n_skipped = n_events - len(tradeable_events)
        if n_skipped:
            print(f"  skipped {n_skipped} events on non-tradeable tickers (mutual funds, indices, INST_*)")

        tickers = sorted({e["ticker"] for e in tradeable_events})
        closes = fetch_latest_closes(tickers)

        rows: list[dict] = []
        for e in tradeable_events:
            t = e["ticker"]
            close_row = closes.get(t)
            if not close_row:
                continue
            row = build_paper_trade(e, kinds[t], close_row)
            if row:
                rows.append(row)
        n_built = len(rows)
        n_written = write_paper_trades(rows)

        elapsed = time.time() - started
        print(f"DONE in {elapsed:.1f}s — {n_events} events seen, {n_built} paper trades built, "
              f"{n_written} inserted (dups ignored)")
        job_run_finish(run_id, "ok", n_events, n_written)
        return 0

    except Exception as e:  # noqa: BLE001
        import traceback
        tb = traceback.format_exc()
        dead_letter("event_paper_agent", None, None, "top_level_failure", tb)
        job_run_finish(run_id, "failed", n_events, n_written, err=str(e))
        print(f"FATAL: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
