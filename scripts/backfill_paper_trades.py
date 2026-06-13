"""One-shot historical paper-trade backfill.

Replays the last LOOKBACK_DAYS of stock_normalized_events through the same
direction + rule_key + target/stop logic that event_paper_agent uses live,
and writes each event × horizon as a status='closed' row in
stock_event_paper_trades with all outcome fields (mfe/mae/target_hit/
stop_hit) populated from yfinance bars.

After the inserts, recomputes stock_rule_calibration (n_observations,
n_correct, accuracy, mean_realized_pct, is_mature, payoff aggregates)
from scratch for every touched rule_key.

Why: live calibration has too few observations per rule (most n<30) to
trigger maturity, so the BUY/SELL gate stays dormant. Backfill seeds
the calibration table with 6 months of historical outcomes.

Idempotent: skips event_ids that already have a paper trade for the
same (ticker, direction). Re-runnable safely.

Tagged: every row gets notes set to BACKFILL_TAG so the inserted rows
can be filtered out of analyses or rolled back if needed.

Usage:
    # Dry-run (default — no DB writes)
    python scripts/backfill_paper_trades.py

    # Actually insert
    python scripts/backfill_paper_trades.py --commit

    # Different window
    python scripts/backfill_paper_trades.py --days 90 --commit

Env: SUPABASE_URL, SUPABASE_SERVICE_KEY (same as live agents).
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agents"))
import _rule_key  # type: ignore
from _maturity import derive_maturity_flags, collapse_to_effective  # type: ignore
from price_agent import (  # type: ignore  # reuse the live outcome computation
    fetch_bars,
    compute_paper_outcome,
)

# ============================================================
# Constants — must match agents/event_paper_agent.py exactly
# ============================================================

HORIZONS = (1, 7, 15, 30)
TARGET_PCT = 0.05
STOP_PCT = 0.03
INSERT_CHUNK = 200

_DIRECTION_DEFAULT: dict[str, str] = {
    "8k_material_event":  "long",
    "earnings_release":   "long",
    "filing_4":           "long",
    "filing_13d":         "long",
    "filing_13g":         "long",
    "filing_10-q":        "long",
    "filing_10-k":        "short",
    "filing_dilution":    "short",
    "filing_s-3":         "short",
    "filing_s-3/a":       "short",
    "news_article":       "long",
    "truth_social_post":  "long",
    "momentum":           "long",
    "crypto_macro_move":  "long",
}

# Event types we backfill. news_article excluded — too noisy and would
# dominate the sample (134/225 of the last 48h ingest), drowning out
# the high-signal event types we actually care about calibrating.
ELIGIBLE_TYPES = {
    "8k_material_event",
    "earnings_release",
    "filing_4",
    "filing_13d",
    "filing_13g",
    "filing_10-q",
    "filing_10-k",
    "filing_dilution",
    "filing_s-3",
    "filing_s-3/a",
    "clinical_readout",
    "truth_social_post",
    "momentum",
    "crypto_macro_move",
}

MATURITY_ACCURACY = 0.90
MATURITY_MIN_N    = 30
# v1 gate constants — must match agents/price_agent.py.
# Promotions also require payoff sanity, not accuracy alone.
TIER_GATE_TEEN_ACC    = 0.70
TIER_GATE_YOUNG_ACC   = 0.80
TIER_GATE_TEEN_MR     = 0.0     # mean_realized_pct > this for teen
TIER_GATE_YOUNG_PF    = 1.2     # profit_factor > this for young_adult
TIER_GATE_ADULT_PF    = 1.5     # profit_factor > this for adult

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
HEADERS_SB = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
}

BACKFILL_TAG = f"historical_backfill_{date.today().isoformat()}"


# ============================================================
# Direction + rule_key — copied from event_paper_agent so the
# backfill produces byte-for-byte identical rule_keys as live.
# ============================================================

def derive_direction(event: dict) -> str:
    payload = event.get("payload") or {}
    d = (payload.get("direction_prior") or "").strip().lower()
    if d in ("long", "short"):
        return d
    et = event["event_type"]
    sub = (event.get("event_subtype") or "").lower()
    if et == "earnings_release":
        if sub == "beat":  return "long"
        if sub == "miss":  return "short"
    return _DIRECTION_DEFAULT.get(et, "long")


def derive_rule_key(event: dict, horizon_days: int) -> str:
    return _rule_key.derive(event["event_type"], event.get("event_subtype"), horizon_days)


# ============================================================
# Fetch + filter events
# ============================================================

def fetch_eligible_events(lookback_days: int) -> list[dict]:
    """Pull non-news events from the last lookback_days where ticker is set."""
    since = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).strftime('%Y-%m-%dT%H:%M:%SZ')
    type_list = ",".join(f'"{t}"' for t in sorted(ELIGIBLE_TYPES))
    print(f"Pulling events from {since} for {len(ELIGIBLE_TYPES)} event types…")

    out: list[dict] = []
    page_size = 1000
    offset = 0
    while True:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/stock_normalized_events",
            headers=HEADERS_SB,
            params={
                "event_at":   f"gte.{since}",
                "event_type": f"in.({type_list})",
                "ticker":     "not.is.null",
                "severity":   "gte.2",
                "select":     "id,event_type,event_subtype,ticker,event_at,severity,payload",
                "order":      "event_at.asc",
                "limit":      str(page_size),
                "offset":     str(offset),
            },
            timeout=60,
        )
        if r.status_code != 200:
            print(f"  fetch events {r.status_code}: {r.text[:200]}", file=sys.stderr)
            break
        batch = r.json()
        out.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    print(f"  fetched {len(out)} candidate events")
    return out


def fetch_existing_trades_for_events(event_ids: list[int]) -> set[tuple[int, str]]:
    """Return {(event_id, direction)} that already exist as paper trades.
    Pre-filter dedupe so we don't re-insert."""
    if not event_ids:
        return set()
    seen: set[tuple[int, str]] = set()
    for i in range(0, len(event_ids), 200):
        chunk = event_ids[i:i+200]
        in_list = ",".join(str(x) for x in chunk)
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/stock_event_paper_trades",
            headers=HEADERS_SB,
            params={
                "event_id": f"in.({in_list})",
                "select":   "event_id,direction",
                "limit":    "5000",
            },
            timeout=30,
        )
        if r.status_code == 200:
            for row in r.json():
                seen.add((row["event_id"], row["direction"]))
    print(f"  pre-existing paper trades: {len(seen)} (will skip these)")
    return seen


