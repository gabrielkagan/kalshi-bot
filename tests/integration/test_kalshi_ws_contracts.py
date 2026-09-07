"""Kalshi WebSocket wire-format contract tests.

WHY THIS FILE EXISTS
--------------------
Kalshi silently renames wire fields. Two known-bad incidents:

  1. Mar 2026 — REST orderbook renamed orderbook.{yes,no} (cent ints)
     to orderbook_fp.{yes_dollars_fp, no_dollars_fp} (fp dollar strings).
     Sports parser broke for 37 days before detection.
     See kb/failures/sports-status-filter-outage.md.

  2. Apr 2026 — WS orderbook_snapshot/delta renamed the same keys and
     changed orderbook_delta to a single-update (not side-grouped)
     schema. Our handlers read keys that no longer existed → every WS
     orderbook msg stored as empty arrays → 95% of 15M rows fell through
     to NBBO fallback → `yes_spread_cents`, `bid_depth`, and Kalshi
     flow signals were ~95% NULL for 5+ weeks (noticed only when
     Phase 1 ML feature wiring surfaced it). See
     kb/failures/kalshi-ws-schema-drift.md.

Contract tests here freeze the CURRENT expected wire format. If Kalshi
migrates again, at least one of these tests should fail before live
data degrades silently. Pair each test with a minimal golden-sample
message sourced from docs.kalshi.com — update the sample AND the test
only when the migration is verified and handled.

Scope:
  - WS orderbook_snapshot (new 2026 + legacy pre-2026 fallback)
  - WS orderbook_delta (new 2026 single-update + legacy grouped)
  - FP-levels normalizer (dollars-fp → cents-int)
  - Downstream helpers on the internal format
    (_best_yes_bid, _best_yes_bid_depth, _best_ask_depth,
     OpportunityScanner._best_yes_ask_cents,
     OpportunityScanner._convert_orderbook_fp)

Out of scope (TODO — expand in follow-up):
  - WS fill message schema
  - WS ticker channel (yes_bid_dollars/yes_ask_dollars)
  - REST /markets NBBO (yes_ask_dollars fallback field)
"""

import json
import os
import sys
import time
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from bot.executor import OrderExecutor
from bot.feeds.kalshi import KalshiFeed
from bot.feeds.orderbook_schema import OrderbookSchemaError
from bot.scanner import OpportunityScanner


# ─────────────────────────────────────────────────────────────────────────────
# Golden samples from docs.kalshi.com (Apr 2026 schema)
# ─────────────────────────────────────────────────────────────────────────────

SNAPSHOT_2026 = {
    "type": "orderbook_snapshot",
    "sid": 2,
    "seq": 2,
    "msg": {
        "market_ticker": "KXBTC15M-26APR231930-30",
        "market_id": "9b0f6b43-5b68-4f9f-9f02-9a2d1b8ac1a1",
        "yes_dollars_fp": [["0.9400", "50.00"], ["0.9500", "100.00"]],
        "no_dollars_fp":  [["0.0400", "20.00"], ["0.0500", "146.00"]],
    },
}

DELTA_2026 = {
    "type": "orderbook_delta",
    "sid": 2,
    "seq": 3,
    "msg": {
        "market_ticker": "KXBTC15M-26APR231930-30",
        "market_id": "9b0f6b43-5b68-4f9f-9f02-9a2d1b8ac1a1",
        "price_dollars": "0.9600",
        "delta_fp": "-54.00",
        "side": "yes",
        "ts_ms": 1669149841000,
    },
}

# Pre-2026 legacy schema (ammario/kalshi era) — still accepted with warning.
SNAPSHOT_LEGACY = {
    "type": "orderbook_snapshot",
    "msg": {
        "market_ticker": "KXBTC15M-LEGACY",
        "yes": [[94, 50], [95, 100]],
        "no":  [[4, 20],  [5, 146]],
    },
}

DELTA_LEGACY = {
    "type": "orderbook_delta",
    "msg": {
        "market_ticker": "KXBTC15M-LEGACY",
        "yes": [[96, 0]],  # qty=0 removes level
        "no":  [[7, 25]],
    },
}


def _make_feed() -> KalshiFeed:
    """Build a KalshiFeed without network — handler methods don't use auth.

    R2 / P1-3: _handle_ob_snapshot now drops snapshots for tickers
    not in `_subscribed_tickers` (cache-leak guard). These contract
    tests pre-populate `_subscribed_tickers` with all tickers used
    in test fixtures so the parser-under-test sees a realistic
    production state (every snapshot we receive should be for a
    subscribed ticker)."""
    feed = KalshiFeed(api_key="test", private_key=MagicMock())
    # Pre-subscribe every ticker referenced in test fixtures.
    # The parser tests treat `_subscribed_tickers` as a no-op
    # precondition; production callers always subscribe first.
    feed._subscribed_tickers.update({
        "KXBTC15M-26APR231930-30",
        "KXBTC15M-26APR241030-30",
        "KXETH15M-26APR241030-30",
        "KXBTC15M-LEGACY",
        "T",
    })
    return feed


# ─────────────────────────────────────────────────────────────────────────────
# FP-levels normalizer (the shared conversion)
# ─────────────────────────────────────────────────────────────────────────────

class TestNormalizeFpLevels(unittest.TestCase):
    """The pure converter used by WS snapshot and (future) REST consolidation.

    Contract: dollars (float str) × 100 → cents int; fp qty string → int.
    """

    def test_simple_pair(self):
        result = KalshiFeed._normalize_fp_levels([["0.9500", "100.00"]])
        self.assertEqual(result, [[95, 100]])

    def test_multiple_levels_preserves_order(self):
        result = KalshiFeed._normalize_fp_levels(
            [["0.0800", "300"], ["0.2200", "333"]])
        self.assertEqual(result, [[8, 300], [22, 333]])

    def test_fractional_qty_rounded_to_int(self):
        # fp can be fractional — match _convert_orderbook_fp convention.
        result = KalshiFeed._normalize_fp_levels([["0.9600", "54.50"]])
        self.assertEqual(result, [[96, 54]])   # round-half-to-even on 54.50

    def test_empty_array(self):
        self.assertEqual(KalshiFeed._normalize_fp_levels([]), [])

    def test_none_treated_as_empty(self):
        self.assertEqual(KalshiFeed._normalize_fp_levels(None), [])

    def test_malformed_entry_skipped_not_raised(self):
        # A single bad entry shouldn't blow away an otherwise-valid snapshot.
        result = KalshiFeed._normalize_fp_levels(
            [["0.95", "100"], "garbage", ["0.96", "50"]])
        self.assertEqual(result, [[95, 100], [96, 50]])

    def test_unparseable_price_skipped(self):
        result = KalshiFeed._normalize_fp_levels(
            [["not_a_price", "100"], ["0.95", "50"]])
        self.assertEqual(result, [[95, 50]])

    def test_penny_boundary_no_rounding_drift(self):
        # 0.08 dollars MUST be 8 cents, not 7 or 9.
        result = KalshiFeed._normalize_fp_levels(
            [["0.08", "1"], ["0.22", "2"], ["0.99", "3"], ["0.01", "4"]])
        self.assertEqual(result, [[8, 1], [22, 2], [99, 3], [1, 4]])


# ─────────────────────────────────────────────────────────────────────────────
# orderbook_snapshot contract
# ─────────────────────────────────────────────────────────────────────────────

