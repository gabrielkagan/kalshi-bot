"""Tests for WS-bypass cooldown (fix #1 from
kb/failures/ws-cache-drift-silent-scan-2026-04-24.md).

When scan() silent-bails on a ticker because WS orderbook cache is
corrupt (empty book / no best ask), `flag_ticker_drifted(ticker)`
adds the ticker to a per-scanner cooldown dict. For the cooldown
window, `_get_orderbook_cached` bypasses WS entirely and uses REST
for that ticker. Cooldown expiry → retry WS normally.

Scope of fix #1a (this file):
- Flagging empty-book drift (`no_orderbook` / `no_best_ask` silent-bail)
- Eviction of the shared _ob_cache at flag-time (otherwise stale
  WS-corrupt data persists via TTL cache — see what-could-go-wrong C3)
- REST-failure fallback to WS (M1)
- Hourly ticker no-op guard (L1)
- Bounded dict size — sweep expired entries (H2)

NOT in scope of fix #1a:
- Phantom-qty drift detection (C1) — deferred to fix #1b, which will
  wire `_drift_probe_tick` results into `flag_ticker_drifted` when
  ws_qty_total diverges from rest_qty_total beyond a threshold.
- Exponential backoff on repeat flags (H3) — deferred; fixed 60s
  cooldown is acceptable starting point.
"""

import os
import sys
import time
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import bot
from bot.scanner import OpportunityScanner


def _make_scanner():
    """Bare OpportunityScanner with the fields needed for _get_orderbook_cached
    and flag_ticker_drifted. No __init__ — we test these two methods in
    isolation against mocks for _kalshi_feed and _client."""
    s = OpportunityScanner.__new__(OpportunityScanner)
    s._ob_cache = {}
    s._ws_drift_cooldown = {}
    s._kalshi_feed = MagicMock()
    s._kalshi_feed.is_connected = True
    s._client = MagicMock()
    return s


def _ws_ob(yes_levels=None, no_levels=None, ts=None):
    """Shape expected by _get_orderbook_cached from KalshiFeed."""
    return {
        "yes": yes_levels or [],
        "no": no_levels or [],
        "ts": ts if ts is not None else time.time(),
    }


class TestFlagTickerDrifted(unittest.TestCase):
    """`flag_ticker_drifted(ticker, cooldown_s=60)` is the new public
    method that scan silent-bail paths call."""

    def test_flag_adds_entry_with_expiry(self):
        s = _make_scanner()
        before = time.time()
        s.flag_ticker_drifted("KXBTC15M-26APR241845-45", cooldown_s=60)
        after = time.time()
        self.assertIn("KXBTC15M-26APR241845-45", s._ws_drift_cooldown)
        expiry = s._ws_drift_cooldown["KXBTC15M-26APR241845-45"]
        # Expiry should be roughly now + cooldown_s (within test-execution slop)
        self.assertGreaterEqual(expiry, before + 60 - 0.1)
        self.assertLessEqual(expiry, after + 60 + 0.1)

    def test_reflag_extends_expiry(self):
        """Second flag with fresh cooldown replaces the earlier expiry,
        not just extends it additively."""
        s = _make_scanner()
        s.flag_ticker_drifted("KXBTC15M-26APR241845-45", cooldown_s=10)
        first_expiry = s._ws_drift_cooldown["KXBTC15M-26APR241845-45"]
        time.sleep(0.05)  # small gap
        s.flag_ticker_drifted("KXBTC15M-26APR241845-45", cooldown_s=60)
        second_expiry = s._ws_drift_cooldown["KXBTC15M-26APR241845-45"]
        # New expiry is ~50s later than old (60s cooldown from a later now())
        self.assertGreater(second_expiry - first_expiry, 45)

    def test_flag_evicts_stale_ob_cache_entry(self):
        """what-could-go-wrong C3: without this, the shared _ob_cache
        TTL path returns the WS-corrupt orderbook for up to ORDERBOOK_CACHE_TTL
        even when the ticker is flagged, defeating the bypass entirely."""
        s = _make_scanner()
        ticker = "KXBTC15M-26APR241845-45"
        s._ob_cache[ticker] = ({"yes": [[99, 1]], "no": []}, time.time())
        s.flag_ticker_drifted(ticker, cooldown_s=60)
        self.assertNotIn(ticker, s._ob_cache,
                         "flag must evict stale _ob_cache entry")

    def test_flag_on_hourly_ticker_is_noop(self):
        """L1: hourly tickers skip WS entirely in _get_orderbook_cached
        (there is no WS path to bypass), so flagging is meaningless.
        Keeping the flag clean ensures we don't dilute the bounded dict."""
        s = _make_scanner()
        # KXBTCD is an hourly series ticker (see HOURLY_SERIES_TICKERS).
        s.flag_ticker_drifted("KXBTCD-26APR2422-45000", cooldown_s=60)
        self.assertNotIn("KXBTCD-26APR2422-45000", s._ws_drift_cooldown,
                         "hourly ticker flagging should be a no-op")

    def test_dict_self_prunes_at_size(self):
        """H2: without a sweep, tickers for settled markets sit in the
        dict forever. On crossing a size threshold, expired entries are
        purged. Threshold is an implementation detail — we test only
        that dict size never exceeds 2× the number of live (unexpired)
        entries after many flags."""
        s = _make_scanner()
        # Add 50 expired entries (simulating old settled markets).
        for i in range(50):
            s._ws_drift_cooldown[f"KXBTC15M-EXPIRED-{i}"] = time.time() - 3600
        # Add one live flag — this call should trigger the sweep.
        s.flag_ticker_drifted("KXBTC15M-26APR241845-45", cooldown_s=60)
        # After the sweep, only the live entry should remain.
        live = [k for k, exp in s._ws_drift_cooldown.items()
                if exp > time.time()]
        self.assertLessEqual(
            len(s._ws_drift_cooldown), len(live) + 5,
            "dict should self-prune expired entries on growth")


