"""
Intraday alert agent — fast-twitch price spike notifier.

Runs every 15 min during US market hours (Mon–Fri 13:30–21:00 UTC).
Pulls latest prices for every tradeable ticker (kind=stock|etf), computes
intraday % change vs prior close, and sends an immediate Telegram alert for
any move >= SPIKE_PCT (5%). Each alert includes recent normalized_events
context so you know WHY it moved.

This is the LITE-spike-fix: the previous bottleneck was that thesis_agent
runs hourly and only dispatches mature/clustered signals — a single +17%
move on a non-mature ticker was invisible. This agent scans all tickers
every 15 minutes and pings unconditionally on big moves.

Dedupe: one alert per ticker per UTC day. Stored in stock_signals with
action='WATCH' and dedupe_key='intraday_spike_TICKER_YYYY-MM-DD' so the
existing telegram_dispatcher infrastructure handles delivery + logging.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests
import yfinance as yf

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from filing_agent import (  # type: ignore
    job_run_start, job_run_finish, SUPABASE_URL, HEADERS_SB,
)

SPIKE_PCT     = 0.05            # 5% intraday move triggers alert
VOLUME_MULT   = 2.0             # alert also when volume > 2× 20-day avg (high conviction)
ALERT_CAP     = 25              # hard cap per run (safety)
BOT_TOKEN     = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID       = os.environ.get("TELEGRAM_CHAT_ID", "")


# ============================================================
# Watchlist fetch
# ============================================================

def fetch_tradeable_tickers() -> list[str]:
    """All watchlist tickers where stock_symbols.kind IN (stock,etf)."""
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/stock_watchlists"
        f"?select=ticker,stock_symbols!inner(kind)"
        f"&stock_symbols.kind=in.(stock,etf)",
        headers=HEADERS_SB, timeout=15,
    )
    if r.status_code != 200:
        print(f"  fetch_tradeable_tickers: {r.status_code} {r.text[:200]}", file=sys.stderr)
        return []
    seen = set()
    out = []
    for row in r.json():
        t = row.get("ticker")
        if t and t not in seen:
            seen.add(t)
            out.append(t)
    return sorted(out)


# ============================================================
# Price + volume snapshot
# ============================================================

def fetch_intraday_snapshot(tickers: list[str]) -> dict[str, dict]:
    """{ticker: {prev_close, current, pct, vol, avg_vol, vol_mult}} via yfinance.

    Batches via yf.download to keep this fast (one HTTP request).
    """
    if not tickers:
        return {}
    try:
        df = yf.download(
            tickers=" ".join(tickers),
            period="5d",
            interval="1d",
            group_by="ticker",
            progress=False,
            auto_adjust=False,
            threads=True,
        )
    except Exception as e:
        print(f"  yf.download failed: {e}", file=sys.stderr)
        return {}

    snapshots: dict[str, dict] = {}
    for t in tickers:
        try:
            sub = df[t] if len(tickers) > 1 else df
            closes = sub["Close"].dropna()
            vols   = sub["Volume"].dropna()
            if len(closes) < 2:
                continue
            current = float(closes.iloc[-1])
            prev    = float(closes.iloc[-2])
            if prev <= 0:
                continue
            pct = (current - prev) / prev
            cur_vol = int(vols.iloc[-1]) if len(vols) else 0
            avg_vol = int(vols.iloc[:-1].mean()) if len(vols) > 1 else 0
            vol_mult = (cur_vol / avg_vol) if avg_vol > 0 else 0.0
            snapshots[t] = {
                "prev":     round(prev, 4),
                "current":  round(current, 4),
                "pct":      round(pct, 6),
                "vol":      cur_vol,
                "avg_vol":  avg_vol,
                "vol_mult": round(vol_mult, 2),
            }
        except Exception:
            continue
    return snapshots


# ============================================================
# Context — recent events for the spiking ticker
# ============================================================

def recent_events_context(ticker: str, hours: int = 168) -> list[dict]:
    """Pull recent normalized_events for context in the alert body.

    Filter by event_at (real-world event date) NOT created_at (ingest time),
    otherwise a backfill makes 6-month-old earnings look like 'recent' events.
    7-day window catches the actual catalyst — earnings release, 8-K, etc.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/stock_normalized_events",
        headers=HEADERS_SB,
        params=[
            ("ticker",      f"eq.{ticker}"),
            ("event_at",    f"gte.{cutoff}"),
            ("select",      "event_type,event_subtype,severity,payload,event_at"),
            ("order",       "severity.desc,event_at.desc"),
            ("limit",       "5"),
        ],
        timeout=10,
    )
    if r.status_code != 200:
        return []
    return r.json()


