# Paper Book — Auto Forward Loop + Edge Instrumentation (A+F) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the existing local paper book run itself unattended in GitHub Actions with an immutable forward ledger, and grade its own edge against a $5k QQQ buy-and-hold via a staggered success bar.

**Architecture:** Reuse the tested local engine (`_paper_book.py`, `_paper_book_store.py`, `price_agent.compute_paper_outcome`). Add (1) a frozen-ledger JSON state model so closed fills are immutable across runs, (2) a pure metrics module that builds book-vs-QQQ equity curves + tier classification, (3) CI wiring in `paper_book.py`, (4) a daily GitHub Actions workflow mirroring `learning_snapshot.yml`.

**Tech Stack:** Python 3.12, SQLite (stdlib), yfinance (bars), pytest, GitHub Actions.

## Global Constraints

- No changes to `thesis_agent` / `trade_setup_agent` / any signal logic.
- No Supabase **writes**; `sync` is read-only.
- No execution, no real money, no BUY/SELL graduation automation.
- Gate metric = full $5k book equity (incl. idle cash at risk-free) vs $5k QQQ buy-and-hold from `forward_epoch`, cumulative + unannualized. OLS alpha/beta and same-slot QQQ are **diagnostics only**.
- Independence counts **distinct entry dates** (cohorts), never raw trades.
- Closed fills are **frozen** once recorded — never re-derived from (mutable) yfinance bars.
- Tier ① output is `continue | inconclusive | fail` (small-n honesty); `fail` only on clear negative excess or >20% drawdown.
- Tier classification is **withheld** (`inconclusive: sync_failed`) when `sync_ok=false`.
- Local run stays **non-regressing**: the SQLite workflow and the `sync`/`replay`/`state`/`dash` outputs are unchanged, and the JSON state round-trip (`book_state.json` hydrate/dump) is **CI-only** (gated on `PAPER_BOOK_STATE_JSON`). `run` mode additionally writes `metrics.json` both locally and in CI (additive — the local dashboard needs it for the tier).
- Per-PR: send each task's diff to Codex (read-only, neutral prompt) before commit, per repo CLAUDE.md.

## File Structure

| File | Responsibility |
|---|---|
| `agents/_paper_book_store.py` (modify) | + `forward_epoch` column, `closed_setup_ids`, `set_forward_epoch`, `export_state`, `import_state` (frozen ledger) |
| `agents/_paper_book_metrics.py` (create) | Pure metrics: equity curves, cumulative excess, drawdown, cohort independence, tier classification, diagnostics |
| `scripts/paper_book.py` (modify) | CI path: JSON hydrate/export, `forward_epoch` init, freeze-skip in replay, `sync_ok` non-fatal, write `metrics.json` |
| `scripts/paper_book_dashboard.py` (modify) | Render tier status + book-vs-QQQ + diagnostics |
| `.github/workflows/paper_book.yml` (create) | Daily unattended run + rebase + commit-back |
| `tests/test_paper_book_state_json.py` (create) | Round-trip identity + frozen-fill immutability |
| `tests/test_paper_book_metrics.py` (create) | Equity/excess/cohort/tier math |

---

### Task 1: Frozen-ledger state model in the store

**Files:**
- Modify: `agents/_paper_book_store.py`
- Test: `tests/test_paper_book_state_json.py`

**Interfaces:**
- Produces: `closed_setup_ids(conn) -> set[int]`; `set_forward_epoch(conn, loop_name, epoch: str)`; `export_state(conn, loop_name) -> dict`; `import_state(conn, state: dict) -> None`. `export_state` dict shape: `{"book_state": {...}, "book_setups": [...], "book_positions_closed": [...]}`.
- Consumes: existing `config`, `all_setups`, `connect`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_paper_book_state_json.py
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "agents"))
import _paper_book_store as store


def _seed(conn):
    store.init_state(conn, loop_name="t", capital_base=5000.0, max_concurrent=5, per_size=1000.0)
    store.ingest_setup(conn, setup_id=1, signal_id=10, ticker="AAA", direction="long",
                       created_at="2026-06-20T13:00:00+00:00", target_pct=0.1, stop_pct=-0.03,
                       horizon_days=30, valid_until=None, raw={"x": 1})
    store.open_position(conn, setup_id=1, signal_id=10, ticker="AAA", direction="long",
                        opened_at="2026-06-21T00:00:00+00:00", open_price=100.0, notional=1000.0,
                        target_pct=0.1, stop_pct=-0.03, horizon_days=30)
    pid = next(p["id"] for p in store.all_positions(conn) if p["setup_id"] == 1)
    store.close_position(conn, pid, closed_at="2026-06-25T00:00:00+00:00", close_price=110.0,
                         close_reason="horizon", realized_pct=0.0995, realized_pnl=99.5)
    store.set_forward_epoch(conn, "t", "2026-06-19")


