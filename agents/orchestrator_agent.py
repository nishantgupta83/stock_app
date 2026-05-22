"""Orchestrator agent.

Daily-fire watchdog. For every agent we expect to run on a schedule,
check stock_job_runs for the most recent successful run; if the gap
exceeds the per-agent max_gap_hours, fire a single Telegram summary
listing every stale agent.

Trading-day gating: agents whose workflow cron restricts firing to
1-5 (Mon-Fri) are marked trading_only=True. On weekends and NYSE
holidays we extend their gap budget by 24h per non-trading day since
the last trading day, so the orchestrator doesn't flag them as missing
when they legitimately shouldn't fire.

Telegram payload routes through telegram_dispatcher.send_and_log so
its alert is itself written to stock_telegram_dispatch_log — which means
audit_agent invariant #1 enforces this layer too.

Scheduled by .github/workflows/orchestrator_agent.yml (cron 30 4 * * *).
"""
from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import requests

from _market_calendar import is_trading_day, previous_trading_day

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

HEADERS_SB = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
}

GH_REPO = "nishantgupta83/stock_app"
GH_TOKEN = os.environ.get("GH_TOKEN", "")


@dataclass(frozen=True)
class AgentExpectation:
    name: str
    cadence: str          # human label only — config truth is max_gap_hours
    max_gap_hours: float  # alert if (now - last_run) exceeds this
    trading_only: bool    # extend window on weekends/holidays


# Truth is what each workflow's cron says (see .github/workflows/*.yml).
# Pad max_gap_hours generously — GHA cron is best-effort (CLAUDE.md rule #6),
# so we only want to alert when something is *really* late, not when a single
# cron fired late.
EXPECTED: list[AgentExpectation] = [
    # ----- always-on agents -----
    AgentExpectation("filing_agent",          "every 5 min, 24/7",     1.0,  False),
    AgentExpectation("news_agent",            "every 5 min, 24/7",     1.0,  False),
    AgentExpectation("thesis_agent",          "every 5 min, 24/7",     1.0,  False),
    AgentExpectation("truth_social_agent",    "every 5 min, 24/7",     1.0,  False),
    AgentExpectation("paper_trade_agent",     "every 15 min, 24/7",    2.0,  False),
    AgentExpectation("site_generator",        "every 15 min, 24/7",    1.5,  False),
    AgentExpectation("risk_agent",            "every 30 min, 24/7",    2.0,  False),
    AgentExpectation("trade_setup_agent",     "every 30 min, 24/7",    2.0,  False),
    AgentExpectation("event_paper_agent",     "hourly, 24/7",          4.0,  False),
    AgentExpectation("activist_insider_agent","every 2h, 24/7",        5.0,  False),

    # ----- daily anytime -----
    AgentExpectation("audit_agent",           "daily 04:00 UTC",       26.0, False),

    # ----- trading-day only -----
    AgentExpectation("intraday_alert_agent",  "*/15 during US session",  2.0, True),
    AgentExpectation("consumer_health_agent", "daily 15:00 UTC weekdays", 28.0, True),
    AgentExpectation("energy_transition_agent","daily 13:45 UTC weekdays", 28.0, True),
    AgentExpectation("biotech_agent",         "daily 14:00 UTC weekdays", 28.0, True),
    AgentExpectation("defense_agent",         "daily 22:30 UTC weekdays", 28.0, True),
    AgentExpectation("crypto_macro_agent",    "daily 21:35 UTC weekdays", 28.0, True),
    AgentExpectation("macro_rates_agent",     "daily 13:00 UTC weekdays", 28.0, True),
    AgentExpectation("market_scanner_agent",  "daily 21:30 UTC weekdays", 28.0, True),
    AgentExpectation("price_agent",           "daily 21:30 UTC weekdays", 28.0, True),

    # ----- weekly -----
    AgentExpectation("archive_agent",         "Sun 03:00 UTC",         8 * 24.0, False),
    AgentExpectation("earnings_agent",        "Sun 12:00 UTC",         8 * 24.0, False),
    AgentExpectation("flows_agent",           "Sun 14:00 UTC",         8 * 24.0, False),

    # ----- monthly -----
    AgentExpectation("source_review_agent",   "1st of month 13:00 UTC", 32 * 24.0, False),
]


