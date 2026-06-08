#!/usr/bin/env python3
"""replay_cluster_coverage.py — PR-B0 cluster-replay coverage (commit-grade).

WHAT THIS ANSWERS
-----------------
PR-A's measure_candidate_coverage.py approximated 2.b gate readiness with
per-EVENT catalyst-role counts and self-documented (its lines 130-139) that
this OVERSTATES readiness — it drops context-role events (e.g. filing_13g
scores 10 ctx pts and a 13G cluster CAN clear the floor) and never actually
clusters. This script does the true CLUSTER replay:

  group events -> score each with the SAME score_cluster() the live agent uses
  -> keep candidates (score >= THESIS_RECALL_FLOOR) -> bucket each candidate by
  its rule_key::horizon calibration cell (n>=100 strict / n>=50 prov / thin).

Coverage% = candidates landing in a gateable cell / all candidates. That is the
number that scopes PR-C's 2.b precision gate.

NO-LOOKAHEAD BASELINE (important framing — see Codex review 2026-06-08)
----------------------------------------------------------------------
Time-dependent inputs are DISABLED so the replay carries no lookahead:
  * risk_off=False, wide_events=[], watchlist_map={}  (no sector/hyperscaler/
    power bonus), agent_weights={}, sector_multipliers={}  (learned/current),
  * news_fetch=None  (no PR1B raw-news promotion),
  * each cluster scored with now=run_at (its reconstructed run time) so
    catalyst-age eligibility is judged as-of that run, not today.

Coverage% is NOT a mathematical lower bound (it is a ratio; live bonuses lift
some sub-floor clusters over the floor and can shift the mix). So:
  * a COMMIT/NARROW result here is STRONG EVIDENCE to proceed,
  * a sub-threshold result is INCONCLUSIVE (live bonuses might raise it), not a
    hard defer — re-measure with point-in-time bonuses before concluding DEFER.

TWO KNOWN BIASES (they cut opposite ways → read coverage as DIRECTIONAL)
-----------------------------------------------------------------------
* DOWN: bonuses/news/risk-off disabled (above) can only omit clusters the live
  agent would lift over the floor.
* UP: each (ticker, event_at-bucket) is scored at run_at = MAX created_at — the
  most-complete bucket snapshot. The live candidate ledger dedupes by
  thesis_{ticker}_{bucket} and records the cluster at its FIRST floor-crossing,
  which may have FEWER rule_keys. So a long-replay cluster can look more gateable
  than the ledger row did. (Exact first-crossing replay = a follow-up; this is a
  max-complete snapshot.)
Because the biases oppose, treat COMMIT/NARROW as strong directional evidence,
not a point estimate. Calibration is TODAY's full-sample stock_rule_calibration
(correct for "is the cell gateable now", which is what PR-C will use; NOT a
walk-forward as-of run_at).

FIDELITY LIMITATION
-------------------
Production runs every ~5 min over a sliding freshness window; this groups by
(ticker, CLUSTER_WINDOW_MIN bucket) once and trims each cluster to a single
freshness window via created_at. It does NOT replay the exact neighbor set a
cluster had across overlapping runs.

Two coverage metrics are reported (Codex review): CANDIDATE-level ("any
constituent cell gateable") and CANDIDATE×HORIZON ("fraction of (candidate,
horizon) cells gateable"). The latter is the one 2.b actually gates on — a
candidate gateable only at h1d is fully gateable candidate-level but only 1/4
gateable candidate×horizon. Trust the candidate×horizon number for PR-C scope.

USAGE
-----
  export SUPABASE_URL=...  SUPABASE_SERVICE_KEY=...   # private shell
  python3 scripts/replay_cluster_coverage.py [--windows 30,90,180]
        [--n-strict 100] [--n-prov 50]

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
import _rule_key  # noqa: E402
from thesis_agent import (  # noqa: E402
    score_cluster,
    _event_within_real_ttl,
    CLUSTER_WINDOW_MIN,
    FRESHNESS_WINDOW_MIN,
    THESIS_RECALL_FLOOR,
    _CANDIDATE_HORIZONS,
)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
HEADERS = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}

# Same thresholds as measure_candidate_coverage.py so PR-A and PR-B0 compare.
COMMIT_PCT = 70.0
NARROW_PCT = 60.0


# ----------------------------------------------------------------------------
# Pure helpers (unit-tested in tests/test_replay_cluster_coverage.py)
# ----------------------------------------------------------------------------
def _parse(ts: str) -> datetime | None:
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (TypeError, ValueError, AttributeError):
        return None


def reconstruct_clusters(events: list[dict], *, freshness_min: int,
                         cluster_window_min: int) -> list[dict]:
    """Group events into (ticker, event_at bucket) clusters, then trim each to a
    SINGLE production-run visibility window so the replay never invents clusters
    production couldn't have seen (Codex finding #1).

    run_at = latest created_at in the bucket (the most-complete run that saw it).
    A cluster keeps only events whose created_at is within freshness_min of
    run_at AND whose event_at is within its real-world TTL as-of run_at — exactly
    fetch_fresh_events()'s two filters, applied at run_at instead of now.
    """
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for e in events:
        ea = _parse(e.get("event_at"))
        if ea is None or not e.get("ticker"):
            continue
        b = ea.replace(second=0, microsecond=0)
        b = b.replace(minute=(b.minute // cluster_window_min) * cluster_window_min)
        groups[(e["ticker"], b.isoformat())].append(e)

    out: list[dict] = []
    for (ticker, bucket), evs in groups.items():
        created = [c for c in (_parse(e.get("created_at")) for e in evs) if c]
        if not created:
            continue
        run_at = max(created)
        floor = run_at - timedelta(minutes=freshness_min)
        visible = [
            e for e in evs
            if (_parse(e.get("created_at")) or run_at) >= floor
            and _event_within_real_ttl(e, run_at)
        ]
        if visible:
            out.append({"ticker": ticker, "bucket": bucket,
                        "run_at": run_at, "events": visible})
    return out


def candidate_rule_keys(events: list[dict],
                        horizons=_CANDIDATE_HORIZONS) -> set[str]:
    """Constituent rule_key::horizon cells — IDENTICAL construction to
    thesis_agent._record_candidates (set over events x horizons), so coverage is
    measured against the exact cells the 2.b gate will look up."""
    keys: set[str] = set()
    for h_keys in candidate_rule_keys_by_horizon(events, horizons).values():
        keys |= h_keys
    return keys


def candidate_rule_keys_by_horizon(events: list[dict],
                                   horizons=_CANDIDATE_HORIZONS) -> dict[int, set[str]]:
    """rule_key cells grouped by horizon — for the candidate×horizon metric
    (2.b gates per (candidate, horizon), so per-horizon gateability is what
    actually scopes PR-C)."""
    by_h: dict[int, set[str]] = {h: set() for h in horizons}
    for e in events:
        et = e.get("event_type")
        if not et:
            continue
        sub = e.get("event_subtype")
        for h in horizons:
            try:
                by_h[h].add(_rule_key.derive(et, sub, h))
            except Exception:  # noqa: BLE001
                continue
    return by_h


def classify_candidate(rule_keys: set[str], strict: set[str],
                       prov: set[str]) -> str:
    """'strict' if ANY constituent cell has n>=strict; else 'prov' if any has
    n>=prov; else 'thin'. A candidate is gateable if any of its cells is."""
    if rule_keys & strict:
        return "strict"
    if rule_keys & prov:
        return "prov"
    return "thin"


def coverage_verdict(strict_pct: float, prov_pct: float) -> str:
    """COMMIT/NARROW + inconclusive-framed sub-threshold (Codex finding #2:
    this is a no-lookahead baseline, not a lower bound — a sub-threshold result
    cannot conclude DEFER)."""
    gateable = strict_pct + prov_pct
    if gateable >= COMMIT_PCT:
        return "COMMIT (build full funnel)"
    if gateable >= NARROW_PCT:
        return "NARROW (high-frequency cells only)"
    return f"INCONCLUSIVE ({gateable:.0f}% baseline; live bonuses may raise — re-measure before DEFER)"


# ----------------------------------------------------------------------------
# I/O
# ----------------------------------------------------------------------------
def fetch_calibration() -> dict[str, int]:
    r = requests.get(f"{SUPABASE_URL}/rest/v1/stock_rule_calibration",
                     headers=HEADERS,
                     params={"select": "rule_key,n_observations", "limit": "2000"},
                     timeout=30)
    r.raise_for_status()
    rows = r.json()
    if len(rows) >= 2000:
        print("  WARNING: calibration fetch hit the 2000-row cap — some cells may "
              "be missing and wrongly counted thin. Page this query.", file=sys.stderr)
    return {row["rule_key"]: (row.get("n_observations") or 0) for row in rows}


def fetch_events_since(cutoff_iso: str) -> list[dict]:
    rows: list[dict] = []
    offset, page = 0, 1000
    while True:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/stock_normalized_events",
            headers={**HEADERS, "Range-Unit": "items", "Range": f"{offset}-{offset+page-1}"},
            params={
                "created_at": f"gte.{cutoff_iso}",
                "ticker": "not.is.null",
                "select": "id,event_type,event_subtype,ticker,event_at,created_at,"
                          "severity,source_table,parser_confidence,payload",
                "order": "created_at.desc",
            },
            timeout=60,
        )
        r.raise_for_status()
        batch = r.json()
        rows.extend(batch)
        if len(batch) < page:
            break
        offset += page
        if offset > 200_000:
            print("  (safety cap hit at 200k events)", file=sys.stderr)
            break
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--windows", default="30,90,180")
    ap.add_argument("--n-strict", type=int, default=100)
    ap.add_argument("--n-prov", type=int, default=50)
    args = ap.parse_args()

    if not SUPABASE_URL or not SUPABASE_KEY:
        print("ERROR: set SUPABASE_URL and SUPABASE_SERVICE_KEY.", file=sys.stderr)
        return 2

    windows = [int(w) for w in args.windows.split(",")]
    cal = fetch_calibration()
    strict = {k for k, n in cal.items() if n >= args.n_strict}
    prov = {k for k, n in cal.items() if n >= args.n_prov} - strict
    print(f"Calibration: {len(cal)} cells | gateable n>={args.n_strict}: {len(strict)} "
          f"| provisional n>={args.n_prov}: {len(prov)}", file=sys.stderr)

    now = datetime.now(timezone.utc)
    max_win = max(windows)
    print(f"Fetching events created in the last {max_win}d ...", file=sys.stderr)
    raw = fetch_events_since((now - timedelta(days=max_win)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    print(f"  {len(raw)} events fetched.\n", file=sys.stderr)

    print(f"{'window':>7} {'events':>8} {'clusters':>9} {'cands':>7} "
          f"{'strict%':>8} {'+prov%':>8} {'thin%':>7} {'emit_cands':>11}  verdict")
    print("─" * 100)

    for w in sorted(windows):
        cutoff = now - timedelta(days=w)
        evs = [e for e in raw if (_parse(e.get("created_at")) or now) >= cutoff]
        clusters = reconstruct_clusters(
            evs, freshness_min=FRESHNESS_WINDOW_MIN, cluster_window_min=CLUSTER_WINDOW_MIN)

        n_cand = n_emit_cand = 0
        cls = {"strict": 0, "prov": 0, "thin": 0}          # candidate-level
        xh = {"strict": 0, "prov": 0, "thin": 0}           # candidate×horizon
        for c in clusters:
            scored = score_cluster(
                c["events"], rule_calibration={}, bucket=c["bucket"],
                now=c["run_at"],
                agent_weights={}, sector_multipliers={}, ticker_sectors={},
                risk_off=False, wide_events=[], watchlist_map={}, news_fetch=None,
            )
            if float(scored.get("score") or 0) < THESIS_RECALL_FLOOR:
                continue
            n_cand += 1
            if scored.get("cluster_ok") and scored.get("action"):
                n_emit_cand += 1
            cls[classify_candidate(candidate_rule_keys(c["events"]), strict, prov)] += 1
            # Per-horizon: 2.b gates each (candidate, horizon) cell independently.
            for _h, keys in candidate_rule_keys_by_horizon(c["events"]).items():
                xh[classify_candidate(keys, strict, prov)] += 1

        if n_cand == 0:
            print(f"{w:>6}d {len(evs):>8} {len(clusters):>9} {0:>7}  (no candidates)")
            continue
        s_pct = 100.0 * cls["strict"] / n_cand
        p_pct = 100.0 * cls["prov"] / n_cand
        t_pct = 100.0 * cls["thin"] / n_cand
        n_xh = xh["strict"] + xh["prov"] + xh["thin"]
        xs_pct = 100.0 * xh["strict"] / n_xh if n_xh else 0.0
        xp_pct = 100.0 * xh["prov"] / n_xh if n_xh else 0.0
        print(f"{w:>6}d {len(evs):>8} {len(clusters):>9} {n_cand:>7} "
              f"{s_pct:>7.1f}% {p_pct:>7.1f}% {t_pct:>6.1f}% {n_emit_cand:>11}  "
              f"{coverage_verdict(s_pct, p_pct)}")
        print(f"{'':>7} {'':>8} {'':>9} {'(×horizon)':>7} "
              f"{xs_pct:>7.1f}% {xp_pct:>7.1f}% {100.0 - xs_pct - xp_pct:>6.1f}% "
              f"{'':>11}  {coverage_verdict(xs_pct, xp_pct)}  ← PR-C scope")

    print("\nNo-lookahead BASELINE (bonuses/news/risk-off disabled). COMMIT/NARROW = "
          "strong evidence; sub-threshold = inconclusive, not DEFER.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
