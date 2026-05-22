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
import yfinance as yf

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from filing_agent import (   # type: ignore
    job_run_start, job_run_finish, dead_letter,
    SUPABASE_URL, HEADERS_SB,
)
import _rule_key   # type: ignore  # agents/ is on sys.path at runtime

LOOKBACK_MIN = 150              # 120min cron window + 30min buffer for GHA queue jitter
SEVERITY_FLOOR = 2              # ignore noise events
# Multi-horizon: every event opens four parallel paper trades. Same direction,
# same entry price, different exit horizons. rule_key carries the horizon so
# stock_rule_calibration tracks accuracy per (event_type:subtype:hNd) — the
# system learns whether "earnings_release:beat" plays out at 1d (front-running),
# 7d (settlement), 15d (PEAD window), or 30d (longer drift). User intent: learn
# WHICH horizon each signal type rewards instead of guessing.
HORIZONS = (1, 7, 15, 30)
TARGET_PCT = 0.05
STOP_PCT = 0.03
INSERT_CHUNK = 200

# Stale-price gate: if the most recent close from stock_raw_prices is older
# than this many days, skip the paper trade for that ticker. Reason: silently
# entering at a 5-day-old "latest close" on an illiquid ticker pollutes
# calibration with a near-meaningless entry price. Default 3 days covers
# the longest US market closure (3-day weekend + holiday) without false
# positives during normal trading.
STALE_PRICE_MAX_AGE_DAYS = 3

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
    and don't fit the next-session-close paper-trade contract.

    PostgREST embedded-column filters: `stock_symbols.kind=in.(stock,etf)`
    works; `or=(...)` does not on embedded columns (PGRST100). Lesson stays
    in this comment so the next person doesn't relearn it."""
    url = (
        f"{SUPABASE_URL}/rest/v1/stock_watchlists"
        f"?select=ticker,stock_symbols!inner(kind)"
        f"&stock_symbols.kind=in.(stock,etf)"
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
            ("created_at", f"gte.{since_iso}"),
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
    """{ticker → most recent {ts, close} row} from stock_raw_prices.

    Falls back to yfinance for any ticker missing from the DB **or whose
    most-recent DB close is older than STALE_PRICE_MAX_AGE_DAYS**. Without
    the stale-eviction the agent would treat a 10-day-old row as
    authoritative, the downstream stale-price gate would then drop the
    event, and we'd silently write zero paper trades — the bug observed
    2026-05-19 → 2026-05-21 when daily-bar ingest had stopped.
    """
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
    latest: dict[str, dict] = {}
    if r.status_code == 200:
        for row in r.json():
            t = row.get("ticker")
            if t and t not in latest and row.get("close") is not None:
                latest[t] = row

    # Evict stale-but-present rows so the yfinance fallback below refreshes them.
    # Same threshold as the main()-level stale-price gate so behaviour is
    # consistent: anything the gate would reject we try to refresh first.
    stale_cutoff = datetime.now(timezone.utc) - timedelta(days=STALE_PRICE_MAX_AGE_DAYS)
    for t in list(latest.keys()):
        ts_raw = latest[t].get("ts") or ""
        try:
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts < stale_cutoff:
                del latest[t]
        except (TypeError, ValueError):
            del latest[t]

    # yfinance fallback for tickers with no DB price data — single batched download,
    # ~5-10× faster than per-ticker yf.Ticker.history() calls.
    missing = [t for t in tickers if t not in latest]
    if missing:
        backfill_rows: list[dict] = []
        try:
            df = yf.download(
                tickers=" ".join(missing),
                period="5d", interval="1d",
                group_by="ticker", progress=False,
                auto_adjust=True, threads=True,
            )
        except Exception as exc:
            print(f"  yfinance batch fallback failed: {exc}", file=sys.stderr)
            df = None
        if df is not None and not df.empty:
            for ticker in missing:
                try:
                    sub = df[ticker] if len(missing) > 1 else df
                    closes = sub["Close"].dropna()
                    if closes.empty:
                        continue
                    last_idx = closes.index[-1]
                    close = float(closes.iloc[-1])
                    if close <= 0:
                        print(f"  yfinance fallback {ticker}: bad close {close}", file=sys.stderr)
                        continue
                    ts_iso = last_idx.isoformat()
                    latest[ticker] = {"ticker": ticker, "ts": ts_iso, "close": close}
                    backfill_rows.append({
                        "ticker": ticker, "ts": ts_iso,
                        "open":   float(sub["Open"].loc[last_idx]),
                        "high":   float(sub["High"].loc[last_idx]),
                        "low":    float(sub["Low"].loc[last_idx]),
                        "close":  close,
                        "volume": int(sub["Volume"].loc[last_idx]),
                        "source": "yfinance_fallback",
                    })
                except Exception as exc:
                    print(f"  yfinance fallback {ticker}: {exc}", file=sys.stderr)
        if backfill_rows:
            # CLAUDE.md rule #2: PostgREST on_conflict=ticker,ts fails 42P10
            # against stock_raw_prices (the table's unique index is partial).
            # Plain INSERT with resolution=ignore-duplicates is the working
            # pattern used elsewhere in this repo. Also check status — the
            # prior unconditional print masked the 400 for weeks.
            wbr = requests.post(
                f"{SUPABASE_URL}/rest/v1/stock_raw_prices",
                headers={**HEADERS_SB, "Prefer": "resolution=ignore-duplicates,return=minimal"},
                json=backfill_rows,
                timeout=20,
            )
            if wbr.status_code in (200, 201, 204):
                print(f"  wrote {len(backfill_rows)} yfinance fallback price(s) to stock_raw_prices")
            else:
                print(f"  yfinance writeback FAILED {wbr.status_code}: {wbr.text[:200]}",
                      file=sys.stderr)
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


def derive_rule_key(event: dict, horizon_days: int) -> str:
    """Granular rule identity. Subtype + horizon included so beat vs miss AND
    1d vs 7d vs 15d vs 30d earn separate calibration tracks. Delegates to the
    canonical agents._rule_key.derive so trade_setup_agent and thesis_agent
    compute the exact same string for the same inputs."""
    return _rule_key.derive(event["event_type"], event.get("event_subtype"), horizon_days)


def build_paper_trades(event: dict, ticker_kind: str, latest_close: dict) -> list[dict]:
    """Four open paper trades per event — one per horizon in HORIZONS.
    Returns [] if entry price is unavailable (yfinance backfill gap)."""
    try:
        entry_price = float(latest_close["close"])
    except (TypeError, ValueError, KeyError):
        return []
    if entry_price <= 0:
        return []
    direction = derive_direction(event)
    rows = []
    for h in HORIZONS:
        rows.append({
            "event_id":       event["id"],
            "event_type":     event["event_type"],
            "event_subtype":  event.get("event_subtype"),
            "ticker":         event["ticker"],
            "vehicle_type":   ticker_kind,
            "direction":      direction,
            "entry_at":       latest_close["ts"],
            "entry_price":    round(entry_price, 4),
            "horizon_days":   h,
            # target/stop scale loosely with horizon — longer holds need wider
            # bands to accommodate normal volatility around the trend.
            "target_pct":     round(TARGET_PCT * (1 + (h - 1) * 0.05), 4),
            "stop_pct":       round(STOP_PCT  * (1 + (h - 1) * 0.05), 4),
            "status":         "open",
            "rule_key":       derive_rule_key(event, h),
            "notes":          None,
        })
    return rows


def fetch_already_traded_keys(event_ids: list[int]) -> set[tuple[int, int]]:
    """Return composite (event_id, horizon_days) pairs already in stock_event_paper_trades.

    Composite — not event_id alone — so that a partial prior write (e.g.,
    only h=1d landed before a crash) doesn't lock out the missing horizons
    on rerun. With the previous event-id-only filter, the entire event was
    marked "done" once any single horizon row existed; h=7/15/30 then never
    healed. Switching to composite keys lets each missing horizon insert
    independently while concurrent runs still hit the unique constraint
    (which Prefer:resolution=ignore-duplicates absorbs into 201).
    """
    if not event_ids:
        return set()
    in_list = ",".join(str(i) for i in event_ids)
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/stock_event_paper_trades",
        headers=HEADERS_SB,
        params={"event_id": f"in.({in_list})", "select": "event_id,horizon_days", "limit": "5000"},
        timeout=15,
    )
    if r.status_code != 200:
        return set()
    return {
        (row["event_id"], row["horizon_days"])
        for row in r.json()
        if row.get("event_id") is not None and row.get("horizon_days") is not None
    }


