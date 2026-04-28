"""
6-month historical backtester (filings-only).

Replays the last 180 days of stock_raw_filings through the SAME thesis-agent
scoring + cluster logic, simulates entries via yfinance next-day-open prices,
reconciles outcomes 1 trading day later, writes:

  - stock_signals (model_version='rubric-v1.0-backtest', status_v2='backtest')
  - stock_forecast_audit (per-signal realized return)
  - stock_agent_weights (per-day per-agent EMA evolution)
  - stock_backtest_runs (summary metrics)

Trigger: gh workflow run backtester.yml --repo nishantgupta83/stock_app
Manual only — never on cron.

HONEST CAVEATS embedded in the metrics output:
  - Survivorship bias (universe = today's S&P leaders)
  - Look-ahead controlled via next-day-open entry
  - Truth Social signals limited to last ~7 days (RSS history limit)
  - 0.05% slippage per side, no commissions
  - 6mo × ~31 symbols → small sample; calibration noisy
"""
from __future__ import annotations

import math
import os
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone, date

import pandas as pd
import requests
import yfinance as yf
# curl_cffi just needs to be importable — yfinance 0.2.55+ auto-uses it
# for browser impersonation, which bypasses Yahoo's GitHub-IP blocking.
import curl_cffi  # noqa: F401

# Reuse the live thesis-agent scoring so backtest and live cannot drift.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from thesis_agent import (   # type: ignore
    score_evidence, cluster_passes, action_for_score, source_agent_for,
    horizon_for, evidence_summary,
)

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

HEADERS_SB = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
    "Prefer":        "resolution=ignore-duplicates,return=minimal",
}

LOOKBACK_DAYS  = 180
SLIPPAGE_BPS   = 5     # 0.05% per side
EMA_ALPHA      = 0.10
MODEL_VERSION  = "rubric-v1.0-backtest"


# ============================================================
# Supabase helpers
# ============================================================

def sb_get(path: str, params: dict | None = None) -> list[dict]:
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{path}", headers=HEADERS_SB, params=params or {}, timeout=30)
    if r.status_code != 200:
        print(f"  SB {path} {r.status_code}: {r.text[:200]}", file=sys.stderr)
        return []
    return r.json()


def sb_post(path: str, payload: list | dict, return_repr: bool = False) -> list[dict] | None:
    headers = {**HEADERS_SB, "Prefer": "return=representation"} if return_repr else HEADERS_SB
    r = requests.post(f"{SUPABASE_URL}/rest/v1/{path}", headers=headers, json=payload, timeout=30)
    if r.status_code not in (200, 201, 204):
        print(f"  SB POST {path} {r.status_code}: {r.text[:200]}", file=sys.stderr)
        return None
    return r.json() if return_repr else []


def sb_patch(path: str, payload: dict) -> None:
    r = requests.patch(f"{SUPABASE_URL}/rest/v1/{path}", headers=HEADERS_SB, json=payload, timeout=30)
    if r.status_code not in (200, 204):
        print(f"  SB PATCH {path} {r.status_code}: {r.text[:200]}", file=sys.stderr)


# ============================================================
# Build synthetic events from historical raw_filings
# ============================================================

def fetch_filings_in_window(start: datetime, end: datetime) -> list[dict]:
    """Pull all relevant filings in the date range. Mimics what filing_agent would have written."""
    rows = sb_get("stock_raw_filings", {
        "filed_at": f"gte.{start.isoformat()}",
        "select":   "ticker,form_type,filed_at,accession_number",
        "order":    "filed_at.asc",
        "limit":    "10000",
    })
    # Manually filter by upper bound (PostgREST AND of two filters via repeated key works but is ugly)
    return [r for r in rows if r["filed_at"] < end.isoformat()]