class TestSnapshotContract2026(unittest.TestCase):
    """Kalshi 2026 schema: yes_dollars_fp / no_dollars_fp."""

    def test_snapshot_populates_cents_format(self):
        feed = _make_feed()
        feed._handle_ob_snapshot(SNAPSHOT_2026)
        ob = feed._orderbooks["KXBTC15M-26APR231930-30"]
        self.assertEqual(ob["yes"], [[94, 50], [95, 100]])
        self.assertEqual(ob["no"],  [[4, 20],  [5, 146]])

    def test_snapshot_missing_yes_side(self):
        """Kalshi docs: yes_dollars_fp absent when no YES offers exist."""
        msg = {
            "msg": {
                "market_ticker": "T",
                "no_dollars_fp": [["0.50", "10"]],
            },
        }
        feed = _make_feed()
        feed._handle_ob_snapshot(msg)
        ob = feed._orderbooks["T"]
        self.assertEqual(ob["yes"], [])
        self.assertEqual(ob["no"], [[50, 10]])

    def test_snapshot_both_sides_empty(self):
        """Thin-book market with no orders on either side."""
        msg = {"msg": {"market_ticker": "T",
                       "yes_dollars_fp": [], "no_dollars_fp": []}}
        feed = _make_feed()
        feed._handle_ob_snapshot(msg)
        ob = feed._orderbooks["T"]
        self.assertEqual(ob["yes"], [])
        self.assertEqual(ob["no"], [])

    def test_snapshot_missing_market_ticker_is_noop(self):
        feed = _make_feed()
        feed._handle_ob_snapshot({"msg": {"yes_dollars_fp": [["0.5", "1"]]}})
        self.assertEqual(feed._orderbooks, {})

    def test_snapshot_updates_existing_entry(self):
        """Second snapshot for the same ticker REPLACES, not merges."""
        feed = _make_feed()
        feed._handle_ob_snapshot(SNAPSHOT_2026)
        new = {"msg": {"market_ticker": "KXBTC15M-26APR231930-30",
                       "yes_dollars_fp": [["0.9700", "200"]],
                       "no_dollars_fp": []}}
        feed._handle_ob_snapshot(new)
        ob = feed._orderbooks["KXBTC15M-26APR231930-30"]
        self.assertEqual(ob["yes"], [[97, 200]])
        self.assertEqual(ob["no"], [])


class TestSnapshotContractLegacy(unittest.TestCase):
    """Pre-2026 schema fallback — legacy yes/no arrays."""

    def test_legacy_snapshot_accepted_with_warning(self):
        feed = _make_feed()
        feed._handle_ob_snapshot(SNAPSHOT_LEGACY)
        ob = feed._orderbooks["KXBTC15M-LEGACY"]
        self.assertEqual(ob["yes"], [[94, 50], [95, 100]])
        self.assertEqual(ob["no"], [[4, 20], [5, 146]])


class TestSnapshotDriftDetection(unittest.TestCase):
    """Unknown schemas are logged as OrderbookSchemaError (caught in outer try).

    The handler catches the error and logs it — doesn't crash — but the LOG
    is the alarm. Post-deploy verifier should grep for WS_SCHEMA_ERROR.
    """

    def test_unknown_schema_does_not_populate_cache(self):
        feed = _make_feed()
        feed._handle_ob_snapshot({"msg": {
            "market_ticker": "DRIFT",
            "yes_fp_v2": [["0.95", "100"]],  # hypothetical future migration
        }})
        self.assertNotIn("DRIFT", feed._orderbooks)

    def test_unknown_schema_no_crash(self):
        """Never crash the WS loop on bad input — just log."""
        feed = _make_feed()
        # Should not raise
        feed._handle_ob_snapshot({"msg": {"market_ticker": "X", "foo": "bar"}})
        feed._handle_ob_snapshot({})  # no msg at all
        feed._handle_ob_snapshot({"msg": None})  # garbage

    def test_schema_probe_fires_once(self):
        """Probe logs the first snapshot's keys, then stays silent."""
        feed = _make_feed()
        self.assertFalse(feed._snapshot_schema_probed)
        feed._handle_ob_snapshot(SNAPSHOT_2026)
        self.assertTrue(feed._snapshot_schema_probed)
        # Second call: probe flag stays True, doesn't re-log
        feed._handle_ob_snapshot(SNAPSHOT_2026)
        self.assertTrue(feed._snapshot_schema_probed)


class TestSnapshotEmptyBookIdentity(unittest.TestCase):
    """Kalshi omits yes_dollars_fp/no_dollars_fp when both sides have no
    resting orders, sending only {market_id, market_ticker}. Before the
    2026-04-24 fix, this raised OrderbookSchemaError and left the cache
    uninitialized — ~384 ERROR logs/day (4 per 15M window open × 4 assets
    × 4 windows/hour × 24h). See kb/failures/kalshi-ws-schema-drift.md
    § "WS delta underflow — ROOT CAUSE PARTIALLY RESOLVED".
    """

    def test_snapshot_identity_only_treated_as_empty(self):
        """{market_id, market_ticker} alone → initializes empty book."""
        feed = _make_feed()
        feed._handle_ob_snapshot({"msg": {
            "market_id": "abc-123",
            "market_ticker": "KXBTC15M-26APR241030-30",
        }})
        self.assertIn("KXBTC15M-26APR241030-30", feed._orderbooks)
        ob = feed._orderbooks["KXBTC15M-26APR241030-30"]
        self.assertEqual(ob["yes"], [])
        self.assertEqual(ob["no"], [])

    def test_snapshot_ticker_only_treated_as_empty(self):
        """market_ticker alone (no market_id) → also initializes empty."""
        feed = _make_feed()
        feed._handle_ob_snapshot({"msg": {
            "market_ticker": "KXETH15M-26APR241030-30",
        }})
        self.assertIn("KXETH15M-26APR241030-30", feed._orderbooks)
        ob = feed._orderbooks["KXETH15M-26APR241030-30"]
        self.assertEqual(ob["yes"], [])
        self.assertEqual(ob["no"], [])

    def test_snapshot_truly_unknown_keys_still_raises(self):
        """Drift detection still fires for genuinely unknown schemas.
        {market_ticker, yes_fp_v2: ...} is not identity-only → not empty."""
        feed = _make_feed()
        feed._handle_ob_snapshot({"msg": {
            "market_ticker": "DRIFT-V3",
            "yes_fp_v2": [["0.95", "100"]],  # hypothetical future schema
        }})
        # The OrderbookSchemaError is caught in the outer try; cache stays empty.
        self.assertNotIn("DRIFT-V3", feed._orderbooks)


# ─────────────────────────────────────────────────────────────────────────────
# orderbook_delta contract
# ─────────────────────────────────────────────────────────────────────────────

class TestDeltaContract2026(unittest.TestCase):
    """Kalshi 2026 schema: single update {price_dollars, delta_fp, side}."""

    def test_delta_adds_to_existing_level(self):
        feed = _make_feed()
        feed._orderbooks["T"] = {"yes": [[96, 100]], "no": [], "ts": 0}
        feed._handle_ob_delta({"msg": {
            "market_ticker": "T", "price_dollars": "0.9600",
            "delta_fp": "50.00", "side": "yes"}})
        self.assertEqual(feed._orderbooks["T"]["yes"], [[96, 150]])

    def test_delta_creates_new_level(self):
        feed = _make_feed()
        feed._orderbooks["T"] = {"yes": [[96, 100]], "no": [], "ts": 0}
        feed._handle_ob_delta({"msg": {
            "market_ticker": "T", "price_dollars": "0.9500",
            "delta_fp": "25.00", "side": "yes"}})
        ys = sorted(feed._orderbooks["T"]["yes"])
        self.assertEqual(ys, [[95, 25], [96, 100]])

    def test_delta_negative_removes_level_when_zero(self):
        """-54 on a level of 54 → level removed."""
        feed = _make_feed()
        feed._orderbooks["T"] = {"yes": [[96, 54]], "no": [], "ts": 0}
        feed._handle_ob_delta({"msg": {
            "market_ticker": "T", "price_dollars": "0.9600",
            "delta_fp": "-54.00", "side": "yes"}})
        self.assertEqual(feed._orderbooks["T"]["yes"], [])

    def test_delta_underflow_hides_book_until_snapshot(self):
        """-50 on a level of 10: hide the book until a snapshot rebuilds.

        Underflow means the cache already disagreed with the venue.
        Clamping the one level and serving the rest preserves the
        corruption (2026-09-07 crossed-book RCA).
        """
        feed = _make_feed()
        feed._orderbooks["T"] = {"yes": [[96, 10]], "no": [[4, 5]], "ts": 0}
        feed._handle_ob_delta({"msg": {
            "market_ticker": "T", "price_dollars": "0.9600",
            "delta_fp": "-50.00", "side": "yes"}})
        self.assertNotIn("T", feed._orderbooks)
        self.assertIsNone(feed.get_orderbook("T"))

    def test_delta_from_docs_sample(self):
        """Verbatim from docs.kalshi.com example: -54 @ 0.960 on yes side."""
        feed = _make_feed()
        feed._orderbooks["KXBTC15M-26APR231930-30"] = {
            "yes": [[96, 100]], "no": [], "ts": 0}
        feed._handle_ob_delta(DELTA_2026)
        self.assertEqual(
            feed._orderbooks["KXBTC15M-26APR231930-30"]["yes"], [[96, 46]])

    def test_delta_no_side(self):
        """Deltas apply independently to NO side."""
        feed = _make_feed()
        feed._orderbooks["T"] = {"yes": [], "no": [[5, 50]], "ts": 0}
        feed._handle_ob_delta({"msg": {
            "market_ticker": "T", "price_dollars": "0.0500",
            "delta_fp": "25.00", "side": "no"}})
        self.assertEqual(feed._orderbooks["T"]["no"], [[5, 75]])

    def test_delta_unknown_side_is_schema_error(self):
        """side='maybe' is not valid."""
        feed = _make_feed()
        feed._orderbooks["T"] = {"yes": [], "no": [], "ts": 0}
        # Should not crash (OrderbookSchemaError caught in handler)
        feed._handle_ob_delta({"msg": {
            "market_ticker": "T", "price_dollars": "0.50",
            "delta_fp": "10", "side": "maybe"}})
        # Cache should still be empty (delta rejected)
        self.assertEqual(feed._orderbooks["T"]["yes"], [])
        self.assertEqual(feed._orderbooks["T"]["no"], [])


