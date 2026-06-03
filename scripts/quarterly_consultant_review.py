#!/usr/bin/env python3
"""Quarterly consultant review — independent agent that feeds back into the pipeline.

Runs on demand or via a quarterly cron. Reads:
  * The most recent ~3 months of YYYYMM_monthly_reconc.md in docs/learning/
  * Live stock_rule_calibration  (where each rule is right now)
  * Live stock_health_pulse_current (operational state)
  * Live stock_event_paper_trades (recent close volume + win-rate trend)

Produces a single doc `docs/learning/YYYYQq_consultant_review.md` —
deterministic, rule-based 'consultant' insights. No LLM, no opinions,
just thresholds applied across longer time horizons than the monthly
reconciler sees.

What 'feeds back into the pipeline' means here:
  - The doc lists CONCRETE actions (rule flips, structural skips, sizing
    amplifications) the operator can ship next week. These are NOT
    auto-applied — the consultant proposes; the operator decides.
  - Operator's path to ship a flip:
      1. Read the consultant doc.
      2. If conviction matches the data, add the rule_key to a
         STRUCTURAL_FLIP set in agents/thesis_agent.py.
      3. Feature-flag it (FLIP_ENABLED env var like SECTOR_CALIB_MULT).
      4. Watch pulsecheck_thesis.rejection_distribution to confirm impact.

This script doesn't change live rules. It writes a single review doc.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone


SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
HEADERS = {
    "apikey": SUPABASE_SERVICE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
}

# Mirror the sequential replay thresholds so monthly + quarterly produce
# consistent recommendations.
MIN_N_FOR_LEARNING = 30
FLIP_PF_MAX        = 1.0
FLIP_ACC_MAX       = 0.50
SKIP_ACC_MAX       = 0.30
AMPLIFY_PF_MIN     = 2.0
AMPLIFY_ACC_MIN    = 0.60


def paginate(table: str, params: dict[str, str], page: int = 1000) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while True:
        q = dict(params)
        q["limit"], q["offset"] = str(page), str(offset)
        qs = urllib.parse.urlencode(q, safe=".,:*=&")
        req = urllib.request.Request(f"{SUPABASE_URL}/rest/v1/{table}?{qs}", headers=HEADERS)
        with urllib.request.urlopen(req, timeout=60) as r:
            chunk = json.loads(r.read())
        if not chunk:
            break
        rows.extend(chunk)
        if len(chunk) < page:
            break
        offset += page
    return rows


def fmt_money(v: float) -> str:
    sign = "-" if v < 0 else ""
    return f"{sign}${abs(v):,.2f}"


def fmt_pct(v: float) -> str:
    return f"{v*100:+.2f}%"


def current_quarter_label(now: datetime) -> tuple[str, str, str]:
    """Return (Q_label, q_start_date_iso, q_end_date_iso) for the quarter
    that just ended. If we're inside Q1 2026, this returns "2025Q4"."""
    y, m = now.year, now.month
    # Quarter that just ended:
    if m <= 3:
        return (f"{y-1}Q4", f"{y-1}-10-01", f"{y-1}-12-31")
    if m <= 6:
        return (f"{y}Q1", f"{y}-01-01", f"{y}-03-31")
    if m <= 9:
        return (f"{y}Q2", f"{y}-04-01", f"{y}-06-30")
    return (f"{y}Q3", f"{y}-07-01", f"{y}-09-30")


def read_recent_monthly_docs(docs_dir: str) -> list[tuple[str, str]]:
    """Return last 3 monthly reconc docs as (filename, content)."""
    if not os.path.exists(docs_dir):
        return []
    files = sorted(
        f for f in os.listdir(docs_dir)
        if f.endswith("_monthly_reconc.md") and len(f) >= 12
    )
    last3 = files[-3:]
    return [(f, open(os.path.join(docs_dir, f)).read()) for f in last3]


