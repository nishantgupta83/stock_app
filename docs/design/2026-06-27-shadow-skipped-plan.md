# Shadow-Skipped Forward-Return Audit — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use `- [ ]` checkboxes.

**Goal:** A standalone audit that forward-grades the trade setups the pipeline SKIPS — per-setup, capacity-free, stratified by skip-category — to find an over-filtering gate, and to flag instrument-gate anomalies (CVX/Chevron).

**Architecture:** New standalone `scripts/shadow_skipped.py` + pure `agents/_shadow_skipped.py` + own SQLite store `agents/_shadow_store.py`. Reuses `price_agent.compute_paper_outcome` (stop_only grader) read-only. `scripts/paper_book.py` and the shared `_paper_book*.py` are NEVER imported or modified.

**Tech Stack:** Python 3.12, SQLite (stdlib), yfinance, pytest, GitHub Actions.

## Global Constraints

- DO NOT modify or import `scripts/paper_book.py` or `agents/_paper_book*.py`. No shared-schema change.
- No Supabase **writes**; sync is a read-only GET (`reason_to_skip=not.is.null`).
- Per-setup, **capacity-free** (no slot cap — categories never compete). Measure % returns vs matched QQQ window, NOT a $ portfolio.
- **Quarantine only UNPRICEABLE** tickers; measure everything priceable, including instrument-flagged.
- Per-setup outcomes are **frozen** once resolved (mutable yfinance bars can't rewrite them).
- All artifacts under `paper_book/shadow/` only. The tradeable book (`paper_book/*`) stays byte-identical.
- Per-PR: stage ONLY the specific files changed (never `git add -A`); NO `Co-Authored-By`/AI trailer in commit messages.
- Per task: Codex/independent review of the diff before marking complete.

## File Structure

| File | Responsibility |
|---|---|
| `agents/_shadow_skipped.py` (create) | Pure: `categorize_skip`, `aggregate`, `by_category`, `anomaly_audit`, `reason_distribution` |
| `agents/_shadow_store.py` (create) | SQLite: `shadow_setups`, `shadow_outcomes` (frozen), cursor; export/import JSON |
| `scripts/shadow_skipped.py` (create) | Orchestrator: own `_sb`+`bars_for`, `sync`, `grade`, `report`, `main` |
| `.github/workflows/paper_book_shadow.yml` (create) | Daily standalone run + commit-back of `paper_book/shadow/` |
| `.gitignore` (modify) | Un-ignore `paper_book/shadow/{state.json,report.json}` |
| `tests/test_shadow_skipped.py` (create) | Pure-function tests |
| `tests/test_shadow_store.py` (create) | Store round-trip + freeze |

---

### Task 1: Pure audit functions (`agents/_shadow_skipped.py`)

**Files:** Create `agents/_shadow_skipped.py`; Test `tests/test_shadow_skipped.py`.

**Interfaces — Produces:** `categorize_skip(reason)->str`; `aggregate(rows)->dict`; `by_category(rows)->dict`; `anomaly_audit(rows)->list`; `reason_distribution(rows)->dict`. `rows` are per-setup dicts: `{ticker, reason_to_skip, skip_category, priceable(bool), status('resolved'|'unpriceable'), return_pct, excess_pct}`.

- [ ] **Step 1: Write failing tests**

```python
# tests/test_shadow_skipped.py
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "agents"))
import _shadow_skipped as s

def test_categorize_skip():
    assert s.categorize_skip("rule 8k_material_event::h1d profit_factor 0.76 < 1.0 (no payoff edge)") == "payoff"
    assert s.categorize_skip("intelligence flagged AVOID_CHASE") == "vocabulary"
    assert s.categorize_skip("CVX not a tradeable instrument (fund/placeholder)") == "instrument"
    assert s.categorize_skip("some new reason") == "other"
    assert s.categorize_skip(None) == "other"

def _row(cat, ret, exc, priceable=True, status="resolved", ticker="X", reason="r"):
    return {"ticker": ticker, "reason_to_skip": reason, "skip_category": cat,
            "priceable": priceable, "status": status, "return_pct": ret, "excess_pct": exc}

def test_aggregate_and_win_rate():
    rows = [_row("payoff", 0.05, 0.02), _row("payoff", -0.03, -0.04)]
    a = s.aggregate(rows)
    assert a["n_resolved"] == 2 and a["win_rate"] == 0.5
    assert a["mean_excess_vs_qqq_pct"] == round((0.02 - 0.04)/2, 4)

def test_aggregate_insufficient():
    assert s.aggregate([_row("payoff", 0, 0, status="unpriceable")])["status"] == "insufficient"

def test_by_category_and_anomaly():
    rows = [_row("payoff", 0.05, 0.02),
            _row("instrument", 0.10, 0.06, ticker="CVX", reason="CVX not a tradeable instrument")]
    bc = s.by_category(rows)
    assert bc["payoff"]["n_resolved"] == 1 and bc["instrument"]["n_resolved"] == 1
    anomalies = s.anomaly_audit(rows)
    assert len(anomalies) == 1 and anomalies[0]["ticker"] == "CVX"
```

- [ ] **Step 2: Run — expect FAIL** (`ModuleNotFoundError: _shadow_skipped`).
Run: `python -m pytest tests/test_shadow_skipped.py -v`

- [ ] **Step 3: Implement**

```python
# agents/_shadow_skipped.py
"""Pure functions for the shadow-skipped forward-return audit. No DB, no network.
For each priceable skipped setup: forward stop_only return vs matched QQQ window,
stratified by WHY it was skipped. Per-setup + capacity-free (categories never compete)."""
from __future__ import annotations
import statistics as st

CATEGORIES = ("payoff", "vocabulary", "instrument", "other")

def categorize_skip(reason):
    r = (reason or "").lower()
    if "profit_factor" in r or "no payoff edge" in r:
        return "payoff"
    if "avoid_chase" in r or "chase_risk" in r or "intelligence flagged" in r:
        return "vocabulary"
    if "not a tradeable instrument" in r or "fund" in r or "placeholder" in r:
        return "instrument"
    return "other"

def aggregate(rows):
    resolved = [x for x in rows if x.get("status") == "resolved"]
    if not resolved:
        return {"n_setups": len(rows), "n_resolved": 0, "status": "insufficient"}
    rets = [float(x["return_pct"]) for x in resolved]
    exc = [float(x["excess_pct"]) for x in resolved]
    wins = sum(1 for e in exc if e > 0)
    return {"n_setups": len(rows), "n_resolved": len(resolved),
            "mean_return_pct": round(st.mean(rets), 4),
            "mean_excess_vs_qqq_pct": round(st.mean(exc), 4),
            "win_rate": round(wins / len(resolved), 4),
            "median_excess_pct": round(st.median(exc), 4), "status": "ok"}

def by_category(rows):
    out = {c: aggregate([x for x in rows if x.get("skip_category") == c]) for c in CATEGORIES}
    out["overall_priceable"] = aggregate([x for x in rows if x.get("priceable")])
    return out

def anomaly_audit(rows):
    return [{"ticker": x.get("ticker"), "reason_to_skip": x.get("reason_to_skip"),
             "return_pct": x.get("return_pct"), "excess_pct": x.get("excess_pct")}
            for x in rows if x.get("skip_category") == "instrument" and x.get("priceable")]

def reason_distribution(rows):
    d = {}
    for x in rows:
        k = x.get("reason_to_skip") or ""
        d[k] = d.get(k, 0) + 1
    return d
```

- [ ] **Step 4: Run — expect PASS.** `python -m pytest tests/test_shadow_skipped.py -v`
- [ ] **Step 5: Stage ONLY these two files, commit (no AI trailer):**
```bash
git add agents/_shadow_skipped.py tests/test_shadow_skipped.py
git commit -m "feat(shadow): pure skip-category audit functions"
```

---

### Task 2: Shadow SQLite store (`agents/_shadow_store.py`)

**Files:** Create `agents/_shadow_store.py`; Test `tests/test_shadow_store.py`.

**Interfaces — Produces:** `connect(path)`; `init(conn)`; `get_cursor(conn)`/`set_cursor(conn,iso)`; `ingest_setup(conn,*,setup_id,...,reason_to_skip,skip_category,raw)->bool`; `all_setups(conn)->list`; `freeze_outcome(conn,*,setup_id,ticker,skip_category,reason_to_skip,priceable,status,entry_date,entry_px,exit_date,exit_px,return_pct,qqq_return_pct,excess_pct)->bool` (idempotent on setup_id); `all_outcomes(conn)->list`; `resolved_setup_ids(conn)->set`; `export_state(conn)->dict`; `import_state(conn,dict)`.

- [ ] **Step 1: Write failing test**

```python
# tests/test_shadow_store.py
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "agents"))
import _shadow_store as store

def test_ingest_freeze_roundtrip(tmp_path):
    c = store.connect(tmp_path / "s.db"); store.init(c)
    assert store.ingest_setup(c, setup_id=1, ticker="CVX", direction="long",
        created_at="2026-06-22T00:00:00+00:00", target_pct=0.1, stop_pct=-0.03,
        horizon_days=30, reason_to_skip="CVX not a tradeable instrument", skip_category="instrument", raw="{}")
    assert store.ingest_setup(c, setup_id=1, ticker="CVX", direction="long", created_at="x",
        target_pct=None, stop_pct=None, horizon_days=None, reason_to_skip="r", skip_category="instrument", raw="{}") is False
    store.freeze_outcome(c, setup_id=1, ticker="CVX", skip_category="instrument",
        reason_to_skip="CVX not a tradeable instrument", priceable=True, status="resolved",
        entry_date="2026-06-23", entry_px=150.0, exit_date="2026-07-23", exit_px=165.0,
        return_pct=0.10, qqq_return_pct=0.04, excess_pct=0.06)
    assert store.resolved_setup_ids(c) == {1}
    snap = store.export_state(c)
    d = store.connect(tmp_path / "d.db"); store.init(d); store.import_state(d, snap)
    outs = store.all_outcomes(d)
    assert outs[0]["excess_pct"] == 0.06 and store.resolved_setup_ids(d) == {1}
```

- [ ] **Step 2: Run — expect FAIL.** `python -m pytest tests/test_shadow_store.py -v`

- [ ] **Step 3: Implement** `agents/_shadow_store.py` with two tables and the listed functions:
  - `shadow_setups(setup_id PK, signal_id, ticker, direction, created_at, target_pct, stop_pct, horizon_days, valid_until, reason_to_skip, skip_category, raw)`.
  - `shadow_outcomes(setup_id PK, ticker, skip_category, reason_to_skip, priceable INT, status, entry_date, entry_px, exit_date, exit_px, return_pct, qqq_return_pct, excess_pct)`.
  - `shadow_state(id INTEGER PK CHECK(id=1), cursor TEXT)`.
  - `ingest_setup`/`freeze_outcome` use `INSERT OR IGNORE` (idempotent). `export_state` returns `{cursor, setups:[...], outcomes:[...]}`; `import_state` restores all three. `row_factory = sqlite3.Row`; return `dict(r)`.

- [ ] **Step 4: Run — expect PASS.** `python -m pytest tests/test_shadow_store.py -v`
- [ ] **Step 5: Stage only the two files, commit `feat(shadow): isolated SQLite store + frozen outcomes`.**

---

### Task 3: Orchestrator (`scripts/shadow_skipped.py`)

**Files:** Create `scripts/shadow_skipped.py`; extend `tests/test_shadow_store.py` or new `tests/test_shadow_skipped_run.py`.

**Interfaces — Consumes:** Task 1 (`_shadow_skipped`), Task 2 (`_shadow_store`), `price_agent.compute_paper_outcome`. **Produces:** `paper_book/shadow/report.json` + `paper_book/shadow/state.json`.

- [ ] **Step 1:** Write a test for the report-building seam (`build_report(conn, sync_ok)`) using a store seeded with frozen outcomes (no network): assert it returns `{by_category, anomalies, reason_distribution, sync_ok}` and writes `report.json`. (Grading/sync are exercised manually + in CI; keep the unit test on the pure-ish report builder.)

- [ ] **Step 2:** Run — expect FAIL (no `build_report`).

- [ ] **Step 3:** Implement `scripts/shadow_skipped.py`:
  - Constants: `ROOT`, `sys.path.insert(agents)`, `DB=paper_book/shadow/shadow.db` (env `SHADOW_DB`), `STATE_JSON=paper_book/shadow/state.json` (env `SHADOW_STATE_JSON`), `REPORT=paper_book/shadow/report.json`, `BENCH="QQQ"`, `COLD_START_HOURS=720` (cold start 30d back so the first run captures recent skipped setups).
  - **Own** `_sb(path)` (paginated Supabase GET, same shape as paper_book's but local) and `bars_for(ticker,start,end)` (yfinance, cached, returns `{}` on failure — this is the priceable test).
  - `sync(conn)`: pull `stock_trade_setups?reason_to_skip=not.is.null&created_at=gt.<cursor>&order=created_at.asc&select=id,signal_id,ticker,direction,created_at,target_pct,stop_pct,horizon_days,valid_until,reason_to_skip`; `skip_category = categorize_skip(r["reason_to_skip"])`; `store.ingest_setup(...)`; advance cursor to max created_at.
  - `grade(conn)`: for each setup NOT in `resolved_setup_ids`: `bars = bars_for(ticker, created, today)`. If `not bars` → `freeze_outcome(..., priceable=False, status="unpriceable", entry/exit/returns=None)`. Else: entry = next session open after `created_at+1d`; `trade={entry_at, entry_price, direction, horizon_days, target_pct, stop_pct}`; `o = compute_paper_outcome(trade, bars, exit_policy="stop_only")`; if `o` is None → leave unresolved (too fresh; revisit next run); else compute matched QQQ return over `[entry_date, exit_date]` from `bars_for("QQQ",…)`, `excess = o_return - qqq_return`, `freeze_outcome(..., priceable=True, status="resolved", …)`.
  - `build_report(conn, sync_ok)`: `rows = store.all_outcomes(conn)`; `report = {captured_at, sync_ok, by_category: by_category(rows), anomalies: anomaly_audit(rows), reason_distribution: reason_distribution(rows)}`; write `REPORT`.
  - `state` round-trip: `load_state(conn)` (import committed `state.json` if present), `dump_state(conn)` (export → `state.json`).
  - `main()`: `connect`+`init` → `load_state` → try `sync` (set `sync_ok=False` on failure, non-fatal in CI) → `grade` → `build_report(sync_ok)` → `dump_state`.
  - Reuse `compute_paper_outcome` exactly as `paper_book.replay` does (deferred import inside `grade`).

- [ ] **Step 4:** Run the suite green. `python -m pytest tests/test_shadow_skipped.py tests/test_shadow_store.py tests/test_shadow_skipped_run.py -v`
- [ ] **Step 5:** Stage only `scripts/shadow_skipped.py` + the test, commit `feat(shadow): orchestrator — sync skipped setups, grade per-setup, report`.

---

### Task 4: Workflow + gitignore + isolation proof

**Files:** Create `.github/workflows/paper_book_shadow.yml`; modify `.gitignore`; add a legacy byte-identity assertion.

- [ ] **Step 1: `.gitignore`** — append after the existing `paper_book/` block, then VERIFY:
```
!paper_book/shadow/
paper_book/shadow/*
!paper_book/shadow/state.json
!paper_book/shadow/report.json
```
Run: `git check-ignore -v paper_book/shadow/report.json` → must show the `!` negation (NOT ignored); `git check-ignore -v paper_book/shadow/shadow.db` → must still be ignored.

- [ ] **Step 2: Workflow** `.github/workflows/paper_book_shadow.yml` — mirror `learning_snapshot.yml`/`paper_book.yml` shape: `cron: "45 22 * * 1-5"` + `workflow_dispatch`; `concurrency: {group: paper_book_shadow, cancel-in-progress: false}`; `permissions: contents: write`; checkout (GITHUB_TOKEN); `ops_recorder --phase start/finish --agent workflow_shadow_skipped`; setup-python 3.12 cache pip; `pip install -r requirements.txt`; run `python scripts/shadow_skipped.py` with `SUPABASE_URL`/`SUPABASE_SERVICE_KEY` secrets; empty-diff-guarded commit of `paper_book/shadow/` with `git pull --rebase` before push, message `chore(shadow): forward-skip audit YYYY-MM-DD`. Do NOT touch `paper_book.yml`.

- [ ] **Step 3: YAML valid** — `python -c "import yaml; yaml.safe_load(open('.github/workflows/paper_book_shadow.yml')); print('ok')"`.

- [ ] **Step 4: Isolation proof** — `git status --porcelain paper_book/` shows nothing under `paper_book/` (non-shadow) changed by this work; the tradeable `paper_book/metrics.json` is untouched.

- [ ] **Step 5:** Stage only `.github/workflows/paper_book_shadow.yml` + `.gitignore`, commit `feat(shadow): daily standalone workflow + gitignore`.

---

## Self-Review

**Spec coverage:** standalone script → Task 3; pure audit → Task 1; isolated store + freeze → Task 2; per-category + anomaly → Task 1; capacity-free per-setup → Task 3 grade(); quarantine-only-unpriceable → Task 3 grade()/Task 1; workflow + gitignore + isolation → Task 4. No shared-schema change (own store) → Task 2. Every task has tests.

**Placeholder scan:** Task 2/3 give signatures + exact behavior rather than every line — acceptable for store boilerplate and a network orchestrator, but the implementer must follow the named interfaces verbatim. No TODO/TBD.

**Type consistency:** `rows` shape (Task 1) matches `all_outcomes` columns (Task 2) matches what `grade()` freezes (Task 3): `{ticker, reason_to_skip, skip_category, priceable, status, return_pct, excess_pct}`. `categorize_skip` used in both Task 1 (pure) and Task 3 (sync).
