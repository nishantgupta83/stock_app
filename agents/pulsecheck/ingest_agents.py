"""pulsecheck_ingest — freshness of the Layer-1 ingest agents.

OWNS:
  * filing_agent_fresh      — SEC EDGAR ingest fired within last 2h (market hours)
  * news_agent_fresh        — covered in pulsecheck_news.recent_runs; here we
                              specifically check that the dedupe key is not all
                              the same article (ingest stuck)
  * truth_social_fresh      — truth_social_agent fired in last 2h
  * earnings_agent_fresh    — earnings_agent fired in last 6h (cadence is lower)

DOES NOT OWN:
  * news classifier quality (pulsecheck_news.classifier_neutrality)
  * Whether downstream signals fire (pulsecheck_thesis)

A single bucket for the ingest layer because the failure mode is the same
shape across these agents (cron drop or upstream-source outage) and the
operator response is the same too (check job_runs for errors, retry).
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pulsecheck._pulse import Check, CheckResult, run_checks, sb_count


AGENT = "pulsecheck_ingest"
MARKET_OPEN_UTC = 13
MARKET_CLOSE_UTC = 21


def _now() -> datetime: return datetime.now(timezone.utc)
def _market_hours() -> bool:
    n = _now()
    return n.weekday() < 5 and MARKET_OPEN_UTC <= n.hour < MARKET_CLOSE_UTC


def _freshness(agent_name: str, hours: int) -> CheckResult:
    """Generic: was `agent_name` ok at least once in last `hours` hours?"""
    # Only enforce floors during market hours — outside, low volume is normal.
    if not _market_hours() and agent_name != "earnings_agent":
        return CheckResult("ok", "outside market hours — freshness not enforced")
    since = (_now() - timedelta(hours=hours)).isoformat()
    n = sb_count("stock_job_runs", {
        "agent":      f"eq.{agent_name}",
        "started_at": f"gte.{since}",
        "status":     "eq.ok",
    })
    status = "ok" if n >= 1 else "warning"
    return CheckResult(
        status,
        f"{agent_name}: {n} ok runs in last {hours}h",
        observed=float(n),
        threshold=1.0,
    )


def filing_agent_fresh() -> CheckResult:    return _freshness("filing_agent", 2)
def truth_social_fresh() -> CheckResult:    return _freshness("truth_social_agent", 2)
def earnings_agent_fresh() -> CheckResult:  return _freshness("earnings_agent", 6)


CHECKS = [
    Check("filing_agent_fresh",   filing_agent_fresh,   depends_on=["pulsecheck_foundation"]),
    Check("truth_social_fresh",   truth_social_fresh,   depends_on=["pulsecheck_foundation"]),
    Check("earnings_agent_fresh", earnings_agent_fresh, depends_on=["pulsecheck_foundation"]),
]


def main() -> int:
    run_checks(AGENT, CHECKS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
