"""Phase 5: orderbook_levels_json on position_price_observations.

Two INSERT sites in bot/_impl.py (15M monitor + weather monitor) both need to
write the cached ladder. Logic is identical: pull from _scan_ob_cache
with the same 10s freshness gate used by eval_opportunities and
order_lifecycle_snapshots.

Refactored into StateManager._get_fresh_ob_ladder(ticker) helper to
share between three call sites without duplicating the freshness math.
"""

import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot


class _TempState(unittest.TestCase):
    def _fresh(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        return bot.StateManager(db_path=tmp.name)


class TestGetFreshOBLadderHelper(_TempState):
    """Unified helper used by three call sites (eval_opportunities,
    order_lifecycle_snapshots, position_price_observations). Same
    freshness gate, same staleness behavior, one definition."""

    SAMPLE = '{"yes_bids":[[87,3377]],"yes_asks":[[96,1]]}'

    def test_returns_json_when_cache_fresh(self):
        sm = self._fresh()
        sm._scan_ob_cache["KX-1"] = (time.monotonic(), self.SAMPLE)
        self.assertEqual(sm._get_fresh_ob_ladder("KX-1"), self.SAMPLE)

    def test_returns_none_when_cache_stale(self):
        sm = self._fresh()
        sm._scan_ob_cache["KX-2"] = (time.monotonic() - 999.0, self.SAMPLE)
        self.assertIsNone(sm._get_fresh_ob_ladder("KX-2"))

    def test_returns_none_when_no_cache_entry(self):
        sm = self._fresh()
        self.assertIsNone(sm._get_fresh_ob_ladder("KX-MISSING"))


class TestPPOInsertWiringInPlace(_TempState):
    """Code-grep wiring backstop. Both PPO INSERT statements (the 15M
    monitor and the weather monitor) must now write to the new column.
    Phase 6 post-deploy verification confirms rows actually populate."""

    def _bot_src(self):
        import bot._impl as bot_mod
        return open(bot_mod.__file__).read()

    def test_ppo_insert_lists_orderbook_levels_json(self):
        """Both INSERT statements (15M + weather) must list the column.
        Same parity rule as evaluated_opportunities — schema column
        without INSERT coverage = silent NULL forever."""
        src = self._bot_src()
        # Find every "INSERT INTO position_price_observations" and
        # verify the column appears in the column list of each.
        idx = 0
        n_found = 0
        while True:
            idx = src.find("INSERT INTO position_price_observations", idx)
            if idx < 0:
                break
            # Find the closing ")" of the column list (start of VALUES)
            values_idx = src.find("VALUES", idx)
            self.assertGreater(values_idx, idx,
                               "INSERT site missing VALUES clause")
            insert_stmt = src[idx:values_idx]
            self.assertIn("orderbook_levels_json", insert_stmt,
                          f"INSERT INTO position_price_observations at offset {idx} "
                          f"missing orderbook_levels_json column")
            n_found += 1
            idx = values_idx
        # Sanity: at least 2 INSERT sites exist (15M + weather monitors)
        self.assertGreaterEqual(n_found, 2,
                                f"expected ≥2 PPO INSERT sites, found {n_found}")

    def test_ppo_insert_populates_from_a_real_ladder_source(self):
        """Both INSERT sites must populate orderbook_levels_json from
        SOMETHING — either the cache helper (_get_fresh_ob_ladder) for
        the 15M monitor or a direct REST fetch (get_orderbook + extract)
        for the weather monitor (whose 900s cycle outruns the 10s cache
        freshness window). Without one of these, the column is silently
        NULL forever — exact failure mode of feedback_verify_new_features."""
        src = self._bot_src()
        idx = 0
        n_with_source = 0
        while True:
            idx = src.find("INSERT INTO position_price_observations", idx)
            if idx < 0:
                break
            preceding = src[max(0, idx - 2000):idx]
            has_cache = "_get_fresh_ob_ladder" in preceding
            has_rest = ("get_orderbook" in preceding
                        and "_extract_book_levels" in preceding)
            if has_cache or has_rest:
                n_with_source += 1
            idx += 1
        self.assertGreaterEqual(
            n_with_source, 2,
            f"only {n_with_source} PPO INSERT sites populate "
            "orderbook_levels_json from cache or REST fetch")


class TestPPOInsertSchemaWriteRoundTrip(_TempState):
    """Smoke test: a direct PPO insert with the new column persists
    through the schema and reads back. Exercises the migrated table."""

    SAMPLE = '{"yes_bids":[[87,1]],"yes_asks":[[96,1]]}'

    def test_direct_insert_with_orderbook_levels_json_persists(self):
        sm = self._fresh()
        sm.conn.execute(
            "INSERT INTO position_price_observations "
            "(ticker, asset, observation_time, "
            "entry_price_cents, position_count, source, orderbook_levels_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("KX-RT", "BTC", "2026-04-25T00:00:00Z", 96, 1, "test", self.SAMPLE))
        sm.conn.commit()
        row = sm.conn.execute(
            "SELECT orderbook_levels_json FROM position_price_observations "
            "WHERE ticker='KX-RT'").fetchone()
        self.assertEqual(row["orderbook_levels_json"], self.SAMPLE)


if __name__ == "__main__":
    unittest.main()
