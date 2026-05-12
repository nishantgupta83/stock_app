"""
Flows agent — institutional 13F + activist parser.

Bridges the gap where filing_agent ingests Berkshire/Bridgewater/Burry/etc.
filings keyed to placeholder tickers (INST_BRK, INST_SCION, …) but never
propagates the affected stock tickers to the rest of the pipeline.

For each new 13F-HR filing on a tracked institutional CIK, this agent:
  1. fetches the index.json for the filing
  2. finds the information_table.xml (SEC's standard holdings file)
  3. parses each <infoTable> entry → name, cusip, shares, value
  4. fuzzy-matches the name of issuer to our watchlist by company name
  5. stores the snapshot in stock_institutional_holdings_snapshot
  6. diffs against the prior quarter's snapshot for the same institution
  7. emits one normalized event per affected ticker per change:
       - institutional_new_position     (Buffett-bump candidate; long bias)
       - institutional_exit             (selling pressure; short bias)
       - institutional_increase         (>25% QoQ increase; long bias)
       - institutional_decrease         (>25% QoQ decrease; short bias)
       - activist_5pct_crossed          (SC 13D fresh threshold cross)

Each event_subtype is suffixed with the institution_label (BRK/BLK/SCION/…)
so calibration tracks per-institution accuracy separately. After a few months
of paper trades, we'll empirically learn which institution's flows actually
move the tape — Berkshire vs BlackRock vs Burry — instead of guessing.

Run via .github/workflows/flows_agent.yml (weekly, Sundays 14:00 UTC).
"""
from __future__ import annotations

import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree as ET

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from filing_agent import (   # type: ignore
    job_run_start, job_run_finish, dead_letter,
    SUPABASE_URL, HEADERS_SB, HEADERS_SEC, EDGAR_BASE,
)

EDGAR_SLEEP = 0.15                # 10 req/sec ceiling; conservative
LOOKBACK_DAYS = 180               # process 13Fs filed in the last 6 months
INCREASE_THRESHOLD = 0.25         # 25% increase QoQ → emit institutional_increase
DECREASE_THRESHOLD = 0.25         # 25% decrease QoQ → emit institutional_decrease

# Institutions we track. Maps CIK → short label used in event subtypes
# (e.g., "institutional_new_position:BRK"). Driven by stock_symbols seed.
INSTITUTION_LABELS: dict[str, str] = {
    "1067983":  "BRK",      # Berkshire Hathaway
    "1364742":  "BLK",      # BlackRock (indexer — usually noise)
    "102909":   "VG",       # Vanguard (indexer)
    "1350694":  "BRDGW",    # Bridgewater (Dalio)
    "1649339":  "SCION",    # Scion (Burry)
    "1336528":  "PERSH",    # Pershing Square (Ackman)
}

# 13F XML namespace varies slightly across filings — strip it for parsing.
_NS_RE = re.compile(r"\{[^}]+\}")


# ============================================================
# Watchlist matching
# ============================================================

def fetch_watchlist_names() -> dict[str, str]:
    """{normalized_name → ticker}. Uses stock_symbols.name from our seed."""
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/stock_symbols",
        headers=HEADERS_SB,
        params={"select": "ticker,name,kind", "limit": "500"},
        timeout=20,
    )
    if r.status_code != 200:
        return {}
    out: dict[str, str] = {}
    for row in r.json():
        # Only stocks + ETFs are tradeable destinations
        if row.get("kind") not in ("stock", "etf"):
            continue
        nm = _normalize_company(row.get("name") or "")
        if nm and row.get("ticker"):
            out[nm] = row["ticker"]
    return out


_STOPWORDS = (
    "CORPORATION", "CORP", "INCORPORATED", "INC", "COMPANY", "CO",
    "PLC", "LIMITED", "LTD", "LLC", "TRUST", "HOLDINGS", "HLDG",
    "GROUP", "GRP", "INDUSTRIES", "PARTNERSHIP", "LP", "NV",
)