def filings_to_events(filings: list[dict]) -> list[dict]:
    """Convert raw filings into the same shape as stock_normalized_events."""
    out = []
    for f in filings:
        ft = f["form_type"]
        # Map form_type → event_type (mirrors filing_agent.emit_normalized_events)
        if ft == "8-K":
            event_type = "8k_material_event"
            sev = 3
        elif ft in ("13D", "SC 13D"):
            event_type = "filing_13d"
            sev = 3
        elif ft in ("13G", "SC 13G"):
            event_type = "filing_13g"
            sev = 2
        elif ft == "13F-HR":
            event_type = "filing_13f-hr"
            sev = 3
        elif ft == "4":
            event_type = "filing_4"
            sev = 1
        elif ft in ("10-Q", "10-K"):
            event_type = f"filing_{ft.lower()}"
            sev = 1
        else:
            continue   # skip forms our live system doesn't score
        out.append({
            "id":            None,                      # not in DB; backtest synthetic
            "event_type":    event_type,
            "event_subtype": None,
            "ticker":        f["ticker"],
            "event_at":      f["filed_at"],
            "severity":      sev,
            "source_table":  "stock_raw_filings",
            "parser_confidence": 1.0,
            "payload":       {"accession_number": f["accession_number"], "form_type": ft},
        })
    return out


# ============================================================
# Price layer (yfinance — free, no API key)
# ============================================================

# Cache yfinance bars per ticker for the whole backtest window
_price_cache: dict[str, pd.DataFrame] = {}

def load_prices(tickers: list[str], start: datetime, end: datetime) -> None:
    """Per-ticker fetch. yfinance auto-uses curl_cffi browser impersonation
    when curl_cffi is installed, bypassing Yahoo's GitHub-IP blocking."""
    print(f"Fetching yfinance daily bars for {len(tickers)} tickers...")
    start_s = start.date().isoformat()
    end_s   = (end + timedelta(days=2)).date().isoformat()
    ok = 0
    for t in tickers:
        try:
            tk = yf.Ticker(t)
            sub = tk.history(start=start_s, end=end_s, auto_adjust=False, prepost=False)
            if sub is None or sub.empty:
                print(f"  {t}: empty", file=sys.stderr)
                continue
            _price_cache[t] = sub[["Open", "Close"]].dropna()
            ok += 1
        except Exception as e:  # noqa: BLE001
            print(f"  {t}: {type(e).__name__}: {e}", file=sys.stderr)
        time.sleep(0.3)
    print(f"  cached prices for {ok}/{len(tickers)} tickers")


def next_session_open(ticker: str, after: datetime) -> tuple[date, float] | None:
    """Find the first trading day strictly AFTER `after` (date), return (date, open)."""
    bars = _price_cache.get(ticker)
    if bars is None or bars.empty:
        return None
    target = after.date()
    for ts, row in bars.iterrows():
        d = ts.date() if hasattr(ts, "date") else ts
        if d > target:
            try:
                return d, float(row["Open"])
            except (KeyError, ValueError, TypeError):
                continue
    return None


def session_close_on(ticker: str, on: date) -> float | None:
    bars = _price_cache.get(ticker)
    if bars is None:
        return None
    for ts, row in bars.iterrows():
        d = ts.date() if hasattr(ts, "date") else ts
        if d == on:
            try:
                return float(row["Close"])
            except (KeyError, ValueError, TypeError):
                return None
    return None


# ============================================================
# Reconcile outcome (entry next session open → close at horizon)
# ============================================================

def realized_outcome(ticker: str, signal_time: datetime, horizon_days: int = 1) -> dict | None:
    entry = next_session_open(ticker, signal_time)
    if not entry:
        return None
    entry_date, entry_px = entry
    # Exit at close of (entry_date + horizon_days - 1) — for horizon=1 that's same day close
    exit_target = entry_date + timedelta(days=horizon_days - 1)
    exit_px = session_close_on(ticker, exit_target)
    if exit_px is None:
        # Try to find next available close within ±3 days
        bars = _price_cache.get(ticker)
        if bars is None:
            return None
        for offset in range(1, 4):
            exit_px = session_close_on(ticker, exit_target + timedelta(days=offset))
            if exit_px is not None:
                break
        if exit_px is None:
            return None
    raw_return = (exit_px - entry_px) / entry_px
    # Subtract slippage on both sides (entry buy, exit sell)
    net_return = raw_return - 2 * (SLIPPAGE_BPS / 10000)
    return {
        "entry_date":   entry_date.isoformat(),
        "entry_px":     entry_px,
        "exit_px":      exit_px,
        "raw_return":   raw_return,
        "net_return":   net_return,
        "correct":      net_return > 0,    # WATCH = directional bullish bias in v1
    }


