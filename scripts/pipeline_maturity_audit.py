#!/usr/bin/env python3
"""Pipeline maturity audit — % mature per agent / per layer.

Maturity has multiple dimensions; this audit reports each separately
instead of collapsing into one misleading score:

  OPERATIONAL maturity      = is the agent reliably running?
                              (cron firings in last 7d vs expected)
  COVERAGE maturity         = is it producing the volume we expected?
                              (rows_in/rows_out over last 7d)
  CALIBRATION maturity      = of the rule_keys this agent feeds, what
                              % have reached n>=30 with non-trivial
                              edge (acc >= 50% AND profit_factor > 1)?
  ACTIONABLE maturity       = of those, what % are at teen / young /
                              adult tier (the alerts that actually
                              unlock the action vocabulary)?

Output: docs/pipeline-maturity-DDMMYYYY.md with a layer-by-layer
breakdown + per-agent table.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone


SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
HEADERS = {
    "apikey": SUPABASE_SERVICE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
}


# Map agent name → event_type prefix(es) it produces. Used to attribute
# rule_keys to source agents.
AGENT_EVENT_TYPES: dict[str, list[str]] = {
    "filing_agent":         ["8k_material_event", "filing_4", "filing_13d", "filing_13g",
                             "filing_s-3", "filing_424b", "filing_dilution",
                             "filing_other_sev1", "filing_3"],
    "news_agent":           ["news_article"],
    "earnings_agent":       ["earnings_release"],
    "truth_social_agent":   ["truth_social_post"],
    "crypto_macro_agent":   ["macro_yield", "yield_snapshot", "crypto_event",
                             "crypto_macro"],
    "biotech_agent":        ["clinical_readout", "fda_pdufa_decision",
                             "fda_approval"],
    "defense_agent":        ["dod_contract_award"],
    "energy_transition_agent": ["nuclear_license_approval", "energy_storage_milestone"],
    "activist_insider_agent": ["activist_5pct_crossed", "insider_cluster_buy"],
    "consumer_health_agent": ["consumer_signal", "consumer_umich_alert"],
    "macro_rates_agent":    ["macro_rate_decision", "macro_cpi"],
    "intraday_alert_agent": ["intraday_alert", "momentum", "price_gap",
                             "volume_anomaly"],
    "flows_agent":          ["institutional_new_position", "institutional_exit",
                             "institutional_add", "institutional_trim"],
    "market_scanner_agent": ["unusual_volume", "breakout", "breakdown"],
}

# Expected cron cadences (runs per 7 days)
AGENT_EXPECTED_RUNS_7D: dict[str, int] = {
    "filing_agent":         2016,   # */5 = 12/h * 24 * 7
    "news_agent":           2016,
    "thesis_agent":         2016,
    "truth_social_agent":   2016,
    "intraday_alert_agent": 6 * 9 * 5,  # */15 13-21 UTC Mon-Fri ≈ 270
    "site_generator":       672,    # */15 = 4/h * 24 * 7
    "paper_trade_agent":    672,
    "event_paper_agent":    168,    # hourly
    "realistic_loop_agent": 168,
    "price_agent":          60,     # */2h Mon-Fri = 12/d * 5 = 60
    "trade_setup_agent":    2016,   # roughly */5
    "risk_agent":           2016,
    "orchestrator_agent":   7,      # daily
    "earnings_agent":       1,      # Sunday only
}


def paginate(table: str, params: dict[str, str], page: int = 1000) -> list[dict]:
    rows, offset = [], 0
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


def sb_count(table: str, params: dict[str, str]) -> int:
    qs = urllib.parse.urlencode({**params, "select": "id"}, safe=".,:*=&")
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/{table}?{qs}",
        method="HEAD",
        headers={**HEADERS, "Prefer": "count=exact"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        cr = r.headers.get("content-range", "")
    return int(cr.rsplit("/", 1)[-1]) if "/" in cr else 0


def event_type_to_agent(et: str) -> str | None:
    for ag, types in AGENT_EVENT_TYPES.items():
        if any(et.startswith(t) for t in types):
            return ag
    return None


def rule_key_to_agent(rk: str) -> str | None:
    et = rk.split(":")[0] if rk else ""
    return event_type_to_agent(et)


def main() -> int:
    now = datetime.now(timezone.utc)
    seven_d_ago = (now - timedelta(days=7)).isoformat()

    print("Pulling cron-run telemetry…", file=sys.stderr)
    runs = paginate("stock_job_runs", {
        "started_at": f"gte.{seven_d_ago}",
        "select":     "agent,status,rows_in,rows_out",
    })

    # Aggregate per agent
    runs_per_agent: dict[str, dict] = defaultdict(
        lambda: {"runs": 0, "ok": 0, "rows_in": 0, "rows_out": 0})
    for r in runs:
        ag = r["agent"]
        if ag.startswith("workflow_"):
            continue  # only count actual agent runs, not workflow brackets
        d = runs_per_agent[ag]
        d["runs"] += 1
        if r.get("status") == "ok":
            d["ok"] += 1
        d["rows_in"] += int(r.get("rows_in") or 0)
        d["rows_out"] += int(r.get("rows_out") or 0)

    print("Pulling rule_calibration…", file=sys.stderr)
    cal = paginate("stock_rule_calibration", {
        "select": "rule_key,n_observations,accuracy,profit_factor,tier,is_mature,is_mature_70,is_mature_80",
    })

    # Bucket each rule_key by its source agent
    rules_by_agent: dict[str, list[dict]] = defaultdict(list)
    for r in cal:
        ag = rule_key_to_agent(r.get("rule_key") or "")
        if ag:
            rules_by_agent[ag].append(r)
        else:
            rules_by_agent["(unknown)"].append(r)

    # Maturity tiers — READ from canonical fields, do NOT recompute locally.
    # (2026-06-04 fix: the prior local logic used acc>=0.90+PF>1.5 which was
    # the OLD adult-gate definition. After the gate was redefined in
    # agents/price_agent.py to payoff-first criteria (n>=100 + PF>=2.0 +
    # mean>=0.5%), this script was reporting different tier counts than the
    # canonical `tier` / `is_mature*` fields in stock_rule_calibration.
    # Codex external review caught the drift. Single source of truth: DB.)
    def tier_counts(rules: list[dict]) -> dict[str, int]:
        c = {"n<30": 0, "child": 0, "teen": 0, "young_adult": 0, "adult": 0}
        for r in rules:
            n = int(r.get("n_observations") or 0)
            tier = (r.get("tier") or "child").lower()
            # The `tier` column defaults to "child" even for n<30 rows (see
            # price_agent.py:695-703 — tier is derived from is_mature* flags;
            # n<30 falls through to "child"). For human-readable reporting
            # we still want to split out the immature bucket, so we use n
            # to distinguish:
            if n < 30:
                c["n<30"] += 1
            elif tier in c:
                c[tier] += 1
            else:
                # Unknown tier value — count as child to avoid losing the row
                c["child"] += 1
        return c

    # "Tradeable" = at least teen tier (is_mature_70 True). Teen tier requires
    # n>=30 + acc>=70% + mean_realized > 0 — a meaningful "has shown positive
    # expectancy" floor. Reading the canonical flag instead of inventing a
    # new "tradeable" definition keeps this script aligned with the rest of
    # the pipeline.
    def tradeable_count(rules: list[dict]) -> int:
        return sum(1 for r in rules if r.get("is_mature_70"))

    # Write the report
    out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs")
    fname = f"pipeline-maturity-{now.strftime('%d%m%Y')}.md"
    path = os.path.join(out_dir, fname)

    md = []
    md.append(f"# Pipeline maturity scorecard — {now.strftime('%Y-%m-%d')}")
    md.append("")
    md.append(f"_Generated by `scripts/pipeline_maturity_audit.py`. "
              f"Sources: stock_job_runs (last 7d), stock_rule_calibration._")
    md.append("")

    # ============================================================
    # Section: layer-by-layer maturity
    # ============================================================
    md.append("## Layer-by-layer summary")
    md.append("")
    md.append("Four maturity dimensions, scored separately because they fail in "
              "different ways. A layer can be operationally healthy but produce no "
              "actionable rules, or vice-versa.")
    md.append("")
    md.append("| Layer | Operational | Coverage | Calibration | Actionable |")
    md.append("|---|---|---|---|---|")

    # Layer 1 — INGEST
    ingest_agents = [a for a in AGENT_EVENT_TYPES if a not in (
        "intraday_alert_agent",)]  # intraday is special — Layer 2 path
    ingest_runs = sum(runs_per_agent.get(a, {}).get("runs", 0) for a in ingest_agents)
    ingest_expected = sum(AGENT_EXPECTED_RUNS_7D.get(a, 0) for a in ingest_agents)
    op_pct_l1 = min(100, ingest_runs / max(1, ingest_expected) * 100)
    ingest_rows_out = sum(runs_per_agent.get(a, {}).get("rows_out", 0) for a in ingest_agents)
    # Rule maturity for INGEST: aggregate
    ingest_rules = [r for a in ingest_agents for r in rules_by_agent.get(a, [])]
    tiers = tier_counts(ingest_rules)
    # teen+ = teen + young_adult + adult (any rule with is_mature_70=True)
    mature_l1 = tiers["teen"] + tiers["young_adult"] + tiers["adult"]
    cal_pct_l1 = (1 - tiers["n<30"] / max(1, len(ingest_rules))) * 100
    act_pct_l1 = mature_l1 / max(1, len(ingest_rules)) * 100
    md.append(f"| **L1 INGEST** | {op_pct_l1:.0f}% ({ingest_runs}/{ingest_expected} cron) | "
              f"{ingest_rows_out:,} events/7d | {cal_pct_l1:.0f}% reached n≥30 "
              f"({len(ingest_rules) - tiers['n<30']}/{len(ingest_rules)} rules) | "
              f"{act_pct_l1:.0f}% at teen+ ({mature_l1}/{len(ingest_rules)}) |")

    # Layer 2 — INTEL
    th = runs_per_agent.get("thesis_agent", {})
    op_pct_l2 = min(100, th.get("runs", 0) / max(1, AGENT_EXPECTED_RUNS_7D["thesis_agent"]) * 100)
    th_emits_7d = sb_count("stock_signals",
                            {"model_version": "eq.rubric-v1.1",
                             "fired_at": f"gte.{seven_d_ago}"})
    md.append(f"| **L2 INTEL** | {op_pct_l2:.0f}% ({th.get('runs', 0)}/{AGENT_EXPECTED_RUNS_7D['thesis_agent']} cron) | "
              f"{th_emits_7d} rubric signals/7d | "
              f"(downstream — see L1 calibration) | {th_emits_7d} alerts in window |")

    # Layer 3 — SETUP
    ts = runs_per_agent.get("trade_setup_agent", {})
    op_pct_l3 = min(100, ts.get("runs", 0) / max(1, AGENT_EXPECTED_RUNS_7D["trade_setup_agent"]) * 100)
    setups_total = sb_count("stock_trade_setups", {"created_at": f"gte.{seven_d_ago}"})
    setups_tradeable = sb_count("stock_trade_setups",
                                 {"created_at": f"gte.{seven_d_ago}",
                                  "reason_to_skip": "is.null"})
    tradeable_ratio = setups_tradeable / max(1, setups_total) * 100
    md.append(f"| **L3 SETUP** | {op_pct_l3:.0f}% ({ts.get('runs', 0)}/{AGENT_EXPECTED_RUNS_7D['trade_setup_agent']} cron) | "
              f"{setups_total} setups/7d ({setups_tradeable} null-reason) | "
              f"{tradeable_ratio:.0f}% tradeable | "
              f"{'⚠️ all flagged' if setups_tradeable == 0 else 'producing tradeable setups'} |")

    # Layer 4 — RISK
    ra = runs_per_agent.get("risk_agent", {})
    op_pct_l4 = min(100, ra.get("runs", 0) / max(1, AGENT_EXPECTED_RUNS_7D["risk_agent"]) * 100)
    risk_total = sb_count("stock_risk_decisions", {"created_at": f"gte.{seven_d_ago}"})
    md.append(f"| **L4 RISK** | {op_pct_l4:.0f}% ({ra.get('runs', 0)}/{AGENT_EXPECTED_RUNS_7D['risk_agent']} cron) | "
              f"{risk_total} decisions/7d | "
              f"(downstream of L3 tradeable ratio) | {'⚠️ idle' if risk_total == 0 else 'sizing'} |")

    # Layer 5 — LEARNING
    pa = runs_per_agent.get("price_agent", {})
    ep = runs_per_agent.get("event_paper_agent", {})
    op_pct_l5 = min(100, (pa.get("runs", 0) + ep.get("runs", 0)) /
                    max(1, AGENT_EXPECTED_RUNS_7D["price_agent"] + AGENT_EXPECTED_RUNS_7D["event_paper_agent"]) * 100)
    closes_7d = sb_count("stock_event_paper_trades", {"status": "eq.closed",
                                                       "exit_at": f"gte.{seven_d_ago}"})
    opens_7d = sb_count("stock_event_paper_trades", {"entry_at": f"gte.{seven_d_ago}"})
    md.append(f"| **L5 LEARNING** | {op_pct_l5:.0f}% (price+event_paper cron) | "
              f"{opens_7d} opens / {closes_7d} closes per 7d | "
              f"calibration auto-recomputed on each close | accumulating |")

    # Layer 6 — PRESENTATION
    sg = runs_per_agent.get("site_generator", {})
    op_pct_l6 = min(100, sg.get("runs", 0) / max(1, AGENT_EXPECTED_RUNS_7D["site_generator"]) * 100)
    md.append(f"| **L6 PRESENT** | {op_pct_l6:.0f}% ({sg.get('runs', 0)}/{AGENT_EXPECTED_RUNS_7D['site_generator']} cron) | "
              f"site published | n/a | dashboard live |")
    md.append("")

    # ============================================================
    # Section: per-ingest-agent table (the granular ask)
    # ============================================================
    md.append("## Per-agent maturity (Layer 1 — INGEST)")
    md.append("")
    md.append("Operational = cron success rate / expected; Calibration = rule_keys "
              "this agent's events feed into. Tradeable = rules with n≥30 AND "
              "acc≥50% AND profit_factor>1 (the minimum bar for the realistic loop "
              "to take a position on them).")
    md.append("")
    md.append("| Agent | Op % | Cron runs/exp | Events 7d | Rule_keys | n≥30 | Tradeable | Mature (teen+) | Adult |")
    md.append("|---|---|---|---|---|---|---|---|---|")
    for ag in sorted(AGENT_EVENT_TYPES):
        r = runs_per_agent.get(ag, {})
        expected = AGENT_EXPECTED_RUNS_7D.get(ag, 0)
        op = min(100, r.get("runs", 0) / max(1, expected) * 100)
        rules = rules_by_agent.get(ag, [])
        tiers = tier_counts(rules)
        n_at_30 = sum(v for k, v in tiers.items() if k != "n<30")
        trade = tradeable_count(rules)
        teen_plus = tiers["teen"] + tiers["young_adult"] + tiers["adult"]
        adult = tiers["adult"]
        md.append(f"| `{ag}` | {op:.0f}% | {r.get('runs', 0)}/{expected} | "
                  f"{r.get('rows_out', 0):,} | {len(rules)} | {n_at_30} | "
                  f"{trade} | {teen_plus} | {adult} |")
    md.append("")

    # ============================================================
    # Section: maturity tier population (the actual rule_key tiers)
    # ============================================================
    md.append("## Population by tier (all rule_keys)")
    md.append("")
    md.append("_Reads canonical `tier` + `is_mature_*` flags from "
              "`stock_rule_calibration` — these are set by "
              "`agents/price_agent.py:upsert_calibration` and reflect the "
              "current gate definitions (see `ADULT_MIN_N/PF/MEAN` constants)._")
    md.append("")
    all_tiers = tier_counts(cal)
    total = len(cal)
    tier_defs = {
        "n<30":        "n < 30 (insufficient sample)",
        "child":       "n ≥ 30, below teen gate",
        "teen":        "n ≥ 30, acc ≥ 70%, mean_realized > 0",
        "young_adult": "n ≥ 30, acc ≥ 80%, PF > 1.2",
        "adult":       "n ≥ 100, PF ≥ 2.0, mean_realized ≥ 0.5% (BUY/SELL unlock)",
    }
    md.append("| Tier | Definition | Count | Share |")
    md.append("|---|---|---|---|")
    for k in ("n<30", "child", "teen", "young_adult", "adult"):
        v = all_tiers.get(k, 0)
        share = v / max(1, total) * 100
        md.append(f"| `{k}` | {tier_defs[k]} | {v} | {share:.1f}% |")
    md.append(f"| **TOTAL** | | **{total}** | 100% |")
    md.append("")

    # ============================================================
    # Section: priority calibration changes (data-driven)
    # ============================================================
    md.append("## Calibration changes the data suggests")
    md.append("")

    # Find: rules with n>=30 but profit_factor<1 (negative-edge mature rules)
    neg_edge = [r for r in cal
                if int(r.get("n_observations") or 0) >= 30
                and (r.get("profit_factor") is not None)
                and float(r["profit_factor"]) < 1.0]
    neg_edge.sort(key=lambda r: float(r.get("profit_factor") or 0))

    # Find: rules with very high profit_factor — candidates for amplification
    high_edge = [r for r in cal
                 if int(r.get("n_observations") or 0) >= 30
                 and (r.get("profit_factor") is not None)
                 and float(r["profit_factor"]) > 2.0]
    high_edge.sort(key=lambda r: -float(r["profit_factor"]))

    if neg_edge:
        md.append("### Mature rules with NEGATIVE edge (profit_factor < 1 with n≥30)")
        md.append("")
        md.append("These rules have enough data to trust the verdict AND that verdict "
                  "is *losing money over time*. Consider: invert direction (treat as "
                  "fade signal) OR add to a structural-skip set.")
        md.append("")
        md.append("| rule_key | n | accuracy | profit_factor |")
        md.append("|---|---|---|---|")
        for r in neg_edge[:12]:
            md.append(f"| `{r['rule_key']}` | {r['n_observations']} | "
                      f"{float(r['accuracy']):.1%} | {float(r['profit_factor']):.2f} |")
        md.append("")

    if high_edge:
        md.append("### Mature rules with HIGH edge (profit_factor > 2.0 with n≥30)")
        md.append("")
        md.append("These are the rules the pipeline should be amplifying — sizing up "
                  "(when risk budget allows), reducing dedupe to let more through, "
                  "and using as anchors for multi-source confirmation.")
        md.append("")
        md.append("| rule_key | n | accuracy | profit_factor |")
        md.append("|---|---|---|---|")
        for r in high_edge[:12]:
            md.append(f"| `{r['rule_key']}` | {r['n_observations']} | "
                      f"{float(r['accuracy']):.1%} | {float(r['profit_factor']):.2f} |")
        md.append("")

    md.append("## How to read this scorecard")
    md.append("")
    md.append("- **Op %** measures cadence reliability vs the workflow's expected "
              "cron firings over the last 7 days. <80% suggests the GHA cron is "
              "dropping and a cron-job.org pinger should be added (see "
              "`scripts/bootstrap_cronjob_org.py`).")
    md.append("- **Calibration columns** answer: of the rule_keys this agent's events "
              "feed into, how many have accumulated enough sample (n≥30) to be "
              "trusted? Tradeable adds the 'has edge' filter (PF>1, acc≥50%).")
    # Gate definition kept in sync with agents/price_agent.py ADULT_MIN_N/PF/MEAN
    # (n>=100, PF>=2.0, mean_realized>=0.5%). The earlier "acc>=90%, n>=30, PF>1.5"
    # string here was stale — it predated the 2026-06-04 payoff-first gate redefinition
    # and contradicted the tier table this same script emits above.
    md.append("- **Adult** is the canonical maturity gate (n≥100, PF≥2.0, "
              "mean_realized≥0.5%) that unlocks BUY/SELL vocabulary in `thesis_agent`. "
              "Until anything reaches Adult, the system stays in paper-tier "
              "(WATCH/RESEARCH/AVOID_CHASE) regardless of how confident a signal looks. "
              "This is the CLEUF-loss discipline.")

    with open(path, "w") as f:
        f.write("\n".join(md))
    print(f"Wrote {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
