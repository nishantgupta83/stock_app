"""
EDGAR filing agent.

Polls SEC EDGAR for new filings on the watchlist, dedupes by accession_number,
writes to stock_raw_filings + stock_normalized_events.

Run via: .github/workflows/filing_agent.yml (cron */5)
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
    "stock":       {
        "8-K","10-Q","10-K","4",
        "13D","13G","13D/A","13G/A",
        "SC 13D","SC 13G","SC 13D/A","SC 13G/A",
        "SCHEDULE 13D","SCHEDULE 13G","SCHEDULE 13D/A","SCHEDULE 13G/A",
        "S-3","S-3/A",
    },
    "etf":         {"N-PORT","N-CSR","N-CSRS","N-PORT-NT","485BPOS"},
    "mutual_fund": {"N-PORT","N-CSR","N-CSRS","N-PORT-NT","485BPOS"},
    "institution": {
        "13F-HR","13F-HR/A","13F-NT",
        "SC 13D","SC 13G","SC 13D/A","SC 13G/A",
        "SCHEDULE 13D","SCHEDULE 13G","SCHEDULE 13D/A","SCHEDULE 13G/A",
    },
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
    descs      = recent.get("primaryDocDescription", [])  # e.g. "Underwriting Agreement"
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
            "primary_doc_desc":  descs[i] if i < len(descs) else "",
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
        "13D/A":    2,
        "13G/A":    1,
        "10-Q":     1,
        "10-K":     1,
        "S-3":      2,
        "S-3/A":    2,
        # Institutional positions
        "13F-HR":   3,
        "13F-HR/A": 2,
        "SC 13D":   3,    # activist stake (>5%, intent to influence)
        "SC 13G":   2,    # passive stake
        "SC 13D/A": 2,
        "SC 13G/A": 1,
        "SCHEDULE 13D":   3,
        "SCHEDULE 13G":   2,
        "SCHEDULE 13D/A": 2,
        "SCHEDULE 13G/A": 1,
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


# Dilution keyword set — appears in EDGAR's primaryDocDescription for 8-Ks
# attached to financing events (PIPE, public offering, ATM, warrant issuance, etc.).
# Source: empirical scan of 8-K filings 2024-2026 across watchlist stocks.
_DILUTION_KEYWORDS = (
    "underwriting agreement",
    "purchase agreement",
    "private placement",
    "registered direct",
    "warrants to purchase",
    "warrant to purchase",
    "convertible notes",
    "at-the-market",
    "atm offering",
    "shelf takedown",
    "pipe financing",
    "stock and warrant",
)


def looks_like_dilution(filing: dict) -> tuple[bool, str]:
    """Detect dilution-flavored 8-Ks via primaryDocDescription text + item codes.
    Avoids fetching the actual document — keeps EDGAR rate-limit pressure flat.

    Returns (is_dilution, matched_keyword). Item 1.01 (Material Agreement) +
    description keyword = high confidence. Description keyword alone = also
    treated as dilution. Item 1.01 alone (no keyword) is too noisy — many
    1.01s are non-dilutive (commercial agreements, partnerships)."""
    if filing.get("form_type") != "8-K":
        return False, ""
    desc = (filing.get("primary_doc_desc") or "").lower()
    for kw in _DILUTION_KEYWORDS:
        if kw in desc:
            return True, kw
    return False, ""


def upsert_filings(rows: list[dict], ticker: str) -> dict[str, int]:
    """Insert into stock_raw_filings and return accession_number -> raw row id."""
    if not rows:
        return {}
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
    url = f"{SUPABASE_URL}/rest/v1/stock_raw_filings?on_conflict=accession_number"
    headers = {**HEADERS_SB, "Prefer": "resolution=merge-duplicates,return=representation"}
    r = requests.post(url, headers=headers, json=payload, timeout=30)
    if r.status_code not in (200, 201, 204):
        print(f"  Supabase insert {r.status_code}: {r.text}", file=sys.stderr)
        return {}
    returned = r.json() if r.text else []
    if not returned:
        in_list = ",".join(f'"{p["accession_number"]}"' for p in payload)
        rr = requests.get(
            f"{SUPABASE_URL}/rest/v1/stock_raw_filings?accession_number=in.({in_list})&select=id,accession_number",
            headers=HEADERS_SB,
            timeout=20,
        )
        returned = rr.json() if rr.status_code == 200 else []
    return {
        row["accession_number"]: int(row["id"])
        for row in returned
        if row.get("id") is not None and row.get("accession_number")
    }


def emit_normalized_events(filings: list[dict], ticker: str, raw_ids: dict[str, int]) -> int:
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
        # Normalize event_type so thesis_agent's lookups match.
        # "SC 13D" (institutional schedule, with space) collapses to filing_13d
        # to match the operating-company "13D" branch in thesis_agent.score_evidence.
        ft = f["form_type"]
        if ft == "8-K":
            event_type = "8k_material_event"
        elif ft in ("SC 13D", "SC 13D/A", "SCHEDULE 13D", "SCHEDULE 13D/A", "13D", "13D/A"):
            event_type = "filing_13d"
        elif ft in ("SC 13G", "SC 13G/A", "SCHEDULE 13G", "SCHEDULE 13G/A", "13G", "13G/A"):
            event_type = "filing_13g"
        else:
            event_type = f"filing_{ft.lower().replace(' ', '_')}"
        payload.append({
            "event_type":   event_type,
            "ticker":       ticker,
            "event_at":     f["filed_at"],
            "severity":     sev,
            "source_table": "stock_raw_filings",
            "source_id":    raw_ids.get(f["accession_number"]),
            # Defensive idempotency: stock_normalized_events has a partial unique index
            # on dedupe_key, so reruns can heal raw/event lineage without dup events.
            "dedupe_key":   f"filing_{f['accession_number']}",
            "payload": {
                "accession_number": f["accession_number"],
                "form_type":        ft,
                "primary_doc_url":  f["primary_doc_url"],
                "primary_doc_desc": f.get("primary_doc_desc") or "",
                "8k_items":         f.get("8k_items") or "",
            },
        })
        # Dilution-flavored 8-K → emit a SECOND event of type filing_dilution
        # with direction_prior=short. Lets thesis_agent treat it as bearish
        # without contaminating the primary 8-K event's neutral/bullish read.
        is_dil, matched_kw = looks_like_dilution(f)
        if is_dil:
            payload.append({
                "event_type":   "filing_dilution",
                "event_subtype": "8k_financing",
                "ticker":       ticker,
                "event_at":     f["filed_at"],
                "severity":     3,
                "source_table": "stock_raw_filings",
                "source_id":    raw_ids.get(f["accession_number"]),
                "dedupe_key":   f"dilution_{f['accession_number']}",
                "payload": {
                    "accession_number": f["accession_number"],
                    "matched_keyword":  matched_kw,
                    "primary_doc_desc": f.get("primary_doc_desc") or "",
                    "direction_prior":  "short",
                },
            })
    if not payload:
        return 0
    # on_conflict targets the partial unique index on dedupe_key (added in 0004).
    # Without this, PostgREST defaults to PK conflict resolution and the
    # ignore-duplicates Prefer header silently fails to suppress 409s.
    url = f"{SUPABASE_URL}/rest/v1/stock_normalized_events?on_conflict=dedupe_key"
    headers = {**HEADERS_SB, "Prefer": "resolution=merge-duplicates,return=minimal"}
    r = requests.post(url, headers=headers, json=payload, timeout=30)
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

                raw_ids = upsert_filings(recent, ticker)
                n_filings = len(raw_ids)
                n_events  = emit_normalized_events(recent, ticker, raw_ids)
                total_new_filings += n_filings
                total_new_events  += n_events
                print(f"  {ticker}: reconciled {n_filings} raw filings, {n_events} events")
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
