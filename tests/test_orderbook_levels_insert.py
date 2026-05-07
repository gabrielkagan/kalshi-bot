"""Tests for orderbook_levels_json insertion via StateManager.insert_evaluated_opportunity.

Phase 3: thread the new column through the insert function — signature
accepts the kwarg, INSERT writes it, ON CONFLICT DO UPDATE persists it on
re-insert.

Schema parity test (test_insert_schema_parity) enforces that EVERY column
in the schema appears in the canonical INSERT — that test will be the
cross-check that this phase actually wired the column. We also add direct
behavior tests here for clarity.
"""

import inspect
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot


class _TempState(unittest.TestCase):
    def _fresh(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        return bot.StateManager(db_path=tmp.name)


class TestSignatureAcceptsKwarg(_TempState):
    def test_signature_has_orderbook_levels_json(self):
        params = inspect.signature(
            bot.StateManager.insert_evaluated_opportunity).parameters
        self.assertIn("orderbook_levels_json", params)
        # Must default to None — adding a non-default at the end would
        # break TypeError-free additive growth and is forbidden by the
        # "additive only" convention.
        self.assertIsNone(params["orderbook_levels_json"].default)


class TestInsertPersistsOrderbookLevelsJson(_TempState):
    SAMPLE = '{"yes_bids":[[87,3377]],"yes_asks":[[96,1]]}'

    def test_explicit_value_lands_in_db(self):
        sm = self._fresh()
        sm.insert_evaluated_opportunity(
            ticker="KXTEST-1", event_ticker="KXTEST", asset="BTC",
            filter_stage="candidate", product_type="15m",
            orderbook_levels_json=self.SAMPLE)
        row = sm.conn.execute(
            "SELECT orderbook_levels_json FROM evaluated_opportunities "
            "WHERE ticker='KXTEST-1'").fetchone()
        self.assertEqual(row["orderbook_levels_json"], self.SAMPLE)

    def test_default_is_null(self):
        sm = self._fresh()
        sm.insert_evaluated_opportunity(
            ticker="KXTEST-2", event_ticker="KXTEST", asset="BTC",
            filter_stage="candidate", product_type="15m")
        row = sm.conn.execute(
            "SELECT orderbook_levels_json FROM evaluated_opportunities "
            "WHERE ticker='KXTEST-2'").fetchone()
        self.assertIsNone(row["orderbook_levels_json"])


class TestOnConflictPersistsUpdate(_TempState):
    """Same (ticker, filter_stage, side) re-insert must overwrite the
    orderbook_levels_json column — not silently drop it on update path.
    Regression target: calibration_confidence had a similar Apr 22 bug
    where ON CONFLICT excluded the column."""

    def test_re_insert_overwrites_with_new_value(self):
        sm = self._fresh()
        sm.insert_evaluated_opportunity(
            ticker="KXTEST-3", event_ticker="KXTEST", asset="BTC",
            filter_stage="candidate", product_type="15m",
            orderbook_levels_json='{"yes_bids":[],"yes_asks":[]}')
        sm.insert_evaluated_opportunity(
            ticker="KXTEST-3", event_ticker="KXTEST", asset="BTC",
            filter_stage="candidate", product_type="15m",
            orderbook_levels_json='{"yes_bids":[[87,100]],"yes_asks":[[96,1]]}')
        rows = list(sm.conn.execute(
            "SELECT orderbook_levels_json FROM evaluated_opportunities "
            "WHERE ticker='KXTEST-3'"))
        self.assertEqual(len(rows), 1)  # still single row (ON CONFLICT)
        self.assertEqual(rows[0]["orderbook_levels_json"],
                         '{"yes_bids":[[87,100]],"yes_asks":[[96,1]]}')

    def test_re_insert_overwrites_with_none(self):
        # If the second call doesn't pass the kwarg (defaults to None),
        # the existing value should be REPLACED with NULL — that's how
        # other ON CONFLICT columns work, and is consistent with the
        # rest of the table. The alternative (preserve previous) would
        # be inconsistent and surprising.
        sm = self._fresh()
        sm.insert_evaluated_opportunity(
            ticker="KXTEST-4", event_ticker="KXTEST", asset="BTC",
            filter_stage="candidate", product_type="15m",
            orderbook_levels_json='{"yes_bids":[[87,100]],"yes_asks":[]}')
        sm.insert_evaluated_opportunity(
            ticker="KXTEST-4", event_ticker="KXTEST", asset="BTC",
            filter_stage="candidate", product_type="15m")
        row = sm.conn.execute(
            "SELECT orderbook_levels_json FROM evaluated_opportunities "
            "WHERE ticker='KXTEST-4'").fetchone()
        self.assertIsNone(row["orderbook_levels_json"])


class TestAutoFillFromScanCache(_TempState):
    """Same pattern as _scan_bid_cache / _scan_ms_cache: when the kwarg is
    None and a fresh per-ticker entry exists in _scan_ob_cache, insert
    auto-fills from cache. Cache stores (monotonic_ts, json) — see
    TestCacheFreshnessGate for the staleness behavior."""

    SAMPLE = '{"yes_bids":[[87,3377]],"yes_asks":[[96,1]]}'

    def test_autofill_when_kwarg_none(self):
        import time
        sm = self._fresh()
        sm._scan_ob_cache["KXTEST-A"] = (time.monotonic(), self.SAMPLE)
        sm.insert_evaluated_opportunity(
            ticker="KXTEST-A", event_ticker="KXTEST", asset="BTC",
            filter_stage="candidate", product_type="15m")
        row = sm.conn.execute(
            "SELECT orderbook_levels_json FROM evaluated_opportunities "
            "WHERE ticker='KXTEST-A'").fetchone()
        self.assertEqual(row["orderbook_levels_json"], self.SAMPLE)

    def test_explicit_kwarg_overrides_cache(self):
        import time
        sm = self._fresh()
        sm._scan_ob_cache["KXTEST-B"] = (time.monotonic(), self.SAMPLE)
        explicit = '{"yes_bids":[[88,1]],"yes_asks":[[95,1]]}'
        sm.insert_evaluated_opportunity(
            ticker="KXTEST-B", event_ticker="KXTEST", asset="BTC",
            filter_stage="candidate", product_type="15m",
            orderbook_levels_json=explicit)
        row = sm.conn.execute(
            "SELECT orderbook_levels_json FROM evaluated_opportunities "
            "WHERE ticker='KXTEST-B'").fetchone()
        self.assertEqual(row["orderbook_levels_json"], explicit)

    def test_no_cache_entry_writes_null(self):
        sm = self._fresh()
        # Cache empty — must NOT raise, must write NULL
        sm.insert_evaluated_opportunity(
            ticker="KXTEST-C", event_ticker="KXTEST", asset="BTC",
            filter_stage="candidate", product_type="15m")
        row = sm.conn.execute(
            "SELECT orderbook_levels_json FROM evaluated_opportunities "
            "WHERE ticker='KXTEST-C'").fetchone()
        self.assertIsNone(row["orderbook_levels_json"])


class TestCacheFreshnessGate(_TempState):
    """Stale cache entries must NOT auto-fill — logging a 15-minute-old
    book labeled as 'now' poisons forensic queries worse than NULL.
    Cache stores (monotonic_ts, json) tuples; reads check freshness."""

    SAMPLE = '{"yes_bids":[[87,3377]],"yes_asks":[[96,1]]}'

    def test_fresh_cache_entry_auto_fills(self):
        import time
        sm = self._fresh()
        sm._scan_ob_cache["KXTEST-F1"] = (time.monotonic(), self.SAMPLE)
        sm.insert_evaluated_opportunity(
            ticker="KXTEST-F1", event_ticker="KXTEST", asset="BTC",
            filter_stage="candidate", product_type="15m")
        row = sm.conn.execute(
            "SELECT orderbook_levels_json FROM evaluated_opportunities "
            "WHERE ticker='KXTEST-F1'").fetchone()
        self.assertEqual(row["orderbook_levels_json"], self.SAMPLE)

    def test_stale_cache_entry_returns_null(self):
        import time
        sm = self._fresh()
        # Synthesise an entry from "30 seconds ago" — past the freshness gate
        sm._scan_ob_cache["KXTEST-F2"] = (time.monotonic() - 30.0, self.SAMPLE)
        sm.insert_evaluated_opportunity(
            ticker="KXTEST-F2", event_ticker="KXTEST", asset="BTC",
            filter_stage="candidate", product_type="15m")
        row = sm.conn.execute(
            "SELECT orderbook_levels_json FROM evaluated_opportunities "
            "WHERE ticker='KXTEST-F2'").fetchone()
        self.assertIsNone(row["orderbook_levels_json"],
                          "stale cache entry must NOT auto-fill — that's forensic poisoning")

    def test_explicit_kwarg_overrides_even_stale_cache(self):
        import time
        sm = self._fresh()
        sm._scan_ob_cache["KXTEST-F3"] = (time.monotonic() - 999.0, "STALE")
        sm.insert_evaluated_opportunity(
            ticker="KXTEST-F3", event_ticker="KXTEST", asset="BTC",
            filter_stage="candidate", product_type="15m",
            orderbook_levels_json=self.SAMPLE)
        row = sm.conn.execute(
            "SELECT orderbook_levels_json FROM evaluated_opportunities "
            "WHERE ticker='KXTEST-F3'").fetchone()
        self.assertEqual(row["orderbook_levels_json"], self.SAMPLE)


class TestCacheEviction(_TempState):
    """Cache must NOT grow monotonically. Quiet tickers and dead markets
    leave entries that should age out. Same failure class as
    failure_15m_silence_apr24_second.md (WS cache phantom state)."""

    def test_stale_entries_evicted_on_new_writes(self):
        import time
        sm = self._fresh()
        # Seed many "old" entries to trigger the eviction threshold
        old_ts = time.monotonic() - 999.0
        for i in range(150):
            sm._scan_ob_cache[f"DEAD-{i}"] = (old_ts, '{"yes_bids":[],"yes_asks":[]}')
        # Trigger a write — should opportunistically evict stale entries
        sm._evict_stale_ob_cache()
        # All seeded entries are stale → must be gone
        live = [k for k in sm._scan_ob_cache if k.startswith("DEAD-")]
        self.assertEqual(live, [],
                         f"{len(live)} stale entries survived eviction")

    def test_fresh_entries_not_evicted(self):
        import time
        sm = self._fresh()
        sm._scan_ob_cache["FRESH-1"] = (time.monotonic(), '{"yes_bids":[],"yes_asks":[]}')
        sm._evict_stale_ob_cache()
        self.assertIn("FRESH-1", sm._scan_ob_cache)


class TestScannerWiringInPlace(_TempState):
    """Code-grep test: confirms scanner is wired to extract + cache + evict.
    Backstop against silent wiring removal (see kb/failures/
    feedback_verify_new_features.md — '0 rows after a qualifying event
    IS a bug'). True integration coverage comes from Phase 6 post-deploy
    verification (asserting orderbook_levels_json IS NOT NULL on new rows)."""

    def _scan_method_src(self):
        """Extract the source of OpportunityScanner.scan() — restricts the
        grep to the scanner's tick body, not random matches in fifteenm
        shadows or comments elsewhere in bot/_impl.py."""
        import bot._impl as bot_mod
        src = open(bot_mod.__file__).read()
        start = src.find("def scan(")
        self.assertGreater(start, 0, "OpportunityScanner.scan() not found")
        # Bound the search to ~5K lines (~250KB chars) — scan() is huge but finite
        return src[start:start + 250_000]

    def test_scanner_calls_extract_book_levels(self):
        scan_src = self._scan_method_src()
        self.assertIn("_extract_book_levels(ob_data)", scan_src,
                      "scanner doesn't call _extract_book_levels(ob_data)")

    def test_scanner_writes_tuple_to_ob_cache(self):
        scan_src = self._scan_method_src()
        # Must write a tuple (ts, json), not the raw json string.
        # If someone reverts to `cache[ticker] = json` the freshness gate
        # silently fails (entry shape mismatch → exception swallowed →
        # cache entry never useful → all rows write NULL).
        self.assertIn("_scan_ob_cache[ticker]", scan_src,
                      "scanner doesn't write to _scan_ob_cache")
        # The write site must include time.monotonic() — the tuple form
        self.assertIn("time.monotonic()", scan_src,
                      "scanner doesn't capture timestamp for cache entry")

    def test_scanner_calls_evict_stale_ob_cache(self):
        """Eviction must be wired so quiet-market tickers don't accumulate.
        Adversary round 2 P1: prior version had eviction inside `if _ob_levels
        is not None` — quiet-market scenario meant eviction never fired."""
        scan_src = self._scan_method_src()
        self.assertIn("_evict_stale_ob_cache()", scan_src,
                      "scanner doesn't call _evict_stale_ob_cache — "
                      "quiet-market tickers will leak indefinitely")

    def test_eviction_not_gated_by_ob_data_present(self):
        """Adversary round 2: eviction must run unconditionally each tick,
        not gated by 'if _ob_levels is not None'. Without this guard fix,
        a global ob_data outage means eviction never runs and stale entries
        accumulate across the entire cache."""
        import bot._impl as bot_mod
        src = open(bot_mod.__file__).read()
        # Find the scanner's eviction call
        idx_evict = src.find("self._state._evict_stale_ob_cache()")
        self.assertGreater(idx_evict, 0, "scanner eviction call not found")
        # Walk back ~20 lines and look for the most recent indent-decreasing
        # 'if _ob_levels is not None:' guard. The eviction line should NOT be
        # nested inside that block.
        preceding = src[max(0, idx_evict - 1500):idx_evict]
        last_guard = preceding.rfind("if _ob_levels is not None:")
        if last_guard >= 0:
            # Check whether the eviction is at the same indent or shallower
            # by examining the line containing the eviction call.
            line_start = src.rfind("\n", 0, idx_evict) + 1
            evict_indent = idx_evict - line_start
            # Find the guard line's indent
            guard_line_start = src.rfind(
                "\n", 0, max(0, idx_evict - 1500) + last_guard) + 1
            guard_indent = (max(0, idx_evict - 1500) + last_guard) - guard_line_start
            self.assertLessEqual(
                evict_indent, guard_indent,
                "_evict_stale_ob_cache is nested inside `if _ob_levels is not None` — "
                "quiet-market tickers will not be evicted (adversary round 2 P1)")


class TestExistingCallerSafety(_TempState):
    """All 109 existing call sites pass the function their current kwargs
    and DON'T know about orderbook_levels_json. Verify that calling the
    function without the new kwarg still works (no TypeError, no missing
    positional arg) and writes NULL."""

    def test_minimum_args_still_works(self):
        sm = self._fresh()
        # Mimic the smallest legal call from a rejection path
        sm.insert_evaluated_opportunity(
            ticker="KXTEST-5", event_ticker="KXTEST", asset="BTC",
            filter_stage="edge_too_low",
            rejection_reason="test")
        row = sm.conn.execute(
            "SELECT orderbook_levels_json, filter_stage FROM evaluated_opportunities "
            "WHERE ticker='KXTEST-5'").fetchone()
        self.assertIsNone(row["orderbook_levels_json"])
        self.assertEqual(row["filter_stage"], "edge_too_low")


if __name__ == "__main__":
    unittest.main()
