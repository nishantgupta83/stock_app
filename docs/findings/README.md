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

## Format

Each finding doc should answer:

1. **What we observed** (numbers, with source query/script).
2. **What it might mean** (interpretations, not a single fix).
3. **Why we're not acting now** (the deferral rationale — preconditions, refactor cost, data sufficiency).
4. **What would change our mind** (the trigger conditions that would promote this to an actionable change).

This format lets a future reviewer (or the orchestrator) decide whether
the precondition is now satisfied without re-deriving the observation.
