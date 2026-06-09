#!/usr/bin/env python3
"""validate_metalabel_gate.py — PR-B walk-forward validation of the 2.b gate.

PR-C will wire the meta-label gate LIVE to SUPPRESS low-expectancy candidates.
Suppression destroys value if it kills winners, so this proves the gate's
effect OFFLINE first, leakage-free, before any live wiring.

METHOD (walk-forward backtest, per Codex review)
------------------------------------------------
For each historical candidate cluster (score >= THESIS_RECALL_FLOOR), at its
reconstructed run_at:
  * PRIMARY cell  = derive(primary_event_type, subtype, horizon) — the cell the
    live signal is attributed to (NOT a cherry-picked best cell).
  * gate stats    = walkforward_stats over closed trades whose outcome was KNOWN
    (realized_at) BEFORE run_at — purges future info AND the candidate's own
    not-yet-closed trade.
  * decision      = gate_decision(stats)  (act / suppressed_low_pf / fail_open_thin)
  * LABEL         = the candidate's actual forward outcome = the paper trade for
    (ticker, cell) entered nearest run_at.
Reported PER HORIZON (the live signal horizon is debatable — h1d nominally, but
the mature rules live at h15d/h30d — so we DON'T pick one; we show all):
  * decision split (act / suppressed / fail_open) ;
  * mean realized return of ACTed vs SUPPRESSED candidates (act should be
    clearly higher — the gate's whole purpose) ;
  * suppression FALSE-NEGATIVE rate = suppressed candidates that actually WON
    (the cost of the gate — keep it low).

USAGE
-----
  export SUPABASE_URL=...  SUPABASE_SERVICE_KEY=...   # private shell
  python3 scripts/validate_metalabel_gate.py [--windows 90,180]
        [--pf-bar 1.5] [--min-n 100] [--match-tol-days 3]

Read-only. No writes.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agents"))
sys.path.insert(0, os.path.dirname(__file__))
import _rule_key  # noqa: E402
from _metalabel_gate import walkforward_stats, gate_decision, GATE_REASONS  # noqa: E402
from thesis_agent import (  # noqa: E402
    score_cluster, CLUSTER_WINDOW_MIN, FRESHNESS_WINDOW_MIN,
    THESIS_RECALL_FLOOR, _CANDIDATE_HORIZONS,
)
from replay_cluster_coverage import reconstruct_clusters, _parse  # noqa: E402

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
HEADERS = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}


# ----------------------------------------------------------------------------
# Pure helpers (unit-tested in tests/test_validate_metalabel_gate.py)
# ----------------------------------------------------------------------------
def primary_event(events: list[dict]) -> tuple[str | None, str, int | None]:
    """(primary_event_type, primary_subtype, primary_event_id) — IDENTICAL
    attribution to thesis_agent.write_signal: alphabetically-first event_type +
    the subtype of the first event of that type. The id is that event's id, used
    to match its EXACT paper trade (no ambiguous time-window matching)."""
    typed = [e for e in events if e.get("event_type")]
    if not typed:
        return None, "", None
    primary_et = sorted({e["event_type"] for e in typed})[0]
    first = next(e for e in typed if e["event_type"] == primary_et)
    return primary_et, (first.get("event_subtype") or "").strip(), first.get("id")


def build_label_index(trades: list[dict]) -> dict[tuple[int, int], tuple[float, bool]]:
    """(event_id, horizon_days) -> (realized_return, correct). Paper trades are
    keyed by (event_id, horizon) in event_paper_agent, so this is the EXACT label
    for a candidate's primary cell — no ticker/entry_at heuristic that could grab
    a neighbouring event's trade (Codex review)."""
    idx: dict[tuple[int, int], tuple[float, bool]] = {}
    for t in trades:
        eid, h, rr = t.get("event_id"), t.get("horizon_days"), t.get("realized_return")
        if eid is None or h is None or rr is None:
            continue
        idx[(int(eid), int(h))] = (float(rr), bool(t.get("correct")))
    return idx


