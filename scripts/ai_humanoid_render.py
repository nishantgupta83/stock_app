"""HTML for the ai_humanoid screen. Pure: takes the dict, returns a string.

Kept out of ai_humanoid_screen.py so the maths and the presentation can be tested apart,
and so a rendering change can never alter a number.
"""
from __future__ import annotations

import html
import json


def _p(x, d=1, plus=True):
    if x is None:
        return "—"
    return f"{x*100:+.{d}f}%" if plus else f"{x*100:.{d}f}%"


def _f(x, d=2):
    return "—" if x is None else f"{x:,.{d}f}"


SCRIPT = r"""<script>
// Formulas ported from scripts/ai_humanoid_screen.py, kept deliberately literal so a live
// lookup and the nightly table cannot silently diverge. Thresholds and the leveraged list
// are injected from the same screen.json the tables were built from, never restated here.
const TH = __TH__, LEVERAGED = __LEV__, INVERSE = __INV__;
const N = 20, K = 2, ATR_N = 14, HOLD = 504, MIN_IND = 4;
const fin = x => (x === null || x === undefined || !isFinite(x)) ? null : +x;
const mean = a => a.reduce((s, x) => s + x, 0) / a.length;

function bands(cl) {
  if (cl.length < N) return null;
  const w = cl.slice(-N), m = mean(w);
  const sd = Math.sqrt(mean(w.map(x => (x - m) ** 2)));   // population std, ddof=0
  return { mid: m, up: m + K * sd, lo: m - K * sd };
}
function pctB(cl) { const b = bands(cl); return (!b || b.up <= b.lo) ? null : (cl[cl.length-1] - b.lo) / (b.up - b.lo); }
function sma(cl, n) { return cl.length >= n ? mean(cl.slice(-n)) : null; }
function atrPct(bars) {
  const r = bars.filter(b => fin(b.h) !== null && fin(b.l) !== null && fin(b.c) !== null);
  if (r.length < ATR_N + 1) return null;
  let v = null;
  for (let i = 1; i < r.length; i++) {
    const pc = r[i-1].c;
    const tr = Math.max(r[i].h - r[i].l, Math.abs(r[i].h - pc), Math.abs(r[i].l - pc));
    v = v === null ? tr : v + (tr - v) / ATR_N;
  }
  return r[r.length-1].c ? v / r[r.length-1].c : null;
}
function rangePos(cl) {
  const w = cl.slice(-252); if (w.length < 60) return null;
  const lo = Math.min.apply(null, w), hi = Math.max.apply(null, w);
  return hi > lo ? { p: (w[w.length-1] - lo) / (hi - lo), n: w.length } : null;
}
function analyse(sym, bars) {
  const cl = bars.map(b => b.c).filter(c => fin(c) !== null && c > 0);
  const last = cl[cl.length-1], b = bands(cl);
  const dv = bars.slice(-60).filter(x => fin(x.c) && fin(x.v) !== null).map(x => x.c * x.v).sort((a, z) => a - z);
  const rp = rangePos(cl), s200 = sma(cl, 200);
  return { symbol: sym, close: last, band: b,
    chg: cl.length > 1 ? last / cl[cl.length-2] - 1 : null,
    pct_b: pctB(cl), vs20: b ? last / b.mid - 1 : null,
    vs200: s200 ? last / s200 - 1 : null,
    range_pos: rp ? rp.p : null, range_n: rp ? rp.n : null,
    atr_pct: atrPct(bars),
    dollar_volume: dv.length ? dv[Math.floor(dv.length/2)] : null, n_bars: cl.length };
}
function verdict(a) {
  if (LEVERAGED.indexOf(a.symbol) >= 0)
    return { label: 'NOT RATED', cls: 'vb-warn',
      why: 'Daily-reset leveraged product. Every threshold on this page was measured on ordinary long instruments and does not transfer.'
         + (INVERSE.indexOf(a.symbol) >= 0 ? ' INVERSE: a low %B here means the underlying is STRONG.' : '') };
  const dv = fin(a.dollar_volume);
  if (dv === null || dv < TH.min_dollar_volume)
    return { label: 'TOO THIN', cls: 'vb-no', why: 'Below the liquidity gate — limit orders only, and size it in days-to-exit rather than dollars.' };
  const pb = fin(a.pct_b);
  if (pb === null) return { label: 'NO DATA', cls: 'vb-mid', why: 'Not enough history for a band reading.' };
  const chg = fin(a.chg);
  if ((chg !== null && Math.abs(chg) > 0.35) || pb < -2)
    return { label: 'SUSPECT BAR', cls: 'vb-no', why: 'Implausible move — suspect an unadjusted split.' };
  if (pb > TH.extended_pct_b)
    return { label: 'EXTENDED — WAIT', cls: 'vb-warn',
      why: '%B ' + pb.toFixed(2) + ' is above ' + TH.extended_pct_b.toFixed(2) + '. The stall zone: it returned 2.65 pts below simply holding.' };
  if (pb < TH.dip_pct_b)
    return { label: 'IN THE BUY BAND', cls: 'vb-go',
      why: '%B ' + pb.toFixed(2) + ' is under ' + TH.dip_pct_b.toFixed(2) + ' — the measured dip band. The 1–3 year hold gate needs ~8 years of history and cannot be judged from this 2-year fetch; read the 2y columns in the table for that.' };
  return { label: 'NEUTRAL — NO EDGE', cls: 'vb-mid',
    why: '%B ' + pb.toFixed(2) + ' sits between ' + TH.dip_pct_b.toFixed(2) + ' and ' + TH.extended_pct_b.toFixed(2) + ', where the measured edge is zero either way.' };
}
const pc  = (x, d) => (x === null || x === undefined) ? '—' : (x*100).toFixed(d === undefined ? 1 : d) + '%';
const pcs = (x, d) => (x === null || x === undefined) ? '—' : (x >= 0 ? '+' : '') + (x*100).toFixed(d === undefined ? 1 : d) + '%';
const usd = x => (x === null || x === undefined) ? '—' : '$' + x.toLocaleString(undefined, { maximumFractionDigits: Math.abs(x) < 100 ? 2 : 0 });
const big = x => x === null ? '—' : x >= 1e9 ? (x/1e9).toFixed(1) + 'B' : (x/1e6).toFixed(0) + 'M';

async function quote(sym) {
  const r = await fetch('/api/quote?symbol=' + encodeURIComponent(sym));
  const j = await r.json().catch(() => ({ error: 'bad response' }));
  if (!r.ok || j.error) throw new Error(j.error || ('HTTP ' + r.status));
  return j;
}
function card(q, a, v, cash, pf) {
  const shares = (cash > 0 && a.close) ? Math.floor(cash / a.close) : 0;
  const atrD = a.atr_pct ? a.atr_pct * a.close * shares : null;
  const chk = [
    ['%B', a.pct_b === null ? '—' : a.pct_b.toFixed(2),
      a.pct_b === null ? 'na' : (a.pct_b < TH.dip_pct_b ? 'ok' : (a.pct_b > TH.extended_pct_b ? 'bad' : 'na')),
      'on sale below ' + TH.dip_pct_b.toFixed(2) + ', extended above ' + TH.extended_pct_b.toFixed(2)],
    ['vs 200-day', pcs(a.vs200), a.vs200 === null ? 'na' : (a.vs200 > 0 ? 'ok' : 'na'),
      'above = steadier odds (71% vs 68% at 60d); below = bigger and lumpier. Not a gate.'],
    ['Liquidity', big(a.dollar_volume), (a.dollar_volume !== null && a.dollar_volume >= TH.min_dollar_volume) ? 'ok' : 'bad',
      'needs ' + big(TH.min_dollar_volume) + '/day to trade without limit orders']
  ];
  return '<div class="vbig ' + v.cls + '">' + q.name + ' · ' + v.label + '<small>' + v.why + '</small></div>'
    + '<div class="chk">' + chk.map(function (r) {
        return '<div><span class="mk ' + r[2] + '">' + (r[2] === 'ok' ? '✓' : r[2] === 'bad' ? '✗' : '·')
             + '</span><span><b>' + r[0] + ' ' + r[1] + '</b> — ' + r[3] + '</span></div>'; }).join('')
    + '</div><div class="kv">'
    + '<div><span>Close (' + q.as_of + ')</span><span>' + usd(a.close) + '</span></div>'
    + '<div><span>1-day</span><span>' + pcs(a.chg, 2) + '</span></div>'
    + '<div><span>vs 20-day</span><span>' + pcs(a.vs20) + '</span></div>'
    + '<div><span>' + (a.range_n >= 200 ? '52w' : (a.range_n || 0) + 'd') + ' range</span><span>' + pc(a.range_pos, 0) + '</span></div>'
    + '<div><span>ATR(14)</span><span>' + pc(a.atr_pct, 1) + '</span></div>'
    + '<div><span>Exit level (20d mid)</span><span>' + (a.band ? usd(a.band.mid) + ' (' + pcs(a.band.mid / a.close - 1) + ')' : '—') + '</span></div>'
    + '<div><span>Shares for ' + usd(cash) + '</span><span>' + (shares || '—') + '</span></div>'
    + '<div><span>Share of portfolio</span><span>' + (pf > 0 && a.close ? pc(shares * a.close / pf, 1) : '—') + '</span></div>'
    + '<div><span>A quiet day moves it</span><span>' + (atrD ? '± ' + usd(atrD) : '—') + '</span></div>'
    + '<div><span>Min sane stop (1 ATR)</span><span>' + (atrD ? usd(atrD) : '—') + '</span></div>'
    + '</div>';
}
(function () {
  const $ = function (i) { return document.getElementById(i); };
  const sym = $('q-sym'), go = $('q-go'), out = $('q-out'), err = $('q-err');
  async function run() {
    const s = (sym.value || '').trim().toUpperCase();
    if (!s) return;
    go.disabled = true; go.textContent = '…'; err.hidden = true;
    try {
      const q = await quote(s), a = analyse(q.symbol, q.bars);
      out.innerHTML = card(q, a, verdict(a), parseFloat($('q-cash').value) || 0, parseFloat($('q-pf').value) || 0);
      out.hidden = false;
    } catch (e) { err.textContent = s + ': ' + e.message; err.hidden = false; out.hidden = true; }
    finally { go.disabled = false; go.textContent = 'Check'; }
  }
  go.addEventListener('click', run);
  sym.addEventListener('keydown', function (e) { if (e.key === 'Enter') { e.preventDefault(); run(); } });
  ['q-cash', 'q-pf'].forEach(function (i) { $(i).addEventListener('input', function () { if (!out.hidden) run(); }); });
  run();

  // Refresh only the pinned sections. A live refresh of all 146 rows would be 146 upstream
  // calls per click, and Yahoo rate-limits hard (a bare request already returns 429).
  Array.prototype.forEach.call(document.querySelectorAll('button.refresh'), function (btn) {
    btn.addEventListener('click', async function () {
      const box = document.querySelector('[data-pin="' + btn.dataset.tag + '"]');
      const trs = Array.prototype.filter.call(box.querySelectorAll('tr'), function (tr) { return tr.querySelector('td'); });
      btn.disabled = true; const label = btn.textContent; btn.textContent = 'refreshing…';
      let done = 0;
      for (const tr of trs) {
        const b = tr.querySelector('td b'); if (!b) continue;
        try {
          const q = await quote(b.textContent.trim());
          const a = analyse(q.symbol, q.bars), v = verdict(a), td = tr.querySelectorAll('td');
          if (td[1]) td[1].textContent = a.close.toFixed(2);
          if (td[2]) td[2].textContent = pcs(a.chg, 2);
          if (td[3]) td[3].textContent = a.pct_b === null ? '—' : a.pct_b.toFixed(2);
          if (td[4]) td[4].textContent = pcs(a.vs20);
          if (td[5]) td[5].textContent = pcs(a.vs200);
          if (td[td.length - 1]) td[td.length - 1].textContent = v.label;
          const badge = tr.querySelector('.stale'); if (badge) badge.textContent = q.as_of;
          tr.classList.add('live'); done++;
        } catch (e) { /* leave the nightly row standing rather than blanking it */ }
      }
      btn.disabled = false;
      btn.textContent = done ? ('live · ' + done + ' updated') : label;
      setTimeout(function () { btn.textContent = label; }, 6000);
    });
  });
})();
</script>
"""


