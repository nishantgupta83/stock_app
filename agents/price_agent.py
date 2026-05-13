"""
Price agent — EOD learning loop (Phase 5).

Runs every weekday at 21:30 UTC (4:30 PM ET, after US market close).

Pipeline:
  1. Fetch live signals (status_v2 IN candidate/sent/suppressed) whose horizon has expired.
  2. Fetch entry price (next session open after fired_at) and exit price (horizon close).
  3. Compute realized return and correctness (direction-aware).
  4. Write stock_forecast_audit row.
  5. Close any stock_paper_forecasts rows tied to that signal.
  6. Update stock_agent_weights EMA for each contributing agent.
  7. Mark signal status_v2 → 'closed'.
  8. Send Telegram EOD digest.

This closes the prediction→outcome loop so agent weights self-correct over time.
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timedelta, timezone
import requests
import yfinance as yf

try:
    from curl_cffi import requests as cffi_requests
    _CF_SESSION = cffi_requests.Session(impersonate="chrome")
except ImportError:
    _CF_SESSION = None

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
BOT_TOKEN    = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID      = os.environ.get("TELEGRAM_CHAT_ID", "")

HEADERS_SB = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
    "Prefer":        "resolution=ignore-duplicates,return=minimal",
}

EMA_ALPHA = 0.1   # same as backtester — consistent learning rate across live + replay
SLIPPAGE_BPS = 5  # same as backtester: 0.05% per side, no commissions
ARCHIVE_INDEX_URL = "https://hub4apps.com/stock_app/archive/index.json"


# ============================================================
# Supabase helpers
# ============================================================

def sb_get(path: str, params: dict | None = None) -> list[dict]:
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/{path}",
        headers=HEADERS_SB, params=params or {}, timeout=20,
    )
    if r.status_code != 200:
        print(f"  SB GET {path} {r.status_code}: {r.text[:200]}", file=sys.stderr)
        return []
    return r.json()


def sb_post(path: str, rows: list[dict], prefer: str = "resolution=ignore-duplicates,return=minimal",
            on_conflict: str | None = None) -> bool:
    if not rows:
        return True
    hdrs = {**HEADERS_SB, "Prefer": prefer}
    suffix = f"?on_conflict={on_conflict}" if on_conflict else ""
    r = requests.post(f"{SUPABASE_URL}/rest/v1/{path}{suffix}", headers=hdrs, json=rows, timeout=20)
    if r.status_code not in (200, 201, 204):
        print(f"  SB POST {path} {r.status_code}: {r.text[:300]}", file=sys.stderr)
        return False
    return True


def sb_patch(path: str, payload: dict) -> bool:
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/{path}",
        headers=HEADERS_SB, json=payload, timeout=10,
    )
    if r.status_code not in (200, 201, 204):
        print(f"  SB PATCH {path} {r.status_code}: {r.text[:200]}", file=sys.stderr)
        return False
    return True


def sb_upsert(path: str, rows: list[dict], on_conflict: str) -> bool:
    if not rows:
        return True
    hdrs = {**HEADERS_SB, "Prefer": "resolution=merge-duplicates"}
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/{path}?on_conflict={on_conflict}",
        headers=hdrs, json=rows, timeout=20,
    )
    if r.status_code not in (200, 201, 204):
        print(f"  SB UPSERT {path} {r.status_code}: {r.text[:300]}", file=sys.stderr)
        return False
    return True


# ============================================================
# Signal fetching
# ============================================================

def fetch_mature_signals() -> list[dict]:
    """Return live signals whose horizon_days have fully elapsed (entry+horizon <= yesterday)."""
    rows = sb_get("stock_signals", {
        "status_v2": "in.(candidate,sent,suppressed)",
        "select":    "id,ticker,fired_at,action,direction,horizon_days,score,weight_at_time",
        "order":     "fired_at.asc",
        "limit":     "500",
    })
    yesterday = datetime.now(timezone.utc).date() - timedelta(days=1)
    mature = []
    for s in rows:
        try:
            fired_date = datetime.fromisoformat(s["fired_at"].replace("Z", "+00:00")).date()
            exit_date  = fired_date + timedelta(days=int(s.get("horizon_days") or 1))
            if exit_date <= yesterday:
                s["_fired_date"] = fired_date
                s["_exit_date"]  = exit_date
                mature.append(s)
        except Exception:
            continue
    return mature


def existing_audits(signal_ids: list[int]) -> dict[int, dict]:
    """Return existing forecast audits by signal_id for idempotent healing."""
    if not signal_ids:
        return {}
    in_list = ",".join(str(i) for i in signal_ids)
    rows = sb_get("stock_forecast_audit", {
        "signal_id": f"in.({in_list})",
        "select":    "signal_id,horizon_days,realized_return,realized_at,correct,entry_price,exit_price,entry_at,exit_at",
    })
    return {int(r["signal_id"]): r for r in rows if r.get("signal_id") is not None}


# ============================================================
# Price fetching
# ============================================================

def _yf_ticker(sym: str) -> yf.Ticker:
    return yf.Ticker(sym, session=_CF_SESSION) if _CF_SESSION else yf.Ticker(sym)


def fetch_bars(ticker: str, start: date, end: date) -> dict[date, dict[str, float]]:
    """Return adjusted daily OHLC bars between start and end+7 days.

    H/L are included so reconciliation can compute MFE/MAE and approximate
    stop/target hit detection without fetching intraday data.
    """
    try:
        t = _yf_ticker(ticker)
        df = t.history(
            start=start.isoformat(),
            end=(end + timedelta(days=7)).isoformat(),
            auto_adjust=True,
        )
        if df.empty:
            return {}
        result: dict[date, dict[str, float]] = {}
        for ts, row in df.iterrows():
            d = ts.date() if hasattr(ts, "date") else ts.to_pydatetime().date()
            result[d] = {
                "open":  float(row["Open"]),
                "high":  float(row["High"]),
                "low":   float(row["Low"]),
                "close": float(row["Close"]),
            }
        return result
    except Exception as e:
        print(f"  {ticker}: price fetch error — {e}", file=sys.stderr)
        return {}


def next_session_open(bars: dict[date, dict[str, float]], after: date) -> tuple[date, float] | None:
    """Return the first trading session open after the signal fire date."""
    for d in sorted(bars):
        if d > after and bars[d].get("open"):
            return d, bars[d]["open"]
    return None


def close_on_or_after(bars: dict[date, dict[str, float]], target: date) -> tuple[date, float] | None:
    """Return the close on target date, or the next available trading day."""
    for d in sorted(bars):
        if d >= target:
            close = bars[d].get("close")
            if close:
                return d, close
    return None


# ============================================================
# Outcome computation
# ============================================================

def compute_outcome(signal: dict, bars: dict[date, dict[str, float]]) -> dict | None:
    """
    Returns {entry_price, exit_price, net_return, correct} or None if prices unavailable.
    correct is direction-aware:
      - AVOID_CHASE is bearish and correct when price falls.
      - CHASE_RISK warns against chasing upside and is correct when no further
        positive follow-through occurs over the audited horizon.
    """
    entry = next_session_open(bars, signal["_fired_date"])
    if entry is None:
        return None
    entry_date, entry_price = entry
    exit_target = entry_date + timedelta(days=int(signal.get("horizon_days") or 1) - 1)
    exit_ = close_on_or_after(bars, exit_target)
    if exit_ is None or entry_price == 0:
        return None
    exit_date, exit_price = exit_
    raw_return = (exit_price - entry_price) / entry_price
    net_return = raw_return - 2 * (SLIPPAGE_BPS / 10000)
    action = signal.get("action") or "RESEARCH"
    # Bearish signals (AVOID_CHASE) are correct when price drops.
    # CHASE_RISK is a caution label: correct if the post-alert move is flat/down.
    if action == "AVOID_CHASE":
        correct = net_return < 0
    elif action == "CHASE_RISK":
        correct = net_return <= 0
    else:
        correct = net_return > 0
    return {
        "entry_price": round(entry_price, 4),
        "exit_price":  round(exit_price, 4),
        "entry_at":     entry_date.isoformat() + "T14:30:00+00:00",
        "exit_at":      exit_date.isoformat() + "T20:00:00+00:00",
        "net_return":  round(net_return, 6),
        "correct":     correct,
    }


# ============================================================
# Learning loop writes
# ============================================================

def write_forecast_audit(signal_id: int, signal: dict, outcome: dict) -> None:
    sb_post("stock_forecast_audit", [{
        "signal_id":       signal_id,
        "horizon_days":    int(signal.get("horizon_days") or 1),
        "realized_return": outcome["net_return"],
        "realized_at":     outcome["exit_at"],
        "correct":         outcome["correct"],
        "entry_price":     outcome["entry_price"],
        "exit_price":      outcome["exit_price"],
        "entry_at":        outcome["entry_at"],
        "exit_at":         outcome["exit_at"],
        "outcome_method":  "next_session_open_to_horizon_close",
    }], prefer="resolution=merge-duplicates,return=minimal", on_conflict="signal_id,horizon_days")


def close_paper_forecasts(signal_id: int, signal: dict, outcome: dict) -> None:
    """Close Phase 6A paper forecasts tied to this signal.

    `stock_paper_forecasts` may not exist until sql/0008 is applied. A missing
    table is tolerated so the existing EOD learning loop keeps running.
    """
    rows = sb_get("stock_paper_forecasts", {
        "signal_id": f"eq.{signal_id}",
        "status":    "eq.open",
        "select":    "id,paper_action",
    })
    if not rows:
        return

    realized = float(outcome["net_return"])
    for row in rows:
        action = row.get("paper_action") or "PAPER_WATCH"
        if action == "PAPER_LONG":
            correct = realized > 0
        elif action == "PAPER_SHORT":
            correct = realized < 0
        elif action in ("PAPER_AVOID", "PAPER_CHASE_RISK"):
            correct = realized <= 0
        else:
            correct = None

        patch = {
            "status":          "closed",
            "realized_return": outcome["net_return"],
            "realized_at":     outcome.get("exit_at") or outcome.get("realized_at"),
            "correct":         correct,
            "updated_at":      datetime.now(timezone.utc).isoformat(),
        }
        if outcome.get("entry_price") is not None:
            patch["entry_price"] = outcome["entry_price"]
        if outcome.get("exit_price") is not None:
            patch["exit_price"] = outcome["exit_price"]
        sb_patch(f"stock_paper_forecasts?id=eq.{row['id']}", patch)


def outcome_from_audit(audit: dict) -> dict | None:
    """Build a patchable paper-forecast outcome from an existing audit row."""
    if audit.get("realized_return") is None:
        return None
    try:
        realized = float(audit["realized_return"])
    except (TypeError, ValueError):
        return None
    return {
        "entry_price": float(audit["entry_price"]) if audit.get("entry_price") is not None else None,
        "exit_price": float(audit["exit_price"]) if audit.get("exit_price") is not None else None,
        "entry_at": audit.get("entry_at"),
        "exit_at": audit.get("exit_at") or audit.get("realized_at"),
        "realized_at": audit.get("realized_at"),
        "net_return": round(realized, 6),
        "correct": bool(audit.get("correct")) if audit.get("correct") is not None else None,
    }


def update_agent_weights(agents: list[str], correct: bool) -> None:
    """Fetch latest EMA for each agent and apply one EMA step."""
    if not agents:
        return
    today = datetime.now(timezone.utc).date().isoformat()
    rows = []
    for agent in agents:
        latest = sb_get("stock_agent_weights", {
            "agent":  f"eq.{agent}",
            "select": "accuracy_ema,n_signals",
            "order":  "date.desc",
            "limit":  "1",
        })
        acc = float(latest[0]["accuracy_ema"]) if latest else 0.5
        n   = int(latest[0]["n_signals"]) + 1  if latest else 1
        new_acc = EMA_ALPHA * (1.0 if correct else 0.0) + (1 - EMA_ALPHA) * acc
        rows.append({
            "agent":        agent,
            "date":         today,
            "accuracy_ema": round(new_acc, 4),
            "weight":       round(max(0.1, min(2.0, new_acc / 0.5)), 4),
            "n_signals":    n,
        })
    sb_upsert("stock_agent_weights", rows, on_conflict="agent,date")


def close_signal(signal_id: int) -> None:
    sb_patch(f"stock_signals?id=eq.{signal_id}", {"status_v2": "closed"})


# ============================================================
# Telegram EOD digest
# ============================================================

def send_digest(results: list[dict]) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        return
    wins   = [r for r in results if r["outcome"]["correct"]]
    losses = [r for r in results if not r["outcome"]["correct"]]
    lines  = [f"<b>📊 EOD Recap · {datetime.now(timezone.utc).date().isoformat()}</b>"]
    lines.append(f"{len(results)} signal(s) closed — {len(wins)} ✅  {len(losses)} ❌\n")
    for r in results:
        o    = r["outcome"]
        pct  = f"{o['net_return']*100:+.2f}%"
        icon = "✅" if o["correct"] else "❌"
        lines.append(f"{icon} <b>{r['ticker']}</b> {r['action']} {pct}")
    text = "\n".join(lines)
    try:
        requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=15,
        )
    except Exception as e:
        print(f"  Telegram digest failed: {e}", file=sys.stderr)


# ============================================================
# Operational logging
# ============================================================

def job_run_start() -> int | None:
    try:
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/stock_job_runs",
            headers={**HEADERS_SB, "Prefer": "return=representation"},
            json={"agent": "price_agent"}, timeout=10,
        )
        if r.status_code in (200, 201) and r.json():
            return r.json()[0]["id"]
    except Exception as e:
        print(f"  job_run_start failed: {e}", file=sys.stderr)
    return None


def job_run_finish(run_id: int | None, status: str, rows_in: int, rows_out: int, err: str | None = None) -> None:
    if run_id is None:
        return
    try:
        requests.patch(
            f"{SUPABASE_URL}/rest/v1/stock_job_runs?id=eq.{run_id}",
            headers=HEADERS_SB,
            json={
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "status":      status,
                "rows_in":     rows_in,
                "rows_out":    rows_out,
                "error_text":  err,
            }, timeout=10,
        )
    except Exception as e:
        print(f"  job_run_finish failed: {e}", file=sys.stderr)


# ============================================================
# Main
# ============================================================

# ============================================================
# Phase 7 — close event paper trades + update per-rule calibration.
# Runs at the end of each EOD pass after the signal-level reconcile.
# ============================================================

# Maturity gate: a rule (event_type[:subtype]) is "mature" when paper trades
# tied to it have ≥ 90% accuracy across at least 30 closed observations.
# Mature rules unlock BUY/SELL action vocabulary in thesis_agent.
MATURITY_ACCURACY = 0.90
MATURITY_MIN_N    = 30


def fetch_open_paper_trades_to_close() -> list[dict]:
    """Open trades whose horizon has expired (entry + horizon_days session
    close has passed). Conservative: include trades older than 1 day so we
    don't try to reconcile something opened 5 minutes ago."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()
    return sb_get("stock_event_paper_trades", {
        "status":   "eq.open",
        "entry_at": f"lte.{cutoff}T23:59:59+00:00",
        "select":   "id,event_id,event_type,event_subtype,ticker,direction,"
                    "entry_at,entry_price,horizon_days,rule_key,vehicle_type",
        "order":    "entry_at.asc",
        "limit":    "500",
    })


