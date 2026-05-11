"""Bit 3.2: KalshiClient circuit-breaker decorator + helpers, extracted from bot/_impl.py.

Per master plan Phase L disambiguation, breakers were originally queued for Bit 4.3 alongside KalshiClient extraction. Bit 3.2 expanded scope to bundle them here per the 4-axis engineering lens (modular + agentic-friendly wins).
"""
import functools
import logging
import os
import time
from typing import Callable

# Sprint 10.5a (2026-05-11): circuit_breaker relocated to bot/infra/. We can't
# top-level-import `bot.infra.circuit_breaker` here — that would violate the
# `helpers-leaf` `.importlinter` contract (bot.helpers.* MUST NOT import sibling
# subpackages). Late-bound inside `_kalshi_breaker` wrapper instead (lazy
# import per-call has negligible overhead since `sys.modules` caches after
# first call; import-linter only inspects module-level imports).

def _extract_tick_error_location(exc) -> str:
    """Return 'basename.py:LINE:func' for the deepest frame of `exc`'s
    traceback, or '?' on any failure.

    Used by the run-loop tick error handler so the operator gets a
    diagnosable Telegram alert without having to chase journalctl.
    Must never raise — it lives inside the exception handler that
    keeps the bot from crashing.

    Chained exceptions: prefers `__cause__` (explicit `raise Y from X`)
    or `__context__` (implicit re-raise) over the wrapper's own
    traceback so the alert points at the original raise site, not
    the re-raise. Falls back to the exception's own traceback if
    no chain is present.

    Implementation walks `tb.tb_next` directly (rather than
    `traceback.extract_tb`) to avoid loading source line text from
    disk inside the error handler.
    """
    try:
        if exc is None:
            return "?"
        # Resolve to the deepest exception in the chain.
        origin = exc
        seen = set()
        while True:
            chained = getattr(origin, "__cause__", None) or getattr(
                origin, "__context__", None)
            if chained is None or id(chained) in seen:
                break
            seen.add(id(origin))
            origin = chained
        tb = getattr(origin, "__traceback__", None)
        if tb is None:
            return "?"
        # Walk to the deepest frame.
        while getattr(tb, "tb_next", None) is not None:
            tb = tb.tb_next
        frame = getattr(tb, "tb_frame", None)
        if frame is None:
            return "?"
        code = getattr(frame, "f_code", None)
        if code is None:
            return "?"
        filename = getattr(code, "co_filename", None) or ""
        funcname = getattr(code, "co_name", None) or "?"
        lineno = getattr(tb, "tb_lineno", None)
        fn = os.path.basename(filename) if filename else "?"
        line_repr = str(lineno) if lineno else "?"
        return f"{fn}:{line_repr}:{funcname}"
    except Exception:
        return "?"


def _kalshi_breaker_success(resp) -> bool:
    """Classifies a `_request()` return value as success/failure for
    the circuit breaker.

    Failure shapes:
      - None: HTTP 4xx/5xx, timeout, network error (definitive)
      - {"error": ...}: explicit Kalshi error wrapper (singular)
      - {"errors": [...]}: plural, used on validation failures

    Round-4 P1 fix (corrected by R5 A1): empty `{}` is NOT treated
    as failure. NOTE: `_request()` returns `{}` only on a 200 with
    literally empty body — Kalshi's actual list endpoints return
    `{"events": []}` / `{"settlements": []}` for empty results, NOT
    `{}`. So an empty `{}` is in fact a Kalshi pathology and is a
    weak degradation signal. We choose to ignore it because (a) it's
    rare in practice, (b) tripping on it caused false-positive
    weekend trips during round-4 review, and (c) the breaker has
    other signals (None on 4xx/5xx) that catch the harder failures.
    Acknowledged trade-off; flagged for revisit if `{}` becomes
    common in real traffic.

    Limitation acknowledged: future Kalshi error shapes that aren't
    `{"error": ...}` or `{"errors": [...]}` (e.g., a hypothetical
    `{"unauth": true}`) would pass as success. If Kalshi changes
    response shape, the new error key needs to be added here. See
    memory/feedback_kalshi_schema_drift for prior incidents of
    silent rename."""
    if resp is None:
        return False
    if isinstance(resp, dict):
        if "error" in resp or "errors" in resp:
            return False
    return True


