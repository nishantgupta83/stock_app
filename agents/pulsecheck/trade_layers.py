"""pulsecheck_trade_layers — Layer-3 (trade_setup_agent) + Layer-4 (risk_agent)
run health.

OWNS:
  * l3_run_health   — trade_setup_agent latest run status (partial/failed loud)
  * l4_run_health   — risk_agent latest run status

DOES NOT OWN:
  * Whether a setup/decision was correct (that's the learning layer's concern)
  * Signal emission (pulsecheck_thesis) or paper-trade reconcile (price_agent)

These two layers had no pulsecheck owner. C3 made their write/skip failures
record status='partial' (+ error_text) in stock_job_runs instead of a silent
'ok'; this check is the consumer that surfaces it — a 'failed' latest run is
critical, a 'partial' is a warning, with the recorded error_text in the detail.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pulsecheck._pulse import Check, CheckResult, run_checks, sb_get


AGENT = "pulsecheck_trade_layers"
LOOKBACK_HOURS = 24
RUNNING_STALE_MIN = 15   # a 'running' row older than this means the job died mid-run


def _latest_run_health(agent_name: str) -> CheckResult:
    since = (datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)).isoformat()
    rows = sb_get("stock_job_runs", {
        "agent":      f"eq.{agent_name}",
        "started_at": f"gte.{since}",
        "select":     "status,error_text,started_at",
        "order":      "started_at.desc",
        "limit":      "20",
    }) or []
    if not rows:
        # No run in window — the agent's own cadence is dense (*/15-ish), so a
        # 24h silence is a warning (its run-count owner could be split out later).
        return CheckResult("warning", f"{agent_name}: no run in last {LOOKBACK_HOURS}h",
                           observed=0.0, threshold=1.0)
    latest = rows[0]
    st = (latest.get("status") or "").lower()
    n_partial = sum(1 for r in rows if (r.get("status") or "").lower() == "partial")
    if st == "failed":
        return CheckResult("critical", f"{agent_name} latest run FAILED: "
                           f"{(latest.get('error_text') or '')[:160]}",
                           meta={"error_text": latest.get("error_text")})
    if st == "running":
        # A 'running' latest run is fine if fresh, but a stale one means the job
        # died mid-run without finishing (the dangling-row class C3's L3 abort
        # fix closed). Warn past a generous grace window.
        try:
            started = datetime.fromisoformat((latest.get("started_at") or "").replace("Z", "+00:00"))
            age_min = (datetime.now(timezone.utc) - started).total_seconds() / 60
        except ValueError:
            age_min = 0.0
        if age_min > RUNNING_STALE_MIN:
            return CheckResult("warning", f"{agent_name} latest run stuck 'running' "
                               f"{age_min:.0f}m (died mid-run?)", observed=age_min,
                               threshold=float(RUNNING_STALE_MIN))
        return CheckResult("ok", f"{agent_name} run in progress ({age_min:.0f}m)")
    if st == "partial":
        return CheckResult("warning", f"{agent_name} latest run PARTIAL: "
                           f"{(latest.get('error_text') or '')[:160]}",
                           observed=float(n_partial),
                           meta={"error_text": latest.get("error_text"),
                                 "partial_runs_24h": n_partial})
    return CheckResult("ok", f"{agent_name} latest run ok "
                       f"({n_partial} partial in {LOOKBACK_HOURS}h)",
                       observed=float(n_partial))


def l3_run_health() -> CheckResult:
    return _latest_run_health("trade_setup_agent")


def l4_run_health() -> CheckResult:
    return _latest_run_health("risk_agent")


CHECKS = [
    Check("l3_run_health", l3_run_health, depends_on=["pulsecheck_foundation"]),
    Check("l4_run_health", l4_run_health, depends_on=["pulsecheck_foundation"]),
]


def main() -> int:
    run_checks(AGENT, CHECKS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
