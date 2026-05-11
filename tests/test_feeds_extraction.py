"""Bit 4.5a + 4.5b — feed classes extracted from bot/_impl.py to bot/feeds/.

Bit 4.5a (2026-05-08, ee918e7):
  CoinbaseFeed → bot/feeds/coinbase.py
  OrderbookSchemaError → bot/feeds/orderbook_schema.py
  CrossExchangeFeed → bot/feeds/cross_exchange.py

Bit 4.5b (2026-05-09):
  KalshiFeed → bot/feeds/kalshi.py
  (largest leaf in Sprint 4; ~1,790 lines; uses sibling
  ``bot.feeds.orderbook_schema.OrderbookSchemaError``).

Locks the contract between bot/_impl.py (which does
`from bot.feeds import CoinbaseFeed, CrossExchangeFeed, KalshiFeed, OrderbookSchemaError`
after the bot.fetchers import) and the bot/feeds/ subpackage. Mirrors
tests/test_fetchers_extraction.py (Bit 4.4) and
tests/test_kalshi_client_extraction.py (Bit 4.3).

Class-specific notes:
- `_swallow_persist_exception` helper moved alongside CoinbaseFeed (its sole
  consumer) in Bit 4.5a.
- OrderbookSchemaError raise/except sites live inside KalshiFeed; post-Bit-4.5b
  the import is sibling-local
  (`from bot.feeds.orderbook_schema import OrderbookSchemaError`).
- CrossExchangeFeed has a `coinbase_feed: CoinbaseFeed` constructor type hint —
  cross-submodule dep, requires explicit import in cross_exchange.py.
- VolatilityEngine.__init__ has `feed: CoinbaseFeed` annotation (not extracted)
  — must continue resolving via the bot._impl re-import.

L33 (Bit 4.4): wrong-class attribution in extraction breadcrumbs is a recurring
drift class. Pin consumer-class identity with positive + negative regression
tests for the type annotations.
"""
from __future__ import annotations

import ast
import importlib
import logging
import re
import subprocess
import sys
from pathlib import Path

import pytest
import bot.fetchers  # noqa: F401

REPO_ROOT = Path(__file__).resolve().parents[1]


# ─── 1. Files exist + imports ───────────────────────────────────────────────


def test_feeds_subpackage_init_exists():
    assert (REPO_ROOT / "bot" / "feeds" / "__init__.py").is_file()


def test_coinbase_module_exists():
    assert (REPO_ROOT / "bot" / "feeds" / "coinbase.py").is_file()


def test_orderbook_schema_module_exists():
    assert (REPO_ROOT / "bot" / "feeds" / "orderbook_schema.py").is_file()


def test_cross_exchange_module_exists():
    assert (REPO_ROOT / "bot" / "feeds" / "cross_exchange.py").is_file()


def test_kalshi_module_exists():
    assert (REPO_ROOT / "bot" / "feeds" / "kalshi.py").is_file()


def test_subpackage_imports():
    importlib.import_module("bot.feeds")


def test_subpackage_exports_all_classes():
    import bot.feeds
    assert hasattr(bot.feeds, "CoinbaseFeed")
    assert hasattr(bot.feeds, "OrderbookSchemaError")
    assert hasattr(bot.feeds, "CrossExchangeFeed")
    assert hasattr(bot.feeds, "KalshiFeed")


# ─── 2. Identity preservation across re-export chain ────────────────────────


def test_coinbase_identity_through_bot_impl():
    import pytest as _pytest_bit_iii_c_skip; _pytest_bit_iii_c_skip.skip("bot/_impl.py removed (Bit 9.3-iii.c) — re-export contract retired", allow_module_level=False)
    import bot.feeds as bf
    import bot.feeds.coinbase as bfc
    assert b.CoinbaseFeed is bf.CoinbaseFeed is bfc.CoinbaseFeed


def test_orderbook_schema_identity_through_bot_impl():
    import pytest as _pytest_bit_iii_c_skip; _pytest_bit_iii_c_skip.skip("bot/_impl.py removed (Bit 9.3-iii.c) — re-export contract retired", allow_module_level=False)
    import bot.feeds as bf
    import bot.feeds.orderbook_schema as bfo
    assert b.OrderbookSchemaError is bf.OrderbookSchemaError is bfo.OrderbookSchemaError


