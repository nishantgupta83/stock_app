# Paper Book — Auto Forward Loop + Edge Instrumentation (A+F)

**Date:** 2026-06-26
**Status:** Design — Codex-reviewed (proceed-with-changes, folded in); pending user approval
**Scope:** First sub-project of the "mature the machine" roadmap. The measuring
instrument every later piece (B learning-gate, C reasoning, D graduation) is judged
against. Builds nothing downstream of itself.

## Context / why

After full pipeline remediation (June 2026), the honest state is:

- `n_mature_rules: 0` of 131 (checked-in `snapshots/2026-06-17.json`). The former lone
  adult rule `8k_material_event::h30d` demoted to `is_mature: false` after the
  `stop_only` regrade. **No rule qualifies for BUY/SELL on honest evidence.**
- The local paper book (`paper_book/book.db`, queried 2026-06-26): 38 closed trades,
  **all long**, net **−$164.38 on $5,000 (−3.3%)**, 37% hit rate. Gains are fat-tail
  (CRDO +$212 alone exceeds the net loss; remove it → −7.5%) and regime-driven.

The binding constraint is **not code quality — it is whether the edge exists forward.**
The book today is mostly a slippage-aware *replay* of backfilled May setups, not a
forward track record. This sub-project converts it into a continuously-running,
unattended forward experiment that grades its own edge against a pre-registered bar.

## Goal

1. **A — Auto forward loop:** the paper book runs itself daily in GitHub Actions,
   unattended, with durable state committed back to the repo (mirroring the existing
   `learning_snapshot.yml` → `snapshots/` convention).
2. **F — Edge instrumentation:** segregate *forward* from *replay*, benchmark against a
   **$5k QQQ buy-and-hold**, and classify the forward book against a staggered success
   ladder — surfaced live on the dashboard.

## Non-goals (explicit)

- No changes to `thesis_agent` / `trade_setup_agent` / any signal logic.
- No Supabase **writes** (the `sync` read stays read-only).
- No execution, no real money, no BUY/SELL graduation automation (that is D + Tier ③).
- No orchestrator-watchdog / cron-job.org pinger registration — isolated v1 to avoid
  the note-#9 four-places false-alarm trap. Watchdog coverage is a follow-up.

## Success definition — the staggered ladder

The solo-person bar: **beat putting the money in QQQ and going to the beach** — after
costs, repeatably, without a blowup or a fluke. The gate compares the **full $5k book
equity curve (including idle cash, modeled at the risk-free rate)** against **$5k QQQ
buy-and-hold from `forward_epoch`** — cumulative and **unannualized**. That is the literal
"beach" test: it charges the book for sitting in cash, which a fully-invested QQQ does
not. Regression alpha/beta and same-slot QQQ are **diagnostics only, never the gate**
(unstable at this n/sparsity — Codex findings 1, 2). Thresholds live in one tunable
config dict (`agents/_paper_book_metrics.py: TIERS`).

