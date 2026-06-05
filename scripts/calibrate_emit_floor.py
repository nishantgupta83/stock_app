#!/usr/bin/env python3
"""calibrate_emit_floor.py — empirical, re-runnable calibration of the Layer 2
emit floor and a validator for the 2.b meta-label gate.

WHY THIS EXISTS
---------------
The thesis_agent emit floor (THESIS_RECALL_FLOOR, currently 30) and the future
2.b precision gate must NOT be hand-picked numbers. This script derives them
from the real outcome corpus (stock_event_paper_trades) so the floor is a number
you VERIFY by reading the curve — not one anybody asserts.

It does two honest things:

  1. PER-RULE EXPECTANCY TABLE (ground truth, no proxy)
     For every rule_key with closed paper trades, compute n, win-rate,
     profit_factor, and mean realized return (= per-trade expectancy). This is
     exactly what a payoff-aware gate (your tier-gate discipline) should read.

  2. FLOOR IMPLICATION + FALSE-POSITIVE FLAGS (uses a documented points proxy)
     Each rule is annotated with its thesis base-point contribution (POINTS_MAP,
     mirrored from agents/thesis_agent.py:score_evidence). Then:
       - implied recall floor = the SMALLEST base-points among rules that clear
         the payoff bar (a single such event already justifies emission).
       - false-positive risk = rules that DON'T clear the bar but whose points
         are >= the current floor (a pure score floor would emit them anyway —
         this is the concrete argument for the 2.b meta-label gate, which gates
         on expectancy instead of score).

USAGE
-----
  export SUPABASE_URL=...            # e.g. https://<ref>.supabase.co
  export SUPABASE_SERVICE_KEY=...    # service_role (private shell only!)
  python3 scripts/calibrate_emit_floor.py [--pf-bar 1.5] [--min-n 30] [--floor 30]

Read-only. Makes no writes. Safe to run anytime.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict

import requests

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

# ─────────────────────────────────────────────────────────────────────────────
# POINTS_MAP — thesis base points per rule family.
# MUST stay in sync with agents/thesis_agent.py:score_evidence add(...) calls.
# Keyed by a prefix of the calibration/paper-trade rule_key. Background-role
# (institutional 13F) rules contribute 0 since PR1A (2026-05-22) — listed as 0
# so the table makes the exclusion explicit.
# These are SINGLE-EVENT base points; a multi-event cluster scores the sum
# (e.g. 3 clinical events = 36), so the implied floor derived here is a
# conservative LOWER bound on what a real cluster containing the rule scores.
# ─────────────────────────────────────────────────────────────────────────────
POINTS_MAP: list[tuple[str, float]] = [
    ("8k_material_event",            25.0),  # new_8k
    ("clinical_readout",             12.0),  # clinical_* (any subtype)
    ("filing_13d",                   20.0),  # new_sc_13d  (activist_13d=30 if 13D-activist)
    ("filing_13g",                   10.0),  # new_sc_13g
    ("earnings_release:beat",        12.0),  # earnings_beat (surprise-scaled; nominal)
    ("earnings_release:miss",        12.0),  # earnings_miss (surprise-scaled; nominal)
    ("earnings_release",              5.0),  # earnings_inline / scheduled
    ("news_article:positive",        12.0),  # news_bullish
    ("news_article:negative",        12.0),  # news_bearish
    ("news_article:neutral",          5.0),  # news_neutral
    ("truth_social_post:tariff",     15.0),  # truth_social_mapping
    ("truth_social_post:djt",        15.0),  # truth_social_mapping
    ("truth_social_post",            15.0),  # other truth_social
    ("institutional",                 0.0),  # BACKGROUND — excluded since PR1A
    ("filing_4",                      0.0),  # Form 4 insider — context only
]


def base_points(rule_key: str) -> float | None:
    """Longest-prefix match against POINTS_MAP. None = unmapped (treat as unknown)."""
    best: tuple[int, float] | None = None
    for prefix, pts in POINTS_MAP:
        if rule_key.startswith(prefix) and (best is None or len(prefix) > best[0]):
            best = (len(prefix), pts)
    return best[1] if best else None


def fetch_closed_trades() -> list[dict]:
    """Page through all closed paper trades (rule_key, realized_return, correct)."""
    rows: list[dict] = []
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}
    offset, page = 0, 1000
    while True:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/stock_event_paper_trades",
            headers={**headers, "Range-Unit": "items", "Range": f"{offset}-{offset+page-1}"},
            params={
                "status": "eq.closed",
                "select": "rule_key,realized_return,correct,horizon_days",
            },
            timeout=30,
        )
        r.raise_for_status()
        batch = r.json()
        rows.extend(batch)
        if len(batch) < page:
            break
        offset += page
    return rows


def expectancy_stats(returns: list[float], correct: list[bool]) -> dict:
    """n, win-rate, profit_factor, expectancy (mean return) from raw outcomes."""
    n = len(returns)
    wins = [x for x in returns if x > 0]
    losses = [x for x in returns if x < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    pf = (gross_win / gross_loss) if gross_loss > 0 else float("inf") if gross_win > 0 else 0.0
    win_rate = (sum(1 for c in correct if c) / n) if n else 0.0
    expectancy = (sum(returns) / n) if n else 0.0
    return {"n": n, "win_rate": win_rate, "pf": pf, "expectancy": expectancy}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pf-bar", type=float, default=1.5,
                    help="profit-factor bar a rule must clear to be 'profitable' (default 1.5)")
    ap.add_argument("--min-n", type=int, default=30,
                    help="minimum closed trades for a rule to count (default 30)")
    ap.add_argument("--floor", type=float, default=30.0,
                    help="current recall floor, for false-positive flagging (default 30)")
    args = ap.parse_args()

    if not SUPABASE_URL or not SUPABASE_KEY:
        print("ERROR: set SUPABASE_URL and SUPABASE_SERVICE_KEY (private shell).", file=sys.stderr)
        return 2

    print(f"Fetching closed paper trades from {SUPABASE_URL} ...", file=sys.stderr)
    trades = fetch_closed_trades()
    print(f"  {len(trades)} closed trades.\n", file=sys.stderr)

    by_rule_ret: dict[str, list[float]] = defaultdict(list)
    by_rule_correct: dict[str, list[bool]] = defaultdict(list)
    for t in trades:
        rk = t.get("rule_key")
        rr = t.get("realized_return")
        if rk is None or rr is None:
            continue
        by_rule_ret[rk].append(float(rr))
        by_rule_correct[rk].append(bool(t.get("correct")))

    rows = []
    for rk in by_rule_ret:
        st = expectancy_stats(by_rule_ret[rk], by_rule_correct[rk])
        if st["n"] < args.min_n:
            continue
        st["rule_key"] = rk
        st["points"] = base_points(rk)
        st["profitable"] = st["pf"] >= args.pf_bar
        rows.append(st)

    rows.sort(key=lambda r: (r["points"] if r["points"] is not None else 999, -r["pf"]))

    # ── Per-rule table ──────────────────────────────────────────────────────
    print(f"{'rule_key':<46} {'pts':>5} {'n':>6} {'win%':>6} {'PF':>7} {'exp%':>7}  bar")
    print("─" * 92)
    for r in rows:
        pts = "?" if r["points"] is None else f"{r['points']:.0f}"
        pf = "inf" if r["pf"] == float("inf") else f"{r['pf']:.2f}"
        mark = "✓" if r["profitable"] else "·"
        print(f"{r['rule_key']:<46} {pts:>5} {r['n']:>6} "
              f"{r['win_rate']*100:>5.1f}% {pf:>7} {r['expectancy']*100:>6.2f}%  {mark}")

    # ── Floor implication ──────────────────────────────────────────────────
    profitable_pts = [r["points"] for r in rows if r["profitable"] and r["points"] is not None and r["points"] > 0]
    print("\n" + "═" * 92)
    if profitable_pts:
        implied = min(profitable_pts)
        print(f"IMPLIED RECALL FLOOR  (smallest base-points among PF≥{args.pf_bar} rules): {implied:.0f}")
        print(f"  → with a working 2.b PRECISION gate, the recall floor (2.a) can be this low —")
        print(f"     catch every profitable rule, let 2.b filter the rest by expectancy.")
        print(f"  → WITHOUT 2.b (today), keep a CONSERVATIVE floor: the same {implied:.0f}-pt rule is")
        print(f"     often unprofitable at a DIFFERENT horizon (see the table — profitability")
        print(f"     tracks HORIZON, not points), so a low floor with no precision gate floods.")
        print(f"  → current THESIS_RECALL_FLOOR = {args.floor:.0f} — a defensible conservative stopgap")
        print(f"     until 2.b lands; it admits strong multi-event/high-point catalysts and")
        print(f"     filters marginal singles. Lower it only once 2.b gates precision.")
    else:
        print("No rule cleared the PF bar at the given --min-n. Loosen --pf-bar or --min-n.")

    # ── False-positive flags: the argument for the 2.b meta-label gate ──────
    fp = [r for r in rows if not r["profitable"] and r["points"] is not None
          and r["points"] >= args.floor]
    print("\nFALSE-POSITIVE RISK at floor "
          f"{args.floor:.0f}  (unprofitable rules a pure SCORE floor would still emit):")
    if fp:
        for r in fp:
            pf = "inf" if r["pf"] == float("inf") else f"{r['pf']:.2f}"
            print(f"  · {r['rule_key']:<44} pts={r['points']:.0f}  PF={pf}  exp={r['expectancy']*100:.2f}%")
        print("  → a score floor CANNOT exclude these (same points as profitable rules).")
        print("  → THIS is what the 2.b payoff-aware meta-label gate fixes: gate on")
        print("     expectancy, not score. See docs/design/layer2-metalabeling-funnel.md.")
    else:
        print("  (none at this floor — but multi-event clusters can still mix rules; 2.b still warranted.)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
