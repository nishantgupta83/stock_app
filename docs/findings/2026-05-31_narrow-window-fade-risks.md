# Narrow-window structural-fade candidates — why we're not hardcoding them

**Date:** 2026-05-31
**Status:** Deferred — the failure mode (recency vs. structural) is unfalsifiable on current data.
**Source:** Same 540d corpus as sector-multiplier work.

## What we observed

A handful of `(rule_key, ticker)` cells show very low accuracy with seemingly
adequate sample counts. The candidate for hardcoded fade is:

| rule_key | ticker | n | accuracy | mean realized | date range |
|---|---|---|---|---|---|
| `truth_social_post:djt_self:h15d` | DJT | 20 | **0.0%** | -9.24% | 2026-04-24 → 2026-05-11 |

A static blacklist would be very easy to ship:

```python
STRUCTURAL_FADE = {
    ("truth_social_post:djt_self:h15d", "DJT"): "0% acc h15d n=20",
}
```

…and skip emitting any signal whose `(rule_key, ticker)` is in that set.

## What it might mean

Two interpretations cover most of the probability mass:

1. **Structural** — DJT is, by design, the ticker most exposed to its
   own founder's posts. Any post directly referencing DJT operations,
   regulatory action, or the Trump Media business creates downside pressure
   regardless of post content. The market discounts the founder
   "validation" instinctively. Under this interpretation the pattern
   continues whenever djt_self events fire on DJT.

2. **Recency / regime** — the 3-week window (2026-04-24 to 2026-05-11)
   coincides with a specific cluster of DJT-related news (litigation,
   share-class disputes, refinancing). Once that resolves, posts no
   longer move the stock as reliably.

Both fit the data we have. We can't tell them apart from 20 observations
concentrated in 3 weeks.

## What happens if the window is too narrow

A static blacklist freezes today's pattern. If interpretation (2) is
correct and the pattern reverses:

- **False negatives accumulate silently.** The signal would have been
  right; we silenced it. There is no telemetry that catches "alert we
  *didn't* send was right."
- **The live calibration loop can't heal it.** `stock_rule_calibration`
  recomputes accuracy on actual paper trades. A blacklist that prevents
  emission also prevents new evidence. The cell is frozen at "bad" forever,
  because we stop feeding it.
- **Asymmetric discipline.** The project's central principle is that rules
  graduate (or get dampened) via the maturity gate over n>=30 trades. Adding
  a static fade bypasses that for negative signals while keeping it for
  positive ones. We'd be saying "discipline applies to graduations but
  not to demotions" — which makes the maturity gate weaker, not stronger.
- **Maintenance burden compounds.** Every future cell that looks bad over
  a narrow window becomes a candidate. The blacklist grows; nothing ages
  out automatically.

## What we shipped instead

The sector multiplier view (`stock_rule_sector_multiplier`) does provide
calibration-driven dampening for cells with n>=30 *over the full corpus*.
It will not catch the DJT case (n=20 is below the floor) but it does
catch the same intuition for well-evidenced cells without the recency
problem. See `sql/0032_rule_sector_multiplier_view.sql`.

If `(djt_self, DJT, h15d)` continues to underperform across more time and
crosses n>=30, the view picks it up automatically and the dampening
applies. No code change required.

## What would change our mind

Promote to a hardcoded fade when **either**:

- The cell crosses n>=30 across at least 3 distinct calendar months
  (not 3 weeks). At that point we have enough evidence that the pattern
  is not a single regime artifact.
- A separate mechanism — operator intuition, a known one-off event —
  argues the cell is structurally biased and the cost of waiting for
  n>=30 is unacceptable. In that case the operator adds it with a
  documented rationale and an explicit review date.

## Cross-references

- View: `sql/0032_rule_sector_multiplier_view.sql` (organic dampener, no recency risk).
- Memory: `feedback_tier_gates.md` — "any new maturity/tier gate must combine
  accuracy + n with profit_factor/mean_realized_pct; never accuracy-only."
- Re-verification query: `select ticker, rule_key, count(*), avg(case when correct then 1.0 else 0.0 end), min(entry_at), max(entry_at) from stock_event_paper_trades where status='closed' group by 1, 2 having count(*) >= 15;`