def test_export_import_roundtrip_and_freeze(tmp_path):
    a = store.connect(tmp_path / "a.db"); _seed(a)
    snap = store.export_state(a, "t")
    assert snap["book_state"]["forward_epoch"] == "2026-06-19"
    assert len(snap["book_setups"]) == 1
    assert len(snap["book_positions_closed"]) == 1
    assert store.closed_setup_ids(a) == {1}

    b = store.connect(tmp_path / "b.db")
    store.import_state(b, snap)
    assert store.config(b, "t")["forward_epoch"] == "2026-06-19"
    assert store.all_setups(b)[0]["ticker"] == "AAA"
    closed = [p for p in store.all_positions(b) if p["status"] == "closed"]
    assert closed[0]["realized_pnl"] == 99.5
    assert store.closed_setup_ids(b) == {1}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_paper_book_state_json.py -v`
Expected: FAIL — `AttributeError: module '_paper_book_store' has no attribute 'set_forward_epoch'`.

- [ ] **Step 3: Add the schema migration + functions**

In `agents/_paper_book_store.py`, add a column-ensure call inside `connect()` right after `conn.executescript(_SCHEMA)`:

```python
    conn.executescript(_SCHEMA)
    _ensure_columns(conn)
    return conn


def _ensure_columns(conn) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(book_state)")}
    if "forward_epoch" not in cols:
        conn.execute("ALTER TABLE book_state ADD COLUMN forward_epoch TEXT")
        conn.commit()
```

Append these functions to the file:

```python
def closed_setup_ids(conn) -> set[int]:
    return {r["setup_id"] for r in conn.execute(
        "SELECT setup_id FROM book_positions WHERE status='closed' AND setup_id IS NOT NULL")}


def set_forward_epoch(conn, loop_name, epoch) -> None:
    conn.execute("UPDATE book_state SET forward_epoch=? WHERE loop_name=?", (epoch, loop_name))
    conn.commit()


def export_state(conn, loop_name) -> dict:
    closed = [dict(r) for r in conn.execute(
        "SELECT * FROM book_positions WHERE status='closed' ORDER BY id")]
    return {"book_state": config(conn, loop_name),
            "book_setups": all_setups(conn),
            "book_positions_closed": closed}


_POS_COLS = ("setup_id", "signal_id", "ticker", "direction", "opened_at", "open_price",
             "notional", "target_price", "stop_price", "target_pct", "stop_pct",
             "horizon_days", "exit_target_date", "valid_until", "status", "closed_at",
             "close_price", "close_reason", "realized_pct", "realized_pnl", "mfe_pct", "mae_pct")


def import_state(conn, state: dict) -> None:
    bs = state.get("book_state") or {}
    if bs:
        conn.execute(
            "INSERT OR REPLACE INTO book_state (loop_name, capital_base, max_concurrent, "
            "per_position_size, last_open_scan_at, last_mark_at, setup_cursor, forward_epoch) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (bs.get("loop_name"), bs.get("capital_base"), bs.get("max_concurrent"),
             bs.get("per_position_size"), bs.get("last_open_scan_at"), bs.get("last_mark_at"),
             bs.get("setup_cursor"), bs.get("forward_epoch")))
    for s in state.get("book_setups", []):
        store_raw = s.get("raw")
        conn.execute(
            "INSERT OR IGNORE INTO book_setups (setup_id, signal_id, ticker, direction, "
            "created_at, target_pct, stop_pct, horizon_days, valid_until, raw) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (s["setup_id"], s.get("signal_id"), s.get("ticker"), s.get("direction"),
             s.get("created_at"), s.get("target_pct"), s.get("stop_pct"),
             s.get("horizon_days"), s.get("valid_until"), store_raw))
    for p in state.get("book_positions_closed", []):
        conn.execute(
            f"INSERT OR IGNORE INTO book_positions ({','.join(_POS_COLS)}) "
            f"VALUES ({','.join('?' * len(_POS_COLS))})",
            tuple(p.get(c) for c in _POS_COLS))
    conn.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_paper_book_state_json.py -v`
Expected: PASS.

- [ ] **Step 5: Codex-review the diff, then commit**

```bash
git diff agents/_paper_book_store.py tests/test_paper_book_state_json.py > /tmp/t1.diff
# neutral Codex review per CLAUDE.md, then:
git add agents/_paper_book_store.py tests/test_paper_book_state_json.py
git commit -m "feat(paper-book): frozen-ledger JSON state (export/import + closed-setup skip)"
```

---

### Task 2: Metrics core — equity curves, excess, drawdown, cohorts

**Files:**
- Create: `agents/_paper_book_metrics.py`
- Test: `tests/test_paper_book_metrics.py`

**Interfaces:**
- Produces: `TIERS` (dict); `independent_cohorts(positions) -> int`; `book_equity_curve(positions, days, capital, rf_annual) -> dict[date,float]`; `qqq_buy_hold_curve(qqq_daily, days, capital, epoch) -> dict[date,float]`; `max_drawdown(curve) -> float`; `cumulative_excess(book_curve, qqq_curve) -> float`; `top_cohort_excess_share(positions) -> float`; `profit_factor(closed) -> float`.
- Consumes: nothing (pure). `positions` are dicts shaped like `book_positions` rows; `qqq_daily` is `dict[date, close]`; `days` is a sorted list of `date`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_paper_book_metrics.py
import sys, pathlib, datetime as dt
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "agents"))
import _paper_book_metrics as m

D = dt.date.fromisoformat


def test_independent_cohorts_counts_entry_dates():
    pos = [{"opened_at": "2026-06-21T00:00:00+00:00"},
           {"opened_at": "2026-06-21T00:00:00+00:00"},   # same day -> still 1 cohort
           {"opened_at": "2026-06-23T00:00:00+00:00"}]
    assert m.independent_cohorts(pos) == 2


def test_book_equity_and_excess():
    days = [D("2026-06-21"), D("2026-06-22")]
    pos = [{"opened_at": "2026-06-20T00:00:00+00:00", "closed_at": "2026-06-22T00:00:00+00:00",
            "status": "closed", "notional": 1000.0, "realized_pnl": 100.0}]
    book = m.book_equity_curve(pos, days, capital=5000.0, rf_annual=0.0)
    assert book[D("2026-06-21")] == 5000.0     # still open, no pnl booked
    assert book[D("2026-06-22")] == 5100.0     # closed -> +100
    qqq_daily = {D("2026-06-21"): 100.0, D("2026-06-22"): 105.0}
    qqq = m.qqq_buy_hold_curve(qqq_daily, days, capital=5000.0, epoch=D("2026-06-21"))
    assert qqq[D("2026-06-22")] == 5250.0       # +5%
    assert m.cumulative_excess(book, qqq) == round(5100.0 - 5250.0, 2)  # book lost to QQQ


def test_max_drawdown():
    curve = {D("2026-06-21"): 100.0, D("2026-06-22"): 120.0, D("2026-06-23"): 90.0}
    assert m.max_drawdown(curve) == 0.25       # (120-90)/120
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_paper_book_metrics.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named '_paper_book_metrics'`.

