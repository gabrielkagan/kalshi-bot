"""Bit 7.1 — StateManager extracted from bot/_impl.py to bot/state.py (path-A++).

Bit 7.1 (2026-05-10):
  StateManager → bot/state.py
  (Sprint 7; first leaf at top-level bot/<file>.py — engines went into a
  subpackage, but the state layer is its own peer module per the master
  modularization plan.)

This bit is the largest single leaf-class extraction yet (~2,607 lines, 38
methods, 2 staticmethods). It is **NOT byte-for-byte path-A**: two `globals()`
call sites inside `__init__` (lines 615-616 of bot/_impl.py pre-extraction)
required intervention because they resolved to bot._impl's namespace which
holds the laundered constants surface (`from config import *` at line 47 +
`from bot.constants import *` at line 83). Path-A's resolution would have
been a `_bot_impl_globals()` wrapper preserving the smell. Path-A++ (the
shipped variant, user-authorized 2026-05-10) refactored
`scripts/cal_mlp/integration.py::parity_assert` and `sizing_parity_assert`
in-Bit to drop their `bot_globals` parameter entirely and import constants
directly. The closer, narrower, more LLM-readable signatures are the
primary win; the secondary win is that bot/state.py only late-binds a
single named function (compute_for_15m_main_path) instead of the whole
bot._impl namespace.

Related lessons:
  L32 (Plan-agent), L33 (wrong-class attribution), L38 (AST walk retargets),
  L39 (config vs bot.constants partition), L40 (@patch routing through
  _BotProxy), L41 (no hand-counted breadcrumbs), L54-L57 (Bit 6.3 path-B
  precedent).

Sister Bit 7.2 ships in lock-step: agent_docs/db_schema.md refresh.

Locks the contract between:
  - bot/_impl.py — does ``from bot.state import StateManager`` at line 109
    and has 3 consumer __init__ annotations (`OpportunityScanner`,
    `OrderExecutor`, `SettlementTracker`: ``state: StateManager``)
  - bot/state.py — owns the class body + top-imports
    ``compute_for_15m_main_path`` from clean-leaf ``bot.boot``
    (Bit 9.3-iii.a, 2026-05-11; the Bit 7.1
    ``_get_compute_for_15m_main_path()`` late-binding helper retired) +
    the ``from bot.engines import calibration as _cal_state`` alias used
    in the failure-path SLOW_BATCH_BREAKDOWN logger
  - bot/engines/calibration.py — keeps ``state: "StateManager"`` as a quoted
    forward-ref in `load_training_data_from_db`'s signature (cycle-avoidance)
  - scripts/cal_mlp/integration.py — refactored signatures: parity_assert(conn)
    -> tuple[str, int] AND sizing_parity_assert(conn, *, rowid, compute_for_15m_main_path)

Mirrors tests/contracts/test_engines_extraction.py (Bit 6.1/6.2/6.3),
tests/contracts/test_feeds_extraction.py (Bit 4.5a/4.5b),
tests/contracts/test_fetchers_extraction.py (Bit 4.4).
"""
from __future__ import annotations

import ast
import importlib
import inspect
import re
import sqlite3
import sys
from pathlib import Path
from typing import List, Tuple

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BOT_PY = REPO_ROOT / "bot" / "_impl.py"
STATE_PY = REPO_ROOT / "bot" / "state.py"
CALIBRATION_PY = REPO_ROOT / "bot" / "engines" / "calibration.py"
INTEGRATION_PY = REPO_ROOT / "scripts" / "cal_mlp" / "integration.py"
# Bit 7.1 fu1 (deploy fix): the schema baseline lives in tests/fixtures/ so CI
# runners (which have a fresh /tmp) can find it. Pre-deploy it was at
# /tmp/bit-7.1/schema-pre-move.txt — that path is preserved as a fallback
# for any local dev runs that pre-date the fixture move.
_FIXTURE_BASELINE = REPO_ROOT / "tests" / "fixtures" / "state_db_schema_baseline.txt"
_LEGACY_TMP_BASELINE = Path("/tmp/bit-7.1/schema-pre-move.txt")
SCHEMA_BASELINE_PATH = _FIXTURE_BASELINE if _FIXTURE_BASELINE.exists() else _LEGACY_TMP_BASELINE


# ------------------------------------------------------------------ Constants


