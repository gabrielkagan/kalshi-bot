"""D2.1.5 — ``coinbase_wire.ws_client.WSClient`` public-API surface pin.

Ticket 86b9zkpny (2026-05-17), sub-Bit of the 86b9zkkv4 D2.x Coinbase WS
bronzing umbrella. Mirrors the D1.1.5 ``test_kalshi_wire_ws_client.py``
shape — pure-transport leaf, sync public API with asyncio internal, 4
sync callbacks.

This test file pins the **API surface**. Behavioral coverage (mocked-
websocket event loop checks against a synthetic Coinbase Exchange WS
server) is the responsibility of a separate behavioral test file
shipped with the consumer side — mirrors the kalshi_wire pattern
where ``tests/equivalence/test_kalshi_wire_differential.py`` shipped
alongside the BronzeArchiver consumer, not alongside the wire body
itself. (D2.2 shipped the ``CoinbaseArchiver`` consumer with 3
contract test files exercising the constructor + worker-thread +
skip-ack invariants; full mocked-WS behavioral suite remains a
future ticket.)

D2.1 (PR #66) shipped scaffolding-only — instantiating ``WSClient``
raised NotImplementedError. D2.1.5 landed the body, so these tests flip
RED → GREEN at body-add time. Post-ship the constructor accepts the
documented kwargs, ``start()`` / ``stop()`` / ``send_frame()`` /
``request_reconnect()`` work, and ``Frame`` / ``build_envelope`` are
importable from ``coinbase_wire.ws_client``.

Pins:
1. ``coinbase_wire.ws_client.WSClient`` is importable + is a class
2. Constructor accepts ``url, on_frame`` + the silence-watchdog tunables
   + ``ws_max_size`` (D1.3-fu1 lesson) + ``channels`` / ``product_ids``
   defaults (per the D2.1.5 channel-set operator decision).
3. Public methods: ``start``, ``stop``, ``send_frame``,
   ``request_reconnect`` + ``is_connected`` property.
4. ``Frame`` dataclass has the 6 expected fields (``wire_recv_ts``,
   ``raw``, ``parsed``, ``channel``, ``msg_type``, ``sequence_num``).
   Coinbase Exchange WS wire shape differs from Kalshi: ``sequence_num``
   is per-product monotonic (Exchange WS ``sequence`` field, populated
   for ticker / match / heartbeat frames). ``channel`` is always None
   at the wire layer (Exchange WS has no top-level channel field);
   consumers use ``msg_type`` as the dispatch key.
5. No async exposure at the public surface — sync/threading per project
   anti-pattern. (asyncio internal to the client is OK; it must NOT leak
   ``async def`` methods into the public API.)
6. ``build_envelope`` is importable from ``coinbase_wire.ws_client`` AND
   re-exported from the package top-level (per D1.1.5 R1 Mn3 follow-up
   precedent — load-bearing D0.3 §2 contract stays discoverable).
7. ``ws_max_size`` defaults to ≥ 16 MiB and rejects non-int / < 1
   (D1.3-fu1 lesson carried forward — the worst case here isn't
   acknowledged Coinbase ack growth but defense-in-depth against any
   future channel growing payloads past the default 1 MiB).

If this test fails: D2.1.5 body is incomplete or the API surface drifted
from the design described above.
"""
from __future__ import annotations

import inspect
import sys
from dataclasses import fields, is_dataclass
from unittest.mock import MagicMock

import pytest


_HEAVY_MOD_NAMES = ("websockets",)
for _mod in _HEAVY_MOD_NAMES:
    sys.modules.setdefault(_mod, MagicMock())


# ─── 1. WSClient class importable ────────────────────────────────────────────


def test_ws_client_class_importable():
    """``coinbase_wire.ws_client.WSClient`` is importable as a class."""
    from coinbase_wire.ws_client import WSClient
    assert inspect.isclass(WSClient)


# ─── 2. Constructor signature ────────────────────────────────────────────────


