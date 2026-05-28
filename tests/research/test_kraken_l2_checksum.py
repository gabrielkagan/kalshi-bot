"""Kraken v2 L2 book CRC32 checksum verification — B2a follow-up (ticket 86ba5xfyb).

The offline RMSE harness reconstructs Kraken's book by applying incremental
``update`` frames to a ``snapshot``. A dropped/out-of-order bronze frame
silently desyncs that book and biases the 60s-average RTI with no signal.
Kraken v2 ships a CRC32 ``checksum`` over the top-10 book on every frame
precisely so a consumer can detect this.

Golden vector: a REAL recorded Kraken HYPE/USD snapshot (S3 bronze,
2026-05-28 hour=17 chunk) with its recorded checksum ``3129294453`` — a
NON-CIRCULAR anchor (the expected value comes from Kraken, not from our own
implementation).

Verified algorithm: top-10 asks (ascending) then top-10 bids (descending);
per level concat ``price.replace('.','').lstrip('0')`` +
``qty.replace('.','').lstrip('0')``; ``zlib.crc32(joined.encode())``. The
price/qty MUST be the original precision-preserving string tokens — a
float-parsed value drops trailing zeros (``"59.50"`` -> ``59.5``) and breaks
the checksum.
"""
from __future__ import annotations

import pytest

from scripts.research import synthetic_rti_rmse as h


# Real Kraken HYPE/USD snapshot top-10 (price_str, qty_str) — precision-
# preserving strings exactly as recorded in bronze ``_raw``.
_GOLDEN_ASKS = [
    ("59.54", "14.85425663"), ("59.55", "34.02374657"), ("59.56", "22.74721667"),
    ("59.57", "198.29546247"), ("59.58", "19.32312643"), ("59.59", "366.42525477"),
    ("59.60", "0.74071190"), ("59.61", "118.24573039"), ("59.62", "17.36243968"),
    ("59.63", "581.47155676"),
]
_GOLDEN_BIDS = [
    ("59.53", "64.36589177"), ("59.52", "51.91300425"), ("59.51", "281.16737860"),
    ("59.50", "395.65040025"), ("59.49", "0.30709338"), ("59.48", "88.20610784"),
    ("59.47", "528.71836576"), ("59.45", "267.07926935"), ("59.44", "118.95157610"),
    ("59.43", "160.49670963"),
]
_GOLDEN_CHECKSUM = 3129294453


def test_kraken_book_checksum_reproduces_real_recorded_value():
    """NON-CIRCULAR: our CRC32 must reproduce Kraken's own recorded checksum."""
    assert h.kraken_book_checksum(_GOLDEN_ASKS, _GOLDEN_BIDS) == _GOLDEN_CHECKSUM


def test_kraken_book_checksum_uses_only_top_10():
    """Levels beyond the top 10 must not change the checksum (Kraken spec)."""
    asks = _GOLDEN_ASKS + [("59.64", "1.0"), ("59.65", "2.0")]
    bids = _GOLDEN_BIDS + [("59.42", "1.0"), ("59.41", "2.0")]
    assert h.kraken_book_checksum(asks, bids) == _GOLDEN_CHECKSUM


def test_kraken_fmt_strips_leading_zeros_keeps_trailing():
    """Decimal removed; leading zeros stripped; trailing/internal zeros kept."""
    assert h._kraken_fmt("59.50") == "5950"           # trailing zero kept
    assert h._kraken_fmt("0.30709338") == "30709338"  # leading zero stripped
    assert h._kraken_fmt("0.74071190") == "74071190"  # lead strip, trail keep
    assert h._kraken_fmt("100.00") == "10000"
    assert h._kraken_fmt("0.00010") == "10"           # only leading zeros go


# ── Desync detection in the 60s replay ─────────────────────────────────────


def _ksnap(bids, asks, checksum):
    """Kraken v2 `book` snapshot frame (string prices, as parse_float=str
    yields from bronze _raw)."""
    return {
        "channel": "book", "type": "snapshot",
        "data": [{
            "symbol": "BTC/USD",
            "bids": [{"price": p, "qty": q} for p, q in bids],
            "asks": [{"price": p, "qty": q} for p, q in asks],
            "checksum": checksum,
        }],
    }


def _kupd(changes, checksum):
    """Kraken v2 `book` update frame. ``changes`` = [(side, price, qty), ...]."""
    return {
        "channel": "book", "type": "update",
        "data": [{
            "symbol": "BTC/USD",
            "bids": [{"price": p, "qty": q} for s, p, q in changes if s == "bid"],
            "asks": [{"price": p, "qty": q} for s, p, q in changes if s == "ask"],
            "checksum": checksum,
        }],
    }


def test_kraken_dropped_update_desyncs_and_drops_venue_from_window():
    """A dropped bronze diff must be CAUGHT by checksum mismatch and the
    Kraken venue dropped from the window — not silently fed a corrupt book.

    Sequence: snapshot (bid 100.50@2 / ask 100.60@3), U1 (bid->5), U2 (ask->7),
    with checksums computed for each resulting book. Clean replay stays in sync
    (Kraken contributes mid 100.55). Drop U1 → U2's checksum no longer matches
    the (still bid@2) book → desync from the window start → Kraken dropped →
    no venue contributes → None."""
    close_ts = 1_000_000.0
    params = {
        "venues": ("kraken",),
        "spacing": 1000.0,  # depth < spacing → RTI collapses to the mid
        "deviation_from_mid_pct": 1.0,
        "potentially_erroneous_pct": 10.0,
        "retrieval_lag_threshold_seconds": 9999.0,
    }
    snap_b, snap_a = [("100.50", "2.00000")], [("100.60", "3.00000")]
    u1_b, u1_a = [("100.50", "5.00000")], snap_a            # bid resized 2->5
    u2_b, u2_a = u1_b, [("100.60", "7.00000")]              # ask resized 3->7
    snap = _ksnap(snap_b, snap_a, h.kraken_book_checksum(snap_a, snap_b))
    u1 = _kupd([("bid", "100.50", "5.00000")], h.kraken_book_checksum(u1_a, u1_b))
    u2 = _kupd([("ask", "100.60", "7.00000")], h.kraken_book_checksum(u2_a, u2_b))

    clean = {"kraken": [
        (close_ts - 100, snap), (close_ts - 90, u1), (close_ts - 59, u2),
    ]}
    dropped = {"kraken": [
        (close_ts - 100, snap), (close_ts - 59, u2),  # U1 lost
    ]}

    assert h.synthetic_rti_60s_average(clean, "BTC", close_ts, params) == pytest.approx(100.55, abs=1e-6)
    assert h.synthetic_rti_60s_average(dropped, "BTC", close_ts, params) is None


def test_kraken_frame_without_checksum_is_not_verified():
    """Frames lacking a checksum field (e.g. test fixtures, control-adjacent)
    skip verification — no false desync, backward-compatible with existing
    60s-average tests."""
    close_ts = 1_000_000.0
    params = {
        "venues": ("kraken",), "spacing": 1000.0, "deviation_from_mid_pct": 1.0,
        "potentially_erroneous_pct": 10.0, "retrieval_lag_threshold_seconds": 9999.0,
    }
    snap = {
        "channel": "book", "type": "snapshot",
        "data": [{"symbol": "BTC/USD",
                  "bids": [{"price": "100.50", "qty": "2.0"}],
                  "asks": [{"price": "100.60", "qty": "3.0"}]}],  # no checksum
    }
    frames = {"kraken": [(close_ts - 100, snap)]}
    assert h.synthetic_rti_60s_average(frames, "BTC", close_ts, params) == pytest.approx(100.55, abs=1e-6)
