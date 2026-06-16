#!/usr/bin/env python3
"""
Bootstrap cron-job.org backup pingers for stock_app GitHub Actions workflows.

GitHub Actions cron is best-effort and silently drops scheduled firings under
runner-pool load. This script provisions external pingers at cron-job.org that
call GitHub's workflow_dispatch API on a staggered cadence, so a dropped GHA
cron is covered within 7 minutes by the pinger instead of waiting for the next
scheduled slot.

Reads two secrets from env, never logs them:
  CRONJOB_API_KEY  - from cron-job.org Settings -> API Keys
  GH_DISPATCH_PAT  - fine-scoped PAT with Actions:write on this repo

Idempotent: existing jobs whose title matches the convention
"stock_app:<workflow>" are PATCHed; missing ones are PUT.

Re-run safely after rotating either secret.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

REPO = "nishantgupta83/stock_app"
GHA_BRANCH = "main"

# Pareto pick: */15 workflows where a dropped firing is most painful.
# Each entry: workflow filename -> (display title, schedule overrides)
# Schedule is staggered 7 min off GHA cron so dropped slots are covered fast
# without doubling work when GHA is healthy (concurrency:cancel-in-progress
# in each workflow cancels the duplicate within a second).
WORKFLOWS = {
    "site_generator.yml": {
        "title": "stock_app:site_generator",
        # Workflow is EOD-only since c35405c (~95% egress cut). This pinger had
        # NOT been updated — it still fired every 15min (~96/day), silently
        # undoing that cut: site_generator was ~85% of all Supabase read egress
        # (2026-06-10 audit; it re-reads 500 full signals + event payloads +
        # chart prices each run). Reduced to ONCE DAILY at 23:07 UTC (after the
        # ~22:00 price_agent EOD reconcile + learning_snapshot settle) — a
        # paper-review board only needs EOD freshness (Telegram is the real-time
        # path). 1 fire/day cuts site_generator from ~85% of Supabase egress to a
        # few %. The complex per-detail-page logic is kept as-is (can revisit
        # later). NOTE: orchestrator max_gap_hours (->30) + site_generator
        # inventory expected_minutes (->1440) updated to match (else false-alert).
        "schedule": {
            "timezone": "UTC",
            "minutes": [7],
            "hours": [23],
            "mdays": [-1],
            "months": [-1],
            "wdays": [-1],
        },
    },
    "paper_trade_agent.yml": {
        "title": "stock_app:paper_trade_agent",
        # GHA cron: */15 * * * *  -> same staggered cadence as site_generator
        "schedule": {
            "timezone": "UTC",
            "minutes": [7, 22, 37, 52],
            "hours": [-1],
            "mdays": [-1],
            "months": [-1],
            "wdays": [-1],
        },
    },
    "intraday_alert_agent.yml": {
        "title": "stock_app:intraday_alert_agent",
        # GHA cron: */15 13-21 * * 1-5 (market hours, weekdays)
        # Pinger mirrors window: same hours/wdays, staggered minutes.
        "schedule": {
            "timezone": "UTC",
            "minutes": [7, 22, 37, 52],
            "hours": [13, 14, 15, 16, 17, 18, 19, 20, 21],
            "mdays": [-1],
            "months": [-1],
            "wdays": [1, 2, 3, 4, 5],
        },
    },
    # Fast-cadence workflows (*/5 GHA cron). Pinger fires at 2,7,12,17,...
    # — offset 2 min from GHA's :00,:05,:10,... so dropped GHA slots are
    # covered within 2 min, doubled fires get cancelled by concurrency.
    "filing_agent.yml": {
        "title": "stock_app:filing_agent",
        "schedule": {
            "timezone": "UTC",
            "minutes": [7, 22, 37, 52],  # 2026-06-16: */5→*/15 egress cut (backstops the */15 GHA cron)
            "hours": [-1],
            "mdays": [-1],
            "months": [-1],
            "wdays": [-1],
        },
    },
    "news_agent.yml": {
        "title": "stock_app:news_agent",
        "schedule": {
            "timezone": "UTC",
            "minutes": [7, 22, 37, 52],  # 2026-06-16: */5→*/15 egress cut (backstops the */15 GHA cron)
            "hours": [-1],
            "mdays": [-1],
            "months": [-1],
            "wdays": [-1],
        },
    },
    "thesis_agent.yml": {
        "title": "stock_app:thesis_agent",
        "schedule": {
            "timezone": "UTC",
            "minutes": [7, 22, 37, 52],  # 2026-06-16: */5→*/15 egress cut (backstops the */15 GHA cron)
            "hours": [-1],
            "mdays": [-1],
            "months": [-1],
            "wdays": [-1],
        },
    },
    "truth_social_agent.yml": {
        "title": "stock_app:truth_social_agent",
        "schedule": {
            "timezone": "UTC",
            "minutes": [7, 22, 37, 52],  # 2026-06-16: */5→*/15 egress cut (backstops the */15 GHA cron)
            "hours": [-1],
            "mdays": [-1],
            "months": [-1],
            "wdays": [-1],
        },
    },
    # ---------------------------------------------------------------
    # Hourly learning & monitoring loops (added 2026-06-02).
    # NVDA-on-6/2 audit found event_paper_agent firing 1-2x/day instead
    # of its hourly cron — many news_article events fell outside the
    # 150-min lookback between runs, never becoming paper trades, so
    # rule_calibration starved. Same drop rate hit the realistic loop
    # and the new price_agent cron. Pingers staggered 17 min off each
    # GHA slot so concurrency-cancel absorbs the duplicate when GHA
    # fires on time.
    # ---------------------------------------------------------------
    "event_paper_agent.yml": {
        "title": "stock_app:event_paper_agent",
        # GHA cron: 5 * * * * → pinger at :22 hourly catches the drop.
        "schedule": {
            "timezone": "UTC",
            "minutes": [22],
            "hours": [-1],
            "mdays": [-1],
            "months": [-1],
            "wdays": [-1],
        },
    },
    "realistic_loop_agent.yml": {
        "title": "stock_app:realistic_loop_agent",
        # GHA cron: 15 * * * * (open) + 30 21 * * * (mark).
        # Pinger at :32 covers the open path; the daily mark is short
        # enough to tolerate a missed slot until the next 2h pulse.
        "schedule": {
            "timezone": "UTC",
            "minutes": [32],
            "hours": [-1],
            "mdays": [-1],
            "months": [-1],
            "wdays": [-1],
        },
    },
    "price_agent.yml": {
        "title": "stock_app:price_agent",
        # GHA cron: 0 */2 * * 1-5 (weekday every 2h). Pinger fires at
        # :17 of those same even hours on weekdays.
        "schedule": {
            "timezone": "UTC",
            "minutes": [17],
            "hours": [0, 2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22],
            "mdays": [-1],
            "months": [-1],
            "wdays": [1, 2, 3, 4, 5],
        },
    },
    "pulsecheck.yml": {
        "title": "stock_app:pulsecheck",
        # GHA cron: 20 * * * * (hourly). Pinger at :47 — half-cycle off
        # so we get either a fresh GHA run or a fresh pinger run within
        # 30 min, max.
        "schedule": {
            "timezone": "UTC",
            "minutes": [47],
            "hours": [-1],
            "mdays": [-1],
            "months": [-1],
            "wdays": [-1],
        },
    },
}

CRONJOB_API = "https://api.cron-job.org"
GITHUB_API = "https://api.github.com"


def fail(msg: str) -> "te.NoReturn":  # type: ignore[name-defined]
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def http(method: str, url: str, headers: dict, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read()
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"raw": raw.decode("utf-8", "replace")}
        return e.code, payload


def resolve_workflow_id(pat: str, filename: str) -> int:
    status, payload = http(
        "GET",
        f"{GITHUB_API}/repos/{REPO}/actions/workflows/{filename}",
        {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {pat}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "stock_app-bootstrap",
        },
    )
    if status != 200:
        fail(f"GitHub workflow lookup for {filename} returned {status}: {payload}")
    return int(payload["id"])


def verify_pat_can_dispatch(pat: str) -> None:
    # A read on /actions/workflows requires actions:read; a dispatch needs
    # actions:write. We pre-check by hitting the workflows list — if PAT lacks
    # the repo scope entirely we'll get 401/404 here, fast and clear.
    status, payload = http(
        "GET",
        f"{GITHUB_API}/repos/{REPO}/actions/workflows?per_page=1",
        {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {pat}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "stock_app-bootstrap",
        },
    )
    if status != 200:
        fail(f"GH_DISPATCH_PAT failed pre-check ({status}): {payload}. "
             "Confirm PAT has Actions:write on nishantgupta83/stock_app.")


def list_existing_jobs(api_key: str) -> dict[str, int]:
    # cron-job.org lists user's jobs at GET /jobs. Returns {title: jobId}
    # filtered to ones we manage (title prefix stock_app:).
    status, payload = http(
        "GET",
        f"{CRONJOB_API}/jobs",
        {"Authorization": f"Bearer {api_key}"},
    )
    if status != 200:
        fail(f"cron-job.org GET /jobs returned {status}: {payload}")
    return {
        j.get("title", ""): j["jobId"]
        for j in payload.get("jobs", [])
        if j.get("title", "").startswith("stock_app:")
    }


def upsert_job(
    api_key: str,
    pat: str,
    workflow_id: int,
    title: str,
    schedule: dict,
    existing_id: int | None,
) -> int:
    job_body = {
        "job": {
            "url": f"{GITHUB_API}/repos/{REPO}/actions/workflows/{workflow_id}/dispatches",
            "title": title,
            "enabled": True,
            "saveResponses": True,
            "requestMethod": 1,  # POST
            "requestTimeout": 30,
            "schedule": schedule,
            "extendedData": {
                "headers": {
                    "Accept": "application/vnd.github+json",
                    "Authorization": f"Bearer {pat}",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "User-Agent": "cron-job.org-stock_app-pinger",
                    "Content-Type": "application/json",
                },
                "body": json.dumps({"ref": GHA_BRANCH}),
            },
            "notification": {
                "onFailure": True,
                "onFailureCount": 2,
                "onSuccess": False,
                "onDisable": True,
            },
        }
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    if existing_id is None:
        status, payload = http("PUT", f"{CRONJOB_API}/jobs", headers, job_body)
        if status not in (200, 201):
            fail(f"PUT new job '{title}' returned {status}: {payload}")
        return int(payload["jobId"])
    else:
        status, payload = http(
            "PATCH", f"{CRONJOB_API}/jobs/{existing_id}", headers, job_body
        )
        if status not in (200, 204):
            fail(f"PATCH existing job '{title}' ({existing_id}) returned {status}: {payload}")
        return existing_id


def main() -> int:
    api_key = os.environ.get("CRONJOB_API_KEY")
    pat = os.environ.get("GH_DISPATCH_PAT")
    if not api_key:
        fail("CRONJOB_API_KEY env var not set")
    if not pat:
        fail("GH_DISPATCH_PAT env var not set")

    print("Pre-checking GitHub PAT scope...")
    verify_pat_can_dispatch(pat)
    print("  OK\n")

    print("Listing existing stock_app:* jobs at cron-job.org...")
    existing = list_existing_jobs(api_key)
    if existing:
        for t, jid in existing.items():
            print(f"  found {t} -> id={jid}")
    else:
        print("  none yet")
    print()

    for wf_file, cfg in WORKFLOWS.items():
        print(f"Provisioning pinger for {wf_file}...")
        wf_id = resolve_workflow_id(pat, wf_file)
        action = "PATCH" if cfg["title"] in existing else "PUT"
        new_id = upsert_job(
            api_key=api_key,
            pat=pat,
            workflow_id=wf_id,
            title=cfg["title"],
            schedule=cfg["schedule"],
            existing_id=existing.get(cfg["title"]),
        )
        print(f"  {action} -> jobId={new_id} (workflow_id={wf_id})\n")

    print("Done. Visit https://console.cron-job.org/jobs to verify schedules.")
    print("Next firing for each job is shown on the dashboard.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