def match_label(idx: dict, event_id: int | None, horizon: int) -> tuple[float, bool] | None:
    """The candidate's actual forward outcome: the paper trade for its PRIMARY
    event_id at this horizon. None if the trade isn't closed/present."""
    if event_id is None:
        return None
    return idx.get((event_id, horizon))


def per_cell_breakdown(cands: list[dict], idx: dict, horizons, now: datetime,
                       mature_tol_days: int) -> dict[str, dict]:
    """Group candidates by their PRIMARY (rule_key::horizon) cell.

    Returns {cell: {"n_cand": int, "labels": [realized_return, ...]}} where
    labels are the matured candidate outcomes for that cell. Lets the diagnostic
    show WHICH cells drive the act/suppress decisions and whether their pooled
    expectancy actually matches the candidate outcomes."""
    cells: dict[str, dict] = defaultdict(lambda: {"n_cand": 0, "labels": []})
    for c in cands:
        et, sub, eid = primary_event(c["events"])
        if not et:
            continue
        for h in horizons:
            cell = _rule_key.derive(et, sub, h)
            cells[cell]["n_cand"] += 1
            if c["run_at"] <= now - timedelta(days=h + mature_tol_days):
                lbl = match_label(idx, eid, h)
                if lbl is not None:
                    cells[cell]["labels"].append(lbl[0])
    return cells


