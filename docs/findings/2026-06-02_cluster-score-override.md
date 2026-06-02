# Score-based cluster_passes override — data-driven L2 unblock

**Date:** 2026-06-02
**Status:** Shipped flag-on (feature: `CLUSTER_SCORE_OVERRIDE_ENABLED`)

## What the data showed

After shipping the rejection audit (`sql/0035`) earlier today, the first
24h of data revealed:

- **444 rejected clusters today, 100% via `cluster_passes` gate** (none
  via the score gate).
- Cluster_label: `single_source_no_exception` (every single one).
- Source agent distribution: `news=350, filing=76, unknown=18`.
- Score distribution: `<10=358, 10-25=56, 25-50=21, 50-70=9, >=70=0`.
- **9 clusters scored ≥50 — they passed the rubric's own alert
  threshold but got blocked by cluster_passes anyway.**

Top sample (one of the 9):
```
ticker:        NFLX
score:         60.03
catalyst_score: 0
n_events:      12
source_agents: [filing]
cluster_label: single_source_no_exception
breakdown_sample:
  weight_adj_filing   12.03
  filing_other_sev1    4.0
  filing_other_sev1    4.0
```

12 filing-agent events on NFLX in a 30-min bucket is real activity-burst
intensity. The rubric scored it 60. The cluster_passes heuristic blocked
it because there was only one source agent. **Double-gating.**

## The fix

`agents/thesis_agent.py:1768` — after `action_for` runs but before the
candidate filter (`cluster_ok AND action`):

```python
if (not ok
    and _cluster_score_override_enabled()
    and score >= CLUSTER_SCORE_OVERRIDE_THRESHOLD     # 50.0
    and action):
    breakdown.append({"rule": "cluster_passes_override", ...})
    ok = True
    cluster_label = f"override:high_score_{int(score)}"
```

What this does NOT touch:
- The score gate (`action_for`) — still requires score ≥ 50 for any
  non-empty action. So the rubric IS the deciding factor.
- The catalyst gate — bullish actions without catalyst still degrade to
  `MOMENTUM_ONLY`.
- The maturity gate — `BUY`/`SELL` still require `is_mature` rule.
- The chase-risk filter — bullish moves >5% still get
  `CHASE_RISK`-downgraded.
- The dedupe filter.
- The cap (`MAX_ALERTS_PER_DAY=5` per lane, post-2026-06-02 fix).

Net effect: at most a handful of additional paper-tier signals per day
(today's data: 9 candidates → capped at 5 alerts).

## Why a flag, defaulting on (via secret)

Pattern follows the SECTOR_CALIB_MULT_ENABLED rollout. The mechanism is
trivially reversible (env var). If signal noise climbs unexpectedly,
flip the secret to `false`. The rejection audit table continues
recording so we can measure either state.

## What the user gets

| Before | After |
|---|---|
| Thesis emits 0 rubric signals for 11 days | Up to 5 rubric signals/day from high-score single-source clusters |
| Realistic loop sees only AVOID_CHASE setups; opens 0 positions | Will see MOMENTUM_ONLY / CATALYST_* signals; can open positions on the first non-AVOID_CHASE setup |
| Calibration has no rubric-class outcomes to learn from | Each emit becomes a paper trade → calibration accumulates → rules can reach n≥30 → maturity gate eventually opens BUY/SELL vocabulary |

The maturity-gate discipline that prevents the CLEUF-style losses stays
fully intact — this change does NOT unlock BUY/SELL. It opens the
**paper-trade-and-learn** path that was structurally blocked.

## What would change my mind / when to flip back off

- If after 5 trading days the realistic loop's drawdown crosses 10% of
  bankroll. The discipline framework would say "halt and rederive."
- If `pulsecheck_thesis.rejection_distribution` shows the override
  promoting a NEW rejection class (e.g., low-quality news article
  bursts that the rubric scores high but humans wouldn't endorse).
- If a single ticker dominates emits (>3 emits for one name in a
  trading day) — that suggests the cluster window is grouping
  unrelated activity bursts together.

## Cross-references

- Implementation: `agents/thesis_agent.py:1768+`.
- Data source: `stock_thesis_rejections` (migration 0035) and
  `stock_thesis_rejection_mix` view.
- Original plan: `~/.claude/plans/rereview-what-is-critical-golden-island.md`
  step P1.4 ("If most failures are cluster_passes single-source").
- Workflow env: `.github/workflows/thesis_agent.yml`.
- Companion finding: `docs/findings/2026-06-02_keyword-db-audit.md`
  (orthogonal — addresses *catalyst recall*, while this finding
  addresses *single-source pass-through*).
