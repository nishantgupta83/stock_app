"""Local HTML dashboard for the Paper Book — pure render from SQLite, no network.

Reads the local ledger + derives state via _paper_book.recompute_state and paints a
single self-contained HTML file (inline CSS/SVG, no external deps, no Supabase).
Pastel palette (teal/coral/amber/sage/sky), no purple.
"""
from __future__ import annotations

import html
from datetime import datetime, timezone

import _paper_book as eng
import _paper_book_store as store

TEAL, CORAL, AMBER, SAGE, SKY = "#1f9e8f", "#ff6b6b", "#f2a541", "#7fbf7f", "#4a9fd4"
INK, PAPER, MUTED, LINE = "#22303a", "#f6faf9", "#6b7c85", "#dde7e4"


def _pnl_color(v: float) -> str:
    return SAGE if v > 0 else (CORAL if v < 0 else MUTED)


def _equity_svg(closed: list[dict]) -> str:
    """Realized cumulative-PnL curve over closed positions (in close order)."""
    pts = sorted((p for p in closed if p.get("closed_at")), key=lambda p: p["closed_at"])
    if not pts:
        return f'<div class="muted">No closed positions yet — equity curve appears once trades resolve.</div>'
    eq, cum = [], 0.0
    for p in pts:
        cum += float(p.get("realized_pnl") or 0)
        eq.append(cum)
    w, h, pad = 760, 180, 24
    lo, hi = min(0.0, min(eq)), max(0.0, max(eq))
    rng = (hi - lo) or 1.0
    n = len(eq)
    def x(i): return pad + (w - 2 * pad) * (i / max(n - 1, 1))
    def y(v): return h - pad - (h - 2 * pad) * ((v - lo) / rng)
    poly = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(eq))
    zero = y(0.0)
    last = eq[-1]
    return (
        f'<svg viewBox="0 0 {w} {h}" width="100%" height="{h}" role="img" aria-label="equity curve">'
        f'<line x1="{pad}" y1="{zero:.1f}" x2="{w-pad}" y2="{zero:.1f}" stroke="{LINE}" stroke-dasharray="4 4"/>'
        f'<polyline fill="none" stroke="{TEAL if last>=0 else CORAL}" stroke-width="2.5" points="{poly}"/>'
        f'<circle cx="{x(n-1):.1f}" cy="{y(last):.1f}" r="3.5" fill="{TEAL if last>=0 else CORAL}"/>'
        f'</svg>')


def _stat(label: str, value: str, color: str = INK) -> str:
    return (f'<div class="stat"><div class="stat-v" style="color:{color}">{value}</div>'
            f'<div class="stat-l">{html.escape(label)}</div></div>')


def _row_open(p: dict) -> str:
    arrow = "▲ BUY" if p["direction"] == "long" else "▼ SELL"
    col = SAGE if p["direction"] == "long" else CORAL
    return (f'<tr><td>{html.escape(p["ticker"])}</td>'
            f'<td style="color:{col};font-weight:600">{arrow}</td>'
            f'<td>{(p.get("opened_at") or "")[:10]}</td>'
            f'<td class="num">{p.get("open_price")}</td>'
            f'<td class="num">${p.get("notional"):.0f}</td>'
            f'<td>{(p.get("exit_target_date") or "")[:10]}</td></tr>')


def _row_closed(p: dict) -> str:
    pct = float(p.get("realized_pct") or 0) * 100
    pnl = float(p.get("realized_pnl") or 0)
    c = _pnl_color(pnl)
    arrow = "BUY" if p["direction"] == "long" else "SELL"
    return (f'<tr><td>{html.escape(p["ticker"])}</td><td>{arrow}</td>'
            f'<td>{(p.get("opened_at") or "")[:10]}</td>'
            f'<td>{(p.get("closed_at") or "")[:10]}</td>'
            f'<td>{html.escape(p.get("close_reason") or "")}</td>'
            f'<td class="num" style="color:{c}">{pct:+.2f}%</td>'
            f'<td class="num" style="color:{c};font-weight:600">${pnl:+.2f}</td></tr>')


