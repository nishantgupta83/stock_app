#!/usr/bin/env python3
"""Intraday SOXL/SOXS checkpoints, sent to Telegram at three fixed Pacific times.

    06:35 PT (09:35 ET)  opening read  -- the first five minutes, where both of the
                         operator's first two trades bottomed
    07:00 PT (10:00 ET)  VWAP check    -- the measured filter: above session VWAP at
                         10:00 ET -> +1.78% open->close (n=31); below -> -3.02% (n=29)
    08:00 PT (11:00 ET)  VWAP check

Every slot sends, so silence always means the job failed and never "nothing happened".
Triggers (VWAP break, re-cross, opening-range break) are flagged loudly in the header.

What this is NOT: a signal that a trade is worth taking. Across a 3x3 grid of entry
times and exit rules on 60 sessions, nothing beat its null convincingly; the one effect
that held in every cell was that a VWAP exit caps the worst day at -3.5%/-5.4% versus
-17%/-19% for hold-to-close. This tool reports state and manages an open position. The
measured setup lives on the daily chart (semis_brief.py band section).

Sampling limitation: this polls three times a day. `first_below` is cumulative, so a VWAP
break is never missed outright -- but any number of break/recover round trips between two
checkpoints collapses into whichever state holds at the later ping, and a break that
recovers before the next slot reports "VWAP BREAK" and "ABOVE VWAP" in the same message.

State: semis_brief/intraday/YYYY-MM-DD.json -- one writer (this script), append-only
carrying a `fired` ledger of every trigger already sent today, so a re-run of a slot --
or a lost prior-slot record -- cannot re-fire an alert that already went out.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import semis_brief as sb  # noqa: E402  (reuses finite/destitch/bollinger/band_state/telegram)

PT, ET = sb.PT, sb.ET
STATE = sb.STATE / "intraday"
TICKERS = ["SOXL", "SOXS"]
CONTEXT = "SOXX"
OPEN_ET, CLOSE_ET = time(9, 30), time(16, 0)
OR_END_ET = time(10, 0)             # opening range = 09:30-10:00 ET
ATR_N = 14
SLOT_TOLERANCE_MIN = 12             # a GHA cron can land late; still fire, but label it
SLOT_EARLY_MIN = -2                 # ...and a pinger can land a shade early; do not drop it

SLOTS = {                           # slot -> nominal Pacific wall-clock time
    "open": time(6, 35),
    "t1000et": time(7, 0),
    "t1100et": time(8, 0),
}
SLOT_LABEL = {
    "open": "OPENING READ (09:35 ET)",
    "t1000et": "VWAP CHECK (10:00 ET)",
    "t1100et": "VWAP CHECK (11:00 ET)",
}


# ----------------------------------------------------------------- pure functions

def slot_for(now_pt: datetime) -> str | None:
    """Which checkpoint this run belongs to, or None if it is outside all of them.
    Weekends are rejected here so the workflow needs no separate guard."""
    if now_pt.weekday() >= 5 or not sb.is_trading_day(now_pt.date()):
        return None
    for name, t in SLOTS.items():
        nominal = now_pt.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
        delta = (now_pt - nominal).total_seconds() / 60.0
        if SLOT_EARLY_MIN <= delta <= SLOT_TOLERANCE_MIN:
            return name
    return None


def session_bars(bars: list[dict], day: date) -> list[dict]:
    """Regular-session bars for `day` only, chronological. bars: {ts(aware), o,h,l,c,v}."""
    out = []
    for b in bars:
        e = b["ts"].astimezone(ET)
        if e.date() == day and OPEN_ET <= e.time() < CLOSE_ET and sb.finite(b["close"]):
            out.append(b)
    return out


def running_vwap(bars: list[dict]) -> list[float | None]:
    """Session VWAP after each bar. Typical price x volume, cumulative.
    None while cumulative volume is zero (a halted or untraded open)."""
    out, pv, vol = [], 0.0, 0.0
    for b in bars:
        h, lo, c = sb.finite(b.get("high")), sb.finite(b.get("low")), sb.finite(b.get("close"))
        v = sb.finite(b.get("volume")) or 0.0
        tp = (h + lo + c) / 3 if (h is not None and lo is not None and c is not None) else c
        if tp is None:
            out.append(out[-1] if out else None)
            continue
        pv += tp * v
        vol += v
        out.append(pv / vol if vol > 0 else None)
    return out


def vwap_events(bars: list[dict], vwap: list[float | None]) -> dict:
    """Closes below VWAP, and the index of the FIRST one. A wick below does not count --
    the rule that capped the worst day in every backtested cell used the bar CLOSE."""
    below = [i for i, (b, v) in enumerate(zip(bars, vwap))
             if v is not None and sb.finite(b["close"]) is not None and b["close"] < v]
    return {"n_below": len(below), "first_below": below[0] if below else None,
            "last_below": below[-1] if below else None, "n_bars": len(bars)}


def opening_range(bars: list[dict]) -> dict | None:
    """High/low of 09:30-10:00 ET. None until the window has closed."""
    w = [b for b in bars if b["ts"].astimezone(ET).time() < OR_END_ET]
    if not w:
        return None
    his = [sb.finite(b.get("high")) for b in w]
    los = [sb.finite(b.get("low")) for b in w]
    his = [x for x in his if x is not None]
    los = [x for x in los if x is not None]
    if not his or not los:
        return None
    complete = any(b["ts"].astimezone(ET).time() >= OR_END_ET for b in bars)
    return {"high": max(his), "low": min(los), "complete": complete}


def split_dates(ticker: str) -> set[str]:
    """Dates yfinance itself reports as splits. SPLIT_RATIO_RANGE only catches a ratio
    outside (0.25, 4.0), so a 2:1 or 3:1 split would slip through it untouched -- and this
    module reads a full year of high/low/volume, not the 20 closes the brief needs. The
    splits feed is the honest guard; the ratio band stays as the fallback for the case
    that bit us (SOXS 2026-05-26, a discontinuity matching NO date in this feed)."""
    try:
        sp = sb._yf().Ticker(ticker).splits
    except Exception:
        return set()
    try:
        return {ts.date().isoformat() for ts, v in sp.items() if sb.finite(v) and v != 1.0}
    except Exception:
        return set()


def destitch_ohlcv(daily: list[dict], splits: set[str] | None = None) -> list[dict]:
    """semis_brief.destitch only rescales open/close. Daily ATR and anchored VWAP also need
    high/low, and volume moves the OTHER way across a reverse split (20:1 up in price is
    20:1 down in share count). Derive the per-bar factor from the tested close rescaling
    and apply it to every field. Without this, SOXS reports ATR 17.1% and an anchored VWAP
    of 98.64 against a price of 32.42 -- both artifacts of the unstitched 2026-05-26 split.
    """
    if not daily:
        return []
    fixed = sb.destitch([{"date": d["date"], "open": d.get("open") or d["close"],
                          "close": d["close"]} for d in daily])
    # Second pass for splits the ratio band cannot see (anything between 4:1 and 1:4).
    # Walk backwards exactly as destitch does, folding in each reported split ratio that
    # the first pass did not already remove.
    if splits:
        extra = 1.0
        for i in range(len(fixed) - 1, 0, -1):
            fixed[i] = {**fixed[i], "close": fixed[i]["close"] * extra,
                        "open": fixed[i]["open"] * extra}
            prev, cur = fixed[i - 1]["close"], fixed[i]["close"]
            if fixed[i]["date"] in splits and prev > 0:
                r = cur / prev
                if sb.SPLIT_RATIO_RANGE[0] <= r <= sb.SPLIT_RATIO_RANGE[1]:
                    extra *= r          # first pass skipped it; fold it in now
        fixed[0] = {**fixed[0], "close": fixed[0]["close"] * extra,
                    "open": fixed[0]["open"] * extra}
    out = []
    for src, fx in zip(daily, fixed):
        f = (fx["close"] / src["close"]) if src.get("close") else 1.0
        row = dict(src)
        for k in ("open", "high", "low", "close"):
            if sb.finite(row.get(k)) is not None:
                row[k] = row[k] * f
        if sb.finite(row.get("volume")) is not None and f:
            row["volume"] = row["volume"] / f
        out.append(row)
    return out


def atr(daily: list[dict], n: int = ATR_N) -> float | None:
    """Wilder ATR over chronological daily bars {high, low, close}."""
    rows = [d for d in daily
            if all(sb.finite(d.get(k)) is not None for k in ("high", "low", "close"))]
    if len(rows) < n + 1:
        return None
    val = None
    for i in range(1, len(rows)):
        pc = rows[i - 1]["close"]
        tr = max(rows[i]["high"] - rows[i]["low"],
                 abs(rows[i]["high"] - pc), abs(rows[i]["low"] - pc))
        val = tr if val is None else val + (tr - val) / n
    return val


def anchored_vwap(daily: list[dict], start_idx: int) -> float | None:
    """Volume-weighted typical price from start_idx to the end. The anchor is an event
    (a high, a low), never 'N days ago' -- that is a property of the chart button."""
    pv = vol = 0.0
    for d in daily[start_idx:]:
        h, lo, c = sb.finite(d.get("high")), sb.finite(d.get("low")), sb.finite(d.get("close"))
        v = sb.finite(d.get("volume"))
        if None in (h, lo, c) or not v:
            continue
        pv += (h + lo + c) / 3 * v
        vol += v
    return pv / vol if vol > 0 else None


def anchors(daily: list[dict]) -> dict:
    """The two anchors worth drawing: the highest close (the down-move's cost basis) and
    the lowest close since it (the rally's cost basis)."""
    closes = [sb.finite(d.get("close")) for d in daily]
    idx = [i for i, c in enumerate(closes) if c is not None]
    if len(idx) < 2:
        return {}
    hi = max(idx, key=lambda i: closes[i])
    after = [i for i in idx if i > hi]
    lo = min(after, key=lambda i: closes[i]) if after else None
    out = {"peak": {"date": daily[hi]["date"], "px": closes[hi],
                    "avwap": anchored_vwap(daily, hi)}}
    if lo is not None:
        out["trough"] = {"date": daily[lo]["date"], "px": closes[lo],
                         "avwap": anchored_vwap(daily, lo)}
    return out


def assess(ticker: str, bars: list[dict], daily: list[dict], shares: int) -> dict:
    """Everything the ping reports for one ticker. Pure: no IO, no clock."""
    vw = running_vwap(bars)
    last = bars[-1] if bars else None
    v = vw[-1] if vw else None
    px = sb.finite(last["close"]) if last else None
    ev = vwap_events(bars, vw)
    orng = opening_range(bars)
    a = atr(daily)
    op = sb.finite(bars[0]["open"]) if bars and sb.finite(bars[0].get("open")) else None
    return {
        "price": px, "vwap": v,
        "vs_vwap": (px / v - 1) if (px and v) else None,
        "above_vwap": (px > v) if (px is not None and v is not None) else None,
        "session_open": op,
        "from_open": (px / op - 1) if (px and op) else None,
        "vwap_events": ev,
        "opening_range": orng,
        # While the 09:30-10:00 window is still forming, `px` is inside it by construction
        # (it IS one of the bars that defines the high/low), so "inside" would be a
        # tautology dressed up as an observation. Report nothing until the range closes.
        "or_break": (None if not (orng and px and orng.get("complete")) else
                     "above" if px > orng["high"] else "below" if px < orng["low"] else "inside"),
        "atr": a,
        "atr_pct": (a / px) if (a and px) else None,
        "noise_dollars": (a * shares) if a else None,
        "anchors": anchors(daily),
    }


def triggers(cur: dict, prev: dict | None) -> list[str]:
    """Only state CHANGES since the previous slot are loud. Re-running a slot re-derives
    the same state and therefore fires nothing new."""
    out = []
    ev, pev = cur.get("vwap_events") or {}, (prev or {}).get("vwap_events") or {}
    if ev.get("first_below") is not None and pev.get("first_below") is None:
        out.append("VWAP BREAK — first 5-min close below session VWAP")
    if (prev and prev.get("above_vwap") is False and cur.get("above_vwap") is True):
        out.append("BACK ABOVE VWAP")
    if (prev and prev.get("above_vwap") is True and cur.get("above_vwap") is False):
        out.append("NOW BELOW VWAP")
    if cur.get("or_break") == "above" and (prev or {}).get("or_break") in (None, "inside", "below"):
        out.append("OPENING-RANGE BREAK UP")
    if cur.get("or_break") == "below" and (prev or {}).get("or_break") in (None, "inside", "above"):
        out.append("OPENING-RANGE BREAK DOWN")
    return out


def fmt_money(x, d=2):
    return "n/a" if x is None else f"{x:,.{d}f}"


def fmt_pct(x, d=2):
    return "n/a" if x is None else f"{x*100:+.{d}f}%"


def render(slot: str, now_pt: datetime, ctx: dict | None, per: dict,
           fired: dict[str, list[str]], shares: int, late: int) -> tuple[str, str]:
    """(subject, text). Kept well under the 4096-char Telegram limit."""
    # Scope every trigger to its ticker: SOXS is the inverse fund, so "VWAP BREAK" on
    # SOXS and on SOXL mean opposite things and must never be merged into one banner.
    hot = [f"{tk}: {msg.split(' — ')[0]}" for tk in TICKERS for msg in fired.get(tk, [])]
    head = ("*** " + "  |  ".join(hot) + " ***") if hot else SLOT_LABEL[slot]
    subj = f"SOXL/SOXS {SLOT_LABEL[slot]}" + (f" — {hot[0]}" if hot else "")
    L = [head, f"{now_pt:%a %Y-%m-%d %H:%M} PT" + (f"  (ran {late} min late)" if late >= 3 else ""), ""]

    if ctx:
        st = ctx.get("band_state") or {}
        if st:
            L.append(f"SOXX daily (as of {st.get('as_of')}): {st['zone'].replace('_', ' ')} — "
                     f"close {fmt_money(st['close'])} "
                     f"(lower {fmt_money(st['lower'])} / mid {fmt_money(st['middle'])} / "
                     f"upper {fmt_money(st['upper'])}), %B "
                     f"{'n/a' if st.get('pct_b') is None else format(st['pct_b'], '.2f')}")
        if ctx.get("price") is not None:
            L.append(f"SOXX now {fmt_money(ctx['price'])} ({fmt_pct(ctx.get('from_open'))} from open), "
                     f"VWAP {fmt_money(ctx.get('vwap'))} — "
                     f"{'ABOVE' if ctx.get('above_vwap') else 'below' if ctx.get('above_vwap') is False else 'n/a'}")
        L.append("")

    for t in TICKERS:
        a = per.get(t) or {}
        if a.get("price") is None:
            L += [f"{t}: no bars", ""]
            continue
        rel = ("ABOVE" if a["above_vwap"] else "BELOW") if a["above_vwap"] is not None else "n/a"
        L.append(f"{t} {fmt_money(a['price'])}  ({fmt_pct(a['from_open'])} from open)")
        L.append(f"   VWAP {fmt_money(a['vwap'])} — {rel} by {fmt_pct(a['vs_vwap'])}")
        ev = a["vwap_events"]
        L.append(f"   closes below VWAP today: {ev['n_bars'] and ev['n_below']}/{ev['n_bars']} bars"
                 + ("  <- exit trigger is the FIRST one" if ev["n_below"] == 0 else ""))
        orng = a.get("opening_range")
        if orng:
            L.append(f"   opening range {fmt_money(orng['low'])}–{fmt_money(orng['high'])}"
                     f"{'' if orng['complete'] else ' (still forming)'} — now {a['or_break']}")
        if a.get("atr"):
            ap_ = "n/a" if a.get("atr_pct") is None else f"{a['atr_pct']*100:.1f}%"
            L.append(f"   ATR(14) ${fmt_money(a['atr'])} ({ap_} of price) "
                     f"= ${fmt_money(a['noise_dollars'], 0)} of normal daily movement on {shares} sh")
        an = a.get("anchors") or {}
        bits = [f"{k} {v['date']} AVWAP {fmt_money(v['avwap'])} ({fmt_pct(a['price'] / v['avwap'] - 1)})"
                for k, v in an.items() if v.get("avwap")]
        if bits:
            L.append("   anchored VWAP: " + " · ".join(bits))
        if fired.get(t):
            L.append("   >>> " + "; ".join(fired[t]))
        L.append("")

    L.append("VWAP break = first 5-min CLOSE below the line, not a wick.")
    return subj, "\n".join(L)


# ----------------------------------------------------------------------- IO

def bars_5m(ticker: str) -> list[dict]:
    try:
        h = sb._yf().Ticker(ticker).history(period="2d", interval="5m", prepost=False,
                                            auto_adjust=False, timeout=sb.HTTP_TIMEOUT)
    except Exception:
        return []
    return [{"ts": ts.to_pydatetime(), "open": sb.finite(r["Open"]), "high": sb.finite(r["High"]),
             "low": sb.finite(r["Low"]), "close": sb.finite(r["Close"]),
             "volume": sb.finite(r["Volume"])} for ts, r in h.iterrows()]


def daily_ohlcv(ticker: str, period: str = "1y") -> list[dict]:
    try:
        h = sb._yf().Ticker(ticker).history(period=period, interval="1d", auto_adjust=False,
                                            timeout=sb.HTTP_TIMEOUT)
    except Exception:
        return []
    rows = [{"date": ts.date().isoformat(), "open": sb.finite(r["Open"]),
             "high": sb.finite(r["High"]), "low": sb.finite(r["Low"]),
             "close": sb.finite(r["Close"]), "volume": sb.finite(r["Volume"])}
            for ts, r in h.iterrows() if sb.finite(r["Close"]) is not None]
    return destitch_ohlcv(rows, split_dates(ticker))


def band_context(day: date) -> dict | None:
    """SOXX daily band state from strictly-prior sessions, split-artifact corrected."""
    bars = [{"date": d.isoformat(), "open": sb.finite(v.get("open")) or sb.finite(v.get("close")),
             "close": sb.finite(v.get("close"))}
            for d, v in sorted(sb.daily_bars(CONTEXT, "5y").items())
            if d < day and sb.finite(v.get("close")) is not None]
    return sb.band_state(sb.destitch(bars)) if len(bars) > sb.BAND_N else None


def cmd_ping(force_slot: str | None, dry: bool) -> int:
    now_pt = datetime.now(PT)
    slot = force_slot or slot_for(now_pt)
    if slot is None:
        print(f"outside every checkpoint window ({now_pt:%a %H:%M} PT) — nothing to do")
        return 0
    nominal = now_pt.replace(hour=SLOTS[slot].hour, minute=SLOTS[slot].minute,
                             second=0, microsecond=0)
    late = 0 if force_slot else max(0, int((now_pt - nominal).total_seconds() // 60))
    day = now_pt.date()

    shares = int(os.environ.get("SEMIS_SHARES", "25"))
    path = STATE / f"{day.isoformat()}.json"
    book = sb.read_json(path, {"date": day.isoformat(), "slots": {}})
    order = list(SLOTS)
    # Include the CURRENT slot's own stored record: a re-run (manual dispatch, "Re-run all
    # jobs", a duplicate cron) must compare against what it itself already sent, not None.
    prior = [book["slots"][s] for s in order[:order.index(slot) + 1] if s in book["slots"]]
    already = set(book.get("fired", []))

    per, fired = {}, {}
    for t in TICKERS:
        bars = session_bars(bars_5m(t), day)
        a = assess(t, bars, daily_ohlcv(t), shares)
        prev = None
        for p in reversed(prior):
            if p.get("per", {}).get(t):
                prev = p["per"][t]; break
        per[t] = a
        # Belt and braces: even if `prev` is lost (a failed commit step means the day file
        # never landed and checkout is fetch-depth 1), a trigger already sent today is not
        # sent again. The ledger, not the previous slot's state, is the source of truth.
        fired[t] = [x for x in triggers(a, prev) if f"{t}:{x}" not in already]

    ctx = None
    cb = session_bars(bars_5m(CONTEXT), day)
    if cb:
        ctx = assess(CONTEXT, cb, daily_ohlcv(CONTEXT), shares)
    bs = band_context(day)
    if bs:
        ctx = (ctx or {}) | {"band_state": bs}

    subj, text = render(slot, now_pt, ctx, per, fired, shares, late)
    print(text)
    if dry:
        return 0

    if "telegram" not in sb.channels():
        # The contract is "every slot sends", so silence must mean the job failed. Exiting 0
        # here would make a missing TELEGRAM_BOT_TOKEN indistinguishable from a quiet market.
        print("::error::TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID not configured — nothing sent")
        return 1
    sent = sb.send_telegram(subj, text, "")
    book["slots"][slot] = {"at": now_pt.isoformat(timespec="seconds"), "late_min": late,
                           "per": per, "triggers": fired, "sent": sent}
    book["fired"] = sorted(already | {f"{t}:{x}" for t, v in fired.items() for x in v})
    sb.write_json(path, book)
    print(f"slot={slot} sent={sent} -> {path.relative_to(sb.REPO)}")
    return 0


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--slot", choices=list(SLOTS), help="force a checkpoint (testing)")
    ap.add_argument("--dry-run", action="store_true", help="render only, send nothing, write nothing")
    a = ap.parse_args()
    return cmd_ping(a.slot, a.dry_run)


if __name__ == "__main__":
    sys.exit(main())
