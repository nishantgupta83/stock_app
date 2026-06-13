#!/usr/bin/env python3
"""repair_calibration_counters.py — C1 one-shot: recompute n_observations /
n_correct / accuracy / mean_realized_pct in stock_rule_calibration from the TRUE
closed-trade population, undoing the DRY_RUN archive-ratchet inflation (1.5-2.5x).

PRECONDITIONS (deploy the C1 code FIRST, else this gets re-floored within 2h):
  - price_agent.enrich_cal_from_archive now ignores unversioned/poisoned indexes.
  - archive_agent no longer merges/saves the index in DRY_RUN.
Run AFTER a price_agent reconcile (and ideally pause price_agent + don't run
backfill_paper_trades.py) so no close races the repair.

n is the count of status=closed trades with NON-NULL correct AND realized_return
(the learning observations). status=expired/skipped and null-outcome rows do NOT
count (null-outcome closed rows are reported as data errors).

USAGE (private shell, SUPABASE_URL + SUPABASE_SERVICE_KEY):
  python3 scripts/repair_calibration_counters.py            # DRY-RUN (default): show diffs
  python3 scripts/repair_calibration_counters.py --commit   # PATCH the counters
  # then: python3 scripts/recompute_maturity_flags.py --commit   # re-derive tiers
"""
from __future__ import annotations

import argparse
import os
import sys

import requests

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
H = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}


def _page(table: str, params: dict) -> list[dict]:
    rows, off, page = [], 0, 1000
    while True:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/{table}", headers=H,
                         params={**params, "offset": str(off), "limit": str(page)}, timeout=60)
        r.raise_for_status()
        b = r.json(); rows.extend(b)
        if len(b) < page:
            break
        off += page
    return rows


def fetch_all_cal() -> dict[str, dict]:
    rows = _page("stock_rule_calibration",
                 {"select": "rule_key,n_observations,n_correct,accuracy,mean_realized_pct",
                  "order": "rule_key.asc"})
    return {r["rule_key"]: r for r in rows}


def fetch_trade_rule_keys() -> set[str]:
    """Distinct rule_keys present in the closed-trade corpus (union coverage)."""
    rows = _page("stock_event_paper_trades",
                 {"status": "eq.closed", "select": "rule_key", "order": "id.asc"})
    return {r["rule_key"] for r in rows if r.get("rule_key")}


def archive_deletion_count() -> int:
    """Hard preflight: active-table counts are the true population ONLY if no
    closed row was ever archived/deleted. If any archived_at is set, abort."""
    r = requests.get(f"{SUPABASE_URL}/rest/v1/stock_event_paper_trades",
                     headers={**H, "Prefer": "count=exact", "Range-Unit": "items", "Range": "0-0"},
                     params={"archived_at": "not.is.null", "select": "id"}, timeout=30)
    return int(r.headers.get("content-range", "?/0").split("/")[-1] or 0)


def true_stats(rule_key: str) -> dict:
    """(n, n_correct, accuracy, mean, n_null_outcome) over the full closed population."""
    returns, correct, n_null = [], 0, 0
    off, page = 0, 1000
    while True:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/stock_event_paper_trades", headers=H,
                         params={"rule_key": f"eq.{rule_key}", "status": "eq.closed",
                                 "select": "correct,realized_return", "order": "id.asc",
                                 "offset": str(off), "limit": str(page)}, timeout=60)
        r.raise_for_status()
        b = r.json()
        for t in b:
            if t.get("correct") is None or t.get("realized_return") is None:
                n_null += 1
                continue
            returns.append(float(t["realized_return"]))
            correct += 1 if t["correct"] else 0
        if len(b) < page:
            break
        off += page
    n = len(returns)
    return {"n": n, "n_correct": correct,
            "accuracy": round(correct / n, 6) if n else 0.0,
            "mean": round(sum(returns) / n, 6) if n else None,
            "n_null": n_null}


