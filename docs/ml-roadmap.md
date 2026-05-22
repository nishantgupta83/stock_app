# ML roadmap & positioning

## What this document is

`stock_app` is an **event-driven market intelligence pipeline with paper-trade calibration**. It has *learning loops* (empirical statistics that update as outcomes arrive) but it is **NOT** a trained-model ML pipeline. That is a deliberate design choice. This document captures why, what would change our mind, and what specifically would land if we ever do add real ML.

This doc exists so that:
- External reviewers (human or LLM) reading the codebase don't ask "where's the ML?" and get silence. The honest answer is here.
- Future-me doesn't, in a moment of enthusiasm, throw 6 months of paper-trade data at xgboost and ship a classifier that memorizes the bugs we just fixed.

---

## Current "learning" inventory

Everything in the repo that updates itself based on outcomes, with the math and the file:line.

| Component | Math | Lives in | What gets calibrated |
|---|---|---|---|
| Per-agent EMA | `new = α·outcome + (1−α)·old`, α=0.1 | `agents/price_agent.py:317-340` | Per-source accuracy (thesis, news, filing, …) |
| Per-rule calibration | Streaming `n_correct/n_obs` + mean realized return | `agents/price_agent.py:497-530` | Per-`rule_key` empirical hit rate + payoff |
| Brier-30d | Mean squared deviation between accuracy claim and outcomes | `agents/price_agent.py:recompute_rule_brier_30d` | Whether a rule's confidence is honest |
| Backtest diagnostics | Pseudo-Brier, Sharpe, max drawdown over historical replay | `agents/backtester.py:790-870` | Offline analysis only — not consumed by live agents |
| Paper-trade prob_win | Beta-binomial Bayesian shrinkage toward base rate | `agents/paper_trade_agent.py` | Per-setup-bucket forecast probability |
| Adaptive target/stop | Per-rule `mfe × 0.7` / `|mae| × 0.5` | `agents/trade_setup_agent.py:217-242` | Sizing once a rule has n≥10 closed trades |

The math: smoothing constants, streaming means, count-based accuracy, one closed-form Bayesian update. **No fit step. No gradient. No model artifact. No feature embeddings.** Calling this "ML" would be a misnomer.

---

## What's deliberately NOT in the system

Each absence is a design choice, not a gap:

- **No trained classifier.** Until labels are clean (post-PR1A causal attribution policy, shipped 2026-05-22) and data accumulates per cell, an ML model would learn pre-fix attribution biases — in particular, the stale-13F-as-catalyst bug we just removed.
- **No gradient-based optimization.** Closed-form Bayesian shrinkage is the *correct* tool for n<100 per cell, which is our actual data regime today (~50 rule_keys, most with n<50). An undertrained xgboost would lose to shrinkage. The reviewer feedback that prompted this doc made this explicit: shrinkage is more powerful here than I originally credited.
- **No feature embedding / similarity search.** Would obscure attribution. The current architecture's auditability (signal → events → `score_breakdown` → outcome → calibration update) is more valuable than a marginal accuracy bump from learned representations.
- **No LLM fine-tuning.** Wrong tool for tabular event data with sparse cells. The narrative-summary fields (`thesis_summary`, `evidence_summary`) are formatted in code, not generated.

`requirements.txt` confirms by absence: 9 deps total, none are ML libraries.

---

## One thing that IS empirically tunable today

The intermediate step before any classifier work.

`compute_target_and_stop()` in `agents/trade_setup_agent.py:225-242` derives per-trade target and stop as:

```python
TARGET_MFE_FRACTION = 0.7    # capture 70% of mean favorable excursion
STOP_MAE_FRACTION   = 0.5    # cut at half the mean adverse excursion
```

These are **global constants applied to every rule**. Per-rule tuning of these two scalars is a real (small) empirical optimization:

- Two-knob hyperparameter sweep per rule, not multi-feature ML
- Requires n≥50 closed trades per rule for stable estimates
- Maximize payoff_factor on a chronological holdout
- Effort: ~6 hr including walk-forward validation + ops integration

**This is the first empirical optimization to revisit** once post-PR1A clean data accumulates (earliest realistic date: 2026-08). Until then, the global 0.7/0.5 is a reasonable default.

---

## Four gate criteria for adding real ML

Do **not** add a trained classifier until ALL four are true. These are gates, not nice-to-haves:

