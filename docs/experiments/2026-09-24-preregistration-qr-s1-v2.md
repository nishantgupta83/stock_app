# Pre-registration — `qr_s1_v2`: v1 plus a 200-day trend filter

**Status:** FROZEN at the commit that adds this file, before v2 was run. Parameters:
`scripts/quarterly_rotation/experiment.py::ExperimentConfigV2` (hash-covered). Paper / research
only, no capital; no Supabase, no Telegram. Everything in
[`2026-09-24-preregistration-qr-s1-v1.md`](2026-09-24-preregistration-qr-s1-v1.md) (calendar,
decision dates, eligibility, %B, entry/exit opens, 0.20% costs, cohort rules, null construction,
2000 draws, hindsight/survivorship and tax caveats) applies unchanged except as stated here.

## Disclosure — this is NOT an independent test

v1 (same data, same universe) failed P1 (p = 0.071) and P4 (hit rate 52%), and showed one
quarter (2026-03-31, +75 pts) carrying its mean. v2 was designed after seeing that. The
200-day layer is therefore a second look at the same 10 years, not fresh evidence. Two trials now
exist, which is why the thresholds tighten rather than relax (below). A v2 pass is a reason to
run a **forward** paper test, never a reason to act.

## Question (one)

> Among AI-universe names in the %B < 0.20 dip band at quarter-end, does requiring the close to
> be **above its 200-session SMA** (a pullback inside an uptrend) beat the same number of names
> drawn at random from the same eligible universe, net of costs?

One layer, one direction. The opposite direction (below the 200-day) and the momentum layer are
not tried here; each would be a new version with the trial count raised again.

## Change from v1 (the only ones)

- **Signal:** dip AND `close(R) > mean(closes of the last 200 calendar sessions through R)`,
  requiring ≥ 190 of those 200 sessions present; a name without them is not a dip name.
- **P1** p ≤ **0.025** (Bonferroni for two trials on this data).
- **P5** mean excess with the best quarter removed **> +0.5 pt** (v1 was "> 0").
- Unchanged: P2 ≥ +1.0 pt · P3 ≥ 20 signal quarters · P4 ≥ 55% · P6 both halves ≥ 0.

All six must hold to promote. If P3 is the only failure the outcome is reported as
**underpowered**, not falsified; it still does not promote. No re-run under this id.

## Primary metric

Mean net quarter excess vs the same-universe equal-weight hold, p-value against the random-pick
null drawn from the **full** eligible universe (identical construction to v1, seed 20260924).

## Informational only (not a gate)

`p_value_vs_trend_pool_null_INFORMATIONAL`: the same statistic against a null that draws only
from names above their 200-day. It separates "dips inside a trend help" from "being in an uptrend
helps". Reported, never used to promote, because adding it as a seventh gate would cut power
further after two trials.

## Trials

Configurations tried on this data under the quarterly-rotation programme: **2** (v1, v2).
