# Layer 2 (thesis_agent) silence — full audit + verified state

> ⚠️ **SUPERSEDED 2026-06-08 — THE ROOT CAUSE IN THIS DOC IS WRONG. DO NOT ACT ON IT.**
> This doc concluded the Layer-2 silence was "one secret-edit away"
> (`CLUSTER_SCORE_OVERRIDE_ENABLED` being off). That was **incorrect**. The
> binding cause was a `stock_signals.action` CHECK constraint that silently
> rejected the post-PR1A vocabulary (`CATALYST_*`/`MOMENTUM_ONLY`); `write_signal`
> swallowed the insert error so runs finished `status=ok` rows_out=0. Fixed in
> `sql/0040` (commit `974d967`); error-surfacing added in `cc250e8`. Thesis
> emitted immediately after — **verified live 2026-06-09** (rubric-v1.1 signals
> firing). **Do NOT run the `gh secret set` command below.** The Layer-5-healthy
> and cohort-drift observations in this doc remain valid; the diagnosis does not.
> Full correction: CLAUDE.md "Feature flags" note + `docs/findings/README.md`.

**Date:** 2026-06-04
**Status:** ❌ SUPERSEDED 2026-06-08 — root cause was the `stock_signals.action` CHECK constraint, not the secret (see banner above)
**Companion to:** `2026-06-02_cluster-score-override.md` (which described the fix; this doc shows the fix never actually took effect in production)

## TL;DR

`thesis_agent` (model_version `rubric-v1.1`) has emitted **0 signals since 2026-05-22T16:32 UTC** — 13 days of silence as of this audit. The cluster_score_override mechanism shipped on 2026-06-02 was supposed to fix this, but the production env shows the flag is **off**. Layer 5 (learning) is genuinely healthy and producing first-ever maturity-tier graduations. Everything downstream of Layer 2 (trade_setups → risk → realistic_loop) is correctly starving because Layer 2 has no inputs to give.

## What we observed

### Layer 2 — silent

