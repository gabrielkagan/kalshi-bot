"""Bounds on KalshiClient._request — timeout tuple + capped 429 retry.

Guards against:
- scalar timeout=10 (connect+read each up to 10s; 429 Retry-After unbounded)
- unsynchronized _429_retries mutated across threads
- recursive 429 sleep using the server's Retry-After verbatim (38s scan-spike class)

See kb/failures/scan-body-5-8s-collecting-mode-sep06.md.
"""
from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from bot.kalshi_client import (
    KalshiClient,
    REST_CONNECT_TIMEOUT_S,
    REST_READ_TIMEOUT_S,
    REST_429_MAX_SLEEP_S,
    REST_429_WALL_CLOCK_CAP_S,
)


def _bare_client() -> KalshiClient:
    c = KalshiClient.__new__(KalshiClient)
    c.api_key = "test-key"
    c.private_key = object()
    c.session = MagicMock()
    c._read_timestamps = []
    c._write_timestamps = []
    c._rate_lock = threading.Lock()
    c._429_lock = threading.Lock()
    c._429_retries = 0
    c._429_t0 = None
    c._create_signature = MagicMock(return_value="sig")
    c._rate_limit_wait = MagicMock()
    return c


def _resp(status: int, headers=None, payload=None):
    r = MagicMock()
    r.status_code = status
    r.headers = headers or {}
    r.content = b"{}" if payload is None else b"x"
    r.text = ""
    r.json.return_value = payload if payload is not None else {"ok": True}
    r.raise_for_status = MagicMock()
    return r


class TestRequestTimeoutTuple(unittest.TestCase):
    def test_session_request_uses_connect_read_tuple(self):
        c = _bare_client()
        c.session.request.return_value = _resp(200)
        c._request("GET", "/trade-api/v2/portfolio/balance")
        kwargs = c.session.request.call_args.kwargs
        self.assertEqual(
            kwargs.get("timeout"),
            (REST_CONNECT_TIMEOUT_S, REST_READ_TIMEOUT_S),
            "timeout must be a (connect, read) tuple, not a scalar",
        )


class Test429RetryBounds(unittest.TestCase):
    def test_retry_after_is_capped_not_server_verbatim(self):
        c = _bare_client()
        c.session.request.side_effect = [
            _resp(429, headers={"Retry-After": "99"}),
            _resp(200),
        ]
        slept = []
        with patch("bot.kalshi_client.time.sleep", side_effect=lambda s: slept.append(s)):
            result = c._request("GET", "/trade-api/v2/markets")
        self.assertIsNotNone(result)
        self.assertTrue(slept, "expected a 429 backoff sleep")
        self.assertLessEqual(slept[0], REST_429_MAX_SLEEP_S)
        self.assertLess(slept[0], 99)

    def test_429_gives_up_when_wall_clock_cap_exceeded(self):
        c = _bare_client()
        c.session.request.return_value = _resp(429, headers={"Retry-After": "5"})
        t0 = {"n": 0}

        def fake_monotonic():
            # First call: start. Subsequent: past the wall-clock cap.
            t0["n"] += 1
            return 0.0 if t0["n"] == 1 else REST_429_WALL_CLOCK_CAP_S + 1.0

        with patch("bot.kalshi_client.time.sleep"), \
             patch("bot.kalshi_client.time.monotonic", side_effect=fake_monotonic):
            result = c._request("GET", "/trade-api/v2/markets")
        self.assertIsNone(result)

    def test_post_orders_429_is_never_retried(self):
        c = _bare_client()
        c.session.request.return_value = _resp(429, headers={"Retry-After": "1"})
        with patch("bot.kalshi_client.time.sleep") as sleep:
            result = c._request("POST", "/trade-api/v2/portfolio/orders")
        self.assertIsNone(result)
        sleep.assert_not_called()
        self.assertEqual(c.session.request.call_count, 1)