# ============================================================
# Per-day replay
# ============================================================

def cluster_events_by_window(events: list[dict], window_min: int = 5) -> dict[tuple[str, str], list[dict]]:
    clusters: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for e in events:
        try:
            t = datetime.fromisoformat(e["event_at"].replace("Z", "+00:00"))
        except Exception:
            continue
        bucket = t.replace(second=0, microsecond=0)
        bucket = bucket.replace(minute=(bucket.minute // window_min) * window_min)
        clusters[(e["ticker"], bucket.isoformat())].append(e)
    return clusters


def replay_day(day_start: datetime, day_end: datetime, all_events: list[dict],
               agent_state: dict[str, dict]) -> list[dict]:
    """Replay one calendar day. Returns list of signals fired with realized outcomes."""
    # Take only events in this day's window
    day_events = [e for e in all_events
                  if day_start.isoformat() <= e["event_at"] < day_end.isoformat()]
    if not day_events:
        return []

    clusters = cluster_events_by_window(day_events)
    fired = []

    for (ticker, bucket_iso), ev_list in clusters.items():
        ok, _ = cluster_passes(ev_list)
        if not ok:
            continue
        score, breakdown = score_evidence(ev_list)
        action = action_for_score(score)
        if action != "WATCH":
            continue

        # Compute outcome
        try:
            sig_time = datetime.fromisoformat(bucket_iso.replace("Z", "+00:00"))
        except Exception:
            continue
        outcome = realized_outcome(ticker, sig_time, horizon_days=1)
        if outcome is None:
            continue

        agents_in_signal = sorted({source_agent_for(e) for e in ev_list})

        # Update agent_state EMA for each contributing agent
        for a in agents_in_signal:
            st = agent_state.setdefault(a, {"acc": 0.5, "n": 0})
            st["acc"] = EMA_ALPHA * (1.0 if outcome["correct"] else 0.0) + (1 - EMA_ALPHA) * st["acc"]
            st["n"]  += 1

        fired.append({
            "ticker":           ticker,
            "fired_at":         bucket_iso,
            "score":            round(score, 2),
            "action":           "WATCH",
            "evidence_summary": evidence_summary(ev_list),
            "agents":           agents_in_signal,
            "horizon_days":     1,
            "outcome":          outcome,
        })

    return fired


# ============================================================
# Persist & summarize
# ============================================================

def persist_signals(signals: list[dict]) -> None:
    """Bulk-insert signals + forecast_audit rows."""
    if not signals:
        return
    payload = [{
        "ticker":           s["ticker"],
        "fired_at":         s["fired_at"],
        "direction":        "WATCH",
        "confidence":       round(min(s["score"], 100) / 100, 4),
        "horizon_days":     s["horizon_days"],
        "thesis_summary":   s["evidence_summary"],
        "model_version":    MODEL_VERSION,
        "weight_at_time":   {"agents": s["agents"]},
        "status":           "open",
        "action":           s["action"],
        "score":            s["score"],
        "evidence_summary": s["evidence_summary"],
        "dedupe_key":       f"backtest_{s['ticker']}_{s['fired_at']}",
        "status_v2":        "backtest",
    } for s in signals]

    # Insert in batches of 500
    inserted_ids = []
    for i in range(0, len(payload), 500):
        chunk = payload[i:i+500]
        res = sb_post("stock_signals", chunk, return_repr=True)
        if res:
            inserted_ids.extend([r["id"] for r in res])
    print(f"  inserted {len(inserted_ids)} backtest signals")

    # forecast_audit
    audit = []
    for sig_id, s in zip(inserted_ids, signals):
        o = s["outcome"]
        audit.append({
            "signal_id":       sig_id,
            "horizon_days":    s["horizon_days"],
            "realized_return": round(o["net_return"], 6),
            "realized_at":     o["entry_date"] + "T20:00:00+00:00",   # ~end of US session
            "correct":         bool(o["correct"]),
        })
    for i in range(0, len(audit), 500):
        sb_post("stock_forecast_audit", audit[i:i+500])
    print(f"  inserted {len(audit)} forecast_audit rows")


def persist_agent_weights(agent_state_history: list[tuple[date, dict]]) -> None:
    """Write one row per (agent, date) showing the EMA evolution."""
    rows = []
    for d, state in agent_state_history:
        for agent, st in state.items():
            rows.append({
                "agent":        agent,
                "date":         d.isoformat(),
                "accuracy_ema": round(st["acc"], 4),
                "weight":       round(max(0.1, min(2.0, st["acc"] / 0.5)), 4),
                "n_signals":    st["n"],
            })
    if rows:
        # Use upsert via on_conflict (PK is agent+date)
        headers = {**HEADERS_SB, "Prefer": "resolution=merge-duplicates"}
        for i in range(0, len(rows), 500):
            requests.post(
                f"{SUPABASE_URL}/rest/v1/stock_agent_weights?on_conflict=agent,date",
                headers=headers, json=rows[i:i+500], timeout=30,
            )
        print(f"  upserted {len(rows)} agent_weights rows")


def compute_metrics(signals: list[dict]) -> dict:
    if not signals:
        return {"days": 0, "signals_total": 0}

    returns   = [s["outcome"]["net_return"] for s in signals]
    confs     = [s["score"] / 100 for s in signals]
    correct   = [s["outcome"]["correct"] for s in signals]

    # Direction accuracy = fraction with positive return
    dir_acc = sum(correct) / len(correct)

    # Brier score: ((p - actual)^2) averaged
    brier = sum((p - (1.0 if c else 0.0)) ** 2 for p, c in zip(confs, correct)) / len(confs)

    # Calibration: bucket by predicted prob, compute realized hit rate per bucket
    buckets = []
    edges = [0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
    for lo, hi in zip(edges[:-1], edges[1:]):
        in_b = [(p, c) for p, c in zip(confs, correct) if lo <= p < hi]
        if in_b:
            actual = sum(1 for _, c in in_b if c) / len(in_b)
            buckets.append({"bucket_lo": lo, "bucket_hi": hi, "n": len(in_b), "actual_rate": actual})
    cal_error = (
        sum(abs(b["actual_rate"] - (b["bucket_lo"] + b["bucket_hi"]) / 2) * b["n"] for b in buckets)
        / sum(b["n"] for b in buckets) if buckets else 0.0
    )

    # Precision @ top 5/day — per calendar day, take top-5 by score, fraction profitable
    by_day: dict[str, list[dict]] = defaultdict(list)
    for s in signals:
        by_day[s["fired_at"][:10]].append(s)
    p5_hits = 0
    p5_total = 0
    for day, sigs in by_day.items():
        top = sorted(sigs, key=lambda x: -x["score"])[:5]
        p5_hits += sum(1 for s in top if s["outcome"]["correct"])
        p5_total += len(top)
    precision_at_5 = p5_hits / p5_total if p5_total else 0.0

    # Equity curve: equal-weight basket, daily-rebalanced (sum of returns / n positions per day)
    daily_pnl = []
    for day in sorted(by_day):
        sigs = by_day[day]
        daily_pnl.append((day, sum(s["outcome"]["net_return"] for s in sigs) / len(sigs)))
    equity = [1.0]
    for _, r in daily_pnl:
        equity.append(equity[-1] * (1 + r))
    if len(daily_pnl) > 1:
        rets = [r for _, r in daily_pnl]
        mean = sum(rets) / len(rets)
        std = (sum((r - mean) ** 2 for r in rets) / len(rets)) ** 0.5
        sharpe = (mean / std) * math.sqrt(252) if std > 0 else 0.0
    else:
        sharpe = 0.0

    # Max drawdown from equity curve
    peak = equity[0]
    max_dd = 0.0
    for v in equity:
        peak = max(peak, v)
        dd = (peak - v) / peak
        max_dd = max(max_dd, dd)

    # Top winners / losers
    sorted_sigs = sorted(signals, key=lambda s: s["outcome"]["net_return"])
    top_losers = [{
        "fired_at": s["fired_at"], "ticker": s["ticker"], "score": s["score"],
        "evidence_summary": s["evidence_summary"], "realized_return": s["outcome"]["net_return"],
    } for s in sorted_sigs[:10]]
    top_winners = [{
        "fired_at": s["fired_at"], "ticker": s["ticker"], "score": s["score"],
        "evidence_summary": s["evidence_summary"], "realized_return": s["outcome"]["net_return"],
    } for s in reversed(sorted_sigs[-10:])]

    return {
        "days":              len(by_day),
        "signals_total":     len(signals),
        "dir_accuracy":      round(dir_acc, 4),
        "precision_at_5":    round(precision_at_5, 4),
        "brier":             round(brier, 4),
        "cal_error":         round(cal_error, 4),
        "sharpe":            round(sharpe, 3),
        "max_dd":            round(max_dd, 4),
        "calibration_buckets": buckets,
        "top_winners":       top_winners,
        "top_losers":        top_losers,
    }


# ============================================================
# Main
# ============================================================

def main() -> int:
    started = time.time()
    end   = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    start = end - timedelta(days=LOOKBACK_DAYS)

    # Register backtest run
    run = sb_post("stock_backtest_runs", {
        "model_version": MODEL_VERSION,
        "config": {"lookback_days": LOOKBACK_DAYS, "slippage_bps": SLIPPAGE_BPS,
                   "ema_alpha": EMA_ALPHA, "universe_size_hint": "current_watchlist"},
    }, return_repr=True)
    run_id = run[0]["id"] if run else None
    print(f"backtest run_id={run_id}, window {start.date()} → {end.date()}")

    # Pull universe (tickers only — for yfinance batch)
    syms = sb_get("stock_watchlists", {
        "name":  "in.(\"core\",\"institutions\",\"mutual_funds\")",
        "select": "ticker,stock_symbols(kind)",
    })
    # Only fetch yfinance prices for actual tradable tickers (stock + etf — not institutions/funds)
    tradable = sorted({s["ticker"] for s in syms
                       if s.get("stock_symbols") and s["stock_symbols"]["kind"] in ("stock", "etf")})
    print(f"Universe: {len(tradable)} tradable tickers")

    load_prices(tradable, start, end)
    if not _price_cache:
        print("FATAL: no prices loaded, aborting", file=sys.stderr)
        if run_id:
            sb_patch(f"stock_backtest_runs?id=eq.{run_id}",
                     {"finished_at": datetime.now(timezone.utc).isoformat(),
                      "metrics": {"error": "no_prices"}})
        return 1

    # Pull all relevant filings once
    filings = fetch_filings_in_window(start, end)
    print(f"Pulled {len(filings)} filings in window")
    events = filings_to_events(filings)
    # Filter to tradable universe only
    events = [e for e in events if e["ticker"] in _price_cache]
    print(f"After universe filter: {len(events)} events")

    # Replay day by day
    agent_state: dict[str, dict] = {}
    agent_state_history: list[tuple[date, dict]] = []
    all_signals = []

    cur = start
    while cur < end:
        day_end = cur + timedelta(days=1)
        fired = replay_day(cur, day_end, events, agent_state)
        if fired:
            all_signals.extend(fired)
            print(f"  {cur.date()}: {len(fired)} signals")
        # Snapshot per-day state for the agent_weights table (deep copy)
        snapshot = {a: {"acc": st["acc"], "n": st["n"]} for a, st in agent_state.items()}
        if snapshot:
            agent_state_history.append((cur.date(), snapshot))
        cur = day_end

    print(f"Total signals fired: {len(all_signals)}")

    # Persist
    persist_signals(all_signals)
    persist_agent_weights(agent_state_history)

    metrics = compute_metrics(all_signals)
    elapsed = time.time() - started
    print(f"Backtest done in {elapsed:.1f}s")
    print(f"Metrics: {metrics}")

    if run_id:
        sb_patch(f"stock_backtest_runs?id=eq.{run_id}", {
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "metrics":     metrics,
        })

    return 0


if __name__ == "__main__":
    sys.exit(main())
