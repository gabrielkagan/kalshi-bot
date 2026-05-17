"""D1.3-fu3 — `collector/ws_connection.py` MUST construct kalshi_wire WSClient
with `ping_timeout=30.0` (ticket 86b9zjyr0, 2026-05-17).

Pre-D1.3-fu3 the collector relied on the kalshi_wire default `ping_timeout=10.0`,
inherited from the bot's tight-loop posture. Post-D1.3-fu1 (ws_max_size raised
to 16 MiB), Kalshi sends 3-4 MiB subscribe-ack messages; processing those on
the single asyncio thread blocks the loop past the 10s ping-timeout window →
WS lib raises 1011 (internal error) → reconnect storm.

D1.3-fu3 stopgap: pass `ping_timeout=30.0` at BOTH WSClient instantiation
sites in `collector/ws_connection.py` (the `if url is None` and `else url=url`
branches of BronzeArchiver.__init__). This gives the asyncio loop 3x more
headroom before declaring the conn dead. Bot's default (10s) is unchanged —
bot's loop is fast (~50-100 tickers).

This file pins both call sites carry the kwarg. Drift would re-open the 1011
storm class.

Pins:
  1. Both `WSClient(...)` calls in `collector/ws_connection.py` pass
     `ping_timeout=` as a keyword argument.
  2. Each passes the value 30.0 (or a higher float — guards against
     future-tightening drift back toward 10).
  3. The value is strictly greater than the kalshi_wire library default
     (10.0) — sanity check that the override actually raises the timeout
     above the lib default.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path


_COLLECTOR_WS_CONNECTION = (
    Path(__file__).resolve().parents[2] / "collector" / "ws_connection.py"
)


def _ws_client_call_kwargs() -> list[dict[str, ast.AST]]:
    """Return the keyword args of every `WSClient(...)` call site in
    collector/ws_connection.py.

    Each entry is a dict ``{kwarg_name: ast.AST_value_node}``. Used by
    the per-site assertions below to locate the `ping_timeout` kwarg
    and verify its value.
    """
    src = _COLLECTOR_WS_CONNECTION.read_text()
    tree = ast.parse(src)
    out: list[dict[str, ast.AST]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_ws_client = (
            (isinstance(func, ast.Name) and func.id == "WSClient")
            or (isinstance(func, ast.Attribute) and func.attr == "WSClient")
        )
        if is_ws_client:
            kwargs = {
                kw.arg: kw.value for kw in node.keywords if kw.arg is not None
            }
            out.append(kwargs)
    return out


def _kw_value_as_float(node: ast.AST) -> float | None:
    """Best-effort: return the float value of a literal kwarg or None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return float(node.value)
    return None


def test_collector_ws_client_call_sites_present():
    """Sanity: collector/ws_connection.py contains at least one
    `WSClient(...)` instantiation. If this fires, the file shape has
    changed since D1.3-fu3 and the sister tests' assumptions are
    invalidated.
    """
    calls = _ws_client_call_kwargs()
    assert len(calls) >= 1, (
        "No `WSClient(...)` call sites found in collector/ws_connection.py. "
        "The collector wire may have been refactored — re-check the D1.3-fu3 "
        "ping_timeout invariant against the new structure."
    )


def test_every_collector_ws_client_call_passes_ping_timeout():
    """EVERY WSClient instantiation in collector/ws_connection.py MUST
    pass `ping_timeout=`. The `if url is None` and `else` branches of
    BronzeArchiver.__init__ both construct a WSClient; both MUST pin
    the elevated timeout to avoid the 1011 storm class on either branch.
    """
    calls = _ws_client_call_kwargs()
    for i, kwargs in enumerate(calls):
        assert "ping_timeout" in kwargs, (
            f"WSClient(...) call #{i+1} in collector/ws_connection.py is "
            f"missing the `ping_timeout=...` kwarg. Without the override "
            f"the collector inherits the kalshi_wire default (10s) which "
            f"is too tight for large-Kalshi-ack processing — reopens the "
            f"D1.3-fu3 1011 storm class on whichever code path takes the "
            f"unpinned branch. Got kwargs: {sorted(kwargs.keys())}. See "
            f"kb/decisions/d1-3-fu3-ping-timeout-plan.md."
        )


def test_collector_ping_timeout_value_at_least_30_seconds():
    """The configured `ping_timeout` must be ≥ 30.0 seconds.

    30s gives ~3x the lib default headroom for the asyncio loop to
    process large incoming acks before a ping is declared lost. Lower
    values risk re-opening the 1011 storm; pin the floor here so a
    future "tightening" patch can't silently regress.
    """
    calls = _ws_client_call_kwargs()
    for i, kwargs in enumerate(calls):
        node = kwargs.get("ping_timeout")
        if node is None:
            # Already asserted in sister test; skip the value check here
            # to keep the per-test failure messages narrowly scoped.
            continue
        value = _kw_value_as_float(node)
        assert value is not None, (
            f"WSClient(...) call #{i+1} in collector/ws_connection.py passes "
            f"`ping_timeout` as a non-literal expression "
            f"({ast.dump(node)!r}). The pin needs a literal float so the "
            f"contract test can verify the value statically. If runtime "
            f"tunability is desired, encode the literal here as a default "
            f"and wire env-override via a constant."
        )
        assert value >= 30.0, (
            f"WSClient(...) call #{i+1} in collector/ws_connection.py passes "
            f"`ping_timeout={value}` — below the 30s floor pinned for D1.3-fu3 "
            f"(86b9zjyr0). Below 30s the asyncio loop may not service the "
            f"ping-pong cycle during large-Kalshi-ack processing → reopens "
            f"the 1011 storm class. Raise to ≥ 30.0 or file a finding doc "
            f"explaining why the bottleneck has been removed."
        )


def test_collector_ping_timeout_strictly_greater_than_lib_default():
    """Sanity: the collector's configured `ping_timeout` must be strictly
    greater than the kalshi_wire library default.

    If a future refactor changes the lib default to a higher value (e.g.,
    a global Bit that raises lib default to 60s), the per-collector
    override would become redundant — but never wrong (callers can pass
    a value ≥ lib default safely). This test asserts the override is
    actually doing what the plan doc claims: raising the timeout above
    what the bot inherits unmodified.
    """
    # Reach the lib default via the WSClient signature so this test
    # auto-tracks any future lib-default change.
    from kalshi_wire.ws_client import WSClient
    sig = inspect.signature(WSClient.__init__)
    lib_default = sig.parameters["ping_timeout"].default
    assert isinstance(lib_default, (int, float)), (
        f"kalshi_wire WSClient.ping_timeout default is non-numeric "
        f"({lib_default!r}); the D1.3-fu3 invariant assumes a numeric "
        f"default to compare against."
    )

    calls = _ws_client_call_kwargs()
    for i, kwargs in enumerate(calls):
        node = kwargs.get("ping_timeout")
        if node is None:
            continue
        value = _kw_value_as_float(node)
        if value is None:
            continue
        assert value > lib_default, (
            f"WSClient(...) call #{i+1} in collector/ws_connection.py passes "
            f"`ping_timeout={value}` — NOT strictly greater than the lib "
            f"default ({lib_default}). The whole point of the override is "
            f"to raise the timeout above what the bot inherits; setting "
            f"equal/lower defeats the D1.3-fu3 invariant."
        )