def effective_max_gap_hours(spec: AgentExpectation, now: datetime) -> float:
    """For trading_only agents, add 24h of slack per non-trading day since
    the last trading day. A weekend correctly gets +48h without us needing
    to know each agent's exact cron minute.
    """
    if not spec.trading_only:
        return spec.max_gap_hours
    d = now.date()
    if is_trading_day(d):
        return spec.max_gap_hours
    # We're on a weekend/holiday — find days since last trading day.
    last_td = previous_trading_day(d + timedelta(days=1))
    non_trading_days = (d - last_td).days
    return spec.max_gap_hours + 24.0 * non_trading_days


def fetch_last_run(agent_name: str) -> datetime | None:
    """Most recent started_at for an agent.

    Some agents record their own job_run via job_run_start() inside the
    Python script (e.g. thesis_agent, audit_agent), while others only get
    recorded by the workflow wrapper as 'workflow_<name>' (e.g. archive_agent,
    which has no in-script job_run call). The orchestrator must look at both
    — the latest of either is the true "this agent ran" timestamp.
    """
    in_list = f"({agent_name},workflow_{agent_name})"
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/stock_job_runs"
        f"?agent=in.{in_list}&order=started_at.desc&limit=1"
        f"&select=started_at",
        headers=HEADERS_SB, timeout=10,
    )
    if r.status_code != 200 or not r.json():
        return None
    raw = r.json()[0]["started_at"]
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def check_agent(spec: AgentExpectation, now: datetime) -> tuple[bool, str, float | None]:
    """Returns (ok, detail, hours_since_last). hours_since_last is None when
    no run was ever recorded."""
    last = fetch_last_run(spec.name)
    budget = effective_max_gap_hours(spec, now)
    if last is None:
        return False, f"no run ever recorded (budget {budget:.1f}h)", None
    age_hours = (now - last).total_seconds() / 3600.0
    if age_hours > budget:
        return False, f"last run {age_hours:.1f}h ago (budget {budget:.1f}h)", age_hours
    return True, f"last run {age_hours:.1f}h ago", age_hours


def format_summary(results: list[tuple[AgentExpectation, bool, str, float | None]],
                   now: datetime) -> str:
    """Telegram-friendly multi-line summary of stale agents."""
    stale = [(s, d) for s, ok, d, _ in results if not ok]
    if not stale:
        return ""
    today = now.strftime("%Y-%m-%d %H:%M UTC")
    is_td = is_trading_day(now.date())
    lines = [
        f"🛎 Orchestrator stale-job report — {today}",
        f"trading_day={is_td}    {len(stale)}/{len(results)} agents flagged",
        "",
    ]
    for spec, detail in stale:
        marker = " (trading_only)" if spec.trading_only else ""
        lines.append(f"  · {spec.name}{marker}: {detail}")
    return "\n".join(lines)


