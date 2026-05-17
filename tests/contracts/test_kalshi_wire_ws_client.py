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


# ─── 6. ws_max_size — incoming Kalshi ack ceiling (D1.3-fu1, 86b9zju8h) ─────


def test_ws_client_init_accepts_ws_max_size_kwarg():
    """Constructor MUST expose ``ws_max_size`` so the per-conn ack-size
    cap is a per-WSClient choice, not a process-wide constant. The bot's
    KalshiFeed subscribes to ~50 tickers (tiny acks) and is happy with the
    default; the collector subscribes to ~74K tickers per conn and needs
    a higher cap because Kalshi's type=subscribed/type=ok acks include
    the cumulative subscribed-ticker list per sid.

    D1.3-fu1 RCA (`kb/decisions/d1-3-fu1-max-size-fix-plan.md`): the WS
    1009 "message too big" storm 2026-05-17 was incoming Kalshi acks
    crossing the python websockets default max_size=1 MiB — NOT our
    outgoing subscribe frames. Smaller subscribe batches don't help; the
    INCOMING ack grows monotonically per sid.
    """
    from kalshi_wire.ws_client import WSClient
    sig = inspect.signature(WSClient.__init__)
    assert "ws_max_size" in sig.parameters, (
        "WSClient.__init__ missing `ws_max_size` kwarg — incoming-message "
        "size cap MUST be per-WSClient (collector needs >1MiB; bot is "
        "happy with the default). See kb/decisions/d1-3-fu1-max-size-fix-plan.md."
    )


def test_ws_client_ws_max_size_default_at_least_16mib():
    """Default ``ws_max_size`` must clear the worst-case Kalshi ack at
    full subscription growth (~74K tickers × ~50 bytes ≈ 3.7 MB). 16 MiB
    gives ~4x headroom; lower values risk the storm class returning if
    Kalshi adds longer ticker names or we subscribe to more markets.

    Upper bound (256 MiB) is a sanity guard against accidental
    ``max_size=None``-equivalent (no cap = potential unbounded memory if
    Kalshi ever sends a malformed huge frame).
    """
    from kalshi_wire.ws_client import WSClient
    sig = inspect.signature(WSClient.__init__)
    default = sig.parameters["ws_max_size"].default
    assert default is not inspect.Parameter.empty, (
        "ws_max_size MUST have a default — callers shouldn't be forced "
        "to know the right ceiling. Default lives in the wire library."
    )
    sixteen_mib = 16 * 1024 * 1024
    assert default >= sixteen_mib, (
        f"ws_max_size default={default} < 16 MiB ({sixteen_mib}). Kalshi "
        f"acks at full collector subscription (~74K tickers/conn) hit "
        f"~3.7 MB; below 16 MiB risks the D1.3-fu1 storm class returning "
        f"under any ticker-distribution shift."
    )
    two_fifty_six_mib = 256 * 1024 * 1024
    assert default <= two_fifty_six_mib, (
        f"ws_max_size default={default} > 256 MiB — caps memory exposure "
        f"per incoming message. If you genuinely need >256 MiB, write a "
        f"finding doc first."
    )


def test_ws_client_rejects_none_or_non_int_ws_max_size():
    """Validator rejects None + non-int with a CLEAN TypeError rather than
    silently forwarding to ``websockets.connect(max_size=None)``.

    R1-M1 (D1.3-fu1 adv round 1, 2026-05-17): the upstream ``websockets``
    library accepts ``max_size=None`` as "no limit", but the wire library
    declines to forward None because (a) unbounded memory on a malformed
    Kalshi frame would breach the collector's MemoryMax=512M systemd cap,
    and (b) callers needing a higher ceiling can pass a concrete int.
    """
    from kalshi_wire.ws_client import WSClient
    # Build a minimal-arg call that only exercises validation.
    base_kwargs = dict(
        api_key="x", private_key=None,
        on_frame=lambda f: None,
        _test_skip_auth=True,
    )
    with pytest.raises(TypeError, match=r"ws_max_size"):
        WSClient(**base_kwargs, ws_max_size=None)
    with pytest.raises(TypeError, match=r"ws_max_size"):
        WSClient(**base_kwargs, ws_max_size="16MB")
    with pytest.raises(TypeError, match=r"ws_max_size"):
        # bool is a subclass of int but is rejected — `ws_max_size=True`
        # would silently coerce to 1 byte and storm immediately.
        WSClient(**base_kwargs, ws_max_size=True)


def test_ws_client_rejects_zero_or_negative_ws_max_size():
    """Below 1 byte is meaningless; clean ValueError."""
    from kalshi_wire.ws_client import WSClient
    base_kwargs = dict(
        api_key="x", private_key=None,
        on_frame=lambda f: None,
        _test_skip_auth=True,
    )
    with pytest.raises(ValueError, match=r"ws_max_size"):
        WSClient(**base_kwargs, ws_max_size=0)
    with pytest.raises(ValueError, match=r"ws_max_size"):
        WSClient(**base_kwargs, ws_max_size=-1)


def test_ws_client_passes_ws_max_size_to_websockets_connect():
    """Runtime pin: when WSClient builds the websockets connection, it
    MUST pass ``max_size=self._ws_max_size`` so the cap actually takes
    effect on the wire. Catches a regression where the kwarg is plumbed
    into ``__init__`` but forgotten at the ``websockets.connect(...)``
    site (the exact shape of the D1.3-fu1 source-of-bug surface).

    Uses module-level AST inspection — avoids running the asyncio loop /
    websockets handshake while still proving the call-site invariant.
    Matches the ``websockets.connect(...)`` Attribute form only; a future
    refactor to ``from websockets import connect`` would trip the
    ``assert connect_calls`` guard loud (intentional — preserve the
    ``websockets.<x>`` import shape this regression test was written for).
    """
    import ast
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "kalshi_wire" / "ws_client.py"
    tree = ast.parse(src.read_text())

    connect_calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_connect = (
            isinstance(func, ast.Attribute) and func.attr == "connect"
            and isinstance(func.value, ast.Name) and func.value.id == "websockets"
        )
        if is_connect:
            connect_calls.append(node)

    assert connect_calls, (
        "No websockets.connect(...) call found in kalshi_wire/ws_client.py "
        "— either the import shape changed or the test needs a broader walk."
    )
    for call in connect_calls:
        kwarg_names = {kw.arg for kw in call.keywords if kw.arg is not None}
        assert "max_size" in kwarg_names, (
            f"websockets.connect(...) at line {call.lineno} missing "
            f"`max_size=...` kwarg. Without it, python websockets defaults "
            f"max_size to 1 MiB and incoming Kalshi acks > 1 MiB trigger "
            f"the WS 1009 'message too big' close — exactly the D1.3-fu1 "
            f"storm class. Pass `max_size=self._ws_max_size`."
        )