def format_context(events: list[dict]) -> str:
    """Compact human-readable summary of recent events for the alert body.
    Each event tagged with its real date so users see WHEN the catalyst hit."""
    if not events:
        return "no catalyst in last 7d — check news/sector rotation"
    parts = []
    for e in events[:3]:
        et = e.get("event_type") or "?"
        sub = e.get("event_subtype") or ""
        sev = e.get("severity") or 0
        date = (e.get("event_at") or "")[:10]
        date_tag = f" ({date})" if date else ""
        if et == "earnings_release":
            payload = e.get("payload") or {}
            surp = payload.get("surprise_pct")
            tag = f"earnings {sub}"
            if surp is not None:
                try:
                    tag += f" {float(surp):+.1f}%"
                except (TypeError, ValueError):
                    pass
            parts.append(f"{tag}{date_tag}")
        elif et == "8k_material_event":
            parts.append(f"8-K sev{sev}{date_tag}")
        elif et.startswith("filing_"):
            parts.append(f"{et.replace('filing_', '').upper()}{date_tag}")
        elif et == "truth_social_post":
            parts.append(f"Trump: {sub}{date_tag}")
        elif et == "news_article":
            parts.append(f"news ({sub}){date_tag}")
        elif et.startswith("institutional_"):
            parts.append(f"{et.replace('institutional_','inst-')} {sub}{date_tag}")
        elif et == "crypto_macro_move":
            parts.append(f"crypto {sub}{date_tag}")
        else:
            parts.append(f"{et}{date_tag}")
    return " · ".join(parts)


# ============================================================
# Dedupe via stock_signals
# ============================================================

def dedupe_key_for(ticker: str) -> str:
    today = datetime.now(timezone.utc).date().isoformat()
    return f"intraday_spike_{ticker}_{today}"


def already_alerted_today(ticker: str) -> bool:
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/stock_signals",
        headers=HEADERS_SB,
        params={
            "dedupe_key": f"eq.{dedupe_key_for(ticker)}",
            "select":     "id",
            "limit":      "1",
        },
        timeout=10,
    )
    return r.status_code == 200 and bool(r.json())


# ============================================================
# Alert send — direct Telegram + signal log
# ============================================================

def send_telegram(text: str) -> tuple[bool, int | None, str | None]:
    if not BOT_TOKEN or not CHAT_ID:
        return False, None, "missing TELEGRAM env"
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
                  "disable_web_page_preview": "true"},
            timeout=15,
        )
        ok = r.status_code == 200 and r.json().get("ok", False)
        msg_id = r.json().get("result", {}).get("message_id") if ok else None
        err = None if ok else r.text[:300]
        return ok, msg_id, err
    except Exception as e:  # noqa: BLE001
        return False, None, str(e)


def insert_spike_signal(ticker: str, snap: dict, context: str) -> int | None:
    """Create a stock_signals row so the alert is auditable and dedupe works."""
    direction = "bullish" if snap["pct"] > 0 else "bearish"
    action    = "WATCH" if snap["pct"] > 0 else "AVOID_CHASE"
    payload = {
        "ticker":           ticker,
        "fired_at":         datetime.now(timezone.utc).isoformat(),
        "direction":        direction,
        "confidence":       min(0.99, abs(snap["pct"]) * 8),  # 5% → 0.4, 12% → 0.96
        "horizon_days":     1,
        "thesis_summary":   f"intraday spike {snap['pct']*100:+.1f}% · {context}"[:240],
        "model_version":    "intraday-spike-v1",
        "weight_at_time":   {"agents": ["intraday"], "snap": snap},
        "status":           "open",
        "action":           action,
        "score":            min(100, int(abs(snap["pct"]) * 1000)),  # 5% → 50, 10% → 100
        "score_breakdown":  {"items": [
            {"rule": "intraday_pct_move",  "points": round(abs(snap["pct"]) * 100, 2),
             "detail": f"{snap['pct']*100:+.2f}% close-to-close"},
            {"rule": "vol_multiplier",     "points": min(20, snap["vol_mult"]),
             "detail": f"{snap['vol_mult']}× 20d avg"},
        ]},
        "evidence_summary": f"{snap['pct']*100:+.1f}% intraday — {context}"[:240],
        "dedupe_key":       dedupe_key_for(ticker),
        "status_v2":        "candidate",
    }
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/stock_signals",
        headers={**HEADERS_SB, "Prefer": "return=representation"},
        json=payload, timeout=15,
    )
    if r.status_code in (200, 201) and r.json():
        return r.json()[0]["id"]
    print(f"  insert_spike_signal {ticker}: {r.status_code} {r.text[:200]}", file=sys.stderr)
    return None


