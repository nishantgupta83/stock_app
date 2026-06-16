#!/usr/bin/env python3
"""Emit an auditable SQL transaction that rebuilds stock_rule_calibration from a
LOCAL snapshot under an exit policy. OFFLINE — zero Supabase read egress.

Recomputes the FULL field set the live path writes (raw counters from
upsert_calibration, payoff + PF-gated flags from recompute_rule_payoff, and the
effective_* columns from _persist_effective_stats), so the row stays internally
consistent — not a partial overwrite. Apply via the Supabase SQL editor (a write,
no read egress). The 30d-window fields (brier_30d/accuracy_30d/n_closed_30d)
self-heal on the next live reconcile and are intentionally left untouched.

Usage:
  REGRADE_DIR=regrade_data_full python3 scripts/emit_calibration_sql.py --policy stop_only
  -> writes <REGRADE_DIR>/rebuild_<policy>.sql  (+ backup_calibration.sql)
"""
from __future__ import annotations
import argparse, json, os, sys, datetime as dt
from collections import defaultdict
from pathlib import Path

os.environ.setdefault("SUPABASE_URL", "http://offline.local")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "offline")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "agents"))
from price_agent import compute_paper_outcome  # noqa: E402
from _maturity import collapse_to_effective, derive_maturity_flags  # noqa: E402

REGRADE_DIR = Path(os.environ.get("REGRADE_DIR", "regrade_data"))
NOW = dt.datetime.now(dt.timezone.utc).isoformat()


def load() -> tuple[list[dict], dict]:
    trades = [json.loads(l) for l in (REGRADE_DIR / "trades.jsonl").read_text().splitlines() if l.strip()]
    bars = {}
    for p in (REGRADE_DIR / "bars").glob("*.json"):
        raw = json.loads(p.read_text())
        bars[p.stem] = {dt.date.fromisoformat(d): {"open": o, "high": h, "low": lo, "close": c}
                        for d, (o, h, lo, c) in raw.items()}
    return trades, bars


