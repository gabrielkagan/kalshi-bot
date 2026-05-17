"""Bit 2 (T1 cross-asset expansion): hype/doge_spot_at_decision columns.

T1 (5dca85a, 2026-05-10) added HYPE/DOGE to shadow observation. The scanner
producer at `bot/scanner/__init__.py:990` (`_compute_cross_asset_spot_snapshot`)
became ASSETS-driven in T1 and ALREADY emits all six keys (incl. hype/doge).
But the consumer chain in `bot/state.py` + `supabase_sync.py` + the G-2
backfill harness in `scripts/backfill/shadow_coverage_backfill.py` still only knows
about btc/eth/sol/xrp — so the producer's hype/doge keys are silently
dropped on the floor today (since 5dca85a ship). Result: HYPE/DOGE shadow
rows accumulate WITHOUT cross-asset features, AND existing
BTC/ETH/SOL/XRP rows accumulate WITHOUT HYPE/DOGE columns. Both block T3.

Bit 2 expands the consumer chain to 6 columns and adds supabase migration 019.

Failure modes guarded:
  - signature-only without DDL → KeyError on insert
  - DDL-only without signature → kwarg rejected (sig contract test)
  - supabase whitelist without remote migration → silent HTTP-400 wedge
    (_check_schema_parity 2026-04-04 postmortem at supabase_sync.py:495)
  - DDL + signature but consumer in `_extended_feature_provider` still
    only reads 4 keys → producer's hype/doge silently dropped (the
    bug class this Bit closes)
  - Backfill harness UPDATEs 4 columns → hype/doge stay NULL forever
    on rows pre-deploy (regenerable only from Coinbase historical
    candles within minute-grain tolerance)

ClickUp: 86b9vrjf2
Bit doc: kb/decisions/asset-onboarding-doge-hype-bit-2-shipped-may10.md
"""

import inspect
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts", "backfill"))


# Bit 2 / T1 cross-asset expansion — the NEW columns added on top of the
# 4 already shipped by Phase B (btc/eth/sol/xrp_spot_at_decision in
# migration 011 2026-05-02). These ship in supabase migration 019 +
# state.py DDL ALTER TABLE additions.
HYPE_DOGE_NEW_COLUMNS = [
    ("hype_spot_at_decision", "REAL"),
    ("doge_spot_at_decision", "REAL"),
]


class TestHypeDogeSpotAtDecisionSchema:
    """Bit 2 schema additions: hype_spot_at_decision + doge_spot_at_decision
    on evaluated_opportunities, REAL-typed, nullable."""

    def test_hype_doge_columns_present_in_schema(self):
        import bot
        import bot.state  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.state.X access)
        sm = bot.state.StateManager(":memory:")
        cols = {
            r["name"] for r in
            sm.conn.execute("PRAGMA table_info(evaluated_opportunities)").fetchall()
        }
        missing = [name for (name, _) in HYPE_DOGE_NEW_COLUMNS if name not in cols]
        assert not missing, (
            f"Bit 2 columns missing from evaluated_opportunities schema: "
            f"{missing}. Add via ALTER TABLE ADD COLUMN in StateManager."
            f"_create_tables migration loop (paired with Phase B precedent)."
        )

    def test_hype_doge_columns_are_real_typed(self):
        """REAL-typed, mirroring btc/eth/sol/xrp_spot_at_decision (Phase B
        precedent). Supabase migration 019 uses `double precision` for
        identical reasons (high-precision spot prices can hold sub-cent BTC
        levels — migration 011 header explains the rationale)."""
        import bot
        import bot.state  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.state.X access)
        sm = bot.state.StateManager(":memory:")
        col_types = {
            r["name"]: r["type"].upper() for r in
            sm.conn.execute("PRAGMA table_info(evaluated_opportunities)").fetchall()
        }
        mistyped = []
        for (name, sql_type) in HYPE_DOGE_NEW_COLUMNS:
            assert name in col_types, f"{name} missing from schema"
            if col_types[name] != sql_type.upper():
                mistyped.append(f"{name}: expected {sql_type}, got {col_types[name]}")
        assert not mistyped, "Type drift on Bit 2 columns:\n" + "\n".join(mistyped)


