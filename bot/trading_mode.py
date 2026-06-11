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
    """Per-strategy live gate (R1-M4): ``is_live(asset)`` OR a strategy-
    scoped override. Today the only override is longshot
    (``LONGSHOT_LIVE_OVERRIDE`` — lets the operator go live with the
    longshot premium-harvest strategy alone while the main pipeline stays
    shadow). For every other strategy this is EXACTLY ``is_live(asset)``
    — main-pipeline behavior unchanged. Consulted at the two existing
    chokepoints only: ``executor.execute()`` (which passes the
    candidate's strategy) and the ``kalshi_client.place_order`` backstop
    (which recovers the strategy from the ``ls-`` client_order_id prefix
    via :func:`strategy_from_client_order_id`)."""
    if is_live(asset):
        return True
    return strategy == "longshot" and bool(_c.LONGSHOT_LIVE_OVERRIDE)


def strategy_from_client_order_id(client_order_id) -> "str | None":
    """Recover the gate-relevant strategy from a client_order_id.

    Longshot stamps ``LONGSHOT_CLIENT_OID_PREFIX`` ('ls-') on every
    placement (R1-M1), which is the only signal available at the
    ``place_order`` API boundary (no candidate dict there). Returns
    'longshot' for prefixed ids, None otherwise (None → plain
    ``is_live`` semantics in :func:`strategy_is_live`)."""
    if isinstance(client_order_id, str) and client_order_id.startswith(
            _c.LONGSHOT_CLIENT_OID_PREFIX):
        return "longshot"
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
