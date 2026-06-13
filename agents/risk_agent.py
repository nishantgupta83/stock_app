"""
risk_agent — Capital allocation layer.

LAYER BOUNDARY:
  Input:  stock_trade_setups
  Output: stock_risk_decisions  (only writer)
  Reads:  stock_event_paper_trades (for drawdown + daily-risk-in-flight)
          stock_rule_calibration (for maturity-tier weighting)
  Never:  writes to setups, signals, or any layer above.

This is the survival layer. A solopreneur pipeline cannot afford a single
catastrophic Black Swan to wipe out savings. The rules below are HARDCODED
(not configurable from upstream) so a bug or stray signal can never
bypass them.

Position Size formula (Van Tharp / Tharpe):
    risk_dollars  = NAV × RISK_PER_TRADE_PCT × maturity_multiplier
    size_dollars  = risk_dollars / stop_distance_pct
    size_pct_nav  = size_dollars / NAV
    max_loss_dollars = risk_dollars (by construction; the stop guarantees it)

Maturity multiplier discounts position size for rules that haven't proven
themselves yet — a 0.5x for training-mature, 0.25x for immature.

Hardcoded survival rules, in evaluation order:
  1. Setup self-skipped (setup.reason_to_skip non-null) → skip
  2. Confidence floor (setup.confidence < CONFIDENCE_FLOOR) → skip
  3. Drawdown circuit breaker (last-30d realized losses ≥ MAX_DRAWDOWN_PCT) → skip all
  4. Daily risk budget (sum of today's max_loss ≥ MAX_DAILY_RISK_PCT × NAV) → skip
  5. Sector concentration (≥ MAX_SAME_RULE_OPEN_TRADES on same rule_key) → skip
  6. Stop-distance sanity (stop_pct must be in (0, 0.20]) → skip
  Otherwise → size with maturity-multiplier-weighted Van Tharp formula.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone, timedelta
from typing import Optional

import requests

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]

HEADERS_SB = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
}

# ============================================================================
# HARDCODED RISK CONSTANTS — DO NOT make these read-from-DB or env-var.
# These are the survival rules; a misconfigured env should never relax them.
# ============================================================================
PORTFOLIO_NAV_BASELINE = 100_000.0      # hypothetical NAV for size_dollars calc
RISK_PER_TRADE_PCT     = 0.01           # 1% of NAV at risk per trade (Van Tharp standard)
MAX_DAILY_RISK_PCT     = 0.03           # 3% of NAV across all today's open setups
MAX_DRAWDOWN_PCT       = 0.10           # 10% peak-to-trough halts new sizes
CONFIDENCE_FLOOR       = 0.30           # below this, skip regardless of size
MAX_SAME_RULE_OPEN     = 3              # concentration cap per rule_key
STOP_PCT_MIN           = 0.005          # 0.5% — tighter is execution noise
STOP_PCT_MAX           = 0.20           # 20% — wider isn't a stop, it's a wish

MATURITY_MULTIPLIER = {
    # v1 canonical tier names (2026-05-26 stage-gate plan).
    # Stricter gates than the legacy ones — see scripts/learning_snapshot.py:TIER_GATES.
    "adult":        1.00,    # n ≥ 100, PF ≥ 2.0, mean_realized ≥ 0.5% (canonical, NO acc floor)
    "young_adult":  0.75,    # acc ≥ 0.80, n ≥ 30, PF > 1.2
    "teen":         0.50,    # acc ≥ 0.70, n ≥ 30, mean_realized > 0
    "child":        0.25,    # everything else
    # Legacy aliases — preserved for one release in case price_agent
    # and risk_agent ever deploy out-of-sync. Remove after a green week.
    "production":   1.00,
    "training":     0.50,
    "immature":     0.25,
}

SETUP_AGE_FLOOR_DAYS = 14   # sanity belt on fetch_recent_setups (B2)


def sb_get(path: str, params: dict) -> list[dict]:
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{path}",
                     headers=HEADERS_SB, params=params, timeout=20)
    if r.status_code != 200:
        print(f"  GET {path} {r.status_code}: {r.text[:200]}", file=sys.stderr)
        return []
    return r.json()


def job_run_start() -> int | None:
    try:
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/stock_job_runs",
            headers={**HEADERS_SB, "Prefer": "return=representation"},
            json={"agent": "risk_agent"}, timeout=10,
        )
        if r.status_code in (200, 201) and r.json():
            return r.json()[0]["id"]
    except Exception as exc:
        print(f"  job_run_start failed: {exc}", file=sys.stderr)
    return None


def job_run_finish(run_id: int | None, status: str,
                   rows_in: int, rows_out: int, err: str | None = None) -> None:
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


def fetch_recent_setups() -> list[dict]:
    """Setups still inside their alpha window — valid_until > now().

    Pre-B2 this filtered on `created_at gte (now - 24h)`, which
    silently dropped activist 13D and similar long-horizon signals whose
    valid_until extends up to 14 days. A setup the risk_agent had to skip
    yesterday because the daily risk budget was tapped would never get
    reconsidered today — even though the alpha window was still open.

    Bounding by `valid_until` makes the lookback automatically match each
    setup's intended horizon. Dedupe vs. already-decided setups is handled by
    fetch_existing_decision_setup_ids further down — a setup that's already
    been sized/skipped won't get a second decision regardless of valid_until.

    The 14-day created_at floor is a sanity belt: any setup older than that
    is either a leftover from a bad migration or someone mis-set valid_until.
    Better to drop it than to act on it.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    floor_iso = (datetime.now(timezone.utc) - timedelta(days=SETUP_AGE_FLOOR_DAYS)).isoformat()
    return sb_get("stock_trade_setups", {
        "valid_until": f"gte.{now_iso}",
        "created_at":  f"gte.{floor_iso}",
        "select":      "id,signal_id,ticker,direction,setup_type,stop_pct,target_pct,"
                       "horizon_days,confidence,reason_to_skip,rule_key,valid_until,created_at",
        "order":       "created_at.desc",
        "limit":       "500",
    })


