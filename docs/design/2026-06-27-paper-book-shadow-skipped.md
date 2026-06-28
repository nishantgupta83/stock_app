# Shadow-Skipped Forward-Return Audit (A+F extension)

**Date:** 2026-06-27
**Status:** Codex-reviewed (proceed-with-changes, folded in); pending user approval
**Scope:** A SEPARATE, standalone audit that forward-grades the trade setups the pipeline SKIPS —
per-setup, capacity-free, stratified by skip-category — to learn whether a gate is over-filtering
real edge, and to surface skip-machinery anomalies. The tradeable book (`paper_book.py`) and the
pipeline are UNTOUCHED.

## Context / why

Live check (2026-06-27): the pipeline produces `stock_trade_setups` daily but **0 are tradeable**
in the last 7 days — every one is skipped (`reason_to_skip` set), mostly `rule … profit_factor <
1.0 (no payoff edge)`, plus `AVOID_CHASE` and `not a tradeable instrument`. So the tradeable
forward book starves. The question becomes **"is the pipeline right to skip everything — or is a
gate over-filtering real edge?"** — and the skip machinery has at least one visible anomaly
(`CVX`/Chevron flagged "not a tradeable instrument").

## Goal

For every PRICEABLE skipped setup, measure its forward stop_only return vs a matched QQQ window
return, **stratified by skip-category**, and report:
1. Per category {payoff, vocabulary, instrument-priceable, other}: does that gate's skipped basket
   beat QQQ forward? → **which gate (if any) over-filters**.
2. An **anomaly list**: instrument-flagged setups that ARE priceable (e.g. CVX/Chevron) — potential
   instrument-gate bugs, for review.

## Why capacity-free per-setup, not a $5k portfolio (Codex #4)

A shared $5k/5-slot portfolio of skipped setups makes the categories COMPETE for 5 slots before
measurement — it answers "which admitted remnants did well," not "which gate over-filters." For a
gate diagnostic you want every priceable skipped setup measured INDEPENDENTLY (no cap), grouped by
why it was skipped. So this is a per-setup forward-return audit (% returns), not a dollar
portfolio. A capacity-constrained $5k view is a possible LATER promotion for a category that proves
out — out of scope here.

## Non-goals (explicit)

- **Do not touch** `scripts/paper_book.py`, the shared `agents/_paper_book*.py` behavior, or any
  Layer 1–5 agent. This is a NEW standalone script — strongest "don't break anything."
- No shared-schema change (the `book_setups` columns) — would leak nulls into the tradeable book's
  committed `book_state.json` via `export_state`'s `SELECT *` (Codex #1).
- No Supabase **writes** (sync is a read-only GET). No execution, no real money.
- Reporting a confirmed anomaly (CVX) here; FIXING the instrument gate upstream is a separate PR.

## Architecture — standalone + isolated

- **New `scripts/shadow_skipped.py`** (standalone). Reuses READ-ONLY: `price_agent.
  compute_paper_outcome` (the stop_only grader), a minimal Supabase GET, yfinance bars. Does NOT
  import or alter `paper_book.py`'s run path.
- **New `agents/_shadow_skipped.py`** — pure functions: `categorize_skip`, the per-category
  forward-return aggregation, the anomaly audit (no DB, no network — testable like
  `_paper_book_metrics.py`).
- **Own storage** `paper_book/shadow/shadow.db` (gitignored) with its OWN tables (setups + frozen
  per-setup outcomes incl. `reason_to_skip` + `skip_category`) — NOT the shared `book_setups`.
- **Committed artifacts** under `paper_book/shadow/`: `report.json` (the per-category audit) +
  `state.json` (cursor + frozen outcomes for durability). Per-setup outcomes are FROZEN once
  resolved — mutable yfinance bars can't rewrite a recorded result.

## Method (per-setup, capacity-free)

