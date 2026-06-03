#!/usr/bin/env python3
"""Historical learning replay (12 months: 2025-06 → 2026-05).

Reads all closed h7d paper trades in the window, simulates the
realistic_loop_agent's $5K bankroll / 5-concurrent / $1K-per-position
ledger event-by-event in chronological order, snapshots state at each
month boundary, and emits one DDMMYYYY_learning_doc.md per month under
docs/learning/.

Why h7d:
  Live realistic_loop reads stock_trade_setups (Layer 3) but those only
  exist since 2026-05-18. For 12-month replay we need the underlying
  outcome ledger — that's stock_event_paper_trades, opened in 4 horizons
  per event (h1/h7/h15/h30). h7d is the highest-PF horizon for the
  dominant 8K/news rule_keys per the sector analysis, and matches a
  realistic "alert + 1 week" trader holding period.

Output: per-month learning doc gives a future agent a time-indexed
snapshot of (a) what the realistic loop would have done with $5K, (b)
which rules were accumulating maturity, (c) what was working / failing.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone


SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
HEADERS = {
    "apikey": SUPABASE_SERVICE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
}

# Window
START = "2025-06-01"
END   = "2026-05-31"

# Simulation parameters — mirror agents/realistic_loop_agent.py
BANKROLL       = 5000.0
PER_POSITION   = 1000.0
MAX_CONCURRENT = 5
HORIZON_DAYS   = 7
SLIPPAGE_BPS   = 5.0  # per side; already netted in stock_event_paper_trades.realized_return


def paginate(table: str, params: dict[str, str], page: int = 1000) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while True:
        q = dict(params)
        q["limit"], q["offset"] = str(page), str(offset)
        qs = urllib.parse.urlencode(q, safe=".,:*=&")
        req = urllib.request.Request(f"{SUPABASE_URL}/rest/v1/{table}?{qs}", headers=HEADERS)
        with urllib.request.urlopen(req, timeout=60) as r:
            chunk = json.loads(r.read())
        if not chunk:
            break
        rows.extend(chunk)
        if len(chunk) < page:
            break
        offset += page
    return rows


def fetch_trades() -> list[dict]:
    print("Fetching closed h7d paper trades…", file=sys.stderr)
    rows = paginate(
        "stock_event_paper_trades",
        {
            "status":       "eq.closed",
            "horizon_days": "eq.7",
            "entry_at":     f"gte.{START}T00:00:00Z",
            "and":          f"(entry_at.lte.{END}T23:59:59Z)",
            "select":       "entry_at,exit_at,ticker,direction,realized_return,"
                            "correct,rule_key,event_type,event_subtype",
            "order":        "entry_at.asc",
        },
    )
    print(f"  fetched {len(rows)} trades", file=sys.stderr)
    return rows


def last_day_of_month(ym: str) -> int:
    y, m = (int(x) for x in ym.split("-"))
    if m == 12:
        return 31
    return (date(y, m + 1, 1) - timedelta(days=1)).day


def fmt_money(v: float) -> str:
    sign = "-" if v < 0 else ""
    return f"{sign}${abs(v):,.2f}"


def fmt_pct(v: float) -> str:
    return f"{v*100:+.2f}%"


def simulate_and_snapshot(trades: list[dict]) -> tuple[dict, dict, dict, dict]:
    """Replay the trades chronologically.

    Returns
      end_of_month[ym]    — loop state at end of YM
      month_activity[ym]  — {opens, closes, wins, losses, pnl} for YM
      rule_snap[ym]       — cumulative rule_key stats AS OF YM end
      ticker_snap[ym]     — cumulative ticker stats AS OF YM end
    """
    # Build event timeline. One open event + one close event per trade.
    events: list[tuple[str, str, dict, int]] = []
    for i, t in enumerate(trades):
        if t.get("entry_at"):
            events.append((t["entry_at"], "open", t, i))
        if t.get("exit_at"):
            events.append((t["exit_at"], "close", t, i))
    events.sort(key=lambda e: e[0])

    cash = BANKROLL
    positions: dict[int, dict] = {}
    cum_pnl = 0.0
    hwm = 0.0
    max_dd = 0.0
    wins = losses = 0
    total_opens = total_closes = 0

    end_of_month: dict[str, dict] = {}
    month_activity: dict[str, dict] = defaultdict(
        lambda: {"opens": 0, "closes": 0, "wins": 0, "losses": 0, "pnl": 0.0}
    )
    rule_n: dict[str, int] = defaultdict(int)
    rule_wins: dict[str, int] = defaultdict(int)
    rule_pnl: dict[str, float] = defaultdict(float)
    ticker_n: dict[str, int] = defaultdict(int)
    ticker_wins: dict[str, int] = defaultdict(int)
    ticker_pnl: dict[str, float] = defaultdict(float)

    rule_snap: dict[str, dict] = {}
    ticker_snap: dict[str, dict] = {}

    current_month: str | None = None

    for ts, action, t, tid in events:
        ym = ts[:7]
        if current_month is None:
            current_month = ym
        if ym != current_month:
            # Snapshot at end of previous month
            end_of_month[current_month] = {
                "cash":           round(cash, 2),
                "positions_open": len(positions),
                "cum_pnl":        round(cum_pnl, 2),
                "hwm":            round(hwm, 2),
                "max_dd":         round(max_dd, 2),
                "wins":           wins,
                "losses":         losses,
                "total_opens":    total_opens,
                "total_closes":   total_closes,
            }
            rule_snap[current_month] = {
                rk: {
                    "n":    rule_n[rk],
                    "wins": rule_wins[rk],
                    "pnl":  round(rule_pnl[rk], 2),
                }
                for rk in rule_n
            }
            ticker_snap[current_month] = {
                tk: {
                    "n":    ticker_n[tk],
                    "wins": ticker_wins[tk],
                    "pnl":  round(ticker_pnl[tk], 2),
                }
                for tk in ticker_n
            }
            current_month = ym

        if action == "open":
            if len(positions) >= MAX_CONCURRENT or cash < PER_POSITION:
                continue
            positions[tid] = t
            cash -= PER_POSITION
            total_opens += 1
            month_activity[ym]["opens"] += 1
        else:  # close
            if tid not in positions:
                continue
            r = float(t.get("realized_return") or 0)
            pnl = r * PER_POSITION
            cum_pnl += pnl
            cash += PER_POSITION
            if r > 0:
                wins += 1
                month_activity[ym]["wins"] += 1
            else:
                losses += 1
                month_activity[ym]["losses"] += 1
            month_activity[ym]["pnl"] += pnl
            month_activity[ym]["closes"] += 1
            total_closes += 1
            hwm = max(hwm, cum_pnl)
            max_dd = max(max_dd, hwm - cum_pnl)
            rk = t.get("rule_key") or t.get("event_type") or "unknown"
            rule_n[rk] += 1
            if r > 0: rule_wins[rk] += 1
            rule_pnl[rk] += pnl
            tk = t.get("ticker") or "unknown"
            ticker_n[tk] += 1
            if r > 0: ticker_wins[tk] += 1
            ticker_pnl[tk] += pnl
            del positions[tid]

    # Final snapshot at end of last month
    if current_month:
        end_of_month[current_month] = {
            "cash":           round(cash, 2),
            "positions_open": len(positions),
            "cum_pnl":        round(cum_pnl, 2),
            "hwm":            round(hwm, 2),
            "max_dd":         round(max_dd, 2),
            "wins":           wins,
            "losses":         losses,
            "total_opens":    total_opens,
            "total_closes":   total_closes,
        }
        rule_snap[current_month] = {
            rk: {"n": rule_n[rk], "wins": rule_wins[rk], "pnl": round(rule_pnl[rk], 2)}
            for rk in rule_n
        }
        ticker_snap[current_month] = {
            tk: {"n": ticker_n[tk], "wins": ticker_wins[tk], "pnl": round(ticker_pnl[tk], 2)}
            for tk in ticker_n
        }

    return end_of_month, month_activity, rule_snap, ticker_snap


def write_month_doc(
    out_dir: str,
    ym: str,
    state: dict,
    monthly: dict,
    rule_cum: dict,
    ticker_cum: dict,
    prev_state: dict | None,
) -> str:
    yr, mo = ym.split("-")
    dd = last_day_of_month(ym)
    fname = f"{dd:02d}{mo}{yr}_learning_doc.md"
    path = os.path.join(out_dir, fname)

    # Compute deltas
    win_rate_cum = state["wins"] / max(1, state["wins"] + state["losses"])
    return_pct_cum = state["cum_pnl"] / BANKROLL
    dd_pct = state["max_dd"] / BANKROLL

    # Per-month
    m_wr = monthly["wins"] / max(1, monthly["wins"] + monthly["losses"])
    m_pnl = monthly["pnl"]

    # Top/bottom rules (cumulative through this month)
    rules_sorted_by_pnl = sorted(
        ((rk, v) for rk, v in rule_cum.items() if v["n"] >= 5),
        key=lambda x: -x[1]["pnl"],
    )
    top_rules = rules_sorted_by_pnl[:5]
    bot_rules = rules_sorted_by_pnl[-5:][::-1] if len(rules_sorted_by_pnl) >= 5 else []

    # Top/bottom tickers
    tickers_sorted_by_pnl = sorted(
        ((tk, v) for tk, v in ticker_cum.items() if v["n"] >= 3),
        key=lambda x: -x[1]["pnl"],
    )
    top_tickers = tickers_sorted_by_pnl[:5]
    bot_tickers = tickers_sorted_by_pnl[-5:][::-1] if len(tickers_sorted_by_pnl) >= 5 else []

    # Maturity gate progress: rules approaching n>=30
    near_mature = sorted(
        ((rk, v) for rk, v in rule_cum.items() if 20 <= v["n"] < 30),
        key=lambda x: -x[1]["n"],
    )[:5]
    mature = sorted(
        ((rk, v) for rk, v in rule_cum.items() if v["n"] >= 30),
        key=lambda x: -x[1]["n"],
    )[:10]

    # Generate doc
    md = []
    md.append(f"# Learning snapshot — end of {ym}")
    md.append("")
    md.append(f"_Generated {datetime.now(timezone.utc).date().isoformat()} from a "
              f"historical replay of stock_event_paper_trades through {ym}-{dd:02d}._")
    md.append("")
    md.append("Window: realistic_loop_agent semantics applied retroactively to all "
              "closed h7d paper trades. $5,000 bankroll, $1,000 per position, "
              "max 5 concurrent, cash recycled on close, no leverage. Realized "
              "returns already net of 10 bps round-trip slippage (event_paper_agent "
              "convention).")
    md.append("")

    md.append("## Hypothetical $5K loop state at month-end")
    md.append("")
    md.append("| Metric | Value |")
    md.append("|---|---|")
    md.append(f"| Cash available | {fmt_money(state['cash'])} |")
    md.append(f"| Positions open | {state['positions_open']} / {MAX_CONCURRENT} |")
    md.append(f"| Cumulative PnL | {fmt_money(state['cum_pnl'])} |")
    md.append(f"| Return % (vs $5K base) | {fmt_pct(return_pct_cum)} |")
    md.append(f"| High-water mark | {fmt_money(state['hwm'])} |")
    md.append(f"| Max drawdown | {fmt_money(state['max_dd'])} ({fmt_pct(dd_pct)}) |")
    md.append(f"| Closed trades | {state['total_closes']} ({state['wins']}W / {state['losses']}L, win-rate {win_rate_cum:.1%}) |")
    md.append(f"| Avg PnL per closed trade | {fmt_money(state['cum_pnl']/max(1,state['total_closes']))} |")
    md.append("")

    if prev_state:
        delta_pnl = state["cum_pnl"] - prev_state["cum_pnl"]
        delta_closes = state["total_closes"] - prev_state["total_closes"]
        md.append("## Month-over-month delta")
        md.append("")
        md.append("| Metric | This month | Cumulative |")
        md.append("|---|---|---|")
        md.append(f"| PnL | {fmt_money(m_pnl)} | {fmt_money(state['cum_pnl'])} |")
        md.append(f"| Opens | {monthly['opens']} | {state['total_opens']} |")
        md.append(f"| Closes | {monthly['closes']} | {state['total_closes']} |")
        md.append(f"| Win-rate (in month) | {m_wr:.1%} | {win_rate_cum:.1%} |")
        md.append("")

    # Top rules
    if top_rules:
        md.append("## Top rule_keys by cumulative PnL (n ≥ 5)")
        md.append("")
        md.append("| rule_key | n | wins | win-rate | cumulative PnL |")
        md.append("|---|---|---|---|---|")
        for rk, v in top_rules:
            wr = v["wins"] / max(1, v["n"])
            md.append(f"| `{rk}` | {v['n']} | {v['wins']} | {wr:.1%} | {fmt_money(v['pnl'])} |")
        md.append("")

    if bot_rules:
        md.append("## Worst rule_keys by cumulative PnL (n ≥ 5)")
        md.append("")
        md.append("| rule_key | n | wins | win-rate | cumulative PnL |")
        md.append("|---|---|---|---|---|")
        for rk, v in bot_rules:
            wr = v["wins"] / max(1, v["n"])
            md.append(f"| `{rk}` | {v['n']} | {v['wins']} | {wr:.1%} | {fmt_money(v['pnl'])} |")
        md.append("")

    # Maturity progress
    if mature:
        md.append(f"## Mature rules (n ≥ 30) as of {ym}-{dd:02d}")
        md.append("")
        md.append("| rule_key | n | win-rate | cumulative PnL |")
        md.append("|---|---|---|---|")
        for rk, v in mature:
            wr = v["wins"] / max(1, v["n"])
            md.append(f"| `{rk}` | {v['n']} | {wr:.1%} | {fmt_money(v['pnl'])} |")
        md.append("")
    if near_mature:
        md.append("## Rules approaching maturity (20 ≤ n < 30)")
        md.append("")
        md.append("| rule_key | n | wins | win-rate |")
        md.append("|---|---|---|---|")
        for rk, v in near_mature:
            wr = v["wins"] / max(1, v["n"])
            md.append(f"| `{rk}` | {v['n']} | {v['wins']} | {wr:.1%} |")
        md.append("")

    # Top/bottom tickers
    if top_tickers:
        md.append("## Top tickers by cumulative PnL (n ≥ 3)")
        md.append("")
        md.append("| ticker | n | wins | win-rate | cumulative PnL |")
        md.append("|---|---|---|---|---|")
        for tk, v in top_tickers:
            wr = v["wins"] / max(1, v["n"])
            md.append(f"| `{tk}` | {v['n']} | {v['wins']} | {wr:.1%} | {fmt_money(v['pnl'])} |")
        md.append("")
    if bot_tickers:
        md.append("## Worst tickers by cumulative PnL (n ≥ 3)")
        md.append("")
        md.append("| ticker | n | wins | win-rate | cumulative PnL |")
        md.append("|---|---|---|---|---|")
        for tk, v in bot_tickers:
            wr = v["wins"] / max(1, v["n"])
            md.append(f"| `{tk}` | {v['n']} | {v['wins']} | {wr:.1%} | {fmt_money(v['pnl'])} |")
        md.append("")

    md.append("## How to read this doc")
    md.append("")
    md.append("This is a *historical replay*, not a live trading record. It answers "
              "the question: *had the realistic_loop_agent been active with a $5K "
              "bankroll and the discipline we ship today, what would it have made "
              "by " + ym + "?* Numbers are bounded by the corpus available — "
              "h7d horizon, severity ≥ 2 events, ~150-ticker universe. They do not "
              "reflect intraday-spike alerts or the maturity-gated BUY/SELL "
              "vocabulary (which never triggered during this period — no rule "
              "reached the 90%/n≥30 adult gate).")
    md.append("")
    md.append("Useful for a future agent reviewing time-indexed learning: load this "
              "and the prior month to see drift in rule_n, win-rate, and which "
              "tickers were accumulating edge or noise.")
    md.append("")

    with open(path, "w") as f:
        f.write("\n".join(md))
    return path


def write_index(out_dir: str, months: list[str], end_of_month: dict) -> str:
    path = os.path.join(out_dir, "README.md")
    md = []
    md.append("# Historical learning docs")
    md.append("")
    md.append("Generated by `scripts/historical_learning_replay.py`. Each doc is a "
              "snapshot of what the realistic_loop_agent would have produced by "
              "end-of-month if it had been active with the current $5K / 5-concurrent "
              "discipline, replayed against closed h7d paper trades from the corpus.")
    md.append("")
    md.append("These docs are intended for time-indexed re-learning: future agents "
              "should read them in order, not re-run the underlying scripts, to "
              "understand how the calibration evolved.")
    md.append("")
    md.append("## Index")
    md.append("")
    md.append("| Month end | Cum PnL | Return % | Max DD | Closed | Win-rate | Doc |")
    md.append("|---|---|---|---|---|---|---|")
    for ym in months:
        s = end_of_month[ym]
        wr = s["wins"] / max(1, s["wins"] + s["losses"])
        ret = s["cum_pnl"] / BANKROLL
        dd = last_day_of_month(ym)
        yr, mo = ym.split("-")
        fname = f"{dd:02d}{mo}{yr}_learning_doc.md"
        md.append(
            f"| {ym}-{dd:02d} | {fmt_money(s['cum_pnl'])} | {fmt_pct(ret)} | "
            f"{fmt_money(s['max_dd'])} | {s['total_closes']} | {wr:.1%} | "
            f"[`{fname}`]({fname}) |"
        )
    md.append("")
    md.append("## Method")
    md.append("")
    md.append(f"- Window: {START} to {END}")
    md.append(f"- Source: `stock_event_paper_trades` where `status='closed' AND horizon_days={HORIZON_DAYS}`")
    md.append(f"- Bankroll: ${BANKROLL:,.0f}, $${PER_POSITION:,.0f} per position, max {MAX_CONCURRENT} concurrent")
    md.append("- Slippage: already netted in `realized_return` (10 bps round-trip)")
    md.append(f"- Horizon: {HORIZON_DAYS}d — chosen because h7d shows highest profit_factor")
    md.append("  for the dominant 8K/news rule_keys per the sector audit, and matches")
    md.append("  a realistic 'alert + 1 week' trader holding period.")
    md.append("")
    md.append("**This is not advice and not a backtest of a real strategy.** It's a")
    md.append("counterfactual: 'what if the $5K shadow portfolio had been live during")
    md.append("this entire window?'")
    md.append("")

    with open(path, "w") as f:
        f.write("\n".join(md))
    return path


def main() -> int:
    out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "docs", "learning")
    os.makedirs(out_dir, exist_ok=True)

    trades = fetch_trades()
    if not trades:
        print("No trades — nothing to do.", file=sys.stderr)
        return 1

    end_of_month, monthly, rule_snap, ticker_snap = simulate_and_snapshot(trades)
    months = sorted(end_of_month.keys())

    print(f"Generating monthly docs for {len(months)} months…", file=sys.stderr)
    written: list[str] = []
    prev_state = None
    for ym in months:
        path = write_month_doc(
            out_dir, ym,
            end_of_month[ym],
            monthly[ym],
            rule_snap[ym],
            ticker_snap[ym],
            prev_state,
        )
        written.append(path)
        prev_state = end_of_month[ym]

    idx = write_index(out_dir, months, end_of_month)
    print(f"  wrote {len(written)} month docs + 1 index → {out_dir}", file=sys.stderr)

    # Terminal summary
    last = months[-1]
    s = end_of_month[last]
    wr = s["wins"] / max(1, s["wins"] + s["losses"])
    print()
    print(f"=== Replay summary (end of {last}) ===")
    print(f"  Cumulative PnL    : {fmt_money(s['cum_pnl'])}")
    print(f"  Return on $5K     : {fmt_pct(s['cum_pnl']/BANKROLL)}")
    print(f"  Max drawdown      : {fmt_money(s['max_dd'])}")
    print(f"  Closed trades     : {s['total_closes']}  ({s['wins']}W / {s['losses']}L, win-rate {wr:.1%})")
    print(f"  Index             : {idx}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
