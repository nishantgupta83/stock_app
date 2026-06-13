# 2026-06-12 — Remaining PR queue (post-Criticals)

Forward work plan after the C1/C2/C3 + H3/H4-writers/H6 work shipped this session
(see [`2026-06-12_e2e-review.md`](2026-06-12_e2e-review.md) for the full review +
progress log). Each row below is **one PR, one behavior, with its own
measurement gate** (per the standing one-behavior-per-PR rule), and runs the
standard discipline: verify the claim live → plan → Codex-review (neutral) →
TDD → implement → live-validate.

## Recommended order

1. ~~**H1** — effective-n gating~~ ✅ **DONE `aa4bd8b`** (see below)
2. ~~**H2-core** — after-hours entry anchor (forward fix)~~ ✅ **DONE `559c753`**
   → **H2b** (open re-anchor + closed cohort) open — see H2 below ← NEXT
3. **H8** — daily-risk budget batch reuse *(risk correctness; small)*
4. **H5** — `retry_dispatch_failed` lane-scope *(smallest; 1-line + test)*
5. **H4b** — payoff-first maturity *display* redesign *(dashboard/report honesty)*
6. **H7** — watchdog / dashboard staleness *(ops false-alarms)*
7. **M2 + M3** — *before* the 2026-07-06 metalabel funnel re-run (preconditions)
8. **M1, M4–M8** — opportunistically
9. **Lows** — batch into a single cleanup PR

> Why this order differs from "biggest-first": H1 deflates the inflated PF/DD
> numbers that H6 just exposed and that H2 also touches, so it should land before
> any decision that reads those numbers. H8/H5 are small correctness wins. H4b is
> display-only (the dangerous writers are already fixed). M2/M3 are time-boxed by
> the 7/06 funnel re-run.

---

## HIGH

### H1 — effective-n: calibration n is not independent evidence ✅ DONE `aa4bd8b`
- **Shipped:** `_maturity.collapse_to_effective` (one obs per (ticker,entry-day)
  cluster; cluster return = mean; correct = mean>0; PF on cluster means).
  `recompute_rule_payoff` + `recompute_maturity_flags` + `backfill` gate on
  effective; `risk_agent.maturity_tier` NULL-fallback fails-to-child. sql/0041
  adds `effective_*` columns; `scripts/recompute_effective_all.py` bulk-applied.
- **Live result:** pseudo-replication inflated PF ~30-50%. 8 demotions —
  **adult is now exactly {`8k_material_event::h30d`}**, young_adult/teen pseudo
  rules → child. No h1d cell is adult ⇒ no BUY/SELL on honest evidence (the
  correct "honest maturity path"). C1 counters intact (0 inflated after).
- **✅ HANDOFF RESOLVED:** `sql/0041` applied via `supabase db query` (out-of-band,
  same pattern as 0031-0040; avoids the migration-history hazard). `effective_*`
  populated for all 118 rules via `recompute_effective_all.py --commit`. Verified:
  8k::h30d effective_n=450/PF=2.12 (adult); 8k::h7d eff_n=487/PF=1.34 (child).
- Original detail (for reference):
- **Why:** 61–93% of closed rows are duplicated `(ticker, entry-day)` pairs; one
  market move fans into 8+ "observations." Every n-based gate (maturity tiers,
  metalabel min-n, confidence) over-counts. This inflates the PF/DD figures H6
  surfaced and flatters the 8-K family at the gate.
- **Scope/approach (decide in the plan):** add an **effective-n** (distinct
  ticker-day, or event-cluster) computed alongside raw n, and switch the gates to
  effective-n — OR dedupe at trade-open per `(rule, ticker, day)`. Likely a new
  column/view + `derive_maturity_flags` taking effective-n. Decide trade-open
  dedupe vs read-time effective-n in the plan (trade-open is cleaner but changes
  the corpus going forward only; read-time fixes history too).
- **Files:** `agents/_maturity.py` (gate input), `agents/price_agent.py`
  (recompute), `agents/event_paper_agent.py` (if dedupe-at-open), possibly a view.
