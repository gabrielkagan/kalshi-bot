"""Tests for WS_DRIFT_PROBE — REST vs WS cache diff instrumentation.

The probe runs once per minute during scan(), picks a random subscribed
15M ticker with a non-empty WS cache, fetches REST /orderbook with
depth=100, and logs a per-side diff (level counts, qty totals, missing
qty, worst-level delta). Observation-only — no state mutation.

Why it exists: to empirically measure the H-NEW-B hypothesis (WS
snapshots are truncated at subscribe time), which is the leading
candidate root cause for residual `WS delta underflow` warnings after
commit 09fee46 proved cosmetic. See
kb/failures/kalshi-ws-schema-drift.md § "Correction + full investigation
walkback".

Coverage:
- 60s throttle (scan() ticks every few seconds; probe should fire at most
  once per minute)
- Feed disconnected / missing → no-op
- No populated 15M tickers → no-op
- Only non-15M tickers in cache → no-op
- REST fetch returns None / exception → no-op (logged but non-fatal)
- Diff math on all four patterns: perfect match, only-WS levels,
  only-REST levels, qty mismatch on matching price
- Worst-missing-level calculation picks the max positive diff
- YES and NO sides handled independently
- Observation-only: WS cache is not modified by the probe
"""

import os
import sys
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bot import OpportunityScanner


def _make_scanner_for_drift_probe(
    *, feed_connected=True, ws_obs=None, rest_resp=None,
    last_run=0.0, current_time=1000.0
) -> OpportunityScanner:
    """Construct an OpportunityScanner with only the attributes the drift
    probe touches — bypasses __init__ to avoid real deps."""
    s = OpportunityScanner.__new__(OpportunityScanner)

    s._drift_probe_last_run = last_run

    # KalshiFeed stub
    feed = MagicMock()
    feed.is_connected = feed_connected
    feed.get_all_orderbooks = MagicMock(return_value=ws_obs or {})
    s._kalshi_feed = feed

    # KalshiClient stub — get_orderbook returns rest_resp (None or dict)
    client = MagicMock()
    if isinstance(rest_resp, Exception):
        client.get_orderbook = MagicMock(side_effect=rest_resp)
    else:
        client.get_orderbook = MagicMock(return_value=rest_resp)
    s._client = client

    return s


class TestDriftProbeThrottle(unittest.TestCase):
    """60s throttle prevents REST-call spam even if scan() fires every
    few seconds."""

    def test_skips_when_last_run_within_60s(self):
        s = _make_scanner_for_drift_probe(
            ws_obs={"KXBTC15M-26APR241100-00": {"yes": [[95, 10]], "no": []}},
        )
        s._drift_probe_last_run = time.time() - 30  # 30s ago
        before = s._kalshi_feed.get_all_orderbooks.call_count
        s._drift_probe_tick()
        # Should bail before calling get_all_orderbooks
        self.assertEqual(s._kalshi_feed.get_all_orderbooks.call_count, before)

    def test_runs_when_last_run_over_60s_ago(self):
        s = _make_scanner_for_drift_probe(
            ws_obs={"KXBTC15M-26APR241100-00": {"yes": [[95, 10]], "no": []}},
            rest_resp={"orderbook_fp": {"yes_dollars": [["0.95", "10"]],
                                     "no_dollars": []}},
        )
        s._drift_probe_last_run = time.time() - 61  # 61s ago
        s._drift_probe_tick()
        self.assertTrue(s._kalshi_feed.get_all_orderbooks.called)

    def test_runs_on_first_call(self):
        """last_run = 0 means never run before — should fire immediately."""
        s = _make_scanner_for_drift_probe(
            ws_obs={"KXBTC15M-26APR241100-00": {"yes": [[95, 10]], "no": []}},
            rest_resp={"orderbook_fp": {"yes_dollars": [["0.95", "10"]],
                                     "no_dollars": []}},
            last_run=0.0,
        )
        s._drift_probe_tick()
        self.assertTrue(s._client.get_orderbook.called)

    def test_updates_last_run_after_throttle_expires(self):
        s = _make_scanner_for_drift_probe(
            ws_obs={"KXBTC15M-26APR241100-00": {"yes": [[95, 10]], "no": []}},
            rest_resp={"orderbook_fp": {"yes_dollars": [["0.95", "10"]],
                                     "no_dollars": []}},
        )
        before = s._drift_probe_last_run
        s._drift_probe_tick()
        self.assertGreater(s._drift_probe_last_run, before)