def fetch_archive_index() -> dict:
    """Fetch archive/index.json from Hostinger. Returns empty dict if unreachable."""
    try:
        r = requests.get(ARCHIVE_INDEX_URL, timeout=10)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f"  archive index fetch failed (non-fatal): {e}", file=sys.stderr)
    return {}


def enrich_cal_from_archive(cal: dict, archive_index: dict) -> None:
    """Boost active calibration counts with archived totals where archive is higher.

    stock_rule_calibration is never pruned so normally its counts already include
    all history. This merge acts as a floor: if archive shows more observations
    than the active table (e.g. after an accidental calibration reset), the
    archived count becomes the new base before today's delta is applied.
    """
    for rule_key, arc in archive_index.get("rule_calibration", {}).items():
        arc_n = int(arc.get("n_observations") or 0)
        arc_c = int(arc.get("n_correct") or 0)
        if arc_n == 0:
            continue
        cur = cal.get(rule_key)
        if cur is None:
            # Rule is in archive but missing from active table — seed it
            cal[rule_key] = {
                "rule_key":       rule_key,
                "n_observations": arc_n,
                "n_correct":      arc_c,
                "accuracy":       arc_c / arc_n if arc_n else 0.5,
                "mean_realized_pct": None,
                "is_mature":      False,
            }
        elif arc_n > int(cur.get("n_observations") or 0):
            # Archive has higher count — use as floor
            cur["n_observations"] = arc_n
            cur["n_correct"]      = arc_c