def test_ws_client_init_signature():
    """``WSClient.__init__`` accepts the design API: positional ``url`` +
    keyword-only ``on_frame`` callback + the silence-watchdog tunables
    + ``channels`` + ``product_ids``.

    The tunables MUST be parameters (not module-level constants the wire
    library reads from bot.constants) — the wire library cannot reach
    into bot/ for config.

    Unlike Kalshi the constructor takes NO ``api_key`` / ``private_key``
    because the operator scoped D2.1.5 to public Coinbase Exchange WS
    channels — default subscribe set is the 4 verified-public channels
    (ticker / matches / heartbeat / status; ``level2_batch`` deferred
    to D2.2). No HMAC handshake required. A future Bit that adds
    private channels can extend the signature without breaking this
    surface.
    """
    from coinbase_wire.ws_client import WSClient
    sig = inspect.signature(WSClient.__init__)
    params = sig.parameters

    assert "on_frame" in params, (
        "WSClient.__init__ missing `on_frame` callback — frames are "
        "delivered to consumers via callback, not via subclass override."
    )
    assert "url" in params, "WSClient.__init__ missing `url` parameter"
    # Optional callbacks per D1.1.5 4-callback pattern.
    for name in ("on_session_start", "on_session_end", "on_drain_tick"):
        assert name in params, (
            f"WSClient.__init__ missing optional callback `{name}` — "
            "the 4-sync-callback pattern is shared across kalshi_wire + "
            "coinbase_wire so consumer code is symmetric."
        )
    # Default channels + product_ids are constructor kwargs so the
    # consumer can override per-Bit (the CoinbaseArchiver D2.2 default
    # accepts the wire library's 4-channel set; the D2.3 bot feed
    # refactor may pick a narrower subset).
    assert "channels" in params, (
        "WSClient.__init__ missing `channels` kwarg — Coinbase WS "
        "subscribe is per-channel; this list controls the default "
        "subscribe set."
    )
    assert "product_ids" in params, (
        "WSClient.__init__ missing `product_ids` kwarg — Coinbase WS "
        "subscribe is per-product; this list controls the default "
        "subscribe set."
    )
    # Silence-watchdog + reconnect tunables.
    for name in (
        "silence_timeout_s",
        "ping_interval",
        "ping_timeout",
        "max_backoff_s",
    ):
        assert name in params, (
            f"WSClient.__init__ missing tunable `{name}` — must be a "
            "constructor kwarg, not a module-level constant the wire "
            "library reads from bot.constants."
        )
    # D1.3-fu1 lesson: ws_max_size is a constructor kwarg with a generous
    # default. Default-1 MiB from `websockets.connect` would re-open the
    # 1009 close-loop class if Coinbase ever ships frames > 1 MiB.
    assert "ws_max_size" in params, (
        "WSClient.__init__ missing `ws_max_size` kwarg — D1.3-fu1 lesson: "
        "the websockets default 1 MiB is too small for cumulative-ack or "
        "future-protocol-growth defense; coinbase_wire carries the same "
        "kwarg as kalshi_wire for symmetry."
    )


# ─── 3. Public methods exist ─────────────────────────────────────────────────


def test_ws_client_public_methods():
    """``WSClient`` exposes ``start``, ``stop``, ``send_frame``,
    ``request_reconnect`` + the ``is_connected`` property.

    Same surface as ``kalshi_wire.ws_client.WSClient`` — consumer code
    that wraps both packages should be able to swap one for the other
    on the public API (channel-subscribe shapes differ; transport
    primitives are identical).
    """
    from coinbase_wire.ws_client import WSClient
    for method_name in ("start", "stop", "send_frame", "request_reconnect"):
        meth = getattr(WSClient, method_name, None)
        assert meth is not None, (
            f"WSClient missing public method `{method_name}`. The "
            "consumer surface mirrors kalshi_wire — adding/removing one "
            "of these breaks the symmetry."
        )
        # Sync, not async — project anti-pattern says no async at the
        # public API.
        assert not inspect.iscoroutinefunction(meth), (
            f"WSClient.{method_name} is `async def` — public API must be "
            "sync. asyncio is INTERNAL to this class only."
        )
    is_connected = getattr(WSClient, "is_connected", None)
    assert isinstance(is_connected, property), (
        "WSClient.is_connected must be a property (sync attribute access), "
        "not a method. Mirrors kalshi_wire."
    )


# ─── 4. Frame dataclass ──────────────────────────────────────────────────────