def upsert(rule_key: str, fields: dict) -> None:
    # merge-duplicates: update the counter fields, leave all other columns (PF,
    # tier flags, etc.) intact. Creates the row if a trade-only key lacks one.
    r = requests.post(f"{SUPABASE_URL}/rest/v1/stock_rule_calibration",
                      headers={**H, "Content-Type": "application/json",
                               "Prefer": "return=minimal,resolution=merge-duplicates"},
                      params={"on_conflict": "rule_key"},
                      json=[{"rule_key": rule_key, **fields}], timeout=30)
    r.raise_for_status()


def _diff(cur: dict, t: dict) -> bool:
    if int(cur.get("n_observations") or 0) != t["n"]:
        return True
    if int(cur.get("n_correct") or 0) != t["n_correct"]:
        return True
    if abs(float(cur.get("accuracy") or 0) - t["accuracy"]) > 1e-6:
        return True
    cm, tm = cur.get("mean_realized_pct"), t["mean"]
    if (cm is None) != (tm is None):
        return True
    if cm is not None and tm is not None and abs(float(cm) - float(tm)) > 1e-6:
        return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--commit", action="store_true", help="actually upsert (default: dry-run)")
    args = ap.parse_args()
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("ERROR: set SUPABASE_URL + SUPABASE_SERVICE_KEY.", file=sys.stderr); return 2

    # HARD PREFLIGHT: active-table counts are the full population only if nothing
    # was ever archived/deleted (Codex). Abort otherwise — repairing from the
    # active tier alone would erase archived history.
    n_arch = archive_deletion_count()
    if n_arch > 0:
        print(f"ABORT: {n_arch} trades have archived_at set — active table is NOT the full "
              f"population. Repair source must merge archived JSONL first.", file=sys.stderr)
        return 3

    cal = fetch_all_cal()
    keys = sorted(set(cal) | fetch_trade_rule_keys())
    print(f"{'DRY-RUN' if not args.commit else 'COMMIT'} — {len(cal)} cal rows, "
          f"{len(keys)} union keys (archived=0 ✓)\n")
    print(f"{'rule_key':<46} {'cur_n':>7} {'true_n':>7} {'ratio':>6}  flags")
    print("─" * 88)
    n_changed = n_inflated = n_zero = n_nullrows = n_orphan = 0
    rows = []
    for rk in keys:
        cur = cal.get(rk, {})
        cur_n = int(cur.get("n_observations") or 0)
        t = true_stats(rk)
        rows.append((cur_n, rk, cur, t))
    for cur_n, rk, cur, t in sorted(rows, key=lambda x: -x[0]):
        flags = []
        if rk not in cal:
            flags.append("NO_CAL_ROW(seed)"); n_orphan += 1
        if t["n"] == 0 and cur_n > 0:
            flags.append("TRUE_N=0(poison)"); n_zero += 1
        if cur_n > t["n"] * 1.05 and t["n"] > 0:
            flags.append(f"inflated {cur_n/max(t['n'],1):.2f}x"); n_inflated += 1
        if t["n_null"]:
            flags.append(f"{t['n_null']} null-outcome"); n_nullrows += t["n_null"]
        if _diff(cur, t):
            n_changed += 1
            ratio = f"{cur_n/max(t['n'],1):.2f}" if t["n"] else "inf"
            print(f"{rk:<46} {cur_n:>7} {t['n']:>7} {ratio:>6}  {' '.join(flags)}")
            if args.commit:
                upsert(rk, {"n_observations": t["n"], "n_correct": t["n_correct"],
                            "accuracy": t["accuracy"], "mean_realized_pct": t["mean"]})
    print("─" * 88)
    print(f"changed={n_changed}  inflated={n_inflated}  true_n=0={n_zero}  "
          f"orphan(no_cal_row)={n_orphan}  null-outcome-rows={n_nullrows}")
    if not args.commit:
        print("\nDRY-RUN — nothing written. Re-run with --commit to repair, then:")
        print("  python3 scripts/recompute_maturity_flags.py --commit")
    else:
        print("\nCommitted. NOW run: python3 scripts/recompute_maturity_flags.py --commit")
    return 0


if __name__ == "__main__":
    sys.exit(main())
