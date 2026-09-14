# Pre-registration — `opt_pcs_v1`: single-name put credit spreads, forward, paper

**Status:** v1 DRAFT — operator sign-off pending. Drafted 2026-09-07 from
`docs/findings/2026-09-07_options-research.md` §4. Parameters below are FROZEN at the commit
that removes "DRAFT"; any later change = new version + new `forward_epoch`. That is the
pre-registration contract (same as `2026-08-02-preregistration-forward-provisional-long.md`).
**Scope guardrails:** PAPER ONLY (Alpaca paper account). No capital at any tier. Isolated from the
frozen equity experiments (read date 2026-10-30): different `experiment_id`, workflow, state dir,
store module, secrets; touches no Supabase table; never imports `scripts/forward_experiment.py`,
`scripts/paper_book.py`, or `agents/_experiment_store.py` (single-leg equity schema).

## Hypothesis (one-sided)

> Systematic 30–45 DTE **put credit spreads** on liquid single names, entered only when the
> name's 30-day implied vol exceeds its 20-day realized vol, earn a positive **net** return on
> capital-at-risk in excess of (i) the same-window underlying and (ii) same-window QQQ —
> forward, after commissions and half-spread slippage.

Two null arms exist to separate "the IV−RV filter adds something" from "any put-selling looks
fine in a rising tape." A clear negative after the minimum sample kills it.

## Universe (frozen at sign-off after ONE live re-screen at 10:30 ET)

Admission criteria, all must hold on the re-screen day: (1) in Cboe's top-40 by average daily
options volume OR in the repo's `core` / `ai_*` watchlists; (2) ≥7 expirations inside 60 days;
(3) spot ≥ $20; (4) at the expiry nearest 35 DTE, the ~7%-OTM put has half-spread ≤ 10% of mid
and total put OI at that expiry ≥ 500.

Friday-close (2026-09-04, after-hours quotes, upper-bound spreads) candidates that cleared — 32:
NVDA, TSLA, AAPL, AMZN, MSFT, META, GOOGL, AMD, NFLX, AVGO, COIN, MSTR, INTC, HOOD, MU, MRVL,
TSM, SMCI, DELL, ORCL, PLTR, CRWD, NOW, IREN, CRWV, VST, VRT, ANET, LITE, GLW, SNOW, NBIS.
Conditional (admit if the live re-screen brings them under 10%): COHR, CIEN, GEV, ASML.
Explicitly excluded (no weeklies or too wide): FN, NVT, MOD, TLN, NRG, CEG, ETN, PATH, AI, HPE, CRDO.
The final list is written into this file at sign-off and never refreshed (no survivorship drift).

## Strategy (ONE) — bull put vertical, defined risk

- **Expiry:** listed expiry nearest 35 DTE within [30, 45].
- **Short strike:** highest strike with |delta| ≤ 0.25 from the Alpaca 10:00 ET snapshot.
  Missing delta → skip that name-day, record `skip_no_greeks`.
- **Long strike:** listed strike closest to 2.5% of spot below the short strike (≥ 1 interval).
  Capital at risk = width×100 − net credit; if > $2,500 → `skip_width_cap`.
- **Entry:** 10:00 ET run. Model fill = short leg at mid − ½ spread, long leg at mid + ½ spread,
  from that run's snapshot. Never a backdated fill. Run > 90 min late → no entries (`skip_late_run`).
- **Exits** (evaluated at the 16:15 ET mark, executed next 10:00 ET run — no lookahead):
  50% of max profit, or 21 DTE, or spread mark ≥ 3× credit received. No holding to expiry.
  No rolling — a roll is a new trade that must be admitted on its own.
- **Assignment:** visible in Alpaca paper NTAs next day → close resulting stock + long put at the
  next 10:00 ET run at mid ∓ ½ spread; record `assignment_event`.
- **Caps per arm:** 1 open spread per ticker; 10 open; 5 new entries per day (treatment: highest
  IV−RV first; nulls: alphabetical — deterministic).

## Three arms (same trade, different entry days; separate ledgers, one experiment id)

| arm | entry rule |
|---|---|
| `T` | enter when ATM 30-day IV (chain) − 20-day realized vol (yfinance closes, annualized) > 0 |
| `N_random` | seeded RNG (seed = sha256 of this config) at T's realized entry rate over the prior 2 weeks |
| `N_monday` | every first session of the week, every eligible name, same caps |

**Filter verdict:** if `N_monday`'s mean net excess ≥ `T`'s at the read, the IV−RV gate is
declared dead regardless of T's sign.

## Sizing (paper, notional)

1 contract per spread; capital at risk ≤ $2,500 per trade; $25,000 book per arm (10 × $2,500).
Per-trade return on capital-at-risk is the graded quantity (return-edge test, not a capital
book — avoids capacity confounds).

## Costs (SINGLE LOCUS — inside `config_hash`)

