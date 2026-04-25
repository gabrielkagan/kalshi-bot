"""Step #2 of the architectural rebuild — apply circuit breakers
to KalshiClient REST GET methods.

Background:
  Apr 25 09:34 UTC sports-400 cascade. Apr 25 morning Kalshi
  /events endpoint returning 400 for ALL series. Each call cost
  ~1s × dozens of series = main-thread stalls. Circuit breakers
  (commit 228d4c2) auto-disable a failing source after N failures
  so the cascade self-mitigates.

Scope:
  Only safe-to-degrade GETs are wrapped — get_balance, get_events,
  get_orderbook, get_settlements. Writes (place_order, cancel_order,
  amend_order) are NOT wrapped because silently dropping a trade is
  worse than letting the call fail loudly.

Per-key isolation:
  Each (method, identifier) gets its own breaker so a failing source
  doesn't trip a healthy one:
    - kalshi_events_<series_ticker>     (per series)
    - kalshi_orderbook_<ticker>         (per ticker)
    - kalshi_balance                    (singleton)
    - kalshi_settlements_<ticker|all>   (per ticker, "all" if None)
"""

import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot
from bot import KalshiClient


def _make_client_with_mocked_request():
    """Construct a KalshiClient bypassing __init__ (which requires
    an API key + RSA private key). Inject a mock _request."""
    c = KalshiClient.__new__(KalshiClient)
    c._request = MagicMock(return_value={"ok": True})
    return c


class TestKalshiClientBreakerImports(unittest.TestCase):
    """AST regression: bot.py must import REGISTRY from
    circuit_breaker so the breakers are reachable from
    KalshiClient methods."""

    def test_bot_imports_registry(self):
        with open(bot.__file__) as f:
            src = f.read()
        self.assertIn(
            "from circuit_breaker import", src,
            "bot.py must import from circuit_breaker so REGISTRY is "
            "available to KalshiClient methods.")
        self.assertIn(
            "REGISTRY", src,
            "REGISTRY symbol must appear in bot.py")


class TestKalshiClientGetBalanceBreaker(unittest.TestCase):

    def setUp(self):
        # Reset the breaker between tests.
        from circuit_breaker import REGISTRY
        REGISTRY._breakers.pop("kalshi_balance", None)

    def test_get_balance_calls_underlying_request_on_success(self):
        c = _make_client_with_mocked_request()
        result = c.get_balance()
        self.assertIsNotNone(result)
        c._request.assert_called_once()

    def test_get_balance_short_circuits_when_open(self):
        from circuit_breaker import REGISTRY
        c = _make_client_with_mocked_request()
        # Trip the breaker to OPEN.
        c._request.return_value = None  # simulate API error
        for _ in range(3):
            c.get_balance()
        # Now breaker should be OPEN. Underlying _request should NOT
        # be called.
        c._request.reset_mock()
        result = c.get_balance()
        self.assertIsNone(result)
        c._request.assert_not_called()

    def test_get_balance_records_failure_on_none_response(self):
        from circuit_breaker import REGISTRY
        c = _make_client_with_mocked_request()
        c._request.return_value = None
        c.get_balance()
        b = REGISTRY.get("kalshi_balance")
        self.assertEqual(b.failure_count, 1)


class TestKalshiClientGetEventsBreakerPerSeries(unittest.TestCase):
    """Per-series isolation — the sports-400 cascade scenario.
    Failing series_ticker=KXFIFAGAME must NOT trip series_ticker=
    KXNBAGAME's breaker."""

    def setUp(self):
        from circuit_breaker import REGISTRY
        for k in list(REGISTRY._breakers.keys()):
            if k.startswith("kalshi_events_"):
                REGISTRY._breakers.pop(k)

    def test_per_series_breaker_isolation(self):
        from circuit_breaker import REGISTRY
        c = _make_client_with_mocked_request()
        # Fail KXFIFAGAME 3 times → its breaker trips.
        c._request.return_value = None
        for _ in range(3):
            c.get_events(series_ticker="KXFIFAGAME")
        fifa_breaker = REGISTRY.get("kalshi_events_KXFIFAGAME")
        self.assertTrue(fifa_breaker.is_open(),
            "FIFA breaker should be open after 3 failures.")
        # KXNBAGAME has its own breaker, unaffected.
        nba_breaker = REGISTRY.get("kalshi_events_KXNBAGAME")
        self.assertFalse(nba_breaker.is_open(),
            "NBA breaker must remain closed despite FIFA failures. "
            "Per-series isolation is the whole point.")

    def test_get_events_open_breaker_short_circuits(self):
        from circuit_breaker import REGISTRY
        c = _make_client_with_mocked_request()
        c._request.return_value = None
        for _ in range(3):
            c.get_events(series_ticker="KXFIFAGAME")
        c._request.reset_mock()
        result = c.get_events(series_ticker="KXFIFAGAME")
        self.assertIsNone(result)
        c._request.assert_not_called()

    def test_get_events_no_series_ticker_uses_all_key(self):
        """When series_ticker=None, the breaker key falls back to
        'kalshi_events_all' so this case is still protected."""
        from circuit_breaker import REGISTRY
        c = _make_client_with_mocked_request()
        c._request.return_value = None
        for _ in range(3):
            c.get_events(series_ticker=None)
        b = REGISTRY.get("kalshi_events_all")
        self.assertTrue(b.is_open())


