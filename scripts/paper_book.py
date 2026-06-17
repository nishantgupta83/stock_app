#!/usr/bin/env python3
"""Local Paper Book — a parallel BUY/SELL paper portfolio stored in local SQLite.

Event-sourced + deterministic (Codex): trade time is set by EVENT time, not by when
this script runs, so intermittent local runs reproduce an identical portfolio.

  sync     pull NEW tradeable setups from the pipeline (minimal Supabase read) into
           the durable local book_setups table; advance the cursor.
  replay   rebuild the portfolio deterministically from book_setups + price bars:
           entry = next session open on/after the setup's created_at; exit via the
           SAME stop_only logic the pipeline uses (price_agent.compute_paper_outcome:
           cut at stop with gap-fill, ride winners to horizon — NO take-profit);
           capacity-cap to max_concurrent (slot recycles on exit).
  state    print derived cash / cumulative_pnl / drawdown from the ledger.
  dash     write a local HTML dashboard (no Supabase).
  run      sync -> replay -> dash (default).

Reuses the pipeline (consumes stock_trade_setups) + the realistic_loop money math
(agents/_paper_book.py) + compute_paper_outcome. Bars come from yfinance (free; zero
Supabase egress). Default book: paper_book_5k ($5K / 5 concurrent / $1K each).

Env: SUPABASE_URL + SUPABASE_SERVICE_KEY (for `sync` only). Run in a private shell.
"""
from __future__ import annotations
import argparse, datetime as dt, json, os, sys, urllib.parse, urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agents"))
os.environ.setdefault("SUPABASE_URL", "http://offline.local")
os.environ.setdefault("SUPABASE_SERVICE_KEY", "offline")

import _paper_book as eng              # noqa: E402
import _paper_book_store as store      # noqa: E402

LOOP = os.environ.get("PAPER_BOOK_NAME", "paper_book_5k")
DB_PATH = Path(os.environ.get("PAPER_BOOK_DB", ROOT / "paper_book" / "book.db"))
COLD_START_HOURS = int(os.environ.get("PAPER_BOOK_COLD_START_HOURS", "24"))
CAPITAL, MAX_CONC, PER_SIZE = 5000.0, 5, 1000.0


# --- sync: pull setups from the pipeline (minimal incremental read) -------------

def _sb(path: str) -> list[dict]:
    url = os.environ["SUPABASE_URL"].rstrip("/"); key = os.environ["SUPABASE_SERVICE_KEY"]
    rows, off = [], 0
    while True:
        req = urllib.request.Request(f"{url}/rest/v1/{path}",
            headers={"apikey": key, "Authorization": f"Bearer {key}", "Range": f"{off}-{off+999}"})
        page = json.load(urllib.request.urlopen(req)); rows += page
        if len(page) < 1000:
            return rows
        off += 1000


def sync(conn) -> int:
    cur = store.config(conn, LOOP).get("setup_cursor")
    if not cur:
        cur = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=COLD_START_HOURS)).isoformat()
    # minimal columns, only tradeable setups created after the cursor (no count=exact).
    # URL-encode the cursor — its +00:00 offset would otherwise decode to a space.
    cur_q = urllib.parse.quote(cur, safe="")
    rows = _sb("stock_trade_setups?reason_to_skip=is.null"
               f"&created_at=gt.{cur_q}&order=created_at.asc"
               "&select=id,signal_id,ticker,direction,created_at,target_pct,stop_pct,horizon_days,valid_until")
    n_new = 0
    newest = cur
    for r in rows:
        if not r.get("ticker") or not r.get("direction"):
            continue
        if store.ingest_setup(conn, setup_id=r["id"], signal_id=r.get("signal_id"),
                              ticker=r["ticker"], direction=r["direction"],
                              created_at=r["created_at"], target_pct=r.get("target_pct"),
                              stop_pct=r.get("stop_pct"), horizon_days=r.get("horizon_days"),
                              valid_until=r.get("valid_until"), raw=r):
            n_new += 1
        newest = max(newest, r["created_at"])
    store.set_marks(conn, LOOP, setup_cursor=newest)
    print(f"[sync] {len(rows)} setups since cursor, {n_new} new (cursor -> {newest[:19]})")
    return n_new


# --- replay: deterministic open/close from event-time + bars --------------------

import yfinance as yf  # noqa: E402

_BARS: dict[str, dict] = {}


def bars_for(ticker: str, start: dt.date, end: dt.date) -> dict[dt.date, dict]:
    if ticker in _BARS:
        return _BARS[ticker]
    out: dict[dt.date, dict] = {}
    try:
        df = yf.Ticker(ticker).history(start=start.isoformat(),
                                       end=(end + dt.timedelta(days=4)).isoformat(),
                                       auto_adjust=True)
        for ts, row in df.iterrows():
            d = ts.date() if hasattr(ts, "date") else ts.to_pydatetime().date()
            out[d] = {"open": float(row["Open"]), "high": float(row["High"]),
                      "low": float(row["Low"]), "close": float(row["Close"])}
    except Exception as e:  # noqa: BLE001
        print(f"  {ticker}: bar fetch failed — {e}", file=sys.stderr)
    _BARS[ticker] = out
    return out


