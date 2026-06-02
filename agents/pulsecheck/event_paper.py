"""pulsecheck_event_paper — paper-trade ledger lifecycle health.

OWNS:
  * recent_runs           — event_paper_agent ran on schedule
  * open_trade_age        — no open trades older than horizon * 1.5
  * horizon_balance       — h1/h7/h15/h30 trades opened in expected ratios
  * close_backlog         — closures keep up with opens

DOES NOT OWN:
  * Calibration recompute (separate concern; could be its own check)
  * Sector multiplier view freshness (view is auto, not agent-driven)
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pulsecheck._pulse import Check, CheckResult, run_checks, sb_get, sb_count


AGENT = "pulsecheck_event_paper"
RUNS_PER_2H_FLOOR = 1     # event_paper_agent cron "5 * * * *" -> >=2/2h
OPEN_STALENESS_FACTOR = 1.5
HORIZONS = (1, 7, 15, 30)


def _now() -> datetime: return datetime.now(timezone.utc)


def recent_runs() -> CheckResult:
    since = (_now() - timedelta(hours=2)).isoformat()
    n = sb_count("stock_job_runs", {
        "agent": "eq.event_paper_agent",
        "started_at": f"gte.{since}",
    })
    status = "ok" if n >= RUNS_PER_2H_FLOOR else "warning"
    return CheckResult(status, f"{n} runs in last 2h", observed=float(n),
                       threshold=float(RUNS_PER_2H_FLOOR))


def open_trade_age() -> CheckResult:
    """Open trades older than horizon_days * 1.5 are stale (price_agent miss)."""
    stale_count = 0
    detail_parts = []
    for h in HORIZONS:
        cutoff = (_now() - timedelta(days=int(h * OPEN_STALENESS_FACTOR))).date().isoformat()
        n = sb_count("stock_event_paper_trades", {
            "status":       "eq.open",
            "horizon_days": f"eq.{h}",
            "entry_at":     f"lt.{cutoff}T00:00:00Z",
        })
        if n:
            detail_parts.append(f"h{h}:{n}")
            stale_count += n
    if stale_count == 0:
        return CheckResult("ok", "no stale open trades", observed=0.0,
                           threshold=10.0)
    detail = "stale opens: " + ", ".join(detail_parts)
    status = "warning" if stale_count < 200 else "critical"
    return CheckResult(status, detail, observed=float(stale_count), threshold=10.0)


def horizon_balance() -> CheckResult:
    """In the last 7d, each horizon should have similar open counts.

    event_paper_agent writes 4 trades per event (h1, h7, h15, h30). If
    one horizon's count diverges sharply, a partial-write regression
    (the bug fixed in event_paper_agent.fetch_already_traded_keys) may
    have returned.
    """
    since = (_now() - timedelta(days=7)).isoformat()
    counts = {}
    for h in HORIZONS:
        counts[h] = sb_count("stock_event_paper_trades", {
            "entry_at":     f"gte.{since}",
            "horizon_days": f"eq.{h}",
        })
    if not any(counts.values()):
        return CheckResult("ok", "no recent trades (low upstream)",
                           observed=0.0, meta={"counts": counts})
    lo, hi = min(counts.values()), max(counts.values())
    spread = (hi - lo) / max(hi, 1)
    status = "ok" if spread <= 0.25 else "warning"
    return CheckResult(
        status,
        f"horizon spread {spread:.2%} (counts={counts})",
        observed=spread,
        threshold=0.25,
        meta={"counts": counts},
    )


def h1d_close_lag() -> CheckResult:
    """h1d trades opened 2+ days ago must be closed (1d horizon + 1d buffer).

    Narrower than a raw open/close ratio because h7/h15/h30 trades
    legitimately stay open for days/weeks. This isolates the price_agent
    reconciliation health signal.
    """
    cutoff = (_now() - timedelta(days=2)).date().isoformat()
    open_old_h1 = sb_count("stock_event_paper_trades", {
        "horizon_days": "eq.1",
        "entry_at":     f"lt.{cutoff}T00:00:00Z",
        "status":       "eq.open",
    })
    status = "ok" if open_old_h1 < 50 else ("warning" if open_old_h1 < 500 else "critical")
    return CheckResult(
        status,
        f"h1d trades 2+ days old still open: {open_old_h1}",
        observed=float(open_old_h1),
        threshold=50.0,
    )


CHECKS = [
    Check("recent_runs",     recent_runs,     depends_on=["pulsecheck_foundation"]),
    Check("open_trade_age",  open_trade_age,  depends_on=["pulsecheck_foundation"]),
    Check("horizon_balance", horizon_balance, depends_on=["pulsecheck_foundation"]),
    Check("h1d_close_lag",   h1d_close_lag,   depends_on=["pulsecheck_foundation"]),
]


def main() -> int:
    run_checks(AGENT, CHECKS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
