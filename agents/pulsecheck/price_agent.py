"""pulsecheck_price_agent — reconciliation health for the EOD learning loop.

OWNS:
  * recent_runs              — price_agent fires at expected cadence
  * reconcile_skip_rate      — % of trades silently skipped due to missing bars
  * orphaned_signals         — signals stuck in status_v2='sent' past audit window
  * stuck_h1d_backlog        — h1d trades that should have closed but haven't

DOES NOT OWN:
  * Whether thesis is emitting (pulsecheck_thesis)
  * Whether new paper trades are being opened (pulsecheck_event_paper)
  * Whether bars are landing in stock_raw_prices (pulsecheck_foundation.recent_bars)

The reconcile_skip_rate + orphaned_signals checks are designed to catch the
513-stuck-h1d failure mode that hid for days because the prior price_agent
had no skip counter. With reconcile_meta.n_skipped_no_bars now written to
job_runs.meta, this pulsecheck flags the regression on day one.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pulsecheck._pulse import Check, CheckResult, run_checks, sb_get, sb_count


AGENT = "pulsecheck_price_agent"
# After the 2026-06-02 cron bump (0 */2 * * 1-5) we expect ~6 runs/24h on
# Mon-Fri (weekdays only). Threshold of 3 tolerates one or two GHA cron drops.
RUNS_PER_24H_FLOOR = 3
SKIP_RATE_WARN     = 0.05      # 5% silent-drop rate = early alert
SKIP_RATE_CRIT     = 0.20
# Volume guard: the skip RATE is volume-sensitive, so a handful of genuinely
# pending trades (e.g. weekend exits awaiting Monday's not-yet-ingested EOD bar)
# can spike the rate to >20% when total reconcile volume is also small. Require
# an absolute skip count before alarming. Set well above a weekend batch but far
# below the 513-stuck-h1d incident, so real regressions still fire.
MIN_SKIP_ABS       = 20
ORPHAN_SIGNAL_AGE_DAYS  = 30   # h1d signals stuck in 'sent' for 30+ days = stuck
ORPHAN_SIGNAL_WARN      = 50
STUCK_H1D_WARN     = 50
STUCK_H1D_CRIT     = 200


def _now() -> datetime: return datetime.now(timezone.utc)


def recent_runs() -> CheckResult:
    """price_agent ran at expected weekday cadence in the last 24h."""
    # Weekend tolerance: on Sat/Sun, the most recent weekday run may be
    # ~48h old. Don't warn during those windows.
    now = _now()
    if now.weekday() >= 5:  # Sat/Sun
        return CheckResult("ok", "weekend — runs paused by design")
    since = (now - timedelta(hours=24)).isoformat()
    n = sb_count("stock_job_runs", {
        "agent":      "eq.price_agent",
        "started_at": f"gte.{since}",
    })
    status = "ok" if n >= RUNS_PER_24H_FLOOR else "warning"
    return CheckResult(status, f"{n} runs in last 24h", observed=float(n),
                       threshold=float(RUNS_PER_24H_FLOOR))


def classify_skip_rate(closed: int, no_bars: int, no_outcome: int) -> tuple[str, float]:
    """Severity from reconcile skip counts, with an absolute-skip volume guard.

    Returns (status, rate). A non-zero rate below MIN_SKIP_ABS is held at 'ok'
    so a small batch of pending trades can't trip a false CRITICAL at low
    volume; the 513-stuck regression (large absolute count) still escalates.
    """
    skipped = no_bars + no_outcome
    total = closed + skipped
    if total == 0:
        return "ok", 0.0
    rate = skipped / total
    # The absolute floor gates ONLY the CRITICAL page: a few pending trades at
    # low volume (e.g. Monday-morning weekend exits awaiting that day's EOD bar)
    # must not page. But a high rate still WARNS regardless of count, so a small
    # PERSISTENT failure stays visible — floor-before-rate would silence a
    # 100%-skip low-volume failure entirely (Codex review).
    if rate >= SKIP_RATE_CRIT and skipped >= MIN_SKIP_ABS:
        return "critical", rate
    if rate >= SKIP_RATE_WARN:
        return "warning", rate
    return "ok", rate


def reconcile_skip_rate() -> CheckResult:
    """Did the most recent reconcile silently skip a significant fraction?

    Reads stock_job_runs.meta.reconcile.{n_closed,n_skipped_no_bars,n_skipped_no_outcome}
    from the latest price_agent run. The skip rate = skipped / (closed + skipped).
    A non-zero rate is expected (delisted tickers, true data gaps); but a high
    rate indicates either yfinance is broken OR our stock_raw_prices coverage
    has gaps the fallback can't recover.
    """
    rows = sb_get("stock_job_runs", {
        "agent":      "eq.price_agent",
        "order":      "started_at.desc",
        "limit":      "1",
        "select":     "started_at,status,meta",
    })
    if not rows:
        return CheckResult("warning", "no price_agent runs found", observed=0.0)
    meta = (rows[0].get("meta") or {})
    rec = (meta.get("reconcile") or {}) if isinstance(meta, dict) else {}
    if not rec:
        # No reconcile metadata at all — either it's a fresh run from before
        # the 2026-06-02 instrumentation, or reconcile didn't run.
        return CheckResult("ok", "latest run predates reconcile instrumentation",
                           meta={"started_at": rows[0].get("started_at")})
    closed = int(rec.get("n_closed") or 0)
    no_bars = int(rec.get("n_skipped_no_bars") or 0)
    no_outcome = int(rec.get("n_skipped_no_outcome") or 0)
    skipped = no_bars + no_outcome
    if closed + skipped == 0:
        return CheckResult("ok", "no trades needed reconcile this run")
    status, rate = classify_skip_rate(closed, no_bars, no_outcome)
    guarded = " [below abs floor]" if skipped < MIN_SKIP_ABS else ""
    return CheckResult(
        status,
        f"latest run: closed={closed} skipped_no_bars={no_bars} "
        f"skipped_no_outcome={no_outcome} ({rate:.1%}){guarded}",
        observed=rate,
        threshold=SKIP_RATE_WARN,
        meta={"started_at": rows[0].get("started_at"),
              "sample_tickers": (rec.get("skipped_tickers") or [])[:8]},
    )


def stuck_h1d_backlog() -> CheckResult:
    """h1d trades opened 2+ trading days ago should not still be open."""
    cutoff = (_now() - timedelta(days=2)).date().isoformat()
    n = sb_count("stock_event_paper_trades", {
        "horizon_days": "eq.1",
        "entry_at":     f"lt.{cutoff}T00:00:00Z",
        "status":       "eq.open",
    })
    if n >= STUCK_H1D_CRIT:
        status = "critical"
    elif n >= STUCK_H1D_WARN:
        status = "warning"
    else:
        status = "ok"
    return CheckResult(
        status,
        f"stuck h1d trades: {n}",
        observed=float(n),
        threshold=float(STUCK_H1D_WARN),
    )


def orphaned_signals() -> CheckResult:
    """Signals stuck in status_v2='sent' that should have been audited.

    A signal lives in 'sent' status until price_agent closes it via the
    forecast_audit + close_signal path. If price_agent's signal-audit code
    path silently drops bars (mirroring the paper-trade bug), signals
    accumulate forever in 'sent'. Threshold uses h=1 since most signals
    have horizon_days=1 (per thesis_agent.horizon_for).
    """
    cutoff = (_now() - timedelta(days=ORPHAN_SIGNAL_AGE_DAYS)).isoformat()
    n = sb_count("stock_signals", {
        "fired_at":  f"lt.{cutoff}",
        "status_v2": "eq.sent",
    })
    status = "ok" if n < ORPHAN_SIGNAL_WARN else "warning"
    return CheckResult(
        status,
        f"signals stuck in 'sent' >{ORPHAN_SIGNAL_AGE_DAYS}d: {n}",
        observed=float(n),
        threshold=float(ORPHAN_SIGNAL_WARN),
    )


WINRATE_DRIFT_THRESHOLD     = 0.25   # lifetime − recent winrate gap that warns
WINRATE_DRIFT_MIN_RECENT_N  = 10     # need enough recent closes to trust the gap


def classify_winrate_drift(lifetime_acc, acc_30d, n_closed_30d, *,
                           threshold: float = WINRATE_DRIFT_THRESHOLD,
                           min_recent_n: int = WINRATE_DRIFT_MIN_RECENT_N) -> tuple[str, str]:
    """Flag a rule whose RECENT winrate has fallen far below its lifetime —
    the regime-break pattern (e.g. news_article:neutral:h7d, lifetime ~52% but
    recent 14%) that erodes calibration before the PR-C recent-window gate lands.
    Monitor only — does NOT change behavior."""
    if lifetime_acc is None or acc_30d is None or (n_closed_30d or 0) < min_recent_n:
        return "ok", "insufficient recent closes"
    drift = float(lifetime_acc) - float(acc_30d)   # +ve = recent worse
    if drift > threshold:
        return "warning", f"recent {acc_30d:.0%} vs lifetime {lifetime_acc:.0%} (−{drift*100:.0f}pts, n_30d={n_closed_30d})"
    return "ok", f"recent {acc_30d:.0%} ≈ lifetime {lifetime_acc:.0%}"


def calibration_drift() -> CheckResult:
    """Surface rules whose trailing-30d winrate diverges sharply from lifetime
    (uses calibration's accuracy_30d vs accuracy — written exactly for this)."""
    rows = sb_get("stock_rule_calibration", {
        "select": "rule_key,accuracy,accuracy_30d,n_closed_30d,n_observations",
        "order":  "n_observations.desc",
        "limit":  "500",
    })
    drifting = []
    for r in rows or []:
        status, detail = classify_winrate_drift(
            r.get("accuracy"), r.get("accuracy_30d"), r.get("n_closed_30d"))
        if status == "warning":
            drifting.append(f"{r.get('rule_key')}: {detail}")
    status = "warning" if drifting else "ok"
    return CheckResult(
        status,
        (f"{len(drifting)} rule(s) with >{WINRATE_DRIFT_THRESHOLD:.0%} recent winrate drop"
         if drifting else "no sharp recent winrate drops"),
        observed=float(len(drifting)),
        threshold=1.0,
        meta={"drifting": drifting[:10]},
    )


CHECKS = [
    Check("recent_runs",          recent_runs,          depends_on=["pulsecheck_foundation"]),
    Check("reconcile_skip_rate",  reconcile_skip_rate,  depends_on=["pulsecheck_foundation"]),
    Check("stuck_h1d_backlog",    stuck_h1d_backlog,    depends_on=["pulsecheck_foundation"]),
    Check("orphaned_signals",     orphaned_signals,     depends_on=["pulsecheck_foundation"]),
    Check("calibration_drift",    calibration_drift,    depends_on=["pulsecheck_foundation"]),
]


def main() -> int:
    run_checks(AGENT, CHECKS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
