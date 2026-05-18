"""BNB T1.5 followup (ticket 86b9zn5pq): bnb_spot_at_decision column.

BNB T1 (2026-05-17) added BNB to ASSETS as a shadow observation asset. The
scanner producer at `bot/scanner/__init__.py:1011`
(`_compute_cross_asset_spot_snapshot`) is ASSETS-driven and ALREADY emits
the `bnb_spot_at_decision` key today (verified by
tests/integration/test_shadow_coverage_phase_f.py:76) — but the consumer
chain in `bot/state.py` + `bot/snapshots/supabase_sync.py` + the G-2
backfill harness in `scripts/backfill/shadow_coverage_backfill.py` still
only knows about btc/eth/sol/xrp/hype/doge. The producer's bnb key is
silently dropped on the floor today (since T1 BNB ship 2026-05-17 ~21:21
UTC) at the consumer block in bot/state.py around line 2244-2247. Result:
BNB shadow rows accumulate WITHOUT a bnb_spot_at_decision value, AND
existing BTC/ETH/SOL/XRP/HYPE/DOGE rows accumulate WITHOUT a BNB column.
Both block T3 calibration training on the BNB spot feature.

This Bit expands the consumer chain to 7 columns and adds supabase
migration 021. The pattern is the verbatim mechanical repeat of the
HYPE/DOGE Bit 2 precedent (ede10ba, 2026-05-11, ticket 86b9vrjf2,
migration 019, kb/decisions/asset-onboarding-doge-hype-bit-2-shipped-may10.md).

Failure modes guarded:
  - signature-only without DDL → KeyError on insert
  - DDL-only without signature → kwarg rejected (sig contract test)
  - supabase whitelist without remote migration → silent HTTP-400 wedge
    (_check_schema_parity 2026-04-04 postmortem at supabase_sync.py:495)
  - DDL + signature but consumer in `_extended_feature_provider` still
    only reads 6 keys → producer's bnb silently dropped (the bug class
    this Bit closes)
  - Backfill harness UPDATEs 6 columns → bnb stays NULL forever on
    rows pre-deploy (regenerable only from Coinbase historical candles
    within minute-grain tolerance)

ClickUp: 86b9zn5pq
Pickup doc: kb/decisions/bnb-t1-5-followups-pickup-prompt-may17.md
"""

import inspect
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts", "backfill"))


# BNB T1.5 followup — the NEW column added on top of the 6 already shipped
# by Phase B (4 — btc/eth/sol/xrp, migration 011) + Bit 2 (2 — hype/doge,
# migration 019). Ships in supabase migration 021 + state.py DDL ALTER TABLE.
BNB_NEW_COLUMNS = [
    ("bnb_spot_at_decision", "REAL"),
]


class TestBnbSpotAtDecisionSchema:
    """BNB followup schema addition: bnb_spot_at_decision on
    evaluated_opportunities, REAL-typed, nullable."""

    def test_bnb_column_present_in_schema(self):
        import bot
        import bot.state  # noqa: F401
        sm = bot.state.StateManager(":memory:")
        cols = {
            r["name"] for r in
            sm.conn.execute("PRAGMA table_info(evaluated_opportunities)").fetchall()
        }
        missing = [name for (name, _) in BNB_NEW_COLUMNS if name not in cols]
        assert not missing, (
            f"BNB column missing from evaluated_opportunities schema: "
            f"{missing}. Add via ALTER TABLE ADD COLUMN in StateManager."
            f"_create_tables migration loop (paired with Bit 2 precedent)."
        )

    def test_bnb_column_is_real_typed(self):
        """REAL-typed, mirroring btc/eth/sol/xrp/hype/doge_spot_at_decision.
        Supabase migration 021 uses `double precision` for identical reasons
        (high-precision spot prices; see migration 019 header)."""
        import bot
        import bot.state  # noqa: F401
        sm = bot.state.StateManager(":memory:")
        col_types = {
            r["name"]: r["type"].upper() for r in
            sm.conn.execute("PRAGMA table_info(evaluated_opportunities)").fetchall()
        }
        mistyped = []
        for (name, sql_type) in BNB_NEW_COLUMNS:
            assert name in col_types, f"{name} missing from schema"
            if col_types[name] != sql_type.upper():
                mistyped.append(f"{name}: expected {sql_type}, got {col_types[name]}")
        assert not mistyped, "Type drift on BNB column:\n" + "\n".join(mistyped)