def _normalize_company(name: str) -> str:
    """Uppercase, strip punctuation, drop common corporate suffixes,
    collapse whitespace. So 'Microsoft Corp.' and 'MICROSOFT CORPORATION'
    both normalize to 'MICROSOFT'. Conservative — only matches exact
    normalized form, no fuzzy distance."""
    if not name:
        return ""
    s = name.upper().replace(".", " ").replace(",", " ").replace("&", " AND ")
    for stop in _STOPWORDS:
        s = re.sub(rf"\b{stop}\b", " ", s)
    return " ".join(s.split())


def match_ticker(name_of_issuer: str, watchlist: dict[str, str]) -> str | None:
    norm = _normalize_company(name_of_issuer)
    if not norm:
        return None
    # Exact normalized match first (e.g. MICROSOFT == MICROSOFT)
    if norm in watchlist:
        return watchlist[norm]
    # Fall back: holding's first significant word matches a watchlist name
    # exactly. Captures cases like "MICROSOFT" matching "MICROSOFT TECHNOLOGY".
    first = norm.split()[0]
    if first in watchlist:
        return watchlist[first]
    return None


# ============================================================
# EDGAR fetch
# ============================================================

def fetch_recent_13f_filings(cik: str, since_iso: str) -> list[dict]:
    """Pull recent 13F-HR filings for the institution. Returns list with
    accession_number, filed_at, primary_doc_url. Reuses filing_agent's
    EDGAR header convention so the User-Agent is set correctly."""
    url = f"{EDGAR_BASE}/submissions/CIK{cik.zfill(10)}.json"
    r = requests.get(url, headers=HEADERS_SEC, timeout=30)
    if r.status_code != 200:
        print(f"  EDGAR submissions {cik}: {r.status_code}", file=sys.stderr)
        return []
    recent = r.json().get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    accs  = recent.get("accessionNumber", [])
    times = recent.get("acceptanceDateTime", [])
    dates = recent.get("filingDate", [])
    out = []
    for i, form in enumerate(forms):
        if form not in ("13F-HR",):     # skip 13F-NT (notice only) and 13F-HR/A for now
            continue
        filed_at = times[i] if i < len(times) and times[i] else f"{dates[i]}T00:00:00"
        if filed_at < since_iso:
            continue
        out.append({
            "accession_number": accs[i],
            "filed_at":         filed_at,
            "form":             form,
        })
    return out


def fetch_information_table(cik: str, accession_number: str) -> list[dict]:
    """Parse the 13F-HR information_table.xml. Returns
    [{name_of_issuer, cusip, shares, value}]. Empty list on any failure.

    Strategy: hit the filing's index.json, find the file ending in
    '.xml' that's NOT the primary doc, parse it. EDGAR doesn't have a
    fixed naming convention for the info-table file across filers, so
    we try the most common patterns then fall back to "the second xml"."""
    acc_nodash = accession_number.replace("-", "")
    cik_int = int(cik)
    base = f"https://www.sec.gov/Archives/edgar/data/{cik_int}/{acc_nodash}"

    try:
        idx = requests.get(f"{base}/index.json", headers=HEADERS_SEC, timeout=20)
        if idx.status_code != 200:
            return []
        files = idx.json().get("directory", {}).get("item", [])
    except Exception as e:  # noqa: BLE001
        print(f"  index.json fetch failed for {accession_number}: {e}", file=sys.stderr)
        return []

    xml_candidates = [f["name"] for f in files
                      if (f.get("name") or "").lower().endswith(".xml")
                      and "primary_doc" not in (f.get("name") or "").lower()]
    if not xml_candidates:
        return []

    # Filers don't agree on a naming convention — Bridgewater + Pershing use
    # "informationtable.xml", but Berkshire's filer (Donnelley) uses a numeric
    # name like "50240.xml". Try preferred names first then fall through, and
    # accept the first XML that actually parses to non-empty holdings.
    preferred = [n for n in xml_candidates
                 if "informationtable" in n.lower() or "infotable" in n.lower()]
    others    = [n for n in xml_candidates if n not in preferred]
    for filename in preferred + others:
        time.sleep(EDGAR_SLEEP)
        try:
            r = requests.get(f"{base}/{filename}", headers=HEADERS_SEC, timeout=30)
            if r.status_code != 200:
                continue
            holdings = _parse_information_table_xml(r.content)
            if holdings:
                return holdings
        except Exception as e:  # noqa: BLE001
            print(f"  {accession_number}/{filename}: {e}", file=sys.stderr)
            continue
    return []


