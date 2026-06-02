"""Shared utilities for pulsecheck agents.

Each pulsecheck script:
  1. Defines a list of Check instances scoped to ONE workflow / agent.
  2. Optionally declares a `depends_on` list — pulsecheck names that must
     have a recent `ok` status. If any dep is missing or not-ok, this
     check skips with status='precondition_failed' (no false alarms).
  3. Calls run_checks(agent_name, checks).

A pulsecheck NEVER queries another agent's domain. If a fact is shared
(e.g., "is Supabase up"), it belongs to a single owner (pulsecheck_foundation)
and other pulsechecks reference it via depends_on. This is the rule that
keeps scopes from intersecting.

Output: one row per Check per run into stock_health_pulse. Use the
stock_health_pulse_current / stock_health_pulse_recent_alerts views to
read state.
"""
from __future__ import annotations

import dataclasses
import os
import sys
import traceback
from datetime import datetime, timezone
from typing import Any, Callable

import requests


SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
HEADERS_SB = {
    "apikey": SUPABASE_SERVICE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=minimal",
}

# How recent a dependency's `ok` pulse must be for this run to accept it.
DEP_FRESHNESS_SEC = 3600 * 2   # 2 hours


@dataclasses.dataclass
class CheckResult:
    """Returned by every Check callable."""
    status: str               # ok | warning | critical | skipped
    detail: str = ""
    observed: float | None = None
    threshold: float | None = None
    meta: dict | None = None


@dataclasses.dataclass
class Check:
    """One pulse measurement. fn must return CheckResult.

    depends_on lists pulsecheck agents (NOT check_names) whose latest
    status must be 'ok'. Use this to prevent cascading false alarms when
    a foundational check (Supabase reachable, fresh bars) has already
    flagged a problem.
    """
    name: str
    fn: Callable[[], CheckResult]
    depends_on: list[str] = dataclasses.field(default_factory=list)


# ---------------------------------------------------------------------------
# Supabase helpers (lightweight — no full client)
# ---------------------------------------------------------------------------

def sb_get(path: str, params: dict[str, str] | None = None) -> Any:
    """GET against PostgREST. Returns parsed JSON. Raises on HTTP error."""
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/{path}",
        headers={k: v for k, v in HEADERS_SB.items() if k != "Prefer"},
        params=params or {},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


def sb_count(path: str, params: dict[str, str] | None = None) -> int:
    """HEAD against PostgREST with Prefer: count=exact. Returns count."""
    headers = {**HEADERS_SB, "Prefer": "count=exact"}
    r = requests.head(
        f"{SUPABASE_URL}/rest/v1/{path}",
        headers=headers,
        params={**(params or {}), "select": "id"},
        timeout=20,
    )
    r.raise_for_status()
    cr = r.headers.get("content-range", "")
    if "/" in cr:
        return int(cr.rsplit("/", 1)[-1])
    return 0


def sb_post(path: str, rows: list[dict]) -> None:
    """POST without expecting a body. Raises on HTTP error."""
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/{path}",
        headers=HEADERS_SB,
        json=rows,
        timeout=20,
    )
    if r.status_code not in (200, 201, 204):
        raise RuntimeError(f"POST {path} -> {r.status_code}: {r.text[:200]}")


# ---------------------------------------------------------------------------
# Dependency resolution
# ---------------------------------------------------------------------------

def _deps_satisfied(deps: list[str]) -> tuple[bool, str]:
    """Are all `deps` agents in a recent ok state across all their checks?

    Returns (ok, reason). reason is empty when ok.
    """
    if not deps:
        return True, ""
    rows = sb_get(
        "stock_health_pulse_current",
        {"agent": f"in.({','.join(deps)})", "select": "agent,check_name,status,age_seconds"},
    )
    if not rows:
        return False, f"deps not yet pulsed: {deps}"
    # Group by agent — every check_name in that agent must be ok and fresh.
    by_agent: dict[str, list[dict]] = {}
    for r in rows:
        by_agent.setdefault(r["agent"], []).append(r)
    for dep in deps:
        if dep not in by_agent:
            return False, f"dep '{dep}' has no pulse yet"
        for r in by_agent[dep]:
            if r["status"] != "ok":
                return False, f"dep '{dep}/{r['check_name']}' is {r['status']}"
            if r["age_seconds"] > DEP_FRESHNESS_SEC:
                return False, f"dep '{dep}/{r['check_name']}' stale ({r['age_seconds']}s old)"
    return True, ""


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run_checks(agent: str, checks: list[Check]) -> int:
    """Execute checks in order, write pulses, return number of non-ok rows."""
    pulsed_at = datetime.now(timezone.utc).isoformat()
    rows = []
    non_ok = 0
    print(f"pulsecheck {agent} @ {pulsed_at}")
    for c in checks:
        deps_ok, reason = _deps_satisfied(c.depends_on)
        if not deps_ok:
            rows.append({
                "agent":      agent,
                "check_name": c.name,
                "status":     "precondition_failed",
                "detail":     reason,
                "observed":   None,
                "threshold":  None,
                "meta":       {"depends_on": c.depends_on},
                "pulsed_at":  pulsed_at,
            })
            print(f"  [skip] {c.name}: {reason}")
            continue
        try:
            res = c.fn()
        except Exception as e:
            tb = traceback.format_exc(limit=2)
            rows.append({
                "agent":      agent,
                "check_name": c.name,
                "status":     "critical",
                "detail":     f"check threw: {type(e).__name__}: {e}",
                "observed":   None,
                "threshold":  None,
                "meta":       {"traceback": tb[-300:]},
                "pulsed_at":  pulsed_at,
            })
            non_ok += 1
            print(f"  [crit] {c.name}: threw {type(e).__name__}")
            continue
        meta = res.meta or {}
        if c.depends_on:
            meta = {**meta, "depends_on": c.depends_on}
        rows.append({
            "agent":      agent,
            "check_name": c.name,
            "status":     res.status,
            "detail":     res.detail,
            "observed":   res.observed,
            "threshold":  res.threshold,
            "meta":       meta,
            "pulsed_at":  pulsed_at,
        })
        if res.status != "ok":
            non_ok += 1
        marker = {"ok": "[ok]  ", "warning": "[WARN]", "critical": "[CRIT]",
                  "skipped": "[skip]"}.get(res.status, "[?]   ")
        obs_str = f" obs={res.observed}" if res.observed is not None else ""
        thr_str = f" thr={res.threshold}" if res.threshold is not None else ""
        print(f"  {marker} {c.name}: {res.detail}{obs_str}{thr_str}")
    if rows:
        sb_post("stock_health_pulse", rows)
    print(f"pulsecheck {agent}: {len(rows)} pulses written, {non_ok} non-ok")
    return non_ok
