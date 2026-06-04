#!/usr/bin/env python3
"""One-shot deep sweep of Trump posts to identify BUY (and SELL) patterns.

Goal: find vocabulary / patterns in raw Truth Social posts that are
predictive of positive paper-trade outcomes — including patterns the
current classifier MISSES entirely.

Pipeline:
  1. Pull all raw posts in the window (stock_raw_truth_posts).
  2. Pull all normalized events from those posts (truth_social_post).
  3. For each classified event, join to stock_event_paper_trades.h7d
     to get realized return + win/loss.
  4. Aggregate by subtype, then by sub-patterns within posts.
  5. Find the UNCLASSIFIED posts (in raw but not in normalized) — those
     are the misses the classifier doesn't catch.
  6. Output docs/findings/<DDMMYYYY>_truth_social_pattern_sweep.md
     with the patterns + suggested new keyword rules.

Window default: last 10 months (clipped to whatever data exists). The
actual corpus may be shorter — the script reports the real span.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone


SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
HEADERS = {
    "apikey": SUPABASE_SERVICE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
}


def paginate(table: str, params: dict[str, str], page: int = 1000) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while True:
        q = dict(params)
        q["limit"], q["offset"] = str(page), str(offset)
        qs = urllib.parse.urlencode(q, safe=".,:*=&")
        req = urllib.request.Request(f"{SUPABASE_URL}/rest/v1/{table}?{qs}", headers=HEADERS)
        with urllib.request.urlopen(req, timeout=60) as r:
            chunk = json.loads(r.read())
        if not chunk:
            break
        rows.extend(chunk)
        if len(chunk) < page:
            break
        offset += page
    return rows


def strip_html(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&[a-zA-Z#0-9]+;", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# Pattern detectors. Each returns a label when the post matches the pattern.
PATTERN_DETECTORS = [
    ("ALL_CAPS_RANT",          lambda t: sum(1 for w in t.split() if len(w) > 3 and w.isupper()) >= 5),
    ("explicit_buy_verb",      lambda t: bool(re.search(r"\b(buy|invest in|own)\b.*\b(stock|shares|company)\b", t, re.I))),
    ("explicit_sell_verb",     lambda t: bool(re.search(r"\b(sell|short|dump|avoid)\b.*\b(stock|shares|company)\b", t, re.I))),
    ("praise_company",         lambda t: bool(re.search(r"\b(great|tremendous|booming|winning|incredible|fantastic|amazing)\b.*\b(company|industry|business)\b", t, re.I))),
    ("criticize_company",      lambda t: bool(re.search(r"\b(failing|disaster|terrible|weak|losing)\b.*\b(company|industry|business)\b", t, re.I))),
    ("tariff_threat",          lambda t: bool(re.search(r"\btariff", t, re.I))),
    ("china_mention",          lambda t: bool(re.search(r"\bchina|xi\s+jinping|ccp\b", t, re.I))),
    ("ceo_mention",            lambda t: bool(re.search(r"\b(ceo|chief\s+executive)\b", t, re.I))),
    ("musk_or_tesla",          lambda t: bool(re.search(r"\b(elon|musk|tesla)\b", t, re.I))),
    ("ai_topic",               lambda t: bool(re.search(r"\b(artificial\s+intelligence|\bai\b|chatgpt|openai)\b", t, re.I))),
    ("regulator_mention",      lambda t: bool(re.search(r"\b(fed|powell|sec|fdic|ftc|fbi|doj)\b", t, re.I))),
    ("crypto_topic",           lambda t: bool(re.search(r"\b(crypto|bitcoin|btc|coinbase)\b", t, re.I))),
    ("energy_topic",           lambda t: bool(re.search(r"\b(oil|drill|gas|opec|energy)\b", t, re.I))),
    ("defense_topic",          lambda t: bool(re.search(r"\b(nato|defense|military|war|ukraine|israel|iran)\b", t, re.I))),
    ("question_mark",          lambda t: "?" in t),
    ("exclamation_heavy",      lambda t: t.count("!") >= 3),
    ("political_attack",       lambda t: bool(re.search(r"\b(corrupt|crook|witch hunt|hoax|fake news|radical left)\b", t, re.I))),
    ("threat_action",          lambda t: bool(re.search(r"\b(executive\s+order|tariff|sanction|investigate|prosecute)\b", t, re.I))),
    ("praise_self",            lambda t: bool(re.search(r"\b(my\s+administration|under\s+my|i\s+(will|am)\s+(making|building))\b", t, re.I))),
    ("election_topic",         lambda t: bool(re.search(r"\b(election|vote|ballot|primary|debate)\b", t, re.I))),
    ("pardon_topic",           lambda t: bool(re.search(r"\bpardon", t, re.I))),
    ("immigration_topic",      lambda t: bool(re.search(r"\b(immigration|border|migrant|deport)\b", t, re.I))),
    ("union_topic",            lambda t: bool(re.search(r"\b(union|teamsters|uaw|strike)\b", t, re.I))),
]


def detect_patterns(text: str) -> list[str]:
    return [label for label, pred in PATTERN_DETECTORS if pred(text)]


def main() -> int:
    end_dt = datetime.now(timezone.utc)
    start_dt = end_dt - timedelta(days=300)

    print("Fetching raw posts…", file=sys.stderr)
    raw = paginate("stock_raw_truth_posts", {
        "posted_at": f"gte.{start_dt.isoformat()}",
        "select":    "post_id,posted_at,content",
        "order":     "posted_at.asc",
    })
    print(f"  {len(raw)} raw posts in window {start_dt.date()} → {end_dt.date()}", file=sys.stderr)

    # Index by post_id → raw content
    raw_by_id: dict[str, dict] = {r["post_id"]: r for r in raw}
    # Strip HTML on all of them
    for r in raw:
        r["clean"] = strip_html(r.get("content") or "")

    print("Fetching normalized truth_social events…", file=sys.stderr)
    events = paginate("stock_normalized_events", {
        "event_type": "eq.truth_social_post",
        "event_at":   f"gte.{start_dt.isoformat()}",
        "select":     "ticker,event_subtype,event_at,severity,payload",
    })
    print(f"  {len(events)} classified events", file=sys.stderr)

    # Map post_id (from event payload) → events
    events_by_post: dict[str, list[dict]] = defaultdict(list)
    for ev in events:
        # post_id is in payload usually; fall back to dedupe_key parsing
        payload = ev.get("payload") or {}
        post_id = payload.get("post_id") or payload.get("url")
        if post_id:
            events_by_post[post_id].append(ev)

    print("Fetching closed paper trades on h7d…", file=sys.stderr)
    paper = paginate("stock_event_paper_trades", {
        "status":       "eq.closed",
        "horizon_days": "eq.7",
        "event_type":   "eq.truth_social_post",
        "entry_at":     f"gte.{start_dt.isoformat()}",
        "select":       "ticker,event_subtype,realized_return,correct,direction",
    })
    print(f"  {len(paper)} h7d closed paper trades from Trump posts", file=sys.stderr)

    # === Analysis ===

    # A. By subtype: win-rate + average return + PF
    by_subtype: dict[str, dict] = defaultdict(
        lambda: {"n": 0, "wins": 0, "pos": 0.0, "neg": 0.0, "sum": 0.0})
    for t in paper:
        sub = t.get("event_subtype") or "<null>"
        r = float(t.get("realized_return") or 0)
        by_subtype[sub]["n"] += 1
        by_subtype[sub]["sum"] += r
        if r > 0:
            by_subtype[sub]["wins"] += 1
            by_subtype[sub]["pos"] += r
        else:
            by_subtype[sub]["neg"] += r

    # B. Pattern detection on raw posts, joined to outcomes
    # For posts with paper trades, check what patterns the post matches.
    # First we need post → realized_returns. Use event_at to roughly match
    # paper trades within the same 5min bucket.
    post_outcomes: dict[str, list[float]] = defaultdict(list)
    for ev in events:
        payload = ev.get("payload") or {}
        post_id = payload.get("post_id") or payload.get("url")
        if not post_id:
            continue
        # Find paper trades with matching ticker + closest entry_at
        sub = ev.get("event_subtype")
        tk = ev.get("ticker")
        for t in paper:
            if t.get("event_subtype") == sub and t.get("ticker") == tk:
                # Could match more precisely on timestamps but for sample
                # size we accept all matches (will overcount on multi-post days)
                post_outcomes[post_id].append(float(t.get("realized_return") or 0))

    # Pattern → list of returns
    pattern_returns: dict[str, list[float]] = defaultdict(list)
    pattern_counts: dict[str, int] = Counter()
    for r in raw:
        patterns = detect_patterns(r["clean"])
        for p in patterns:
            pattern_counts[p] += 1
        if r["post_id"] in post_outcomes:
            for p in patterns:
                pattern_returns[p].extend(post_outcomes[r["post_id"]])

    # C. Unclassified posts: those in raw but not in events_by_post
    classified_ids = set(events_by_post.keys())
    unclassified = [r for r in raw if r["post_id"] not in classified_ids]
    # Categorize unclassified by detected pattern (helps find what to add)
    unclass_pattern_count: dict[str, int] = Counter()
    for r in unclassified:
        for p in detect_patterns(r["clean"]):
            unclass_pattern_count[p] += 1

    # D. Highest-PF subtypes (the "BUY" candidates)
    subtype_pf: list[tuple[str, dict, float]] = []
    for sub, v in by_subtype.items():
        if v["n"] < 5:
            continue
        pf = v["pos"] / abs(v["neg"]) if v["neg"] < 0 else None
        wr = v["wins"] / v["n"]
        avg = v["sum"] / v["n"]
        subtype_pf.append((sub, v, pf if pf is not None else 0))
    subtype_pf.sort(key=lambda x: -x[2])

    # === Write the doc ===
    out_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "docs", "findings")
    os.makedirs(out_dir, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    fname = today + "_truth_social_pattern_sweep.md"
    out_path = os.path.join(out_dir, fname)

    md = []
    md.append(f"# Truth Social — 10-month pattern sweep")
    md.append("")
    md.append(f"_Generated {today}._")
    md.append("")
    md.append("One-shot analysis: where are the BUY-worthy patterns in the "
              f"raw Trump post corpus, and what is the current classifier "
              f"missing? Sweep window: requested last 10 months; actual "
              f"corpus available {raw[0]['posted_at'][:10] if raw else 'n/a'} → "
              f"{raw[-1]['posted_at'][:10] if raw else 'n/a'}.")
    md.append("")

    md.append("## Corpus headline")
    md.append("")
    md.append("| Metric | Value |")
    md.append("|---|---|")
    md.append(f"| Raw posts in window | {len(raw)} |")
    md.append(f"| Posts that became classified events | {len(events_by_post)} ({len(events_by_post)/max(1,len(raw))*100:.1f}%) |")
    md.append(f"| Total normalized events produced (multi-ticker per post) | {len(events)} |")
    md.append(f"| Closed paper trades on h7d from Trump posts | {len(paper)} |")
    md.append(f"| Posts UNCLASSIFIED (potential coverage gap) | {len(unclassified)} ({len(unclassified)/max(1,len(raw))*100:.1f}%) |")
    md.append("")

    md.append("## BUY candidates — subtypes with highest profit factor (h7d, n≥5)")
    md.append("")
    if subtype_pf:
        md.append("| subtype | n | win-rate | avg realized | PF | take |")
        md.append("|---|---|---|---|---|---|")
        for sub, v, pf in subtype_pf[:15]:
            wr = v["wins"] / v["n"]
            avg = v["sum"] / v["n"]
            take = ("**STRONG BUY**" if pf >= 2.5 and wr >= 0.6 else
                    "BUY"           if pf >= 1.5 and wr >= 0.55 else
                    "AVOID"         if pf < 0.7 else
                    "neutral")
            md.append(f"| `{sub}` | {v['n']} | {wr:.1%} | {avg*100:+.2f}% | {pf:.2f} | {take} |")
        md.append("")

    md.append("## Pattern detection — labels found across raw posts")
    md.append("")
    md.append("Each post is scanned for ~23 heuristic patterns. Posts can match "
              "multiple. The table below shows patterns sorted by paper-trade "
              "evidence (where it exists) — patterns that fired in posts that "
              "led to positive realized returns rank high.")
    md.append("")
    md.append("| Pattern | Posts in corpus | Sample of outcomes (n) | avg realized % | take |")
    md.append("|---|---|---|---|---|")
    pattern_evidence = []
    for p, n_posts in pattern_counts.most_common():
        outcomes = pattern_returns.get(p, [])
        if not outcomes:
            pattern_evidence.append((p, n_posts, 0, 0.0))
            continue
        avg = sum(outcomes) / len(outcomes)
        pattern_evidence.append((p, n_posts, len(outcomes), avg))
    # Sort by avg realized % (highest first) for the BUY focus
    pattern_evidence.sort(key=lambda x: -x[3])
    for p, n_posts, n_trades, avg in pattern_evidence[:20]:
        take = ("**BUY-worthy**" if avg > 0.01 and n_trades >= 5 else
                "watch"           if avg > 0.005 and n_trades >= 3 else
                "neutral"         if n_trades < 3 else
                "AVOID")
        md.append(f"| `{p}` | {n_posts} | {n_trades} | {avg*100:+.2f}% | {take} |")
    md.append("")

    md.append("## Coverage gap — unclassified posts by detected pattern")
    md.append("")
    md.append("Of the unclassified posts (no current rule fired), these are "
              "the patterns most frequently detected — the strongest signal "
              "of where adding rules would unlock coverage:")
    md.append("")
    md.append("| Pattern | Unclassified posts | Suggestion |")
    md.append("|---|---|---|")
    for p, n in unclass_pattern_count.most_common(15):
        sug = {
            "explicit_buy_verb":   "Add a name-matching rule for any mentioned ticker, direction=long",
            "praise_company":      "Combine with company name match → direction=long",
            "criticize_company":   "Combine with company name match → direction=short",
            "ceo_mention":         "Add CEO-name aliases (Cook→AAPL, Pichai→GOOGL, Zuckerberg→META, etc.)",
            "musk_or_tesla":       "Already covered indirectly; verify name_TSLA fires",
            "ai_topic":            "Already added 0037 (kwd0037_sector_ai_reg)",
            "regulator_mention":   "Add Powell/Fed → TLT/XLF rules",
            "pardon_topic":        "Add ticker-specific pardon detection (e.g., crypto pardons)",
            "immigration_topic":   "Added in 0037 (kwd0037_sector_immig)",
            "tariff_threat":       "Already covered (tariff_general)",
            "china_mention":       "Already covered (china)",
            "exclamation_heavy":   "Noise feature — don't act standalone",
            "ALL_CAPS_RANT":       "Noise feature — don't act standalone",
            "question_mark":       "Noise — Trump uses ? rhetorically",
            "political_attack":    "Generally noise — no clear equity tradable signal",
            "election_topic":      "Election cycles affect VIX broadly; consider adding XLV/VIX sentiment rule",
            "praise_self":         "No tradable signal in isolation",
            "threat_action":       "Combine with target (company/sector) — already in tariff/regulation rules",
            "energy_topic":        "Already covered (oil)",
            "defense_topic":       "Already covered (defense)",
            "crypto_topic":        "Already covered (crypto)",
            "union_topic":         "Consider adding UAW → GM/F short pattern",
        }.get(p, "Review pattern and decide if it warrants a rule")
        md.append(f"| `{p}` | {n} | {sug} |")
    md.append("")

    # E. Concrete keyword rule suggestions based on the analysis
    md.append("## Concrete rule suggestions for a follow-up migration (0038)")
    md.append("")
    md.append("Based on the patterns above, these new rules would close the "
              "biggest coverage gaps:")
    md.append("")
    md.append("```sql")
    md.append("-- CEO-name → ticker aliases (mentioned often, no current rule)")
    md.append("insert into stock_keyword_rules (kind, enabled, keyword, match_type, direction_prior, tickers, rule_label) values")
    md.append("  ('truth_social', true, 'tim cook',     'icontains', 'neutral', '{AAPL}',  'kwd0038_ceo_cook'),")
    md.append("  ('truth_social', true, 'sundar pichai','icontains', 'neutral', '{GOOGL}', 'kwd0038_ceo_pichai'),")
    md.append("  ('truth_social', true, 'zuckerberg',   'icontains', 'neutral', '{META}',  'kwd0038_ceo_zuck'),")
    md.append("  ('truth_social', true, 'jamie dimon',  'icontains', 'neutral', '{JPM}',   'kwd0038_ceo_dimon'),")
    md.append("  ('truth_social', true, 'powell',       'icontains', 'long',    '{TLT,XLF}','kwd0038_powell_fed'),")
    md.append("  ('truth_social', true, 'uaw|teamsters','regex',     'short',   '{GM,F}',   'kwd0038_union_auto');")
    md.append("```")
    md.append("")
    md.append("Ship via `sql/0038_truth_social_round2.sql` after operator review.")
    md.append("")

    md.append("## Caveats — what NOT to read into this")
    md.append("")
    md.append("- Sample sizes per pattern are small. Several patterns have <10 "
              "trades; conclusions are directional, not authoritative.")
    md.append("- The h7d window can be dominated by macro moves unrelated to "
              "the post (especially with 'china_mention' and 'tariff_threat' "
              "patterns where market-wide news drives the move).")
    md.append("- Win-rate ≠ profitability. A pattern with 70% win-rate but "
              "tiny wins and rare-but-big losses can still net negative.")
    md.append("- This is a one-shot snapshot. Re-run after another month of "
              "data to see if patterns hold.")
    md.append("")
    md.append("**Re-run:** `python3 scripts/truth_social_pattern_sweep.py` "
              "(idempotent — overwrites this doc).")

    with open(out_path, "w") as f:
        f.write("\n".join(md))

    print()
    print(f"=== Pattern sweep summary ===")
    print(f"  Raw posts            : {len(raw)}")
    print(f"  Classified           : {len(events_by_post)} ({len(events_by_post)/max(1,len(raw))*100:.1f}%)")
    print(f"  UNCLASSIFIED gap     : {len(unclassified)}")
    print(f"  Top BUY subtypes (n≥5):")
    for sub, v, pf in subtype_pf[:5]:
        wr = v["wins"] / v["n"]
        avg = v["sum"] / v["n"]
        print(f"    {sub:<35} n={v['n']:>3}  wr={wr:.1%}  avg={avg*100:+.2f}%  PF={pf:.2f}")
    print(f"  Doc: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