class TestDriftProbeFeedGating(unittest.TestCase):
    """Probe no-ops gracefully when the WS feed is missing or disconnected."""

    def test_no_feed_skips(self):
        s = OpportunityScanner.__new__(OpportunityScanner)
        s._drift_probe_last_run = 0.0
        s._kalshi_feed = None
        s._client = MagicMock()
        # Should not raise
        s._drift_probe_tick()
        self.assertFalse(s._client.get_orderbook.called)

    def test_disconnected_feed_skips(self):
        s = _make_scanner_for_drift_probe(feed_connected=False)
        s._drift_probe_tick()
        self.assertFalse(s._client.get_orderbook.called)


class TestDriftProbeTickerSelection(unittest.TestCase):
    """Probe picks a subscribed 15M ticker with non-empty WS cache. Skips
    non-15M tickers and empty books."""

    def test_skips_non_15m_tickers(self):
        s = _make_scanner_for_drift_probe(
            ws_obs={
                "KXBTCD-26APR241100": {"yes": [[95, 10]], "no": []},
                "KXHIGHNY-26APR24-T67": {"yes": [[61, 100]], "no": []},
            },
        )
        s._drift_probe_tick()
        # No 15M tickers → no REST fetch
        self.assertFalse(s._client.get_orderbook.called)

    def test_skips_empty_books(self):
        s = _make_scanner_for_drift_probe(
            ws_obs={
                "KXBTC15M-26APR241100-00": {"yes": [], "no": []},
            },
        )
        s._drift_probe_tick()
        self.assertFalse(s._client.get_orderbook.called)

    def test_picks_among_populated_15m_only(self):
        s = _make_scanner_for_drift_probe(
            ws_obs={
                "KXBTC15M-26APR241100-00": {"yes": [], "no": []},       # empty
                "KXETH15M-26APR241100-00": {"yes": [[90, 5]], "no": []},  # populated
                "KXBTCD-26APR241100": {"yes": [[95, 100]], "no": []},   # non-15m
            },
            rest_resp={"orderbook_fp": {"yes_dollars": [["0.90", "5"]],
                                     "no_dollars": []}},
        )
        s._drift_probe_tick()
        # Must have fetched ETH 15M (the only eligible ticker)
        args, _ = s._client.get_orderbook.call_args
        self.assertEqual(args[0], "KXETH15M-26APR241100-00")

    def test_case_insensitive_15m_filter(self):
        """Tickers are uppercase by convention — filter should tolerate
        variations defensively."""
        s = _make_scanner_for_drift_probe(
            ws_obs={"kxbtc15m-26apr241100-00": {"yes": [[95, 10]], "no": []}},
            rest_resp={"orderbook_fp": {"yes_dollars": [["0.95", "10"]],
                                     "no_dollars": []}},
        )
        s._drift_probe_tick()
        self.assertTrue(s._client.get_orderbook.called)


