"""Robustness grid for the ai_humanoid dip rule (%B < 0.20, next open, fixed 60-session hold).

DIAGNOSTIC ONLY. The production rule is frozen. Reading rule, written before the first run:

  * The grid can only DEMOTE the default cell (%B<0.20, 60 sessions, next open); it never
    promotes another cell. Picking the best of 60 cells after the fact is the forking-paths
    problem this report exists to expose.
  * Default is DEMOTED if its edge vs the same-universe null is <= 0, or if 2+ of its 4
    neighbours (%B 0.15 / 0.25 at 60 sessions; 40 / 90 sessions at 0.20) have edge <= 0 or
    fewer than MIN_EPISODES non-overlapping episodes -- i.e. an isolated spike, not a plateau.
  * The index check (QQQ / SPY / XLK since 1999) uses no hand-picked universe. The rule
    "transfers" only if the default cell's edge is > 0 on at least 2 of the 3 indices; an
    index that failed to download counts against it.
  * The next_open_cost cells show the NET mean; their edge equals next_open's by construction
    (the null pays the same cost), so they inform the level, not the verdict.

Edge = mean forward return of dip entries minus the mean forward return of EVERY session for
the same names over the same span (the unconditional null). Daily entries overlap, so each
cell also reports non-overlapping episodes; judge a cell on those, not on raw n.

    python3 scripts/quarterly_rotation/robustness.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.quarterly_rotation.experiment import CALENDAR_TICKER, UNIVERSE   # noqa: E402

THRESHOLDS = (0.10, 0.15, 0.20, 0.25, 0.30)
HOLDS = (20, 40, 60, 90)
ENTRIES = ("next_open", "next_close", "next_open_cost")
COST = 0.0020                      # round trip, same as qr_s1
DEFAULT = (0.20, 60, "next_open")
MIN_EPISODES = 10
INDICES = ("QQQ", "SPY", "XLK")


def pct_b_series(closes: list[float], n: int = 20, k: float = 2.0) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    for i in range(n - 1, len(closes)):
        w = closes[i - n + 1: i + 1]
        m = sum(w) / n
        sd = (sum((x - m) ** 2 for x in w) / n) ** 0.5
        out[i] = ((w[-1] - (m - k * sd)) / (2 * k * sd)) if sd > 0 else None
    return out


def _fwd(opens, closes, i, h, entry):
    if i + h >= len(closes):
        return None
    base = closes[i + 1] if entry == "next_close" else opens[i + 1]
    r = closes[i + h] / base - 1
    return r - COST if entry == "next_open_cost" else r


def cell(series: dict[str, dict], th: float, h: int, entry: str) -> dict:
    """series: ticker -> {"open","close","pb","date"} lists. Pools all names."""
    sig, allr, episodes = [], [], 0
    for s in series.values():
        nxt = 0
        for i, pb in enumerate(s["pb"]):
            if pb is None:
                continue
            r = _fwd(s["open"], s["close"], i, h, entry)
            if r is None:
                continue
            allr.append(r)
            if pb < th:
                sig.append(r)
                if i >= nxt:
                    episodes += 1
                    nxt = i + 1 + h
    if not sig or not allr:
        return {"n": len(sig), "episodes": episodes, "mean": None, "null": None, "edge": None}
    m, nl = sum(sig) / len(sig), sum(allr) / len(allr)
    return {"n": len(sig), "episodes": episodes, "mean": m, "null": nl, "edge": m - nl,
            "hit": sum(1 for r in sig if r > 0) / len(sig)}


def split(series: dict[str, dict], th: float, h: int, entry: str, key) -> dict:
    """Edge per group, where key(ticker, i) names the group of session i (or None to skip)."""
    sig: dict = {}
    allr: dict = {}
    for t, s in series.items():
        for i, pb in enumerate(s["pb"]):
            if pb is None:
                continue
            r = _fwd(s["open"], s["close"], i, h, entry)
            g = key(t, i)
            if r is None or g is None:
                continue
            allr.setdefault(g, []).append(r)
            if pb < th:
                sig.setdefault(g, []).append(r)
    out = {}
    for g in sorted(allr):
        a, sg = allr[g], sig.get(g, [])
        out[str(g)] = {"n": len(sg), "edge": (sum(sg) / len(sg) - sum(a) / len(a)) if sg else None}
    return out


def verdict(grid: dict) -> dict:
    d = grid[f"{DEFAULT[0]:.2f}|{DEFAULT[1]}|{DEFAULT[2]}"]["edge"]
    nb_keys = [f"0.15|60|next_open", f"0.25|60|next_open", f"0.20|40|next_open", f"0.20|90|next_open"]
    nb = {k: grid[k]["edge"] for k in nb_keys}
    bad = sum(1 for k, v in nb.items()
              if v is None or v <= 0 or grid[k].get("episodes", MIN_EPISODES) < MIN_EPISODES)
    demoted = d is None or d <= 0 or bad >= 2
    return {"default_edge": d, "neighbours": nb, "neighbours_nonpositive": bad, "demoted": demoted}


def run(series: dict[str, dict], regime_up: set | None = None,
        regime_known: set | None = None) -> dict:
    grid = {f"{th:.2f}|{h}|{e}": cell(series, th, h, e) for th in THRESHOLDS for h in HOLDS for e in ENTRIES}
    th, h, e = DEFAULT
    out = {"grid": grid, "verdict": verdict(grid),
           "by_year": split(series, th, h, e, lambda t, i: series[t]["date"][i][:4])}
    if regime_up is not None:
        out["by_regime"] = split(series, th, h, e,
                                 lambda t, i: (None if (regime_known is not None and series[t]["date"][i] not in regime_known)
                                               else "qqq_above_200" if series[t]["date"][i] in regime_up
                                               else "qqq_below_200"))
    return out


def _download(tickers, period):
    import warnings
    warnings.filterwarnings("ignore")
    import yfinance as yf
    raw = yf.download(list(tickers), period=period, interval="1d", auto_adjust=True,
                      group_by="ticker", progress=False)
    out = {}
    for t in tickers:
        try:
            df = raw[t][["Open", "Close"]].dropna()
        except KeyError:
            continue
        if df.empty:
            continue
        closes = [float(x) for x in df["Close"]]
        out[t] = {"date": [i.strftime("%Y-%m-%d") for i in df.index],
                  "open": [float(x) for x in df["Open"]], "close": closes,
                  "pb": pct_b_series(closes)}
    return out


def main() -> int:
    ai = _download(list(UNIVERSE) + [CALENDAR_TICKER], "10y")
    q = ai.pop(CALENDAR_TICKER)
    up, known = set(), set()
    for i in range(199, len(q["close"])):       # no 200-day reading before 199 -> not classified
        known.add(q["date"][i])
        if q["close"][i] > sum(q["close"][i - 199: i + 1]) / 200:
            up.add(q["date"][i])
    res = {"universe": "ai_stocks_10y", "names": sorted(ai), "rule": "see module docstring",
           "ai": run(ai, up, known)}
    idx = _download(INDICES, "max")
    res["indices"] = {}
    for t, s in idx.items():
        s = {k: v for k, v in s.items()}
        keep = [i for i, d in enumerate(s["date"]) if d >= "1999-01-01"]
        s = {k: [v[i] for i in keep] for k, v in s.items()}
        r = run({t: s})
        res["indices"][t] = {"span": [s["date"][0], s["date"][-1]], "verdict": r["verdict"],
                             "default": r["grid"]["0.20|60|next_open"]}
    good = sum(1 for v in res["indices"].values()
               if v["default"]["edge"] is not None and v["default"]["edge"] > 0)
    res["indices_missing"] = sorted(set(INDICES) - set(res["indices"]))
    res["indices_transfer"] = good >= 2
    out = Path(__file__).resolve().parent / "results" / "robustness_v1.json"
    out.write_text(json.dumps(res, indent=1, sort_keys=True))
    v = res["ai"]["verdict"]
    def pts(x):
        return "n/a" if x is None else "%+.2f pts" % (x * 100)
    print("AI default edge %s, weak neighbours: %d/4, demoted: %s"
          % (pts(v["default_edge"]), v["neighbours_nonpositive"], v["demoted"]))
    for t, x in res["indices"].items():
        print(t, x["span"], "edge", pts(x["default"]["edge"]), "episodes", x["default"]["episodes"])
    print("indices transfer:", res["indices_transfer"], "->", out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
