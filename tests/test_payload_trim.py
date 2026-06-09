"""Egress trim: thesis event fetches SELECT only the ~11 scoring-needed payload
fields via `payload->field` (PostgREST projects them to TOP LEVEL), then
_reassemble_payload rebuilds row['payload'] so scoring is unchanged.

The shim is the risk surface — a wrong shim would make scoring silently read
None for every payload field (wrong directions, wrong scores). These lock it.
"""
from __future__ import annotations

from thesis_agent import _reassemble_payload, _EVENT_PAYLOAD_FIELDS, _event_payload_select


def test_reassemble_rebuilds_payload_and_strips_top_level():
    row = {"id": 1, "event_type": "news_article", "ticker": "ETN", "severity": 3,
           "direction_prior": "long", "surprise_pct": 5.2, "headline": "Beat",
           "8k_items": None}
    out = _reassemble_payload(row)
    # payload rebuilt with correct values + types
    assert out["payload"]["direction_prior"] == "long"
    assert out["payload"]["surprise_pct"] == 5.2          # type preserved (number)
    assert out["payload"]["headline"] == "Beat"
    # absent/null projected field is OMITTED so .get(field, DEFAULT) falls back
    # to the default exactly as before the trim (not returned as None).
    assert "matched_keyword" not in out["payload"]
    assert "8k_items" not in out["payload"]               # was None → omitted
    assert out["payload"].get("matched_keyword", "X") == "X"
    # projected fields removed from top level (no leakage / key collision)
    for f in _EVENT_PAYLOAD_FIELDS:
        assert f not in out
    # base columns untouched
    assert out["event_type"] == "news_article" and out["ticker"] == "ETN"


def test_select_projects_only_needed_fields():
    sel = _event_payload_select()
    assert sel == ("payload->direction_prior,payload->surprise_pct,"
                   "payload->rel_strength_pct,payload->matched_keyword,"
                   "payload->filer_count,payload->amount,payload->accession_number,"
                   "payload->primary_doc_desc,payload->headline,payload->title,"
                   "payload->8k_items")
    assert "payload\b" not in sel  # not selecting the WHOLE payload


def test_reassembled_row_scores_identically_to_full_payload():
    # A row as it arrives from the trimmed projection (fields at top level) →
    # reassemble → must produce the same direction/score inputs as a row that
    # carried the full payload dict.
    from thesis_agent import signal_direction
    projected = {"id": 7, "event_type": "news_article", "event_subtype": "positive",
                 "ticker": "NVDA", "event_at": "2026-06-09T14:00:00Z",
                 "direction_prior": "long"}
    full = {"id": 7, "event_type": "news_article", "event_subtype": "positive",
            "ticker": "NVDA", "event_at": "2026-06-09T14:00:00Z",
            "payload": {"direction_prior": "long"}}
    assert signal_direction([_reassemble_payload(projected)]) == signal_direction([full])