class TestDriftProbeRestErrors(unittest.TestCase):
    """REST fetch failures don't crash the probe or the scan loop."""

    def test_rest_returns_none_no_crash(self):
        s = _make_scanner_for_drift_probe(
            ws_obs={"KXBTC15M-26APR241100-00": {"yes": [[95, 10]], "no": []}},
            rest_resp=None,
        )
        # Should not raise
        s._drift_probe_tick()

    def test_rest_raises_exception_no_crash(self):
        s = _make_scanner_for_drift_probe(
            ws_obs={"KXBTC15M-26APR241100-00": {"yes": [[95, 10]], "no": []}},
            rest_resp=ConnectionError("network down"),
        )
        # Should not raise
        s._drift_probe_tick()

    def test_rest_returns_empty_dict_no_crash(self):
        s = _make_scanner_for_drift_probe(
            ws_obs={"KXBTC15M-26APR241100-00": {"yes": [[95, 10]], "no": []}},
            rest_resp={},
        )
        s._drift_probe_tick()

    def test_rest_missing_orderbook_key_no_crash(self):
        """When response has neither orderbook_fp nor orderbook, we log
        WS_DRIFT_PROBE unknown_rest_shape and return without crashing."""
        s = _make_scanner_for_drift_probe(
            ws_obs={"KXBTC15M-26APR241100-00": {"yes": [[95, 10]], "no": []}},
            rest_resp={"something_else": "x"},
        )
        with self.assertLogs(level="WARNING") as cm:
            s._drift_probe_tick()
        self.assertTrue(any("unknown_rest_shape" in r.getMessage()
                            for r in cm.records))


class TestDriftProbeRestShapes(unittest.TestCase):
    """Kalshi REST /orderbook returns one of two shapes. Probe supports both.

    See bot.py:13538-13546 for the same dual-shape handling pattern in
    _get_orderbook_cached.
    """

    def test_new_orderbook_fp_shape_processed(self):
        """New FP format: {"orderbook_fp": {"yes_dollars": [[dollar_str,
        fp_qty_str], ...], "no_dollars": [...]}}."""
        s = _make_scanner_for_drift_probe(
            ws_obs={"KXBTC15M-26APR241100-00": {"yes": [[95, 50]], "no": []}},
            rest_resp={"orderbook_fp": {
                "yes_dollars": [["0.95", "200"]],
                "no_dollars": [],
            }},
        )
        with self.assertLogs(level="WARNING") as cm:
            s._drift_probe_tick()
        yes_log = next(r.getMessage() for r in cm.records
                       if "WS_DRIFT_PROBE" in r.getMessage() and " yes:" in r.getMessage())
        # WS has 50, REST has 200 → missing_qty=+150
        self.assertIn("missing_qty=+150", yes_log)

    def test_legacy_orderbook_shape_processed(self):
        """Legacy format: {"orderbook": {"yes": [[cents_int, qty_int], ...],
        "no": [...]}}."""
        s = _make_scanner_for_drift_probe(
            ws_obs={"KXBTC15M-26APR241100-00": {"yes": [[95, 50]], "no": []}},
            rest_resp={"orderbook": {
                "yes": [[95, 200]],
                "no": [],
            }},
        )
        with self.assertLogs(level="WARNING") as cm:
            s._drift_probe_tick()
        yes_log = next(r.getMessage() for r in cm.records
                       if "WS_DRIFT_PROBE" in r.getMessage() and " yes:" in r.getMessage())
        self.assertIn("missing_qty=+150", yes_log)

    def test_fp_preferred_over_legacy_when_both_present(self):
        """If response somehow has BOTH keys, prefer orderbook_fp (newer,
        higher fidelity). Documents current precedence."""
        s = _make_scanner_for_drift_probe(
            ws_obs={"KXBTC15M-26APR241100-00": {"yes": [[95, 50]], "no": []}},
            rest_resp={
                "orderbook_fp": {
                    "yes_dollars": [["0.95", "300"]],
                    "no_dollars": [],
                },
                "orderbook": {   # deliberately different, should be ignored
                    "yes": [[95, 999999]],
                    "no": [],
                },
            },
        )
        with self.assertLogs(level="WARNING") as cm:
            s._drift_probe_tick()
        yes_log = next(r.getMessage() for r in cm.records
                       if "WS_DRIFT_PROBE" in r.getMessage() and " yes:" in r.getMessage())
        # Should use orderbook_fp value (300), not orderbook value (999999)
        self.assertIn("missing_qty=+250", yes_log)