def fetch_calibration_map() -> dict[str, dict]:
    """{rule_key → {n_observations, n_correct, mean_realized_pct, is_mature}}"""
    rows = sb_get("stock_rule_calibration", {
        "select": "rule_key,n_observations,n_correct,accuracy,mean_realized_pct,is_mature,matured_at",
    })
    return {r["rule_key"]: r for r in rows}


def upsert_calibration(rule_key: str, current: dict | None,
                       new_correct: bool, new_return: float) -> bool:
    """Increment counters for one rule_key. Returns True if the rule just
    crossed the maturity threshold on this update."""
    cur = current or {}
    n_obs   = int(cur.get("n_observations") or 0) + 1
    n_corr  = int(cur.get("n_correct") or 0) + (1 if new_correct else 0)
    # Streaming mean: prev_mean + (x - prev_mean) / n
    prev_mean = float(cur.get("mean_realized_pct") or 0)
    mean_new  = prev_mean + (new_return - prev_mean) / n_obs
    accuracy  = n_corr / n_obs if n_obs > 0 else 0.5
    was_mature = bool(cur.get("is_mature"))
    is_mature  = (accuracy >= MATURITY_ACCURACY) and (n_obs >= MATURITY_MIN_N)
    just_matured = is_mature and not was_mature

    payload = {
        "rule_key":          rule_key,
        "n_observations":    n_obs,
        "n_correct":         n_corr,
        "accuracy":          round(accuracy, 6),
        "mean_realized_pct": round(mean_new, 6),
        "is_mature":         is_mature,
        "matured_at":        datetime.now(timezone.utc).isoformat() if just_matured else cur.get("matured_at"),
        "last_updated":      datetime.now(timezone.utc).isoformat(),
    }
    sb_upsert("stock_rule_calibration", [payload], on_conflict="rule_key")
    return just_matured


