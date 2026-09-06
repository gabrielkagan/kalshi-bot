"""Bit 4.4: HTTP fetcher classes extracted from bot/_impl.py.

Subpackage hosting daemon-thread HTTP pollers that feed external
derivatives-market signals into the bot's volatility / probability
pipeline:

- ``DeribitDVOLFetcher`` — Deribit DVOL implied-volatility index
  (BTC/ETH only — DOGE has perp coverage but no DVOL index;
  HYPE not listed on Deribit per T1.5 verification) via REST every
  ``DVOL_FETCH_INTERVAL`` seconds.
- ``CoinGlassFetcher`` — CoinGlass funding-rate API for every
  symbol in ``bot.config.ASSETS`` (BTC/ETH/SOL/XRP/HYPE/DOGE post-T1
  2026-05-10; BNB post-T1 2026-05-17 ticket 86b9zmj0c;
  ``COINGLASS_SYMBOLS["BNB"]="BNB"`` entry added in T1.5 ticket
  86b9zmj15; ADA + BCH post-T1 2026-05-30 + NEAR + ZEC post-T1 2026-09-05 15M shadow — their
  CoinGlass/external-feed entries deferred to T1.5) via authenticated REST every
  ``COINGLASS_FETCH_INTERVAL`` seconds.

``bot/_impl.py`` does
``from bot.fetchers import DeribitDVOLFetcher, CoinGlassFetcher``
so the runtime construction in ``MainLoop.__init__`` and the
``Optional[DeribitDVOLFetcher]`` type hint on
``VolatilityEngine.__init__`` resolve through the proxy.
"""

from bot.fetchers.coinglass import CoinGlassFetcher  # noqa: F401
from bot.fetchers.deribit import DeribitDVOLFetcher  # noqa: F401