def _next_session(bars: dict[dt.date, dict], on_or_after: dt.date):
    for d in sorted(bars):
        if d >= on_or_after:
            return d, bars[d]
    return None


def replay(conn) -> None:
    from price_agent import compute_paper_outcome  # reuse the pipeline's stop_only grader
    setups = store.all_setups(conn)
    today = dt.datetime.now(dt.timezone.utc).date()
    candidates: list[dict] = []
    for s in setups:
        created = dt.date.fromisoformat(s["created_at"][:10])
        # enter the NEXT session on/after the setup (a trader acting the next morning)
        bars = bars_for(s["ticker"], created, today)
        if not bars:
            continue
        entry = _next_session(bars, created + dt.timedelta(days=1))
        if not entry:
            continue                       # no session yet — too fresh, revisit next run
        entry_date, entry_bar = entry
        entry_price = entry_bar["open"]
        trade = {"entry_at": entry_date.isoformat() + "T00:00:00+00:00",
                 "entry_price": entry_price, "direction": s["direction"],
                 "horizon_days": int(s.get("horizon_days") or 30),
                 "target_pct": s.get("target_pct"), "stop_pct": s.get("stop_pct")}
        outcome = compute_paper_outcome(trade, bars, exit_policy="stop_only")
        candidates.append({
            "setup_id": s["setup_id"], "signal_id": s.get("signal_id"), "ticker": s["ticker"],
            "direction": s["direction"], "entry_at": entry_date.isoformat(),
            "entry_price": entry_price, "horizon_days": trade["horizon_days"],
            "target_pct": s.get("target_pct"), "stop_pct": s.get("stop_pct"),
            "valid_until": s.get("valid_until"),
            "exit_at": (outcome["exit_at"][:10] if outcome else None),
            "outcome": outcome,
        })
    admitted = eng.admit_positions(candidates, MAX_CONC)
    opened = closed = 0
    for c in admitted:
        if store.open_position(conn, setup_id=c["setup_id"], signal_id=c["signal_id"],
                               ticker=c["ticker"], direction=c["direction"],
                               opened_at=c["entry_at"] + "T00:00:00+00:00",
                               open_price=round(c["entry_price"], 4), notional=PER_SIZE,
                               target_pct=c["target_pct"], stop_pct=c["stop_pct"],
                               horizon_days=c["horizon_days"], exit_target_date=c["exit_at"],
                               valid_until=c["valid_until"]):
            opened += 1
        o = c["outcome"]
        if o is not None:                  # a determined exit (stop or horizon) exists
            pid = next(p["id"] for p in store.all_positions(conn) if p["setup_id"] == c["setup_id"])
            row = next(p for p in store.all_positions(conn) if p["id"] == pid)
            if row["status"] == "open":
                pct, pnl = eng.close_position(c["entry_price"], o["exit_price"],
                                              c["direction"], PER_SIZE)
                store.close_position(conn, pid, closed_at=o["exit_at"], close_price=o["exit_price"],
                                     close_reason=o["exit_reason"], realized_pct=pct, realized_pnl=pnl,
                                     mfe_pct=o.get("mfe_pct"), mae_pct=o.get("mae_pct"))
                closed += 1
    store.set_marks(conn, LOOP, last_open_scan_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                    last_mark_at=dt.datetime.now(dt.timezone.utc).isoformat())
    print(f"[replay] {len(candidates)} candidates, {len(admitted)} admitted, "
          f"{opened} opened, {closed} closed")


def print_state(conn) -> dict:
    s = eng.recompute_state(store.all_positions(conn), CAPITAL)
    print(f"[state] cash=${s['cash_available']} open={s['positions_open']}/{MAX_CONC} "
          f"pnl=${s['cumulative_pnl']:+} hwm=${s['high_water_mark']} dd=${s['max_drawdown']}")
    return s


def dashboard(conn) -> Path:
    from paper_book_dashboard import render
    html = render(conn, LOOP, CAPITAL, MAX_CONC)
    out = DB_PATH.parent / "index.html"          # index.html so a local server root serves it
    out.write_text(html)
    (DB_PATH.parent / "dashboard.html").write_text(html)
    print(f"[dash] {out}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("mode", nargs="?", default="run",
                    choices=["sync", "replay", "state", "dash", "run"])
    args = ap.parse_args()
    conn = store.connect(DB_PATH)
    store.init_state(conn, loop_name=LOOP, capital_base=CAPITAL,
                     max_concurrent=MAX_CONC, per_size=PER_SIZE)
    if args.mode in ("sync", "run"):
        sync(conn)
    if args.mode in ("replay", "run"):
        replay(conn)
    if args.mode in ("state", "run"):
        print_state(conn)
    if args.mode in ("dash", "run"):
        dashboard(conn)
    return 0


if __name__ == "__main__":
    sys.exit(main())
