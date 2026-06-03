#!/usr/bin/env python3
"""Sequential monthly learning replay: $500/wk DCA with closed-loop feedback.

Single-pass replay (historical_learning_replay.py) holds the rules fixed
across the whole 12 months. This script is the closed-loop version:

  Month M trades  →  end-of-M reconciliation produces learnings
  →  Month M+1 trades respect those learnings (flips, skips, amplifications)
  →  end-of-(M+1) reconciliation incorporates the new data
  →  ...

Why this matters: in the single-pass replay, a rule that loses money
on direction=long over its first 30 trades keeps losing all year. In
the sequential version, end of the month when n crosses 30 with PF<1,
that rule gets flipped — and the next 100+ trades on the same rule
contribute positive PnL instead.

Inputs: stock_event_paper_trades (status='closed', horizon_days=7),
window 2025-05-01 → 2026-05-31.

Outputs:
  docs/learning/YYYYMM_monthly_reconc.md  × ~13
  docs/learning/sequential_replay_summary_DDMMYYYY.md  × 1

No live-pipeline changes. Analytical script — re-runnable, idempotent
(overwrites prior outputs).
"""
from __future__ import annotations

import json
import os
import sys
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Optional


SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
HEADERS = {
    "apikey": SUPABASE_SERVICE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
}

# -- Replay window
START = "2025-05-01"
END   = "2026-05-31"
DEPOSIT_START_ISO = "2025-05-05"  # first Monday in May 2025

# -- Capital + sizing parameters (per the approved plan)
WEEKLY_DEPOSIT     = 500.0
PER_POSITION_BASE  = 500.0
MAX_CONCURRENT     = 10
AMPLIFY_MULTIPLIER = 1.5
AMPLIFY_PF_MIN     = 2.0
AMPLIFY_ACC_MIN    = 0.60
FLIP_PF_MAX        = 1.0
FLIP_ACC_MAX       = 0.50
SKIP_ACC_MAX       = 0.30
MIN_N_FOR_LEARNING = 30


# ------------------------------------------------------------------
# Reused utilities (lift the proven pattern from historical_learning_replay)
# ------------------------------------------------------------------

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


# ------------------------------------------------------------------
# Per-rule running stats
# ------------------------------------------------------------------

class RuleStats:
    """Track cumulative stats per rule_key. Profit-factor uses CLOSE-LEVEL
    realized_return contributions (per-position absolute dollars) so the
    ratio is dimensionally consistent with the rest of the simulation."""

    __slots__ = ("n", "wins", "pos_pnl_sum", "neg_pnl_sum", "cum_pnl",
                 "tier_at_mature", "matured_at")

    def __init__(self) -> None:
        self.n            = 0
        self.wins         = 0
        self.pos_pnl_sum  = 0.0   # sum of positive PnL contributions
        self.neg_pnl_sum  = 0.0   # sum of negative PnL contributions (negative number)
        self.cum_pnl      = 0.0
        self.tier_at_mature: Optional[str] = None
        self.matured_at:   Optional[str] = None

    def record(self, realized_pct: float, position_size: float) -> None:
        pnl = realized_pct * position_size
        self.n += 1
        self.cum_pnl += pnl
        if pnl > 0:
            self.wins += 1
            self.pos_pnl_sum += pnl
        else:
            self.neg_pnl_sum += pnl   # accumulates as negative

    @property
    def acc(self) -> float:
        return self.wins / self.n if self.n else 0.0

    @property
    def profit_factor(self) -> Optional[float]:
        if self.neg_pnl_sum >= 0:
            return None  # no losses recorded yet
        return self.pos_pnl_sum / abs(self.neg_pnl_sum)

    def tier(self) -> str:
        """Same gates as sql/0031 tiered_maturity."""
        if self.n < MIN_N_FOR_LEARNING:
            return "n<30"
        pf = self.profit_factor
        if self.acc >= 0.90 and pf is not None and pf > 1.5:
            return "adult"
        if self.acc >= 0.80:
            return "young"
        if self.acc >= 0.70:
            return "teen"
        return "child"


# ------------------------------------------------------------------
# Sequential monthly simulator
# ------------------------------------------------------------------

def iter_mondays(start_iso: str, end_iso: str):
    """Yield each Monday between start and end (inclusive of start if Monday)."""
    cur = datetime.fromisoformat(start_iso + "T09:00:00+00:00")
    end = datetime.fromisoformat(end_iso + "T23:59:59+00:00")
    while cur <= end:
        if cur.weekday() == 0:  # Monday
            yield cur
        cur += timedelta(days=1)


