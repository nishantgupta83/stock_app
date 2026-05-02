"""
Workflow-level operational recorder.

Agent code already records its own `stock_job_runs` rows after dependencies are
installed. This tiny stdlib-only helper lets GitHub Actions record wrapper
failures such as dependency install, timeout-adjacent cancellation, or deploy
failure without importing third-party packages.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib import parse, request
from urllib.error import URLError, HTTPError


STATUS_MAP = {
    "success": "ok",
    "ok": "ok",
    "failure": "failed",
    "failed": "failed",
    "cancelled": "failed",
    "canceled": "failed",
    "partial": "partial",
    "running": "running",
}


def _env() -> tuple[str, str] | None:
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        print("ops_recorder: missing Supabase env; skipping", file=sys.stderr)
        return None
    return url, key


def _headers(key: str, prefer: str | None = None) -> dict[str, str]:
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    if prefer:
        headers["Prefer"] = prefer
    return headers


def _request(method: str, url: str, key: str, payload: dict | None = None,
             prefer: str | None = None) -> tuple[int, list[dict] | dict | None, str]:
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
    req = request.Request(url, data=data, method=method, headers=_headers(key, prefer))
    try:
        with request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8")
            parsed = json.loads(body) if body else None
            return resp.status, parsed, body
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return e.code, None, body
    except URLError as e:
        return 0, None, str(e)


def _run_id_path(agent: str) -> Path:
    safe = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in agent)
    return Path(f".ops_run_id_{safe}")


def start(agent: str, meta: dict) -> int:
    env = _env()
    if env is None:
        return 0
    url, key = env
    payload = {
        "agent": agent,
        "status": "running",
        "meta": meta,
    }
    status, parsed, body = _request(
        "POST",
        f"{url}/rest/v1/stock_job_runs",
        key,
        payload,
        prefer="return=representation",
    )
    if status not in (200, 201) or not isinstance(parsed, list) or not parsed:
        print(f"ops_recorder start failed: HTTP {status} {body[:300]}", file=sys.stderr)
        return 0
    _run_id_path(agent).write_text(str(parsed[0]["id"]))
    return 0


def finish(agent: str, status: str, error: str | None) -> int:
    env = _env()
    if env is None:
        return 0
    url, key = env
    path = _run_id_path(agent)
    if not path.exists():
        return start(f"{agent}_finish_without_start", {
            "observed_status": status,
            "error": error,
        })
    run_id = path.read_text().strip()
    mapped = STATUS_MAP.get(status.lower(), "failed")
    payload = {
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "status": mapped,
        "error_text": error,
    }
    http_status, _, body = _request(
        "PATCH",
        f"{url}/rest/v1/stock_job_runs?id=eq.{parse.quote(run_id)}",
        key,
        payload,
    )
    if http_status not in (200, 201, 204):
        print(f"ops_recorder finish failed: HTTP {http_status} {body[:300]}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Record workflow wrapper status")
    ap.add_argument("--phase", choices=("start", "finish"), required=True)
    ap.add_argument("--agent", required=True)
    ap.add_argument("--status", default="running")
    ap.add_argument("--error", default=None)
    args = ap.parse_args(argv)

    meta = {
        "github_run_id": os.environ.get("GITHUB_RUN_ID"),
        "github_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        "github_workflow": os.environ.get("GITHUB_WORKFLOW"),
        "github_job": os.environ.get("GITHUB_JOB"),
        "github_ref": os.environ.get("GITHUB_REF"),
        "github_sha": os.environ.get("GITHUB_SHA"),
    }
    if args.phase == "start":
        return start(args.agent, meta)
    return finish(args.agent, args.status, args.error)


if __name__ == "__main__":
    sys.exit(main())
