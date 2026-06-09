"""_metalabel_gate — the Layer-2.b precision gate decision + walk-forward read.

Shared by the offline validator (scripts/validate_metalabel_gate.py) and, in
PR-C, the live thesis_agent emit path — SAME function both places so the
validated decision is exactly the one that ships.

The gate's job: given a candidate's PRIMARY (rule_key, horizon) cell, decide
ACT (emit as actionable) vs WATCH (emit at the lower non-actionable tier).
Suppression is the dangerous operation, so the policy is conservative and
FAIL-OPEN — an uncalibrated cell goes to WATCH (still emitted, still paper-
traded by event_paper_agent, so its calibration keeps maturing), never dropped.

Policy (Codex-reviewed):
  n >= min_n AND pf >= pf_bar AND expectancy > 0 -> ("act",  "calibrated_profitable")
  n >= min_n AND not profitable                  -> ("watch","suppressed_low_pf")
  n <  min_n  OR stats missing                   -> ("watch","fail_open_thin")

Walk-forward (leakage purge): a candidate at run_at may only count trades whose
outcome was KNOWN (realized_at) strictly before run_at. This excludes future
regime information AND the candidate's own not-yet-closed trade (it closes
after run_at), so the offline validation can't cheat.
"""
from __future__ import annotations

from datetime import datetime

# Defaults: a SUPPRESSIVE gate needs more confidence than the display tables.
DEFAULT_PF_BAR = 1.5
DEFAULT_MIN_N = 100

# The exhaustive set of reasons gate_decision can return. Consumers key their
# tallies off this so they can't drift from the decision logic (a harness that
# hard-coded "act" instead of "calibrated_profitable" KeyError'd live).
GATE_REASONS = ("calibrated_profitable", "suppressed_low_pf", "fail_open_thin")


def expectancy_stats(returns: list[float], correct: list[bool]) -> dict:
    """n, win-rate, profit_factor, expectancy (mean return) from raw outcomes.
    Mirrors scripts/calibrate_emit_floor.expectancy_stats."""
    n = len(returns)
    wins = [x for x in returns if x > 0]
    losses = [x for x in returns if x < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf") if gross_win > 0 else 0.0
    win_rate = (sum(1 for c in correct if c) / n) if n else 0.0
    expectancy = (sum(returns) / n) if n else 0.0
    return {"n": n, "win_rate": win_rate, "pf": pf, "expectancy": expectancy}


def _parse(ts) -> datetime | None:
    if isinstance(ts, datetime):
        return ts
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def walkforward_stats(trades: list[dict], rule_key: str, as_of: datetime) -> dict:
    """Expectancy stats for `rule_key` using ONLY trades whose outcome was KNOWN
    to the system strictly before `as_of` (walk-forward / leakage-purged).

    A trade counts only if BOTH:
      * exit_at    < as_of  — it had closed (outcome existed), and
      * created_at < as_of  — its ROW existed in the DB.
    The created_at guard is essential because this pipeline BACKFILLS paper
    trades: a backfilled row has a historical exit_at but a recent created_at
    and was NOT in calibration at as_of — exit_at alone would leak it
    (Codex review). It also reinforces self-exclusion of the candidate's own
    trade (which both closes and, if backfilled, lands after as_of).

    trades: rows with {rule_key, realized_return, correct, exit_at, created_at}.
    (exit_at is stock_event_paper_trades' close timestamp; there is no
    realized_at column on that table.)
    Missing/unparseable exit_at -> excluded (can't prove it predates as_of).
    created_at is enforced only when present.
    """
    returns: list[float] = []
    correct: list[bool] = []
    for t in trades:
        if t.get("rule_key") != rule_key:
            continue
        xa = _parse(t.get("exit_at"))
        if xa is None or xa >= as_of:
            continue
        ca = _parse(t.get("created_at"))
        if ca is not None and ca >= as_of:
            continue
        rr = t.get("realized_return")
        if rr is None:
            continue
        returns.append(float(rr))
        correct.append(bool(t.get("correct")))
    return expectancy_stats(returns, correct)


def gate_decision(stats: dict | None, *, pf_bar: float = DEFAULT_PF_BAR,
                  min_n: int = DEFAULT_MIN_N) -> tuple[str, str]:
    """(action, reason) for one (rule_key, horizon) cell. See module policy."""
    if not stats or int(stats.get("n") or 0) < min_n:
        return "watch", "fail_open_thin"
    pf = float(stats.get("pf") or 0.0)
    expectancy = float(stats.get("expectancy") or 0.0)
    if pf >= pf_bar and expectancy > 0:
        return "act", "calibrated_profitable"
    return "watch", "suppressed_low_pf"