def test_cross_exchange_identity_through_bot_impl():
    import pytest as _pytest_bit_iii_c_skip; _pytest_bit_iii_c_skip.skip("bot/_impl.py removed (Bit 9.3-iii.c) — re-export contract retired", allow_module_level=False)
    import bot.feeds as bf
    import bot.feeds.cross_exchange as bfx
    assert b.CrossExchangeFeed is bf.CrossExchangeFeed is bfx.CrossExchangeFeed


def test_kalshi_identity_through_bot_impl():
    import pytest as _pytest_bit_iii_c_skip; _pytest_bit_iii_c_skip.skip("bot/_impl.py removed (Bit 9.3-iii.c) — re-export contract retired", allow_module_level=False)
    import bot.feeds as bf
    import bot.feeds.kalshi as bfk
    assert b.KalshiFeed is bf.KalshiFeed is bfk.KalshiFeed


def test_all_four_identity_through_bot_proxy():
    """All 4 names resolve through `bot.X` -> `bot._BotProxy` -> `bot._impl.X`
    -> the re-imported reference. Multiple production code paths use this
    chain (MainLoop construction, KalshiFeed's OrderbookSchemaError raises)."""
    import bot
    import bot.feeds.coinbase as bfc
    import bot.feeds.cross_exchange as bfx
    import bot.feeds.kalshi as bfk
    import bot.feeds.orderbook_schema as bfo
    assert bot.feeds.CoinbaseFeed is bfc.CoinbaseFeed
    assert bot.feeds.CrossExchangeFeed is bfx.CrossExchangeFeed
    assert bot.feeds.KalshiFeed is bfk.KalshiFeed
    assert bot.feeds.OrderbookSchemaError is bfo.OrderbookSchemaError


# ─── 3. Drift guards (AST + source-string) ──────────────────────────────────


@pytest.mark.parametrize(
    "class_name",
    ["CoinbaseFeed", "OrderbookSchemaError", "CrossExchangeFeed", "KalshiFeed"],
)
def test_class_not_defined_in_bot_impl(class_name):
    """Future drift guard: catches "I'll just add it back to _impl.py".

    Mirrors test_fetchers_extraction.py (Bit 4.4). Bit 4.5b adds KalshiFeed
    to the parametrize list; the re-import chain in bot/_impl.py was the only
    place the name should resolve from pre-Bit-9.3-iii.c. Post-Bit-9.3-iii.c
    bot/_impl.py is DELETED — the negative pin is vacuous (no file to define
    classes in)."""
    bot_impl = REPO_ROOT / "bot" / "_impl.py"
    if not bot_impl.exists():
        pytest.skip("bot/_impl.py removed (Bit 9.3-iii.c) — negative pin vacuous")
    tree = ast.parse(bot_impl.read_text(), filename=str(bot_impl))
    classdefs = [
        node for node in ast.iter_child_nodes(tree)
        if isinstance(node, ast.ClassDef) and node.name == class_name
    ]
    assert classdefs == [], (
        f"{class_name} ClassDef found at module scope in bot/_impl.py "
        f"(line {classdefs[0].lineno if classdefs else '?'}). The class was "
        f"extracted to bot/feeds/ in Bit 4.5a/4.5b — re-introducing it "
        f"breaks the import chain and identity preservation."
    )


