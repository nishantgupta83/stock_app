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
    """Return adjusted daily OHLC bars between start and end+7 days."""
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

def main() -> int:
    run_id   = job_run_start()
    rows_in  = 0
    rows_out = 0

    try:
        signals = fetch_mature_signals()
        rows_in = len(signals)
        print(f"Mature signals to reconcile: {rows_in}")

        if not signals:
            print("Nothing to close today.")
            job_run_finish(run_id, "ok", 0, 0)
            return 0

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
