"""Phase 2.8 — stale-ticker cleanup root-cause fix.

Pre-fix: `_subscribe_discovery_orderbooks` diffs against
`_discovery_ob_tickers` (its own private previous-cycle view).
Tickers added through 4 OTHER paths (lazy _get_orderbook, scan()
direct subscribe, PPO held-position monitor) bypass that view
and never get cleaned up. Result: post-close tickers accumulate
in `_subscribed_tickers` indefinitely.

Empirical evidence: at 19:00 UTC the bot was sending
update_subscription for `KXXRP15M-26APR251500-00` (15:00 UTC
close — 4 hours dead) → Kalshi rejected with code=7 "Unknown
subscription ID" → 269 stuck-subscribe events in 60min.

Fix: diff against `kalshi_feed.get_subscribed_tickers()` —
the AUTHORITATIVE thread-safe accessor. All subscribe paths
flow into this set, so the diff catches every leaked ticker.

Safety preserved:
  - Held-position tickers excluded (PPO needs them)
  - Empty-active-tickers short-circuit (Kalshi /events outage)
  - active_windows freshness gating (existing
    _active_windows_is_stale check upstream)
"""

import ast
import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BOT_PY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot.py")


def _make_main_loop():
    """Build a minimal MainLoop fixture for testing
    _subscribe_discovery_orderbooks behavior. Mocks kalshi_feed
    and state."""
    import bot
    ml = bot.MainLoop.__new__(bot.MainLoop)
    ml._active_windows = []
    ml._discovery_ob_tickers = set()

    # Mock kalshi_feed with a fake subscribed-tickers set.
    fake_kf = MagicMock()
    fake_kf.is_connected = True
    fake_kf._fake_subscribed = set()
    fake_kf._fake_unsubbed = []
    fake_kf._fake_subbed = []

    def _get_subscribed():
        return list(fake_kf._fake_subscribed)

    def _subscribe(t):
        fake_kf._fake_subscribed.add(t)
        fake_kf._fake_subbed.append(t)

    def _unsubscribe(t):
        fake_kf._fake_subscribed.discard(t)
        fake_kf._fake_unsubbed.append(t)

    fake_kf.get_subscribed_tickers = _get_subscribed
    fake_kf.subscribe_ticker = _subscribe
    fake_kf.unsubscribe_ticker = _unsubscribe
    ml.kalshi_feed = fake_kf

    # Mock state with no held positions.
    fake_state = MagicMock()
    fake_state.get_open_positions = MagicMock(return_value=[])
    ml.state = fake_state

    return ml, fake_kf


class TestAuthoritativeDiffUnsubscribesLeakedTickers(unittest.TestCase):
    """Phase 2.8 P0: tickers in _subscribed_tickers but NOT in
    _active_windows must be unsubscribed, regardless of whether
    they were added via _discovery_ob_tickers, lazy _get_orderbook,
    PPO, or scan(). The fix uses the AUTHORITATIVE
    `kalshi_feed.get_subscribed_tickers()` for the diff."""

    def test_leaked_ticker_added_outside_discovery_gets_unsubscribed(self):
        ml, kf = _make_main_loop()
        # Active windows: just BTC.
        ml._active_windows = [{
            "product_type": "15m",
            "markets": [{"ticker": "KXBTC15M-NOW"}],
        }]
        # Pre-existing subscriptions: BTC (legitimate) + STALE
        # (added via lazy _get_orderbook in a previous cycle, never
        # touched _discovery_ob_tickers).
        kf._fake_subscribed.update({"KXBTC15M-NOW", "KXXRP15M-DEAD-PAST"})
        # _discovery_ob_tickers does NOT know about KXXRP15M-DEAD-PAST.
        ml._discovery_ob_tickers = {"KXBTC15M-NOW"}

        ml._subscribe_discovery_orderbooks()

        self.assertIn(
            "KXXRP15M-DEAD-PAST", kf._fake_unsubbed,
            "Leaked stale ticker (added outside _discovery_ob_tickers) "
            "MUST be unsubscribed by the cleanup cycle. Pre-fix this "
            "leaked because diff was against _discovery_ob_tickers, "
            "not the authoritative _subscribed_tickers.")

    def test_active_ticker_not_unsubscribed(self):
        ml, kf = _make_main_loop()
        ml._active_windows = [{
            "product_type": "15m",
            "markets": [{"ticker": "KXBTC15M-NOW"}],
        }]
        kf._fake_subscribed.add("KXBTC15M-NOW")
        ml._subscribe_discovery_orderbooks()
        self.assertNotIn(
            "KXBTC15M-NOW", kf._fake_unsubbed,
            "Currently-active ticker must NEVER be unsubscribed.")


class TestHeldPositionsProtected(unittest.TestCase):
    """Held-position tickers must NEVER be unsubscribed even if
    their market is no longer in _active_windows. Phase 2.8 R2:
    protection is NOT gated on POSITION_PRICE_MONITOR_ENABLED —
    settlement detection, fill reconciliation, and other paths
    depend on the WS feed regardless of the PPO flag."""

    def test_held_position_ticker_protected_regardless_of_ppo_flag(self):
        ml, kf = _make_main_loop()
        ml._active_windows = [{
            "product_type": "15m",
            "markets": [{"ticker": "KXBTC15M-OTHER"}],
        }]
        kf._fake_subscribed.update({
            "KXBTC15M-OTHER", "KXBTC15M-HELD"})
        ml.state.get_open_positions = MagicMock(return_value=[{
            "ticker": "KXBTC15M-HELD",
            "status": "open",
        }])
        # Even with PPO DISABLED, held position must be protected.
        import bot
        original = bot.POSITION_PRICE_MONITOR_ENABLED
        try:
            bot.POSITION_PRICE_MONITOR_ENABLED = False
            ml._subscribe_discovery_orderbooks()
        finally:
            bot.POSITION_PRICE_MONITOR_ENABLED = original
        self.assertNotIn(
            "KXBTC15M-HELD", kf._fake_unsubbed,
            "Held-position ticker MUST be protected even if PPO "
            "flag is off — other paths (settlement, fill recon) "
            "depend on the WS feed for held tickers.")