def test_swallow_persist_exception_not_defined_in_bot_impl():
    """The `_swallow_persist_exception` helper moved to bot/feeds/coinbase.py
    alongside CoinbaseFeed (its sole consumer). bot/_impl.py must NOT
    redefine it."""
    bot_impl = REPO_ROOT / "bot" / "_impl.py"
    if not bot_impl.exists() if hasattr(bot_impl, 'exists') else not __import__('os').path.exists(bot_impl): pytest.skip("bot/_impl.py removed (Bit 9.3-iii.c)")
    tree = ast.parse(bot_impl.read_text(), filename=str(bot_impl))
    funcdefs = [
        node for node in ast.iter_child_nodes(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "_swallow_persist_exception"
    ]
    assert funcdefs == [], (
        "_swallow_persist_exception module-level function found in bot/_impl.py. "
        "It moved to bot/feeds/coinbase.py in Bit 4.5a (alongside its sole "
        "consumer, CoinbaseFeed._snapshot_loop)."
    )


def test_bot_impl_imports_feeds_subpackage():
    """bot/_impl.py must import all 3 feeds names from bot.feeds.

    AST-based to avoid false matches inside docstrings/comments.
    """
    bot_impl = REPO_ROOT / "bot" / "_impl.py"
    if not bot_impl.exists() if hasattr(bot_impl, 'exists') else not __import__('os').path.exists(bot_impl): pytest.skip("bot/_impl.py removed (Bit 9.3-iii.c)")
    tree = ast.parse(bot_impl.read_text(), filename=str(bot_impl))
    imported = set()
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "bot.feeds":
            for alias in node.names:
                imported.add(alias.name)
    expected = {
        "CoinbaseFeed",
        "CrossExchangeFeed",
        "KalshiFeed",
        "OrderbookSchemaError",
    }
    missing = expected - imported
    assert not missing, (
        f"bot/_impl.py is missing feeds re-imports: {sorted(missing)}. "
        f"Without them, MainLoop construction (kalshi_feed/feed/cross_feed) "
        f"+ VolatilityEngine.__init__ + OpportunityScanner type "
        f"annotations all break."
    )


# ─── 4. Constants resolve at import time ────────────────────────────────────


COINBASE_CONSTANTS = (
    "COINBASE_PRODUCTS",
    "COINBASE_WS_URL",
    "PRICE_BUFFER_SIZE",
    "SPOT_BUFFER_PERSIST_INTERVAL_S",
    "SPOT_BUFFER_PERSIST_PATH",
)


@pytest.mark.parametrize("name", COINBASE_CONSTANTS)
def test_coinbase_constants_resolve_from_bot_constants(name):
    """All Coinbase WS / persist constants live in bot.constants per Bit 3.1.
    The coinbase module imports them explicitly."""
    import bot.constants
    import bot.feeds.coinbase as bfc
    assert getattr(bfc, name) is getattr(bot.constants, name), (
        f"bot.feeds.coinbase.{name} drifted from bot.constants.{name}."
    )


def test_coinbase_assets_from_config():
    """ASSETS lives in config.py (not bot.constants). Pin the source so a
    future maintainer doesn't try to re-import from bot.constants and
    silently break. Same drift class as Bit 4.4's DVOL_ANNUALIZED_TO_5S."""
    import bot.feeds.coinbase as bfc
    import config
    assert bfc.ASSETS is config.ASSETS or bfc.ASSETS == config.ASSETS


CROSS_EXCHANGE_CONSTANTS = (
    "BINANCE_FEED_ENABLED",
    "BINANCE_WS_URL",
    "BYBIT_WS_URL",
    "CROSS_EXCHANGE_BUFFER_SIZE",
    "CROSS_EXCHANGE_CONSENSUS_MIN",
    "CROSS_EXCHANGE_LEAD_THRESHOLD",
    "CROSS_EXCHANGE_STALE_SECONDS",
    "CROSS_EXCHANGE_SYMBOLS",
    "KRAKEN_WS_URL",
)


@pytest.mark.parametrize("name", CROSS_EXCHANGE_CONSTANTS)
def test_cross_exchange_constants_resolve_from_bot_constants(name):
    import bot.constants
    import bot.feeds.cross_exchange as bfx
    assert getattr(bfx, name) is getattr(bot.constants, name) or (
        getattr(bfx, name) == getattr(bot.constants, name)
    )


# ─── 5. Method-presence pins ────────────────────────────────────────────────


COINBASE_METHODS = (
    "__init__", "start", "stop",
    "_load_persisted_buffer", "persist_buffer",
    "get_price", "get_all_prices", "get_buffer", "get_price_trailing_avg",
    "_run_thread", "_run", "_ws_loop", "_handle_message", "_snapshot_loop",
)


@pytest.mark.parametrize("method", COINBASE_METHODS)
def test_coinbase_method_present(method):
    from bot.feeds.coinbase import CoinbaseFeed
    assert callable(getattr(CoinbaseFeed, method, None))


CROSS_EXCHANGE_METHODS = (
    "__init__", "start", "stop", "get_prices", "get_lead_lag",
    "_run_thread", "_run",
    "_ws_binance", "_handle_binance",
    "_ws_kraken", "_handle_kraken",
    "_ws_bybit", "_handle_bybit",
    "_snapshot_loop",
)


@pytest.mark.parametrize("method", CROSS_EXCHANGE_METHODS)
def test_cross_exchange_method_present(method):
    from bot.feeds.cross_exchange import CrossExchangeFeed
    assert callable(getattr(CrossExchangeFeed, method, None))


def test_orderbook_schema_error_is_exception_subclass():
    """OrderbookSchemaError must remain an Exception subclass so the
    existing `except OrderbookSchemaError` clauses inside KalshiFeed
    continue to catch it correctly."""
    from bot.feeds.orderbook_schema import OrderbookSchemaError
    assert issubclass(OrderbookSchemaError, Exception)


# ─── 6. Module hygiene ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "submodule",
    ["coinbase", "cross_exchange", "kalshi", "orderbook_schema", "__init__"],
)
def test_no_forbidden_numerical_imports(submodule):
    """No numpy/scipy/torch/sklearn/pandas in feed modules. Bit 4.1/4.2/4.3/4.4
    precedent — numerical libs have OMP thread-count side effects (per
    bot/CLAUDE.md "Threading + numerical libraries") and must not load
    before bot._thread_env."""
    src_path = REPO_ROOT / "bot" / "feeds" / f"{submodule}.py"
    src = src_path.read_text()
    forbidden = ("numpy", "scipy", "torch", "sklearn", "pandas")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                assert root not in forbidden, (
                    f"bot/feeds/{submodule}.py imports {alias.name!r}."
                )
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                root = node.module.split(".", 1)[0]
                assert root not in forbidden, (
                    f"bot/feeds/{submodule}.py imports from {node.module!r}."
                )