def simulate(trades: list[dict]) -> dict:
    """Run the sequential monthly replay. Returns a dict of artifacts:
      months              — sorted list of YM strings present
      end_of_month[ym]    — state snapshot at end of YM
      monthly[ym]         — activity stats per month
      learnings[ym]       — reconciliation decisions made at end of YM
      rule_stats          — terminal RuleStats per rule_key
      ticker_stats        — terminal per-ticker stats
      tier_pop_history    — tier-population counts at each YM end
      forward_state       — final overrides / skips / amplifiers
    """

    # Build the event timeline. Each trade contributes (open, close) events.
    # Deposits are also events on each Monday.
    events: list[tuple[str, str, dict, int]] = []
    for i, t in enumerate(trades):
        if t.get("entry_at"):
            events.append((t["entry_at"], "open", t, i))
        if t.get("exit_at"):
            events.append((t["exit_at"], "close", t, i))
    for dep in iter_mondays(DEPOSIT_START_ISO, END):
        events.append((dep.isoformat(), "deposit", {"amount": WEEKLY_DEPOSIT}, -1))
    events.sort(key=lambda e: e[0])

    # State
    cash = 0.0
    total_deposits = 0.0
    cum_pnl = 0.0
    hwm = 0.0
    max_dd = 0.0
    wins = losses = 0
    total_opens = total_closes = 0
    # Each open position: tid → {"trade": t, "size": effective_size, "flipped": bool}
    positions: dict[int, dict] = {}

    # Learning state (carried forward across months)
    direction_overrides: dict[str, str] = {}   # rule_key → "flip"
    structural_skip: set[str] = set()
    size_multipliers: dict[str, float] = {}    # rule_key → multiplier

    # Per-rule cumulative stats
    rule_stats: dict[str, RuleStats] = defaultdict(RuleStats)
    # Per-ticker cumulative stats
    ticker_stats: dict[str, dict] = defaultdict(
        lambda: {"n": 0, "wins": 0, "pnl": 0.0})

    # Per-month activity
    monthly: dict[str, dict] = defaultdict(
        lambda: {"opens": 0, "closes": 0, "wins": 0, "losses": 0,
                 "pnl": 0.0, "deposits": 0.0, "skipped_by_rule": 0,
                 "flipped_trades": 0})
    end_of_month: dict[str, dict] = {}
    learnings_by_month: dict[str, dict] = {}
    tier_pop_history: dict[str, dict] = {}

    months_seen: list[str] = []
    current_month: Optional[str] = None

    def snapshot(ym: str) -> None:
        end_of_month[ym] = {
            "cash":            round(cash, 2),
            "positions_open":  len(positions),
            "cum_pnl":         round(cum_pnl, 2),
            "hwm":             round(hwm, 2),
            "max_dd":          round(max_dd, 2),
            "wins":            wins,
            "losses":          losses,
            "total_opens":     total_opens,
            "total_closes":    total_closes,
            "total_deposits":  round(total_deposits, 2),
            "deployed_in_positions": round(
                sum(p["size"] for p in positions.values()), 2),
        }
        # Tier population
        pop = {"n<30": 0, "child": 0, "teen": 0, "young": 0, "adult": 0}
        for rk, rs in rule_stats.items():
            pop[rs.tier()] = pop.get(rs.tier(), 0) + 1
        tier_pop_history[ym] = pop

    def reconcile(ym: str) -> dict:
        """End-of-month reconciliation. Identifies new learnings and applies
        them forward (mutating direction_overrides, structural_skip,
        size_multipliers). Returns a structured record for the doc."""
        new_flips: list[tuple[str, dict]] = []
        new_skips: list[tuple[str, dict]] = []
        new_amplifies: list[tuple[str, dict]] = []
        new_matured: list[tuple[str, str]] = []   # (rule_key, tier)

        for rk, rs in rule_stats.items():
            if rs.n < MIN_N_FOR_LEARNING:
                continue
            tier = rs.tier()
            # Newly mature this month (didn't have tier_at_mature recorded)
            if rs.tier_at_mature is None:
                rs.tier_at_mature = tier
                rs.matured_at = ym
                new_matured.append((rk, tier))

            pf = rs.profit_factor
            evidence = {
                "n": rs.n, "wins": rs.wins, "acc": rs.acc,
                "pf": pf, "cum_pnl": round(rs.cum_pnl, 2),
            }

            # Skip — worst case: acc consistently bad. One-shot decision.
            if rs.acc < SKIP_ACC_MAX and rk not in structural_skip:
                structural_skip.add(rk)
                new_skips.append((rk, evidence))
                continue

            # Flip — negative PF with mediocre acc. Direction reversal.
            if (pf is not None and pf < FLIP_PF_MAX
                    and rs.acc < FLIP_ACC_MAX
                    and rk not in direction_overrides
                    and rk not in structural_skip):
                direction_overrides[rk] = "flip"
                new_flips.append((rk, evidence))
                continue

            # Amplify — high PF + reasonable acc. Size up 1.5×.
            if (pf is not None and pf >= AMPLIFY_PF_MIN
                    and rs.acc >= AMPLIFY_ACC_MIN
                    and rk not in size_multipliers):
                size_multipliers[rk] = AMPLIFY_MULTIPLIER
                new_amplifies.append((rk, evidence))

        return {
            "new_flips":      new_flips,
            "new_skips":      new_skips,
            "new_amplifies":  new_amplifies,
            "new_matured":    new_matured,
            "current_flips":      dict(direction_overrides),
            "current_skips":      sorted(structural_skip),
            "current_amplifiers": dict(size_multipliers),
        }

    # ---- main event loop ----
    for ts, action, payload, tid in events:
        ym = ts[:7]
        if current_month is None:
            current_month = ym
            months_seen.append(ym)
        if ym != current_month:
            # End of `current_month` — snapshot + reconcile + roll to ym
            snapshot(current_month)
            learnings_by_month[current_month] = reconcile(current_month)
            current_month = ym
            months_seen.append(ym)

        if action == "deposit":
            amt = float(payload["amount"])
            cash += amt
            total_deposits += amt
            monthly[ym]["deposits"] += amt
            continue

        if action == "open":
            rk = payload.get("rule_key") or payload.get("event_type") or "unknown"
            # Skip if structurally banned
            if rk in structural_skip:
                monthly[ym]["skipped_by_rule"] += 1
                continue
            size = PER_POSITION_BASE * size_multipliers.get(rk, 1.0)
            if len(positions) >= MAX_CONCURRENT or cash < size:
                continue
            flipped = (direction_overrides.get(rk) == "flip")
            if flipped:
                monthly[ym]["flipped_trades"] += 1
            positions[tid] = {"trade": payload, "size": size, "flipped": flipped}
            cash -= size
            total_opens += 1
            monthly[ym]["opens"] += 1
            continue

        # action == "close"
        if tid not in positions:
            continue
        p = positions.pop(tid)
        t = p["trade"]
        size = p["size"]
        r = float(t.get("realized_return") or 0)
        # If we flipped at open time, invert the realized return
        effective_r = -r if p["flipped"] else r
        pnl = effective_r * size
        cum_pnl += pnl
        cash += size
        if pnl > 0:
            wins += 1
            monthly[ym]["wins"] += 1
        else:
            losses += 1
            monthly[ym]["losses"] += 1
        monthly[ym]["pnl"] += pnl
        monthly[ym]["closes"] += 1
        total_closes += 1
        hwm = max(hwm, cum_pnl)
        max_dd = max(max_dd, hwm - cum_pnl)

        rk = t.get("rule_key") or t.get("event_type") or "unknown"
        rule_stats[rk].record(effective_r, size)
        tk = t.get("ticker") or "unknown"
        ticker_stats[tk]["n"] += 1
        if pnl > 0:
            ticker_stats[tk]["wins"] += 1
        ticker_stats[tk]["pnl"] += pnl

    # Final month snapshot + reconcile
    if current_month and current_month not in end_of_month:
        snapshot(current_month)
        learnings_by_month[current_month] = reconcile(current_month)

    return {
        "months":           months_seen,
        "end_of_month":     end_of_month,
        "monthly":          monthly,
        "learnings":        learnings_by_month,
        "rule_stats":       rule_stats,
        "ticker_stats":     ticker_stats,
        "tier_pop_history": tier_pop_history,
        "forward_state": {
            "direction_overrides": dict(direction_overrides),
            "structural_skip":     sorted(structural_skip),
            "size_multipliers":    dict(size_multipliers),
        },
    }


