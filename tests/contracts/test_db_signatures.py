"""DB Signature Alignment Tests.

Failure mode: New key added to _shadow_diag without updating insert_rejection() /
insert_evaluated_opportunity() signatures -> runtime crash on first trade.
Past incident: Documented in CLAUDE.md as a critical rule.

Also checks:
- busy_timeout on all sqlite3.connect() calls
- check_same_thread=False on multi-threaded files
"""

import ast
import inspect
import os
import re
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

# Files that are called from multiple threads and MUST use check_same_thread=False
MULTI_THREAD_FILES = [
    "bot/_impl.py",
    "bot/shadows/fifteenm_shadow.py",  # Sprint 10.2 (2026-05-11)
    "bot/shadows/hourly_alt_shadow.py",  # Sprint 10.2 (2026-05-11)
    "bot/shadows/spx_harrv_shadow.py",  # Sprint 10.2 (2026-05-11)
    "bot/snapshots/supabase_sync.py",  # Sprint 10 Bit 10.4 (2026-05-12)
]

# All production .py files (exclude venv, tests, scripts, migration utilities)
PRODUCTION_FILES = [
    "bot/_impl.py",
    "bot/ai/analyst.py",  # Sprint 10.3 (2026-05-12)
    "bot/snapshots/dashboard_snapshot.py",  # Sprint 10 Bit 10.4 (2026-05-12)
    "bot/shadows/fifteenm_shadow.py",  # Sprint 10.2 (2026-05-11)
    "bot/shadows/hourly_alt_shadow.py",  # Sprint 10.2 (2026-05-11)
    "market_config.py",
    "bot/engines/spx_engine.py",  # Sprint 10.1b sibling-reorg (2026-05-11)
    "bot/shadows/spx_harrv_shadow.py",  # Sprint 10.2 (2026-05-11)
    "bot/engines/sports_engine.py",  # Sprint 10.1d (2026-05-11)
    "bot/snapshots/supabase_sync.py",  # Sprint 10 Bit 10.4 (2026-05-12)
    "watchdog.py",
    "bot/engines/weather_engine.py",  # Sprint 10.1c sibling-reorg (2026-05-11)
    "bot/infra/capital_allocator.py",  # Sprint 10.5a (2026-05-11)
]


class TestShadowDiagKeyCoverage:
    """Every _shadow_diag key must be accepted by both insert functions."""

    def _get_shadow_diag_keys(self):
        """Parse bot/_impl.py + bot/scanner/__init__.py AST to find all keys
        in _shadow_diag = {...}.

        Bit 8.1 (2026-05-10): scanner moved out of bot/_impl.py; the
        _shadow_diag literal lives in bot/scanner/__init__.py post-extraction.
        Walk both files so the audit survives the move.
        """
        for relpath in ("bot/_impl.py", "bot/scanner/__init__.py"):
            full_path = os.path.join(PROJECT_ROOT, relpath)
            if not os.path.isfile(full_path):
                continue
            with open(full_path) as f:
                source = f.read()
            # Find _shadow_diag = { ... } via regex (AST won't easily find dict in function body)
            pattern = r'_shadow_diag\s*=\s*\{([^}]+)\}'
            match = re.search(pattern, source)
            if not match:
                continue
            dict_content = match.group(1)
            keys = re.findall(r'"(\w+)"', dict_content)
            assert len(keys) > 0, f"_shadow_diag has no keys (found in {relpath})"
            return set(keys)
        raise AssertionError(
            "_shadow_diag dict not found in bot/_impl.py or bot/scanner/__init__.py"
        )

    def _get_function_params(self, func_name):
        """Get the parameter names of a function from bot/_impl.py's StateManager."""
        from bot.state import StateManager
        func = getattr(StateManager, func_name)
        sig = inspect.signature(func)
        # Skip 'self'
        return {name for name in sig.parameters if name != 'self'}

    def test_shadow_diag_keys_in_insert_rejection(self):
        """Every _shadow_diag key must be a parameter of insert_rejection()."""
        diag_keys = self._get_shadow_diag_keys()
        rejection_params = self._get_function_params("insert_rejection")
        missing = diag_keys - rejection_params
        assert not missing, (
            f"_shadow_diag keys missing from insert_rejection(): {missing}")

    def test_shadow_diag_keys_in_insert_evaluated_opportunity(self):
        """Every _shadow_diag key must be a parameter of insert_evaluated_opportunity()."""
        diag_keys = self._get_shadow_diag_keys()
        eval_params = self._get_function_params("insert_evaluated_opportunity")
        missing = diag_keys - eval_params
        assert not missing, (
            f"_shadow_diag keys missing from insert_evaluated_opportunity(): {missing}")

    def test_shadow_diag_keys_in_db_schema(self):
        """Tri-contract third leg: every _shadow_diag key must be a real
        column in evaluated_opportunities AND rejected_opportunities.

        Without this check, a key can live through both sibling signatures
        and still fail silently at INSERT time with `no such column` —
        the row never lands. Wires the CLAUDE.md "shadow diag" critical
        rule end-to-end against the live sqlite schema.
        """
        import bot
        import bot.state  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.state.X access)
        sm = bot.state.StateManager(":memory:")
        eval_cols = {
            r["name"] for r in
            sm.conn.execute("PRAGMA table_info(evaluated_opportunities)").fetchall()
        }
        rej_cols = {
            r["name"] for r in
            sm.conn.execute("PRAGMA table_info(rejected_opportunities)").fetchall()
        }
        diag_keys = self._get_shadow_diag_keys()

        missing_eval = diag_keys - eval_cols
        missing_rej = diag_keys - rej_cols
        errors = []
        if missing_eval:
            errors.append(
                f"_shadow_diag keys missing from evaluated_opportunities "
                f"schema: {sorted(missing_eval)} — add via "
                f"ALTER TABLE ADD COLUMN in StateManager._create_tables "
                f"migration loop."
            )
        if missing_rej:
            errors.append(
                f"_shadow_diag keys missing from rejected_opportunities "
                f"schema: {sorted(missing_rej)} — add via "
                f"ALTER TABLE ADD COLUMN in StateManager._create_tables "
                f"migration loop."
            )
        assert not errors, "\n".join(errors)


