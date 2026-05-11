# Multi-Domain Expansion Roadmap

## Premise

The AI cluster proved the architecture works: ticker basket → event ingest →
normalized events → paper trades → calibration → telegram. Every major capital
flow theme is now just another `<domain>_agent.py` + `<domain>_agent.yml` + a
categorized watchlist.

This document is the build plan for graduating from "AI signal pipeline" to a
**multi-domain market intelligence platform** by adding six new domain agents
that share the same event bus and calibration table.

## The architectural pattern (one domain = one GitHub Actions agent)

```
                  ┌──────────────────────────────────┐
                  │   stock_normalized_events        │
                  │   (universal event bus)          │
                  └────────────┬─────────────────────┘
                               │
   ┌───────────┬───────────┬───┴────┬───────────┬───────────┐
   ↓           ↓           ↓        ↓           ↓           ↓
 ai_*      defense_*   biotech_*  energy_*   macro_*    activist_*
 (have)    (planned)   (planned)  (planned)  (planned)  (planned)
```

Every new agent contributes to the same `stock_normalized_events` table and the
same `stock_rule_calibration` learning loop. Cross-domain bonuses (e.g.,
"defense + rates risk-off" or "biotech FDA + macro Fed pivot") emerge naturally
through the existing `thesis_agent` intelligence layer.

## Existing AI cluster (reference architecture)

Six watchlists totalling 33 stocks + 15 ETFs. Categorized:

| Watchlist | Tickers |
|---|---|
| `ai_compute`  | MU, MRVL, TSM, ASML, ALAB |
| `ai_optical`  | LITE, COHR, CIEN, ANET, GLW, FN, CRDO |
| `ai_servers`  | SMCI, DELL, HPE, VRT, NVT, MOD |
| `ai_power`    | CEG, VST, GEV, ETN, TLN, NRG |
| `ai_software` | ORCL, PLTR, CRWD, NOW, SNOW, AI, PATH |
| `ai_neocloud` | IREN, CRWV |

Plus pre-existing `core` (23 mega-caps) and `context` (18 ETFs/indices).

## Six new domain agents

### 1. `macro_rates_agent` — the master signal

Every other domain's risk-on/off behavior is driven by Fed, CPI, jobs, yields.
Predicts the regime everything else trades in.

| Aspect | Detail |
|---|---|
| Watchlist | `macro_rates`: TLT, IEF, SHY, UUP, GLD, GDX, ^TNX, DXY |
| Event types | `fomc_decision`, `cpi_release`, `nfp_release`, `fed_speech`, `yield_inversion` |
| Catalysts | FOMC meetings (8/yr), monthly CPI/NFP/PPI, daily 10Y yield moves > 5 bps |
| Telegram triggers | VIX > 25, 10Y crosses 5%, FOMC surprise, CPI ± 0.2% surprise |
| Data sources | FRED API (free), Fed RSS, Treasury auction calendar |
| Effort | ~2 hours |
| Cross-domain payoff | Massive — feeds `is_risk_off()` check used by AI, biotech, energy |

### 2. `defense_agent` — geopolitical + government capex

Sustained 2026+ defense spending cycle. Counter-cyclical to consumer.

| Aspect | Detail |
|---|---|
| Watchlists | `defense_primes`: LMT, RTX, NOC, GD, BA, HII, LHX. `defense_drones`: AVAV, KTOS, RKLB. `defense_cyber`: PANW, CRWD, FTNT, NET, ZS |
| Event types | `dod_contract_award`, `defense_bill_passed`, `geopolitical_event` |
| Catalysts | DoD contract awards ($50M+ via DD250 public feed), defense bill votes, conflict escalation |
| Telegram triggers | Contract awards > $1B, defense bill votes, geopolitical severity 4 |
| Data sources | DoD contracts feed (free), Congress.gov API, Reuters/AP geopolitics RSS |
| Effort | ~3 hours |

### 3. `biotech_agent` — pure event-driven alpha

FDA approval = ± 50% overnight. Uncorrelated to AI/macro. Cleanest binary catalysts in the market.

| Aspect | Detail |
|---|---|
| Watchlists | `biotech_glp1`: NVO, LLY, VKTX, AMGN. `biotech_oncology`: REGN, VRTX, ALNY. `pharma_majors`: LLY, MRK, PFE, JNJ, ABBV, MRNA. `medtech`: ISRG, BSX, MDT |
| Event types | `fda_pdufa_decision`, `clinical_readout`, `biotech_ma`, `panel_vote` |
| Catalysts | PDUFA dates (calendar), Phase 2/3 readouts, AdCom panel votes, M&A |
| Telegram triggers | Any FDA decision, Phase 3 topline result, M&A premium > 25% |
| Data sources | FDA calendar (free), clinicaltrials.gov API, BioPharma Catalyst RSS |
| Effort | ~4 hours |
| Edge | Pre-PDUFA positioning — buy 5 days before, exit pre-decision (positive expectancy in literature) |

### 4. `energy_transition_agent` — EV / solar / nuclear / battery

Policy-driven mega-cycle. IRA tax credits, EV mandates, nuclear renaissance for AI data centers.

