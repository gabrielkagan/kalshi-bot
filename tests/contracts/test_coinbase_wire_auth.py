"""D2.1.5 — ``coinbase_wire.auth`` public-subscribe-message helper +
HMAC-stub contract.

Ticket 86b9zkpny (2026-05-17). The operator scoped D2.1.5 to PUBLIC
Coinbase Exchange WS channels — default subscribe set is the 4
verified-public channels (ticker / matches / heartbeat / status);
``level2_batch`` is deferred to D2.2 pending in-archiver reachability
verification. No HMAC handshake is required for any of these.

The auth module therefore exposes:

  - ``build_public_subscribe_message(channels, product_ids) -> dict``
    The Coinbase Exchange WS subscribe payload for public channels —
    no signature field, just ``{type, product_ids, channels}``. Note
    that ``channels`` is PLURAL — Coinbase Exchange WS lets a single
    subscribe message cover multiple channels.
  - ``sign(...)`` / ``make_ws_headers(...)`` — sentinel stubs that raise
    ``NotImplementedError`` until a future Bit adds private channels
    (full ``user`` channel, authenticated ``level2`` for high-rate-limit
    access, etc.). Documented but not wired.

The forecast at D2.1 (scaffolding) said the body would ship HMAC. That
forecast is intentionally NARROWED at D2.1.5: shipping HMAC without a
caller is dead code, and bronzing public-only is sufficient to close
the "non-reproducible Coinbase orderbook" training-data gap. The HMAC
stubs preserve the forecast surface for the future Bit; the
NotImplementedError guards prevent silent misuse.

Pins:
1. ``build_public_subscribe_message(channels, product_ids)`` exists and
   produces a dict with the documented Coinbase Exchange WS subscribe
   shape.
2. The output is JSON-serializable as a single line (the wire format).
3. ``channels`` and ``product_ids`` are required + must be non-empty
   lists.
4. ``sign(...)`` and ``make_ws_headers(...)`` raise
   ``NotImplementedError`` with a clear "D2.1.5 public-only" message
   pointing to the future Bit.

If this test fails: D2.1.5 auth body is incomplete or drifted from the
public-only scope.
"""
from __future__ import annotations

import inspect
import json

import pytest


# ─── 1. build_public_subscribe_message exists + signature ────────────────────


def test_build_public_subscribe_message_importable():
    """``coinbase_wire.auth.build_public_subscribe_message`` exists."""
    from coinbase_wire.auth import build_public_subscribe_message
    assert callable(build_public_subscribe_message)


def test_build_public_subscribe_message_signature():
    """Signature: ``(channels: List[str], product_ids: List[str]) -> Dict``.

    ``channels`` is PLURAL — Coinbase Exchange WS lets a single subscribe
    message cover multiple channels in one shot, distinct from
    Coinbase Advanced Trade WS's per-channel subscribe shape.
    """
    from coinbase_wire.auth import build_public_subscribe_message
    sig = inspect.signature(build_public_subscribe_message)
    params = sig.parameters
    assert "channels" in params, "missing `channels` parameter"
    assert "product_ids" in params, "missing `product_ids` parameter"


# ─── 2. Output shape ─────────────────────────────────────────────────────────


def test_build_public_subscribe_message_returns_dict_with_expected_keys():
    """Output is a dict with exactly the documented Coinbase Exchange WS
    subscribe keys — ``type``, ``product_ids``, ``channels``. No
    ``api_key`` / ``signature`` / ``timestamp`` fields (those are for
    private channels).
    """
    from coinbase_wire.auth import build_public_subscribe_message
    payload = build_public_subscribe_message(
        channels=["level2_batch", "matches"],
        product_ids=["BTC-USD", "ETH-USD"],
    )
    assert isinstance(payload, dict)
    assert payload.get("type") == "subscribe", (
        f"Coinbase Exchange WS subscribe payload missing `type=subscribe`; "
        f"got {payload.get('type')!r}"
    )
    assert payload.get("channels") == ["level2_batch", "matches"], (
        f"`channels` field must echo input; got "
        f"{payload.get('channels')!r}"
    )
    assert payload.get("product_ids") == ["BTC-USD", "ETH-USD"], (
        f"`product_ids` must echo input; got {payload.get('product_ids')!r}"
    )
    # No auth fields on the public path.
    for forbidden in ("api_key", "signature", "timestamp", "passphrase"):
        assert forbidden not in payload, (
            f"Public subscribe payload should not carry auth field "
            f"`{forbidden}`; D2.1.5 is public-only."
        )


