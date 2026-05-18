-- 0026_risk_decisions.sql
--
-- New table for the risk layer (Layer 4). risk_agent reads stock_trade_setups
-- and emits one decision per setup: either sized (with size_pct_portfolio
-- and rules_applied breakdown) or skipped (with a reason).
--
-- IMPORTANT layer rule: this table is paper-only / advisory. risk_agent
-- never sends orders. The size_pct_portfolio field is a recommendation
-- under a hypothetical $100k portfolio; a future broker_adapter would
-- translate it into real qty.
--
-- Stage 6 of the trading-pipeline backlog.

create table if not exists stock_risk_decisions (
  id                       bigserial primary key,
  setup_id                 bigint      not null references stock_trade_setups(id) on delete cascade,
  decision                 text        not null check (decision in ('size', 'skip')),
  size_pct_portfolio       numeric,                                 -- e.g. 0.01 = 1% of NAV at risk
  size_dollars_at_100k     numeric,                                 -- size if portfolio is $100k baseline
  max_loss_dollars         numeric,                                 -- stop-distance × position size
  reason                   text,
  rules_applied            jsonb       not null default '[]'::jsonb, -- ordered list of risk rules that ran
  portfolio_state          jsonb,                                   -- snapshot of state used for sizing
  created_at               timestamptz not null default now(),
  unique (setup_id)                                                 -- one decision per setup, idempotent re-runs
);

create index if not exists stock_risk_decisions_decision_idx
  on stock_risk_decisions (decision, created_at desc);

create index if not exists stock_risk_decisions_setup_idx
  on stock_risk_decisions (setup_id);

alter table stock_risk_decisions enable row level security;

comment on table stock_risk_decisions is
  'Risk-layer output: one decision per stock_trade_setups row. Paper-only/advisory: size_pct_portfolio is a recommendation under hypothetical $100k NAV. A future broker_adapter translates this to real qty.';

comment on column stock_risk_decisions.rules_applied is
  'JSON array of risk rules in order of evaluation: [{rule, passed, detail}]. Lets operators audit WHY a setup was sized or skipped.';
