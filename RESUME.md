# Resume Work Session

**Saved**: 2026-09-23 17:05 PT
**Project**: /Users/nishantgupta/Documents/nishant_projects/stock_app

---

## Completed

### Supabase quota emergency — resolved
- DB was at **0.592/0.5 GB = 118%**, grace period expired (402s imminent). Root cause: the
  three largest tables were operational logs **no archiver had ever covered**:
  `stock_job_runs` 125 MB, `stock_thesis_rejections` 103 MB, `stock_health_pulse` 40 MB
  = 268 MB = **45% of the database**.
- Built `scripts/prune_supabase_logs.py` (dry-run default, `--apply`, `--days` override,
  FORBIDDEN list protecting calibration/paper-trades/signals). **Operator ran it + VACUUM.**
  `stock_health_pulse` confirmed 112,692 → 18,224 rows.
- Two bugs found and fixed during the run: guessed `created_at` (real column is `fired_at`,
  `sql/0035:27`) and the run still **exited 0** while skipping the 103 MB table; and
  PostgREST's `Range` header bounds the RESPONSE not the DELETE, so "batches" tried to
  delete 156,965 rows in one statement → HTTP 500. Now id-batched at 500.
- `.github/workflows/prune_supabase_logs.yml` — weekly Sun 05:00 UTC, **applies on schedule**.
- Added the three tables to `agents/archive_agent.py` TABLES with a new `"archive": False`
  flag (delete, don't export). Also replaced that agent's own latent unbounded DELETE.

### **MAJOR DISCOVERY: archive_agent has never deleted anything**
`.github/workflows/archive_agent.yml:53` → `DRY_RUN: ${{ github.event.inputs.dry_run || 'true' }}`.
On a `schedule:` event `github.event.inputs` is null → **`DRY_RUN='true'` every Sunday**.
Confirmed against the live DB: all six export tables have **zero rows with `archived_at` set**.
Tiered storage has only ever been the *storage* half since Phase 9. **Not flipped** — doing so
would enable live deletion of `stock_normalized_events`, `stock_event_paper_trades`,
`stock_signals`, `stock_raw_prices`, `stock_raw_filings`,
`stock_institutional_holdings_snapshot` for the first time ever. That is a separate decision.

### ai_humanoid screen — built, committed, never run
- `scripts/ai_humanoid_screen.py` + `scripts/ai_humanoid_render.py` → `ai_humanoid/{screen.json,index.html}`
- `.github/workflows/ai_humanoid_screen.yml` — registered (id `365605637`), cron `10 8 * * 2-6`
  UTC = **01:10 PT**, deliberately after PT midnight
- 145-ticker universe (104 NDX + AI/humanoid overlay), 144 resolve
- Zero Supabase, zero Telegram — only `sb.PT/finite/destitch/bollinger/stocktwits` are used
- 22 tests; full suite 680 passing

### Measured findings that shaped the design
- **Buying momentum LOSES on the AI complex.** 26 names, 2023+ wave, fwd 60d, n=22,199:
  null **+17.95%/69% pos**; `%B<0.20` +21.42% (**+3.47 pts**); `%B>1.00` **−2.65 pts**;
  top-quartile 12m momentum **−3.01 pts**; downtrend AND `%B<0.20` **+7.49 pts** (best).
  **Owning the complex mattered ~5× more than entry timing.**
- **Meta/Muse replay**: dip rule 08-21 @549.47 → **+35.4%** (18d *before* launch); spike rule
  09-09 @653.17 → +13.9%; analyst upgrade 09-21 → **+0.4%** (98% of the move was already gone).
- **52-week-range rule in Decision Card v1 was WRONG** — corrected in v2. Bottom-20% is not an
  avoid; on NDX-100 (n=210,611) and 20y of ETFs (n=72,155) it was the *best* bucket.
- **Momentum scoring in `thesis_agent.py:775` (+25/+15) has never fired.** `agents/backtester.py:589`
  is the only writer of `event_type="momentum"` and `backtester.yml` is dispatch-only
  ("NEVER on cron"). Proof: 3 days of `snapshots/*.json` = 182 calibrated rule keys, zero momentum.
- **RUNBOOK cron drift: 6 of 10 documented schedules were wrong** — fixed in `4ed0c9a`.

### Also shipped earlier today
- `semis_brief.py`: Bollinger band section + portfolio watch (8 tickers), brief moved to 6:00 AM PT
- `semis_intraday.py`: 06:35 / 07:00 / 08:00 PT checkpoints (VWAP, ATR, opening range)
- Artifacts: cheat sheet https://claude.ai/artifact/PddsHWEVUJBj3K3qUU5aUN + Decision Card v2 PNG

---

## Next Steps

### Operator (stated: "i will work on cron job and github setup")
1. **Create the Cloudflare Pages project** — currently returns HTTP 522, so the deploy step
   will warn and exit 0 without publishing:
   ```bash
   npx wrangler@4 pages project create ai-humanoid-screen --production-branch=main
   ```
2. **Fix `GH_DISPATCH_PAT`** — all 16 cron-job.org pingers dead since 2026-09-09 (401). Then:
   ```bash
   python3 scripts/bootstrap_cronjob_org.py   # needs CRONJOB_API_KEY + GH_DISPATCH_PAT
   ```
   New entries waiting: `semis_intraday` ×3 (06:35/07:00/08:00 PT), `semis_brief` moved to
   06:00 PT, `ai_humanoid_screen` (08:10 UTC Tue-Sat).
3. **Confirm repo secrets exist** for the new workflows: `SUPABASE_URL`, `SUPABASE_SERVICE_KEY`
   (prune), `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID` (screen publish).

### Tomorrow's analysis work
4. **Verify the first scheduled `ai_humanoid_screen` run** (01:10 PT / 08:10 UTC Thu).
   This is the real test — see blocker #1. Check: `n_stale` fraction, `n_resolved >= 100`,
   whether StockTwits at ~22 tickers stays inside the 25-min job budget.
5. **Re-run the screen after the market closes** and see whether any AI/humanoid name enters
   the buy zone. Right now **zero of the 17 buy-zone names are AI or humanoid** — the whole
   theme is neutral-to-extended while utilities/staples are on sale. That is the screen
   working correctly, and it is the single most useful thing it has said so far.
6. **Decide the momentum question** (deferred from today): either power the dead
   `+25/+15` branches with a real producer agent, or delete them and stop the docs
   describing a capability that does not exist.
7. **Decide whether `archive_agent` should delete on schedule** (see Blockers #2).

---

## Blockers/Issues

1. **The screen's first scheduled run may fail its own verify gate.** The verify step errors
   when `n_stale / n_resolved > 0.50`. Today's local run was **68% stale (98 of 144)** because
   yfinance omits whole trading days per-ticker. The 01:10 PT schedule *should* fix this (today
   becomes 09-24, so the complete 09-23 bars are included), but **this is untested**. If it
   fails, the fix is to widen the threshold, not to hide the staleness — every row already
   carries its own date badge.
2. **`archive_agent` never deletes on schedule** (DRY_RUN defaults true). Not fixed
   deliberately. Flipping it enables first-ever live deletion of six pipeline tables.
   Needs a dry-run read of row counts before any decision.
3. ~~`bootstrap_cronjob_org.py` had the OLD `ai_humanoid_screen` time (22:40 UTC).~~
   **FIXED** — now `08:10 UTC, wdays 2-6`, asserted equal to the workflow cron `10 8 * * 2-6`.
   Safe to run the bootstrap.
4. **No tests for `archive_agent` at all** — two defects shipped in today's change were caught
   only by review, not by the suite. `fetch()` in `ai_humanoid_screen.py` is also untested.
5. **`usage_tool_calls` (37 MB) belongs to another project** sharing the same Supabase DB —
   stock_app can never use the full 0.5 GB.
6. Quota after prune is ~87% of limit with ~67 MB headroom ≈ **37 days** at the 1.8 MB/day
   refill. The weekly prune caps the level; `--days 45` would take it to ~76% if needed.

---

## Key Files

**ai_humanoid pipeline**
- `scripts/ai_humanoid_screen.py` — universe, metrics, `classify()`, `fetch()`
- `scripts/ai_humanoid_render.py` — HTML only; kept separate so rendering can't alter numbers
- `.github/workflows/ai_humanoid_screen.yml` — cron `10 8 * * 2-6` UTC
- `ai_humanoid/screen.json` + `index.html` — committed output
- `tests/test_ai_humanoid_screen.py` — 22 tests

**Supabase quota**
- `scripts/prune_supabase_logs.py` — the working pruner
- `.github/workflows/prune_supabase_logs.yml` — Sun 05:00 UTC, applies on schedule
- `agents/archive_agent.py` — TABLES now 9 entries; `sb_delete_ids`, `sb_delete_stamped`
- `.github/workflows/archive_agent.yml:53` — **the DRY_RUN line that has never let it delete**

**Pinger config**
- `scripts/bootstrap_cronjob_org.py` — 14 workflow entries; `ai_humanoid_screen` aligned to
  08:10 UTC Tue-Sat. Run by hand with `CRONJOB_API_KEY` + `GH_DISPATCH_PAT` set.

**Semis**
- `scripts/semis_brief.py`, `scripts/semis_intraday.py`, `docs/RUNBOOK.md` (crons corrected)

---

## Resume Prompt
Copy this to continue:
```
Continue working on this project. Read ./RESUME.md for context on what was done and what needs to happen next.
```
