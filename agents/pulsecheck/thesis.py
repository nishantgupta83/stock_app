"""pulsecheck_thesis — emit-path health for the rubric scoring agent.

OWNS:
  * recent_runs              — at least 6 thesis_agent runs in the last 60 min
  * emit_rate_market_hours   — during US market hours, thesis emits >=1 signal in 6h
                               OR explains why (cap saturated, 0 candidates, etc.)
  * cap_consumption          — daily cap not silently saturated by other lanes
  * candidate_dryness        — flag if rows_in > 30 produces 0 candidates for >3h
                               during market hours (the 5/22-6/2 silence pattern)

DOES NOT OWN:
  * Foundation prereqs (covered by pulsecheck_foundation)
  * Setup / risk / paper-trade health (separate pulsechecks)
  * Classifier accuracy (separate concern; news pulsecheck)

The 5/22-6/2 silence — thesis emitted 0 rows for 10 days because
intraday-spike alerts burned the shared cap — is exactly the failure
mode candidate_dryness + cap_consumption are designed to catch on day 1.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pulsecheck._pulse import Check, CheckResult, run_checks, sb_get, sb_count


AGENT = "pulsecheck_thesis"
THESIS_MODEL_VERSION = "rubric-v1.1"
RUNS_PER_HOUR_FLOOR = 6      # thesis_agent.yml cron is */5 -> ~12/h expected
CANDIDATE_DRY_HOURS = 3      # 3h of rows_out=0 during market hours = warn
MARKET_OPEN_UTC = 13         # 9am ET = 13:00 UTC (DST: 14:00 — close enough)
MARKET_CLOSE_UTC = 21        # 4pm ET = 20:00 UTC; pad +1 for after-hours bleed


def _now() -> datetime: return datetime.now(timezone.utc)


def _in_market_window(dt: datetime | None = None) -> bool:
    dt = dt or _now()
    if dt.weekday() >= 5:
        return False
    return MARKET_OPEN_UTC <= dt.hour < MARKET_CLOSE_UTC


def recent_runs() -> CheckResult:
    """Was thesis_agent actually running on schedule?"""
    since = (_now() - timedelta(hours=1)).isoformat()
    n = sb_count("stock_job_runs", {
        "agent": "eq.thesis_agent",
        "started_at": f"gte.{since}",
    })
    status = "ok" if n >= RUNS_PER_HOUR_FLOOR else "warning"
    return CheckResult(status, f"{n} runs in last 60 min", observed=float(n),
                       threshold=float(RUNS_PER_HOUR_FLOOR))


def cap_consumption() -> CheckResult:
    """Is the daily cap being burned by non-thesis lanes?

    Counts non-thesis signals that landed today. If a large number are
    NOT model_version=rubric-v1.1 AND status_v2='sent', the old
    pre-2026-06-02 bug pattern may have regressed.
    """
    today = _now().date().isoformat()
    non_thesis = sb_count("stock_signals", {
        "fired_at":      f"gte.{today}T00:00:00Z",
        "status_v2":     "eq.sent",
        "model_version": f"neq.{THESIS_MODEL_VERSION}",
    })
    # This is informational — a high count is fine NOW (we fixed the cap),
    # but if alerts_sent_today() ever drops its model_version filter again,
    # this signal would silently approach MAX_ALERTS_PER_DAY=5 from a
    # cross-lane source. Warning threshold tracks that regression risk.
    status = "ok" if non_thesis < 50 else "warning"
    return CheckResult(
        status,
        f"non-thesis sent today: {non_thesis}",
        observed=float(non_thesis),
        threshold=50.0,
        meta={"model_version_floor": THESIS_MODEL_VERSION},
    )


def candidate_dryness() -> CheckResult:
    """During market hours, alert if N consecutive thesis runs produced 0 rows.

    This is the 5/22-6/2 silence pattern: thesis processes events, gets
    cap-blocked, and writes 0 signals — for days. The dryness check fires
    a warning if 3+ market hours pass with zero rows_out from thesis_agent
    despite non-trivial rows_in.
    """
    if not _in_market_window():
        return CheckResult("ok", "outside US market hours — dryness not evaluated")
    since = (_now() - timedelta(hours=CANDIDATE_DRY_HOURS)).isoformat()
    rows = sb_get("stock_job_runs", {
        "agent": "eq.thesis_agent",
        "started_at": f"gte.{since}",
        "select": "rows_in,rows_out,status",
        "limit": "100",
    })
    ok_rows = [r for r in rows if r.get("status") == "ok"]
    if not ok_rows:
        return CheckResult("warning", "no ok thesis_agent runs in window",
                           observed=0.0)
    rows_in = sum((r.get("rows_in") or 0) for r in ok_rows)
    rows_out = sum((r.get("rows_out") or 0) for r in ok_rows)
    detail = f"{CANDIDATE_DRY_HOURS}h window: rows_in={rows_in}, rows_out={rows_out}"
    if rows_in < 30:
        # Quiet upstream — not thesis's fault.
        return CheckResult("ok", detail + " (low upstream volume — not a dryness)")
    status = "warning" if rows_out == 0 else "ok"
    return CheckResult(
        status,
        detail,
        observed=float(rows_out),
        threshold=1.0,
        meta={"rows_in_total": rows_in},
    )


_SEVERITY_RANK = {"ok": 0, "warning": 1, "critical": 2}


def classify_emit(emit: dict) -> tuple[str, str]:
    """Per-run severity from a thesis run's meta.emit block.

    Uses ATTEMPTED inserts (emitted + failed), NOT n_candidates — candidates
    includes recently_dispatched dedupe-skips, which never attempted an insert.
    """
    n_failed = int(emit.get("n_insert_failed") or 0)
    n_emit = int(emit.get("n_emitted") or 0)
    # n_attempted is written by thesis_agent; fall back to emitted+failed for
    # any run recorded before that field existed.
    n_attempted = int(emit.get("n_attempted") or (n_emit + n_failed))
    if n_failed > 0 and n_emit == 0 and n_attempted > 0:
        return "critical", f"all {n_failed} signal inserts rejected (attempted={n_attempted})"
    if n_failed > 0:
        return "warning", f"{n_failed} signal insert(s) failed (emitted={n_emit})"
    return "ok", f"attempted={n_attempted} emitted={n_emit}"


def worst_emit(runs: list[dict]) -> tuple[str, str]:
    """Worst per-run emit severity across a window of runs.

    Cadence guard (Codex review): thesis runs ~*/5min but this pulsecheck runs
    hourly, so a clean/no-candidate run can immediately follow a fully-rejected
    one. Latest-row-only would re-hide the Finding-C failure; scan the window
    and surface the worst.
    """
    worst = ("ok", "no insert failures in window")
    for row in runs:
        meta = (row.get("meta") or {})
        emit = (meta.get("emit") or {}) if isinstance(meta, dict) else {}
        if not emit:
            continue
        status, detail = classify_emit(emit)
        if _SEVERITY_RANK[status] > _SEVERITY_RANK[worst[0]]:
            worst = (status, detail)
    return worst


INSERT_FAIL_WINDOW_HOURS = 3


def insert_failures() -> CheckResult:
    """Did any recent thesis run lose signals to a DB insert rejection?

    Reads stock_job_runs.meta.emit over the last INSERT_FAIL_WINDOW_HOURS.
    CRITICAL when a run scored candidates but emitted 0 because every insert
    was rejected — the silent failure mode that hid for 13 days in 2026-06
    (action CHECK constraint). WARNING on partial losses.
    """
    since = (_now() - timedelta(hours=INSERT_FAIL_WINDOW_HOURS)).isoformat()
    rows = sb_get("stock_job_runs", {
        "agent":      "eq.thesis_agent",
        "started_at": f"gte.{since}",
        "order":      "started_at.desc",
        "limit":      "100",
        "select":     "started_at,status,meta",
    })
    if not rows:
        return CheckResult("warning", "no thesis_agent runs in window", observed=0.0)
    status, detail = worst_emit(rows)
    total_failed = sum(
        int(((r.get("meta") or {}).get("emit") or {}).get("n_insert_failed") or 0)
        for r in rows if isinstance(r.get("meta"), dict)
    )
    return CheckResult(
        status,
        f"{INSERT_FAIL_WINDOW_HOURS}h window: {detail}",
        observed=float(total_failed),
        threshold=1.0,
        meta={"window_hours": INSERT_FAIL_WINDOW_HOURS,
              "total_insert_failed": total_failed},
    )


def rejection_distribution() -> CheckResult:
    """Read the 24h rejection mix and flag any single fail_reason >60%.

    Once stock_thesis_rejections has data (added 2026-06-02), this is the
    primary diagnostic for the candidate_dryness alarm — it tells us WHICH
    gate is binding. >60% of rejections through one gate means that gate
    deserves the next fix (lower threshold / add exception / ship keywords).
    """
    try:
        rows = sb_get("stock_thesis_rejection_mix", {"select": "fail_reason,n"})
    except Exception as e:  # noqa: BLE001
        return CheckResult("ok", f"view unavailable: {e}")
    if not rows:
        return CheckResult("ok", "no rejections in 24h (good — or instrumentation not yet writing)")
    total = sum(int(r.get("n") or 0) for r in rows)
    if total == 0:
        return CheckResult("ok", "0 rejections recorded")
    dominant = max(rows, key=lambda r: int(r.get("n") or 0))
    dominant_share = int(dominant["n"]) / total
    status = "warning" if dominant_share >= 0.60 else "ok"
    return CheckResult(
        status,
        f"24h rejections: {total} total, dominant={dominant['fail_reason']} ({dominant_share:.0%})",
        observed=dominant_share,
        threshold=0.60,
        meta={"mix": {r["fail_reason"]: int(r["n"]) for r in rows}},
    )


CHECKS = [
    Check("recent_runs",            recent_runs,            depends_on=["pulsecheck_foundation"]),
    Check("cap_consumption",        cap_consumption,        depends_on=["pulsecheck_foundation"]),
    Check("candidate_dryness",      candidate_dryness,      depends_on=["pulsecheck_foundation"]),
    Check("insert_failures",        insert_failures,        depends_on=["pulsecheck_foundation"]),
    Check("rejection_distribution", rejection_distribution, depends_on=["pulsecheck_foundation"]),
]


def main() -> int:
    run_checks(AGENT, CHECKS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
