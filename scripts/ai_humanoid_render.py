"""HTML for the ai_humanoid screen. Pure: takes the dict, returns a string.

Kept out of ai_humanoid_screen.py so the maths and the presentation can be tested apart,
and so a rendering change can never alter a number.
"""
from __future__ import annotations

import html


def _p(x, d=1, plus=True):
    if x is None:
        return "—"
    return f"{x*100:+.{d}f}%" if plus else f"{x*100:.{d}f}%"


def _f(x, d=2):
    return "—" if x is None else f"{x:,.{d}f}"


VERDICT = {
    "buy_zone": ("BUY ZONE", "v-go"),
    "dip_but_fails_hold": ("DIP · FAILS HOLD", "v-warn"),
    "neutral": ("neutral", "v-mid"),
    "extended": ("extended", "v-warn"),
    "illiquid": ("illiquid", "v-no"),
    "no_hold_data": ("dip · unmeasured", "v-warn"),
    "no_data": ("no data", "v-mid"),
}


def _row(r):
    label, cls = VERDICT.get(r["verdict"], (r["verdict"], "v-mid"))
    dv = r.get("dollar_volume")
    dvs = "—" if dv is None else (f"{dv/1e9:,.1f}B" if dv >= 1e9 else f"{dv/1e6:,.0f}M")
    h = r.get("hold") or {}
    rng = "—" if r.get("range_pos") is None else (
        f"{r['range_pos']*100:.0f}%" + ("" if (r.get("range_n") or 0) >= 200 else f" ({r['range_n']}d)"))
    soc = r.get("social")
    socs = "—" if not soc else f"{soc['messages_12h']}"
    sp = r.get("spike")
    tag = "".join(f'<span class="tag">{html.escape(t)}</span>' for t in r["tags"][:3])
    # A stale row's numbers are from an older session than the header claims. Say so on the
    # row rather than letting one as_of speak for a table of mixed dates.
    st = f'<span class="stale">{html.escape(str(r.get("as_of")))}</span>' if r.get("stale") else ""
    hn = h.get("n_independent")
    hn_s = f'<span class="tiny">{hn}w</span>' if hn else ""
    return f"""<tr class="{cls}">
<td class="tk"><b>{html.escape(r['ticker'])}</b>{st}<br>{tag}</td>
<td>{_f(r['close'])}</td><td>{_p(r.get('chg'), 2)}</td>
<td class="pb">{'—' if r.get('pct_b') is None else f"{r['pct_b']:.2f}"}</td>
<td>{_p(r.get('vs_sma20'))}</td><td>{_p(r.get('vs_sma200'))}</td><td>{rng}</td>
<td>{_p(r.get('atr_pct'), 1, plus=False)}</td><td>{dvs}</td>
<td>{'—' if h.get('positive') is None else f"{h['positive']*100:.0f}%"} {hn_s}</td>
<td>{_p(h.get('p10'))}</td><td>{socs}</td>
<td class="vd">{label}{'<br><span class="sp">SPIKE ' + _p(sp['move'], 1) + f" · {sp['vol_mult']:.1f}x</span>" if sp else ''}</td>
</tr>"""