class TestBusyTimeout:
    """Every sqlite3.connect() must set PRAGMA busy_timeout."""

    def test_all_production_files_have_busy_timeout(self):
        """Every sqlite3.connect() call is followed by PRAGMA busy_timeout within 5 lines."""
        failures = []

        for filename in PRODUCTION_FILES:
            filepath = os.path.join(PROJECT_ROOT, filename)
            if not os.path.exists(filepath):
                continue

            with open(filepath) as f:
                lines = f.readlines()

            for i, line in enumerate(lines):
                if "sqlite3.connect(" in line:
                    # Check next 8 lines for busy_timeout
                    window = "".join(lines[i:i + 9])
                    if "busy_timeout" not in window and "timeout=" not in window:
                        failures.append(f"{filename}:{i+1}")

        assert not failures, (
            f"sqlite3.connect() without busy_timeout: {failures}")


class TestCheckSameThread:
    """Multi-threaded files must use check_same_thread=False."""

    def test_multi_thread_files_have_check_same_thread(self):
        """Files called from multiple threads must use check_same_thread=False."""
        failures = []

        for filename in MULTI_THREAD_FILES:
            filepath = os.path.join(PROJECT_ROOT, filename)
            if not os.path.exists(filepath):
                continue

            with open(filepath) as f:
                lines = f.readlines()

            for i, line in enumerate(lines):
                # Only match actual sqlite3.connect() calls, not comments
                stripped = line.lstrip()
                if stripped.startswith('#') or stripped.startswith('//'):
                    continue
                if 'sqlite3.connect(' not in line:
                    continue

                # Check the connect call and the next few lines for check_same_thread
                window = "".join(lines[i:i + 5])
                if "check_same_thread=False" not in window:
                    failures.append(f"{filename}:{i+1}")

        assert not failures, (
            f"Multi-threaded sqlite3.connect() without check_same_thread=False: {failures}")