class TestDeltaContractLegacy(unittest.TestCase):
    """Pre-2026 side-grouped schema: msg['yes'], msg['no'] as arrays."""

    def test_legacy_delta_merges_both_sides(self):
        """Legacy contract: array of [cents, qty] per side; qty=0 REMOVES a level."""
        feed = _make_feed()
        feed._orderbooks["KXBTC15M-LEGACY"] = {
            "yes": [[96, 50]], "no": [[5, 20]], "ts": 0}
        feed._handle_ob_delta(DELTA_LEGACY)
        ob = feed._orderbooks["KXBTC15M-LEGACY"]
        # DELTA_LEGACY: yes=[[96,0]] removes 96c level; no=[[7,25]] adds 7c level
        self.assertEqual(ob["yes"], [])
        self.assertIn([5, 20], ob["no"])
        self.assertIn([7, 25], ob["no"])


class TestDeltaDriftDetection(unittest.TestCase):
    def test_unknown_delta_schema_noop(self):
        feed = _make_feed()
        feed._orderbooks["T"] = {"yes": [[96, 100]], "no": [], "ts": 0}
        feed._handle_ob_delta({"msg": {"market_ticker": "T", "v2": "x"}})
        # State unchanged
        self.assertEqual(feed._orderbooks["T"]["yes"], [[96, 100]])

    def test_unparseable_price_dollars_no_crash(self):
        feed = _make_feed()
        feed._orderbooks["T"] = {"yes": [], "no": [], "ts": 0}
        feed._handle_ob_delta({"msg": {
            "market_ticker": "T", "price_dollars": "not_a_number",
            "delta_fp": "10", "side": "yes"}})
        # Doesn't crash; state unchanged
        self.assertEqual(feed._orderbooks["T"]["yes"], [])


class TestWsSilenceWatchdogFields(unittest.TestCase):
    """State fields + frame-ingress watchdog wiring for the silence
    detector. The actual reconnect behavior is in WSClient's
    ``_silence_watchdog`` async task; we test the observable state that
    drives it.

    D1.1.5 Phase 3b: the silence-watchdog ``_last_msg_ts`` moved from
    ``KalshiFeed._ws_last_msg_ts`` to ``WSClient._last_msg_ts``
    (set BEFORE invoking the on_frame callback — Apr-24 ordering
    invariant preserved). These tests now exercise the wire path via
    ``feed._wire._handle_raw_frame(raw)`` which is the equivalent of
    the pre-extraction ``feed._handle_message(raw)`` watchdog bump.

    Regression guard for the 2026-04-24 17:30 UTC 15M outage: Kalshi's
    WS stayed "connected" (ping/pong healthy) while delivering zero
    protocol messages for 32 min. Without this watchdog, pending subs
    never flush, snapshots never arrive, 15M trading dies silently.
    """

    def test_init_zeroes_watchdog_timestamps(self):
        feed = _make_feed()
        # _last_msg_ts now lives on WSClient (D1.1.5 Phase 3b)
        self.assertEqual(feed._wire._last_msg_ts, 0.0)
        # _ws_connect_ts stays on KalshiFeed (used by _should_log_raw_in
        # to gate the post-connect raw-log window — bot-specific
        # log budget per pickup prompt L42)
        self.assertEqual(feed._ws_connect_ts, 0.0)

    def test_handle_message_updates_last_msg_ts(self):
        """Every incoming frame bumps the watchdog, even unknown types.

        D1.1.5 Phase 3b: the bump happens in ``WSClient._handle_raw_frame``
        BEFORE the on_frame dispatch (Apr-24 silence-watchdog ordering —
        load-bearing).
        """
        import time as _t
        feed = _make_feed()
        before = _t.time()
        feed._wire._handle_raw_frame(json.dumps({"type": "something_unknown"}))
        self.assertGreaterEqual(feed._wire._last_msg_ts, before)

    def test_handle_message_on_empty_json_still_bumps_watchdog(self):
        """Even an empty message body = Kalshi is talking to us. The
        intent of the watchdog is 'server alive at all' not 'server
        delivering useful data' — dispatching happens below."""
        import time as _t
        feed = _make_feed()
        before = _t.time()
        feed._wire._handle_raw_frame(json.dumps({}))
        self.assertGreaterEqual(feed._wire._last_msg_ts, before)

    def test_handle_message_invalid_json_doesnt_crash(self):
        """Garbage frame — don't crash. Timestamp updated BEFORE json
        parse per design (server connectivity proven by frame arrival)."""
        feed = _make_feed()
        feed._wire._handle_raw_frame("not valid json")

    def test_watchdog_timestamp_advances_across_messages(self):
        """Multiple messages → timestamp monotonically advances."""
        import time as _t
        feed = _make_feed()
        feed._wire._handle_raw_frame(json.dumps({"type": "a"}))
        t1 = feed._wire._last_msg_ts
        _t.sleep(0.01)
        feed._wire._handle_raw_frame(json.dumps({"type": "b"}))
        t2 = feed._wire._last_msg_ts
        self.assertGreater(t2, t1)