def fetch_tradeable_kinds() -> dict[str, str]:
    """{ticker → kind} for stocks + etfs — same as event_paper_agent."""
    url = (
        f"{SUPABASE_URL}/rest/v1/stock_watchlists"
        f"?select=ticker,stock_symbols!inner(kind)"
        f"&stock_symbols.kind=in.(stock,etf)"
    )
    r = requests.get(url, headers=HEADERS_SB, timeout=30)
    if r.status_code != 200:
        return {}
    out: dict[str, str] = {}
    for row in r.json():
        sym = row.get("stock_symbols") or {}
        if row.get("ticker") and sym.get("kind") in ("stock", "etf"):
            out[row["ticker"]] = sym["kind"]
    return out


# ============================================================
# Build closed-trade rows
# ============================================================

def build_trade_row(event: dict, ticker_kind: str, horizon: int,
                    bars: dict, entry_date: date, entry_price: float) -> dict | None:
    """Construct a fully-closed paper-trade row from historical bars."""
    direction = derive_direction(event)
    # event_paper_agent's target/stop scaling formula
    target_pct = round(TARGET_PCT * (1 + (horizon - 1) * 0.05), 4)
    stop_pct   = round(STOP_PCT  * (1 + (horizon - 1) * 0.05), 4)

    # Faux trade dict so we can reuse compute_paper_outcome verbatim
    fake = {
        "entry_at":    entry_date.isoformat() + "T00:00:00+00:00",
        "entry_price": entry_price,
        "horizon_days": horizon,
        "target_pct": target_pct,
        "stop_pct":   stop_pct,
        "direction":  direction,
    }
    outcome = compute_paper_outcome(fake, bars)
    if not outcome:
        return None
    return {
        "event_id":       event["id"],
        "event_type":     event["event_type"],
        "event_subtype":  event.get("event_subtype"),
        "ticker":         event["ticker"],
        "vehicle_type":   ticker_kind,
        "direction":      direction,
        "entry_at":       fake["entry_at"],
        "entry_price":    round(entry_price, 4),
        "horizon_days":   horizon,
        "target_pct":     target_pct,
        "stop_pct":       stop_pct,
        "status":         "closed",
        "rule_key":       derive_rule_key(event, horizon),
        "notes":          BACKFILL_TAG,
        **outcome,
    }


