"""D2.1.5 — ``coinbase_wire`` envelope construction matches D0.3 §2 contract.

Ticket 86b9zkpny (2026-05-17). Mirrors the D1.1.5
``test_kalshi_wire_envelope.py`` byte-for-byte in structure; the
envelope IS the bronze contract and applies symmetrically across the
Kalshi + Coinbase wire surfaces per D0.3 §2.

The 6 reserved fields (D0.3 §2):

| Field | Type | Source |
|---|---|---|
| ``_wire_recv_ts`` | ISO-8601 UTC w/ μs precision | Captured at frame ingress, BEFORE deserialization |
| ``_source`` | string | e.g. ``coinbase_ws`` (set by the caller) |
| ``_conn`` | string \\| null | WS connection id; null for REST snapshots |
| ``_channel`` | string \\| null | WS channel name |
| ``_collector_seq`` | int | Monotone-increasing per-collector-process sequence |
| ``_raw`` | string | The full raw wire payload as a string (NOT JSON-parsed) |

This test pins:
1. ``coinbase_wire`` exposes ``build_envelope`` (in ``coinbase_wire.ws_client``
   AND re-exported at the package top-level).
2. ``_wire_recv_ts`` is ISO-8601 UTC with microsecond precision and a
   trailing ``Z`` (matches D0.3 §2 example).
3. ``_raw`` is the **raw string** — bronze does NOT JSON-parse it.
4. The 6 reserved keys are present and ordered as specified.
5. ``_source`` is caller-controlled (defaults to ``coinbase_ws``).
6. The envelope is JSON-serializable as a single line (JSONL invariant).

If this test fails: the Coinbase envelope drifted from the D0.3 §2
contract — which is shared across wire sources for silver-ETL dispatch
symmetry.
"""
from __future__ import annotations

import json
import re
import sys
from unittest.mock import MagicMock


_HEAVY_MOD_NAMES = ("websockets",)
for _mod in _HEAVY_MOD_NAMES:
    sys.modules.setdefault(_mod, MagicMock())


SPEC_FIELDS = (
    "_wire_recv_ts",
    "_source",
    "_conn",
    "_channel",
    "_collector_seq",
    "_raw",
)

ISO8601_UTC_MICROSEC = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$"
)


# ─── 1. Envelope surface exists ──────────────────────────────────────────────


def test_envelope_callable_resolvable():
    """``build_envelope`` exists in ``coinbase_wire.ws_client`` (and is
    re-exported at the package top-level; that re-export is asserted by
    ``test_coinbase_wire_ws_client.py``)."""
    from coinbase_wire.ws_client import build_envelope
    assert callable(build_envelope)


# ─── 2. 6 reserved fields, exact names ───────────────────────────────────────


def test_envelope_has_exactly_six_reserved_fields():
    """``build_envelope(...)`` returns a dict with the 6 reserved fields
    from D0.3 §2 — no more, no fewer.
    """
    from coinbase_wire.ws_client import build_envelope
    env = build_envelope(
        raw='{"type":"match","product_id":"BTC-USD","trade_id":1,"price":"50000.00"}',
        source="coinbase_ws",
        channel="matches",
        conn="A",
        collector_seq=42,
    )
    actual = set(env.keys())
    expected = set(SPEC_FIELDS)
    missing = expected - actual
    extra = actual - expected
    assert not missing, f"envelope missing fields {missing}"
    assert not extra, (
        f"envelope has extra fields {extra}; D0.3 §2 locks the 6-field "
        "shape — extra fields belong in silver, not bronze."
    )


def test_envelope_field_order():
    """Field insertion order matches D0.3 §2 spec."""
    from coinbase_wire.ws_client import build_envelope
    env = build_envelope(
        raw="{}", source="coinbase_ws", channel="matches",
        conn="A", collector_seq=1,
    )
    actual_order = list(env.keys())
    assert actual_order == list(SPEC_FIELDS), (
        f"envelope field order {actual_order} != spec {list(SPEC_FIELDS)}."
    )


# ─── 3. _wire_recv_ts is ISO-8601 UTC with μs precision ──────────────────────


def test_wire_recv_ts_iso8601_utc_microsec():
    """``_wire_recv_ts`` matches ``YYYY-MM-DDTHH:MM:SS.uuuuuuZ``."""
    from coinbase_wire.ws_client import build_envelope
    env = build_envelope(
        raw="{}", source="coinbase_ws", channel=None, conn=None,
        collector_seq=0,
    )
    ts = env["_wire_recv_ts"]
    assert isinstance(ts, str)
    assert ISO8601_UTC_MICROSEC.match(ts), (
        f"_wire_recv_ts={ts!r} does not match D0.3 §2 spec "
        "ISO-8601 UTC w/ μs precision."
    )


# ─── 4. _raw preserved verbatim ──────────────────────────────────────────────


def test_raw_field_preserves_bytes_verbatim():
    """``_raw`` is the literal string passed in, unmodified."""
    from coinbase_wire.ws_client import build_envelope
    raw_input = '{"type":"ticker","product_id":"BTC-USD","price":"50000.00","sequence":12345}'
    env = build_envelope(
        raw=raw_input, source="coinbase_ws", channel="ticker",
        conn="A", collector_seq=100,
    )
    assert env["_raw"] == raw_input
    assert isinstance(env["_raw"], str)


# ─── 5. Other reserved fields carry the right types ──────────────────────────


def test_source_conn_channel_collector_seq_types():
    """``_source`` is str; ``_collector_seq`` is int; ``_conn`` and
    ``_channel`` are str or None (D0.3 §2 allows null for REST snapshots).
    """
    from coinbase_wire.ws_client import build_envelope
    env_full = build_envelope(
        raw="{}", source="coinbase_ws", channel="ticker", conn="A",
        collector_seq=5,
    )
    assert isinstance(env_full["_source"], str)
    assert isinstance(env_full["_channel"], str)
    assert isinstance(env_full["_conn"], str)
    assert isinstance(env_full["_collector_seq"], int)

    env_rest = build_envelope(
        raw="{}", source="coinbase_rest", channel=None, conn=None,
        collector_seq=6,
    )
    assert env_rest["_conn"] is None
    assert env_rest["_channel"] is None


# ─── 6. Envelope is JSON-serializable as a single line ───────────────────────


def test_envelope_is_jsonl_round_trippable():
    """``json.dumps(build_envelope(...))`` is single-line JSONL."""
    from coinbase_wire.ws_client import build_envelope
    env = build_envelope(
        raw='{"a":1}', source="coinbase_ws", channel="heartbeat",
        conn="A", collector_seq=99,
    )
    line = json.dumps(env)
    assert "\n" not in line
    recovered = json.loads(line)
    assert recovered == env
