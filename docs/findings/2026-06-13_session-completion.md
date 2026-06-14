# 2026-06-13 — Session completion record (e2e-review remediation)

Canonical record of the remediation of the [2026-06-12 end-to-end review](2026-06-12_e2e-review.md).
**23 commits**, every one: claim verified (code + live where possible) → plan
Codex-reviewed (neutral) → TDD → implemented → Codex diff-reviewed → live-validated.
438 tests pass; tree clean; all pushed to `origin/main`.

## Validation discipline applied to EVERY PR
1. **Factual verification first** — the claim was reproduced against code AND live
   Supabase before any change (e.g. H1's per-rule effective-n table, H6's true
   open/closed counts, H2's 76%-after-close measurement, M8's stuck-signal lanes).
2. **Codex independent review** — plan (neutral prompt) + diff, separate instance
   per PR, `< /dev/null`. It caught real latent bugs that self-review missed:
   H1 missing import (would `NameError` live), H6/H8/M5 read-side staleness gaps,
   H4b false-status strings, C1 retention hazard.
3. **TDD** — failing test first, then minimal code; full suite after each.
4. **Cross-module / e2e checks** — after calibration-affecting changes, re-ran
   the C1 integrity check (counters == truth, 0 inflated) to prove no corruption
   leaked across modules; triggered the affected live workflows and confirmed
   green + correct job-run status.

## Shipped

### Criticals
- **C1** `a3abae5` — stop the DRY_RUN archive-ratchet + gated counter repair
  (89 rows fixed, 32 inflated). Live: 0 inflated on re-run; tariff demoted.
- **C2-core** `bd4abf4` — maturity gate scoped to the EMITTED horizon +
  tradeable-instrument guard (L2 thesis + L3 trade_setup, shared `_instruments`).
- **C2-pflag** `2f28a72` — `recompute_rule_payoff` is the authoritative fresh-PF
  flag writer; `upsert` stops promoting on stale PF; shared `_maturity` gate.
- **C3** `6b0359c` — L3/L4 write losses + price reconcile crash + signal-outcome
  losses record `partial`+meta; new `pulsecheck/trade_layers` owner.
- **Validation** — all 6 affected workflows green; calibration consistent post-run.

### HIGH (all 8)
- **H1** `aa4bd8b` — gate maturity on EFFECTIVE-n (distinct ticker-day clusters;
  `_maturity.collapse_to_effective`). Pseudo-replication inflated PF ~30-50%. Live
  bulk-recompute: **adult = exactly `{8k_material_event::h30d}`** (was 7). sql/0041
  applied + effective_* populated for all 118 rules.
- **H2-core** `559c753` — bump the entry anchor +1 day past the 16:00 ET close
  (ET-converted before date; event_at + created_at). Forward-only.
- **H3** — subsumed by C1 + the C2-pflag cache-mean fix.
- **H4 / H4b-public** `2f28a72`/`cd56e8b` — DB writers use the shared gate;
  status.json publishes the payoff-first gate (dynamic from the adult-h1d set).
- **H5** `c210991` — lane-scope `retry_dispatch_failed` to MODEL_VERSION.
- **H6** `a60addb` — paginate risk portfolio-state reads (was capped 2000 of 8k+).
- **H7-core** `509e4dc` — price_agent watchdog 28h→5h (2h cadence) +
  learning_snapshot coverage; rule-#9 4-place alignment.
- **H8** `10a6bf0` — daily-risk budget binds across the batch + prospective cap.

### Mediums (6 of 8)
- **M1** `a1b074a` — backtester stops writing live `stock_agent_weights` (loop
  isolation; opt-in flag).
- **M2** `b347224` — resolved-by-guard (archive DRY_RUN → 7/06 corpus complete).
- **M3** `cd3354b` — walk-forward purge requires the close settled ≥1 trading day
  (no same-day not-yet-reconciled leakage). *Precondition for 7/06.*
- **M5** `85c1477` — recompute realistic_loop state from the ledger (crash-safe);
  mark+open WRITE and GATE on ledger-derived state.
- **M6.1+M6.2** `d16b1ec` — backtester per-chunk audit pairing; flows 13F
  prior-snapshot scoped-to-latest-filing + paginated (no fabricated diff events).
- **M8-core** `d18da26` — lane-scope `fetch_mature_signals` + `count_open_signals`.

## Remaining (in `2026-06-12_remaining-prs.md`)
M4 (ingest swallow + bus-volume pulse), M6.3 (NULL-dupe index migration),
M7 (egress docs/estimator), Lows; minor sub-parts (M1-2, M5-2, M8-2); deferred
HIGH sub-parts (H2b open-trade re-anchor, H4b-2 progress-viz, H7-2 intraday
session-aware). **Historical-re-run analysis** is queued after M4/M6.3/M7/Lows —
see `2026-06-13_historical-rerun-analysis.md` (to be written).

## Cross-cutting honest outcome
H1 + H2 together showed pseudo-replication AND after-hours leakage were both
inflating the 8-K family. On honest evidence, no h1d cell is adult → no BUY/SELL
fires. The system is correctly paper-only; the maturity numbers it now reports
are trustworthy (calibration counters verified == truth throughout).