def render(data: dict) -> str:
    rows = data["rows"]
    buys = [r for r in rows if r["verdict"] == "buy_zone"]
    spikes = [r for r in rows if r.get("spike")]
    near = [r for r in rows if r["verdict"] in ("dip_but_fails_hold", "neutral")][:25]
    ext = [r for r in rows if r["verdict"] == "extended"][:15]
    th = ("<tr><th>Ticker</th><th>Close</th><th>1d</th><th>%B</th><th>vs 20d</th>"
          "<th>vs 200d</th><th>range</th><th>ATR</th><th>$vol/d</th>"
          "<th>2y pos</th><th>2y p10</th><th>msgs</th><th>Verdict</th></tr>")

    def table(rs):
        return f'<div class="tw"><table>{th}{"".join(_row(r) for r in rs)}</table></div>' if rs \
            else '<p class="none">Nothing in this group today.</p>'

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>AI &amp; Humanoid Screen</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,700&family=IBM+Plex+Mono:wght@400;500;600&display=swap">
<style>
:root{{--ink:#12201e;--ink2:#41514e;--muted:#7b8b88;--hair:#dcd6cb;--paper:#f6f4ef;
--card:#fffdf9;--sunk:#ece8df;--go:#3f7d4f;--go-bg:#e2efe3;--warn:#a96f16;--warn-bg:#faf0dc;
--no:#b8452f;--no-bg:#fae4de;--teal:#136b62;}}
@media(prefers-color-scheme:dark){{:root:not([data-theme=light]){{--ink:#e9e7e0;--ink2:#c3c9c6;
--muted:#8b9a97;--hair:#2a3836;--paper:#0f1817;--card:#172322;--sunk:#1d2b29;--go:#7fb98d;
--go-bg:#1b2f20;--warn:#dda54e;--warn-bg:#332714;--no:#e58873;--no-bg:#3a201a;--teal:#4db8a8;}}}}
:root[data-theme=dark]{{--ink:#e9e7e0;--ink2:#c3c9c6;--muted:#8b9a97;--hair:#2a3836;
--paper:#0f1817;--card:#172322;--sunk:#1d2b29;--go:#7fb98d;--go-bg:#1b2f20;--warn:#dda54e;
--warn-bg:#332714;--no:#e58873;--no-bg:#3a201a;--teal:#4db8a8;}}
*{{box-sizing:border-box;margin:0;padding:0}}
body{{background:var(--paper);color:var(--ink);font-family:"IBM Plex Mono",ui-monospace,monospace;
font-size:14px;line-height:1.5;padding-inline:16px;padding-block:0 60px;max-width:1240px;margin:0 auto;
font-variant-numeric:tabular-nums}}
h1,h2{{font-family:"Bricolage Grotesque",system-ui,sans-serif;letter-spacing:-.02em;text-wrap:balance}}
h1{{font-size:clamp(28px,6vw,42px);line-height:1.05}}
h2{{font-size:20px;margin-bottom:4px}}
header{{padding-block:34px 20px;border-bottom:3px solid var(--ink);margin-bottom:24px}}
.kick{{font-size:11px;letter-spacing:.18em;text-transform:uppercase;color:var(--teal);font-weight:600;margin-bottom:10px}}
.lede{{color:var(--ink2);margin-top:12px;max-width:80ch;font-size:13.5px}}
section{{margin-bottom:30px}}
.sub{{color:var(--muted);font-size:12.5px;margin:2px 0 12px;max-width:88ch}}
.tw{{overflow-x:auto;border:1px solid var(--hair);border-radius:3px;background:var(--card)}}
table{{border-collapse:collapse;width:100%;font-size:12.5px;min-width:900px}}
th{{background:var(--ink);color:var(--paper);font-size:10px;letter-spacing:.07em;text-transform:uppercase;
padding:7px 9px;text-align:right;font-weight:600;position:sticky;top:0}}
th:first-child{{text-align:left}}
td{{padding:7px 9px;text-align:right;border-top:1px solid var(--hair);white-space:nowrap}}
td.tk{{text-align:left;line-height:1.25}}
td.pb{{font-weight:600}}
td.vd{{font-weight:600;font-size:11px;letter-spacing:.03em}}
tr.v-go td.vd{{color:var(--go)}} tr.v-go{{background:var(--go-bg)}}
tr.v-warn td.vd{{color:var(--warn)}}
tr.v-no td.vd{{color:var(--no)}} tr.v-no{{background:var(--no-bg)}}
.sp{{color:var(--no);font-weight:600;font-size:10px;letter-spacing:.04em}}
.stale{{display:inline-block;background:var(--warn-bg);color:var(--warn);font-size:9px;
padding:1px 4px;border-radius:2px;margin-left:5px;letter-spacing:.03em}}
.tiny{{color:var(--muted);font-size:9.5px}}
.tag{{display:inline-block;background:var(--sunk);color:var(--muted);font-size:9.5px;
padding:1px 5px;border-radius:2px;margin:2px 3px 0 0;letter-spacing:.03em}}
.none{{color:var(--muted);font-size:13px;padding:12px;background:var(--sunk);border-radius:3px}}
.ev{{background:var(--sunk);border-left:3px solid var(--teal);padding:14px 16px;margin-bottom:26px;font-size:13px}}
.ev b{{color:var(--teal)}}
.ev table{{min-width:0;margin-top:8px;font-size:12px}}
.ev td,.ev th{{border:none;padding:3px 10px 3px 0;text-align:left;background:none;color:inherit;
position:static;text-transform:none;letter-spacing:0;font-size:12px}}
footer{{border-top:1px solid var(--hair);padding-top:16px;color:var(--muted);font-size:12px}}
</style></head><body>
<header>
<div class="kick">Nasdaq-100 + the AI / humanoid complex · {data['n_resolved']} of {data['n_universe']} resolved{f" · {data['n_stale']} on an older bar" if data.get('n_stale') else ""}</div>
<h1>AI &amp; Humanoid Screen</h1>
<p class="lede">Ranked by <b>%B ascending</b> — which names are on sale — not by momentum.
Measured on the AI complex over the 2023+ wave, forward 60 days: buying dips beat simply
holding by <b>+3.47 pts</b>, while buying strength lost <b>2.65 pts</b> and top-quartile
12-month momentum lost <b>3.01 pts</b>. Owning the complex at all was worth <b>+17.95%</b>,
which is roughly five times what the timing overlay adds.</p>
</header>

<div class="ev"><b>Why there are two signals.</b> Meta launched Muse on 2026-09-08 and ran
+20.3% before the first analyst upgrade on 09-21 — following that upgrade captured 2% of
the move. Replaying these rules over those sessions:
<table>
<tr><td><b>%B &lt; 0.20 dip rule</b></td><td>2026-08-21 @ 549.47</td><td><b>+35.4%</b></td><td>18 days <i>before</i> the launch, and META was 11.8% below its 200-day</td></tr>
<tr><td><b>move + volume spike</b></td><td>2026-09-09 @ 653.17</td><td><b>+13.9%</b></td><td>the day <i>after</i> the launch</td></tr>
<tr><td>follow the analyst</td><td>2026-09-21 @ 741.25</td><td>+0.4%</td><td>the move was over</td></tr>
</table>
The dip rule caught it by accident — it was buying a drawdown, not predicting a product.
The spike rule reacts to the catalyst, late but not uselessly. They are labelled separately
so a reaction is never mistaken for a setup.</div>

<section><h2>Buy zone</h2>
<p class="sub">%B below {data['thresholds']['dip_pct_b']}, liquid, and passes the 1–3 year hold gate
(≥{data['thresholds']['hold_min_positive']*100:.0f}% of rolling 2-year windows positive with a
10th percentile above {data['thresholds']['hold_min_p10']*100:.0f}%).</p>
{table(buys)}</section>

<section><h2>Catalyst spikes today</h2>
<p class="sub">A move of {data['thresholds']['spike_move']*100:.0f}%+ on
{data['thresholds']['spike_vol']:.0f}×+ the 20-day average volume. This is a reaction, not a setup —
something happened, and the rule does not know what or which direction it resolves.</p>
{table(spikes)}</section>

<section><h2>Near the band / neutral</h2>
<p class="sub">Dips that fail the hold gate, plus names with no measured edge either way.</p>
{table(near)}</section>

<section><h2>Extended</h2>
<p class="sub">%B above {data['thresholds']['extended_pct_b']:.2f} — the stall zone, −2.65 pts vs the
null at 60 days. Not a short signal; a not-today signal.</p>
{table(ext)}</section>

<footer><b>as of {html.escape(str(data.get('as_of')))}</b>
{f"— but {data['n_stale']} of {data['n_resolved']} rows carry an OLDER bar, tagged with their own date beside the ticker. yfinance drops whole trading days per-ticker, so one header date cannot speak for the table." if data.get('n_stale') else ""}
· the <b>2y pos</b> column shows the count of INDEPENDENT (non-overlapping) 2-year windows
behind it — a name with 1-2 is one market cycle, not a distribution · generated
{html.escape(str(data.get('generated_at')))} · bars strictly before today, split-corrected,
entry measured at the next open. The <b>msgs</b> column is StockTwits message volume in 12h —
the bull/bear ratio is deliberately not shown, because in a live pull across 20 tickers nine
of them read 100% bullish. Thresholds were each measured against the unconditional base rate;
stacking them into one verdict is judgement, not a tested system.</footer>
</body></html>"""
