"""Coinbase WS transport — D2.1 scaffolding stub
(ticket 86b9zkpc6, 2026-05-17). Body deferred to D2.1.5.

Pure-transport leaf that will (post-D2.1.5) be consumed by
``bot/feeds/coinbase.py`` AND (post-D2.2)
``collector/coinbase_archiver.py``. Mirrors the D1.1.5
``kalshi_wire/ws_client.py`` shape — pure-transport, sync public API,
asyncio internal.

Planned surface (D2.1.5):

  - ``WSClient`` class — connects to Coinbase's advanced-trade WS,
    handles HMAC handshake auth (via ``coinbase_wire.auth.make_ws_headers``),
    reconnect with exponential backoff, silence watchdog, 4 sync
    callbacks (``on_session_start``, ``on_frame``, ``on_session_end``,
    ``on_drain_tick``). Mirrors the ``kalshi_wire.ws_client.WSClient``
    contract documented at the top of that module.
  - ``Frame`` dataclass — ``wire_recv_ts`` (captured at frame ingress
    BEFORE ``json.loads``) + ``raw`` (verbatim payload). D0.3 §2 contract
    applies symmetrically to the Coinbase wire.
  - ``build_envelope(frame, *, channel, collector_seq, conn_id) -> Dict``
    — Coinbase variant of the 6-field bronze envelope, structurally
    parallel to ``kalshi_wire.ws_client.build_envelope``.

NO imports from ``bot.*`` or ``collector.*`` (pinned by import-linter
contracts ``coinbase_wire-no-bot`` + ``coinbase_wire-no-collector``).

Anti-patterns honored (root ``CLAUDE.md``):
  - Synchronous public API. asyncio will be INTERNAL to this class.
  - No SQLite touch (wire-only).
  - All silence-watchdog / reconnect tunables become constructor kwargs
    when the body lands; never reach into a bot-side config singleton.

D2.1 ships this file as an EMPTY STUB — instantiating ``WSClient`` will
fail with ``NotImplementedError`` until D2.1.5 populates the body.
"""
from __future__ import annotations


def _d2_1_stub() -> None:
    """Sentinel placeholder so importers can verify the module is
    importable but the body isn't wired yet.

    Removed at D2.1.5 when ``WSClient`` + ``Frame`` + ``build_envelope``
    land.
    """
    raise NotImplementedError(
        "coinbase_wire.ws_client body is deferred to D2.1.5 — see the "
        "D2.x umbrella ticket 86b9zkkv4. D2.1 (86b9zkpc6) ships "
        "scaffolding only."
    )
