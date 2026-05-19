-- 0029_signals_direction_check.sql
-- E3: pin stock_signals.direction to the current vocabulary.
--
-- Pre-fix: sql/0001:126 declared `direction text not null` with the comment
-- 'BUY', 'SELL', 'TRIM' — but the current code writes 'bullish' / 'bearish'
-- / 'neutral'. No CHECK constraint enforced anything. Future schema drift
-- (a typo, a new value) would silently corrupt the column.
--
-- This migration:
--   1. Backfills any legacy 'BUY'/'SELL'/'TRIM' rows to the current vocab
--      (BUY → bullish, SELL → bearish, TRIM → neutral). If no legacy rows
--      exist (expected on a fresh DB) the UPDATE is a no-op.
--   2. Drops the now-incorrect column comment and replaces it.
--   3. Adds a CHECK constraint enforcing the current vocabulary.

-- 1. Backfill legacy values
update stock_signals set direction = 'bullish' where direction in ('BUY');
update stock_signals set direction = 'bearish' where direction in ('SELL');
update stock_signals set direction = 'neutral' where direction in ('TRIM');

-- 2. Replace the misleading comment
comment on column stock_signals.direction is
  'Cluster net direction: bullish | bearish | neutral. Maturity gate (90% acc, n>=30) unlocks BUY/SELL only in the Telegram vocabulary layer — the column itself stays in the intelligence vocabulary.';

-- 3. Enforce the constraint. NOT VALID first so legacy rows that escape the
-- backfill don't block the migration; we'll VALIDATE separately after
-- confirming nothing remains.
do $$
begin
  if not exists (
    select 1 from information_schema.table_constraints
    where table_name = 'stock_signals'
      and constraint_name = 'stock_signals_direction_check'
  ) then
    alter table stock_signals
      add constraint stock_signals_direction_check
      check (direction in ('bullish', 'bearish', 'neutral'));
  end if;
end$$;
