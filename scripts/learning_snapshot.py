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

The diff calls extract_meaningful_changes() — that's where the user defines
what "learning happened this week" actually means. See the TODO at the bottom.
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
        "is_mature,profit_factor,target_hit_rate,stop_hit_rate,mean_mfe_pct,mean_mae_pct,"
        "avg_win_pct,avg_loss_pct,mean_realized_pct,accuracy_30d,brier_30d,n_closed_30d,last_updated"
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


# ---------------------------------------------------------------------------
# Tier promotion gates — v1 (2026-05-26, accuracy + payoff sanity).
# These mirror the gates that will land in stock_rule_calibration in Phase 3
# of the stage-gate plan. Keeping them here lets the snapshot diff surface
# tier crossings BEFORE the schema migration is applied — so we can validate
# the math against real data first.
# ---------------------------------------------------------------------------
TIER_GATES = (
    # (tier_name, min_n, min_accuracy, payoff_field, payoff_min)
    ("teen",        30, 0.70, "mean_realized_pct", 0.0),
    ("young_adult", 30, 0.80, "profit_factor",     1.2),
    ("adult",       30, 0.90, "profit_factor",     1.5),
)

# How close (in accuracy points) counts as "near the threshold" for the
# closest-to-crossing surface — useful when nothing actually crossed this week.
NEAR_THRESHOLD_PTS = 0.02


def _passes_gate(rule: dict, min_n: int, min_acc: float,
                  payoff_field: str, payoff_min: float) -> bool:
    n = int(rule.get("n_observations") or 0)
    acc = float(rule.get("accuracy") or 0)
    payoff = rule.get(payoff_field)
    if n < min_n or acc < min_acc:
        return False
    # Payoff field may be NULL for rules with too few closed trades to compute
    # PF / mean_realized — treat NULL as failing payoff to be conservative.
    if payoff is None:
        return False
    return float(payoff) >= payoff_min


def _tier_for(rule: dict) -> str:
    """Highest tier this rule qualifies for under v1 gates. 'child' if none."""
    for tier, n, acc, field, pmin in reversed(TIER_GATES):   # check adult first
        if _passes_gate(rule, n, acc, field, pmin):
            return tier
    return "child"