def main() -> int:
    now = datetime.now(timezone.utc)
    out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "docs", "learning")
    os.makedirs(out_dir, exist_ok=True)
    q_label, q_start, q_end = current_quarter_label(now)
    out_path = os.path.join(out_dir, f"{q_label}_consultant_review.md")

    # --- Read live data
    print("Reading live rule_calibration…", file=sys.stderr)
    cal = paginate("stock_rule_calibration", {
        "select": "rule_key,n_observations,accuracy,profit_factor,tier,is_mature,"
                  "is_mature_70,is_mature_80,last_updated",
    })
    print(f"  {len(cal)} rule_keys", file=sys.stderr)

    # Closed trades in the quarter window (for recent activity view)
    print("Reading closed trades from the quarter…", file=sys.stderr)
    quarter_trades = paginate("stock_event_paper_trades", {
        "status":   "eq.closed",
        "exit_at":  f"gte.{q_start}T00:00:00Z",
        "and":      f"(exit_at.lte.{q_end}T23:59:59Z)",
        "select":   "rule_key,realized_return,correct,ticker",
    })
    print(f"  {len(quarter_trades)} closed trades in {q_label}", file=sys.stderr)

    # Pulsecheck health snapshot
    print("Reading current pulsecheck state…", file=sys.stderr)
    pulses = paginate("stock_health_pulse_current", {
        "select": "agent,check_name,status,detail",
    })

    # Recent monthly docs (last 3) for chronology
    recent_docs = read_recent_monthly_docs(out_dir)

    # --- Per-rule analysis: which rules cross the action thresholds?
    flip_candidates: list[dict] = []
    skip_candidates: list[dict] = []
    amp_candidates:  list[dict] = []
    for r in cal:
        try:
            n = int(r.get("n_observations") or 0)
            if n < MIN_N_FOR_LEARNING:
                continue
            acc = float(r.get("accuracy") or 0)
            pf  = r.get("profit_factor")
            pf  = float(pf) if pf is not None else None

            if acc < SKIP_ACC_MAX:
                skip_candidates.append({"rule_key": r["rule_key"],
                                         "n": n, "acc": acc, "pf": pf})
            elif pf is not None and pf < FLIP_PF_MAX and acc < FLIP_ACC_MAX:
                flip_candidates.append({"rule_key": r["rule_key"],
                                         "n": n, "acc": acc, "pf": pf})
            elif pf is not None and pf >= AMPLIFY_PF_MIN and acc >= AMPLIFY_ACC_MIN:
                amp_candidates.append({"rule_key": r["rule_key"],
                                        "n": n, "acc": acc, "pf": pf})
        except (TypeError, ValueError):
            continue

    flip_candidates.sort(key=lambda x: (x["pf"] or 9, -x["n"]))
    skip_candidates.sort(key=lambda x: x["acc"])
    amp_candidates.sort(key=lambda x: -(x["pf"] or 0))

    # --- Quarterly volume + win-rate by rule_key
    by_rule: dict[str, dict] = defaultdict(lambda: {"n": 0, "wins": 0, "pnl": 0.0})
    for t in quarter_trades:
        rk = t.get("rule_key") or "unknown"
        r = float(t.get("realized_return") or 0)
        by_rule[rk]["n"] += 1
        if r > 0: by_rule[rk]["wins"] += 1
        by_rule[rk]["pnl"] += r

    # --- Build doc
    md: list[str] = []
    md.append(f"# Quarterly consultant review — {q_label}")
    md.append("")
    md.append(f"_Generated {now.date().isoformat()}. Window: {q_start} → {q_end}._")
    md.append("")
    md.append("**Independent automated review.** Reads the last 3 monthly "
              "reconciliations, the live `stock_rule_calibration` table, the "
              "pulsecheck dashboard, and closed paper trades for the quarter. "
              "Produces deterministic recommendations using the same thresholds "
              "the monthly reconciler uses. **Not LLM-generated — no judgment "
              "calls beyond the stated thresholds.**")
    md.append("")
    md.append("Feeds back into the pipeline as: a list of concrete rule-level "
              "actions the operator can ship next week. These are NOT auto-applied — "
              "the consultant proposes; the operator decides.")
    md.append("")

    # Quarter activity summary
    total_n = sum(v["n"] for v in by_rule.values())
    total_wins = sum(v["wins"] for v in by_rule.values())
    total_pnl_pct = sum(v["pnl"] for v in by_rule.values())
    wr = total_wins / max(1, total_n)
    md.append("## Quarter at a glance")
    md.append("")
    md.append("| Metric | Value |")
    md.append("|---|---|")
    md.append(f"| Closed trades in window | {total_n} |")
    md.append(f"| Wins / Losses | {total_wins} / {total_n - total_wins} |")
    md.append(f"| Aggregate win-rate | {wr:.1%} |")
    md.append(f"| Sum of realized return % across all trades | {total_pnl_pct*100:.1f}% |")
    md.append(f"| Distinct rule_keys producing trades | {len(by_rule)} |")
    md.append("")

    # Pipeline health (pulsecheck overview)
    md.append("## Pipeline health snapshot")
    md.append("")
    if pulses:
        statuses = defaultdict(int)
        for p in pulses:
            statuses[p["status"]] += 1
        md.append("| Status | Count |")
        md.append("|---|---|")
        for st in ("ok", "warning", "critical", "precondition_failed", "skipped"):
            if statuses.get(st):
                md.append(f"| {st} | {statuses[st]} |")
        md.append("")
        warnings = [p for p in pulses if p["status"] in ("warning", "critical")]
        if warnings:
            md.append("### Active warnings/criticals")
            md.append("")
            md.append("| Agent | Check | Status | Detail |")
            md.append("|---|---|---|---|")
            for p in warnings:
                md.append(f"| `{p['agent']}` | `{p['check_name']}` | {p['status']} | "
                          f"{(p['detail'] or '')[:55]} |")
            md.append("")
    else:
        md.append("_(No pulses found — pulsecheck workflow may not have run recently.)_")
        md.append("")

    # Recommended actions
    md.append("## Recommended actions (data-driven)")
    md.append("")
    if flip_candidates:
        md.append(f"### Direction flips ({len(flip_candidates)} rules cross PF<{FLIP_PF_MAX} AND acc<{FLIP_ACC_MAX:.0%} at n≥{MIN_N_FOR_LEARNING})")
        md.append("")
        md.append("Adding these to a STRUCTURAL_FLIP set in `agents/thesis_agent.py` "
                  "and feature-flagging would invert their direction. The evidence "
                  "is they are losing money in the original direction with enough "
                  "sample to trust the verdict.")
        md.append("")
        md.append("| rule_key | n | acc | PF |")
        md.append("|---|---|---|---|")
        for c in flip_candidates[:15]:
            md.append(f"| `{c['rule_key']}` | {c['n']} | {c['acc']:.1%} | {c['pf']:.2f} |")
        md.append("")
    if skip_candidates:
        md.append(f"### Structural skips ({len(skip_candidates)} rules cross acc<{SKIP_ACC_MAX:.0%} at n≥{MIN_N_FOR_LEARNING})")
        md.append("")
        md.append("These rule_keys have severely low accuracy with significant "
                  "sample. Both directions are losing — better to NOT emit signals "
                  "on these at all.")
        md.append("")
        md.append("| rule_key | n | acc | PF |")
        md.append("|---|---|---|---|")
        for c in skip_candidates[:15]:
            pf_str = f"{c['pf']:.2f}" if c['pf'] is not None else "—"
            md.append(f"| `{c['rule_key']}` | {c['n']} | {c['acc']:.1%} | {pf_str} |")
        md.append("")
    if amp_candidates:
        md.append(f"### Amplifications ({len(amp_candidates)} rules cross PF≥{AMPLIFY_PF_MIN} AND acc≥{AMPLIFY_ACC_MIN:.0%} at n≥{MIN_N_FOR_LEARNING})")
        md.append("")
        md.append("Consider raising live position size on these in `agents/risk_agent.py` "
                  "(or letting them through dedupe more freely). Profit factor ≥ 2 "
                  "with reasonable accuracy means wins are durable.")
        md.append("")
        md.append("| rule_key | n | acc | PF |")
        md.append("|---|---|---|---|")
        for c in amp_candidates[:15]:
            md.append(f"| `{c['rule_key']}` | {c['n']} | {c['acc']:.1%} | {c['pf']:.2f} |")
        md.append("")
    if not (flip_candidates or skip_candidates or amp_candidates):
        md.append("_No new actionable thresholds crossed this quarter. The discipline "
                  "is operating within the existing carry-forward learning. Continue "
                  "current cadence._")
        md.append("")

    # Top rules by quarter activity
    md.append("## Top rule_keys by quarterly activity (n ≥ 5 in window)")
    md.append("")
    md.append("| rule_key | n (in quarter) | wins | win-rate | sum realized return % |")
    md.append("|---|---|---|---|---|")
    ranked = sorted(
        ((rk, v) for rk, v in by_rule.items() if v["n"] >= 5),
        key=lambda x: -x[1]["pnl"],
    )
    for rk, v in ranked[:10]:
        wr_r = v["wins"] / v["n"]
        md.append(f"| `{rk}` | {v['n']} | {v['wins']} | {wr_r:.1%} | {v['pnl']*100:.1f}% |")
    md.append("")

    # Source chronology
    if recent_docs:
        md.append("## Source chronology")
        md.append("")
        md.append("Monthly docs read for this review (most recent first):")
        md.append("")
        for fname, _content in reversed(recent_docs):
            md.append(f"- [`{fname}`]({fname})")
        md.append("")

    md.append("## How to action this")
    md.append("")
    md.append("1. Pick one recommended action from above (start with flips — "
              "highest ROI per code change).")
    md.append("2. Add the rule_key to the relevant set in `agents/thesis_agent.py` "
              "or `agents/risk_agent.py`.")
    md.append("3. Gate behind a feature flag (e.g., `STRUCTURAL_FLIP_ENABLED`).")
    md.append("4. Push, set the secret to `true`, watch the relevant pulsecheck.")
    md.append("5. Re-run this consultant in 2 weeks to confirm impact.")
    md.append("")
    md.append("**This consultant runs deterministically** — same inputs produce "
              "the same outputs. There's no surprise. Re-run any time:")
    md.append("")
    md.append("```bash")
    md.append("python3 scripts/quarterly_consultant_review.py")
    md.append("```")
    md.append("")

    with open(out_path, "w") as f:
        f.write("\n".join(md))

    print()
    print(f"=== Consultant review {q_label} ===")
    print(f"  Closed trades in window  : {total_n}")
    print(f"  Flip candidates          : {len(flip_candidates)}")
    print(f"  Skip candidates          : {len(skip_candidates)}")
    print(f"  Amplify candidates       : {len(amp_candidates)}")
    print(f"  Doc                      : {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
