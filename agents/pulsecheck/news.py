"""pulsecheck_news — news_agent ingest + classifier quality.

OWNS:
  * recent_runs            — news_agent ran on schedule
  * ingest_volume          — non-zero article landing during market hours
  * classifier_neutrality  — too high neutral rate suggests classifier under-recall
  * watchlist_coverage     — at least N watchlisted tickers got an article in 24h

DOES NOT OWN:
  * Thesis emit rate (downstream consumer; pulsecheck_thesis)
  * Specific keyword DB contents (config audit, not a runtime check)
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone, timedelta
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pulsecheck._pulse import Check, CheckResult, run_checks, sb_get, sb_count


AGENT = "pulsecheck_news"
RUNS_PER_2H_FLOOR = 1
INGEST_VOLUME_2H_FLOOR = 10
NEUTRALITY_RATE_WARN = 0.80     # >80% neutral = classifier likely under-recalling
WATCHLIST_COVERAGE_24H_FLOOR = 5


def _now() -> datetime: return datetime.now(timezone.utc)


def recent_runs() -> CheckResult:
    since = (_now() - timedelta(hours=2)).isoformat()
    n = sb_count("stock_job_runs", {
        "agent":      "eq.news_agent",
        "started_at": f"gte.{since}",
    })
    status = "ok" if n >= RUNS_PER_2H_FLOOR else "warning"
    return CheckResult(status, f"{n} runs in last 2h",
                       observed=float(n), threshold=float(RUNS_PER_2H_FLOOR))


def ingest_volume() -> CheckResult:
    """During US market hours, news_agent should land >=10 articles/2h."""
    now = _now()
    if now.weekday() >= 5 or not (13 <= now.hour < 21):
        return CheckResult("ok", "outside market hours — volume not evaluated")
    since = (now - timedelta(hours=2)).isoformat()
    n = sb_count("stock_normalized_events", {
        "event_type": "eq.news_article",
        "created_at": f"gte.{since}",
    })
    status = "ok" if n >= INGEST_VOLUME_2H_FLOOR else "warning"
    return CheckResult(status, f"{n} articles in last 2h", observed=float(n),
                       threshold=float(INGEST_VOLUME_2H_FLOOR))


def classifier_neutrality() -> CheckResult:
    """Caught the 6/2 finding: too high neutral rate suggests under-recall.

    Computes the share of news_article events in the last 24h classified
    as neutral. If >80%, the keyword classifier is probably missing
    catalysts (the 6/2 "Computex positives flagged neutral" pattern).
    """
    since = (_now() - timedelta(hours=24)).isoformat()
    rows = sb_get("stock_normalized_events", {
        "event_type": "eq.news_article",
        "created_at": f"gte.{since}",
        "select":     "event_subtype",
        "limit":      "1000",
    })
    if not rows:
        return CheckResult("ok", "no news in 24h (low upstream)")
    counts = Counter((r.get("event_subtype") or "unknown") for r in rows)
    total = sum(counts.values())
    neutral = counts.get("neutral", 0)
    rate = neutral / total
    status = "ok" if rate < NEUTRALITY_RATE_WARN else "warning"
    return CheckResult(
        status,
        f"24h neutral share: {neutral}/{total} ({rate:.0%})",
        observed=rate,
        threshold=NEUTRALITY_RATE_WARN,
        meta={"subtype_counts": dict(counts)},
    )


def watchlist_coverage() -> CheckResult:
    """In 24h, at least N watchlisted tickers should have a news article.

    If watchlist coverage drops to ~0, either the watchlist→subscription
    mapping broke or the news source dried up for those names.
    """
    since = (_now() - timedelta(hours=24)).isoformat()
    rows = sb_get("stock_normalized_events", {
        "event_type": "eq.news_article",
        "created_at": f"gte.{since}",
        "select":     "ticker",
        "limit":      "1000",
    })
    tickers = {r["ticker"] for r in rows if r.get("ticker")}
    # Cross-reference with active watchlists
    wl_rows = sb_get("stock_watchlists", {"select": "ticker", "limit": "1000"})
    wl = {r["ticker"] for r in wl_rows if r.get("ticker")}
    covered = tickers & wl
    n = len(covered)
    status = "ok" if n >= WATCHLIST_COVERAGE_24H_FLOOR else "warning"
    return CheckResult(
        status,
        f"{n} watchlisted tickers had news in 24h",
        observed=float(n),
        threshold=float(WATCHLIST_COVERAGE_24H_FLOOR),
        meta={"sample_covered": sorted(covered)[:10]},
    )


CHECKS = [
    Check("recent_runs",           recent_runs,           depends_on=["pulsecheck_foundation"]),
    Check("ingest_volume",         ingest_volume,         depends_on=["pulsecheck_foundation"]),
    Check("classifier_neutrality", classifier_neutrality, depends_on=["pulsecheck_foundation"]),
    Check("watchlist_coverage",    watchlist_coverage,    depends_on=["pulsecheck_foundation"]),
]


def main() -> int:
    run_checks(AGENT, CHECKS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
