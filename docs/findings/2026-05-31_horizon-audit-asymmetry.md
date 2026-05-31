# Horizon / audit asymmetry — 8K events pay off at h15d, but live signals are audited at h1d

**Date:** 2026-05-31
**Status:** Deferred — needs audit-window refactor, not a config change.
**Source:** 540d corpus analysis (7,340 closed paper trades), re-verified post-730d backfill (8,736 closed).

## What we observed

On the same ticker, same event type, the `correct` rate diverges sharply by holding horizon.
`stock_event_paper_trades`, sample cells (n shown):

| ticker | rule_key | n | accuracy | mean realized | profit_factor |
|---|---|---|---|---|---|
| AAPL | `8k_material_event::h1d` | 97 | **6.2%** | -1.07% | 0.12 |
| AAPL | `8k_material_event::h15d` | 97 | **86.6%** | +4.29% | 5.89 |
| V | `8k_material_event::h1d` | 137 | **14.6%** | -0.37% | 0.30 |
| V | `8k_material_event::h15d` | 137 | **86.9%** | +0.83% | 3.23 |
| TSLA | `8k_material_event::h1d` | 80 | 85.0% | +0.10% | 1.14 |
| TSLA | `8k_material_event::h15d` | 80 | 87.5% | +4.35% | 4.98 |

Aggregate by horizon (all tickers, 540d):

| rule_key | n | accuracy | profit_factor | tier |
|---|---|---|---|---|
| `8k_material_event::h1d` | 1,166 | 45.3% | 0.73 | child |
| `8k_material_event::h7d` | 946 | 52.0% | 2.18 | child |
| `8k_material_event::h15d` | 938 | 70.7% | 3.08 | **teen** (the only teen-tier rule in the corpus) |
| `8k_material_event::h30d` | 520 | 54.0% | 1.77 | child |

## What it might mean

8K filings tend to be either (a) routine governance / dividend declarations with no
fundamental impact, or (b) real material events whose price impact takes days to
diffuse as analysts re-rate. The h1d window captures intraday noise; the h15d window
captures the actual re-rating. The current pipeline scores both identically because
the rule_key the calibration loop trusts is the same as the rule_key the live audit
produces — and the live audit runs at h1d only.

This is the dominant explanation for why `8k_material_event` cannot graduate above
`child` tier at h1d: at that horizon the rule isn't actually positive expectancy.
It is positive expectancy at h15d, but no live alert ever gets credit for that
because no signal is held that long.

## Why we're not acting now

The naive fix — change `thesis_agent.horizon_for()` to return `"15d"` for 8K
events — is **cosmetic**. It would relabel `horizon_days=15` in the signal
payload, but `price_agent` would still audit at h1d (line: every live signal
uses a 1d paper-trading horizon until intraday prices are added). The
calibration loop would still see h1d outcomes. Nothing about scoring would
actually improve.

The real fix touches multiple layers:

1. **`price_agent`**: needs to record outcomes at multiple horizons per signal,
   not just `exit_at = entry + horizon_days` (currently single-horizon close).
2. **`stock_forecast_audit`** and **`stock_signals.status_v2` lifecycle**: a
   signal at h15d can't be closed at h1d. Today's `sent` → `audited` flow
   assumes one outcome.
3. **`event_paper_agent`**: already opens 4 paper trades per event (1, 7, 15, 30) —
   that's the right shape, but the live signal -> live audit loop needs the
   same shape.
4. **Dashboard / alert UX**: a 15d hold is a different mental model than the
   current "watch / research / avoid_chase" cards which feel intraday.

Each is independently scoped. Together, this is a multi-PR refactor.

## What would change our mind

Promote this to an actionable change when **any** of the following holds:

- We're already extending `price_agent` to support intraday bars (the design
  doc that referenced "until intraday prices are added"). That refactor's
  scope is similar; folding multi-horizon audit in is cheap then.
- An out-of-sample test shows the h15d effect is regime-stable (it currently
  hangs on POST-2024-11 data only; PRE has n=161 for `8k_material_event::h15d`
  with acc 60.9% — close to but below the maturity gate at h15d).
- The sector-aware multiplier (`stock_rule_sector_multiplier` view) is shown
  to fix this gap implicitly without horizon changes — possible if the cells
  that fire 8K alerts cluster in sectors where h1d performance is actually
  positive.

## Cross-references

- View: `sql/0032_rule_sector_multiplier_view.sql` (sector multiplier — orthogonal but related).
- Feature flag: `SECTOR_CALIB_MULT_ENABLED` in `agents/thesis_agent.py`.
- Live horizon stub: `agents/thesis_agent.py::horizon_for` (returns `"1d"` for all events).
