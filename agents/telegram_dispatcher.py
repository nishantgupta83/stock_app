"""
Telegram dispatcher.

Imported by thesis_agent.py — not run standalone in Phase 1.
Formats the locked WATCH/RESEARCH/AVOID_CHASE payload (§17.3),
sends via Telegram Bot API, logs to stock_telegram_dispatch_log.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

import requests

SUPABASE_URL  = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY  = os.environ["SUPABASE_SERVICE_KEY"]
BOT_TOKEN     = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID       = os.environ["TELEGRAM_CHAT_ID"]

HEADERS_SB = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
    "Prefer":        "resolution=ignore-duplicates,return=minimal",
}

EMOJI = {"WATCH": "🟢", "RESEARCH": "🟡", "AVOID_CHASE": "🔴"}
SITE_BASE = "https://market.hub4apps.com"   # Phase 2 will populate; harmless if 404 for now


def fetch_signal(signal_id: int) -> dict | None:
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/stock_signals?id=eq.{signal_id}"
        f"&select=id,ticker,action,score,confidence,evidence_summary,horizon_days,fired_at,model_version,direction",
        headers=HEADERS_SB, timeout=10,
    )
    if r.status_code != 200 or not r.json():
        return None
    return r.json()[0]


def format_payload(sig: dict) -> str:
    emoji = EMOJI.get(sig["action"], "⚪")
    horizon = "1d" if sig.get("horizon_days") == 1 else "15m"
    fired = sig["fired_at"][:19].replace("T", " ")
    direction = sig.get("direction") or "neutral"
    dir_line = f"Direction: {direction}" if direction not in ("neutral", "WATCH") else ""
    body = (
        f"{emoji} {sig['ticker']} · {sig['action']} · score {int(sig['score'])}/100\n"
        f"{sig['evidence_summary']}\n"
        f"Confidence: {float(sig['confidence']):.2f} · Horizon: {horizon}\n"
    )
    if dir_line:
        body += f"{dir_line}\n"
    body += (
        f"Fired: {fired} UTC\n"
        f"View thesis → {SITE_BASE}/alert/{sig['id']}.html"
    )
    return body


def inline_keyboard(signal_id: int) -> dict:
    return {
        "inline_keyboard": [[
            {"text": "🔍 Researched", "callback_data": f"act:researched:{signal_id}"},
            {"text": "💰 Acted",      "callback_data": f"act:acted:{signal_id}"},
            {"text": "⏭ Skipped",    "callback_data": f"act:skipped:{signal_id}"},
        ]]
    }


def log_dispatch(signal_id: int, payload_text: str, ok: bool, msg_id: int | None, err: str | None) -> None:
    requests.post(
        f"{SUPABASE_URL}/rest/v1/stock_telegram_dispatch_log",
        headers=HEADERS_SB,
        json={
            "signal_id":       signal_id,
            "sent_at":         datetime.now(timezone.utc).isoformat(),
            "payload":         payload_text,
            "delivery_ok":     ok,
            "telegram_msg_id": msg_id,
            "error":           err,
            "dedupe_key":      f"dispatch_signal_{signal_id}",
        }, timeout=10,
    )


def dispatch_signal(signal_id: int) -> bool:
    sig = fetch_signal(signal_id)
    if sig is None:
        print(f"  dispatch: signal {signal_id} not found", file=sys.stderr)
        return False
    text = format_payload(sig)
    body = {
        "chat_id":      CHAT_ID,
        "text":         text,
        "reply_markup": json.dumps(inline_keyboard(signal_id)),
    }
    try:
        r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                          data=body, timeout=15)
        ok = r.status_code == 200 and r.json().get("ok", False)
        msg_id = r.json().get("result", {}).get("message_id") if ok else None
        err = None if ok else r.text[:500]
        log_dispatch(signal_id, text, ok, msg_id, err)
        return ok
    except Exception as e:  # noqa: BLE001
        log_dispatch(signal_id, text, False, None, str(e))
        return False


# Allow standalone smoke test:  python agents/telegram_dispatcher.py <signal_id>
if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: telegram_dispatcher.py <signal_id>", file=sys.stderr)
        sys.exit(2)
    ok = dispatch_signal(int(sys.argv[1]))
    sys.exit(0 if ok else 1)