- `stock_signals` last `model_version=rubric-v1.1` row: **2026-05-22T16:32** (MRK, WATCH, score=81.79).
- Last 30 `thesis_agent` runs (2h20min window): all status=ok, 2,299 normalized events processed, **0 signals written**.
- All-time rubric signals ever: 50 rows total, spanning 2026-04-28 → 2026-05-22. Mix: 28 RESEARCH, 18 WATCH, 4 AVOID_CHASE. Zero BUY/SELL (correct — maturity gate hadn't graduated any rule until 2026-06-02+).

### Why it's silent — the rejection audit confirms

`stock_thesis_rejection_mix` (24h window):
- `cluster_passes`: **8,261 rejections** (98%), avg_score=5.19, 75 distinct tickers.
- `action_empty_low_score`: 183 rejections (2%), avg_score=32.22.

Both fail modes have avg_score well below the `CLUSTER_SCORE_OVERRIDE_THRESHOLD=50` — meaning even with the override flag on, only a handful of candidates would have been promoted. But the population of candidates above 50 is non-zero.

### The override flag — built, but not on

`stock_thesis_rejections` has **9 rows with score ≥ 50, all dated 2026-06-02 22:37–23:12 UTC**, all `cluster_ticker=NFLX`, all `fail_reason=cluster_passes`, all `source_agents=["filing"]`. Score 60.029 on 8 of them, 50.024 on the 9th.

If `CLUSTER_SCORE_OVERRIDE_ENABLED` had been actually `true` in the workflow env, every one of those 9 would have been promoted to `ok=True` and emitted as MOMENTUM_ONLY signals. They were rejected → **the flag is not actually enabling the override in production.**

Cross-check:
- `gh secret list --repo nishantgupta83/stock_app` confirms `CLUSTER_SCORE_OVERRIDE_ENABLED` exists, updated `2026-06-02T23:43:50Z`.
- `.github/workflows/thesis_agent.yml` wires `${{ secrets.CLUSTER_SCORE_OVERRIDE_ENABLED }}` into the env block.
- `agents/thesis_agent.py:_cluster_score_override_enabled()` parses correctly: `os.environ.get(...).lower() in ("1","true","yes")`.
- → Conclusion: the secret value is something other than `"true"`/`"1"`/`"yes"` (empty, "false", or absent — `gh secret list` can show name+timestamp but not value).

### Layer 5 — healthy, first graduations ever

`snapshots/2026-05-30.json` vs production today:
| Metric | 2026-05-30 | 2026-06-04 | Δ |
|---|---|---|---|
| Closed paper trades | 2,000 | 11,248 | **+9,248 (5.6×)** |
| Mature rules | 0 | 6 (2 adult / 1 young_adult / 3 teen) | first graduations |
| Open paper trades | — | 5,619 | — |
| Past-horizon stuck | — | 0 | clean ✅ |

The 5.6× closure surge in 5 days is the 2026-06-02 price_agent cron bump (once-daily → every 2h) doing exactly what it was designed to do. The 513-stuck-trade incident from 2026-06-02 stays cleaned up.

### Mature rules — verified payoff

```
[adult]       8k_material_event::h15d                       n=1155  acc=0.69  acc_30d=0.90  mean_ret=+2.36%  pf=2.81
[adult]       8k_material_event::h30d                       n=1119  acc=0.53  acc_30d=0.47  mean_ret=+2.30%  pf=2.30
[young_adult] clinical_readout:active_not_recruiting:h15d   n=33    acc=0.91  acc_30d=0.91  mean_ret=-0.12%  pf=9.33
[teen]        clinical_readout:active_not_recruiting:h1d    n=259   acc=0.71  acc_30d=0.69  mean_ret=+0.61%  pf=3.80
[teen]        truth_social_post:tariff_general:h7d          n=42    acc=0.74  acc_30d=0.75  mean_ret=+1.58%  pf=4.40
[teen]        truth_social_post:tariff_general:h15d         n=30    acc=0.70  acc_30d=0.70  mean_ret=+1.18%  pf=7.80
```

All 6 satisfy the payoff-sanity gate (`feedback_tier_gates` memory: accuracy + n + profit_factor). The one near-zero `mean_realized_pct` (young_adult clinical h15d, -0.12%) has profit_factor=9.33 — that's many small wins offsetting a few small losses, payoff math is genuinely positive even if mean is near zero.

### Per-agent weight updates — narrowed to intraday only

`stock_agent_weights` distinct dates:
- 2026-05-25: price, intraday, earnings, filing, biotech all updating.
- 2026-05-26 onward: **only `intraday` agent** gets fresh weight rows.
- 2026-06-04: intraday n=351, weight=1.61, acc_ema=0.81 (healthy).

Almost certainly downstream of Layer 2 silence — with no rubric signals firing, no per-agent attribution feedback. Likely resolves automatically once thesis re-emits.

### Operational sub-warnings (pulsecheck)

`stock_health_pulse_recent_alerts` (last 24h):
- `pulsecheck_thesis.candidate_dryness=warning` ("3h window: rows_in=2424, rows_out=0").
- `pulsecheck_thesis.rejection_distribution=warning` ("dominant=cluster_passes (98%)").
- `pulsecheck_news.classifier_neutrality=warning` ("24h neutral share: 201/241 = 83% > 80% threshold").
- `pulsecheck_realistic_loop.input_starvation=warning` ("null-reason setups in last 5d: 0").

The pulsecheck system added 2026-06-02 is doing its job — surfacing every consequence of the Layer 2 silence in real time.

### Untracked workflow — `learning_snapshot.yml`

`git status` shows `?? .github/workflows/learning_snapshot.yml`. Daily 22:00 UTC snapshots haven't run since 2026-05-30 because the workflow file isn't on the remote. Snapshots in `snapshots/`: 2026-05-26, 2026-05-27, 2026-05-30 only. Five weekday snapshots missing.

### Cohort drift to monitor

Trades opened in May, closed in June: n=1121, acc=0.49, mean_ret=**+4.79%** (carried by tail wins).
Trades opened in June, closed in June: n=879, acc=0.41, mean_ret=**-0.21%**.

The recent (June-entry) cohort is netting negative. Not catastrophic with current sample sizes, but worth watching as Layer 2 turns back on — the rubric may be tuned to a May regime.

## What it might mean

1. The 2026-06-02 fix was **shipped at the code level but never activated at the runtime level**. The doc says "Shipped flag-on (via secret)" but the production secret value is not `true`. Either the rollout step was skipped, or the secret was set then reverted.

2. The override-threshold value (50) is conservatively set. Even with the flag on, only 9 of 8,444 recent rejections would have crossed it. Net emission rate would be ~1-3/day, capped at 5/day. Not a flood — still aligned with `MAX_ALERTS_PER_DAY=5` per lane.

3. Layer 5 is generating quality calibration data without Layer 2 input — the `8k_material_event::h15d` rule alone (n=1155, acc_30d=0.90, mean_ret=+2.36%) is producing real signal that nothing is currently consuming. **The learning loop is decoupled from the intelligence layer right now.**

## Why we're acting now

This is not deferred. The fix is a single `gh secret set` command. Cost: zero refactor, zero risk (trivially reversible by flipping the secret back).

## How to fix (the actual fix, not just doc)

```bash
gh secret set CLUSTER_SCORE_OVERRIDE_ENABLED \
  --repo nishantgupta83/stock_app \
  --body "true"
```

Then wait for the next `thesis_agent` run (cron `*/5 * * * *`, so within 5 min) and verify:

```bash
source /tmp/sb_env.sh   # SUPABASE_URL + SUPABASE_SERVICE_KEY
curl -s "${SUPABASE_URL}/rest/v1/stock_signals?model_version=like.rubric-v*&order=fired_at.desc&limit=3&select=fired_at,ticker,action,score" \
  -H "apikey: ${SUPABASE_SERVICE_KEY}" -H "Authorization: Bearer ${SUPABASE_SERVICE_KEY}"
```

If a row with `fired_at` after the secret-set time appears → fix is live.

If still silent after 30 min: check `stock_thesis_rejections` for rows with `cluster_label=override:high_score_*` (the marker the override path writes). Absence after a high-score (>=50) rejection means the flag still isn't being parsed as true.

## Companion follow-ups (lower urgency)

1. **Commit `learning_snapshot.yml`** — restore daily snapshot cadence. Cost: one `git add + commit + push`.
2. **`filing_13g::h15d` regression** — accuracy dropped 0.86→0.66 as n went 51→89. The 38 new closures averaged sub-50%. Check whether the new rows skew activist vs. non-activist (different signal strength).
3. **News neutrality at 83%** — already covered by `2026-06-02_keyword-db-audit.md` and `2026-06-02_slm-classifier-feasibility.md`. The threshold tripping is consistent with the diagnosis there.
4. **Per-agent weight updates** — recheck after Layer 2 re-emits for 3 days. If non-intraday agents still aren't getting weight rows by 2026-06-07, the weight-updater needs its own audit (separate from this finding).

## What would change my mind

- If the secret is already set to `true` and I missed it: check the workflow run logs for the last `thesis_agent` run — env block will show `CLUSTER_SCORE_OVERRIDE_ENABLED=true` if it's actually set. (GHA prints all env vars at job start unless masked; this one isn't sensitive.)
- If flipping the secret produces no new emissions within 30 min: there's a second blocker. The most likely candidate is `MAX_ALERTS_PER_DAY` accounting (CLAUDE.md note 7 about per-lane budgets) — verify that `alerts_sent_today(model_version=MODEL_VERSION)` is actually scoping correctly.

