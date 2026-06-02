# Findings

Standalone research artifacts that capture **observations whose action
is deferred** — usually because shipping requires a larger refactor, more
data, or a design decision the calling agent isn't in a position to make.

These docs are advisory. They do not impact running code by themselves.
Anything that becomes an immediate change should land in `RUNBOOK.md`,
the relevant agent, or `next-phases-roadmap.md`.

| File | Date | Status | One-liner |
|------|------|--------|-----------|
| [`2026-05-31_horizon-audit-asymmetry.md`](2026-05-31_horizon-audit-asymmetry.md) | 2026-05-31 | Deferred | All live signals are audited at h1d, but 8K and similar events pay off at h15d. Closing this gap needs an audit-window refactor, not a config flip. |
| [`2026-05-31_narrow-window-fade-risks.md`](2026-05-31_narrow-window-fade-risks.md) | 2026-05-31 | Deferred | The `(djt_self, DJT, h15d)` n=20 acc=0% finding is concentrated in a 3-week window, not 540 days. Hardcoding a blacklist on it would violate the project's own maturity-gate discipline. |
| [`2026-06-02_keyword-db-audit.md`](2026-06-02_keyword-db-audit.md) | 2026-06-02 | Exploration | News classifier has only 24 rules (22 neutral name-matchers + 1 bullish regex + 1 bearish). ~80 catalysts proposed across conferences/AI/geopolitical/regulatory. Computex case study. |
| [`2026-06-02_sev2-news-bar-design.md`](2026-06-02_sev2-news-bar-design.md) | 2026-06-02 | Exploration | Design: let sev≥2 *neutral* news on watchlisted focus tickers count for half-points. Complements keyword DB expansion. Half-points, not full, to avoid over-weighting mega-cap coverage. |
| [`2026-06-02_slm-classifier-feasibility.md`](2026-06-02_slm-classifier-feasibility.md) | 2026-06-02 | Exploration | Free SLM classifier (HF finbert) inside news_agent GHA job. Viable as Path A. Recommended only if keyword DB expansion plateaus above 70% neutral share. |
| [`2026-06-02_independent-research-firms.md`](2026-06-02_independent-research-firms.md) | 2026-06-02 | Exploration | Of 7 named firms (Trading Central, Jefferson, Zacks, etc.) only Zacks has a free tier. Best path: add firm-name keywords to capture coverage that already leaks via news. Free-tier alternatives surveyed (Zacks RSS, Quiver, StockTwits). |
| [`2026-06-02_cluster-score-override.md`](2026-06-02_cluster-score-override.md) | 2026-06-02 | Shipped (flag) | Rejection audit confirmed 100% of thesis silence traced to single_source_no_exception. 9/444 had score>=50. Override added: high-score single-source clusters bypass cluster_passes. Feature-flagged via `CLUSTER_SCORE_OVERRIDE_ENABLED`. |

## Format

Each finding doc should answer:

1. **What we observed** (numbers, with source query/script).
2. **What it might mean** (interpretations, not a single fix).
3. **Why we're not acting now** (the deferral rationale — preconditions, refactor cost, data sufficiency).
4. **What would change our mind** (the trigger conditions that would promote this to an actionable change).

This format lets a future reviewer (or the orchestrator) decide whether
the precondition is now satisfied without re-deriving the observation.
