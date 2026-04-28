"""
Static site generator.

Pulls data from Supabase, renders 5 HTML pages + CSS via Jinja2 into dist/.
The dist/ branch is then committed by the workflow for user FTP upload.

Run via .github/workflows/site_generator.yml on */15 cron.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from jinja2 import Environment, FileSystemLoader, select_autoescape

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = ROOT / "templates"
DIST_DIR = ROOT / "dist"

HEADERS_SB = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
}

# Known agent inventory — drives the agents tab even if agent has no signals yet
KNOWN_AGENTS = ["filing", "truth_social", "thesis", "telegram_dispatcher", "news", "flows", "price"]


def sb_get(path: str, params: dict | None = None) -> list[dict]:
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{path}", headers=HEADERS_SB, params=params or {}, timeout=20)
    if r.status_code != 200:
        print(f"  SB {path} {r.status_code}: {r.text[:200]}", file=sys.stderr)
        return []
    return r.json()


# ============================================================
# Data fetchers
# ============================================================

def fetch_signals(limit: int = 500) -> list[dict]:
    rows = sb_get("stock_signals", {
        "select": "id,ticker,fired_at,action,score,confidence,evidence_summary,status_v2,model_version,weight_at_time",
        "order":  "fired_at.desc",
        "limit":  str(limit),
    })
    # Flatten weight_at_time.agents → agents list for the UI
    for r in rows:
        wt = r.get("weight_at_time") or {}
        r["agents"] = wt.get("agents", []) if isinstance(wt, dict) else []
        r["score"] = float(r.get("score") or 0)
        r["status_v2"] = r.get("status_v2") or "candidate"
        r["action"] = r.get("action") or "RESEARCH"
    return rows


def fetch_recent_events(limit: int = 200) -> list[dict]:
    return sb_get("stock_normalized_events", {
        "select": "id,ticker,event_type,event_subtype,event_at,severity,payload",
        "order":  "event_at.desc",
        "limit":  str(limit),
    })


def fetch_agent_freshness() -> list[dict]:
    return sb_get("stock_agent_freshness", {"select": "*"})


def fetch_recent_failures(limit: int = 10) -> list[dict]:
    return sb_get("stock_dead_letter_events", {
        "select": "occurred_at,agent,reason,detail",
        "order":  "occurred_at.desc",
        "limit":  str(limit),
    })


def fetch_latest_agent_weights() -> dict[str, dict]:
    """Return {agent_name: {weight, accuracy_ema, n_signals}} from most recent date per agent."""
    rows = sb_get("stock_agent_weights", {
        "select": "agent,date,weight,accuracy_ema,n_signals",
        "order":  "date.desc",
        "limit":  "1000",
    })
    latest: dict[str, dict] = {}
    for r in rows:
        latest.setdefault(r["agent"], r)
    return latest


def fetch_latest_backtest() -> dict | None:
    runs = sb_get("stock_backtest_runs", {
        "select": "id,model_version,started_at,finished_at,metrics",
        "order":  "finished_at.desc.nullslast",
        "limit":  "1",
    })
    if not runs or not runs[0].get("metrics"):
        return None
    run = runs[0]
    m = run["metrics"]
    m["model_version"]    = run["model_version"]
    m["run_finished_at"]  = (run.get("finished_at") or "")[:16]
    return m


def count_alerts_today() -> int:
    today = datetime.now(timezone.utc).date().isoformat()
    rows = sb_get("stock_signals", {
        "fired_at":  f"gte.{today}T00:00:00Z",
        "status_v2": "eq.sent",
        "select":    "id",
    })
    return len(rows)


def count_open_signals() -> int:
    rows = sb_get("stock_signals", {
        "status_v2": "eq.candidate",
        "select":    "id",
    })
    return len(rows)


def count_fresh_events() -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    rows = sb_get("stock_normalized_events", {
        "event_at": f"gte.{cutoff}",
        "select":   "id",
    })
    return len(rows)


# ============================================================
# Build derived views
# ============================================================

def derive_agent_rows(weights: dict, freshness: list[dict], signals: list[dict]) -> list[dict]:
    fresh_map = {f["agent"]: f for f in freshness}
    contrib = Counter()
    cutoff_30d = datetime.now(timezone.utc) - timedelta(days=30)
    for s in signals:
        try:
            t = datetime.fromisoformat(s["fired_at"].replace("Z", "+00:00"))
        except Exception:
            continue
        if t < cutoff_30d:
            continue
        for a in s.get("agents", []):
            contrib[a] += 1

    rows = []
    for name in KNOWN_AGENTS:
        w = weights.get(name) or {}
        f = fresh_map.get(name) or {}
        rows.append({
            "name":               name,
            "weight":             float(w.get("weight") or 1.0),
            "accuracy_ema":       float(w.get("accuracy_ema") or 0.5),
            "contributions_30d":  contrib.get(name, 0),
            "last_seen":          (f.get("last_seen") or "")[:16],
            "failures_1h":        int(f.get("failures_last_hour") or 0),
        })
    return rows


def derive_dashboard_metrics(events: list[dict], freshness: list[dict]) -> dict:
    cutoff_24h = datetime.now(timezone.utc) - timedelta(hours=24)
    by_agent = defaultdict(int)
    samples = {}
    for e in events:
        try:
            t = datetime.fromisoformat(e["event_at"].replace("Z", "+00:00"))
        except Exception:
            continue
        if t < cutoff_24h:
            continue
        # Map event_type → agent
        et = e["event_type"]
        agent = "filing" if et.startswith("filing_") or et == "8k_material_event" else (
                "truth_social" if et == "truth_social_post" else "other")
        by_agent[agent] += 1
        samples.setdefault(agent, e.get("event_subtype") or et)

    # Agent health: anyone seen in last 30 min (ingestion agents) considered healthy
    cutoff_health = datetime.now(timezone.utc) - timedelta(minutes=30)
    healthy = []
    stale = []
    for f in freshness:
        last = f.get("last_seen") or ""
        try:
            t = datetime.fromisoformat(last.replace("Z", "+00:00"))
            (healthy if t > cutoff_health else stale).append(f["agent"])
        except Exception:
            stale.append(f["agent"])

    return {
        "agent_activity": [(a, by_agent[a], samples.get(a, "")) for a in ("filing", "truth_social", "thesis")],
        "healthy_agents": healthy,
        "stale_agents":   stale,
    }


# ============================================================
# Render
# ============================================================

def render_all() -> int:
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "j2"]),
        keep_trailing_newline=True,
    )

    # Pull data once
    signals     = fetch_signals(500)
    events      = fetch_recent_events(200)
    freshness   = fetch_agent_freshness()
    failures    = fetch_recent_failures(10)
    weights     = fetch_latest_agent_weights()
    backtest    = fetch_latest_backtest()
    alerts_today = count_alerts_today()
    open_signals = count_open_signals()
    fresh_events = count_fresh_events()

    agent_rows = derive_agent_rows(weights, freshness, signals)
    dash       = derive_dashboard_metrics(events, freshness)

    distinct_agents = sorted({a for s in signals for a in s.get("agents", [])} | set(KNOWN_AGENTS))
    distinct_types  = sorted({e["event_type"] for e in events})

    common = {
        "generated_at":  datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
    }

    DIST_DIR.mkdir(exist_ok=True)
    shutil.copy(TEMPLATES_DIR / "styles.css", DIST_DIR / "styles.css")

    # Dashboard
    (DIST_DIR / "index.html").write_text(env.get_template("index.html.j2").render(
        **common,
        title="Dashboard", active="index",
        alerts_today=alerts_today,
        open_signals=open_signals,
        fresh_events=fresh_events,
        recent_signals=signals[:10],
        agent_activity=dash["agent_activity"],
        all_agents_healthy=len(dash["stale_agents"]) == 0 and len(dash["healthy_agents"]) > 0,
        healthy_agent_count=len(dash["healthy_agents"]),
        total_agent_count=len(dash["healthy_agents"]) + len(dash["stale_agents"]),
        stale_agents=dash["stale_agents"],
    ))

    # Signals (with embedded JSON for client-side filter)
    (DIST_DIR / "signals.html").write_text(env.get_template("signals.html.j2").render(
        **common,
        title="Signals", active="signals",
        distinct_agents=distinct_agents,
        signals_json=json.dumps(signals, default=str),
    ))

    # Events
    (DIST_DIR / "events.html").write_text(env.get_template("events.html.j2").render(
        **common,
        title="Events", active="events",
        distinct_types=distinct_types,
        events_json=json.dumps(events, default=str),
    ))

    # Agents
    (DIST_DIR / "agents.html").write_text(env.get_template("agents.html.j2").render(
        **common,
        title="Agents", active="agents",
        agent_rows=agent_rows,
        recent_failures=failures,
    ))

    # Backtest
    (DIST_DIR / "backtest.html").write_text(env.get_template("backtest.html.j2").render(
        **common,
        title="Backtest", active="backtest",
        bt=backtest,
    ))

    print(f"Wrote {len(list(DIST_DIR.glob('*.html')))} HTML files + styles.css to {DIST_DIR}")
    return 0


# ============================================================
# Operational logging
# ============================================================

def job_run_start() -> int | None:
    try:
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/stock_job_runs",
            headers={**HEADERS_SB, "Content-Type": "application/json", "Prefer": "return=representation"},
            json={"agent": "site_generator"}, timeout=10,
        )
        if r.status_code in (200, 201) and r.json():
            return r.json()[0]["id"]
    except Exception:
        pass
    return None


def job_run_finish(run_id: int | None, status: str, err: str | None = None) -> None:
    if run_id is None:
        return
    try:
        requests.patch(
            f"{SUPABASE_URL}/rest/v1/stock_job_runs?id=eq.{run_id}",
            headers={**HEADERS_SB, "Content-Type": "application/json"},
            json={
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "status":      status,
                "error_text":  err,
            }, timeout=10,
        )
    except Exception:
        pass


def main() -> int:
    run_id = job_run_start()
    try:
        rc = render_all()
        job_run_finish(run_id, "ok" if rc == 0 else "failed")
        return rc
    except Exception as e:
        import traceback
        print(traceback.format_exc(), file=sys.stderr)
        job_run_finish(run_id, "failed", err=str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
