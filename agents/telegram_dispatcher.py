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
SITE_BASE = "https://hub4apps.com/stock_app"

# Feature flag — when False, the inline keyboard is suppressed. Pre-fix the
# keyboard rendered Researched/Acted/Skipped buttons on every alert but no
# webhook handler existed (zero rows ever written to stock_user_decisions).
# Shipping UI that does nothing is a broken promise to the operator. Flip
# this to True once the webhook handler lands AND stock_user_decisions
# gains signal_id + decision columns (deferred — see plan F4).
TELEGRAM_CALLBACKS_ENABLED = False


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
    """Append a row to stock_telegram_dispatch_log.

    Cannot use PostgREST `?on_conflict=dedupe_key` here — the unique index on
    dedupe_key is partial (sql/0004:77-79 `WHERE dedupe_key IS NOT NULL`)
    and PostgREST silently 42P10's on partial indexes (CLAUDE.md rule #2).
    Pattern: pre-check for an existing dedupe_key row, plain INSERT if
    absent. Re-dispatches of the same signal are a no-op (already logged).
    """
    dedupe = f"dispatch_signal_{signal_id}"
    try:
        existing = requests.get(
            f"{SUPABASE_URL}/rest/v1/stock_telegram_dispatch_log"
            f"?dedupe_key=eq.{dedupe}&select=id&limit=1",
            headers=HEADERS_SB, timeout=5,
        )
        if existing.status_code == 200 and existing.json():
            return
    except Exception as e:  # noqa: BLE001
        print(f"  log_dispatch precheck failed: {e}", file=sys.stderr)
        # fall through — try the INSERT; a duplicate-key error there will
        # just mean we lost the race, which is fine.
    insert = requests.post(
        f"{SUPABASE_URL}/rest/v1/stock_telegram_dispatch_log",
        headers={**HEADERS_SB, "Prefer": "return=minimal"},
        json={
            "signal_id":       signal_id,
            "sent_at":         datetime.now(timezone.utc).isoformat(),
            "payload":         payload_text,
            "delivery_ok":     ok,
            "telegram_msg_id": msg_id,
            "error":           err,
            "dedupe_key":      dedupe,
        }, timeout=10,
    )
    if insert.status_code not in (200, 201, 204):
        # 409 from the partial-index race is acceptable; anything else is a
        # real failure operators need to see.
        if insert.status_code != 409:
            print(f"  log_dispatch INSERT failed: {insert.status_code} {insert.text[:200]}",
                  file=sys.stderr)


def send_and_log(signal_id: int, text: str, parse_mode: str | None = None,
                 disable_web_page_preview: bool = True) -> bool:
    """Send a Telegram message and append a dispatch_log row in one call.

    This is the entry point every signal-emitting agent should use after
    inserting its stock_signals row — it closes the audit-invariant contract
    (status_v2='sent' ⇔ dispatch_log row with delivery_ok=true) that
    audit_agent invariant #1 enforces. Returns the delivery_ok boolean.
    """
    if not BOT_TOKEN or not CHAT_ID:
        print(f"  send_and_log({signal_id}): missing TELEGRAM_BOT_TOKEN/CHAT_ID",
              file=sys.stderr)
        return False
    body: dict = {"chat_id": CHAT_ID, "text": text}
    if parse_mode:
        body["parse_mode"] = parse_mode
    if disable_web_page_preview:
        body["disable_web_page_preview"] = "true"
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


def dispatch_signal(signal_id: int) -> bool:
    """Used by thesis_agent: fetch the signal, format with the WATCH/RESEARCH
    payload, send via send_and_log. The inline_keyboard branch is the only
    reason this exists separately from send_and_log."""
    sig = fetch_signal(signal_id)
    if sig is None:
        print(f"  dispatch: signal {signal_id} not found", file=sys.stderr)
        return False
    text = format_payload(sig)
    if not TELEGRAM_CALLBACKS_ENABLED:
        return send_and_log(signal_id, text, disable_web_page_preview=False)
    # Callback-enabled path retains the inline keyboard — keep the inline
    # send so we can attach reply_markup; still routes through log_dispatch.
    if not BOT_TOKEN or not CHAT_ID:
        return False
    body = {
        "chat_id":     CHAT_ID,
        "text":        text,
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
