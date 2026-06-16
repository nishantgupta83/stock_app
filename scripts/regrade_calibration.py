#!/usr/bin/env python3
"""OFFLINE calibration re-grade from a local snapshot (scripts/snapshot_paper_trades.py).

Re-grades every closed paper trade under an exit policy and rebuilds the FULL
per-rule calibration (effective-n gate, raw counters, payoff) WITHOUT touching
Supabase. Lets you compare exit policies and rebuild historical calibration for
free, repeatedly. The actual DB write is a separate, explicit --commit step.

Grading sources (single source of truth where it matters):
  stop_only, hold  -> agents/price_agent.compute_paper_outcome (the LIVE grader)
  trail            -> local grade_trail (comparison only; conservative gap-fill,
                      trailing stop at TRAIL_MULT x stop_pct below the running peak)

Usage:
  python3 scripts/regrade_calibration.py                 # dry-run: hold vs stop_only vs trail
  python3 scripts/regrade_calibration.py --policy stop_only --save
  SUPABASE_URL=... SUPABASE_SERVICE_KEY=... \
    python3 scripts/regrade_calibration.py --policy stop_only --commit   # WRITES DB (gated)
"""
from __future__ import annotations
import argparse, json, os, sys, datetime as dt
from collections import defaultdict
from pathlib import Path

# Allow importing the canonical grader offline (compute_paper_outcome is pure;
# the module only reads these env vars at import time).
os.environ.setdefault("SUPABASE_URL", "http://offline.local")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "offline")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agents"))
from price_agent import compute_paper_outcome  # noqa: E402
from _maturity import collapse_to_effective, derive_maturity_flags  # noqa: E402

REGRADE_DIR = Path(os.environ.get("REGRADE_DIR", "regrade_data"))
SLIP = 2 * (5 / 10000)
TRAIL_MULT = float(os.environ.get("TRAIL_MULT", "2.0"))
POLICIES = ("hold", "stop_only", "trail")


def load_snapshot() -> tuple[list[dict], dict[str, dict]]:
    trades = [json.loads(l) for l in (REGRADE_DIR / "trades.jsonl").read_text().splitlines() if l.strip()]
    bars: dict[str, dict] = {}
    for p in (REGRADE_DIR / "bars").glob("*.json"):
        raw = json.loads(p.read_text())
        bars[p.stem] = {dt.date.fromisoformat(d): {"open": o, "high": h, "low": lo, "close": c}
                        for d, (o, h, lo, c) in raw.items()}
    return trades, bars


def grade_trail(trade: dict, bars: dict, trail_mult: float = TRAIL_MULT) -> dict | None:
    """Trailing stop at trail_mult x stop_pct below the running peak; gap-fill at
    open; ride to horizon close if never triggered. Comparison-only."""
    try:
        entry = float(trade["entry_price"])
    except (TypeError, ValueError):
        return None
    if entry <= 0:
        return None
    e = dt.date.fromisoformat(trade["entry_at"][:10])
    h = int(trade.get("horizon_days") or 1)
    long = (trade.get("direction") or "long") == "long"
    sp = float(trade.get("stop_pct") or 0) * trail_mult
    days = [d for d in sorted(bars) if e < d <= e + dt.timedelta(days=h + 5)]
    if not days:
        return None
    horizon_d = next((d for d in days if d >= e + dt.timedelta(days=h)), days[-1])
    peak = entry
    exit_px, reason = bars[horizon_d]["close"], "horizon"
    for d in days:
        if d > horizon_d:
            break
        bar = bars[d]
        hi, lo, op = bar["high"], bar["low"], bar.get("open")
        peak = max(peak, hi) if long else min(peak, lo)
        if not sp:
            continue
        tl = peak * (1 - sp) if long else peak * (1 + sp)
        if (lo <= tl) if long else (hi >= tl):
            if long:
                exit_px = tl if (op is None or op > tl) else op
            else:
                exit_px = tl if (op is None or op < tl) else op
            reason = "trail"
            break
    dm = 1.0 if long else -1.0
    return {"realized_return": round((exit_px - entry) / entry * dm - SLIP, 6),
            "exit_reason": reason}