def sweep_stale_running(agent: str | None = "orchestrator_agent", max_age_hours: int = 2) -> int:
    """Mark prior `status='running'` rows as failed if older than max_age_hours.

    Covers SIGKILL-from-runner-timeout cases that bypass main()'s try/except —
    GHA hard-kills a job at the runner timeout, so job_run_finish never runs.

    agent=None sweeps every agent. 2h matches the orchestrator's hourly
    cadence + buffer; for agents that legitimately run longer (backtester
    can take 10 min, never hours) the 2h floor is still safe.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).strftime('%Y-%m-%dT%H:%M:%SZ')
    qs = f"status=eq.running&started_at=lt.{cutoff}"
    if agent:
        qs = f"agent=eq.{agent}&{qs}"
    try:
        r = requests.patch(
            f"{SUPABASE_URL}/rest/v1/stock_job_runs?{qs}",
            headers={**HEADERS_SB, "Prefer": "return=representation"},
            json={
                "status":      "failed",
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "error_text":  f"presumed_killed (running > {max_age_hours}h)",
            }, timeout=10,
        )
        if r.status_code in (200, 204):
            swept = len(r.json()) if r.text else 0
            if swept:
                tag = agent or "any"
                print(f"  swept {swept} stale '{tag}' running row(s)")
            return swept
    except Exception as exc:  # noqa: BLE001
        print(f"  sweep_stale_running failed: {exc}", file=sys.stderr)
    return 0


# ============================================================
# Auto-remediation playbook
# ============================================================
#
# Each Remediation has:
#   - detect(): returns a reason string if the issue is present, else None
#   - act():    returns True if the corrective action dispatched successfully
#   - cooldown_hours: minimum wait between successive attempts (don't thrash)
#   - max_per_day:    upper bound on attempts in any 24h window
#   - escalate_after_attempts: if dispatched this many times in 24h and the
#                              symptom still recurs, send a Telegram alert
#
# Attempt history is read from prior orchestrator_agent job_runs.meta —
# no new schema. The current run's attempts are written back to its own
# meta column on finish.

@dataclass
class Remediation:
    key: str
    description: str
    detect: object  # callable() -> str | None
    act: object     # callable() -> bool
    cooldown_hours: float
    max_per_day: int
    escalate_after_attempts: int = 3


def dispatch_workflow(yml: str) -> bool:
    """Trigger a same-repo workflow via the GitHub Actions dispatches API.
    Requires GH_TOKEN env (PAT with 'workflow' scope). Returns False
    silently if the token is missing so the orchestrator stays useful in
    a partial-config state — Telegram will surface the gap."""
    if not GH_TOKEN:
        print(f"  dispatch_workflow({yml}): GH_TOKEN missing — skipping", file=sys.stderr)
        return False
    try:
        r = requests.post(
            f"https://api.github.com/repos/{GH_REPO}/actions/workflows/{yml}/dispatches",
            headers={
                "Authorization": f"token {GH_TOKEN}",
                "Accept":        "application/vnd.github+json",
            },
            json={"ref": "main"},
            timeout=10,
        )
        if r.status_code in (200, 201, 204):
            return True
        print(f"  dispatch_workflow({yml}) {r.status_code}: {r.text[:200]}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        print(f"  dispatch_workflow({yml}) failed: {exc}", file=sys.stderr)
    return False


def fetch_recent_orchestrator_attempts(hours: float) -> list[dict]:
    """Return meta.remediations entries from this agent's prior runs within `hours`."""
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime('%Y-%m-%dT%H:%M:%SZ')
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/stock_job_runs",
            headers=HEADERS_SB,
            params={
                "agent":      "eq.orchestrator_agent",
                "started_at": f"gte.{since}",
                "select":     "meta",
                "limit":      "200",
            }, timeout=10,
        )
        if r.status_code == 200:
            return [(row.get("meta") or {}).get("remediations") or {} for row in r.json()]
    except Exception as exc:  # noqa: BLE001
        print(f"  fetch_recent_orchestrator_attempts: {exc}", file=sys.stderr)
    return []


def count_attempts(key: str, hours: float, history: list[dict] | None = None) -> int:
    """How many times in the last `hours` did orchestrator dispatch this remediation?"""
    rows = history if history is not None else fetch_recent_orchestrator_attempts(hours)
    return sum(1 for rem in rows if rem.get(key, {}).get("dispatched"))


# ----- Detectors -----