def extract_meaningful_changes(s1: dict, s2: dict) -> list[str]:
    """Three surfaces, in priority order:

    1. Tier crossings — rules that promoted between snapshots (the user's
       "option #1" — most actionable signal of weekly learning).
    2. Closest to crossing — rules within NEAR_THRESHOLD_PTS of the next
       tier, sorted by how close. Useful when nothing actually crossed.
    3. Payoff sanity flags — rules whose accuracy meets a tier but whose
       payoff (profit_factor / mean_realized_pct) fails it. The bot's
       "accurate but money-losing" rules.
    """
    out: list[str] = []
    by_key_s1 = {r["rule_key"]: r for r in s1.get("calibration", [])}
    by_key_s2 = {r["rule_key"]: r for r in s2.get("calibration", [])}

    # --- Surface 1: tier crossings ------------------------------------------
    crossings: list[tuple[str, str, str, dict]] = []   # (rule_key, from, to, row)
    for rk, r2 in by_key_s2.items():
        r1 = by_key_s1.get(rk)
        tier_now = _tier_for(r2)
        tier_then = _tier_for(r1) if r1 else "child"
        if tier_now != tier_then:
            crossings.append((rk, tier_then, tier_now, r2))

    promotions = [c for c in crossings if _tier_rank(c[2]) > _tier_rank(c[1])]
    demotions = [c for c in crossings if _tier_rank(c[2]) < _tier_rank(c[1])]

    if promotions:
        out.append("=== TIER PROMOTIONS ===")
        for rk, frm, to, r in sorted(promotions, key=lambda c: -_tier_rank(c[2])):
            acc = float(r.get("accuracy") or 0)
            n = int(r.get("n_observations") or 0)
            pf = r.get("profit_factor")
            pf_str = f"PF={pf:.2f}" if pf is not None else "PF=n/a"
            out.append(f"  {rk}: {frm} → {to}  (acc={acc:.1%}, n={n}, {pf_str})")
    if demotions:
        out.append("=== TIER DEMOTIONS (rule degraded — investigate) ===")
        for rk, frm, to, r in sorted(demotions, key=lambda c: _tier_rank(c[2])):
            acc = float(r.get("accuracy") or 0)
            n = int(r.get("n_observations") or 0)
            pf = r.get("profit_factor")
            pf_str = f"PF={pf:.2f}" if pf is not None else "PF=n/a"
            out.append(f"  {rk}: {frm} → {to}  (acc={acc:.1%}, n={n}, {pf_str})")

    # --- Surface 2: closest to crossing -------------------------------------
    out.append("=== CLOSEST TO PROMOTION (current snapshot) ===")
    near: dict[str, list[tuple[float, str]]] = {"teen": [], "young_adult": [], "adult": []}
    for rk, r in by_key_s2.items():
        current_tier = _tier_for(r)
        for tier_name, min_n, min_acc, field, pmin in TIER_GATES:
            if _tier_rank(tier_name) <= _tier_rank(current_tier):
                continue   # already at or above this tier
            acc = float(r.get("accuracy") or 0)
            n = int(r.get("n_observations") or 0)
            gap = min_acc - acc
            # Want strictly below threshold but within window AND with enough n
            if 0 < gap <= NEAR_THRESHOLD_PTS and n >= min_n:
                payoff = r.get(field)
                payoff_str = (
                    f"{field}={payoff:.2f}" if isinstance(payoff, (int, float))
                    else f"{field}=n/a"
                )
                near[tier_name].append((
                    gap,
                    f"  {rk}: acc={acc:.1%} (need {min_acc:.0%}, gap {gap*100:.1f}pts), "
                    f"n={n}, {payoff_str}",
                ))
                break   # only show the next tier up
    any_near = False
    for tier_name in ("adult", "young_adult", "teen"):
        rows = sorted(near[tier_name])[:5]
        if rows:
            any_near = True
            out.append(f"  → {tier_name}:")
            for _, line in rows:
                out.append(line)
    if not any_near:
        out.append("  (no rules within 2 accuracy points of next tier with enough n)")

    # --- Surface 3: payoff sanity flags -------------------------------------
    # For each rule with n≥30, find the HIGHEST tier its accuracy qualifies
    # for. If that tier's payoff field is populated AND fails the threshold,
    # flag it: "accurate but not profitable enough." NULL payoff is treated
    # as 'no data to assess' (separate problem), not as a failure.
    sanity_flags: list[str] = []
    missing_payoff: list[str] = []
    for rk, r in by_key_s2.items():
        n = int(r.get("n_observations") or 0)
        if n < 30:
            continue
        acc = float(r.get("accuracy") or 0)
        # Walk tiers highest-to-lowest; first accuracy-qualified one is the target
        target_tier = None
        for tier_name, _, min_acc, field, pmin in reversed(TIER_GATES):
            if acc >= min_acc:
                target_tier = (tier_name, field, pmin)
                break
        if target_tier is None:
            continue
        tier_name, field, pmin = target_tier
        payoff = r.get(field)
        if payoff is None:
            missing_payoff.append(
                f"  {rk}: acc={acc:.1%} qualifies for {tier_name} but {field} not computed (n_closed_30d may be too low)"
            )
        elif float(payoff) < pmin:
            sanity_flags.append(
                f"  {rk}: acc={acc:.1%} qualifies for {tier_name} but "
                f"{field}={float(payoff):.2f} (need ≥{pmin}) — accurate but unprofitable"
            )
    if sanity_flags:
        out.append("=== PAYOFF SANITY FAILURES (accurate but unprofitable) ===")
        out.extend(sanity_flags[:10])
    if missing_payoff:
        out.append("=== PAYOFF METRICS MISSING (recompute needed) ===")
        out.extend(missing_payoff[:10])

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