# Per-ticker test removed — round-1 A2 fix groups by series instead.
# See TestKalshiClientOrderbookKeyGrouping below.


class TestKalshiClientGetSettlementsBreaker(unittest.TestCase):

    def setUp(self):
        from circuit_breaker import REGISTRY
        for k in list(REGISTRY._breakers.keys()):
            if k.startswith("kalshi_settlements_"):
                REGISTRY._breakers.pop(k)

    def test_get_settlements_uses_breaker(self):
        from circuit_breaker import REGISTRY
        c = _make_client_with_mocked_request()
        c._request.return_value = None
        for _ in range(3):
            c.get_settlements()
        b = REGISTRY.get("kalshi_settlements_all")
        self.assertTrue(b.is_open())


class TestKalshiClientWritesAreNotWrapped(unittest.TestCase):
    """Writes MUST NOT be wrapped — silently dropping a trade via
    circuit breaker is worse than letting it fail loudly. Round-1
    A7: cover all 3 writes via parametrized AST check."""

    def _assert_method_not_wrapped(self, method_name):
        with open(bot.__file__) as f:
            src = f.read()
        i = src.index(f"def {method_name}")
        j = src.index("\n    def ", i + 1)
        body = src[i:j]
        self.assertNotIn(
            "_BREAKER_REGISTRY", body,
            f"{method_name} MUST NOT use a circuit breaker — failed "
            f"trades must surface loudly, not silently no-op.")

    def test_place_order_not_wrapped(self):
        self._assert_method_not_wrapped("place_order")

    def test_cancel_order_not_wrapped(self):
        self._assert_method_not_wrapped("cancel_order")

    def test_amend_order_not_wrapped(self):
        self._assert_method_not_wrapped("amend_order")


class TestKalshiClientSuccessDetection(unittest.TestCase):
    """Round-1 A1 fix: empty dict and error-shaped responses must
    count as failures, not successes."""

    def setUp(self):
        from circuit_breaker import REGISTRY
        for k in list(REGISTRY._breakers.keys()):
            REGISTRY._breakers.pop(k)

    def test_empty_dict_response_is_NOT_failure(self):
        """Round-4 P1 reversal: empty `{}` is NOT failure. Kalshi
        legitimately returns empty `{"settlements": []}`-shaped
        payloads, and `_request()` may return `{}` on a 200 with
        empty body — we can't disambiguate without per-endpoint
        contract knowledge. Erring on side of NOT tripping."""
        from circuit_breaker import REGISTRY
        c = _make_client_with_mocked_request()
        c._request.return_value = {}
        c.get_balance()
        b = REGISTRY.get("kalshi_balance")
        self.assertEqual(b.failure_count, 0,
            "Empty dict must NOT trip the breaker — would false-"
            "positive on weekend windows with no settlements/etc.")

    def test_error_shaped_response_is_failure(self):
        """200 OK with `{"error": ...}` payload counts as failure."""
        from circuit_breaker import REGISTRY
        c = _make_client_with_mocked_request()
        c._request.return_value = {"error": "rate_limited"}
        c.get_balance()
        b = REGISTRY.get("kalshi_balance")
        self.assertEqual(b.failure_count, 1)

    def test_errors_plural_response_is_failure(self):
        """Round-2 A2 fix: `{"errors": [...]}` (plural, validation
        failures) counts as failure."""
        from circuit_breaker import REGISTRY
        c = _make_client_with_mocked_request()
        c._request.return_value = {
            "errors": [{"code": "INVALID", "message": "bad"}]}
        c.get_balance()
        b = REGISTRY.get("kalshi_balance")
        self.assertEqual(b.failure_count, 1)

    def test_real_response_is_success(self):
        """Non-empty dict without 'error' key is success."""
        from circuit_breaker import REGISTRY
        c = _make_client_with_mocked_request()
        c._request.return_value = {"events": []}  # legit empty list
        c.get_events(series_ticker="KXNBAGAME")
        b = REGISTRY.get("kalshi_events_KXNBAGAME")
        self.assertEqual(b.failure_count, 0,
            "Non-empty dict without 'error' key is a real response.")