def _kalshi_series_key(ticker: str, kind: str) -> str:
    """Derive a per-series breaker key from a market ticker. Round-1 A2
    + round-2 A3+A4 fixes: bounded cardinality (one breaker per series,
    not per expiring ticker) and validated input (rejects empty/None).

    KXBTC15M-26APR250000-00, kind="orderbook" → kalshi_orderbook_KXBTC15M.
    """
    if not ticker or not isinstance(ticker, str):
        # Bucket malformed inputs into a single sentinel so they
        # don't poison the registry with garbage keys. Logged once
        # via the breaker itself the first time it trips.
        return f"kalshi_{kind}_unknown"
    series = ticker.split("-", 1)[0] if "-" in ticker else ticker
    return f"kalshi_{kind}_{series}"


def _kalshi_breaker(method):
    """Decorator that wraps a KalshiClient REST GET method with a
    per-key circuit breaker. The decorated method must declare a
    `_breaker_key_fn` and optional `_breaker_recovery_seconds` /
    `_breaker_failures_to_open` attributes via the helper
    `@_breaker_config(...)`. Decorator order matters:

        @_kalshi_breaker        # outer
        @_breaker_config(...)   # inner — sets attributes
        def get_foo(self): ...

    Round-3 P2-1+P2-2 fix: validate at decoration time so a missing
    or wrongly-ordered `@_breaker_config` raises immediately rather
    than producing a silent AttributeError at first call in
    production.

    Round-2 A8 refactor: replaces 9 copies of the try/acquire/
    record_result boilerplate with a single decorator. When the
    contract changes (e.g., add per-call timeout, distinguish 4xx
    vs 5xx), only the decorator changes — not 9 call sites."""
    if not hasattr(method, "_breaker_key_fn"):
        raise TypeError(
            f"@_kalshi_breaker on {method.__qualname__}: missing "
            f"@_breaker_config(...) inner decorator. Decorator order "
            f"must be `@_kalshi_breaker` (outer) then "
            f"`@_breaker_config(...)` (inner).")
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        # Late-bind to avoid module-level `bot.helpers → bot.infra` edge
        # (helpers-leaf .importlinter contract violation post-10.5a).
        from bot.infra.circuit_breaker import REGISTRY as _BREAKER_REGISTRY
        key = method._breaker_key_fn(self, *args, **kwargs)
        breaker = _BREAKER_REGISTRY.get(
            key,
            failures_to_open=getattr(method, "_breaker_failures_to_open", 3),
            recovery_seconds=getattr(method, "_breaker_recovery_seconds", 300))
        gen = breaker.acquire()
        if gen is None:
            return None
        try:
            resp = method(self, *args, **kwargs)
        except Exception:
            breaker.record_result(gen, success=False)
            raise
        breaker.record_result(gen, success=_kalshi_breaker_success(resp))
        return resp
    return wrapper


def _breaker_config(key_fn, *, failures_to_open: int = 3,
                    recovery_seconds: float = 300.0):
    """Attaches breaker config to a function. Use BEFORE @_kalshi_breaker:
        @_kalshi_breaker
        @_breaker_config(key_fn=lambda self, ticker: f"kalshi_orderbook_{ticker.split('-')[0]}",
                         recovery_seconds=120)
        def get_orderbook(self, ticker, depth=10): ...
    """
    def attach(fn):
        fn._breaker_key_fn = key_fn
        fn._breaker_failures_to_open = failures_to_open
        fn._breaker_recovery_seconds = recovery_seconds
        return fn
    return attach
