# Layer 2 → meta-labeling funnel (2.a / 2.b / orchestrator)

**Status:** DESIGN — review before any code. No implementation yet.
**Date:** 2026-06-05
**Authors:** Claude (analysis + draft) with an independent neutral review by Codex (gpt-5.5).
**Supersedes the control surface in:** `agents/thesis_agent.py` (monolithic scorer + single hardcoded emit floor).
**Related:** `docs/findings/2026-06-04_layer2-thesis-silence-audit.md`, `scripts/calibrate_emit_floor.py`, CLAUDE.md notes 6–7.

---

## 1. Why (the problem this fixes)

The monolithic `thesis_agent` gates emission on **one hardcoded score floor (50)**. That floor was the single brittle control surface that caused a **13-day Layer 2 outage**: a 2026-05-22 change (PR1A) correctly stopped stale 13F filings from inflating scores, which ~halved the score scale, but nobody re-tuned the floor — so it silently rejected every genuine catalyst (verified: PFE *same* biotech catalyst 91.51 → 44.17; GOOG suppressed at 49.756, 0.244 under the floor).

A single score floor is the wrong control surface for two reasons:

1. **It can't separate profitable from unprofitable signals at the same score.** A 44-pt clinical cluster (PF 9.3) and a 44-pt earnings cluster (PF 0.79) score identically but have opposite expectancy.
2. **It conflates two jobs** — "is there a plausible catalyst?" (recall) and "has this kind of signal made money?" (precision) — into one number, so re-scaling the score scale breaks emission.

**The fix:** split Layer 2 into a **meta-labeling funnel** (López de Prado): a loose recall stage, a payoff-aware precision gate, and an orchestrator. The learning layer (`stock_rule_calibration`, keyed `rule_key::horizon`) is already the precision gate's evidence base — ~80% of this is built.

---

## 2. Empirical basis (and a correction)

`scripts/calibrate_emit_floor.py` over ~11,200 closed paper trades shows profitability varies **by holding horizon** within the same rule:

| rule | h1d | h7d | h15d | h30d |
|---|---|---|---|---|
| `8k_material_event` | 0.73 ✗ | 2.04 ✓ | 2.58 ✓ | 2.48 ✓ |
| `news_article:neutral` | 0.86 ✗ | 3.39 ✓ | — | — |
| `clinical:active_not_recruiting` | 3.80 ✓ | 1.49 | 9.33 ✓ | — |
| `earnings_release:beat` | 0.79 ✗ | 0.93 ✗ | 1.36 | 1.37 |
| `truth_social:tariff_general` | 3.08 ✓ | 4.40 ✓ | 7.80 ✓ | — |