class TestHypeDogeSpotAtDecisionSignature:
    """insert_evaluated_opportunity accepts hype/doge_spot_at_decision as
    kwargs (Optional[float] = None, matching btc/eth/sol/xrp precedent)."""

    def test_signature_accepts_hype_doge_kwargs(self):
        from bot.state import StateManager
        sig = inspect.signature(StateManager.insert_evaluated_opportunity)
        params = set(sig.parameters.keys())
        missing = [name for (name, _) in HYPE_DOGE_NEW_COLUMNS if name not in params]
        assert not missing, (
            f"insert_evaluated_opportunity missing Bit 2 kwargs: {missing}. "
            f"Add to function signature + INSERT column list + VALUES "
            f"placeholders + ON CONFLICT DO UPDATE SET + parameter tuple."
        )


class TestHypeDogeSpotAtDecisionRoundtrip:
    """Real DB roundtrip: insert with values, read back, both columns survive."""

    def test_insert_round_trips_hype_doge_columns(self):
        import bot
        import bot.state  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.state.X access)
        sm = bot.state.StateManager(":memory:")
        kwargs = {
            "ticker": "KXHYPE15M-26MAY101900-T26",
            "event_ticker": "KXHYPE15M-26MAY101900",
            "asset": "HYPE", "filter_stage": "hype_shadow",
            "hype_spot_at_decision": 24.512,
            "doge_spot_at_decision": 0.1837,
        }
        sm.insert_evaluated_opportunity(**kwargs)
        row = sm.conn.execute(
            "SELECT hype_spot_at_decision, doge_spot_at_decision "
            "FROM evaluated_opportunities WHERE ticker = ?",
            (kwargs["ticker"],)
        ).fetchone()
        assert row is not None, "row not inserted"
        assert row["hype_spot_at_decision"] == pytest.approx(24.512), (
            f"hype_spot_at_decision: got {row['hype_spot_at_decision']!r}, "
            f"expected 24.512"
        )
        assert row["doge_spot_at_decision"] == pytest.approx(0.1837), (
            f"doge_spot_at_decision: got {row['doge_spot_at_decision']!r}, "
            f"expected 0.1837"
        )


class TestHypeDogeSpotAtDecisionProviderRoundtrip:
    """Closes the silent-drop bug class: when _extended_feature_provider
    returns hype/doge keys (T1 producer at bot/scanner/__init__.py:990
    already does this), the insert path must consume them — they were
    being silently dropped on the floor pre-Bit-2 because the consumer
    block at bot/state.py:1833-1840 only read 4 keys."""

    def test_provider_six_keys_populates_all_six_columns(self):
        import bot
        import bot.state  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.state.X access)
        sm = bot.state.StateManager(":memory:")

        # Stub _extended_feature_provider returning all six spot keys —
        # mirrors the ASSETS-driven scanner output. Provider signature is
        # (ticker, asset, spot_price, threshold, product_type) per
        # bot/state.py:1782.
        def _provider(ticker, asset, spot_price, threshold, product_type):
            return {
                "btc_spot_at_decision": 67432.5,
                "eth_spot_at_decision": 3210.5,
                "sol_spot_at_decision": 142.7,
                "xrp_spot_at_decision": 2.51,
                "hype_spot_at_decision": 24.512,
                "doge_spot_at_decision": 0.1837,
            }
        sm._extended_feature_provider = _provider

        sm.insert_evaluated_opportunity(
            ticker="KXBTC15M-26MAY101900-T67400",
            event_ticker="KXBTC15M-26MAY101900",
            asset="BTC", filter_stage="candidate",
        )
        row = sm.conn.execute(
            "SELECT btc_spot_at_decision, eth_spot_at_decision, "
            "sol_spot_at_decision, xrp_spot_at_decision, "
            "hype_spot_at_decision, doge_spot_at_decision "
            "FROM evaluated_opportunities WHERE ticker = 'KXBTC15M-26MAY101900-T67400'"
        ).fetchone()
        assert row is not None
        # All six columns populated — closes the silent-drop bug class.
        assert row["btc_spot_at_decision"] == pytest.approx(67432.5)
        assert row["eth_spot_at_decision"] == pytest.approx(3210.5)
        assert row["sol_spot_at_decision"] == pytest.approx(142.7)
        assert row["xrp_spot_at_decision"] == pytest.approx(2.51)
        assert row["hype_spot_at_decision"] == pytest.approx(24.512), (
            "hype key from provider was silently dropped (the bug Bit 2 closes)"
        )
        assert row["doge_spot_at_decision"] == pytest.approx(0.1837), (
            "doge key from provider was silently dropped (the bug Bit 2 closes)"
        )