class TestWsSeqGapDetector(unittest.TestCase):
    """H3 diagnostic: WS (sid, seq) gap detection.

    Kalshi WS envelope carries `sid` (subscription id) and `seq` (message
    number within subscription). Seq should be monotonically +1 per sid.
    Any gap = dropped/reordered/duplicate messages and is the leading
    hypothesis for the residual deep-level delta underflow warnings that
    persist even after the 2026-04-24 empty-snapshot fix.

    D1.1.5 Phase 3b: the seq-gap detector moved from
    ``KalshiFeed._handle_message`` to ``WSClient._handle_raw_frame``.
    Tests now exercise the wire layer directly and read
    ``feed._wire._ws_last_seq`` / ``feed._wire._ws_seq_gap_logs`` /
    ``feed._wire._seq_gap_max_logs``.

    Diagnostic test — remove once H3 is confirmed/rejected and the
    instrumentation is cleaned up.
    """

    def _msg(self, sid, seq, ticker="T"):
        """Minimal WS envelope for gap detector — goes through dispatch
        but has no side effects because msg_type is unknown."""
        return json.dumps({
            "type": "heartbeat",   # unhandled — dispatch is a no-op
            "sid": sid, "seq": seq,
            "msg": {"market_ticker": ticker},
        })

    def test_consecutive_seq_no_gap(self):
        feed = _make_feed()
        feed._wire._handle_raw_frame(self._msg(1, 1))
        feed._wire._handle_raw_frame(self._msg(1, 2))
        feed._wire._handle_raw_frame(self._msg(1, 3))
        self.assertEqual(feed._wire._ws_seq_gap_logs, 0)
        self.assertEqual(feed._wire._ws_last_seq[1], 3)

    def test_first_seq_per_sid_not_a_gap(self):
        """First message on a new sid — prev is None, skip gap check."""
        feed = _make_feed()
        feed._wire._handle_raw_frame(self._msg(42, 1000))
        self.assertEqual(feed._wire._ws_seq_gap_logs, 0)
        self.assertEqual(feed._wire._ws_last_seq[42], 1000)

    def test_gap_forward_logs(self):
        """Seq jumps 1 → 5 = 3 messages lost, gap=3."""
        feed = _make_feed()
        feed._wire._handle_raw_frame(self._msg(1, 1))
        feed._wire._handle_raw_frame(self._msg(1, 5))
        self.assertEqual(feed._wire._ws_seq_gap_logs, 1)
        # Tracker updates to latest — next gap check is against the new seq.
        self.assertEqual(feed._wire._ws_last_seq[1], 5)

    def test_reorder_backward_logs(self):
        """Seq goes 3 → 2 (out-of-order or duplicate) also trips detector."""
        feed = _make_feed()
        feed._wire._handle_raw_frame(self._msg(1, 3))
        feed._wire._handle_raw_frame(self._msg(1, 2))
        self.assertEqual(feed._wire._ws_seq_gap_logs, 1)

    def test_parallel_sids_tracked_independently(self):
        """Multiple subscriptions interleave. Each sid's seq monotonic
        independently — no cross-contamination."""
        feed = _make_feed()
        feed._wire._handle_raw_frame(self._msg(1, 1))
        feed._wire._handle_raw_frame(self._msg(2, 100))
        feed._wire._handle_raw_frame(self._msg(1, 2))
        feed._wire._handle_raw_frame(self._msg(2, 101))
        feed._wire._handle_raw_frame(self._msg(1, 3))
        self.assertEqual(feed._wire._ws_seq_gap_logs, 0)
        self.assertEqual(feed._wire._ws_last_seq[1], 3)
        self.assertEqual(feed._wire._ws_last_seq[2], 101)

    def test_missing_sid_or_seq_no_crash(self):
        """If Kalshi omits sid/seq (old protocol or malformed), skip silently."""
        feed = _make_feed()
        feed._wire._handle_raw_frame(json.dumps({"type": "heartbeat"}))
        feed._wire._handle_raw_frame(json.dumps({"type": "heartbeat", "sid": 1}))
        feed._wire._handle_raw_frame(json.dumps({"type": "heartbeat", "seq": 1}))
        self.assertEqual(feed._wire._ws_seq_gap_logs, 0)
        self.assertEqual(feed._wire._ws_last_seq, {})

    def test_log_cap_prevents_flood(self):
        """Gap logs are capped to avoid log flood. Past the cap, tracker
        still updates but no more warnings fire.

        D1.1.5 Phase 3b: the cap moved from
        ``feed._ws_seq_gap_max_logs`` to ``feed._wire._seq_gap_max_logs``
        (constructor kwarg ``seq_gap_max_logs``).
        """
        feed = _make_feed()
        feed._wire._seq_gap_max_logs = 3
        for i in range(10):
            # Force a gap on every message
            feed._wire._handle_raw_frame(self._msg(1, i * 10))
        self.assertEqual(feed._wire._ws_seq_gap_logs, 3)


# ─────────────────────────────────────────────────────────────────────────────
# Downstream helpers: must operate on the internal [cents, qty] format
# ─────────────────────────────────────────────────────────────────────────────

class TestDownstreamHelpersOnInternalFormat(unittest.TestCase):
    """Whatever the WS / REST path produces, these helpers must consume it.

    If these fail, Phase 1 features (yes_spread_cents, bid_depth) break even
    if the handler is fixed.
    """

    def test_best_yes_ask_cents_from_no_bids(self):
        """best_yes_ask = 100 - highest NO bid (line 13172)."""
        ob = {"yes": [[95, 100]], "no": [[54, 20], [56, 150]]}
        self.assertEqual(OpportunityScanner._best_yes_ask_cents(ob), 44)

    def test_best_yes_ask_cents_empty_no_side(self):
        ob = {"yes": [[95, 100]], "no": []}
        self.assertIsNone(OpportunityScanner._best_yes_ask_cents(ob))

    def test_best_yes_bid(self):
        ob = {"yes": [[94, 50], [95, 100]], "no": []}
        self.assertEqual(OrderExecutor._best_yes_bid(ob), 95)

    def test_best_yes_bid_empty(self):
        ob = {"yes": [], "no": []}
        self.assertIsNone(OrderExecutor._best_yes_bid(ob))

    def test_best_yes_bid_depth(self):
        ob = {"yes": [[94, 50], [95, 100]], "no": []}
        self.assertEqual(OrderExecutor._best_yes_bid_depth(ob), 100)

    def test_best_yes_bid_depth_empty_returns_zero(self):
        """Empty yes side returns 0 (not None) — this is why pre-fix
        bid_depth populated as 0 in 95% of rows while yes_bid_cents was NULL."""
        ob = {"yes": [], "no": [[5, 10]]}
        self.assertEqual(OrderExecutor._best_yes_bid_depth(ob), 0)

    def test_best_ask_depth_from_no_side(self):
        """best_ask_depth reads the highest-price NO bid level's qty."""
        ob = {"yes": [], "no": [[54, 20], [56, 150]]}
        self.assertEqual(OrderExecutor._best_ask_depth(ob), 150)

    def test_best_ask_depth_thin_top_on_terminal_market(self):
        """Contract pin for post-WS-fix TM_99 scenario: on near-settlement
        15M markets the NO bid at 1c (= YES ask at 99c) is often qty=1,
        reflecting thin resting ask supply near the ceiling. Asserts the
        decoder → helper pipeline yields the expected thin top-of-book
        value for a realistic wire shape. See
        kb/failures/kalshi-ws-schema-drift.md and
        kb/decisions/no-floor-relaxation-on-ws-fix.md.
        """
        # Simulate a near-terminal YES market: single NO bid level at 1c × 1
        # (= YES ask 99c × 1), deep YES bids on the other side.
        feed = _make_feed()
        msg = {
            "type": "orderbook_snapshot", "sid": 1, "seq": 1,
            "msg": {
                "market_ticker": "KXBTC15M-26APR231930-30",
                "market_id": "x",
                "yes_dollars_fp": [["0.9800", "620.00"]],
                "no_dollars_fp":  [["0.0100", "1.00"]],
            },
        }
        feed._handle_ob_snapshot(msg)
        ob = feed._orderbooks["KXBTC15M-26APR231930-30"]
        self.assertEqual(OrderExecutor._best_ask_depth(ob), 1)
        # And YES-side bid depth is healthy (matches the asymmetry we see live)
        self.assertEqual(OrderExecutor._best_yes_bid_depth(ob), 620)


# ─────────────────────────────────────────────────────────────────────────────
# End-to-end: WS snapshot → downstream helpers produce real values
# ─────────────────────────────────────────────────────────────────────────────

