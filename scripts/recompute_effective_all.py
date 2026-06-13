#!/usr/bin/env python3
"""recompute_effective_all.py — H1 one-shot: re-derive every rule's maturity
tier on EFFECTIVE-n (distinct ticker-day clusters) instead of raw trade count.

WHY: price_agent.recompute_rule_payoff now gates on effective-n, but it only
runs for rules that close a trade. The existing pseudo-replicated "adult" rules
(clinical x3, news:positive:h15d) won't demote until they next close a trade —
this sweeps ALL rules now. Gating is inline-effective, so this works BEFORE
sql/0041 is applied (the effective_* column persist is guarded); applying the
migration first additionally persists the effective_* stats for the dashboard +
recompute_maturity_flags.

USAGE (private shell, SUPABASE_URL + SUPABASE_SERVICE_KEY):
  python3 scripts/recompute_effective_all.py            # DRY-RUN: preview tier changes
  python3 scripts/recompute_effective_all.py --commit   # call recompute_rule_payoff per rule
"""
from __future__ import annotations

import argparse
import os
import sys

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agents"))
import _maturity        # noqa: E402
import price_agent      # noqa: E402  (uses its live recompute_rule_payoff on --commit)

URL = os.environ["SUPABASE_URL"].rstrip("/")
KEY = os.environ["SUPABASE_SERVICE_KEY"]
H = {"apikey": KEY, "Authorization": f"Bearer {KEY}"}


def _page(table: str, params: dict) -> list[dict]:
    rows, off = [], 0
    while True:
        r = requests.get(f"{URL}/rest/v1/{table}", headers=H,
                         params={**params, "offset": str(off), "limit": "1000"}, timeout=60)
        r.raise_for_status(); b = r.json(); rows += b
        if len(b) < 1000:
            return rows
        off += 1000


def effective_tier(rule_key: str) -> tuple[dict, str]:
    """(effective stats, effective-gated tier) over the rule's closed population."""
    rows = _page("stock_event_paper_trades",
                 {"rule_key": f"eq.{rule_key}", "status": "eq.closed",
                  "select": "ticker,entry_at,realized_return,correct"})
    outcome = [r for r in rows if r.get("correct") is not None and r.get("realized_return") is not None]
    eff = _maturity.collapse_to_effective(outcome)
    flags = _maturity.derive_maturity_flags(
        eff["effective_n"], eff["effective_profit_factor"],
        eff["effective_mean_realized_pct"], eff["effective_accuracy"])
    return eff, flags["tier"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true")
    args = ap.parse_args()

    cal = _page("stock_rule_calibration", {"select": "rule_key,n_observations,tier"})
    print(f"{'COMMIT' if args.commit else 'DRY-RUN'} — {len(cal)} rules\n")
    print(f"{'rule_key':<46} {'raw_n':>6} {'eff_n':>6} {'cur_tier':>11} {'new_tier':>11}")
    print("-" * 86)
    changed = demoted = 0
    rank = {"child": 0, "teen": 1, "young_adult": 2, "adult": 3}
    for c in sorted(cal, key=lambda x: -(int(x.get("n_observations") or 0))):
        rk = c["rule_key"]; cur = c.get("tier") or "child"
        eff, new = effective_tier(rk)
        if new != cur:
            changed += 1
            if rank.get(new, 0) < rank.get(cur, 0):
                demoted += 1
            print(f"{rk:<46} {int(c.get('n_observations') or 0):>6} "
                  f"{eff['effective_n']:>6} {cur:>11} {new:>11}")
        # On --commit, recompute EVERY rule (not just tier changes) so the
        # effective_* stats are persisted for all rows — the gate already
        # demoted tiers, but the dashboard + recompute_maturity_flags need the
        # effective_* columns populated.
        if args.commit:
            price_agent.recompute_rule_payoff(rk)
    print("-" * 86)
    print(f"tier changes: {changed} ({demoted} demotions); recomputed all {len(cal)} on commit")
    if not args.commit:
        print("\nDRY-RUN — nothing written. Re-run with --commit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
