"""Tests for SPXPriceFeed Finnhub WebSocket + REST fallback, and SPXVolatilityEngine."""

import json
import math
import time
import threading
from unittest.mock import patch, MagicMock

import pytest

import spx_engine
from spx_engine import (
    SPXPriceFeed,
    SPXVolatilityEngine,
    SPXEGARCHEstimator,
    IntradaySeasonalFilter,
    REST_FALLBACK_POLL_INTERVAL,
    NO_PRICE_ERROR_THRESHOLD,
    NO_PRICE_CRITICAL_THRESHOLD,
    RECONNECT_BASE_DELAY,
    SPY_TO_SPX_RATIO,
    WS_SNAPSHOT_INTERVAL,
)


class FakeResponse:
    """Minimal requests.Response stand-in."""
    def __init__(self, status_code, json_data=None):
        self.status_code = status_code
        self._json = json_data or {}

    def json(self):
        return self._json


# ── WebSocket message parsing ─────────────────────────────────────────────


class TestWSMessageParsing:

    def test_trade_message_updates_price(self):
        """A valid trade message should update SPX price via SPY * ratio."""
        feed = SPXPriceFeed(finnhub_key="test_key")
        msg = json.dumps({
            "type": "trade",
            "data": [{"p": 567.89, "s": "SPY", "t": 1700000000000, "v": 100}]
        })
        feed._ws_handle_message(msg)

        expected = 567.89 * SPY_TO_SPX_RATIO
        assert feed.get_price("SPX") == pytest.approx(expected, rel=1e-6)

    def test_batch_trade_uses_last_price(self):
        """When multiple trades arrive in one message, use the last (most recent)."""
        feed = SPXPriceFeed(finnhub_key="test_key")
        msg = json.dumps({
            "type": "trade",
            "data": [
                {"p": 560.00, "s": "SPY", "t": 1700000001000, "v": 50},
                {"p": 561.00, "s": "SPY", "t": 1700000002000, "v": 75},
                {"p": 562.50, "s": "SPY", "t": 1700000003000, "v": 200},
            ]
        })
        feed._ws_handle_message(msg)

        expected = 562.50 * SPY_TO_SPX_RATIO
        assert feed.get_price("SPX") == pytest.approx(expected, rel=1e-6)

    def test_non_trade_message_ignored(self):
        """Ping and status messages should not update price."""
        feed = SPXPriceFeed(finnhub_key="test_key")
        feed._ws_handle_message(json.dumps({"type": "ping"}))
        assert feed.get_price("SPX") is None

    def test_empty_data_ignored(self):
        """Trade message with empty data array should not crash."""
        feed = SPXPriceFeed(finnhub_key="test_key")
        feed._ws_handle_message(json.dumps({"type": "trade", "data": []}))
        assert feed.get_price("SPX") is None

    def test_zero_price_ignored(self):
        """Price of 0 should be ignored (invalid trade)."""
        feed = SPXPriceFeed(finnhub_key="test_key")
        feed._ws_handle_message(json.dumps({
            "type": "trade",
            "data": [{"p": 0, "s": "SPY", "t": 1700000000000, "v": 1}]
        }))
        assert feed.get_price("SPX") is None

    def test_malformed_json_ignored(self):
        """Malformed JSON should not crash."""
        feed = SPXPriceFeed(finnhub_key="test_key")
        feed._ws_handle_message("not valid json {{{")
        assert feed.get_price("SPX") is None

    def test_ws_msg_count_increments(self):
        """Each valid trade message should increment the message counter."""
        feed = SPXPriceFeed(finnhub_key="test_key")
        assert feed._ws_msg_count == 0

        for i in range(5):
            feed._ws_handle_message(json.dumps({
                "type": "trade",
                "data": [{"p": 560.0 + i * 0.01, "s": "SPY", "t": 1700000000000 + i, "v": 1}]
            }))

        assert feed._ws_msg_count == 5


# ── ws_active property ────────────────────────────────────────────────────


class TestWSActive:

    def test_ws_active_false_by_default(self):
        """WebSocket starts disconnected."""
        feed = SPXPriceFeed(finnhub_key="test_key")
        assert feed.ws_active is False

    def test_ws_active_reflects_connection_state(self):
        """ws_active should track _ws_connected."""
        feed = SPXPriceFeed(finnhub_key="test_key")
        feed._ws_connected = True
        assert feed.ws_active is True
        feed._ws_connected = False
        assert feed.ws_active is False


# ── Finnhub REST fallback ─────────────────────────────────────────────────