class TestBnbSpotAtDecisionSignature:
    """insert_evaluated_opportunity accepts bnb_spot_at_decision as kwarg
    (Optional[float] = None, matching btc/eth/sol/xrp/hype/doge precedent)."""

    def test_signature_accepts_bnb_kwarg(self):
        from bot.state import StateManager
        sig = inspect.signature(StateManager.insert_evaluated_opportunity)
        params = set(sig.parameters.keys())
        missing = [name for (name, _) in BNB_NEW_COLUMNS if name not in params]
        assert not missing, (
            f"insert_evaluated_opportunity missing BNB kwarg: {missing}. "
            f"Add to function signature + INSERT column list + VALUES "
            f"placeholders + ON CONFLICT DO UPDATE SET + parameter tuple."
        )


class TestBnbSpotAtDecisionRoundtrip:
    """Real DB roundtrip: insert with values, read back, column survives."""

    def test_insert_round_trips_bnb_column(self):
        import bot
        import bot.state  # noqa: F401
        sm = bot.state.StateManager(":memory:")
        kwargs = {
            "ticker": "KXBNB15M-26MAY171900-T655",
            "event_ticker": "KXBNB15M-26MAY171900",
            "asset": "BNB", "filter_stage": "bnb_shadow",
            "bnb_spot_at_decision": 655.42,
        }
        sm.insert_evaluated_opportunity(**kwargs)
        row = sm.conn.execute(
            "SELECT bnb_spot_at_decision "
            "FROM evaluated_opportunities WHERE ticker = ?",
            (kwargs["ticker"],)
        ).fetchone()
        assert row is not None, "row not inserted"
        assert row["bnb_spot_at_decision"] == pytest.approx(655.42), (
            f"bnb_spot_at_decision: got {row['bnb_spot_at_decision']!r}, "
            f"expected 655.42"
        )


class TestBnbSpotAtDecisionProviderRoundtrip:
    """Closes the silent-drop bug class: when _extended_feature_provider
    returns 7 spot keys (T1 BNB producer at bot/scanner/__init__.py:1011
    is ASSETS-driven and already does this), the insert path must consume
    them — bnb is being silently dropped today because the consumer block
    at bot/state.py around line 2244-2247 only reads 6 keys post-Bit-2."""

    def test_provider_seven_keys_populates_all_seven_columns(self):
        import bot
        import bot.state  # noqa: F401
        sm = bot.state.StateManager(":memory:")

        # Stub _extended_feature_provider returning all seven spot keys —
        # mirrors the ASSETS-driven scanner output post-BNB-T1.
        def _provider(ticker, asset, spot_price, threshold, product_type):
            return {
                "btc_spot_at_decision": 67432.5,
                "eth_spot_at_decision": 3210.5,
                "sol_spot_at_decision": 142.7,
                "xrp_spot_at_decision": 2.51,
                "hype_spot_at_decision": 24.512,
                "doge_spot_at_decision": 0.1837,
                "bnb_spot_at_decision": 655.42,
            }
        sm._extended_feature_provider = _provider

        sm.insert_evaluated_opportunity(
            ticker="KXBTC15M-26MAY171900-T67400",
            event_ticker="KXBTC15M-26MAY171900",
            asset="BTC", filter_stage="candidate",
        )
        row = sm.conn.execute(
            "SELECT btc_spot_at_decision, eth_spot_at_decision, "
            "sol_spot_at_decision, xrp_spot_at_decision, "
            "hype_spot_at_decision, doge_spot_at_decision, "
            "bnb_spot_at_decision "
            "FROM evaluated_opportunities WHERE ticker = 'KXBTC15M-26MAY171900-T67400'"
        ).fetchone()
        assert row is not None
        # All seven columns populated — closes the silent-drop bug class.
        assert row["btc_spot_at_decision"] == pytest.approx(67432.5)
        assert row["eth_spot_at_decision"] == pytest.approx(3210.5)
        assert row["sol_spot_at_decision"] == pytest.approx(142.7)
        assert row["xrp_spot_at_decision"] == pytest.approx(2.51)
        assert row["hype_spot_at_decision"] == pytest.approx(24.512)
        assert row["doge_spot_at_decision"] == pytest.approx(0.1837)
        assert row["bnb_spot_at_decision"] == pytest.approx(655.42), (
            "bnb key from provider was silently dropped (the bug this Bit closes)"
        )