def _parse_information_table_xml(xml_bytes: bytes) -> list[dict]:
    """Strip namespace, walk infoTable nodes."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        print(f"  XML parse: {e}", file=sys.stderr)
        return []

    def localname(el: ET.Element) -> str:
        return _NS_RE.sub("", el.tag)

    out = []
    for node in root.iter():
        if localname(node) != "infoTable":
            continue
        row = {}
        for child in node:
            tag = localname(child)
            if tag in ("nameOfIssuer", "cusip"):
                row[tag] = (child.text or "").strip()
            elif tag == "value":
                try:
                    row["value"] = int((child.text or "0").strip())
                except (TypeError, ValueError):
                    row["value"] = 0
            elif tag == "shrsOrPrnAmt":
                # nested: <sshPrnamt>1234</sshPrnamt>
                for sub in child:
                    if localname(sub) == "sshPrnamt":
                        try:
                            row["shares"] = int((sub.text or "0").strip())
                        except (TypeError, ValueError):
                            row["shares"] = 0
        if row.get("nameOfIssuer") and row.get("cusip"):
            out.append({
                "name_of_issuer": row["nameOfIssuer"],
                "cusip":          row["cusip"],
                "shares":         row.get("shares") or 0,
                "value":          row.get("value") or 0,
            })
    return out


# ============================================================
# Snapshot persistence + quarterly diff
# ============================================================

def upsert_snapshot(institution_cik: str, institution_label: str,
                    filing: dict, holdings: list[dict],
                    watchlist: dict[str, str]) -> int:
    if not holdings:
        return 0
    # We need the filing_id from stock_raw_filings. filing_agent has already
    # ingested 13F-HR rows for INST_* tickers; look up by accession.
    fid_resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/stock_raw_filings",
        headers=HEADERS_SB,
        params={
            "accession_number": f"eq.{filing['accession_number']}",
            "select": "id",
            "limit":  "1",
        },
        timeout=15,
    )
    if fid_resp.status_code != 200 or not fid_resp.json():
        # filing_agent may not have ingested yet; insert a stub so FK works
        return 0
    filing_id = fid_resp.json()[0]["id"]

    rows = []
    for h in holdings:
        ticker = match_ticker(h["name_of_issuer"], watchlist)
        rows.append({
            "institution_cik":   institution_cik,
            "institution_label": institution_label,
            "filing_id":         filing_id,
            "accession_number":  filing["accession_number"],
            "filed_at":          filing["filed_at"],
            "ticker":            ticker,           # may be None for non-watchlist names
            "name_of_issuer":    h["name_of_issuer"][:200],
            "cusip":             h["cusip"],
            "shares":            h["shares"],
            "value_usd":         h["value"],
        })
    url = (
        f"{SUPABASE_URL}/rest/v1/stock_institutional_holdings_snapshot"
        f"?on_conflict=filing_id,cusip"
    )
    headers = {**HEADERS_SB, "Prefer": "resolution=ignore-duplicates,return=minimal"}
    inserted = 0
    chunk = 500
    for i in range(0, len(rows), chunk):
        batch = rows[i:i+chunk]
        r = requests.post(url, headers=headers, json=batch, timeout=30)
        if r.status_code in (200, 201, 204):
            inserted += len(batch)
        else:
            print(f"  snapshot insert chunk {i//chunk} {r.status_code}: {r.text[:300]}",
                  file=sys.stderr)
    return inserted


def fetch_prior_snapshot(institution_cik: str, before_iso: str) -> dict[str, int]:
    """{ticker → shares} from the most recent snapshot strictly before
    `before_iso`. Tickers only — unmatched names are excluded; we can't
    compute a meaningful diff for them anyway."""
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/stock_institutional_holdings_snapshot",
        headers=HEADERS_SB,
        params=[
            ("institution_cik", f"eq.{institution_cik}"),
            ("filed_at",        f"lt.{before_iso}"),
            ("ticker",          "not.is.null"),
            ("select",          "ticker,shares,filed_at"),
            ("order",           "filed_at.desc"),
            ("limit",           "5000"),
        ],
        timeout=20,
    )
    if r.status_code != 200:
        return {}
    rows = r.json()
    if not rows:
        return {}
    # Group to find the LATEST filing's holdings (rows are mixed across filings)
    latest_filed = rows[0]["filed_at"]
    return {row["ticker"]: int(row.get("shares") or 0)
            for row in rows
            if row["filed_at"] == latest_filed}


def diff_holdings(current: dict[str, int], prior: dict[str, int]) -> list[tuple[str, str, dict]]:
    """Returns [(ticker, change_type, payload)] tuples.
    change_type ∈ {new_position, exit, increase, decrease}."""
    events: list[tuple[str, str, dict]] = []
    cur_set = set(current.keys())
    prior_set = set(prior.keys())

    for t in cur_set - prior_set:
        events.append((t, "new_position", {
            "current_shares": current[t], "prior_shares": 0,
        }))
    for t in prior_set - cur_set:
        events.append((t, "exit", {
            "current_shares": 0, "prior_shares": prior[t],
        }))
    for t in cur_set & prior_set:
        cur, pri = current[t], prior[t]
        if pri == 0:
            continue
        delta = (cur - pri) / pri
        if delta >= INCREASE_THRESHOLD:
            events.append((t, "increase", {
                "current_shares": cur, "prior_shares": pri, "pct_change": round(delta, 4),
            }))
        elif delta <= -DECREASE_THRESHOLD:
            events.append((t, "decrease", {
                "current_shares": cur, "prior_shares": pri, "pct_change": round(delta, 4),
            }))
    return events


# ============================================================
# Event emission
# ============================================================

# Direction priors per change type. Calibration will refine these over time;
# starting from sensible defaults so the first few paper trades aren't random.
_DIRECTION_BY_CHANGE = {
    "new_position":  "long",
    "exit":          "short",
    "increase":      "long",
    "decrease":      "short",
}


def emit_flow_events(institution_label: str, filed_at: str,
                     accession: str, diffs: list[tuple[str, str, dict]]) -> int:
    if not diffs:
        return 0
    rows = []
    for ticker, change_type, payload in diffs:
        event_type = f"institutional_{change_type}"
        # severity: new positions + exits are louder than incremental moves
        sev = 3 if change_type in ("new_position", "exit") else 2
        rows.append({
            "event_type":     event_type,
            "event_subtype":  institution_label,           # "BRK", "SCION", etc.
            "ticker":         ticker,
            "event_at":       filed_at,
            "severity":       sev,
            "source_table":   "stock_institutional_holdings_snapshot",
            "parser_confidence": 0.8,
            "dedupe_key":     f"flows_{accession}_{ticker}_{change_type}",
            "payload": {
                "institution":     institution_label,
                "accession":       accession,
                "change_type":     change_type,
                "direction_prior": _DIRECTION_BY_CHANGE[change_type],
                **payload,
            },
        })
    url = (
        f"{SUPABASE_URL}/rest/v1/stock_normalized_events"
        f"?on_conflict=dedupe_key"
    )
    headers = {**HEADERS_SB, "Prefer": "resolution=merge-duplicates,return=minimal"}
    r = requests.post(url, headers=headers, json=rows, timeout=30)
    if r.status_code not in (200, 201, 204):
        print(f"  emit_flow_events {r.status_code}: {r.text[:300]}", file=sys.stderr)
        return 0
    return len(rows)


# ============================================================
# Main
# ============================================================

def main() -> int:
    started = time.time()
    run_id = job_run_start("flows_agent")
    n_filings = n_holdings = n_events = 0
    try:
        watchlist = fetch_watchlist_names()
        if not watchlist:
            print("  no watchlist names — abort (cold start? check stock_symbols.name)", file=sys.stderr)
            job_run_finish(run_id, "partial", 0, 0, err="empty watchlist")
            return 0
        print(f"Loaded {len(watchlist)} normalized watchlist names")

        since = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).isoformat()
        print(f"Scanning 13F-HR filings since {since[:10]} for {len(INSTITUTION_LABELS)} institutions")

        for cik, label in INSTITUTION_LABELS.items():
            try:
                filings = fetch_recent_13f_filings(cik, since)
                if not filings:
                    print(f"  {label} (CIK {cik}): 0 filings in window")
                    time.sleep(EDGAR_SLEEP)
                    continue

                # Process newest-first so the snapshot is current after one pass
                for filing in sorted(filings, key=lambda f: f["filed_at"]):
                    n_filings += 1
                    holdings = fetch_information_table(cik, filing["accession_number"])
                    if not holdings:
                        print(f"  {label} {filing['accession_number']}: empty info table")
                        time.sleep(EDGAR_SLEEP)
                        continue

                    inserted = upsert_snapshot(cik, label, filing, holdings, watchlist)
                    n_holdings += inserted

                    # Build the matched-ticker view for THIS filing (just inserted)
                    current_matched = {}
                    for h in holdings:
                        t = match_ticker(h["name_of_issuer"], watchlist)
                        if t:
                            current_matched[t] = h["shares"]

                    # Diff vs the snapshot from BEFORE this filing
                    prior = fetch_prior_snapshot(cik, filing["filed_at"])
                    if not prior:
                        print(f"  {label} {filing['accession_number'][:20]}…: "
                              f"first snapshot ({len(current_matched)} matched), no diff this round")
                        time.sleep(EDGAR_SLEEP)
                        continue

                    diffs = diff_holdings(current_matched, prior)
                    if diffs:
                        n_events += emit_flow_events(label, filing["filed_at"],
                                                     filing["accession_number"], diffs)
                        print(f"  {label} {filing['accession_number'][:20]}…: "
                              f"{len(current_matched)} matched, {len(diffs)} flow events emitted")
                    else:
                        print(f"  {label} {filing['accession_number'][:20]}…: "
                              f"{len(current_matched)} matched, no significant changes")
                    time.sleep(EDGAR_SLEEP)
            except Exception as e:  # noqa: BLE001 — don't let one institution block the rest
                dead_letter("flows_agent", "stock_symbols", None,
                            "per_institution_failure", f"{label}: {e}",
                            {"cik": cik, "label": label})
                print(f"  {label}: FAILED ({e})", file=sys.stderr)

        elapsed = time.time() - started
        print(f"DONE in {elapsed:.1f}s — {n_filings} filings processed, "
              f"{n_holdings} snapshot rows, {n_events} flow events emitted")
        job_run_finish(run_id, "ok", n_filings, n_events)
        return 0
    except Exception as e:  # noqa: BLE001
        import traceback
        tb = traceback.format_exc()
        dead_letter("flows_agent", None, None, "top_level_failure", tb)
        job_run_finish(run_id, "failed", n_filings, n_events, err=str(e))
        print(f"FATAL: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
