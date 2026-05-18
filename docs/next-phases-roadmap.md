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

## Phase 11.5 — Two-tier maturity + learning-loop precision

**Trigger:** 2026-05-12 — `status.json` v1.1 already exposes both tiers (`production` = 0.90 acc, `training` = 0.70 acc). Wiring the rest happens here.
**Effort:** ~4–6 hours across 3 incremental commits.
**Risk:** low — purely additive (production gate untouched).

### What's already in place (2026-05-12)
- `status.json` surfaces `n_mature_production` and `n_mature_training` separately, with their respective vocabularies and gates.
- `is_mature_training` derived in `site_generator.py` from the same numbers `price_agent` uses for `is_mature`; both consumers (dashboard + digest routines) read the same definitions.
- First training-tier graduate: `8k_material_event::h7d` (n=115, acc=71.3%).

### Phase 11.5a — Wire training-tier emission into thesis_agent
- Add `cluster_has_training_mature_rule()` mirroring `cluster_has_mature_rule()` in `agents/thesis_agent.py`.
- Extend `action_for()` to return `PROVISIONAL_LONG` / `PROVISIONAL_SHORT` when the cluster has only training-mature rules (not production-mature).
- Telegram dispatcher: prepend `[TRAINING]` to subject line for these signals so the user can never confuse them with production BUY/SELL.
- Open question: do training-tier signals count against `MAX_ALERTS_PER_DAY = 5`? Default: yes, but lift the daily cap to 8 to make room.

### Phase 11.5b — Platt scaling on closed_30d
- Replace raw frequency `accuracy = n_correct / n_observations` with logistic-regressed probability calibrated on `stock_forecast_audit` rows.
- Output `accuracy_calibrated` alongside the raw value. Gate uses the calibrated version.
- Reason: at low n the raw rate is noisy; Platt scaling pulls toward the prior, dampening false-positive maturity.

### Phase 11.5c — Decay-weighted calibration
- Add `time_decay_weight = 0.5 ** (days_since_observation / 90)` to each observation when computing accuracy.
- Reason: rules can decay (alpha decay). A rule that worked 6 months ago shouldn't dominate today's accuracy because it had 50 observations then.
- Recompute matured_at when decay pushes accuracy back below either gate.

### Acceptance criteria
- After 11.5a: at least one `[TRAINING] PROVISIONAL_LONG` alert lands in Telegram for `8k_material_event::h7d`.
- After 11.5b: `accuracy_calibrated` field visible in `status.json` and dashboard calibration page.
- After 11.5c: `matured_at` can NULL-out for a previously-mature rule that has decayed.

### Out of scope (this phase)
- Production-tier vocabulary changes — BUY/SELL emission still gated on 0.90 + n≥30.
- Auto-tuning the training threshold — kept manual at 0.70 until we have ≥30 closed training-tier trades to evaluate.

---

---

## Phase 11.6 — Trading-pipeline backlog (2026-05-18: STAGES 1-6 SHIPPED)

Backlog from the 2026-05-13 validation review + the 4th external review's
sequencing. Each stage shipped as one or more atomic commits with a quality gate.

| Stage | Items | Status | Commit(s) |
|---|---|---|---|
| 1A  | Dedupe `workflow_*` rows in dashboard health | ✅ | `2ec0905` |
| 1A.1 | Fix 24h staleness cap that misflagged weekly agents | ✅ | `0e90534` |
| 1B  | `parent_run_id` / `run_type` / `stage` on `stock_job_runs` | ✅ | `ee3a08d` + sql/0022 |
| 2   | Structured `score_breakdown` + `valid_until` per signal | ✅ | `2931985` + sql/0023 |
| 3   | Stale-price gate in `event_paper_agent` | ✅ | `d4627b3` (+ hotfix `6e1c88f`) |
| 4   | MFE/MAE + payoff metrics + daily-HL stop/target audit | ✅ | `864a949` + sql/0024 |
| 5   | **Layer 3 — `trade_setup_agent`** + `stock_trade_setups` | ✅ | `a71c372` + sql/0025 |
| 6   | **Layer 4 — `risk_agent`** + `stock_risk_decisions` | ✅ | `7c218f5` + sql/0026 |
| 7   | Documentation update (this doc + README + CLAUDE.md) | ✅ | this commit |

### Still deferred (NOT shipped — explicitly out of scope)
- Stage 9 / Item #10: Alpaca broker adapter. Gated on a production-mature
  rule existing — none today. The current `risk_agent` produces paper-only
  decisions; adding a broker would require a separate execution adapter
  that translates `size_pct_portfolio` into qty.
- True intraday-bar audit (1-min / 5-min bars). The Stage 4 daily-HL
  approximation covers the gap; a real intraday path requires new
  ingestion + storage.
- Multi-factor regime detection beyond the single VIX > 25 check.
- Portfolio correlation filter beyond the simple `MAX_SAME_RULE_OPEN`
  concentration cap.

### Outstanding gaps surfaced during Stage 6 audit (2026-05-18)
- `intraday_alert_agent` running at ~9% of scheduled cadence due to GH
  Actions cron drift. Not blocking; the workflow_run chain mitigates for
  most downstream consumers.
- ~~`thesis_agent` emitted 0 signals after May 15.~~ **FIXED** in `a2e71e8`:
  root cause was the cluster rule requiring ≥2 distinct source agents, but
  new domain agents (biotech / FDA / DoD / nuclear / insider) emit
  inherently single-source events. Extended single-source exceptions for
  these binary-catalyst event types. Verified: `in=24, out=3` after fix vs
  `in=24, out=0` before.
- `8k_material_event::h7d` regressed from 71.3% acc (n=115) on May 12 to
  65.1% (n=232) by May 18. Still profit_factor 9.15 — payoff edge intact.
  Below training-tier gate now but worth watching as more obs accumulate.

---

## Phase 11.7 — Strategic-feedback improvements (2026-05-18, shipped same day)

Direct response to the external "solo algorithmic trading" review (the
"Cohen/Lou edge" + "out-of-sample validation" + "small-cap focus" guidance).
Each shipped as an atomic commit with a verification:

| Item | Status | Commit |
|---|---|---|
| Cluster-rule fix for domain binary catalysts (unsticks thesis_agent) | ✅ | `a2e71e8` |
| `is_near_52w_low()` + severity escalation for insider cluster buys | ✅ | `0a9a718` |
| Small-cap insider watchlist (8 starter names: HMST, SAVA, AGX, BJRI, AROC, KRYS, BOOT, IMVT) | ✅ | sql/0027 + `af9681f` |
| OOS train/test split + drift verdict in backtester | ✅ | `af9681f` |

### Next from the strategic-feedback list (NOT shipped — future cycles)
- **Replace yfinance fallback on the decision path** with a primary
  point-in-time data source (the feedback recommended EODHD). Today's
  fallback is informational; would matter if we ever connect a broker.
- **Multi-factor regime detection** — today's regime layer is just
  `VIX > 25 = risk_off`. Strategic feedback calls for liquidity / rates /
  vol / sector-momentum regimes.
- **Live LLM-classified events with budget guardrail** — feedback's
  "degradation ladder" pattern (80% alert / 90% downgrade Opus→Haiku /
  100% hard stop). Not applicable today since the pipeline uses no LLM
  agents.

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