- [ ] **Step 3: Create the module**

```python
# agents/_paper_book_metrics.py
"""Pure metrics for the Paper Book forward-edge test. No DB, no network.

Gate = full $5k book equity (incl. idle cash at the risk-free rate) vs $5k QQQ
buy-and-hold from forward_epoch, cumulative + unannualized. OLS alpha/beta and
same-slot QQQ are diagnostics only (unstable at this n/sparsity)."""
from __future__ import annotations
import datetime as dt

TRADING_DAYS = 252

TIERS = {
    "alive": {"min_cohorts": 30, "min_weeks": 8, "max_dd": 0.20},
    "edge":  {"min_cohorts": 50, "min_weeks": 13, "min_pf": 1.4, "min_subperiods_pos": 2},
}


def _d(x) -> dt.date:
    return dt.date.fromisoformat(str(x)[:10])


def independent_cohorts(positions: list[dict]) -> int:
    return len({_d(p["opened_at"]) for p in positions if p.get("opened_at")})


def _open_notional_on(positions, day) -> float:
    tot = 0.0
    for p in positions:
        if not p.get("opened_at"):
            continue
        o = _d(p["opened_at"])
        c = _d(p["closed_at"]) if p.get("closed_at") else None
        if o <= day and (c is None or day < c):
            tot += float(p.get("notional") or 0)
    return tot


def book_equity_curve(positions, days, capital, rf_annual) -> dict:
    rf_daily = rf_annual / TRADING_DAYS
    curve, interest = {}, 0.0
    for day in days:
        idle = max(0.0, capital - _open_notional_on(positions, day))
        interest += idle * rf_daily
        realized = sum(float(p.get("realized_pnl") or 0) for p in positions
                       if p.get("status") == "closed" and p.get("closed_at")
                       and _d(p["closed_at"]) <= day)
        curve[day] = round(capital + realized + interest, 2)
    return curve


def qqq_buy_hold_curve(qqq_daily, days, capital, epoch) -> dict:
    base = qqq_daily.get(epoch) if epoch in qqq_daily else None
    if base is None:
        base = next((qqq_daily[d] for d in days if d in qqq_daily), None)
    if not base:
        return {}
    return {day: round(capital * qqq_daily[day] / base, 2) for day in days if day in qqq_daily}


def max_drawdown(curve: dict) -> float:
    peak = None
    mdd = 0.0
    for day in sorted(curve):
        v = curve[day]
        peak = v if peak is None else max(peak, v)
        if peak > 0:
            mdd = max(mdd, (peak - v) / peak)
    return round(mdd, 4)


def cumulative_excess(book_curve, qqq_curve) -> float:
    days = sorted(set(book_curve) & set(qqq_curve))
    if not days:
        return 0.0
    last = days[-1]
    return round(book_curve[last] - qqq_curve[last], 2)


def profit_factor(closed) -> float:
    wins = sum(float(p.get("realized_pnl") or 0) for p in closed if (p.get("realized_pnl") or 0) > 0)
    losses = -sum(float(p.get("realized_pnl") or 0) for p in closed if (p.get("realized_pnl") or 0) < 0)
    if losses <= 0:
        return float("inf") if wins > 0 else 0.0
    return round(wins / losses, 4)


def top_cohort_excess_share(positions) -> float:
    by_day: dict[dt.date, float] = {}
    for p in positions:
        if p.get("status") == "closed" and p.get("opened_at"):
            k = _d(p["opened_at"])
            by_day[k] = by_day.get(k, 0.0) + float(p.get("realized_pnl") or 0)
    total = sum(by_day.values())
    if not by_day or abs(total) < 1e-9:
        return 0.0
    return round(max(by_day.values(), key=abs) / total, 4)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_paper_book_metrics.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Codex-review the diff, then commit**

```bash
git add agents/_paper_book_metrics.py tests/test_paper_book_metrics.py
git commit -m "feat(paper-book): metrics core — book-vs-QQQ equity curves + cohort independence"
```

---

### Task 3: Tier classification, diagnostics, and `compute_metrics`

**Files:**
- Modify: `agents/_paper_book_metrics.py`
- Test: `tests/test_paper_book_metrics.py`

**Interfaces:**
- Produces: `weeks_span(positions) -> float`; `subperiods_positive(curve) -> int`; `beta_alpha(book_daily, qqq_daily_ret) -> tuple`; `classify_tier(fwd: dict, tiers=TIERS, sync_ok=True) -> dict`; `compute_metrics(positions, qqq_daily, forward_epoch, capital, sync_ok=True, rf_annual=0.05, tiers=TIERS) -> dict` (returns `{"replay": {...}, "forward": {...}, "tier": {...}, "captured_at": ...}`).
- Consumes: Task 2 functions.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_paper_book_metrics.py

def test_classify_tier_withholds_on_sync_failure():
    out = m.classify_tier({"n_independent_cohorts": 99, "weeks": 99, "cumulative_excess": 999,
                           "max_drawdown": 0.0, "top_cohort_excess_share": 0.1}, sync_ok=False)
    assert out["status"] == "inconclusive" and out["reason"] == "sync_failed"


def test_classify_tier_insufficient_then_fail_then_alive():
    base = {"max_drawdown": 0.0, "top_cohort_excess_share": 0.1, "profit_factor": 2.0,
            "subperiods_positive": 2}
    thin = dict(base, n_independent_cohorts=5, weeks=2, cumulative_excess=10.0)
    assert m.classify_tier(thin)["status"] == "inconclusive"
    bad = dict(base, n_independent_cohorts=40, weeks=10, cumulative_excess=-50.0)
    assert m.classify_tier(bad)["status"] == "fail"
    ok = dict(base, n_independent_cohorts=40, weeks=10, cumulative_excess=25.0)
    assert m.classify_tier(ok)["status"] in ("alive", "edge")


def test_compute_metrics_splits_forward_and_replay():
    pos = [
        {"opened_at": "2026-06-10T00:00:00+00:00", "closed_at": "2026-06-12T00:00:00+00:00",
         "status": "closed", "notional": 1000.0, "realized_pnl": 50.0},   # replay (pre-epoch)
        {"opened_at": "2026-06-21T00:00:00+00:00", "closed_at": "2026-06-23T00:00:00+00:00",
         "status": "closed", "notional": 1000.0, "realized_pnl": -20.0},  # forward
    ]
    qqq = {D("2026-06-10"): 100.0, D("2026-06-12"): 101.0, D("2026-06-21"): 102.0,
           D("2026-06-23"): 103.0}
    out = m.compute_metrics(pos, qqq, forward_epoch="2026-06-19", capital=5000.0, sync_ok=True)
    assert out["replay"]["n_raw_trades"] == 1
    assert out["forward"]["n_raw_trades"] == 1
    assert out["tier"]["status"] == "inconclusive"   # only 1 forward cohort
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_paper_book_metrics.py -v`
Expected: FAIL — `AttributeError: module '_paper_book_metrics' has no attribute 'classify_tier'`.