class TestInsertFunctionSignatures:
    """Insert functions accept all columns they try to write."""

    def test_insert_rejection_has_product_type(self):
        """insert_rejection must accept product_type for multi-market support."""
        from bot.state import StateManager
        sig = inspect.signature(StateManager.insert_rejection)
        assert "product_type" in sig.parameters, (
            "insert_rejection missing product_type parameter")

    def test_insert_evaluated_opportunity_has_product_type(self):
        """insert_evaluated_opportunity must accept product_type."""
        from bot.state import StateManager
        sig = inspect.signature(StateManager.insert_evaluated_opportunity)
        assert "product_type" in sig.parameters, (
            "insert_evaluated_opportunity missing product_type parameter")

    def test_insert_evaluated_opportunity_has_raw_prob(self):
        """insert_evaluated_opportunity must accept raw_prob for CalEngine pipeline."""
        from bot.state import StateManager
        sig = inspect.signature(StateManager.insert_evaluated_opportunity)
        assert "raw_prob" in sig.parameters, (
            "insert_evaluated_opportunity missing raw_prob parameter")

    def test_insert_evaluated_opportunity_has_side(self):
        """insert_evaluated_opportunity must accept side for NO-side trading."""
        from bot.state import StateManager
        sig = inspect.signature(StateManager.insert_evaluated_opportunity)
        assert "side" in sig.parameters, (
            "insert_evaluated_opportunity missing side parameter")

    def test_insert_evaluated_opportunity_has_order_tracking(self):
        """insert_evaluated_opportunity must accept order tracking fields."""
        from bot.state import StateManager
        sig = inspect.signature(StateManager.insert_evaluated_opportunity)
        for field in ("order_id", "order_submitted_at", "order_outcome"):
            assert field in sig.parameters, (
                f"insert_evaluated_opportunity missing {field} parameter")

    def test_insert_evaluated_opportunity_has_weather_fields(self):
        """insert_evaluated_opportunity must accept weather ensemble fields."""
        from bot.state import StateManager
        sig = inspect.signature(StateManager.insert_evaluated_opportunity)
        weather_fields = [
            "wx_ensemble_mean", "wx_ensemble_std", "wx_bias_correction",
            "wx_n_members", "wx_market_type",
        ]
        for field in weather_fields:
            assert field in sig.parameters, (
                f"insert_evaluated_opportunity missing {field}")

    def test_insert_evaluated_opportunity_has_hourly_shadow_fields(self):
        """insert_evaluated_opportunity must accept hourly shadow comparison fields."""
        from bot.state import StateManager
        sig = inspect.signature(StateManager.insert_evaluated_opportunity)
        hourly_fields = [
            "hourly_pre_temp_prob", "hourly_applied_temp_t",
        ]
        for field in hourly_fields:
            assert field in sig.parameters, (
                f"insert_evaluated_opportunity missing {field}")

    def test_insert_evaluated_opportunity_has_data_provenance(self):
        """Phase G-6: insert_evaluated_opportunity must accept data_provenance
        with default 'live_ws'. v2 calibrator training filters held-out
        validation on this column — losing the kwarg silently regresses to
        all-NULL provenance and breaks the train/serve skew gate.

        See kb/decisions/v2-train-must-account-for-backfill-skew-may02.md.
        """
        from bot.state import StateManager
        sig = inspect.signature(StateManager.insert_evaluated_opportunity)
        assert "data_provenance" in sig.parameters, (
            "insert_evaluated_opportunity missing data_provenance parameter")
        assert sig.parameters["data_provenance"].default == "live_ws", (
            "data_provenance default must be 'live_ws' so live-bot inserts "
            "auto-tag without each caller having to remember the kwarg")

    def test_insert_evaluated_opportunity_has_bot_state_snapshot_json(self):
        """Phase H-2: insert_evaluated_opportunity must accept
        bot_state_snapshot_json with default None. Forward-going microstate
        capture for v2 cal_mlp; default None means callers that don't pass
        it (e.g., legacy paths) get NULL rather than crashing.

        See kb/decisions/phase-h2-bot-microstate-fwd-may02.md.
        """
        from bot.state import StateManager
        sig = inspect.signature(StateManager.insert_evaluated_opportunity)
        assert "bot_state_snapshot_json" in sig.parameters, (
            "insert_evaluated_opportunity missing bot_state_snapshot_json"
        )
        assert sig.parameters["bot_state_snapshot_json"].default is None, (
            "bot_state_snapshot_json default must be None so legacy callers "
            "(or callers that fail to compute the snapshot) write NULL "
            "rather than crashing the insert"
        )


class TestSyntaxCheck:
    """All critical production files parse without syntax errors."""

    CRITICAL_FILES = [
        "bot/_impl.py",
        "market_config.py",
        "bot/snapshots/dashboard_snapshot.py",  # Sprint 10 Bit 10.4 (2026-05-12)
        "bot/ai/analyst.py",  # Sprint 10.3 (2026-05-12)
        "bot/snapshots/supabase_sync.py",  # Sprint 10 Bit 10.4 (2026-05-12)
        "bot/engines/spx_engine.py",  # Sprint 10.1b sibling-reorg (2026-05-11)
        "bot/engines/weather_engine.py",  # Sprint 10.1c sibling-reorg (2026-05-11)
        "bot/engines/sports_engine.py",  # Sprint 10.1d (2026-05-11)
        "bot/shadows/fifteenm_shadow.py",  # Sprint 10.2 (2026-05-11)
        "bot/shadows/hourly_alt_shadow.py",  # Sprint 10.2 (2026-05-11)
        "bot/shadows/spx_harrv_shadow.py",  # Sprint 10.2 (2026-05-11)
        "bot/infra/capital_allocator.py",  # Sprint 10.5a (2026-05-11)
    ]

    def test_all_critical_files_parse(self):
        failures = []
        for filename in self.CRITICAL_FILES:
            filepath = os.path.join(PROJECT_ROOT, filename)
            if not os.path.exists(filepath):
                continue
            with open(filepath) as f:
                source = f.read()
            try:
                ast.parse(source)
            except SyntaxError as e:
                failures.append(f"{filename}: {e}")

        assert not failures, f"Syntax errors: {failures}"
