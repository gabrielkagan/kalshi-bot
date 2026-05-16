"""D1.1.5 — ``kalshi_wire.auth`` RSA-PSS-SHA256 byte-equivalence to
``bot/kalshi_client.py:60`` + ``bot/feeds/kalshi.py:768`` (the pre-extraction
sources).

Ticket 86b9zdhz2 (2026-05-16). The whole point of extracting auth to a
shared package is byte-equivalence: ``kalshi_wire.auth.sign(...)`` for a
given (private_key, timestamp_ms, method, path) tuple MUST return the
same base64-encoded signature as the historical inline RSA-PSS call sites
that ``bot/`` accumulated over 2 years. Any drift here = bot can't
authenticate against Kalshi after deploy = data-corpus dies = trading
halts.

Test strategy:
1. **Determinism / no-RNG pin**: RSA-PSS uses ``salt_length=DIGEST_LENGTH``
   which IS randomized. Verification is the correctness contract, NOT
   byte-comparison of two separate signs. We sign once, then VERIFY the
   signature against the public key + message — that's the spec.
2. **Cross-implementation parity**: sign the same (ts, method, path) via
   ``kalshi_wire.auth.sign`` AND via ``bot.kalshi_client.KalshiClient
   ._create_signature``; verify BOTH signatures with the same public key.
   Different bytes (because of salt), same verification outcome.
3. **WS handshake parity**: same idea against ``bot/feeds/kalshi.py``'s
   ``_create_ws_headers`` (timestamp + "GET" + "/trade-api/ws/v2").
4. **Header shape**: ``make_rest_headers`` + ``make_ws_headers`` produce
   exactly the 4 expected keys with the expected values.

A pre-generated test private key is used (NOT a real Kalshi key — generated
in-test for hermeticity).
"""
from __future__ import annotations

import base64
from pathlib import Path
from unittest.mock import MagicMock
import sys

import pytest


# Heavy-dep stubs for the import chain — bot/kalshi_client.py top-imports
# requests + cryptography (real); bot/feeds/kalshi.py top-imports
# websockets (mock OK since we never actually connect).
_HEAVY_MOD_NAMES = (
    "websockets",
)
for _mod in _HEAVY_MOD_NAMES:
    sys.modules.setdefault(_mod, MagicMock())


REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def test_private_key():
    """Generate an ephemeral RSA-2048 key for the test session.

    NOT a real Kalshi key — hermetic and CI-safe. Real key behavior is
    indistinguishable (RSA-PSS-SHA256 is determined by the key + salt
    + padding scheme, not by which key it is).
    """
    from cryptography.hazmat.primitives.asymmetric import rsa
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def test_public_key(test_private_key):
    return test_private_key.public_key()


# ─── 1. kalshi_wire.auth surface exists ──────────────────────────────────────


def test_kalshi_wire_auth_module_importable():
    """``kalshi_wire.auth`` is importable as a sibling-of-bot package.

    Fails at import time if kalshi_wire/auth.py is missing OR contains a
    syntax error OR pulls a forbidden dep. The remaining tests in this
    file assume the import works.
    """
    import kalshi_wire.auth  # noqa: F401


def test_kalshi_wire_auth_public_surface():
    """Public callable surface per D1.1.5 pickup prompt: ``sign``,
    ``load_private_key``, ``make_rest_headers``, ``make_ws_headers``.
    """
    import kalshi_wire.auth as auth
    expected = {"sign", "load_private_key", "make_rest_headers", "make_ws_headers"}
    actual = {n for n in expected if hasattr(auth, n)}
    missing = expected - actual
    assert not missing, (
        f"kalshi_wire.auth missing public callables: {sorted(missing)}. "
        "Per the D1.1.5 pickup prompt, the auth module exposes 4 names: "
        "load_private_key + sign (low-level) + make_rest_headers + "
        "make_ws_headers (high-level convenience)."
    )


# ─── 2. RSA-PSS-SHA256 spec verification (signature → verify roundtrip) ──────


