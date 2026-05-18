"""Coinbase Exchange WS subscribe-payload helper — D2.1.5.

Ticket 86b9zkpny (2026-05-17), sub-Bit of the 86b9zkkv4 D2.x Coinbase
WS bronzing umbrella.

D2.1 (PR #66, ticket 86b9zkpc6) shipped this file as an empty stub.

**Protocol surface — Coinbase Exchange WS, NOT Coinbase Advanced Trade
WS.** The bot already consumes ``wss://ws-feed.exchange.coinbase.com``
via ``bot/feeds/coinbase.py`` (see ``bot.constants.COINBASE_WS_URL``);
mirroring the same endpoint here let D2.3 (SHIPPED 2026-05-17, ticket
``86b9zkppt``) be a *structural* refactor — point
``bot/feeds/coinbase.py`` at ``coinbase_wire.ws_client.WSClient`` —
rather than a protocol-flip. The Coinbase Advanced Trade WS surface
(``wss://advanced-trade-ws.coinbase.com``) is a DIFFERENT API with a
DIFFERENT subscribe shape and DIFFERENT product coverage — out of scope
for D2.1.5.

D2.1.5 NARROWED the D2.1 forecast to PUBLIC channels only. At D2.1.5
ship-time the default subscribe set was 4 verified-public channels
(``ticker`` + ``matches`` + ``heartbeat`` + ``status``); ``level2_batch``
was deferred pending in-archiver reachability verification. D2.5
(ticket ``86b9znq4w``, 2026-05-18) PROMOTED ``level2_batch`` after
the R0 reachability spike confirmed public access — post-D2.5 the
default subscribe set is 5 verified-public channels. Public-only
Exchange WS connects need no signature, api_key, timestamp, or
passphrase. The HMAC stubs at ``sign`` + ``make_ws_headers`` below
preserve the D2.1 forecast surface for a future Bit that adds private
channels (full ``user`` channel, authenticated ``level2`` for high-
rate-limit access, etc.).

NO imports from ``bot.*`` or ``collector.*`` (pinned by import-linter
contracts ``coinbase_wire-no-bot`` + ``coinbase_wire-no-collector`` and
the AST-walk guards in
``tests/contracts/test_coinbase_wire_no_bot_imports.py``).
"""
from __future__ import annotations

from typing import Any, Dict, List


# Subscribe-payload type constant — Coinbase Exchange WS protocol.
_SUBSCRIBE_TYPE = "subscribe"