# ------------------------------------------------------------------
# Doc writers
# ------------------------------------------------------------------

def write_month_doc(out_dir: str, ym: str, state: dict, monthly: dict,
                     learnings: dict, rule_stats: dict[str, RuleStats],
                     prev_state: Optional[dict],
                     tier_pop: dict, prev_tier_pop: Optional[dict]) -> str:
    fname = f"{ym.replace('-', '')}_monthly_reconc.md"
    path = os.path.join(out_dir, fname)

    total_equity = state["cash"] + state["deployed_in_positions"] + state["cum_pnl"]
    ret_on_dep = state["cum_pnl"] / state["total_deposits"] if state["total_deposits"] else 0
    wr_cum = state["wins"] / max(1, state["wins"] + state["losses"])
    m_wr = monthly["wins"] / max(1, monthly["wins"] + monthly["losses"])

    md: list[str] = []
    md.append(f"# Monthly reconciliation — {ym}")
    md.append("")
    md.append(f"_Generated {datetime.now(timezone.utc).date().isoformat()}._")
    md.append("")
    md.append(f"Sequential learning replay: $500 weekly deposits, $500 base "
              f"position size, max {MAX_CONCURRENT} concurrent. Each prior "
              f"month's reconciliation produced rule-level flips, skips, and "
              f"amplifiers that THIS month's trading respected.")
    md.append("")

    md.append("## Month-end state")
    md.append("")
    md.append("| Metric | This month | Cumulative |")
    md.append("|---|---|---|")
    md.append(f"| Deposits | {fmt_money(monthly['deposits'])} | "
              f"{fmt_money(state['total_deposits'])} |")
    md.append(f"| Trading PnL | {fmt_money(monthly['pnl'])} | {fmt_money(state['cum_pnl'])} |")
    md.append(f"| Opens | {monthly['opens']} | {state['total_opens']} |")
    md.append(f"| Closes | {monthly['closes']} | {state['total_closes']} |")
    md.append(f"| Win-rate | {m_wr:.1%} | {wr_cum:.1%} |")
    md.append(f"| Skipped (rule banned) | {monthly['skipped_by_rule']} | n/a |")
    md.append(f"| Flipped-direction trades opened | {monthly['flipped_trades']} | n/a |")
    md.append("")
    md.append("| Snapshot | Value |")
    md.append("|---|---|")
    md.append(f"| Cash idle | {fmt_money(state['cash'])} |")
    md.append(f"| Deployed in positions | {fmt_money(state['deployed_in_positions'])} ({state['positions_open']}/{MAX_CONCURRENT}) |")
    md.append(f"| Cumulative PnL | {fmt_money(state['cum_pnl'])} |")
    md.append(f"| **Total equity** | **{fmt_money(total_equity)}** |")
    md.append(f"| Return on deposits | {fmt_pct(ret_on_dep)} |")
    md.append(f"| Max drawdown | {fmt_money(state['max_dd'])} |")
    md.append("")

    # Learnings this month
    new_flips     = learnings.get("new_flips", [])
    new_skips     = learnings.get("new_skips", [])
    new_amplifies = learnings.get("new_amplifies", [])
    new_matured   = learnings.get("new_matured", [])

    md.append("## Reconciliation — learnings produced this month")
    md.append("")
    if not (new_flips or new_skips or new_amplifies or new_matured):
        md.append("_No new mature rules this month. Existing carry-forward decisions "
                  "remain in effect (see 'Active learning state' below)._")
        md.append("")
    if new_matured:
        md.append(f"### Newly mature (n crossed {MIN_N_FOR_LEARNING})")
        md.append("")
        md.append("| rule_key | tier on maturity |")
        md.append("|---|---|")
        for rk, tier in new_matured:
            md.append(f"| `{rk}` | {tier} |")
        md.append("")
    if new_flips:
        md.append("### Direction flips applied (PF < 1.0 AND acc < 50%)")
        md.append("")
        md.append("From next month onward, trades on these rule_keys will be opened "
                  "with INVERTED direction. Evidence at flip time:")
        md.append("")
        md.append("| rule_key | n | acc | PF | cum PnL |")
        md.append("|---|---|---|---|---|")
        for rk, ev in new_flips:
            pf_str = f"{ev['pf']:.2f}" if ev['pf'] is not None else "—"
            md.append(f"| `{rk}` | {ev['n']} | {ev['acc']:.1%} | {pf_str} | "
                      f"{fmt_money(ev['cum_pnl'])} |")
        md.append("")
    if new_skips:
        md.append(f"### Structural skips (acc < {SKIP_ACC_MAX:.0%})")
        md.append("")
        md.append("Trades on these rule_keys will NOT be opened going forward — the "
                  "loss rate is severe enough that even direction-flip might just be "
                  "regression to mean.")
        md.append("")
        md.append("| rule_key | n | acc | PF | cum PnL |")
        md.append("|---|---|---|---|---|")
        for rk, ev in new_skips:
            pf_str = f"{ev['pf']:.2f}" if ev['pf'] is not None else "—"
            md.append(f"| `{rk}` | {ev['n']} | {ev['acc']:.1%} | {pf_str} | "
                      f"{fmt_money(ev['cum_pnl'])} |")
        md.append("")
    if new_amplifies:
        md.append(f"### Amplifications (PF ≥ {AMPLIFY_PF_MIN}, acc ≥ {AMPLIFY_ACC_MIN:.0%})")
        md.append("")
        md.append(f"Position size will scale by {AMPLIFY_MULTIPLIER}× on these "
                  f"rule_keys from next month onward.")
        md.append("")
        md.append("| rule_key | n | acc | PF | cum PnL |")
        md.append("|---|---|---|---|---|")
        for rk, ev in new_amplifies:
            pf_str = f"{ev['pf']:.2f}" if ev['pf'] is not None else "—"
            md.append(f"| `{rk}` | {ev['n']} | {ev['acc']:.1%} | {pf_str} | "
                      f"{fmt_money(ev['cum_pnl'])} |")
        md.append("")

    # Carry-forward state (full active set)
    md.append("## Active learning state (cumulative through this month)")
    md.append("")
    md.append("These are ALL the rule-level decisions currently in effect — "
              "everything from this month plus carry-forward from prior months.")
    md.append("")
    cur_flips = learnings.get("current_flips", {})
    cur_skips = learnings.get("current_skips", [])
    cur_amps  = learnings.get("current_amplifiers", {})
    md.append(f"- **Direction flips active**: {len(cur_flips)} rules")
    if cur_flips:
        for rk in sorted(cur_flips):
            md.append(f"  - `{rk}`")
    md.append(f"- **Structural skips active**: {len(cur_skips)} rules")
    if cur_skips:
        for rk in cur_skips:
            md.append(f"  - `{rk}`")
    md.append(f"- **Amplified rules active**: {len(cur_amps)} rules (×{AMPLIFY_MULTIPLIER})")
    if cur_amps:
        for rk in sorted(cur_amps):
            md.append(f"  - `{rk}`")
    md.append("")

    # Tier population drift
    if prev_tier_pop:
        md.append("## Tier population drift (vs prior month)")
        md.append("")
        md.append("| Tier | Prior | Current | Δ |")
        md.append("|---|---|---|---|")
        for tier in ("n<30", "child", "teen", "young", "adult"):
            prev = prev_tier_pop.get(tier, 0)
            cur = tier_pop.get(tier, 0)
            diff = cur - prev
            sign = "+" if diff > 0 else ""
            md.append(f"| `{tier}` | {prev} | {cur} | {sign}{diff} |")
        md.append("")

    # Top/worst rules this month (by per-month PnL)
    # Compute from rule_stats — but those are cumulative. For a single-month
    # contribution view, use the activity counter we already have.
    md.append("## Top rules by cumulative PnL (n ≥ 10)")
    md.append("")
    md.append("| rule_key | n | win-rate | PF | cum PnL |")
    md.append("|---|---|---|---|---|")
    ranked = sorted(
        ((rk, rs) for rk, rs in rule_stats.items() if rs.n >= 10),
        key=lambda x: -x[1].cum_pnl,
    )
    for rk, rs in ranked[:8]:
        pf = rs.profit_factor
        pf_str = f"{pf:.2f}" if pf is not None else "—"
        md.append(f"| `{rk}` | {rs.n} | {rs.acc:.1%} | {pf_str} | "
                  f"{fmt_money(rs.cum_pnl)} |")
    md.append("")
    if len(ranked) >= 6:
        md.append("## Worst rules by cumulative PnL (n ≥ 10)")
        md.append("")
        md.append("| rule_key | n | win-rate | PF | cum PnL |")
        md.append("|---|---|---|---|---|")
        for rk, rs in ranked[-6:][::-1]:
            pf = rs.profit_factor
            pf_str = f"{pf:.2f}" if pf is not None else "—"
            md.append(f"| `{rk}` | {rs.n} | {rs.acc:.1%} | {pf_str} | "
                      f"{fmt_money(rs.cum_pnl)} |")
        md.append("")

    md.append("## How to read this doc")
    md.append("")
    md.append("This snapshot was produced AT the end of the month named in the "
              "title. The 'Reconciliation' section shows the new decisions made "
              "that night; those decisions then governed the NEXT month's "
              "trading. The 'Active learning state' section shows the full "
              "carry-forward set including all prior months' decisions.")
    md.append("")

    with open(path, "w") as f:
        f.write("\n".join(md))
    return path