# 38 methods (pre-extraction AST scan; verify post-extraction with:
#   grep -nE '^    def ' bot/state.py | wc -l
# ). The list itself is the ground truth — no separate count claim per L41.
STATE_METHODS = (
    "__init__",
    "_create_tables",
    "_asset_from_ticker",
    "_event_ticker_from_ticker",
    "reconcile_with_api",
    "_reconcile_positions",
    "_reconcile_orders",
    "set_bot_state_provider",
    "get_open_positions",
    "get_unsettled_positions",
    "get_resting_orders",
    "record_settlement",
    "_get_fresh_ob_ladder",
    "insert_order_lifecycle_snapshot",
    "insert_decision_snapshot",
    "append_decision_followup_tick",
    "prune_old_decision_snapshots",
    "_evict_stale_ob_cache",
    "insert_rejection",
    "get_unsettled_rejections",
    "mark_rejection_settled",
    "insert_evaluated_opportunity",
    "update_evaluated_opportunity_order",
    "insert_tm_sweep_shadow_row",
    "update_tm_sweep_shadow_on_settlement",
    "backfill_tm_sweep_with_97",
    "get_unsettled_evaluated_opportunities",
    "mark_evaluated_opportunity_settled",
    "insert_sol_pathc_shadow",
    "update_sol_pathc_observation",
    "update_sol_pathc_touch",
    "settle_sol_pathc_shadow",
    "get_pending_sol_pathc_shadows",
    "insert_bot_order",
    "confirm_order_submitted",
    "cleanup_expired_resting_orders",
    "mark_order_status",
    "record_position_from_fill",
    "update_garch_params",
    "update_egarch_params",
    "close",
)

STATE_STATIC_METHODS = ("_asset_from_ticker", "_event_ticker_from_ticker")

# L39 partition: 5 names from bot.constants. Verified pre-flight.
STATE_BOT_CONSTANTS = (
    "DB_PATH",
    "OB_CACHE_EVICT_AGE_SECONDS",
    "OB_CACHE_FRESHNESS_SECONDS",
    "SOL_RESCUE_CONTRACT_CAP",
    "STACKING_ENABLED",
)

STATE_DB_WRITER_REGISTRY_NAMES = ("recent_writes", "snapshot_active", "tracked_write")

# Strict ban — StateManager has zero numerical deps. Parallel to
# bot/engines/volatility.py + bot/engines/calibration.py.
STATE_FORBIDDEN_IMPORTS = (
    (STATE_PY, "bot/state.py", ("numpy", "scipy", "torch", "sklearn", "pandas")),
)


def _parse_state_baseline_tables() -> Tuple[str, ...]:
    """Parse /tmp/bit-7.1/schema-pre-move.txt for table names.
    Each table appears as `## <table_name> (N columns)`."""
    if not SCHEMA_BASELINE_PATH.exists():
        return ()
    text = SCHEMA_BASELINE_PATH.read_text()
    return tuple(re.findall(r"^## (\w+) \(\d+ columns\)$", text, flags=re.MULTILINE))


STATE_SCHEMA_TABLES = _parse_state_baseline_tables()


# ----------------------------------------------------------- File existence


def test_state_module_file_exists():
    """The bot/state.py module file must exist on disk (RED until Step 3 of
    plan)."""
    assert STATE_PY.is_file(), (
        f"{STATE_PY} not found — bot/state.py must be created in Step 3 of "
        f"the Bit 7.1 plan (kb/decisions/bit-7.1-plan-may10.md)."
    )


def test_state_module_imports_resolve():
    """Importing bot.state must not raise."""
    importlib.import_module("bot.state")


# ----------------------------------------------------------- Identity


def test_state_identity_through_bot_impl():
    """Post-Bit-9.3-iii.c (2026-05-11): bot/_impl.py was DELETED. The
    bot._impl.StateManager re-export no longer exists; the canonical home
    bot.state.StateManager is the only resolution path. Self-skip when
    bot/_impl.py is absent — preserved as a breadcrumb of the prior
    re-export contract.
    """
    import bot.state
    import importlib.util
    if importlib.util.find_spec("bot._impl") is None:
        pytest.skip("bot/_impl.py removed (Bit 9.3-iii.c) — re-export contract retired")
    import pytest as _pytest_bit_iii_c_skip; _pytest_bit_iii_c_skip.skip("bot/_impl.py removed (Bit 9.3-iii.c) — re-export contract retired", allow_module_level=False)

    assert bot._impl.StateManager is bot.state.StateManager


def test_state_identity_through_bot_proxy():
    """bot.state.StateManager (resolved via canonical submodule (post-Bit-9.3-iii.b — _BotProxy retired)) must be the same class object
    as bot.state.StateManager."""
    import bot
    import bot.state

    assert bot.state.StateManager is bot.state.StateManager


def test_state_module_attribute_post_extraction():
    """Class object's __module__ must be bot.state, not bot._impl —
    proof of where the class actually lives post-extraction."""
    import bot.state

    assert bot.state.StateManager.__module__ == "bot.state"


# ----------------------------------------------------------- Drift guards (AST)


