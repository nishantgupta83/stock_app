# Session worklog — 2026-06-08 → 2026-06-09 (for review)

**⚠️ DEPLOY STATUS: 16 commits are LOCAL on `main`, NOT pushed to `origin/main`.**
The live GitHub-Actions pipeline runs `origin/main` (currently `974d967`), so every
code fix below is **not yet live**. The ONE exception: `sql/0040` (the action-CHECK
widening) was applied directly to the remote Supabase DB yesterday — that's why
thesis is emitting again. Pushing deploys all 16 to the live pipeline at once.

Every change was TDD'd and independently Codex-reviewed (plan + diff), with each
Codex finding verified against the code before adoption. Full suite: **365 passing**,
0 failures (the 2 pre-existing `risk_rules` failures were fixed this session).

---

## A. Layer-2 silence — definitive root cause + observability

**`974d967` (pushed) — root cause.** Layer-2 was silent 13 days NOT because of the
`CLUSTER_SCORE_OVERRIDE_ENABLED` secret (the prior hypothesis was wrong) but a
`stock_signals.action` CHECK constraint silently rejecting the post-PR1A vocabulary
(`CATALYST_*`/`MOMENTUM_ONLY`); `write_signal` swallowed the insert error → runs
finished `ok` rows_out=0. `sql/0040` widened the CHECK; thesis emitted immediately.

**`cc250e8` — surface insert failures.** `write_signal` now records rejected inserts
to `stock_job_runs.meta.emit`, flips the run to `partial`, and a `pulsecheck_thesis`
check scans a 3h window (not latest-only) so this silent class can't hide again.
*Verify:* `tests/test_thesis_insert_failures.py` (17 cases).

**`d6b06b1` — de-page weekend skip-rate + dashboard colors.** `reconcile_skip_rate`
got an absolute-skip floor (no false CRITICAL on weekend-pending trades, while the
513-stuck regression still fires); `styles.css` gained the `CATALYST_*`/`MOMENTUM_ONLY`
tag colors so new-vocab signals render.

## B. Layer-2 meta-labeling funnel (PR-B0 + PR-B)

**`0f57609` — extract `score_cluster` shared core.** Behavior-preserving refactor of
thesis's inline cluster-scoring loop + injectable clock; the live path and the replay
share ONE scorer so the coverage number can't drift.

**`1fcde4a` — PR-B0 cluster-replay coverage.** `scripts/replay_cluster_coverage.py`
measures candidate×horizon gate coverage. **Measured: 75% gateable → COMMIT.**

**`0104f20` (+ `64a3e08`, `d5ed7a9`, `faa44b9`) — PR-B walk-forward gate + validation.**
`agents/_metalabel_gate.py` (the 2.b gate, shared with the future live path) +
`scripts/validate_metalabel_gate.py` (leakage-free backtest). **Run verdict:
INCONCLUSIVE — the ~90d corpus is too young to validate the 15-30d horizons (heavy
right-censoring).** Per-cell diagnostic added. Re-run scheduled **2026-07-06**.
Codex caught: leakage (→ walk-forward), backfill leak (→ created_at guard), wrong-trade
match (→ exact event_id label), a KeyError, and a wrong column (`realized_at`→`exit_at`).

## C. Doc / test hygiene

- **`1644a63`** — fixed the 2 stale `risk_rules` tests (supply PF/mean to the PF-aware gate).
- **`cd94dcb`** — corrected the superseded "secret was the cause" narrative in CLAUDE.md
  + 2 findings docs (point at `974d967`).
- **`c411681`** — tracked `learning_snapshot.yml` (daily snapshots missing since 5/30),
  pinned to known-good `@v4`/`@v5`.
- **`1458826`** — committed prior-session learning-doc refreshes + the 5/30 snapshot.

## D. End-to-end boundary remediation (H1–H3, M1–M2)

**`7255894` — H1 + H2.** **H1:** `trade_setup_agent` (Layer 3) ingested the whole
`stock_signals` table (only `fired_at`), so intraday-spike (L1) + suppressed thesis
signals bled in — an *active* path via `realistic_loop_agent`, which opens setups
bypassing L4. Now DB-filters to the thesis lane + `{candidate,sent}`, with a starvation
canary → `partial`. **H2:** dashboard counted all lanes as "Layer 2" (overstated ~4-6×);
now thesis-scoped, non-thesis reported separately. New `agents/_lanes.py` is the single
lane source, pinned to `thesis_agent.MODEL_VERSION` by test.

**`554b415` — M1.** `risk_agent.maturity_tier` fallback used a stale 3rd copy of the
adult gate (`acc≥0.90/n≥30/PF>1.5`); aligned to canonical (`n≥100/PF≥2.0/mean≥0.5%`,
no acc floor).

**`0b4b581` — M2.** `pulsecheck` winrate-drift monitor (flags rules whose recent winrate
falls >25pts below lifetime — the news-cohort regime break).

**`74817d1` — H3.** VIX is not ingested (verified: zero rows), so `is_risk_off`'s VIX
branch silently failed open. Made it LOUD (didn't delete — it threads the hot path and
yield/FOMC paths still work) + corrected the false comment.

*Verify D:* `tests/test_l3_input_filter.py`, `test_dashboard_lane_split.py`,
`test_maturity_tier_fallback.py`, `test_calibration_drift.py`, `test_risk_off_loud.py`.

---

## Two non-obvious facts surfaced (code *looks* fine — see memory)
1. **VIX is not ingested** → VIX-based risk-off is dead until `^VIX` is wired into price ingestion.
2. **`realistic_loop_agent` bypasses L4** → L3 is the only enforced boundary (which is why H1 fixed it at the L3 source).

## Open (deferred, with plan)
- **DEPLOY:** push the 16 commits (deploys to live pipeline — your call).
- PR-B gate re-run 2026-07-06 (data maturity); then PR-C wires the gate live.
- Wire VIX (before real capital); cleanups: O1 false-stale window, 25 untested `@v6` bumps, stale `pipeline-maturity` scorecard.