Per contract per side: $0.65 commission + $0.05 clearing/regulatory = **$0.70**; a 2-leg spread
round trip = 4 × $0.70 = **$2.80**. Slippage: **half the quoted bid-ask per leg per side** from
the fill-run snapshot. No other cost line. Alpaca paper orders are placed as a **shadow
cross-check only** (their NBBO fills already embed the spread — never summed with our
slippage); `alpaca_fill − model_fill` per leg is a read-side diagnostic. If |mean divergence| >
25% of mean credit, the COST MODEL (not the verdict) is revised in v2.

## Benchmarks

Per trade: (1) same-window buy-and-hold of the underlying (entry-run open → exit-run open),
(2) same-window QQQ. `excess_i = net return on capital at risk − benchmark return`.
Reference only: Cboe PUT index over the window (free daily CSV).

## What is graded, daily

- **16:15 ET:** mark every open leg at mid (or last trade if one-sided; record which); per-leg IV,
  delta, bid, ask, timestamp; unrealized P&L; exit triggers.
- **10:00 ET:** execute pending exits, admit entries, write realized P&L + cost lines,
  assignment/expiry events. Both runs commit `state.json` + `metrics.json`. Every snapshot
  timestamp is stored so cron drift is visible, never silent.

## Go/no-go (per arm; read ONCE on the read date; paper only)

- **n** = distinct entry-date cohorts; a trade counts only when closed.
- **Tier ①:** ≥ 8 weekly cohorts AND ≥ 60 closed spreads AND ≥ 12 weeks. Then `continue` iff
  mean net excess ≥ 0 vs underlying AND vs QQQ, AND arm book max drawdown ≤ 20%, AND no single
  ticker > 50% of summed excess (report `mean_excess_ex_top_ticker`), AND **tail metric**: mean
  loss of the worst 5% of trades ≤ 6 × mean credit received.
- `fail` only on a CLEAR negative: mean excess vs underlying < **−1.0% per trade net**, or a
  drawdown breach. In [−1.0%, 0) → `inconclusive`, keep running.
- **Kill rule:** any `fail` → shelve; diagnose; no v2 without a new epoch.
- **NO real money at any tier.** Tier ③ is out of scope (existing standard).

## Dates

- Universe freeze + this doc committed without "DRAFT": first trading day after sign-off
  (earliest 2026-09-08). `forward_epoch` = first CI run on/after that commit.
- First fills: week of 2026-09-14. Last admissions: 2026-11-13 (every 21-DTE exit clears).
- **Fixed read date: 2026-12-18** (≥ 14 weeks after first fill). Deliberately after the equity
  read of 2026-10-30 so the two verdicts are read separately.

## What cannot be backtested for free, and the compensation

Historical option chains with quotes and greeks (needed to replay strike-by-delta and to cost
the spread) are paid everywhere. Alpaca's free option bars have no quotes. Compensation:
forward-only accrual on a longer clock; the PUT index as the long-history reference for the
mechanism; optionally a clearly-labelled plumbing check on DoltHub's 2019–Jun-2024 EOD chains
(strike selection / exit logic only — never an edge estimate).

## Failure modes specific to paper options — each recorded, none filtered

| failure mode | recorded as |
|---|---|
| fills at mid vs reality | model fill = mid ∓ ½ spread; `alpaca_fill` shadow; `spread_pct_at_fill` per leg |
| wide / stale / one-sided 10:00 quotes | `quote_age_s`; `skip_no_bid` if bid = 0 |
| early assignment | `assignment_event` (next-day NTA); forced close next run |
| dividends (paper does not simulate) | `ex_div_in_window` from yfinance calendar; stratified on read |
| earnings inside the DTE window | `earnings_in_window`; NOT a filter (that would be a second strategy); stratified; tail metric |
| IV crush / spike | `iv_entry`, `iv_exit`, `iv_max_mark` per leg |
| missing greeks on the indicative feed | `skip_no_greeks`; > 10% of eligible name-days → feed problem flagged in metrics |
| cron drift / late runs | snapshot timestamps; `skip_late_run`; never a backfilled fill |
| stop gapped through | `exit_reason`, `realized_loss_over_credit` |
| pin / expiry | avoided by the 21-DTE rule; `expiry_event` with $0.01-ITM auto-exercise modelled if reached |

## Isolation checklist

Own `experiment_id = opt_pcs_v1`; own workflow `options_experiment.yml` (own concurrency group;
crons 14:00 UTC and 20:15 UTC weekdays — watch DST in November); own Alpaca **paper** keys as
new GHA secrets; own state dir `paper_book/experiments/opt_pcs_v1/`; own store module. No read
of `stock_signal_candidates` in v1 (a pipeline-gated arm is a deferred v2). This is the
smallest honest experiment; it is not a strategy recommendation.

## Operator actions required before the first fill

1. Open an Alpaca **paper** account; confirm Level 3 shows in paper; create API key/secret.
2. Add `ALPACA_PAPER_KEY` / `ALPACA_PAPER_SECRET` as GitHub Actions secrets (never in the repo).
3. Approve or amend every parameter above; remove "DRAFT"; the commit timestamp is the proof.
4. Add `options_experiment` to the cron-job.org pinger list (the equity experiment just lost a
   night to GHA cron drift on 2026-09-05).
