#!/usr/bin/env python3
"""semis_brief — 6:15 AM PT pre-open email for SOXX / SOXL (3x bull) / SOXS (3x bear).

Isolated module: reads yfinance, Yahoo RSS, StockTwits, FRED, and two raw Supabase tables
(read-only); writes only committed JSON under semis_brief/. Never touches the frozen
forward experiments, calibration, or Layers 2-4.

Two subcommands, run by .github/workflows/semis_brief.yml:
  prepare  gate (trading day, 06:10-06:29 PT window, not already sent on origin/main),
           grade any earlier SENT calls whose official close is now published, build
           today's snapshot, write semis_brief/calls/DATE.json. Emits send=true|false
           and call_path to $GITHUB_OUTPUT. The workflow commits+pushes the call BEFORE send.
  send     render + email the call, then write semis_brief/sent/DATE.json.

One writer per file: `prepare` owns calls/, grades/, scorecard.json, social_history.json;
`send` owns sent/. Manual runs (--force) write under manual/ and are never graded.
"""
from __future__ import annotations

import argparse
import html
import json
import math
import os
import re
import smtplib
import statistics
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "agents"))
from _market_calendar import ALL_HOLIDAYS, is_trading_day, previous_trading_day  # noqa: E402

PT = ZoneInfo("America/Los_Angeles")
ET = ZoneInfo("America/New_York")
STATE = REPO / "semis_brief"

WINDOW_START = time(6, 10)          # PT
WINDOW_END = time(6, 30)            # PT, exclusive
OPEN_ET = time(9, 30)
PREMARKET_START_ET = time(4, 0)
STALE_MINUTES = 30
BIAS_THRESHOLD = 0.0025             # |implied SOXX| >= 0.25% -> lean
HTTP_TIMEOUT = 12

ETFS = ["SOXX", "SOXL", "SOXS"]
OVERNIGHT = {"2330.TW": "TSMC (Taiwan)", "000660.KS": "SK Hynix", "005930.KS": "Samsung",
             "8035.T": "Tokyo Electron", "ASML.AS": "ASML (Amsterdam)"}
SOCIAL_SYMBOLS = ["SOXL", "SOXS", "SOXX", "NVDA", "MU", "AMD"]
TRUTH_KEYWORDS = ("chip", "semiconductor", "nvidia", "intel", "taiwan", "china", "tariff",
                  "export", "micron", "tsmc", "broadcom", "amd")
POS_WORDS = ("upgrade", "raises", "raised", "beat", "beats", "record", "outperform", "buy rating",
             "price target raised", "surge", "soars", "jumps", "wins", "approval", "partnership")
NEG_WORDS = ("downgrade", "cut", "cuts", "miss", "misses", "export ban", "restriction", "probe",
             "investigation", "lawsuit", "plunge", "falls", "slump", "warns", "weak", "delay",
             "underperform", "price target cut", "tariff")
FRED_WATCH = ("Consumer Price Index", "Employment Situation", "Producer Price Index",
              "Advance Monthly Sales for Retail", "Gross Domestic Product", "Personal Income",
              "Job Openings", "Unemployment Insurance Weekly Claims", "Industrial Production")


# ----------------------------------------------------------------------------- pure helpers

