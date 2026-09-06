"""Trading-mode control — the single source of truth for live vs shadow.

One modular gate added ALONGSIDE (defense-in-depth with) the still-present
scattered inline `_15M_SHADOW` checks (~6 candidate-append sites, 4 assets
hardcoded — the anti-pattern this complements; both fail toward shadow). Every real order
flows through `executor.execute()` → `kalshi_client.place_order()`; both consult
`is_live(asset)` here, so the entire live/shadow surface is one file.

Reads `bot.constants` via module-attribute access (NOT a from-import binding) so
flipping `GLOBAL_LIVE_TRADING` or an `ASSET_LIVE_TRADING` entry is a RUNTIME
kill-switch — takes effect on the next decision with no restart (mirrors the
`_cal_state` / `_telegram_state` mutation-freshness pattern in bot/CLAUDE.md).

Semantics (fail-safe):
    is_live(asset) == GLOBAL_LIVE_TRADING AND ASSET_LIVE_TRADING.get(asset, DEFAULT)
  - GLOBAL off      → everything shadow (one-flag master kill)
  - asset off       → that asset shadow
  - unknown asset   → DEFAULT (shipped False = never trades live until enabled)

Clean leaf: imports only `bot.constants`.
"""
from __future__ import annotations

import bot.constants as _c


def is_live(asset: str) -> bool:
    """True iff REAL orders should be placed for ``asset`` right now."""
    if not _c.GLOBAL_LIVE_TRADING:
        return False
    return bool(_c.ASSET_LIVE_TRADING.get(asset, _c.ASSET_LIVE_TRADING_DEFAULT))


def strategy_is_live(strategy, asset: str) -> bool:
    """Per-strategy live gate (R1-M4, extended at Bit T-1; asset-scoped at
    R4-M1): for the two engine-owned strategies the result is
    ``(is_live(asset) OR <override>) AND asset in <validated set>``. Two
    overrides exist — longshot (``LONGSHOT_LIVE_OVERRIDE``) and twaplock
    (``TWAPLOCK_LIVE_OVERRIDE``) — each lets the operator go live with
    that ONE strategy while the main pipeline (and the sibling strategy)
    stays shadow. The live-universe check (``LONGSHOT_LIVE_ASSETS`` =
    the 02b positive set + BNB per the 2026-06-12 operator directive —
    see the constants comment; ``TWAPLOCK_LIVE_ASSETS`` mirrors 01b
    TRACKED) deliberately gates the WHOLE strategy branch, not just the
    override leg: even in a future dual-live posture (GLOBAL +
    ASSET_LIVE_TRADING flipped on), a strategy trades only its declared
    live universe — an asset being main-pipeline live says nothing about
    longshot/twaplock edge there, and ADA/BCH stay excluded (Kalshi 15M
    series not yet listed; T1 zero-live-orders shadow designation
    ADA_15M_SHADOW/BCH_15M_SHADOW), as do NEAR/ZEC (series listed since
    2026-06-30 but T1 shadow 2026-09-05 — NEAR_15M_SHADOW/ZEC_15M_SHADOW —
    and outside the 01b/02b validation corpora). For every other strategy this is
    EXACTLY ``is_live(asset)`` — main-pipeline behavior unchanged.
    Consulted at the two existing chokepoints only: ``executor.execute()``
    (which passes the candidate's strategy) and the
    ``kalshi_client.place_order`` backstop (which recovers the strategy
    from the engine-owned client_order_id prefix — ``ls-``/``tw-`` — via
    :func:`strategy_from_client_order_id`). Override flags + asset sets
    are read live via module-attribute access (runtime kill-switch
    pattern)."""
    if strategy == "longshot":
        return ((is_live(asset) or bool(_c.LONGSHOT_LIVE_OVERRIDE))
                and asset in _c.LONGSHOT_LIVE_ASSETS)
    if strategy == "twaplock":
        return ((is_live(asset) or bool(_c.TWAPLOCK_LIVE_OVERRIDE))
                and asset in _c.TWAPLOCK_LIVE_ASSETS)
    return is_live(asset)


def strategy_from_client_order_id(client_order_id) -> "str | None":
    """Recover the gate-relevant strategy from a client_order_id.

    Engine-owned strategies stamp a prefix on every placement (longshot
    'ls-' per R1-M1; twaplock 'tw-' per Bit T-1) — the only signal
    available at the ``place_order`` API boundary (no candidate dict
    there). The prefix→strategy map is single-sourced in
    ``bot.constants.ENGINE_OWNED_OID_PREFIX_TO_STRATEGY``. Returns the
    strategy for prefixed ids, None otherwise (None → plain ``is_live``
    semantics in :func:`strategy_is_live`)."""
    if isinstance(client_order_id, str):
        for prefix, strategy in _c.ENGINE_OWNED_OID_PREFIX_TO_STRATEGY.items():
            if client_order_id.startswith(prefix):
                return strategy
    return None


def mode_reason(asset: str) -> str:
    """Why the gate decided as it did — for structured logging at the chokepoint.
    Returns 'live' | 'global_shadow' | 'asset_shadow'."""
    if not _c.GLOBAL_LIVE_TRADING:
        return "global_shadow"
    if not bool(_c.ASSET_LIVE_TRADING.get(asset, _c.ASSET_LIVE_TRADING_DEFAULT)):
        return "asset_shadow"
    return "live"


def asset_from_ticker(ticker: str):
    """Map a Kalshi market ticker to the crypto asset this gate governs, or None.
    Matches only the 15M-crypto series (``KX<ASSET>15M-…``) so the place_order
    backstop never touches non-crypto products (daily/hourly/weather). Returns the
    asset key (e.g. 'BTC') or None if the ticker isn't a governed crypto-15M market."""
    if not ticker:
        return None
    for a in _c.ASSET_LIVE_TRADING:
        if ticker.startswith("KX" + a + "15M"):
            return a
    return None
