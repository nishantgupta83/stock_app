# 2026-06-13 — Test-suite coverage state + policy

Written because the pipeline changes daily, so the suite must catch regressions.
Snapshot: **53 test files, 453 tests, green; run in CI (`tests.yml`) on every
push + PR.**

## Coverage by layer (what's protected today)

| Area | Coverage | Notes |
|------|----------|-------|
| **Shared gates/contracts** (`_maturity`, `_instruments`, `_lanes`, `_metalabel_gate`, `_rule_key`, `_market_calendar`) | ✅ unit + import-smoke | the safety-critical core — gate logic, effective-n, tradeable, walk-forward |
| **Layer 2** thesis (scoring, action gate, cluster, tradeable guard, lane filter) | ✅ strong | many unit + characterization tests |
| **Layer 3/4** trade_setup, risk (sizing, daily cap, drawdown, instrument guard, pagination) | ✅ strong | |
| **Layer 5** price_agent (maturity flags, payoff recompute, effective-n, reconcile status), event_paper (anchor), realistic_loop (ledger recompute) | ✅ strong (pure helpers) | I/O paths tested via monkeypatch |
| **All agents** import-smoke | ✅ every `agents/*.py` + shared `_*` | catches import-chain breaks at CI, not 04:00 UTC |
| **Layer 1 ingest** (filing, news, truth_social, earnings, biotech, defense, …) | ⚠️ **smoke-only** | the normalize/parse transforms are untested |

## The real gap: L1 ingest normalizers

The ingest agents fetch external sources → NORMALIZE → write the event bus. The
fetch is I/O (hard to unit-test), but the **normalize/parse transform is pure**
(raw payload → `stock_normalized_events` row: event_type, subtype, severity,
direction prior, dedupe_key). That transform is where a daily change (new source
field, changed keyword DB, severity tweak) silently breaks ingestion — and it's
untested. M4's bus-volume pulse now catches a TOTAL collapse, but not a
single-source normalize regression.

## Policy (the discipline that's been applied)

1. **Every behavior change ships with a test, TDD-first** (failing test → minimal
   code). This session added a test per fix (C1–M8); keep it.
2. **Import-smoke covers every module** — never let an import break reach prod.
3. **Pure-extraction for testability**: when fixing I/O code, extract the pure
   decision into a helper (e.g. `classify_bus_volume`, `derive_maturity_flags`,
   `collapse_to_effective`, `write_run_status`) and test the helper. Do the same
   for the ingest normalizers as they're touched.

## Recommended next coverage work (incremental, by risk)

1. **Ingest normalizer tests** — for each L1 agent, extract + test the pure
   raw→event transform (start with the highest-churn: news_agent keyword
   classification, filing_agent 8-K parsing, truth_social pattern matching).
2. **`ops_recorder`** — shared workflow status helper, currently smoke-only.
3. **Consider `pytest-cov` in CI** (non-blocking report first) to TRACK coverage
   so it can't silently regress; promote to a floor once the ingest tier lands.

This doc is the checklist for closing the L1 gap over the coming PRs.