class TestHypeDogeSpotAtDecisionSupabaseWhitelist:
    """Supabase _EVAL_COLUMNS whitelist must include hype/doge column names
    or the mirror skips them. CAVEAT: only add to whitelist AFTER migration
    019 is applied to remote — otherwise silent HTTP-400 wedge per
    _check_schema_parity 2026-04-04 postmortem."""

    def test_eval_columns_whitelist_includes_hype_doge(self):
        from bot.snapshots.supabase_sync import SupabaseSyncer
        whitelist = {c.strip() for c in SupabaseSyncer._EVAL_COLUMNS.split(",")}
        missing = [name for (name, _) in HYPE_DOGE_NEW_COLUMNS if name not in whitelist]
        assert not missing, (
            f"supabase_sync._EVAL_COLUMNS missing Bit 2 columns: {missing}. "
            f"Add to the comma-separated string AFTER landing supabase migration "
            f"019 that adds these columns to the remote `evaluations` table."
        )


class TestHypeDogeSpotAtDecisionMigration019Sql:
    """A migration file `scripts/supabase_migration_019_*.sql` must add the
    two new spot columns to public.evaluations with idempotent
    ADD COLUMN IF NOT EXISTS, and end with NOTIFY pgrst, 'reload schema'."""

    def _migration_path(self):
        # Bit 11.2 (2026-05-12): supabase_migration_*.sql moved to scripts/ops/.
        scripts_dir = os.path.join(PROJECT_ROOT, "scripts", "ops")
        candidates = [
            f for f in os.listdir(scripts_dir)
            if f.startswith("supabase_migration_019_") and f.endswith(".sql")
            # Skip iCloud-dup `* 2.sql`/`* 3.sql` suffixes (Mac sync noise).
            and " " not in f
        ]
        assert candidates, (
            "No scripts/ops/supabase_migration_019_*.sql found. Bit 2 requires a "
            "supabase migration that adds hype/doge_spot_at_decision to the "
            "remote evaluations table."
        )
        assert len(candidates) == 1, f"Multiple migration_019 files: {candidates}"
        return os.path.join(scripts_dir, candidates[0])

    def test_migration_sql_adds_hype_doge_columns(self):
        path = self._migration_path()
        sql = open(path).read().lower()
        missing = [
            name for (name, _) in HYPE_DOGE_NEW_COLUMNS
            if f"add column if not exists {name.lower()}" not in sql
        ]
        assert not missing, (
            f"Migration 019 missing ADD COLUMN IF NOT EXISTS for: {missing}"
        )

    def test_migration_sql_notifies_pgrst(self):
        path = self._migration_path()
        sql = open(path).read().lower()
        assert "notify pgrst" in sql, (
            "Migration 019 must NOTIFY pgrst, 'reload schema' so PostgREST "
            "picks up the new columns (per migration 010+011 convention)."
        )

    def test_migration_sql_targets_evaluations_table(self):
        path = self._migration_path()
        sql = open(path).read().lower()
        assert "alter table public.evaluations" in sql or "alter table evaluations" in sql, (
            "Migration 019 must target the evaluations table."
        )