class TestBnbSpotAtDecisionSupabaseWhitelist:
    """Supabase _EVAL_COLUMNS whitelist must include bnb column name or
    the mirror skips it. CAVEAT: only add to whitelist AFTER migration 021
    is applied to remote — otherwise silent HTTP-400 wedge per
    _check_schema_parity 2026-04-04 postmortem."""

    def test_eval_columns_whitelist_includes_bnb(self):
        from bot.snapshots.supabase_sync import SupabaseSyncer
        whitelist = {c.strip() for c in SupabaseSyncer._EVAL_COLUMNS.split(",")}
        missing = [name for (name, _) in BNB_NEW_COLUMNS if name not in whitelist]
        assert not missing, (
            f"supabase_sync._EVAL_COLUMNS missing BNB column: {missing}. "
            f"Add to the comma-separated string AFTER landing supabase migration "
            f"021 that adds this column to the remote `evaluations` table."
        )


class TestBnbSpotAtDecisionMigration021Sql:
    """A migration file `scripts/ops/supabase_migration_021_*.sql` must add
    the new spot column to public.evaluations with idempotent
    ADD COLUMN IF NOT EXISTS, and end with NOTIFY pgrst, 'reload schema'."""

    def _migration_path(self):
        scripts_dir = os.path.join(PROJECT_ROOT, "scripts", "ops")
        candidates = [
            f for f in os.listdir(scripts_dir)
            if f.startswith("supabase_migration_021_") and f.endswith(".sql")
            # Skip iCloud-dup `* 2.sql`/`* 3.sql` suffixes (Mac sync noise).
            and " " not in f
        ]
        assert candidates, (
            "No scripts/ops/supabase_migration_021_*.sql found. This Bit "
            "requires a supabase migration that adds bnb_spot_at_decision "
            "to the remote evaluations table."
        )
        assert len(candidates) == 1, f"Multiple migration_021 files: {candidates}"
        return os.path.join(scripts_dir, candidates[0])

    def test_migration_sql_adds_bnb_column(self):
        path = self._migration_path()
        sql = open(path).read().lower()
        missing = [
            name for (name, _) in BNB_NEW_COLUMNS
            if f"add column if not exists {name.lower()}" not in sql
        ]
        assert not missing, (
            f"Migration 021 missing ADD COLUMN IF NOT EXISTS for: {missing}"
        )

    def test_migration_sql_notifies_pgrst(self):
        path = self._migration_path()
        sql = open(path).read().lower()
        assert "notify pgrst" in sql, (
            "Migration 021 must NOTIFY pgrst, 'reload schema' so PostgREST "
            "picks up the new column (per migration 010+011+019 convention)."
        )

    def test_migration_sql_targets_evaluations_table(self):
        path = self._migration_path()
        sql = open(path).read().lower()
        assert "alter table public.evaluations" in sql or "alter table evaluations" in sql, (
            "Migration 021 must target the evaluations table."
        )