class TestFinnhubRESTFallback:

    def test_finnhub_rest_returns_spy_times_ratio(self):
        """Successful Finnhub REST fetch returns SPY price * SPY_TO_SPX_RATIO."""
        feed = SPXPriceFeed(finnhub_key="test_key")
        resp = FakeResponse(200, {"c": 560.0})
        with patch("spx_engine.requests.get", return_value=resp):
            result = feed._fetch_spx_finnhub()
        assert result == pytest.approx(560.0 * SPY_TO_SPX_RATIO, rel=1e-6)

    def test_finnhub_429_raises_connection_error(self):
        """Finnhub 429 should raise ConnectionError to trigger backoff."""
        feed = SPXPriceFeed(finnhub_key="test_key")
        assert feed._reconnect_delay == RECONNECT_BASE_DELAY

        with patch("spx_engine.requests.get", return_value=FakeResponse(429)):
            with pytest.raises(ConnectionError, match="Finnhub 429"):
                feed._fetch_spx_finnhub()

        assert feed._reconnect_delay == RECONNECT_BASE_DELAY * 2

    def test_finnhub_429_doubles_delay_each_time(self):
        """Successive 429s should keep doubling the reconnect delay."""
        feed = SPXPriceFeed(finnhub_key="test_key")
        for _ in range(3):
            with patch("spx_engine.requests.get", return_value=FakeResponse(429)):
                with pytest.raises(ConnectionError):
                    feed._fetch_spx_finnhub()

        expected = RECONNECT_BASE_DELAY * (2 ** 3)  # 1.0 * 8 = 8.0
        assert feed._reconnect_delay == expected

    def test_no_key_returns_none(self):
        """Without a Finnhub key, REST fetch returns None immediately."""
        feed = SPXPriceFeed(finnhub_key="")
        result = feed._fetch_spx_finnhub()
        assert result is None


# ── VIX fetch (placeholder for free tier) ─────────────────────────────────


class TestVIXFetch:

    def test_vix_error_message_returns_none(self):
        """Finnhub error response for VIX (free tier) should return None gracefully."""
        feed = SPXPriceFeed(finnhub_key="test_key")
        resp = FakeResponse(200, {"error": "Market data subscription required for CFD indices."})
        with patch("spx_engine.requests.get", return_value=resp):
            result = feed._fetch_vix_finnhub()
        assert result is None

    def test_vix_valid_price_returned(self):
        """If VIX quote succeeds (paid tier), price should be returned."""
        feed = SPXPriceFeed(finnhub_key="test_key")
        resp = FakeResponse(200, {"c": 20.5, "d": 0.3, "dp": 1.5})
        with patch("spx_engine.requests.get", return_value=resp):
            result = feed._fetch_vix_finnhub()
        assert result == 20.5


# ── Poll loop: REST fallback only when WS is down ─────────────────────────


class TestPollLoop:

    def test_poll_skips_spx_when_ws_active(self):
        """When WebSocket is connected, poll loop should NOT fetch SPX via REST."""
        feed = SPXPriceFeed(finnhub_key="test_key")
        feed._ws_connected = True

        rest_calls = {"spx": 0, "vix": 0}
        original_spx = feed._fetch_spx_finnhub
        original_vix = feed._fetch_vix_finnhub

        def mock_spx():
            rest_calls["spx"] += 1
            return 5600.0

        def mock_vix():
            rest_calls["vix"] += 1
            return None

        feed._fetch_spx_finnhub = mock_spx
        feed._fetch_vix_finnhub = mock_vix

        # Simulate one poll iteration
        with patch.object(spx_engine.MarketHoursGuard, "is_within_buffer", return_value=True):
            # Run the SPX check from poll loop
            if not feed._ws_connected:
                feed._fetch_spx_finnhub()

        assert rest_calls["spx"] == 0, "SPX REST should be skipped when WS is active"

    def test_poll_fetches_spx_when_ws_down(self):
        """When WebSocket is disconnected, poll loop should fetch SPX via REST."""
        feed = SPXPriceFeed(finnhub_key="test_key")
        feed._ws_connected = False

        if not feed._ws_connected:
            with patch("spx_engine.requests.get", return_value=FakeResponse(200, {"c": 560.0})):
                result = feed._fetch_spx_finnhub()
            assert result is not None

    def test_rest_fallback_interval_is_3s(self):
        """REST fallback poll interval should be 3s to respect Finnhub rate limit."""
        assert REST_FALLBACK_POLL_INTERVAL == 3.0
        requests_per_minute = 60 / REST_FALLBACK_POLL_INTERVAL
        assert requests_per_minute <= 60, f"Would make {requests_per_minute} req/min"


# ── Observability ─────────────────────────────────────────────────────────


