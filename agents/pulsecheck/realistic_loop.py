"""pulsecheck_realistic_loop — shadow $5K portfolio liveness.

OWNS:
  * recent_runs            — realistic_loop_agent ran on schedule
  * input_starvation       — null-reason setups exist if it's been quiet for days
  * position_lifecycle     — no positions stuck open past horizon_days * 2
  * pnl_drawdown           — cumulative drawdown stays within bankroll bounds

DOES NOT OWN:
  * Whether thesis is emitting (pulsecheck_thesis owns)
  * Whether intelligence layer is flagging (could be its own check)
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pulsecheck._pulse import Check, CheckResult, run_checks, sb_get, sb_count


AGENT = "pulsecheck_realistic_loop"
LOOP_NAME = "shadow_5k"
RUNS_PER_DAY_FLOOR = 6       # hourly opens (24/d) + 1 daily mark, but GHA cron
                             # drops ~30% under runner-pool contention, AND the
                             # workflow no-ops when there are no null-reason
                             # setups. 6/day = 1 every 4h is the floor that
                             # actually indicates "broken", not just "quiet".
STARVATION_DAYS = 5          # 5 trading days of 0 null-reason setups = warn
DRAWDOWN_PCT_WARN = 0.10     # 10% of bankroll
DRAWDOWN_PCT_CRIT = 0.20


def _now() -> datetime: return datetime.now(timezone.utc)


def recent_runs() -> CheckResult:
    since = (_now() - timedelta(hours=24)).isoformat()
    n = sb_count("stock_job_runs", {
        "agent": "eq.workflow_realistic_loop_agent",
        "started_at": f"gte.{since}",
    })
    status = "ok" if n >= RUNS_PER_DAY_FLOOR else "warning"
    return CheckResult(status, f"{n} workflow runs in 24h", observed=float(n),
                       threshold=float(RUNS_PER_DAY_FLOOR))


def input_starvation() -> CheckResult:
    """If we haven't had ANY null-reason setups for STARVATION_DAYS, flag it.

    This is the "loop is alive but starved" signal — the agent IS running
    and the workflow IS green, but the input pipe is dry. This is what
    would have surfaced the 5/22-6/2 thesis silence within 5 days of it
    starting (instead of 10).
    """
    since = (_now() - timedelta(days=STARVATION_DAYS)).isoformat()
    n = sb_count("stock_trade_setups", {
        "reason_to_skip": "is.null",
        "created_at":     f"gte.{since}",
    })
    status = "ok" if n > 0 else "warning"
    return CheckResult(
        status,
        f"null-reason setups in last {STARVATION_DAYS}d: {n}",
        observed=float(n),
        threshold=1.0,
    )


def position_lifecycle() -> CheckResult:
    """Open positions older than horizon_days * 2 are stuck."""
    rows = sb_get("stock_realistic_loop_positions", {
        "loop_name": f"eq.{LOOP_NAME}",
        "status":    "eq.open",
        "select":    "ticker,horizon_days,opened_at",
        "limit":     "200",
    })
    now = _now()
    stuck = []
    for p in rows:
        opened = datetime.fromisoformat(p["opened_at"].replace("Z", "+00:00"))
        age_days = (now - opened).total_seconds() / 86400
        if age_days > p["horizon_days"] * 2:
            stuck.append(f"{p['ticker']}(h={p['horizon_days']}d,age={age_days:.1f}d)")
    status = "ok" if not stuck else "warning"
    detail = "no stuck positions" if not stuck else f"stuck: {', '.join(stuck[:5])}"
    return CheckResult(status, detail, observed=float(len(stuck)), threshold=1.0)


def pnl_drawdown() -> CheckResult:
    rows = sb_get("stock_realistic_loop_state", {"loop_name": f"eq.{LOOP_NAME}"})
    if not rows:
        return CheckResult("warning", "no loop state row")
    s = rows[0]
    base = float(s["capital_base"])
    dd = float(s["max_drawdown"])
    pct = dd / base if base > 0 else 0
    if pct >= DRAWDOWN_PCT_CRIT:
        status = "critical"
    elif pct >= DRAWDOWN_PCT_WARN:
        status = "warning"
    else:
        status = "ok"
    return CheckResult(
        status,
        f"drawdown ${dd:.2f} ({pct:.1%} of bankroll)",
        observed=pct,
        threshold=DRAWDOWN_PCT_WARN,
    )


CHECKS = [
    Check("recent_runs",        recent_runs,        depends_on=["pulsecheck_foundation"]),
    Check("input_starvation",   input_starvation,   depends_on=["pulsecheck_foundation"]),
    Check("position_lifecycle", position_lifecycle, depends_on=["pulsecheck_foundation"]),
    Check("pnl_drawdown",       pnl_drawdown,       depends_on=["pulsecheck_foundation"]),
]


def main() -> int:
    run_checks(AGENT, CHECKS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
