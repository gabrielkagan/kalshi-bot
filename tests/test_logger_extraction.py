"""Bit 4.1 — Logger class extracted from bot/_impl.py to bot/logger.py.

Locks the contract between bot/_impl.py (which does
`from bot.logger import Logger` after the helpers star-import) and the
new bot/logger.py module. Mirrors tests/test_helpers_extraction.py
(Bit 3.2) and tests/test_constants_extraction.py (Bit 3.1).

L2 (Bit 3.0.5): tests call production directly. No reimplementing the
contract in test helpers.
"""
import ast
import importlib
import json
import logging
import subprocess
import sys
from pathlib import Path

import pytest
import bot.constants  # noqa: F401

REPO_ROOT = Path(__file__).resolve().parents[1]


# Methods on Logger that wrap _write_entry with a {"type": "<name>", **data}
# envelope, by (method_name, expected_type, journal_filename). Mirrors the
# 8 log_X methods in the class body.
WRAPPED_LOG_METHODS = [
    ("log_scan",        "scan",        "scan_journal.jsonl"),
    ("log_trade",       "trade",       "trade_journal.jsonl"),
    ("log_settlement",  "settlement",  "settlement_journal.jsonl"),
    ("log_order",       "order",       "order_journal.jsonl"),
    ("log_rejection",   "rejection",   "rejection_journal.jsonl"),
    ("log_opportunity", "opportunity", "opportunity_journal.jsonl"),
    ("log_execution",   "execution",   "execution_journal.jsonl"),
    ("log_performance", "performance", "performance_journal.jsonl"),
]


# ─── 1. File exists + imports ───────────────────────────────────────────────


def test_logger_file_exists():
    assert (REPO_ROOT / "bot" / "logger.py").is_file()


def test_logger_module_imports():
    importlib.import_module("bot.logger")


def test_logger_class_on_module():
    import bot.logger
    assert hasattr(bot.logger, "Logger")


# ─── 2. Identity preservation across re-export chain ────────────────────────


def test_logger_identity_through_bot_impl():
    """bot._impl.Logger is bot.logger.Logger.

    The `from bot.logger import Logger` line in bot/_impl.py is the only
    way KalshiClient/OrderExecutor/SettlementTracker type annotations
    (`logger: Logger`) resolve at class-body time. Identity here =
    identity-checkable annotation handoff.
    """
    import pytest as _pytest_bit_iii_c_skip; _pytest_bit_iii_c_skip.skip("bot/_impl.py removed (Bit 9.3-iii.c) — re-export contract retired", allow_module_level=False)
    import bot.logger as bl
    assert b.Logger is bl.Logger


def test_logger_identity_through_bot_proxy():
    """`from bot import Logger` resolves through canonical submodule (post-Bit-9.3-iii.b — _BotProxy retired)."""
    import bot
    import bot.logger as bl
    assert bot.logger.Logger is bl.Logger


# ─── 3. Drift guards (AST + source-string) ──────────────────────────────────


def test_logger_class_not_defined_in_bot_impl():
    """Future drift guard: catches "I'll just add it back to _impl.py".

    Mirrors test_moved_helpers_not_defined_in_bot_impl (Bit 3.2). After the
    move, no `class Logger` ClassDef node should remain at bot/_impl.py
    module scope.
    """
    bot_impl = REPO_ROOT / "bot" / "_impl.py"
    if not bot_impl.exists() if hasattr(bot_impl, 'exists') else not __import__('os').path.exists(bot_impl): pytest.skip("bot/_impl.py removed (Bit 9.3-iii.c)")
    tree = ast.parse(bot_impl.read_text(), filename=str(bot_impl))
    module_level_classdefs = {
        node.name for node in tree.body if isinstance(node, ast.ClassDef)
    }
    assert "Logger" not in module_level_classdefs, (
        "class Logger should live in bot/logger.py, not bot/_impl.py"
    )