def test_frame_dataclass_shape():
    """``coinbase_wire.ws_client.Frame`` is a dataclass with the 6
    documented fields. Different from Kalshi's ``Frame``: Kalshi has
    ``sid`` + ``seq`` for per-subscription sequencing; Coinbase has
    ``sequence_num`` which is per-product monotonic (passed through
    verbatim from Exchange WS frames — wire-level gap detection is
    deferred to the consumer which holds per-product context).
    ``Frame.channel`` is always None at the wire layer (Exchange WS
    has no top-level channel field); consumers map ``msg_type`` →
    channel using their own dispatch table.
    """
    from coinbase_wire.ws_client import Frame
    assert is_dataclass(Frame), "Frame must be a @dataclass"
    field_names = {f.name for f in fields(Frame)}
    expected = {
        "wire_recv_ts",
        "raw",
        "parsed",
        "channel",
        "msg_type",
        "sequence_num",
    }
    missing = expected - field_names
    extra = field_names - expected
    assert not missing, f"Frame missing fields: {missing}"
    assert not extra, (
        f"Frame has unexpected extra fields: {extra}. Adding fields to "
        "Frame is a contract change — extra metadata belongs in the "
        "build_envelope output or in the consumer's per-frame handler, "
        "not on the shared dataclass."
    )


# ─── 5. No async exposure at the public surface ──────────────────────────────


def test_no_async_def_methods_on_public_api():
    """The WSClient public surface is fully sync — no async-def methods
    leak through. Pinning this prevents future drift where a contributor
    adds an `async def`-flavored helper that bypasses the sync wrapper.

    Internal methods (starting with `_`) are allowed to be async (the
    asyncio loop body lives there).
    """
    from coinbase_wire.ws_client import WSClient
    offenders: list[str] = []
    for name, value in inspect.getmembers(WSClient):
        if name.startswith("_"):
            continue
        if inspect.iscoroutinefunction(value):
            offenders.append(name)
    assert not offenders, (
        f"WSClient has async-def public methods: {offenders}. asyncio "
        "must be INTERNAL — wrap them in sync entry-points."
    )


# ─── 6. build_envelope exported from ws_client AND package top-level ─────────


def test_build_envelope_in_ws_client_module():
    """``build_envelope`` is importable from ``coinbase_wire.ws_client``."""
    from coinbase_wire.ws_client import build_envelope
    assert callable(build_envelope)


def test_build_envelope_re_exported_at_package_top_level():
    """``coinbase_wire.build_envelope`` is the same object as
    ``coinbase_wire.ws_client.build_envelope`` (re-exported via the
    package ``__init__.py``).

    Follows the D1.1.5 R1 Mn3 lesson: the 6-field D0.3 §2 envelope is
    load-bearing bronze contract; keeping it discoverable at the
    package top-level mirrors how ``kalshi_wire.build_envelope`` is
    reachable.
    """
    import coinbase_wire
    from coinbase_wire.ws_client import build_envelope as deep
    top = getattr(coinbase_wire, "build_envelope", None)
    assert top is not None, (
        "coinbase_wire.build_envelope missing from package top-level. "
        "Add `from coinbase_wire.ws_client import ... build_envelope` "
        "to coinbase_wire/__init__.py."
    )
    assert top is deep, (
        "coinbase_wire.build_envelope is not the same object as "
        "coinbase_wire.ws_client.build_envelope — re-export must alias, "
        "not duplicate."
    )


def test_frame_re_exported_at_package_top_level():
    """``coinbase_wire.Frame`` is re-exported from ``ws_client``.

    Mirrors the kalshi_wire convention; consumers should not have to
    know the internal submodule split to import the canonical types.
    """
    import coinbase_wire
    from coinbase_wire.ws_client import Frame as deep
    top = getattr(coinbase_wire, "Frame", None)
    assert top is not None, (
        "coinbase_wire.Frame missing from package top-level. Re-export "
        "via coinbase_wire/__init__.py."
    )
    assert top is deep


def test_ws_client_re_exported_at_package_top_level():
    """``coinbase_wire.WSClient`` is re-exported from ``ws_client``."""
    import coinbase_wire
    from coinbase_wire.ws_client import WSClient as deep
    top = getattr(coinbase_wire, "WSClient", None)
    assert top is not None, (
        "coinbase_wire.WSClient missing from package top-level. Re-export "
        "via coinbase_wire/__init__.py."
    )
    assert top is deep


# ─── 7. ws_max_size validation (D1.3-fu1 lesson carried forward) ─────────────