1. **sync** (incremental, read-only): `stock_trade_setups?reason_to_skip=not.is.null&created_at=
   gt.<cursor>&order=created_at.asc&select=id,signal_id,ticker,direction,created_at,target_pct,
   stop_pct,horizon_days,valid_until,reason_to_skip`. Store each new skipped setup.
2. **categorize** each: `categorize_skip(reason)` → `payoff` ("profit_factor"/"no payoff edge"),
   `vocabulary` ("AVOID_CHASE"/"CHASE_RISK"/"intelligence flagged"), `instrument` ("not a tradeable
   instrument"/"fund"/"placeholder"), else `other`.
3. **grade** each setup with a PRICEABLE ticker: entry = next session open after `created_at`; exit
   via `compute_paper_outcome(..., exit_policy="stop_only")`; **freeze** {entry_date, entry_px,
   exit_date, exit_px, return_pct} once resolved. Matched QQQ return over the SAME [entry, exit].
   Per-setup excess = `return_pct − qqq_window_return_pct`.
4. **quarantine ONLY unpriceable** tickers (no bars → can't grade; counted, not measured). Every
   priceable setup — including instrument-flagged ones — IS measured (Codex #5).

## Report (`paper_book/shadow/report.json`)

- **Per category** {payoff, vocabulary, instrument, other}: `n_setups, n_priceable, n_resolved,
  mean_return_pct, mean_excess_vs_qqq_pct, win_rate, median_excess_pct`.
- **Overall priceable-skipped**: same aggregate.
- **Anomalies**: `instrument`-categorized but PRICEABLE — `[{ticker, reason_to_skip, return_pct,
  excess_pct}]`, labeled **"potential anomalies for review"** (priceable ≠ confirmed bug — yfinance
  also prices ETFs/ADRs/stale symbols; Codex #6).
- **`reason_distribution`**: observed `reason_to_skip` → count, so the keyword categorizer can be
  tuned against reality.
- **`sync_ok`**: false if the sync failed (don't present stale as current).

## Error handling / robustness ("validate for anomalies")

- Unpriceable ticker: `bars_for` returns `{}` → setup quarantined, no crash.
- `sync` failure → `report.json.sync_ok=false`; still re-grade existing setups; cursor not advanced.
- Uncategorized reason → `other` (counted, surfaced via `reason_distribution`).
- Empty / all-unresolved basket → per-category stats null + `status: insufficient`.

## Isolation guarantees + testing

- **Legacy byte-identity test** (Codex #7): assert the tradeable book's `book_state.json` shape,
  `metrics.json`/`dashboard.html` paths are unchanged — proving zero shared-schema/export churn.
- `shadow_skipped` writes ONLY under `paper_book/shadow/` (asserted).
- `categorize_skip`: each category's keywords → correct label; unknown → `other`.
- per-category aggregation on a synthetic frozen ledger: known means/win-rates; quarantine excludes
  unpriceable, INCLUDES priceable-instrument; anomaly list flags a priceable instrument row (CVX
  stub via injected `priceable_fn`); empty → `insufficient`.

## Workflow + gitignore

- **New `.github/workflows/paper_book_shadow.yml`** — mirrors `paper_book.yml` shape (checkout,
  ops_recorder, setup-python, `pip install -r requirements.txt`, run `scripts/shadow_skipped.py`,
  rebase+commit `paper_book/shadow/`, empty-diff guard). Cron `45 22 * * 1-5` + `workflow_dispatch`.
  `paper_book.yml` is NOT touched.
- `.gitignore` nested negation (Codex confirmed correct; verify with `git check-ignore -v`):
  ```
  !paper_book/shadow/
  paper_book/shadow/*
  !paper_book/shadow/state.json
  !paper_book/shadow/report.json
  ```

## Open questions (tuning, non-blocking)

- Categorizer keyword map — tune against the live `reason_distribution` after first run.
- Optional anomaly metadata (quote_type / exchange / avg $ volume) — defer unless cheap via yfinance.
- A capacity-constrained $5k shadow book for a category that proves out — explicit LATER promotion.
