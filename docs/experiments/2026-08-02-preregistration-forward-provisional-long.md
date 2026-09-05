# Pre-registration — Forward provisional-long edge test (h1d & h7d)

**Committed:** 2026-08-02 (the git commit timestamp is the pre-registration proof — these
parameters are FIXED before any forward session they will grade).
**Status:** v1 DRAFT — operator sign-off pending. If the operator confirms these params
as-is, `forward_epoch` stands. If ANY parameter changes, a NEW experiment version + new
`forward_epoch` starts — **no parameter change after data without a version bump** (that is
the pre-registration contract; it is what makes the result honest).
**Scope guardrails:** PAPER ONLY. No capital ramp at any tier here — Tier ③ / real money is
out of scope and retains the existing standard (`docs/design/2026-06-26-paper-book-forward-edge.md:55-57`).

## Two isolated experiments (one per horizon — never share cohorts)

| id | horizon | book/state |
|---|---|---|
| `fwd_prov_long_h1d` | 1 trading day | `paper_book/experiments/fwd_prov_long_h1d/` |
| `fwd_prov_long_h7d` | 7 trading days | `paper_book/experiments/fwd_prov_long_h7d/` |

They are graded **separately** (separate DB, state JSON, `forward_epoch`, `config_hash`). A
candidate that carries both `::h1d` and `::h7d` rule-keys enters BOTH — as independent
positions in independent experiments — but is **never double-counted within one experiment's
cohort evidence**.

## Hypothesis (per experiment)

> The pipeline's **bullish** candidates, entered **long** at the next session's open and held
> to horizon H with a fixed stop, earn a **positive net excess return vs a same-window QQQ
> position** — forward, after costs.

Directional, one-sided. A clear negative net excess after the minimum sample **kills** it.

## Source — `stock_signal_candidates` (sql/0039), NOT `stock_trade_setups`

Rationale (verified 2026-08-02, `docs/findings/2026-08-02_schema-reconciliation.md`): the
candidate ledger is **live and populating** (217 rows), whereas `stock_trade_setups` is
starved by Layer-2 emit-silence + the closed maturity gate. Candidates are the recall-stage,
**pre-gate** output — exactly the "provisional / not-yet-trustworthy" set this test exists to
grade forward. Candidates lack target/stop/horizon shape, so we **supply fixed exits** below.

## Admission rule (a candidate is admitted to `fwd_prov_long_h{1,7}d` iff ALL hold)

1. `direction = 'bullish'` (this is the LONG experiment; bearish/AVOID_CHASE is a **separate,
   deferred** short experiment — it needs borrow costs — and is explicitly NOT included here).
2. `rule_keys` has ≥1 key whose **last colon-segment** is the horizon label: `h1d`
   (1a) or `h7d` (1b). Live candidate keys are `type:subtype:h1d` (single colon),
   e.g. `news_article:positive:h1d`; empty-subtype keys are `type::h1d`. Both end in
   the segment `h1d`, so we match the last segment (verified 2026-08-02 — an
   `endswith("::h1d")` match would silently miss the single-colon majority).
3. **Tradeable single-name/ETF ticker:** exclude any ticker starting `INST_` (institutional
   placeholders) and any non-tradeable fund (VTSAX/FXAIX/VFIAX/… per `agents/_instruments`).
4. `created_at >= forward_epoch` — **forward only**; earlier candidates are excluded and
   **never backfilled**.
5. Dedup: at most one position per `(ticker, entry_session, horizon)`. Cohorts are counted by
   **distinct `entry_session` date** (same-day entries = one independent cohort).

`score` is recorded (provenance) but is **not** an admission gate beyond the recall floor the
candidate already cleared to be written (empirical finding: payoff tracks horizon, not score).

## Entry / exit / cost model (SINGLE LOCUS — frozen)

- **Entry:** the OPEN of the next trading session **strictly after** `candidate.created_at`
  (anti-lookahead — we only enter after we knew of the candidate; never a backdated fill).
- **Horizon exit:** close at the horizon bar `H` days after entry (H = 1 or 7),
  following the pipeline's own `HORIZONS=(1,7,15,30)` convention — `compute_paper_outcome`
  advances `entry_date + H` days (calendar), so h7d ≈ 5 trading days. QQQ shares the exact
  same exit bar, so the excess stays apples-to-apples.
- **Stop:** −3.0% from entry (`stop_pct = 0.03`).
- **Target:** +5.0% from entry (`target_pct = 0.05`).
- **Exit policy:** `price_agent.compute_paper_outcome(trade, bars, exit_policy="stop_only")` —
  the SAME honest grader the calibration pipeline uses (grades to the horizon unless the stop
  is hit first; it does not assume optimistic target fills).
- **Costs (the single locus — never double-count):** round-trip slippage **10 bps** (5 bps/side,
  from `agents/_paper_book.SLIPPAGE_PER_SIDE`), embedded in the fill (`net = raw − 2·slippage`).
  **No separate commission line** (free-tier retail assumption; a commission on top of the
  embedded slippage would double-charge). Spread is subsumed in the slippage. This is stated
  ONCE and covered by the `config_hash`.