@pytest.mark.parametrize("class_name", ["StateManager"])
def test_class_not_defined_in_bot_impl(class_name):
    """L38 negative pin — StateManager must NOT be defined as a module-level
    class in bot/_impl.py post-extraction. Vacuous post-Bit-9.3-iii.c
    deletion; self-skip when bot/_impl.py is absent."""
    if not BOT_PY.exists():
        pytest.skip("bot/_impl.py removed (Bit 9.3-iii.c) — negative pin vacuously true")
    if not BOT_PY.exists():
        pytest.skip("bot/_impl.py removed (Bit 9.3-iii.c) — extraction-pin vacuous")
    tree = ast.parse(BOT_PY.read_text())
    classdefs = [
        n.name for n in ast.iter_child_nodes(tree) if isinstance(n, ast.ClassDef)
    ]
    assert class_name not in classdefs, (
        f"{class_name} still defined in bot/_impl.py — Bit 7.1 extraction "
        f"requires the class body to live in bot/state.py only. The "
        f"line-109 re-export is the only mechanism for callers to reach it "
        f"via bot._impl.StateManager."
    )


def test_state_class_defined_at_module_scope():
    """Exactly one module-scope ClassDef in bot/state.py, named StateManager."""
    tree = ast.parse(STATE_PY.read_text())
    classdefs = [
        n.name for n in ast.iter_child_nodes(tree) if isinstance(n, ast.ClassDef)
    ]
    assert classdefs == ["StateManager"], (
        f"bot/state.py module-scope ClassDefs = {classdefs!r}; expected "
        f"exactly ['StateManager']."
    )


def test_bot_impl_imports_state_from_bot_state():
    """bot/_impl.py must have a top-level `from bot.state import StateManager`
    statement (the line-109 re-export contract)."""
    if not BOT_PY.exists():
        pytest.skip("bot/_impl.py removed (Bit 9.3-iii.c) — extraction-pin vacuous")
    src = BOT_PY.read_text()
    assert "from bot.state import StateManager" in src, (
        "bot/_impl.py missing the `from bot.state import StateManager` "
        "re-export — `bot._impl.StateManager` won't resolve, breaking ~50 "
        "test instantiation sites + the MainLoop construction."
    )


# ----------------------------------------------------------- Method presence


@pytest.mark.parametrize("method_name", STATE_METHODS)
def test_state_method_present(method_name):
    """All 38 StateManager methods enumerated in STATE_METHODS must survive
    the verbatim move."""
    import bot.state

    assert hasattr(bot.state.StateManager, method_name), (
        f"StateManager.{method_name} missing post-extraction — class body "
        f"was not transferred verbatim, OR a method was renamed/dropped."
    )


@pytest.mark.parametrize("static_method_name", STATE_STATIC_METHODS)
def test_state_static_methods_remain_static(static_method_name):
    """Both staticmethods (_asset_from_ticker, _event_ticker_from_ticker)
    must survive the move with their @staticmethod decorator preserved."""
    tree = ast.parse(STATE_PY.read_text())
    sm_class = next(
        n for n in ast.iter_child_nodes(tree)
        if isinstance(n, ast.ClassDef) and n.name == "StateManager"
    )
    method_def = next(
        (m for m in sm_class.body
         if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef))
         and m.name == static_method_name),
        None,
    )
    assert method_def is not None, (
        f"StateManager.{static_method_name} not found in bot/state.py AST."
    )
    decorators = [
        d.id for d in method_def.decorator_list if isinstance(d, ast.Name)
    ]
    assert "staticmethod" in decorators, (
        f"StateManager.{static_method_name} missing @staticmethod — "
        f"decorator was dropped during extraction."
    )


# ----------------------------------------------------------- Constants partition (L39)


@pytest.mark.parametrize("constant_name", STATE_BOT_CONSTANTS)
def test_state_constants_resolve_from_bot_constants(constant_name):
    """L39 partition pin: each of the 5 bot.constants names referenced by
    StateManager must be importable into bot.state's namespace via
    `from bot.constants import (...)` and resolve to the same object."""
    import bot.constants
    import bot.state

    bot_state_value = getattr(bot.state, constant_name, None)
    bot_constants_value = getattr(bot.constants, constant_name, None)
    assert bot_state_value is not None, (
        f"bot.state.{constant_name} missing — partition import of "
        f"{constant_name} from bot.constants didn't land."
    )
    assert bot_state_value is bot_constants_value, (
        f"bot.state.{constant_name} ({bot_state_value!r}) is NOT the same "
        f"object as bot.constants.{constant_name} ({bot_constants_value!r})."
    )


# ----------------------------------------------------------- parity_assert refactored signature (path-A++)


def test_parity_assert_signature_no_bot_globals():
    """Path-A++ refactor: parity_assert no longer accepts bot_globals.
    Signature is `parity_assert(conn) -> tuple[str, int]`."""
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "cal_mlp"))
    import integration

    sig = inspect.signature(integration.parity_assert)
    params = list(sig.parameters)
    assert params == ["conn"], (
        f"parity_assert.signature = {sig}; expected single 'conn' parameter "
        f"per the path-A++ refactor (kb/decisions/bit-7.1-plan-may10.md "
        f"PATH-A++ AMENDMENT). Got params={params!r}."
    )


