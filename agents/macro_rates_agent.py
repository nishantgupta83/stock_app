"""
Macro rates agent — the master signal.

Ingests Fed/CPI/jobs/yield data from FRED and emits macro-wide events to
`stock_normalized_events` plus immediate Telegram alerts on regime changes
(VIX>25, 10Y>5%, FOMC decisions, surprise CPI/NFP).

Why this matters: every other domain agent's risk-on/off behavior is driven by
these signals. macro_rates is the upstream regime detector — its output feeds
`thesis_agent.is_risk_off()` which suppresses bullish alerts during VIX spikes
or yield blowouts.

Data sources (all free, no paid APIs):
  - FRED API series:
      DGS10     10Y Treasury yield (daily)
      DGS2      2Y Treasury yield (daily, for inversion check)
      CPIAUCSL  CPI All Urban (monthly headline)
      CPILFESL  Core CPI (monthly, ex food+energy)
      PAYEMS    Non-farm payrolls (monthly first Friday)
      UNRATE    Unemployment rate (monthly)
      DFEDTARU  Fed funds target upper bound (changes on FOMC days)
  - VIX: ⚠️ NOT currently ingested into stock_raw_prices (verified 2026-06-09 —
    zero VIX rows). check_vix_regime() + thesis_agent.is_risk_off()'s VIX branch
    therefore fail open silently; only the yield/FOMC regime paths are live.
    Wire VIX into price ingestion before relying on VIX-based risk-off.

Events emitted to stock_normalized_events (ticker='MACRO' sentinel):
  - yield_milestone    severity 3-4  (10Y crosses 5% / curve inverts vs 2Y)
  - cpi_release        severity 2-4  (graded by surprise magnitude)
  - nfp_release        severity 2-4
  - fomc_decision      severity 4
  - vix_spike          severity 3-4  (VIX > 25 / > 35)

Runs hourly during market hours plus once after FOMC announcement window.
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from filing_agent import (   # type: ignore
    job_run_start, job_run_finish, dead_letter,
    SUPABASE_URL, HEADERS_SB,
)

FRED_API_KEY  = os.environ.get("FRED_API_KEY", "")
BOT_TOKEN     = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID       = os.environ.get("TELEGRAM_CHAT_ID", "")

# ============================================================
# Thresholds — empirical, conservative; tune via calibration over time
# ============================================================

# Yield milestones — anything above 5% on 10Y is a regime change signal;
# below 4% is "normalized"; above 4.5% is "elevated".
YIELD_10Y_REGIME_HIGH      = 5.0
YIELD_CURVE_INVERSION_BPS  = 0   # 10Y-2Y < 0 = inverted

# VIX bands — 25 = stressed, 35 = panicked
VIX_STRESS = 25.0
VIX_PANIC  = 35.0

# CPI / NFP surprise bands (vs survey of economists; using prior-month-on-month
# as a proxy when we don't have a consensus source). 0.3% MoM CPI surprise is
# the same magnitude that historically moves markets >2%.
CPI_SURPRISE_HIGH = 0.003   # 0.3% MoM
CPI_SURPRISE_MED  = 0.001   # 0.1% MoM
NFP_SURPRISE_HIGH = 100_000 # jobs vs prior
NFP_SURPRISE_MED  = 50_000

FRED_BASE = "https://api.stlouisfed.org/fred"

# ============================================================
# FRED client
# ============================================================

def fred_observations(series_id: str, limit: int = 4) -> list[dict]:
    """Most-recent N observations for a FRED series. Returns [] on any error."""
    if not FRED_API_KEY:
        print(f"  FRED_API_KEY missing — skipping {series_id}", file=sys.stderr)
        return []
    try:
        r = requests.get(
            f"{FRED_BASE}/series/observations",
            params={
                "series_id":   series_id,
                "api_key":     FRED_API_KEY,
                "file_type":   "json",
                "sort_order":  "desc",
                "limit":       limit,
            },
            timeout=15,
        )
        if r.status_code != 200:
            print(f"  FRED {series_id} {r.status_code}: {r.text[:200]}", file=sys.stderr)
            return []
        return r.json().get("observations", [])
    except Exception as exc:  # noqa: BLE001
        print(f"  FRED {series_id} exc: {exc}", file=sys.stderr)
        return []


def _f(obs: dict) -> float | None:
    """FRED returns '.' for missing values; cast safely to float."""
    v = obs.get("value")
    if v in (None, "", "."):
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ============================================================
# Event emission
# ============================================================

def emit_event(event_type: str, severity: int, payload: dict,
                event_subtype: str | None = None, event_at: str | None = None) -> int | None:
    """Insert into stock_normalized_events with MACRO sentinel ticker.

    dedupe_key prevents duplicate emissions of the same release (yield
    milestone crossings can flap day-to-day; we want one event per crossing).
    """
    when = event_at or datetime.now(timezone.utc).isoformat()
    dedupe_key = payload.get("dedupe_key") or f"macro_{event_type}_{when[:10]}"
    row = {
        "ticker":            "MACRO",
        "event_type":        event_type,
        "event_subtype":     event_subtype,
        "event_at":          when,
        "severity":          severity,
        "source_table":      "fred_api",
        "parser_confidence": 0.95,
        "dedupe_key":        dedupe_key,
        "payload":           {k: v for k, v in payload.items() if k != "dedupe_key"},
    }
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/stock_normalized_events?on_conflict=dedupe_key",
        headers={**HEADERS_SB, "Prefer": "resolution=ignore-duplicates,return=representation"},
        json=[row], timeout=15,
    )
    if r.status_code in (200, 201) and r.json():
        return r.json()[0]["id"]
    if r.status_code in (200, 201, 204):
        return None  # duplicate ignored
    print(f"  emit_event {event_type} {r.status_code}: {r.text[:200]}", file=sys.stderr)
    return None


# ============================================================
# Telegram (direct, no signal row — these are regime alerts, not setups)
# ============================================================

def send_macro_alert(text: str, dedupe_key: str) -> bool:
    """Send a Telegram message and log via stock_signals MACRO row for dedupe.

    Reusing stock_signals.dedupe_key so repeated runs in the same day don't
    spam. action=WATCH and direction=neutral keep it within the existing
    action constraint. status_v2='sent' on success.
    """
    if not BOT_TOKEN or not CHAT_ID:
        print("  Telegram env missing — skipping macro alert", file=sys.stderr)
        return False

    existing = requests.get(
        f"{SUPABASE_URL}/rest/v1/stock_signals",
        headers=HEADERS_SB,
        params={"dedupe_key": f"eq.{dedupe_key}", "select": "id,status_v2", "limit": "1"},
        timeout=10,
    )
    if existing.status_code == 200 and existing.json():
        if existing.json()[0].get("status_v2") == "sent":
            print(f"  macro alert dedupe — {dedupe_key} already sent")
            return False

    sig_row = {
        "ticker":           "MACRO",
        "fired_at":         datetime.now(timezone.utc).isoformat(),
        "direction":        "neutral",
        "confidence":       0.9,
        "horizon_days":     1,
        "thesis_summary":   text[:240],
        "model_version":    "macro-v1.0",
        "weight_at_time":   {"agents": ["macro_rates"]},
        "status":           "open",
        "action":           "WATCH",
        "score":            85,
        "score_breakdown":  {"items": [{"rule": "macro_regime_alert", "points": 85,
                                        "detail": dedupe_key}]},
        "evidence_summary": text[:240],
        "dedupe_key":       dedupe_key,
        "status_v2":        "candidate",
    }
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/stock_signals",
        headers={**HEADERS_SB, "Prefer": "resolution=ignore-duplicates,return=representation"},
        json=sig_row, timeout=15,
    )
    sig_id = None
    if r.status_code in (200, 201) and r.json():
        sig_id = r.json()[0]["id"]
    if sig_id is None:
        return False
    from telegram_dispatcher import send_and_log
    ok = send_and_log(sig_id, text, parse_mode="HTML")
    requests.patch(
        f"{SUPABASE_URL}/rest/v1/stock_signals?id=eq.{sig_id}",
        headers=HEADERS_SB,
        json={"status_v2": "sent" if ok else "dispatch_failed"},
        timeout=10,
    )
    return ok


# ============================================================
# Probe / detector functions
# ============================================================

def check_yield_regime() -> tuple[int, int]:
    """10Y level + 2s10s curve. Returns (events_emitted, alerts_sent)."""
    n_events = n_alerts = 0
    obs_10y = fred_observations("DGS10", limit=4)
    obs_2y  = fred_observations("DGS2",  limit=4)
    if not obs_10y or not obs_2y:
        return 0, 0
    y10 = _f(obs_10y[0])
    y10_prior = _f(obs_10y[1]) if len(obs_10y) > 1 else None
    y2  = _f(obs_2y[0])
    if y10 is None or y2 is None:
        return 0, 0
    obs_date = obs_10y[0].get("date", datetime.now(timezone.utc).date().isoformat())

    # 10Y crosses 5%
    if y10 >= YIELD_10Y_REGIME_HIGH and (y10_prior is None or y10_prior < YIELD_10Y_REGIME_HIGH):
        payload = {"value": y10, "prior": y10_prior, "threshold": YIELD_10Y_REGIME_HIGH,
                   "direction_prior": "short",
                   "dedupe_key": f"macro_yield_milestone_10y_5pct_{obs_date}"}
        if emit_event("yield_milestone", 4, payload,
                       event_subtype="10y_above_5pct",
                       event_at=obs_date + "T16:15:00+00:00"):
            n_events += 1
        if send_macro_alert(
            f"⚠️ <b>10Y Treasury crosses 5%</b>\n"
            f"Current: <b>{y10:.2f}%</b> (prior {y10_prior:.2f}%)\n"
            f"<i>Risk-off regime — tightens bullish thresholds across the AI cluster.</i>",
            dedupe_key=f"macro_yield_milestone_10y_5pct_{obs_date}",
        ):
            n_alerts += 1

    # 2s10s inversion
    spread = y10 - y2
    if spread < YIELD_CURVE_INVERSION_BPS:
        payload = {"y10": y10, "y2": y2, "spread_bps": round(spread * 100, 1),
                   "direction_prior": "short",
                   "dedupe_key": f"macro_yield_inversion_{obs_date}"}
        if emit_event("yield_milestone", 3, payload,
                       event_subtype="2s10s_inversion",
                       event_at=obs_date + "T16:15:00+00:00"):
            n_events += 1

    # Always emit a daily snapshot at non-critical severity for backtest provenance
    payload = {"y10": y10, "y2": y2, "spread_bps": round(spread * 100, 1),
               "direction_prior": "neutral",
               "dedupe_key": f"macro_yield_snapshot_{obs_date}"}
    if emit_event("yield_snapshot", 1, payload, event_at=obs_date + "T16:15:00+00:00"):
        n_events += 1

    print(f"  10Y={y10:.2f}%  2Y={y2:.2f}%  2s10s={spread*100:+.0f}bps")
    return n_events, n_alerts


def check_cpi_release() -> tuple[int, int]:
    """Detect a fresh CPI release (latest obs date < 5 days old + delta vs prior)."""
    obs = fred_observations("CPIAUCSL", limit=3)
    obs_core = fred_observations("CPILFESL", limit=3)
    if len(obs) < 2:
        return 0, 0
    latest = _f(obs[0]); prior = _f(obs[1])
    if latest is None or prior is None:
        return 0, 0
    obs_date = obs[0].get("date", "")
    try:
        d = datetime.fromisoformat(obs_date).date()
        days_old = (datetime.now(timezone.utc).date() - d).days
    except (ValueError, TypeError):
        return 0, 0
    if days_old > 35:
        return 0, 0
    mom_change = (latest - prior) / prior
    core_mom = None
    if len(obs_core) >= 2:
        core_latest = _f(obs_core[0]); core_prior = _f(obs_core[1])
        if core_latest is not None and core_prior is not None and core_prior != 0:
            core_mom = (core_latest - core_prior) / core_prior

    abs_chg = abs(mom_change)
    if abs_chg >= CPI_SURPRISE_HIGH:
        sev = 4; band = "high_surprise"
    elif abs_chg >= CPI_SURPRISE_MED:
        sev = 3; band = "med_surprise"
    else:
        sev = 2; band = "inline"

    direction = "short" if mom_change > CPI_SURPRISE_MED else \
                "long"  if mom_change < -CPI_SURPRISE_MED else "neutral"

    payload = {
        "headline_mom":    round(mom_change, 5),
        "core_mom":        round(core_mom, 5) if core_mom is not None else None,
        "obs_date":        obs_date,
        "level":           latest,
        "direction_prior": direction,
        "dedupe_key":      f"macro_cpi_release_{obs_date}",
    }
    n_events = 1 if emit_event("cpi_release", sev, payload,
                                 event_subtype=band,
                                 event_at=obs_date + "T12:30:00+00:00") else 0
    n_alerts = 0
    if sev == 4:
        if send_macro_alert(
            f"📊 <b>CPI surprise — {abs_chg*100:.2f}% MoM</b>\n"
            f"Headline: <b>{mom_change*100:+.2f}%</b>"
            f"{f' · Core: {core_mom*100:+.2f}%' if core_mom is not None else ''}\n"
            f"Release date: {obs_date}\n"
            f"<i>{'Hot print — pressures Fed pause' if mom_change > 0 else 'Cool print — disinflation tailwind'}.</i>",
            dedupe_key=f"macro_cpi_alert_{obs_date}",
        ):
            n_alerts = 1
    print(f"  CPI MoM={mom_change*100:+.2f}% ({obs_date}, {days_old}d old, sev={sev})")
    return n_events, n_alerts


def check_nfp_release() -> tuple[int, int]:
    """Non-farm payrolls — change in absolute jobs vs prior month."""
    obs = fred_observations("PAYEMS", limit=3)
    if len(obs) < 2:
        return 0, 0
    latest = _f(obs[0]); prior = _f(obs[1])
    if latest is None or prior is None:
        return 0, 0
    obs_date = obs[0].get("date", "")
    try:
        d = datetime.fromisoformat(obs_date).date()
        days_old = (datetime.now(timezone.utc).date() - d).days
    except (ValueError, TypeError):
        return 0, 0
    if days_old > 35:
        return 0, 0
    change = (latest - prior) * 1000  # PAYEMS is in thousands

    abs_chg = abs(change)
    if abs_chg >= NFP_SURPRISE_HIGH:
        sev = 4; band = "high_surprise"
    elif abs_chg >= NFP_SURPRISE_MED:
        sev = 3; band = "med_surprise"
    else:
        sev = 2; band = "inline"

    direction = "long" if change > NFP_SURPRISE_MED else \
                "short" if change < -NFP_SURPRISE_MED else "neutral"

    payload = {
        "jobs_added":      int(change),
        "obs_date":        obs_date,
        "level":           int(latest * 1000),
        "direction_prior": direction,
        "dedupe_key":      f"macro_nfp_release_{obs_date}",
    }
    n_events = 1 if emit_event("nfp_release", sev, payload,
                                 event_subtype=band,
                                 event_at=obs_date + "T12:30:00+00:00") else 0
    n_alerts = 0
    if sev == 4:
        arrow = "📈" if change > 0 else "📉"
        if send_macro_alert(
            f"{arrow} <b>NFP surprise — {int(change):+,} jobs</b>\n"
            f"Release date: {obs_date}\n"
            f"<i>{'Hot labor — Fed pause delayed' if change > 0 else 'Cool labor — cuts back on table'}.</i>",
            dedupe_key=f"macro_nfp_alert_{obs_date}",
        ):
            n_alerts = 1
    print(f"  NFP {change:+,.0f} jobs ({obs_date}, {days_old}d old, sev={sev})")
    return n_events, n_alerts


def check_fomc_rate_change() -> tuple[int, int]:
    """Detect Fed funds target rate change in last 7 days."""
    obs = fred_observations("DFEDTARU", limit=5)
    if len(obs) < 2:
        return 0, 0
    rates = [(o.get("date"), _f(o)) for o in obs if _f(o) is not None]
    if len(rates) < 2 or rates[0][0] == rates[1][0]:
        return 0, 0
    latest_date, latest_rate = rates[0]
    prior_rate = next((r for _, r in rates[1:] if r is not None and r != latest_rate), None)
    if prior_rate is None:
        return 0, 0
    delta = latest_rate - prior_rate
    direction = "short" if delta > 0 else "long" if delta < 0 else "neutral"
    payload = {
        "new_rate":        latest_rate,
        "prior_rate":      prior_rate,
        "delta_bps":       round(delta * 100, 0),
        "direction_prior": direction,
        "dedupe_key":      f"macro_fomc_{latest_date}",
    }
    n_events = 1 if emit_event("fomc_decision", 4, payload,
                                 event_subtype="hike" if delta > 0 else "cut" if delta < 0 else "hold",
                                 event_at=latest_date + "T18:00:00+00:00") else 0
    n_alerts = 0
    arrow = "📈" if delta > 0 else "📉"
    if send_macro_alert(
        f"{arrow} <b>Fed funds target → {latest_rate:.2f}%</b>\n"
        f"Change: <b>{delta*100:+.0f} bps</b> from {prior_rate:.2f}%\n"
        f"Date: {latest_date}\n"
        f"<i>{'Hawkish — risk-off' if delta > 0 else 'Dovish — risk-on' if delta < 0 else 'Hold'}.</i>",
        dedupe_key=f"macro_fomc_alert_{latest_date}",
    ):
        n_alerts = 1
    print(f"  FOMC: {prior_rate:.2f}% → {latest_rate:.2f}% ({delta*100:+.0f}bps, {latest_date})")
    return n_events, n_alerts


def check_vix_regime() -> tuple[int, int]:
    """VIX level vs stress / panic thresholds. Reads from stock_raw_prices."""
    r = requests.get(
        f"{SUPABASE_URL}/rest/v1/stock_raw_prices",
        headers=HEADERS_SB,
        params={"ticker": "eq.VIX", "select": "close,ts", "order": "ts.desc", "limit": "2"},
        timeout=10,
    )
    if r.status_code != 200 or not r.json():
        return 0, 0
    rows = r.json()
    vix = float(rows[0].get("close") or 0)
    vix_prior = float(rows[1].get("close")) if len(rows) > 1 and rows[1].get("close") else None
    obs_date = (rows[0].get("ts") or "")[:10]
    if vix <= 0:
        return 0, 0

    n_events = n_alerts = 0
    if vix >= VIX_PANIC:
        payload = {"vix": vix, "prior": vix_prior, "direction_prior": "short",
                   "threshold": VIX_PANIC, "dedupe_key": f"macro_vix_panic_{obs_date}"}
        if emit_event("vix_spike", 4, payload, event_subtype="panic",
                       event_at=obs_date + "T20:00:00+00:00"):
            n_events += 1
        if send_macro_alert(
            f"🚨 <b>VIX panic — {vix:.1f}</b>\n"
            f"Crossed {VIX_PANIC:.0f} threshold (prior {vix_prior:.1f if vix_prior else 0})\n"
            f"<i>All bullish signals downgraded one tier.</i>",
            dedupe_key=f"macro_vix_panic_alert_{obs_date}",
        ):
            n_alerts += 1
    elif vix >= VIX_STRESS:
        payload = {"vix": vix, "prior": vix_prior, "direction_prior": "short",
                   "threshold": VIX_STRESS, "dedupe_key": f"macro_vix_stress_{obs_date}"}
        if emit_event("vix_spike", 3, payload, event_subtype="stress",
                       event_at=obs_date + "T20:00:00+00:00"):
            n_events += 1
        # Only alert on a fresh crossing (prior was below threshold)
        if vix_prior is not None and vix_prior < VIX_STRESS:
            if send_macro_alert(
                f"⚠️ <b>VIX crosses {VIX_STRESS:.0f} — {vix:.1f}</b>\n"
                f"Prior session: {vix_prior:.1f}\n"
                f"<i>Risk-off regime active.</i>",
                dedupe_key=f"macro_vix_stress_alert_{obs_date}",
            ):
                n_alerts += 1
    print(f"  VIX={vix:.1f} ({obs_date}, prior={vix_prior})")
    return n_events, n_alerts


# ============================================================
# Main
# ============================================================

def main() -> int:
    started = time.time()
    run_id = job_run_start("macro_rates_agent")
    total_events = total_alerts = 0
    try:
        if not FRED_API_KEY:
            print("FRED_API_KEY not set — cannot proceed", file=sys.stderr)
            job_run_finish(run_id, "failed", 0, 0, err="missing FRED_API_KEY")
            return 1

        for label, fn in (
            ("yield_regime",   check_yield_regime),
            ("cpi_release",    check_cpi_release),
            ("nfp_release",    check_nfp_release),
            ("fomc_decision",  check_fomc_rate_change),
            ("vix_regime",     check_vix_regime),
        ):
            try:
                e, a = fn()
                total_events += e
                total_alerts += a
                print(f"  [{label}] events={e} alerts={a}")
            except Exception as exc:  # noqa: BLE001
                import traceback
                tb = traceback.format_exc()
                print(f"  {label} failed: {exc}\n{tb}", file=sys.stderr)
                dead_letter("macro_rates_agent", None, label, "probe_failure", tb)

        elapsed = time.time() - started
        print(f"DONE in {elapsed:.1f}s — events={total_events} alerts={total_alerts}")
        job_run_finish(run_id, "ok", total_events + total_alerts, total_events)
        return 0
    except Exception as exc:  # noqa: BLE001
        import traceback
        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        job_run_finish(run_id, "failed", 0, total_events, err=str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
