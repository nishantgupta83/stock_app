#!/usr/bin/env python3
"""
Capture or diff weekly learning snapshots.

Usage:
  scripts/learning_snapshot.py capture                  # dump current learning state to snapshots/YYYY-MM-DD.json
  scripts/learning_snapshot.py diff <date1> <date2>     # diff two captured snapshots

Captures three learning tables:
  - stock_rule_calibration  (per-rule accuracy + payoff stats)
  - stock_agent_weights     (per-agent EMA weights, latest date only)
  - closed paper-trade stats (rollup of stock_event_paper_trades where status='closed')

The diff calls extract_meaningful_changes() — that's where "learning happened
this week" is defined: tier crossings + closest-to-adult + payoff-sanity, all
read from runtime-truth maturity (stored tier / is_mature flags + effective_* stats).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
SNAPSHOT_DIR = Path(__file__).resolve().parent.parent / "snapshots"

# Maturity tiers come from the shared, env-free gate module — the SAME code the
# runtime writer (agents/price_agent.py) uses — so this report can never drift
# from what actually licenses BUY/SELL. Pre-fix this file hardcoded an obsolete
# `acc≥0.90 / n≥30` adult gate on RAW n; runtime is payoff-first on EFFECTIVE
# (independent ticker-entry-day) evidence. See agents/_maturity.py + sql/0041.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agents"))
from _maturity import (  # type: ignore  # noqa: E402
    derive_maturity_flags,
    MATURITY_MIN_N, ADULT_MIN_N, ADULT_MIN_PF, ADULT_MIN_MEAN,
    TIER_GATE_YOUNG_ACC, TIER_GATE_YOUNG_PF,
)


def _get(path: str) -> list[dict]:
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/{path}",
        headers={"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def capture() -> Path:
    calibration = _get(
        "stock_rule_calibration?select=rule_key,n_observations,n_correct,accuracy,"
        "is_mature,is_mature_70,is_mature_80,tier,profit_factor,target_hit_rate,stop_hit_rate,"
        "mean_mfe_pct,mean_mae_pct,avg_win_pct,avg_loss_pct,mean_realized_pct,"
        "effective_n,effective_n_correct,effective_accuracy,effective_mean_realized_pct,"
        "effective_profit_factor,accuracy_30d,brier_30d,n_closed_30d,last_updated"
    )

    weights_all = _get("stock_agent_weights?select=agent,date,accuracy_ema,weight,n_signals&order=date.desc")
    latest_date = max((r["date"] for r in weights_all), default=None)
    weights = [r for r in weights_all if r["date"] == latest_date]

    closed = _get(
        "stock_event_paper_trades?status=eq.closed"
        "&select=rule_key,direction,correct,realized_return,target_hit,stop_hit,horizon_days"
    )
    rollup: dict[str, dict] = {}
    for t in closed:
        rk = t["rule_key"] or "unknown"
        d = rollup.setdefault(rk, {"n": 0, "wins": 0, "sum_return": 0.0, "target_hits": 0, "stop_hits": 0})
        d["n"] += 1
        if t.get("correct"):
            d["wins"] += 1
        if t.get("realized_return") is not None:
            d["sum_return"] += float(t["realized_return"])
        if t.get("target_hit"):
            d["target_hits"] += 1
        if t.get("stop_hit"):
            d["stop_hits"] += 1

    snapshot = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "calibration": calibration,
        "agent_weights": {"date": latest_date, "rows": weights},
        "closed_trades_rollup": rollup,
        "n_calibration_rules": len(calibration),
        "n_mature_rules": sum(1 for r in calibration if r.get("is_mature")),
        "n_closed_trades_total": len(closed),
    }

    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    out = SNAPSHOT_DIR / f"{date_str}.json"
    out.write_text(json.dumps(snapshot, indent=2, default=str))
    print(f"✓ Captured {len(calibration)} rules, {len(weights)} agent weights, {len(closed)} closed trades")
    print(f"✓ Wrote {out}")
    return out


def diff(date1: str, date2: str) -> None:
    s1 = json.loads((SNAPSHOT_DIR / f"{date1}.json").read_text())
    s2 = json.loads((SNAPSHOT_DIR / f"{date2}.json").read_text())

    print(f"\n=== Snapshot diff: {date1} → {date2} ===\n")
    print(f"  Rules tracked:    {s1['n_calibration_rules']} → {s2['n_calibration_rules']}")
    print(f"  Mature rules:     {s1['n_mature_rules']} → {s2['n_mature_rules']}")
    print(f"  Closed trades:    {s1['n_closed_trades_total']} → {s2['n_closed_trades_total']}  "
          f"(+{s2['n_closed_trades_total'] - s1['n_closed_trades_total']} this period)")
    print()

    changes = extract_meaningful_changes(s1, s2)
    if not changes:
        print("  (no meaningful changes surfaced)")
        return
    for line in changes:
        print(line)


# "Closest to adult" = effective_n within this fraction of the production floor.
NEAR_N_FRAC = 0.85


def _has_effective(rule: dict) -> bool:
    """Whether this snapshot carries effective_* stats (captured after sql/0041).
    Older snapshots on disk have ONLY raw columns + the stored is_mature* flags —
    and raw n over-counts 2-4x, so it must never reach the effective adult gate."""
    return rule.get("effective_n") is not None


def _tier_for(rule: dict | None) -> str:
    """Runtime-truth tier for a rule — WITHOUT ever recomputing the adult gate on
    raw n (which over-counts 2-4x vs the effective floor; sql/0041).

    Priority:
      1. stored `tier` (exact runtime truth, sql/0031);
      2. recompute via the shared gate on effective_* stats, when present;
      3. pre-effective snapshot → trust the stored is_mature* booleans (they
         already encode the effective-evidence decision and are False for the
         over-counted-but-inaccurate rules the raw fallback used to mis-promote).
         Sub-adult tiers collapse to 'child' when is_mature_70/80 weren't captured.
    'child' when unknown."""
    if not rule:
        return "child"
    if rule.get("tier"):
        return rule["tier"]
    if _has_effective(rule):
        return derive_maturity_flags(
            int(rule["effective_n"] or 0),
            rule.get("effective_profit_factor"),
            float(rule.get("effective_mean_realized_pct") or 0),
            float(rule.get("effective_accuracy") or 0),
        )["tier"]
    if rule.get("is_mature"):
        return "adult"
    if rule.get("is_mature_80"):
        return "young_adult"
    if rule.get("is_mature_70"):
        return "teen"
    return "child"


def _adult_shortfall(rule: dict) -> list[str] | None:
    """Which adult-gate criteria a not-yet-adult rule fails, using EFFECTIVE stats
    ONLY (adult = effective_n≥100 AND PF≥2.0 AND mean_realized≥0.5%, no accuracy
    floor — agents/_maturity.py). Returns None when already adult OR when the
    snapshot has no effective_* stats (we refuse to assess the production BUY/SELL
    gate on raw n)."""
    if _tier_for(rule) == "adult" or not _has_effective(rule):
        return None
    n = int(rule["effective_n"] or 0)
    pf = rule.get("effective_profit_factor")
    mean = rule.get("effective_mean_realized_pct")
    unmet: list[str] = []
    if n < ADULT_MIN_N:
        unmet.append(f"eff_n={n} (need {ADULT_MIN_N})")
    if pf is None or float(pf) < ADULT_MIN_PF:
        unmet.append(f"PF={'n/a' if pf is None else format(float(pf), '.2f')} (need {ADULT_MIN_PF})")
    if mean is None or float(mean) < ADULT_MIN_MEAN:
        unmet.append(f"mean={'n/a' if mean is None else format(float(mean), '.4f')} (need {ADULT_MIN_MEAN})")
    return unmet


def extract_meaningful_changes(s1: dict, s2: dict) -> list[str]:
    """Three surfaces, all driven by RUNTIME-TRUTH tiers + effective stats:

    1. Tier crossings — rules that promoted/demoted between snapshots (stored
       `tier`, the same value that licenses BUY/SELL), the most actionable
       weekly-learning signal.
    2. Closest to ADULT — not-yet-adult rules with effective_n within
       NEAR_N_FRAC of the production floor, showing which adult criteria remain
       unmet (adult is payoff-first: eff_n/PF/mean, no accuracy floor).
    3. Payoff sanity — rules accurate enough for young_adult (acc≥0.80) whose
       effective payoff (PF) still fails — "accurate but unprofitable."
    """
    out: list[str] = []
    by_key_s1 = {r["rule_key"]: r for r in s1.get("calibration", [])}
    by_key_s2 = {r["rule_key"]: r for r in s2.get("calibration", [])}

    def _fmt_row(rk: str, frm: str, to: str, r: dict) -> str:
        # Label the count honestly: eff_n only when the effective stat is present,
        # else raw n (never dress raw n up as effective).
        if r.get("effective_n") is not None:
            n_str, pf = f"eff_n={int(r['effective_n'] or 0)}", r.get("effective_profit_factor")
        else:
            n_str, pf = f"n={int(r.get('n_observations') or 0)}", r.get("profit_factor")
        pf_str = f"PF={float(pf):.2f}" if pf is not None else "PF=n/a"
        return f"  {rk}: {frm} → {to}  ({n_str}, {pf_str})"

    # --- Surface 1: tier crossings ------------------------------------------
    promotions, demotions = [], []
    for rk, r2 in by_key_s2.items():
        tier_now = _tier_for(r2)
        tier_then = _tier_for(by_key_s1.get(rk))
        if tier_now == tier_then:
            continue
        bucket = promotions if _tier_rank(tier_now) > _tier_rank(tier_then) else demotions
        bucket.append((rk, tier_then, tier_now, r2))

    if promotions:
        out.append("=== TIER PROMOTIONS ===")
        for c in sorted(promotions, key=lambda c: -_tier_rank(c[2])):
            out.append(_fmt_row(*c))
    if demotions:
        out.append("=== TIER DEMOTIONS (rule degraded — investigate) ===")
        for c in sorted(demotions, key=lambda c: _tier_rank(c[2])):
            out.append(_fmt_row(*c))

    # --- Surface 2: closest to ADULT (production BUY/SELL gate) --------------
    # _adult_shortfall returns None for pre-effective snapshots, so old snapshots
    # are simply not assessed here (never mis-surfaced as near-adult on raw n).
    out.append("=== CLOSEST TO ADULT (production BUY/SELL gate) ===")
    near: list[tuple[int, str]] = []
    for rk, r in by_key_s2.items():
        unmet = _adult_shortfall(r)     # None if adult OR no effective stats
        if not unmet:
            continue
        n = int(r["effective_n"] or 0)  # guaranteed present when unmet is non-None
        if n < int(ADULT_MIN_N * NEAR_N_FRAC):
            continue   # not close on sample yet — don't surface noise
        near.append((ADULT_MIN_N - n, f"  {rk}: {', '.join(unmet)}"))
    if near:
        for _, line in sorted(near)[:8]:
            out.append(line)
    else:
        out.append(f"  (no rule with effective evidence within {int(NEAR_N_FRAC * 100)}% of the floor)")

    # --- Surface 3: payoff sanity (accurate but unprofitable) ---------------
    # Effective stats only — a soft diagnostic, but still never computed on raw.
    sanity_flags: list[str] = []
    for rk, r in by_key_s2.items():
        if not _has_effective(r) or int(r["effective_n"] or 0) < MATURITY_MIN_N:
            continue
        acc = r.get("effective_accuracy")
        if acc is None or float(acc) < TIER_GATE_YOUNG_ACC:
            continue
        pf = r.get("effective_profit_factor")
        if pf is not None and float(pf) < TIER_GATE_YOUNG_PF:
            sanity_flags.append(
                f"  {rk}: acc={float(acc):.1%} but PF={float(pf):.2f} "
                f"(need ≥{TIER_GATE_YOUNG_PF}) — accurate but unprofitable"
            )
    if sanity_flags:
        out.append("=== PAYOFF SANITY FAILURES (accurate but unprofitable) ===")
        out.extend(sanity_flags[:10])

    return out


def _tier_rank(tier: str) -> int:
    return {"child": 0, "teen": 1, "young_adult": 2, "adult": 3}.get(tier, 0)


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "capture"
    if cmd == "capture":
        capture()
    elif cmd == "diff" and len(sys.argv) == 4:
        diff(sys.argv[2], sys.argv[3])
    else:
        print(__doc__)
        sys.exit(1)