- [ ] **Step 3: Append classification, diagnostics, and the orchestrator**

```python
# append to agents/_paper_book_metrics.py

def weeks_span(positions) -> float:
    ds = [_d(p["opened_at"]) for p in positions if p.get("opened_at")]
    if not ds:
        return 0.0
    return round((max(ds) - min(ds)).days / 7.0, 1)


def subperiods_positive(curve, halves=2) -> int:
    days = sorted(curve)
    if len(days) < halves + 1:
        return 0
    size = len(days) // halves
    pos = 0
    for h in range(halves):
        lo = days[h * size]
        hi = days[(h + 1) * size - 1] if h < halves - 1 else days[-1]
        if curve[hi] - curve[lo] > 0:
            pos += 1
    return pos


def beta_alpha(book_daily, qqq_daily_ret):
    n = len(book_daily)
    if n < 2 or len(qqq_daily_ret) != n:
        return (None, None)
    mb = sum(book_daily) / n
    mq = sum(qqq_daily_ret) / n
    var = sum((q - mq) ** 2 for q in qqq_daily_ret) / n
    if var == 0:
        return (None, None)
    cov = sum((book_daily[i] - mb) * (qqq_daily_ret[i] - mq) for i in range(n)) / n
    beta = cov / var
    return (round(beta, 4), round(mb - beta * mq, 6))


def classify_tier(fwd: dict, tiers=TIERS, sync_ok=True) -> dict:
    if not sync_ok:
        return {"status": "inconclusive", "reason": "sync_failed", "next": "alive"}
    a = tiers["alive"]
    cohorts = fwd.get("n_independent_cohorts", 0)
    weeks = fwd.get("weeks", 0)
    excess = fwd.get("cumulative_excess", 0.0)
    dd = fwd.get("max_drawdown", 0.0)
    top_share = abs(fwd.get("top_cohort_excess_share", 0.0))
    if cohorts < a["min_cohorts"] or weeks < a["min_weeks"]:
        return {"status": "inconclusive", "reason": "insufficient_sample",
                "next": "alive", "have_cohorts": cohorts, "need_cohorts": a["min_cohorts"],
                "have_weeks": weeks, "need_weeks": a["min_weeks"]}
    if excess < 0 or dd > a["max_dd"]:
        return {"status": "fail", "reason": "negative_excess_or_drawdown",
                "excess": excess, "max_drawdown": dd}
    if top_share >= 1.0:
        return {"status": "inconclusive", "reason": "single_cohort_dominates",
                "top_cohort_share": top_share}
    status = "alive"
    e = tiers["edge"]
    if (cohorts >= e["min_cohorts"] and weeks >= e["min_weeks"] and excess > 0
            and fwd.get("profit_factor", 0) > e["min_pf"]
            and fwd.get("subperiods_positive", 0) >= e["min_subperiods_pos"]):
        status = "edge"
    return {"status": status, "excess": excess, "max_drawdown": dd,
            "cohorts": cohorts, "weeks": weeks}


def _block(sub, qqq_daily, days, capital, rf_annual, sync_ok) -> dict:
    closed = [p for p in sub if p.get("status") == "closed"]
    bcurve = book_equity_curve(sub, days, capital, rf_annual)
    qcurve = qqq_buy_hold_curve(qqq_daily, days, capital, days[0] if days else None)
    return {
        "n_raw_trades": len(closed),
        "n_independent_cohorts": independent_cohorts(sub),
        "weeks": weeks_span(sub),
        "book_equity_end": bcurve[max(bcurve)] if bcurve else capital,
        "qqq_buy_hold_end": qcurve[max(qcurve)] if qcurve else capital,
        "cumulative_excess": cumulative_excess(bcurve, qcurve),
        "max_drawdown": max_drawdown(bcurve),
        "top_cohort_excess_share": top_cohort_excess_share(sub),
        "profit_factor": profit_factor(closed),
        "subperiods_positive": subperiods_positive(bcurve),
        "sync_ok": sync_ok,
    }


def compute_metrics(positions, qqq_daily, forward_epoch, capital,
                    sync_ok=True, rf_annual=0.05, tiers=TIERS) -> dict:
    epoch = _d(forward_epoch) if forward_epoch else None

    def is_fwd(p):
        return epoch and p.get("opened_at") and _d(p["opened_at"]) >= epoch

    fwd_days = sorted(d for d in qqq_daily if (not epoch) or d >= epoch)
    rep_days = sorted(d for d in qqq_daily if epoch and d < epoch)
    out = {
        "replay": _block([p for p in positions if not is_fwd(p)], qqq_daily, rep_days,
                         capital, rf_annual, sync_ok),
        "forward": _block([p for p in positions if is_fwd(p)], qqq_daily, fwd_days,
                          capital, rf_annual, sync_ok),
        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
    }
    out["tier"] = classify_tier(out["forward"], tiers, sync_ok)
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_paper_book_metrics.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Codex-review the diff, then commit**

```bash
git add agents/_paper_book_metrics.py tests/test_paper_book_metrics.py
git commit -m "feat(paper-book): tier classification (continue/inconclusive/fail) + diagnostics"
```

---

### Task 4: CI wiring in `scripts/paper_book.py`

**Files:**
- Modify: `scripts/paper_book.py`
- Test: `tests/test_paper_book_state_json.py` (add a CI-path test)

**Interfaces:**
- Consumes: Task 1 (`export_state`/`import_state`/`closed_setup_ids`/`set_forward_epoch`), Task 3 (`compute_metrics`).
- Produces: env-gated CI behavior; `paper_book/book_state.json` + `paper_book/metrics.json` artifacts.

- [ ] **Step 1: Write the failing test** (hydrate→export round-trip via the JSON path, sync stubbed)

```python
# append to tests/test_paper_book_state_json.py
import json, importlib


