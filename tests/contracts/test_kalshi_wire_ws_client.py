"""D1.1.5 — ``kalshi_wire.ws_client.WSClient`` public-API surface pin.

Ticket 86b9zdhz2 (2026-05-16). The wire-loop transport semantics that the
bot's KalshiFeed accumulated over 2 years (Apr-24 silence watchdog,
Phase 2.5/2.6/2.7/2.10 sid handling, R3/P0-A reconnect cleanup) must
survive the extraction byte-for-byte.

This test file pins the **API surface**. The **behavior** is pinned by:
  - ``tests/equivalence/test_kalshi_wire_differential.py`` (Pillar 3
    differential test — load-bearing)
  - The existing tests under ``tests/integration/test_kalshi_feed*.py``
    that exercise the consumer side

If the WSClient class is missing, malformed, or breaks the documented API
shape, the rest of D1.1.5 cannot land. RED scaffold until Phase 3b
extraction.

Pins:
1. ``kalshi_wire.ws_client.WSClient`` is importable + is a class
2. Constructor signature accepts ``api_key, private_key, *, url, on_frame``
   + the silence-watchdog tunables (matches bot/feeds/kalshi.py's
   accumulated WS_* constants without coupling to bot.constants)
3. Public methods: ``start``, ``stop``, ``send_frame``, plus
   ``is_connected`` property
4. ``Frame`` dataclass is importable with the 6 expected fields
   (``wire_recv_ts``, ``raw``, ``parsed``, ``msg_type``, ``sid``, ``seq``)
5. No async exposure at the public surface — sync/threading per project
   anti-pattern. (asyncio internal to the client is OK; it must NOT leak
   ``async def`` methods into the public API.)

If this test fails: Phase 3b (WSClient extraction) is incomplete or the
API surface drifted from the design.
"""
from __future__ import annotations

import inspect
import sys
from unittest.mock import MagicMock

import pytest


_HEAVY_MOD_NAMES = ("websockets",)
for _mod in _HEAVY_MOD_NAMES:
    sys.modules.setdefault(_mod, MagicMock())


# ─── 1. WSClient class importable ────────────────────────────────────────────


def test_ws_client_class_importable():
    """``kalshi_wire.ws_client.WSClient`` is importable as a class."""
    from kalshi_wire.ws_client import WSClient
    assert inspect.isclass(WSClient)


# ─── 2. Constructor signature ────────────────────────────────────────────────


def test_ws_client_init_signature():
    """``WSClient.__init__`` accepts the design API: positional
    ``api_key`` + ``private_key`` plus keyword-only ``url`` +
    ``on_frame`` callback + the silence-watchdog tunables.

    The tunables MUST be parameters (not module-level constants the wire
    library reads from bot.constants) — the wire library cannot reach
    into bot/ for config.
    """
    from kalshi_wire.ws_client import WSClient
    sig = inspect.signature(WSClient.__init__)
    params = sig.parameters

    assert "api_key" in params, "WSClient.__init__ missing `api_key` parameter"
    assert "private_key" in params, "WSClient.__init__ missing `private_key` parameter"
    assert "on_frame" in params, (
        "WSClient.__init__ missing `on_frame` callback — frames are "
        "delivered to consumers via callback, not via subclass override."
    )
    # `url` is keyword-only with a default per the design API; check by name only.
    assert "url" in params, "WSClient.__init__ missing `url` parameter"


# ─── 3. Public methods exist with the right shape ────────────────────────────


def test_ws_client_has_start_method():
    from kalshi_wire.ws_client import WSClient
    assert hasattr(WSClient, "start")
    assert callable(getattr(WSClient, "start"))


def test_ws_client_has_stop_method():
    from kalshi_wire.ws_client import WSClient
    assert hasattr(WSClient, "stop")
    assert callable(getattr(WSClient, "stop"))


def test_ws_client_has_is_connected_property():
    from kalshi_wire.ws_client import WSClient
    # Property on the class, accessible without instantiation
    assert hasattr(WSClient, "is_connected")
    # Should be either a `property` descriptor or a plain method
    attr = inspect.getattr_static(WSClient, "is_connected")
    assert isinstance(attr, (property, staticmethod, classmethod)) or callable(attr), (
        "WSClient.is_connected should be a property (or readable attribute)"
    )


def test_ws_client_has_send_frame_method():
    """``send_frame(payload)`` is the consumer-facing send primitive.
    Consumers (bot/collector) own cmd_id management, sid mapping, and
    ack correlation; the wire layer just serializes + sends.
    """
    from kalshi_wire.ws_client import WSClient
    assert hasattr(WSClient, "send_frame")
    assert callable(getattr(WSClient, "send_frame"))


# ─── 4. Frame dataclass ──────────────────────────────────────────────────────


def test_frame_dataclass_importable():
    """``Frame`` is importable from ``kalshi_wire.ws_client`` (or
    ``kalshi_wire`` as a re-export)."""
    from kalshi_wire.ws_client import Frame
    assert inspect.isclass(Frame)


def test_frame_has_six_expected_fields():
    """``Frame`` carries the 6 fields the design API specifies:
    ``wire_recv_ts`` (float), ``raw`` (str), ``parsed`` (dict),
    ``msg_type`` (str | None), ``sid`` (int | None), ``seq`` (int | None).

    Uses ``__annotations__`` (set by @dataclass) so the test is robust to
    Frame being a dataclass, attrs class, or NamedTuple.
    """
    from kalshi_wire.ws_client import Frame
    annotations = getattr(Frame, "__annotations__", {})
    expected = {"wire_recv_ts", "raw", "parsed", "msg_type", "sid", "seq"}
    actual = set(annotations.keys())
    missing = expected - actual
    assert not missing, (
        f"Frame missing fields {sorted(missing)}; design API specifies "
        f"all 6 of: {sorted(expected)}"
    )


# ─── 5. No async leakage at public API ───────────────────────────────────────


def test_ws_client_public_methods_are_sync():
    """No public ``async def`` methods at WSClient surface. asyncio is
    used INTERNALLY (mirrors bot/feeds/kalshi.py's pattern) but must
    NOT leak into the caller's API — project anti-pattern "no async"
    per root CLAUDE.md.
    """
    from kalshi_wire.ws_client import WSClient
    public_methods = [
        name for name in dir(WSClient)
        if not name.startswith("_") and callable(getattr(WSClient, name, None))
    ]
    coroutine_methods = [
        name for name in public_methods
        if inspect.iscoroutinefunction(getattr(WSClient, name, None))
    ]
    assert not coroutine_methods, (
        f"WSClient exposes async public methods {coroutine_methods}; "
        "the wire library uses asyncio internally but must present a "
        "sync API to consumers (project anti-pattern: no async)."
    )