class TestEmptyActiveTickersShortCircuit(unittest.TestCase):
    """Existing safety: if active_tickers is empty AND we had subs
    last cycle, skip cleanup entirely (Kalshi /events transient
    failure should not cause mass unsubscription)."""

    def test_empty_active_tickers_does_not_unsubscribe_anything(self):
        ml, kf = _make_main_loop()
        ml._active_windows = []
        kf._fake_subscribed.add("KXBTC15M-LIVE")
        ml._discovery_ob_tickers = {"KXBTC15M-LIVE"}
        ml._subscribe_discovery_orderbooks()
        self.assertEqual(
            kf._fake_unsubbed, [],
            "Empty active_tickers (Kalshi /events transient failure) "
            "MUST NOT trigger any unsubscribes. Existing safety guard.")


class TestAstUsesAuthoritativeAccessor(unittest.TestCase):
    """AST: _subscribe_discovery_orderbooks must reference
    `get_subscribed_tickers` to compute the cleanup diff."""

    def test_subscribe_discovery_uses_get_subscribed_tickers(self):
        with open(BOT_PY) as fh:
            src = fh.read()
        tree = ast.parse(src)
        target = None
        for cls in ast.walk(tree):
            if (not isinstance(cls, ast.ClassDef)
                    or cls.name != "MainLoop"):
                continue
            for fn in cls.body:
                if (isinstance(fn, ast.FunctionDef)
                        and fn.name == "_subscribe_discovery_orderbooks"):
                    target = fn
                    break
        self.assertIsNotNone(
            target, "_subscribe_discovery_orderbooks not found")
        body_src = ast.unparse(target)
        self.assertIn(
            "get_subscribed_tickers", body_src,
            "_subscribe_discovery_orderbooks MUST diff against the "
            "authoritative kalshi_feed.get_subscribed_tickers() to "
            "catch tickers leaked by paths bypassing "
            "_discovery_ob_tickers.")


class TestR1A1EmptyActiveGuardUsesAuthoritative(unittest.TestCase):
    """R-review A1: the empty-active short-circuit MUST check
    `all_subscribed` (authoritative), NOT `_discovery_ob_tickers`
    (deprecated). Pre-fix: on first cycle after restart with
    Kalshi /events transient failure, _discovery_ob_tickers is
    empty too → guard doesn't trigger → mass-unsubscribe of
    every ticker added by lazy _get_orderbook / scan() / PPO
    during startup."""

    def test_empty_active_with_subscribed_via_other_paths_skips_cleanup(self):
        ml, kf = _make_main_loop()
        ml._active_windows = []
        # _discovery_ob_tickers is empty (this is a fresh cycle)
        ml._discovery_ob_tickers = set()
        # But _subscribed_tickers HAS tickers (added via lazy
        # _get_orderbook during startup before /events refreshed).
        kf._fake_subscribed.update({
            "KXBTC15M-EARLY", "KXETH15M-EARLY"})
        ml._subscribe_discovery_orderbooks()
        self.assertEqual(
            kf._fake_unsubbed, [],
            "Empty active_tickers WITH non-empty all_subscribed "
            "MUST skip cleanup. Pre-fix the guard checked the "
            "deprecated _discovery_ob_tickers, missing this case.")


class TestR1A2HeldPositionAllProductTypesProtected(unittest.TestCase):
    """R-review A2: protect ALL held-position tickers, not just
    15M. Pre-fix excluded weather/SPX/sports held positions from
    protection — they could be unsubscribed mid-hold."""

    def test_non_15m_held_position_protected(self):
        ml, kf = _make_main_loop()
        ml._active_windows = [{
            "product_type": "15m",
            "markets": [{"ticker": "KXBTC15M-OTHER"}],
        }]
        kf._fake_subscribed.update({
            "KXBTC15M-OTHER", "KXHIGHTNY-26APR25-B83.5"})
        ml.state.get_open_positions = MagicMock(return_value=[{
            "ticker": "KXHIGHTNY-26APR25-B83.5",  # weather, NOT 15M
            "status": "open",
        }])
        ml._subscribe_discovery_orderbooks()
        self.assertNotIn(
            "KXHIGHTNY-26APR25-B83.5", kf._fake_unsubbed,
            "Non-15M held positions (weather, SPX, sports) MUST "
            "be protected too. Pre-fix only 15M was protected.")


class TestEmptyEverywhere(unittest.TestCase):
    """R2 / P2: explicit coverage of (active=∅, subscribed=∅) —
    fresh restart with /events transient empty AND no leaked subs.
    Should be a clean no-op."""

    def test_empty_active_empty_subscribed_is_noop(self):
        ml, kf = _make_main_loop()
        ml._active_windows = []
        ml._subscribe_discovery_orderbooks()
        self.assertEqual(kf._fake_unsubbed, [])
        self.assertEqual(kf._fake_subbed, [])


if __name__ == "__main__":
    unittest.main()