def test_ci_state_json_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPER_BOOK_DB", str(tmp_path / "book.db"))
    state_json = tmp_path / "book_state.json"
    monkeypatch.setenv("PAPER_BOOK_STATE_JSON", str(state_json))
    import paper_book as pb  # scripts/ is on sys.path via conftest or insert below
    importlib.reload(pb)
    conn = pb.store.connect(tmp_path / "book.db")
    pb.store.init_state(conn, loop_name=pb.LOOP, capital_base=pb.CAPITAL,
                        max_concurrent=pb.MAX_CONC, per_size=pb.PER_SIZE)
    pb.store.set_forward_epoch(conn, pb.LOOP, "2026-06-19")
    pb.dump_state_json(conn)
    assert state_json.exists()
    blob = json.loads(state_json.read_text())
    assert blob["book_state"]["forward_epoch"] == "2026-06-19"
```

Add to the top of `tests/test_paper_book_state_json.py` (so `import paper_book` resolves):

```python
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_paper_book_state_json.py::test_ci_state_json_roundtrip -v`
Expected: FAIL — `AttributeError: module 'paper_book' has no attribute 'dump_state_json'`.

- [ ] **Step 3: Add JSON hydrate/dump + freeze-skip + sync_ok + metrics to `scripts/paper_book.py`**

Add near the top (after the `LOOP`/`DB_PATH` constants):

```python
import json
STATE_JSON = os.environ.get("PAPER_BOOK_STATE_JSON")     # set in CI; unset locally
RF_ANNUAL = float(os.environ.get("PAPER_BOOK_RF_ANNUAL", "0.05"))
BENCH = os.environ.get("PAPER_BOOK_BENCH", "QQQ")