def write_paper_trades(rows: list[dict]) -> int:
    """Plain INSERT with resolution=ignore-duplicates.

    Despite sql/0015's intent, PostgREST still returns 42P10 on
    ?on_conflict=event_id,ticker,direction (the unique index on
    stock_event_paper_trades is still partial in practice — verified
    via direct POST 2026-05-21). Caller pre-filters via
    fetch_already_traded_keys so the dedupe path is upstream,
    and the table's own unique constraint catches the residual race
    between concurrent runs. Concurrent-collision rows hit the constraint
    and the whole chunk would normally 409 — Prefer:resolution=ignore-
    duplicates downgrades that to a 201 with skipped rows.
    """
    if not rows:
        return 0
    headers = {
        **HEADERS_SB,
        "Prefer": "return=minimal,resolution=ignore-duplicates",
    }
    inserted = 0
    for i in range(0, len(rows), INSERT_CHUNK):
        batch = rows[i:i+INSERT_CHUNK]
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/stock_event_paper_trades",
            headers=headers, json=batch, timeout=30,
        )
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

        # Composite-key dedupe: fetched once up front, filtered per-row after
        # build_paper_trades so partial prior writes (only some horizons
        # landed) can heal on rerun. Per-event filtering was the old behavior
        # and caused horizons 7/15/30 to never insert if h=1d alone landed.
        already_traded = fetch_already_traded_keys([e["id"] for e in tradeable_events])
        if already_traded:
            print(f"  found {len(already_traded)} existing (event,horizon) pairs — will skip those")
        if n_skipped:
            print(f"  skipped {n_skipped} events on non-tradeable tickers (mutual funds, indices, INST_*)")

        tickers = sorted({e["ticker"] for e in tradeable_events})
        closes = fetch_latest_closes(tickers)

        # Stale-price gate: drop tickers whose latest close is older than
        # STALE_PRICE_MAX_AGE_DAYS. Logged so we can audit how many
        # would-be paper trades are skipped due to stale data — high
        # stale-skip count is a sign the ingest path needs attention.
        now_utc = datetime.now(timezone.utc)
        stale_threshold = now_utc - timedelta(days=STALE_PRICE_MAX_AGE_DAYS)
        stale_tickers: dict[str, str] = {}
        for t, row in list(closes.items()):
            ts_raw = row.get("ts") or ""
            try:
                ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
                # Supabase returns timestamptz as "2026-05-11T00:00:00+00:00"
                # but legacy/migrated rows may be naive. Force tz-aware to
                # avoid "can't compare offset-naive and offset-aware datetimes"
                # (regression introduced in commit d4627b3, observed 2026-05-13+).
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                stale_tickers[t] = "unparseable_ts"
                del closes[t]
                continue
            if ts < stale_threshold:
                age_days = (now_utc - ts).days
                stale_tickers[t] = f"close_{age_days}d_old"
                del closes[t]

        rows: list[dict] = []
        n_skipped_stale = 0
        for e in tradeable_events:
            t = e["ticker"]
            close_row = closes.get(t)
            if not close_row:
                if t in stale_tickers:
                    n_skipped_stale += 1
                continue
            # 4 trades per event — one per horizon in HORIZONS
            rows.extend(build_paper_trades(e, kinds[t], close_row))

        # Composite-key filter: drop only the (event,horizon) pairs already
        # written, not the whole event. Missing horizons from a partial prior
        # run will still flow through to write_paper_trades.
        n_built = len(rows)
        rows = [r for r in rows if (r["event_id"], r["horizon_days"]) not in already_traded]
        n_filtered_existing = n_built - len(rows)
        if n_filtered_existing:
            print(f"  filtered {n_filtered_existing} rows whose (event,horizon) already existed")
        n_written = write_paper_trades(rows)

        if stale_tickers:
            print(f"  STALE-PRICE GATE: dropped {len(stale_tickers)} tickers, "
                  f"skipping {n_skipped_stale} events. Sample: "
                  f"{dict(list(stale_tickers.items())[:5])}")

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
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill-days", type=int, default=0,
                    help="Replay events created in the last N calendar days instead of LOOKBACK_MIN window.")
    args = ap.parse_args()
    if args.backfill_days > 0:
        # Override LOOKBACK_MIN globally so main() uses the wider window
        LOOKBACK_MIN = args.backfill_days * 24 * 60
    sys.exit(main())
