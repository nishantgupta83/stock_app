#!/usr/bin/env python3
"""measure_candidate_coverage.py — PR-A feasibility gate for the Layer 2
meta-labeling funnel (docs/design/layer2-metalabeling-funnel.md §7, §8).

THE QUESTION
------------
The 2.b precision gate can only confidently judge a (rule_key, horizon) cell
once it has enough closed trades (n>=100 strict, n>=50 provisional). Today only
19 of 97 cells reach n>=100. So: does the candidate flow actually LAND in those
gateable cells, or does it land in the thin long tail (where the gate just
fails-open to WATCH and buys nothing over a re-tuned floor)?

This script measures, over multiple lookback windows (volume can lie in any one
window), what fraction of real event/candidate flow falls into gateable cells —
and prints a commit/defer recommendation against the agreed thresholds:

    coverage >= 70%   -> build the full funnel
    coverage 60-70%   -> build a NARROW high-frequency-only precision gate
    coverage <  60%   -> DEFER the funnel; re-tune the floor + coarse payoff,
                         let calibration mature first

Coverage is computed at the canonical (rule_key::horizon) granularity via
agents/_rule_key.derive, the same mapping the pipeline uses — no ad-hoc guesses.

USAGE
-----
  export SUPABASE_URL=...   SUPABASE_SERVICE_KEY=...    # private shell
  python3 scripts/measure_candidate_coverage.py [--windows 30,90,180]
        [--n-strict 100] [--n-prov 50] [--horizons 1,7,15,30]

Read-only. No writes.
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agents"))
import _rule_key        # canonical rule_key derivation (same as the pipeline)
import _catalyst_policy  # canonical event role (catalyst/context/background)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
HEADERS = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}"}


def fetch_calibration() -> dict[str, int]:
    """rule_key -> n_observations for every cell."""
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/stock_rule_calibration",
        headers=HEADERS,
        params={"select": "rule_key,n_observations", "limit": "1000"},
        timeout=30,
    )
    r.raise_for_status()
    return {row["rule_key"]: (row.get("n_observations") or 0) for row in r.json()}


def fetch_events_since(cutoff_iso: str) -> list[dict]:
    """Page normalized events created since cutoff (created_at, not event_at —
    we want what LANDED, per CLAUDE.md critical rule #1)."""
    rows: list[dict] = []
    offset, page = 0, 1000
    while True:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/stock_normalized_events",
            headers={**HEADERS, "Range-Unit": "items", "Range": f"{offset}-{offset+page-1}"},
            params={
                "created_at": f"gte.{cutoff_iso}",
                "select": "ticker,event_type,event_subtype,created_at",
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
        if offset > 200_000:  # safety valve
            print("  (safety cap hit at 200k events)", file=sys.stderr)
            break
    return rows


def classify_event(ev: dict, horizons: list[int], strict: set[str], prov: set[str]) -> str:
    """Return 'strict' / 'prov' / 'thin' for an event based on how many of its
    horizon cells are gateable. 'strict' = >=1 horizon at n>=strict; 'prov' =
    >=1 horizon at n>=prov (but none strict); else 'thin'."""
    keys = [_rule_key.derive(ev.get("event_type") or "", ev.get("event_subtype"), h) for h in horizons]
    if any(k in strict for k in keys):
        return "strict"
    if any(k in prov for k in keys):
        return "prov"
    return "thin"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--windows", default="30,90,180")
    ap.add_argument("--n-strict", type=int, default=100)
    ap.add_argument("--n-prov", type=int, default=50)
    ap.add_argument("--horizons", default="1,7,15,30")
    args = ap.parse_args()

    if not SUPABASE_URL or not SUPABASE_KEY:
        print("ERROR: set SUPABASE_URL and SUPABASE_SERVICE_KEY.", file=sys.stderr)
        return 2

    windows = [int(w) for w in args.windows.split(",")]
    horizons = [int(h) for h in args.horizons.split(",")]

    cal = fetch_calibration()
    strict = {k for k, n in cal.items() if n >= args.n_strict}
    prov = {k for k, n in cal.items() if n >= args.n_prov} - strict
    print(f"Calibration: {len(cal)} cells | gateable n>={args.n_strict}: {len(strict)} "
          f"| provisional n>={args.n_prov}: {len(prov)}\n", file=sys.stderr)

    now = datetime.now(timezone.utc)
    max_win = max(windows)
    print(f"Fetching events for the last {max_win}d ...", file=sys.stderr)
    raw_events = fetch_events_since((now - timedelta(days=max_win)).strftime("%Y-%m-%dT%H:%M:%SZ"))

    # Candidate denominator = CATALYST-role events only. This is a FIRST-PASS
    # APPROXIMATION, not a cluster replay (see header + verdict caveat):
    #   - It correctly drops BACKGROUND events (Form 4 / 13F / institutional_*),
    #     which contribute 0 to score (PR1A) and were 80% of raw volume.
    #   - But CONTEXT-role events are NOT all zero — e.g. filing_13g scores 10
    #     context points (thesis_agent.py ~676), so a cluster of 13Gs can clear
    #     the floor. Excluding them here means coverage is measured over
    #     catalyst-attributed volume, which OVERSTATES gate-readiness vs. the
    #     true candidate-cluster population. Commit-grade requires a cluster
    #     replay (group by ticker+window, score, filter >=floor) — see header.
    role_counts = Counter(_catalyst_policy.policy_for(e.get("event_type") or "")["role"]
                          for e in raw_events)
    all_events = [e for e in raw_events
                  if _catalyst_policy.policy_for(e.get("event_type") or "")["role"] == "catalyst"]
    print(f"  {len(raw_events)} raw events → {len(all_events)} catalyst-role candidates "
          f"(excluded: { {k: v for k, v in role_counts.items() if k != 'catalyst'} }).\n",
          file=sys.stderr)

    print(f"{'window':>7} {'events':>8} {'strict%':>8} {'+prov%':>8} {'thin%':>7} "
          f"{'symbols':>8} {'busiest day':>12}")
    print("─" * 70)
    verdict_cov = None
    for w in sorted(windows):
        cutoff = now - timedelta(days=w)
        evs = [e for e in all_events
               if datetime.fromisoformat(e["created_at"].replace("Z", "+00:00")) >= cutoff]
        if not evs:
            print(f"{w:>6}d {'0':>8}  (no events)")
            continue
        cls = Counter(classify_event(e, horizons, strict, prov) for e in evs)
        n = len(evs)
        strict_pct = 100 * cls["strict"] / n
        prov_pct = 100 * (cls["strict"] + cls["prov"]) / n
        thin_pct = 100 * cls["thin"] / n
        symbols = len({e.get("ticker") for e in evs})
        per_day = Counter(e["created_at"][:10] for e in evs)
        busiest = per_day.most_common(1)[0] if per_day else ("-", 0)
        print(f"{w:>6}d {n:>8} {strict_pct:>7.1f}% {prov_pct:>7.1f}% {thin_pct:>6.1f}% "
              f"{symbols:>8} {busiest[0]} ({busiest[1]})")
        if w == max_win:
            verdict_cov = strict_pct  # use the longest, least cycle-biased window

    # ── Per-event-type coverage breakdown (where does volume concentrate?) ──
    print("\nTop event_type:subtype by volume (full window) — gateable?")
    bucket = defaultdict(lambda: {"n": 0, "cls": "thin"})
    for e in all_events:
        key = f"{e.get('event_type')}:{e.get('event_subtype') or ''}"
        bucket[key]["n"] += 1
        bucket[key]["cls"] = classify_event(e, horizons, strict, prov)
    for key, info in sorted(bucket.items(), key=lambda kv: -kv[1]["n"])[:15]:
        mark = {"strict": "✓ gateable", "prov": "~ provisional", "thin": "· thin"}[info["cls"]]
        print(f"  {key:<46} {info['n']:>7}  {mark}")

    # ── Verdict ─────────────────────────────────────────────────────────────
    print("\n" + "═" * 70)
    if verdict_cov is None:
        print("No data to judge.")
        return 0
    print(f"Catalyst EVENT-VOLUME gateable coverage over {max_win}d: {verdict_cov:.1f}%")
    print("  ⚠ This is event-volume, NOT a cluster replay. It overstates gate-")
    print("    readiness (excludes context-role 13G/13D that can still cluster ≥floor).")
    print("    Treat as a feasibility signal, not the commit-grade candidate number.")
    print()
    if verdict_cov >= 70:
        print("→ DIRECTION: gateable classes dominate catalyst volume → COMMIT to building")
        print("  the funnel ARCHITECTURE, but START NARROW: gate only the n>=100 high-")
        print("  frequency classes (8-K / news / earnings / clinical) for act/pass; fail-")
        print("  open 13G / truth-niche / tail to WATCH until their cells mature. (Codex.)")
    elif verdict_cov >= 60:
        print("→ Build a NARROW high-frequency-only precision gate; fail-open the tail.")
    else:
        print("→ DEFER the funnel. Re-tune the recall floor + add coarse payoff sanity;")
        print("  let thin cells mature (revisit when n>=100 coverage rises).")
    print("\nCOMMIT-GRADE NEXT STEP (before PR-C, the actual gate): replace event-count")
    print("with a CLUSTER REPLAY — group events by (ticker, window), score, keep clusters")
    print(">=floor, then report candidate-level AND candidate×horizon gateable coverage.")
    print("Caveat (Codex): volume can lie both ways — weigh the per-type breakdown and the")
    print("symbol/day concentration above, not the % alone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
