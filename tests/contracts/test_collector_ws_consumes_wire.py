"""D1.1.5 — ``collector/`` MUST delegate auth + WS transport to ``kalshi_wire``.

Ticket 86b9zdhz2 (2026-05-16). Bot-side peer at
``test_bot_feeds_kalshi_delegates_to_wire.py``. Together they prove the
"two sides of the same coin" symmetry the 2026-05-16 advisor pivot
mandates: BOTH consumers stay on the shared library; neither builds an
inline parser.

Per D0.3 §5 AMENDMENT 2026-05-16:
  - ``collector/auth.py`` → DELETED at D1.1.5; replaced by
    ``from kalshi_wire.auth import sign, load_private_key``
  - ``collector/ws_connection.py`` → THIN consumer of
    ``kalshi_wire.ws_client.WSClient``; pipes Frame.raw through
    ``collector.writer.BronzeWriter``.

Scope split:
  - **Auth delegation (Phase 3a/4a — this session or next)**: enforced
    once ``collector/auth.py`` is deleted in favor of kalshi_wire import.
    Until then, xfail.
  - **WS delegation (Phase 4 — next session)**: ``ws_connection.py``
    rewrite consumes WSClient. Until then, xfail.

This test pairs with the import-linter ``collector-no-bot`` contract
(unchanged at D1.1.5) — collector still cannot reach into bot/, but it
SHOULD reach into kalshi_wire/. Together they enforce the right shape:
the collector talks to Kalshi via the SHARED library, not via the BOT.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
COLLECTOR_AUTH = REPO_ROOT / "collector" / "auth.py"
COLLECTOR_WS = REPO_ROOT / "collector" / "ws_connection.py"


def _collect_imports(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    src = path.read_text()
    if not src.strip():
        return set()
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


# ─── 1. collector/auth.py — DELETED post-D1.1.5 Phase 4 ─────────────────────


def test_collector_auth_file_is_deleted():
    """D1.1.5 Phase 4: ``collector/auth.py`` is DELETED, replaced by
    ``from kalshi_wire.auth import sign, load_private_key`` in
    ``collector/ws_connection.py``. Re-creating ``collector/auth.py``
    would forfeit the "two sides of the same coin" symmetry the
    2026-05-16 §5 amendment mandates — fail the contract loudly.
    """
    assert not COLLECTOR_AUTH.exists(), (
        f"{COLLECTOR_AUTH} still exists. Per D1.1.5, the "
        "collector/auth.py duplicate is REPLACED by `from kalshi_wire.auth "
        "import sign, load_private_key`."
    )


# ─── 2. collector/ws_connection.py — consumes kalshi_wire.ws_client ─────────


def test_collector_ws_connection_imports_ws_client():
    """``collector/ws_connection.py`` imports ``WSClient`` from
    ``kalshi_wire.ws_client`` (Phase 4 wire-up).
    """
    imports = _collect_imports(COLLECTOR_WS)
    has_ws_client = any(
        i == "kalshi_wire.ws_client" or i.startswith("kalshi_wire.ws_client.")
        for i in imports
    )
    assert has_ws_client, (
        "collector/ws_connection.py does NOT import kalshi_wire.ws_client. "
        "Phase 4 wire-up should rewrite the stub to consume WSClient and "
        "pipe Frame.raw to collector.writer.BronzeWriter."
    )


def test_collector_ws_connection_does_not_reimplement_rsa_pss():
    """``collector/ws_connection.py`` MUST NOT inline its own RSA-PSS
    construction — that's exactly the duplication D0.3 §5 paragraph 6's
    AMENDMENT was designed to eliminate.

    Passes today (the D1.1 stub is docstring-only); stays a ratchet for
    Phase 4 wire-up so the rewrite cannot reintroduce inline PSS.
    """
    if not COLLECTOR_WS.is_file():
        pytest.skip("collector/ws_connection.py missing")
    src = COLLECTOR_WS.read_text()
    if not src.strip():
        pytest.skip("collector/ws_connection.py is empty stub")
    tree = ast.parse(src)
    inline_pss_sites: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if (node.func.attr == "PSS" and
                    isinstance(node.func.value, ast.Name) and
                    node.func.value.id == "padding"):
                inline_pss_sites.append(node.lineno)
    assert not inline_pss_sites, (
        f"collector/ws_connection.py contains inline padding.PSS(...) at "
        f"lines {inline_pss_sites}. After D1.1.5, RSA-PSS lives in "
        "kalshi_wire.auth — delegate, don't reconstruct."
    )


# ─── 3. D0.3 §2 wire_recv_ts capture-at-ingress (D1.2 R1-C1) ────────────────


def test_bronze_archiver_passes_frame_wire_recv_ts_to_build_envelope():
    """D0.3 §2 invariant: ``_wire_recv_ts`` is captured at frame ingress
    BEFORE deserialization — not at on_frame-callback-dispatch time.

    WSClient stamps ``Frame.wire_recv_ts`` at the recv site (BEFORE
    ``json.loads``); BronzeArchiver._on_frame MUST forward it to
    ``build_envelope(wire_recv_ts=...)`` rather than letting the
    default-None substitute ``datetime.now(UTC)`` at dispatch time.
    The difference grows under load — silver QA's "detect gaps via
    _wire_recv_ts" stops being trustworthy if dispatch-time leaks in.

    R1-C1 pin: the kwarg landed on ``build_envelope`` with a back-compat
    default-None that masks the bug if the caller is silent. This test
    AST-walks ``BronzeArchiver._on_frame`` and asserts the
    ``build_envelope(...)`` call site passes ``wire_recv_ts=`` explicitly.
    """
    if not COLLECTOR_WS.is_file():
        pytest.skip("collector/ws_connection.py missing")
    src = COLLECTOR_WS.read_text()
    tree = ast.parse(src)

    found_build_envelope_calls: list[tuple[int, set[str]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            fn_name = None
            if isinstance(fn, ast.Name):
                fn_name = fn.id
            elif isinstance(fn, ast.Attribute):
                fn_name = fn.attr
            if fn_name == "build_envelope":
                kwarg_names = {kw.arg for kw in node.keywords if kw.arg}
                found_build_envelope_calls.append((node.lineno, kwarg_names))

    assert found_build_envelope_calls, (
        "no build_envelope(...) call found in collector/ws_connection.py — "
        "BronzeArchiver._on_frame should construct the bronze envelope "
        "via kalshi_wire.build_envelope."
    )
    for lineno, kwargs in found_build_envelope_calls:
        assert "wire_recv_ts" in kwargs, (
            f"collector/ws_connection.py:{lineno} calls build_envelope(...) "
            f"WITHOUT the wire_recv_ts= kwarg (kwargs present: "
            f"{sorted(kwargs)}). D0.3 §2 capture-at-ingress invariant "
            f"REQUIRES forwarding Frame.wire_recv_ts; default-None falls "
            f"back to datetime.now() at dispatch-callback time, which "
            f"silently corrupts bronze timestamps under any load."
        )