class TestDriftProbeDiffLogging(unittest.TestCase):
    """Verify the log output contains correct diff stats for each pattern.

    The probe logs via logging.warning; we capture with assertLogs."""

    def _run_probe(self, ws_ob, rest_ob_fp):
        s = _make_scanner_for_drift_probe(
            ws_obs={"KXBTC15M-26APR241100-00": ws_ob},
            rest_resp={"orderbook_fp": rest_ob_fp},
        )
        with self.assertLogs(level="WARNING") as cm:
            s._drift_probe_tick()
        return [r.getMessage() for r in cm.records
                if "WS_DRIFT_PROBE" in r.getMessage()]

    def test_perfect_match_zero_diff(self):
        """WS and REST have identical state → log shows missing_qty=+0."""
        ws_ob = {"yes": [[95, 100]], "no": [[5, 50]]}
        rest_fp = {
            "yes_dollars": [["0.95", "100"]],
            "no_dollars": [["0.05", "50"]],
        }
        logs = self._run_probe(ws_ob, rest_fp)
        yes_log = next(m for m in logs if " yes:" in m)
        no_log = next(m for m in logs if " no:" in m)
        self.assertIn("missing_qty=+0", yes_log)
        self.assertIn("missing_qty=+0", no_log)
        self.assertIn("only_ws=0", yes_log)
        self.assertIn("only_rest=0", yes_log)

    def test_rest_has_more_levels_missing_qty_positive(self):
        """REST has levels WS doesn't know about (H-NEW-B signature)."""
        ws_ob = {"yes": [[95, 10]], "no": []}
        rest_fp = {
            "yes_dollars": [["0.95", "10"], ["0.01", "8880"]],  # deep level missing
            "no_dollars": [],
        }
        logs = self._run_probe(ws_ob, rest_fp)
        yes_log = next(m for m in logs if " yes:" in m)
        self.assertIn("only_rest=1", yes_log)
        self.assertIn("missing_qty=+8880", yes_log)
        self.assertIn("worst_level=1", yes_log)
        self.assertIn("worst_missing=8880", yes_log)

    def test_ws_has_levels_rest_does_not(self):
        """Stale WS state with levels that REST says are gone (rare)."""
        ws_ob = {"yes": [[95, 10], [50, 100]], "no": []}
        rest_fp = {
            "yes_dollars": [["0.95", "10"]],  # 50c level is gone in REST
            "no_dollars": [],
        }
        logs = self._run_probe(ws_ob, rest_fp)
        yes_log = next(m for m in logs if " yes:" in m)
        self.assertIn("only_ws=1", yes_log)
        # missing_qty is negative when WS has more than REST
        self.assertIn("missing_qty=-100", yes_log)

    def test_qty_mismatch_same_price(self):
        """Same price level, different qty → counted as qty_mismatch."""
        ws_ob = {"yes": [[95, 100]], "no": []}
        rest_fp = {
            "yes_dollars": [["0.95", "250"]],  # REST has 250, WS has 100
            "no_dollars": [],
        }
        logs = self._run_probe(ws_ob, rest_fp)
        yes_log = next(m for m in logs if " yes:" in m)
        self.assertIn("qty_mismatch=1", yes_log)
        self.assertIn("missing_qty=+150", yes_log)
        self.assertIn("ws_qty_total=100", yes_log)
        self.assertIn("rest_qty_total=250", yes_log)

    def test_worst_level_picks_max_missing(self):
        """With multiple missing levels, worst_level/worst_missing pick
        the largest positive diff."""
        # WS needs SOMETHING on at least one side to not be filtered out
        # as an "empty book"; keep NO side populated to exercise YES diff.
        ws_ob = {"yes": [], "no": [[5, 1]]}
        rest_fp = {
            "yes_dollars": [
                ["0.01", "100"],
                ["0.05", "9999"],   # biggest
                ["0.95", "50"],
            ],
            "no_dollars": [["0.05", "1"]],
        }
        logs = self._run_probe(ws_ob, rest_fp)
        yes_log = next(m for m in logs if " yes:" in m)
        self.assertIn("worst_level=5", yes_log)
        self.assertIn("worst_missing=9999", yes_log)

    def test_sides_handled_independently(self):
        """Different diffs on YES vs NO sides log as separate lines."""
        ws_ob = {"yes": [[95, 100]], "no": [[5, 50]]}
        rest_fp = {
            "yes_dollars": [["0.95", "100"]],   # matches
            "no_dollars": [["0.05", "500"]],    # 10x WS
        }
        logs = self._run_probe(ws_ob, rest_fp)
        yes_log = next(m for m in logs if " yes:" in m)
        no_log = next(m for m in logs if " no:" in m)
        self.assertIn("missing_qty=+0", yes_log)
        self.assertIn("missing_qty=+450", no_log)

    def test_produces_exactly_two_log_lines_per_run(self):
        """One WARNING log per side (yes, no) = 2 lines per probe run."""
        ws_ob = {"yes": [[95, 10]], "no": [[5, 5]]}
        rest_fp = {
            "yes_dollars": [["0.95", "10"]],
            "no_dollars": [["0.05", "5"]],
        }
        logs = self._run_probe(ws_ob, rest_fp)
        self.assertEqual(len(logs), 2)
        self.assertTrue(any(" yes:" in m for m in logs))
        self.assertTrue(any(" no:" in m for m in logs))