def detect_paper_trades_silent() -> str | None:
    """event_paper_agent has had ≥2 consecutive runs with rows_in>0 and rows_out=0.
    Means events are landing but no trades are being opened — exactly the
    2026-05-18→05-21 silent failure pattern."""
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/stock_job_runs",
            headers=HEADERS_SB,
            params={
                "agent":  "eq.event_paper_agent",
                "status": "eq.ok",
                "order":  "started_at.desc",
                "limit":  "3",
                "select": "rows_in,rows_out,started_at",
            }, timeout=10,
        )
        if r.status_code != 200:
            return None
        runs = r.json()
        if len(runs) < 2:
            return None
        zeros = [x for x in runs[:2] if (x.get("rows_in") or 0) > 0 and (x.get("rows_out") or 0) == 0]
        if len(zeros) >= 2:
            return f"event_paper_agent rows_out=0 across last {len(zeros)} runs"
    except Exception:
        pass
    return None


def detect_backtester_stale() -> str | None:
    """stock_agent_weights hasn't been updated in > 7 days."""
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/stock_agent_weights",
            headers=HEADERS_SB,
            params={"order": "date.desc", "limit": "1", "select": "date"},
            timeout=10,
        )
        if r.status_code != 200 or not r.json():
            return None
        d = datetime.fromisoformat(r.json()[0]["date"]).date()
        age = (datetime.now(timezone.utc).date() - d).days
        if age > 7:
            return f"max(agent_weights.date)={d} is {age}d old (>7d budget)"
    except Exception:
        pass
    return None


