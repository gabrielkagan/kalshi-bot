"""Bounds on KalshiClient._request — timeout tuple + capped 429 retry.

Guards against:
- scalar timeout=10 (connect+read each up to 10s; 429 Retry-After unbounded)
- instance-level 429 budget reset by a concurrent GET's finally
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
    REST_WRITE_READ_TIMEOUT_S,
    REST_429_MAX_SLEEP_S,
    REST_429_MAX_RETRIES,
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


class TestClockDriftVsElapsed(unittest.TestCase):
    """clock_drift_detected must not fire when HTTP Date lag equals RTT.

    VPS timedatectl 2026-09-07: NTP synchronized. 5–15s warnings were
    abs(local_recv − Date) with Date at 1s resolution — request duration,
    not OS skew. Warn only on residual beyond elapsed + 2s.
    """

    def test_rtt_explained_date_lag_does_not_warn(self):
        c = _bare_client()
        from email.utils import formatdate
        send = 1_000_000.0
        elapsed = 8.0
        date_hdr = formatdate(timeval=send, usegmt=True)
        c.session.request.return_value = _resp(200, headers={"Date": date_hdr})
        times = [send, send + elapsed]

        def fake_time():
            return times.pop(0) if times else send + elapsed

        with patch("bot.kalshi_client.time.time", side_effect=fake_time), \
             patch("bot.kalshi_client.logging.warning") as warn:
            c._request("GET", "/trade-api/v2/portfolio/balance")
        drift_warns = [
            str(c) for c in warn.call_args_list
            if "clock_drift_detected" in str(c)
        ]
        self.assertFalse(drift_warns, f"RTT-explained lag must not warn: {drift_warns}")

    def test_true_skew_beyond_elapsed_warns(self):
        c = _bare_client()
        from email.utils import formatdate
        send = 1_000_000.0
        elapsed = 0.2
        date_hdr = formatdate(timeval=send - 8.0, usegmt=True)
        c.session.request.return_value = _resp(200, headers={"Date": date_hdr})
        times = [send, send + elapsed]

        def fake_time():
            return times.pop(0) if times else send + elapsed

        with patch("bot.kalshi_client.time.time", side_effect=fake_time), \
             patch("bot.kalshi_client.logging.warning") as warn:
            c._request("GET", "/trade-api/v2/portfolio/balance")
        drift_warns = [
            str(c) for c in warn.call_args_list
            if "clock_drift_detected" in str(c)
        ]
        self.assertTrue(drift_warns, "true 8s skew on a 0.2s GET must warn")

    def test_true_skew_on_slow_request_still_warns(self):
        """8s OS skew on an 8s GET is not RTT — Date is 8s before send."""
        c = _bare_client()
        from email.utils import formatdate
        send = 1_000_000.0
        elapsed = 8.0
        date_hdr = formatdate(timeval=send - 8.0, usegmt=True)
        c.session.request.return_value = _resp(200, headers={"Date": date_hdr})
        times = [send, send + elapsed]

        def fake_time():
            return times.pop(0) if times else send + elapsed

        with patch("bot.kalshi_client.time.time", side_effect=fake_time), \
             patch("bot.kalshi_client.logging.warning") as warn:
            c._request("GET", "/trade-api/v2/portfolio/balance")
        drift_warns = [
            str(c) for c in warn.call_args_list
            if "clock_drift_detected" in str(c)
        ]
        self.assertTrue(
            drift_warns,
            "true 8s skew on an 8s GET must still warn")


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

    def test_post_orders_keeps_longer_read_timeout(self):
        """False timeout on POST /orders abandons a live resting order."""
        c = _bare_client()
        c.session.request.return_value = _resp(200)
        c._request("POST", "/trade-api/v2/portfolio/orders", json_body={})
        kwargs = c.session.request.call_args.kwargs
        self.assertEqual(
            kwargs.get("timeout"),
            (REST_CONNECT_TIMEOUT_S, REST_WRITE_READ_TIMEOUT_S),
        )
        self.assertGreaterEqual(REST_WRITE_READ_TIMEOUT_S, 10.0)


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

    def test_429_total_sleep_does_not_exceed_wall_cap(self):
        """REST_429_WALL_CLOCK_CAP_S must bound sleep, not just refuse the next start.

        Checking elapsed before sleep lets two 5s backoffs sum to 10s.
        Sleep remaining budget so sum(slept) <= cap. See Claude MAJOR on PR #177.
        """
        c = _bare_client()
        c.session.request.return_value = _resp(429, headers={"Retry-After": "5"})
        clock = {"t": 0.0}
        slept = []

        def fake_mono():
            return clock["t"]

        def fake_sleep(s):
            slept.append(s)
            clock["t"] += s

        with patch("bot.kalshi_client.time.sleep", side_effect=fake_sleep), \
             patch("bot.kalshi_client.time.monotonic", side_effect=fake_mono):
            result = c._request("GET", "/trade-api/v2/markets")
        self.assertIsNone(result)
        self.assertLessEqual(sum(slept), REST_429_WALL_CLOCK_CAP_S)
        self.assertGreater(sum(slept), 0)

    def test_post_orders_429_is_never_retried(self):
        c = _bare_client()
        c.session.request.return_value = _resp(429, headers={"Retry-After": "1"})
        with patch("bot.kalshi_client.time.sleep") as sleep:
            result = c._request("POST", "/trade-api/v2/portfolio/orders")
        self.assertIsNone(result)
        sleep.assert_not_called()
        self.assertEqual(c.session.request.call_count, 1)

    def test_concurrent_429_chains_each_honor_cap(self):
        """Two GET 429 storms on one client must each stop at the retry cap.

        Instance-level _429_retries + finally:0 lets the chains reset each
        other and sleep unbounded (the SCAN_BODY 38s class, concurrent
        form). Budget must be per-call. See kb/failures/scan-body-5-8s-collecting-mode-sep06.md.
        """
        c = _bare_client()
        n = {"k": 0}
        cap = 2 * (REST_429_MAX_RETRIES + 1) + 2

        def always_429(*_a, **_k):
            n["k"] += 1
            if n["k"] > cap:
                raise AssertionError(
                    f"429 retry unbounded under concurrency: {n['k']} calls"
                )
            return _resp(429, headers={"Retry-After": "1"})

        c.session.request.side_effect = always_429
        results = []

        def worker():
            results.append(c._request("GET", "/trade-api/v2/markets"))

        with patch("bot.kalshi_client.time.sleep"):
            threads = [
                threading.Thread(target=worker),
                threading.Thread(target=worker),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5)
                self.assertFalse(t.is_alive(), "429 worker hung")
        self.assertEqual(results, [None, None])
        self.assertLessEqual(n["k"], cap)

    def test_sibling_finally_reset_cannot_reopen_retry_budget(self):
        """A sibling chain's finally:0 during our sleep must not uncap us.

        Recreates the race without threads: every backoff zeros the
        instance counter the way a concurrent GET's finally does.
        """
        c = _bare_client()
        n = {"k": 0}
        cap = REST_429_MAX_RETRIES + 1

        def always_429(*_a, **_k):
            n["k"] += 1
            if n["k"] > cap:
                raise AssertionError(
                    f"sibling finally-reset reopened 429 budget: {n['k']} calls"
                )
            return _resp(429, headers={"Retry-After": "1"})

        c.session.request.side_effect = always_429

        def sibling_finally(_s):
            # Concurrent GET completing. Must not uncap this chain even
            # if leftover instance fields exist.
            if hasattr(c, "_429_retries"):
                c._429_retries = 0
            if hasattr(c, "_429_t0"):
                c._429_t0 = None

        with patch("bot.kalshi_client.time.sleep", side_effect=sibling_finally):
            result = c._request("GET", "/trade-api/v2/markets")
        self.assertIsNone(result)
        self.assertLessEqual(n["k"], cap)