class TestDriftProbeNoMutation(unittest.TestCase):
    """The probe MUST be observation-only. Running it cannot alter the
    WS cache, otherwise it'd confound trading state."""

    def test_ws_cache_unchanged_after_probe(self):
        ws_ob = {"yes": [[95, 100]], "no": [[5, 50]]}
        ws_ob_snapshot_copy = {
            "yes": [list(lvl) for lvl in ws_ob["yes"]],
            "no": [list(lvl) for lvl in ws_ob["no"]],
        }
        s = _make_scanner_for_drift_probe(
            ws_obs={"KXBTC15M-26APR241100-00": ws_ob},
            rest_resp={"orderbook_fp": {
                "yes_dollars": [["0.95", "999"]],  # radically different
                "no_dollars": [["0.05", "999"]],
            }},
        )
        s._drift_probe_tick()
        # Mutating REST response shouldn't propagate back into WS cache —
        # probe should only READ get_all_orderbooks (which returns shallow
        # copies per contract).
        self.assertEqual(ws_ob["yes"], ws_ob_snapshot_copy["yes"])
        self.assertEqual(ws_ob["no"], ws_ob_snapshot_copy["no"])


class TestDriftProbeMalformedLevels(unittest.TestCase):
    """Robustness against corrupt level arrays from either side."""

    def test_skips_malformed_ws_level(self):
        """A malformed WS entry (missing qty) is skipped without crash."""
        s = _make_scanner_for_drift_probe(
            ws_obs={"KXBTC15M-26APR241100-00": {
                "yes": [[95, 100], "garbage", [50]],  # 2nd/3rd are malformed
                "no": [],
            }},
            rest_resp={"orderbook_fp": {
                "yes_dollars": [["0.95", "100"]],
                "no_dollars": [],
            }},
        )
        # Should not raise
        s._drift_probe_tick()

    def test_skips_malformed_rest_level(self):
        s = _make_scanner_for_drift_probe(
            ws_obs={"KXBTC15M-26APR241100-00": {"yes": [[95, 100]], "no": []}},
            rest_resp={"orderbook_fp": {
                "yes_dollars": [["0.95", "100"], "garbage", [50]],
                "no_dollars": [],
            }},
        )
        s._drift_probe_tick()


if __name__ == "__main__":
    unittest.main()