def finite(x):
    """float(x) if finite, else None. NaN is truthy, so never use `if not x` on prices."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def in_brief_window(now_pt: datetime) -> bool:
    return WINDOW_START <= now_pt.timetz().replace(tzinfo=None) < WINDOW_END


def calendar_covered(d: date) -> bool:
    """_market_calendar silently treats uncovered-year holidays as trading days."""
    return d.year <= max(h.year for h in ALL_HOLIDAYS)


def snapshot_cutoff(now_utc: datetime, day: date) -> datetime:
    """Latest instant the brief may read: min(run time, 09:30 ET that day)."""
    return min(now_utc, datetime.combine(day, OPEN_ET, ET).astimezone(timezone.utc))


def premarket_last(bars: list[tuple[datetime, float]], day: date, cutoff_utc: datetime):
    """Last premarket print for `day`: bar start >= 04:00 ET and < cutoff; stale if the
    newest usable bar started more than STALE_MINUTES before the cutoff.
    bars: [(tz-aware bar-start datetime, close)]. Returns (price, ts_utc, stale) or None."""
    lo = datetime.combine(day, PREMARKET_START_ET, ET).astimezone(timezone.utc)
    usable = [(t.astimezone(timezone.utc), finite(p)) for t, p in bars]
    usable = [(t, p) for t, p in usable if p is not None and lo <= t < cutoff_utc]
    if not usable:
        return None
    t, p = max(usable, key=lambda r: r[0])
    return p, t, (cutoff_utc - t) > timedelta(minutes=STALE_MINUTES)


def implied_open(weights: dict[str, float], moves: dict[str, float | None]) -> dict:
    """Weight-average of available holding moves, renormalised over present names."""
    present = {t: m for t, m in moves.items() if t in weights and m is not None}
    wsum = sum(weights[t] for t in present)
    if not present or wsum <= 0:
        return {"implied_soxx": None, "n_used": 0, "n_missing": len(weights),
                "coverage": 0.0, "contrib": {}}
    contrib = {t: weights[t] * m / wsum for t, m in present.items()}
    imp = sum(contrib.values())
    vals = list(present.values())
    return {"implied_soxx": imp, "n_used": len(present),
            "n_missing": len([t for t in weights if t not in present]),
            "coverage": wsum / sum(weights.values()),
            "contrib": contrib,
            "dispersion": statistics.pstdev(vals) if len(vals) > 1 else 0.0,
            "n_up": sum(1 for v in vals if v > 0), "n_down": sum(1 for v in vals if v < 0)}


def bias_from(implied: float | None) -> str:
    if implied is None:
        return "no_edge"
    if implied >= BIAS_THRESHOLD:
        return "lean_soxl"
    if implied <= -BIAS_THRESHOLD:
        return "lean_soxs"
    return "no_edge"


def sign(x: float | None) -> int:
    if x is None:
        return 0
    return 1 if x > 0 else -1 if x < 0 else 0


def tag_headline(title: str) -> int:
    t = title.lower()
    pos = any(w in t for w in POS_WORDS)
    neg = any(w in t for w in NEG_WORDS)
    return 1 if pos and not neg else -1 if neg and not pos else 0


def norm_title(title: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", title.lower()).strip()[:90]


def filter_news(items: list[dict], since_utc: datetime, until_utc: datetime) -> list[dict]:
    """items: {ticker, title, published (tz-aware), link}. Window + dedupe + tag."""
    seen, out = set(), []
    for it in sorted(items, key=lambda r: r["published"], reverse=True):
        if not (since_utc <= it["published"] < until_utc):
            continue
        k = norm_title(it["title"])
        if not k or k in seen:
            continue
        seen.add(k)
        out.append({**it, "tag": tag_headline(it["title"])})
    return out


def gap_bucket(gap: float) -> str:
    a = abs(gap)
    b = "0-1" if a < 0.01 else "1-2" if a < 0.02 else "2-4" if a < 0.04 else "4+"
    return f"{'up' if gap >= 0 else 'down'} {b}%"


def gap_fill_rate(daily: list[dict], gap_now: float) -> dict:
    """daily: chronological [{open, high, low, close}] (unadjusted). Fill = the day traded
    back to the prior close (low <= prev close for a gap up; high >= prev close for down)."""
    want = gap_bucket(gap_now)
    n = filled = 0
    for prev, cur in zip(daily, daily[1:]):
        pc, o, hi, lo = (finite(prev.get("close")), finite(cur.get("open")),
                         finite(cur.get("high")), finite(cur.get("low")))
        if None in (pc, o, hi, lo) or pc <= 0:
            continue
        g = o / pc - 1
        if g == 0 or gap_bucket(g) != want:
            continue
        n += 1
        filled += (lo <= pc) if g > 0 else (hi >= pc)
    return {"bucket": want, "n": n, "fill_rate": (filled / n) if n else None}


def validate_call(call: dict) -> None:
    """Lookahead guard: no snapshot timestamp may be at or after 09:30 ET of the call day."""
    day = date.fromisoformat(call["date"])
    open_utc = datetime.combine(day, OPEN_ET, ET).astimezone(timezone.utc)
    for name, rec in (call.get("premarket") or {}).items():
        ts = rec.get("ts")
        if ts and datetime.fromisoformat(ts) >= open_utc:
            raise ValueError(f"lookahead: {name} snapshot {ts} is at/after the 09:30 ET open")


def grade_day(call: dict, soxx: dict, soxl: dict, soxs: dict, soxx_1030: float | None) -> dict:
    """Pure grade. soxx/soxl/soxs = that day's unadjusted daily {open, high, low, close}.
    A missing/NaN open or close -> status 'pending' (never scored as a miss)."""
    if finite(call.get("implied_soxx")) is None:
        # outage day (no usable premarket at all) — not a model "no_edge" decision
        return {"date": call["date"], "status": "no_data"}
    o, c = finite(soxx.get("open")), finite(soxx.get("close"))
    if o is None or c is None or o <= 0:
        return {"date": call["date"], "status": "pending"}
    oc = c / o - 1
    out = {"date": call["date"], "status": "graded", "bias": call["bias"],
           "soxx_open_to_close": oc,
           "soxx_open_to_1030": (soxx_1030 / o - 1) if finite(soxx_1030) else None,
           "outcome_sign": sign(oc)}
    lo_, lc = finite(soxl.get("open")), finite(soxl.get("close"))
    so_, sc = finite(soxs.get("open")), finite(soxs.get("close"))
    out["soxl_open_to_close"] = (lc / lo_ - 1) if (lo_ and lc) else None
    out["soxs_open_to_close"] = (sc / so_ - 1) if (so_ and sc) else None
    pc = finite((call.get("levels") or {}).get("soxl_prior_close"))
    imp = finite(call.get("implied_soxl"))
    if pc and lo_:
        out["soxl_open_actual_vs_prior"] = lo_ / pc - 1
        out["soxl_open_error"] = (lo_ / pc - 1 - imp) if imp is not None else None
        g = lo_ / pc - 1
        hi, lo = finite(soxl.get("high")), finite(soxl.get("low"))
        out["gap_filled"] = (None if g == 0 or hi is None or lo is None
                             else bool(lo <= pc) if g > 0 else bool(hi >= pc))
    # tie (open->close exactly 0) is excluded from every hit test, and counted
    b = {"lean_soxl": 1, "lean_soxs": -1}.get(call["bias"], 0)
    out["bias_hit"] = None if (b == 0 or out["outcome_sign"] == 0) else (b == out["outcome_sign"])
    comps = {}
    for k, v in (call.get("components") or {}).items():
        s = sign(v)
        comps[k] = None if (s == 0 or out["outcome_sign"] == 0) else (s == out["outcome_sign"])
    out["component_hits"] = comps
    ig = sign(finite(call.get("implied_soxx")))
    # Evaluated on EVERY graded day with a nonzero implied gap (incl. no_edge days):
    # on called days bias == sign(implied) by construction, so restricting the null to
    # those days would make it exactly 1 - bias hit rate and carry no information.
    out["fade_gap_hit"] = (None if (ig == 0 or out["outcome_sign"] == 0)
                           else (-ig == out["outcome_sign"]))
    return out


ROUND_TRIP_COST = 0.001   # 10 bps per open->close round trip


def strategy_returns(rows: list[dict]) -> dict:
    """Compounded open->close result of acting on each called day, both ways:
    follow = SOXL on lean_soxl / SOXS on lean_soxs; fade = the other fund."""
    legs = {"follow": [], "fade": []}
    for x in rows:
        l, s = x.get("soxl_open_to_close"), x.get("soxs_open_to_close")
        if x.get("bias") == "lean_soxl":
            f, o = l, s
        elif x.get("bias") == "lean_soxs":
            f, o = s, l
        else:
            continue
        if f is not None and o is not None:     # same days in both legs -> comparable
            legs["follow"].append(f)
            legs["fade"].append(o)
    out = {}
    for k, v in legs.items():
        eq = 1.0
        for r in v:
            eq *= 1 + r - ROUND_TRIP_COST
        out[f"{k}_compounded"] = (eq - 1) if v else None
        out[f"{k}_n"] = len(v)
    return out


def scorecard(grades: list[dict], sent_dates: set[str]) -> dict:
    g = [x for x in grades if x.get("status") == "graded" and x["date"] in sent_dates]
    g.sort(key=lambda x: x["date"])

    def rate(vals):
        v = [x for x in vals if x is not None]
        return {"n": len(v), "hits": sum(v), "hit_rate": (sum(v) / len(v)) if v else None}

    called = [x for x in g if x["bias"] != "no_edge"]
    no_edge = [x for x in g if x["bias"] == "no_edge"]
    comp_names = sorted({k for x in g for k in (x.get("component_hits") or {})})
    lean_l = [x["soxl_open_to_close"] for x in called
              if x["bias"] == "lean_soxl" and x.get("soxl_open_to_close") is not None]
    lean_s = [x["soxs_open_to_close"] for x in called
              if x["bias"] == "lean_soxs" and x.get("soxs_open_to_close") is not None]

    def block(rows):
        return {"bias": rate([x.get("bias_hit") for x in rows]),
                "fade_called": rate([None if x.get("bias_hit") is None else not x["bias_hit"]
                                     for x in rows]),
                "up_day_base_rate": rate([(x["outcome_sign"] > 0) if x["outcome_sign"] else None
                                          for x in rows]),
                **strategy_returns(rows)}
    return {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "n_graded": len(g), "n_called": len(called), "n_no_edge": len(no_edge),
        "all": block(called), "last20": block(called[-20:]),
        "fade_gap_null_all_days": rate([x.get("fade_gap_hit") for x in g]),
        "fade_gap_null_last20": rate([x.get("fade_gap_hit") for x in g[-20:]]),
        "no_edge_days_up_rate": rate([(x["outcome_sign"] > 0) if x["outcome_sign"] else None
                                      for x in no_edge]),
        "components": {k: rate([(x.get("component_hits") or {}).get(k) for x in g])
                       for k in comp_names},
        "mean_soxl_oc_on_lean_soxl": statistics.mean(lean_l) if lean_l else None,
        "mean_soxs_oc_on_lean_soxs": statistics.mean(lean_s) if lean_s else None,
    }


# ----------------------------------------------------------------------------- IO: data

def _yf():
    import warnings
    warnings.filterwarnings("ignore")
    import yfinance as yf
    return yf


def daily_bars(ticker: str, period: str = "10d") -> dict[date, dict]:
    try:
        h = _yf().Ticker(ticker).history(period=period, interval="1d", auto_adjust=False,
                                         timeout=HTTP_TIMEOUT)
    except Exception:
        return {}
    return {ts.date(): {"open": finite(r["Open"]), "high": finite(r["High"]),
                        "low": finite(r["Low"]), "close": finite(r["Close"])}
            for ts, r in h.iterrows()}


def intraday_bars(ticker: str) -> list[tuple[datetime, float]]:
    try:
        h = _yf().Ticker(ticker).history(period="5d", interval="5m", prepost=True,
                                         auto_adjust=False, timeout=HTTP_TIMEOUT)
    except Exception:
        return []
    return [(ts.to_pydatetime(), r["Close"]) for ts, r in h.iterrows()]


def soxx_weights() -> tuple[dict[str, float], str]:
    try:
        th = _yf().Ticker("SOXX").funds_data.top_holdings
        w = {str(sym): float(row["Holding Percent"]) for sym, row in th.head(10).iterrows()}
        if len(w) >= 8 and all(math.isfinite(v) and v > 0 for v in w.values()):
            return w, "yfinance"
    except Exception:
        pass
    return json.loads((STATE / "weights_fallback.json").read_text())["weights"], "fallback"


def http_json(url: str, headers: dict | None = None):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0", **(headers or {})})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return json.load(r)


def yahoo_news(tickers: list[str]) -> list[dict]:
    import feedparser
    out = []
    for t in tickers:
        try:
            f = feedparser.parse(f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={t}"
                                 "&region=US&lang=en-US",
                                 request_headers={"User-Agent": "Mozilla/5.0"})
        except Exception:
            continue
        for e in f.entries:
            p = e.get("published_parsed")
            if not p:
                continue
            out.append({"ticker": t, "title": e.get("title", "").strip(),
                        "link": e.get("link", ""),
                        "published": datetime(*p[:6], tzinfo=timezone.utc)})
    return out


def supabase_rows(table: str, params: dict) -> list[dict] | None:
    url, key = os.environ.get("SUPABASE_URL", "").rstrip("/"), os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not url or not key:
        return None
    q = "&".join(f"{k}={urllib.request.quote(str(v), safe='(),.:*')}" for k, v in params.items())
    try:
        return http_json(f"{url}/rest/v1/{table}?{q}", {"apikey": key, "Authorization": f"Bearer {key}"})
    except Exception:
        return None


def stocktwits(symbol: str, now_utc: datetime) -> dict | None:
    try:
        d = http_json(f"https://api.stocktwits.com/api/2/streams/symbol/{symbol}.json")
    except Exception:
        return None
    msgs = d.get("messages") or []
    tags = [((m.get("entities") or {}).get("sentiment") or {}).get("basic") for m in msgs]
    recent = 0
    for m in msgs:
        try:
            t = datetime.fromisoformat(m["created_at"].replace("Z", "+00:00"))
            recent += (now_utc - t) <= timedelta(hours=12)
        except Exception:
            pass
    return {"bull": tags.count("Bullish"), "bear": tags.count("Bearish"),
            "msgs": len(msgs), "last12h": recent}


def fred_today(day: date) -> list[str] | None:
    key = os.environ.get("FRED_API_KEY", "")
    if not key:
        return None
    try:
        d = http_json("https://api.stlouisfed.org/fred/releases/dates?file_type=json"
                      f"&api_key={key}&realtime_start={day}&realtime_end={day}"
                      "&include_release_dates_with_no_data=true&limit=1000")
    except Exception:
        return None
    names = {r.get("release_name", "") for r in d.get("release_dates", []) if r.get("date") == str(day)}
    return sorted(n for n in names if any(w.lower() in n.lower() for w in FRED_WATCH))


def earnings_soon(tickers: list[str], day: date, horizon_days: int = 7) -> list[dict]:
    out = []
    for t in tickers:
        try:
            ed = (_yf().Ticker(t).calendar or {}).get("Earnings Date")
            ed = ed[0] if isinstance(ed, list) and ed else ed
            ed = ed.date() if hasattr(ed, "date") and not isinstance(ed, date) else ed
            if isinstance(ed, date) and day <= ed <= day + timedelta(days=horizon_days):
                out.append({"ticker": t, "date": ed.isoformat()})
        except Exception:
            continue
    return sorted(out, key=lambda r: r["date"])


# ----------------------------------------------------------------------------- IO: state

def read_json(p: Path, default):
    try:
        return json.loads(p.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def write_json(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1, default=str) + "\n")
    tmp.replace(p)


def already_sent_on_origin(rel_path: str) -> bool:
    """A queued duplicate dispatch checks out the pre-push commit, so the local tree can't
    be trusted — ask origin/main directly."""
    try:
        subprocess.run(["git", "-C", str(REPO), "fetch", "-q", "origin", "main"],
                       check=True, timeout=60)
        r = subprocess.run(["git", "-C", str(REPO), "cat-file", "-e", f"origin/main:{rel_path}"],
                           capture_output=True, timeout=30)
        return r.returncode == 0
    except Exception as e:
        print(f"origin check failed ({e}); falling back to local tree only", file=sys.stderr)
        return False


def gh_output(**kv) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    line = "\n".join(f"{k}={v}" for k, v in kv.items())
    if path:
        with open(path, "a") as f:
            f.write(line + "\n")
    print(line)


# ----------------------------------------------------------------------------- grading

def grade_pending(today: date) -> None:
    sent = {p.stem for p in (STATE / "sent").glob("*.json")}
    changed = False
    for cp in sorted((STATE / "calls").glob("*.json")):
        d = cp.stem
        if d >= today.isoformat() or d not in sent:
            continue
        gp = STATE / "grades" / f"{d}.json"
        prior = read_json(gp, {})
        if prior.get("status") == "graded" and (prior.get("soxx_open_to_1030") is not None
                                                or (today - date.fromisoformat(d)).days > 5):
            continue
        call = read_json(cp, {})
        day = date.fromisoformat(d)
        bars = {t: daily_bars(t, "1mo").get(day, {}) for t in ETFS}
        soxx_1030 = None
        for ts, px in intraday_bars("SOXX") if (today - day).days <= 5 else []:
            t_et = ts.astimezone(ET)
            if t_et.date() == day and t_et.time() == time(10, 25):
                soxx_1030 = px   # close of the 10:25 bar = price at 10:30
        g = grade_day(call, bars["SOXX"], bars["SOXL"], bars["SOXS"], soxx_1030)
        write_json(gp, g)
        changed = True
        print(f"graded {d}: {g.get('status')} bias_hit={g.get('bias_hit')}")
    if changed or not (STATE / "scorecard.json").exists():
        grades = [read_json(p, {}) for p in sorted((STATE / "grades").glob("*.json"))]
        write_json(STATE / "scorecard.json", scorecard(grades, sent))


# ----------------------------------------------------------------------------- prepare

@dataclass
class Ctx:
    now_utc: datetime
    today: date


def build_call(ctx: Ctx) -> dict:
    today, now = ctx.today, ctx.now_utc
    cutoff = snapshot_cutoff(now, today)
    prev = previous_trading_day(today)
    since = datetime.combine(prev, time(16, 0), ET).astimezone(timezone.utc)

    weights, wsrc = soxx_weights()
    names = list(weights) + ETFS
    premarket, moves, prior_closes = {}, {}, {}
    for t in names:
        pc = finite(daily_bars(t).get(prev, {}).get("close"))
        prior_closes[t] = pc
        pm = premarket_last(intraday_bars(t), today, cutoff)
        if pc and pm and not pm[2]:
            premarket[t] = {"price": pm[0], "ts": pm[1].isoformat(), "prior_close": pc,
                            "move": pm[0] / pc - 1}
            moves[t] = pm[0] / pc - 1
        else:
            premarket[t] = {"price": pm[0] if pm else None, "ts": pm[1].isoformat() if pm else None,
                            "prior_close": pc, "move": None,
                            "why_missing": "no prior close" if not pc else "no bar" if not pm else "stale"}
            moves[t] = None
    imp = implied_open(weights, {t: moves.get(t) for t in weights})
    implied = imp["implied_soxx"]

    overnight = {}
    for t, label in OVERNIGHT.items():
        d = [v for _, v in sorted(daily_bars(t).items()) if v.get("close")]
        overnight[t] = {"label": label, "move": (d[-1]["close"] / d[-2]["close"] - 1) if len(d) >= 2 else None}
    try:
        fi = _yf().Ticker("NQ=F").fast_info
        nq = finite(fi["lastPrice"]) / finite(fi["previousClose"]) - 1
    except Exception:
        nq = None
    ov_moves = [v["move"] for v in overnight.values() if v["move"] is not None]

    news = filter_news(yahoo_news(list(weights)), since, cutoff)
    filings = supabase_rows("stock_raw_filings", {
        "select": "ticker,form_type,filed_at,primary_doc_url",
        "ticker": f"in.({','.join(weights)})", "filed_at": f"gte.{since.isoformat()}",
        "order": "filed_at.desc", "limit": "30"})
    posts = supabase_rows("stock_raw_truth_posts", {
        "select": "posted_at,content,url", "posted_at": f"gte.{since.isoformat()}",
        "order": "posted_at.desc", "limit": "200"})
    truth = None if posts is None else [
        {"posted_at": p["posted_at"], "text": re.sub(r"<[^>]+>", " ", p.get("content") or "")[:280],
         "url": p.get("url")}
        for p in posts if any(k in (p.get("content") or "").lower() for k in TRUTH_KEYWORDS)][:6]

    hist = read_json(STATE / "social_history.json", {})
    social = {}
    for s in SOCIAL_SYMBOLS:
        st = stocktwits(s, now)
        past = [h["last12h"] for h in hist.get(s, [])[-20:]]
        if st:
            st["vol_ratio"] = (st["last12h"] / statistics.median(past)) if len(past) >= 5 and statistics.median(past) > 0 else None
            hist.setdefault(s, []).append({"date": today.isoformat(), "last12h": st["last12h"]})
            hist[s] = hist[s][-60:]
        social[s] = st
    tagged = [v for v in social.values() if v]
    skew = (sum(v["bull"] for v in tagged) - sum(v["bear"] for v in tagged)) if tagged else None

    soxl_daily = [v for _, v in sorted(daily_bars("SOXL", "2y").items())]
    soxl_pm = premarket.get("SOXL", {})
    soxl_pc = prior_closes.get("SOXL")
    gap = soxl_pm.get("move")
    prev_bar = daily_bars("SOXL").get(prev, {})
    pm_prices = [p for t, p in intraday_bars("SOXL")
                 if t.astimezone(ET).date() == today and PREMARKET_START_ET <= t.astimezone(ET).time()
                 and t.astimezone(timezone.utc) < cutoff and finite(p)]
    levels = {"soxl_prior_close": soxl_pc, "soxl_prior_high": prev_bar.get("high"),
              "soxl_prior_low": prev_bar.get("low"),
              "soxl_pm_high": max(pm_prices) if pm_prices else None,
              "soxl_pm_low": min(pm_prices) if pm_prices else None,
              "soxl_gap": gap,
              "gap_fill": gap_fill_rate(soxl_daily, gap) if gap is not None else None}

    net_news = sum(n["tag"] for n in news) if news else 0
    return {
        "date": today.isoformat(), "generated_at": now.isoformat(timespec="seconds"),
        "snapshot_cutoff": cutoff.isoformat(), "prior_session": prev.isoformat(),
        "calendar_verified": calendar_covered(today),
        "weights": weights, "weights_source": wsrc,
        "premarket": premarket, "implied": imp,
        "implied_soxx": implied,
        "implied_soxl": 3 * implied if implied is not None else None,
        "implied_soxs": -3 * implied if implied is not None else None,
        "bias": bias_from(implied),
        "components": {"overnight_asia_eu": statistics.mean(ov_moves) if ov_moves else None,
                       "nasdaq_futures": nq, "news_net": net_news or None,
                       "social_skew": skew or None},
        "overnight": overnight, "nasdaq_futures": nq,
        "news": news[:10], "news_net": net_news,
        "filings": filings, "truth_social": truth,
        "social": social, "_social_history": hist,
        "levels": levels,
        "catalysts": {"earnings_next_7d": earnings_soon(list(weights), today),
                      "macro_today": fred_today(today)},
    }


def cmd_prepare(force: bool) -> int:
    now = datetime.now(timezone.utc)
    now_pt = now.astimezone(PT)
    today = now_pt.date()
    if not force:
        if not is_trading_day(today):
            print(f"{today} is not a trading day"); gh_output(send="false"); return 0
        if not in_brief_window(now_pt):
            print(f"{now_pt:%H:%M} PT is outside the 06:10-06:29 brief window"); gh_output(send="false"); return 0
    if not channels():
        # Checked before anything is written or pushed: a call committed without any way
        # to deliver it would use up the day (origin idempotency would block a retry).
        print("no delivery channel configured (need GMAIL_USER+GMAIL_APP_PASSWORD and/or "
              "TELEGRAM_BOT_TOKEN+TELEGRAM_CHAT_ID) — refusing to record a call", file=sys.stderr)
        return 1
    rel = f"semis_brief/calls/{today}.json"
    if not force and ((REPO / rel).exists() or already_sent_on_origin(rel)):
        print(f"call for {today} already exists on origin/main — not sending again")
        gh_output(send="false"); return 0

    grade_pending(today)
    call = build_call(Ctx(now, today))
    validate_call(call)
    hist = call.pop("_social_history")
    if force:
        rel = f"semis_brief/manual/{now_pt:%Y-%m-%d_%H%M}.json"
        call["manual"] = True
    else:
        write_json(STATE / "social_history.json", hist)
    write_json(REPO / rel, call)
    print(f"call written: {rel} bias={call['bias']} implied_soxx={call['implied_soxx']}")
    gh_output(send="true", call_path=rel)
    return 0


# ----------------------------------------------------------------------------- send

def pct(x, digits=2):
    return "n/a" if x is None else f"{x * 100:+.{digits}f}%"


BIAS_LABEL = {"lean_soxl": "lean SOXL", "lean_soxs": "lean SOXS", "no_edge": "no edge"}
GAP_LABEL = {"lean_soxl": "gap up", "lean_soxs": "gap down", "no_edge": "flat open"}
CALL_PAIR = {"lean_soxl": ("SOXL", "SOXS"), "lean_soxs": ("SOXS", "SOXL"), "no_edge": ("—", "—")}
BIAS_COLOR = {"lean_soxl": "#2a9d8f", "lean_soxs": "#e76f51", "no_edge": "#e9c46a"}


def render(call: dict, score: dict, last_grade: dict | None) -> tuple[str, str, str]:
    d = date.fromisoformat(call["date"])
    tags = []
    if call.get("manual"):
        tags.append("[MANUAL]")
    if not call.get("calendar_verified", True):
        tags.append("[CALENDAR UNVERIFIED]")
    follow, fade = CALL_PAIR[call["bias"]]
    gap = "no data" if call.get("implied_soxx") is None else GAP_LABEL[call["bias"]]
    subj = (f"{' '.join(tags) + ' ' if tags else ''}SOXL 6:15 · {gap} · follow: {follow} / "
            f"fade: {fade} · implied SOXX {pct(call['implied_soxx'])} (SOXL ~{pct(call['implied_soxl'], 1)}) · {d:%a %b %-d}")
    lines = [subj, ""]
    rows = []
    for t, w in sorted(call["weights"].items(), key=lambda kv: -kv[1]):
        pm = call["premarket"].get(t, {})
        c = call["implied"]["contrib"].get(t)
        lines.append(f"  {t:<5} w {w*100:4.1f}%  pre {pct(pm.get('move'))}  contrib {pct(c, 3)}"
                     + (f"  ({pm.get('why_missing')})" if pm.get("why_missing") else ""))
        rows.append(f"<tr><td>{t}</td><td>{w*100:.1f}%</td><td>{pct(pm.get('move'))}</td>"
                    f"<td>{pct(c, 3)}</td><td>{html.escape(pm.get('why_missing') or '')}</td></tr>")
    imp = call["implied"]
    etf = " · ".join(f"{t} {pct(call['premarket'].get(t, {}).get('move'))}" for t in ETFS)
    comp = call["components"]
    b = sign({"lean_soxl": 1, "lean_soxs": -1}.get(call["bias"], 0))

    def agree(v):
        s = sign(v)
        return "n/a" if v is None else "flat" if s == 0 else "agrees" if (b and s == b) else "disagrees" if b else ("up" if s > 0 else "down")

    ov = " · ".join(f"{v['label']} {pct(v['move'])}" for v in call["overnight"].values())
    cat = call["catalysts"]
    earn = ", ".join(f"{e['ticker']} {e['date']}" for e in cat["earnings_next_7d"]) or "none in 7 days"
    macro = "n/a" if cat["macro_today"] is None else (", ".join(cat["macro_today"]) or "none")
    news_html = "".join(f"<li>{'▲' if n['tag']>0 else '▼' if n['tag']<0 else '·'} <b>{n['ticker']}</b> "
                        f"<a href='{html.escape(n['link'])}'>{html.escape(n['title'])}</a></li>"
                        for n in call["news"]) or "<li>none since the close</li>"
    news_txt = "\n".join(f"  {'+' if n['tag']>0 else '-' if n['tag']<0 else '.'} {n['ticker']}: {n['title']}"
                         for n in call["news"]) or "  none since the close"
    fil = call["filings"]
    fil_txt = "n/a" if fil is None else (", ".join(f"{f['ticker']} {f['form_type']}" for f in fil) or "none")
    tru = call["truth_social"]
    tru_txt = "n/a" if tru is None else ("\n".join(f"  {t['posted_at'][:16]} {t['text'][:160]}" for t in tru) or "  none")
    soc = call["social"]
    def soc_one(s, v):
        if v is None:
            return f"{s} n/a"
        vol = f" vol×{v['vol_ratio']:.1f}" if v.get("vol_ratio") else ""
        return f"{s} {v['bull']}▲/{v['bear']}▼{vol}"
    soc_txt = " · ".join(soc_one(s, v) for s, v in soc.items())
    lv = call["levels"]
    gf = lv.get("gap_fill") or {}
    gf_txt = (f"{gf.get('bucket')}: filled {gf['fill_rate']*100:.0f}% of {gf['n']} days (2y)"
              if gf.get("fill_rate") is not None else "n/a")

    def f2(x):
        return "n/a" if x is None else f"{x:.2f}"
    lv_txt = (f"prior close {f2(lv['soxl_prior_close'])} · prior H/L {f2(lv['soxl_prior_high'])}/{f2(lv['soxl_prior_low'])}"
              f" · premarket H/L {f2(lv['soxl_pm_high'])}/{f2(lv['soxl_pm_low'])} · gap {pct(lv['soxl_gap'])}")
    lg = ""
    if last_grade and last_grade.get("status") == "graded":
        lg = (f"{last_grade['date']}: {BIAS_LABEL.get(last_grade['bias'], last_grade['bias'])} → SOXX o→c "
              f"{pct(last_grade['soxx_open_to_close'])}, SOXL o→c {pct(last_grade.get('soxl_open_to_close'))}, "
              f"hit={last_grade.get('bias_hit')}, SOXL open err {pct(last_grade.get('soxl_open_error'))}")
    sa = (score or {}).get("last20", {})

    def r(x):
        return "n/a" if not x or x.get("hit_rate") is None else f"{x['hit_rate']*100:.0f}% (n={x['n']})"
    def cr(x):
        return "n/a" if x is None else f"{x*100:+.1f}%"
    sc_txt = (f"last 20 called days: follow {r(sa.get('bias'))} ({cr(sa.get('follow_compounded'))}) · "
              f"fade {r(sa.get('fade_called'))} ({cr(sa.get('fade_compounded'))}) · "
              f"up-day base {r(sa.get('up_day_base_rate'))} · "
              f"fade-the-gap on all days {r((score or {}).get('fade_gap_null_last20'))}")
    bf = read_json(STATE / "backfill" / "backfill.json", {}).get("summary")
    bf_txt = (f"reconstructed with today's top-10 weights, {bf['first']}..{bf['last']} "
              f"({bf['n_days']} days): follow "
              f"{r(bf['follow'])} ({cr(bf['follow_compounded'])}) · fade {r(bf['fade'])} "
              f"({cr(bf['fade_compounded'])}), SOXL/SOXS open→close, 10 bps") if bf else ""
    comp_sc = " · ".join(f"{k} {r(v)}" for k, v in ((score or {}).get("components") or {}).items())

    text = "\n".join(lines[:1] + [
        "", f"{gap.upper()} — follow the gap: {follow} · fade the gap: {fade}",
        f"implied SOXX {pct(call['implied_soxx'])}, SOXL ~{pct(call['implied_soxl'],1)}, SOXS ~{pct(call['implied_soxs'],1)}",
        f"ETF premarket: {etf}",
        f"Top-10 coverage {imp['coverage']*100:.0f}% · up {imp.get('n_up',0)} / down {imp.get('n_down',0)} · dispersion {pct(imp.get('dispersion'))}",
        *lines[2:],
        "", f"Components vs bias: overnight {agree(comp['overnight_asia_eu'])} · NQ {agree(comp['nasdaq_futures'])} · news {agree(comp['news_net'])} · social {agree(comp['social_skew'])}",
        f"Overnight: {ov} · NQ futures {pct(call['nasdaq_futures'])}",
        f"Catalysts: earnings {earn} · macro today {macro}",
        "", "News since close:", news_txt, f"Filings: {fil_txt}",
        "", "Truth Social (chips/China/tariffs):", tru_txt,
        "", f"StockTwits: {soc_txt}",
        "", f"SOXL levels: {lv_txt}", f"Gap history: {gf_txt}",
        "", f"Yesterday: {lg or 'n/a'}", f"Scorecard: {sc_txt}", f"Components: {comp_sc or 'n/a'}",
        *([f"Backfill: {bf_txt}"] if bf_txt else []),
    ])
    htm = f"""<html><body style="font-family:-apple-system,Helvetica,Arial,sans-serif;color:#264653;max-width:720px">
