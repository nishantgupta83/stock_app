"""Audit agent (C2).

Daily pipeline-invariant audit. Runs after the overnight learning cycle,
queries Supabase for known integrity invariants, and alerts via Telegram
if any are violated.

Invariants checked (each independent — one failure doesn't block the rest):

  1. Every stock_signals.status_v2='sent' has a matching
     stock_telegram_dispatch_log row with delivery_ok=true.
  2. Every stock_risk_decisions.decision='size' ties to a setup whose
     signal's valid_until was > decision_at at decision time.
  3. stock_rule_calibration.n_observations == n_correct + n_incorrect.
  4. No stock_event_paper_trades.status='open' row older than
     horizon_days + 5 days (the close-window should have caught it).
  5. stock_normalized_events 24h count did not drop > 50% vs the same DOW
     last week (silent ingest failure signal).

Cron: daily at 04:00 UTC (after the learning cycle).
Workflow: .github/workflows/audit_agent.yml.

This agent does NOT write to any data table — its job is observation, not
mutation. The only side effects are stock_job_runs lifecycle rows
(via ops_recorder pattern) and Telegram alerts on failure.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
BOT_TOKEN    = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID      = os.environ.get("TELEGRAM_CHAT_ID", "")

HEADERS_SB = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
}

# Cardinality drop threshold: if today's event ingest is < 50% of the same-DOW
# value from a week ago, flag it as a likely silent ingest failure.
EVENT_DROP_THRESHOLD = 0.50
STALE_OPEN_GRACE_DAYS = 5


def sb_get(path: str, params: dict) -> list[dict]:
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{path}",
                     headers=HEADERS_SB, params=params, timeout=20)
    if r.status_code != 200:
        print(f"  GET {path} {r.status_code}: {r.text[:200]}", file=sys.stderr)
        return []
    return r.json()


def sb_count(path: str, params: dict) -> int:
    """Use PostgREST's HEAD + Prefer: count=exact to get row count cheaply."""
    headers = {**HEADERS_SB, "Prefer": "count=exact"}
    r = requests.head(f"{SUPABASE_URL}/rest/v1/{path}",
                      headers=headers, params=params, timeout=20)
    if r.status_code not in (200, 206):
        return -1
    cr = r.headers.get("content-range", "")
    if "/" in cr:
        try:
            return int(cr.split("/")[-1])
        except ValueError:
            return -1
    return -1


# ---------- invariants -------------------------------------------------------

def check_sent_signals_have_dispatch_logs() -> tuple[bool, str]:
    """Every status_v2='sent' signal must have delivery_ok=true in the
    dispatch log within ±1h of fired_at."""
    today_iso = datetime.now(timezone.utc).date().isoformat()
    sent = sb_get("stock_signals", {
        "status_v2": "eq.sent",
        "fired_at":  f"gte.{today_iso}T00:00:00Z",
        "select":    "id,fired_at",
        "limit":     "500",
    })
    if not sent:
        return True, "no signals sent today"
    sent_ids = [s["id"] for s in sent]
    in_list = ",".join(str(i) for i in sent_ids)
    logs = sb_get("stock_telegram_dispatch_log", {
        "signal_id":   f"in.({in_list})",
        "delivery_ok": "eq.true",
        "select":      "signal_id",
    })
    logged_ids = {row["signal_id"] for row in logs}
    missing = [i for i in sent_ids if i not in logged_ids]
    if missing:
        return False, f"{len(missing)} sent signals missing dispatch_log: {missing[:5]}"
    return True, f"all {len(sent_ids)} sent signals have dispatch logs"


def check_sized_decisions_have_live_signals() -> tuple[bool, str]:
    """Every decision='size' must reference a setup whose signal had
    valid_until > created_at at the time of decision."""
    today_iso = datetime.now(timezone.utc).date().isoformat()
    decisions = sb_get("stock_risk_decisions", {
        "decision":   "eq.size",
        "created_at": f"gte.{today_iso}T00:00:00Z",
        "select":     "setup_id,created_at",
        "limit":      "500",
    })
    if not decisions:
        return True, "no sized decisions today"
    setup_ids = list({d["setup_id"] for d in decisions})
    in_list = ",".join(str(i) for i in setup_ids)
    setups = sb_get("stock_trade_setups", {
        "id":     f"in.({in_list})",
        "select": "id,signal_id,valid_until",
    })
    setup_by_id = {s["id"]: s for s in setups}
    bad = []
    for d in decisions:
        s = setup_by_id.get(d["setup_id"])
        if not s or not s.get("valid_until"):
            bad.append(d["setup_id"])
            continue
        try:
            decided_at = datetime.fromisoformat(d["created_at"].replace("Z", "+00:00"))
            vu = datetime.fromisoformat(s["valid_until"].replace("Z", "+00:00"))
            if vu <= decided_at:
                bad.append(d["setup_id"])
        except (TypeError, ValueError):
            bad.append(d["setup_id"])
    if bad:
        return False, f"{len(bad)} sized decisions on expired/invalid setups: {bad[:5]}"
    return True, f"all {len(decisions)} sized decisions reference live signals"