@pytest.mark.parametrize(
    "submodule",
    ["coinbase", "cross_exchange", "kalshi", "orderbook_schema", "__init__"],
)
def test_no_circular_bot_impl_import(submodule):
    """Submodules MUST NOT import from bot._impl. Three forms checked
    (per Bit 4.1 R2 #1 hole).
    """
    src_path = REPO_ROOT / "bot" / "feeds" / f"{submodule}.py"
    src = src_path.read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module != "bot._impl", (
                f"bot/feeds/{submodule}.py uses `from bot._impl import ...` — cycle."
            )
            if node.module == "bot":
                for alias in node.names:
                    assert alias.name != "_impl", (
                        f"bot/feeds/{submodule}.py uses `from bot import _impl` — cycle."
                    )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "bot._impl", (
                    f"bot/feeds/{submodule}.py uses `import bot._impl` — cycle."
                )


def test_cross_exchange_imports_coinbase_from_sibling():
    """CrossExchangeFeed.__init__ has `coinbase_feed: CoinbaseFeed` annotation.
    With `from __future__ import annotations`, the annotation is a string —
    but the cross_exchange module still needs CoinbaseFeed in scope IF
    any consumer calls `typing.get_type_hints` on it. Belt-and-suspenders:
    pin the explicit import."""
    src = (REPO_ROOT / "bot" / "feeds" / "cross_exchange.py").read_text()
    tree = ast.parse(src)
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "bot.feeds.coinbase":
            for alias in node.names:
                if alias.name == "CoinbaseFeed":
                    found = True
                    break
    assert found, (
        "bot/feeds/cross_exchange.py must explicitly "
        "`from bot.feeds.coinbase import CoinbaseFeed` so the constructor "
        "type hint resolves under any future runtime introspection."
    )


