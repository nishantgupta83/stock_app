# Options sub-project — primer, evidence, infrastructure, experiment design, study plan

**Date:** 2026-09-07 · **Status:** Research + design (deferred-action). No code, nothing touched in the frozen equity experiments (`fwd_prov_long_h1d/h7d`, read 2026-10-30).
**Provenance:** produced by an independent research agent from primary sources (OCC/Cboe/FINRA/SEC/IRS, broker docs, peer-reviewed papers); URLs inline; the CONFIDENCE list at the end names everything it could not verify. Two headline numbers were spot-checked by the agent against the source PDFs (Cboe PUT factsheet as of 2026-07-31; Alpaca Level-3-in-paper blog, 2025-02-25). Relayed without editorial changes to the numbers.
**Operator's ask:** "So many people sell options. A sub-project on ~34 liquid names (TSLA, NVDA, AVGO, mega-cap tech, AI infrastructure), paper-traded, validated every day; learn options from scratch."

---

## 1. Options primer

**Contract.** One standard equity option = the right (holder) / obligation (writer) on **100 shares** (OCC Options Disclosure Document: https://www.theocc.com/company-information/documents-and-archives/options-disclosure-document). A **call** is the right to buy at the strike; a **put** the right to sell. **Long** = you paid premium and hold the right; **short** = you received premium and carry the obligation. Everything marketed as "income" from options is the short side.

**Premium = intrinsic + extrinsic.** Intrinsic = value if exercised now (max(0, S−K) call; max(0, K−S) put). Extrinsic ("time value") is the rest. It decays to zero at expiry; that decay is what a seller collects if nothing else happens. Moneyness: ITM (intrinsic > 0), ATM, OTM. A short "0.25-delta" put is OTM with roughly a 25% model-implied chance of finishing ITM.

**Expiration, exercise, assignment.** US equity options are **American-style** (exercisable any day); most index options (SPX) are European (OIC: https://www.optionseducation.org/referencelibrary/faq/options-exercise). At expiry OCC auto-exercises anything **$0.01 or more ITM** (Cboe RG08-073). OCC assigns clearing firms **randomly**; firms allocate to customers randomly or FIFO (OIC: https://www.optionseducation.org/referencelibrary/faq/options-assignment). **Early assignment** is mostly a dividend phenomenon (deep-ITM short calls the day before ex-div; short puts just after, when deep ITM with little extrinsic left). For a put-seller on mega-cap tech the practical trigger is "deep ITM + near expiry".

**The Greeks.** *Delta*: P&L per $1 move; ≈ probability of finishing ITM; a short 0.25Δ put behaves like ~25 long shares. *Gamma*: how fast delta changes — sellers are short gamma, so the position gets *more* long as the stock falls. *Theta*: daily decay of extrinsic — positive for sellers, the only thing they are paid. *Vega*: P&L per 1-point IV change — sellers are short vega, so a vol spike hurts before the stock moves. *Rho*: negligible at 30–45 DTE.

**Implied volatility, IV rank/percentile.** IV = the volatility that makes the model price equal the market price. IV rank = (IV − 52w low)/(52w high − low); IV percentile = share of the last 252 days with IV below today (https://www.tastylive.com/concepts-strategies/implied-volatility-rank-percentile). Both need a year of IV history per name, which is not free (§3).

**The volatility risk premium — why sellers are paid, and for what.** IV has on average exceeded subsequently realized vol: 1990–2018 average VIX 19.3 vs realized 15.1 (~4.2 points; Cboe/Bondarenko 2019: https://www.cboe.com/insights/posts/white-paper-shows-volatility-risk-premium-facilitated-higher-risk-adjusted-returns-for-put-index/); Israelov & Nielsen: +3.4 points, positive 88% of months, 1990–2014 (https://www.aqr.com/-/media/AQR/Documents/Journal-Articles/JPM-Still-Not-Cheap.pdf). Sellers are paid this gap *as insurance premium for absorbing crashes*: many small gains, rare very large losses. Theta is the actuarial fee for being short the left tail.

**Buying power / margin** (Cboe Margin Manual: https://cdn.cboe.com/resources/membership/Margin_Manual.pdf). Cash-secured put: full strike × 100 in cash; max loss = strike×100 − premium. Naked put (margin): premium + 20% of underlying − OTM amount, min premium + 10% of strike. Covered call: long 100 shares + short call. **Defined-risk vertical spread**: requirement = width×100 − credit = exact max loss — the reason the design below uses it.

**Approval levels.** FINRA Rule 2360(b)(16) requires written suitability approval (https://www.finra.org/rules-guidance/rulebooks/finra-rules/2360). Schwab L0–L3, Fidelity L1–L5, Alpaca L1 (covered calls + CSPs) / L2 (long options) / L3 (multi-leg) (https://docs.alpaca.markets/docs/options-trading).

**Pattern Day Trader rule — changed this year.** FINRA Notice 26-10 replaces the day-trade-count PDT designation and the $25,000 minimum with intraday-margin standards, effective **June 4, 2026**, broker phase-in allowed until **October 20, 2027** (https://www.finra.org/rules-guidance/notices/26-10). Irrelevant to a paper book; relevant because most online PDT advice is now stale.

**Costs.** Commissions: Schwab/Fidelity $0.65/contract; tastytrade $1.00 open / $0 close, capped $10/leg; Robinhood $0 + pass-throughs. Pass-throughs 2026: OCC $0.025/contract; Cboe ORF $0.01248/contract from 2026-07-01; SEC §31 $20.60/$1M on sales; FINRA TAF $0.00329/contract. **The dominant cost is the bid-ask spread**: retail-favored short-dated options carry ~12.6% quoted / 6.6% effective spreads (Bryzgalova, Pavlova & Sikorskaya, JF 2023); the Friday-close screen (§4) shows 1.4%–7% half-spreads even on mega-caps at 35 DTE.

**Tax** (IRS Pub 550, 2025 ed.: https://www.irs.gov/pub/irs-pdf/p550.pdf). Wash-sale rules **do** apply to options. **Section 1256 (60/40) does NOT apply to single-stock or ETF options** — only broad-based index options (SPX, XSP). Assigned short put: premium reduces stock basis; expired short option: short-term gain.

## 2. What "everyone selling options" actually earns

**(a) Cboe benchmark indices vs S&P 500, Jun 1986–Dec 2018** (Wilshire 2019; Bondarenko/Cboe 2019):

| Index (SPX) | Ann. return | Ann. stdev | Max DD | Sharpe | Beta |
|---|---|---|---|---|---|
| PUT (ATM cash-secured put, monthly) | 9.54% | 9.9% | −35.5% | 0.64 | 0.47 |
| BXM (ATM covered call) | 8.50% | 10.6% | −35.8% | 0.51 | 0.55 |
| BXMD (30Δ covered call) | 10.22% | 12.8% | −42.7% | 0.55 | 0.77 |
| S&P 500 TR | 9.80% | 14.9% | −50.9% | 0.45 | 1.00 |

Current PUT factsheet (as of 2026-07-31, verified): since Jan 2007 **7.1% / 10.8% vol / −32.7% DD / Sharpe 0.51** vs S&P TR **10.9% / 15.5% / −50.9% / 0.61**. Calendar years PUT vs S&P: 2020 +2.1 vs +18.4; 2022 −7.7 vs −18.1; 2024 +17.8 vs +25.0; 2025 +9.2 vs +17.9. Weekly put-write (WPUT) 2006–2018: 4.51%/yr, −24.2% DD — worse than monthly despite collecting 37%/yr gross premium vs PUT's 22%/yr.
*So what:* gross premium (22–37%/yr) is not return (5–9%/yr). Put-writing is an equity-like return with ~0.5 beta, ~2/3 the vol, a fatter left tail (PUT skew −2.1, kurtosis 9.7). Its Sharpe advantage is a 1986–2008 phenomenon; since 2007 the S&P's is higher.

**(b) The literature.** Bondarenko (2003/2014): 1987–2000 one-month ATM SPX puts returned ~−39%/month to buyers; the premium is real, but naked puts are "very risky". Israelov & Nielsen, *Covered Call Strategies: One Fact and Eight Myths* (FAJ 2014, https://images.aqr.com/-/media/AQR/Documents/Insights/Journal-Article/FAJ-Covered-Call-Strategies-One-Fact-and-Eight-Myths.pdf): the one fact — a covered call is long equity + short volatility. The myths: the payoff diagram shows the risk; downside protection; "income"; higher-vol/shorter-dated = higher yield; time decay is on your side; fits a neutral view; paid for what you'd do anyway; buy stock at a discount. Decomposition 1996–2013: 5.0% excess = **3.3% equity beta + 1.8% short vol**; risk 64% equity / 36% short vol. Carr & Wu (RFS 2009), Bollerslev-Tauchen-Zhou (RFS 2009): the VRP is negative for variance buyers across indices.
*So what:* most of what the wheel/CSP crowd calls income is beta. The genuine VRP sleeve is real, small, high-Sharpe, diluted.

**(c) Retail outcomes.** Bryzgalova, Pavlova & Sikorskaya (JF 2023): retail >60% of option volume by 2021; aggregate retail P&L **−$2.1B** Nov 2019–Jun 2021; **$6.4B** indirect spread costs; retail favors <1-week options with 12.6% quoted spreads. de Silva, Smith & So (Rev. Finance 2026): retail loses 5–9% on average around earnings, 10–14% for high-expected-vol events, mainly by buying overpriced vol and paying the spread. Beckmeyer, Branger & Gayda (2023): ~75% of retail SPX trades are 0DTE; >$70M lost Jan 2021–Feb 2023, ~60% of it costs. Bogousslavsky & Muravyev (2024) dispute magnitude, agree the spread is the killer. FINRA 0DTE insight (June 2026).
*So what:* the retail-loss literature is mostly about **buyers** of short-dated options. The seller-side base rate is (a).

**(d) Tail risk.**

| Event | VIX | Short-vol / short-put outcome |
|---|---|---|
| 2018-02-05 "Volmageddon" | 17.3→37.3 (+115%, largest ever) | XIV/SVXY −90%+; LJM fund $812M→$14M in 2 days. Unlevered PUT −4.8%, BXM −4.7% that week |
| 2018-11 OptionSellers.com | nat gas +18% in a day | naked calls; clients lost 100% and owed debit balances (~$150M [UNVERIFIED]) |
| 2020-03 COVID | 82.69 close 3/16 | 2/19→3/23: PUT −28.9%, BXM −30.3%, WPUT −25.6% vs S&P ~−34%; PUT still +2.1% for 2020 |
| 2024-08-05 yen unwind | 65.7 intraday | SVIX −38.9% [UNVERIFIED]; PUT/BXM −5% peak-trough, positive for August |
| 2025-04 tariffs | 52.3 close 4/8 | 2/19→4/8: PUT −15.1%, BXM −15.5% |

*Mechanism:* leveraged or undiversified short vol dies in 1–2 days; unlevered index put-writing takes ~85% of an equity crash and recovers. A short put is a low-beta equity position with negative convexity, not a hedge and not income.

**(e) Wheel / "theta gang" vs evidence.** Spintwig SPY wheel 2007–2024 (~2,200 trades, 10 variants): **94–99% of return from the long SPY leg**; no variant beat buy-and-hold (https://spintwig.com/spy-wheel-45-dte-options-backtest/). Early Retirement Now (2024): the wheel *adds* delta as the market falls. tastytrade's own "manage at 50%" study (secondary): win rate 82→90% but total P&L cut ~40%. Steady Options (SPX 2001–2020): the 45-DTE / 50% / 21-DTE recipe CAGR 4.44% vs passive hold 5.46%. No independent backtest shows the recipe beating buy-and-hold on total return.
*Base rate to carry:* net ~5–9%/yr, ~0.5 beta, a −30% drawdown roughly once a decade, for a diversified **index** program; single names add earnings gap risk the index does not have.

## 3. Free / paper infrastructure for a headless GitHub Actions pipeline

| Platform | Official API | Options in paper | Spreads in paper | Chains w/ greeks + IV | Historical options | Rate limit | Headless credentials | $ |
|---|---|---|---|---|---|---|---|---|
| **Alpaca** | REST+WS | Yes, default in paper | **Yes** — Level 3 auto-granted to paper accounts (`order_class: mleg`) | snapshots with BS greeks + IV; free `indicative` feed (delayed trades, modified quotes); greeks "not always available for all contracts" | option **bars** since Feb 2024 (traded contracts only; no quotes) | 200/min | static key/secret, no expiry | $0 |
| Tradier sandbox | REST | Yes | multileg ≤4 legs; fill simulation undocumented | ORATS greeks, 15-min delayed | none | 60/min | token never expires | $0 but needs a real brokerage account |
| tastytrade sandbox | REST+DXLink | Yes | synthetic $1 fills | **no market data in sandbox** | none | ? | OAuth refresh non-expiring | $0; wipes daily |
| IBKR paper | TWS/Gateway | Yes | Yes | 15-min delayed unless OPRA paid | on subscribed data | — | needs running Java gateway + 2FA → not headless | $0 acct |
| Schwab / paperMoney | REST (live only) | **No** | — | live only | none | — | 7-day refresh token | $0 |
| Robinhood | agentic MCP | **real money only** | — | — | — | — | OAuth | — |
| Public.com | REST | **no paper** | — | live | none | — | short-lived token | $0 |

**Historical chains are the hard part.** yfinance `option_chain()`: current only, IV but no greeks, unofficial. Cboe delayed JSON: full greeks but ToS forbids automated extraction. Alpaca free: option bars since 2024-02, no quotes → cannot cost a fill. Market Data App: 1-yr free at 100 credits/day, 24h-delayed. Polygon/Massive greeks from $29/mo; ThetaData $40+/mo; ORATS $99/mo; Databento $199/mo; OptionMetrics institutional. DoltHub `post-no-preference/options`: free EOD chains 2019–Jun 2024 (plumbing checks only).

**Conclusion — single stack: Alpaca paper** is the only one meeting all of: official API + options in paper + spreads in paper + chains with greeks/IV + static credentials + $0. Caveats to record: indicative quotes are "modified", greeks occasionally missing per contract, paper fills match NBBO (a short put fills at the bid), paper does not simulate dividends. **The experiment is forward-only by necessity.**

## 4. Design — `opt_pcs_v1`, an isolated pre-registered paper-options experiment

See `docs/experiments/2026-09-08-preregistration-opt-pcs-v1.md` (DRAFT, operator sign-off pending) for the frozen parameters. Summary of the shape: own `experiment_id`, SQLite + committed `state.json`/`metrics.json` under `paper_book/experiments/opt_pcs_v1/`, own `forward_epoch` and `config_hash` incl. costs, own workflow and Alpaca **paper** keys, own store module (the equity `_experiment_store` is single-leg). Touches no Supabase table, never imports the equity experiment scripts.

- **Strategy:** one, defined-risk: 30–45 DTE put credit spread, short strike at |Δ| ≤ 0.25, long strike ~2.5% of spot below; exits at 50% max profit, 21 DTE, or mark ≥ 3× credit; no rolling, no holding to expiry.
- **Three arms, identical trade, different entry days:** `T` (enter when 30-day IV − 20-day realized vol > 0), `N_random` (seeded random days at T's realized frequency), `N_monday` (every Monday regardless). If `N_monday` ≥ `T`, the IV filter is declared dead regardless of T's sign.
- **Universe (freeze at pre-registration, live re-screen 10:30 ET):** 32 names cleared a Friday-close screen (top-40 by options volume or repo `core`/`ai_*`; ≥7 expirations in 60d; spot ≥ $20; 7%-OTM put half-spread ≤ 10% of mid and OI ≥ 500): NVDA, TSLA, AAPL, AMZN, MSFT, META, GOOGL, AMD, NFLX, AVGO, COIN, MSTR, INTC, HOOD, MU, MRVL, TSM, SMCI, DELL, ORCL, PLTR, CRWD, NOW, IREN, CRWV, VST, VRT, ANET, LITE, GLW, SNOW, NBIS. Conditional: COHR, CIEN, GEV, ASML. **Not tradeable cheaply:** FN, NVT, MOD (no weeklies); TLN (41.7% half-spread, OI 0), NRG, CEG, ETN, PATH, AI, HPE, CRDO (too wide) — most of `ai_power` is not an options universe.
- **Costs (single locus, in `config_hash`):** $0.70/contract/side (commission + pass-throughs) → $2.80 per 2-leg round trip; plus half the quoted bid-ask per leg per side. Alpaca paper fills recorded as a shadow cross-check only, never summed.
- **Benchmarks:** same-window buy-and-hold of the underlying AND QQQ; Cboe PUT index as a reference. **Graded daily:** 16:15 ET marks (mid, IV, delta, bid/ask per leg); 10:00 ET entries/exits; every snapshot timestamped so cron drift is visible.
- **Tier-1 (read once):** per arm ≥8 weekly cohorts, ≥60 closed spreads, ≥12 weeks; `continue` iff mean net excess ≥ 0 vs underlying and vs QQQ, book drawdown ≤ 20%, no ticker > 50% of excess (report ex-top-ticker), tail metric: worst-5% mean loss ≤ 6× mean credit. `fail` only below −1.0%/trade net or a drawdown breach. Kill = shelve. No capital at any tier.
- **Dates:** freeze + pre-registration commit at sign-off (earliest 2026-09-08); first fills week of 2026-09-14; last admissions 2026-11-13; **read date 2026-12-18**, deliberately after the equity read.
- **Failure modes recorded:** fills at mid vs reality (`spread_pct_at_fill`, shadow fill), stale 10:00 quotes (`quote_age_s`, `skip_no_bid`), early assignment (`assignment_event`), dividends (`ex_div_in_window`; paper doesn't simulate), earnings in window (`earnings_in_window`, stratified, NOT filtered), IV crush/spike (`iv_entry/exit/max`), missing greeks (`skip_no_greeks`), cron drift (`skip_late_run`, no backfilled fills), stop gapped through (`realized_loss_over_credit`), expiry/pin (`expiry_event`).

## 5. Four-week self-study plan (free)

1. **Contracts & mechanics** — OCC ODD equity chapters; Cboe Options 101; Khan Academy through put-call parity; Hull ch. 10–11; Natenberg ch. 1–4. *Exercise:* pull one NVDA chain in Alpaca paper, compute intrinsic/extrinsic for 5 strikes by hand, place one 1-lot short put and one spread; note fill vs quote. *Surprise:* the short put filled at the bid, not mid — cost as % of premium.
2. **Greeks & volatility** — Cboe greeks modules; tastylive beginner delta/theta/vega/IV rank; Hull 15, 19; Natenberg 5–7. *Exercise:* log Δ/θ/ν daily for a week; predict tomorrow's P&L from theta alone. *Surprise:* a flat-stock day with a big P&L swing — find the vega term.
3. **Spreads, margin, assignment** — OIC assignment FAQ; Cboe Margin Manual; broker level pages; Hull 12; Natenberg 10–12, 16. *Exercise:* compute buying power for CSP / naked put / spread on the same MU strike; reproduce the experiment's `mleg` order by hand. *Surprise:* the CSP on MU ties up ~$95k for ~$4k premium — annualize it.
4. **Evidence & tails** — Israelov & Nielsen; Cboe PUT paper; "After the Volpocalypse"; FINRA 0DTE insight and Notice 26-10; Pub 550. *Exercise:* download PUT and S&P TR daily CSVs, reproduce the 2020/2022 factsheet numbers and the Feb–Mar 2020 drawdown. *Surprise:* PUT collected ~22%/yr premium and kept ~6% — explain where the other 16% went without the word "income".

## What's true vs what's hype

- TRUE: IV exceeds realized vol on average (~3–4 SPX points); sellers are paid a real premium. HYPE: that it is "income" — it is insurance compensation for a fat left tail.
- TRUE: index put-writing delivered equity-like return with ~2/3 the volatility over 1986–2018. HYPE: "beats the market" — since 2007 the S&P's return and Sharpe are higher.
- TRUE: ~2/3 of a covered-call/put-write return is equity beta. HYPE: "market-neutral income."
- TRUE: the bid-ask spread is the largest documented drain on retail option P&L. HYPE: "$0.65 commissions are the cost."
- TRUE: leveraged/naked short vol has been wiped out in 1–2 days; unlevered, diversified, defined-risk selling survived every episode. HYPE: "the wheel can't lose."
- TRUE: "manage at 50% / 21 DTE" raises win rate. HYPE: that it raises returns.
- TRUE: the PDT $25k rule is gone as of June 2026 (phase-in). HYPE: any earlier article about day-trading limits.
- TRUE: nothing in this literature validates single-name put selling on AI names specifically. That is exactly what the experiment would measure.

## Confidence — unverified or partially verified

Alpaca greeks coverage per contract (measure via `skip_no_greeks`); Tradier sandbox fill semantics and token-without-funding; Schwab 7-day refresh (archived docs); Webull paper-options specifics (press release); SVIX −38.9% on 2024-08-05, OptionSellers "$150M / 290 clients", $241k/day 0DTE figure, Carr-Wu VRP magnitude (secondary); CNDR/CMBO/WPUT long-run stats computed from Cboe daily CSVs, not a Cboe document; tastytrade "manage at 50%" numbers from a secondary blog; Natenberg chapter numbers from memory; the liquidity screen used Friday-close after-hours quotes on a holiday (half-spreads are upper bounds; re-screen live before freezing); weekly availability inferred from yfinance expiration counts (Cboe weeklys CSV 404).
