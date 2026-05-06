-- Phase 8 — institutional flows.
-- flows_agent fetches each institution's 13F-HR information_table.xml from EDGAR,
-- parses the holdings (CUSIP, name, shares, value), fuzzy-matches the name of
-- issuer to our watchlist, and stores the snapshot here. Quarter-over-quarter
-- diff against this table produces the per-ticker institutional flow events
-- (institutional_new_position / exit / increase / decrease).
--
-- Why a snapshot table and not just re-parse on demand: avoids re-fetching the
-- 2-10MB xml on every run, and lets us answer "what did Berkshire hold last
-- quarter?" instantly. Storage is small (≤ ~30 watchlist matches × 6 institutions
-- × 8 quarterly snapshots ≈ 1500 rows).

create table if not exists stock_institutional_holdings_snapshot (
  id                bigserial primary key,
  institution_cik   text not null,                   -- e.g. '0001067983' (Berkshire)
  institution_label text not null,                    -- short tag for breakdowns: 'BRK','BLK','SCION',...
  filing_id         bigint references stock_raw_filings(id) on delete cascade,
  accession_number  text not null,
  filed_at          timestamptz not null,
  ticker            text,                              -- our matched watchlist ticker (NULL = unmatched)
  name_of_issuer    text not null,                     -- as reported in 13F (uppercase)
  cusip             text not null,
  shares            bigint,
  value_usd         numeric,                           -- raw value field; thousands vs $ varies by year
  created_at        timestamptz default now()
);

-- One row per (filing, cusip) so re-parsing the same 13F is idempotent.
create unique index if not exists stock_institutional_holdings_snapshot_uniq
  on stock_institutional_holdings_snapshot (filing_id, cusip);

-- Hot read paths:
--   1. "Did this institution hold this ticker as of the last filing?" → quarterly diff
create index if not exists stock_inst_holdings_inst_ticker_idx
  on stock_institutional_holdings_snapshot (institution_cik, ticker, filed_at desc);

--   2. "What's the latest set of institutions holding TSLA?" → cross-institution view
create index if not exists stock_inst_holdings_ticker_idx
  on stock_institutional_holdings_snapshot (ticker, filed_at desc) where ticker is not null;

alter table stock_institutional_holdings_snapshot enable row level security;