def compute_paper_outcome(trade: dict, bars: dict[date, dict[str, float]]) -> dict | None:
    """Direction-aware close-to-close return + MFE/MAE + stop/target hit audit.

    realized_return remains close-to-close (the canonical truth for
    calibration). The new fields are informational:
      mfe_pct   — best excursion in our favor between entry and exit
      mae_pct   — worst excursion against us between entry and exit
      target_hit — True if daily High (long) / Low (short) ever breached
                   target_pct between entry+1 and exit
      stop_hit   — True if daily Low (long) / High (short) ever breached
                   stop_pct between entry+1 and exit

    This is the "daily-HL audit" approximation. A full intraday audit would
    require 1-min or 5-min bars which we don't ingest yet.
    """
    entry_date = datetime.fromisoformat(trade["entry_at"].replace("Z", "+00:00")).date()
    horizon = int(trade.get("horizon_days") or 1)
    exit_target = entry_date + timedelta(days=horizon)
    exit_pair = close_on_or_after(bars, exit_target)
    if not exit_pair:
        return None
    exit_date, exit_price = exit_pair
    try:
        entry_price = float(trade["entry_price"])
    except (TypeError, ValueError):
        return None
    if entry_price <= 0:
        return None
    raw_return = (exit_price - entry_price) / entry_price
    direction = trade.get("direction") or "long"
    direction_mult = 1.0 if direction == "long" else -1.0
    realized = raw_return * direction_mult

    # MFE/MAE + stop/target audit over the holding period.
    target_pct = float(trade.get("target_pct") or 0)
    stop_pct = float(trade.get("stop_pct") or 0)
    if direction == "long":
        target_px = entry_price * (1 + target_pct) if target_pct else None
        stop_px = entry_price * (1 - stop_pct) if stop_pct else None
    else:
        target_px = entry_price * (1 - target_pct) if target_pct else None
        stop_px = entry_price * (1 + stop_pct) if stop_pct else None

    mfe_pct = 0.0
    mae_pct = 0.0
    target_hit = False
    stop_hit = False
    for d in sorted(bars):
        if d <= entry_date or d > exit_date:
            continue
        bar = bars[d]
        hi = bar.get("high")
        lo = bar.get("low")
        if hi is None or lo is None:
            continue
        # Excursions, direction-aware
        if direction == "long":
            up_excursion = (hi - entry_price) / entry_price
            down_excursion = (lo - entry_price) / entry_price
            mfe_pct = max(mfe_pct, up_excursion)
            mae_pct = min(mae_pct, down_excursion)
            if target_px is not None and hi >= target_px:
                target_hit = True
            if stop_px is not None and lo <= stop_px:
                stop_hit = True
        else:
            up_excursion = (entry_price - lo) / entry_price
            down_excursion = (entry_price - hi) / entry_price
            mfe_pct = max(mfe_pct, up_excursion)
            mae_pct = min(mae_pct, down_excursion)
            if target_px is not None and lo <= target_px:
                target_hit = True
            if stop_px is not None and hi >= stop_px:
                stop_hit = True

    return {
        "exit_at":         exit_date.isoformat() + "T00:00:00+00:00",
        "exit_price":      round(exit_price, 4),
        "realized_return": round(realized, 6),
        "correct":         realized > 0,
        "mfe_pct":         round(mfe_pct, 6),
        "mae_pct":         round(mae_pct, 6),
        "target_hit":      target_hit,
        "stop_hit":        stop_hit,
    }