def fetch_existing_decision_setup_ids(setup_ids: list[int]) -> set[int]:
    if not setup_ids:
        return set()
    in_list = ",".join(str(i) for i in setup_ids)
    rows = sb_get("stock_risk_decisions", {
        "setup_id": f"in.({in_list})",
        "select":   "setup_id",
    })
    return {r["setup_id"] for r in rows}


def fetch_calibration_for_rule_keys(rule_keys: list[str]) -> dict[str, dict]:
    """Lookup tier + payoff fields needed for maturity-multiplier.

    Selects the stored tier column from sql/0031 plus the legacy is_mature
    flag (which still gates BUY/SELL vocabulary in thesis_agent) and the
    fields needed for live-fallback tier computation if tier is NULL.
    """
    if not rule_keys:
        return {}
    in_list = ",".join(f'"{k}"' for k in rule_keys)
    rows = sb_get("stock_rule_calibration", {
        "rule_key": f"in.({in_list})",
        "select":   "rule_key,is_mature,is_mature_70,is_mature_80,tier,"
                    "accuracy,n_observations,profit_factor,mean_realized_pct",
    })
    return {r["rule_key"]: r for r in rows}


def maturity_tier(rule_cal: dict | None) -> str:
    """Map rule calibration → tier label.

    Prefers the stored `tier` column (populated by price_agent.upsert_calibration
    post-Phase 3, plus the backfill from sql/0031). Falls back to live
    computation using v1 gates for any row where tier is NULL — that's the
    safety net for the brief window after the migration applies but before
    the first EOD reconcile writes a tier value, and for any future schema
    that pushes a row without the tier column.

    Fallback gates MUST match the canonical definitions in price_agent
    (ADULT_MIN_N/PF/MEAN) — the adult tier is payoff-first with NO accuracy
    floor (a high-accuracy / negative-expectancy rule must NOT size). Pre-
    2026-06-09 this fallback used the OLD acc≥0.90/n≥30/PF>1.5 adult gate, a
    3rd divergent copy that would have sized a rule the canonical gate rejects.
      adult       : n ≥ 100  AND  PF ≥ 2.0   AND  mean_realized_pct ≥ 0.5%
      young_adult : n ≥ 30   AND  acc ≥ 0.80  AND  profit_factor > 1.2
      teen        : n ≥ 30   AND  acc ≥ 0.70  AND  mean_realized_pct > 0
      child       : everything else
    """
    if not rule_cal:
        return "child"
    stored = rule_cal.get("tier")
    if stored in ("adult", "young_adult", "teen", "child"):
        return stored
    acc = float(rule_cal.get("accuracy") or 0)
    n = int(rule_cal.get("n_observations") or 0)
    pf_raw = rule_cal.get("profit_factor")
    mr_raw = rule_cal.get("mean_realized_pct")
    pf = float(pf_raw) if pf_raw is not None else None
    mr = float(mr_raw) if mr_raw is not None else None
    # adult: canonical payoff-first gate (price_agent ADULT_MIN_N/PF/MEAN), no acc floor
    if n >= 100 and pf is not None and pf >= 2.0 and mr is not None and mr >= 0.005:
        return "adult"
    if n >= 30 and acc >= 0.80 and pf is not None and pf > 1.2:
        return "young_adult"
    if n >= 30 and acc >= 0.70 and mr is not None and mr > 0:
        return "teen"
    return "child"