class TestGetOrderbookCachedWithFlag(unittest.TestCase):
    """`_get_orderbook_cached(ticker)` must honor the flag and route
    around WS to REST for flagged tickers."""

    def test_flagged_ticker_bypasses_ws_goes_rest(self):
        s = _make_scanner()
        ticker = "KXBTC15M-26APR241845-45"
        s._ws_drift_cooldown[ticker] = time.time() + 60
        rest_ob = {"yes": [[50, 100]], "no": [[49, 200]]}
        s._client.get_orderbook.return_value = {"orderbook": rest_ob}
        s._kalshi_feed.get_orderbook.return_value = _ws_ob(
            yes_levels=[[99, 1]], no_levels=[])  # corrupt-looking
        result, was_fresh = s._get_orderbook_cached(ticker)
        # WS path must have been skipped entirely.
        s._kalshi_feed.get_orderbook.assert_not_called()
        # REST must have been called.
        s._client.get_orderbook.assert_called_once()
        # Returned data must be the REST orderbook.
        self.assertEqual(result, rest_ob)
        self.assertTrue(was_fresh)

    def test_expired_flag_is_removed_and_ws_path_resumes(self):
        s = _make_scanner()
        ticker = "KXBTC15M-26APR241845-45"
        # Already-expired flag.
        s._ws_drift_cooldown[ticker] = time.time() - 1
        s._kalshi_feed.get_orderbook.return_value = _ws_ob(
            yes_levels=[[77, 100]], no_levels=[[23, 200]])
        result, _ = s._get_orderbook_cached(ticker)
        # Expired flag cleaned up.
        self.assertNotIn(ticker, s._ws_drift_cooldown)
        # WS path used (get_orderbook called).
        s._kalshi_feed.get_orderbook.assert_called_once()
        # REST should NOT have been called.
        s._client.get_orderbook.assert_not_called()

    def test_rest_failure_under_flag_falls_back_to_ws(self):
        """M1: if REST raises while ticker is flagged, we prefer stale WS
        data over returning None. Losing WS-bypass is less harmful than
        losing orderbook data entirely."""
        s = _make_scanner()
        ticker = "KXBTC15M-26APR241845-45"
        s._ws_drift_cooldown[ticker] = time.time() + 60
        s._client.get_orderbook.side_effect = RuntimeError("kalshi REST 500")
        ws_ob = _ws_ob(yes_levels=[[80, 50]], no_levels=[[20, 60]])
        s._kalshi_feed.get_orderbook.return_value = ws_ob
        result, _ = s._get_orderbook_cached(ticker)
        # REST was attempted.
        s._client.get_orderbook.assert_called_once()
        # Returned WS data as fallback — not None.
        self.assertIsNotNone(result)
        self.assertEqual(result, ws_ob)

    def test_unflagged_ticker_uses_ws_path_unchanged(self):
        """Existing behavior must not regress — healthy WS stays WS."""
        s = _make_scanner()
        ticker = "KXBTC15M-26APR241845-45"
        ws_ob = _ws_ob(yes_levels=[[77, 100]], no_levels=[[23, 200]])
        s._kalshi_feed.get_orderbook.return_value = ws_ob
        result, was_fresh = s._get_orderbook_cached(ticker)
        s._kalshi_feed.get_orderbook.assert_called_once()
        s._client.get_orderbook.assert_not_called()
        self.assertEqual(result, ws_ob)
        self.assertFalse(was_fresh)


class TestScanSilentBailFlagsDriftedTicker(unittest.TestCase):
    """Regression: the scan silent-bail paths (bot/_impl.py ~8438 `no_orderbook`
    and ~8487 `no_best_ask`) must call `self.flag_ticker_drifted(ticker)`
    before falling into the insert_rejection + continue block.
    Without this wiring, the cooldown dict stays empty and the bypass
    never activates."""

    def test_scan_no_orderbook_branch_calls_flag_ticker_drifted(self):
        import ast
        bot_py = os.path.join(
            os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
            "bot/scanner/__init__.py")
        with open(bot_py) as f:
            tree = ast.parse(f.read())
        scan = None
        for cls in ast.walk(tree):
            if isinstance(cls, ast.ClassDef) and cls.name == "OpportunityScanner":
                for node in cls.body:
                    if isinstance(node, ast.FunctionDef) and node.name == "scan":
                        scan = node
                        break
        self.assertIsNotNone(scan, "OpportunityScanner.scan not found")
        # Find every call to self.flag_ticker_drifted in scan.
        flag_calls = [
            n for n in ast.walk(scan)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "flag_ticker_drifted"
            and isinstance(n.func.value, ast.Name)
            and n.func.value.id == "self"
        ]
        self.assertGreaterEqual(
            len(flag_calls), 2,
            "scan() must call self.flag_ticker_drifted on both silent-bail "
            "paths (no_orderbook, no_best_ask)")


if __name__ == "__main__":
    unittest.main()
