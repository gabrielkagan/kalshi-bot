"""HMAC-SHA256 auth for Coinbase WS — D2.1 scaffolding stub
(ticket 86b9zkpc6, 2026-05-17). Body deferred to D2.1.5.

This module will house the Coinbase-side equivalents of the
``kalshi_wire.auth`` primitives — but using HMAC-SHA256 (Coinbase's
WS auth scheme) instead of RSA-PSS-SHA256 (Kalshi's). The two wire
libraries are intentionally NOT unified: the auth shapes differ
fundamentally (symmetric vs. asymmetric, message-format conventions,
header names), and forcing a common abstraction at D2.1 would optimize
prematurely.

Planned surface (D2.1.5):

  - ``sign(secret, timestamp, method, path, body) -> str``  — HMAC-SHA256
    of ``f"{timestamp}{method}{path}{body}"`` against the base64-decoded
    secret; base64-encode the digest. Spec follows Coinbase's
    advanced-trade WS auth document.
  - ``make_ws_headers(api_key, api_secret) -> Dict[str, str]`` — produces
    the ``CB-ACCESS-*`` header set + optional ``CB-VERSION``.

NO imports from ``bot.*`` or ``collector.*`` (pinned by import-linter
contracts ``coinbase_wire-no-bot`` + ``coinbase_wire-no-collector`` and
the AST-walk guards in ``tests/contracts/test_coinbase_wire_no_bot_imports.py``
+ ``tests/contracts/test_coinbase_wire_no_collector.py``).

D2.1 ships this file as an EMPTY STUB — calling any function here will
fail with ``NotImplementedError`` until D2.1.5 populates the body.
"""
from __future__ import annotations


def _d2_1_stub() -> None:
    """Sentinel placeholder so importers can verify the module is
    importable but the body isn't wired yet.

    Removed at D2.1.5 when ``sign`` / ``make_ws_headers`` land.
    """
    raise NotImplementedError(
        "coinbase_wire.auth body is deferred to D2.1.5 — see the D2.x "
        "umbrella ticket 86b9zkkv4. D2.1 (86b9zkpc6) ships scaffolding only."
    )