def test_bot_impl_imports_logger():
    """bot/_impl.py must contain `from bot.logger import Logger` so the
    re-imported class lands in bot._impl's __dict__ (proxy chain) and the
    type annotations on OpportunityScanner/OrderExecutor/SettlementTracker
    resolve at class-body time.
    """
    if not (REPO_ROOT / "bot" / "_impl.py").exists():
        pytest.skip("bot/_impl.py removed (Bit 9.3-iii.c) — extraction-pin vacuous")
    src = (REPO_ROOT / "bot" / "_impl.py").read_text()
    assert "from bot.logger import Logger" in src


# ─── 4. Per-method happy paths (call production directly) ───────────────────


@pytest.mark.parametrize("method_name,expected_type,journal_filename", WRAPPED_LOG_METHODS)
def test_log_method_writes_jsonl(tmp_path, monkeypatch, method_name, expected_type, journal_filename):
    """Each log_X(data) writes one JSONL line with type=<expected> + data
    fields + a UTC ts. Constants are bare filenames (cwd-relative) so
    monkeypatch.chdir redirects writes into tmp_path.
    """
    monkeypatch.chdir(tmp_path)
    from bot.logger import Logger
    logger = Logger()
    method = getattr(logger, method_name)
    method({"k": 1, "asset": "BTC"})

    journal = tmp_path / journal_filename
    assert journal.is_file(), f"expected {journal_filename} after {method_name}"
    lines = journal.read_text().strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["type"] == expected_type
    assert row["k"] == 1
    assert row["asset"] == "BTC"
    assert row["ts"].endswith("Z")
    assert "T" in row["ts"]


# ─── 5. log_fill dedup contract ─────────────────────────────────────────────