def reconcile_event_paper_trades() -> tuple[int, int, int]:
    """Close mature paper trades, update per-rule calibration, return
    (n_closed, n_rules_updated, n_newly_matured)."""
    trades = fetch_open_paper_trades_to_close()
    if not trades:
        return 0, 0, 0

    cal = fetch_calibration_map()
    archive_index = fetch_archive_index()
    enrich_cal_from_archive(cal, archive_index)
    n_closed = n_rules_updated = n_matured = 0

    # Cache bars per ticker — avoid yfinance round-trips for the same ticker
    bars_cache: dict[str, dict] = {}

    for t in trades:
        ticker = t["ticker"]
        try:
            entry_date = datetime.fromisoformat(t["entry_at"].replace("Z", "+00:00")).date()
        except Exception:
            continue
        horizon = int(t.get("horizon_days") or 1)
        end_date = entry_date + timedelta(days=horizon + 3)   # buffer for weekends/holidays
        if ticker not in bars_cache:
            bars_cache[ticker] = fetch_bars(ticker, entry_date, end_date)
        bars = bars_cache[ticker]
        if not bars:
            continue

        outcome = compute_paper_outcome(t, bars)
        if outcome is None:
            continue

        # 1. Close the trade row (includes the daily-HL audit fields)
        sb_patch(f"stock_event_paper_trades?id=eq.{t['id']}", {
            "status":          "closed",
            "exit_at":         outcome["exit_at"],
            "exit_price":      outcome["exit_price"],
            "realized_return": outcome["realized_return"],
            "correct":         outcome["correct"],
            "mfe_pct":         outcome.get("mfe_pct"),
            "mae_pct":         outcome.get("mae_pct"),
            "target_hit":      outcome.get("target_hit"),
            "stop_hit":        outcome.get("stop_hit"),
        })
        n_closed += 1

        # 2. Update per-rule calibration
        rk = t.get("rule_key") or t["event_type"]
        just_mature = upsert_calibration(
            rk, cal.get(rk),
            new_correct=outcome["correct"],
            new_return=outcome["realized_return"],
        )
        # Refresh in-memory cache so subsequent trades for the same rule see updated counts
        cal[rk] = {
            **(cal.get(rk) or {}),
            "n_observations": int((cal.get(rk) or {}).get("n_observations") or 0) + 1,
            "n_correct":      int((cal.get(rk) or {}).get("n_correct") or 0) + (1 if outcome["correct"] else 0),
            "is_mature":      just_mature or (cal.get(rk) or {}).get("is_mature"),
        }
        n_rules_updated += 1
        if just_mature:
            n_matured += 1
            print(f"  🎓 rule '{rk}' matured: ≥{MATURITY_ACCURACY*100:.0f}% accuracy "
                  f"with n≥{MATURITY_MIN_N} — BUY/SELL unlocked")

    # 3. Recompute per-rule payoff aggregates for every rule that saw an
    # update this run. Cheap: pulls closed trades for the rule and reduces.
    rules_touched = {(t.get("rule_key") or t["event_type"]) for t in trades}
    for rk in rules_touched:
        recompute_rule_payoff(rk)

    return n_closed, n_rules_updated, n_matured


