"""Bit 4.3 — KalshiClient class extracted from bot/_impl.py to bot/kalshi_client.py.

Locks the contract between bot/_impl.py (which does
`from bot.kalshi_client import KalshiClient` after the Logger and
TelegramNotifier imports) and the new bot/kalshi_client.py module.
Mirrors tests/test_notifier_extraction.py (Bit 4.2) and
tests/test_logger_extraction.py (Bit 4.1).

L2 (Bit 3.0.5): tests call production directly. No reimplementing the
contract in test helpers.

L29 (Bit 4.1): doc-drift in agent_docs/bot_layout.md ships in the same
atomic commit (regen via the recipe in the doc preamble).

R2 #1 fix (Bit 4.1): the cycle-guard rejects all 3 forms (`from bot._impl`,
`import bot._impl`, `from bot import _impl`).

Bit 4.3 specifics: KalshiClient is the largest leaf yet (~349 body lines,
17 methods). Free-name lookups in lambda key_fns reference
`_kalshi_series_key` (Bit 3.2 helper) — the new module must import it
explicitly. `_BREAKER_REGISTRY` is also referenced directly inside
`get_queue_position` (manual breaker wrapping). Both checked here.
"""
import ast
import importlib
import inspect
import logging
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


# ─── 1. File exists + imports ───────────────────────────────────────────────


def test_kalshi_client_file_exists():
    assert (REPO_ROOT / "bot" / "kalshi_client.py").is_file()


def test_kalshi_client_module_imports():
    importlib.import_module("bot.kalshi_client")


def test_kalshi_client_class_on_module():
    import bot.kalshi_client
    assert hasattr(bot.kalshi_client, "KalshiClient")


# ─── 2. Identity preservation across re-export chain ────────────────────────


def test_kalshi_client_identity_through_bot_impl():
    """bot._impl.KalshiClient is bot.kalshi_client.KalshiClient.

    The `from bot.kalshi_client import KalshiClient` line in bot/_impl.py is
    what makes runtime construction in `MainLoop.__init__` (search
    `self.client = KalshiClient` for the current line) resolve, plus all
    the type annotations across reconcile_with_api / OpportunityScanner /
    OrderExecutor / SettlementTracker / discover_active_windows.
    """
    import bot._impl as b
    import bot.kalshi_client as bkc
    assert b.KalshiClient is bkc.KalshiClient


def test_kalshi_client_identity_through_bot_proxy():
    """`bot.KalshiClient` resolves through the _BotProxy.

    Multiple scripts use `from bot import KalshiClient`
    (correct_ioc_double_count.py, sports_diagnose.py, sports_raw_probe.py,
    reconcile_ioc_losses.py); this is the path they hit.
    """
    import bot
    import bot.kalshi_client as bkc
    assert bot.KalshiClient is bkc.KalshiClient


def test_kalshi_client_three_way_identity_chain():
    """All three resolution paths point at the same class object.

    Locks the precedent set by Bit 4.1 (Logger) and Bit 4.2 (TelegramNotifier).
    """
    import bot
    import bot._impl as b
    import bot.kalshi_client as bkc
    assert bot.KalshiClient is b.KalshiClient is bkc.KalshiClient


# ─── 3. Drift guards (AST + source-string) ──────────────────────────────────


def test_kalshi_client_class_not_defined_in_bot_impl():
    """Future drift guard: catches "I'll just add it back to _impl.py".

    Mirrors test_notifier_class_not_defined_in_bot_impl (Bit 4.2).
    """
    bot_impl = REPO_ROOT / "bot" / "_impl.py"
    tree = ast.parse(bot_impl.read_text(), filename=str(bot_impl))
    classdefs = [
        node for node in ast.iter_child_nodes(tree)
        if isinstance(node, ast.ClassDef) and node.name == "KalshiClient"
    ]
    assert classdefs == [], (
        f"KalshiClient ClassDef found at module scope in bot/_impl.py "
        f"(line {classdefs[0].lineno if classdefs else '?'}). The class was "
        f"extracted to bot/kalshi_client.py in Bit 4.3 — re-introducing it "
        f"breaks the import chain and identity preservation. If this is "
        f"intentional, also delete bot/kalshi_client.py and update this test."
    )