class TestKalshiClientOrderbookKeyGrouping(unittest.TestCase):
    """Round-1 A2 fix: orderbook breakers MUST group by series, not
    per-ticker, to prevent unbounded registry growth as 15M tickers
    expire every 15 min."""

    def setUp(self):
        from circuit_breaker import REGISTRY
        for k in list(REGISTRY._breakers.keys()):
            if k.startswith("kalshi_orderbook_"):
                REGISTRY._breakers.pop(k)

    def test_orderbook_breakers_group_by_series(self):
        """All BTC 15M tickers must share one breaker (key
        kalshi_orderbook_KXBTC15M), not one per expiring ticker."""
        from circuit_breaker import REGISTRY
        c = _make_client_with_mocked_request()
        c._request.return_value = None
        # Hit 5 different BTC 15M tickers — should all use the same
        # breaker key.
        for i in range(5):
            c.get_orderbook(f"KXBTC15M-26APR2500{i:02d}-{i:02d}")
        # Only ONE breaker should exist for KXBTC15M.
        keys = [k for k in REGISTRY._breakers
                if k.startswith("kalshi_orderbook_")]
        self.assertEqual(keys, ["kalshi_orderbook_KXBTC15M"],
            f"Expected single breaker for KXBTC15M; got {keys}.")
        # Failure count should reflect all 5 hits before tripping.
        b = REGISTRY.get("kalshi_orderbook_KXBTC15M")
        self.assertTrue(b.is_open(),
            "5 failures across 5 BTC 15M tickers should trip the "
            "shared series-level breaker.")

    def test_different_series_have_separate_breakers(self):
        """KXBTC15M failures must NOT trip KXETH15M (per-series
        isolation, just like get_events)."""
        from circuit_breaker import REGISTRY
        c = _make_client_with_mocked_request()
        c._request.return_value = None
        for _ in range(3):
            c.get_orderbook("KXBTC15M-26APR250000-00")
        btc = REGISTRY.get("kalshi_orderbook_KXBTC15M")
        eth = REGISTRY.get("kalshi_orderbook_KXETH15M")
        self.assertTrue(btc.is_open())
        self.assertFalse(eth.is_open())


class TestKalshiClientAdditionalGetsWrapped(unittest.TestCase):
    """Round-1 A4 fix: get_markets, get_market, get_orders, get_fills,
    get_queue_position must also be wrapped."""

    def setUp(self):
        from circuit_breaker import REGISTRY
        for k in list(REGISTRY._breakers.keys()):
            REGISTRY._breakers.pop(k)

    def test_get_markets_wrapped(self):
        from circuit_breaker import REGISTRY
        c = _make_client_with_mocked_request()
        c._request.return_value = None
        for _ in range(3):
            c.get_markets(series_ticker="KXNBAGAME")
        self.assertTrue(
            REGISTRY.get("kalshi_markets_KXNBAGAME").is_open())

    def test_get_market_wrapped(self):
        from circuit_breaker import REGISTRY
        c = _make_client_with_mocked_request()
        c._request.return_value = None
        for _ in range(3):
            c.get_market("KXBTC15M-26APR250000-00")
        # Market breaker is per-series via series-extraction.
        self.assertTrue(
            REGISTRY.get("kalshi_market_KXBTC15M").is_open())

    def test_get_orders_wrapped(self):
        from circuit_breaker import REGISTRY
        c = _make_client_with_mocked_request()
        c._request.return_value = None
        for _ in range(3):
            c.get_orders()
        self.assertTrue(REGISTRY.get("kalshi_orders").is_open())

    def test_get_fills_wrapped(self):
        from circuit_breaker import REGISTRY
        c = _make_client_with_mocked_request()
        c._request.return_value = None
        for _ in range(3):
            c.get_fills()
        self.assertTrue(REGISTRY.get("kalshi_fills").is_open())

    def test_get_queue_position_wrapped(self):
        from circuit_breaker import REGISTRY
        c = _make_client_with_mocked_request()
        c._request.return_value = None
        for _ in range(3):
            c.get_queue_position("test_order_id")
        self.assertTrue(
            REGISTRY.get("kalshi_queue_position").is_open())

    def test_get_positions_wrapped(self):
        """Round-2 A1 fix — get_positions was silently unwrapped."""
        from circuit_breaker import REGISTRY
        c = _make_client_with_mocked_request()
        c._request.return_value = None
        for _ in range(3):
            c.get_positions()
        self.assertTrue(REGISTRY.get("kalshi_positions_all").is_open())