VERDICT = {
    "buy_zone": ("BUY ZONE", "v-go"),
    "dip_but_fails_hold": ("DIP · FAILS HOLD", "v-warn"),
    "neutral": ("neutral", "v-mid"),
    "extended": ("extended", "v-warn"),
    "illiquid": ("illiquid", "v-no"),
    "no_hold_data": ("dip · unmeasured", "v-warn"),
    "leveraged": ("leveraged · not rated", "v-warn"),
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


def _by_tag(rows, tag):
    """Rows carrying `tag`, in the order the universe lists them, not by %B.

    A pinned member that failed to resolve gets a placeholder row rather than vanishing:
    these sections promise "tracked every day whether or not they signal", and a silently
    missing ticker reads as "no signal" when it actually means the feed dropped it.
    """
    from ai_humanoid_screen import AI_HUMANOID
    want = AI_HUMANOID.get(tag, [])
    have = {r["ticker"]: r for r in rows if tag in r["tags"]}
    out = []
    for t in want:
        out.append(have.get(t) or {"ticker": t, "tags": [tag], "close": None,
                                   "verdict": "no_data", "why": ["did not resolve"],
                                   "hold": {}, "stale": False, "as_of": None})
    return out


def render(data: dict) -> str:
    from ai_humanoid_screen import PINNED, AI_HUMANOID
    script = (SCRIPT
              .replace("__TH__", json.dumps(data["thresholds"]))
              .replace("__LEV__", json.dumps(AI_HUMANOID.get("leveraged", [])))
              .replace("__INV__", json.dumps(AI_HUMANOID.get("inverse", []))))
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
section.pin h2{{border-left:4px solid var(--teal);padding-left:10px}}
.rules{{display:grid;gap:10px;margin-bottom:26px}}
@media(min-width:720px){{.rules{{grid-template-columns:repeat(3,1fr)}}}}
.rule{{background:var(--card);border:1px solid var(--hair);border-left:3px solid var(--go);
padding:12px 14px;border-radius:3px;font-size:13px}}
.rule b{{display:block;font-family:"Bricolage Grotesque",system-ui,sans-serif;font-size:15px;margin-bottom:2px}}
.rule .th{{font-family:"IBM Plex Mono",monospace;color:var(--go);font-weight:600;font-size:14px;display:block;margin:4px 0 6px}}
.rule p{{color:var(--ink2);margin:0}}
.exit{{background:var(--sunk);border-left:3px solid var(--warn);padding:12px 14px;
border-radius:3px;font-size:13px;margin-bottom:26px}}
.ev{{background:var(--sunk);border-left:3px solid var(--teal);padding:14px 16px;margin-bottom:26px;font-size:13px}}
.ev b{{color:var(--teal)}}
.ev table{{min-width:0;margin-top:8px;font-size:12px}}
.ev td,.ev th{{border:none;padding:3px 10px 3px 0;text-align:left;background:none;color:inherit;
position:static;text-transform:none;letter-spacing:0;font-size:12px}}
footer{{border-top:1px solid var(--hair);padding-top:16px;color:var(--muted);font-size:12px}}
.pinhd{{display:flex;align-items:baseline;justify-content:space-between;gap:12px;flex-wrap:wrap}}
button{{font-family:"IBM Plex Mono",monospace;font-size:12px;font-weight:600;letter-spacing:.04em;
padding:7px 13px;border:1px solid var(--teal);background:var(--card);color:var(--teal);
border-radius:3px;cursor:pointer}}
button:hover{{background:var(--teal);color:var(--paper)}}
button:disabled{{opacity:.5;cursor:progress}}
button:focus-visible{{outline:2px solid var(--teal);outline-offset:2px}}
.tool{{background:var(--sunk);border:1px solid var(--hair);border-radius:3px;padding:16px}}
.trow{{display:flex;gap:12px;flex-wrap:wrap;align-items:flex-end}}
.fld{{flex:1 1 140px;display:flex;flex-direction:column;gap:4px}}
.fld label{{font-size:10.5px;letter-spacing:.07em;text-transform:uppercase;color:var(--muted);font-weight:600}}
.fld input{{font-family:"IBM Plex Mono",monospace;font-size:16px;padding:8px 10px;border:1px solid var(--hair);
border-radius:2px;background:var(--card);color:var(--ink);width:100%}}
.fld input:focus{{outline:2px solid var(--teal);outline-offset:1px}}
.qout{{margin-top:16px}}
.qerr{{margin-top:14px;padding:10px 12px;background:var(--no-bg);color:var(--no);border-radius:3px;font-size:13px}}
.vbig{{font-family:"Bricolage Grotesque",system-ui,sans-serif;font-size:clamp(19px,4vw,25px);
font-weight:700;padding:13px 16px;border-radius:3px;margin-bottom:14px;letter-spacing:-.01em;line-height:1.2}}
.vbig small{{display:block;font-family:"IBM Plex Mono",monospace;font-size:12px;font-weight:400;
letter-spacing:0;margin-top:6px;opacity:.9;line-height:1.45}}
.vb-go{{background:var(--go-bg);color:var(--go)}} .vb-warn{{background:var(--warn-bg);color:var(--warn)}}
.vb-no{{background:var(--no-bg);color:var(--no)}}
.vb-mid{{background:var(--card);color:var(--ink2);border:1px solid var(--hair)}}
.chk{{display:grid;gap:7px;margin-bottom:14px}}
.chk div{{display:flex;gap:10px;align-items:baseline;font-size:13.5px}}
.chk .mk{{flex:0 0 16px;font-weight:700}}
.chk .ok{{color:var(--go)}} .chk .bad{{color:var(--no)}} .chk .na{{color:var(--muted)}}
.chk b{{font-family:"IBM Plex Mono",monospace}}
.kv{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:8px 18px;
padding-top:13px;border-top:1px solid var(--hair);font-size:13px}}
.kv div{{display:flex;justify-content:space-between;gap:10px}}
.kv span:first-child{{color:var(--muted)}}
.kv span:last-child{{font-family:"IBM Plex Mono",monospace;font-weight:600}}
tr.live td{{background:var(--go-bg)}}
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