def compute_equity_curve_drawdown(closed_trades: list[dict],
                                   risk_per_trade_pct: float = None) -> dict:
    """Cumulative equity-curve max drawdown over the supplied closed trades,
    scaled into NAV-equivalent units.

    realized_return is a per-TRADE percentage. To map it onto portfolio NAV
    drawdown, each trade's contribution is scaled by RISK_PER_TRADE_PCT —
    the Van Tharp risk-per-trade fraction the position would have used.
    Equivalent to assuming each paper trade represents one R-multiple
    sized at the survival-rule's per-trade risk budget. Without this
    scaling, summing per-trade percentages over N trades produces a number
    that grows unboundedly with N and shares no units with MAX_DRAWDOWN_PCT
    (the NAV-fraction threshold).

    Trades must be sorted by exit_at ascending. Drawdown is peak-to-trough
    of the cumulative NAV-equivalent curve.

    Returns dict with drawdown_pct (≤ 0, NAV fraction), sum_return_nav,
    peak_cumulative, n.
    """
    if risk_per_trade_pct is None:
        risk_per_trade_pct = RISK_PER_TRADE_PCT
    cumulative = 0.0
    running_peak = 0.0
    max_dd = 0.0
    for t in closed_trades:
        r = float(t.get("realized_return") or 0)
        # Scale per-trade return → NAV-equivalent return.
        cumulative += r * risk_per_trade_pct
        if cumulative > running_peak:
            running_peak = cumulative
        dd = cumulative - running_peak
        if dd < max_dd:
            max_dd = dd
    return {
        "drawdown_pct":     round(max_dd, 6),
        "sum_return_nav":   round(cumulative, 6),
        "peak_cumulative":  round(running_peak, 6),
        "n":                len(closed_trades),
    }


def compute_portfolio_state() -> dict:
    """Snapshot of risk-budget state used for sizing decisions.

    Returns:
      drawdown_pct: equity-curve max drawdown over last 30 days (≤ 0)
      sum_return_30d: cumulative realized return over the window
      n_closed_30d: number of closed trades contributing
      daily_risk_in_flight_pct: today's already-sized max_loss / NAV
      open_per_rule: {rule_key → count of currently-open paper trades}
    """
    state = {
        "drawdown_pct": 0.0,
        "sum_return_30d": 0.0,
        "n_closed_30d": 0,
        "daily_risk_in_flight_pct": 0.0,
        "open_per_rule": {},
        "computed_at": datetime.now(timezone.utc).isoformat(),
    }

    # Equity-curve max drawdown over the last 30 days of closed trades.
    # Sorted ascending by exit_at so the curve is built in chronological order.
    cutoff_30 = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    closed = sb_get("stock_event_paper_trades", {
        "status":  "eq.closed",
        "exit_at": f"gte.{cutoff_30}",
        "select":  "realized_return,exit_at",
        "order":   "exit_at.asc",
        "limit":   "5000",
    })
    if closed:
        dd = compute_equity_curve_drawdown(closed)
        state["drawdown_pct"]   = dd["drawdown_pct"]
        state["sum_return_30d"] = dd["sum_return_nav"]
        state["n_closed_30d"]   = dd["n"]

    # Daily risk in flight: sum max_loss_dollars from today's sized decisions.
    today_iso = datetime.now(timezone.utc).date().isoformat()
    today_decisions = sb_get("stock_risk_decisions", {
        "created_at": f"gte.{today_iso}T00:00:00Z",
        "decision":   "eq.size",
        "select":     "max_loss_dollars",
    })
    in_flight = sum(float(d.get("max_loss_dollars") or 0) for d in today_decisions)
    state["daily_risk_in_flight_pct"] = round(in_flight / PORTFOLIO_NAV_BASELINE, 6)

    # Open-per-rule concentration (using open paper trades as a proxy).
    open_trades = sb_get("stock_event_paper_trades", {
        "status": "eq.open",
        "select": "rule_key",
        "limit":  "2000",
    })
    open_per_rule: dict[str, int] = {}
    for t in open_trades:
        rk = t.get("rule_key") or ""
        if rk:
            open_per_rule[rk] = open_per_rule.get(rk, 0) + 1
    state["open_per_rule"] = open_per_rule

    return state