<div style="background:{BIAS_COLOR[call['bias']]};color:#fff;padding:10px 14px;border-radius:8px;font-size:18px">
<b>{gap}</b> — follow the gap: <b>{follow}</b> · fade the gap: <b>{fade}</b><br>
<span style="font-size:14px">implied SOXX {pct(call['implied_soxx'])} · SOXL ~{pct(call['implied_soxl'],1)} · SOXS ~{pct(call['implied_soxs'],1)}</span></div>
<p>ETF premarket: {etf}<br>Top-10 coverage {imp['coverage']*100:.0f}% · up {imp.get('n_up',0)} / down {imp.get('n_down',0)} · dispersion {pct(imp.get('dispersion'))}</p>
<table cellpadding="4" style="border-collapse:collapse;font-size:13px"><tr style="background:#e8f4f2">
<th align=left>Holding</th><th>Weight</th><th>Premarket</th><th>Contribution</th><th></th></tr>{''.join(rows)}</table>
<p><b>Components vs bias:</b> overnight {agree(comp['overnight_asia_eu'])} · NQ {agree(comp['nasdaq_futures'])} · news {agree(comp['news_net'])} · social {agree(comp['social_skew'])}</p>
<p><b>Overnight:</b> {html.escape(ov)} · NQ futures {pct(call['nasdaq_futures'])}<br>
<b>Catalysts:</b> earnings {html.escape(earn)} · macro today {html.escape(macro)}</p>
<p><b>News since close</b></p><ul style="font-size:13px">{news_html}</ul><p><b>Filings:</b> {html.escape(fil_txt)}</p>
<p><b>Truth Social</b> (chips/China/tariffs)</p><pre style="white-space:pre-wrap;font-size:12px">{html.escape(tru_txt)}</pre>
<p><b>StockTwits:</b> {html.escape(soc_txt)}</p>
<p><b>SOXL levels:</b> {lv_txt}<br><b>Gap history:</b> {html.escape(gf_txt)}</p>
<p style="background:#fdf6e3;padding:8px;border-radius:6px"><b>Yesterday:</b> {html.escape(lg or 'n/a')}<br>
<b>Scorecard:</b> {html.escape(sc_txt)}<br><b>Components:</b> {html.escape(comp_sc or 'n/a')}
{('<br><b>Backfill:</b> ' + html.escape(bf_txt)) if bf_txt else ''}</p>
</body></html>"""
    return subj, text, htm


def cmd_send(call_path: str) -> int:
    call = read_json(REPO / call_path, None)
    if not call:
        print(f"no call at {call_path}", file=sys.stderr); return 1
    score = read_json(STATE / "scorecard.json", {})
    grades = sorted((STATE / "grades").glob("*.json"))
    last = read_json(grades[-1], None) if grades else None
    subj, text, htm = render(call, score, last)
    sent_via = []
    for ch in channels():
        try:
            if ch == "email":
                send_email(subj, text, htm)
                sent_via.append("email")
            else:
                sent_via.append(send_telegram(subj, text, htm))
            print(f"sent via {ch}: {subj}")
        except Exception as e:                      # one channel failing must not block the other
            print(f"{ch} send failed: {type(e).__name__}: {e}", file=sys.stderr)
    if not sent_via:
        print("no channel delivered the brief", file=sys.stderr)
        return 1
    if not call.get("manual"):
        write_json(STATE / "sent" / f"{call['date']}.json",
                   {"date": call["date"], "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "subject": subj, "channels": sent_via})
    return 0


def channels() -> list[str]:
    """Delivery channels whose credentials are present in the environment."""
    out = []
    if os.environ.get("GMAIL_USER") and os.environ.get("GMAIL_APP_PASSWORD"):
        out.append("email")
    if os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHAT_ID"):
        out.append("telegram")
    return out


def send_email(subj: str, text: str, htm: str) -> None:
    user, pw = os.environ["GMAIL_USER"], os.environ["GMAIL_APP_PASSWORD"]
    to = os.environ.get("BRIEF_TO") or user
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subj, user, to
    msg.attach(MIMEText(text, "plain", "utf-8"))
    msg.attach(MIMEText(htm, "html", "utf-8"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(user, pw)
        s.sendmail(user, [to], msg.as_string())


TELEGRAM_LIMIT = 4000   # API hard limit is 4096 chars per message


def telegram_chunks(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Split on line boundaries so no message exceeds the Telegram limit; a single
    over-long line is hard-cut rather than dropped."""
    chunks, cur = [], ""
    for line in text.splitlines():
        while len(line) > limit:
            if cur:
                chunks.append(cur); cur = ""
            chunks.append(line[:limit]); line = line[limit:]
        if cur and len(cur) + len(line) + 1 > limit:
            chunks.append(cur); cur = ""
        cur = f"{cur}\n{line}" if cur else line
    if cur:
        chunks.append(cur)
    return chunks