def load_state_json(conn) -> None:
    if STATE_JSON and Path(STATE_JSON).exists():
        store.import_state(conn, json.loads(Path(STATE_JSON).read_text()))


def dump_state_json(conn) -> None:
    if STATE_JSON:
        Path(STATE_JSON).parent.mkdir(parents=True, exist_ok=True)
        Path(STATE_JSON).write_text(json.dumps(store.export_state(conn, LOOP), indent=0, default=str))
```

In `replay()`, skip frozen (already-closed) setups — change the loop head:

```python
    setups = store.all_setups(conn)
    frozen = store.closed_setup_ids(conn)            # <-- add
    today = dt.datetime.now(dt.timezone.utc).date()
    candidates: list[dict] = []
    for s in setups:
        if s["setup_id"] in frozen:                  # <-- add: never re-grade a frozen close
            continue
```

Make `sync` failure non-fatal in `main()` and add a metrics step. Replace `main()` with:

```python
def write_metrics(conn, sync_ok: bool) -> Path | None:
    import _paper_book_metrics as met
    cfg = store.config(conn, LOOP)
    epoch = cfg.get("forward_epoch")
    positions = store.all_positions(conn)
    qqq = bars_for(BENCH, dt.date(2026, 1, 1), dt.datetime.now(dt.timezone.utc).date())
    qqq_daily = {d: bar["close"] for d, bar in qqq.items()}
    metrics = met.compute_metrics(positions, qqq_daily, epoch, CAPITAL,
                                  sync_ok=sync_ok, rf_annual=RF_ANNUAL)
    out = DB_PATH.parent / "metrics.json"
    out.write_text(json.dumps(metrics, indent=2, default=str))
    print(f"[metrics] tier={metrics['tier']['status']} -> {out}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("mode", nargs="?", default="run",
                    choices=["sync", "replay", "state", "dash", "run"])
    args = ap.parse_args()
    conn = store.connect(DB_PATH)
    load_state_json(conn)                                       # <-- hydrate from committed JSON
    store.init_state(conn, loop_name=LOOP, capital_base=CAPITAL,
                     max_concurrent=MAX_CONC, per_size=PER_SIZE)
    if STATE_JSON and not store.config(conn, LOOP).get("forward_epoch"):
        store.set_forward_epoch(conn, LOOP,
                                dt.datetime.now(dt.timezone.utc).date().isoformat())
    sync_ok = True
    if args.mode in ("sync", "run"):
        try:
            sync(conn)
        except Exception as e:                                  # noqa: BLE001
            sync_ok = False
            print(f"[sync] FAILED (non-fatal in CI): {e}", file=sys.stderr)
            if not STATE_JSON:                                  # local: fail loudly
                raise
    if args.mode in ("replay", "run"):
        replay(conn)
    if args.mode in ("state", "run"):
        print_state(conn)
    if args.mode in ("run",):
        write_metrics(conn, sync_ok)
    if args.mode in ("dash", "run"):
        dashboard(conn)
    dump_state_json(conn)                                        # <-- persist frozen ledger
    return 0