class TestBnbSpotAtDecisionBackfill:
    """Phase G-2 backfill harness must UPDATE bnb_spot_at_decision column.
    Pre-fix the UPDATE SQL listed only 6 columns; BNB rows accumulated since
    T1 ship (2026-05-17 ~21:21 UTC) without backfill coverage."""

    def _make_db_with_eval_schema(self, tmp_path):
        import bot
        import bot.state  # noqa: F401
        db_path = tmp_path / "state.db"
        sm = bot.state.StateManager(str(db_path))
        return sm

    def test_backfill_picks_up_row_with_other_spots_populated_but_bnb_null(self, tmp_path):
        """Rows accumulated since T1 BNB ship (2026-05-17 ~21:21 UTC) have
        btc/eth/sol/xrp/hype/doge_spot_at_decision populated but
        bnb_spot_at_decision NULL because the consumer at
        bot/state.py:2244-2247 silently dropped the bnb key from the
        producer. This Bit widens the backfill WHERE predicate so these
        rows ARE picked up. Without the widening, the post-ship backfill
        runbook would skip exactly the cohort the Bit is designed to fix
        (mechanically identical to Bit 2 R1 M1)."""
        from shadow_coverage_backfill import backfill_xasset_spots
        sm = self._make_db_with_eval_schema(tmp_path)
        # Row that has 6 spots populated (pre-fix live capture) but
        # bnb NULL (silent-drop bug).
        sm.conn.execute(
            "INSERT INTO evaluated_opportunities(ticker, event_ticker, asset, "
            "filter_stage, evaluation_time, product_type, status, "
            "btc_spot_at_decision, eth_spot_at_decision, "
            "sol_spot_at_decision, xrp_spot_at_decision, "
            "hype_spot_at_decision, doge_spot_at_decision) "
            "VALUES ('PARTIALBNB', 'E', 'BTC', 'candidate', "
            "'2026-05-17T23:59:00Z', '15m', 'pending', "
            "55555.0, 5555.0, 555.0, 5.55, 24.5, 0.18)"
        )
        sm.conn.commit()

        import datetime as _dt
        epoch_sec = int(_dt.datetime(2026, 5, 17, 23, 59, 0,
                                       tzinfo=_dt.timezone.utc).timestamp())

        def _mock_fetch(asset, start_iso, end_iso):
            base = {"BTC": 67432.5, "ETH": 3210.5,
                    "SOL": 142.7, "XRP": 2.51,
                    "HYPE": 24.512, "DOGE": 0.1837,
                    "BNB": 655.42}[asset]
            return [[epoch_sec, base, base, base, base, 1.0]]

        n = backfill_xasset_spots(sm.conn, fetcher=_mock_fetch, batch_size=10)
        # The partial row must be picked up by the widened predicate.
        assert n == 1, (
            "Row with 6 spots populated but bnb NULL must be picked up by "
            "widened WHERE clause. n=0 means the predicate still only "
            "checks the 6 pre-BNB columns — regression."
        )
        row = sm.conn.execute(
            "SELECT bnb_spot_at_decision "
            "FROM evaluated_opportunities WHERE ticker='PARTIALBNB'"
        ).fetchone()
        # Critical: the missing column IS filled by the backfill.
        assert row["bnb_spot_at_decision"] == pytest.approx(655.42)

    def test_backfill_writes_bnb_spot(self, tmp_path):
        from shadow_coverage_backfill import backfill_xasset_spots
        sm = self._make_db_with_eval_schema(tmp_path)
        sm.conn.execute(
            "INSERT INTO evaluated_opportunities(ticker, event_ticker, asset, "
            "filter_stage, evaluation_time, product_type, status) "
            "VALUES ('TESTBNB', 'E', 'BNB', 'bnb_shadow', "
            "'2026-05-17T23:59:00Z', '15m', 'pending')"
        )
        sm.conn.commit()

        import datetime as _dt
        epoch_sec = int(_dt.datetime(2026, 5, 17, 23, 59, 0,
                                       tzinfo=_dt.timezone.utc).timestamp())

        def _mock_fetch(asset, start_iso, end_iso):
            base = {"BTC": 67432.5, "ETH": 3210.5,
                    "SOL": 142.7, "XRP": 2.51,
                    "HYPE": 24.512, "DOGE": 0.1837,
                    "BNB": 655.42}[asset]
            return [[epoch_sec, base, base, base, base, 1.0]]

        n = backfill_xasset_spots(sm.conn, fetcher=_mock_fetch, batch_size=10)
        assert n == 1
        row = sm.conn.execute(
            "SELECT btc_spot_at_decision, eth_spot_at_decision, "
            "sol_spot_at_decision, xrp_spot_at_decision, "
            "hype_spot_at_decision, doge_spot_at_decision, "
            "bnb_spot_at_decision "
            "FROM evaluated_opportunities WHERE ticker = 'TESTBNB'"
        ).fetchone()
        # Closes the bug class: pre-fix the UPDATE SQL only listed 6
        # columns, so bnb stayed NULL forever on historical rows.
        assert row["btc_spot_at_decision"] == pytest.approx(67432.5)
        assert row["eth_spot_at_decision"] == pytest.approx(3210.5)
        assert row["sol_spot_at_decision"] == pytest.approx(142.7)
        assert row["xrp_spot_at_decision"] == pytest.approx(2.51)
        assert row["hype_spot_at_decision"] == pytest.approx(24.512)
        assert row["doge_spot_at_decision"] == pytest.approx(0.1837)
        assert row["bnb_spot_at_decision"] == pytest.approx(655.42), (
            "BNB followup: backfill UPDATE must extend to bnb column"
        )