## Benchmark

Same-window **QQQ**: buy QQQ at the same `entry_session` open, sell at the same exit bar.
Per-cohort matched excess = `candidate_net_return − qqq_same_window_return` (matches
`scripts/shadow_skipped.py`). Idle-cash / capacity are not modeled — this is a per-cohort
**return-edge** test, not a capital book (that avoids capacity confounds).

## Go/no-go (per experiment, FORWARD block only — paper)

- **n** = independent entry-date cohorts.
- **Tier ① (kill switch):** ≥30 cohorts AND ≥8 weeks AND mean cohort excess ≥ 0 (net) AND no
  single cohort accounts for all the excess AND max cohort-equity drawdown ≤ 20%.
  Returns **continue / inconclusive / fail**. `fail` only on a **clear** negative excess —
  mean cohort excess < **−0.5% net** (`fail_margin`) — OR a max-drawdown breach; a mean in
  `[−0.5%, 0)` is noise at n≈30 → `inconclusive` (keep running). small-n → `inconclusive`
  too — do not bless or kill on noise.
- **Tier ② (scale PAPER):** ≥50 cohorts AND ≥13 weeks AND mean excess clearly positive AND
  profit_factor > 1.4 AND positive in ≥2 sub-periods.
- **Kill rule:** clear negative mean excess after the minimum sample → shelve; diagnose, do
  not keep trading a dead edge.
- **NO real money at any tier here.** Tier ③ / capital is out of scope (existing standard).

## Isolation (never touches production)

- Own `experiment_id`, SQLite DB + committed state JSON under
  `paper_book/experiments/<id>/`, own `forward_epoch` (set ONCE on the first CI run = the first
  session on/after this commit), own `config_hash = sha256(frozen params above, incl. costs)`.
- Reuses `agents/_paper_book.py` money-math + `price_agent.compute_paper_outcome` by
  **import/call only**. **Never** edits `scripts/paper_book.py`, its `sync()`/eligibility
  (`:70`), the production `book_state`/`book.db`, or any production risk gate
  (`feedback_forward_experiment_isolation`).

## Read date and freeze (added 2026-09-04 — no parameter change, `forward_epoch` and `config_hash` stand)

**Verdict read date: 2026-10-30.** Until then nothing in admission, entry, exit, stop,
target, costs or benchmark changes for either experiment. This section adds
**read-side diagnostics only**; it does not alter Tier ① / Tier ② and is not part of
`config_hash` — `config_hash()` in `scripts/forward_experiment.py` hashes only the frozen
parameters (experiment, version, direction, horizon, stop, target, exit policy, slippage,
benchmark, admission rule). `tests/test_forward_experiment.py::
test_robustness_reads_do_not_touch_the_verdict_or_config_hash` pins both live digests
(`f1b6a019…` h1d, `47eb4eb5…` h7d), so any frozen-parameter change without a version bump
fails CI.

Why: on 2026-09-04 the h1d ledger's equal-weighted cohort mean (+1.03%) was carried by
two tickers (`AI` 51% and `NOW` 25% of summed excess); the other 73 positions averaged
+0.30%. A cohort mean lets an n=2 day vote like an n=11 day. So `metrics.json` now also
reports, per experiment:

- `mean_position_excess` — position-weighted net excess vs matched QQQ
- `top_ticker`, `top_ticker_excess_share`, `top_ticker_positions`
- `mean_position_excess_ex_top_ticker`, `n_positions_ex_top_ticker`

**What is read on 2026-10-30, for each of `fwd_prov_long_h1d` and `fwd_prov_long_h7d`:**
Tier ① as pre-registered, **and** `mean_position_excess >= 0`, **and**
`mean_position_excess_ex_top_ticker >= 0`, at ≥30 cohorts. Both experiments clearing
all three → continue to Tier ② (paper). Either experiment clearly negative on the
position-weighted or ex-top-ticker read → the recall generator has no forward edge;
shelve and collapse the architecture to ingest + candidates + experiments.

**Stop-doing list until the read date** (the repo's own history is: return → audit →
fix sprint → forward clock resets → verdict never read):
- No changes to `scripts/forward_experiment.py` admission / exits / costs (a change
  = new experiment version + new `forward_epoch`, and the current clock restarts).
- No "just exclude AI" / per-ticker caps inside the experiment — concentration is
  handled on the read side above, never in admission.
- No capital step, no Tier ② claim, no BUY/SELL wiring on the strength of the
  equal-weighted headline. The ex-top-ticker figure is the one quoted.
- Layers 3/4/6, `realistic_loop` and the production `paper_book` are frozen legacy
  for the duration; defects there are logged, not fixed.

## Deferred (explicitly NOT in this pre-registration)

- The **AVOID_CHASE / bearish short** hypothesis (shadow showed +3.9% gross, capacity-free) —
  a separate, later pre-registered SHORT experiment that must model **borrow** costs. Not here.