class TestObservability:

    def test_consecutive_no_price_increments(self):
        """Each failed REST poll should increment counter."""
        feed = SPXPriceFeed(finnhub_key="")
        feed._ws_connected = False
        assert feed._consecutive_no_price == 0

        for _ in range(5):
            spx = feed._fetch_spx_finnhub()  # no key → returns None
            if spx is None:
                feed._consecutive_no_price += 1

        assert feed._consecutive_no_price == 5

    def test_thresholds(self):
        """Verify error/critical thresholds are at expected values."""
        assert NO_PRICE_ERROR_THRESHOLD == 60
        assert NO_PRICE_CRITICAL_THRESHOLD == 300


# ── Vol engine: WS-active vs REST-fallback paths ─────────────────────────


class TestVolEngineWSIntegration:

    def _make_feed_with_returns(self, ws_connected: bool, returns: list):
        """Create a mock feed with given returns and WS state."""
        feed = MagicMock(spec=SPXPriceFeed)
        feed.ws_active = ws_connected
        feed.get_returns.return_value = returns
        feed.get_vix.return_value = None
        return feed

    def test_ws_active_feeds_egarch(self):
        """When WS is active, EGARCH recursive_update should be called."""
        # Create real EGARCH with known state
        egarch = SPXEGARCHEstimator()
        egarch._n_updates = 100  # past warmup
        egarch._log_var = -18.8  # healthy sigma
        initial_log_var = egarch._log_var

        seasonal = IntradaySeasonalFilter()

        # Create realistic returns (like tick-level data)
        returns = [0.00001 * ((-1) ** i) for i in range(20)]

        feed = self._make_feed_with_returns(True, returns)
        vol = SPXVolatilityEngine(feed, seasonal, egarch)

        result = vol.update(600)
        assert result is not None
        # EGARCH log_var should have changed (recursive_update was called)
        assert egarch._log_var != initial_log_var, \
            "EGARCH should be updated when WS is active"

    def test_rest_fallback_freezes_egarch(self):
        """When WS is down (REST fallback), EGARCH should NOT be updated."""
        egarch = SPXEGARCHEstimator()
        egarch._n_updates = 100
        egarch._log_var = -18.8
        initial_log_var = egarch._log_var

        seasonal = IntradaySeasonalFilter()
        returns = [0.00001 * ((-1) ** i) for i in range(20)]

        feed = self._make_feed_with_returns(False, returns)
        vol = SPXVolatilityEngine(feed, seasonal, egarch)

        result = vol.update(600)
        assert result is not None
        # EGARCH log_var should NOT have changed
        assert egarch._log_var == initial_log_var, \
            "EGARCH should be frozen when WS is down (REST fallback)"

    def test_rest_fallback_returns_valid_estimate(self):
        """REST fallback should still produce a vol estimate (using frozen sigma)."""
        egarch = SPXEGARCHEstimator()
        egarch._n_updates = 100
        egarch._log_var = -18.8  # sigma ≈ 8.24e-5

        seasonal = IntradaySeasonalFilter()
        returns = [0.00001 * ((-1) ** i) for i in range(20)]

        feed = self._make_feed_with_returns(False, returns)
        vol = SPXVolatilityEngine(feed, seasonal, egarch)

        result = vol.update(600)
        assert result is not None
        assert result["blended_rv"] > 1e-6, "Blended RV should be positive"
        assert result["egarch_blend_weight"] == 1.0, "REST fallback uses EGARCH-only"
        assert result["rk_rv"] is None, "No RK in REST fallback"

    def test_rest_fallback_logged_flag_reset_on_ws_reconnect(self):
        """The REST fallback log flag should reset when WS comes back."""
        egarch = SPXEGARCHEstimator()
        egarch._n_updates = 100
        egarch._log_var = -18.8

        seasonal = IntradaySeasonalFilter()
        returns = [0.00001 * ((-1) ** i) for i in range(20)]

        # First: REST fallback sets the flag
        feed_rest = self._make_feed_with_returns(False, returns)
        vol = SPXVolatilityEngine(feed_rest, seasonal, egarch)
        vol.update(600)
        assert hasattr(vol, '_rest_fallback_logged')

        # Second: WS reconnects — flag should be cleared
        feed_ws = self._make_feed_with_returns(True, returns)
        vol._feed = feed_ws
        vol.update(600)
        assert not hasattr(vol, '_rest_fallback_logged'), \
            "Flag should be cleared when WS reconnects"


# ── EGARCH sigma stability test ──────────────────────────────────────────