def test_build_public_subscribe_message_is_json_serializable():
    """Output JSON-encodes to a single line — the WS wire payload."""
    from coinbase_wire.auth import build_public_subscribe_message
    payload = build_public_subscribe_message(
        channels=["matches"],
        product_ids=["BTC-USD"],
    )
    encoded = json.dumps(payload)
    assert "\n" not in encoded
    recovered = json.loads(encoded)
    assert recovered == payload


# ─── 3. Argument validation ──────────────────────────────────────────────────


def test_build_public_subscribe_message_rejects_empty_product_ids():
    """Empty ``product_ids`` is rejected — subscribing to zero products
    is meaningless on Coinbase Exchange WS and would silently establish
    an idle channel.
    """
    from coinbase_wire.auth import build_public_subscribe_message
    with pytest.raises((TypeError, ValueError)):
        build_public_subscribe_message(
            channels=["matches"], product_ids=[],
        )


def test_build_public_subscribe_message_rejects_empty_channels():
    """Empty ``channels`` is rejected — subscribing to zero channels
    would silently establish an idle WS session.
    """
    from coinbase_wire.auth import build_public_subscribe_message
    with pytest.raises((TypeError, ValueError)):
        build_public_subscribe_message(
            channels=[], product_ids=["BTC-USD"],
        )


def test_build_public_subscribe_message_rejects_non_list_channels():
    """``channels`` must be a list (not a bare string).

    A bare string ``"matches"`` is iterable but would silently expand to
    ``["m","a","t","c","h","e","s"]`` if the function delegated to
    ``list(channels)``. Reject up front.
    """
    from coinbase_wire.auth import build_public_subscribe_message
    with pytest.raises((TypeError, ValueError)):
        build_public_subscribe_message(
            channels="matches", product_ids=["BTC-USD"],
        )


def test_build_public_subscribe_message_rejects_non_list_product_ids():
    """``product_ids`` must be a list (not a bare string)."""
    from coinbase_wire.auth import build_public_subscribe_message
    with pytest.raises((TypeError, ValueError)):
        build_public_subscribe_message(
            channels=["matches"], product_ids="BTC-USD",
        )


# ─── 4. HMAC sentinel stubs ──────────────────────────────────────────────────


def test_sign_stub_raises_notimplementederror():
    """``coinbase_wire.auth.sign`` is reserved for the future private-
    channel Bit. Until then, calling it raises ``NotImplementedError``
    with a clear message.
    """
    from coinbase_wire.auth import sign
    with pytest.raises(NotImplementedError) as exc:
        sign(secret="x", timestamp="1", method="GET", path="/users/self/verify")
    msg = str(exc.value).lower()
    assert "d2.1.5" in msg or "public-only" in msg or "private" in msg, (
        f"sign() NotImplementedError message should mention D2.1.5 / "
        f"public-only / private; got {exc.value!r}"
    )


def test_make_ws_headers_stub_raises_notimplementederror():
    """``coinbase_wire.auth.make_ws_headers`` is reserved for the future
    private-channel Bit (analogous to ``kalshi_wire.auth.make_ws_headers``,
    which IS implemented because Kalshi WS auth is always required).
    """
    from coinbase_wire.auth import make_ws_headers
    with pytest.raises(NotImplementedError) as exc:
        make_ws_headers(api_key="x", api_secret="y")
    msg = str(exc.value).lower()
    assert "d2.1.5" in msg or "public-only" in msg or "private" in msg, (
        f"make_ws_headers() NotImplementedError should mention "
        f"D2.1.5 / public-only / private; got {exc.value!r}"
    )


# ─── 5. No bot.* or collector.* imports (defense-in-depth) ───────────────────


def test_auth_module_imports_cleanly_without_bot():
    """Importing ``coinbase_wire.auth`` does not transitively import
    ``bot`` — checked by inspecting ``sys.modules`` after import.
    """
    import sys
    for name in list(sys.modules):
        if name.startswith("coinbase_wire"):
            sys.modules.pop(name, None)
    bot_before = {n for n in sys.modules if n == "bot" or n.startswith("bot.")}
    import coinbase_wire.auth  # noqa: F401
    bot_after = {n for n in sys.modules if n == "bot" or n.startswith("bot.")}
    new = bot_after - bot_before
    assert not new, (
        f"Importing coinbase_wire.auth pulled in bot.* modules: {new}. "
        "The wire layer must not import bot."
    )