def evaluate_setup(setup: dict, cal: dict[str, dict], state: dict) -> dict:
    """Apply hardcoded rules in order; return a stock_risk_decisions row dict.

    `rules_applied` accumulates the audit trail so an operator can see exactly
    why a setup was sized or skipped.
    """
    rules_applied: list[dict] = []

    def rule(name: str, passed: bool, detail: str) -> bool:
        rules_applied.append({"rule": name, "passed": passed, "detail": detail})
        return passed

    # 1. Setup self-skipped
    if setup.get("reason_to_skip"):
        rule("setup_self_skip", False, setup["reason_to_skip"])
        return _skip_decision(setup, f"setup self-skipped: {setup['reason_to_skip']}", rules_applied, state)

    # 2. Confidence floor
    conf = float(setup.get("confidence") or 0)
    if not rule("confidence_floor",
                conf >= CONFIDENCE_FLOOR,
                f"confidence={conf:.2f} vs floor={CONFIDENCE_FLOOR}"):
        return _skip_decision(setup, f"confidence {conf:.2f} below floor {CONFIDENCE_FLOOR}",
                              rules_applied, state)

    # 3. Drawdown circuit breaker
    dd = float(state.get("drawdown_pct") or 0)
    if not rule("drawdown_circuit_breaker",
                dd > -MAX_DRAWDOWN_PCT,
                f"30d equity-curve max DD={dd:.4f} vs threshold={-MAX_DRAWDOWN_PCT}"):
        return _skip_decision(setup, f"drawdown circuit breaker (30d max DD {dd:.4f} ≤ {-MAX_DRAWDOWN_PCT})",
                              rules_applied, state)

    # 4. Daily risk budget
    in_flight = float(state.get("daily_risk_in_flight_pct") or 0)
    if not rule("daily_risk_budget",
                in_flight < MAX_DAILY_RISK_PCT,
                f"in_flight_pct={in_flight:.4f} vs cap={MAX_DAILY_RISK_PCT}"):
        return _skip_decision(setup,
                              f"daily risk budget exhausted ({in_flight*100:.2f}% in flight ≥ {MAX_DAILY_RISK_PCT*100:.1f}%)",
                              rules_applied, state)

    # 5. Sector / rule concentration
    rule_key = setup.get("rule_key") or ""
    open_count = int(state.get("open_per_rule", {}).get(rule_key, 0))
    if not rule("rule_concentration",
                open_count < MAX_SAME_RULE_OPEN,
                f"open_on_rule[{rule_key}]={open_count} vs cap={MAX_SAME_RULE_OPEN}"):
        return _skip_decision(setup,
                              f"too many open on rule {rule_key} ({open_count} ≥ {MAX_SAME_RULE_OPEN})",
                              rules_applied, state)

    # 6. Stop-distance sanity
    stop_pct = float(setup.get("stop_pct") or 0)
    if not rule("stop_sanity",
                STOP_PCT_MIN <= stop_pct <= STOP_PCT_MAX,
                f"stop_pct={stop_pct} must be in [{STOP_PCT_MIN}, {STOP_PCT_MAX}]"):
        return _skip_decision(setup,
                              f"stop_pct {stop_pct} outside sanity band",
                              rules_applied, state)

    # All gates passed — size the position.
    tier = maturity_tier(cal.get(rule_key))
    mult = MATURITY_MULTIPLIER[tier]
    rule("maturity_weight", True, f"tier={tier} → multiplier={mult}")

    risk_dollars = PORTFOLIO_NAV_BASELINE * RISK_PER_TRADE_PCT * mult
    size_dollars = risk_dollars / stop_pct
    size_pct = size_dollars / PORTFOLIO_NAV_BASELINE

    return {
        "setup_id":             setup["id"],
        "decision":             "size",
        "size_pct_portfolio":   round(size_pct, 6),
        "size_dollars_at_100k": round(size_dollars, 2),
        "max_loss_dollars":     round(risk_dollars, 2),
        "reason":               f"sized at {size_pct*100:.2f}% NAV with {tier} multiplier {mult}x",
        "rules_applied":        rules_applied,
        "portfolio_state":      state,
    }