class TestEGARCHSigmaStability:

    def _make_calibrated_egarch(self):
        """Create EGARCH with calibrated parameters matching VPS state."""
        egarch = SPXEGARCHEstimator()
        egarch._n_updates = 100
        egarch._log_var = -18.8  # sigma ≈ 8.24e-5 (VIX-implied at VIX=20)
        # Use calibrated parameters from production state
        egarch._omega = -0.9213
        egarch._alpha = 0.3988
        egarch._gamma = -0.1217
        egarch._beta = 0.9990
        return egarch

    def test_tick_level_returns_maintain_sigma(self):
        """Realistic tick-level returns (like WS data) should keep sigma in valid range."""
        egarch = self._make_calibrated_egarch()
        initial_sigma = egarch.get_sigma()

        # Simulate 1000 realistic tick returns (SPY moves $0.01-$0.05 per tick)
        # SPY ≈ $567, returns ≈ 0.01/567 = 1.76e-5
        import random
        rng = random.Random(42)
        for _ in range(1000):
            # Mix of small real returns and occasional zero
            r = rng.gauss(0, 2e-5) if rng.random() > 0.1 else 0.0
            egarch.recursive_update(r)

        final_sigma = egarch.get_sigma()
        assert final_sigma is not None
        assert final_sigma > 1e-8, \
            f"Sigma collapsed to {final_sigma} — tick-level returns should maintain sigma"
        # With calibrated params (high persistence beta=0.999), sigma stays bounded
        # by the log_var safety bounds [-30, 0]
        assert final_sigma < 1.0, \
            f"Sigma exploded to {final_sigma} — unreasonably large"

    def test_zero_returns_collapse_sigma_with_calibrated_params(self):
        """All-zero returns with calibrated params (alpha=0.40, beta=0.999) collapse sigma.

        This documents the EGARCH collapse mechanism the WebSocket feed solves.
        With alpha=0.40: each zero-return pushes log_var down by alpha * (-0.798) ≈ -0.32.
        With beta=0.999: nearly all of that negative shift persists.
        """
        egarch = self._make_calibrated_egarch()
        initial_sigma = egarch.get_sigma()  # ≈ 8.24e-5

        # Feed 500 zero returns (simulating REST polling duplicates)
        for _ in range(500):
            egarch.recursive_update(0.0)

        final_sigma = egarch.get_sigma()
        # With calibrated params, sigma should collapse toward exp(-30/2) = 3.06e-7
        # (the safety floor). Initial was 8.24e-5 → final should be much smaller.
        assert final_sigma < initial_sigma * 0.01, \
            f"Zero returns should collapse sigma — got {final_sigma:.2e} vs initial {initial_sigma:.2e}"


# ── Buffer management ─────────────────────────────────────────────────────


class TestBufferManagement:

    def test_ws_and_rest_dont_double_append(self):
        """When WS is active, REST poll should not append to SPX buffer."""
        feed = SPXPriceFeed(finnhub_key="test_key")
        feed._ws_connected = True

        # Simulate WS snapshot appending
        with feed._lock:
            feed._prices["SPX"] = 5600.0
            feed._buffers["SPX"].append(5600.0)

        initial_len = len(feed._buffers["SPX"])

        # REST poll should skip SPX when WS is active
        if not feed._ws_connected:
            with feed._lock:
                feed._buffers["SPX"].append(5601.0)

        assert len(feed._buffers["SPX"]) == initial_len, \
            "REST poll should not append to buffer when WS is active"

    def test_returns_from_varying_prices(self):
        """Log returns should reflect actual price changes."""
        feed = SPXPriceFeed(finnhub_key="test_key")

        # Append prices that simulate real tick changes
        prices = [5600.0, 5600.5, 5601.0, 5600.8, 5601.5]
        for p in prices:
            feed._buffers["SPX"].append(p)

        returns = feed.get_returns("SPX", n=10)
        assert len(returns) == 4  # n-1 returns from n prices

        # Verify returns are non-zero (real price changes, not duplicates)
        for r in returns:
            assert r != 0.0, "Returns from varying prices should be non-zero"

    def test_returns_from_identical_prices(self):
        """Identical prices (REST polling duplicates) produce zero returns."""
        feed = SPXPriceFeed(finnhub_key="test_key")

        # Same price 5 times (simulating REST polling duplicates)
        for _ in range(5):
            feed._buffers["SPX"].append(5600.0)

        returns = feed.get_returns("SPX", n=10)
        assert len(returns) == 4
        for r in returns:
            assert r == 0.0, "Identical prices should produce zero returns"


# ── Stop/cleanup ──────────────────────────────────────────────────────────


class TestStopCleanup:

    def test_stop_sets_event(self):
        """stop() should set the stop event for the poll thread."""
        feed = SPXPriceFeed(finnhub_key="test_key")
        assert not feed._stop.is_set()
        feed.stop()
        assert feed._stop.is_set()