# ─── 7. Root-logger handler regression (subprocess isolation) ───────────────


def test_feeds_does_not_install_root_logger_handlers():
    """Importing bot.feeds must NOT install handlers on the root logger.
    Mirrors Logger / TelegramNotifier / KalshiClient / fetchers tests."""
    code = (
        "import logging\n"
        "before = list(logging.getLogger().handlers)\n"
        "import bot.feeds  # noqa: F401\n"
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
        f"bot.feeds added root-logger handlers at import time:\n"
        f"  stdout: {result.stdout}\n"
        f"  stderr: {result.stderr}"
    )


# ─── 8. Behavioral smoke ────────────────────────────────────────────────────


def test_coinbase_get_price_returns_none_for_unknown_asset(tmp_path):
    """Empty cache returns None. Verifies post-extraction class behavior.
    Uses tmp_path so persist_buffer doesn't pollute the repo dir."""
    from bot.feeds.coinbase import CoinbaseFeed
    persist = str(tmp_path / "spot_buffer.json")
    f = CoinbaseFeed(persist_path=persist)
    assert f.get_price("BTC") is None
    assert f.get_price("UNKNOWN") is None


def test_coinbase_get_price_trailing_avg_returns_none_when_below_min(tmp_path):
    from bot.feeds.coinbase import CoinbaseFeed
    persist = str(tmp_path / "spot_buffer.json")
    f = CoinbaseFeed(persist_path=persist)
    assert f.get_price_trailing_avg("BTC") is None


def test_cross_exchange_get_lead_lag_returns_default_for_empty_buffer(tmp_path):
    """Empty snapshot buffer returns the default zero-state dict."""
    from bot.feeds.coinbase import CoinbaseFeed
    from bot.feeds.cross_exchange import CrossExchangeFeed
    cb = CoinbaseFeed(persist_path=str(tmp_path / "spot_buffer.json"))
    cx = CrossExchangeFeed(coinbase_feed=cb)
    result = cx.get_lead_lag("BTC")
    assert result["consensus_direction"] == "none"
    assert result["exchanges_above"] == 0
    assert result["exchanges_below"] == 0


def test_orderbook_schema_error_can_be_raised_and_caught():
    """The exception class works as a normal Exception subclass.
    Belt-and-suspenders for the raise/except contract that KalshiFeed
    relies on."""
    from bot.feeds.orderbook_schema import OrderbookSchemaError
    try:
        raise OrderbookSchemaError("test message")
    except OrderbookSchemaError as e:
        assert str(e) == "test message"


# ─── 9. Subpackage __init__ contract ────────────────────────────────────────


def test_subpackage_init_re_exports_match_submodule_classes():
    """bot.feeds exports the same class objects as the submodules.
    A future maintainer might add another class and forget to wire
    the __init__.py re-export."""
    import bot.feeds
    import bot.feeds.coinbase
    import bot.feeds.cross_exchange
    import bot.feeds.kalshi
    import bot.feeds.orderbook_schema
    assert bot.feeds.CoinbaseFeed is bot.feeds.coinbase.CoinbaseFeed
    assert bot.feeds.CrossExchangeFeed is bot.feeds.cross_exchange.CrossExchangeFeed
    assert bot.feeds.KalshiFeed is bot.feeds.kalshi.KalshiFeed
    assert bot.feeds.OrderbookSchemaError is bot.feeds.orderbook_schema.OrderbookSchemaError


# ─── 10. Annotation-consumer pins (L33 from Bit 4.4) ────────────────────────


