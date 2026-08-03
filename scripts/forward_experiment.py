#!/usr/bin/env python3
"""Forward provisional-long edge experiment driver (ISOLATED, off-Supabase ledger).

Pre-registration: docs/experiments/2026-08-02-preregistration-forward-provisional-long.md
Every frozen parameter (admission, entry/exit, costs, tiers, isolation) lives there
and is bound into `config_hash`. Two horizon experiments run independently:

  EXPERIMENT_ID=fwd_prov_long_h1d   1 trading-day horizon, rule-key horizon segment 'h1d'
  EXPERIMENT_ID=fwd_prov_long_h7d   7 trading-day horizon, rule-key horizon segment 'h7d'

Each has its OWN db + state JSON + forward_epoch + config_hash; they never share a
cohort. Source is the LIVE candidate ledger `stock_signal_candidates` (sql/0039),
NOT stock_trade_setups (which is starved). Candidates lack target/stop/horizon, so
we supply the frozen fixed exits (target 5%, stop 3%, stop_only grading).

Reuses by IMPORT/CALL ONLY (never edits):
  agents/_experiment_store.py            — isolated SQLite store
  agents/_paper_book.close_position      — net-of-slippage money math (single cost locus)
  agents/_paper_book.SLIPPAGE_PER_SIDE   — canonical 5 bps/side cost constant
  agents/_instruments.{fetch_tradeable_tickers,is_tradeable} — tradeable gate
  price_agent.compute_paper_outcome      — the SAME honest stop_only grader (deferred import)

Writes (under paper_book/experiments/<EXPERIMENT_ID>/):
  experiment.db  (gitignored) — durable per-experiment ledger
  metrics.json   (committed)   — forward-block metrics + tier verdict
  state.json     (committed, CI only via EXP_STATE_JSON) — full round-trip snapshot

Env:
  EXPERIMENT_ID   required: fwd_prov_long_h1d | fwd_prov_long_h7d
  EXP_DB          optional: db path override (default paper_book/experiments/<id>/experiment.db)
  EXP_STATE_JSON  optional: committed state path; SET in CI, UNSET locally
  SUPABASE_URL / SUPABASE_SERVICE_KEY  — for `sync` only (run in a private shell)
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agents"))

# Keep import-safe when Supabase creds are absent (matches paper_book.py pattern).
os.environ.setdefault("SUPABASE_URL", "http://offline.local")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "offline")

import _experiment_store as store                 # noqa: E402
from _paper_book import close_position, SLIPPAGE_PER_SIDE   # noqa: E402
from _instruments import fetch_tradeable_tickers, is_tradeable  # noqa: E402

# ---------------------------------------------------------------------------
# Frozen experiment definitions (the id selects one)
# ---------------------------------------------------------------------------

EXPERIMENTS = {
    # horizon_tag = the horizon LABEL = the LAST colon-segment of a rule_key.
    # Candidate rule_keys are `event_type:subtype:h1d` (single colon); empty-subtype
    # keys are `event_type::h1d` (double). BOTH end in the segment `h1d`, so we match
    # the last segment — NEVER `endswith("::h1d")`, which misses every non-empty-
    # subtype key (verified live 2026-08-02: candidates are `news_article:positive:h1d`).
    "fwd_prov_long_h1d": {"horizon_days": 1, "horizon_tag": "h1d"},
    "fwd_prov_long_h7d": {"horizon_days": 7, "horizon_tag": "h7d"},
}

# Frozen entry/exit/cost params (single locus — mirror the pre-registration).
DIRECTION = "bullish"        # admission filter (candidate ledger direction)
TRADE_DIRECTION = "long"     # bullish -> long position for the grader/money-math
STOP_PCT = 0.03              # -3.0% from entry
TARGET_PCT = 0.05            # +5.0% from entry
EXIT_POLICY = "stop_only"    # the honest grader policy
BENCH = "QQQ"

COLD_START_HOURS = int(os.environ.get("EXP_COLD_START_HOURS", "168"))  # 7d; epoch gates anyway

# Tier thresholds (pre-registration go/no-go).
TIER1 = {"min_cohorts": 30, "min_weeks": 8, "max_dd": 0.20, "fail_margin": 0.005}
# fail_margin (0.5% net mean cohort excess): the pre-registration kills only on a
# CLEAR negative. A mean excess in [-fail_margin, 0) is noise at n≈30 → inconclusive
# (keep running), not fail.
TIER2 = {"min_cohorts": 50, "min_weeks": 13, "min_pf": 1.4, "min_subperiods_pos": 2}


def _resolve_experiment() -> tuple[str, int, str]:
    exp_id = os.environ.get("EXPERIMENT_ID", "")
    if exp_id not in EXPERIMENTS:
        raise SystemExit(
            f"EXPERIMENT_ID must be one of {sorted(EXPERIMENTS)} (got {exp_id!r})")
    spec = EXPERIMENTS[exp_id]
    return exp_id, spec["horizon_days"], spec["horizon_tag"]


def _db_path(exp_id: str) -> Path:
    return Path(os.environ.get("EXP_DB")
                or str(ROOT / "paper_book" / "experiments" / exp_id / "experiment.db"))


def _metrics_path(exp_id: str) -> Path:
    return ROOT / "paper_book" / "experiments" / exp_id / "metrics.json"


# ---------------------------------------------------------------------------
# config_hash — sha256 of the FROZEN params only. Deterministic; NO timestamp.
# ---------------------------------------------------------------------------

def config_hash(horizon_days: int, horizon_tag: str, *,
                direction: str = DIRECTION,
                stop_pct: float = STOP_PCT,
                target_pct: float = TARGET_PCT,
                slippage_per_side: float = SLIPPAGE_PER_SIDE,
                exit_policy: str = EXIT_POLICY,
                benchmark: str = BENCH) -> str:
    """SHA-256 over the frozen parameter set. Includes cost (slippage) so a cost
    change yields a different hash; excludes forward_epoch and any wall-clock time
    so it is fully deterministic across runs and machines."""
    payload = {
        "experiment": "forward_provisional_long",
        "version": "v1",
        "direction": direction,
        "horizon_days": horizon_days,
        "horizon_tag": horizon_tag,
        "stop_pct": stop_pct,
        "target_pct": target_pct,
        "exit_policy": exit_policy,
        "slippage_per_side": slippage_per_side,
        "benchmark": benchmark,
        "admission": ("bullish&rule_keys~horizon_tag&tradeable(non_INST,non_fund)"
                      "&created_at>=forward_epoch&dedup(ticker,entry_session,horizon)"),
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Date helpers (pure)
# ---------------------------------------------------------------------------

def _date(x) -> dt.date:
    return dt.date.fromisoformat(str(x)[:10])


# ---------------------------------------------------------------------------
# Admission (pure) — a candidate enters iff ALL pre-registration rules hold
# ---------------------------------------------------------------------------

def is_admitted(candidate: dict, *, horizon_tag: str,
                tradeable_tickers: set[str], forward_epoch) -> bool:
    """Pre-registration admission rule (§Admission). Pure — no I/O.

    1. direction == 'bullish'
    2. rule_keys has >=1 key whose LAST colon-segment is the horizon label
       (h1d / h7d). Candidate keys are `type:subtype:h1d` (single colon); empty-
       subtype keys are `type::h1d` — both end in the segment `h1d`.
    3. tradeable single-name/ETF (excludes INST_* and non-tradeable funds)
    4. created_at >= forward_epoch (forward only; never backfilled)
    """
    if (candidate.get("direction") or "").strip().lower() != DIRECTION:
        return False
    rule_keys = candidate.get("rule_keys") or []
    if not any(str(k).rsplit(":", 1)[-1] == horizon_tag for k in rule_keys):
        return False
    if not is_tradeable(candidate.get("ticker"), tradeable_tickers):
        return False
    if forward_epoch is not None:
        created = candidate.get("created_at")
        if not created or _date(created) < _date(forward_epoch):
            return False
    return True


# ---------------------------------------------------------------------------
# Price bars — yfinance, per-ticker cache (replicates paper_book.bars_for; do NOT
# import it). yfinance imported lazily so this module stays test-importable.
# ---------------------------------------------------------------------------

_BARS: dict[str, dict] = {}


def bars_for(ticker: str, start: dt.date, end: dt.date) -> dict:
    if ticker in _BARS:
        return _BARS[ticker]
    import yfinance as yf  # lazy — avoids a heavy import at test-collection time
    out: dict[dt.date, dict] = {}
    try:
        df = yf.Ticker(ticker).history(
            start=start.isoformat(),
            end=(end + dt.timedelta(days=4)).isoformat(),
            auto_adjust=True)
        for ts, row in df.iterrows():
            d = ts.date() if hasattr(ts, "date") else ts.to_pydatetime().date()
            out[d] = {
                "open": float(row["Open"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "close": float(row["Close"]),
            }
    except Exception as e:  # noqa: BLE001
        print(f"  {ticker}: bar fetch failed — {e}", file=sys.stderr)
    _BARS[ticker] = out
    return out


def _next_session(bars: dict, on_or_after: dt.date):
    for d in sorted(bars):
        if d >= on_or_after:
            return d, bars[d]
    return None


def entry_after(bars: dict, created_date: dt.date):
    """Anti-lookahead entry: the first session STRICTLY AFTER created_date (never
    the same calendar day the candidate landed). Returns (date, bar) or None."""
    return _next_session(bars, created_date + dt.timedelta(days=1))


# ---------------------------------------------------------------------------
# Paginated Supabase GET (replicates paper_book._sb — do NOT import it)
# ---------------------------------------------------------------------------

def _sb(path: str) -> list[dict]:
    url = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ["SUPABASE_SERVICE_KEY"]
    rows, off = [], 0
    while True:
        req = urllib.request.Request(
            f"{url}/rest/v1/{path}",
            headers={
                "apikey": key,
                "Authorization": f"Bearer {key}",
                "Range": f"{off}-{off+999}",
            })
        page = json.load(urllib.request.urlopen(req))
        rows += page
        if len(page) < 1000:
            return rows
        off += 1000


# ---------------------------------------------------------------------------
# Sync — incremental pull of bullish candidates, admit in-memory
# ---------------------------------------------------------------------------

def sync(conn, exp_id: str, horizon_tag: str, forward_epoch) -> int:
    cur = store.get_state(conn, exp_id).get("cursor")
    if not cur:
        cur = (dt.datetime.now(dt.timezone.utc)
               - dt.timedelta(hours=COLD_START_HOURS)).isoformat()

    # Fail-closed on the tradeable gate: without it we cannot safely exclude funds,
    # so skip this sync (non-fatal in CI, retried next run) rather than admit blind.
    base_url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_KEY"]
    tradeable = fetch_tradeable_tickers(
        base_url, {"apikey": key, "Authorization": f"Bearer {key}"})
    if tradeable is None:
        raise RuntimeError("tradeable-ticker set unavailable — failing closed (no admit)")

    cur_q = urllib.parse.quote(cur, safe="")
    rows = _sb(
        "stock_signal_candidates?direction=eq.bullish"
        f"&created_at=gt.{cur_q}&order=created_at.asc"
        "&select=id,created_at,ticker,direction,score,recall_floor,"
        "rule_keys,source_agents")
    n_new = 0
    newest = cur
    for r in rows:
        newest = max(newest, r["created_at"])
        if not is_admitted(r, horizon_tag=horizon_tag,
                           tradeable_tickers=tradeable, forward_epoch=forward_epoch):
            continue
        if store.ingest_candidate(
                conn, exp_id,
                candidate_id=r["id"],
                created_at=r["created_at"],
                ticker=r["ticker"],
                direction=r.get("direction"),
                score=r.get("score"),
                rule_keys=json.dumps(r.get("rule_keys") or []),
                source_agents=json.dumps(r.get("source_agents") or []),
                raw=json.dumps(r)):
            n_new += 1
    store.set_cursor(conn, exp_id, newest)
    print(f"[sync] {exp_id}: {len(rows)} bullish candidates since cursor, "
          f"{n_new} newly admitted (cursor -> {newest[:19]})")
    return n_new


# ---------------------------------------------------------------------------
# Grade — per-candidate forward stop_only outcome vs matched QQQ window
# ---------------------------------------------------------------------------

def _matched_qqq_return(entry_date: dt.date, exit_date: dt.date) -> float | None:
    """Gross same-window QQQ return: buy at entry_session OPEN, sell at exit-bar
    CLOSE (per the pre-registration benchmark). Returns None if either leg is
    unavailable (leave the candidate UNRESOLVED — do not freeze a bogus excess)."""
    qbars = bars_for(BENCH, entry_date, exit_date)
    if not qbars:
        return None
    q_entry = q_exit = None
    for d in sorted(qbars):
        if d >= entry_date and q_entry is None:
            q_entry = qbars[d].get("open")
    for d in sorted(qbars):
        if d >= exit_date:
            q_exit = qbars[d].get("close")
            break
    if not q_entry or q_exit is None:
        return None
    return (q_exit - q_entry) / q_entry


def grade(conn, exp_id: str, horizon_days: int) -> None:
    # Deferred import: price_agent reads SUPABASE_* at import time and pulls
    # yfinance/pandas — keep it out of test collection.
    from price_agent import compute_paper_outcome

    today = dt.datetime.now(dt.timezone.utc).date()
    candidates = store.all_candidates(conn, exp_id)
    frozen = store.closed_candidate_ids(conn, exp_id)
    pending = [c for c in candidates if c["candidate_id"] not in frozen]
    if not pending:
        return

    # Pre-warm the QQQ cache over the FULL window so per-candidate matched lookups
    # (bars_for caches by ticker) don't lock to the first narrow range fetched.
    earliest = min(_date(c["created_at"]) for c in pending)
    bars_for(BENCH, earliest, today)

    for c in pending:
        try:
            cid = c["candidate_id"]
            ticker = c["ticker"]
            created_date = _date(c["created_at"])
            bars = bars_for(ticker, created_date, today)
            if not bars:
                continue  # unpriceable this run — retry next run (do not freeze)

            entry = entry_after(bars, created_date)
            if not entry:
                continue  # too fresh — no session strictly after created_at yet
            entry_date, entry_bar = entry
            entry_open = entry_bar.get("open")
            if not entry_open:
                continue

            trade = {
                "entry_at": entry_date.isoformat() + "T00:00:00+00:00",
                "entry_price": entry_open,
                "direction": TRADE_DIRECTION,
                "horizon_days": horizon_days,
                "target_pct": TARGET_PCT,
                "stop_pct": STOP_PCT,
            }
            outcome = compute_paper_outcome(trade, bars, exit_policy=EXIT_POLICY)

            if outcome is None:
                # Horizon not matured and no stop yet — still open; re-grade next run.
                store.save_position(
                    conn, exp_id, candidate_id=cid, ticker=ticker,
                    direction=TRADE_DIRECTION, horizon_days=horizon_days,
                    created_at=c["created_at"], entry_session=entry_date.isoformat(),
                    entry_px=round(entry_open, 4), exit_session=None, exit_px=None,
                    exit_reason=None, net_return=None, qqq_return=None, excess=None,
                    status="open")
                continue

            exit_date = _date(outcome["exit_at"])
            if exit_date >= today:
                # Exit bar not final yet — keep open, re-grade once today's bar closes.
                store.save_position(
                    conn, exp_id, candidate_id=cid, ticker=ticker,
                    direction=TRADE_DIRECTION, horizon_days=horizon_days,
                    created_at=c["created_at"], entry_session=entry_date.isoformat(),
                    entry_px=round(entry_open, 4), exit_session=None, exit_px=None,
                    exit_reason=None, net_return=None, qqq_return=None, excess=None,
                    status="open")
                continue

            exit_px = outcome["exit_price"]  # RAW price (compute_paper_outcome does
                                             # not apply slippage to exit_price)
            # SINGLE COST LOCUS: net the round-trip slippage exactly once, via the
            # canonical _paper_book money-math. exit_px is raw, so this applies
            # slippage one time (equals compute_paper_outcome.realized_return).
            net_return, _pnl = close_position(
                entry_open, exit_px, TRADE_DIRECTION, 1.0, SLIPPAGE_PER_SIDE)

            qqq_return = _matched_qqq_return(entry_date, exit_date)
            if qqq_return is None:
                continue  # benchmark missing — leave UNRESOLVED, retry next run

            store.save_position(
                conn, exp_id, candidate_id=cid, ticker=ticker,
                direction=TRADE_DIRECTION, horizon_days=horizon_days,
                created_at=c["created_at"], entry_session=entry_date.isoformat(),
                entry_px=round(entry_open, 4), exit_session=exit_date.isoformat(),
                exit_px=round(exit_px, 4), exit_reason=outcome.get("exit_reason"),
                net_return=round(net_return, 6), qqq_return=round(qqq_return, 6),
                excess=round(net_return - qqq_return, 6), status="closed")
        except Exception as e:  # noqa: BLE001
            print(f"  grade skipped candidate {c.get('candidate_id')}: {e}",
                  file=sys.stderr)


# ---------------------------------------------------------------------------
# Metrics (pure) — forward/replay split, cohorts, tiers
# ---------------------------------------------------------------------------

def split_forward_replay(positions: list[dict], forward_epoch) -> tuple[list, list]:
    """Partition positions by created_at vs forward_epoch. No epoch -> all forward."""
    if not forward_epoch:
        return list(positions), []
    ep = _date(forward_epoch)
    fwd, rep = [], []
    for p in positions:
        created = p.get("created_at")
        (fwd if (created and _date(created) >= ep) else rep).append(p)
    return fwd, rep


def dedup_positions(positions: list[dict]) -> list[dict]:
    """At most one position per (ticker, entry_session, horizon_days); the earliest
    candidate (lowest candidate_id) wins. Rows without an entry_session are dropped."""
    seen: dict[tuple, dict] = {}
    for p in sorted(positions, key=lambda r: (r.get("candidate_id") or 0)):
        es = p.get("entry_session")
        if not es:
            continue
        key = (p.get("ticker"), str(es)[:10], p.get("horizon_days"))
        seen.setdefault(key, p)
    return list(seen.values())


def count_cohorts(positions: list[dict]) -> int:
    """Independent cohorts = distinct entry_session dates (same-day = one cohort)."""
    return len({str(p["entry_session"])[:10] for p in positions
                if p.get("entry_session")})


def build_cohorts(closed_positions: list[dict]) -> dict:
    """Group DEDUPED closed positions by entry_session date; equal-weight within a
    day. Returns {iso_date: {n, net, qqq, excess}}."""
    by_day: dict[str, list[dict]] = {}
    for p in dedup_positions(closed_positions):
        by_day.setdefault(str(p["entry_session"])[:10], []).append(p)
    cohorts: dict[str, dict] = {}
    for day, ps in by_day.items():
        nets = [float(p["net_return"]) for p in ps if p.get("net_return") is not None]
        qqqs = [float(p["qqq_return"]) for p in ps if p.get("qqq_return") is not None]
        exs = [float(p["excess"]) for p in ps if p.get("excess") is not None]
        cohorts[day] = {
            "n": len(ps),
            "net": (sum(nets) / len(nets)) if nets else 0.0,
            "qqq": (sum(qqqs) / len(qqqs)) if qqqs else 0.0,
            "excess": (sum(exs) / len(exs)) if exs else 0.0,
        }
    return cohorts


def _subperiods_positive(days: list[str], cohorts: dict, halves: int = 2) -> int:
    if len(days) < halves:
        return 0
    size = len(days) // halves
    pos = 0
    for h in range(halves):
        lo = h * size
        hi = (h + 1) * size if h < halves - 1 else len(days)
        if sum(cohorts[d]["excess"] for d in days[lo:hi]) > 0:
            pos += 1
    return pos


def forward_block(closed_positions: list[dict]) -> dict:
    cohorts = build_cohorts(closed_positions)
    days = sorted(cohorts)
    n = len(days)
    excesses = [cohorts[d]["excess"] for d in days]
    nets = [cohorts[d]["net"] for d in days]

    mean_excess = (sum(excesses) / n) if n else 0.0
    mean_net = (sum(nets) / n) if n else 0.0

    wins = sum(e for e in excesses if e > 0)
    losses = -sum(e for e in excesses if e < 0)
    if losses <= 0:
        pf = None if wins > 0 else 0.0   # None == no losing cohorts (JSON-safe)
    else:
        pf = round(wins / losses, 4)

    total = sum(excesses)
    top_share = (round(max(excesses) / total, 4)
                 if excesses and abs(total) > 1e-9 else 0.0)

    # Candidate cohort-equity drawdown (compounded net returns, equal-weight/day).
    equity = peak = 1.0
    mdd = 0.0
    for d in days:
        equity *= (1.0 + cohorts[d]["net"])
        peak = max(peak, equity)
        if peak > 0:
            mdd = max(mdd, (peak - equity) / peak)

    weeks = round((_date(days[-1]) - _date(days[0])).days / 7.0, 1) if n >= 2 else 0.0

    return {
        "n_cohorts": n,
        "n_positions": len(closed_positions),
        "weeks": weeks,
        "mean_cohort_excess": round(mean_excess, 6),
        "mean_cohort_net": round(mean_net, 6),
        "profit_factor": pf,
        "top_cohort_excess_share": top_share,
        "max_drawdown": round(mdd, 4),
        "subperiods_positive": _subperiods_positive(days, cohorts),
        "cohorts": cohorts,
    }


def classify_tier(fwd: dict, sync_ok: bool = True) -> dict:
    """Pre-registration go/no-go (FORWARD block only): continue / inconclusive /
    fail, with 'scale_paper' when Tier ② also clears."""
    if not sync_ok:
        return {"status": "inconclusive", "reason": "sync_failed", "next": "tier1"}
    n = fwd["n_cohorts"]
    weeks = fwd["weeks"]
    excess = fwd["mean_cohort_excess"]
    dd = fwd["max_drawdown"]
    top = abs(fwd["top_cohort_excess_share"])
    if n < TIER1["min_cohorts"] or weeks < TIER1["min_weeks"]:
        return {"status": "inconclusive", "reason": "insufficient_sample",
                "have_cohorts": n, "need_cohorts": TIER1["min_cohorts"],
                "have_weeks": weeks, "need_weeks": TIER1["min_weeks"]}
    if dd > TIER1["max_dd"]:
        return {"status": "fail", "reason": "max_drawdown_breach",
                "max_drawdown": dd, "limit": TIER1["max_dd"]}
    if excess < -TIER1["fail_margin"]:
        # CLEAR negative excess (beyond the noise band) → kill (pre-registration).
        return {"status": "fail", "reason": "clear_negative_excess",
                "mean_cohort_excess": excess, "fail_below": -TIER1["fail_margin"]}
    if excess < 0:
        # Marginally negative — within noise at this n; keep running, do NOT kill.
        return {"status": "inconclusive", "reason": "marginal_negative_excess",
                "mean_cohort_excess": excess}
    if top >= 1.0:
        return {"status": "inconclusive", "reason": "single_cohort_dominates",
                "top_cohort_excess_share": top}
    status = "continue"
    pf = fwd["profit_factor"]
    pf_ok = (pf is None) or (pf > TIER2["min_pf"])   # None => no losing cohorts
    if (n >= TIER2["min_cohorts"] and weeks >= TIER2["min_weeks"] and excess > 0
            and pf_ok and fwd["subperiods_positive"] >= TIER2["min_subperiods_pos"]):
        status = "scale_paper"
    return {"status": status, "mean_cohort_excess": excess, "max_drawdown": dd,
            "n_cohorts": n, "weeks": weeks, "profit_factor": pf}


def compute_metrics(positions: list[dict], forward_epoch, config_hash_val: str,
                    exp_id: str, sync_ok: bool = True) -> dict:
    fwd_pos, rep_pos = split_forward_replay(positions, forward_epoch)
    fwd = forward_block([p for p in fwd_pos if p.get("status") == "closed"])
    rep = forward_block([p for p in rep_pos if p.get("status") == "closed"])
    return {
        "experiment_id": exp_id,
        "config_hash": config_hash_val,
        "forward_epoch": forward_epoch,
        "captured_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "sync_ok": sync_ok,
        "n_admitted": len(positions),
        "n_open": sum(1 for p in positions if p.get("status") == "open"),
        "n_cohorts_forward": count_cohorts(
            [p for p in fwd_pos if p.get("status") == "closed"]),
        "forward": fwd,
        "replay": rep,
        "tier": classify_tier(fwd, sync_ok=sync_ok),
    }


# ---------------------------------------------------------------------------
# State round-trip — CI durability via committed state.json
# ---------------------------------------------------------------------------

def load_state(conn, exp_id: str, state_json: str | None) -> None:
    if not state_json:
        return
    p = Path(state_json)
    if p.exists():
        store.import_state(conn, json.loads(p.read_text()))


def dump_state(conn, exp_id: str, state_json: str | None) -> None:
    if not state_json:
        return
    p = Path(state_json)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(store.export_state(conn, exp_id), indent=0, default=str))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    exp_id, horizon_days, horizon_tag = _resolve_experiment()
    state_json = os.environ.get("EXP_STATE_JSON")   # set in CI; unset locally
    db_path = _db_path(exp_id)

    conn = store.connect(db_path)
    store.init(conn)
    store.ensure_state(conn, exp_id)
    load_state(conn, exp_id, state_json)             # hydrate committed snapshot (CI)

    # Freeze the config hash (raises on drift; sets it on first run).
    cfg_hash = config_hash(horizon_days, horizon_tag)
    store.check_config_hash(conn, exp_id, cfg_hash)

    # Set forward_epoch ONCE (first run) = today's date. In CI, load_state has
    # already restored the committed epoch, so this only fires on the very first run.
    if not store.get_state(conn, exp_id).get("forward_epoch"):
        store.set_forward_epoch(
            conn, exp_id, dt.datetime.now(dt.timezone.utc).date().isoformat())
    forward_epoch = store.get_state(conn, exp_id).get("forward_epoch")

    sync_ok = True
    try:
        sync(conn, exp_id, horizon_tag, forward_epoch)
    except Exception as e:  # noqa: BLE001
        sync_ok = False
        label = "non-fatal in CI" if state_json else "fatal locally"
        print(f"[sync] FAILED ({label}): {e}", file=sys.stderr)
        if not state_json:
            raise

    grade(conn, exp_id, horizon_days)

    positions = store.all_positions(conn, exp_id)
    metrics = compute_metrics(positions, forward_epoch, cfg_hash, exp_id, sync_ok=sync_ok)
    out = _metrics_path(exp_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(metrics, indent=2, default=str))

    dump_state(conn, exp_id, state_json)

    fwd = metrics["forward"]
    print(f"[{exp_id}] admitted={metrics['n_admitted']} open={metrics['n_open']} "
          f"cohorts={fwd['n_cohorts']} mean_excess={fwd['mean_cohort_excess']} "
          f"tier={metrics['tier']['status']} sync_ok={sync_ok} -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