def _skip_decision(setup: dict, reason: str, rules_applied: list[dict],
                   state: dict) -> dict:
    return {
        "setup_id":             setup["id"],
        "decision":             "skip",
        "size_pct_portfolio":   None,
        "size_dollars_at_100k": None,
        "max_loss_dollars":     None,
        "reason":               reason,
        "rules_applied":        rules_applied,
        "portfolio_state":      state,
    }


def write_run_status(n_expected: int, n_written: int) -> tuple[str, str | None]:
    """C3: ('partial', err) when some risk-decision rows failed to persist, else
    ('ok', None). A failed chunk previously still recorded status='ok' with
    rows_out<expected — a silent loss invisible to pulsecheck."""
    missing = n_expected - n_written
    if missing > 0:
        return "partial", f"{missing}/{n_expected} risk-decision rows failed to write"
    return "ok", None


def write_decisions(rows: list[dict]) -> int:
    if not rows:
        return 0
    written = 0
    for i in range(0, len(rows), 100):
        chunk = rows[i:i + 100]
        # rules_applied / portfolio_state are JSONB — let requests encode dicts.
        r = requests.post(
            f"{SUPABASE_URL}/rest/v1/stock_risk_decisions?on_conflict=setup_id",
            headers={**HEADERS_SB, "Prefer": "resolution=ignore-duplicates,return=minimal"},
            json=chunk, timeout=20,
        )
        if r.status_code in (200, 201, 204):
            written += len(chunk)
        else:
            print(f"  risk decision insert {r.status_code}: {r.text[:300]}", file=sys.stderr)
    return written


def main() -> int:
    run_id = job_run_start()
    rows_in = rows_out = 0
    try:
        setups = fetch_recent_setups()
        rows_in = len(setups)
        print(f"Fetched {rows_in} trade setups still inside their valid_until window")
        if not setups:
            job_run_finish(run_id, "ok", 0, 0)
            return 0

        existing = fetch_existing_decision_setup_ids([s["id"] for s in setups])
        setups = [s for s in setups if s["id"] not in existing]
        print(f"  {len(existing)} already decided; {len(setups)} new to evaluate")
        if not setups:
            job_run_finish(run_id, "ok", rows_in, 0)
            return 0

        rule_keys = sorted({s.get("rule_key") for s in setups if s.get("rule_key")})
        cal = fetch_calibration_for_rule_keys(list(rule_keys))
        print(f"  loaded calibration for {len(cal)} rules")

        state = compute_portfolio_state()
        print(f"  portfolio state: drawdown_pct={state['drawdown_pct']:.4f} "
              f"sum_return_30d={state['sum_return_30d']:.4f} "
              f"n_closed_30d={state['n_closed_30d']} "
              f"in_flight_pct={state['daily_risk_in_flight_pct']:.4f} "
              f"open_rules={len(state['open_per_rule'])}")

        decisions = [evaluate_setup(s, cal, state) for s in setups]
        sized = [d for d in decisions if d["decision"] == "size"]
        skipped = [d for d in decisions if d["decision"] == "skip"]
        print(f"  sized:   {len(sized)}")
        print(f"  skipped: {len(skipped)}")
        if skipped:
            from collections import Counter
            for reason, n in Counter(d["reason"][:50] for d in skipped).most_common(5):
                print(f"    skip: {n}× {reason}")

        rows_out = write_decisions(decisions)
        print(f"DONE — wrote {rows_out} risk decisions")
        # C3: surface a partial write loss (was hardcoded 'ok' regardless).
        status, write_err = write_run_status(len(decisions), rows_out)
        job_run_finish(run_id, status, rows_in, rows_out, err=write_err)
        return 0

    except Exception as exc:
        import traceback
        tb = traceback.format_exc()
        print(f"FATAL: {exc}\n{tb}", file=sys.stderr)
        job_run_finish(run_id, "failed", rows_in, rows_out, err=str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