def test_volatility_engine_still_annotates_coinbase_feed():
    """`VolatilityEngine.__init__` has `feed: CoinbaseFeed` annotation
    (L33 carry-forward from Bit 4.4). Pin which class hosts it so a
    future class-rename / parameter-rename trips the suite. Without
    this, the breadcrumb in `bot/_impl.py:104` referencing
    VolatilityEngine could rot silently. Post-Bit-6.1 the class lives in
    bot/engines/volatility.py — AST walk retargeted per L38."""
    target_path = REPO_ROOT / "bot" / "engines" / "volatility.py"
    src = target_path.read_text()
    tree = ast.parse(src)
    vol_engine = next(
        (
            node
            for node in ast.iter_child_nodes(tree)
            if isinstance(node, ast.ClassDef) and node.name == "VolatilityEngine"
        ),
        None,
    )
    assert vol_engine is not None, (
        "VolatilityEngine ClassDef missing from bot/engines/volatility.py "
        "(post-Bit-6.1 location)."
    )
    init = next(
        (
            n
            for n in vol_engine.body
            if isinstance(n, ast.FunctionDef) and n.name == "__init__"
        ),
        None,
    )
    assert init is not None, "VolatilityEngine.__init__ missing"
    init_src = ast.get_source_segment(src, init) or ""
    assert "CoinbaseFeed" in init_src, (
        "VolatilityEngine.__init__ no longer annotates with CoinbaseFeed. "
        "Either the annotation was removed (then drop the `Bit 4.5a leaf "
        "extraction` breadcrumb in bot/_impl.py referencing it) or it moved "
        "to a different class (then update the breadcrumb)."
    )


def test_opportunity_scanner_still_annotates_coinbase_feed():
    """`OpportunityScanner.__init__` has `feed: CoinbaseFeed` annotation
    per the original code at line 8091 (pre-Bit-4.5a). Pin it.

    Bit 8.1 (2026-05-10): OpportunityScanner extracted from bot/_impl.py
    to bot/scanner/__init__.py — read scanner first; fall back to
    bot/_impl.py for older branches that haven't merged the extraction."""
    scanner_init = REPO_ROOT / "bot" / "scanner" / "__init__.py"
    bot_impl = REPO_ROOT / "bot" / "_impl.py"
    src_path = scanner_init if scanner_init.exists() else bot_impl
    src = src_path.read_text()
    tree = ast.parse(src)
    scanner = next(
        (
            node
            for node in ast.iter_child_nodes(tree)
            if isinstance(node, ast.ClassDef) and node.name == "OpportunityScanner"
        ),
        None,
    )
    assert scanner is not None, (
        f"OpportunityScanner ClassDef missing in {src_path}"
    )
    init = next(
        (
            n
            for n in scanner.body
            if isinstance(n, ast.FunctionDef) and n.name == "__init__"
        ),
        None,
    )
    assert init is not None, "OpportunityScanner.__init__ missing"
    init_src = ast.get_source_segment(src, init) or ""
    assert "CoinbaseFeed" in init_src, (
        "OpportunityScanner.__init__ no longer annotates with CoinbaseFeed."
    )


def test_kalshi_feed_still_uses_orderbook_schema_error():
    """KalshiFeed (now bot/feeds/kalshi.py per Bit 4.5b) raises
    OrderbookSchemaError in multiple sites. Pin that the symbol is still
    referenced — guards the sibling import + the bot/_impl.py re-import
    line against "this is unused, can we delete it?" mistakes."""
    kalshi_src_path = REPO_ROOT / "bot" / "feeds" / "kalshi.py"
    src = kalshi_src_path.read_text()
    tree = ast.parse(src)
    kfeed = next(
        (
            node
            for node in ast.iter_child_nodes(tree)
            if isinstance(node, ast.ClassDef) and node.name == "KalshiFeed"
        ),
        None,
    )
    assert kfeed is not None, "KalshiFeed ClassDef missing in bot/feeds/kalshi.py"
    kfeed_src = ast.get_source_segment(src, kfeed) or ""
    assert "OrderbookSchemaError" in kfeed_src, (
        "KalshiFeed no longer references OrderbookSchemaError. If "
        "intentional, also drop the OrderbookSchemaError name from the "
        "`from bot.feeds.orderbook_schema import OrderbookSchemaError` line "
        "in bot/feeds/kalshi.py and the `from bot.feeds import` line in "
        "bot/_impl.py."
    )


