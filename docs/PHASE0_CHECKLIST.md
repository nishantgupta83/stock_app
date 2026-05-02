# Phase 0 Checklist

These five steps require **your** action — I cannot do them from here. Estimated time end-to-end: **30-45 minutes**.

When all five are done, the EDGAR filing agent will run every 5 minutes on GitHub Actions and write new SEC filings into `stock_raw_filings`.

---

## 1. Create the GitHub repo (5 min)

1. Go to https://github.com/new
2. Repo name: `stock_app` (or whatever you prefer)
3. **Visibility: Public** ← unlocks unlimited free Actions minutes
4. Do **not** initialize with README/license/.gitignore (we have them)
5. After creation, GitHub shows you the push commands. From this directory:
   ```bash
   cd /Users/nishantgupta/Documents/nishant_projects/stock_app
   git init
   git add .
   git commit -m "Phase 0 scaffold"
   git branch -M main
   git remote add origin https://github.com/YOUR-USER/stock_app.git
   git push -u origin main
   ```

> **Note:** `stock_app/` is currently tracked under the parent `nishant_projects` repo. Running `git init` here creates a nested repo. If you want a clean split, first `git rm -r --cached stock_app/` from the parent.

## 2. Create the Supabase project (10 min)

1. Go to https://app.supabase.com/projects → **New project**
2. Name: `stock-intelligence` (anything, just remember it)
3. Region: pick closest (US East if East Coast, US West if West Coast)
4. Database password: **save this in your password manager** — you won't be shown it again
5. Wait ~2 min for provisioning
6. Once ready: **SQL Editor → New query** → paste the contents of each file in order:
   - `sql/0001_initial_schema.sql` → Run
   - `sql/0002_seed_universe.sql` → Run
   - `sql/0003_add_kind_and_funds.sql` → Run
   - `sql/0004_ops_tables.sql` → Run (heartbeat + dead-letter + signal enrichment)
   - `sql/0005_extend_status_and_data_sources.sql` → Run (adds status_v2='backtest' + data sources registry)
   - `sql/0006_add_closed_status.sql` → Run (adds status_v2='closed')
   - `sql/0007_allow_chase_risk.sql` → Run (adds action='CHASE_RISK')
   - `sql/0008_paper_forecasts.sql` → Run (adds calibrated paper forecasts)
   - `sql/0009_paper_forecast_modes.sql` → Run (separates live from shadow_backtest forecasts)
   - `sql/0010_reliability_and_calibration.sql` → Run (adds audit/evidence uniqueness, dispatch retry status, and calibration summary)
7. Verify: **Table Editor** should show the `stock_*` tables incl. `stock_job_runs`, `stock_dead_letter_events`, `stock_data_sources`
8. Get your credentials: **Project Settings → API**:
   - Copy `Project URL` → this is `SUPABASE_URL`
   - Copy `service_role` key (NOT `anon`) → this is `SUPABASE_SERVICE_KEY`
   - **Treat `service_role` like a password.** Never commit it. Never paste it into a browser. Only into GitHub Actions secrets.

## 3. Create the Telegram bot (5 min)

1. Open Telegram, search for `@BotFather`, start a chat
2. Send `/newbot`
3. Pick a name (`Hub4Apps Market Intel`) and username (must end in `bot`, e.g. `hub4apps_market_bot`)
4. BotFather replies with a token like `123456789:ABCdef...` → this is `TELEGRAM_BOT_TOKEN`
5. **Send any message to your new bot** (this opens the chat from your side)
6. Open this URL in a browser, replacing `<TOKEN>`:
   ```
   https://api.telegram.org/bot<TOKEN>/getUpdates
   ```
7. In the JSON response find `"chat":{"id": 987654321, ...}` → that number is `TELEGRAM_CHAT_ID`

(Phase 0 doesn't actually push Telegram alerts yet — Phase 1 does — but capture these now so you don't have to come back.)

## 4. Add GitHub Actions secrets (5 min)

In your GitHub repo: **Settings → Secrets and variables → Actions → New repository secret**.

Add these four:

| Name | Value |
|---|---|
| `SUPABASE_URL` | from step 2 (e.g. `https://abcdefgh.supabase.co`) |
| `SUPABASE_SERVICE_KEY` | from step 2 (long `eyJ...` string) |
| `EDGAR_USER_AGENT` | `Nishant Gupta nishugupta@gmail.com` (your real name + email — required by SEC) |
| `TELEGRAM_BOT_TOKEN` | from step 3 (Phase 0 doesn't use it yet, add anyway) |
| `TELEGRAM_CHAT_ID` | from step 3 (Phase 0 doesn't use it yet, add anyway) |

## 5. Trigger the first run (2 min)

1. In your GitHub repo → **Actions** tab → `filing_agent` (left sidebar)
2. Click **Run workflow** → **Run workflow** (uses the manual trigger)
3. Wait ~1 minute, refresh, click the run to see logs
4. Successful output looks like:
   ```
   Watchlist: 27 symbols with CIKs
     NVDA: +0 filings, +0 events
     AAPL: +1 filings, +1 events
     ...
   Done in 4.2s. New filings: 3, new events: 2
   ```
5. Verify in Supabase: **Table Editor → stock_raw_filings** → you should see rows

After this, the workflow runs every 5 minutes automatically. Walk away.

---

## Troubleshooting

**Workflow fails with `KeyError: 'SUPABASE_URL'`** → secret not set or misnamed. Check Settings → Secrets.

**Workflow runs but inserts 0 rows** → most likely the EDGAR endpoint returned 200 but no new filings since last poll. Confirm with a manual query:
```bash
curl -A "Your Name your@email.com" https://data.sec.gov/submissions/CIK0001045810.json | head -c 500
```

**EDGAR returns 403** → your `EDGAR_USER_AGENT` is wrong format. Must be `Name email@domain.com`, not `python-requests/...`.

**Supabase POST returns 401** → you used the `anon` key instead of `service_role`. Fix the secret.

**Supabase POST returns 409** → conflict on `accession_number`. This is **expected and fine** — it means the filing was already ingested. The `Prefer: resolution=ignore-duplicates` header should make this silent; if you see 409, re-check the header is set in `agents/filing_agent.py`.

---

## What's next (Phase 1)

Once Phase 0 is humming for a day or two:
- `truth_social_agent` polling Trump posts via trumpstruth.org RSS
- `thesis_agent` joining filing + Truth Social evidence within 5-min windows
- Telegram dispatcher with the locked alert payload
- First real alert on your phone
