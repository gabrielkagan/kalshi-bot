"""Sprint 14-A Bit X.5 — watchdog.py moved root → ops/.

The 2-min cron health monitor lived at repo root since the
pre-modularization era. Sprint 10.5c deferred relocation because
watchdog.py has __file__-derived load-bearing paths (STATE_FILE,
DB_PATH) + a cron CLI invocation that operators copy from the
Usage docstring. Sprint 14-A Bit X.5 closes the deferral.

After this Bit:
  - File lives at ops/watchdog.py (not repo root).
  - ops/ is a Python package (ops/__init__.py exists) so
    `import ops.watchdog` resolves cleanly.
  - STATE_FILE / DB_PATH still resolve to repo root via
    Path(__file__).parent.parent — keeps .watchdog_state.json and
    state.db co-located with the bot's state.db.
  - Usage docstring references ops/watchdog.py (operators copy
    this line into the VPS crontab).
  - 3 contract-test schema lists (test_db_signatures,
    test_product_type_enum, test_insert_schema_parity) reference
    'ops/watchdog.py' — these lists enforce schema-write lockstep
    and would silently no-op on a stale path.

VPS crontab edit is a post-merge operator action and is NOT
test-pinned (it lives in `~/`; not in-repo).
"""
from __future__ import annotations

import importlib
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ROOT_WATCHDOG = REPO_ROOT / "watchdog.py"
OPS_WATCHDOG = REPO_ROOT / "ops" / "watchdog.py"


def test_watchdog_relocated_to_ops():
    assert not ROOT_WATCHDOG.exists(), (
        f"watchdog.py must NOT exist at repo root after Bit X.5; "
        f"found {ROOT_WATCHDOG}"
    )
    assert OPS_WATCHDOG.exists(), (
        f"watchdog.py must exist at {OPS_WATCHDOG} after Bit X.5"
    )


def test_ops_is_a_package_for_import():
    assert (REPO_ROOT / "ops" / "__init__.py").exists(), (
        "ops/__init__.py is required for `import ops.watchdog` to resolve"
    )


def test_ops_watchdog_importable():
    mod = importlib.import_module("ops.watchdog")
    # Surface the symbols other tests + the cron entry-point rely on.
    assert hasattr(mod, "check_orphan_db_holders")
    assert hasattr(mod, "STATE_FILE")
    assert hasattr(mod, "DB_PATH")
    assert hasattr(mod, "main")


def test_watchdog_paths_resolve_to_repo_root():
    """STATE_FILE / DB_PATH must still resolve to repo root after the move.

    Path(__file__).parent inside the moved file is `ops/`; to keep
    co-location with state.db we need .parent.parent (the repo root).
    A silent regression here would point STATE_FILE at
    `ops/.watchdog_state.json` and DB_PATH at `ops/state.db` — wrong
    files, never written by the bot, would silently break loss-streak
    + balance checks.
    """
    mod = importlib.import_module("ops.watchdog")
    assert mod.STATE_FILE == REPO_ROOT / ".watchdog_state.json", (
        f"STATE_FILE drifted to {mod.STATE_FILE!r}; "
        f"must be repo-root .watchdog_state.json"
    )
    assert mod.DB_PATH == REPO_ROOT / "state.db", (
        f"DB_PATH drifted to {mod.DB_PATH!r}; "
        f"must be repo-root state.db"
    )


def test_usage_docstring_references_new_path():
    src = OPS_WATCHDOG.read_text(encoding="utf-8")
    assert "ops/watchdog.py" in src, (
        "Usage docstring (cron command) must reference 'ops/watchdog.py' "
        "post-move; operators copy this line into the VPS crontab"
    )
    # The pre-move literal `python3 watchdog.py` (no `ops/` prefix) must
    # NOT remain — it would mislead operators reading the docstring after
    # the relocation.
    assert not re.search(
        r"python3\s+watchdog\.py(?!\w)", src
    ), (
        "Stale 'python3 watchdog.py' must not remain in ops/watchdog.py"
    )


def _contract_list_path_check(test_file: Path):
    """Schema-list contract tests maintain string tuples of files that
    must move in lockstep on column writes. A bare 'watchdog.py' entry
    post-move would silently no-op since that path no longer exists."""
    text = test_file.read_text(encoding="utf-8")
    assert not re.search(r'^\s*"watchdog\.py",\s*$', text, re.MULTILINE), (
        f"Stale '\"watchdog.py\",' entry found in {test_file.name} — "
        f"must use '\"ops/watchdog.py\",'"
    )
    assert '"ops/watchdog.py"' in text, (
        f"{test_file.name} missing '\"ops/watchdog.py\"' entry"
    )


def test_db_signatures_contract_list_uses_new_path():
    _contract_list_path_check(
        REPO_ROOT / "tests" / "contracts" / "test_db_signatures.py"
    )


def test_product_type_enum_contract_list_uses_new_path():
    _contract_list_path_check(
        REPO_ROOT / "tests" / "integration" / "test_product_type_enum.py"
    )


def test_insert_schema_parity_contract_list_uses_new_path():
    _contract_list_path_check(
        REPO_ROOT / "tests" / "integration" / "test_insert_schema_parity.py"
    )