def test_bot_impl_imports_kalshi_client():
    """bot/_impl.py must import KalshiClient from bot.kalshi_client.

    Without this import, runtime construction `KalshiClient(...)` in
    MainLoop.__init__ raises NameError. AST-based to avoid false matches
    inside docstrings/comments.
    """
    bot_impl = REPO_ROOT / "bot" / "_impl.py"
    tree = ast.parse(bot_impl.read_text(), filename=str(bot_impl))
    found = False
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "bot.kalshi_client":
                names = {alias.name for alias in node.names}
                if "KalshiClient" in names:
                    found = True
                    break
    assert found, (
        "bot/_impl.py is missing `from bot.kalshi_client import KalshiClient`. "
        "Without it, MainLoop construction + type annotations all break."
    )


# ─── 4. Decorator + helper symbols available at class-body time ─────────────


def test_kalshi_breaker_decorator_resolves_at_import():
    """The class-body decorators @_kalshi_breaker / @_breaker_config evaluate
    at import time. If bot.helpers.breakers fails to expose these names, the
    bot.kalshi_client module raises at import — catastrophic.
    """
    import bot.helpers.breakers as bb
    import bot.kalshi_client as bkc
    assert bkc._kalshi_breaker is bb._kalshi_breaker
    assert bkc._breaker_config is bb._breaker_config


def test_kalshi_breaker_helpers_resolve_at_import():
    """`_kalshi_series_key` is referenced as a free name inside the lambda
    key_fns of get_market / get_orderbook. `_kalshi_breaker_success` is
    called manually inside get_queue_position. Both must be in the
    bot.kalshi_client module globals so Python's free-name lookup finds
    them at call time.
    """
    import bot.helpers.breakers as bb
    import bot.kalshi_client as bkc
    assert bkc._kalshi_series_key is bb._kalshi_series_key
    assert bkc._kalshi_breaker_success is bb._kalshi_breaker_success


def test_breaker_registry_resolves_at_import():
    """`_BREAKER_REGISTRY` is referenced directly inside get_queue_position
    (manual breaker wrapping). The new module must import it from
    `circuit_breaker` — not inherit it from bot/_impl.py.
    """
    import circuit_breaker
    import bot.kalshi_client as bkc
    assert bkc._BREAKER_REGISTRY is circuit_breaker.REGISTRY


# ─── 5. Method-signature pins (prevents accidental signature drift) ─────────


PUBLIC_METHODS = (
    "get_balance",
    "get_markets",
    "get_events",
    "get_market",
    "get_orderbook",
    "place_order",
    "cancel_order",
    "amend_order",
    "get_queue_position",
    "get_orders",
    "get_fills",
    "get_settlements",
    "get_positions",
)


@pytest.mark.parametrize("method_name", PUBLIC_METHODS)
def test_public_method_present_on_class(method_name):
    """All 13 public methods enumerated at extraction time are still
    callable attributes on KalshiClient. Catches accidental deletion or
    rename during the move."""
    from bot.kalshi_client import KalshiClient
    method = getattr(KalshiClient, method_name, None)
    assert callable(method), (
        f"KalshiClient.{method_name} missing or not callable post-extraction."
    )


def test_init_signature_pin():
    """KalshiClient.__init__(self, api_key: str, private_key_path: str)."""
    from bot.kalshi_client import KalshiClient
    sig = inspect.signature(KalshiClient.__init__)
    params = list(sig.parameters.values())
    assert [p.name for p in params] == ["self", "api_key", "private_key_path"]
    # Annotation-strings vs annotations: tolerate both since str | type are
    # both acceptable.
    for p in params[1:]:
        assert p.annotation is str or p.annotation == "str"


def test_load_private_key_is_staticmethod():
    """The constructor calls `self._load_private_key(path)` but the impl is
    `@staticmethod`. Drift away from staticmethod would break unit tests
    that call it without an instance."""
    from bot.kalshi_client import KalshiClient
    # __dict__ access bypasses the descriptor protocol; the raw attribute
    # is staticmethod.
    raw = KalshiClient.__dict__["_load_private_key"]
    assert isinstance(raw, staticmethod)