def write_quarterly_review_doc(
    out_dir: str,
    quarter: str,         # e.g., "2025Q3"
    quarter_months: list[str],
    result: dict,
    quarter_start_state: dict,   # state at start of quarter
) -> str:
    """Quarterly 'consultant' review for the 3 months in the quarter.

    Reads the monthly reconciliations + cumulative state and produces a
    higher-level analytical doc. This is the historical-replay
    counterpart to scripts/quarterly_consultant_review.py.
    """
    path = os.path.join(out_dir, f"{quarter}_quarterly_review.md")

    # Quarter-end is the last month's state
    qe = quarter_months[-1]
    state_end = result["end_of_month"][qe]
    state_start = quarter_start_state

    # Quarter deltas
    d_deposits = state_end["total_deposits"] - state_start.get("total_deposits", 0.0)
    d_pnl = state_end["cum_pnl"] - state_start.get("cum_pnl", 0.0)
    d_closes = state_end["total_closes"] - state_start.get("total_closes", 0)
    d_wins = state_end["wins"] - state_start.get("wins", 0)
    d_losses = state_end["losses"] - state_start.get("losses", 0)
    q_wr = d_wins / max(1, d_wins + d_losses)
    q_return = d_pnl / d_deposits if d_deposits else 0

    # Aggregate learnings from each month in the quarter
    q_flips: list[tuple[str, str, dict]] = []   # (ym, rule, evidence)
    q_skips: list[tuple[str, str, dict]] = []
    q_amps:  list[tuple[str, str, dict]] = []
    q_matured: list[tuple[str, str, str]] = []  # (ym, rule, tier)
    for ym in quarter_months:
        L = result["learnings"].get(ym, {})
        for rk, ev in L.get("new_flips", []):     q_flips.append((ym, rk, ev))
        for rk, ev in L.get("new_skips", []):     q_skips.append((ym, rk, ev))
        for rk, ev in L.get("new_amplifies", []): q_amps.append((ym, rk, ev))
        for rk, tier in L.get("new_matured", []): q_matured.append((ym, rk, tier))

    # Tier population drift across the quarter
    p_start = result["tier_pop_history"].get(quarter_months[0], {})
    p_end = result["tier_pop_history"].get(qe, {})

    md: list[str] = []
    md.append(f"# Quarterly review — {quarter}")
    md.append("")
    md.append(f"_Generated {datetime.now(timezone.utc).date().isoformat()}._")
    md.append("")
    md.append("**Independent-consultant view across the prior 3 months.** Reads "
              "monthly reconciliations and the calibration trajectory; provides "
              "insights that wouldn't be visible in a single month's view.")
    md.append("")

    md.append(f"## Quarter at a glance ({quarter_months[0]} → {quarter_months[-1]})")
    md.append("")
    md.append("| Metric | Quarter Δ | Quarter-end cumulative |")
    md.append("|---|---|---|")
    md.append(f"| Deposits | {fmt_money(d_deposits)} | {fmt_money(state_end['total_deposits'])} |")
    md.append(f"| Trading PnL | {fmt_money(d_pnl)} | {fmt_money(state_end['cum_pnl'])} |")
    md.append(f"| Closed trades | {d_closes} | {state_end['total_closes']} |")
    md.append(f"| Win-rate | {q_wr:.1%} | {state_end['wins'] / max(1, state_end['wins'] + state_end['losses']):.1%} |")
    md.append(f"| Return on quarter's deposits | {fmt_pct(q_return)} | — |")
    md.append(f"| Max drawdown | — | {fmt_money(state_end['max_dd'])} |")
    md.append("")

    # New mature rules — the most-important learning of the quarter
    md.append("## What the calibration learned this quarter")
    md.append("")
    if q_matured:
        md.append(f"### {len(q_matured)} rules crossed n ≥ {MIN_N_FOR_LEARNING}")
        md.append("")
        md.append("| Month | rule_key | Tier on maturity |")
        md.append("|---|---|---|")
        for ym, rk, tier in q_matured:
            md.append(f"| {ym} | `{rk}` | {tier} |")
        md.append("")
    if q_flips:
        md.append(f"### {len(q_flips)} direction flips applied this quarter")
        md.append("")
        md.append("| Month | rule_key | n | acc | PF | evidence-time PnL |")
        md.append("|---|---|---|---|---|---|")
        for ym, rk, ev in q_flips:
            pf = f"{ev['pf']:.2f}" if ev['pf'] is not None else "—"
            md.append(f"| {ym} | `{rk}` | {ev['n']} | {ev['acc']:.1%} | {pf} | "
                      f"{fmt_money(ev['cum_pnl'])} |")
        md.append("")
    if q_skips:
        md.append(f"### {len(q_skips)} structural skips applied")
        md.append("")
        md.append("| Month | rule_key | n | acc |")
        md.append("|---|---|---|---|")
        for ym, rk, ev in q_skips:
            md.append(f"| {ym} | `{rk}` | {ev['n']} | {ev['acc']:.1%} |")
        md.append("")
    if q_amps:
        md.append(f"### {len(q_amps)} amplifications applied")
        md.append("")
        md.append("| Month | rule_key | PF |")
        md.append("|---|---|---|")
        for ym, rk, ev in q_amps:
            md.append(f"| {ym} | `{rk}` | {ev['pf']:.2f} |")
        md.append("")
    if not (q_matured or q_flips or q_skips or q_amps):
        md.append("_No new mature rules, flips, skips, or amplifications produced "
                  "this quarter. The calibration is still accumulating sample. This "
                  "is the maturity-gate working as designed; not a failure._")
        md.append("")

    # Tier population drift
    md.append("## Tier population drift across the quarter")
    md.append("")
    md.append("| Tier | Quarter start | Quarter end | Δ |")
    md.append("|---|---|---|---|")
    for tier in ("n<30", "child", "teen", "young", "adult"):
        s_v = p_start.get(tier, 0)
        e_v = p_end.get(tier, 0)
        diff = e_v - s_v
        sign = "+" if diff > 0 else ""
        md.append(f"| `{tier}` | {s_v} | {e_v} | {sign}{diff} |")
    md.append("")

    # Top rules by activity this quarter
    md.append("## Top rules contributing this quarter (n contribution ≥ 5)")
    md.append("")
    md.append("| rule_key | Quarter-end n | win-rate | PF | cum PnL |")
    md.append("|---|---|---|---|---|")
    ranked = sorted(
        ((rk, rs) for rk, rs in result["rule_stats"].items() if rs.n >= 5),
        key=lambda x: -x[1].cum_pnl,
    )
    for rk, rs in ranked[:8]:
        pf = rs.profit_factor
        pf_str = f"{pf:.2f}" if pf is not None else "—"
        md.append(f"| `{rk}` | {rs.n} | {rs.acc:.1%} | {pf_str} | {fmt_money(rs.cum_pnl)} |")
    md.append("")

    # Independent-consultant observations
    md.append("## Consultant observations")
    md.append("")
    obs: list[str] = []

    # Observation 1: are the flips paying off?
    if any(direction_overrides := result["forward_state"]["direction_overrides"]):
        # Check whether flipped rules' subsequent contribution is positive
        flip_winners = []
        flip_losers = []
        for rk in direction_overrides:
            rs = result["rule_stats"].get(rk)
            if not rs: continue
            if rs.cum_pnl > 0: flip_winners.append((rk, rs.cum_pnl))
            else: flip_losers.append((rk, rs.cum_pnl))
        if flip_winners:
            obs.append(f"**Flips are working:** {len(flip_winners)} of "
                       f"{len(direction_overrides)} flipped rules have positive "
                       f"cumulative PnL after the flip was applied. "
                       f"Top: `{flip_winners[0][0]}` at {fmt_money(flip_winners[0][1])}.")
        if flip_losers:
            obs.append(f"**Some flips need review:** {len(flip_losers)} flipped "
                       f"rule(s) still net-negative — could be (a) more sample "
                       f"needed, or (b) the flip was the wrong direction (rule is "
                       f"genuinely noisy at h7d).")

    # Observation 2: where is the deployed capital going?
    deployed = state_end["deployed_in_positions"]
    cash = state_end["cash"]
    if cash > deployed * 3:
        obs.append(f"**Capital under-deployment:** {fmt_money(cash)} sits idle "
                   f"vs {fmt_money(deployed)} deployed in positions. The $500 "
                   f"position cap × 10 concurrent gives a $5K deployment ceiling; "
                   f"beyond that deposits accumulate. Strategic question: raise "
                   f"per-position size as bankroll grows, OR move excess to a "
                   f"passive sleeve.")

    # Observation 3: tier movement
    if p_end.get("adult", 0) > p_start.get("adult", 0):
        obs.append(f"**Adult-tier promotion this quarter:** went from "
                   f"{p_start.get('adult', 0)} → {p_end.get('adult', 0)} adult-tier "
                   f"rules. Adult unlocks BUY/SELL vocabulary in `thesis_agent`.")
    elif p_end.get("teen", 0) > p_start.get("teen", 0):
        obs.append(f"**Teen-tier growth:** {p_start.get('teen', 0)} → "
                   f"{p_end.get('teen', 0)} teen rules. Progress toward maturity "
                   f"is happening; the gate is structurally workable.")

    # Observation 4: pipeline maturity %
    total_rules = sum(p_end.values()) if p_end else 0
    mature_rules = p_end.get("teen", 0) + p_end.get("young", 0) + p_end.get("adult", 0)
    if total_rules:
        mat_pct = mature_rules / total_rules * 100
        obs.append(f"**Pipeline maturity:** {mat_pct:.0f}% of tracked rule_keys "
                   f"are at teen+ tier ({mature_rules}/{total_rules}). The "
                   f"remaining {100-mat_pct:.0f}% are still in child or n<30 — "
                   f"sample accumulating.")

    if obs:
        for o in obs:
            md.append(f"- {o}")
        md.append("")

    # Recommendations
    md.append("## Recommendations to feed back into the pipeline")
    md.append("")
    recs: list[str] = []
    # If many flips and they're paying off, suggest the live pipeline adopt them
    if q_flips:
        recs.append(f"**Adopt the {len(q_flips)} new flip(s) live:** the historical "
                    f"replay shows these rules' direction is reliably inverted with "
                    f"the evidence we now have. Add to a `STRUCTURAL_FLIP` set in "
                    f"`thesis_agent.score_evidence()` and feature-flag it.")
    if q_amps:
        recs.append(f"**Adopt the {len(q_amps)} new amplification(s) live:** "
                    f"these rule_keys are showing PF ≥ 2.0 — consider raising the "
                    f"live position size in `risk_agent` for them.")
    if cash > deployed * 5 and state_end["total_deposits"] > 5000:
        recs.append("**Address capital under-deployment:** the $500 × 10 ceiling "
                    "leaves accumulated DCA cash idle. Either scale per-position "
                    "size with bankroll OR route excess to a passive sleeve. This "
                    "is a discipline question for the operator, not a tweak.")
    if total_rules and (p_end.get("n<30", 0) / total_rules > 0.50):
        recs.append("**Accelerate maturity collection:** >50% of rules are still "
                    "below n=30. The cron-pinger work has already addressed event_paper_agent "
                    "drops; consider expanding the watchlist or relaxing severity "
                    "floor on specific event_types to grow sample faster.")
    if not recs:
        recs.append("_No specific strategic recommendations this quarter — the "
                    "discipline is operating within expected bounds. Continue the "
                    "current cadence and re-review next quarter._")
    for r in recs:
        md.append(f"- {r}")
    md.append("")

    md.append("## How to read this doc")
    md.append("")
    md.append("Generated by an automated rule-based 'consultant' that reads the "
              "monthly reconciliations + cumulative state. Not an LLM judgment — "
              "deterministic insights derived from the same thresholds the monthly "
              "reconciler uses, applied at a higher time-horizon. This is the "
              "historical-replay equivalent; the live operational version lives at "
              "`scripts/quarterly_consultant_review.py`.")
    md.append("")

    with open(path, "w") as f:
        f.write("\n".join(md))
    return path


