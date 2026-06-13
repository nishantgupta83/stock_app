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
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
import yfinance as yf

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from filing_agent import (   # type: ignore
    job_run_start, job_run_finish, dead_letter,
    SUPABASE_URL, HEADERS_SB,
)
import _rule_key   # type: ignore  # agents/ is on sys.path at runtime

LOOKBACK_MIN = 150              # 120min cron window + 30min buffer for GHA queue jitter
# FIX-2 (2026-06-05): entry must be anchored to the close on/after the event,
# not the latest close (which leaked pre-event prices into calibration). An
# intraday event's same-day EOD close may not be ingested yet, so the trade is
# DEFERRED and retried until that close lands. We therefore re-scan events over
# a multi-day window (not just LOOKBACK_MIN) and anti-join against already-
# opened events so only genuinely-unfilled ones are processed. 4 days covers a
# 3-day holiday weekend. Egress note: this re-reads the recent-events window
# each run; the egress-optimal form is a server-side anti-join VIEW (follow-up).
RETRY_LOOKBACK_DAYS = 4
# Live runs floor the entry anchor at created_at (we can't enter before we KNEW
# of an event — prevents a late-ingested live row from filling at a backdated
# close price_agent would then close with hindsight). Backfill runs anchor on
# event_at alone (intentional historical simulation). Set True by --backfill-days.
BACKFILL_MODE = False
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
            ("select",   "id,event_type,event_subtype,ticker,event_at,created_at,severity,payload"),
            ("order",    "event_at.asc"),
            ("limit",    "2000"),
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


def fetch_close_window(tickers: list[str], since_date: str) -> dict[str, list[dict]]:
    """{ticker → [{ts, close} rows sorted ASC]} from stock_raw_prices since
    `since_date` (a YYYY-MM-DD). Unlike fetch_latest_closes (latest only), this
    returns the full window so entry can be anchored to the close on/after each
    event's own date — required for both live deferral and historical backfill.
    Paginated so a wide window across many tickers isn't silently truncated."""
    if not tickers:
        return {}
    in_list = ",".join(f'"{t}"' for t in tickers)
    out: dict[str, list[dict]] = {}
    offset, page = 0, 1000
    while True:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/stock_raw_prices",
            headers=HEADERS_SB,
            params={
                "ticker": f"in.({in_list})",
                "ts":     f"gte.{since_date}",
                "select": "ticker,ts,close",
                "order":  "ticker.asc,ts.asc",
                "offset": str(offset),
                "limit":  str(page),
            },
            timeout=30,
        )
        if r.status_code != 200:
            print(f"  fetch_close_window: {r.status_code} {r.text[:200]}", file=sys.stderr)
            break
        batch = r.json()
        for row in batch:
            t = row.get("ticker")
            if t and row.get("close") is not None:
                out.setdefault(t, []).append(row)
        if len(batch) < page:
            break
        offset += page
    return out


def _to_date(x) -> str | None:
    try:
        return datetime.fromisoformat(str(x).replace("Z", "+00:00")).date().isoformat()
    except (TypeError, ValueError):
        return None


_ET = ZoneInfo("America/New_York")
_MARKET_CLOSE_ET_HOUR = 16   # 16:00 ET regular-session close


def _entry_anchor_from_ts(ts) -> str | None:
    """Entry-anchor date (YYYY-MM-DD) for a timestamp, with the H2 after-hours
    bump: an event at/after the 16:00 ET close enters at the NEXT calendar day's
    close, not that same day's 16:00 (pre-event) close.

    Convert to America/New_York BEFORE taking the date — a UTC timestamp just
    after midnight is the PRIOR ET day, after-close (00:30Z → 20:30 ET prev day).
    Naive timestamps are assumed UTC (never the local machine tz). zoneinfo
    handles EDT/EST. pick_entry_close then rolls Sat/holiday anchors to the next
    real session, so no market calendar is needed (early-close half-days are an
    accepted residual — a 14:00 ET filing on a 13:00 close day still anchors
    same-day; full half-day modelling is out of scope)."""
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    et = dt.astimezone(_ET)
    d = et.date()
    if et.hour >= _MARKET_CLOSE_ET_HOUR:
        d = d + timedelta(days=1)
    return d.isoformat()


def _event_anchor_date(event: dict, floor_created_at: bool = False) -> str | None:
    """The date (YYYY-MM-DD) the entry close must be on/after.

    Base anchor = the event's own date (event_at), so a historically-replayed
    BACKFILL event enters at its OWN event-day close, not today's.

    When `floor_created_at` (live runs), the anchor is raised to
    max(event_at, created_at): we cannot enter before we KNEW of the event, so a
    late-ingested live row (old event_at, recent created_at) won't fill at a
    backdated close that price_agent would then close with hindsight. For a
    normal live event event_at ≈ created_at, so this is a no-op.

    H2: both dates get the after-16:00-ET bump (a row whose event OR ingest is
    after the close must not fill at that day's pre-event/pre-ingest close)."""
    ead = _entry_anchor_from_ts(event.get("event_at"))
    if ead is None:
        return None
    if floor_created_at:
        cad = _entry_anchor_from_ts(event.get("created_at"))
        if cad:
            return max(ead, cad)
    return ead


