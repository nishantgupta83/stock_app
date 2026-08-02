# Schema reconciliation baseline — 2026-08-02

**Why:** Precondition for the prove-or-kill plan's Step 2 (candidate ledger). The plan said
"compare `sql/` vs the `supabase/migrations/` CLI ledger + live schema before creating any
migration." This is that comparison. It changed the plan (see Conclusions).

## Two parallel migration tracks (they are NOT in sync)

| Track | Naming | Count | Last entry | Role |
|---|---|---|---|---|
| `sql/` | sequential `00NN_*.sql` | **42** (0001–0042) | `0042_event_obs_nulls_not_distinct` | The **de-facto working set**; applied to live DB **out-of-band** (Supabase Management API / psql), per CLAUDE.md "Apply SQL migration". |
| `supabase/migrations/` | timestamped | **9** | `20260522040859_brier_calibration` (2026-05-22) | The Supabase **CLI** track. **Stale / effectively abandoned** — 2.5 months behind. |

The two tracks use different names and are **not** 1:1. The CLI track captured only the first
~9 migrations; everything since has gone through `sql/` applied out-of-band.

## Live schema probes (PostgREST, 2026-08-02)

| Marker | Migration | HTTP | Applied live? |
|---|---|---|---|
| `stock_signal_candidates` (full column set) | `sql/0039` | 200 | **YES** — and populated (217 rows, latest 2026-08-01) |
| `stock_rule_calibration.{tier,is_mature_70,is_mature_80}` | `sql/0031` | 200 | YES |
| `stock_rule_calibration.effective_*` | `sql/0041` | 200 | YES |
| `stock_realistic_loop_positions` | `sql/0033` | 200 | YES |
| `stock_health_pulse` | post-ledger (docs: 2026-06-02) | 200 | YES |
| `stock_thesis_rejections` | post-ledger (docs: 2026-06-02) | 200 | YES |
| `stock_user_decisions.signal_id` | (Step-5, not built) | 400 | NO (expected) |
| `stock_candidate_gate_decisions` (+2 name variants) | (Step-2 proposal) | 404 | NO (expected) |

## Conclusions (these change Step 2)

1. **`sql/0039` is already LIVE and populated** — 217 candidate rows, 2.a generation is
   working. Its file header still says "⚠️ DRAFT — NOT YET APPLIED"; that is **stale** (fixed
   in this change). Memory `project_prove_or_kill_plan_2026_08_02` and the older audit notes
   that said "0039 drafted, not applied" are **wrong** and corrected.
2. **The funnel is half-live:** 2.a (candidate ledger) writes rows; `gate_decision` and
   `emitted_signal_id` are **NULL on every row**, so 2.b (`_metalabel_gate`) and the
   orchestrator are **not** live — consistent with "gate drafted, not live."
3. **Never run `supabase db push`** expecting it to sync — the CLI ledger thinks only 9
   migrations exist; a push would mis-apply against a schema that is 30+ migrations ahead.
   `sql/` applied out-of-band is the source of truth.
4. **Step 2 shrinks:** the candidate ledger does **not** need to be applied. What remains:
   - the append-only **gate-decision-history** table (genuinely new — 404 confirmed);
   - the Step-5 `stock_user_decisions` columns (separate step);
   - hardening the `thesis_agent` candidate write to fail-closed (lower urgency — it is
     currently **succeeding**, not silently failing as assumed, but still good hygiene).
   Apply any new SQL the same out-of-band way (new `sql/0043+`), never via the CLI push.

## Follow-ups noted (not done here)
- The `sql/` ↔ `supabase/migrations/` divergence is a standing hazard. Options: (a) formally
  abandon the CLI track and document `sql/` + out-of-band as the process (low effort), or
  (b) re-baseline the CLI track to current schema (higher effort). Deferred — flagged for the
  operator to choose.
