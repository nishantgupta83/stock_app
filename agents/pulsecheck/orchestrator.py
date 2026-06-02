"""pulsecheck_orchestrator — orchestrator_agent freshness.

OWNS:
  * recent_run            — orchestrator_agent fired in last 36h

DOES NOT OWN:
  * What orchestrator decides to do (its rule logic is its own concern)
  * Per-agent freshness (covered by pulsecheck_ingest, _thesis, etc.)

A minimal pulsecheck — the orchestrator's own cadence is daily (per its
04:30 UTC cron), so a 36h floor catches a missed day without false-firing
on the normal gap between runs.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pulsecheck._pulse import Check, CheckResult, run_checks, sb_count


AGENT = "pulsecheck_orchestrator"
RUN_AGE_WARN_HOURS = 36


def _now() -> datetime: return datetime.now(timezone.utc)


def recent_run() -> CheckResult:
    since = (_now() - timedelta(hours=RUN_AGE_WARN_HOURS)).isoformat()
    n = sb_count("stock_job_runs", {
        "agent":      "eq.orchestrator_agent",
        "started_at": f"gte.{since}",
    })
    status = "ok" if n >= 1 else "warning"
    return CheckResult(
        status,
        f"orchestrator runs in last {RUN_AGE_WARN_HOURS}h: {n}",
        observed=float(n),
        threshold=1.0,
    )


CHECKS = [
    Check("recent_run", recent_run, depends_on=["pulsecheck_foundation"]),
]


def main() -> int:
    run_checks(AGENT, CHECKS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