def test_ws_max_size_default_is_generous():
    """The default ``ws_max_size`` is ≥ 16 MiB.

    D1.3-fu1 lesson: python-websockets default of 1 MiB silently caused
    a 1009 close-loop class once Kalshi's cumulative acks crossed that
    threshold. Carry the lesson forward by setting coinbase_wire's
    default to the same 16 MiB headroom.
    """
    from coinbase_wire.ws_client import WSClient
    sig = inspect.signature(WSClient.__init__)
    ws_max_size_default = sig.parameters["ws_max_size"].default
    assert isinstance(ws_max_size_default, int), (
        f"ws_max_size default must be int, got "
        f"{type(ws_max_size_default).__name__}"
    )
    assert ws_max_size_default >= 16 * 1024 * 1024, (
        f"ws_max_size default {ws_max_size_default} < 16 MiB. D1.3-fu1 "
        "lesson — pick a default with headroom against frame-size growth."
    )


def test_ws_max_size_rejects_non_int_and_subzero():
    """Bool / float / None / 0 / negative are rejected at construction
    time — never pass ``None`` to ``websockets.connect(max_size=...)``
    even by accident (that disables the cap entirely → unbounded memory).
    """
    from coinbase_wire.ws_client import WSClient
    common_kwargs = dict(on_frame=lambda _f: None)
    for bad in (None, "16777216", 0, -1, 1.5, True, False):
        with pytest.raises((TypeError, ValueError)):
            WSClient(ws_max_size=bad, **common_kwargs)


# ─── 8. Happy-path constructor (R1 Mn1) ──────────────────────────────────────


def test_ws_client_constructs_with_default_kwargs():
    """``WSClient(on_frame=callback)`` constructs cleanly with all other
    kwargs taking their documented defaults — no crash, ``is_connected``
    reads False before ``start()`` is called.

    Added per R1 Mn1: the validator tests above (``ws_max_size`` rejects)
    don't prove the happy-path constructor doesn't crash for an unrelated
    reason. This is the minimum positive-path smoke.
    """
    from coinbase_wire.ws_client import WSClient
    client = WSClient(on_frame=lambda _f: None)
    assert client.is_connected is False, (
        "Newly-constructed WSClient should report is_connected=False "
        "until start() is called."
    )


# ─── 9. DEFAULT_CHANNELS scope pin (R4 Mn2 — code-side regression catch) ────


def test_default_channels_match_post_d2_5_scope():
    """``DEFAULT_CHANNELS`` is exactly the 5 verified-public Coinbase
    Exchange WS channels at the post-D2.5 scope: ``ticker`` +
    ``matches`` + ``heartbeat`` + ``status`` + ``level2_batch``.

    D2.5 (ticket 86b9znq4w, 2026-05-18) PROMOTED ``level2_batch`` into
    DEFAULT_CHANNELS after the R0 reachability spike at D2.5 kickoff
    confirmed public access: 1 snapshot + 502 l2update frames over 30s
    for BTC-USD alone, NO ``type=error`` response. The D2.2
    ``CoinbaseArchiver`` dispatch table
    (``DEFAULT_MSG_TYPE_TO_CHANNEL``) extends in the SAME Bit (adds
    ``"snapshot": "level2_batch"`` + ``"l2update": "level2_batch"``).

    Pre-D2.5 (R2 M4 retract) this test pinned the 4-channel scope as
    defensive — a future Bit could have quietly re-added
    ``level2_batch`` to ``DEFAULT_CHANNELS`` without any sister-doc
    drift firing (L99 catches doc paraphrases, not code-side
    mutation). D2.5 promotes via the BUNDLED retract: this test +
    L99 STALE patterns + sister docs update atomically.

    If a future Bit re-narrows the default set (e.g., Coinbase gates
    one of these channels), update BOTH this assertion AND the L99
    STALE_PATTERNS in the same Bit.
    """
    from coinbase_wire.ws_client import DEFAULT_CHANNELS
    assert DEFAULT_CHANNELS == (
        "ticker", "matches", "heartbeat", "status", "level2_batch",
    ), (
        f"DEFAULT_CHANNELS = {DEFAULT_CHANNELS!r}; expected the 5 "
        f"verified-public Exchange WS channels per the D2.5 "
        f"level2_batch promotion. If level2_batch was removed back, "
        f"document the reason + update the L99 ratchet + the D2.2 "
        f"dispatch table in the same Bit."
    )