def recompute_rule_payoff(rule_key: str) -> None:
    """Pull all closed trades for a rule and recompute payoff aggregates.

    Adds to stock_rule_calibration: median_return_pct, avg_win_pct,
    avg_loss_pct, profit_factor, target_hit_rate, stop_hit_rate,
    mean_mfe_pct, mean_mae_pct. Skipped if the rule has < 5 closed trades
    (not enough sample to be meaningful).
    """
    rows = sb_get("stock_event_paper_trades", {
        "rule_key": f"eq.{rule_key}",
        "status":   "eq.closed",
        "select":   "realized_return,correct,mfe_pct,mae_pct,target_hit,stop_hit",
        "limit":    "1000",
    })
    if not rows or len(rows) < 5:
        return

    returns = [float(r.get("realized_return") or 0) for r in rows]
    wins = [v for v in returns if v > 0]
    losses = [v for v in returns if v <= 0]
    sum_wins = sum(wins)
    sum_losses = sum(losses)

    median_return = sorted(returns)[len(returns) // 2]
    avg_win = (sum_wins / len(wins)) if wins else None
    avg_loss = (sum_losses / len(losses)) if losses else None
    profit_factor = (sum_wins / abs(sum_losses)) if sum_losses < 0 else None

    target_hits = [r for r in rows if r.get("target_hit") is True]
    stop_hits = [r for r in rows if r.get("stop_hit") is True]
    target_hit_rate = len(target_hits) / len(rows)
    stop_hit_rate = len(stop_hits) / len(rows)

    mfe_values = [float(r.get("mfe_pct")) for r in rows if r.get("mfe_pct") is not None]
    mae_values = [float(r.get("mae_pct")) for r in rows if r.get("mae_pct") is not None]
    mean_mfe = (sum(mfe_values) / len(mfe_values)) if mfe_values else None
    mean_mae = (sum(mae_values) / len(mae_values)) if mae_values else None

    payload = {
        "rule_key":                   rule_key,
        "median_return_pct":          round(median_return, 6),
        "avg_win_pct":                round(avg_win, 6) if avg_win is not None else None,
        "avg_loss_pct":               round(avg_loss, 6) if avg_loss is not None else None,
        "profit_factor":              round(profit_factor, 4) if profit_factor is not None else None,
        "target_hit_rate":            round(target_hit_rate, 4),
        "stop_hit_rate":              round(stop_hit_rate, 4),
        "mean_mfe_pct":               round(mean_mfe, 6) if mean_mfe is not None else None,
        "mean_mae_pct":               round(mean_mae, 6) if mean_mae is not None else None,
        "last_payoff_recomputed_at":  datetime.now(timezone.utc).isoformat(),
    }
    sb_upsert("stock_rule_calibration", [payload], on_conflict="rule_key")


# ============================================================
# Main
# ============================================================

def main() -> int:
    run_id   = job_run_start()
    rows_in  = 0
    rows_out = 0

    try:
        signals = fetch_mature_signals()
        rows_in = len(signals)
        print(f"Mature signals to reconcile: {rows_in}")

        if signals:
            # Existing audit rows are not ignored: reruns use them to heal dependent
            # paper_forecasts and signal status without double-counting weights.
            audits = existing_audits([s["id"] for s in signals])
            pending = [s for s in signals if s["id"] not in audits]
            print(f"  {len(audits)} already audited, {len(pending)} to process")
            for sig in signals:
                audit = audits.get(int(sig["id"]))
                if audit:
                    healed = outcome_from_audit(audit)
                    if healed:
                        close_paper_forecasts(sig["id"], sig, healed)
                    close_signal(sig["id"])

            results = []
            for sig in pending:
                ticker = sig["ticker"]
                bars = fetch_bars(ticker, sig["_fired_date"], sig["_exit_date"])
                if not bars:
                    print(f"  {ticker} signal {sig['id']}: no price data — skipping", file=sys.stderr)
                    continue

                outcome = compute_outcome(sig, bars)
                if outcome is None:
                    print(f"  {ticker} signal {sig['id']}: price unavailable for window — skipping", file=sys.stderr)
                    continue

                # Extract contributing agents from weight_at_time snapshot
                wt     = sig.get("weight_at_time") or {}
                agents = wt.get("agents", []) if isinstance(wt, dict) else []

                write_forecast_audit(sig["id"], sig, outcome)
                close_paper_forecasts(sig["id"], sig, outcome)
                update_agent_weights(agents, outcome["correct"])
                close_signal(sig["id"])

                icon = "✅" if outcome["correct"] else "❌"
                print(f"  {icon} {ticker} signal {sig['id']}: entry={outcome['entry_price']} "
                      f"exit={outcome['exit_price']} ret={outcome['net_return']:+.4f} "
                      f"correct={outcome['correct']}")
                results.append({**sig, "outcome": outcome})
                rows_out += 1

            if results:
                send_digest(results)
            print(f"Closed {rows_out}/{len(pending)} signals")
        else:
            print("No mature signals to close.")

        # Phase 7 — close mature event paper trades + update per-rule calibration.
        # Always runs regardless of whether any signals were mature, so open
        # paper trades are reconciled even on low-signal days.
        try:
            n_paper_closed, n_rules_updated, n_matured = reconcile_event_paper_trades()
            rows_in += n_paper_closed   # count open trades as input work
            if n_paper_closed or n_rules_updated:
                print(f"Paper trades closed: {n_paper_closed}, "
                      f"rules updated: {n_rules_updated}, "
                      f"newly mature: {n_matured}")
            rows_out += n_paper_closed
        except Exception as e:  # noqa: BLE001 — never let learning loop crash the EOD job
            import traceback
            print(f"  paper-trade reconcile failed: {e}\n{traceback.format_exc()}", file=sys.stderr)
        job_run_finish(run_id, "ok", rows_in, rows_out)
        return 0

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        job_run_finish(run_id, "failed", rows_in, rows_out, err=str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