| Aspect | Detail |
|---|---|
| Watchlists | `ev_makers`: TSLA, RIVN, LCID, NIO. `solar`: FSLR, ENPH, SEDG. `battery_storage`: ALB, LAC, FLNC. `nuclear`: CCJ, BWXT, NNE, OKLO, SMR. `charging_infra`: CHPT, BLNK |
| Event types | `ev_sales_release`, `policy_change`, `solar_subsidy`, `nuclear_license_approval` |
| Catalysts | Monthly EV deliveries, IRA tax credit changes, solar tariffs, NRC approvals |
| Telegram triggers | Monthly delivery beat/miss, policy reversals, M&A |
| Data sources | EIA API, NRC RSS, OEM monthly delivery reports |
| Effort | ~3 hours |

### 5. `activist_insider_agent` — highest signal-to-noise

When tracked activists file 13D, underlying often rallies 10-30% in 60 days. Cluster CEO/CFO buys
have the cleanest fundamental signal in equities.

| Aspect | Detail |
|---|---|
| Watchlist | Track *activists*, not tickers: Pershing Square (Ackman), Icahn, Elliott (Singer), ValueAct, Trian, Starboard, Third Point (Loeb), Scion (Burry) |
| Event types | `activist_initial_position`, `activist_doubled_down`, `insider_cluster_buy`, `ceo_open_market_buy` |
| Catalysts | New 13D filings, Form 4 cluster patterns (3+ insiders same week), CEO buys > $5M |
| Telegram triggers | Any new 13D from tracked activist, insider cluster buy |
| Data sources | **Already in pipeline** — `filing_agent` pulls 13D and Form 4; needs activist registry + cluster detector |
| Effort | ~2 hours (mostly query + watchlist) |
| Edge | Lowest hit-rate-needed, highest payoff per signal |

### 6. `consumer_health_agent` — cycle sentinel

Retail sales, traffic, restaurant data → consumer cycle predictor. Counter-signal to AI/tech bubble talk.

| Aspect | Detail |
|---|---|
| Watchlists | `retail_big_box`: WMT, COST, HD, LOW, TGT. `restaurants`: SBUX, MCD, CMG. `travel_leisure`: ABNB, BKNG. `discretionary`: NKE, AMZN, SHOP |
| Event types | `monthly_retail_sales`, `same_store_sales`, `traffic_data`, `consumer_sentiment` |
| Catalysts | Monthly retail reports, Black Friday/holiday data, consumer sentiment |
| Telegram triggers | Monthly retail miss/beat, weekly TSA throughput records, holiday guidance |
| Data sources | Census Bureau, ICSC retail, TSA daily data |
| Effort | ~3 hours |

## Recommended build sequence

```
Week 1:  macro_rates_agent       ← unlocks risk_off for everything
Week 2:  activist_insider_agent  ← reuses existing filing_agent infra
Week 3:  defense_agent           ← new data source (DoD)
Week 4:  biotech_agent           ← FDA calendar integration
Week 5:  energy_transition_agent ← policy-heavy, longer ingest dev
Week 6:  consumer_health_agent   ← polish + cycle indicators
```

Build sequentially — each new agent enriches the next via the thesis_agent
intelligence layer (sector cluster bonus, hyperscaler echo, risk-off, etc.).

## Cross-domain leverage points

Once six domains exist, rotation signals emerge that aren't possible from one cluster:

| Signal | Logic | Trade thesis |
|---|---|---|
| Risk-off rotation | `macro_rates` fires risk-off + `activist_insider` shows defensive buys (UNH, JNJ) | Long defensives, short cyclicals |
| Capex cycle | AI capex up + `energy_transition` utility 8-K up + `defense` contracts up | Long industrial/electrical (ETN, GEV) — feed all three |
| GLP-1 + retail link | `biotech` GLP-1 beat + `consumer_health` retail miss (food, apparel) | Long NVO/LLY, short MCD/PEP |
| Geopolitical shock | `defense` event sev 4 + `macro_rates` risk-off + AI down | Long defense + gold, short tech |
| Election echo | `truth_social` (have) + `macro_rates` + `biotech` drug pricing | Position drug-pricing-sensitive names |

## Implementation notes (reuse, do not reinvent)

For each new domain agent:

1. **Skeleton**: copy `agents/filing_agent.py` structure (job_run_start, HEADERS_SB, helpers) — every agent shares the same operational pattern
2. **Watchlist**: insert categorized rows in `stock_watchlists` with the domain prefix (e.g., `defense_primes`)
3. **stock_symbols**: add CIK from `https://www.sec.gov/files/company_tickers.json` for US-listed tickers
4. **Event types**: pick names that don't collide with existing types — prefix with domain if needed
5. **GitHub workflow**: copy `.github/workflows/filing_agent.yml` and adjust cron + step name; reuse `ops_recorder.py` for workflow-level health tracking
6. **Intelligence layer**: extend `thesis_agent.py` `HYPERSCALER_SUPPLIERS` / `POWER_UTILITIES` / sector watchlist arrays to recognize the new domain

## Status

This is a forward-looking roadmap. The AI cluster (Phase 10) is live as of 2026-05-11.
Multi-domain expansion begins after AI calibration produces its first mature rule
(currently 1,604 open paper trades feeding `stock_rule_calibration` overnight; expected
first mature rule in 2-3 weeks per the post-backfill trajectory).
