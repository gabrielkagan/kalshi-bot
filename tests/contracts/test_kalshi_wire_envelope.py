"""D1.1.5 — ``kalshi_wire`` envelope construction matches D0.3 §2 contract.

Ticket 86b9zdhz2 (2026-05-16). The envelope IS the bronze contract.
Per D0.3 §2 (``kb/decisions/data-corpus-architecture.md``) each bronze
line is a JSONL record with EXACTLY 6 reserved fields:

| Field | Type | Source |
|---|---|---|
| ``_wire_recv_ts`` | ISO-8601 UTC w/ μs precision | Captured at frame ingress, BEFORE deserialization |
| ``_source`` | string | e.g. ``kalshi_ws``, ``coinbase_ws``, ``nws_hrrr`` |
| ``_conn`` | string \\| null | WS connection id (A/B/...); null for REST snapshots |
| ``_channel`` | string \\| null | WS channel (``orderbook_delta`` / ``trade`` / ...) |
| ``_collector_seq`` | int | Monotone-increasing per-collector-process sequence |
| ``_raw`` | string | The full raw wire payload as a string (NOT JSON-parsed) |

This test pins:
1. ``kalshi_wire`` exposes an envelope-construction surface (function or
   method) that emits the 6 reserved fields in the spec-required shape.
2. ``_wire_recv_ts`` is ISO-8601 UTC with microsecond precision and a
   trailing ``Z`` (matches D0.3 §2 example).
3. ``_raw`` is the **raw string** — bronze does NOT JSON-parse it (the
   advisor's "store all raw" architectural principle from §0).
4. The 6 reserved keys are present and ordered as specified (Python dict
   ordering is insertion-ordered post-3.7 so this is testable).

If this test fails:
- Intentional schema rev: this is a BRONZE schema change, which D0.3 §2
  explicitly forbids ("bronze cannot rev; silver/gold do"). Re-read the
  AMENDMENT before proceeding.
- The envelope shape drifted: lock it back to the D0.3 §2 contract;
  silver QA reconciliation depends on the shape staying stable.
"""
from __future__ import annotations

import json
import re
import sys
from unittest.mock import MagicMock

import pytest


_HEAVY_MOD_NAMES = ("websockets",)
for _mod in _HEAVY_MOD_NAMES:
    sys.modules.setdefault(_mod, MagicMock())


# Per D0.3 §2 — exact field names, exact order.
SPEC_FIELDS = (
    "_wire_recv_ts",
    "_source",
    "_conn",
    "_channel",
    "_collector_seq",
    "_raw",
)

# ISO-8601 UTC w/ μs precision per D0.3 §2 example
# ``"2026-05-15T18:35:12.034501Z"`` — 6 fractional digits + literal ``Z``.
ISO8601_UTC_MICROSEC = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$"
)


def _resolve_envelope_callable():
    """Locate the envelope-construction callable.

    Per pickup prompt, envelope MAY live in ``kalshi_wire.envelope`` OR
    fold into ``kalshi_wire.ws_client``. Try both. Returns ``(callable,
    location_str)`` for diagnostic clarity; raises pytest.skip if neither
    exposes the surface yet.

    Expected signature:
      build_envelope(raw: str, *, source: str, channel: str | None,
                     conn: str | None, collector_seq: int) -> dict
    """
    try:
        from kalshi_wire import envelope as env_mod
        if hasattr(env_mod, "build_envelope"):
            return env_mod.build_envelope, "kalshi_wire.envelope.build_envelope"
    except ImportError:
        pass
    try:
        from kalshi_wire import ws_client as ws_mod
        if hasattr(ws_mod, "build_envelope"):
            return ws_mod.build_envelope, "kalshi_wire.ws_client.build_envelope"
    except ImportError:
        pass
    pytest.fail(
        "kalshi_wire does not expose `build_envelope` in either "
        "`kalshi_wire.envelope` or `kalshi_wire.ws_client`. D1.1.5 must "
        "ship one of them per D0.3 §2 6-field contract."
    )


# ─── 1. Envelope surface exists ──────────────────────────────────────────────


def test_envelope_callable_resolvable():
    """``build_envelope`` exists in either ``kalshi_wire.envelope`` or
    ``kalshi_wire.ws_client`` (pickup prompt allows either)."""
    fn, _ = _resolve_envelope_callable()
    assert callable(fn)


# ─── 2. 6 reserved fields, exact names ───────────────────────────────────────


def test_envelope_has_exactly_six_reserved_fields():
    """``build_envelope(...)`` returns a dict with the 6 reserved fields
    from D0.3 §2 — no more, no fewer. Extra fields would dilute the
    bronze contract and confuse silver ETL dispatch.
    """
    fn, location = _resolve_envelope_callable()
    env = fn(
        raw='{"type":"orderbook_delta","msg":{"market_ticker":"X"}}',
        source="kalshi_ws",
        channel="orderbook_delta",
        conn="A",
        collector_seq=42,
    )
    actual = set(env.keys())
    expected = set(SPEC_FIELDS)
    missing = expected - actual
    extra = actual - expected
    assert not missing, f"{location} envelope missing fields {missing}"
    assert not extra, (
        f"{location} envelope has extra fields {extra}; D0.3 §2 locks "
        "the 6-field shape — extra fields belong in silver, not bronze."
    )


