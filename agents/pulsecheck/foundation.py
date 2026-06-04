"""pulsecheck_foundation — system-wide preconditions only.

OWNS:
  * supabase_reachable        — PostgREST responds with 2xx in <5s
  * site_freshness            — hub4apps.com/stock_app/status.json updated within 30 min
  * recent_bars               — stock_raw_prices has a bar from the last trading day

DOES NOT OWN:
  * Anything agent-specific (those live in their own pulsechecks)
  * Workflow run health (each agent's pulsecheck owns its own runs)

This is the dependency-root: every other pulsecheck declares
depends_on=['pulsecheck_foundation']. If Supabase is unreachable or
prices are stale, downstream checks SKIP rather than emit confusing
agent-specific alerts.
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, timezone

# Allow this file to be executed directly: ensure agents/ on sys.path.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests

from pulsecheck._pulse import Check, CheckResult, run_checks, sb_get


AGENT = "pulsecheck_foundation"

LIVE_SITE_URL = "https://hub4apps.com/stock_app/status.json"
# Bumped 2026-06-04 from 30 min to 26 h to match site_generator's new
# EOD-only cadence (cron "0 22 * * *"). Anything older than ~1 EOD cycle
# plus slop indicates the site_generator job failed or Hostinger upload
# stuck — that's the real warning condition now.
SITE_STALE_THRESHOLD_SEC = 26 * 60 * 60
BARS_STALE_THRESHOLD_DAYS = 5               # weekend + 1 holiday tolerance


def supabase_reachable() -> CheckResult:
    try:
        rows = sb_get("stock_symbols", {"select": "ticker", "limit": "1"})
    except requests.HTTPError as e:
        return CheckResult("critical", f"http error: {e.response.status_code}")
    except Exception as e:  # noqa: BLE001
        return CheckResult("critical", f"unreachable: {type(e).__name__}: {e}")
    return CheckResult("ok", f"PostgREST responded ({len(rows)} sample row)", observed=1.0)


def site_freshness() -> CheckResult:
    """Public site age check — informational only.

    Returns 'ok' on ANY fetch failure (network errors from GHA runners to
    Hostinger are not reliable indicators of an actual site outage —
    cron-job.org pingers per CLAUDE.md §8 are the authoritative external
    check). Only returns 'warning' when we CAN fetch the JSON and the
    generated_at is verifiably old. This prevents downstream pulsechecks
    from being blocked by GHA-side connectivity flakes — site freshness
    is a Layer-6 concern, not a hard prerequisite for Layer 1-5 checks.
    """
    try:
        r = requests.get(LIVE_SITE_URL, timeout=10)
    except Exception as e:  # noqa: BLE001
        # GHA can't reach the site — could be Hostinger, DNS, or just GHA's
        # outbound network. Don't poison the dependency graph from here.
        return CheckResult("ok", f"fetch n/a from runner ({type(e).__name__}); "
                                 f"external pingers are authoritative",
                           meta={"error": str(e)[:120]})
    if r.status_code != 200:
        return CheckResult("ok", f"site HTTP {r.status_code} (n/a — see external pingers)",
                           meta={"status_code": r.status_code})
    try:
        gen_at = r.json().get("generated_at")
    except Exception:
        return CheckResult("ok", "status.json not JSON (n/a)")
    if not gen_at:
        return CheckResult("ok", "no generated_at in status.json (n/a)")
    try:
        gen = datetime.fromisoformat(gen_at.replace("Z", "+00:00"))
    except Exception:
        return CheckResult("ok", f"bad generated_at: {gen_at}")
    age = int((datetime.now(timezone.utc) - gen).total_seconds())
    status = "ok" if age <= SITE_STALE_THRESHOLD_SEC else "warning"
    return CheckResult(status, f"site age {age}s", observed=age, threshold=SITE_STALE_THRESHOLD_SEC)


def recent_bars() -> CheckResult:
    """Latest stock_raw_prices.ts is within BARS_STALE_THRESHOLD_DAYS."""
    rows = sb_get("stock_raw_prices", {"select": "ts", "order": "ts.desc", "limit": "1"})
    if not rows:
        return CheckResult("critical", "stock_raw_prices is empty")
    latest = datetime.fromisoformat(rows[0]["ts"].replace("Z", "+00:00")).date()
    today = date.today()
    age_days = (today - latest).days
    status = "ok" if age_days <= BARS_STALE_THRESHOLD_DAYS else "warning"
    return CheckResult(
        status,
        f"latest bar {latest.isoformat()} ({age_days}d ago)",
        observed=float(age_days),
        threshold=float(BARS_STALE_THRESHOLD_DAYS),
    )


CHECKS = [
    Check("supabase_reachable", supabase_reachable),
    Check("site_freshness",     site_freshness),
    Check("recent_bars",        recent_bars),
]


def main() -> int:
    return 0 if run_checks(AGENT, CHECKS) == 0 else 0
    # We always exit 0 — non-ok status is information for the dashboard,
    # not a workflow failure. A workflow failure would mean the pulsecheck
    # itself broke, which we'd want to investigate separately.


if __name__ == "__main__":
    sys.exit(main())