class TestEndToEndPhase1FeaturePopulation(unittest.TestCase):
    """Regression guard for the Apr 23 Phase 1 bug:

    When Kalshi sends a canonical 2026-schema snapshot, the bot's internal
    state must be such that Phase 1 features populate end-to-end:
      - yes_spread_cents = best_ask - best_yes_bid (both non-None)
      - bid_depth        = _best_yes_bid_depth
    """

    def test_canonical_snapshot_enables_spread_and_depth(self):
        feed = _make_feed()
        feed._handle_ob_snapshot(SNAPSHOT_2026)
        ob = feed._orderbooks["KXBTC15M-26APR231930-30"]

        best_ask = OpportunityScanner._best_yes_ask_cents(ob)  # 100 - max no = 95
        best_bid = OrderExecutor._best_yes_bid(ob)              # 95
        depth = OrderExecutor._best_yes_bid_depth(ob)           # 100

        self.assertEqual(best_ask, 95)  # 100 - 5
        self.assertEqual(best_bid, 95)
        self.assertEqual(depth, 100)
        # yes_spread_cents = best_ask - best_bid = 0 (one-tick book)
        self.assertEqual(best_ask - best_bid, 0)


# ─────────────────────────────────────────────────────────────────────────────
# REST path contract (pin the fix from Apr 19 sports outage)
# ─────────────────────────────────────────────────────────────────────────────

class TestRestOrderbookFpContract(unittest.TestCase):
    """_convert_orderbook_fp was the fix for the Mar/Apr 2026 REST migration.

    Pinning the contract here so a future rewrite can't silently break it
    (precedent: the WS handler predated this fix by weeks and was missed).
    """

    def test_rest_conversion_matches_ws_normalizer(self):
        """Both paths should produce the same cents format from the same FP input."""
        fp = {
            "yes_dollars": [["0.9500", "100.00"]],
            "no_dollars":  [["0.0500", "50.00"]],
        }
        rest_out = OpportunityScanner._convert_orderbook_fp(fp)
        ws_yes = KalshiFeed._normalize_fp_levels(fp["yes_dollars"])
        ws_no = KalshiFeed._normalize_fp_levels(fp["no_dollars"])
        self.assertEqual(rest_out["yes"], ws_yes)
        self.assertEqual(rest_out["no"], ws_no)

    def test_rest_and_ws_merge_same_integer_cent(self):
        fp = {
            "yes_dollars": [["0.1300", "10.00"], ["0.1310", "20.00"]],
            "no_dollars": [["0.0200", "5.00"], ["0.0210", "7.00"]],
        }
        rest_out = OpportunityScanner._convert_orderbook_fp(fp)
        self.assertEqual(rest_out["yes"], [[13, 30]])
        self.assertEqual(rest_out["no"], [[2, 12]])
        self.assertEqual(
            KalshiFeed._normalize_fp_levels(fp["yes_dollars"]), [[13, 30]])


# ─────────────────────────────────────────────────────────────────────────────
# WS book desync (2026-09-07) — empty-init + integer-cent first-match
# ─────────────────────────────────────────────────────────────────────────────