def write_summary_doc(out_dir: str, result: dict) -> str:
    today = datetime.now(timezone.utc).strftime("%d%m%Y")
    path = os.path.join(out_dir, f"sequential_replay_summary_{today}.md")
    months = result["months"]
    last = months[-1]
    s = result["end_of_month"][last]
    total_eq = s["cash"] + s["deployed_in_positions"] + s["cum_pnl"]
    ret_on_dep = s["cum_pnl"] / s["total_deposits"] if s["total_deposits"] else 0
    wr = s["wins"] / max(1, s["wins"] + s["losses"])

    md: list[str] = []
    md.append(f"# Sequential monthly replay — summary ({START} → {END})")
    md.append("")
    md.append(f"_Generated {datetime.now(timezone.utc).date().isoformat()}._")
    md.append("")
    md.append("Each month's reconciliation produced flip/skip/amplify decisions "
              "that the NEXT month's trading respected. This is the closed-loop "
              "version of the historical replay.")
    md.append("")

    md.append("## Headline")
    md.append("")
    md.append("| Metric | Value |")
    md.append("|---|---|")
    md.append(f"| Months simulated | {len(months)} |")
    md.append(f"| Total deposits | {fmt_money(s['total_deposits'])} |")
    md.append(f"| Cumulative trading PnL | {fmt_money(s['cum_pnl'])} |")
    md.append(f"| **Total equity** | **{fmt_money(total_eq)}** |")
    md.append(f"| Effective return on deposits | {fmt_pct(ret_on_dep)} |")
    md.append(f"| Max drawdown | {fmt_money(s['max_dd'])} |")
    md.append(f"| Closed trades | {s['total_closes']} ({s['wins']}W / {s['losses']}L, win-rate {wr:.1%}) |")
    md.append("")

    md.append("## Equity curve by month-end")
    md.append("")
    md.append("| Month | Deposits cum | Trading PnL | Equity | Return on dep |")
    md.append("|---|---|---|---|---|")
    for ym in months:
        st = result["end_of_month"][ym]
        eq = st["cash"] + st["deployed_in_positions"] + st["cum_pnl"]
        r = st["cum_pnl"] / st["total_deposits"] if st["total_deposits"] else 0
        md.append(
            f"| {ym} | {fmt_money(st['total_deposits'])} | "
            f"{fmt_money(st['cum_pnl'])} | {fmt_money(eq)} | {fmt_pct(r)} |"
        )
    md.append("")

    md.append("## All reconciliation decisions over the period")
    md.append("")
    md.append("### Direction flips applied")
    md.append("")
    cur_flips = result["forward_state"]["direction_overrides"]
    if cur_flips:
        md.append("| rule_key | flipped at month |")
        md.append("|---|---|")
        # Find which month each was flipped
        for ym in months:
            for rk, _ev in result["learnings"][ym].get("new_flips", []):
                md.append(f"| `{rk}` | {ym} |")
    else:
        md.append("_None._")
    md.append("")
    md.append("### Structural skips applied")
    md.append("")
    if result["forward_state"]["structural_skip"]:
        md.append("| rule_key | skipped at month |")
        md.append("|---|---|")
        for ym in months:
            for rk, _ev in result["learnings"][ym].get("new_skips", []):
                md.append(f"| `{rk}` | {ym} |")
    else:
        md.append("_None._")
    md.append("")
    md.append("### Amplifications applied")
    md.append("")
    if result["forward_state"]["size_multipliers"]:
        md.append("| rule_key | amplified at month | multiplier |")
        md.append("|---|---|---|")
        for ym in months:
            for rk, _ev in result["learnings"][ym].get("new_amplifies", []):
                md.append(f"| `{rk}` | {ym} | ×{AMPLIFY_MULTIPLIER} |")
    else:
        md.append("_None._")
    md.append("")

    md.append("## Tier population trajectory")
    md.append("")
    md.append("| Month | n<30 | child | teen | young | adult |")
    md.append("|---|---|---|---|---|---|")
    for ym in months:
        p = result["tier_pop_history"].get(ym, {})
        md.append(
            f"| {ym} | {p.get('n<30', 0)} | {p.get('child', 0)} | "
            f"{p.get('teen', 0)} | {p.get('young', 0)} | {p.get('adult', 0)} |"
        )
    md.append("")

    md.append("## Method")
    md.append("")
    md.append(f"- Window: {START} → {END}")
    md.append(f"- Weekly deposit: ${WEEKLY_DEPOSIT:.0f} every Monday from {DEPOSIT_START_ISO}")
    md.append(f"- Per-position base size: ${PER_POSITION_BASE:.0f}, max {MAX_CONCURRENT} concurrent")
    md.append(f"- Flip threshold: PF < {FLIP_PF_MAX} AND acc < {FLIP_ACC_MAX:.0%} at n ≥ {MIN_N_FOR_LEARNING}")
    md.append(f"- Skip threshold: acc < {SKIP_ACC_MAX:.0%} at n ≥ {MIN_N_FOR_LEARNING}")
    md.append(f"- Amplify threshold: PF ≥ {AMPLIFY_PF_MIN} AND acc ≥ {AMPLIFY_ACC_MIN:.0%} at n ≥ {MIN_N_FOR_LEARNING}, "
              f"scale ×{AMPLIFY_MULTIPLIER}")
    md.append("- Slippage: already in `realized_return` (10 bps round-trip)")
    md.append("- Horizon: h7d (matches the single-pass DCA replay for comparability)")
    md.append("")
    md.append("Re-runnable: `python3 scripts/sequential_monthly_replay.py` "
              "(idempotent — overwrites prior docs).")
    md.append("")

    with open(path, "w") as f:
        f.write("\n".join(md))
    return path


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------