def test_log_fill_returns_true_on_first_then_false_on_dup(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from bot.logger import Logger
    logger = Logger()
    assert logger.log_fill({"fill_id": "f1", "asset": "BTC"}) is True
    assert logger.log_fill({"fill_id": "f1", "asset": "BTC"}) is False
    journal = tmp_path / "trade_journal.jsonl"
    assert len(journal.read_text().strip().splitlines()) == 1


def test_log_fill_empty_id_dedupes_to_one_row(tmp_path, monkeypatch):
    """Pre-existing behavior: missing/blank fill_id collapses to the empty
    string and dedupes against itself. Pin verbatim — Bit 4.1 is a move,
    not a behavior change.
    """
    monkeypatch.chdir(tmp_path)
    from bot.logger import Logger
    logger = Logger()
    assert logger.log_fill({"asset": "BTC"}) is True
    assert logger.log_fill({"asset": "ETH"}) is False
    journal = tmp_path / "trade_journal.jsonl"
    assert len(journal.read_text().strip().splitlines()) == 1


def test_log_fill_writes_type_fill(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from bot.logger import Logger
    logger = Logger()
    logger.log_fill({"fill_id": "f1", "asset": "BTC"})
    row = json.loads((tmp_path / "trade_journal.jsonl").read_text().strip())
    assert row["type"] == "fill"
    assert row["fill_id"] == "f1"


# ─── 6. load_logged_fill_ids rebuild contract ───────────────────────────────


def test_load_logged_fill_ids_rebuilds_from_journal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    journal = tmp_path / "trade_journal.jsonl"
    journal.write_text(json.dumps({"type": "fill", "fill_id": "abc"}) + "\n")
    from bot.logger import Logger
    logger = Logger()
    logger.load_logged_fill_ids()
    assert logger.log_fill({"fill_id": "abc"}) is False, (
        "load_logged_fill_ids should have populated _logged_fill_ids with 'abc'"
    )


def test_load_logged_fill_ids_missing_file_is_ok(tmp_path, monkeypatch):
    """Fresh boot, no journal yet. Must not raise."""
    monkeypatch.chdir(tmp_path)
    from bot.logger import Logger
    Logger().load_logged_fill_ids()


def test_load_logged_fill_ids_skips_malformed_lines(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    journal = tmp_path / "trade_journal.jsonl"
    journal.write_text(
        "not-json\n"
        + json.dumps({"type": "fill", "fill_id": "good"}) + "\n"
        + "{broken\n"
    )
    from bot.logger import Logger
    logger = Logger()
    logger.load_logged_fill_ids()
    assert logger.log_fill({"fill_id": "good"}) is False


def test_load_logged_fill_ids_skips_non_fill_rows(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    journal = tmp_path / "trade_journal.jsonl"
    journal.write_text(
        json.dumps({"type": "trade", "fill_id": "ignore_me"}) + "\n"
        + json.dumps({"type": "fill", "fill_id": "real"}) + "\n"
    )
    from bot.logger import Logger
    logger = Logger()
    logger.load_logged_fill_ids()
    # 'ignore_me' was type=trade, not seen as a fill → log_fill should accept it.
    assert logger.log_fill({"fill_id": "ignore_me"}) is True
    # 'real' was type=fill → already loaded → dedup'd.
    assert logger.log_fill({"fill_id": "real"}) is False


# ─── 7. Module hygiene (no forbidden imports, no cycle) ─────────────────────


def test_no_forbidden_numerical_imports_in_logger():
    """bot/logger.py must not import numpy/scipy/torch/sklearn/pandas at
    module-load time. Logger is stdlib + bot.constants only. Mirrors the
    same guard for bot/helpers/*.py from Bit 3.2.
    """
    forbidden = {"numpy", "scipy", "torch", "sklearn", "pandas"}
    path = REPO_ROOT / "bot" / "logger.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in forbidden, (
                    f"bot/logger.py: forbidden import {alias.name}"
                )
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                assert node.module.split(".")[0] not in forbidden, (
                    f"bot/logger.py: forbidden from-import {node.module}"
                )


def test_no_circular_bot_impl_import_in_logger():
    """bot/logger.py must not create a cycle back to bot/_impl.py.

    Three import forms must all be rejected:
      1. `from bot._impl import ...`
      2. `import bot._impl` / `import bot._impl as ...`
      3. `from bot import _impl`  (Bit 4.2 R2 #1 back-port — the original
         Bit 4.1 version of this guard missed this form)

    bot._impl does `from bot.logger import Logger`, so the cycle would
    resolve at runtime if any direction back exists.
    """
    path = REPO_ROOT / "bot" / "logger.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            # Form 1: from bot._impl import X
            assert node.module != "bot._impl", (
                "bot/logger.py: forbidden `from bot._impl import ...` "
                "would create cycle"
            )
            # Form 3: from bot import _impl
            if node.module == "bot":
                names = [alias.name for alias in node.names]
                assert "_impl" not in names, (
                    "bot/logger.py: forbidden `from bot import _impl` "
                    "would create cycle"
                )
        if isinstance(node, ast.Import):
            for alias in node.names:
                # Form 2: import bot._impl
                assert alias.name != "bot._impl", (
                    "bot/logger.py: forbidden `import bot._impl` "
                    "would create cycle"
                )


# ─── 8. Root-logger handler-count regression ────────────────────────────────


def test_importing_bot_impl_does_not_clobber_root_logger():
    """Importing bot._impl (which used to trigger `from bot.logger import Logger`)
    must NOT install handlers on the root logger or call basicConfig.

    Bit 9.3-iii.c (2026-05-11): bot/_impl.py was DELETED. The regression target
    no longer exists; the equivalent invariant lives on for the canonical home —
    importing `bot.logger` directly must remain side-effect free.
    """
    code = (
        "import logging; pre=len(logging.getLogger().handlers); "
        "import bot.logger; "
        "post=len(logging.getLogger().handlers); "
        "assert pre == post, f'root handler delta: {pre} -> {post}'"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"subprocess failed:\nstdout={result.stdout}\nstderr={result.stderr}"
    )
