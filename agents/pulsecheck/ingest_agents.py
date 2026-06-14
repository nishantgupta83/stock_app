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
from _market_calendar import is_trading_day   # M4: holiday-aware market hours


AGENT = "pulsecheck_ingest"
MARKET_OPEN_UTC = 13
MARKET_CLOSE_UTC = 21


def _now() -> datetime: return datetime.now(timezone.utc)
def _market_hours() -> bool:
    n = _now()
    # M4 (Codex): exclude NYSE holidays too, not just weekends — a quiet holiday
    # window must not false-warn the ingest freshness / bus-volume checks.
    return is_trading_day(n.date()) and MARKET_OPEN_UTC <= n.hour < MARKET_CLOSE_UTC


# M4: event-bus VOLUME floor. The freshness checks pass when an agent RAN ok,
# but an EDGAR all-429 sweep (or any bus-write failure) still records ok while
# landing ZERO events — the layer reads healthy but ingests nothing. This catches
# a TOTAL ingest collapse (all producers silent). Coarse on purpose: a single
# sporadic source (filings) going quiet is normal, so the floor sits FAR below
# normal market-hours volume (~20-30 events / 6h measured live) to avoid false
# alarms. Per-source volume floors are M4-2.
BUS_VOLUME_WINDOW_H = 6
BUS_VOLUME_FLOOR = 1   # warn if FEWER than this landed in the window (market hrs)


def classify_bus_volume(n_landed: int, market_hours: bool) -> tuple[str, str]:
    """Pure: (status, detail) for the event-bus volume check."""
    if not market_hours:
        return "ok", f"outside market hours — bus volume not enforced (n={n_landed})"
    if n_landed < BUS_VOLUME_FLOOR:
        return "warning", (f"event bus: {n_landed} events landed in last "
                           f"{BUS_VOLUME_WINDOW_H}h (market hours) < floor "
                           f"{BUS_VOLUME_FLOOR} — total ingest may be stalled")
    return "ok", f"event bus: {n_landed} events landed in last {BUS_VOLUME_WINDOW_H}h"


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
def earnings_agent_fresh() -> CheckResult:
    # earnings_agent.yml cron is `0 12 * * 0` (Sundays only) per the repo's
    # current cadence. A 6h threshold made this warn every weekday-night.
    # 7 days + 12h slack gives one Sunday-window grace.
    return _freshness("earnings_agent", 7 * 24 + 12)


def event_bus_volume() -> CheckResult:
    """M4: did ANY events land on the bus recently? Catches a total ingest
    collapse the per-agent freshness checks miss (an agent can run ok + ingest
    zero). Counts by created_at (what LANDED), per CLAUDE.md rule #1."""
    since = (_now() - timedelta(hours=BUS_VOLUME_WINDOW_H)).isoformat()
    n = sb_count("stock_normalized_events", {"created_at": f"gte.{since}"})
    status, detail = classify_bus_volume(n, _market_hours())
    return CheckResult(status, detail, observed=float(n), threshold=float(BUS_VOLUME_FLOOR))


CHECKS = [
    Check("filing_agent_fresh",   filing_agent_fresh,   depends_on=["pulsecheck_foundation"]),
    Check("truth_social_fresh",   truth_social_fresh,   depends_on=["pulsecheck_foundation"]),
    Check("earnings_agent_fresh", earnings_agent_fresh, depends_on=["pulsecheck_foundation"]),
    Check("event_bus_volume",     event_bus_volume,     depends_on=["pulsecheck_foundation"]),
]


def main() -> int:
    run_checks(AGENT, CHECKS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
