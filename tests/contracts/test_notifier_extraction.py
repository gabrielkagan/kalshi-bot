"""Bit 4.2 — TelegramNotifier class extracted from bot/_impl.py to bot/notifier.py.

Locks the contract between bot/_impl.py (which does
`from bot.notifier import TelegramNotifier` after `from bot.logger import Logger`)
and the new bot/notifier.py module. Mirrors tests/contracts/test_logger_extraction.py
(Bit 4.1).

L2 (Bit 3.0.5): tests call production directly. No reimplementing the
contract in test helpers.

L29 (Bit 4.1): doc-drift in agent_docs/bot_layout.md ships in the same
atomic commit (regen via the recipe in the doc preamble).

R2 #1 fix (Bit 4.1): the cycle-guard now also rejects the
`from bot import _impl` form, which test_logger_extraction.py missed.
"""
import ast
import importlib
import logging
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import bot.helpers  # noqa: F401

REPO_ROOT = Path(__file__).resolve().parents[2]


# ─── 1. File exists + imports ───────────────────────────────────────────────


def test_notifier_file_exists():
    assert (REPO_ROOT / "bot" / "notifier.py").is_file()


def test_notifier_module_imports():
    importlib.import_module("bot.notifier")


def test_notifier_class_on_module():
    import bot.notifier
    assert hasattr(bot.notifier, "TelegramNotifier")


# ─── 2. Identity preservation across re-export chain ────────────────────────


def test_notifier_identity_through_bot_impl():
    """bot._impl.TelegramNotifier is bot.notifier.TelegramNotifier.

    The `from bot.notifier import TelegramNotifier` line in bot/_impl.py is
    what makes the runtime construction in `MainLoop.__init__` (search
    `self.telegram = TelegramNotifier` for the current line) resolve.
    """
    import pytest as _pytest_bit_iii_c_skip; _pytest_bit_iii_c_skip.skip("bot/_impl.py removed (Bit 9.3-iii.c) — re-export contract retired", allow_module_level=False)
    import bot.notifier as bn
    assert b.TelegramNotifier is bn.TelegramNotifier


def test_notifier_identity_through_bot_proxy():
    """`bot.notifier.TelegramNotifier` resolves through canonical submodule (post-Bit-9.3-iii.b — _BotProxy retired)."""
    import bot
    import bot.notifier as bn
    assert bot.notifier.TelegramNotifier is bn.TelegramNotifier


# ─── 3. Drift guards (AST + source-string) ──────────────────────────────────


def test_telegram_notifier_class_not_defined_in_bot_impl():
    """Future drift guard: catches "I'll just add it back to _impl.py".

    Mirrors test_logger_class_not_defined_in_bot_impl (Bit 4.1). After the
    move, no `class TelegramNotifier` ClassDef node should remain at
    bot/_impl.py module scope.
    """
    bot_impl = REPO_ROOT / "bot" / "_impl.py"
    if not bot_impl.exists() if hasattr(bot_impl, 'exists') else not __import__('os').path.exists(bot_impl): pytest.skip("bot/_impl.py removed (Bit 9.3-iii.c)")
    tree = ast.parse(bot_impl.read_text(), filename=str(bot_impl))
    module_level_classdefs = {
        node.name for node in tree.body if isinstance(node, ast.ClassDef)
    }
    assert "TelegramNotifier" not in module_level_classdefs, (
        "class TelegramNotifier should live in bot/notifier.py, not bot/_impl.py"
    )


def test_bot_impl_imports_telegram_notifier():
    """bot/_impl.py must contain `from bot.notifier import TelegramNotifier` so
    the re-imported class lands in bot._impl's __dict__ (proxy chain) and the
    runtime construction in `MainLoop.__init__` (search
    `self.telegram = TelegramNotifier` for the current line) resolves.
    """
    if not (REPO_ROOT / "bot" / "_impl.py").exists():
        pytest.skip("bot/_impl.py removed (Bit 9.3-iii.c) — extraction-pin vacuous")
    src = (REPO_ROOT / "bot" / "_impl.py").read_text()
    assert "from bot.notifier import TelegramNotifier" in src


# ─── 4. __init__ behavior pins ──────────────────────────────────────────────


def test_init_disabled_when_token_missing():
    from bot.notifier import TelegramNotifier
    n = TelegramNotifier("", "chat-id-x")
    assert n.enabled is False


def test_init_disabled_when_chat_missing():
    from bot.notifier import TelegramNotifier
    n = TelegramNotifier("tok-x", "")
    assert n.enabled is False


def test_init_enabled_when_both_present():
    from bot.notifier import TelegramNotifier
    n = TelegramNotifier("tok-x", "chat-id-x")
    assert n.enabled is True