def test_sign_produces_verifiable_signature(test_private_key, test_public_key):
    """The byte-level contract: ``kalshi_wire.auth.sign(key, ts, method, path)``
    returns a base64-encoded RSA-PSS-SHA256 signature that verifies under
    the matching public key against the SAME message
    (``ts + method + path_no_query``).

    This is the strict spec: any other key, any other padding, any other
    salt scheme → verification fails. Pins:
     - hash algorithm = SHA256
     - padding = PSS w/ MGF1(SHA256) + salt_length=DIGEST_LENGTH
     - message = f"{ts}{method}{path}".encode("utf-8")
     - path is stripped of query string (matches Kalshi server-side)
    """
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    import kalshi_wire.auth as auth

    ts_ms = "1715900000000"
    method = "GET"
    path = "/trade-api/v2/portfolio/balance"
    sig_b64 = auth.sign(test_private_key, ts_ms, method, path)
    sig_bytes = base64.b64decode(sig_b64)
    message = f"{ts_ms}{method}{path}".encode("utf-8")

    # MUST verify — any deviation from the spec (wrong hash, wrong padding,
    # wrong message construction) makes this raise.
    test_public_key.verify(
        sig_bytes,
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )

    # Negative: tampered message MUST fail verification.
    with pytest.raises(InvalidSignature):
        test_public_key.verify(
            sig_bytes,
            b"tampered" + message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )


def test_sign_strips_query_string_from_path(test_private_key, test_public_key):
    """``sign(..., path)`` strips ``?...`` per Kalshi server-side contract.

    Two sigs with same ts+method but different query strings on the same
    base path → both verify against the SAME base-path message. Mirrors
    bot/kalshi_client.py:62 (``path_no_query = path.split("?")[0]``).
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    import kalshi_wire.auth as auth

    ts_ms = "1715900000000"
    method = "GET"
    base = "/trade-api/v2/markets"
    sig_with_query = base64.b64decode(
        auth.sign(test_private_key, ts_ms, method, f"{base}?limit=200&cursor=ABC")
    )
    sig_no_query = base64.b64decode(auth.sign(test_private_key, ts_ms, method, base))

    # Both must verify against the SAME base-path message — proving the
    # query-strip happened.
    message = f"{ts_ms}{method}{base}".encode("utf-8")
    pss = padding.PSS(
        mgf=padding.MGF1(hashes.SHA256()),
        salt_length=padding.PSS.DIGEST_LENGTH,
    )
    test_public_key.verify(sig_with_query, message, pss, hashes.SHA256())
    test_public_key.verify(sig_no_query, message, pss, hashes.SHA256())


# ─── 3. Cross-implementation parity vs bot/kalshi_client.py ──────────────────


def test_sign_parity_with_bot_kalshi_client(test_private_key, test_public_key):
    """``kalshi_wire.auth.sign`` and ``bot.kalshi_client.KalshiClient
    ._create_signature`` MUST both produce signatures that verify against
    the same public key for the same (ts, method, path) tuple.

    They CANNOT produce byte-identical signatures (PSS salt is random),
    but they MUST both verify — proving identical message construction +
    padding + hash spec. This is the load-bearing parity claim: the bot's
    accumulated 2 years of RSA-PSS behavior is matched.
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    from bot.kalshi_client import KalshiClient
    import kalshi_wire.auth as auth

    # Construct a KalshiClient with our test private_key. Skip __init__'s
    # file-load step by manual attribute assignment.
    client = KalshiClient.__new__(KalshiClient)
    client.private_key = test_private_key
    client.api_key = "test-key-id"

    ts_ms = "1715900000000"
    method = "POST"
    path = "/trade-api/v2/portfolio/orders"

    wire_sig = base64.b64decode(auth.sign(test_private_key, ts_ms, method, path))
    bot_sig = base64.b64decode(client._create_signature(ts_ms, method, path))
    message = f"{ts_ms}{method}{path}".encode("utf-8")
    pss = padding.PSS(
        mgf=padding.MGF1(hashes.SHA256()),
        salt_length=padding.PSS.DIGEST_LENGTH,
    )
    test_public_key.verify(wire_sig, message, pss, hashes.SHA256())
    test_public_key.verify(bot_sig, message, pss, hashes.SHA256())


