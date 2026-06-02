# Can the named independent research firms be incorporated?

**Date:** 2026-06-02
**Status:** Exploration — feasibility per firm + free-tier alternatives.

## The list

Firms the user surfaced (typical broker-platform "Independent Research"
section):

- Trading Central
- Jefferson Research
- Zacks Investment Research
- McLean Capital Management
- Argus Research / A6 Quantitative
- Refinitiv / Verus
- ISS-EVA

## Direct integration — feasibility honest table

| Firm | Distribution model | Free API? | Verdict for solo / free-tier project |
|---|---|---|---|
| **Trading Central** | Broker-licensed widgets, OEM SaaS | No public API; broker-embedded | ❌ No direct integration on free tier |
| **Jefferson Research** | Subscription + broker | No | ❌ Same |
| **Zacks** | Public + subscription | **Partial — free RSS, free screener, public Zacks Rank changes** | ✅ Yes — ingestable |
| **McLean Capital Mgmt** | Boutique, mostly institutional | No | ❌ |
| **Argus Research / A6** | Broker-licensed + subscription | No | ❌ |
| **Refinitiv / Verus** | Enterprise data terminal (LSEG) | No (Workspace API gated by enterprise contract) | ❌ |
| **ISS-EVA** | Institutional Shareholder Services subscription | No | ❌ |

**Net: 1 of 7 has free tier (Zacks).** The rest are paid enterprise /
broker-distributed.

## What the firms produce that DOES leak to us today (for free)

When any of these firms changes a rating or publishes a note, the
*coverage of that note* tends to land in mainstream news. We already
ingest Seeking Alpha + RSS feeds via `news_agent`. So:

- "Trading Central raises NVDA to buy" → Seeking Alpha post → our news_article event
- "Zacks Rank goes from 3 to 1 on AAPL" → multiple aggregators pick it up
- "Argus initiates META with buy" → headline coverage

**The information leaks; it just isn't directly classified.** Today's
keyword DB (24 rules, see `2026-06-02_keyword-db-audit.md`) catches
`upgrade`, `downgrade`, `outperform`, `underperform`, `buy rating`,
`sell rating` — generic verbs, not firm names.

**Concrete enhancement (low cost, high yield):** add the firm names as
matchable keywords with `direction_prior` inferred from co-occurring
verbs. Example:

```sql
insert into stock_keyword_rules (kind, enabled, keyword, match_type, direction_prior, rule_label) values
('news', true, 'zacks raises',          'icontains', 'long',  'firm_zacks_raise'),
('news', true, 'zacks lowers',          'icontains', 'short', 'firm_zacks_cut'),
('news', true, 'trading central',       'icontains', null,    'firm_tc_mentioned'),    -- presence-flag
('news', true, 'argus initiates',       'icontains', null,    'firm_argus_init'),
('news', true, 'jefferson research',    'icontains', null,    'firm_jefferson'),
('news', true, 'iss-eva',               'icontains', null,    'firm_iss');
```

The presence-only rules don't promote direction but DO promote the
article to "research-house attention" category — useful as a signal-quality
flag for thesis_agent's catalyst scoring.

## Free-tier alternatives that produce SIMILAR signals

These are publicly-available data sources at the same level of
quantitative sentiment that the named firms provide. Worth evaluating as
new ingest agents (each is a candidate for `agents/<name>_agent.py`
following the existing pattern):

| Source | What it provides | Free tier? | Effort to integrate |
|---|---|---|---|
| **Zacks Rank RSS** | Daily rank changes (1=strong buy → 5=strong sell) | Yes — free public RSS | LOW — new agent, RSS parser, same shape as news_agent |
| **Finviz Elite (free tier)** | Screener results, target prices | Free for basic screens | MEDIUM — HTML scraping (TOS-grey) |
| **TipRanks (limited free)** | Analyst consensus, target price aggregation | Limited free, mostly paid | MEDIUM |
| **StockTwits API** | Crowd sentiment per ticker | Free public API | LOW — new ingest agent |
| **Quiver Quantitative** | Congressional trades, lobbying, patents | **Free tier API (50 req/day)** | LOW — high-quality alt-data |
| **OpenInsider** | SEC Form 4 insider buys/sells parsed | Free | LOW — we already have `activist_insider_agent`; could augment |
| **WhaleWisdom** | 13F holdings changes | Free tier | LOW — overlaps existing `flows_agent` |
| **Benzinga RSS** | Catalyst-heavy headlines | Free RSS | LOW — drop into news_agent feed list |
| **Seeking Alpha Premium** | Already partially ingested | Free with ads | (already in use) |

**Highest-yield candidates** for a follow-up sprint (not this session):

1. **Zacks Rank RSS** — direct substitute for the missing "research firm
   consensus" signal. Daily rank changes are exactly what an alert
   subscriber would notice.
2. **Quiver Quantitative** — completely free tier with 50 req/day, gives
   us Congressional trading and lobbying data NOT available anywhere
   else cheap.
3. **StockTwits sentiment** — adds a crowd-sentiment dimension to the
   intelligence layer that complements the news classifier.

## Recommendation

In strict priority order, given solo / free-tier / loss-recovery framing:

1. **Now (this PR):** Just write this doc — no new ingest agents yet.
   The Layer 2 emit gate is broken (per
   `rereview-what-is-critical-golden-island.md` plan); adding more
   ingest sources without fixing the bottleneck downstream just adds
   work that gets dropped.
2. **After Layer 2 unblocks** (post-P1 instrumentation): add the
   research-firm keyword rules to `stock_keyword_rules`. Costs nothing,
   improves classifier recall on a category we DO see in news flow.
3. **Next sprint:** ship a `zacks_agent.py` that polls the free RSS and
   writes to `stock_normalized_events` with `event_type='analyst_rank_change'`.
   Follow `news_agent.py` pattern.
4. **Later (only if signal seems thin):** Quiver, StockTwits as
   separate agents.

**Specifically NOT recommended:**

- Trying to license Trading Central / Argus / Refinitiv on free tier —
  not viable, not worth time investigating.
- Scraping the broker-side embedded widgets — TOS violation + fragile.

## Cross-references

- Keyword DB (where the new firm rules would land): `agents/news_agent.py:108`
  (`stock_keyword_rules` table loader).
- Existing keyword audit: `docs/findings/2026-06-02_keyword-db-audit.md`.
- Existing similar agent pattern (RSS-driven ingest): `agents/news_agent.py`.