def test_init_dedup_starts_empty():
    from bot.notifier import TelegramNotifier
    n = TelegramNotifier("tok-x", "chat-id-x")
    assert n._dedup == {}


# ─── 5. send() dedup contract ───────────────────────────────────────────────


def test_send_disabled_short_circuits_no_thread_spawn():
    """When .enabled is False, send() returns before spawning a thread.
    Patch threading.Thread; assert never called.
    """
    from bot.notifier import TelegramNotifier
    n = TelegramNotifier("", "")  # disabled
    with patch("bot.notifier.threading.Thread") as mock_thread:
        n.send("hello", silent=False, dedup_key="k1")
    mock_thread.assert_not_called()


def test_send_dedups_within_60s_window():
    """Same dedup_key twice within 60s → only one thread spawn."""
    from bot.notifier import TelegramNotifier
    n = TelegramNotifier("tok", "chat")
    with patch("bot.notifier.threading.Thread") as mock_thread, \
         patch("bot.notifier.time.time", side_effect=[100.0, 130.0]):
        n.send("first", dedup_key="k1")
        n.send("second", dedup_key="k1")  # 30s later, within 60s window
    assert mock_thread.call_count == 1


def test_send_redelivers_after_60s_window():
    """Same dedup_key after 61s → two thread spawns."""
    from bot.notifier import TelegramNotifier
    n = TelegramNotifier("tok", "chat")
    with patch("bot.notifier.threading.Thread") as mock_thread, \
         patch("bot.notifier.time.time", side_effect=[100.0, 161.0]):
        n.send("first", dedup_key="k1")
        n.send("second", dedup_key="k1")  # 61s later, outside window
    assert mock_thread.call_count == 2


def test_send_no_dedup_key_always_delivers():
    """Two calls with dedup_key=None → two thread spawns (no dedup)."""
    from bot.notifier import TelegramNotifier
    n = TelegramNotifier("tok", "chat")
    with patch("bot.notifier.threading.Thread") as mock_thread:
        n.send("a")
        n.send("b")
    assert mock_thread.call_count == 2


def test_send_different_dedup_keys_both_deliver():
    """dedup is per-key, not global."""
    from bot.notifier import TelegramNotifier
    n = TelegramNotifier("tok", "chat")
    with patch("bot.notifier.threading.Thread") as mock_thread, \
         patch("bot.notifier.time.time", return_value=100.0):
        n.send("a", dedup_key="k1")
        n.send("b", dedup_key="k2")
    assert mock_thread.call_count == 2


# ─── 6. send() truncation + threading contract ──────────────────────────────


def test_send_truncates_to_4096_chars():
    """Messages over 4096 chars are truncated. Capture via patched Thread."""
    from bot.notifier import TelegramNotifier
    n = TelegramNotifier("tok", "chat")
    long_msg = "x" * 5000
    with patch("bot.notifier.threading.Thread") as mock_thread:
        n.send(long_msg)
    # Thread(target=self._post, args=(text, silent), daemon=True)
    call_kwargs = mock_thread.call_args.kwargs
    text_arg = call_kwargs["args"][0]
    assert len(text_arg) == 4096
    assert text_arg == "x" * 4096


def test_send_short_message_not_truncated():
    from bot.notifier import TelegramNotifier
    n = TelegramNotifier("tok", "chat")
    with patch("bot.notifier.threading.Thread") as mock_thread:
        n.send("hello world")
    text_arg = mock_thread.call_args.kwargs["args"][0]
    assert text_arg == "hello world"


def test_send_spawns_daemon_thread():
    """Thread must be daemon=True (process-exit semantics)."""
    from bot.notifier import TelegramNotifier
    n = TelegramNotifier("tok", "chat")
    with patch("bot.notifier.threading.Thread") as mock_thread:
        n.send("hi")
    assert mock_thread.call_args.kwargs["daemon"] is True


def test_send_thread_targets_post():
    """Thread.target must be self._post."""
    from bot.notifier import TelegramNotifier
    n = TelegramNotifier("tok", "chat")
    with patch("bot.notifier.threading.Thread") as mock_thread:
        n.send("hi")
    assert mock_thread.call_args.kwargs["target"] == n._post


def test_send_passes_silent_flag_to_post():
    from bot.notifier import TelegramNotifier
    n = TelegramNotifier("tok", "chat")
    with patch("bot.notifier.threading.Thread") as mock_thread:
        n.send("hi", silent=True)
    args = mock_thread.call_args.kwargs["args"]
    assert args[1] is True


# ─── 7. _post() HTTP contract ───────────────────────────────────────────────


