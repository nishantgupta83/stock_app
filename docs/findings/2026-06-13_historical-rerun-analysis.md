# 2026-06-13 — Historical pipeline re-run analysis

**Question (user):** we fixed a lot — do we need to review/re-run the HISTORICAL
pipeline so calibration isn't built on buggy data?

**Answer: NO full historical re-run is warranted.** The fixes that affect
historical GATE decisions were already applied via one-shot recomputes; the one
forward-only data fix (H2) has a measurable but **gate-invariant** historical
taint. Details + the (low-priority) optional cleanup below. All figures are
measured live.

## Which fixes touch historical data, and their state

| Fix | Touches history? | Already remediated? |
|-----|------------------|---------------------|
| **C1** counters | YES (n/accuracy/mean inflated 1.5-2.5x) | ✅ repaired live (89 rows; 0 inflated on re-verify) |
| **C2 / C2-pflag** maturity gate | gate logic only (no trade data) | ✅ tiers re-derived; 5 leaked setups flagged |
| **H1** effective-n | re-derives tiers from existing trades | ✅ bulk recompute applied (all 118 rules) |
| **H2** after-hours entry anchor | YES — historical entries (forward-only fix) | ⚠️ NOT rewritten — see taint analysis |
| **H6/H8** risk reads/budget | read-time only | n/a (no historical data) |
| **M3** walk-forward timing | future 7/06 funnel | ✅ fixed (no historical mutation) |
| **M5/M8/M1/M6** | forward / one-shot deduped | ✅ |

So the only historical-DATA question is **H2**: closed trades whose entry was a
pre-event (after-hours-leaked) close have an inflated/shifted realized_return,
and C1+H1 calibration was recomputed FROM those returns.

## H2 historical taint — measured

- Corpus: **14,857 closed** + 7,052 open event paper trades.
- Of a 6,000 all-event-type sample: 38% are after-16:00-ET events, **9.3% (556)
  actually entered at a leaked pre-event close**. Their mean realized_return is
  **−1.4%** (differs from the corpus-wide +1-3%), so re-anchoring would
  materially change those 556 trades.
- **BUT the gate-relevant rule is untouched:** `8k_material_event::h30d` — the
  ONLY adult rule post-H1 — has just **2 leaked trades (0%)**. Effective stats
  dropping the leaked ones: **PF 2.12 → 2.14, mean 2.96% → 3.00%, n 450 → 448 —
  still adult.** The leaked 9.3% are concentrated in rules that are CHILD
  regardless, so re-anchoring them changes no tier.

## Why a full re-run is NOT warranted

1. **BUY/SELL gate outcome is invariant.** The maturity gate (what licenses
   BUY/SELL Telegram emission) is driven by C1 (✅ repaired) + H1 (✅ applied).
   The only adult rule (`8k::h30d`) is ~0% H2-exposed and stays adult; all
   others are child with or without the leak.
2. **No FINANCIAL harm; paper-only.** No real capital moves anywhere — and no
   BUY/SELL Telegram alert fires (no h1d adult cell).
3. **Re-running rewrites 14.8k trades** (re-price entry+exit, re-close,
   re-derive) — real egress + mutation risk — to correct ~9% of rows that change
   no BUY/SELL decision. Cost ≫ benefit for the gate.

### CORRECTION (Codex validation — the taint is NOT display-only)
My first draft said "no live decision rides it / display-only." That over-claimed.
Verified against code, the H2-leaked returns DO feed paper-layer decisions:
- `trade_setup_agent` reads RAW `accuracy`/`profit_factor` (confidence) +
  `mean_mfe_pct`/`mean_mae_pct` (adaptive target/stop) — `trade_setup_agent.py:286,313`.
- `realistic_loop_agent` opens a paper position for every `reason_to_skip IS NULL`
  setup (bypasses L4), using that target/stop.
- Lifetime calibration fields are recomputed over the FULL closed population
  (`price_agent.py:1020`), so the 556 leaked rows are DILUTED by new clean data,
  **not aged out**.
So the corrected claim: the BUY/SELL **gate** is invariant, but L3 confidence /
target / stop and the realistic_loop paper portfolio DO consume the tainted
per-rule metrics. No financial harm (paper), but the paper decisions are affected.

## The one real consideration: the 2026-07-06 metalabel funnel (PR-C)

The funnel reads RAW closed-trade `realized_return`; ~9.3% carry the H2 entry-leak.
M3 fixed the walk-forward TIMING leak; the H2 ENTRY-leak remains. Codex's key
point: **H2 is label leakage, not random noise** — it is event-time/type
clustered, so it can bias a (rule,horizon) cell's measured edge directionally,
not just add variance. The funnel is fail-open-to-WATCH (harm-limited), but PR-C
would SUPPRESS/ACT from this evidence. → **Required before PR-C trusts the 7/06
re-run as launch evidence: a leak-exclusion SENSITIVITY run** — compute the gate
decisions twice (all rows vs leaked-rows-excluded). If ACT/WATCH decisions are
invariant, accept. If any flip, re-anchor the leaked rows first. (This is cheap:
exclude the ~556 rows in the validator, no mutation.)

## Recommendation

- **Do NOT run a full historical re-run / backfill.** (Confirms the standing
  guidance to leave `backfill_paper_trades.py` unused — it would rewrite 14.8k
  rows for no BUY/SELL-gate change, and re-pricing risk is real.)
- **BEFORE PR-C / the 7/06 funnel:** run the leak-exclusion sensitivity (above).
  Accept-as-is only if gate decisions are invariant under exclusion.
- **H2b open-trade re-anchor — RAISE priority (Codex):** the ~tens of OPEN
  after-hours-leaked trades are FUTURE contamination (they'll close with leaked
  entries + feed L3/realistic_loop). Re-anchor them (gated one-shot) ahead of the
  closed-cohort cleanup. Higher value than touching closed history.
- **Closed-cohort re-anchor:** optional, gated one-shot of the ~556 leaked closed
  trades + `recompute_effective_all`, ONLY if the sensitivity run shows a flip.
- **Two verifications Codex flagged — ✅ DONE, both pass:** (1) latest
  `stock_agent_weights` are live-sourced (2026-06-13 daily, sensible per-agent
  weights + real n_signals); the old backtest dispatch dates are diluted/
  overwritten — M1 isolation confirmed clean. (2) flows produces only ~5 13F-diff
  events total (`institutional_new_position`, source
  `stock_institutional_holdings_snapshot`); even if any were truncation-
  fabricated the volume is negligible (~5 events) and M6.2 prevents recurrence —
  no material historical contamination.

## What would CHANGE this recommendation

- A **new adult rule** emerging that is after-hours-heavy (high leaked-trade
  fraction) → re-anchor it before trusting its tier. (Re-check the per-rule
  leaked fraction whenever a rule crosses the adult gate.)
- **Archive deletion** being enabled (currently DRY_RUN) → would also force the
  M2 corpus-merge question.

## Verification trail (re-runnable)
- C1 integrity: `scripts/repair_calibration_counters.py` (dry-run) → 0 inflated.
- H1 tiers: `scripts/recompute_effective_all.py` (dry-run) → 0 tier changes.
- H2 taint + gate-invariance: the per-rule leaked-fraction + clean-vs-all
  effective-stats query in this session's analysis (8k::h30d: 0% leaked, PF stable).