- **Measurement gate:** the duplication query per rule (distinct ticker-day vs
  raw n); re-evaluate adult tier on effective-n — expect the 8-K family + defense
  young_adult to move; confirm no rule is adult on <30 effective-n.
- **Risk:** HIGH — touches the maturity gate. Needs careful live re-validation of
  every adult/young rule before + after.

### H2 — after-hours entry leakage residual
- **✅ H2-core DONE `559c753`:** `_entry_anchor_from_ts` bumps the anchor +1 day
  when the event is at/after the 16:00 ET close (ET-converted before date; both
  event_at + created_at floor). All FUTURE event paper-trades anchor correctly.
  Verified: 76% of live 8-K events are after-close. 9 TDD edge tests.
- **⬜ H2b OPEN (Codex HIGH — forward fix alone is insufficient):** still-OPEN
  trades opened pre-fix will close later with leaked entries. Scope is SMALL
  (measured: 27 of 32 open 8-K trades re-anchorable; total open after-hours
  cohort ~tens, NOT the 7020 all-open count — trades age out in 1-30 days).
  - **Open trades:** one-shot re-anchor `entry_at`/`entry_price` → next-session
    close (needs stock_raw_prices lookup per (ticker, next-session); defer rows
    whose next-session close hasn't landed). Gated dry-run→commit like the C1
    repair. It IS a live entry_price mutation → own focused PR.
  - **Closed trades:** their realized_return baked in the leaked entry →
    calibration metrics tainted. Recommended: ACCEPT + age out (rewriting closed
    history with hindsight is wrong; H1 already gates everything to non-adult, so
    no BUY/SELL rides the tainted metrics — new clean data dominates over time).
    Re-derive only if a cohort study needs it.
  - Priority: LOW-urgency given H1's non-adult gating; do as a focused PR.
- Original detail:
- **Why:** events 16:00–19:59 ET anchor to that same day's 16:00 close (a
  pre-event price), crediting the overnight gap to the trade — flatters the
  8-K/earnings rules nearest the gate (`event_paper_agent.py:346-368`).
- **Approach:** bump the entry anchor a day when event time ≥16:00 ET.
- **Measurement gate:** sample 50 closed 8-K trades, recompute cohort PF
  excluding the first overnight gap; confirm the anchored entry matches.
- **Risk:** MEDIUM-HIGH — changes every future entry price → realized_return →
  calibration. Forward-only effect; validate the anchor logic on known cases.

### H8 — daily-risk budget reused across batch decisions *(found during H6)*
- **Why:** `compute_portfolio_state.daily_risk_in_flight_pct` is computed once and
  reused for every `evaluate_setup` in the batch, so multiple same-run `size`
  decisions can collectively exceed the 3% daily cap (`risk_agent.py` ~303-311 +
  loop ~483).
- **Approach:** accumulate tentative `max_loss_dollars` across decisions in the
  batch loop, or evaluate/write sequentially re-reading in-flight.
- **Measurement gate:** unit test — a batch that would breach 3% has its later
  sizes skipped; the sum of sized `max_loss_dollars` ≤ cap.
- **Risk:** LOW-MEDIUM — isolated to the batch loop.

### H5 — `retry_dispatch_failed` not lane-scoped
- **Why:** `thesis_agent.py:1928-1943` re-queries `dispatch_failed` without
  `model_version` — the exact class `e35aa89` fixed one function over. Latent (0
  rows live) but a Telegram outage queues foreign-lane failures that burn thesis's
  5/day budget (the 5/22–6/2 silence via a new door).
- **Approach:** add `model_version=eq.{MODEL_VERSION}` (1 line) + decide who
  sweeps foreign lanes.
- **Measurement gate:** mirror test of `tests/test_l3_input_filter.py` (a
  foreign-lane dispatch_failed row is not picked up).
- **Risk:** LOW.

