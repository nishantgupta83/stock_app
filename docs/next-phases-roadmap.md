# Next-Phases Roadmap

Sequencing chosen 2026-05-12: **Path C (tightening)** ✅ done → **Path B (dashboard v2)** during the 2–3 week maturity-data accumulation window → **Path A (trading-grade)** as the long horizon.

---

## Phase 11 — Dashboard v2 (Path B)

**Trigger:** start now in parallel with the calibration loop accumulating mature-rule observations.
**Effort:** ~3 hours (one focused session)
**Risk:** low (visual change only; no agent logic touched)

### Scope

Port the `trader's almanac` aesthetic from `docs/dashboard-preview.html` into the live site, replacing the current dashboard pages.

| Page | Today | After Phase 11 |
|---|---|---|
| Dashboard | Bare stats + tables | Editorial-grade hero + sector heatmap + wire feed |
| Signals | Plain table | Same + ticker direction icons + maturity badges |
| Events | Plain table | Sector-colored event_type column |
| Calibration | List | Per-rule × per-horizon accuracy grid |
| Paper Trades | Open trades only | Open + closed split, P&L heatmap |

### Files to touch
- `templates/dashboard-v2.html.j2` (new) — port from `docs/dashboard-preview.html`
- `templates/_layout.html.j2` — update masthead + footer to almanac style
- `templates/calibration.html.j2` — convert to per-rule × horizon heatmap
- `templates/vendor/` — download Newsreader + JetBrains Mono webfonts to bypass Hostinger CSP
- `agents/site_generator.py` — add `sector_rotation_data()` aggregating last-24h events per `ai_*` watchlist, pass to dashboard template

### Acceptance criteria
- Dashboard at `hub4apps.com/stock_app/` matches the preview aesthetic
- Sector heatmap renders all 6 AI sub-watchlists (compute/optical/servers/power/software/neocloud) plus the 6 new domains (macro/activist/defense/biotech/energy/consumer)
- Mobile layout works on iPhone (one-column collapse)
- No external CDN fetches (Hostinger CSP-compliant)

### Out of scope
- Real-time WebSocket updates — static HTML refresh every 15 min is sufficient
- User auth / personalization — single private dashboard

---

## Phase 12 — Trading-Grade Additions (Path A)

**Trigger:** when ≥3 rules have matured (≥30 obs, ≥90% accuracy) — expected 4–8 weeks out.
**Effort:** ~10–15 hours total across 3 new agents.
**Risk:** medium — adds capital-allocation logic that needs careful validation.

This is the transition from **intelligence pipeline** (signals + paper trades) to **trading pipeline** (positions + execution + risk controls).

### Three new agents

#### 12.1 `risk_overlay_agent` — sits between thesis_agent and event_paper_agent
**Purpose:** veto / size-down signals based on portfolio-level risk.

| Check | Action |
|---|---|
| Sector concentration | If `ai_compute` already has 5+ open trades, downsize new ones in that watchlist |
| Beta clustering | Pre-compute βs to SPY; veto a new long if portfolio aggregate β > 1.5 |
| VaR cap | Sum of `entry_price × max_loss_pct` across open trades < portfolio_size × 0.2 |
| Position sizing | Replace fixed 5% target with Kelly: `f* = (p×b − q) / b` where p=rule accuracy, b=avg_win/avg_loss, q=1−p |

**Files:** `agents/risk_overlay_agent.py`, schema: `stock_portfolio_state` table (current open positions, βs, sector exposures).

#### 12.2 `liquidity_gatekeeper` — filters small/illiquid signals
**Purpose:** prevent the pipeline from generating signals on names where your own theoretical $5K position would move the market.

| Check | Action |
|---|---|
| 30-day avg daily volume | < $5M traded/day → mark signal as `paper_only` |
| Bid-ask spread proxy | yfinance high/low > 1% spread → flag thinly traded |
| Pre/post-market | Skip signals that fire during illiquid hours |
| Market cap floor | Require > $1B market cap unless `activist_initial_position` |

**Files:** `agents/liquidity_gatekeeper.py`, hooks into `event_paper_agent` pre-insert.

#### 12.3 `alpha_decay_monitor` — measures how late we got the signal
**Purpose:** track post-event drift. If `LITE +17%` fires the moment LITE is +14%, alpha decay = 80%. Tells us which agents catch signals while there's still juice vs. ones that arrive after the move.

| Metric | Per-rule output |
|---|---|
| Time-to-fire | seconds between `event_at` and `created_at` |
| Pre-fire move | % move of the underlying in the 60 min before signal |
| Post-fire move | % move in the 24h after signal |
| Decay coefficient | post-fire / (pre-fire + post-fire) — closer to 1 = caught early |

**Files:** `agents/alpha_decay_monitor.py`, schema: `stock_alpha_decay` table.

### Integration with existing pipeline

```
[CURRENT] event → thesis → event_paper → price (close) → calibration
                    ↓
                Telegram

[PHASE 12] event → thesis → ┌─ risk_overlay (veto/size) ─┐
                            ↓                            ↓
                       liquidity_gatekeeper → event_paper → price (close)
                            ↓                            ↓
                        Telegram (only liquid +     calibration + alpha_decay
                        sized-up signals)
```

### Out of scope (deliberately)

- **Real broker execution.** Pipeline stays paper-only — moving to Alpaca/IBKR API is a separate "Phase 13" decision that requires real-money risk acceptance.
- **Multi-asset support.** No options, futures, crypto-perp logic yet. Keep equities-only for now.
- **Tax / cost-basis tracking.** Out of scope until real execution.

---

## Phase 13+ — Future considerations (not committed)

- **Real-time WebSocket dashboard** — replaces 15-min Hostinger refresh with live updates. Requires moving off static hosting.
- **Multi-user / SaaS** — currently private/educational; commercializing requires auth, billing, compliance. Significantly different product.
- **LLM-assisted thesis generation** — feed signal context to a Haiku-class model for plain-English summaries in Telegram alerts. Cost-controlled via DB-cached prompt embeddings.
- **Options overlay** — when liquidity_gatekeeper says a stock is too thin, see if an options-based proxy (LEAP, vertical spread) is liquid enough.
- **Crypto perpetual futures** — extend `crypto_macro_agent` from BTC/ETH spot to deribit/binance funding rates + open interest.

---

## How to revisit this doc

This roadmap is **living**. When starting Phase 11/12 work, update the relevant section with: actual scope diffs from this plan, blockers encountered, what stayed in spec vs. what shifted. The Git history of this file IS the project journal.