class TestKalshiBreakerDecoratorContract(unittest.TestCase):
    """Round-3 P2-1+P2-2: missing or wrongly-ordered @_breaker_config
    must fail at decoration time, not silently AttributeError at
    first call in production."""

    def test_missing_inner_breaker_config_raises_at_decoration_time(self):
        from bot import _kalshi_breaker
        with self.assertRaises(TypeError) as cm:
            @_kalshi_breaker
            def some_method(self):
                return None
        self.assertIn("missing @_breaker_config", str(cm.exception))

    def test_wrong_decorator_order_raises_at_decoration_time(self):
        """Reversed order: @_breaker_config OUTER (wrong) — the
        resulting outer wrapper has no _breaker_key_fn until
        _breaker_config attaches it, but by then _kalshi_breaker
        already ran and captured the inner unconfigured function."""
        from bot import _kalshi_breaker, _breaker_config
        with self.assertRaises(TypeError):
            @_breaker_config(key_fn=lambda self: "x")
            @_kalshi_breaker
            def some_method(self):
                return None


class TestKalshiSeriesKeyHelper(unittest.TestCase):
    """Round-2 A4 fix: malformed ticker inputs must not poison the
    breaker registry with garbage keys."""

    def test_normal_ticker_extracts_series(self):
        from bot import _kalshi_series_key
        self.assertEqual(
            _kalshi_series_key("KXBTC15M-26APR250000-00", "orderbook"),
            "kalshi_orderbook_KXBTC15M")

    def test_ticker_without_dash_uses_full_ticker(self):
        from bot import _kalshi_series_key
        self.assertEqual(
            _kalshi_series_key("KXSPX", "market"),
            "kalshi_market_KXSPX")

    def test_empty_string_falls_back_to_unknown(self):
        from bot import _kalshi_series_key
        self.assertEqual(
            _kalshi_series_key("", "orderbook"),
            "kalshi_orderbook_unknown")

    def test_none_falls_back_to_unknown(self):
        from bot import _kalshi_series_key
        self.assertEqual(
            _kalshi_series_key(None, "orderbook"),
            "kalshi_orderbook_unknown")

    def test_non_string_falls_back_to_unknown(self):
        from bot import _kalshi_series_key
        self.assertEqual(
            _kalshi_series_key(12345, "orderbook"),
            "kalshi_orderbook_unknown")


class TestCircuitBreakerLogsOnTransition(unittest.TestCase):
    """Round-1 A3 fix: log on CLOSED→OPEN transitions so operators
    see when a source disabled itself, not just silent failures."""

    def setUp(self):
        from circuit_breaker import REGISTRY
        for k in list(REGISTRY._breakers.keys()):
            REGISTRY._breakers.pop(k)

    def test_logs_circuit_breaker_tripped_on_open(self):
        from circuit_breaker import CircuitBreaker
        import logging
        b = CircuitBreaker(failures_to_open=2, recovery_seconds=10,
                           name="test_endpoint")
        with self.assertLogs("circuit_breaker", level="WARNING") as cm:
            for _ in range(2):
                gen = b.acquire()
                b.record_result(gen, success=False)
        msgs = "\n".join(cm.output)
        self.assertIn("CIRCUIT_BREAKER_TRIPPED", msgs)
        self.assertIn("test_endpoint", msgs)

    def test_breaker_name_auto_set_from_registry_key(self):
        from circuit_breaker import REGISTRY
        b = REGISTRY.get("kalshi_balance",
                         failures_to_open=3, recovery_seconds=10)
        self.assertEqual(b.name, "kalshi_balance")


if __name__ == "__main__":
    unittest.main()
