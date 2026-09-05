"""backfill_paper_trades.recompute_calibration must read the FULL closed set.

Found 2026-09-04 by an adversarial review of the maturity gate.

The recompute fetched with `"limit": "5000"`, no `order`, no pagination — while
`price_agent.recompute_rule_payoff` (the other authoritative writer) was
explicitly paginated with `order=id.asc` in FIX-1B for exactly this reason.

Four live rules exceed 5000 closed trades as of 2026-09-04:
    news_article:neutral:h7d  11186   :h1d 11109   :h15d 10237   :h30d 9292

so running the backfill overwrote n_observations with 5000 plus accuracy,
mean_realized_pct, profit_factor and every effective_* column, computed over an
arbitrary PostgREST-ordered subset — and then wrote is_mature/tier from it.
`agents/_maturity.py:5-7` names this script as one of three authoritative gate
writers, and its own comment at :319-321 claims the shared collapse means
"backfill cannot re-promote a rule the live path demoted" — which truncation
silently defeats, because it gates on a different population.
"""
from __future__ import annotations

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "agents"))

import backfill_paper_trades as bp


class _Resp:
    status_code = 200

    def __init__(self, rows):
        self._rows = rows

    def json(self):
        return self._rows


def _make_rows(n, start=0):
    # Alternating winners/losers so a truncated read would still look plausible.
    return [{"ticker": f"T{(start + i) % 7}",
             "entry_at": f"2026-0{(i % 9) + 1}-0{(i % 9) + 1}T00:00:00+00:00",
             "realized_return": 0.02 if (start + i) % 2 == 0 else -0.01,
             "correct": (start + i) % 2 == 0,
             "mfe_pct": 0.03, "mae_pct": -0.01,
             "target_hit": False, "stop_hit": False}
            for i in range(n)]


class TestRecomputeReadsEveryClosedTrade:
    def _install(self, monkeypatch, total):
        """Serve `total` rows through a paginating fake, recording each request."""
        calls = []

        def fake_get(url, headers=None, params=None, timeout=None):
            calls.append(dict(params or {}))
            if "stock_event_paper_trades" not in url:
                return _Resp([])
            limit = int(params.get("limit", 1000))
            offset = int(params.get("offset", 0))
            return _Resp(_make_rows(max(0, min(limit, total - offset)), start=offset))

        monkeypatch.setattr(bp.requests, "get", fake_get)
        seen = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            if "stock_rule_calibration" in url and json:
                seen.update(json[0])
            return _Resp([{"rule_key": "x"}])

        monkeypatch.setattr(bp.requests, "post", fake_post)
        return calls, seen

    def test_reads_past_the_5000_row_boundary(self, monkeypatch):
        # 11186 = the live size of news_article:neutral:h7d.
        calls, seen = self._install(monkeypatch, 11186)
        bp.recompute_calibration({"news_article:neutral:h7d"})
        fetched = sum(int(c.get("limit", 0)) for c in calls if "offset" in c) or None
        assert seen, "no calibration row was written"
        assert seen["n_observations"] == 11186, (
            f"truncated to {seen['n_observations']} — the full closed population "
            "must be read before the gate is recomputed")

    def test_requests_a_stable_order(self, monkeypatch):
        # Without an explicit order, PostgREST may return an arbitrary subset per
        # page, so pages can overlap or skip rows even when pagination exists.
        calls, _ = self._install(monkeypatch, 6000)
        bp.recompute_calibration({"news_article:neutral:h7d"})
        trade_calls = [c for c in calls if c.get("status") == "eq.closed"]
        assert trade_calls, "no closed-trade fetch was issued"
        assert all("order" in c for c in trade_calls), \
            "paginated reads must pin an explicit order"

    def test_small_rule_still_works(self, monkeypatch):
        _, seen = self._install(monkeypatch, 137)
        bp.recompute_calibration({"clinical_readout:completed:h7d"})
        assert seen["n_observations"] == 137