def _telegram_post(token: str, chat: str, part: str) -> None:
    body = json.dumps({"chat_id": chat, "text": part, "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        if not json.load(r).get("ok"):
            raise RuntimeError("telegram sendMessage returned ok=false")


def send_telegram(subj: str, text: str, htm: str) -> str:
    """Returns "telegram", or "telegram_partial" if the first chunk (subject + both calls)
    posted but a later one failed — the operator saw the call, so it counts as delivered
    and gets graded; the partial flag keeps the bookkeeping honest. A first-chunk failure
    raises (nothing was seen)."""
    token, chat = os.environ["TELEGRAM_BOT_TOKEN"], os.environ["TELEGRAM_CHAT_ID"]
    parts = telegram_chunks(text)
    _telegram_post(token, chat, parts[0])       # text starts with the subject line
    for part in parts[1:]:
        try:
            _telegram_post(token, chat, part)
        except Exception as e:
            print(f"telegram later chunk failed: {type(e).__name__}", file=sys.stderr)
            return "telegram_partial"
    return "telegram"


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--force", action="store_true", help="manual run: bypass window/idempotency, never graded")
    s = sub.add_parser("send")
    s.add_argument("call_path")
    a = ap.parse_args()
    return cmd_prepare(a.force) if a.cmd == "prepare" else cmd_send(a.call_path)


if __name__ == "__main__":
    sys.exit(main())
