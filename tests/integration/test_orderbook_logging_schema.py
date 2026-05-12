"""Schema migration tests for per-level orderbook logging (Phase 2).

Adds:
- evaluated_opportunities.orderbook_levels_json TEXT
- position_price_observations.orderbook_levels_json TEXT
- order_lifecycle_snapshots (new table) — keyed by order_id + event_type

Critical invariants enforced:
- Migrations are idempotent (re-instantiating StateManager doesn't error
  or lose existing rows on second init)
- New columns default to NULL on legacy rows (no data backfill side-effect)
- New table inherits WAL + busy_timeout from parent connection (no
  separate sqlite3.connect that would need its own pragmas — see CLAUDE.md
  rule about analyst.py / sports_engine.py contention bugs)
- Index exists on order_lifecycle_snapshots for query patterns we'll use
  (lookup by ticker for forensic dive, by order_id for trade-lifecycle replay)
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import bot


class _TempDB(unittest.TestCase):
    def _fresh_path(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        return tmp.name

    def _columns(self, sm, table):
        return {row["name"]: row["type"]
                for row in sm.conn.execute(f"PRAGMA table_info({table})")}


class TestEvaluatedOpportunitiesNewColumn(_TempDB):
    def test_orderbook_levels_json_column_exists(self):
        sm = bot.state.StateManager(db_path=self._fresh_path())
        cols = self._columns(sm, "evaluated_opportunities")
        self.assertIn("orderbook_levels_json", cols)
        self.assertEqual(cols["orderbook_levels_json"].upper(), "TEXT")


class TestPositionPriceObservationsNewColumn(_TempDB):
    def test_orderbook_levels_json_column_exists(self):
        sm = bot.state.StateManager(db_path=self._fresh_path())
        cols = self._columns(sm, "position_price_observations")
        self.assertIn("orderbook_levels_json", cols)
        self.assertEqual(cols["orderbook_levels_json"].upper(), "TEXT")


class TestOrderLifecycleSnapshotsTable(_TempDB):
    def test_table_exists_with_required_columns(self):
        sm = bot.state.StateManager(db_path=self._fresh_path())
        cols = self._columns(sm, "order_lifecycle_snapshots")
        # Required columns per design
        for required in ("id", "order_id", "ticker", "event_type",
                         "observation_time", "orderbook_levels_json"):
            self.assertIn(required, cols, f"missing column: {required}")
        # id is PK
        pk_rows = list(sm.conn.execute(
            "PRAGMA table_info(order_lifecycle_snapshots)"))
        id_row = next(r for r in pk_rows if r["name"] == "id")
        self.assertEqual(id_row["pk"], 1)

    def test_indexes_exist(self):
        sm = bot.state.StateManager(db_path=self._fresh_path())
        idx = {row["name"] for row in sm.conn.execute(
            "PRAGMA index_list(order_lifecycle_snapshots)")}
        # Need lookups by order_id (lifecycle replay) and ticker (forensic)
        self.assertTrue(any("order_id" in n for n in idx),
                        f"no order_id index found: {idx}")
        self.assertTrue(any("ticker" in n for n in idx),
                        f"no ticker index found: {idx}")

    def test_required_columns_not_null(self):
        sm = bot.state.StateManager(db_path=self._fresh_path())
        cols = list(sm.conn.execute(
            "PRAGMA table_info(order_lifecycle_snapshots)"))
        notnull = {r["name"]: r["notnull"] for r in cols}
        for required in ("ticker", "event_type", "observation_time"):
            self.assertEqual(notnull[required], 1,
                             f"{required} should be NOT NULL")


class TestMigrationIdempotency(_TempDB):
    """Critical: instantiating StateManager twice on same DB must not
    error and must not destroy data. Bot restarts hit this every time."""

    def test_re_init_does_not_error(self):
        path = self._fresh_path()
        bot.state.StateManager(db_path=path)
        # Second init must succeed (ADD COLUMN should fail silently)
        try:
            bot.state.StateManager(db_path=path)
        except Exception as e:
            self.fail(f"Second StateManager init raised: {e}")

    def test_existing_eval_opp_rows_survive_reinit(self):
        path = self._fresh_path()
        sm1 = bot.state.StateManager(db_path=path)
        sm1.insert_evaluated_opportunity(
            ticker="KXTEST-1", event_ticker="KXTEST", asset="BTC",
            filter_stage="candidate", product_type="15m")
        sm1.conn.close()
        sm2 = bot.state.StateManager(db_path=path)
        rows = list(sm2.conn.execute(
            "SELECT ticker, orderbook_levels_json FROM evaluated_opportunities "
            "WHERE ticker='KXTEST-1'"))
        self.assertEqual(len(rows), 1)
        # Legacy row gets NULL for the new column
        self.assertIsNone(rows[0]["orderbook_levels_json"])

    def test_existing_position_obs_rows_survive_reinit(self):
        path = self._fresh_path()
        sm1 = bot.state.StateManager(db_path=path)
        sm1.conn.execute(
            "INSERT INTO position_price_observations "
            "(ticker, asset, observation_time, entry_price_cents, position_count, source) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("KXTEST-1", "BTC", "2026-04-25T00:00:00Z", 96, 1, "test"))
        sm1.conn.commit()
        sm1.conn.close()
        sm2 = bot.state.StateManager(db_path=path)
        rows = list(sm2.conn.execute(
            "SELECT ticker, orderbook_levels_json FROM position_price_observations "
            "WHERE ticker='KXTEST-1'"))
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["orderbook_levels_json"])


class TestOrderLifecycleSnapshotsBasicInsert(_TempDB):
    """Smoke test: can we actually write to the new table via the parent
    StateManager connection (which has WAL + busy_timeout)?"""

    def test_basic_insert_and_read(self):
        sm = bot.state.StateManager(db_path=self._fresh_path())
        sm.conn.execute(
            "INSERT INTO order_lifecycle_snapshots "
            "(order_id, ticker, event_type, observation_time, orderbook_levels_json) "
            "VALUES (?, ?, ?, ?, ?)",
            ("ord-1", "KXTEST-1", "submit", "2026-04-25T00:00:00Z",
             '{"yes_bids":[[87,100]],"yes_asks":[[96,1]]}'))
        sm.conn.commit()
        rows = list(sm.conn.execute(
            "SELECT * FROM order_lifecycle_snapshots WHERE order_id='ord-1'"))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["event_type"], "submit")
        self.assertEqual(rows[0]["ticker"], "KXTEST-1")


class TestEventTypeCheckConstraint(_TempDB):
    """event_type must be enum-constrained (CHECK) so typo writes
    ('FILL', 'submitted', 'fil') fail loudly instead of silently
    polluting forensic GROUP BY queries."""

    ALLOWED = ("submit", "fill", "partial_fill", "cancel")

    def test_allowed_event_types_accepted(self):
        sm = bot.state.StateManager(db_path=self._fresh_path())
        for et in self.ALLOWED:
            sm.conn.execute(
                "INSERT INTO order_lifecycle_snapshots "
                "(order_id, ticker, event_type, observation_time) VALUES (?, ?, ?, ?)",
                (f"ord-{et}", "KXTEST-1", et, "2026-04-25T00:00:00Z"))
        sm.conn.commit()  # if any CHECK rejected, IntegrityError raised on commit

    def test_invalid_event_type_rejected(self):
        import sqlite3
        sm = bot.state.StateManager(db_path=self._fresh_path())
        with self.assertRaises(sqlite3.IntegrityError):
            sm.conn.execute(
                "INSERT INTO order_lifecycle_snapshots "
                "(order_id, ticker, event_type, observation_time) VALUES (?, ?, ?, ?)",
                ("ord-bad", "KXTEST-1", "FILLED", "2026-04-25T00:00:00Z"))


class TestOrderIdNotNull(_TempDB):
    """Table is named order_lifecycle_snapshots — every row must have
    an order_id. Generic snapshots without an order belong elsewhere
    (position_price_observations for held-position monitoring)."""

    def test_order_id_required(self):
        import sqlite3
        sm = bot.state.StateManager(db_path=self._fresh_path())
        with self.assertRaises(sqlite3.IntegrityError):
            sm.conn.execute(
                "INSERT INTO order_lifecycle_snapshots "
                "(ticker, event_type, observation_time) VALUES (?, ?, ?)",
                ("KXTEST-1", "submit", "2026-04-25T00:00:00Z"))


class TestPartialMigrationRecovery(_TempDB):
    """If a prior init created the table but failed before creating
    indexes (process crash, lock timeout), re-init must create the
    missing indexes without erroring."""

    def test_missing_index_recreated_on_reinit(self):
        path = self._fresh_path()
        sm1 = bot.state.StateManager(db_path=path)
        # Simulate partial-migration state: drop both indexes
        sm1.conn.execute("DROP INDEX IF EXISTS idx_ols_order_id")
        sm1.conn.execute("DROP INDEX IF EXISTS idx_ols_ticker_time")
        sm1.conn.commit()
        sm1.conn.close()
        # Re-init must restore them
        sm2 = bot.state.StateManager(db_path=path)
        idx = {row["name"] for row in sm2.conn.execute(
            "PRAGMA index_list(order_lifecycle_snapshots)")}
        self.assertTrue(any("order_id" in n for n in idx),
                        f"order_id index not restored: {idx}")
        self.assertTrue(any("ticker" in n for n in idx),
                        f"ticker index not restored: {idx}")


class TestTableInsideCreateTables(_TempDB):
    """Sanity: the new table must be created inside StateManager._create_tables
    so it inherits the parent connection (WAL + busy_timeout=30000 set in
    __init__). Any new sqlite3.connect() touching this table MUST set its
    own PRAGMAs per CLAUDE.md (analyst.py / sports_engine.py contention bugs)."""

    def test_create_table_lives_in_create_tables(self):
        # Bit 7.1 retarget (2026-05-10): StateManager._create_tables moved to
        # bot/state.py.
        import bot.state as state_mod
        src = open(state_mod.__file__).read()
        idx_create_tables = src.find("def _create_tables")
        idx_create_olc = src.find(
            "CREATE TABLE IF NOT EXISTS order_lifecycle_snapshots", idx_create_tables)
        self.assertGreater(idx_create_olc, idx_create_tables,
                           "CREATE TABLE order_lifecycle_snapshots must be inside _create_tables")
        # Cheap heuristic: no sqlite3.connect on the same line as a write
        # to this table. Phase 4 wiring will need to share self.conn.
        for line_no, line in enumerate(src.splitlines(), 1):
            if "order_lifecycle_snapshots" in line and "sqlite3.connect" in line:
                self.fail(f"bot/state.py:{line_no} opens a separate sqlite3 connection "
                          f"to order_lifecycle_snapshots — must use self.conn")


if __name__ == "__main__":
    unittest.main()