def regrade(trades: list[dict], bars: dict, policy: str) -> dict[str, dict]:
    """Return {rule_key: calibration-row dict} rebuilt under `policy`."""
    by_rule: dict[str, list[dict]] = defaultdict(list)
    for t in trades:
        b = bars.get(t["ticker"])
        if not b:
            continue
        if policy == "trail":
            o = grade_trail(t, b)
        else:
            o = compute_paper_outcome(t, b, exit_policy=policy)
        if o is None:
            continue
        by_rule[t["rule_key"]].append({"ticker": t["ticker"], "entry_at": t["entry_at"],
                                       "realized_return": o["realized_return"]})
    out: dict[str, dict] = {}
    for rk, graded in by_rule.items():
        eff = collapse_to_effective(graded)
        flags = derive_maturity_flags(eff["effective_n"], eff["effective_profit_factor"],
                                      eff["effective_mean_realized_pct"], eff["effective_accuracy"])
        rets = [g["realized_return"] for g in graded]
        n = len(rets); ncorr = sum(1 for r in rets if r > 0)
        out[rk] = {
            "rule_key": rk, "n_observations": n, "n_correct": ncorr,
            "accuracy": round(ncorr / n, 6) if n else 0.0,
            "mean_realized_pct": round(sum(rets) / n, 6) if n else 0.0,
            "effective_n": eff["effective_n"],
            "effective_profit_factor": (round(eff["effective_profit_factor"], 4)
                                        if eff["effective_profit_factor"] is not None else None),
            "effective_mean_realized_pct": round(eff["effective_mean_realized_pct"], 6),
            "tier": flags["tier"], "is_mature": flags["is_mature"],
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--policy", choices=POLICIES, help="single policy (default: compare all)")
    ap.add_argument("--save", action="store_true", help="write regrade_<policy>.json locally")
    ap.add_argument("--commit", action="store_true", help="WRITE rebuilt calibration to Supabase (gated)")
    ap.add_argument("--top", type=int, default=12, help="rows to print, by effective_n")
    args = ap.parse_args()

    trades, bars = load_snapshot()
    print(f"loaded {len(trades)} trades / {len(bars)} tickers from {REGRADE_DIR}\n")
    policies = [args.policy] if args.policy else list(POLICIES)
    graded = {p: regrade(trades, bars, p) for p in policies}

    rules = sorted({rk for g in graded.values() for rk in g},
                   key=lambda rk: -max(graded[p].get(rk, {}).get("effective_n", 0) for p in policies))
    print(f"{'rule_key':42s} " + " ".join(f"{p[:9]:>22s}" for p in policies))
    print(f"{'':42s} " + " ".join(f"{'effN  PF   mean  tier':>22s}" for _ in policies))
    print("-" * (43 + 23 * len(policies)))
    adult_count = {p: 0 for p in policies}
    for rk in rules[:args.top]:
        cells = []
        for p in policies:
            c = graded[p].get(rk)
            if not c:
                cells.append(f"{'—':>22s}"); continue
            if c["is_mature"]:
                adult_count[p] += 1
            pf = f"{c['effective_profit_factor']:.2f}" if c["effective_profit_factor"] is not None else "inf"
            cells.append(f"{c['effective_n']:>4d} {pf:>5s} {c['mean_realized_pct']*100:>+5.1f} {c['tier'][:5]:>5s}")
        print(f"{rk:42s} " + " ".join(cells))
    print("\nadult (is_mature) rule counts:", {p: sum(1 for c in graded[p].values() if c['is_mature']) for p in policies})

    if args.save or args.commit:
        for p in policies:
            (REGRADE_DIR / f"regrade_{p}.json").write_text(json.dumps(graded[p], indent=2))
            print(f"saved {REGRADE_DIR}/regrade_{p}.json ({len(graded[p])} rules)")
    if args.commit:
        if not args.policy:
            print("ERROR: --commit requires a single --policy", file=sys.stderr); return 2
        print("\n--commit requested. Writing rebuilt calibration to Supabase is GATED:\n"
              "  snapshot the current stock_rule_calibration FIRST, then wire the upsert.\n"
              "  (Not auto-writing — confirm intent and supply real creds.)", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