1. **Clean data accumulation.** ≥3 months of post-PR1A paper trades closed with the new catalyst-policy attribution. Pre-PR1A data is unusable for training — it contains the stale-13F-attribution bias as a systematic mislabel.
2. **At least one mature rule.** An existing rule has crossed the 90%/n≥30 gate. This proves the empirical baseline can actually mature on this data; if not, no classifier will either.
3. **Identified ceiling.** A specific decision (sizing or direction) where the empirical method has hit a quantifiable wall — not "more accuracy in general." If you can't name the decision, you don't need the model.
4. **Evaluation plan.** Walk-forward chronological split, per-regime stratification, AND the metric to beat is the existing rule-based calibration baseline — not "better than coin-flip." Anything else is grading on a curve.

If all four pass, the classifier is worth the engineering cost. Miss any, and the model is decoration.

---

## When ML lands, where it fits structurally

A classifier **goes alongside the rule system, not in place of it.** The rule layer stays the audit trail.

- `score_breakdown` remains the explainability surface. Every signal still traces back to events with rule + points + role.
- A classifier provides ONE additional input: `ml_p_win: 0.62` as a row in `score_breakdown`.
- The classifier output is weighted exactly the same way agent EMA weights other sources today — through `stock_agent_weights` with bounded `[0.1, 2.0]` multiplier.
- A bad model gets turned off with a weight of zero. No rollback, no migration.

This is the same architectural discipline applied to every other layer in the pipeline. ML earns its slot through calibration; it doesn't get a special bypass.

---

## Two specific decisions where ML would actually help

Only two places where empirical bookkeeping structurally can't match a learned model:

1. **Position sizing via calibrated p_win + Kelly fraction.** The current `MATURITY_MULTIPLIER` step function (1x / 1.5x / 2x by maturity tier) is a crude approximation of "how much should we size into this signal." A genuinely calibrated probability with Kelly sizing would move the needle here — and the bar is low because the step function is crude.
2. **Cluster signal direction.** `signal_direction()` in `agents/thesis_agent.py` is vote-counting (bull += 1, bear += 1, with the dilution-direction tie-break). A classifier on the full evidence vector could plausibly beat vote-counting on mixed-signal clusters where the marginal bull/bear evidence offsets but the *combination* implies a direction.

A general-purpose "predict whether this trade will be profitable" classifier is **explicitly NOT** on the roadmap. That's the kind of scope that generates papers and rarely generates trading edge.

---

## Anti-patterns this roadmap exists to prevent

Specific things that would feel productive but degrade the system:

- **"Throw all historical data at xgboost and see what falls out."** Would memorize the PR1A attribution bug as a feature. Most catastrophic failure mode.
- **Replacing the rule layer with a black-box model.** Destroys auditability — which is the single biggest defensive feature this system has against silent drift.
- **Multi-feature classifier with n<100 per cell.** Undertrained, unreliable, and harder to debug than the existing empirical layer it would replace.
- **Calling the existing system "ML" in docs or external review.** Invites the wrong critique. Use accurate framing: "rule-based research stack with outcome-based calibration."

---

## What earns the system the right to add ML

Concrete checklist that converts the four gates above into testable conditions you can run a query against:

- [ ] Date is after 2026-08-22 (3 months past PR1A landing)
- [ ] `SELECT count(*) FROM stock_rule_calibration WHERE is_mature = true` returns ≥ 1
- [ ] Documented case where calibrated empirical accuracy plateaued: "rule X is at Y% for the last N trades and we have specific reason to think a model could do better at sizing/direction for it"
- [ ] Evaluation script committed BEFORE any modeling code, asserting: walk-forward chronological split, per-regime accuracy + Brier + Kelly-sized return, baseline = existing empirical rule system
- [ ] One concrete decision identified: "this classifier exists to improve sizing" or "this classifier exists to improve cluster direction" — never general-purpose

All five → ship a classifier alongside the rule system. Any miss → keep iterating on the empirical layer (it has years of headroom).

---

## Related

- `agents/_catalyst_policy.py` — the policy that made post-PR1A labels clean enough to even consider training on
- `agents/price_agent.py:recompute_rule_brier_30d` — the Brier score added to live calibration so we know when an existing rule's accuracy claim is honest. The first signal we'd watch when deciding whether a classifier can clear the bar.
- `templates/calibration.html.j2` — where calibration + drift are surfaced for human review
- `docs/technical-architecture.md` Layer 5 — overall "Learning" layer the bookkeeping lives in

---

## TL;DR

We don't have ML. We have honest empirical calibration. That's the right tool for this data regime. Real ML, if added, will be one specific scoped classifier added alongside the rule system to improve sizing or cluster direction — not a general-purpose "predict trade success" model. The four gate criteria above must all pass first.
