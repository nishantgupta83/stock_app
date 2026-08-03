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
- **Horizon exit:** close at the close of `entry_session + H` trading days (H = 1 or 7).
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
  Returns **continue / inconclusive / fail** (`fail` only on clear negative excess after the
  minimum sample; small-n → `inconclusive`, keep running — do not bless or kill on noise).
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

## Deferred (explicitly NOT in this pre-registration)

- The **AVOID_CHASE / bearish short** hypothesis (shadow showed +3.9% gross, capacity-free) —
  a separate, later pre-registered SHORT experiment that must model **borrow** costs. Not here.
