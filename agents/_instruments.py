"""Single source of truth for which instruments may carry a directional action.

A matured rule licenses BUY/SELL at Layer 2 (thesis_agent) and an actionable
trade setup at Layer 3 (trade_setup_agent) ONLY for a tradeable vehicle:
`stock_symbols.kind in (stock, etf)`. Index mutual funds (VTSAX, VFIAX) price
on NAV, and INST_* tickers are institutional placeholders — neither is
tradeable. Keeping the definition here stops the L2 gate and the L3 guard from
drifting apart (a drift would silently re-open the premature-BUY/SELL leak C2
was created to close).
"""
from __future__ import annotations

import sys

import requests

TRADEABLE_KINDS = ("stock", "etf")
LIMIT = 5000   # stock_symbols is ~160 rows; cap guards against silent truncation


def fetch_tradeable_tickers(base_url: str, headers: dict, *,
                            timeout: int = 10) -> set[str] | None:
    """{tickers} whose kind is tradeable, or None on fetch failure.

    None (not empty set) signals "unknown" so the caller can choose: a live
    emitter should fail-closed (treat as empty → suppress BUY/SELL) rather than
    let a transient Supabase hiccup disable the guard; a non-emitting caller
    (replay/tests) leaves the guard off.
    """
    try:
        r = requests.get(
            f"{base_url.rstrip('/')}/rest/v1/stock_symbols",
            headers=headers,
            params={"select": "ticker",
                    "kind": f"in.({','.join(TRADEABLE_KINDS)})",
                    "limit": str(LIMIT)},
            timeout=timeout,
        )
        if r.status_code != 200:
            print(f"  fetch_tradeable_tickers: {r.status_code} {r.text[:160]}",
                  file=sys.stderr)
            return None
        rows = r.json()
        # Truncation guard: if we got a full page, the result may be capped and
        # legit tickers would silently read as non-tradeable. Treat as "unknown"
        # (None) so the caller fails closed rather than suppressing real signals.
        if len(rows) >= LIMIT:
            print(f"  fetch_tradeable_tickers: {len(rows)} rows hit page cap "
                  f"{LIMIT} — possible truncation, returning None", file=sys.stderr)
            return None
        return {row["ticker"] for row in rows if row.get("ticker")}
    except Exception as e:  # noqa: BLE001
        print(f"  fetch_tradeable_tickers failed: {e}", file=sys.stderr)
        return None


def is_tradeable(ticker: str | None, tradeable_tickers: set[str]) -> bool:
    """True if `ticker` is a tradeable vehicle. INST_* placeholders are never
    tradeable, even if present in the set. Caller must handle a None set
    (guard-off vs fail-closed) before calling this."""
    if not ticker or ticker.startswith("INST_"):
        return False
    return ticker in tradeable_tickers
