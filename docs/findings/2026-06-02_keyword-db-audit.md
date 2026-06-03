# Keyword DB audit — why "Computex positives" got classified neutral

**Date:** 2026-06-02
**Status:** Exploration — quantified gap; not yet acted on.

## What's actually in the keyword DB

Live query on `stock_keyword_rules where kind='news' and enabled=true`:

| Direction | Count | Notes |
|---|---|---|
| `neutral` | 22 | Almost all are *ticker-name matchers* — "Apple" → AAPL, "Nvidia" → NVDA, etc. They identify *which ticker* the article is about, not the *direction*. |
| `long` | 1 | A single regex covering ~10 bullish keywords (see below). |
| `short` | 1 | A single regex with similar coverage on the bearish side. |
| **Total** | **24** | |

The bullish regex (one row in the DB):

```
\b(beat(s)?|jumps?|rally|surge(s|d)?|raises?\s+guidance|buyback|
   acquisition|upgrade(s|d)?|record\s+(high|profit|revenue)|
   dividend|strong\s+(earnings|results)|outperform)\b
```

That's the **entire bullish classifier**. ~10 vocabulary items.

For comparison, `agents/_catalyst_policy.py::CAUSAL_KEYWORDS` (a
separate set used by `thesis_agent` to *promote* signals back to
catalyst tier) has ~70 keywords across 7 categories. The promotion
path is better-stocked than the *original* direction classifier.

## Why "Nvidia, Qualcomm, Intel, Marvell offer up positives at Computex" missed

The headline contains zero matches against the bullish regex:

- `positives` — not in the list.
- `Computex` — conference name, not a directional verb.
- `offer up` — not in the list.

So the article was classified `neutral`. With `neutral`, `thesis_agent`
adds it as **context** to whatever signal cluster forms, but it
contributes no points to `catalyst_score`. Without catalyst points,
the signal stays in MOMENTUM_ONLY territory and gets `AVOID_CHASE`'d
by the intelligence layer downstream.

This is the structural cause of the 5/22–6/2 silence: the rubric was
working, the cap (until 6/2's fix) was actively blocking, AND the
classifier was failing to recognize most real bullish catalysts.
Three independent issues stacked.

## What keywords would have caught it (concrete additions)

Grouped by category. These come from looking at the 30 most recent
`AVOID_CHASE` setups + their underlying articles, plus standard financial
news vocabulary.

### Conference / industry events (currently zero coverage)
- `computex`, `ces`, `gtc`, `wwdc`, `google i/o`, `microsoft ignite`,
  `oracle openworld`, `aws reinvent`, `goldman tech conference`,
  `morgan stanley tech conference`
- These are scheduled, calendar-known catalysts. Whenever a megacap
  presents, sentiment from coverage typically moves the stock.

### AI / cloud catalysts (zero coverage)
- `ai partnership`, `cloud win`, `ai deal`, `ai contract`,
  `ai integration`, `gpu order`, `gpu allocation`, `accelerator`,
  `inference deployment`, `data center deal`, `chip ban`, `chip export`
- AI mega-caps dominate the watchlists; these terms appear in nearly
  every move-worthy article.

### Geopolitical / trade (zero coverage)
- `tariff`, `trade war`, `china access`, `china ban`, `export controls`,
  `huawei`, `tsmc`, `sanctions`, `embargo`, `denied entry list`
- Affect IT/semis directly. The current pipeline only catches these
  via `truth_social_post:china` etc., not via news.

### Soft positives currently missed
- `positives`, `bullish on`, `street-high target`, `raised pt`,
  `raises pt`, `bull case`, `upside`, `re-rating`, `re-rated`,
  `multiple expansion`, `firm initiates`, `initiates with buy`

### Soft negatives currently missed
- `negatives`, `bearish on`, `cut to underperform`, `cuts pt`,
  `downside risk`, `headwind`, `derisk`, `de-rated`, `multiple compression`

### Regulatory / FDA (partial coverage)
- Already have: `fda approval`, `fda rejection`, `pdufa`, `complete response letter`.
- Missing: `ema approval`, `breakthrough designation`, `priority review`,
  `phase 3 readout`, `topline data`, `survival benefit`, `met endpoint`,
  `missed endpoint`, `enrollment paused`.

## Estimated impact

If we add ~80 keywords across these categories, the share of articles
classified neutral should drop from today's ~82% (per
`pulsecheck_news.classifier_neutrality`) toward 50-60%. That's still
high — most news IS neutral — but it would surface the 20-30% of
articles that ARE catalysts and currently get lost.

This won't make `thesis_agent` produce 5/day automatically — the rubric
still needs the scoring path to compute high enough — but it would
materially increase the rate of catalyst-tier candidates.

## How to ship (when ready)

The keyword DB is a Supabase table — additions don't require a deploy:

```sql
insert into stock_keyword_rules
  (kind, enabled, keyword, match_type, direction_prior, rule_label)
values
  ('news', true, 'computex',         'icontains', 'long',  'conf_computex'),
  ('news', true, 'wins ai contract', 'icontains', 'long',  'cat_ai_contract'),
  …
;
```

The bullish regex is one row — better to *replace* it with a wider regex
than to scatter literal terms (regex compiles once per article rather
than 80 substring matches).

## What actually shipped (2026-06-02)

**Migration 0036 added 46 catalyst phrases** as `icontains` rules (regex
reserved for the future ambiguity-fix pass). Categories:

| Category | Count | Direction |
|---|---|---|
| Price target raises | 6 | long |
| Price target cuts | 4 | short |
| Analyst initiations (buy/outperform) | 4 | long |
| Analyst sells/downgrades | 5 | short |
| AI / cloud catalysts | 6 | long |
| FDA / Biotech catalysts | 7 | long |
| FDA / Biotech bearish | 5 | short |
| Geopolitical / tariffs | 5 | short |
| Operational risk | 4 | short |

**Total enabled news rules: 70** (was 24). Distribution: 24 long, 24
short, 22 neutral (ticker-name matchers).

Specifically NOT added (deferred to a follow-up that needs regex):
- Generic verbs like "positive" / "negative" / "rally" alone — false
  positive risk on context like "rejected bear case"
- Conference names without context (Computex / WWDC) — the venue is
  not the catalyst; "positives at Computex" is, but without a
  compound matcher the venue alone fires too broadly.
- The companion `2026-06-02_sev2-news-bar-design.md` proposal covers
  the latter via watchlist-aware half-points.

**Reversal:** any rule is independently disable-able:

```sql
update stock_keyword_rules set enabled=false where rule_label='kwd0036_xyz';
```

No deploy required. news_agent reloads rules on every run.

## What would change my mind on rolling it out

- A pulsecheck-derived precision/recall measurement on the new
  classifications. Add ~10 keywords as a beta; monitor
  `classifier_neutrality` and a manual spot-check of newly bullish-flagged
  articles for false positives. Iterate.

## Cross-references

- Classifier code: `agents/news_agent.py::load_rules` (reads
  `stock_keyword_rules`).
- Promotion fallback: `agents/_catalyst_policy.py::CAUSAL_KEYWORDS`
  (separate, larger set).
- Live pulse: `pulsecheck_news.classifier_neutrality` (24h neutral share,
  warns at >80%).
- Related finding: `docs/findings/2026-06-02_sev2-news-bar-design.md`
  proposes a complementary band — let *neutral* news on watchlisted
  tickers count for half-points without needing keyword promotion.
