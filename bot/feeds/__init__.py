"""Bit 4.5a: WebSocket feed classes (small feeds + orderbook exception).

Subpackage hosting WebSocket feed daemon-thread classes that produce
real-time price/orderbook data for the bot's volatility + execution
pipelines:

- ``CoinbaseFeed`` (`coinbase` submodule) — Coinbase WS feed for
  BTC/ETH/SOL/XRP spot prices with persistent 30-min snapshot buffer.
- ``OrderbookSchemaError`` (`orderbook_schema` submodule) — exception
  raised when a Kalshi WS orderbook message violates the expected wire
  contract (consumed by the still-in-bot/_impl.py ``KalshiFeed`` class;
  Bit 4.5b will move ``KalshiFeed`` here too).
- ``CrossExchangeFeed`` (`cross_exchange` submodule) — Binance/Kraken/
  Bybit WS feeds for lead/lag detection vs Coinbase; takes a
  ``CoinbaseFeed`` reference at construction time.

``bot/_impl.py`` does
``from bot.feeds import CoinbaseFeed, OrderbookSchemaError, CrossExchangeFeed``
so the runtime construction in ``MainLoop.__init__`` and the
``OrderbookSchemaError`` raises inside ``KalshiFeed`` resolve through
the proxy.
"""

from bot.feeds.coinbase import CoinbaseFeed  # noqa: F401
from bot.feeds.cross_exchange import CrossExchangeFeed  # noqa: F401
from bot.feeds.orderbook_schema import OrderbookSchemaError  # noqa: F401