<div class="rules">
<div class="rule"><b>1. Is it on sale?</b><span class="th">%B &lt; 0.20</span>
<p>%B says where price sits inside its normal trading channel. Below 0.20 means it has
pulled back to the bottom of that channel — on sale. Above 1.00 is <i>extended</i>: too
stretched to buy, and it returned <b>2.65 pts below</b> simply holding.</p></div>
<div class="rule"><b>2. Above or below the 200-day?</b><span class="th">a trade-off, not a gate</span>
<p>Two studies disagree, so this is <b>not</b> a filter the verdict column applies.
<b>46 large caps, 15y, fwd 60d:</b> dips <i>above</i> the 200-day hit <b>71%</b> vs
<b>68%</b> below — better odds, but below-200d averaged slightly <i>more</i> (+7.16% vs
+6.96%). <b>The AI complex, 2023+:</b> dips <i>below</i> the 200-day were the best bucket
of all, <b>+7.49 pts</b> vs the null. The Meta trade in the box below was 11.8% <i>below</i>
its 200-day. Above = steadier; below = bigger and lumpier. Pick per your tolerance.</p></div>
<div class="rule"><b>3. Does it survive bad timing?</b>
<span class="th">2y pos &ge; {data['thresholds']['hold_min_positive']*100:.0f}% AND 2y p10 &gt; {data['thresholds']['hold_min_p10']*100:.0f}%</span>
<p>Both halves are gates, and <b>2y pos is often the binding one</b>. p10 is the
10th-percentile outcome across past 2-year holds — your unlucky-but-realistic case; a −50%
p10 means one in ten 2-year holds halved. These are the exact thresholds the verdict column
applies, not a rule of thumb.</p></div>
</div>