```

- [ ] **Step 4: Run the test + full suite**

Run: `python -m pytest tests/test_paper_book_state_json.py tests/test_paper_book.py tests/test_paper_book_metrics.py -v`
Expected: PASS (existing paper-book tests stay green; new CI round-trip passes).

- [ ] **Step 5: Manually verify local behavior is unchanged**

Run: `python scripts/paper_book.py state`
Expected: prints `[state] cash=... open=.../5 pnl=...` exactly as before (no JSON written — `PAPER_BOOK_STATE_JSON` unset).

- [ ] **Step 6: Codex-review the diff, then commit**

```bash
git add scripts/paper_book.py tests/test_paper_book_state_json.py
git commit -m "feat(paper-book): CI state hydrate/dump + freeze-skip + non-fatal sync + metrics.json"
```

---

### Task 5: Dashboard shows tier + book-vs-QQQ

**Files:**
- Modify: `scripts/paper_book_dashboard.py`
- Test: `tests/test_paper_book_dashboard.py` (create)

**Interfaces:**
- Consumes: `paper_book/metrics.json` (Task 4). Produces: HTML containing tier status + cumulative excess.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_paper_book_dashboard.py
import sys, pathlib, json
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
import paper_book_dashboard as dash


def test_render_metrics_block_shows_tier_and_excess():
    metrics = {"forward": {"cumulative_excess": -150.0, "book_equity_end": 4850.0,
                           "qqq_buy_hold_end": 5000.0, "n_independent_cohorts": 12, "weeks": 4.0},
               "tier": {"status": "inconclusive", "reason": "insufficient_sample"}}
    html = dash.render_metrics_block(metrics)
    assert "inconclusive" in html
    assert "-150" in html or "−150" in html
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_paper_book_dashboard.py -v`
Expected: FAIL — `AttributeError: module 'paper_book_dashboard' has no attribute 'render_metrics_block'`.

- [ ] **Step 3: Add `render_metrics_block` and call it from `render`**

```python
def render_metrics_block(metrics: dict) -> str:
    f = metrics.get("forward", {})
    t = metrics.get("tier", {})
    status = t.get("status", "n/a")
    colors = {"fail": "#e07a5f", "inconclusive": "#e9c46a", "alive": "#81b29a",
              "edge": "#2a9d8f", "conviction": "#264653"}  # coral/amber/sage/teal (no purple)
    chip = colors.get(status, "#8d99ae")
    return (
        f'<div class="tier" style="border-left:6px solid {chip};padding:10px;margin:12px 0">'
        f'<b>Forward tier:</b> {status} '
        f'<span style="color:#666">({t.get("reason","")})</span><br>'
        f'Book ${f.get("book_equity_end","?")} vs QQQ ${f.get("qqq_buy_hold_end","?")} '
        f'&nbsp;|&nbsp; excess ${f.get("cumulative_excess","?")} '
        f'&nbsp;|&nbsp; cohorts {f.get("n_independent_cohorts","?")} '
        f'&nbsp;|&nbsp; {f.get("weeks","?")}w</div>'
    )
```

In `render(...)`, read `metrics.json` if present and inject the block near the top of the body:

```python
    import json, pathlib
    mpath = pathlib.Path(__file__).resolve().parent.parent / "paper_book" / "metrics.json"
    metrics_html = ""
    if mpath.exists():
        try:
            metrics_html = render_metrics_block(json.loads(mpath.read_text()))
        except Exception:
            metrics_html = ""
    # ... insert `metrics_html` into the existing HTML template body, just under the <h1>.
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_paper_book_dashboard.py -v`
Expected: PASS.

- [ ] **Step 5: Codex-review the diff, then commit**

```bash
git add scripts/paper_book_dashboard.py tests/test_paper_book_dashboard.py
git commit -m "feat(paper-book): dashboard shows forward tier + book-vs-QQQ excess"
```

---

### Task 6: GitHub Actions workflow

**Files:**
- Create: `.github/workflows/paper_book.yml`
- Modify: `.gitignore` (un-ignore `paper_book/book_state.json`, `paper_book/metrics.json`)

**Interfaces:**
- Consumes: Task 4 CI behavior. Secrets `SUPABASE_URL`, `SUPABASE_SERVICE_KEY` (already present, used by `learning_snapshot.yml`).

- [ ] **Step 1: Un-ignore the committed state files**

Check `.gitignore` for the `paper_book/` rule and add explicit un-ignores after it:

```gitignore
paper_book/
!paper_book/book_state.json
!paper_book/metrics.json
```

(If `.gitignore` ignores `paper_book/book.db` specifically rather than the whole dir, no change is needed — confirm with `git check-ignore -v paper_book/book_state.json`.)

- [ ] **Step 2: Create the workflow** (mirrors `learning_snapshot.yml`; adds a `pip install yfinance` step that `learning_snapshot` does not need)

