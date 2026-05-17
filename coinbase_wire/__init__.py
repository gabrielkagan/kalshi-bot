"""Pure-transport Coinbase wire library — shared by the bot's spot feed
and (post-D2.2) the collector's Coinbase bronze archiver.

D2.1 (ticket ``86b9zkpc6``, 2026-05-17). Sub-Bit of the ``86b9zkkv4``
D2.x Coinbase WS bronzing umbrella. Mirrors the D1.1.5 ``kalshi_wire/``
"two sides of the same coin" pattern (per the 2026-05-16 AMENDMENT to
``kb/decisions/data-corpus-architecture.md`` §5) for the Coinbase wire
surface:

  > "Capture and replay need to be two sides of the same coin for the
  >  data to serve your needs."

The bronze tape captured by the future ``collector/coinbase_archiver.py``
(D2.2) and the live decisions made by ``bot/feeds/coinbase.py`` (refactor
in a later D2 Bit) MUST agree on what came over the wire. Putting auth +
WS connect/reconnect + envelope construction behind a shared package
eliminates the drift surface — both consumers parse the same bytes the
same way.

Pure-transport leaf — pinned by two import-linter forbidden contracts:

  - ``coinbase_wire-no-bot``       — must not reach into ``bot/``
  - ``coinbase_wire-no-collector`` — must not reach into ``collector/``

Sibling to ``bot/``, ``collector/``, and ``kalshi_wire/`` at the repo
root; NOT a subpackage of any of them. The fourth top-level Python
package.

## D2.1 status: SCAFFOLDING ONLY

This Bit ships ZERO behavior change. Empty stub modules with docstrings
+ the import-linter contracts + AST guards. Body lands in subsequent
Bits per the umbrella plan:

  - D2.1.5: ``auth.py`` body (Coinbase HMAC-SHA256 — distinct from
    Kalshi's RSA-PSS) + ``ws_client.py`` body (mirrors D1.1.5 Phase 3b's
    ``WSClient`` shape, adapted for Coinbase's advanced-trade WS protocol).
  - D2.2: ``collector/coinbase_archiver.py`` thin-consumer (mirrors
    ``collector/ws_connection.py`` BronzeArchiver shape).
  - Later D2.x: ``bot/feeds/coinbase.py`` refactor to consume the shared
    ``coinbase_wire.ws_client.WSClient`` (mirrors how ``bot/feeds/kalshi.py``
    was refactored at D1.1.5 Phase 4).
  - D2.5: systemd unit for the Coinbase collector process (mirrors D1.5).

## Submodules (canonical Bit ⇄ surface mapping; populated as Bits ship)

| Submodule | Surface | Bit |
|---|---|---|
| ``coinbase_wire.auth`` | HMAC-SHA256 sign + header helpers (stub at D2.1) | D2.1.5 |
| ``coinbase_wire.ws_client`` | ``WSClient`` + ``Frame`` + ``build_envelope`` (stub at D2.1) | D2.1.5 |

The single-source-of-truth Bit ⇄ submodule mapping; sister docs
(``agent_docs/bot_layout.md``, future README updates) cross-reference
this docstring rather than duplicating the enumeration (mirrors the
D1.1.5 ``kalshi_wire/__init__.py`` convention).

## What this package does NOT do (forecast)

- No bot-specific state (cross-exchange feed cache, blacklist, schema
  probes) — those stay in ``bot/feeds/coinbase.py``.
- No collector-specific state (write rotation, S3 upload) — those live
  in ``collector/coinbase_archiver.py``.
- No async at the public API — sync/threading per project anti-pattern.
  asyncio lives INSIDE ``WSClient`` (mirrors ``kalshi_wire.ws_client``)
  but does not leak through the public surface.
"""
from __future__ import annotations

# D2.1 stub: re-exports intentionally empty — there are no body symbols
# yet. D2.1.5 will populate this with `from coinbase_wire.ws_client
# import Frame, WSClient, build_envelope` mirroring the kalshi_wire
# convention.
__all__: list[str] = []