<div class="exit"><b>The exit is a rule, not a guess</b> — but be clear which rule.
Everything in these tables is a <b>fixed hold</b>: the +3.47 / −2.65 / +7.49 pt figures are
<b>60 trading days, entry at the next open</b>, and the <b>2y pos / 2y p10</b> columns are
504-day holds. Nothing on this page measures a middle-band exit.<br>
Selling on the first close above the <b>20-day middle band</b> is the rule used by the
separate SOXX/SOXL/SOXS band strategy in the morning brief, and it is a reasonable discipline
— it just is not what produced these numbers. Reproduce the tabulated edge by holding the
horizon, not by exiting at the midband.</div>

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

{"".join(f'<section class="pin"><div class="pinhd"><h2>{ttl}</h2><button type="button" class="refresh" data-tag="{tag}">Refresh live</button></div><p class="sub">{sub}</p><div data-pin="{tag}">{table(_by_tag(rows, tag))}</div></section>' for tag, ttl, sub in PINNED)}

<section id="tools">
<h2>Live lookup &amp; sizing</h2>
<p class="sub">Any symbol, priced now. The browser cannot call Yahoo directly (no CORS), so this
goes through this site's own <code>/api/quote</code> and then applies the <b>same formulas as the
nightly table</b>, ported from the Python — so a live check and the tables cannot disagree.
Cached 10 minutes.</p>
<div class="tool">
  <div class="trow">
    <div class="fld" style="flex:2 1 190px"><label for="q-sym">Symbol</label>
      <input id="q-sym" type="text" autocapitalize="characters" spellcheck="false" placeholder="NVDA" value="NVDA"></div>
    <div class="fld"><label for="q-cash">Amount to put in ($)</label>
      <input id="q-cash" type="number" inputmode="numeric" value="500" step="50" min="0"></div>
    <div class="fld"><label for="q-pf">Whole portfolio ($)</label>
      <input id="q-pf" type="number" inputmode="numeric" value="7000" step="100" min="0"></div>
    <div class="fld" style="flex:0 0 auto"><label>&nbsp;</label><button id="q-go" type="button">Check</button></div>
  </div>
  <div id="q-out" class="qout" hidden></div>
  <div id="q-err" class="qerr" hidden></div>
</div>
</section>

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
{script}
</body></html>"""