class TestWsBookDesyncSep07(unittest.TestCase):
    """Regression for the live Kalshi book staying crossed ~50% of the time.

    Two cooperating defects, measured 2026-09-07 (Track M + live
    WS_DRIFT_PROBE / underflow logs):

    1. ``_apply_fp_delta`` initialized an empty book when a delta arrived
       before the subscribe snapshot and applied the delta as if the
       venue qty at that price was 0. Live fingerprint: underflow on a
       brand-new 15M ticker within 1s of subscribe
       (``existing=75 delta=-9632``). Same class as ticket 86bbvztem
       (Gemini L2 never cleared on reconnect): diffs merged into a book
       we no longer have the base state for. Fix pattern: awaiting
       snapshot exclusion until a full-book frame rebuilds it.
    2. ``_normalize_fp_levels`` stored one list entry per sub-cent
       wire price, all rounded to integer cents, and ``_apply_fp_delta``
       updated only the FIRST matching cent. A delete larger than that
       first bucket underflow-clamped and left sibling cent-buckets in
       place — immortal far-from-touch levels. That is the 12¢ stale
       NO bid (implied YES ask 87¢ against a 99¢ YES bid) healed only
       by the 5-min get_snapshot.

    Trading ``get_orderbook`` must fail closed on a strictly crossed
    book and on a ticker still awaiting its snapshot, so
    ``OrderExecutor._best_yes_bid`` / ``_best_yes_ask_cents`` cannot
    hit a ghost level. The snapshotter keeps the raw cache via
    ``get_all_orderbooks_snapshot`` so the defect stays measurable.
    """

    def test_relatch_snapshot_replaces_delta_built_book(self):
        """Track B2 handoff (kb/findings/HANDOFF-ws-book-desync-acceptance-test.md):
        the only reconstruction rule that scores is relatch — reset
        the book on every snapshot. Accumulate-from-birth was 0/60
        tickers clean; relatch 55/60. A later snapshot must drop
        levels that only existed as post-snapshot deltas."""
        feed = _make_feed()
        feed._handle_ob_snapshot({"msg": {
            "market_ticker": "T",
            "yes_dollars_fp": [["0.5000", "10.00"]],
            "no_dollars_fp": [["0.4900", "10.00"]],
        }})
        feed._handle_ob_delta({"msg": {
            "market_ticker": "T",
            "price_dollars": "0.6000",
            "delta_fp": "5.00",
            "side": "yes",
        }})
        self.assertEqual(feed._orderbooks["T"]["yes"], [[50, 10], [60, 5]])
        feed._handle_ob_snapshot({"msg": {
            "market_ticker": "T",
            "yes_dollars_fp": [["0.5500", "3.00"]],
            "no_dollars_fp": [["0.4400", "2.00"]],
        }})
        ob = feed.get_orderbook("T")
        self.assertEqual(ob["yes"], [[55, 3]])
        self.assertEqual(ob["no"], [[44, 2]])

    def test_subcent_snapshot_levels_merge_same_integer_cent(self):
        """Kalshi prices are 4-decimal dollar strings. Rounding each
        independently to cents without summing qty leaves duplicate
        cent buckets that delta-apply cannot drain."""
        merged = KalshiFeed._normalize_fp_levels([
            ["0.1300", "10.00"],
            ["0.1310", "20.00"],
            ["0.1340", "5.00"],
        ])
        self.assertEqual(merged, [[13, 35]])

    def test_large_delete_drains_merged_cent_not_first_duplicate(self):
        """Live underflow: existing=75 delta=-9632 at 2¢. Pre-fix the
        first 2¢ duplicate (qty 75) was popped and the sibling 2¢
        bucket survived. Merged, the same delete lands on total qty
        and the cent is removed when it hits 0."""
        feed = _make_feed()
        feed._handle_ob_snapshot({"msg": {
            "market_ticker": "T",
            "yes_dollars_fp": [
                ["0.0200", "75.00"],
                ["0.0210", "9632.00"],
            ],
            "no_dollars_fp": [["0.5000", "10.00"]],
        }})
        self.assertEqual(feed._orderbooks["T"]["yes"], [[2, 9707]])
        feed._handle_ob_delta({"msg": {
            "market_ticker": "T",
            "price_dollars": "0.0210",
            "delta_fp": "-9632.00",
            "side": "yes",
        }})
        self.assertEqual(feed._orderbooks["T"]["yes"], [[2, 75]])
        self.assertIsNotNone(feed.get_orderbook("T"))

    def test_delta_before_snapshot_is_dropped(self):
        """Do NOT initialize an empty book from a delta. The delta is
        a diff against a snapshot we do not have; applying it is how
        the book goes permanently stale (86bbvztem class)."""
        feed = _make_feed()
        feed._handle_ob_delta({"msg": {
            "market_ticker": "T",
            "price_dollars": "0.9500",
            "delta_fp": "10.00",
            "side": "yes",
        }})
        self.assertNotIn("T", feed._orderbooks)
        self.assertIsNone(feed.get_orderbook("T"))

    def test_snapshot_then_delta_still_applies(self):
        """Happy path: snapshot arms the book; subsequent deltas apply."""
        feed = _make_feed()
        feed._handle_ob_snapshot({"msg": {
            "market_ticker": "T",
            "yes_dollars_fp": [["0.9500", "10.00"]],
            "no_dollars_fp": [["0.0400", "5.00"]],
        }})
        feed._handle_ob_delta({"msg": {
            "market_ticker": "T",
            "price_dollars": "0.9500",
            "delta_fp": "5.00",
            "side": "yes",
        }})
        self.assertEqual(feed._orderbooks["T"]["yes"], [[95, 15]])

    def test_get_orderbook_returns_a_copy(self):
        """Claude-C1: scanner `_ob_cache` stored the live dict, so a
        later crossed hide still served the mutated object via TTL."""
        feed = _make_feed()
        feed._orderbooks["T"] = {
            "yes": [[49, 10]], "no": [[50, 10]], "ts": 0,
        }
        ob = feed.get_orderbook("T")
        self.assertIsNotNone(ob)
        ob["yes"].append([99, 1])
        self.assertEqual(feed._orderbooks["T"]["yes"], [[49, 10]])

    def test_underflow_on_unknown_price_does_not_desync(self):
        """Claude-M6: a delete at a price we never held (truncated
        snapshot / already gone) used to be a benign clamp. Desyncing
        it pops the book on every deep cancel at window open."""
        feed = _make_feed()
        feed._handle_ob_snapshot({"msg": {
            "market_ticker": "T",
            "yes_dollars_fp": [["0.9500", "10.00"]],
            "no_dollars_fp": [["0.0400", "5.00"]],
        }})
        feed._handle_ob_delta({"msg": {
            "market_ticker": "T",
            "price_dollars": "0.0200",
            "delta_fp": "-9632.00",
            "side": "yes",
        }})
        self.assertIsNotNone(feed.get_orderbook("T"))
        self.assertEqual(feed._orderbooks["T"]["yes"], [[95, 10]])

    def test_get_orderbook_hides_strictly_crossed_book(self):
        """Live path (OrderExecutor / scanner) must not read a book
        whose yes_bid > implied yes ask. `>` not `>=`: a locked book
        is not proof of corruption (same as SyntheticRTIFeed)."""
        feed = _make_feed()
        feed._orderbooks["T"] = {
            "yes": [[99, 100]],
            "no": [[13, 1]],  # implied YES ask = 87 → crossed by 12¢
            "ts": 0,
        }
        self.assertIsNone(feed.get_orderbook("T"))
        # Raw cache kept for the snapshotter / forensics.
        self.assertEqual(feed._orderbooks["T"]["no"], [[13, 1]])
        self.assertIn("T", feed._pending_snapshot_requests)

    def test_get_orderbook_serves_locked_and_uncrossed(self):
        feed = _make_feed()
        feed._orderbooks["LOCKED"] = {
            "yes": [[50, 10]], "no": [[50, 10]], "ts": 0,
        }
        feed._orderbooks["OPEN"] = {
            "yes": [[49, 10]], "no": [[50, 10]], "ts": 0,
        }
        self.assertIsNotNone(feed.get_orderbook("LOCKED"))
        self.assertIsNotNone(feed.get_orderbook("OPEN"))

    def test_underflow_after_merge_hides_book_until_snapshot(self):
        """True underflow (venue remove > our merged qty) means the
        book already disagrees with the venue. Clamp is not a heal —
        hide the ticker until a snapshot rebuilds it."""
        feed = _make_feed()
        feed._handle_ob_snapshot({"msg": {
            "market_ticker": "T",
            "yes_dollars_fp": [["0.9600", "10.00"]],
            "no_dollars_fp": [["0.0300", "5.00"]],
        }})
        feed._handle_ob_delta({"msg": {
            "market_ticker": "T",
            "price_dollars": "0.9600",
            "delta_fp": "-50.00",
            "side": "yes",
        }})
        self.assertIsNone(feed.get_orderbook("T"))
        # A further delta must not resurrect a partial book.
        feed._handle_ob_delta({"msg": {
            "market_ticker": "T",
            "price_dollars": "0.5000",
            "delta_fp": "100.00",
            "side": "no",
        }})
        self.assertIsNone(feed.get_orderbook("T"))
        self.assertNotIn("T", feed._orderbooks)

    def test_seq_gap_on_orderbook_delta_drops_book_until_snapshot(self):
        """Kalshi docs: seq is for snapshot/delta consistency. A gap
        means we missed a diff; the current book is not a valid base.
        Port of SyntheticRTIFeed.reset_venue + awaiting_snapshot."""
        from kalshi_wire.ws_client import Frame

        feed = _make_feed()
        feed._handle_ob_snapshot({"msg": {
            "market_ticker": "T",
            "yes_dollars_fp": [["0.9500", "10.00"]],
            "no_dollars_fp": [["0.0400", "5.00"]],
        }})
        feed._ticker_to_sid["T"] = 2
        raw = json.dumps({
            "type": "orderbook_delta",
            "sid": 2,
            "seq": 99,
            "msg": {
                "market_ticker": "T",
                "price_dollars": "0.9900",
                "delta_fp": "50.00",
                "side": "yes",
            },
        })
        frame = Frame(
            wire_recv_ts=0.0,
            raw=raw,
            parsed=json.loads(raw),
            msg_type="orderbook_delta",
            sid=2,
            seq=99,
            seq_gap=True,
        )
        feed._on_frame(frame)
        self.assertIsNone(feed.get_orderbook("T"))
        self.assertNotIn("T", feed._orderbooks)

    def test_seq_gap_on_orderbook_snapshot_is_the_rebuild(self):
        """The first frame after a hole is often the get_snapshot
        response. Dropping it (same as a gapped delta) would leave
        the ticker awaiting forever. Apply the snapshot after the
        sid-wide drop."""
        from kalshi_wire.ws_client import Frame

        feed = _make_feed()
        feed._handle_ob_snapshot({"msg": {
            "market_ticker": "T",
            "yes_dollars_fp": [["0.4000", "10.00"]],
            "no_dollars_fp": [["0.5000", "10.00"]],
        }})
        feed._ticker_to_sid["T"] = 2
        raw = json.dumps({
            "type": "orderbook_snapshot",
            "sid": 2,
            "seq": 99,
            "msg": {
                "market_ticker": "T",
                "yes_dollars_fp": [["0.5500", "20.00"]],
                "no_dollars_fp": [["0.4400", "5.00"]],
            },
        })
        frame = Frame(
            wire_recv_ts=0.0,
            raw=raw,
            parsed=json.loads(raw),
            msg_type="orderbook_snapshot",
            sid=2,
            seq=99,
            seq_gap=True,
        )
        feed._on_frame(frame)
        ob = feed.get_orderbook("T")
        self.assertIsNotNone(ob)
        self.assertEqual(ob["yes"], [[55, 20]])
        self.assertEqual(ob["no"], [[44, 5]])

    def test_seq_gap_unmapped_sid_does_not_wipe_all_books(self):
        """R1-M1: an orphan/unmapped sid is not 'every cached book'."""
        from kalshi_wire.ws_client import Frame

        feed = _make_feed()
        feed._handle_ob_snapshot({"msg": {
            "market_ticker": "T",
            "yes_dollars_fp": [["0.5000", "10.00"]],
            "no_dollars_fp": [["0.4900", "10.00"]],
        }})
        raw = json.dumps({
            "type": "orderbook_delta",
            "sid": 99,
            "seq": 5,
            "msg": {
                "market_ticker": "ORPHAN",
                "price_dollars": "0.10",
                "delta_fp": "1",
                "side": "yes",
            },
        })
        feed._wire.request_reconnect = MagicMock()
        feed._on_frame(Frame(
            wire_recv_ts=0.0, raw=raw, parsed=json.loads(raw),
            msg_type="orderbook_delta", sid=99, seq=5, seq_gap=True,
        ))
        self.assertIn("T", feed._orderbooks)
        self.assertNotIn("ORPHAN", feed._awaiting_set())
        self.assertNotIn("ORPHAN", feed._pending_snapshot_requests)
        feed._wire.request_reconnect.assert_not_called()

    def test_seq_gap_on_ok_rebuilds_mapped_sid(self):
        """R1-M2: type=ok shares the orderbook seq stream. A gap on
        an ack is a missed delta; the next delta must not apply on
        the dirty book."""
        from kalshi_wire.ws_client import Frame

        feed = _make_feed()
        feed._handle_ob_snapshot({"msg": {
            "market_ticker": "T",
            "yes_dollars_fp": [["0.5000", "10.00"]],
            "no_dollars_fp": [["0.4900", "10.00"]],
        }})
        feed._ticker_to_sid["T"] = 2
        raw = json.dumps({
            "type": "ok", "sid": 2, "seq": 99, "id": 3,
            "msg": {"market_tickers": ["T"]},
        })
        feed._on_frame(Frame(
            wire_recv_ts=0.0, raw=raw, parsed=json.loads(raw),
            msg_type="ok", sid=2, seq=99, seq_gap=True,
        ))
        self.assertNotIn("T", feed._orderbooks)
        self.assertIsNone(feed.get_orderbook("T"))

    def test_oft_snapshot_for_keeps_crossed_and_awaiting(self):
        """Claude-R2 M4: OFT is flow-diagnostic, not trading-price.
        Filtering here interleaves full-depth WS with REST depth=5
        and fires false depth_drain. Trading-path hide stays on
        get_orderbook."""
        feed = _make_feed()
        feed._orderbooks["CROSSED"] = {
            "yes": [[99, 1]], "no": [[13, 1]], "ts": 0,
        }
        feed._orderbooks["OK"] = {
            "yes": [[49, 1]], "no": [[50, 1]], "ts": 0,
        }
        feed._orderbooks["WAIT"] = {
            "yes": [[40, 1]], "no": [[50, 1]], "ts": 0,
        }
        feed._awaiting_set().add("WAIT")
        out = feed.get_orderbooks_snapshot_for(["CROSSED", "OK", "WAIT"])
        self.assertEqual(set(out), {"CROSSED", "OK", "WAIT"})
        self.assertIsNone(feed.get_orderbook("CROSSED"))
        self.assertIsNone(feed.get_orderbook("WAIT"))
        self.assertIsNotNone(feed.get_orderbook("OK"))

    def test_get_snapshot_without_sid_while_awaiting_reconnects(self):
        """R1-M4: awaiting + no sid cannot skip-and-forget — reconnect."""
        feed = _make_feed()
        feed._ws_connect_ts = time.time() - 120  # past boot window
        feed._last_ws_reconnect_request_mono = 0.0
        with feed._lock:
            feed._mark_book_desynced_locked("T")
        feed._wire.request_reconnect = MagicMock()
        feed._send_ob_get_snapshot("T")
        feed._wire.request_reconnect.assert_called_once()
        self.assertIn("T", feed._awaiting_set())

    def test_second_seq_gap_inside_reconnect_cooldown_queues_snapshots(self):
        """Claude-R2 M1: a second gap inside the 30s reconnect
        cooldown used to pop every book on the sid and queue no
        heal. Recovery must still be dispatched (per-ticker
        get_snapshot) even when reconnect is suppressed."""
        from kalshi_wire.ws_client import Frame

        feed = _make_feed()
        for t in ("A", "B"):
            feed._subscribed_tickers.add(t)
            feed._handle_ob_snapshot({"msg": {
                "market_ticker": t,
                "yes_dollars_fp": [["0.5000", "10.00"]],
                "no_dollars_fp": [["0.4900", "10.00"]],
            }})
            feed._ticker_to_sid[t] = 2
        feed._wire.request_reconnect = MagicMock()
        raw = json.dumps({
            "type": "orderbook_delta",
            "sid": 2,
            "seq": 99,
            "msg": {
                "market_ticker": "A",
                "price_dollars": "0.5000",
                "delta_fp": "1.00",
                "side": "yes",
            },
        })
        frame = Frame(
            wire_recv_ts=0.0, raw=raw, parsed=json.loads(raw),
            msg_type="orderbook_delta", sid=2, seq=99, seq_gap=True,
        )
        feed._on_frame(frame)
        feed._wire.request_reconnect.assert_called_once()
        # Same session, cooldown still live (do not reset stamp).
        for t in ("A", "B"):
            feed._awaiting_set().discard(t)
            feed._handle_ob_snapshot({"msg": {
                "market_ticker": t,
                "yes_dollars_fp": [["0.5000", "10.00"]],
                "no_dollars_fp": [["0.4900", "10.00"]],
            }})
        feed._last_ws_reconnect_request_mono = time.monotonic()
        feed._pending_snapshot_requests.clear()
        feed._on_frame(frame)
        feed._wire.request_reconnect.assert_called_once()
        self.assertNotIn("A", feed._orderbooks)
        self.assertNotIn("B", feed._orderbooks)
        self.assertIn("A", feed._pending_snapshot_requests)
        self.assertIn("B", feed._pending_snapshot_requests)

    def test_seq_gap_on_fill_does_not_drop_book_or_reconnect(self):
        """Claude-R2 M2: fill-channel sid is never in _ticker_to_sid.
        A gapped fill must not take the market_ticker fallback, pop
        that book's cache, or bounce the session."""
        from kalshi_wire.ws_client import Frame

        feed = _make_feed()
        feed._handle_ob_snapshot({"msg": {
            "market_ticker": "T",
            "yes_dollars_fp": [["0.5000", "10.00"]],
            "no_dollars_fp": [["0.4900", "10.00"]],
        }})
        feed._ticker_to_sid["T"] = 2
        raw = json.dumps({
            "type": "fill",
            "sid": 99,
            "seq": 5,
            "msg": {
                "market_ticker": "T",
                "ticker": "T",
                "order_id": "oid",
                "side": "yes",
                "count": 1,
            },
        })
        feed._wire.request_reconnect = MagicMock()
        feed._on_frame(Frame(
            wire_recv_ts=0.0, raw=raw, parsed=json.loads(raw),
            msg_type="fill", sid=99, seq=5, seq_gap=True,
        ))
        self.assertIn("T", feed._orderbooks)
        self.assertNotIn("T", feed._awaiting_set())
        feed._wire.request_reconnect.assert_not_called()

    def test_get_snapshot_no_sid_during_boot_retries_without_timeout(self):
        """Grok-R5 M4: skip-and-forget on boot/in-flight popped
        _snapshot_request_pending and left no work on the drain
        queue. Skip must re-queue get_snapshot and must not arm
        the 5s unsub+resub timer (we never sent)."""
        feed = _make_feed()
        feed._ws_connect_ts = time.time()  # still in boot window
        with feed._lock:
            feed._mark_book_desynced_locked("T")
        # Drain copies then CLEARS the queue before send (the
        # forget-on-skip hole).
        snap_reqs = list(feed._pending_snapshot_requests)
        feed._pending_snapshot_requests.clear()
        feed._wire.request_reconnect = MagicMock()
        for t in snap_reqs:
            feed._send_ob_get_snapshot(t)
        feed._wire.request_reconnect.assert_not_called()
        self.assertIn("T", feed._pending_snapshot_requests)
        self.assertNotIn("T", feed._snapshot_request_pending)
        timed_out = feed._check_snapshot_timeouts()
        self.assertNotIn("T", timed_out)
        self.assertNotIn("T", feed._pending_unsubscribes)

    def test_get_snapshot_no_sid_in_flight_retries_without_reconnect(self):
        """Grok-R5 M4: subscribe-in-flight is not 'sid lost'."""
        feed = _make_feed()
        feed._ws_connect_ts = time.time() - 120
        feed._outstanding_subscribes[101] = "T"
        with feed._lock:
            feed._mark_book_desynced_locked("T")
        snap_reqs = list(feed._pending_snapshot_requests)
        feed._pending_snapshot_requests.clear()
        feed._wire.request_reconnect = MagicMock()
        for t in snap_reqs:
            feed._send_ob_get_snapshot(t)
        feed._wire.request_reconnect.assert_not_called()
        self.assertIn("T", feed._pending_snapshot_requests)
        self.assertNotIn("T", feed._snapshot_request_pending)

    def test_crossed_hide_does_not_starve_force_resubscribe(self):
        """Claude-R2 MN1: get_orderbook on a crossed book used
        _force_resub_cooldown, so flag_ticker_drifted →
        force_resubscribe always lost the race."""
        feed = _make_feed()
        feed._ticker_to_sid["T"] = 2
        feed._orderbooks["T"] = {
            "yes": [[99, 100]],
            "no": [[13, 1]],
            "ts": 0,
        }
        self.assertIsNone(feed.get_orderbook("T"))
        feed._pending_snapshot_requests.clear()
        feed.force_resubscribe("T", purge_cache=False)
        self.assertIn("T", feed._pending_snapshot_requests)

    def test_get_orderbook_copy_is_two_level_not_alias(self):
        """Claude-R2 MN2: trading-path copy must not alias level lists."""
        feed = _make_feed()
        feed._orderbooks["T"] = {
            "yes": [[49, 10]], "no": [[50, 10]], "ts": 1.5,
        }
        ob = feed.get_orderbook("T")
        ob["yes"][0][1] = 999
        self.assertEqual(feed._orderbooks["T"]["yes"], [[49, 10]])

    def test_unsubscribe_between_drain_and_send_does_not_rearm_pending(self):
        """Claude-R3 M1: post-send arming re-inserted the tracker
        unsubscribe_ticker just popped. Snapshot never arrives
        (unsubscribed) → guaranteed timeout → disable latch."""
        feed = _make_feed()
        feed._ws_connect_ts = time.time() - 120
        feed._ticker_to_sid["T"] = 2
        with feed._lock:
            feed._mark_book_desynced_locked("T")
        snap_reqs = list(feed._pending_snapshot_requests)
        feed._pending_snapshot_requests.clear()
        feed.unsubscribe_ticker("T")
        feed._wire.send_frame = MagicMock()
        sweeps_before = feed._get_snapshot_consecutive_failed_sweeps
        for t in snap_reqs:
            feed._send_ob_get_snapshot(t)
        self.assertNotIn("T", feed._snapshot_request_pending)
        feed._wire.send_frame.assert_not_called()
        timed_out = feed._check_snapshot_timeouts()
        self.assertNotIn("T", timed_out)
        self.assertEqual(
            feed._get_snapshot_consecutive_failed_sweeps, sweeps_before)

    def test_unsubscribed_stale_pending_does_not_increment_failed_sweeps(self):
        """Claude-R3 M1 defense: a leftover tracker for a ticker
        that has already left _subscribed_tickers is lifecycle
        churn, not a Kalshi-contract failure."""
        import bot.constants as C
        feed = _make_feed()
        feed._subscribed_tickers.discard("T")
        feed._snapshot_request_pending["T"] = (
            time.monotonic() - C.WS_SNAPSHOT_REQUEST_TIMEOUT_S - 1.0
        )
        sweeps_before = feed._get_snapshot_consecutive_failed_sweeps
        timed_out = feed._check_snapshot_timeouts()
        self.assertEqual(timed_out, [])
        self.assertEqual(
            feed._get_snapshot_consecutive_failed_sweeps, sweeps_before)
        self.assertFalse(feed._get_snapshot_disabled)

    def test_seq_gap_unions_frame_ticker_not_yet_in_sid_map(self):
        """Claude-R5 M1: sid already mapped to A/B, gapped delta for
        C (snapshot landed, ack not yet → no _ticker_to_sid). The
        fallback was `if not tickers` so C was skipped; rebuilt=True
        still dropped C's delta and left C's book live unflagged."""
        from kalshi_wire.ws_client import Frame

        feed = _make_feed()
        for t in ("A", "B", "C"):
            feed._subscribed_tickers.add(t)
            feed._handle_ob_snapshot({"msg": {
                "market_ticker": t,
                "yes_dollars_fp": [["0.5000", "10.00"]],
                "no_dollars_fp": [["0.4900", "10.00"]],
            }})
        feed._ticker_to_sid["A"] = 1
        feed._ticker_to_sid["B"] = 1
        # C snapshot landed, subscribe ack not yet — no sid.
        self.assertNotIn("C", feed._ticker_to_sid)
        feed._wire.request_reconnect = MagicMock()
        raw = json.dumps({
            "type": "orderbook_delta",
            "sid": 1,
            "seq": 99,
            "msg": {
                "market_ticker": "C",
                "price_dollars": "0.5000",
                "delta_fp": "-4.00",
                "side": "yes",
            },
        })
        feed._on_frame(Frame(
            wire_recv_ts=0.0, raw=raw, parsed=json.loads(raw),
            msg_type="orderbook_delta", sid=1, seq=99, seq_gap=True,
        ))
        self.assertNotIn("C", feed._orderbooks)
        self.assertIn("C", feed._awaiting_set())
        self.assertEqual(feed._orderbooks.get("C"), None)

    def test_disabled_unsub_resub_skips_ticker_without_sid(self):
        """Claude-R5 MN1: _queue_unsub_resub_locked must not queue
        a resub that would leak sid_v1."""
        feed = _make_feed()
        feed._get_snapshot_disabled = True
        feed._ticker_to_sid.pop("T", None)
        with feed._lock:
            feed._mark_book_desynced_locked("T")
        self.assertNotIn("T", feed._pending_subscribes)
        self.assertNotIn("T", feed._pending_unsubscribes)

    def test_session_start_then_seq_gap_does_not_bounce(self):
        """Claude-R4 M1: after a Kalshi-side disconnect the stamp
        was 0.0, so a gap in the resubscribe-burst window promptly
        closed the brand-new session. Session start must arm the
        cooldown so the gap queues snapshots instead."""
        from kalshi_wire.ws_client import Frame

        feed = _make_feed()
        feed._wire.send_frame = MagicMock()
        feed._wire.request_reconnect = MagicMock()
        feed._handle_ob_snapshot({"msg": {
            "market_ticker": "T",
            "yes_dollars_fp": [["0.5000", "10.00"]],
            "no_dollars_fp": [["0.4900", "10.00"]],
        }})
        feed._ticker_to_sid["T"] = 2
        feed._last_ws_reconnect_request_mono = 0.0
        feed._on_session_start()
        raw = json.dumps({
            "type": "orderbook_delta",
            "sid": 2,
            "seq": 99,
            "msg": {
                "market_ticker": "T",
                "price_dollars": "0.5000",
                "delta_fp": "1.00",
                "side": "yes",
            },
        })
        feed._on_frame(Frame(
            wire_recv_ts=0.0, raw=raw, parsed=json.loads(raw),
            msg_type="orderbook_delta", sid=2, seq=99, seq_gap=True,
        ))
        feed._wire.request_reconnect.assert_not_called()
        self.assertIn("T", feed._pending_snapshot_requests)

    def test_unsubscribed_no_sid_does_not_requeue_snapshot(self):
        """Claude-R4 MN1 / Grok-R7 MN1: first sid-is-None block
        re-queued a settled ticker forever."""
        feed = _make_feed()
        feed._ws_connect_ts = time.time() - 120
        feed._ticker_to_sid.pop("T", None)
        feed._subscribed_tickers.discard("T")
        feed._pending_snapshot_requests.append("T")
        snap_reqs = list(feed._pending_snapshot_requests)
        feed._pending_snapshot_requests.clear()
        feed._wire.request_reconnect = MagicMock()
        for t in snap_reqs:
            feed._send_ob_get_snapshot(t)
        feed._wire.request_reconnect.assert_not_called()
        self.assertNotIn("T", feed._pending_snapshot_requests)

    def test_request_reconnect_schedules_ws_close(self):
        """Grok-R6 M1: request_reconnect only set a flag the
        silence watchdog observes AFTER a 30s sleep, so seq-gap
        fail-close stayed dark for a full watchdog interval."""
        from kalshi_wire.ws_client import WSClient

        wire = WSClient(
            api_key="t", private_key=MagicMock(), on_frame=MagicMock(),
        )
        loop = MagicMock()
        ws = MagicMock()
        wire._loop = loop
        wire._ws = ws
        wire.request_reconnect()
        self.assertTrue(wire._force_reconnect_requested)
        loop.call_soon_threadsafe.assert_called_once()


if __name__ == "__main__":
    unittest.main()