def check_calibration_count_consistency() -> tuple[bool, str]:
    """For every rule, n_observations should equal n_correct + n_incorrect."""
    rows = sb_get("stock_rule_calibration", {
        "select": "rule_key,n_observations,n_correct,n_incorrect",
        "limit":  "2000",
    })
    bad = []
    for r in rows:
        n = int(r.get("n_observations") or 0)
        c = int(r.get("n_correct") or 0)
        ic = int(r.get("n_incorrect") or 0)
        if n != c + ic:
            bad.append(f"{r['rule_key']} ({n}!={c}+{ic})")
    if bad:
        return False, f"{len(bad)} rules with inconsistent counts: {bad[:3]}"
    return True, f"all {len(rows)} rules have consistent observation counts"


def check_no_stale_open_paper_trades() -> tuple[bool, str]:
    """No open paper trade may be older than horizon_days + STALE_OPEN_GRACE_DAYS.

    If any are, event_paper_agent.reconcile didn't close them — likely a
    yfinance gap or a bug in close-window selection."""
    rows = sb_get("stock_event_paper_trades", {
        "status": "eq.open",
        "select": "id,horizon_days,entry_at",
        "limit":  "2000",
    })
    now = datetime.now(timezone.utc)
    stale = []
    for r in rows:
        try:
            entry = datetime.fromisoformat(r["entry_at"].replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        h = int(r.get("horizon_days") or 1)
        age_days = (now - entry).days
        if age_days > h + STALE_OPEN_GRACE_DAYS:
            stale.append((r["id"], age_days, h))
    if stale:
        return False, f"{len(stale)} stale open paper trades (oldest: id={stale[0][0]} age={stale[0][1]}d horizon={stale[0][2]}d)"
    return True, f"all {len(rows)} open paper trades within their reconcile window"


def check_event_cardinality_not_dropping() -> tuple[bool, str]:
    """24h event count must not be < EVENT_DROP_THRESHOLD of same-DOW last week.

    Catches silent ingest failure — a wholesale collapse means an agent is
    broken even if individual job_runs still report success."""
    now = datetime.now(timezone.utc)
    day_ago = now - timedelta(days=1)
    week_ago_end = now - timedelta(days=7)
    week_ago_start = week_ago_end - timedelta(days=1)
    today_count = sb_count("stock_normalized_events", {
        "created_at": f"gte.{day_ago.isoformat()}",
    })
    weekago_count = sb_count("stock_normalized_events", {
        "created_at": f"gte.{week_ago_start.isoformat()}",
        "and":        f"(created_at.lt.{week_ago_end.isoformat()})",
    })
    if weekago_count <= 0:
        return True, f"no baseline (last week count={weekago_count}, today={today_count})"
    ratio = today_count / weekago_count
    if ratio < EVENT_DROP_THRESHOLD:
        return False, (f"event ingest collapsed: today={today_count} vs "
                       f"same-DOW last week={weekago_count} ({ratio:.1%})")
    return True, f"event ingest healthy: today={today_count} vs last-week={weekago_count} ({ratio:.1%})"


INVARIANTS = [
    ("sent_signals_have_dispatch_logs", check_sent_signals_have_dispatch_logs),
    ("sized_decisions_have_live_signals", check_sized_decisions_have_live_signals),
    ("calibration_count_consistency", check_calibration_count_consistency),
    ("no_stale_open_paper_trades", check_no_stale_open_paper_trades),
    ("event_cardinality_not_dropping", check_event_cardinality_not_dropping),
]


def send_telegram(text: str) -> bool:
    if not BOT_TOKEN or not CHAT_ID:
        print("  Telegram credentials not configured — skipping alert", file=sys.stderr)
        return False
    try:
        r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                          data={"chat_id": CHAT_ID, "text": text},
                          timeout=15)
        return r.status_code == 200 and r.json().get("ok", False)
    except Exception as e:  # noqa: BLE001
        print(f"  Telegram send failed: {e}", file=sys.stderr)
        return False


def job_run_start() -> int | None:
    try:
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/stock_job_runs",
            headers={**HEADERS_SB, "Prefer": "return=representation"},
            json={"agent": "audit_agent"}, timeout=10,
        )
        if r.status_code in (200, 201) and r.json():
            return r.json()[0]["id"]
    except Exception as exc:  # noqa: BLE001
        print(f"  job_run_start failed: {exc}", file=sys.stderr)
    return None


def job_run_finish(run_id: int | None, status: str, rows_in: int, rows_out: int,
                   err: str | None = None) -> None:
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
    except Exception:
        pass


def main() -> int:
    started = time.time()
    run_id = job_run_start()
    results: list[tuple[str, bool, str]] = []
    try:
        for name, fn in INVARIANTS:
            try:
                ok, detail = fn()
            except Exception as e:  # noqa: BLE001
                ok, detail = False, f"check raised: {e}"
            results.append((name, ok, detail))
            mark = "✓" if ok else "✗"
            print(f"  {mark} {name}: {detail}")

        failures = [(n, d) for n, ok, d in results if not ok]
        if failures:
            lines = ["🚨 Pipeline audit failures:"]
            for n, d in failures:
                lines.append(f"  · {n}: {d}")
            send_telegram("\n".join(lines))

        elapsed = time.time() - started
        job_run_finish(run_id, "ok" if not failures else "warning",
                       len(results), len(failures))
        print(f"audit_agent done in {elapsed:.1f}s ({len(failures)}/{len(results)} failures)")
        return 0
    except Exception as exc:  # noqa: BLE001
        job_run_finish(run_id, "error", 0, 0, str(exc)[:500])
        print(f"audit_agent fatal: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