class TestHypeDogeSpotAtDecisionBackfill:
    """Phase G-2 backfill harness must UPDATE hype/doge_spot_at_decision
    columns. Pre-Bit-2 the UPDATE SQL listed only 4 columns; HYPE/DOGE
    rows accumulated since T1 ship (5dca85a) without backfill coverage."""

    def _make_db_with_eval_schema(self, tmp_path):
        import bot
        import bot.state  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.state.X access)
        db_path = tmp_path / "state.db"
        sm = bot.state.StateManager(str(db_path))
        return sm

    def test_backfill_picks_up_row_with_btc_populated_but_hype_doge_null(self, tmp_path):
        """R1 adversarial review M1 regression: rows accumulated since T1
        5dca85a (2026-05-10) have btc/eth/sol/xrp_spot_at_decision populated
        but hype/doge_spot_at_decision NULL because the pre-Bit-2 consumer
        at bot/state.py:1833-1840 silently dropped hype/doge keys from the
        producer. Bit 2 widened the backfill WHERE predicate so these rows
        ARE picked up. Without the widening, the post-ship backfill runbook
        would skip exactly the cohort the Bit is designed to fix."""
        from shadow_coverage_backfill import backfill_xasset_spots
        sm = self._make_db_with_eval_schema(tmp_path)
        # Row that has btc populated (pre-Bit-2 live capture) but
        # hype/doge NULL (silent-drop bug).
        sm.conn.execute(
            "INSERT INTO evaluated_opportunities(ticker, event_ticker, asset, "
            "filter_stage, evaluation_time, product_type, status, "
            "btc_spot_at_decision, eth_spot_at_decision, "
            "sol_spot_at_decision, xrp_spot_at_decision) "
            "VALUES ('PARTIAL', 'E', 'BTC', 'candidate', "
            "'2026-05-10T23:59:00Z', '15m', 'pending', "
            "55555.0, 5555.0, 555.0, 5.55)"
        )
        sm.conn.commit()

        import datetime as _dt
        epoch_sec = int(_dt.datetime(2026, 5, 10, 23, 59, 0,
                                       tzinfo=_dt.timezone.utc).timestamp())

        def _mock_fetch(asset, start_iso, end_iso):
            base = {"BTC": 67432.5, "ETH": 3210.5,
                    "SOL": 142.7, "XRP": 2.51,
                    "HYPE": 24.512, "DOGE": 0.1837,
                    "BNB": 655.4}[asset]
            return [[epoch_sec, base, base, base, base, 1.0]]

        n = backfill_xasset_spots(sm.conn, fetcher=_mock_fetch, batch_size=10)
        # The partial row must be picked up by the widened predicate.
        assert n == 1, (
            "Bit 2 R1 M1: row with btc populated but hype/doge NULL must "
            "be picked up by widened WHERE clause. n=0 means the predicate "
            "still only checks btc_spot_at_decision IS NULL — regression."
        )
        row = sm.conn.execute(
            "SELECT hype_spot_at_decision, doge_spot_at_decision "
            "FROM evaluated_opportunities WHERE ticker='PARTIAL'"
        ).fetchone()
        # Critical: the missing columns ARE filled by the backfill.
        assert row["hype_spot_at_decision"] == pytest.approx(24.512)
        assert row["doge_spot_at_decision"] == pytest.approx(0.1837)

    def test_backfill_writes_hype_doge_spots(self, tmp_path):
        from shadow_coverage_backfill import backfill_xasset_spots
        sm = self._make_db_with_eval_schema(tmp_path)
        sm.conn.execute(
            "INSERT INTO evaluated_opportunities(ticker, event_ticker, asset, "
            "filter_stage, evaluation_time, product_type, status) "
            "VALUES ('TESTHYPE', 'E', 'HYPE', 'hype_shadow', "
            "'2026-05-10T23:59:00Z', '15m', 'pending')"
        )
        sm.conn.commit()

        import datetime as _dt
        epoch_sec = int(_dt.datetime(2026, 5, 10, 23, 59, 0,
                                       tzinfo=_dt.timezone.utc).timestamp())

        def _mock_fetch(asset, start_iso, end_iso):
            base = {"BTC": 67432.5, "ETH": 3210.5,
                    "SOL": 142.7, "XRP": 2.51,
                    "HYPE": 24.512, "DOGE": 0.1837,
                    "BNB": 655.4}[asset]
            return [[epoch_sec, base, base, base, base, 1.0]]

        n = backfill_xasset_spots(sm.conn, fetcher=_mock_fetch, batch_size=10)
        assert n == 1
        row = sm.conn.execute(
            "SELECT btc_spot_at_decision, eth_spot_at_decision, "
            "sol_spot_at_decision, xrp_spot_at_decision, "
            "hype_spot_at_decision, doge_spot_at_decision "
            "FROM evaluated_opportunities WHERE ticker = 'TESTHYPE'"
        ).fetchone()
        # Closes the bug class: pre-Bit-2 the UPDATE SQL only listed 4
        # columns, so hype/doge stayed NULL forever on historical rows.
        assert row["btc_spot_at_decision"] == pytest.approx(67432.5)
        assert row["eth_spot_at_decision"] == pytest.approx(3210.5)
        assert row["sol_spot_at_decision"] == pytest.approx(142.7)
        assert row["xrp_spot_at_decision"] == pytest.approx(2.51)
        assert row["hype_spot_at_decision"] == pytest.approx(24.512), (
            "Bit 2: backfill UPDATE must extend to hype column"
        )
        assert row["doge_spot_at_decision"] == pytest.approx(0.1837), (
            "Bit 2: backfill UPDATE must extend to doge column"
        )
