# Dashboard hosting migration: Hostinger FTPS → Cloudflare Pages (free plan)

**Date:** 2026-07-14 · **Status:** Plan — pending Codex review + user approval · **Target: all phases 0–5 today (~2h)**

## Verified preconditions (checked live 2026-07-14, not assumed)

- `wrangler whoami`: OAuth as **nishugupta@gmail.com**; target account **`cf4e17207cd036cd992130e86b339e24`** ("Nishugupta@gmail.com's Account") present; **`pages (write)` scope present** (only `challenge-widgets.write` missing — Turnstile mgmt, irrelevant).
- ⚠️ Token spans **3 accounts** (tkob24 / Nishugupta / Vick.advisor) → **every wrangler command must pin `CLOUDFLARE_ACCOUNT_ID=cf4e17207cd036cd992130e86b339e24`** (non-interactive wrangler has NO default with 2+ accounts — hard-won lesson).
- Account already runs 4+ Pages projects (hub4apps-archive, hub4apps-admin, …) → direct upload proven on this account. Free-plan D1 enabled (hub4apps-tickets, 1/10).
- `hub4apps.com` NS = `ns1/ns2.dns-parking.com` (**Hostinger DNS, zone NOT on Cloudflare**) → subdomain custom-domain via external CNAME is the only same-day domain option; apex needs a zone move (out of scope).
- Current deploy: `site_generator.yml` → SamKirkland FTP-Deploy ×3 attempts + `site_generator_retry.yml` + cron-job.org pinger (CLAUDE.md #5 flakiness scaffolding). CSP is set via generated `.htaccess` (site_generator.py ~1666) — **inert on Pages; must become `_headers`**.
- Pages free limits (skill ref, Jan 2026): **500 deployments/mo** (we do ~30–60), unlimited static requests/bandwidth, `_headers` ≤100 rules (we need 1), ≤20k files/deploy (dist/ is a few hundred).

## Why move (what free Pages buys over Hostinger FTPS)

Atomic versioned deploys with **instant rollback** + preview URLs, **no FTPS control-socket flakiness** (retires the 3-attempt loop + retry workflow eventually), global CDN, free TLS, and a real auth option (Access). Supabase egress is **unchanged** (site_generator still reads Supabase once per build; only the publish target changes).

## Phases (all today except 6)

### Phase 0 — Preflight (done above) + naming
Project name: **`hub4apps-stock`** (matches existing account naming). **Rule: NEVER connect the GitHub repo to this Pages project** — a Git-connected project breaks `wrangler pages deploy` (Cloudflare forbids Direct Upload on Git-connected projects) and enables unguarded auto-deploy-on-push (hard-won 2026-07-13 lesson).

### Phase 1 — Emit `_headers` + `404.html` from site_generator (~20 min, code)
- In `site_generator.py`, next to the `.htaccess` writer: also write `dist/_headers` with the SAME CSP (Pages format: `/*` + `Content-Security-Policy: …` line) — keep `.htaccess` during dual-publish (inert on Pages, needed on Hostinger).
- Write a minimal `dist/404.html` (pastel, no purple).
- **Path check (Codex):** on Pages, dist/ serves at the ROOT — `status.json` lives at `/status.json`, NOT `/stock_app/status.json`. Grep templates + generator for absolute `/stock_app/` references; nav links are relative (verified in audit) so the site works at root, but fix any absolute path found.
- Test: build check that dist/ contains `_headers` with the CSP string; existing tests stay green.

### Phase 2 — First manual deploy + verify (~20 min)
- Create the project EXPLICITLY (don't rely on deploy auto-create — Codex):
  `CLOUDFLARE_ACCOUNT_ID=cf4e17… npx wrangler pages project create hub4apps-stock --production-branch=main`
- Build dist/ locally once (private shell, SUPABASE_URL/KEY exported — one extra Supabase read cycle, acceptable one-off), then:
  `CLOUDFLARE_ACCOUNT_ID=cf4e17… npx wrangler pages deploy dist/ --project-name=hub4apps-stock --branch=main`
- Verify at `hub4apps-stock.pages.dev` (edge-verify, not assume): `curl -sI` → CSP header present; `<meta git_sha>` matches local; index + Signals + one ticker page render; 404.html serves on a junk path.

### Phase 3 — CI cutover, dual-publish (~30 min)
- Dashboard: create **scoped API token** — Account: *Nishugupta only*, permission: *Cloudflare Pages: Edit* (nothing else). → GitHub secrets `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`.
- `site_generator.yml`: add "Deploy to Cloudflare Pages" step (`npx wrangler pages deploy dist/ --project-name=hub4apps-stock --branch=main`) **BEFORE the FTPS attempts**, AND mark the FTPS attempt steps `continue-on-error: true` (Codex: otherwise FTPS attempt-3 failure still fails the whole job even after a good Pages deploy). Consequence, accepted: the `site_generator_retry.yml` no longer triggers on FTPS flakes — Hostinger becomes best-effort immediately, self-healing on the next run; Pages is the primary target.
- Raise the job `timeout-minutes` 8 → 15 (Codex: wrangler install + upload + extra smoke on top of a worst-case FTPS run).
- Extend the post-deploy git_sha smoke check (the existing D5 pattern) to ALSO verify `https://hub4apps-stock.pages.dev/` (root path — NOT /stock_app/).
- `gh workflow run site_generator.yml` → both targets green, same git_sha.

### Phase 4 — Real auth: Cloudflare Access (~20 min, dashboard)
- Zero Trust (free ≤50 users) → Access application for `hub4apps-stock.pages.dev` (+ `*.hub4apps-stock.pages.dev` for previews): policy = allow **nishugupta@gmail.com** via One-time PIN (no IdP setup).
- This upgrades the client-side PIN theater (hash embedded in page source, content present pre-unlock) to real edge auth. Keep the PIN gate as a harmless inner layer.
- **Automation access (Codex's riskiest-assumption flag):** Access gates EVERYTHING — the CI git_sha smoke, the 3 Claude digests, any monitor reading `/status.json`. Resolution: create an **Access service token** (free) and (a) add a policy allowing that service token on the app, (b) send `CF-Access-Client-Id/Secret` headers from the CI smoke + digest fetchers. Enable Access ONLY after the smoke passes with the token (order inside Phase 4). Set session duration long (e.g. 1 month) so daily human use isn't OTP-per-visit inbox friction.

### Phase 5 — Custom domain, optional (~15 min)
- Pages → Custom domains → add **`stock.hub4apps.com`**; at Hostinger hPanel DNS add `CNAME stock → hub4apps-stock.pages.dev`. (Subdomain-on-external-DNS is supported; **apex is not** without moving the zone.) Add the hostname to the Access app too.
- Bolder option (explicitly deferred, NOT today): move hub4apps.com DNS to Cloudflare free — unlocks apex/WAF/analytics but touches every other site on the domain + 24-48h propagation.

### Phase 6 — Observation + decommission (NOT today; gate: ≥5 green dual-publish days)
- Flip README/docs links to the new URL; remove FTPS steps, `site_generator_retry.yml`, `.htaccess` writer; update CLAUDE.md #5, RUNBOOK §8.
- Flip the absolute self-URLs found in the Phase-1 audit: `telegram_dispatcher.py:42` `SITE_BASE`, `site_generator.py:1922` `SITE_BASE=` (Telegram links + rendered links point at Hostinger until then — fine under dual-publish). NOTE: `archive/` (Phase-9 tiered storage) genuinely LIVES on Hostinger webspace — `price_agent`/`site_generator` read `hub4apps.com/stock_app/archive/index.json`; the archive is NOT part of this migration and must keep working (another reason Hostinger stays).
- **Pinger/cadence untouched** — cron-job.org dispatches the *workflow* (target-agnostic), so the note-#9 four-places trap is not triggered by this migration.
- Hostinger account itself stays (other sites live there).

## Free-plan leverage inventory (beyond hosting — for later, not today)

| Free product | Limit (verify at build time) | Use for THIS project |
|---|---|---|
| **Access (Zero Trust)** | 50 users | Real dashboard auth (Phase 4) |
| **Workers + Cron** | 100k req/day | **F4 Telegram webhook** — the advisor's missing inbound channel (write to `stock_user_decisions`) |
| **D1** | 5GB, 5M reads/day (already on) | Store for the F4 feedback loop / advisor context |
| **Web Analytics** | free, no-cookie RUM | Learn when/whether the dashboard is actually read (informs advisor brief) |
| KV / R2 / Turnstile / Email routing | 100k reads/day · 10GB · unlimited widgets · needs zone | config cache / artifact overflow / future public forms / alerts@ — all deferred |

## Risks & mitigations
1. **Wrong-account writes** → pin `CLOUDFLARE_ACCOUNT_ID` in every command AND in the workflow env; CI token scoped to the one account.
2. **Git-connect temptation** → never connect the repo to the Pages project (breaks direct upload; bypasses deploy rails).
3. **Access lockout of automation** → status.json consumers audited in Phase 4 before enabling.
4. **CSP regression on Pages** → `_headers` verified by curl in Phase 2 (edge-verify; `.htaccess` does nothing on Pages).
5. **Rollback** — Hostinger keeps publishing throughout (dual-publish); Pages has one-click deployment rollback.
