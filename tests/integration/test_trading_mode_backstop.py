"""place_order / amend_order trading-mode backstops — behavioral.

The hard backstops in KalshiClient.place_order + amend_order gate all order
PLACEMENT and AMEND at the API boundary: each must refuse (return None, NO API
call) for a governed crypto-15M ticker whose asset is shadow — catching any path
(e.g. taker escalation, mid-flight maker reprice) that reaches the API without
re-entering executor.execute(). cancel_order is intentionally ungated (reduces
exposure). Non-crypto tickers must pass through.

Calls place_order as an unbound method with a mock `self` so the guard is tested
without the heavy KalshiClient init (PEM load etc.) — the guard short-circuits
before touching self._request.

NOTE: the integration-tier autouse fixture (tests/integration/conftest.py) sets
live mode, so this test monkeypatches bot.constants directly to exercise BOTH
shadow and live, overriding the fixture within each test.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import bot.constants as C
from bot.kalshi_client import KalshiClient


def test_place_order_blocks_shadow_crypto_ticker(monkeypatch):
    monkeypatch.setattr(C, "GLOBAL_LIVE_TRADING", False)  # everything shadow
    monkeypatch.setattr(C, "ASSET_LIVE_TRADING", {"BTC": True})  # even asset-on is overridden by GLOBAL off
    client = MagicMock()
    result = KalshiClient.place_order(
        client, "KXBTC15M-26MAY3015-T100", "yes", "buy", 1, yes_price=50)
    assert result is None
    client._request.assert_not_called()  # NO real API call


def test_place_order_allows_live_crypto_ticker(monkeypatch):
    monkeypatch.setattr(C, "GLOBAL_LIVE_TRADING", True)
    monkeypatch.setattr(C, "ASSET_LIVE_TRADING", {"BTC": True})
    monkeypatch.setattr(C, "ASSET_LIVE_TRADING_DEFAULT", False)
    client = MagicMock()
    client._request.return_value = {"order": {"order_id": "ok"}}
    result = KalshiClient.place_order(
        client, "KXBTC15M-26MAY3015-T100", "yes", "buy", 1, yes_price=50)
    client._request.assert_called_once()  # real placement proceeds
    assert result == {"order": {"order_id": "ok"}}


def test_place_order_ignores_non_crypto_ticker_even_when_global_off(monkeypatch):
    # Weather/hourly are NOT governed by this gate — must pass through to the API
    # regardless of GLOBAL_LIVE_TRADING (they have their own *_NO_SIDE_LIVE gates).
    monkeypatch.setattr(C, "GLOBAL_LIVE_TRADING", False)
    monkeypatch.setattr(C, "ASSET_LIVE_TRADING", {"BTC": False})
    client = MagicMock()
    client._request.return_value = {"order": {"order_id": "wx"}}
    result = KalshiClient.place_order(
        client, "KXHIGHNYC-26MAY29-T75", "no", "buy", 1, no_price=40)
    client._request.assert_called_once()  # non-crypto untouched by the gate
    assert result == {"order": {"order_id": "wx"}}


def test_amend_order_blocks_shadow_crypto_ticker(monkeypatch):
    # R2/R3: amend is an order-WORKING call — gated identically so the kill-switch
    # holds for a resting maker on an asset flipped to shadow mid-flight.
    monkeypatch.setattr(C, "GLOBAL_LIVE_TRADING", False)
    monkeypatch.setattr(C, "ASSET_LIVE_TRADING", {"BTC": True})
    client = MagicMock()
    result = KalshiClient.amend_order(
        client, "ord123", "KXBTC15M-26MAY3015-T100", "yes", "buy", yes_price=55)
    assert result is None
    client._request.assert_not_called()  # NO amend API call


def test_amend_order_ignores_non_crypto_ticker(monkeypatch):
    monkeypatch.setattr(C, "GLOBAL_LIVE_TRADING", False)
    monkeypatch.setattr(C, "ASSET_LIVE_TRADING", {"BTC": False})
    client = MagicMock()
    client._request.return_value = {"order": {"order_id": "wx"}}
    result = KalshiClient.amend_order(
        client, "ord9", "KXHIGHNYC-26MAY29-T75", "no", "buy", no_price=40)
    client._request.assert_called_once()  # non-crypto amend untouched
    assert result == {"order": {"order_id": "wx"}}
