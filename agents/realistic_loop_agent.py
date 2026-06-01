"""realistic_loop_agent — $5K bankroll shadow portfolio.

What this is:
  A capital-deployed, capped-concurrency paper-trade ledger that shadows the
  live pipeline. Reads tradeable setups from stock_trade_setups; opens positions
  sized by deployed capital (default $1,000 each, max 5 concurrent); marks
  to market daily via stock_raw_prices; closes on target/stop/horizon/valid_until.

What this is NOT:
  * Not a calibration source — does not write to stock_rule_calibration. The
    canonical multi-horizon calibration loop lives in event_paper_agent +
    price_agent and is owned by the maturity-gate discipline.
  * Not a Van Tharp risk-sized strategy — positions are dollar-equal, not
    risk-equal. Per the project memory: paper-trade budgets are notional, not
    risk units.

Modes (single --mode flag):
  * open  — scan new tradeable setups since last_open_scan_at, open positions
            up to max_concurrent, decrement cash.
  * mark  — for every open position, walk stock_raw_prices since opened_at,
            detect target/stop/horizon/valid_until hits, close as appropriate,
            return notional to cash, accumulate PnL + drawdown.

The agent is idempotent: rerunning open immediately won't open the same setup
twice (unique(loop_name, setup_id)). Rerunning mark on a closed position is a
no-op (status='closed' filtered out).
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any

import requests


SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
HEADERS_SB = {
    "apikey": SUPABASE_SERVICE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}

LOOP_NAME = os.environ.get("REALISTIC_LOOP_NAME", "shadow_5k")
# Slippage convention matches price_agent (5 bps per side, 10 bps round-trip).
SLIPPAGE_BPS = 5.0
SLIPPAGE_PER_SIDE = SLIPPAGE_BPS / 10_000


# ---------------------------------------------------------------------------
# Supabase helpers
# ---------------------------------------------------------------------------

def sb_get(path: str, params: dict[str, str] | None = None) -> list[dict]:
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/{path}",
        headers=HEADERS_SB,
        params=params or {},
        timeout=20,
    )
    if r.status_code != 200:
        raise RuntimeError(f"GET {path} -> {r.status_code}: {r.text[:300]}")
    return r.json()


def sb_post(path: str, rows: list[dict] | dict, prefer: str | None = None) -> Any:
    headers = dict(HEADERS_SB)
    if prefer:
        headers["Prefer"] = prefer
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/{path}",
        headers=headers,
        json=rows,
        timeout=20,
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"POST {path} -> {r.status_code}: {r.text[:300]}")
    return r.json() if r.text else None


def sb_patch(path: str, params: dict[str, str], body: dict) -> Any:
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/{path}",
        headers=HEADERS_SB,
        params=params,
        json=body,
        timeout=20,
    )
    if r.status_code not in (200, 204):
        raise RuntimeError(f"PATCH {path} -> {r.status_code}: {r.text[:300]}")
    return r.json() if r.text else None


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def get_state() -> dict:
    rows = sb_get(
        "stock_realistic_loop_state",
        {"loop_name": f"eq.{LOOP_NAME}"},
    )
    if not rows:
        raise RuntimeError(f"No state row for loop_name={LOOP_NAME}. "
                           "Did migration 0033 run with the seed insert?")
    return rows[0]


def update_state(patch: dict) -> None:
    patch = {**patch, "updated_at": datetime.now(timezone.utc).isoformat()}
    sb_patch(
        "stock_realistic_loop_state",
        {"loop_name": f"eq.{LOOP_NAME}"},
        patch,
    )


# ---------------------------------------------------------------------------
# Open mode — scan setups, open new positions
# ---------------------------------------------------------------------------

def latest_price(ticker: str) -> dict | None:
    """Most recent close bar for a ticker."""
    rows = sb_get(
        "stock_raw_prices",
        {
            "ticker": f"eq.{ticker}",
            "order": "ts.desc",
            "limit": "1",
            "select": "ticker,ts,open,high,low,close",
        },
    )
    return rows[0] if rows else None


def existing_setup_ids() -> set[int]:
    """All setup_ids already in the loop ledger (open or closed)."""
    rows = sb_get(
        "stock_realistic_loop_positions",
        {
            "loop_name": f"eq.{LOOP_NAME}",
            "select": "setup_id",
            "limit": "10000",
        },
    )
    return {r["setup_id"] for r in rows if r.get("setup_id") is not None}


COLD_START_LOOKBACK_HOURS = int(os.environ.get("LOOP_COLD_START_LOOKBACK_HOURS", "24"))


def fetch_candidate_setups(since: str | None) -> list[dict]:
    """Tradeable setups: reason_to_skip null, recent.

    Ordering is ASCENDING by created_at within the eligibility window so that
    when the live pipeline emits a burst of setups, we open them in fire order
    (matches what a human trader watching alerts would do).

    Cold start: when state has no last_open_scan_at, ignore any backlog older
    than COLD_START_LOOKBACK_HOURS — the loop should shadow the LIVE pipeline,
    not retroactively trade weeks of historical setups.
    """
    if since:
        floor = since
    else:
        floor = (datetime.now(timezone.utc)
                 - timedelta(hours=COLD_START_LOOKBACK_HOURS)).isoformat()
    params: dict[str, str] = {
        "created_at":     f"gt.{floor}",
        "reason_to_skip": "is.null",
        "order":          "created_at.asc",
        "limit":          "200",
        "select":         "id,signal_id,ticker,direction,setup_type,entry_ref_price,"
                          "stop_pct,target_pct,horizon_days,valid_until,created_at",
    }
    return sb_get("stock_trade_setups", params)


def open_positions(now: datetime) -> int:
    state = get_state()
    capacity = state["max_concurrent"] - state["positions_open"]
    per_size = float(state["per_position_size"])
    cash = float(state["cash_available"])

    if capacity <= 0:
        print(f"[open] at max concurrency ({state['positions_open']}/{state['max_concurrent']}); skipping")
        return 0
    if cash < per_size:
        print(f"[open] cash ${cash:.2f} < per-position ${per_size:.2f}; skipping")
        return 0

    candidates = fetch_candidate_setups(state.get("last_open_scan_at"))
    if not candidates:
        print("[open] no new candidate setups")
        update_state({"last_open_scan_at": now.isoformat()})
        return 0

    already = existing_setup_ids()
    candidates = [s for s in candidates if s["id"] not in already]
    print(f"[open] {len(candidates)} unseen candidate setups; capacity={capacity}, cash=${cash:.2f}")

    opened = 0
    for setup in candidates:
        if capacity <= 0 or cash < per_size:
            break
        ticker = setup["ticker"]
        direction = setup["direction"]
        if direction not in ("long", "short"):
            print(f"  [skip] setup {setup['id']} {ticker}: unknown direction {direction!r}")
            continue
        bar = latest_price(ticker)
        if not bar or not bar.get("close"):
            print(f"  [skip] setup {setup['id']} {ticker}: no price")
            continue
        open_price = float(bar["close"])
        if open_price <= 0:
            print(f"  [skip] setup {setup['id']} {ticker}: bad price {open_price}")
            continue

        # Apply 5 bps entry slippage (consistent with price_agent).
        entry_eff = open_price * (1 + SLIPPAGE_PER_SIDE) if direction == "long" \
                    else open_price * (1 - SLIPPAGE_PER_SIDE)
        shares = round(per_size / entry_eff, 6)
        if shares <= 0:
            continue
        notional = round(shares * entry_eff, 2)

        target_pct = float(setup.get("target_pct") or 0.05)
        stop_pct = float(setup.get("stop_pct") or 0.03)
        horizon = int(setup.get("horizon_days") or 1)
        if direction == "long":
            target_price = round(open_price * (1 + target_pct), 4)
            stop_price = round(open_price * (1 - stop_pct), 4)
        else:
            target_price = round(open_price * (1 - target_pct), 4)
            stop_price = round(open_price * (1 + stop_pct), 4)

        exit_target = (now.date() + timedelta(days=horizon)).isoformat()
        row = {
            "loop_name":        LOOP_NAME,
            "setup_id":         setup["id"],
            "signal_id":        setup.get("signal_id"),
            "ticker":           ticker,
            "direction":        direction,
            "opened_at":        now.isoformat(),
            "open_price":       round(open_price, 4),
            "notional":         notional,
            "shares":           shares,
            "target_pct":       target_pct,
            "stop_pct":         stop_pct,
            "target_price":     target_price,
            "stop_price":       stop_price,
            "horizon_days":     horizon,
            "exit_target_date": exit_target,
            "valid_until":      setup.get("valid_until"),
            "status":           "open",
            "meta": {
                "setup_type":   setup.get("setup_type"),
                "entry_ref":    setup.get("entry_ref_price"),
                "bar_ts":       bar.get("ts"),
            },
        }
        try:
            sb_post("stock_realistic_loop_positions", [row],
                    prefer="resolution=ignore-duplicates,return=representation")
        except RuntimeError as e:
            print(f"  [skip] setup {setup['id']} {ticker}: insert failed: {e}")
            continue
        cash -= notional
        capacity -= 1
        opened += 1
        print(f"  [open] {ticker} {direction} ${notional:.2f} "
              f"target={target_price} stop={stop_price} horizon={horizon}d")

    if opened:
        new_state = get_state()  # re-read in case of concurrent updates
        update_state({
            "cash_available":    round(float(new_state["cash_available"]) - sum(
                                    1 for _ in range(opened)) * per_size, 2),
            "positions_open":    new_state["positions_open"] + opened,
            "last_open_scan_at": now.isoformat(),
        })
    else:
        update_state({"last_open_scan_at": now.isoformat()})
    return opened


# ---------------------------------------------------------------------------
# Mark mode — close positions on target/stop/horizon/valid_until
# ---------------------------------------------------------------------------

def fetch_bars(ticker: str, since: str, until: str | None = None) -> list[dict]:
    params: dict[str, str] = {
        "ticker":  f"eq.{ticker}",
        "ts":      f"gte.{since}",
        "order":   "ts.asc",
        "limit":   "1000",
        "select":  "ts,open,high,low,close",
    }
    if until:
        # PostgREST chained filter — second `ts` filter via "and" clause.
        params["and"] = f"(ts.lte.{until})"
    return sb_get("stock_raw_prices", params)


def evaluate_position(pos: dict, now: datetime) -> dict | None:
    """Walk bars since opened_at; return close info on first exit trigger, else None."""
    opened = datetime.fromisoformat(pos["opened_at"].replace("Z", "+00:00")).date()
    bars = fetch_bars(pos["ticker"], opened.isoformat())
    if not bars:
        return None

    direction = pos["direction"]
    open_price = float(pos["open_price"])
    target = float(pos["target_price"])
    stop = float(pos["stop_price"])
    exit_target_date = (date.fromisoformat(pos["exit_target_date"])
                        if pos.get("exit_target_date") else None)
    valid_until = (datetime.fromisoformat(pos["valid_until"].replace("Z", "+00:00")).date()
                   if pos.get("valid_until") else None)

    mfe = 0.0
    mae = 0.0
    for bar in bars:
        bar_date = datetime.fromisoformat(bar["ts"].replace("Z", "+00:00")).date()
        if bar_date <= opened:
            continue
        try:
            hi = float(bar["high"])
            lo = float(bar["low"])
            close = float(bar["close"])
        except (TypeError, ValueError):
            continue

        if direction == "long":
            mfe = max(mfe, (hi - open_price) / open_price)
            mae = min(mae, (lo - open_price) / open_price)
            if hi >= target:
                return _close_at(pos, target, "target_hit", bar_date, mfe, mae)
            if lo <= stop:
                return _close_at(pos, stop, "stop_hit", bar_date, mfe, mae)
        else:
            mfe = max(mfe, (open_price - lo) / open_price)
            mae = min(mae, (open_price - hi) / open_price)
            if lo <= target:
                return _close_at(pos, target, "target_hit", bar_date, mfe, mae)
            if hi >= stop:
                return _close_at(pos, stop, "stop_hit", bar_date, mfe, mae)

        if exit_target_date and bar_date >= exit_target_date:
            return _close_at(pos, close, "horizon_expired", bar_date, mfe, mae)
        if valid_until and bar_date >= valid_until:
            return _close_at(pos, close, "valid_until_expired", bar_date, mfe, mae)
    return None


def _close_at(pos: dict, close_price: float, reason: str,
              exit_date: date, mfe: float, mae: float) -> dict:
    open_price = float(pos["open_price"])
    direction = pos["direction"]
    # Exit slippage on the close side (same convention as event_paper_agent /
    # price_agent — 5 bps each side, applied to the gross move).
    direction_mult = 1.0 if direction == "long" else -1.0
    raw = (close_price - open_price) / open_price * direction_mult
    net = raw - 2 * SLIPPAGE_PER_SIDE
    notional = float(pos["notional"])
    realized_pnl = round(net * notional, 4)
    return {
        "closed_at":     exit_date.isoformat() + "T20:00:00+00:00",
        "close_price":   round(close_price, 4),
        "close_reason":  reason,
        "realized_pct":  round(net, 6),
        "realized_pnl":  realized_pnl,
        "mfe_pct":       round(mfe, 6),
        "mae_pct":       round(mae, 6),
        "status":        "closed",
    }


def mark_positions(now: datetime) -> int:
    state = get_state()
    opens = sb_get(
        "stock_realistic_loop_positions",
        {
            "loop_name": f"eq.{LOOP_NAME}",
            "status":    "eq.open",
            "order":     "opened_at.asc",
            "limit":     "200",
        },
    )
    if not opens:
        print("[mark] no open positions")
        update_state({"last_mark_at": now.isoformat()})
        return 0

    print(f"[mark] checking {len(opens)} open positions")
    closed_count = 0
    pnl_added = 0.0
    cash_returned = 0.0
    for pos in opens:
        result = evaluate_position(pos, now)
        if not result:
            continue
        sb_patch(
            "stock_realistic_loop_positions",
            {"id": f"eq.{pos['id']}"},
            result,
        )
        closed_count += 1
        pnl_added += float(result["realized_pnl"])
        cash_returned += float(pos["notional"])
        print(f"  [close] {pos['ticker']} {pos['direction']} -> {result['close_reason']} "
              f"@ {result['close_price']} pnl=${result['realized_pnl']:+.2f}")

    if closed_count:
        new_state = get_state()
        new_pnl = float(new_state["cumulative_pnl"]) + pnl_added
        hwm = max(float(new_state["high_water_mark"]), new_pnl)
        drawdown = round(max(float(new_state["max_drawdown"]), hwm - new_pnl), 4)
        update_state({
            "cash_available":   round(float(new_state["cash_available"]) + cash_returned, 2),
            "positions_open":   max(0, new_state["positions_open"] - closed_count),
            "cumulative_pnl":   round(new_pnl, 4),
            "high_water_mark":  round(hwm, 4),
            "max_drawdown":     drawdown,
            "last_mark_at":     now.isoformat(),
        })
    else:
        update_state({"last_mark_at": now.isoformat()})
    return closed_count


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("open", "mark", "both"), default="both")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    print(f"realistic_loop_agent: loop={LOOP_NAME} mode={args.mode} now={now.isoformat()}")

    if args.mode in ("open", "both"):
        opened = open_positions(now)
        print(f"[open] {opened} positions opened")
    if args.mode in ("mark", "both"):
        closed = mark_positions(now)
        print(f"[mark] {closed} positions closed")

    final = get_state()
    print(f"[state] cash=${final['cash_available']} "
          f"open={final['positions_open']}/{final['max_concurrent']} "
          f"pnl=${final['cumulative_pnl']} "
          f"drawdown=${final['max_drawdown']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
