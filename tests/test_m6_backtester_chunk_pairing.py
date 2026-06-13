"""M6.1 — backtester forecast_audit must pair to the right signal per chunk.

The old code flattened all chunk-insert ids into one list and zip()'d against
the full signals list. A chunk that returned fewer ids than sent (partial
insert) shifted every subsequent (id, signal) pairing, mislabelling later audit
rows with the WRONG signal's outcome. _insert_signals_chunked pairs per chunk
and drops a short chunk's rows instead of shifting.
"""
from __future__ import annotations

from backtester import _insert_signals_chunked


def test_partial_chunk_does_not_shift_later_pairings():
    payload = [{"p": i} for i in range(7)]
    signals = [{"sig": i} for i in range(7)]

    # chunk size 3: chunk0 ok (ids 100,101,102), chunk1 PARTIAL (2 ids for 3 sent),
    # chunk2 ok (ids 300). chunk1 must be dropped, not shift chunk2.
    def poster(c):
        n = len(c)
        first = c[0]["p"]
        if first == 3:            # the partial middle chunk
            return [{"id": 200}, {"id": 201}]   # only 2 of 3
        return [{"id": 100 + first + j} for j in range(n)]

    pairs = _insert_signals_chunked(payload, signals, poster, chunk=3)
    # chunk0: sigs 0,1,2 ; chunk2: sig 6 — chunk1 (sigs 3,4,5) dropped entirely
    assert [s["sig"] for _, s in pairs] == [0, 1, 2, 6]
    # ids correctly attached (chunk2's sig 6 → id 100+6)
    assert dict((s["sig"], i) for i, s in pairs)[6] == 106


def test_all_ok_pairs_one_to_one():
    payload = [{"p": i} for i in range(4)]
    signals = [{"sig": i} for i in range(4)]
    pairs = _insert_signals_chunked(payload, signals,
                                    lambda c: [{"id": x["p"]} for x in c], chunk=2)
    assert [(i, s["sig"]) for i, s in pairs] == [(0, 0), (1, 1), (2, 2), (3, 3)]


def test_empty_chunk_result_dropped():
    payload = [{"p": 0}]
    pairs = _insert_signals_chunked(payload, [{"sig": 0}], lambda c: None)
    assert pairs == []