## Source queries (for re-verification when context is cleared)

```bash
# 1. Layer 2 silence
curl -s "$SUPABASE_URL/rest/v1/stock_signals?model_version=like.rubric-v*&order=fired_at.desc&limit=5&select=fired_at,ticker,action,score" \
  -H "apikey: $SUPABASE_SERVICE_KEY" -H "Authorization: Bearer $SUPABASE_SERVICE_KEY" | jq -c '.[]'

# 2. Rejection mix
curl -s "$SUPABASE_URL/rest/v1/stock_thesis_rejection_mix" \
  -H "apikey: $SUPABASE_SERVICE_KEY" -H "Authorization: Bearer $SUPABASE_SERVICE_KEY" | jq -c '.[]'

# 3. The 9 high-score rejections that would have been promoted
curl -s "$SUPABASE_URL/rest/v1/stock_thesis_rejections?score=gte.50&order=score.desc" \
  -H "apikey: $SUPABASE_SERVICE_KEY" -H "Authorization: Bearer $SUPABASE_SERVICE_KEY" | jq -c '.[]'

# 4. Mature rules (Layer 5 health)
curl -s "$SUPABASE_URL/rest/v1/stock_rule_calibration?tier=in.(adult,young_adult,teen)&order=n_observations.desc" \
  -H "apikey: $SUPABASE_SERVICE_KEY" -H "Authorization: Bearer $SUPABASE_SERVICE_KEY" | jq -c '.[]'

# 5. Pulsecheck warnings
curl -s "$SUPABASE_URL/rest/v1/stock_health_pulse_recent_alerts?select=*" \
  -H "apikey: $SUPABASE_SERVICE_KEY" -H "Authorization: Bearer $SUPABASE_SERVICE_KEY" | jq -c '.[]'
```
