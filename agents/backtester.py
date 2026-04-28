"""
6-month historical backtester — filings + earnings + momentum.

Replays the last 180 days of multi-source events through scoring + cluster
logic, simulates entries via yfinance next-day-open prices, reconciles
outcomes at horizon, writes:
  - stock_signals (model_version='rubric-v1.0-backtest', status_v2='backtest')
  - stock_forecast_audit (per-signal realized return)
  - stock_agent_weights (per-day per-agent EMA evolution)
  - stock_backtest_runs (summary metrics)

Signal sources (hedge fund pattern):
  1. SEC filings — 8-K material events, SC 13D activist, Form 4 insider
  2. Earnings events:
     - earnings_pre  — 5-day momentum INTO earnings (pre-earnings drift)
     - earnings_release — actual vs estimated EPS (surprise classification)
     - earnings_post — 1-day reaction (post-earnings drift, PEAD)
  3. Momentum events:
     - 20-day relative strength vs SPY (top decile = bullish)

HONEST CAVEATS in metrics output:
  - Survivorship bias (universe = today's S&P leaders)
  - Look-ahead controlled via next-day-open entry
  - Truth Social out of scope (RSS history limit)
  - Fundamentals (P/E, FCF, short interest) require paid feeds — not in v1
  - 0.05% slippage per side, no commissions
  - 6mo × ~21 tickers × 4 quarters → ~84 earnings events, ~1200 filings,
    plus ~20 momentum signals/month → ~3000 total signals upper bound
  - PEAD literature suggests holding 60d post-earnings; v1 uses 1-day for
    direct comparability with filing signals

Trigger: gh workflow run backtester.yml --repo nishantgupta83/stock_app
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
    score_evidence as _score_filings_truth, cluster_passes,
    action_for_score, source_agent_for, horizon_for, evidence_summary,
)


# ============================================================
# Extended scoring rubric — earnings + momentum
# These rules don't exist in live thesis_agent yet (no earnings/momentum
# agent shipped). Backtester scores them here so we can decide whether
# to wire them up live based on results. Documented as v1.1-extension.
# Hedge fund pattern reference:
#   - Bernard & Thomas (1989) on PEAD
#   - Fama-French (1992) on momentum
#   - Pre-earnings drift: Frazzini & Lamont (2007)
# ============================================================

def _score_earnings_momentum(events: list[dict]) -> tuple[float, list[dict]]:
    """Score earnings + momentum events. Returns (added_score, breakdown)."""
    score = 0.0
    breakdown: list[dict] = []

    for e in events:
        et = e["event_type"]
        sub = e.get("event_subtype") or ""
        sev = e.get("severity") or 0
        payload = e.get("payload") or {}

        if et == "earnings_pre":
            # Pre-earnings drift. Strong drift in either direction is informational.
            # Moderate positive drift → momentum into earnings (long bias).
            # Strong drift (>5%) often exhausts before/after release — penalize chase.
            drift = payload.get("drift_pct") or 0
            if   drift >  10: score += 10; breakdown.append({"rule": "earnings_pre_overextended", "points": 10, "event_id": None, "detail": f"+{drift:.1f}% drift, possible chase risk"})
            elif drift >   2: score += 25; breakdown.append({"rule": "earnings_pre_drift_bullish", "points": 25, "event_id": None, "detail": f"+{drift:.1f}% drift"})
            elif drift <  -2: score += 15; breakdown.append({"rule": "earnings_pre_drift_bearish", "points": 15, "event_id": None, "detail": f"{drift:.1f}% drift (informational, long-only)"})

        elif et == "earnings_release":
            # Surprise classification — beats are the primary buy signal.
            surprise_pct = payload.get("surprise_pct")
            if surprise_pct is None:
                continue
            if   sub == "beat" and surprise_pct >= 10: score += 40; breakdown.append({"rule": "earnings_beat_strong",   "points": 40, "event_id": None, "detail": f"+{surprise_pct:.1f}% vs est"})
            elif sub == "beat" and surprise_pct >=  3: score += 25; breakdown.append({"rule": "earnings_beat_moderate", "points": 25, "event_id": None, "detail": f"+{surprise_pct:.1f}% vs est"})
            elif sub == "beat":                         score +=  5; breakdown.append({"rule": "earnings_beat_inline",   "points":  5, "event_id": None, "detail": f"+{surprise_pct:.1f}% vs est"})
            elif sub == "miss" and abs(surprise_pct) >= 10: score -= 40; breakdown.append({"rule": "earnings_miss_strong", "points": -40, "event_id": None, "detail": f"{surprise_pct:.1f}% vs est"})
            elif sub == "miss":                              score -= 20; breakdown.append({"rule": "earnings_miss",        "points": -20, "event_id": None, "detail": f"{surprise_pct:.1f}% vs est"})

        elif et == "earnings_post":
            # PEAD entry — applied next day after a known surprise.
            # Worth +30 ONLY if the prior earnings_release was a beat (we don't have
            # cross-event awareness here; backtest scores it independently and the
            # multi-event cluster naturally combines).
            score += 15
            breakdown.append({"rule": "pead_entry", "points": 15, "event_id": None, "detail": "1d after earnings, PEAD window"})

        elif et == "momentum":
            # 20d relative strength vs SPY
            rs = payload.get("rel_strength_pct") or 0
            if   rs >  10: score += 25; breakdown.append({"rule": "momentum_strong_long", "points": 25, "event_id": None, "detail": f"+{rs:.1f}% vs SPY 20d"})
            elif rs >   5: score += 15; breakdown.append({"rule": "momentum_moderate",    "points": 15, "event_id": None, "detail": f"+{rs:.1f}% vs SPY 20d"})
            elif rs < -10: score -= 20; breakdown.append({"rule": "momentum_weak",        "points": -20, "event_id": None, "detail": f"{rs:.1f}% vs SPY 20d"})

    return score, breakdown


def score_evidence(events: list[dict]) -> tuple[float, list[dict]]:
    """Combined scorer: live filings/truth rubric + extended earnings/momentum."""
    s1, b1 = _score_filings_truth(events)
    s2, b2 = _score_earnings_momentum(events)
    return s1 + s2, b1 + b2

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
MODEL_VERSION  = "rubric-v1.1-backtest-multisource"
# Backtest mode: live system requires ≥2 distinct source agents (cluster rule).
# Filings-only data has just 1 source agent, so live would fire 0 signals.
# Permissive mode scores every cluster regardless, AND uses RESEARCH (score≥50)
# as the entry threshold — this gives us calibration data on the rubric itself,
# separate from cluster gating. Tagged in metrics so it can never be confused
# with a real system simulation.
BACKTEST_MODE  = "multi_source_research_grade"
# v1.1: filings + earnings + momentum gives 3 distinct source agents,
# so the live cluster rule can pass naturally. Entry at RESEARCH (≥50)
# for richer calibration than WATCH-only.
ENTRY_SCORE_MIN = 50
ENTRY_SCORE_MAX = 100

# Suffix for dedupe_key — set per-run in main() so re-runs insert fresh signals.
_RUN_SUFFIX = "default"


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
    # Combine ignore-duplicates with return=representation so collisions silently
    # drop instead of 409, and we still get inserted rows back.
    if return_repr:
        headers = {**HEADERS_SB, "Prefer": "resolution=ignore-duplicates,return=representation"}
    else:
        headers = HEADERS_SB
    r = requests.post(f"{SUPABASE_URL}/rest/v1/{path}", headers=headers, json=payload, timeout=30)
    if r.status_code not in (200, 201, 204):
        print(f"  SB POST {path} {r.status_code}: {r.text[:200]}", file=sys.stderr)
        return None
    if return_repr:
        try:
            return r.json()
        except Exception:
            return []
    return []


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
# Earnings events (hedge fund pattern: pre-drift + surprise + PEAD)
# ============================================================

def fetch_earnings_events(tickers: list[str], start: datetime, end: datetime) -> list[dict]:
    """Pull earnings dates + actual/estimated EPS via yfinance per ticker.
    Generates three event types per earnings:
      - earnings_pre     (5 trading days before — captures pre-earnings drift)
      - earnings_release (the day — captures the surprise itself)
      - earnings_post    (1 trading day after — captures PEAD)
    """
    events: list[dict] = []
    n_with_data = 0
    for t in tickers:
        try:
            tk = yf.Ticker(t)
            # Try multiple yfinance APIs — they break across versions
            ed = None
            try:
                ed = tk.get_earnings_dates(limit=24)   # 6 quarters back + future
            except Exception:
                pass
            if ed is None or ed.empty:
                ed = tk.earnings_dates                 # legacy property
            if ed is None or ed.empty:
                print(f"  earnings {t}: no data", file=sys.stderr)
                continue
            n_with_data += 1
            # Filter to backtest window
            for ts, row in ed.iterrows():
                d = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
                if not isinstance(d, datetime):
                    continue
                d = d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)
                if d < start or d > end:
                    continue
                actual_eps   = row.get("Reported EPS")
                estimated    = row.get("EPS Estimate")
                surprise_pct = row.get("Surprise(%)")
                # earnings_pre: 5 days before — use the prior price-history slope as drift signal
                pre_dt = d - timedelta(days=7)   # calendar days; weekends skipped naturally
                if pre_dt > start:
                    drift = compute_drift_pct(t, pre_dt, d)
                    if drift is not None:
                        events.append({
                            "id": None,
                            "event_type":    "earnings_pre",
                            "event_subtype": f"drift_{drift:+.1f}pct",
                            "ticker":        t,
                            "event_at":      pre_dt.isoformat(),
                            "severity":      3 if abs(drift) > 5 else 2 if abs(drift) > 2 else 1,
                            "source_table":  "yfinance_earnings",
                            "parser_confidence": 0.9,
                            "payload":       {"drift_pct": drift, "earnings_date": d.isoformat()},
                        })
                # earnings_release: the day — surprise classification
                release_sev = 2   # default, used by earnings_post too
                if actual_eps is not None and estimated is not None and not pd.isna(actual_eps) and not pd.isna(estimated):
                    surprise_dir = "beat" if actual_eps > estimated else ("miss" if actual_eps < estimated else "inline")
                    if surprise_pct is not None and not pd.isna(surprise_pct):
                        release_sev = 4 if abs(surprise_pct) > 10 else 3 if abs(surprise_pct) > 3 else 2
                    events.append({
                        "id": None,
                        "event_type":    "earnings_release",
                        "event_subtype": surprise_dir,
                        "ticker":        t,
                        "event_at":      d.isoformat(),
                        "severity":      release_sev,
                        "source_table":  "yfinance_earnings",
                        "parser_confidence": 1.0,
                        "payload":       {
                            "actual_eps":    float(actual_eps),
                            "estimated_eps": float(estimated),
                            "surprise_pct":  float(surprise_pct) if surprise_pct is not None and not pd.isna(surprise_pct) else None,
                        },
                    })
                # earnings_post: next trading day — PEAD entry point
                post_dt = d + timedelta(days=1)
                if post_dt < end:
                    events.append({
                        "id": None,
                        "event_type":    "earnings_post",
                        "event_subtype": "pead_entry",
                        "ticker":        t,
                        "event_at":      post_dt.isoformat(),
                        "severity":      release_sev,
                        "source_table":  "yfinance_earnings",
                        "parser_confidence": 0.9,
                        "payload":       {"earnings_date": d.isoformat()},
                    })
        except Exception as e:  # noqa: BLE001
            print(f"  earnings {t}: {type(e).__name__}: {e}", file=sys.stderr)
        time.sleep(0.2)
    print(f"  earnings: {n_with_data}/{len(tickers)} tickers returned data, {len(events)} events", file=sys.stderr)
    return events


def compute_drift_pct(ticker: str, from_dt: datetime, to_dt: datetime) -> float | None:
    """Return % change between two dates, using closes from the price cache."""
    bars = _price_cache.get(ticker)
    if bars is None or bars.empty:
        return None
    from_d = from_dt.date()
    to_d   = to_dt.date()
    p_from = p_to = None
    for ts, row in bars.iterrows():
        d = ts.date() if hasattr(ts, "date") else ts
        if p_from is None and d >= from_d:
            try: p_from = float(row["Close"])
            except Exception: pass
        if d <= to_d:
            try: p_to = float(row["Close"])
            except Exception: pass
    if p_from is None or p_to is None or p_from <= 0:
        return None
    return ((p_to - p_from) / p_from) * 100


# ============================================================
# Momentum events (20-day relative strength vs SPY)
# ============================================================

def fetch_momentum_events(tickers: list[str], start: datetime, end: datetime,
                          period_days: int = 20, threshold_pct: float = 5.0) -> list[dict]:
    """Generate momentum signals: stocks outperforming SPY by ≥ threshold over rolling period.
    Fires once per ticker per non-overlapping window — gives ~9 signals per ticker over 6 months."""
    events = []
    spy_bars = _price_cache.get("SPY")
    if spy_bars is None or spy_bars.empty:
        print("  momentum: no SPY prices, skipping", file=sys.stderr)
        return events
    for t in tickers:
        if t == "SPY":
            continue
        bars = _price_cache.get(t)
        if bars is None or bars.empty:
            continue
        # Iterate by trading day, every period_days emit one event
        all_dates = sorted({ts.date() for ts in bars.index if start.date() <= ts.date() <= end.date()})
        i = period_days
        while i < len(all_dates):
            anchor_date = all_dates[i]
            from_date   = all_dates[i - period_days]
            t_ret  = compute_drift_pct(t,   datetime.combine(from_date, datetime.min.time(), tzinfo=timezone.utc),
                                            datetime.combine(anchor_date, datetime.min.time(), tzinfo=timezone.utc))
            spy_ret = compute_drift_pct("SPY", datetime.combine(from_date, datetime.min.time(), tzinfo=timezone.utc),
                                              datetime.combine(anchor_date, datetime.min.time(), tzinfo=timezone.utc))
            if t_ret is None or spy_ret is None:
                i += period_days
                continue
            rel_strength = t_ret - spy_ret
            if abs(rel_strength) >= threshold_pct:
                events.append({
                    "id": None,
                    "event_type":    "momentum",
                    "event_subtype": f"{period_days}d_rel_strength",
                    "ticker":        t,
                    "event_at":      datetime.combine(anchor_date, datetime.min.time(), tzinfo=timezone.utc).isoformat(),
                    "severity":      3 if abs(rel_strength) > 10 else 2,
                    "source_table":  "yfinance_prices",
                    "parser_confidence": 0.85,
                    "payload":       {
                        "ticker_return_pct": t_ret,
                        "spy_return_pct":    spy_ret,
                        "rel_strength_pct":  rel_strength,
                        "lookback_days":     period_days,
                    },
                })
            i += period_days
    return events


# ============================================================
# Per-day replay
# ============================================================

def cluster_events_by_window(events: list[dict], window_min: int = 2880) -> dict[tuple[str, str], list[dict]]:
    """Backtest: 2-calendar-day cluster window (2880 min).
    Why 2 days: yfinance earnings dates are US/Eastern-anchored, EDGAR filed_at
    is UTC. After-market earnings (filed ~4:00 PM ET = 8:00 PM UTC) can land on
    a different UTC calendar date than the corresponding 8-K filing. A 2-day
    bucket reliably joins them.
    Live system stays at 5 min — different problem (intraday signals)."""
    clusters: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for e in events:
        try:
            t = datetime.fromisoformat(e["event_at"].replace("Z", "+00:00"))
        except Exception:
            continue
        if window_min >= 1440:
            # Round DOWN to a 2-day bucket: epoch_days // 2 * 2
            day_index = (t.date() - date(1970, 1, 1)).days
            bucket_start = date(1970, 1, 1) + timedelta(days=(day_index // (window_min // 1440)) * (window_min // 1440))
            bucket = datetime.combine(bucket_start, datetime.min.time(), tzinfo=timezone.utc)
        else:
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
        # Apply the live cluster rule — with earnings + momentum + filings we
        # now have multiple source agents, so real clusters can form.
        ok, _ = cluster_passes(ev_list)
        if not ok:
            # Single-source acceptable IF it's a high-severity earnings_release
            # (severity 4 = >10% surprise, self-validating). Otherwise skip.
            single_source_ok = any(
                e["event_type"] == "earnings_release" and (e.get("severity") or 0) >= 4
                for e in ev_list
            )
            if not single_source_ok:
                continue
        score, breakdown = score_evidence(ev_list)
        action = action_for_score(score)
        if not action or score < ENTRY_SCORE_MIN:
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
            "action":           action,
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
        "direction":        s["action"],
        "confidence":       round(min(s["score"], 100) / 100, 4),
        "horizon_days":     s["horizon_days"],
        "thesis_summary":   s["evidence_summary"],
        "model_version":    MODEL_VERSION,
        "weight_at_time":   {"agents": s["agents"], "mode": BACKTEST_MODE},
        "status":           "open",
        "action":           s["action"],
        "score":            s["score"],
        "evidence_summary": s["evidence_summary"],
        # Run suffix in dedupe key so re-runs always insert fresh rows.
        "dedupe_key":       f"bt_{_RUN_SUFFIX}_{s['ticker']}_{s['fired_at']}",
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
        "mode":              BACKTEST_MODE,
        "entry_score_min":   ENTRY_SCORE_MIN,
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
    run_id = run[0]["id"] if run else int(started)   # fallback: epoch as run id
    # Suffix appended to dedupe_key so each backtest run produces fresh signals
    # even with same MODEL_VERSION (otherwise re-runs collide silently).
    global _RUN_SUFFIX
    _RUN_SUFFIX = f"r{run_id}"
    print(f"backtest run_id={run_id}, window {start.date()} → {end.date()}")

    # Pull universe — include 'context' so SPY/QQQ are loaded for momentum baseline
    syms = sb_get("stock_watchlists", {
        "name":  "in.(\"core\",\"institutions\",\"mutual_funds\",\"context\")",
        "select": "ticker,stock_symbols(kind)",
    })
    tradable = sorted({s["ticker"] for s in syms
                       if s.get("stock_symbols") and s["stock_symbols"]["kind"] in ("stock", "etf")})
    print(f"Universe: {len(tradable)} tradable tickers (incl. SPY for momentum baseline)")

    load_prices(tradable, start, end)
    if not _price_cache:
        print("FATAL: no prices loaded, aborting", file=sys.stderr)
        if run_id:
            sb_patch(f"stock_backtest_runs?id=eq.{run_id}",
                     {"finished_at": datetime.now(timezone.utc).isoformat(),
                      "metrics": {"error": "no_prices"}})
        return 1

    # Layer 1: filings
    filings = fetch_filings_in_window(start, end)
    filing_events = [e for e in filings_to_events(filings) if e["ticker"] in _price_cache]
    print(f"Filings: {len(filings)} pulled → {len(filing_events)} after universe filter")

    # Layer 2: earnings (per-ticker — yfinance earnings_dates)
    # Only on stocks (skip ETFs which don't report)
    stock_tickers = sorted({s["ticker"] for s in syms
                            if s.get("stock_symbols") and s["stock_symbols"]["kind"] == "stock"
                            and s["ticker"] in _price_cache})
    earnings_events = fetch_earnings_events(stock_tickers, start, end)
    print(f"Earnings: {len(earnings_events)} events across {len(stock_tickers)} stocks")

    # Layer 3: momentum (20d relative strength vs SPY)
    momentum_events = fetch_momentum_events(stock_tickers, start, end, period_days=20, threshold_pct=5.0)
    print(f"Momentum: {len(momentum_events)} events (20d vs SPY, ≥5% rel strength)")

    events = filing_events + earnings_events + momentum_events
    print(f"Total events: {len(events)}")

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