def build_public_subscribe_message(
    channels: List[str], product_ids: List[str],
) -> Dict[str, Any]:
    """Build a Coinbase Exchange WS subscribe payload for public channels.

    Wire shape (per Coinbase Exchange WS docs + the existing
    ``bot/feeds/coinbase.py`` subscribe site)::

        {
          "type": "subscribe",
          "product_ids": ["BTC-USD", "ETH-USD", ...],
          "channels": [<any subset of supported public channels>]
        }

    The default ``WSClient`` constructor dispatches one subscribe
    with the post-D2.5 5-channel set: ``channels=("ticker", "matches",
    "heartbeat", "status", "level2_batch")``. D2.5 (ticket
    ``86b9znq4w``, 2026-05-18) promoted ``level2_batch`` after the
    R0 reachability spike at D2.5 kickoff confirmed public access on
    the Exchange WS endpoint. D2.1.5 originally shipped with the
    4-channel subset (level2_batch deferred); D2.5 extended same-Bit
    after the R0 verification.

    Coinbase Exchange WS lets a SINGLE subscribe message cover multiple
    channels (the field name is ``channels`` plural, accepting an array
    of channel-name strings). This is structurally distinct from
    Coinbase Advanced Trade WS (the newer API), which requires a
    separate subscribe frame per channel — D2.1.5 follows the Exchange
    WS pattern because the bot already lives there.

    Public channels need no signature, api_key, timestamp, or
    passphrase. A future Bit that wires private channels will go
    through ``sign`` + ``make_ws_headers`` below — those are sentinel-
    stubbed at D2.1.5.

    Args:
        channels: Non-empty list of Coinbase Exchange WS channel names.
            Common values: ``level2_batch`` (batched orderbook updates,
            preferred over deprecated full ``level2``), ``matches``
            (trade ticks), ``ticker`` (real-time best-bid/best-ask +
            last price), ``heartbeat`` (server liveness frames),
            ``status`` (product online/offline transitions).
        product_ids: Non-empty list of Coinbase product IDs (``"BTC-USD"``,
            etc.). Empty list is rejected so a typo doesn't silently
            produce an idle subscription.

    Returns:
        A dict suitable for ``json.dumps(...)`` and ``ws.send(...)``.

    Raises:
        TypeError: ``channels`` or ``product_ids`` not a list. Bare
            strings for either are rejected (they're iterable but
            ``list("BTC-USD")`` would silently produce
            ``["B","T","C","-","U","S","D"]``).
        ValueError: ``channels`` or ``product_ids`` empty.
    """
    if not isinstance(channels, list):
        raise TypeError(
            f"channels must be a list, got {type(channels).__name__}. "
            "Bare strings are rejected to avoid the list(str)-expands-"
            "to-chars footgun."
        )
    if not channels:
        raise ValueError(
            "channels must be non-empty — subscribing to zero channels "
            "would silently establish an idle WS session."
        )
    if not isinstance(product_ids, list):
        raise TypeError(
            f"product_ids must be a list, got "
            f"{type(product_ids).__name__}. Bare strings are rejected "
            "to avoid the list(str)-expands-to-chars footgun."
        )
    if not product_ids:
        raise ValueError(
            "product_ids must be non-empty — subscribing to zero "
            "products would silently establish an idle channel."
        )
    return {
        "type": _SUBSCRIBE_TYPE,
        "product_ids": list(product_ids),
        "channels": list(channels),
    }


def sign(secret: str, timestamp: str, method: str, path: str,
         body: str = "") -> str:
    """Reserved for a future Bit that adds private Coinbase channels.

    At D2.1.5 the operator scoped this Bit to public channels only;
    bronzing public WS data is sufficient to close the
    "non-reproducible Coinbase orderbook" training-data gap and avoids
    provisioning + rotating Coinbase API credentials.

    The Coinbase Exchange WS private-channel auth scheme uses HMAC-
    SHA256 of ``f"{timestamp}{method}{path}{body}"`` against the
    base64-decoded secret, then base64-encodes the digest. When this
    Bit lands, the implementation will mirror that spec (NOT the Kalshi
    RSA-PSS shape — different curve, different message format).
    """
    raise NotImplementedError(
        "coinbase_wire.auth.sign is reserved for a future Bit that adds "
        "private Coinbase channels. D2.1.5 + D2.5 ship public-only "
        "(post-D2.5 default subscribe set is 5 verified-public channels: "
        "ticker / matches / heartbeat / status / level2_batch); no HMAC "
        "handshake is required for any of those. See ticket 86b9zkpny "
        "(D2.1.5 body) + 86b9znq4w (D2.5 level2_batch promotion)."
    )


def make_ws_headers(api_key: str, api_secret: str,
                    passphrase: str = "") -> Dict[str, str]:
    """Reserved for a future Bit that adds private Coinbase channels.

    Analogous to ``kalshi_wire.auth.make_ws_headers`` (which IS
    implemented — Kalshi WS auth is always required). Coinbase WS auth
    is conditional on the subscribed channel set; D2.1.5 sticks to
    public channels so this helper is a sentinel until a private-channel
    Bit needs it.
    """
    raise NotImplementedError(
        "coinbase_wire.auth.make_ws_headers is reserved for a future "
        "Bit that adds private Coinbase channels. D2.1.5 is public-only; "
        "no headers required for the public WS connect. See ticket "
        "86b9zkpny."
    )