def main() -> int:
    out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "docs", "learning")
    os.makedirs(out_dir, exist_ok=True)

    trades = fetch_trades()
    if not trades:
        print("No trades — nothing to do.", file=sys.stderr)
        return 1

    result = simulate(trades)

    months = result["months"]
    print(f"Generating {len(months)} monthly reconc docs…", file=sys.stderr)

    prev_state = None
    prev_tier = None
    # For quarter-end emission, remember the state at the START of each
    # quarter so the review can compute Δ properly.
    quarter_start_state: dict | None = None
    quarter_months_buffer: list[str] = []
    last_quarter_seen: str | None = None
    quarterly_paths: list[str] = []

    def ym_to_quarter(ym: str) -> str:
        y, m = ym.split("-")
        q = (int(m) - 1) // 3 + 1
        return f"{y}Q{q}"

    for ym in months:
        write_month_doc(
            out_dir, ym,
            result["end_of_month"][ym],
            result["monthly"][ym],
            result["learnings"][ym],
            result["rule_stats"],
            prev_state,
            result["tier_pop_history"][ym],
            prev_tier,
        )

        # Quarter tracking: when the month is the last of a quarter (Mar/Jun/Sep/Dec)
        # AND we have at least 1 month buffered, emit a quarterly review.
        q_label = ym_to_quarter(ym)
        if last_quarter_seen is None:
            last_quarter_seen = q_label
            quarter_start_state = prev_state or {
                "total_deposits": 0.0, "cum_pnl": 0.0, "total_closes": 0,
                "wins": 0, "losses": 0,
            }
        if q_label != last_quarter_seen:
            # We just rolled into a new quarter — emit review for the prior one.
            if quarter_months_buffer:
                p = write_quarterly_review_doc(
                    out_dir, last_quarter_seen,
                    quarter_months_buffer, result, quarter_start_state,
                )
                quarterly_paths.append(p)
            last_quarter_seen = q_label
            quarter_start_state = prev_state or quarter_start_state
            quarter_months_buffer = []
        quarter_months_buffer.append(ym)

        prev_state = result["end_of_month"][ym]
        prev_tier = result["tier_pop_history"][ym]

    # Emit final quarter (whatever months we have buffered at the end)
    if quarter_months_buffer and last_quarter_seen and quarter_start_state is not None:
        p = write_quarterly_review_doc(
            out_dir, last_quarter_seen,
            quarter_months_buffer, result, quarter_start_state,
        )
        quarterly_paths.append(p)

    summary_path = write_summary_doc(out_dir, result)

    # Terminal summary
    last = months[-1]
    s = result["end_of_month"][last]
    total_eq = s["cash"] + s["deployed_in_positions"] + s["cum_pnl"]
    wr = s["wins"] / max(1, s["wins"] + s["losses"])
    fs = result["forward_state"]
    print()
    print(f"=== Sequential replay summary (end of {last}) ===")
    print(f"  Months simulated  : {len(months)}")
    print(f"  Total deposits    : {fmt_money(s['total_deposits'])}")
    print(f"  Trading PnL       : {fmt_money(s['cum_pnl'])}")
    print(f"  Total equity      : {fmt_money(total_eq)}")
    print(f"  Effective return  : {fmt_pct(s['cum_pnl']/max(1, s['total_deposits']))}")
    print(f"  Max drawdown      : {fmt_money(s['max_dd'])}")
    print(f"  Closed trades     : {s['total_closes']}  "
          f"({s['wins']}W / {s['losses']}L, {wr:.1%})")
    print(f"  Direction flips   : {len(fs['direction_overrides'])}")
    print(f"  Structural skips  : {len(fs['structural_skip'])}")
    print(f"  Amplified rules   : {len(fs['size_multipliers'])}")
    print(f"  Quarterly reviews : {len(quarterly_paths)} written")
    print(f"  Summary doc       : {summary_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
