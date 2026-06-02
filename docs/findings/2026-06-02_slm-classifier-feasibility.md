# Feasibility: free SLM news classifier vs. keyword DB

**Date:** 2026-06-02
**Status:** Exploration — feasibility + cost sketch, no commitment.

## Why ask

Keyword DB expansion (see `2026-06-02_keyword-db-audit.md`) caps out
at maybe 60% recall — there's always vocabulary the list doesn't
catch ("AI is doing well at semis", "the cycle is turning"). A small
language model could classify direction *semantically* and would
generalize without needing dictionary maintenance.

Constraints (from project memory + CLAUDE.md):
- **Free tier infra only.** No paid OpenAI / Anthropic API.
- **Solo dev.** No ML ops appetite. The model has to be runnable as
  part of the existing GHA + Supabase pipeline.
- **Latency tolerance** is forgiving — news classification can be
  asynchronous; a 2-min delay is fine.

## Three candidate paths

### A. Run a small model inside `news_agent` GHA job

Use a distilled HuggingFace classifier (e.g.,
`distilbert-base-uncased-finetuned-sst-2-english` or
`ProsusAI/finbert` — financial sentiment, ~250MB).

- **Inference cost:** ~1-2s per article on a free GHA runner CPU.
- **Volume:** ~150-300 articles/day → 5-10 min total per run.
- **Memory:** ~1-2 GB. Free runner has 7 GB — fits.
- **Setup:** `pip install transformers torch` adds ~500 MB to the
  cache. Slow first run, cached after.
- **Limitations:** General-purpose sentiment. Finbert is the
  closest-purpose model but trained on 10-K filings, not news headlines.

### B. Use HuggingFace Inference Endpoints (free tier)

HF offers a free Serverless Inference API with ~3,000 requests/month
limit. At ~200 articles/day that's 6,000/month — over the limit.

- **Cost:** Free up to 3K req/month; $0.06 per 1K above.
- **Latency:** ~500-1500ms per request.
- **Verdict:** Volume exceeds free tier. Not viable as the sole path.

### C. Run a HF Space as a microservice

Deploy a free HF Space running the classifier; `news_agent` POSTs
articles for classification.

- **Cost:** Free tier (2 vCPU, 16 GB) gives ~5-10 RPS.
- **Latency:** ~200-500ms per request.
- **Always-on?** Free Spaces sleep after 48h of inactivity but auto-wake.
- **Pros:** Decouples classifier from GHA cron. Could serve multiple
  agents.
- **Cons:** Extra deploy + monitoring surface (a new pulsecheck
  candidate). Auth needed if non-public.
- **Verdict:** Viable. Adds a small Spaces deploy + a new pulsecheck
  (`pulsecheck_classifier_service`).

## Recommended starting point: Path A with finbert

Direct integration, no new infra surface:

1. `pip install transformers torch` added to `requirements.txt` (or
   only to `news_agent.yml` if size is a concern).
2. Lazy-load the model on first article per run.
3. Run classifier alongside existing keyword rules — store *both*
   results. The keyword DB stays the primary classifier; the SLM
   result is recorded in `payload.slm_direction` for offline comparison.
4. After a calibration window (say 1000 classifications), compare
   SLM vs keyword DB accuracy against actual market reaction (paper
   trade outcomes). Choose which to make canonical.

### Concrete code shape

```python
# agents/_slm_classifier.py
from functools import lru_cache

@lru_cache(maxsize=1)
def _pipeline():
    from transformers import pipeline
    return pipeline("text-classification",
                    model="ProsusAI/finbert",
                    truncation=True, max_length=128)

def classify_direction(headline: str) -> tuple[str, float]:
    """Return ('positive'|'negative'|'neutral', confidence)."""
    if not headline:
        return ("neutral", 0.0)
    out = _pipeline()(headline)[0]
    label_map = {"positive": "long", "negative": "short", "neutral": "neutral"}
    return (label_map.get(out["label"].lower(), "neutral"), float(out["score"]))
```

In `news_agent.py`, after `load_rules()` classifies, also call this and
attach the result to the event payload. No behavior change yet.

## Honest tradeoffs in your project's frame

| Axis | Keyword DB | SLM (path A) |
|---|---|---|
| Setup cost | Insert rows into Supabase | +500MB GHA cache, +5-10 min/run |
| Maintenance | Add keywords as new catalysts emerge | Self-generalizes |
| Recall | ~60% (estimated, after expansion) | ~80-85% (typical for finbert on financial news) |
| Precision | High (controlled vocabulary) | ~75-80% (general sentiment models drift on "neutral but bullish" framings) |
| Reproducibility | Identical results for same input | Identical (model is deterministic) |
| Free-tier safety | Trivial — text in DB | Eats more GHA minutes |
| Reversibility | Drop rows | Delete imports + uninstall |
| Audit trail | Every keyword rule has a label | Black-box (per-headline confidence is the only handle) |

## What would change my mind on which path to pursue

- Pursue **SLM first** if keyword DB additions (the audit doc's ~80
  proposed terms) leave neutral share above 70% after a 2-week trial.
- Pursue **keyword DB first** if the audit's additions hit 50% neutral
  share. At that level, the marginal recall improvement from SLM may
  not be worth the operational complexity.
- **Reject both** if it turns out the entire bottleneck was the cap
  budget (already fixed 6/2) AND `pulsecheck_news.classifier_neutrality`
  drops below 60% naturally as news volume diversifies.

The data tells the order. Don't pre-commit.

## What this would NOT solve

- **Direction is not catalysts.** SLM tells you "is this positive or
  negative?", not "is this a catalyst worth waking up for?" A neutral
  headline announcing a routine dividend is correctly neutral but
  still not catalyst-worthy.
- **Severity is orthogonal.** SLM gives you direction confidence;
  thesis_agent still needs severity to decide alert worthiness.

The keyword DB's `causal_keywords` set in `_catalyst_policy.py` is
actually a *separate* concern — "is this a catalyst class at all" —
and SLM doesn't replace that path. Both layers remain.

## Cross-references

- Current keyword DB: `stock_keyword_rules` table; loader at
  `agents/news_agent.py:102`.
- Causal keywords (promotion path): `agents/_catalyst_policy.py:105`.
- Pulsecheck: `pulsecheck_news.classifier_neutrality` will be the
  primary metric for deciding "is the current classifier good enough?"