def render(conn, loop_name: str, capital_base: float, max_concurrent: int) -> str:
    positions = store.all_positions(conn)
    state = eng.recompute_state(positions, capital_base)
    cfg = store.config(conn, loop_name)
    openp = [p for p in positions if p["status"] == "open"]
    closedp = [p for p in positions if p["status"] == "closed"]
    wins = sum(1 for p in closedp if float(p.get("realized_pnl") or 0) > 0)
    hit = (wins / len(closedp) * 100) if closedp else 0.0
    pnl = state["cumulative_pnl"]
    equity = capital_base + pnl
    n_setups = len(store.all_setups(conn))
    gen = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    open_rows = "".join(_row_open(p) for p in sorted(openp, key=lambda p: p.get("opened_at") or "", reverse=True)) \
        or '<tr><td colspan="6" class="muted">No open positions.</td></tr>'
    closed_rows = "".join(_row_closed(p) for p in sorted(closedp, key=lambda p: p.get("closed_at") or "", reverse=True)[:50]) \
        or '<tr><td colspan="7" class="muted">No closed positions yet.</td></tr>'

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Paper Book · {html.escape(loop_name)}</title>
<style>
 :root {{ color-scheme: light; }}
 body {{ font: 15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif; color:{INK};
        background:{PAPER}; margin:0; padding:28px; }}
 h1 {{ font-size:19px; margin:0 0 2px; }} .sub {{ color:{MUTED}; font-size:13px; margin-bottom:20px; }}
 .stats {{ display:flex; gap:14px; flex-wrap:wrap; margin-bottom:22px; }}
 .stat {{ background:#fff; border:1px solid {LINE}; border-radius:12px; padding:14px 18px; min-width:120px; }}
 .stat-v {{ font-size:22px; font-weight:700; }} .stat-l {{ color:{MUTED}; font-size:12px; margin-top:3px; }}
 .card {{ background:#fff; border:1px solid {LINE}; border-radius:12px; padding:18px; margin-bottom:18px; }}
 .card h2 {{ font-size:14px; margin:0 0 12px; color:{MUTED}; text-transform:uppercase; letter-spacing:.04em; }}
 table {{ width:100%; border-collapse:collapse; font-size:14px; }}
 th {{ text-align:left; color:{MUTED}; font-weight:600; font-size:12px; padding:6px 10px; border-bottom:1px solid {LINE}; }}
 td {{ padding:7px 10px; border-bottom:1px solid {PAPER}; }}
 .num {{ text-align:right; font-variant-numeric:tabular-nums; }}
 .muted {{ color:{MUTED}; }}
 .badge {{ display:inline-block; background:{AMBER}22; color:{AMBER}; border-radius:20px;
           padding:2px 10px; font-size:11px; font-weight:600; }}
</style></head><body>
 <h1>📓 Paper Book — {html.escape(loop_name)}</h1>
 <div class="sub">${capital_base:,.0f} bankroll · {max_concurrent} concurrent · $1,000/position · stop_only exit ·
   <span class="badge">PAPER — no real capital</span> · generated {gen}</div>
 <div class="stats">
   {_stat("Equity", f"${equity:,.0f}", _pnl_color(pnl))}
   {_stat("Cumulative P&L", f"${pnl:+,.0f}", _pnl_color(pnl))}
   {_stat("Cash", f"${state['cash_available']:,.0f}")}
   {_stat("Open", f"{state['positions_open']}/{max_concurrent}", SKY)}
   {_stat("Closed", str(len(closedp)))}
   {_stat("Hit rate", f"{hit:.0f}%", SAGE if hit>=50 else AMBER)}
   {_stat("Max drawdown", f"${state['max_drawdown']:,.0f}", CORAL if state['max_drawdown']>0 else MUTED)}
 </div>
 <div class="card"><h2>Realized equity curve</h2>{_equity_svg(closedp)}</div>
 <div class="card"><h2>Open positions</h2>
   <table><tr><th>Ticker</th><th>Side</th><th>Opened</th><th>Entry</th><th>Size</th><th>Target exit</th></tr>
   {open_rows}</table></div>
 <div class="card"><h2>Closed positions (latest 50)</h2>
   <table><tr><th>Ticker</th><th>Side</th><th>Opened</th><th>Closed</th><th>Reason</th><th>Return</th><th>P&amp;L</th></tr>
   {closed_rows}</table></div>
 <div class="sub">Source: {n_setups} setups ingested from the pipeline · ledger: local SQLite (zero Supabase egress).
   Equity curve is realized P&amp;L only; open-position mark-to-market not yet shown.</div>
</body></html>"""