def format_spike_alert(ticker: str, snap: dict, context: str) -> str:
    pct = snap["pct"] * 100
    arrow = "📈" if pct > 0 else "📉"
    direction = "UP" if pct > 0 else "DOWN"
    vol_tag = f" · vol {snap['vol_mult']:.1f}×" if snap['vol_mult'] >= 1.5 else ""
    return (
        f"{arrow} <b>{ticker}</b> {direction} <b>{pct:+.1f}%</b>\n"
        f"${snap['prev']:.2f} → ${snap['current']:.2f}{vol_tag}\n"
        f"<i>{context}</i>\n"
        f"⏱ {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
    )


# ============================================================
# Main
# ============================================================

def main() -> int:
    started = time.time()
    run_id = job_run_start("intraday_alert_agent")
    n_alerts = 0
    n_checked = 0

    try:
        tickers = fetch_tradeable_tickers()
        n_checked = len(tickers)
        if not tickers:
            print("no tradeable tickers — abort")
            job_run_finish(run_id, "ok", 0, 0)
            return 0
        print(f"Scanning {n_checked} tickers for intraday spikes...")

        snapshots = fetch_intraday_snapshot(tickers)
        spikes = [(t, s) for t, s in snapshots.items()
                  if abs(s["pct"]) >= SPIKE_PCT
                  or (abs(s["pct"]) >= 0.02 and s["vol_mult"] >= VOLUME_MULT)]
        # Sort by abs move desc — most extreme first
        spikes.sort(key=lambda x: abs(x[1]["pct"]), reverse=True)

        if not spikes:
            print(f"No spikes detected (threshold {SPIKE_PCT*100:.0f}%)")
            job_run_finish(run_id, "ok", n_checked, 0)
            return 0

        print(f"Detected {len(spikes)} spike(s) — alerting (cap {ALERT_CAP})")
        for ticker, snap in spikes[:ALERT_CAP]:
            if already_alerted_today(ticker):
                print(f"  {ticker}: dedupe — already alerted today, skip")
                continue
            ctx_events = recent_events_context(ticker)
            context_str = format_context(ctx_events)
            sig_id = insert_spike_signal(ticker, snap, context_str)
            if sig_id is None:
                continue
            text = format_spike_alert(ticker, snap, context_str)
            ok, msg_id, err = send_telegram(text)
            status = "sent" if ok else "dispatch_failed"
            requests.patch(
                f"{SUPABASE_URL}/rest/v1/stock_signals?id=eq.{sig_id}",
                headers=HEADERS_SB,
                json={"status_v2": status}, timeout=10,
            )
            if ok:
                n_alerts += 1
                print(f"  {ticker}: ALERT SENT {snap['pct']*100:+.1f}% (sig_id={sig_id})")
            else:
                print(f"  {ticker}: dispatch failed — {err}", file=sys.stderr)

        elapsed = time.time() - started
        print(f"DONE in {elapsed:.1f}s — checked {n_checked}, alerted {n_alerts}")
        job_run_finish(run_id, "ok", n_checked, n_alerts)
        return 0

    except Exception as e:  # noqa: BLE001
        import traceback
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        job_run_finish(run_id, "failed", n_checked, n_alerts, err=str(e))
        return 1


if __name__ == "__main__":
    sys.exit(main())