| Tier | Gate (all must hold) | Output / Unlocks |
|---|---|---|
| **① MINIMUM — kill switch** | ≥30 independent **entry-date cohorts** **AND** ≥8 weeks fwd **AND** full-book equity ≥ $5k QQQ buy-and-hold (cumulative, net of slippage) **AND** max drawdown ≤ 20% **AND** no single entry-date cohort accounts for all the excess | **continue / inconclusive / fail** — `fail` only on clear negative excess; small-n → `inconclusive` (keep running, don't bless or kill on noise) |
| **② "Edge"** | ≥50 indep cohorts **AND** ≥13 weeks **AND** cumulative excess vs $5k QQQ clearly positive **AND** profit factor > 1.4 **AND** positive in ≥2 sub-periods | Scale paper; take B/C/D seriously |
| **③ "Conviction"** | sustained ② ≥26 weeks **AND** ≥1 rule honestly graduated the maturity gate (D) **AND** excess survives dropping top 3 trades + ≥2 regimes | E — small real-money sizing |

**Minimum bar = kill switch (honest version).** A hard `fail` (clear negative excess vs
$5k QQQ after the minimum sample) → **stop**: diagnose or shelve, do not build B/C/D on a
dead edge. But at n≈30 with fat tails, a bare "≥ 0" is often **noise** — so the gate
returns `continue / inconclusive / fail`, not a confident "alive." The minimum still
binds (the `fail` path); it just refuses to bless OR kill on a number small-n can't
support (Codex finding 4).

**Why the floor is "match QQQ," not "just be positive":** the book is 100% long, so in an
up market it makes money from beta (market rising) regardless of signal skill. Only
"match the index after costs — including the cash-drag handicap" forces actual selection
skill. Matching is the floor; beating is Tier ②.

**Independence** (anti pseudo-replication): the gate counts **independent entry-date
cohorts**, not raw trades — ten different long names entered the same week are **one**
correlated bet (a market drawdown stops them together), not ten. Collapsing only by
`(ticker, entry_date)` is insufficient (Codex finding 3). Raw trade count is reported
separately and does not gate.

## Architecture

Reuse all tested logic; add the thinnest cloud-durability shim.

**Determinism correction (Codex finding 5):** the book is deterministic *only given fixed
bars* — but `replay` fetches yfinance with `auto_adjust=True` and uses *today* as the
replay end (`paper_book.py:95,118`), so split/dividend back-adjustment, bar revisions, or
a prior fetch failure can make a re-replay of the SAME setup produce a different fill/exit
later. A forward track record that silently rewrites itself is worthless. Therefore the
durable state is **`book_setups` + cursor + `forward_epoch` + an immutable realized
ledger**: once a position CLOSES, its fill/exit/return is **frozen** in `book_state.json`
and never re-derived. Only still-OPEN positions re-replay (to detect a newly-hit
stop/horizon), then freeze on close.

### Components

| File | Change | Purpose |
|---|---|---|
| `agents/_paper_book_store.py` | **add** `export_state`, `import_state`, freeze-on-close | Round-trip `book_setups` + cursor + `forward_epoch` + **frozen closed ledger**; closed fills become immutable |
| `paper_book/book_state.json` | **new, committed** (not gitignored) | Durable cross-run state: setups + cursor + `forward_epoch` + frozen closed-position fills/exits. `book.db` stays ephemeral/gitignored |
| `agents/_paper_book_metrics.py` | **new** | F: forward/replay split, two equity curves vs $5k QQQ, tier classification; alpha/beta + same-slot QQQ as diagnostics |
| `paper_book/metrics.json` | **new, committed** | Latest metrics incl. `sync_ok`, tier status (dashboard + audit trail) |
| `scripts/paper_book.py` | **extend** | If `PAPER_BOOK_STATE_JSON` set: hydrate from JSON (incl. frozen ledger) on start, freeze-on-close, export back; compute + write `metrics.json`; `sync` failure → `sync_ok=false`, non-fatal |
| `scripts/paper_book_dashboard.py` | **extend** | Render tier status + progress, forward/replay split, book-vs-QQQ equity, diagnostics |
| `.github/workflows/paper_book.yml` | **new** | Daily unattended run + rebase + commit-back, mirroring `learning_snapshot.yml` |
| `tests/test_paper_book_state_json.py` | **new** | export→import round-trip identity incl. frozen ledger; frozen fills never change on re-replay |
| `tests/test_paper_book_metrics.py` | **new** | equity-curve excess, cohort independence, tier boundaries, top-cohort robustness |

### Data flow (CI run)

```
hydrate SQLite from committed book_state.json   (import_state; restores frozen closed ledger)
  -> sync     pull NEW stock_trade_setups since cursor (read-only; failure -> sync_ok=false, non-fatal)
  -> replay   re-replay OPEN positions only from book_setups + bars (stop_only); FREEZE on close
  -> metrics  forward/replay split + full-book-vs-$5k-QQQ equity curves + tier  -> metrics.json
              (alpha/beta + same-slot QQQ as diagnostics; tier WITHHELD if sync_ok=false)
  -> dash     write dashboard.html (tier status visible)
  -> export   book_setups + cursor + forward_epoch + frozen closed ledger -> book_state.json
  -> commit   only if meaningful change; git pull --rebase before push (empty-diff guarded)
```

Local run (`PAPER_BOOK_STATE_JSON` unset) is **unchanged** — pure local SQLite.

### forward_epoch

Set once, on first CI run, to that date (persisted in `book_state.json`). Setups with
`created_at >= forward_epoch` are **forward**; earlier are **replay**. The current 182
setups (5/18→6/15) are all replay, so the forward book starts empty and accrues genuinely
forward — the forward clock starts at go-live.

### F — instrumentation detail (`_paper_book_metrics.py`)

**Gate (primary):** two equity curves from `forward_epoch`, both starting at $5k:
- `book_equity[t]`: realized PnL of the **frozen** ledger + idle cash at the risk-free rate.
- `qqq_buy_hold[t]`: $5k in QQQ at `forward_epoch`, held.
- `cumulative_excess = book_equity[T] − qqq_buy_hold[T]` (unannualized). The "beach" test;
  it CHARGES the book for cash drag.

**Diagnostics (never gate):** OLS `beta = cov(book_daily, qqq_daily)/var(qqq_daily)`,
`alpha`; same-slot QQQ (QQQ only during active windows) as a selection-skill read.
Dashboarded, but classification ignores them (Codex findings 1, 2).

- `compute_metrics(...)` returns replay block + forward block, each with {n_raw_trades,
  n_independent_cohorts, weeks, book_equity_end, qqq_buy_hold_end, cumulative_excess,
  max_drawdown, top_cohort_excess_share, profit_factor, beta (diag), alpha (diag),
  same_slot_qqq (diag), sync_ok}, plus `tier_status` and `progress_to_next`.
- `classify_tier(metrics, TIERS)` → `continue | inconclusive | fail` for Tier ①, then
  `edge | conviction` once cleared — on the **forward** block only, and **withheld**
  (`inconclusive: sync_failed`) when `sync_ok=false`.

## Error handling / crash-safety

- **`sync` failure** (Supabase down): caught, **`sync_ok=false`** in `metrics.json` +
  `ops_recorder` meta (loud, per "no swallowed failures"); `replay` of open positions
  still runs, but **tier classification is withheld** so a stale book is never marked as a
  current forward result (Codex finding 6). An explicit LOCAL `sync` still fails loudly.
- **yfinance per-ticker failure:** already skipped in `bars_for` (revisit next run). A
  failed fetch never overwrites a **frozen** closed fill.
- **QQQ fetch failure:** metrics mark `benchmark_unavailable`; tier withheld that run; no
  crash; next run recomputes.
- **Empty diff / no-op:** commit skipped (guard on `git status --porcelain paper_book/`);
  JSON kept compact; only meaningful state/metric changes committed.
- **Missed cron run:** harmless — frozen closed trades persist; open positions re-replay
  next run; only new-setup pickup is delayed and the cursor catches it up. No pinger/retry
  scaffolding needed.
- **Push race with human pushes to `main`:** workflow `concurrency` only serializes THIS
  workflow, not all of `main` — so `git pull --rebase` before `git push`, retry once; a
  still-rejected push fails loudly and the next run recovers (Codex finding 6).

## GitHub Actions workflow

Mirror `learning_snapshot.yml`:
- `cron: "30 22 * * 1-5"` (weekday 22:30 UTC, after price_agent 21:30 + snapshot 22:00).
- `concurrency: { group: paper_book, cancel-in-progress: false }`; `permissions: contents: write`.
- `ops_recorder.py --phase start/finish --agent workflow_paper_book`.
- `actions/setup-python@v6` 3.12, `cache: pip`; install yfinance + deps.
- Run `python scripts/paper_book.py run` with `PAPER_BOOK_STATE_JSON=paper_book/book_state.json`
  and `SUPABASE_URL` / `SUPABASE_SERVICE_KEY` secrets.
- `git pull --rebase` then commit `paper_book/{book_state.json,metrics.json,dashboard.html}`
  (empty-diff guarded), message `chore(paper-book): auto forward mark YYYY-MM-DD`.

## Testing

- `test_paper_book_state_json.py`: build a book, `export_state` → fresh conn →
  `import_state`, assert setups + cursor + `forward_epoch` + frozen ledger identical;
  assert a re-replay with *perturbed bars* leaves frozen closed fills unchanged.
- `test_paper_book_metrics.py`: synthetic frozen ledger + synthetic QQQ → known
  cumulative excess; cohort-independence counting; tier classification at each boundary
  + `sync_ok=false` withholding; top-cohort-excess-share robustness.
- Existing `test_paper_book.py` / `test_paper_book_store.py` stay green.

## Dependencies / preconditions / risks

- **Precondition (live check, deferred):** A+F can only accrue forward data if the
  pipeline is *currently emitting* `stock_trade_setups`. Layer-2 silence has recurred
  before. Before/just-after merge, verify recent `stock_trade_setups` rows exist (one
  PostgREST GET, private shell). If empty, the forward test starves — a separate diagnosis
  (is Layer 2/3 emitting?), not part of A+F.
- yfinance is the only price source (zero Supabase egress for bars). Mutable adjusted bars
  are handled by freezing closed fills; transient failures self-heal next run.
- Adds near-zero Supabase read egress (sync = minimal incremental read, daily).
- Idle-cash risk-free rate: a static config constant in v1 (e.g. ~5%/yr); a live T-bill
  feed is a follow-up.

## Codex review (2026-06-26, folded in)

Independent Codex pass — verdict **proceed-with-changes**. All seven findings folded in
above; provenance noted inline. Headline corrections: (1) the gate is the **full-book vs
$5k-QQQ buy-and-hold equity curve**, not OLS alpha or same-window QQQ (those are
diagnostics); (2) **determinism was overstated** — closed fills are now **frozen** because
yfinance bars are mutable; (3) independence counts **entry-date cohorts**, not
`(ticker, entry_date)`; (4) Tier ① returns **continue/inconclusive/fail** (small-n
honesty); (5) **`sync_ok` gates tier**; (6) **rebase before push**. Verified Codex's code
citations against `paper_book.py:95,118,126` — accurate.

## Open questions (for tuning, not blocking)

- Tier numbers (30 / 8wk / 20% DD / PF 1.4) and the idle-cash rate are tunable in `TIERS`.
- Dashboard publish target: committed `dashboard.html` in v1; optional GH Pages /
  Hostinger publish is a follow-up.
- "Regime" definition for Tier ③ — coarse calendar sub-periods in v1; can sharpen to
  VIX/market-state buckets later.