def test_parity_assert_returns_status_rowid_tuple():
    """parity_assert(conn) returns (status: str, rowid: int).

    Sets up only the bot_startup_log table that parity_assert needs to
    INSERT into — `migrate_schema` is not called here because it ALTERs
    existing tables (state.db's full schema lives in
    StateManager._create_tables; testing parity_assert in isolation
    only requires the one table it touches)."""
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "cal_mlp"))
    import integration

    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute(
        "CREATE TABLE bot_startup_log ("
        "id INTEGER PRIMARY KEY, ts TEXT NOT NULL, "
        "parity_check_status TEXT, sizing_parity_status TEXT, pid INTEGER"
        ")"
    )
    result = integration.parity_assert(conn)
    assert isinstance(result, tuple) and len(result) == 2, (
        f"parity_assert(conn) = {result!r}; expected (status, rowid) tuple."
    )
    status, rowid = result
    assert status in ("passed", "failed"), f"unexpected status={status!r}"
    assert isinstance(rowid, int) and rowid > 0, f"rowid={rowid!r} not a positive int"
    conn.close()


def test_sizing_parity_assert_signature_explicit_kwargs():
    """Path-A++ refactor: sizing_parity_assert no longer accepts bot_globals.
    Signature is `sizing_parity_assert(conn, *, rowid, compute_for_15m_main_path)`."""
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "cal_mlp"))
    import integration

    sig = inspect.signature(integration.sizing_parity_assert)
    params = list(sig.parameters)
    assert params == ["conn", "rowid", "compute_for_15m_main_path"], (
        f"sizing_parity_assert.signature = {sig}; expected "
        f"(conn, *, rowid, compute_for_15m_main_path) per path-A++. "
        f"Got params={params!r}."
    )
    rowid_param = sig.parameters["rowid"]
    cfm_param = sig.parameters["compute_for_15m_main_path"]
    assert rowid_param.kind == inspect.Parameter.KEYWORD_ONLY, (
        f"rowid must be keyword-only; got kind={rowid_param.kind}"
    )
    assert cfm_param.kind == inspect.Parameter.KEYWORD_ONLY, (
        f"compute_for_15m_main_path must be keyword-only; got kind={cfm_param.kind}"
    )


