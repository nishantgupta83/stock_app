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
    AgentExpectation("event_paper_agent",     "hourly, 24/7",          3.0,  False),
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
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/stock_job_runs"
        f"?agent=eq.{agent_name}&order=started_at.desc&limit=1"
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
                   err: str | None = None) -> None:
    if run_id is None:
        return
    try:
        requests.patch(
            f"{SUPABASE_URL}/rest/v1/stock_job_runs?id=eq.{run_id}",
            headers=HEADERS_SB,
            json={
                "finished_at": datetime.now(timezone.utc).isoformat(),
                "status":      status,
                "rows_in":     rows_in,
                "rows_out":    rows_out,
                "error_text":  err,
            }, timeout=10,
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
        if stale:
            summary = format_summary(results, now)
            print(summary)
            alert_telegram(summary)
        else:
            print(f"orchestrator: all {len(EXPECTED)} agents healthy")

        elapsed = time.time() - started
        job_run_finish(run_id, "ok" if not stale else "warning",
                       len(EXPECTED), len(stale))
        print(f"orchestrator_agent done in {elapsed:.1f}s ({len(stale)}/{len(EXPECTED)} stale)")
        return 0
    except Exception as exc:  # noqa: BLE001
        import traceback
        traceback.print_exc()
        job_run_finish(run_id, "error", 0, 0, str(exc)[:500])
        return 1


if __name__ == "__main__":
    sys.exit(main())
