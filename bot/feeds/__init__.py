"""Bit 4.5a + 4.5b: WebSocket feed classes.

Subpackage hosting WebSocket feed daemon-thread classes that produce
real-time price/orderbook/fill data for the bot's volatility,
calibration, and execution pipelines:

- ``CoinbaseFeed`` (`coinbase` submodule, Bit 4.5a) — Coinbase WS
  feed for BTC/ETH/SOL/XRP spot prices with persistent 30-min
  snapshot buffer.
- ``OrderbookSchemaError`` (`orderbook_schema` submodule, Bit 4.5a)
  — exception raised when a Kalshi WS orderbook message violates
  the expected wire contract; consumed by ``KalshiFeed``.
- ``CrossExchangeFeed`` (`cross_exchange` submodule, Bit 4.5a) —
  Binance/Kraken/Bybit WS feeds for lead/lag detection vs
  Coinbase; takes a ``CoinbaseFeed`` reference at construction
  time.
- ``KalshiFeed`` (`kalshi` submodule, Bit 4.5b) — Kalshi WS feed
  for fill notifications + per-ticker orderbook snapshots/deltas.
  Largest leaf in the Sprint 4 modularization track (~1,790
  lines).

``bot/_impl.py`` does
``from bot.feeds import CoinbaseFeed, CrossExchangeFeed, KalshiFeed, OrderbookSchemaError``
so the runtime constructions in ``MainLoop.__init__`` and the
``OrderbookSchemaError`` raise/except sites inside ``KalshiFeed``
resolve through the proxy.
"""

from bot.feeds.coinbase import CoinbaseFeed  # noqa: F401
from bot.feeds.cross_exchange import CrossExchangeFeed  # noqa: F401
from bot.feeds.kalshi import KalshiFeed  # noqa: F401
from bot.feeds.orderbook_schema import OrderbookSchemaError  # noqa: F401
