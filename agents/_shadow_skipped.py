"""Pure functions for the shadow-skipped forward-return audit. No DB, no network.
For each priceable skipped setup: forward stop_only return vs matched QQQ window,
stratified by WHY it was skipped. Per-setup + capacity-free (categories never compete)."""
from __future__ import annotations
import statistics as st

CATEGORIES = ("payoff", "vocabulary", "instrument", "other")

def categorize_skip(reason):
    r = (reason or "").lower()
    if "profit_factor" in r or "no payoff edge" in r:
        return "payoff"
    if "avoid_chase" in r or "chase_risk" in r or "intelligence flagged" in r:
        return "vocabulary"
    if "not a tradeable instrument" in r or "fund" in r or "placeholder" in r:
        return "instrument"
    return "other"

def aggregate(rows):
    resolved = [x for x in rows if x.get("status") == "resolved"]
    if not resolved:
        return {"n_setups": len(rows), "n_resolved": 0, "status": "insufficient"}
    rets = [float(x["return_pct"]) for x in resolved]
    exc = [float(x["excess_pct"]) for x in resolved]
    wins = sum(1 for e in exc if e > 0)
    return {"n_setups": len(rows), "n_resolved": len(resolved),
            "mean_return_pct": round(st.mean(rets), 4),
            "mean_excess_vs_qqq_pct": round(st.mean(exc), 4),
            "win_rate": round(wins / len(resolved), 4),
            "median_excess_pct": round(st.median(exc), 4), "status": "ok"}

def by_category(rows):
    out = {c: aggregate([x for x in rows if x.get("skip_category") == c]) for c in CATEGORIES}
    out["overall_priceable"] = aggregate([x for x in rows if x.get("priceable")])
    return out

def anomaly_audit(rows):
    return [{"ticker": x.get("ticker"), "reason_to_skip": x.get("reason_to_skip"),
             "return_pct": x.get("return_pct"), "excess_pct": x.get("excess_pct")}
            for x in rows if x.get("skip_category") == "instrument" and x.get("priceable")]

def reason_distribution(rows):
    d = {}
    for x in rows:
        k = x.get("reason_to_skip") or ""
        d[k] = d.get(k, 0) + 1
    return d
