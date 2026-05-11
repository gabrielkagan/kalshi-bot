"""Phase B (Shadow Coverage Expansion): schema additions to evaluated_opportunities.

Tracks the 18 new nullable columns that future phases (D/E/F) populate:
  - State-at-decision-time (3): n_open_positions, recent_n_outcome_streak,
    time_since_last_fill_s
  - Maker counterfactual (3): maker_price_cents, maker_depth_at_post,
    maker_would_fill_within_30s
  - Path-of-rejection (1): next_blocking_gate
  - Resolution metadata (5): final_spot_price, knockout_time_relative,
    max_excursion_from_strike, time_above_strike_seconds, time_below_strike_seconds
  - Cross-asset (4): btc/eth/sol/xrp_spot_at_decision
  - Funding/basis (2): okx/deribit_funding_rate_at_decision

This test asserts: column exists in DB schema after migrate, signature
accepts each as a kwarg, supabase mirror whitelist includes each, integer
columns are listed in supabase_sync._INT_COLUMNS, supabase migration 011
SQL ships the same set.

Phase B is schema-only. Phase E/F populate. Phase D handles cal_mlp annotation.

Master plan: kb/decisions/shadow-coverage-expansion-may01.md
"""

import inspect
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


# Authoritative list of new columns added by Phase B. (col_name, sql_type, is_int_typed)
# Types are SQLite types; the supabase migration uses Postgres equivalents.
PHASE_B_NEW_COLUMNS = [
    # State-at-decision-time (3): account_balance_dollars + drawdown_scaler_value
    # already exist as available_balance_cents + drawdown_scaler — not duplicated.
    # active_positions_same_asset already exists for same-asset count;
    # n_open_positions is the total across all assets.
    ("n_open_positions", "INTEGER", True),
    ("recent_n_outcome_streak", "INTEGER", True),  # signed (e.g. +3 or -2)
    ("time_since_last_fill_s", "REAL", False),
    # Maker counterfactual (3) — populated post-hoc by maker fillability job.
    ("maker_price_cents", "INTEGER", True),
    ("maker_depth_at_post", "INTEGER", True),
    ("maker_would_fill_within_30s", "INTEGER", True),  # 0/1
    # Path-of-rejection (1) — populated at rejection-decision time.
    ("next_blocking_gate", "TEXT", False),
    # Resolution metadata (5) — populated by settlement.
    ("final_spot_price", "REAL", False),
    ("knockout_time_relative", "REAL", False),  # seconds, fractional
    ("max_excursion_from_strike", "REAL", False),
    ("time_above_strike_seconds", "REAL", False),
    ("time_below_strike_seconds", "REAL", False),
    # Cross-asset (4) — absolute prices at decision tick.
    # Existing btc_spot_change_5m/30m_bps are RELATIVE; these are levels.
    ("btc_spot_at_decision", "REAL", False),
    ("eth_spot_at_decision", "REAL", False),
    ("sol_spot_at_decision", "REAL", False),
    ("xrp_spot_at_decision", "REAL", False),
    # Funding/basis (2) — populated when accessible.
    ("okx_funding_rate_at_decision", "REAL", False),
    ("deribit_funding_rate_at_decision", "REAL", False),
]


class TestPhaseBSchemaColumns:
    """All 18 Phase B columns exist on evaluated_opportunities after StateManager init."""

    def test_all_phase_b_columns_present_in_schema(self):
        import bot
        sm = bot.state.StateManager(":memory:")
        cols = {
            r["name"] for r in
            sm.conn.execute("PRAGMA table_info(evaluated_opportunities)").fetchall()
        }
        missing = [name for (name, _, _) in PHASE_B_NEW_COLUMNS if name not in cols]
        assert not missing, (
            f"Phase B columns missing from evaluated_opportunities schema: {missing}. "
            f"Add via ALTER TABLE ADD COLUMN in StateManager._create_tables migration loop."
        )

    def test_phase_b_int_columns_are_integer_typed(self):
        """INTEGER-typed Phase B columns must be declared INTEGER in SQLite.
        Float values silently round in SQLite (loose typing) but the supabase
        mirror does not — the four-site lock-step rule (CLAUDE.md) means a
        type mismatch is a 22P02 wedge waiting to happen."""
        import bot
        sm = bot.state.StateManager(":memory:")
        col_types = {
            r["name"]: r["type"].upper() for r in
            sm.conn.execute("PRAGMA table_info(evaluated_opportunities)").fetchall()
        }
        mistyped = []
        for (name, sql_type, _is_int) in PHASE_B_NEW_COLUMNS:
            assert name in col_types, f"{name} missing from schema"
            if col_types[name] != sql_type.upper():
                mistyped.append(f"{name}: expected {sql_type}, got {col_types[name]}")
        assert not mistyped, "Type drift on Phase B columns:\n" + "\n".join(mistyped)