```yaml
# .github/workflows/paper_book.yml
name: paper_book

# Runs the local paper book unattended: hydrate committed JSON state -> sync new
# trade setups -> replay (freeze closed fills) -> compute forward-vs-QQQ metrics ->
# commit book_state.json + metrics.json + dashboard.html back to the repo.

on:
  schedule:
    - cron: "30 22 * * 1-5"
  workflow_dispatch:

concurrency:
  group: paper_book
  cancel-in-progress: false

permissions:
  contents: write

jobs:
  paper_book:
    runs-on: ubuntu-latest
    timeout-minutes: 10
    steps:
      - uses: actions/checkout@v6
        with:
          fetch-depth: 1
          token: ${{ secrets.GITHUB_TOKEN }}

      - name: Record workflow start
        env:
          SUPABASE_URL:         ${{ secrets.SUPABASE_URL }}
          SUPABASE_SERVICE_KEY: ${{ secrets.SUPABASE_SERVICE_KEY }}
        run: python agents/ops_recorder.py --phase start --agent workflow_paper_book

      - uses: actions/setup-python@v6
        with:
          python-version: "3.12"
          cache: pip

      - name: Install deps
        run: pip install yfinance

      - name: Run paper book
        env:
          SUPABASE_URL:           ${{ secrets.SUPABASE_URL }}
          SUPABASE_SERVICE_KEY:   ${{ secrets.SUPABASE_SERVICE_KEY }}
          PAPER_BOOK_STATE_JSON:  paper_book/book_state.json
        run: python scripts/paper_book.py run

      - name: Commit and push
        run: |
          set -e
          git config user.name  "github-actions[bot]"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
          if [ -z "$(git status --porcelain paper_book/)" ]; then
            echo "No paper-book changes."
            exit 0
          fi
          git add paper_book/book_state.json paper_book/metrics.json paper_book/dashboard.html
          today=$(date -u +%Y-%m-%d)
          git pull --rebase --autostash || true
          git commit -m "chore(paper-book): auto forward mark ${today}"
          git push || (git pull --rebase --autostash && git push)

      - name: Record workflow finish
        if: always()
        env:
          SUPABASE_URL:         ${{ secrets.SUPABASE_URL }}
          SUPABASE_SERVICE_KEY: ${{ secrets.SUPABASE_SERVICE_KEY }}
          WORKFLOW_STATUS:      ${{ job.status }}
        run: python agents/ops_recorder.py --phase finish --agent workflow_paper_book --status "$WORKFLOW_STATUS"
```

- [ ] **Step 3: Validate the workflow YAML**

Run: `python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/paper_book.yml')); print('yaml ok')"`
Expected: `yaml ok`. (If `actionlint` is installed: `actionlint .github/workflows/paper_book.yml`.)

- [ ] **Step 4: Codex-review the diff, then commit**

```bash
git add .github/workflows/paper_book.yml .gitignore
git commit -m "feat(paper-book): daily GitHub Actions forward loop + commit-back"
```

- [ ] **Step 5: Live validation (manual, after merge)**

```bash
gh workflow run paper_book.yml --repo nishantgupta83/stock_app
gh run watch "$(gh run list --workflow=paper_book.yml -L1 --json databaseId -q '.[0].databaseId')" --repo nishantgupta83/stock_app
# then confirm the commit-back + metrics:
git pull && python -c "import json;print(json.load(open('paper_book/metrics.json'))['tier'])"
```

Expected: workflow green; `paper_book/metrics.json` committed; `tier.status` is `inconclusive` (forward book just started — correct).

---

## Self-Review

**Spec coverage:** A (auto loop) → Tasks 4+6. Frozen ledger / determinism fix → Task 1 + Task 4 freeze-skip. F instrumentation (equity curves, excess, cohorts, diagnostics, tiers, sync_ok) → Tasks 2+3. Dashboard → Task 5. forward_epoch → Task 1 + Task 4 init. Error handling (non-fatal sync, rebase-before-push, empty-diff guard) → Tasks 4+6. Tests → every task. No spec section is unbacked.

**Placeholder scan:** No TBD/TODO; every code step has runnable code. The dashboard `render` injection point references "the existing HTML template body" — this is an integration instruction against existing code the engineer can see, not a placeholder for new logic.

**Type consistency:** `compute_metrics` returns `{"replay","forward","tier","captured_at"}`; `classify_tier` consumes the `forward` block keys produced by `_block` (`n_independent_cohorts`, `weeks`, `cumulative_excess`, `max_drawdown`, `top_cohort_excess_share`, `profit_factor`, `subperiods_positive`). `export_state`/`import_state` use the same `_POS_COLS`/`book_setups` columns as the live schema. `dump_state_json`/`load_state_json` gate on `STATE_JSON` consistently.

**Known follow-ups (out of scope, flagged in spec):** Tier ③ "conviction" gate (needs D + regime buckets); idle-cash live T-bill rate; GH Pages/Hostinger publish; orchestrator-watchdog registration.