def test_make_ws_headers_parity_with_bot_feeds_kalshi(test_private_key, test_public_key):
    """``kalshi_wire.auth.make_ws_headers`` MUST construct headers that
    verify against the same message that bot/feeds/kalshi.py:768
    (``_create_ws_headers``) produces. WS-handshake spec: message =
    ``ts + "GET" + "/trade-api/ws/v2"``.

    Pulls bot.feeds.kalshi's KalshiFeed to compare. Mock websockets to
    avoid real connect.
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    from bot.feeds.kalshi import KalshiFeed
    import kalshi_wire.auth as auth

    feed = KalshiFeed.__new__(KalshiFeed)
    feed._api_key = "test-key-id"
    feed._private_key = test_private_key

    bot_headers = feed._create_ws_headers()
    wire_headers = auth.make_ws_headers("test-key-id", test_private_key)

    # Same header keys.
    expected_keys = {"KALSHI-ACCESS-KEY", "KALSHI-ACCESS-TIMESTAMP",
                     "KALSHI-ACCESS-SIGNATURE"}
    assert set(bot_headers.keys()) == expected_keys
    assert set(wire_headers.keys()) == expected_keys
    assert bot_headers["KALSHI-ACCESS-KEY"] == wire_headers["KALSHI-ACCESS-KEY"]

    # Both signatures verify against the WS handshake message — using each
    # header's OWN timestamp (they're seconds apart from ``time.time()``).
    pss = padding.PSS(
        mgf=padding.MGF1(hashes.SHA256()),
        salt_length=padding.PSS.DIGEST_LENGTH,
    )
    for hdrs in (bot_headers, wire_headers):
        ts = hdrs["KALSHI-ACCESS-TIMESTAMP"]
        sig_bytes = base64.b64decode(hdrs["KALSHI-ACCESS-SIGNATURE"])
        ws_message = f"{ts}GET/trade-api/ws/v2".encode("utf-8")
        test_public_key.verify(sig_bytes, ws_message, pss, hashes.SHA256())


# ─── 4. Header shape ─────────────────────────────────────────────────────────


def test_make_rest_headers_shape(test_private_key):
    """``make_rest_headers(api_key, private_key, method, path)`` returns
    the 4 expected headers (KEY / TIMESTAMP / SIGNATURE / Content-Type)
    matching bot/kalshi_client.py:115-120.
    """
    import kalshi_wire.auth as auth
    hdrs = auth.make_rest_headers(
        "test-key-id", test_private_key, "GET",
        "/trade-api/v2/portfolio/balance",
    )
    assert set(hdrs.keys()) == {
        "KALSHI-ACCESS-KEY", "KALSHI-ACCESS-TIMESTAMP",
        "KALSHI-ACCESS-SIGNATURE", "Content-Type",
    }
    assert hdrs["KALSHI-ACCESS-KEY"] == "test-key-id"
    assert hdrs["Content-Type"] == "application/json"


def test_make_ws_headers_shape(test_private_key):
    """``make_ws_headers(api_key, private_key)`` returns the 3 expected
    headers (KEY / TIMESTAMP / SIGNATURE — NO Content-Type for WS)
    matching bot/feeds/kalshi.py:782-786.
    """
    import kalshi_wire.auth as auth
    hdrs = auth.make_ws_headers("test-key-id", test_private_key)
    assert set(hdrs.keys()) == {
        "KALSHI-ACCESS-KEY", "KALSHI-ACCESS-TIMESTAMP",
        "KALSHI-ACCESS-SIGNATURE",
    }
    assert hdrs["KALSHI-ACCESS-KEY"] == "test-key-id"


# ─── 5. load_private_key delegates to cryptography library cleanly ──────────


def test_load_private_key_roundtrip(tmp_path, test_private_key):
    """``load_private_key(path)`` deserializes a PEM-encoded private key.

    Round-trip: serialize test key to PEM file → load_private_key → sign
    with the loaded key → verify with the original public key. Confirms
    the load function preserves the key material exactly.
    """
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    import kalshi_wire.auth as auth

    pem_path = tmp_path / "test_key.pem"
    pem_bytes = test_private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pem_path.write_bytes(pem_bytes)

    loaded = auth.load_private_key(str(pem_path))
    sig_b64 = auth.sign(loaded, "1715900000000", "GET", "/trade-api/v2/markets")
    sig_bytes = base64.b64decode(sig_b64)
    test_private_key.public_key().verify(
        sig_bytes,
        b"1715900000000GET/trade-api/v2/markets",
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