def pick_entry_close(event: dict, closes_asc: list[dict],
                     floor_created_at: bool = False) -> dict | None:
    """First close on/after the event's anchor date (the earliest tradable
    close once the event was known). None → defer: the anchor close hasn't
    landed yet (intraday before EOD ingest), so the trade is retried next run
    rather than filled at a pre-event price.

    The anchor (_event_anchor_date → _entry_anchor_from_ts) already applies the
    H2 after-16:00-ET bump, so an after-close event's anchor is the NEXT calendar
    day and this date-level scan correctly skips the same-day (pre-event) 16:00
    close. (Early-close half-days remain an accepted residual — see
    _entry_anchor_from_ts.)"""
    anchor = _event_anchor_date(event, floor_created_at=floor_created_at)
    if anchor is None or not closes_asc:
        return None
    for row in closes_asc:
        ts = row.get("ts") or ""
        if ts[:10] >= anchor:
            try:
                if float(row["close"]) > 0:
                    return row
            except (TypeError, ValueError, KeyError):
                continue
    return None


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
        # FIX-2: scan a multi-day window (not just LOOKBACK_MIN) so events whose
        # anchor close wasn't ingested yet get retried until it lands. Backfill
        # (--backfill-days widens LOOKBACK_MIN) automatically extends this.
        lookback_days = max(RETRY_LOOKBACK_DAYS, LOOKBACK_MIN / (24 * 60))
        since_dt = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        since = since_dt.isoformat()
        events = fetch_recent_events(SEVERITY_FLOOR, since)
        n_events = len(events)
        print(f"Found {n_events} events since {since[:19]} (severity ≥ {SEVERITY_FLOOR}, "
              f"{lookback_days:.1f}d retry window)")
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

        # FIX-2 anti-join: skip only events that are ALREADY COMPLETE (all
        # horizons present), so the multi-day retry scan doesn't re-process
        # fully-opened events — but PARTIAL prior writes (only some horizons
        # landed) still flow through to heal their missing horizons via the
        # composite (event,horizon) filter below. Excluding any-horizon events
        # here would permanently strand partials (regression caught in review).
        horizon_counts = Counter(eid for (eid, _h) in already_traded)
        complete_event_ids = {eid for eid, c in horizon_counts.items() if c >= len(HORIZONS)}
        pending = [e for e in tradeable_events if e["id"] not in complete_event_ids]
        print(f"  {len(pending)} events to anchor "
              f"({len(complete_event_ids)} already complete; "
              f"{len(horizon_counts) - len(complete_event_ids)} partial → healing)")

        # Per-event anchored entry: fetch the close window once, then for each
        # event pick the first close ON/AFTER its event date. No pre-event-price
        # fills; events whose anchor close hasn't ingested yet are deferred.
        tickers = sorted({e["ticker"] for e in pending})
        # Side-effect: yfinance-backfill missing/stale tickers' latest close into
        # stock_raw_prices (the 5/19–5/21 ingest-gap safety net) BEFORE reading
        # the window. We don't use the return value — fetch_close_window reads
        # the now-refreshed table.
        fetch_latest_closes(tickers)
        close_window = fetch_close_window(tickers, since_dt.date().isoformat())

        rows: list[dict] = []
        n_deferred = 0
        deferred_tickers: set[str] = set()
        for e in pending:
            entry_close = pick_entry_close(e, close_window.get(e["ticker"], []),
                                           floor_created_at=not BACKFILL_MODE)
            if entry_close is None:
                n_deferred += 1   # anchor close (>= event date) not available yet → retry next run
                deferred_tickers.add(e["ticker"])
                continue
            # 4 trades per event — one per horizon in HORIZONS
            rows.extend(build_paper_trades(e, kinds[e["ticker"]], entry_close))

        # Composite-key filter: drop only the (event,horizon) pairs already
        # written, not the whole event. Missing horizons from a partial prior
        # run will still flow through to write_paper_trades.
        n_built = len(rows)
        rows = [r for r in rows if (r["event_id"], r["horizon_days"]) not in already_traded]
        n_filtered_existing = n_built - len(rows)
        if n_filtered_existing:
            print(f"  filtered {n_filtered_existing} rows whose (event,horizon) already existed")
        n_written = write_paper_trades(rows)

        if n_deferred:
            print(f"  DEFERRED {n_deferred} events ({len(deferred_tickers)} tickers): anchor "
                  f"close (>= event date) not yet ingested — retry next run (avoids pre-event "
                  f"fill). Sample: {sorted(deferred_tickers)[:8]}")

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
        # Override LOOKBACK_MIN globally so main() uses the wider window, and
        # anchor on event_at alone (intentional historical simulation — no
        # created_at floor, since we're deliberately replaying past events).
        LOOKBACK_MIN = args.backfill_days * 24 * 60
        BACKFILL_MODE = True
    sys.exit(main())