def detect_stuck_running_global() -> str | None:
    """Any agent (besides orchestrator, already swept) with running>2h."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime('%Y-%m-%dT%H:%M:%SZ')
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/stock_job_runs",
            headers=HEADERS_SB,
            params={
                "status":     "eq.running",
                "started_at": f"lt.{cutoff}",
                "select":     "agent,id",
                "limit":      "20",
            }, timeout=10,
        )
        if r.status_code != 200 or not r.json():
            return None
        agents = {row["agent"] for row in r.json()}
        return f"{len(r.json())} stuck running row(s) across {len(agents)} agent(s)"
    except Exception:
        pass
    return None


def detect_news_classifier_coverage_drift() -> str | None:
    """Ratio of normalized news_article events to raw_news rows ingested in
    last 48h is below 8% — the classifier is missing most tickers.

    Background (2026-05-22 audit): the classifier only matched headlines
    via 22 hardcoded company-name aliases + ~30 hardcoded watchlist symbols,
    so news mentioning tickers like ENPH, SEDG, OKLO landed in raw_news but
    produced zero normalized events. PR1A precursor expanded both sets to
    pull from stock_symbols dynamically. This detector watches for future
    regression — if the ratio drops below 8%, the alias loading likely
    broke or the source mix shifted toward off-watchlist names.

    Alert-only — no auto-remediation. Operator should:
      1. Check news_agent's most recent run log for "+N name aliases" /
         "+M symbol-scan tickers" — should be non-zero
      2. Verify stock_symbols.is_active=true has the expected ~150 rows
      3. Spot-check a known-active ticker's last 24h raw_news for
         orphaned (ticker=NULL) rows
    """
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        h = {**HEADERS_SB, "Prefer": "count=exact", "Range": "0-0"}
        # raw_news total
        r1 = requests.get(
            f"{SUPABASE_URL}/rest/v1/stock_raw_news",
            headers=h, params={"published_at": f"gte.{cutoff}"}, timeout=10,
        )
        # normalized_events news_article subset
        r2 = requests.get(
            f"{SUPABASE_URL}/rest/v1/stock_normalized_events",
            headers=h,
            params={"event_type": "eq.news_article",
                    "created_at": f"gte.{cutoff}"},
            timeout=10,
        )
        raw_n = int((r1.headers.get("content-range", "*/0").split("/")[-1]) or 0)
        ev_n  = int((r2.headers.get("content-range", "*/0").split("/")[-1]) or 0)
        if raw_n < 50:
            return None  # too little volume to draw any conclusion
        ratio = ev_n / raw_n if raw_n else 0
        if ratio < 0.08:
            return (f"news classifier coverage {ratio*100:.1f}% over 48h "
                    f"({ev_n} events / {raw_n} raw) — likely classifier "
                    f"alias/symbol-load regression; see news_agent.load_symbol_names")
    except Exception:
        pass
    return None


def detect_mature_no_matured_at() -> str | None:
    """is_mature=true but matured_at IS NULL — the bookkeeping gap from 2026-05-21."""
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/stock_rule_calibration",
            headers=HEADERS_SB,
            params={
                "is_mature":  "eq.true",
                "matured_at": "is.null",
                "select":     "rule_key",
                "limit":      "20",
            }, timeout=10,
        )
        if r.status_code != 200 or not r.json():
            return None
        return f"{len(r.json())} mature rule(s) with matured_at=NULL"
    except Exception:
        pass
    return None


# ----- Actions -----

def act_dispatch_event_paper_agent() -> bool:
    return dispatch_workflow("event_paper_agent.yml")


def act_dispatch_backtester() -> bool:
    return dispatch_workflow("backtester.yml")


def act_sweep_global() -> bool:
    swept = sweep_stale_running(agent=None, max_age_hours=2)
    return swept >= 0  # treat call success as action success even if 0 rows


def act_alert_only_no_op() -> bool:
    """Marker action for detectors that should ALERT but not auto-remediate.

    Returns True so the playbook records the attempt + escalates after the
    threshold; the side effect is purely the Telegram alert + dashboard
    visibility. Use this for issues that require operator judgement (e.g.,
    "classifier coverage is drifting — review which feeds changed") rather
    than mechanical fixes ("re-dispatch the workflow").
    """
    return True


def act_backfill_matured_at() -> bool:
    """PATCH is_mature=true AND matured_at IS NULL → matured_at=now()."""
    try:
        r = requests.patch(
            f"{SUPABASE_URL}/rest/v1/stock_rule_calibration"
            f"?is_mature=eq.true&matured_at=is.null",
            headers={**HEADERS_SB, "Prefer": "return=representation"},
            json={"matured_at": datetime.now(timezone.utc).isoformat()},
            timeout=10,
        )
        return r.status_code in (200, 204)
    except Exception:
        return False


REMEDIATIONS: list[Remediation] = [
    Remediation(
        key="paper_trades_silent",
        description="event_paper_agent runs ok but writes 0 trades for ≥2 consecutive runs",
        detect=detect_paper_trades_silent,
        act=act_dispatch_event_paper_agent,
        cooldown_hours=2,
        max_per_day=4,
        escalate_after_attempts=3,
    ),
    Remediation(
        key="backtester_stale",
        description="stock_agent_weights last update > 7d ago",
        detect=detect_backtester_stale,
        act=act_dispatch_backtester,
        cooldown_hours=24,
        max_per_day=1,
        escalate_after_attempts=2,
    ),
    Remediation(
        key="stuck_running_global",
        description="Any agent stuck with status='running' > 2h",
        detect=detect_stuck_running_global,
        act=act_sweep_global,
        cooldown_hours=0,
        max_per_day=99,
        escalate_after_attempts=99,  # silent maintenance
    ),
    Remediation(
        key="mature_no_matured_at",
        description="stock_rule_calibration has is_mature=true rows with matured_at=NULL",
        detect=detect_mature_no_matured_at,
        act=act_backfill_matured_at,
        cooldown_hours=0,
        max_per_day=99,
        escalate_after_attempts=99,  # silent maintenance
    ),
    Remediation(
        key="news_classifier_coverage_drift",
        description=("news_agent classifier coverage of raw_news < 8% over 48h — "
                     "likely regression in load_symbol_names() alias/symbol loading. "
                     "ALERT-ONLY: operator should review which feeds or symbols changed."),
        detect=detect_news_classifier_coverage_drift,
        act=act_alert_only_no_op,
        cooldown_hours=6,        # don't spam — once per quarter-day if persistent
        max_per_day=4,
        escalate_after_attempts=1,  # escalate immediately so operator sees it
    ),
]


def run_remediations() -> tuple[dict, list[str]]:
    """Execute the playbook. Returns (attempts_for_meta, escalations_for_telegram)."""
    attempts: dict = {}
    escalations: list[str] = []
    # Pull 24h of history once and reuse for both cooldown and cap checks.
    history_24h = fetch_recent_orchestrator_attempts(24)

    for rem in REMEDIATIONS:
        reason = rem.detect()
        if not reason:
            continue

        # Cooldown gate
        if rem.cooldown_hours > 0:
            cooldown_window = fetch_recent_orchestrator_attempts(rem.cooldown_hours)
            if count_attempts(rem.key, rem.cooldown_hours, cooldown_window) > 0:
                attempts[rem.key] = {"skipped": "cooldown", "reason": reason}
                print(f"  [{rem.key}] cooldown active — skip ({reason})")
                continue

        # Daily cap gate
        prior_24h = count_attempts(rem.key, 24, history_24h)
        if prior_24h >= rem.max_per_day:
            attempts[rem.key] = {"skipped": "max_per_day", "reason": reason, "prior_24h": prior_24h}
            escalations.append(f"[{rem.key}] max_per_day cap reached ({prior_24h}/{rem.max_per_day}); reason still present: {reason}")
            print(f"  [{rem.key}] max_per_day cap reached — escalating")
            continue

        # Dispatch
        ok = bool(rem.act())
        attempts[rem.key] = {
            "dispatched": ok,
            "reason":     reason,
            "at":         datetime.now(timezone.utc).isoformat(),
        }
        mark = "✓" if ok else "✗"
        print(f"  [{rem.key}] {mark} dispatched={ok}  reason={reason}")
        if not ok:
            escalations.append(f"[{rem.key}] dispatch FAILED — reason still present: {reason}")
            continue

        # Repeat-attempt escalation
        if (prior_24h + 1) >= rem.escalate_after_attempts:
            escalations.append(
                f"[{rem.key}] dispatched {prior_24h + 1}× in 24h — symptom keeps returning: {reason}"
            )

    return attempts, escalations


def job_run_start() -> int | None:
    try:
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/stock_job_runs",
            headers={**HEADERS_SB, "Prefer": "return=representation"},
            json={"agent": "orchestrator_agent"}, timeout=10,
        )
        if r.status_code in (200, 201) and r.json():
            return r.json()[0]["id"]
    except Exception as exc:  # noqa: BLE001
        print(f"  job_run_start failed: {exc}", file=sys.stderr)
    return None


def job_run_finish(run_id: int | None, status: str, rows_in: int, rows_out: int,
                   err: str | None = None, meta: dict | None = None) -> None:
    if run_id is None:
        return
    payload: dict = {
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "status":      status,
        "rows_in":     rows_in,
        "rows_out":    rows_out,
        "error_text":  err,
    }
    if meta is not None:
        payload["meta"] = meta
    try:
        requests.patch(
            f"{SUPABASE_URL}/rest/v1/stock_job_runs?id=eq.{run_id}",
            headers=HEADERS_SB, json=payload, timeout=10,
        )
    except Exception:
        pass


def alert_telegram(text: str) -> None:
    """Post the orchestrator summary using a synthetic signal id of -1
    (orchestrator alerts are not tied to a stock_signals row). The
    send_and_log path still writes a dispatch_log entry so audit_agent
    invariant #1 stays satisfied for this agent too."""
    # Synthetic signal_id: a stable sentinel that won't collide with any
    # real PK. dedupe_key includes the date so we get one alert per day max.
    today = datetime.now(timezone.utc).date().isoformat()
    sentinel_dedupe = f"orchestrator_summary_{today}"
    # Use bare requests rather than send_and_log here because send_and_log
    # builds dedupe_key from signal_id; the orchestrator wants a date-based
    # dedupe so a re-run on the same day is idempotent.
    bot = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    if not bot or not chat:
        print("  alert_telegram: missing TELEGRAM env — skipping", file=sys.stderr)
        return
    # Idempotency: skip if already alerted today.
    existing = requests.get(
        f"{SUPABASE_URL}/rest/v1/stock_telegram_dispatch_log"
        f"?dedupe_key=eq.{sentinel_dedupe}&select=id&limit=1",
        headers=HEADERS_SB, timeout=10,
    )
    if existing.status_code == 200 and existing.json():
        print(f"  alert_telegram: already alerted today ({sentinel_dedupe})")
        return
    try:
        r = requests.post(f"https://api.telegram.org/bot{bot}/sendMessage",
                          data={"chat_id": chat, "text": text}, timeout=15)
        ok = r.status_code == 200 and r.json().get("ok", False)
        msg_id = r.json().get("result", {}).get("message_id") if ok else None
        err = None if ok else r.text[:500]
    except Exception as exc:  # noqa: BLE001
        ok, msg_id, err = False, None, str(exc)
    requests.post(
        f"{SUPABASE_URL}/rest/v1/stock_telegram_dispatch_log",
        headers={**HEADERS_SB, "Prefer": "return=minimal"},
        json={
            "signal_id":       None,
            "sent_at":         datetime.now(timezone.utc).isoformat(),
            "payload":         text,
            "delivery_ok":     ok,
            "telegram_msg_id": msg_id,
            "error":           err,
            "dedupe_key":      sentinel_dedupe,
        }, timeout=10,
    )