# ----------------------------------------------------------------------------
# I/O
# ----------------------------------------------------------------------------
def _page(table: str, params: dict) -> list[dict]:
    rows, offset, page = [], 0, 1000
    while True:
        r = requests.get(f"{SUPABASE_URL}/rest/v1/{table}",
                         headers={**HEADERS, "Range-Unit": "items",
                                  "Range": f"{offset}-{offset+page-1}"},
                         params=params, timeout=60)
        r.raise_for_status()
        batch = r.json()
        rows.extend(batch)
        if len(batch) < page:
            break
        offset += page
        if offset > 300_000:
            break
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--windows", default="90,180")
    ap.add_argument("--pf-bar", type=float, default=1.5)
    ap.add_argument("--min-n", type=int, default=100)
    ap.add_argument("--match-tol-days", type=int, default=3)
    args = ap.parse_args()
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("ERROR: set SUPABASE_URL and SUPABASE_SERVICE_KEY.", file=sys.stderr)
        return 2

    windows = [int(w) for w in args.windows.split(",")]
    now = datetime.now(timezone.utc)
    max_win = max(windows)

    print("Fetching closed paper trades ...", file=sys.stderr)
    trades = _page("stock_event_paper_trades", {
        "status": "eq.closed",
        "select": "rule_key,realized_return,correct,exit_at,created_at,"
                  "event_id,horizon_days",
    })
    idx = build_label_index(trades)
    print(f"  {len(trades)} closed trades.", file=sys.stderr)

    print(f"Fetching events created in the last {max_win}d ...", file=sys.stderr)
    raw = _page("stock_normalized_events", {
        "created_at": f"gte.{(now - timedelta(days=max_win)).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        "ticker": "not.is.null",
        "select": "id,event_type,event_subtype,ticker,event_at,created_at,"
                  "severity,source_table,parser_confidence,payload",
        "order": "created_at.desc",
    })
    print(f"  {len(raw)} events.\n", file=sys.stderr)

    for w in sorted(windows):
        cutoff = now - timedelta(days=w)
        evs = [e for e in raw if (_parse(e.get("created_at")) or now) >= cutoff]
        clusters = reconstruct_clusters(evs, freshness_min=FRESHNESS_WINDOW_MIN,
                                        cluster_window_min=CLUSTER_WINDOW_MIN)
        cands = []
        for c in clusters:
            scored = score_cluster(c["events"], rule_calibration={}, bucket=c["bucket"],
                                   now=c["run_at"], agent_weights={}, sector_multipliers={},
                                   ticker_sectors={}, risk_off=False, wide_events=[],
                                   watchlist_map={}, news_fetch=None)
            if float(scored.get("score") or 0) >= THESIS_RECALL_FLOOR:
                cands.append(c)

        print(f"═══ {w}d window: {len(cands)} candidates ═══")
        print(f"  decisions: act / suppr / failopen   |   labeled outcomes "
              f"(mature only): act_ret%(n) vs suppr_ret%(n), suppr_FN%, censored")
        print(f"{'horizon':>8} {'act':>5} {'suppr':>6} {'failopen':>9} "
              f"{'act_ret%':>10} {'suppr_ret%':>12} {'suppr_FN%':>10} {'censored':>9}")
        for h in _CANDIDATE_HORIZONS:
            counts = {r: 0 for r in GATE_REASONS}
            act_ret: list[float] = []
            sup_ret: list[float] = []
            sup_fn = 0          # suppressed candidates that actually won (the cost)
            censored = 0        # too recent for this horizon to have closed
            # A candidate can only have a closeable label if run_at + horizon +
            # tol has already passed (right-censoring guard, Codex review).
            mature_before = now - timedelta(days=h + args.match_tol_days)
            for c in cands:
                et, sub, eid = primary_event(c["events"])
                if not et:
                    continue
                cell = _rule_key.derive(et, sub, h)
                stats = walkforward_stats(trades, cell, c["run_at"])
                action, reason = gate_decision(stats, pf_bar=args.pf_bar, min_n=args.min_n)
                counts[reason] += 1
                if c["run_at"] > mature_before:
                    censored += 1
                    continue
                label = match_label(idx, eid, h)
                if label is None:
                    continue
                rr, _correct = label
                if action == "act":
                    act_ret.append(rr)
                elif reason == "suppressed_low_pf":
                    sup_ret.append(rr)
                    if rr > 0:
                        sup_fn += 1
            a = (sum(act_ret) / len(act_ret) * 100) if act_ret else float("nan")
            s = (sum(sup_ret) / len(sup_ret) * 100) if sup_ret else float("nan")
            fn = (100.0 * sup_fn / len(sup_ret)) if sup_ret else float("nan")
            print(f"{('h'+str(h)+'d'):>8} {counts['calibrated_profitable']:>5} "
                  f"{counts['suppressed_low_pf']:>6} {counts['fail_open_thin']:>9} "
                  f"{a:>7.2f}%({len(act_ret):>2}) {s:>8.2f}%({len(sup_ret):>2}) "
                  f"{fn:>9.1f}% {censored:>9}")
        print()

        # PER-CELL DIAGNOSTIC: which primary cells drive the decisions, with a
        # POOLED full-corpus read (uses ALL closed trades, NOT walk-forward — so
        # it LEAKS; directional only). Pooled has more data than any single
        # run_at, so it explains whether an acting cell is genuinely profitable
        # or the walk-forward result was just thin/noisy.
        cells = per_cell_breakdown(cands, idx, _CANDIDATE_HORIZONS, now,
                                   args.match_tol_days)
        print("  per-cell (pooled = full-corpus, LEAKY/directional; top 18 by candidates):")
        print(f"  {'cell':<34} {'cand':>4} {'pool_n':>6} {'pool_pf':>7} "
              f"{'pool_exp%':>9} {'pool_gate':>10} {'cand_ret%':>11}")
        ranked = sorted(cells.items(), key=lambda kv: kv[1]["n_cand"], reverse=True)[:18]
        for cell, agg in ranked:
            pooled = walkforward_stats(trades, cell, now)   # as_of=now → all closed
            _, reason = gate_decision(pooled, pf_bar=args.pf_bar, min_n=args.min_n)
            labels = agg["labels"]
            cret = (sum(labels) / len(labels) * 100) if labels else float("nan")
            pf = pooled["pf"]
            pf_s = "inf" if pf == float("inf") else f"{pf:.2f}"
            print(f"  {cell:<34} {agg['n_cand']:>4} {pooled['n']:>6} {pf_s:>7} "
                  f"{pooled['expectancy']*100:>8.2f}% {reason.split('_')[0]:>10} "
                  f"{cret:>9.2f}%({len(labels):>2})")
        print()

    print("Walk-forward (gate stats use only trades closed before each run_at). "
          "act_ret should exceed suppr_ret; suppr_FN = winners the gate would have "
          "suppressed (the cost — keep low). fail_open candidates still emit as WATCH.",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