def test_parity_assert_imports_from_bot_constants_and_config():
    """Path-A++ refactor: parity_assert function body must import from
    bot.constants and config directly (instead of reading via bot_globals)."""
    src = INTEGRATION_PY.read_text()
    tree = ast.parse(src)
    pa = next(
        n for n in ast.iter_child_nodes(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "parity_assert"
    )
    import_strs = []
    for node in ast.walk(pa):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            names = [a.name for a in node.names]
            import_strs.append(f"from {mod} import {', '.join(names)}")
    flat = "\n".join(import_strs)
    assert "from bot.constants import" in flat, (
        f"parity_assert body should `from bot.constants import ...` "
        f"directly per path-A++. Found imports inside the function:\n{flat}"
    )
    assert "from config import" in flat, (
        f"parity_assert body should `from config import ...` directly per "
        f"path-A++. Found imports inside the function:\n{flat}"
    )


def test_parity_assert_drawdown_halt_floor_fallback_preserved():
    """DRAWDOWN_HALT_FLOOR is in NEITHER bot.constants nor config.py at the
    time of Bit 7.1 ship. Pre-refactor used `bot_globals.get('DRAWDOWN_HALT_FLOOR', 0.10)`.
    Post-refactor must preserve the same 0.10 fallback semantics (no
    behavioral change)."""
    src = INTEGRATION_PY.read_text()
    assert "DRAWDOWN_HALT_FLOOR" in src and "0.10" in src, (
        "parity_assert must preserve DRAWDOWN_HALT_FLOOR=0.10 fallback "
        "semantics (the literal 0.10 is the canonical value when neither "
        "bot.constants nor config define it). Path-A++ refactor must keep "
        "behavior unchanged."
    )


# ----------------------------------------------------------- bot/state.py call-site pins (path-A++)


def test_state_no_late_binding_helper_post_bit_9_3_iii_a():
    """Bit 9.3-iii.a (2026-05-11) retired both `_get_compute_for_15m_main_path()`
    AND `_bot_impl_globals()`. The closure `compute_for_15m_main_path` relocated
    to clean-leaf bot/boot.py, and bot/state.py top-imports it directly — no
    late-binding needed because bot.boot has zero bot.state edges (no
    load-order cycle to avoid)."""
    tree = ast.parse(STATE_PY.read_text())
    fn_names = [
        n.name for n in ast.iter_child_nodes(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    assert "_get_compute_for_15m_main_path" not in fn_names, (
        "bot/state.py still defines _get_compute_for_15m_main_path() — Bit "
        "9.3-iii.a should have deleted the late-binding helper and use "
        "top-level `from bot.boot import compute_for_15m_main_path`."
    )
    assert "_bot_impl_globals" not in fn_names, (
        f"bot/state.py defines `def _bot_impl_globals` — path-A++ requires "
        f"the narrower `_get_compute_for_15m_main_path` instead. The whole-"
        f"namespace helper preserves the smell that path-A++ was authorized "
        f"to fix. Module-level fns: {fn_names}"
    )


def test_state_init_calls_parity_assert_with_conn_only():
    """In bot/state.py StateManager.__init__: parity_assert is called with
    self.conn ONLY (no bot_globals arg) and the (status, rowid) tuple is
    captured."""
    src = STATE_PY.read_text()
    pat = re.compile(r"_calmlp_parity_assert_impl\(\s*self\.conn\s*\)")
    assert pat.search(src), (
        "bot/state.py StateManager.__init__ must call "
        "_calmlp_parity_assert_impl(self.conn) — no bot_globals arg, no "
        "globals() arg. Path-A++ contract."
    )
    assert (re.search(r"=\s*_calmlp_parity_assert_impl\(self\.conn\)", src)), (
        "parity_assert's return tuple must be captured (status, rowid)."
    )


def test_state_init_calls_sizing_parity_assert_with_explicit_kwargs():
    """sizing_parity_assert is called with self.conn + rowid= + compute_for_15m_main_path=
    (no bot_globals, no globals())."""
    src = STATE_PY.read_text()
    pat = re.compile(
        r"_calmlp_sizing_parity_assert_impl\([^)]*rowid\s*=[^)]*compute_for_15m_main_path\s*=",
        flags=re.DOTALL,
    )
    assert pat.search(src), (
        "bot/state.py StateManager.__init__ must call "
        "_calmlp_sizing_parity_assert_impl(self.conn, rowid=..., "
        "compute_for_15m_main_path=compute_for_15m_main_path) — "
        "explicit kwargs per path-A++ (post-Bit-9.3-iii.a: the late-binding "
        "helper was retired; the callable is top-imported from bot.boot)."
    )


def test_state_no_top_level_bot_impl_import():
    """bot/state.py must NOT import bot._impl at top level.

    Pre-Bit-9.3-iii.a, the Bit 7.1 ``_get_compute_for_15m_main_path()`` helper
    was the only bot._impl edge (method-body late-binding to dodge the
    load-order cycle). Bit 9.3-iii.a (2026-05-11) relocated
    ``compute_for_15m_main_path`` to clean-leaf ``bot.boot`` — bot/state.py
    now top-imports the callable directly, has ZERO bot._impl edges at any
    scope, and the helper is retired. This test stays as the original
    top-level pin; the stronger ``test_state_has_zero_bot_impl_edges`` in
    tests/integration/test_bit_9_3_iii_a_boot_relocation.py covers the full zero-edge claim.
    """
    src = STATE_PY.read_text()
    tree = ast.parse(src)
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module != "bot._impl", (
                f"bot/state.py has top-level `from bot._impl import {node.names}` "
                f"— forbidden. Only method-body late-binding is allowed."
            )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "bot._impl", (
                    f"bot/state.py has top-level `import bot._impl` — "
                    f"forbidden. Only method-body late-binding is allowed."
                )


# ----------------------------------------------------------- make_compute_for_15m_main_path no-arg signature (Bit 7.1 fu / Smell 4)


def test_make_compute_for_15m_main_path_signature_no_args():
    """Smell 4 (Bit 7.1 follow-up) drops the `bot_globals: dict` parameter
    from `make_compute_for_15m_main_path`. The closure now imports its
    11 dependent names from `bot.constants` + `config` inside the function
    body (mirroring Bit 7.1 path-A++ `parity_assert`/`sizing_parity_assert`),
    plus a literal `DRAWDOWN_HALT_FLOOR = 0.10` fallback for the one name
    not in either source.

    Pinning the new no-arg signature locks the laundered-namespace
    coupling shut: a future regression that re-introduces a
    `bot_globals: dict` parameter would fail this test."""
    sys.path.insert(0, str(REPO_ROOT / "scripts" / "cal_mlp"))
    import integration

    sig = inspect.signature(integration.make_compute_for_15m_main_path)
    params = list(sig.parameters)
    assert params == [], (
        f"make_compute_for_15m_main_path.signature = {sig}; Smell 4 "
        f"refactor requires zero parameters. The closure must import "
        f"its dependent names directly inside the function body — not "
        f"accept a globals dict from the caller. Got params={params!r}."
    )


# ----------------------------------------------------------- L33 consumer-init pins


def _walk_class_init_annotations(cls_def: ast.ClassDef) -> List[Tuple[str, str]]:
    """Helper: enumerate (param_name, annotation_repr) for the class's __init__."""
    init = next(
        (m for m in cls_def.body
         if isinstance(m, ast.FunctionDef) and m.name == "__init__"),
        None,
    )
    if init is None:
        return []
    return [
        (a.arg, ast.unparse(a.annotation) if a.annotation else "")
        for a in init.args.args
    ]


# Bit 8.1 (2026-05-10): OpportunityScanner moved to bot/scanner/__init__.py.
# Walk both files when looking for consumer-class annotations.
SCANNER_PY = REPO_ROOT / "bot" / "scanner" / "__init__.py"


def _consumer_classdef(class_name):
    """Find a ClassDef by name, searching bot/_impl.py + bot/scanner/__init__.py
    + bot/executor.py + bot/settlement.py + bot/main_loop.py (Scanner moved
    out per Bit 8.1; OrderExecutor per Bit 9.1; SettlementTracker per Bit 9.2;
    MainLoop per Bit 9.3 — quintuple-walk as of 2026-05-10)."""
    EXECUTOR_PY = REPO_ROOT / "bot" / "executor.py"
    SETTLEMENT_PY = REPO_ROOT / "bot" / "settlement.py"
    MAIN_LOOP_PY = REPO_ROOT / "bot" / "main_loop.py"
    for path in (BOT_PY, SCANNER_PY, EXECUTOR_PY, SETTLEMENT_PY, MAIN_LOOP_PY):
        if not path.is_file():
            continue
        tree = ast.parse(path.read_text())
        for c in ast.iter_child_nodes(tree):
            if isinstance(c, ast.ClassDef) and c.name == class_name:
                return c, path
    return None, None


@pytest.mark.parametrize(
    "class_name",
    ["OpportunityScanner", "OrderExecutor", "SettlementTracker"],
)
def test_consumer_class_annotates_state_manager_in_bot_impl(class_name):
    """L33 positive pin: each of the 3 consumer classes' __init__ has the
    `state: StateManager` annotation (bare, not quoted forward ref)."""
    consumer, found_in = _consumer_classdef(class_name)
    assert consumer is not None, f"{class_name} missing from bot/_impl.py + bot/scanner/__init__.py"
    annotations = dict(_walk_class_init_annotations(consumer))
    assert annotations.get("state") == "StateManager", (
        f"{class_name}.__init__ has `state: {annotations.get('state')!r}` "
        f"(in {found_in}); expected `state: StateManager` (bare). The "
        f"line-114 re-export makes the unquoted name resolve to bot.state.StateManager."
    )


def test_only_three_consumers_annotate_state_manager():
    """L33 negative pin: exactly 3 module-level classes (across bot/_impl.py +
    bot/scanner/__init__.py + bot/executor.py + bot/settlement.py) annotate
    `state: StateManager`. Catches accidental drift if a new consumer is
    added without explicit knowledge.

    Bit 9.1 (2026-05-10) extended walk to bot/executor.py — OrderExecutor moved
    out of bot/_impl.py. Bit 9.2 (2026-05-10) extended walk to bot/settlement.py
    — SettlementTracker moved out. Bit 9.3 (2026-05-10) extended walk to
    bot/main_loop.py — MainLoop moved out. (Note: MainLoop does NOT annotate
    state, so the walk extension is L86/L90 contagion-seal hygiene rather
    than load-bearing — but if a future maintainer adds a state-annotated
    method to MainLoop, this walk surfaces it.)"""
    EXECUTOR_PY = REPO_ROOT / "bot" / "executor.py"
    SETTLEMENT_PY = REPO_ROOT / "bot" / "settlement.py"
    MAIN_LOOP_PY = REPO_ROOT / "bot" / "main_loop.py"
    matches = []
    for path in (BOT_PY, SCANNER_PY, EXECUTOR_PY, SETTLEMENT_PY, MAIN_LOOP_PY):
        if not path.is_file():
            continue
        tree = ast.parse(path.read_text())
        for c in ast.iter_child_nodes(tree):
            if not isinstance(c, ast.ClassDef):
                continue
            annotations = dict(_walk_class_init_annotations(c))
            if annotations.get("state") == "StateManager":
                matches.append(c.name)
    assert sorted(matches) == sorted(
        ["OpportunityScanner", "OrderExecutor", "SettlementTracker"]
    ), (
        f"Classes annotating `state: StateManager`: {sorted(matches)}; "
        f"expected exactly OpportunityScanner / OrderExecutor / "
        f"SettlementTracker. Drift suggests a new consumer needs review."
    )


def test_calibration_engine_keeps_state_manager_forward_ref_quoted():
    """bot/engines/calibration.py keeps `state: "StateManager"` as a quoted
    forward-ref in load_training_data_from_db's signature. Cycle avoidance:
    importing StateManager into bot/engines/calibration.py would loop
    bot/state.py → bot/engines/calibration.py (via _cal_state) → back."""
    src = CALIBRATION_PY.read_text()
    assert 'state: "StateManager"' in src, (
        "bot/engines/calibration.py must keep `state: \"StateManager\"` "
        "quoted forward-ref. Importing StateManager into calibration.py "
        "would create a circular import."
    )


# ----------------------------------------------------------- _cal_state alias usage


def test_state_uses_cal_state_alias():
    """bot/state.py top-level: `from bot.engines import calibration as _cal_state`."""
    src = STATE_PY.read_text()
    assert "from bot.engines import calibration as _cal_state" in src, (
        "bot/state.py missing the path-B `_cal_state` alias. "
        "StateManager's failure-path SLOW_BATCH_BREAKDOWN logger "
        "references _cal_state._CALIBRATION_ENGINE and "
        "_cal_state._resolve_cal_engine."
    )


def test_state_uses_cal_state_attribute_access():
    """The 3 in-class _cal_state references (_CALIBRATION_ENGINE × 2,
    _resolve_cal_engine × 1) survive the move."""
    src = STATE_PY.read_text()
    assert "_cal_state._CALIBRATION_ENGINE" in src, (
        "Missing _cal_state._CALIBRATION_ENGINE reference — failure-path "
        "logger drift."
    )
    assert "_cal_state._resolve_cal_engine" in src, (
        "Missing _cal_state._resolve_cal_engine reference — failure-path "
        "logger drift."
    )


# ----------------------------------------------------------- recent_writes/tracked_write instrumentation


def test_state_imports_db_writer_registry():
    """bot/state.py must import the 3 db_writer_registry names — load-bearing
    for the cf34b5c db-locked instrumentation surface."""
    src = STATE_PY.read_text()
    assert "from bot.db_writer_registry import" in src, (
        "bot/state.py must `from bot.db_writer_registry import "
        "tracked_write, snapshot_active, recent_writes` — the cf34b5c "
        "(May 9 2026) instrumentation contract requires these 3 names."
    )
    for name in STATE_DB_WRITER_REGISTRY_NAMES:
        assert re.search(rf"\b{name}\b", src), (
            f"bot/state.py missing reference to `{name}` — "
            f"db_writer_registry instrumentation surface incomplete."
        )


def test_state_uses_tracked_write_in_insert_evaluated_opportunity():
    """The `with tracked_write("state_manager", "insert_evaluated_opportunity"):`
    context manager is preserved in bot/state.py."""
    src = STATE_PY.read_text()
    assert 'tracked_write("state_manager", "insert_evaluated_opportunity")' in src, (
        "Missing `with tracked_write(\"state_manager\", "
        "\"insert_evaluated_opportunity\"):` — load-bearing per cf34b5c. "
        "SLOW_BATCH_BREAKDOWN logging depends on this surface."
    )


def test_state_uses_recent_writes_for_slow_batch_breakdown():
    """The `recent_writes(2.0)` call in the SLOW_BATCH_BREAKDOWN failure
    formatter is preserved in bot/state.py."""
    src = STATE_PY.read_text()
    assert "recent_writes(2.0)" in src, (
        "Missing `recent_writes(2.0)` call — load-bearing per cf34b5c "
        "SLOW_BATCH_BREAKDOWN diagnostic."
    )


# ----------------------------------------------------------- Forbidden imports


@pytest.mark.parametrize("module_path,friendly,forbidden", STATE_FORBIDDEN_IMPORTS)
def test_state_no_forbidden_numerical_imports(module_path, friendly, forbidden):
    """Strict ban: bot/state.py imports zero of numpy/scipy/torch/sklearn/pandas.
    StateManager is pure stdlib + sqlite3 + bot.constants.

    Parallel to bot/engines/volatility.py + bot/engines/calibration.py which
    are also strict-ban (see ENGINE_FORBIDDEN_IMPORTS in
    tests/contracts/test_engines_extraction.py)."""
    src = module_path.read_text()
    for name in forbidden:
        assert f"import {name}" not in src, (
            f"{friendly} imports {name} — forbidden per "
            f"STATE_FORBIDDEN_IMPORTS strict ban. The OpenBLAS-thread-cache "
            f"contention guard "
            f"(kb/failures/cal-mlp-torch-thread-contention-apr29.md) "
            f"requires StateManager to use only stdlib + sqlite3."
        )
        assert f"from {name}" not in src, (
            f"{friendly} uses `from {name} import ...` — forbidden per "
            f"the per-module allow-list strict-ban policy."
        )


# ----------------------------------------------------------- Schema zero-delta (HIGH-RISK acceptance criterion)


def test_schema_baseline_present():
    """The baseline at /tmp/bit-7.1/schema-pre-move.txt must exist.
    Captured pre-extraction in Task #2 of the Bit 7.1 plan."""
    assert SCHEMA_BASELINE_PATH.exists(), (
        f"Schema baseline not found at {SCHEMA_BASELINE_PATH}. Re-run "
        f"Task #2 of the Bit 7.1 plan (kb/decisions/bit-7.1-plan-may10.md)."
    )


def _dump_pragma_table_info(conn: sqlite3.Connection, table: str) -> List[Tuple]:
    """Mirror the baseline's pragma table_info dump shape."""
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    return [tuple(row) for row in cur.fetchall()]


def _baseline_table_columns(table: str) -> List[Tuple]:
    """Parse one table's column rows out of /tmp/bit-7.1/schema-pre-move.txt."""
    if not SCHEMA_BASELINE_PATH.exists():
        return []
    text = SCHEMA_BASELINE_PATH.read_text()
    pat = re.compile(
        rf"^## {re.escape(table)} \(\d+ columns\)$\n((?:  cid=.*\n)+)",
        flags=re.MULTILINE,
    )
    m = pat.search(text)
    if not m:
        return []
    rows = []
    # rstrip("\n") instead of strip() so the leading 2-space indent of the
    # first cid line survives — it's part of the per-line format, not block
    # whitespace.
    for line in m.group(1).rstrip("\n").split("\n"):
        # type field can be empty (SQLite system tables like sqlite_sequence
        # have untyped columns) — use .*? non-greedy capture instead of \S+
        # for both name and type to tolerate the empty-type case.
        cm = re.match(
            r"^\s*cid=\s*(\d+)\s+name=(.*?)\s+type=(.*?)\s+notnull=(\d+)\s+pk=(\d+)\s+dflt=(.+)$",
            line,
        )
        if not cm:
            continue
        cid = int(cm.group(1))
        name = cm.group(2).strip()
        ctype = cm.group(3).strip()
        notnull = int(cm.group(4))
        pk = int(cm.group(5))
        dflt_repr = cm.group(6).strip()
        if dflt_repr == "None":
            dflt = None
        else:
            dflt = dflt_repr[1:-1] if dflt_repr.startswith(("'", '"')) else dflt_repr
        rows.append((cid, name, ctype, notnull, dflt, pk))
    return rows


@pytest.mark.parametrize("table_name", STATE_SCHEMA_TABLES or ["__skip__"])
def test_schema_zero_delta_per_table(table_name):
    """ACCEPTANCE CRITERION: post-extraction StateManager(":memory:") must
    produce byte-identical pragma table_info as /tmp/bit-7.1/schema-pre-move.txt
    for every one of the 17 tables. Any delta is a red-stop blocker."""
    if table_name == "__skip__":
        pytest.skip(
            "Schema baseline missing — run Task #2 of Bit 7.1 plan to "
            "generate /tmp/bit-7.1/schema-pre-move.txt first."
        )
    import bot.state

    sm = bot.state.StateManager(":memory:")
    try:
        actual = _dump_pragma_table_info(sm.conn, table_name)
        expected = _baseline_table_columns(table_name)
        assert expected, f"Failed to parse baseline for table {table_name}"
        assert len(actual) == len(expected), (
            f"Schema delta in {table_name}: column count "
            f"{len(actual)} (post) vs {len(expected)} (baseline). "
            f"Acceptance criterion violated."
        )
        for a, e in zip(actual, expected):
            assert a[1] == e[1], (
                f"{table_name}: column name drift cid={a[0]}: "
                f"actual={a[1]!r} vs baseline={e[1]!r}"
            )
            assert a[2] == e[2], (
                f"{table_name}.{a[1]}: type drift "
                f"actual={a[2]!r} vs baseline={e[2]!r}"
            )
            assert a[3] == e[3], (
                f"{table_name}.{a[1]}: notnull drift "
                f"actual={a[3]} vs baseline={e[3]}"
            )
            assert a[5] == e[5], (
                f"{table_name}.{a[1]}: pk drift "
                f"actual={a[5]} vs baseline={e[5]}"
            )
    finally:
        sm.close()


# ----------------------------------------------------------- Behavioral smoke


def test_state_manager_instantiates_with_in_memory_db():
    """Smoke: `bot.state.StateManager(":memory:")` doesn't raise SystemExit
    or KeyError. Catches any post-extraction parity_assert/sizing_parity_assert
    regression (the smoking gun for path-A++ refactor going wrong)."""
    import bot.state

    sm = bot.state.StateManager(":memory:")
    try:
        assert sm.conn is not None
    finally:
        sm.close()


def test_state_manager_instantiation_writes_bot_startup_log_row():
    """parity_assert + sizing_parity_assert must write ONE row to
    bot_startup_log with both parity_check_status and sizing_parity_status
    populated. Verifies the explicit-rowid contract in path-A++."""
    import bot.state

    sm = bot.state.StateManager(":memory:")
    try:
        cur = sm.conn.cursor()
        cur.execute(
            "SELECT parity_check_status, sizing_parity_status "
            "FROM bot_startup_log ORDER BY id DESC LIMIT 1"
        )
        row = cur.fetchone()
        assert row is not None, (
            "bot_startup_log has no rows post-construction — parity_assert "
            "didn't insert. Path-A++ refactor regression."
        )
        parity_status, sizing_status = row
        assert parity_status == "passed", (
            f"parity_check_status={parity_status!r}; expected 'passed'."
        )
        assert sizing_status == "passed", (
            f"sizing_parity_status={sizing_status!r}; expected 'passed'."
        )
    finally:
        sm.close()
