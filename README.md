# Hub4Apps Market Intelligence

Free-tier, notification-first market intelligence platform.
Telegram alerts on filings, news, and Trump posts that move the tape.

**Design doc:** [`docs/market-intelligence-platform-design.md`](docs/market-intelligence-platform-design.md)
**Phase 0 setup:** [`docs/PHASE0_CHECKLIST.md`](docs/PHASE0_CHECKLIST.md)

## Stack

| Layer | Service | Cost |
|---|---|---|
| Compute | GitHub Actions (public repo, unlimited minutes) | $0 |
| DB + auth | Supabase Free | $0 |
| Push notifications | Telegram Bot API | $0 |
| Static frontend | Hostinger shared hosting (manual upload) | already paid |
| Domain | hub4apps.com | already paid |

## Layout

```
.
├── docs/                       Design doc + Phase 0 checklist
├── sql/                        Supabase migrations (run in order)
├── agents/                     One Python file per agent
├── .github/workflows/          One YAML per agent (cron-scheduled)
├── templates/                  Jinja2 for static-site generator (Phase 2)
└── dist/                       Generated HTML to upload to Hostinger (Phase 2)
```

## Phase 0 status

- [x] Supabase schema written (`sql/0001_initial_schema.sql`, `sql/0002_seed_universe.sql`, `sql/0003_add_kind_and_funds.sql`)
- [x] EDGAR filing agent written (`agents/filing_agent.py`)
- [x] GitHub Actions workflow for filing agent (`.github/workflows/filing_agent.yml`)
- [ ] **You: complete Phase 0 checklist** (see `docs/PHASE0_CHECKLIST.md`)
- [ ] First filing observed in `stock_raw_filings`
