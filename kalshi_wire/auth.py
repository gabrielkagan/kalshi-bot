"""RSA-PSS-SHA256 auth for Kalshi REST + WS — D1.1.5 (ticket 86b9zdhz2, 2026-05-16).

Canonical authentication primitives shared by ``bot/kalshi_client.py``
(REST) and ``bot/feeds/kalshi.py`` (WS handshake). The 2026-05-16
AMENDMENT to ``kb/decisions/data-corpus-architecture.md`` §5 SUPERSEDED
the original "duplicate the minimum in collector" plan after external
advisor noted that collector and bot must be "two sides of the same
coin" for the data corpus to serve its purpose.

Pre-D1.1.5 the RSA-PSS construction lived in three places:
  - ``bot/kalshi_client.py:60`` (``KalshiClient._create_signature``, REST)
  - ``bot/feeds/kalshi.py:768`` (``KalshiFeed._create_ws_headers``, WS)
  - ``collector/auth.py`` (collector RSA-PSS — duplicate-by-design at D0.3 §5)

Post-D1.1.5 those three sites all delegate here. ``collector/auth.py`` is
DELETED in the same Bit's Phase 4 rebase.

Spec (per Kalshi server-side contract):
  - hash algorithm = SHA256
  - padding = ``PSS(mgf=MGF1(SHA256), salt_length=DIGEST_LENGTH)``
  - REST message = ``f"{timestamp_ms}{METHOD}{path_no_query}"``
  - WS message = ``f"{timestamp_ms}GET/trade-api/ws/v2"``
  - Headers = ``KALSHI-ACCESS-KEY`` / ``KALSHI-ACCESS-TIMESTAMP`` /
    ``KALSHI-ACCESS-SIGNATURE`` (+ ``Content-Type: application/json`` for REST)

Byte-equivalence to the pre-extraction inline call sites is pinned by
``tests/contracts/test_kalshi_wire_auth.py::test_sign_parity_with_bot_kalshi_client``
+ ``::test_make_ws_headers_parity_with_bot_feeds_kalshi`` (verify the
signatures produced here verify against the same public key + message
as the historical inline sites — PSS salt is random so byte-identical
output is NOT possible, but verifiable parity is the actual spec).

NO imports from ``bot.*`` or ``collector.*`` (pinned by import-linter
contracts ``kalshi_wire-no-bot`` + ``kalshi_wire-no-collector``).
"""
from __future__ import annotations

import base64
import time
from pathlib import Path
from typing import Dict, Union

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


def load_private_key(key_path: Union[str, Path]):
    """Deserialize a PEM-encoded private key from disk.

    Mirrors ``bot/kalshi_client.py:55-58`` (the historical PEM-load step
    inside ``KalshiClient._load_private_key``).
    """
    with open(key_path, "rb") as f:
        return serialization.load_pem_private_key(f.read(), password=None)


def sign(private_key, timestamp_ms: str, method: str, path: str) -> str:
    """Sign ``timestamp_ms + METHOD + path_no_query`` with RSA-PSS-SHA256.

    The path is stripped of its query string per Kalshi's server-side
    contract — query params do NOT participate in the signature
    (matches ``bot/kalshi_client.py:62`` ``path_no_query = path.split("?")[0]``).

    Returns base64-encoded signature (as a str).

    Note: PSS uses a random salt (``salt_length=DIGEST_LENGTH``), so two
    successive calls with identical inputs return different bytes — that's
    spec, not a bug. The correctness contract is signature VERIFICATION,
    not byte-equality.
    """
    path_no_query = path.split("?")[0]
    message = f"{timestamp_ms}{method}{path_no_query}".encode("utf-8")
    sig = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.DIGEST_LENGTH,
        ),
        hashes.SHA256(),
    )
    return base64.b64encode(sig).decode("utf-8")


def make_rest_headers(
    api_key: str, private_key, method: str, path: str,
) -> Dict[str, str]:
    """Build the 4 REST headers for an authenticated Kalshi request.

    Mirrors ``bot/kalshi_client.py:111-120``. Timestamp is sourced from
    ``time.time()`` at call time — caller does NOT pass it explicitly
    because each request needs a fresh timestamp anyway (within Kalshi's
    server-side clock-drift tolerance).

    Returns:
        ``{KALSHI-ACCESS-KEY, KALSHI-ACCESS-TIMESTAMP, KALSHI-ACCESS-SIGNATURE,
        Content-Type}``
    """
    timestamp_ms = str(int(time.time() * 1000))
    signature = sign(private_key, timestamp_ms, method, path)
    return {
        "KALSHI-ACCESS-KEY": api_key,
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        "KALSHI-ACCESS-SIGNATURE": signature,
        "Content-Type": "application/json",
    }


def make_ws_headers(api_key: str, private_key) -> Dict[str, str]:
    """Build the 3 WS-handshake headers for the Kalshi orderbook feed.

    Mirrors ``bot/feeds/kalshi.py:768-786`` (``_create_ws_headers``).
    The WS handshake signs the fixed message ``f"{ts}GET/trade-api/ws/v2"``
    — the path and method are constants because all WS connections target
    the same endpoint.

    Returns:
        ``{KALSHI-ACCESS-KEY, KALSHI-ACCESS-TIMESTAMP, KALSHI-ACCESS-SIGNATURE}``

    NO ``Content-Type`` — WS handshakes don't carry a body.
    """
    timestamp_ms = str(int(time.time() * 1000))
    # The WS endpoint path is hardcoded in the signature (matches
    # bot/feeds/kalshi.py:772 message construction). Callers don't pass
    # the path because there's only one WS endpoint.
    signature = sign(private_key, timestamp_ms, "GET", "/trade-api/ws/v2")
    return {
        "KALSHI-ACCESS-KEY": api_key,
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        "KALSHI-ACCESS-SIGNATURE": signature,
    }