def test_kalshi_feed_imports_orderbook_schema_from_sibling():
    """Bit 4.5b: KalshiFeed must import OrderbookSchemaError from the sibling
    submodule (`bot.feeds.orderbook_schema`), NOT from `bot._impl` — the
    latter would be a circular import. Mirrors
    `test_cross_exchange_imports_coinbase_from_sibling`.
    """
    src = (REPO_ROOT / "bot" / "feeds" / "kalshi.py").read_text()
    tree = ast.parse(src)
    found = False
    for node in ast.walk(tree):
        if (isinstance(node, ast.ImportFrom)
                and node.module == "bot.feeds.orderbook_schema"):
            for alias in node.names:
                if alias.name == "OrderbookSchemaError":
                    found = True
                    break
    assert found, (
        "bot/feeds/kalshi.py must explicitly "
        "`from bot.feeds.orderbook_schema import OrderbookSchemaError` so "
        "the raise/except sites resolve without going through bot._impl "
        "(which would create a circular import)."
    )


KALSHI_FEED_CONSTANTS = (
    "KALSHI_WS_URL",
    "WS_FORCE_RESUB_COOLDOWN_S",
    "WS_FORCE_RESUB_RECOVERY_TIMEOUT_S",
    "WS_GET_SNAPSHOT_DISABLE_AFTER",
    "WS_OUTSTANDING_SUBSCRIBE_TIMEOUT_S",
    "WS_RAW_LOG_DURATION_S",
    "WS_RAW_LOG_MAX_PER_SESSION",
    "WS_RAW_LOG_TRUNCATE",
    "WS_SILENCE_GRACE_SECONDS",
    "WS_SILENCE_TIMEOUT_SECONDS",
    "WS_SNAPSHOT_REQUEST_TIMEOUT_S",
    "WS_UNSUBSCRIBE_BLACKLIST_S",
    "WS_WATCHDOG_CHECK_INTERVAL",
)


@pytest.mark.parametrize("name", KALSHI_FEED_CONSTANTS)
def test_kalshi_feed_constants_resolve_from_bot_constants(name):
    """All KalshiFeed WS tunables live in bot.constants per Bit 3.1.
    The kalshi module imports them explicitly (the `import *` shortcut
    is not used so a bot.constants rename trips the suite immediately)."""
    import bot.constants
    import bot.feeds.kalshi as bfk
    assert getattr(bfk, name) is getattr(bot.constants, name), (
        f"bot.feeds.kalshi.{name} drifted from bot.constants.{name}."
    )


KALSHI_FEED_METHODS = (
    "__init__", "start", "stop",
    "subscribe_ticker", "unsubscribe_ticker",
    "force_resubscribe", "_sweep_unsubscribe_blacklist",
    "_check_snapshot_timeouts",
    "get_subscribed_tickers", "get_subscribed_count", "get_cached_ob_count",
    "get_orderbook", "get_all_orderbooks", "get_all_orderbooks_snapshot",
    "pop_fills",
    "_cleanup_session_state",
    "_create_ws_headers",
    "_run_thread", "_ws_loop",
    "_send_ob_subscribe", "_send_ob_unsubscribe", "_send_ob_get_snapshot",
    "_process_pending_subs",
    "_handle_subscribe_ack",
    "_should_log_raw_in", "_raw_log_budget_ok",
    "_log_raw_out", "_log_raw_in",
    "_handle_message", "_handle_fill",
    "_handle_ob_snapshot", "_handle_ob_delta",
    "_apply_fp_delta", "_apply_legacy_delta",
    "_normalize_fp_levels", "_level_price", "_level_qty",
)


@pytest.mark.parametrize("method", KALSHI_FEED_METHODS)
def test_kalshi_feed_method_present(method):
    from bot.feeds.kalshi import KalshiFeed
    assert callable(getattr(KalshiFeed, method, None))


def test_kalshi_feed_is_connected_is_property():
    """`is_connected` is a `@property`, not a callable. Pin its shape so a
    refactor to a method (or vice versa) trips the suite — KalshiFeed
    consumers (MainLoop / OpportunityScanner watchdog) read it as
    `feed.is_connected`, not `feed.is_connected()`."""
    from bot.feeds.kalshi import KalshiFeed
    assert isinstance(KalshiFeed.is_connected, property)