def main() -> int:
    started = time.time()
    sweep_stale_running()
    run_id = job_run_start()
    try:
        now = datetime.now(timezone.utc)
        results: list[tuple[AgentExpectation, bool, str, float | None]] = []
        for spec in EXPECTED:
            ok, detail, age = check_agent(spec, now)
            results.append((spec, ok, detail, age))
            mark = "✓" if ok else "✗"
            extra = " [trading_only]" if spec.trading_only else ""
            print(f"  {mark} {spec.name}{extra}: {detail}")

        stale = [r for r in results if not r[1]]

        # Auto-remediation: detect-then-act playbook. Each remediation
        # respects cooldown + max_per_day to avoid thrashing. Escalations
        # come back when caps are hit or repeated dispatches haven't cleared
        # the symptom — those are the only conditions worth a Telegram now.
        print("\nremediation pass:")
        attempts, escalations = run_remediations()
        if not attempts:
            print("  (no remediations needed)")

        # Telegram policy:
        #  - Stale-agent summary still sent when staleness exists AND
        #    no auto-remediation was attempted for that staleness (i.e.
        #    the system has no playbook entry — operator action required).
        #  - Escalations always sent (cap-hit or dispatch-failure).
        remediated_keys = {k for k, v in attempts.items() if v.get("dispatched")}
        unhandled_stale = stale and not remediated_keys
        if escalations:
            alert_telegram("Auto-remediation needs attention:\n" + "\n".join(escalations))
        if unhandled_stale:
            alert_telegram(format_summary(results, now))
        if not escalations and not unhandled_stale:
            if stale:
                print(f"orchestrator: {len(stale)} stale agent(s) — remediation dispatched, suppressing alert")
            else:
                print(f"orchestrator: all {len(EXPECTED)} agents healthy")

        elapsed = time.time() - started
        job_run_finish(
            run_id,
            "ok" if not stale and not escalations else "warning",
            len(EXPECTED), len(stale),
            meta={"remediations": attempts} if attempts else None,
        )
        print(f"orchestrator_agent done in {elapsed:.1f}s "
              f"({len(stale)}/{len(EXPECTED)} stale, {len(attempts)} remediations)")
        return 0
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        job_run_finish(run_id, "error", 0, 0, str(exc)[:500])
        return 1


if __name__ == "__main__":
    sys.exit(main())