class TestPhaseBInsertSignature:
    """insert_evaluated_opportunity accepts every Phase B column as a kwarg."""

    def test_signature_accepts_all_phase_b_columns(self):
        from bot.state import StateManager
        sig = inspect.signature(StateManager.insert_evaluated_opportunity)
        params = set(sig.parameters.keys())
        missing = [name for (name, _, _) in PHASE_B_NEW_COLUMNS if name not in params]
        assert not missing, (
            f"insert_evaluated_opportunity missing Phase B kwargs: {missing}. "
            f"Add to function signature + INSERT column list + VALUES tuple."
        )

    def test_insert_round_trips_phase_b_columns(self):
        """Real DB roundtrip: insert with values, read back, every column survives."""
        import bot
        sm = bot.state.StateManager(":memory:")
        kwargs = {
            "ticker": "TEST15M-TEST", "event_ticker": "TEST15M",
            "asset": "BTC", "filter_stage": "low_price_shadow",
            # Provide a representative non-None for every Phase B column.
            "n_open_positions": 3,
            "recent_n_outcome_streak": -2,
            "time_since_last_fill_s": 612.5,
            "maker_price_cents": 65,
            "maker_depth_at_post": 100,
            "maker_would_fill_within_30s": 1,
            "next_blocking_gate": "edge_fee_filter",
            "final_spot_price": 67421.5,
            "knockout_time_relative": 142.3,
            "max_excursion_from_strike": 234.7,
            "time_above_strike_seconds": 720.1,
            "time_below_strike_seconds": 179.9,
            "btc_spot_at_decision": 67400.0,
            "eth_spot_at_decision": 3210.5,
            "sol_spot_at_decision": 142.7,
            "xrp_spot_at_decision": 2.51,
            "okx_funding_rate_at_decision": 0.0001,
            "deribit_funding_rate_at_decision": -0.00005,
        }
        sm.insert_evaluated_opportunity(**kwargs)
        row = sm.conn.execute(
            "SELECT * FROM evaluated_opportunities WHERE ticker = ?",
            (kwargs["ticker"],)
        ).fetchone()
        assert row is not None, "row not inserted"
        for name, sql_type, _ in PHASE_B_NEW_COLUMNS:
            actual = row[name]
            expected = kwargs[name]
            assert actual is not None, f"{name} round-tripped as NULL (expected {expected!r})"
            if sql_type == "INTEGER":
                assert actual == expected, f"{name}: got {actual!r}, expected {expected!r}"
            elif sql_type == "REAL":
                assert abs(actual - expected) < 1e-9, (
                    f"{name}: got {actual!r}, expected {expected!r}"
                )
            else:  # TEXT
                assert actual == expected, f"{name}: got {actual!r}, expected {expected!r}"


class TestPhaseBSupabaseMirror:
    """Supabase mirror whitelist + int-coercion set must include Phase B columns
    so the sync (supabase_sync._sync_evaluations) does not skip new fields, and
    int-typed columns get the defensive round-to-int from _coerce_int_columns."""

    def test_eval_columns_whitelist_includes_phase_b(self):
        from supabase_sync import SupabaseSyncer
        whitelist = {c.strip() for c in SupabaseSyncer._EVAL_COLUMNS.split(",")}
        missing = [name for (name, _, _) in PHASE_B_NEW_COLUMNS if name not in whitelist]
        assert not missing, (
            f"supabase_sync._EVAL_COLUMNS missing Phase B columns: {missing}. "
            f"Add to the comma-separated string AFTER landing the supabase "
            f"migration that adds these columns to the remote `evaluations` "
            f"table — adding to whitelist before the remote migration causes "
            f"silent HTTP-400 + frozen watermark."
        )

    def test_int_columns_includes_phase_b_integers(self):
        from supabase_sync import SupabaseSyncer
        int_cols_in_phase_b = [
            name for (name, _, is_int) in PHASE_B_NEW_COLUMNS if is_int
        ]
        missing = [c for c in int_cols_in_phase_b if c not in SupabaseSyncer._INT_COLUMNS]
        assert not missing, (
            f"supabase_sync._INT_COLUMNS missing Phase B int columns: {missing}. "
            f"Add to defensive coerce-set so a float upstream cannot 22P02-wedge "
            f"the mirror (memory: project_may01_calmlp_dashboard_chain)."
        )


class TestPhaseBSupabaseMigrationSql:
    """A migration file `scripts/supabase_migration_011_*.sql` must add the same
    18 Phase B columns to public.evaluations with idempotent ADD COLUMN IF NOT
    EXISTS, and end with NOTIFY pgrst, 'reload schema' (per migration 010 +
    dashboard-drift postmortem)."""

    def _migration_path(self):
        scripts_dir = os.path.join(PROJECT_ROOT, "scripts")
        candidates = [
            f for f in os.listdir(scripts_dir)
            if f.startswith("supabase_migration_011_") and f.endswith(".sql")
        ]
        assert candidates, (
            "No scripts/supabase_migration_011_*.sql found. Phase B requires a "
            "supabase migration that adds the new evaluations columns."
        )
        assert len(candidates) == 1, f"Multiple migration_011 files: {candidates}"
        return os.path.join(scripts_dir, candidates[0])

    def test_migration_sql_adds_all_phase_b_columns(self):
        path = self._migration_path()
        sql = open(path).read().lower()
        missing = [
            name for (name, _, _) in PHASE_B_NEW_COLUMNS
            if f"add column if not exists {name.lower()}" not in sql
        ]
        assert not missing, (
            f"Migration 011 missing ADD COLUMN IF NOT EXISTS for: {missing}"
        )

    def test_migration_sql_notifies_pgrst(self):
        path = self._migration_path()
        sql = open(path).read().lower()
        assert "notify pgrst" in sql, (
            "Migration 011 must NOTIFY pgrst, 'reload schema' so PostgREST "
            "picks up the new columns (per migration 010 convention)."
        )

    def test_migration_sql_targets_evaluations_table(self):
        path = self._migration_path()
        sql = open(path).read().lower()
        assert "alter table public.evaluations" in sql or "alter table evaluations" in sql, (
            "Migration 011 must target the evaluations table."
        )