def next_session_close(bars: dict[date, dict[str, float]], after: date) -> tuple[date, float] | None:
    """First trading session strictly after `after`, returning (date, close)."""
    for d in sorted(bars):
        if d > after and bars[d].get("close"):
            return d, bars[d]["close"]
    return None


# ============================================================
# Calibration recompute (from scratch for affected rules)
# ============================================================

def recompute_calibration(rule_keys: set[str]) -> int:
    """For each rule_key, recompute n_obs/n_correct/accuracy/is_mature
    + payoff aggregates from the table. Returns rule count updated."""
    updated = 0
    for rk in sorted(rule_keys):
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/stock_event_paper_trades",
            headers=HEADERS_SB,
            params={
                "rule_key": f"eq.{rk}",
                "status":   "eq.closed",
                "select":   "ticker,entry_at,realized_return,correct,mfe_pct,mae_pct,target_hit,stop_hit",
                "limit":    "5000",
            },
            timeout=30,
        )
        if r.status_code != 200:
            continue
        rows = r.json()
        if not rows:
            continue
        n = len(rows)
        n_correct = sum(1 for x in rows if x.get("correct"))
        returns = [float(x.get("realized_return") or 0) for x in rows]
        wins = [v for v in returns if v > 0]
        losses = [v for v in returns if v <= 0]
        mean = sum(returns) / n if n else 0
        median = sorted(returns)[n // 2] if n else 0
        accuracy = n_correct / n if n else 0

        mfe_vals = [float(x["mfe_pct"]) for x in rows if x.get("mfe_pct") is not None]
        mae_vals = [float(x["mae_pct"]) for x in rows if x.get("mae_pct") is not None]
        target_hit_rate = sum(1 for x in rows if x.get("target_hit")) / n
        stop_hit_rate   = sum(1 for x in rows if x.get("stop_hit")) / n
        profit_factor = (sum(wins) / abs(sum(losses))) if sum(losses) < 0 else None

        # H1: gate on EFFECTIVE-n (distinct ticker-day clusters), not raw n —
        # raw n over-counts pseudo-replication 2-4x. Shared collapse + gate so
        # backfill cannot re-promote a rule the live path demoted.
        eff = collapse_to_effective(rows)
        _flags = derive_maturity_flags(eff["effective_n"], eff["effective_profit_factor"],
                                       eff["effective_mean_realized_pct"], eff["effective_accuracy"])
        is_mature    = _flags["is_mature"]
        is_mature_70 = _flags["is_mature_70"]
        is_mature_80 = _flags["is_mature_80"]
        tier         = _flags["tier"]

        # Preserve existing matured_*_at timestamps (self-heal otherwise)
        existing = requests.get(
            f"{SUPABASE_URL}/rest/v1/stock_rule_calibration"
            f"?rule_key=eq.{rk}&select=matured_at,matured_70_at,matured_80_at",
            headers=HEADERS_SB, timeout=10,
        )
        prev_matured = prev_matured_70 = prev_matured_80 = None
        if existing.status_code == 200 and existing.json():
            row = existing.json()[0]
            prev_matured    = row.get("matured_at")
            prev_matured_70 = row.get("matured_70_at")
            prev_matured_80 = row.get("matured_80_at")
        now_iso = datetime.now(timezone.utc).isoformat()
        if is_mature and not prev_matured:
            prev_matured = now_iso
        if is_mature_70 and not prev_matured_70:
            prev_matured_70 = now_iso
        if is_mature_80 and not prev_matured_80:
            prev_matured_80 = now_iso

        payload = {
            "rule_key":           rk,
            "n_observations":     n,
            "n_correct":          n_correct,
            "accuracy":           round(accuracy, 6),
            "mean_realized_pct":  round(mean, 6),
            "median_return_pct":  round(median, 6),
            "is_mature":          is_mature,
            "is_mature_70":       is_mature_70,
            "is_mature_80":       is_mature_80,
            "matured_at":         prev_matured,
            "matured_70_at":      prev_matured_70,
            "matured_80_at":      prev_matured_80,
            "tier":               tier,
            "avg_win_pct":        round(sum(wins)/len(wins), 6) if wins else None,
            "avg_loss_pct":       round(sum(losses)/len(losses), 6) if losses else None,
            "profit_factor":      round(profit_factor, 4) if profit_factor is not None else None,
            "target_hit_rate":    round(target_hit_rate, 4),
            "stop_hit_rate":      round(stop_hit_rate, 4),
            "mean_mfe_pct":       round(sum(mfe_vals)/len(mfe_vals), 6) if mfe_vals else None,
            "mean_mae_pct":       round(sum(mae_vals)/len(mae_vals), 6) if mae_vals else None,
            # H1 effective-n stats (the gate inputs) — persisted for readers.
            "effective_n":                 eff["effective_n"],
            "effective_n_correct":         eff["effective_n_correct"],
            "effective_accuracy":          round(eff["effective_accuracy"], 6),
            "effective_mean_realized_pct": round(eff["effective_mean_realized_pct"], 6),
            "effective_profit_factor":     (round(eff["effective_profit_factor"], 4)
                                            if eff["effective_profit_factor"] is not None else None),
            "last_updated":             now_iso,
            "last_payoff_recomputed_at": now_iso,
        }
        u = requests.post(
            f"{SUPABASE_URL}/rest/v1/stock_rule_calibration?on_conflict=rule_key",
            headers={**HEADERS_SB, "Prefer": "resolution=merge-duplicates,return=minimal"},
            json=[payload], timeout=20,
        )
        if u.status_code in (200, 201, 204):
            updated += 1
            mark = {"adult": "🎓", "young_adult": "📈", "teen": "📊", "child": "  "}.get(tier, "  ")
            print(f"  {mark} {rk:<55} tier={tier:<11s} n={n:>4} acc={accuracy:.3f}  pf={payload['profit_factor']}")
        else:
            print(f"  calibration upsert {rk} {u.status_code}: {u.text[:200]}", file=sys.stderr)
    return updated


# ============================================================
# Main
# ============================================================

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days", type=int, default=180, help="Lookback window (default 180)")
    ap.add_argument("--commit", action="store_true", help="Actually insert rows (default: dry-run)")
    ap.add_argument("--ticker", type=str, default="", help="Limit to one ticker (for testing)")
    args = ap.parse_args()

    if not args.commit:
        print("DRY RUN — no DB writes. Pass --commit to actually insert.\n")
    print(f"Backfill tag: {BACKFILL_TAG}")
    print(f"Window: {args.days} days\n")

    events = fetch_eligible_events(args.days)
    if args.ticker:
        events = [e for e in events if e["ticker"] == args.ticker]
        print(f"  filtered to ticker={args.ticker}: {len(events)} events")

    kinds = fetch_tradeable_kinds()
    print(f"  tradeable universe: {len(kinds)} tickers")
    events = [e for e in events if e["ticker"] in kinds]
    print(f"  events on tradeable tickers: {len(events)}")

    existing = fetch_existing_trades_for_events([e["id"] for e in events])

    # Group events by ticker so we only fetch each ticker's bars once
    by_ticker: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        # Skip if all 4 horizons already exist for this (event, direction)
        direction = derive_direction(e)
        if (e["id"], direction) in existing:
            continue
        by_ticker[e["ticker"]].append(e)

    print(f"  unique tickers to fetch: {len(by_ticker)}\n")

    rows_to_insert: list[dict] = []
    rule_keys_touched: set[str] = set()
    n_no_bars = n_no_entry = n_no_outcome = 0

    for i, (ticker, ticker_events) in enumerate(sorted(by_ticker.items()), 1):
        # Window covers earliest event - 2d through latest event + max(horizon) + 7d
        ev_dates = [datetime.fromisoformat(e["event_at"].replace("Z","+00:00")).date()
                    for e in ticker_events]
        bars_start = min(ev_dates) - timedelta(days=2)
        bars_end   = max(ev_dates) + timedelta(days=max(HORIZONS) + 7)
        bars = fetch_bars(ticker, bars_start, bars_end)
        if not bars:
            n_no_bars += len(ticker_events) * len(HORIZONS)
            continue

        for ev in ticker_events:
            ev_date = datetime.fromisoformat(ev["event_at"].replace("Z","+00:00")).date()
            entry_pair = next_session_close(bars, ev_date)
            if not entry_pair:
                n_no_entry += len(HORIZONS)
                continue
            entry_date, entry_price = entry_pair
            for h in HORIZONS:
                row = build_trade_row(ev, kinds.get(ticker, "stock"), h,
                                      bars, entry_date, entry_price)
                if row is None:
                    n_no_outcome += 1
                    continue
                rows_to_insert.append(row)
                rule_keys_touched.add(row["rule_key"])

        if i % 25 == 0:
            print(f"  …processed {i}/{len(by_ticker)} tickers, {len(rows_to_insert)} rows built")
        time.sleep(0.3)  # gentle on yfinance

    print(f"\nBuilt {len(rows_to_insert)} closed paper-trade rows across "
          f"{len(rule_keys_touched)} rule_keys")
    print(f"  skipped: no_bars={n_no_bars}  no_entry={n_no_entry}  no_outcome={n_no_outcome}")

    if not args.commit:
        # Sample preview
        print("\nSample (first 3 rows):")
        for r in rows_to_insert[:3]:
            print(f"  {r['ticker']} {r['rule_key']:<45} entry={r['entry_at'][:10]} "
                  f"exit={r['exit_at'][:10]} ret={r['realized_return']:+.4f} correct={r['correct']}")
        print("\nRe-run with --commit to insert.")
        return 0

    # Insert in chunks
    print(f"\nInserting {len(rows_to_insert)} rows…")
    inserted = 0
    for i in range(0, len(rows_to_insert), INSERT_CHUNK):
        batch = rows_to_insert[i:i+INSERT_CHUNK]
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/stock_event_paper_trades",
            headers={**HEADERS_SB, "Prefer": "resolution=ignore-duplicates,return=minimal"},
            json=batch, timeout=60,
        )
        if r.status_code in (200, 201, 204):
            inserted += len(batch)
        else:
            print(f"  chunk {i//INSERT_CHUNK} {r.status_code}: {r.text[:300]}", file=sys.stderr)
    print(f"  inserted {inserted}/{len(rows_to_insert)} rows")

    print(f"\nRecomputing calibration for {len(rule_keys_touched)} rule_keys…")
    updated = recompute_calibration(rule_keys_touched)
    print(f"  calibration rows updated: {updated}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
