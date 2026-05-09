"""Bit 4.4: HTTP fetcher classes extracted from bot/_impl.py.

Subpackage hosting daemon-thread HTTP pollers that feed external
derivatives-market signals into the bot's volatility / probability
pipeline:

- ``DeribitDVOLFetcher`` — Deribit DVOL implied-volatility index
  (BTC/ETH only) via REST every ``DVOL_FETCH_INTERVAL`` seconds.
- ``CoinGlassFetcher`` — CoinGlass funding-rate API
  (BTC/ETH/SOL/XRP) via authenticated REST every
  ``COINGLASS_FETCH_INTERVAL`` seconds.

``bot/_impl.py`` does
``from bot.fetchers import DeribitDVOLFetcher, CoinGlassFetcher``
so the runtime construction in ``MainLoop.__init__`` and the
``Optional[DeribitDVOLFetcher]`` type hint on
``VolatilityEngine.__init__`` resolve through the proxy.
"""

from bot.fetchers.coinglass import CoinGlassFetcher  # noqa: F401
from bot.fetchers.deribit import DeribitDVOLFetcher  # noqa: F401