def test_writes_are_not_breaker_wrapped():
    """place_order / cancel_order / amend_order are intentionally NOT
    decorated with @_kalshi_breaker (write-side exemption: tripping a
    breaker on a transient 5xx would block position exit / risk
    duplicates). Lock the contract here.

    Mirrors tests/test_kalshi_client_breakers.py::TestKalshiClientWritesAreNotWrapped
    but pins the post-extraction state explicitly.
    """
    from bot.kalshi_client import KalshiClient
    for method_name in ("place_order", "cancel_order", "amend_order"):
        method = getattr(KalshiClient, method_name)
        # Decorator-wrapped methods carry a `__wrapped__` attribute set by
        # functools.wraps inside _kalshi_breaker. Unwrapped methods do not.
        assert not hasattr(method, "__wrapped__"), (
            f"KalshiClient.{method_name} is breaker-wrapped — write-side "
            f"exemption broken in Bit 4.3 extraction."
        )


def test_breaker_wrapped_methods_have_wrapped_attr():
    """Conversely, all GET-style methods MUST be breaker-wrapped. If
    Bit 4.3 silently dropped the decorator (e.g. by extracting just the
    body without the @_kalshi_breaker line), these would lose protection."""
    from bot.kalshi_client import KalshiClient
    wrapped_methods = (
        "get_balance",
        "get_markets",
        "get_events",
        "get_market",
        "get_orderbook",
        "get_orders",
        "get_fills",
        "get_settlements",
        "get_positions",
    )
    for method_name in wrapped_methods:
        method = getattr(KalshiClient, method_name)
        assert hasattr(method, "__wrapped__"), (
            f"KalshiClient.{method_name} missing breaker decorator — "
            f"GET-side protection broken in Bit 4.3 extraction."
        )


# ─── 6. Module hygiene ──────────────────────────────────────────────────────


def test_no_forbidden_numerical_imports_in_kalshi_client():
    """KalshiClient should not transitively pull numpy/scipy/torch/sklearn/
    pandas. The Bit 4.1/4.2 precedents pinned this for Logger and
    TelegramNotifier; KalshiClient is a heavier leaf but still pure-IO,
    so the same guard applies. Numerical libraries have OMP thread-count
    side effects (per bot/CLAUDE.md "Threading + numerical libraries"
    section) and must not load before bot._thread_env.
    """
    src = (REPO_ROOT / "bot" / "kalshi_client.py").read_text()
    forbidden = ("numpy", "scipy", "torch", "sklearn", "pandas")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                assert root not in forbidden, (
                    f"bot/kalshi_client.py imports {alias.name!r} — "
                    f"numerical-library side effect risk."
                )
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                root = node.module.split(".", 1)[0]
                assert root not in forbidden, (
                    f"bot/kalshi_client.py imports from {node.module!r} — "
                    f"numerical-library side effect risk."
                )


def test_no_circular_bot_impl_import_in_kalshi_client():
    """bot.kalshi_client must NOT import from bot._impl, or the import chain
    cycles (bot._impl imports bot.kalshi_client which imports bot._impl).

    Three forms checked: `from bot._impl ...`, `import bot._impl`, and
    `from bot import _impl` (the R2 #1 hole flagged in Bit 4.1's
    test_logger_extraction.py post-mortem).
    """
    src = (REPO_ROOT / "bot" / "kalshi_client.py").read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module != "bot._impl", (
                f"bot/kalshi_client.py uses `from bot._impl import ...` — "
                f"creates an import cycle."
            )
            if node.module == "bot":
                for alias in node.names:
                    assert alias.name != "_impl", (
                        f"bot/kalshi_client.py uses `from bot import _impl` "
                        f"— creates an import cycle."
                    )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "bot._impl", (
                    f"bot/kalshi_client.py uses `import bot._impl` — "
                    f"creates an import cycle."
                )


def test_kalshi_client_imports_constants_from_bot_constants():
    """The class body references BASE_URL / API_PATH_PREFIX / READ_RATE_LIMIT
    / WRITE_RATE_LIMIT. These live in bot/constants.py post-Bit-3.1 and
    must be imported explicitly (the new module is independent — it does
    NOT inherit bot/_impl.py's `from bot.constants import *` star-import).
    """
    src = (REPO_ROOT / "bot" / "kalshi_client.py").read_text()
    tree = ast.parse(src)
    expected = {"BASE_URL", "API_PATH_PREFIX", "READ_RATE_LIMIT", "WRITE_RATE_LIMIT"}
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "bot.constants":
            for alias in node.names:
                found.add(alias.name)
    missing = expected - found
    assert not missing, (
        f"bot/kalshi_client.py missing constants imports from bot.constants: "
        f"{sorted(missing)}. The class body references these names; without "
        f"explicit import the call-site lookup raises NameError."
    )