### H4b — payoff-first maturity *display* redesign  ✅ PUBLIC PART DONE
- **✅ H4b-public DONE:** status.json now publishes the payoff-first gate
  (effective-n>=100/PF>=2.0/mean>=0.5%, imported from `_maturity`), the
  emission_status is dynamic from the adult-h1d set (fixes the false 'no rule
  crossed 0.90'), and the superset data-note + stale comments are corrected.
- **⬜ H4b-2 OPEN (internal progress-viz redesign, render-coupled, low-urgency):**
  the accuracy-gap 'progress toward maturity' surfaces still use the old model —
  site_generator §2 scorecard (`MATURE_ACC=0.90`/`gap_acc`, raw n) +
  learning_snapshot near-threshold (200-260). Redesign to show gap toward the
  payoff-first dims on effective-n. These are internal weekly-scorecard/snapshot
  views (the public status.json + the stored is_mature data are already correct).
- Original detail:
- **Why:** the dangerous DB writers are fixed (H4, `2f28a72`), but three
  *accuracy-gap progress* surfaces still describe the OLD gate and are coupled
  (fixing one field leaves the dashboard internally inconsistent):
  - `site_generator.py` — status.json `maturity_gate.production.min_accuracy:0.90`
    (1426-1432), §2 "Rule maturity (toward 90%)" scorecard `MATURE_ACC=0.90` +
    `gap_acc` (627-636), "no rule crossed 0.90" string (1423), comment (1762).
  - `scripts/learning_snapshot.py` — `TIER_GATES` adult tuple (117) + the
    accuracy-gap "closest to promotion" / payoff-sanity surfaces (200-260).
- **Approach:** import the canonical gate from `agents/_maturity.py`; redesign the
  "progress toward adult" surfaces around the payoff-first criteria (n→100,
  PF→2.0, mean→0.5%) instead of accuracy-gap. Publish the real gate in status.json.
- **Measurement gate:** grep `0.90`/`1.5` gate literals → only `_maturity.py`;
  status.json shows the payoff-first gate; snapshot promotion surface uses it.
- **Risk:** LOW (display/report only) but needs a coherent redesign, not a swap.
- Also fold in: backfill's unpaginated `limit:5000` read.

### H7 — watchdog / dashboard false-stale cluster  ✅ H7-core DONE
- **✅ H7-core DONE:** the DANGEROUS gaps (real stalls hiding) — price_agent
  watchdog tightened 28h→5h to match its every-2h-weekday cron (a 513-class
  stall hid for a day+); dashboard expected_minutes 1440→120 (rule #9 4th place);
  learning_snapshot added to the orchestrator EXPECTED list (had NO coverage,
  failed silently 5/30-6/08). Verified live (Sat, trading_only slack → no false fire).
- **⬜ H7-2 OPEN (false-alarm NOISE, needs new logic, low-urgency):** orchestrator
  flags intraday stale every trading morning (2h budget vs overnight session gap)
  — needs US-RTH session-awareness (`_market_calendar` has no market-hours by
  design); and site_generator self-flags on status=running (the stock_agent_freshness
  view treats a fresh 'running' as stale). Neither is dangerous (false positives).
- Original detail:
- **Why:** orchestrator flags intraday stale every trading morning (2h budget vs
  overnight gap); `status=running` counts as stale so site_generator always flags
  itself (`site_generator.py:1150`); price_agent watchdogs never tightened after
  the 2h-cadence bump (28h/1440min — a new 513-class stall hides a day+);
  learning_snapshot has no watchdog (already failed silently 5/30–6/08).
- **Approach:** session-aware budgets; treat fresh `running` as healthy; align
  rule-#9's four places for price_agent; add a snapshot expectation.
- **Measurement gate:** next 04:30 UTC orchestrator run flags nothing falsely.
- **Risk:** LOW-MEDIUM (ops logic; verify no real stall is now masked).

---

## MEDIUM (compact — see e2e-review.md for full detail)

| # | Finding | Fix direction | Timing |
|---|---------|---------------|--------|
| **M2** ✅ RESOLVED-BY-GUARD | Archive DELETION (not yet live) would erode the validator corpus | archive_agent is DRY_RUN (0 archived, verified) so the active corpus is COMPLETE → the 7/06 re-run is NOT underpowered. GUARD: keep archive deletion disabled through the 7/06 re-run. If deletion is ever enabled before then, the validator must fetch+merge the per-trade JSONL.gz archive (feasible — archive_agent serialises per-trade rows before delete). Not shipping speculative merge code against non-existent archived data. | done (guard) |
| ~~**M3**~~ ✅ DONE | Walk-forward gate counted same-day not-yet-reconciled outcomes | now requires next_trading_day(close) < as_of (TDD) | done |
| **M1** | backtester upserts live `stock_agent_weights` unmarked | source column / date-guard | — |
| **M4** | L1 ingest agents finish `ok` on bus-write failure; no volume pulse (same swallow class as C3, read side) | partial + bus-volume pulsecheck | — |
| **M5** ✅ DONE `85c1477` | realistic_loop mark non-atomic → crash mid-loop leaked cash forever | recompute_state() derives full state from ledger (crash-safe); mark+open WRITE and GATE on ledger-derived state (TDD). ⬜ minor: tag agent-path backfills | core done |
| **M6** | backtester partial-insert zips audit→wrong signals; flows 13F truncation fabricates events; market_scanner NULL prior_event_id dupes | per-chunk zip; paginate; NULLS NOT DISTINCT | — |
| **M7** | egress 78% of budget; estimator model stale; CLAUDE.md rule #6 "all cancel-in-progress" false for 7 pinged; bootstrap file ≠ provisioned state | fix docs + estimator; reconcile bootstrap before re-running | careful: console-check cron-job.org first |
| **M8** ✅ CORE DONE | lane-unscoped signal reads caused churn + dashboard conflation | ✅ fetch_mature_signals + count_open_signals now lane-scoped to THESIS_MODEL_VERSION (TDD). ⬜ M8-2: close the stale stuck 'sent' signals (3 MACRO foreign-lane + INST_* in-lane, hygiene); dead telegram_dispatcher inventory entry; _lanes.py 'two producers'→9; route pulse criticals to Telegram | core done |

---

## LOW (batch into one cleanup PR)

- Stale 42P10/index folklore in comments (`filing_agent:314`,
  `telegram_dispatcher:106`, `event_paper:233`).
- CLAUDE.md rule #1's intraday parenthetical is wrong (intraday correctly uses
  `event_at`).
- v6 actions bump missed 3 files vs its commit message.
- `is_high_conviction` computed but never persisted.
- `PF=inf` passes the metalabel gate (document or clamp).
- `L3_INPUT_STATUSES` drops >24h-old retried signals (document the trade-off).
- Deferred from C3: source `sb_get` GET failures collapse to `[]` → `ok` empty
  run (broad read-contract change; touches every agent — its own PR, arguably
  Medium).

---

## Funnel go-live (2026-07-06 metalabel re-run)

Preconditions that MUST land first: **M2** (archive corpus) + **M3** (timestamp).
Then the criteria in the e2e-review doc's "Meta-labeling funnel — go-live
criteria" section (≥30 labeled/arm/horizon post-PR1A; ACT>SUPPRESSED bootstrap
90% CI excluding zero on ≥2 horizons; same-sign across 90d/180d × pf_bar ∈
{1.3,1.5,2.0}; fail-open <~60–80%; 2–4 wks shadow; PR-C maps calibration-fetch
exceptions to fail-open WATCH). The re-run is already on the calendar via the
existing `validate_metalabel_gate.py` reminder.

## Maturity-path note

No h1d cell legitimately qualifies for BUY/SELL today; the genuine edge is the
8-K family at h7/15/30 (which the system cannot emit or grade yet — everything is
h1d on daily closes). H1 + H2 currently flatter that family's numbers. The honest
unlock is either (a) wait for an h1d cell to mature on clean effective-n data
post-H1, or (b) the horizon-aware emission refactor (larger; where the edge
measurably lives). Track (b) as a future epic, not a PR in this queue.
