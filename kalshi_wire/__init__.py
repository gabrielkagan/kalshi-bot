"""Pure-transport Kalshi wire library — shared by ``bot/feeds/kalshi.py``
and ``collector/ws_connection.py``.

D1.1.5 (ticket ``86b9zdhz2``, 2026-05-16). Created in response to the
2026-05-16 AMENDMENT to ``kb/decisions/data-corpus-architecture.md`` §5
which SUPERSEDED the original "collector reimplements the minimum" /
"decline shared utility in v1" decisions after external-advisor input:

  > "Capture and replay need to be two sides of the same coin for the
  >  data to serve your needs."

The bronze tape captured by ``collector/`` and the live decisions made
by ``bot/`` MUST agree on what came over the wire. Putting auth + WS
connect/reconnect/subscribe protocol + 6-field envelope construction
behind a shared package eliminates the drift surface — both consumers
parse the same bytes the same way.

Pure-transport leaf — pinned by two import-linter forbidden contracts:

  - ``kalshi_wire-no-bot``       — must not reach into ``bot/``
  - ``kalshi_wire-no-collector`` — must not reach into ``collector/``

Sibling to ``bot/`` and ``collector/`` at the repo root; NOT a subpackage
of either. The third top-level Python package.

## Submodules (canonical Bit ⇄ surface mapping)

| Submodule | Surface | Bit |
|---|---|---|
| ``kalshi_wire.auth`` | RSA-PSS-SHA256 sign + load + REST/WS header helpers | D1.1.5 Phase 3a |
| ``kalshi_wire.ws_client`` | ``WSClient`` (connect/reconnect/auth/drain/silence-watchdog) + ``Frame`` dataclass + ``build_envelope`` | D1.1.5 Phase 3b |

The single-source-of-truth Bit ⇄ submodule mapping; sister docs
(``CLAUDE.md`` sacred-boundary bullet, ``agent_docs/bot_layout.md``,
``README.md`` Project Structure) cross-reference this docstring rather
than duplicating the enumeration (mirrors the D1.1 ``collector/__init__.py``
convention that lesson L3 in the D1.1 R2 carry-list distilled).

## Consumers

- ``bot/kalshi_client.py`` — REST auth via ``kalshi_wire.auth.sign``
- ``bot/feeds/kalshi.py`` — WS handshake auth via ``kalshi_wire.auth.make_ws_headers``
  (and Phase 3b: ``WSClient`` consumer wrapping the existing state machine)
- ``collector/main_loop.py`` — Phase 4 wire-up: ``WSClient`` consumer piping
  ``Frame.raw`` through ``collector.writer.BronzeWriter``

## What this package does NOT do

- No bot-specific state (orderbook cache, blacklist, schema probes,
  force_resubscribe, snapshot watchdog) — those stay in ``bot/feeds/kalshi.py``
- No collector-specific state (write rotation, S3 upload, bronze envelope
  encoding — well, envelope helpers live here; encoding happens in
  ``collector/writer.py``)
- No async at the public API — sync/threading per project anti-pattern.
  asyncio lives INSIDE ``WSClient`` (mirrors ``bot/feeds/kalshi.py``'s
  current pattern) but does not leak through the public surface.
"""
from __future__ import annotations

# Top-level re-exports — the envelope helpers + Frame dataclass +
# WSClient are the canonical bronze surface, so consumers shouldn't
# have to know the internal submodule split. ``build_envelope`` is
# load-bearing D0.3 §2 contract — keep it discoverable at the package
# top-level (R1 Mn3 follow-up).
from kalshi_wire.ws_client import Frame, WSClient, build_envelope

__all__ = ["auth", "ws_client", "Frame", "WSClient", "build_envelope"]