# ─── 7. Root-logger handler regression (subprocess isolation) ───────────────


def test_kalshi_client_does_not_install_root_logger_handlers():
    """Importing bot.kalshi_client must NOT install handlers on the root
    logger. Mirrors the Logger / TelegramNotifier subprocess-isolation
    test (Bit 4.1, 4.2).

    Failure mode: a module-level `logging.basicConfig(...)` call (or a
    `logging.<level>(...)` call before any handlers are configured)
    auto-installs a handler on the root logger, which shows up later as
    duplicate log output and breaks pytest's caplog fixture in tests that
    import the module transitively.

    Subprocess isolation: ensures no other test fixtures contaminate the
    handler count.
    """
    code = (
        "import logging\n"
        "before = list(logging.getLogger().handlers)\n"
        "import bot.kalshi_client  # noqa: F401\n"
        "after = list(logging.getLogger().handlers)\n"
        "added = [h for h in after if h not in before]\n"
        "if added:\n"
        "    print(f'ADDED: {added}')\n"
        "    raise SystemExit(1)\n"
        "print('clean')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, (
        f"bot.kalshi_client added root-logger handlers at import time:\n"
        f"  stdout: {result.stdout}\n"
        f"  stderr: {result.stderr}"
    )


# ─── 8. _request signing-correctness mock-driven smoke test ─────────────────


def test_request_signs_and_rate_limits_post_extraction():
    """_request still signs/rate-limits correctly post-extraction.

    Constructs a KalshiClient via __new__ (skipping __init__'s file-read
    of the RSA key), wires up a mock private_key + mock requests.Session,
    and asserts the request was issued with the expected headers + URL.
    """
    from bot.kalshi_client import KalshiClient, BASE_URL, API_PATH_PREFIX

    client = KalshiClient.__new__(KalshiClient)
    client.api_key = "test-key"
    mock_pk = MagicMock()
    mock_pk.sign.return_value = b"raw-signature-bytes"
    client.private_key = mock_pk
    client.session = MagicMock()
    client._read_timestamps = []
    client._write_timestamps = []
    import threading
    client._rate_lock = threading.Lock()

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.headers = {}
    mock_resp.content = b'{"x":1}'
    mock_resp.json.return_value = {"x": 1}
    client.session.request.return_value = mock_resp

    result = client._request("GET", f"{API_PATH_PREFIX}/portfolio/balance")
    assert result == {"x": 1}

    call = client.session.request.call_args
    assert call.args[0] == "GET"
    assert call.args[1] == f"{BASE_URL}{API_PATH_PREFIX}/portfolio/balance"
    headers = call.kwargs["headers"]
    assert headers["KALSHI-ACCESS-KEY"] == "test-key"
    assert "KALSHI-ACCESS-TIMESTAMP" in headers
    assert "KALSHI-ACCESS-SIGNATURE" in headers
    assert headers["Content-Type"] == "application/json"

    # Signature was Base64-encoded
    import base64
    sig = headers["KALSHI-ACCESS-SIGNATURE"]
    assert base64.b64decode(sig) == b"raw-signature-bytes"

    # Rate-limit timestamps were appended (read-side, not write)
    assert len(client._read_timestamps) == 1
    assert len(client._write_timestamps) == 0


def test_request_handles_delete_404_with_sentinel():
    """The DELETE 404 path must return the sentinel dict (not None or raise)
    post-extraction. This is the cancel-404 V8 fix from May 4.
    """
    from bot.kalshi_client import KalshiClient, API_PATH_PREFIX

    client = KalshiClient.__new__(KalshiClient)
    client.api_key = "test-key"
    mock_pk = MagicMock()
    mock_pk.sign.return_value = b"sig"
    client.private_key = mock_pk
    client.session = MagicMock()
    client._read_timestamps = []
    client._write_timestamps = []
    import threading
    client._rate_lock = threading.Lock()

    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_resp.headers = {}
    client.session.request.return_value = mock_resp

    result = client._request("DELETE", f"{API_PATH_PREFIX}/portfolio/orders/abc")
    assert result == {"_error": True, "_status_code": 404}
