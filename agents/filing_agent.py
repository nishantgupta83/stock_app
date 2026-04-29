"""
EDGAR filing agent.

Polls SEC EDGAR for new filings on the watchlist, dedupes by accession_number,
writes to stock_raw_filings + stock_normalized_events.

Run via: .github/workflows/filing_agent.yml (cron */2)
Local test: SUPABASE_URL=... SUPABASE_SERVICE_KEY=... EDGAR_USER_AGENT="..." python agents/filing_agent.py
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone

import requests

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_KEY"]
EDGAR_UA     = os.environ["EDGAR_USER_AGENT"]   # required by SEC fair-access policy

EDGAR_BASE   = "https://data.sec.gov"
HEADERS_SEC  = {"User-Agent": EDGAR_UA, "Accept": "application/json"}
HEADERS_SB   = {
    "apikey":        SUPABASE_KEY,
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "Content-Type":  "application/json",
    "Prefer":        "resolution=ignore-duplicates,return=minimal",
}

# Forms we care about per kind of filer. Stocks get the broad set; institutions
# get 13F (positions); mutual funds get N-PORT/N-CSR (holdings).
FORMS_BY_KIND = {
    "stock":       {"8-K","10-Q","10-K","4","13D","13G","13D/A","13G/A","S-3","S-3/A"},
    "etf":         {"N-PORT","N-CSR","N-CSRS","N-PORT-NT","485BPOS"},
    "mutual_fund": {"N-PORT","N-CSR","N-CSRS","N-PORT-NT","485BPOS"},
    "institution": {"13F-HR","13F-HR/A","13F-NT","SC 13D","SC 13G"},
    "index":       set(),
}

WATCHLISTS = ("core", "institutions", "mutual_funds")  # ETFs deferred — no actionable filings


def fetch_watchlist() -> list[dict]:
    """Pull (ticker, cik, kind) for every symbol in any tracked watchlist with a CIK."""
    name_filter = ",".join(f'"{n}"' for n in WATCHLISTS)
    url = (
        f"{SUPABASE_URL}/rest/v1/stock_watchlists"
        f"?name=in.({name_filter})"
        f"&select=ticker,stock_symbols(cik,kind)"
    )
    r = requests.get(url, headers=HEADERS_SB, timeout=30)
    r.raise_for_status()
    rows, seen = [], set()
    for row in r.json():
        sym = row.get("stock_symbols") or {}
        cik, kind = sym.get("cik"), sym.get("kind", "stock")
        if not cik or row["ticker"] in seen:
            continue
        seen.add(row["ticker"])
        rows.append({"ticker": row["ticker"], "cik": cik, "kind": kind})
    return rows


def fetch_recent_filings(cik: str, kind: str) -> list[dict]:
    """EDGAR submissions endpoint returns the last ~1000 filings for the CIK.
    `kind` selects which forms to keep (stock vs institution vs mutual_fund)."""
    relevant = FORMS_BY_KIND.get(kind, set())
    if not relevant:
        return []
    url = f"{EDGAR_BASE}/submissions/CIK{cik}.json"
    r = requests.get(url, headers=HEADERS_SEC, timeout=30)
    if r.status_code != 200:
        print(f"  EDGAR {r.status_code} for CIK {cik}", file=sys.stderr)
        return []
    data = r.json()
    recent = data.get("filings", {}).get("recent", {})
    forms      = recent.get("form", [])
    accs       = recent.get("accessionNumber", [])
    dates      = recent.get("filingDate", [])
    times      = recent.get("acceptanceDateTime", [])
    docs       = recent.get("primaryDocument", [])
    items_list = recent.get("items", [])   # 8-K item numbers e.g. "2.02,9.01"
    out = []
    for i, form in enumerate(forms):
        if form not in relevant:
            continue
        acc = accs[i]
        filed_at = times[i] if i < len(times) and times[i] else f"{dates[i]}T00:00:00"
        primary_url = (
            f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/"
            f"{acc.replace('-', '')}/{docs[i]}"
        )
        out.append({
            "accession_number": acc,
            "cik":               cik,
            "form_type":         form,
            "filed_at":          filed_at,
            "primary_doc_url":   primary_url,
            "8k_items":          items_list[i] if i < len(items_list) else "",
        })
    return out


def severity_for_filing(form_type: str, raw: dict) -> int:
    """
    Score how loud this filing should ring on your phone.
    Returns 0..4 (0=info, 4=critical).

    For 8-Ks, item numbers from the EDGAR submissions `items` field are used
    to differentiate high-impact items (acquisition, CEO change, agreement)
    from routine disclosures (financial exhibits, other events).
    """
    table = {
        # Operating company filings
        "8-K":      3,    # upgraded to 4 for high-impact items below
        "4":        1,
        "13D":      3,
        "13G":      2,
        "10-Q":     1,
        "10-K":     1,
        "S-3":      2,
        "S-3/A":    2,
        # Institutional positions
        "13F-HR":   3,
        "13F-HR/A": 2,
        "SC 13D":   3,    # activist stake (>5%, intent to influence)
        "SC 13G":   2,    # passive stake
        # Mutual fund / ETF
        "N-PORT":   1,
        "N-CSR":    1,
        "N-CSRS":   1,
        "485BPOS":  0,
    }
    base = table.get(form_type, 0)

    # Upgrade 8-K severity based on item numbers (parsed from EDGAR submissions JSON)
    if form_type == "8-K":
        items_str = raw.get("8k_items") or ""
        if items_str:
            items = {x.strip() for x in items_str.split(",")}
            # High-impact items → sev 4 (M&A, material agreement, officer change, delisting)
            if items & {"2.01", "1.01", "5.02", "3.01", "1.05"}:
                return 4
            # Routine-only items → sev 1 (exhibits, other events with no specifics)
            if items <= {"8.01", "9.01"}:
                return 1

    return base


def upsert_filings(rows: list[dict], ticker: str) -> int:
    """Insert into stock_raw_filings; conflict on accession_number is fine (dedupe)."""
    if not rows:
        return 0
    payload = []
    for r in rows:
        payload.append({
            "accession_number": r["accession_number"],
            "cik":               r["cik"],
            "ticker":            ticker,
            "form_type":         r["form_type"],
            "filed_at":          r["filed_at"],
            "primary_doc_url":   r["primary_doc_url"],
            "raw_payload":       r,
        })
    url = f"{SUPABASE_URL}/rest/v1/stock_raw_filings"
    r = requests.post(url, headers=HEADERS_SB, json=payload, timeout=30)
    if r.status_code not in (200, 201, 204):
        print(f"  Supabase insert {r.status_code}: {r.text}", file=sys.stderr)
        return 0
    return len(payload)


def emit_normalized_events(filings: list[dict], ticker: str) -> int:
    """
    For each new filing, emit one stock_normalized_events row keyed off it.
    The Thesis Agent later joins these with other agents' events within a 5-min window.
    """
    if not filings:
        return 0
    payload = []
    for f in filings:
        sev = severity_for_filing(f["form_type"], f)
        if sev == 0:
            continue
        payload.append({
            "event_type":   "8k_material_event" if f["form_type"] == "8-K" else f"filing_{f['form_type'].lower()}",
            "ticker":       ticker,
            "event_at":     f["filed_at"],
            "severity":     sev,
            "source_table": "stock_raw_filings",
            "payload": {
                "accession_number": f["accession_number"],
                "form_type":        f["form_type"],
                "primary_doc_url":  f["primary_doc_url"],
                "8k_items":         f.get("8k_items") or "",
            },
        })
    if not payload:
        return 0
    url = f"{SUPABASE_URL}/rest/v1/stock_normalized_events"
    r = requests.post(url, headers=HEADERS_SB, json=payload, timeout=30)
    if r.status_code not in (200, 201, 204):
        print(f"  events insert {r.status_code}: {r.text}", file=sys.stderr)
        return 0
    return len(payload)


# ============================================================
# Operational logging — job heartbeat + dead-letter
# Writes to stock_job_runs and stock_dead_letter_events (see sql/0004).
# Survives Supabase being down: failures here print but don't crash the run.
# ============================================================

def job_run_start(agent: str) -> int | None:
    """Insert a 'running' row, return its id (or None on failure)."""
    url = f"{SUPABASE_URL}/rest/v1/stock_job_runs"
    headers = {**HEADERS_SB, "Prefer": "return=representation"}
    try:
        r = requests.post(url, headers=headers, json={"agent": agent}, timeout=10)
        if r.status_code in (200, 201) and r.json():
            return r.json()[0]["id"]
    except Exception as e:  # noqa: BLE001 — best-effort logging
        print(f"  job_run_start failed: {e}", file=sys.stderr)
    return None


def job_run_finish(run_id: int | None, status: str, rows_in: int, rows_out: int, err: str | None = None) -> None:
    if run_id is None:
        return
    url = f"{SUPABASE_URL}/rest/v1/stock_job_runs?id=eq.{run_id}"
    payload = {
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "status":      status,
        "rows_in":     rows_in,
        "rows_out":    rows_out,
        "error_text":  err,
    }
    try:
        requests.patch(url, headers=HEADERS_SB, json=payload, timeout=10)
    except Exception as e:  # noqa: BLE001
        print(f"  job_run_finish failed: {e}", file=sys.stderr)


def dead_letter(agent: str, source_table: str | None, source_id: int | None, reason: str, detail: str, payload: dict | None = None) -> None:
    url = f"{SUPABASE_URL}/rest/v1/stock_dead_letter_events"
    body = {
        "agent":        agent,
        "source_table": source_table,
        "source_id":    source_id,
        "reason":       reason,
        "detail":       detail[:2000],
        "payload":      payload or {},
    }
    try:
        requests.post(url, headers=HEADERS_SB, json=body, timeout=10)
    except Exception as e:  # noqa: BLE001
        print(f"  dead_letter failed: {e}", file=sys.stderr)


def already_seen_accessions(accs: list[str]) -> set[str]:
    """Pre-filter: query which of these accession numbers already exist."""
    if not accs:
        return set()
    in_list = ",".join(f'"{a}"' for a in accs)
    url = f"{SUPABASE_URL}/rest/v1/stock_raw_filings?accession_number=in.({in_list})&select=accession_number"
    r = requests.get(url, headers=HEADERS_SB, timeout=30)
    if r.status_code != 200:
        return set()
    return {row["accession_number"] for row in r.json()}


def main() -> int:
    started = time.time()
    run_id = job_run_start("filing_agent")
    total_new_filings = 0
    total_new_events  = 0
    n_symbols_processed = 0

    try:
        watchlist = fetch_watchlist()
        print(f"Watchlist: {len(watchlist)} symbols with CIKs")

        for sym in watchlist:
            ticker, cik, kind = sym["ticker"], sym["cik"], sym["kind"]
            try:
                recent = fetch_recent_filings(cik, kind)
                # Healthy no-op (empty response or all-already-seen) still counts as processed.
                n_symbols_processed += 1
                if not recent:
                    time.sleep(0.15)
                    continue

                accs = [r["accession_number"] for r in recent]
                seen = already_seen_accessions(accs)
                new  = [r for r in recent if r["accession_number"] not in seen]
                if not new:
                    time.sleep(0.15)
                    continue

                n_filings = upsert_filings(new, ticker)
                n_events  = emit_normalized_events(new, ticker)
                total_new_filings += n_filings
                total_new_events  += n_events
                print(f"  {ticker}: +{n_filings} filings, +{n_events} events")
            except Exception as e:  # noqa: BLE001 — never let one symbol crash the run
                n_symbols_processed -= 1   # roll back the optimistic increment
                dead_letter("filing_agent", "stock_symbols", None,
                            "per_symbol_failure", f"{ticker}/{cik}: {e}",
                            {"ticker": ticker, "cik": cik, "kind": kind})
                print(f"  {ticker}: FAILED ({e})", file=sys.stderr)
            time.sleep(0.15)

        elapsed = time.time() - started
        status = "ok" if n_symbols_processed == len(watchlist) else "partial"
        print(f"Done in {elapsed:.1f}s. New filings: {total_new_filings}, new events: {total_new_events}")
        job_run_finish(run_id, status, len(watchlist), total_new_filings)
        return 0

    except Exception as e:  # noqa: BLE001 — top-level safety net
        import traceback
        tb = traceback.format_exc()
        dead_letter("filing_agent", None, None, "top_level_failure", tb)
        job_run_finish(run_id, "failed", 0, 0, err=str(e))
        print(f"FATAL: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