def test_post_uses_markdown_and_timeout():
    """_post issues requests.post with parse_mode=Markdown, timeout=5,
    chat_id from constructor, and disable_notification=silent.
    """
    from bot.notifier import TelegramNotifier
    n = TelegramNotifier("tok-z", "chat-z")
    with patch("bot.notifier.requests.post") as mock_post:
        n._post("body text", silent=True)
    mock_post.assert_called_once()
    call_args = mock_post.call_args
    assert call_args.args[0] == "https://api.telegram.org/bottok-z/sendMessage"
    payload = call_args.kwargs["json"]
    assert payload["chat_id"] == "chat-z"
    assert payload["text"] == "body text"
    assert payload["parse_mode"] == "Markdown"
    assert payload["disable_notification"] is True
    assert call_args.kwargs["timeout"] == 5


def test_post_swallows_request_exceptions(caplog):
    """_post must swallow any exception (including network failures) and
    log a warning. Exception MUST NOT propagate to caller.
    """
    from bot.notifier import TelegramNotifier
    n = TelegramNotifier("tok", "chat")
    with patch("bot.notifier.requests.post", side_effect=RuntimeError("boom")), \
         caplog.at_level(logging.WARNING):
        n._post("text", silent=False)  # must not raise
    assert any("Telegram send failed" in rec.message for rec in caplog.records)


def test_url_built_from_token():
    from bot.notifier import TelegramNotifier
    n = TelegramNotifier("ABCDE:fake", "12345")
    assert n._url == "https://api.telegram.org/botABCDE:fake/sendMessage"


# ─── 8. Module hygiene (no forbidden imports, no cycle) ─────────────────────


def test_no_forbidden_numerical_imports_in_notifier():
    """bot/notifier.py must not import numpy/scipy/torch/sklearn/pandas at
    module-load time. Notifier is stdlib + third-party `requests` only.
    Mirrors the same guard for bot/logger.py (Bit 4.1) and bot/helpers/*.py
    (Bit 3.2).
    """
    forbidden = {"numpy", "scipy", "torch", "sklearn", "pandas"}
    path = REPO_ROOT / "bot" / "notifier.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name.split(".")[0] not in forbidden, (
                    f"bot/notifier.py: forbidden import {alias.name}"
                )
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                assert node.module.split(".")[0] not in forbidden, (
                    f"bot/notifier.py: forbidden from-import {node.module}"
                )


def test_no_circular_bot_impl_import_in_notifier():
    """bot/notifier.py must not create a cycle back to bot/_impl.py.

    Three import forms must all be rejected:
      1. `from bot._impl import ...`
      2. `import bot._impl` / `import bot._impl as ...`
      3. `from bot import _impl`  (Bit 4.1 R2 #1 hole — fix here per
         Bit 4.2 plan §5)

    bot._impl does `from bot.notifier import TelegramNotifier`, so the cycle
    would resolve at runtime if any direction back exists.
    """
    path = REPO_ROOT / "bot" / "notifier.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            # Form 1: from bot._impl import X
            assert node.module != "bot._impl", (
                "bot/notifier.py: forbidden `from bot._impl import ...` "
                "would create cycle"
            )
            # Form 3: from bot import _impl
            if node.module == "bot":
                names = [alias.name for alias in node.names]
                assert "_impl" not in names, (
                    "bot/notifier.py: forbidden `from bot import _impl` "
                    "would create cycle"
                )
        if isinstance(node, ast.Import):
            for alias in node.names:
                # Form 2: import bot._impl
                assert alias.name != "bot._impl", (
                    "bot/notifier.py: forbidden `import bot._impl` "
                    "would create cycle"
                )


def test_notifier_does_not_import_bot_helpers():
    """bot/notifier.py is a pure leaf (per Bit 4.2 plan §1): no helper deps.
    Prevents drift toward 'just add a small helper call here'.
    """
    path = REPO_ROOT / "bot" / "notifier.py"
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            assert not node.module.startswith("bot.helpers"), (
                f"bot/notifier.py: unexpected helper import {node.module}"
            )


# ─── 9. Root-logger handler-count regression ────────────────────────────────


def test_importing_bot_notifier_does_not_clobber_root_logger():
    """Importing bot.notifier must NOT install handlers on the root logger or
    call basicConfig. Subprocess isolation — root-logger state in the parent
    pytest worker is already tainted by other test imports.

    Mirrors test_importing_bot_impl_does_not_clobber_root_logger (Bit 4.1)
    but targets bot.notifier directly so we catch a regression even if
    bot._impl's handler-count check passes.
    """
    code = (
        "import logging; pre=len(logging.getLogger().handlers); "
        "import bot.notifier; "
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
