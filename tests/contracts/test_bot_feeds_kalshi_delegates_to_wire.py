"""D1.1.5 — ``bot/feeds/kalshi.py`` and ``bot/kalshi_client.py`` MUST
delegate auth + transport to ``kalshi_wire``.

Ticket 86b9zdhz2 (2026-05-16). After D1.1.5 ships, the bot must consume
the shared wire library for:

  - **Auth**: ``bot/kalshi_client.py:60`` (``_create_signature``) +
    ``bot/feeds/kalshi.py:768`` (``_create_ws_headers``) → delegate to
    ``kalshi_wire.auth.sign`` / ``kalshi_wire.auth.make_ws_headers``.
  - **WS transport** (Phase 3b): ``bot/feeds/kalshi.py``'s ``_ws_loop`` +
    ``_send_ob_subscribe`` + ``_send_ob_unsubscribe`` + ``_send_ob_get_snapshot``
    + ``_handle_subscribe_ack`` → delegate to
    ``kalshi_wire.ws_client.WSClient``.

This is the AST guard that **prevents re-divergence** — without it, a
future maintainer could copy the auth/transport logic back inline and
break the symmetry. It catches the "two parsers diverge" risk at compile
time, not at runtime.

This test is the BOT-SIDE peer of ``test_collector_ws_consumes_wire.py``
(collector-side). Together they prove both consumers of kalshi_wire stay
on the library, not on inline copies.

Scope split:
  - **Auth delegation (Phase 3a — this session)**: HARD-asserted. bot
    files MUST import from ``kalshi_wire.auth``.
  - **WS transport delegation (Phase 3b — next session)**: SOFT-asserted
    via a parametrized marker. When Phase 3b lands, the marker flips
    from xfail to required.

If this test fails:
- The bot stopped delegating to ``kalshi_wire`` — investigate whether the
  extracted call sites were silently reverted (manual replace_all
  cleanup gone wrong, merge conflict resolved the wrong way).
- ``kalshi_wire.auth.sign`` doesn't exist — D1.1.5 Phase 3a isn't shipped yet.
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
BOT_FEEDS_KALSHI = REPO_ROOT / "bot" / "feeds" / "kalshi.py"
BOT_KALSHI_CLIENT = REPO_ROOT / "bot" / "kalshi_client.py"


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


# ─── 1. Auth delegation (Phase 3a — REQUIRED) ────────────────────────────────


def test_bot_kalshi_client_imports_kalshi_wire_auth():
    """``bot/kalshi_client.py`` imports from ``kalshi_wire.auth``.

    After Phase 3a delegation, ``_create_signature`` and the RSA-PSS
    sign step must route through ``kalshi_wire.auth.sign``. The bot's
    own KalshiClient retains its rate-limiting + circuit-breaker
    wrapper logic; only the cryptographic primitive moves.
    """
    imports = _collect_imports(BOT_KALSHI_CLIENT)
    wire_imports = {i for i in imports if i.startswith("kalshi_wire")}
    assert wire_imports, (
        f"bot/kalshi_client.py does NOT import from kalshi_wire — Phase "
        "3a auth-delegation regressed. Expected at least "
        "`from kalshi_wire.auth import sign` (or sibling)."
    )


def test_bot_feeds_kalshi_imports_kalshi_wire_auth():
    """``bot/feeds/kalshi.py`` imports from ``kalshi_wire.auth``.

    After Phase 3a delegation, ``_create_ws_headers`` (the WS handshake
    auth at line 768-786) must route through ``kalshi_wire.auth``. The
    KalshiFeed class keeps all its state-machinery / queue / sid
    management — only the auth primitive moves.
    """
    imports = _collect_imports(BOT_FEEDS_KALSHI)
    wire_imports = {i for i in imports if i.startswith("kalshi_wire")}
    assert wire_imports, (
        f"bot/feeds/kalshi.py does NOT import from kalshi_wire — Phase "
        "3a auth-delegation regressed. Expected at least "
        "`from kalshi_wire.auth import make_ws_headers` (or sibling)."
    )


def test_bot_kalshi_client_does_not_inline_rsa_pss_padding():
    """After Phase 3a auth-delegation, ``bot/kalshi_client.py`` should
    NOT directly invoke ``padding.PSS(...)`` — the RSA-PSS construction
    moved to ``kalshi_wire.auth.sign``.

    AST guard: walk Call nodes for ``padding.PSS`` attribute access.
    Catches the silent-revert failure mode (someone copies the inline
    PSS block back).
    """
    src = BOT_KALSHI_CLIENT.read_text()
    tree = ast.parse(src)
    inline_pss_sites: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if (node.func.attr == "PSS" and
                    isinstance(node.func.value, ast.Name) and
                    node.func.value.id == "padding"):
                inline_pss_sites.append(node.lineno)
    assert not inline_pss_sites, (
        f"bot/kalshi_client.py contains inline `padding.PSS(...)` calls at "
        f"lines {inline_pss_sites}. After D1.1.5 Phase 3a, RSA-PSS construction "
        "lives in kalshi_wire.auth.sign — the bot client should delegate, "
        "not reconstruct the padding inline."
    )


def test_bot_feeds_kalshi_does_not_inline_rsa_pss_padding():
    """After Phase 3a auth-delegation, ``bot/feeds/kalshi.py`` should NOT
    directly invoke ``padding.PSS(...)`` — see sister test rationale.
    """
    src = BOT_FEEDS_KALSHI.read_text()
    tree = ast.parse(src)
    inline_pss_sites: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if (node.func.attr == "PSS" and
                    isinstance(node.func.value, ast.Name) and
                    node.func.value.id == "padding"):
                inline_pss_sites.append(node.lineno)
    assert not inline_pss_sites, (
        f"bot/feeds/kalshi.py contains inline `padding.PSS(...)` calls at "
        f"lines {inline_pss_sites}. After D1.1.5 Phase 3a, RSA-PSS lives "
        "in kalshi_wire.auth — _create_ws_headers should delegate."
    )


# ─── 2. WS transport delegation (Phase 3b — REQUIRED) ───────────────────────
# Phase 3b extracts the WS transport (connect/reconnect/_send_ob_*/
# _handle_subscribe_ack frame parse) into kalshi_wire.ws_client.WSClient.
# The xfail was flipped OFF in the Phase 3b commit; this is now a hard ratchet.


def test_bot_feeds_kalshi_imports_ws_client():
    """Phase 3b: ``bot/feeds/kalshi.py`` imports ``WSClient`` from
    ``kalshi_wire.ws_client``. The KalshiFeed class wraps WSClient with
    bot-specific queue management, blacklist semantics, force_resubscribe,
    and the orderbook state machine — but the connect/reconnect/auth/
    drain/silence-watchdog skeleton moves to WSClient.
    """
    imports = _collect_imports(BOT_FEEDS_KALSHI)
    has_ws_client = any(
        i == "kalshi_wire.ws_client" or i.startswith("kalshi_wire.ws_client.")
        for i in imports
    )
    assert has_ws_client, (
        "bot/feeds/kalshi.py does NOT import kalshi_wire.ws_client — Phase 3b "
        "WS-transport delegation regressed. Expected "
        "`from kalshi_wire.ws_client import WSClient`."
    )


def test_bot_feeds_kalshi_no_inline_websockets_connect():
    """After Phase 3b extraction, the ``websockets.connect`` call lives
    inside ``kalshi_wire.ws_client.WSClient._ws_loop`` — NOT inline in
    ``bot/feeds/kalshi.py``.

    AST guard against silent reversion: walks Call nodes for the
    ``websockets.connect(...)`` invocation. If a future maintainer copies
    the WS-loop scaffold back inline, this fails. The cancel of WS-loop
    from KalshiFeed is what makes Phase 3b structurally meaningful.
    """
    src = BOT_FEEDS_KALSHI.read_text()
    tree = ast.parse(src)
    inline_connect_sites: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if (node.func.attr == "connect" and
                    isinstance(node.func.value, ast.Name) and
                    node.func.value.id == "websockets"):
                inline_connect_sites.append(node.lineno)
    assert not inline_connect_sites, (
        f"bot/feeds/kalshi.py contains inline `websockets.connect(...)` "
        f"calls at lines {inline_connect_sites}. After D1.1.5 Phase 3b, "
        "the WS-loop transport lives in kalshi_wire.ws_client.WSClient — "
        "KalshiFeed should consume frames via on_frame callback, not "
        "open its own WS connection."
    )


def test_bot_feeds_kalshi_ws_loop_method_removed():
    """After Phase 3b extraction, the ``async def _ws_loop`` method on
    KalshiFeed no longer exists — the loop body lives in
    ``WSClient._ws_loop`` (private to kalshi_wire).

    KalshiFeed.start() now delegates to ``self._wire.start()`` instead of
    spawning its own asyncio thread.
    """
    src = BOT_FEEDS_KALSHI.read_text()
    tree = ast.parse(src)
    ws_loop_methods: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_ws_loop":
            ws_loop_methods.append(node.lineno)
    assert not ws_loop_methods, (
        f"bot/feeds/kalshi.py still defines `async def _ws_loop` at lines "
        f"{ws_loop_methods}. After D1.1.5 Phase 3b, the WS-loop lives in "
        "kalshi_wire.ws_client.WSClient and KalshiFeed.start() delegates "
        "to self._wire.start()."
    )
