"""D2.3 — ``bot/feeds/coinbase.py`` MUST delegate WS transport to
``coinbase_wire.ws_client.WSClient``.

Ticket 86b9zkppt (2026-05-17). After D2.3 ships, the bot must consume
the shared wire library for:

  - **WS transport**: ``bot/feeds/coinbase.py``'s ``_ws_loop`` +
    inline ``websockets.connect`` + inline ``async for raw in ws:``
    drain → delegate to ``coinbase_wire.ws_client.WSClient``.
  - **Subscribe payload**: the inline
    ``json.dumps({"type":"subscribe", ...})`` in ``_ws_loop`` → delegate
    to ``coinbase_wire.auth.build_public_subscribe_message`` + the
    wire's ``WSClient.send_frame`` (R1-M1 D2.2 defense: build via
    public helper so a wire-side rename of ``_default_on_session_start``
    can't silently strand the consumer).

This is the AST guard that **prevents re-divergence** — without it, a
future maintainer could copy the WS-loop / subscribe scaffold back
inline and break the symmetry. It catches the "two parsers diverge"
risk at compile time, not at runtime.

This test is the BOT-SIDE peer of
``tests/contracts/test_coinbase_archiver_on_session_start.py``
(collector-side). Together they prove both consumers of
``coinbase_wire`` stay on the library, not on inline copies.

If this test fails:
- The bot stopped delegating to ``coinbase_wire`` — investigate whether
  the extracted call sites were silently reverted (manual replace_all
  cleanup gone wrong, merge conflict resolved the wrong way).
- ``coinbase_wire.ws_client.WSClient`` doesn't exist — D2.1.5 isn't
  shipped yet (would be surprising at this point — D2.1.5 + D2.2 are
  both SHIPPED per the lockstep ratchet).
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


_HEAVY_MOD_NAMES = ("websockets",)
for _mod in _HEAVY_MOD_NAMES:
    sys.modules.setdefault(_mod, MagicMock())


REPO_ROOT = Path(__file__).resolve().parents[2]
BOT_FEEDS_COINBASE = REPO_ROOT / "bot" / "feeds" / "coinbase.py"


def _collect_imports(path: Path) -> set[str]:
    """Return the set of dotted module names imported by ``path``.

    Walks both ``import X`` and ``from X import Y`` forms. Sub-imports
    inside functions (late-binding) are included.
    """
    if not path.is_file():
        pytest.skip(f"{path} missing")
    src = path.read_text()
    tree = ast.parse(src)
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module:
                imports.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name)
    return imports


# ─── WS transport delegation (REQUIRED) ─────────────────────────────────────


def test_bot_feeds_coinbase_imports_ws_client():
    """``bot/feeds/coinbase.py`` imports ``WSClient`` from
    ``coinbase_wire.ws_client``. CoinbaseFeed wraps WSClient with
    bot-specific state (price dict + 30-min persistent buffer + 1s
    sampler) but the connect/reconnect/auth-irrelevant/drain/silence-
    watchdog skeleton lives in WSClient.
    """
    imports = _collect_imports(BOT_FEEDS_COINBASE)
    has_ws_client = any(
        i == "coinbase_wire.ws_client"
        or i.startswith("coinbase_wire.ws_client.")
        for i in imports
    )
    assert has_ws_client, (
        "bot/feeds/coinbase.py does NOT import coinbase_wire.ws_client — "
        "D2.3 WS-transport delegation regressed. Expected "
        "`from coinbase_wire.ws_client import WSClient`."
    )


def test_bot_feeds_coinbase_imports_public_subscribe_helper():
    """``bot/feeds/coinbase.py`` imports
    ``build_public_subscribe_message`` from ``coinbase_wire.auth``.

    D2.2 R1-M1 RCA carried forward: the consumer MUST build the
    subscribe payload via the public helper + ``WSClient.send_frame``
    (NOT via reaching into ``self._wire._default_on_session_start()``
    which is a private wire-library method). A wire-side rename of
    the private method would silently strand the consumer:
    construction + import + start() all succeed, but at first WS
    connect ``AttributeError`` fires inside the wire's
    ``try: cb() except Exception: log.warning(...)`` swallower — no
    subscribe dispatches, no price ticks reach the bot, and only the
    90s silence-watchdog as the alert.

    This guard enforces that the consumer-side subscribe path is
    structurally visible to AST tooling rather than buried inside a
    runtime attribute access.
    """
    imports = _collect_imports(BOT_FEEDS_COINBASE)
    has_helper = any(
        i == "coinbase_wire.auth"
        or i.startswith("coinbase_wire.auth.")
        for i in imports
    )
    assert has_helper, (
        "bot/feeds/coinbase.py does NOT import from coinbase_wire.auth — "
        "D2.3 subscribe-delegation regressed. Expected "
        "`from coinbase_wire.auth import build_public_subscribe_message` "
        "so the consumer-side subscribe path goes through the public "
        "helper rather than reaching into the wire's private "
        "`_default_on_session_start()` method (R1-M1 D2.2 defense)."
    )


# ─── Anti-regression: inline transport surfaces must NOT live in coinbase.py ─


def test_bot_feeds_coinbase_no_inline_websockets_connect():
    """After D2.3 extraction, the ``websockets.connect`` call lives
    inside ``coinbase_wire.ws_client.WSClient._ws_loop`` — NOT inline
    in ``bot/feeds/coinbase.py``.

    AST guard against silent reversion: walks Call nodes for the
    ``websockets.connect(...)`` invocation. If a future maintainer
    copies the WS-loop scaffold back inline, this fails. The cancel
    of WS-loop ownership from CoinbaseFeed is what makes D2.3
    structurally meaningful — the bot becomes a pure consumer of
    the shared transport.
    """
    if not BOT_FEEDS_COINBASE.is_file():
        pytest.skip(f"{BOT_FEEDS_COINBASE} missing")
    src = BOT_FEEDS_COINBASE.read_text()
    tree = ast.parse(src)
    inline_connect_sites: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if (node.func.attr == "connect"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "websockets"):
                inline_connect_sites.append(node.lineno)
    assert not inline_connect_sites, (
        f"bot/feeds/coinbase.py contains inline `websockets.connect(...)` "
        f"calls at lines {inline_connect_sites}. After D2.3, the WS-loop "
        "transport lives in coinbase_wire.ws_client.WSClient — "
        "CoinbaseFeed should consume frames via on_frame callback, not "
        "open its own WS connection."
    )


def test_bot_feeds_coinbase_ws_loop_method_removed():
    """After D2.3 extraction, the ``async def _ws_loop`` method on
    CoinbaseFeed no longer exists — the loop body lives in
    ``WSClient._ws_loop`` (private to coinbase_wire).

    CoinbaseFeed.start() now delegates to ``self._wire.start()``
    instead of spawning its own asyncio thread + event loop.
    """
    if not BOT_FEEDS_COINBASE.is_file():
        pytest.skip(f"{BOT_FEEDS_COINBASE} missing")
    src = BOT_FEEDS_COINBASE.read_text()
    tree = ast.parse(src)
    ws_loop_methods: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_ws_loop":
            ws_loop_methods.append(node.lineno)
    assert not ws_loop_methods, (
        f"bot/feeds/coinbase.py still defines `async def _ws_loop` at "
        f"lines {ws_loop_methods}. After D2.3, the WS-loop lives in "
        "coinbase_wire.ws_client.WSClient and CoinbaseFeed.start() "
        "delegates to self._wire.start()."
    )


def test_bot_feeds_coinbase_no_inline_asyncio_event_loop_construction():
    """After D2.3 extraction, ``bot/feeds/coinbase.py`` no longer
    constructs its own asyncio event loop — WSClient owns the asyncio
    thread.

    AST guard: walks Call nodes for ``asyncio.new_event_loop()``. If a
    future maintainer copies the dedicated-thread-with-its-own-loop
    scaffold back inline, this fails. The point of D2.3 is that
    transport-layer asyncio is wire-owned; the bot stays on synchronous
    threading for its sampler.
    """
    if not BOT_FEEDS_COINBASE.is_file():
        pytest.skip(f"{BOT_FEEDS_COINBASE} missing")
    src = BOT_FEEDS_COINBASE.read_text()
    tree = ast.parse(src)
    inline_loop_sites: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if (node.func.attr == "new_event_loop"
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "asyncio"):
                inline_loop_sites.append(node.lineno)
    assert not inline_loop_sites, (
        f"bot/feeds/coinbase.py contains inline "
        f"`asyncio.new_event_loop()` calls at lines {inline_loop_sites}. "
        "After D2.3, the asyncio event loop lives in "
        "coinbase_wire.ws_client.WSClient — CoinbaseFeed runs only its "
        "synchronous sampler thread."
    )


def test_bot_feeds_coinbase_narrow_subscribe_scope_is_ticker_only():
    """The bot MUST subscribe to ``channels=("ticker",)`` only — NOT the
    wire's post-D2.5 5-channel ``DEFAULT_CHANNELS`` set (ticker +
    matches + heartbeat + status + level2_batch) that the collector
    uses for bronze archiving.

    Why pin this here (not just via the equivalence differential): the
    differential test in ``tests/equivalence/test_coinbase_wire_differential.py``
    pins that two WSClient consumers see byte-identical frames from a
    given wire connection — but it does NOT pin the bot-side
    subscribe-payload SHAPE (the mock server emits all subscribed
    channels regardless of what the consumer requests). Without this
    pin, a future maintainer could widen ``_BOT_CHANNELS`` to the
    wire's default set without tripping any contract test, adding CPU
    + GIL noise to the bot's spot-feed path (and after D2.5 level2_batch
    promotion, the high-volume orderbook firehose) while everything
    still "works".

    AST guard: walk the module for the literal tuple assignment
    ``_BOT_CHANNELS = (...)`` and assert the only element is
    ``"ticker"``. The constant must remain a top-level tuple literal
    so the AST walker can statically inspect it (no runtime
    composition).
    """
    if not BOT_FEEDS_COINBASE.is_file():
        pytest.skip(f"{BOT_FEEDS_COINBASE} missing")
    src = BOT_FEEDS_COINBASE.read_text()
    tree = ast.parse(src)
    found_assignments: list[tuple[int, list[str]]] = []
    for node in ast.iter_child_nodes(tree):
        # Match `_BOT_CHANNELS: Tuple[str, ...] = ("ticker",)` (annotated)
        # OR `_BOT_CHANNELS = ("ticker",)` (plain).
        target_name = None
        value_node = None
        if isinstance(node, ast.AnnAssign) and isinstance(
                node.target, ast.Name):
            target_name = node.target.id
            value_node = node.value
        elif isinstance(node, ast.Assign) and len(node.targets) == 1 and \
                isinstance(node.targets[0], ast.Name):
            target_name = node.targets[0].id
            value_node = node.value
        if target_name != "_BOT_CHANNELS" or value_node is None:
            continue
        if not isinstance(value_node, ast.Tuple):
            continue
        elements: list[str] = []
        for elt in value_node.elts:
            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                elements.append(elt.value)
            else:
                elements.append(f"<non-string:{ast.dump(elt)}>")
        found_assignments.append((node.lineno, elements))
    assert len(found_assignments) == 1, (
        f"Expected exactly 1 module-level `_BOT_CHANNELS = (...)` "
        f"assignment in bot/feeds/coinbase.py; found {len(found_assignments)}: "
        f"{found_assignments}. The constant pins the bot's narrow "
        "subscribe scope and must stay a top-level tuple literal so the "
        "AST contract walker can verify it without executing the module."
    )
    lineno, elements = found_assignments[0]
    assert elements == ["ticker"], (
        f"bot/feeds/coinbase.py:{lineno} `_BOT_CHANNELS = {elements!r}` "
        f"— expected exactly `('ticker',)`. The bot intentionally "
        "subscribes to ticker channel ONLY (narrower than the wire's "
        "post-D2.5 5-channel DEFAULT_CHANNELS = ticker + matches + "
        "heartbeat + status + level2_batch that the collector uses for "
        "bronze archiving). Widening this set adds CPU + GIL noise to "
        "the bot's spot-feed path with no consumer for the extra "
        "channels (and after D2.5 level2_batch promotion, a high-volume "
        "orderbook firehose). The collector keeps the wider set in "
        "collector/coinbase_archiver.py via DEFAULT_CHANNELS."
    )


def test_bot_feeds_coinbase_no_reach_into_wire_private_default_subscribe():
    """R1-M1 D2.2 RCA pin: ``bot/feeds/coinbase.py`` MUST NOT call
    ``self._wire._default_on_session_start()`` — that's a private wire-
    library method whose rename would silently strand the consumer.

    The consumer builds its subscribe payload via the public helper
    ``coinbase_wire.auth.build_public_subscribe_message`` and dispatches
    via ``WSClient.send_frame``. AST guard: walk Call nodes for any
    ``self._wire._<single-underscore-non-dunder>`` attribute access.
    """
    if not BOT_FEEDS_COINBASE.is_file():
        pytest.skip(f"{BOT_FEEDS_COINBASE} missing")
    src = BOT_FEEDS_COINBASE.read_text()
    tree = ast.parse(src)
    private_reach_sites: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        # Want: self._wire._something (where ._something is single-leading
        # underscore, not dunder)
        if not (node.attr.startswith("_")
                and not node.attr.startswith("__")):
            continue
        inner = node.value
        if not (isinstance(inner, ast.Attribute) and inner.attr == "_wire"):
            continue
        innermost = inner.value
        if (isinstance(innermost, ast.Name) and innermost.id == "self"):
            private_reach_sites.append((node.lineno, node.attr))
    assert not private_reach_sites, (
        f"bot/feeds/coinbase.py reaches into private wire-library "
        f"methods at {private_reach_sites}. Per R1-M1 D2.2 RCA: "
        "consumers MUST NOT call `self._wire._<private>()` — a wire-"
        "side rename would silently strand the consumer (AttributeError "
        "swallowed by the wire's outer try/except). Use the public "
        "API: `coinbase_wire.auth.build_public_subscribe_message` + "
        "`self._wire.send_frame`."
    )