def sqlval(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    return "'" + str(v).replace("'", "''") + "'"


def rule_payload(graded: list[dict]) -> dict:
    """Match the live recompute formulas exactly (price_agent.recompute_rule_payoff
    + upsert_calibration counters + _persist_effective_stats)."""
    rows = graded
    n = len(rows)
    n_correct = sum(1 for r in rows if r["correct"])
    accuracy = round(n_correct / n, 6) if n else 0.0
    mean_realized = round(sum(r["realized_return"] for r in rows) / n, 6) if n else 0.0
    p = {"n_observations": n, "n_correct": n_correct, "accuracy": accuracy,
         "mean_realized_pct": mean_realized}
    if n >= 5:
        rets = [r["realized_return"] for r in rows]
        wins = [v for v in rets if v > 0]; losses = [v for v in rets if v <= 0]
        sw, sl = sum(wins), sum(losses)
        mfe_v = [float(r["mfe_pct"]) for r in rows if r.get("mfe_pct") is not None]
        mae_v = [float(r["mae_pct"]) for r in rows if r.get("mae_pct") is not None]
        p.update({
            "median_return_pct": round(sorted(rets)[len(rets) // 2], 6),
            "avg_win_pct":  round(sw / len(wins), 6) if wins else None,
            "avg_loss_pct": round(sl / len(losses), 6) if losses else None,
            "profit_factor": round(sw / abs(sl), 4) if sl < 0 else None,
            "target_hit_rate": round(sum(1 for r in rows if r.get("target_hit") is True) / n, 4),
            "stop_hit_rate":   round(sum(1 for r in rows if r.get("stop_hit") is True) / n, 4),
            "mean_mfe_pct": round(sum(mfe_v) / len(mfe_v), 6) if mfe_v else None,
            "mean_mae_pct": round(sum(mae_v) / len(mae_v), 6) if mae_v else None,
        })
    eff = collapse_to_effective(rows)
    flags = derive_maturity_flags(eff["effective_n"], eff["effective_profit_factor"],
                                  eff["effective_mean_realized_pct"], eff["effective_accuracy"])
    p.update({
        "effective_n": eff["effective_n"], "effective_n_correct": eff["effective_n_correct"],
        "effective_accuracy": round(eff["effective_accuracy"], 6),
        "effective_mean_realized_pct": round(eff["effective_mean_realized_pct"], 6),
        "effective_profit_factor": (round(eff["effective_profit_factor"], 4)
                                    if eff["effective_profit_factor"] is not None else None),
        "is_mature": flags["is_mature"], "is_mature_70": flags["is_mature_70"],
        "is_mature_80": flags["is_mature_80"], "tier": flags["tier"],
        "matured_at":    NOW if flags["is_mature"] else None,
        "matured_70_at": NOW if flags["is_mature_70"] else None,
        "matured_80_at": NOW if flags["is_mature_80"] else None,
        "last_payoff_recomputed_at": NOW,
    })
    return p


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--policy", default="stop_only", choices=("stop_only", "trail", "hold"))
    args = ap.parse_args()
    if args.policy == "trail":
        print("trail is comparison-only (not in the canonical grader); "
              "use stop_only or hold for a committable rebuild.", file=sys.stderr)
        return 2

    trades, bars = load()
    by_rule: dict[str, list[dict]] = defaultdict(list)
    trade_rows: list[tuple] = []   # (id, realized_return, exit_at, exit_price, correct, target_hit, stop_hit)
    for t in trades:
        b = bars.get(t["ticker"])
        if not b:
            continue
        o = compute_paper_outcome(t, b, exit_policy=args.policy)
        if o is None:
            continue
        # Isolate the POLICY effect: rewrite ONLY trades the stop actually changed
        # (exit_reason == "stop"). Trades that rode to the horizon are identical to
        # the stored hold grading — keep their stored values so the rebuild reflects
        # the stop policy, not a bar-source switch.
        if o["exit_reason"] == "stop":
            rec = {"realized_return": o["realized_return"], "correct": o["correct"],
                   "target_hit": o["target_hit"], "stop_hit": o["stop_hit"],
                   "mfe_pct": o["mfe_pct"], "mae_pct": o["mae_pct"]}
            if t.get("id") is not None:
                trade_rows.append((t["id"], o["realized_return"], o["exit_at"], o["exit_price"],
                                   o["correct"], o["target_hit"], o["stop_hit"]))
        else:
            sr = t.get("realized_return")
            rec = {"realized_return": float(sr) if sr is not None else o["realized_return"],
                   "correct": t.get("correct"), "target_hit": t.get("target_hit"),
                   "stop_hit": t.get("stop_hit"), "mfe_pct": t.get("mfe_pct"),
                   "mae_pct": t.get("mae_pct")}
            if rec["correct"] is None:
                rec["correct"] = rec["realized_return"] > 0
        rec["ticker"] = t["ticker"]
        rec["entry_at"] = t["entry_at"]
        by_rule[t["rule_key"]].append(rec)
    n_changed = len(trade_rows)

    # ---- (1) TRADE re-grade: the DURABLE step. Without it the live reconcile
    # recomputes calibration from the still-hold-graded trades and reverts the
    # rebuild (archive is DRY_RUN, so historical trades never age out). ----
    tlines = [
        f"-- stock_event_paper_trades re-grade under exit_policy='{args.policy}'",
        f"-- generated {NOW} OFFLINE ({len(trade_rows)} closed trades; ~{n_changed} change value).",
        "-- APPLY THIS BEFORE the calibration rebuild. Chunked UPDATE..FROM (VALUES).",
        "BEGIN;",
    ]
    for i in range(0, len(trade_rows), 1000):
        ch = trade_rows[i:i + 1000]
        vals = ",\n".join(
            f"  ({tid}, {sqlval(rr)}, {sqlval(ea)}, {sqlval(ep)}, {sqlval(c)}, {sqlval(th)}, {sqlval(sh)})"
            for (tid, rr, ea, ep, c, th, sh) in ch)
        tlines += [
            "UPDATE stock_event_paper_trades AS t SET",
            "  realized_return = v.realized_return::numeric, exit_at = v.exit_at::timestamptz,",
            "  exit_price = v.exit_price::numeric, correct = v.correct::boolean,",
            "  target_hit = v.target_hit::boolean, stop_hit = v.stop_hit::boolean",
            "FROM (VALUES\n" + vals,
            ") AS v(id, realized_return, exit_at, exit_price, correct, target_hit, stop_hit)",
            "WHERE t.id = v.id;",
        ]
    tlines.append("COMMIT;")
    tout = REGRADE_DIR / f"trades_regrade_{args.policy}.sql"
    tout.write_text("\n".join(tlines) + "\n")

    lines = [
        f"-- stock_rule_calibration rebuild under exit_policy='{args.policy}'",
        f"-- generated {NOW} OFFLINE from {REGRADE_DIR}/ ({len(trades)} closed trades, zero read egress).",
        "-- Apply in the Supabase SQL editor (a write; no read egress). Idempotent UPDATEs.",
        "-- Leaves brier_30d/accuracy_30d/n_closed_30d untouched (self-heal next reconcile).",
        "BEGIN;",
    ]
    cols_order = ["n_observations", "n_correct", "accuracy", "mean_realized_pct",
                  "median_return_pct", "avg_win_pct", "avg_loss_pct", "profit_factor",
                  "target_hit_rate", "stop_hit_rate", "mean_mfe_pct", "mean_mae_pct",
                  "effective_n", "effective_n_correct", "effective_accuracy",
                  "effective_mean_realized_pct", "effective_profit_factor",
                  "is_mature", "is_mature_70", "is_mature_80", "tier",
                  "matured_at", "matured_70_at", "matured_80_at", "last_payoff_recomputed_at"]
    n_adult = 0
    for rk in sorted(by_rule):
        p = rule_payload(by_rule[rk])
        if p["is_mature"]:
            n_adult += 1
        sets = ", ".join(f"{c} = {sqlval(p[c])}" for c in cols_order if c in p)
        lines.append(f"UPDATE stock_rule_calibration SET {sets} WHERE rule_key = {sqlval(rk)};")
    lines += [
        "-- verification: adult/teen/young counts AFTER the rebuild",
        "SELECT tier, count(*) FROM stock_rule_calibration GROUP BY tier ORDER BY tier;",
        "SELECT count(*) AS adult_rules FROM stock_rule_calibration WHERE is_mature;",
        "COMMIT;",
    ]
    out = REGRADE_DIR / f"rebuild_{args.policy}.sql"
    out.write_text("\n".join(lines) + "\n")

    backup = REGRADE_DIR / "backup_before_regrade.sql"
    backup.write_text(
        "-- ROLLBACK POINT — run FIRST, before either regrade file.\n"
        "CREATE TABLE IF NOT EXISTS stock_event_paper_trades_bak_20260615 AS\n"
        "  SELECT * FROM stock_event_paper_trades WHERE status = 'closed';\n"
        "CREATE TABLE IF NOT EXISTS stock_rule_calibration_bak_20260615 AS\n"
        "  SELECT * FROM stock_rule_calibration;\n")

    print(f"APPLY ORDER (Supabase SQL editor / psql — all writes, zero read egress):")
    print(f"  1. {backup}")
    print(f"  2. {tout}   ({len(trade_rows)} trades, ~{n_changed} change)")
    print(f"  3. {out}    ({len(by_rule)} rule UPDATEs; {n_adult} adult after rebuild)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
