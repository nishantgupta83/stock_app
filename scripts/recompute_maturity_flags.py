#!/usr/bin/env python3
"""One-shot recompute of is_mature / is_mature_70 / is_mature_80 flags
in stock_rule_calibration after the 2026-06-04 adult-gate redefinition.

WHY this script exists:
  upsert_calibration() only updates a rule's flags when a NEW trade closes
  on that rule. Rules without fresh trades stay flagged under the OLD gate
  semantics indefinitely. After we change ADULT_MIN_N/PF/MEAN constants,
  the flags in the DB are stale until each rule next sees a trade.

  This script reads every rule, applies the NEW gate logic, and PATCHes
  the is_mature_* flags in-place. Idempotent — re-running with the same
  thresholds is a no-op.

USAGE:
  python3 scripts/recompute_maturity_flags.py            # dry-run
  python3 scripts/recompute_maturity_flags.py --commit   # actually write
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone


SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
HEADERS = {
    "apikey": SUPABASE_SERVICE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    "Content-Type": "application/json",
}

# Mirror agents/price_agent.py module-level constants so a divergence is
# caught at review time, not in production.
MATURITY_MIN_N         = 30
TIER_GATE_TEEN_ACC     = 0.70
TIER_GATE_TEEN_MR      = 0.0
TIER_GATE_YOUNG_ACC    = 0.80
TIER_GATE_YOUNG_PF     = 1.2
ADULT_MIN_N            = 100
ADULT_MIN_PF           = 2.0
ADULT_MIN_MEAN         = 0.005


def fetch_all_rules() -> list[dict]:
    rows: list[dict] = []
    offset = 0
    page = 500
    while True:
        params = {
            "select": "rule_key,n_observations,accuracy,profit_factor,mean_realized_pct,"
                      "is_mature,is_mature_70,is_mature_80,matured_at,matured_70_at,matured_80_at,tier",
            "limit":  str(page),
            "offset": str(offset),
        }
        qs = urllib.parse.urlencode(params, safe=".,:*=&")
        req = urllib.request.Request(
            f"{SUPABASE_URL}/rest/v1/stock_rule_calibration?{qs}",
            headers={"apikey": SUPABASE_SERVICE_KEY,
                     "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}"},
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            chunk = json.loads(r.read())
        if not chunk:
            break
        rows.extend(chunk)
        if len(chunk) < page:
            break
        offset += page
    return rows


def evaluate(r: dict) -> dict:
    """Apply the NEW gate logic; return desired flag values + tier."""
    n   = int(r.get("n_observations") or 0)
    acc = float(r.get("accuracy") or 0)
    pf  = r.get("profit_factor")
    pf  = float(pf) if pf is not None else None
    mean = float(r.get("mean_realized_pct") or 0)

    is_mature_70 = bool(n >= MATURITY_MIN_N and acc >= TIER_GATE_TEEN_ACC
                        and mean > TIER_GATE_TEEN_MR)
    is_mature_80 = bool(n >= MATURITY_MIN_N and acc >= TIER_GATE_YOUNG_ACC
                        and pf is not None and pf > TIER_GATE_YOUNG_PF)
    is_mature = bool(n >= ADULT_MIN_N and pf is not None
                     and pf >= ADULT_MIN_PF and mean >= ADULT_MIN_MEAN)

    if is_mature:        tier = "adult"
    elif is_mature_80:   tier = "young_adult"
    elif is_mature_70:   tier = "teen"
    else:                tier = "child"

    return {
        "is_mature":    is_mature,
        "is_mature_70": is_mature_70,
        "is_mature_80": is_mature_80,
        "tier":         tier,
    }


def needs_update(current: dict, desired: dict) -> dict | None:
    """Return a PATCH payload of only the fields that differ."""
    patch: dict = {}
    for key in ("is_mature", "is_mature_70", "is_mature_80", "tier"):
        cur_val = current.get(key)
        if key == "tier" and cur_val is None:
            cur_val = "child"
        if cur_val != desired[key]:
            patch[key] = desired[key]
    # Stamp matured_at when crossing False -> True; CLEAR it on True -> False
    # (demotion) so a tier that was falsely matured on corrupted n doesn't keep
    # a stale maturation timestamp after C1's counter repair.
    now = datetime.now(timezone.utc).isoformat()
    for flag, stamp in (("is_mature", "matured_at"),
                        ("is_mature_70", "matured_70_at"),
                        ("is_mature_80", "matured_80_at")):
        if desired[flag] and not current.get(flag) and not current.get(stamp):
            patch[stamp] = now
        elif not desired[flag] and current.get(stamp) is not None:
            patch[stamp] = None     # demoted -> clear stale maturation timestamp
    return patch or None


def patch_rule(rule_key: str, payload: dict) -> None:
    params = urllib.parse.urlencode({"rule_key": f"eq.{rule_key}"}, safe=".,:*=&")
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/stock_rule_calibration?{params}",
        data=json.dumps(payload).encode(),
        method="PATCH",
        headers={**HEADERS, "Prefer": "return=minimal"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        r.read()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true", help="Actually write to DB")
    args = ap.parse_args()
    dry = not args.commit

    print(f"recompute_maturity_flags: dry_run={dry}")
    rules = fetch_all_rules()
    print(f"  fetched {len(rules)} rules")

    promotions: list[tuple[str, dict]] = []
    demotions:  list[tuple[str, dict]] = []
    tier_only:  list[tuple[str, str, str]] = []
    unchanged = 0

    for r in rules:
        desired = evaluate(r)
        patch = needs_update(r, desired)
        if not patch:
            unchanged += 1
            continue
        if patch.get("is_mature") is True:
            promotions.append((r["rule_key"], patch))
        elif patch.get("is_mature") is False:
            demotions.append((r["rule_key"], patch))
        elif "tier" in patch:
            tier_only.append((r["rule_key"], r.get("tier") or "child", patch["tier"]))
        if not dry:
            patch_rule(r["rule_key"], patch)

    print()
    print(f"=== Recompute summary ({'DRY' if dry else 'COMMITTED'}) ===")
    print(f"  unchanged                 : {unchanged}")
    print(f"  promoted to adult         : {len(promotions)}")
    print(f"  demoted from adult        : {len(demotions)}")
    print(f"  tier-only delta (no adult): {len(tier_only)}")
    if promotions:
        print("\nNewly ADULT rules:")
        for rk, p in promotions:
            print(f"  + {rk}")
    if demotions:
        print("\nDemoted from ADULT:")
        for rk, p in demotions:
            print(f"  - {rk}")
    if tier_only:
        print(f"\nOther tier changes (sample):")
        for rk, old, new in tier_only[:8]:
            print(f"    {rk}: {old} -> {new}")
    if dry:
        print("\nRe-run with --commit to write changes.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
