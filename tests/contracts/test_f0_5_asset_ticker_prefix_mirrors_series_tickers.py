"""Anti-drift contract: F0.5's `ASSET_TICKER_PREFIX` mirrors `bot.constants.SERIES_TICKERS`.

D1.11.a `LEAGUES_ESPN` pattern (see
`tests/contracts/test_collector_espn_archiver.py::test_leagues_espn_mirrors_bot_leagues`).
When the bot's 7-asset universe changes (new asset promoted to T4 +
added to `SERIES_TICKERS`), THIS test fails RED and the operator must
update F0.5's `ASSET_TICKER_PREFIX` to match before re-running the
falsification script.

F0.5 is read-only research (no live constants), but its 7-asset
universe + 15M-specific ticker prefix MUST stay locked to
`bot.constants.SERIES_TICKERS` so the verdict applies to the actual
production universe.

Parent: kb/decisions/ct-mdp-f0-5-settlement-window-gamma-plan.md
"""

from __future__ import annotations


def test_asset_ticker_prefix_mirrors_bot_series_tickers():
    """F0.5's ASSET_TICKER_PREFIX must equal bot.constants.SERIES_TICKERS.

    Full `dict.__eq__` (key-set + value equality): F0.5 must use the
    canonical `KX<ASSET>15M` prefix verbatim for the canonical 7-asset
    universe. Per impl-R11-M1 correction: ASSET_TICKER_PREFIX is used in
    F0.5 only as (a) the iteration key set in `_load_spot_series_by_asset`
    + (b) this anti-drift pin against SERIES_TICKERS. The script does NOT
    construct a LIKE clause against `settled_trades.ticker` — 15M-window
    discrimination uses the `product_type='15m'` SQL filter instead, which
    is the canonical Kalshi-DB enum (a per-ticker LIKE would be redundant).
    """
    from bot import constants as bot_constants
    from scripts.research import f0_5_settlement_window_gamma as gamma

    expected = dict(bot_constants.SERIES_TICKERS)
    actual = dict(gamma.ASSET_TICKER_PREFIX)

    assert actual == expected, (
        "ASSET_TICKER_PREFIX drift from bot.constants.SERIES_TICKERS.\n"
        f"In bot but NOT F0.5: {set(expected) - set(actual)}\n"
        f"In F0.5 but NOT bot: {set(actual) - set(expected)}\n"
        f"Value mismatches: "
        f"{ {k: (expected.get(k), actual.get(k)) for k in expected if k in actual and expected[k] != actual[k]} }\n"
        "To fix: update scripts/research/f0_5_settlement_window_gamma.py "
        "ASSET_TICKER_PREFIX to mirror the canonical 7-asset universe in "
        "bot/constants.py SERIES_TICKERS."
    )
