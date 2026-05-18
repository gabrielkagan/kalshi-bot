"""Pure-transport Coinbase wire library — shared by the bot's spot feed
and the collector's Coinbase bronze archiver (D2.2 SHIPPED 2026-05-17,
ticket 86b9zkppk).

D2.1 (ticket ``86b9zkpc6``, 2026-05-17) shipped scaffolding; D2.1.5
(ticket ``86b9zkpny``, 2026-05-17) lands the body. Sub-Bits of the
``86b9zkkv4`` D2.x Coinbase WS bronzing umbrella. Mirrors the D1.1.5
``kalshi_wire/`` "two sides of the same coin" pattern (per the
2026-05-16 AMENDMENT to ``kb/decisions/data-corpus-architecture.md`` §5)
for the Coinbase wire surface:

  > "Capture and replay need to be two sides of the same coin for the
  >  data to serve your needs."

The bronze tape captured by ``collector/coinbase_archiver.py``
(D2.2 SHIPPED) and the live decisions made by ``bot/feeds/coinbase.py``
(refactor in D2.3) MUST agree on what came over the wire. Putting auth + WS
connect/reconnect + envelope construction behind a shared package
eliminates the drift surface — both consumers parse the same bytes the
same way.

Pure-transport leaf — pinned by two import-linter forbidden contracts:

  - ``coinbase_wire-no-bot``       — must not reach into ``bot/``
  - ``coinbase_wire-no-collector`` — must not reach into ``collector/``

Sibling to ``bot/``, ``collector/``, and ``kalshi_wire/`` at the repo
root; NOT a subpackage of any of them. The fourth top-level Python
package.

## Protocol surface (D2.1.5): Coinbase Exchange WS

The wire library targets ``wss://ws-feed.exchange.coinbase.com`` — the
same Coinbase Exchange WS endpoint the bot already consumes via
``bot/feeds/coinbase.py`` (see ``bot.constants.COINBASE_WS_URL``). The
shared-endpoint choice let D2.3 (SHIPPED 2026-05-17, ticket
``86b9zkppt``) be a *structural* refactor — point the existing bot
feed at this WSClient — rather than a protocol-flip.

Coinbase Advanced Trade WS (``wss://advanced-trade-ws.coinbase.com``) is
a separate API surface with a different subscribe shape and different
product coverage; D2.1.5 does NOT target it.

## D2.1.5 scope (current Bit): public channels only

The D2.1 docstring forecast that the body would ship HMAC-SHA256 signing
for private channels. That forecast was **NARROWED** at D2.1.5 kickoff
to public channels only — bronzing public Exchange WS data closes the
non-reproducible Coinbase orderbook training-data gap with zero
credential surface. The sentinel stubs at ``auth.sign`` +
``auth.make_ws_headers`` raise ``NotImplementedError`` with a clear
message pointing to the future private-channel Bit; HMAC will land
there.

Default channel set: ``ticker`` + ``matches`` + ``heartbeat`` +
``status`` (4 public Coinbase Exchange WS channels with confirmed
reachability via the production ``bot/feeds/coinbase.py`` + Coinbase
public docs). ``level2_batch`` is intentionally OMITTED from D2.1.5
defaults — its reachability on the public WS endpoint without auth is
not in-repo verified, and an in-archiver reachability check (observing
``type=error`` subscribe-rejection frames before bronze goes silent on
that channel) is the right place to verify-then-extend the channel
set. A followup ticket adds ``level2_batch`` to defaults once that
verification lands.

Default product set: 7 entries — BTC / ETH / SOL / XRP / HYPE / DOGE /
BNB — mirroring ``bot.constants.COINBASE_PRODUCTS``. HYPE-USD verified
live on Coinbase Exchange 2026-05-10 per the constants-file comment;
BNB-USD landed in main 2026-05-17 via the BNB T1-onboarding ship (PR
#78, ticket ``86b9zmj0c``) with the verification "status=online +
trading_disabled=false" on Coinbase Exchange.

## Submodules (canonical Bit ⇄ surface mapping)

| Submodule | Surface | Bit |
|---|---|---|
| ``coinbase_wire.auth`` | Public subscribe-payload helper + HMAC sentinel stubs | D2.1.5 |
| ``coinbase_wire.ws_client`` | ``WSClient`` + ``Frame`` + ``build_envelope`` | D2.1.5 |

The single-source-of-truth Bit ⇄ submodule mapping; sister docs
(``agent_docs/bot_layout.md``, future README updates) cross-reference
this docstring rather than duplicating the enumeration (mirrors the
D1.1.5 ``kalshi_wire/__init__.py`` convention).

## What this package does NOT do

- No bot-specific state (cross-exchange feed cache, blacklist, schema
  probes) — those stay in ``bot/feeds/coinbase.py``.
- No collector-specific state (write rotation, S3 upload) — those live
  in ``collector/coinbase_archiver.py`` (D2.2 SHIPPED).
- No async at the public API — sync/threading per project anti-pattern.
  asyncio lives INSIDE ``WSClient`` but does not leak through the
  public surface.
- No HMAC handshake at D2.1.5 — public channels only; the ``auth.sign``
  / ``auth.make_ws_headers`` stubs raise ``NotImplementedError`` until
  a future Bit lands private-channel support.
- No wire-level seq-gap detection — Coinbase Exchange WS ``sequence``
  is per-product monotonic, so per-product joining belongs at the
  consumer where product context is held cleanly. ``Frame.sequence_num``
  is passed through verbatim for consumer use.
"""
from __future__ import annotations

# Top-level re-exports. ``Frame`` + ``build_envelope`` are the canonical
# bronze data surface (D0.3 §2 6-field contract); ``WSClient`` is the
# transport class consumers instantiate to receive ``Frame``s. Keeping
# all three discoverable at the package top-level mirrors the D1.1.5
# kalshi_wire convention (R1 Mn3 follow-up precedent there).
from coinbase_wire.ws_client import Frame, WSClient, build_envelope

__all__ = ["auth", "ws_client", "Frame", "WSClient", "build_envelope"]