**Correction (from Codex's review, accepted):** this does **not** prove "horizon matters more than score" globally. It proves the *current* score is weak/mis-scaled relative to horizon-conditioned outcomes. **Score is retained** — for candidate generation, dedup priority, and within-cell ranking — but it stops being the precision gate.

---

## 3. The funnel

```
2.a CANDIDATE GENERATION  (high recall)
  in:  stock_normalized_events
  out: candidates (ticker, cluster, score, direction, constituent rule_keys)
  rule: emit any plausible catalyst above a LOOSE recall floor (~25–30).
        Score is kept for ranking/dedup, NOT as the precision decision.

2.b META-LABEL GATE  (precision; per rule × horizon)
  in:  candidates + stock_rule_calibration (walk-forward expectancy per rule_key::horizon)
  out: per-(candidate, horizon) {act | pass} + confidence
  rule: ACT on a horizon only if that (rule_key, horizon) cell clears the
        payoff + sample guardrails (§5). FAIL OPEN to WATCH on thin/uncalibrated
        cells — never silently drop the long tail.

ORCHESTRATOR  (stitch + present)
  in:  2.b per-horizon decisions
  out: stock_signals — ONE compact signal per ticker/catalyst carrying a
        horizon profile ("tradable h7d/h15d/h30d; suppress h1d").
  applies: dedup by ticker/catalyst, per-lane daily cap (counts the CLUSTER,
           not each horizon), risk-off shift, severity-4 bypass.
```

---

## 4. The horizon decision (resolved)

Three options were weighed; **Option C** was chosen on Codex's independent recommendation:

- **A — per-rule × per-horizon, 4 separate alerts:** most precise but UX-hostile (4 alerts/catalyst), breaks dedup and the daily cap.
- **B — per-rule blended, one act/pass:** simplest, but knowingly emits the losing h1d horizon. **Rejected** — it conflates learning and alerting; if the data says h1d loses, don't label it actionable.
- **C — horizon-aware internally, one compact alert externally:** ✅ the gate is per-(rule, horizon); the orchestrator collapses to a single alert with a horizon profile. Daily cap counts the cluster. **BUY/SELL maturity becomes rule×horizon-scoped** — already supported by the `rule_key::horizon` calibration schema, so it's free.

---

## 5. Guardrails before 2.b may SUPPRESS or PROMOTE a cell

Sequenced — cheap/high-value first, statistical machinery deferred until cell counts justify it:

**Tier 1 (adopt at launch):**
- **n ≥ 100** per cell for normal gating; **n ≥ 50** only as "provisional / WATCH." (Matches `feedback_ml_discipline`.)
- **Walk-forward only:** the gate at time `t` uses trades closed **before** `t`. No full-sample tuning. (`calibrate_emit_floor.py` full-sample stats are for *diagnosis*, not the live gate.)
- **Recent-window sanity:** last 60–90 days must not strongly contradict lifetime stats.
- **Payoff sanity, not PF alone:** PF ≥ ~1.5 **and** positive mean realized return **and** acceptable tail/drawdown. (Matches `feedback_tier_gates`.)
- **Shadow-log suppressed horizons:** keep opening paper trades for suppressed cells so the gate doesn't bias its own future sample (selection feedback).

**Tier 2 (defer until cell counts justify):**
- Multiple-testing shrinkage across rule×horizon cells (Bayesian shrink toward rule-family mean, or FDR control).
- Lower-confidence-bound gating (e.g. Wilson / bootstrap CI lower bound > threshold).
- Explicit handling of overlapping-outcome non-independence (nearby-date h7/h15/h30 trades aren't IID).

---

## 6. Data contracts

- **New (proposed):** `stock_signal_candidates` (2.a output) — ticker, cluster_key, score, direction, rule_keys[], created_at. Cheap; lets 2.a/2.b be tested in isolation.
- **Reuse:** `stock_rule_calibration` (2.b evidence — needs a **walk-forward read path**, see §5).
- **Extend:** `stock_signals` gains a `horizon_profile` (which horizons cleared) + keeps the single-row-per-catalyst contract.
- **Reuse:** `stock_event_paper_trades` — already opens all 4 horizons; shadow-logging is the existing behavior, just don't stop it for suppressed cells.

---

## 7. Build sequence (smallest useful version first)

Per `feedback_pr_sizing` — one behavior per PR, measurement gate between:

0. **PR-A — feasibility measurement [DONE 2026-06-05].** `scripts/measure_candidate_coverage.py`. **Finding:** of 37,211 raw events/180d only 7,449 are catalyst-role candidates (Form 4 / 13F are background, 80% of raw volume — excluded). Catalyst event-volume gateable coverage = **94.1%**; the n≥100 cells are exactly the high-frequency classes (8-K, news, earnings, clinical). **Verdict (Claude + Codex review): COMMIT to the architecture, but START NARROW** — gate only the HF n≥100 classes; fail-open 13G / truth-niche / tail to WATCH until their cells mature. Caveat: 94% is event-volume, not a cluster replay; the commit-grade cluster-level number is deferred to PR-B0 (below), but the *start-narrow* decision is robust to it.
1. **PR-A.1 — 2.a candidate generation + `stock_signal_candidates`.** Loose recall floor; score retained for ranking. (The current stopgap floor=30 is the interim stand-in.)
2. **PR-B0 — cluster-replay coverage (commit-grade).** Upgrade the measurement to group events by (ticker, window), score, keep clusters ≥floor, and report candidate-level **and** candidate×horizon gateable coverage. Confirms the narrow-gate class list before it gates suppression.
3. **PR-B — walk-forward calibration read path.** A `rule_key::horizon` expectancy lookup that only sees trades closed before the decision time. Validate with `calibrate_emit_floor.py` (walk-forward mode added).
4. **PR-C — 2.b NARROW gate (HF classes, Tier-1 guardrails) + orchestrator compact alert.** Horizon-aware act/pass for 8-K/news/earnings/clinical; everything else fails open to WATCH; one alert/catalyst with horizon profile; rule×horizon maturity. Measure: emission rate, paper-trade accuracy/PF of emitted vs suppressed, false-suppression audit.
5. **PR-D (deferred) — widen the gate to maturing classes (13G, truth-niche) + Tier-2 statistical guardrails** once their cells reach n≥100.

Remove the `THESIS_RECALL_FLOOR` / override stopgap scaffolding when PR-C lands.

---

## 8. Open questions / what would change our mind

- **Is the split over-engineering for a solo paper project?** Verdict (Claude + Codex): no — but only the smallest version (§7). If candidate volume turns out tiny, 2.a/2.b may collapse back toward a re-tuned floor + a thin payoff check. Revisit after PR-A measures real candidate volume.
- **Cell sparsity:** if most rule×horizon cells never reach n≥100, the gate mostly fails-open to WATCH and the precision benefit is small. Measure cell-count distribution before committing to PR-C.
- **UX:** does a horizon profile in a Telegram alert read clearly, or is it noise? Prototype one alert string before building the orchestrator.

---

## 9. Provenance

Drafted by Claude from the live audit + the `calibrate_emit_floor.py` corpus. Independently reviewed by **Codex (gpt-5.5, read-only)** on 2026-06-05, which: chose Option C, rejected Option B, corrected the "horizon > score" overstatement, flagged the walk-forward/leakage requirement, and supplied the guardrail list. Disagreement noted: Codex's full guardrail set is heavier than a solo project needs at launch → sequenced into Tier 1/2 here. External-review practice per `feedback_external_ai_second_opinion`.
