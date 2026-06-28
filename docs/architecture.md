# Architecture

One-page view of the `stock_app` pipeline: six isolated layers, the **learn → act**
feedback loop, and the **forward-edge validation** harness.

![stock_app architecture](architecture.png)

> Source: [`architecture.dot`](architecture.dot) — regenerate with
> `dot -Tpng -Gdpi=140 docs/architecture.dot -o docs/architecture.png`
> (or `-Tsvg` → [`architecture.svg`](architecture.svg)). Palette is the project pastel set
> (sky-blue / sage / coral / amber / slate — no purple).

## The pipeline (top to bottom)

Each layer reads only from the layers below it and writes only its own output table, so a
bug in one layer cannot corrupt the layers above.

| Layer | Does | Output |
|---|---|---|
| **1 · Ingest** | EDGAR / RSS / Truth Social / 13F / yfinance → normalized events | `stock_normalized_events` |
| **2 · Intelligence** | `thesis_agent` clusters events + scores them with the §17.7 100-point rubric + intelligence bonuses. *(This is the "reasoning" today — structured scoring, not an LLM.)* | `stock_signals` |
| **3 · Trade construction** | `trade_setup_agent` turns each signal into a tradable setup **or** records a `reason_to_skip` | `stock_trade_setups` |
| **4 · Risk / sizing** | `risk_agent` — Van Tharp sizing, drawdown circuit-breaker, daily budget (paper only) | `stock_risk_decisions` |
| **5 · Learning** | `event_paper_agent` opens paper trades hourly; `price_agent` grades outcomes every 2h → updates per-rule profit-factor / accuracy / n | `stock_rule_calibration` |
| **6 · Presentation** | `site_generator` → dashboard + Telegram | hub4apps.com |

## The learn → act loop (the dashed teal edges)

This is what makes it a *learning* system rather than a static scorer:

1. Events become **paper trades** (Layer 5).
2. `price_agent` **grades the outcomes** and updates each rule's calibrated
   profit-factor / accuracy / n.
3. That calibration **feeds back up**: it gates Layer 2 scoring and tells Layer 3 to
   **skip any rule whose PF < 1.0** ("no payoff edge").

So the system doesn't just record — it *changes its behavior from realized outcomes*.
The clearest proof: in June 2026 the grader was made honest (count the declared stop
instead of holding naked to the horizon), and the one rule that looked mature
(`8k_material_event::h30d`) **demoted itself** — PF 2.45 → 2.02, mature → not-mature —
and the pipeline stopped proposing it. It talked itself *down* rather than trade a fake edge.

## The maturity gate (paper → conviction)

The bottom of the diagram. The pipeline emits **WATCH / RESEARCH / AVOID_CHASE** (paper
only) until a rule's calibration crosses the gate (n ≥ 30, PF ≥ bar) **and holds
forward** — only then does it graduate to **BUY / SELL**. As of 2026-06, **0 rules are
mature** on honest evidence, so everything is paper. That is by design: the gate refuses
to license conviction it hasn't earned.

## Layer 5.5 · Forward-edge validation (added 2026-06)

Two **isolated, read-only** validators (they never write Supabase; they commit JSON to the
repo) answer the binding question — *does an edge exist forward?*

- **`paper_book`** — grades the *tradeable* setups forward as a $5k book vs a $5k QQQ
  buy-and-hold, with a staggered tier (continue / inconclusive / fail → edge → conviction).
  Currently starved (0 tradeable setups → honest `inconclusive`).
- **`paper_book_shadow`** — grades the *skipped* setups, per-setup and capacity-free,
  stratified by skip-reason (payoff / vocabulary / instrument) → tells you **which gate
  over-filters real edge**, and flags instrument-gate anomalies (e.g. CVX/Chevron flagged
  untradeable despite being liquid).

Design docs: [`design/2026-06-26-paper-book-forward-edge.md`](design/2026-06-26-paper-book-forward-edge.md),
[`design/2026-06-27-paper-book-shadow-skipped.md`](design/2026-06-27-paper-book-shadow-skipped.md).