def test_envelope_field_order():
    """Field insertion order matches D0.3 §2 spec. JSON-serializing
    preserves insertion order; downstream JSONL.zst tape readers may
    depend on consistent column layout for column-store joins.
    """
    fn, location = _resolve_envelope_callable()
    env = fn(
        raw="{}", source="kalshi_ws", channel="trade", conn="B", collector_seq=1,
    )
    actual_order = list(env.keys())
    assert actual_order == list(SPEC_FIELDS), (
        f"{location} envelope field order {actual_order} != spec "
        f"{list(SPEC_FIELDS)}. D0.3 §2 lists fields in this order."
    )


# ─── 3. _wire_recv_ts is ISO-8601 UTC with μs precision ──────────────────────


def test_wire_recv_ts_iso8601_utc_microsec():
    """``_wire_recv_ts`` matches the D0.3 §2 example regex
    ``YYYY-MM-DDTHH:MM:SS.uuuuuuZ`` — 6 fractional digits + trailing ``Z``.
    """
    fn, location = _resolve_envelope_callable()
    env = fn(
        raw="{}", source="kalshi_ws", channel=None, conn=None, collector_seq=0,
    )
    ts = env["_wire_recv_ts"]
    assert isinstance(ts, str), (
        f"{location}: _wire_recv_ts must be str, got {type(ts).__name__}"
    )
    assert ISO8601_UTC_MICROSEC.match(ts), (
        f"{location}: _wire_recv_ts={ts!r} does not match D0.3 §2 spec "
        "ISO-8601 UTC w/ μs precision (`YYYY-MM-DDTHH:MM:SS.uuuuuuZ`). "
        "Specifically: 6 fractional-second digits required + trailing 'Z'."
    )


# ─── 4. _raw is the raw string — bronze does NOT JSON-parse ──────────────────


def test_raw_field_preserves_bytes_verbatim():
    """``_raw`` is the **literal string** passed in, unmodified. Bronze
    captures bytes verbatim per the D0.3 §0 operator principle ("store
    all raw data, transform downstream with dbt"). Decoding/normalizing
    happens at silver.
    """
    fn, location = _resolve_envelope_callable()
    raw_input = '{"type":"orderbook_snapshot","msg":{"market_ticker":"KX","yes_dollars_fp":[["0.96","54"]]}}'
    env = fn(
        raw=raw_input, source="kalshi_ws", channel="orderbook_delta",
        conn="A", collector_seq=100,
    )
    assert env["_raw"] == raw_input, (
        f"{location}: _raw was modified — bronze must capture verbatim. "
        f"got {env['_raw']!r}, expected {raw_input!r}"
    )
    assert isinstance(env["_raw"], str), (
        f"{location}: _raw must be str (not dict — bronze does NOT JSON-parse)."
    )


# ─── 5. Other reserved fields carry the right types ──────────────────────────


def test_source_conn_channel_collector_seq_types():
    """``_source`` is str; ``_collector_seq`` is int; ``_conn`` and
    ``_channel`` are str or None (D0.3 §2 allows null for REST snapshots).
    """
    fn, location = _resolve_envelope_callable()
    env_full = fn(
        raw="{}", source="kalshi_ws", channel="trade", conn="A", collector_seq=5,
    )
    assert isinstance(env_full["_source"], str), f"{location}: _source must be str"
    assert isinstance(env_full["_channel"], str), f"{location}: _channel must be str when non-null"
    assert isinstance(env_full["_conn"], str), f"{location}: _conn must be str when non-null"
    assert isinstance(env_full["_collector_seq"], int), f"{location}: _collector_seq must be int"

    # REST-snapshot shape — conn/channel allowed to be None.
    env_rest = fn(
        raw="{}", source="kalshi_rest", channel=None, conn=None, collector_seq=6,
    )
    assert env_rest["_conn"] is None, (
        f"{location}: _conn=None must round-trip as None (REST snapshot shape)"
    )
    assert env_rest["_channel"] is None, (
        f"{location}: _channel=None must round-trip as None (REST snapshot shape)"
    )


# ─── 6. Envelope is JSON-serializable as a single line (JSONL invariant) ────


def test_envelope_is_jsonl_round_trippable():
    """``json.dumps(build_envelope(...))`` produces a single line that
    ``json.loads`` recovers to an equal dict — D0.3 §2 invariant for
    JSONL.zst bronze tape.
    """
    fn, _ = _resolve_envelope_callable()
    env = fn(
        raw='{"a":1}', source="kalshi_ws", channel="trade",
        conn="A", collector_seq=99,
    )
    line = json.dumps(env)
    assert "\n" not in line, "envelope JSONL line must not contain embedded newlines"
    recovered = json.loads(line)
    assert recovered == env
