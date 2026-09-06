"""Bit B: regression guards on module docstrings in bot/feeds and bot/fetchers.

T1 (5dca85a, 2026-05-10) added HYPE/DOGE to `config.ASSETS` for shadow
observation. T1.5 (bf8b9a3, 2026-05-10) wired external feeds for the
two new assets per-exchange (Binance/Kraken/Bybit/OKX/CoinGlass with
documented gaps for HYPE on Binance.com + Deribit). But the
module-level docstrings in `bot/feeds/coinbase.py`,
`bot/feeds/__init__.py`, `bot/fetchers/__init__.py`, and
`bot/fetchers/coinglass.py` were not updated and still claimed the
feeds cover only the original 4 assets (BTC/ETH/SOL/XRP).

This pin asserts the docstrings list all 7 assets (BTC/ETH/SOL/XRP/
HYPE/DOGE/BNB — BNB added in T1.5 2026-05-17, ticket 86b9zmj15) so
future asset-onboarding work can grep + verify without re-reading
every module. ClickUp 86b9vrr9c (original HYPE/DOGE Bit B);
86b9zmj15 (BNB T1.5 extension).

Pattern: assert each asset symbol appears literally in the
module-level `__doc__` for each affected module.

Failure mode this guards: a future regression that drops one or more
assets from the docstring while keeping the runtime ASSETS-driven
behavior — silently misleading anyone grepping for asset coverage.
"""

import importlib
import os
import sys

import pytest
import bot.constants  # noqa: F401
import bot.feeds  # noqa: F401
import bot.fetchers  # noqa: F401

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)


ALL_ASSETS = ("BTC", "ETH", "SOL", "XRP", "HYPE", "DOGE", "BNB", "ADA", "BCH", "NEAR", "ZEC")


_TARGET_MODULES = [
    "bot.feeds.coinbase",
    "bot.feeds",
    "bot.fetchers",
    "bot.fetchers.coinglass",
]


@pytest.mark.parametrize("module_name", _TARGET_MODULES)
def test_module_docstring_lists_all_assets(module_name: str):
    """Module docstring must mention every asset in `config.ASSETS`.

    These modules describe feeds/fetchers that runtime-iterate over
    `ASSETS` — the docstring is the human-readable contract and must
    stay in sync. Asset list grows over time (T1 added HYPE/DOGE
    2026-05-10; BNB added 2026-05-17, ticket 86b9zmj0c).
    """
    mod = importlib.import_module(module_name)
    doc = (mod.__doc__ or "")
    missing = [a for a in ALL_ASSETS if a not in doc]
    assert not missing, (
        f"{module_name} module docstring is missing asset symbols: "
        f"{missing}. Update `__doc__` to list every asset in "
        f"`config.ASSETS = {list(ALL_ASSETS)}`. "
        f"ClickUp 86b9vrr9c (HYPE/DOGE) + 86b9zmj0c (BNB)."
    )


def test_cross_exchange_consensus_min_doc_mentions_hype_asymmetry():
    """Bit B mini-guard on ticket 86b9vrr9h: the comment block above
    `CROSS_EXCHANGE_CONSENSUS_MIN` in `bot/constants.py` must mention
    the HYPE dev-env asymmetry (Kraken+Bybit only — Binance.com does
    NOT list HYPE per T1.5 verification). Production-safe since
    `BINANCE_FEED_ENABLED=0` keeps MIN=2; flipping to 1 in dev would
    leave HYPE unable to reach MIN=3 silently.

    Pattern: open `bot/constants.py` and assert that within the comment
    block surrounding `CROSS_EXCHANGE_CONSENSUS_MIN =` there is at
    least one mention of "HYPE" (the dev-env asymmetry warning).
    """
    import inspect
    from bot import constants
    source = inspect.getsource(constants)
    lines = source.split("\n")
    target_idx = next(
        (i for i, ln in enumerate(lines) if "CROSS_EXCHANGE_CONSENSUS_MIN =" in ln),
        None,
    )
    assert target_idx is not None, (
        "CROSS_EXCHANGE_CONSENSUS_MIN definition not found in bot.constants source"
    )
    # Scan the 20 lines before the assignment for the asymmetry warning.
    comment_block = "\n".join(lines[max(0, target_idx - 20):target_idx])
    assert "HYPE" in comment_block, (
        "CROSS_EXCHANGE_CONSENSUS_MIN comment block must mention HYPE — "
        "the dev-env asymmetry (HYPE only feeds Kraken+Bybit; "
        "BINANCE_FEED_ENABLED=1 in dev makes MIN=3 unreachable for HYPE) "
        "is documented in kb/decisions/asset-onboarding-doge-hype-bit-1-5-shipped-may10.md. "
        "ClickUp 86b9vrr9h."
    )
