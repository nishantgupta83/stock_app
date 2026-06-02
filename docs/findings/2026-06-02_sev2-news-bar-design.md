# Design: lower the bar for sev≥2 news on watchlisted tickers

**Date:** 2026-06-02
**Status:** Exploration — design + tradeoffs sketched, not implemented.

## The proposal

Today, a `news_article` event with `direction_prior='neutral'`
contributes **zero** points to `catalyst_score` regardless of context.
A neutral article on a watchlisted ticker is treated identically to a
neutral article on a random ticker.

The proposal: **let sev≥2 neutral news on a watchlisted ticker
contribute half-points** (e.g., 4 points instead of 0, where bullish
news contributes ~8). The intent is that *frequency of coverage on a
focus ticker* is itself signal — coverage frequency correlates with
something happening, even when the classifier can't tell what.

## Why this matters

Concrete failure mode (verified 6/2): "Nvidia, Qualcomm, Intel, Marvell
offer up positives at Computex: GF" was classified neutral by the
keyword DB → contributed zero to NVDA's score → no signal.
But NVDA *is* on the AI watchlist (`ai_compute`), and the article
*was* published by a serious source (Seeking Alpha), and it *did*
generate market reaction. Some signal got through to a human; none got
through to thesis_agent.

## Two distinct fixes for the same gap

| Fix | Approach | Captures |
|---|---|---|
| **Keyword DB audit** (see `2026-06-02_keyword-db-audit.md`) | Add ~80 catalysts to the classifier vocabulary | Articles with *recognizable* catalyst language |
| **Lower bar for sev≥2 watchlist news** (this doc) | Score neutral articles on focus tickers at half-points | Articles the classifier *can't* recognize but where coverage frequency on a focus ticker is itself signal |

These are **complementary, not alternatives**. Both fix different failure
modes:

- Keyword DB upgrade fixes **classifier recall** for catalyst-language
  articles (most market-moving news IS written with catalyst vocabulary).
- Lower-bar fix catches the **edge cases** where coverage is high-quality
  but the language is conversational ("AI is doing well", "the
  semiconductor cycle is turning") — language a keyword DB will never
  catch without a thousand entries.

## Implementation sketch

Code touch: `agents/thesis_agent.py::score_evidence` — the `news_article`
scoring branch.

```python
# Before (illustrative)
elif et == "news_article":
    if subtype == "long":
        add("news_bullish", 8, e["id"], headline)
    elif subtype == "short":
        add("news_bearish", -8, e["id"], headline)
    # neutral: nothing

# After
elif et == "news_article":
    if subtype == "long":
        add("news_bullish", 8, e["id"], headline)
    elif subtype == "short":
        add("news_bearish", -8, e["id"], headline)
    elif (sev >= 2
          and ticker in watchlist_focus_tickers
          and direction_inference != "neutral_from_low_signal"):
        add("news_watchlist_neutral", 4, e["id"], f"watchlist {headline}")
```

Where `watchlist_focus_tickers` is loaded once per run from the
existing `watchlist_map`.

## Why a half-point band, not full points

Three risks of giving neutral articles full credit:

1. **Distribution noise.** Articles like "YieldMax NVDA ETF announces
   weekly distribution of $0.2453" appear daily for NVDA — they're
   genuinely neutral, not a catalyst. Full points would have these
   "explain" NVDA score moves and degrade signal quality.

2. **Frequency exploits frequency.** Mega-caps get covered 10x more
   than mid-caps. Full-point neutral coverage would systematically
   over-weight mega-cap signals further than they already are.

3. **No direction.** Neutral has no implied direction. Adding half-points
   without specifying bull/bear adds **conviction** without **direction**
   — useful for "this is worth watching" alerts but not for setup
   construction.

Half-points means: enough to nudge a score over the WATCH threshold
when *combined* with another signal (e.g., a positive intraday move),
not enough to fire on coverage alone.

## The harder question — what's "watchlist_focus" exactly?

Today's watchlists (`stock_watchlists`):
- `core`, `context`, `ai_compute`, `ai_optical`, `ai_servers`,
  `ai_power`, `ai_software`, `ai_neocloud`, `institutions`, `mutual_funds`

For this proposal, **focus** means: tickers in
`core ∪ ai_compute ∪ ai_servers ∪ ai_software` — the names you actually
trade. Excludes `institutions`/`mutual_funds` (proxy holdings) and
`context` (macro). About 30-40 tickers.

If "focus" is too narrow, the band rarely fires. Too broad and it
becomes background noise. 30-40 names feels right for a $5K shadow
portfolio that opens 5 concurrent positions.

## Calibration plan (before flipping)

Same playbook as the sector multiplier:

1. Ship behind feature flag `WATCHLIST_NEUTRAL_SCORING_ENABLED`,
   default off.
2. Run for 2 weeks at flag-off, recording in
   `pulsecheck_news.classifier_neutrality` how many sev≥2 watchlist
   neutral articles fire.
3. Flip on. Compare downstream signal volume, accuracy of resulting
   paper trades (via `stock_event_paper_trades` for the new
   `news_watchlist_neutral` rule_key).
4. Decide: keep at half-points, raise to 6, or kill.

## What would change my mind

- If keyword DB audit alone reduces neutral share below 50% AND
  thesis_agent starts emitting at expected volume, this proposal is
  unnecessary. The keyword DB is the simpler, more direct fix.
- If after the keyword DB upgrade the neutral share is still 75%+ and
  signal volume is still low, this proposal becomes the right next step.

Order of operations matters: **keyword DB first, watchlist-neutral
band second**.

## Cross-references

- Score function: `agents/thesis_agent.py::score_evidence` (line 476+).
- Watchlist loader: `agents/thesis_agent.py::fetch_watchlist_map`.
- Keyword DB audit (the simpler fix): `docs/findings/2026-06-02_keyword-db-audit.md`.
- Free SLM (the more ambitious fix): `docs/findings/2026-06-02_slm-classifier-feasibility.md`.
