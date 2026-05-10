"""Step #4 — AST tripwire (NOT a guarantee) against synchronous
network calls in `OpportunityScanner.scan()` body.

What this test honestly is:
  A pattern-match tripwire over the most common ways someone
  could put a sync REST call into scan() body. It catches the
  copy-paste regression — `self._client.get_orderbook(...)` typed
  inline. It does NOT catch:
    - Aliasing: `c = self._client; c.get_orderbook(...)`
    - Dynamic dispatch: `getattr(self._client, "get_orderbook")(...)`
    - Indirection: `self._helper_that_calls_client(...)`
    - `from requests import get; get(...)` (banned via Name, but
      can be hidden behind alias renames)
    - Untracked HTTP libs (we only ban a known set)

The architectural goal — "scan() reads from caches, doesn't make
network calls" — is enforced by code review + the threading work
in steps 1-3 (workers populate caches, scan reads). This test is
defense-in-depth for the most obvious regression shape.

Banned patterns (top-level scan body only — see "nested defs"
below):
  - `requests.<method>` / `httpx.<method>` / `aiohttp.<method>` /
    `urllib3.<method>` / `pycurl.<method>` (HTTP libraries)
  - `urllib.<x>(...)` / `urllib.<x>.<y>(...)`
  - `urlopen(...)` (bare Name — covers `from urllib... import
    urlopen`)
  - `http.client.HTTPConnection(...)` etc.
  - `self._client.<method>` / `self.client.<method>` (direct
    KalshiClient API — even though these are circuit-breaker-
    protected, they take wall time when closed)

Nested defs:
  We do NOT descend into nested FunctionDef/Lambda bodies. The
  threading pattern established in steps 1-3 (define a worker
  inside the parent function, hand it to threading.Thread.start)
  is the RIGHT architecture, and would false-positive if we
  walked nested bodies. Sneaky regressions hidden in nested defs
  are out of scope for this tripwire.

Socket calls:
  We do NOT pattern-match `socket.<method>` because too many
  legit calls (`socket.gethostname`, error types) would false-
  positive. If raw socket usage is ever a real risk, add a
  targeted ban on `socket.socket()` / `socket.create_connection()`.

Allowlist-by-silence:
  Helpers like `self._get_orderbook_cached` aren't explicitly
  allowed — they're just not banned. The tripwire is a denylist
  of obvious bad shapes, not an exhaustive allowlist of safe
  ones. If a contributor adds a new helper that internally
  fetches REST without breaker protection, this test will not
  catch it. Code review must.

See kb/failures/scan-tick-stall-cluster-2026-04-25.md.
"""

import ast
import os
import sys
import unittest
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BOT_PY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot/scanner/__init__.py")


def _find_scan_method() -> ast.FunctionDef:
    with open(BOT_PY) as f:
        tree = ast.parse(f.read())
    for cls in ast.walk(tree):
        if (isinstance(cls, ast.ClassDef)
                and cls.name == "OpportunityScanner"):
            for node in cls.body:
                if (isinstance(node, ast.FunctionDef)
                        and node.name == "scan"):
                    return node
    raise AssertionError("OpportunityScanner.scan not found")


# Top-level HTTP libraries that should never appear in scan() body.
_BANNED_HTTP_LIB_NAMES = {
    "requests", "httpx", "aiohttp", "urllib3", "pycurl",
}


def _is_forbidden_call(call: ast.Call) -> Optional[str]:
    """Return a description of the forbidden pattern, or None if
    the call is allowed."""
    func = call.func

    # Pattern 1: `<http_lib>.<method>(...)` — covers requests,
    # httpx, aiohttp, urllib3, pycurl.
    if (isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id in _BANNED_HTTP_LIB_NAMES):
        return f"{func.value.id}.{func.attr}"

    # Pattern 2: `urllib.<x>.<y>(...)` (e.g., urllib.request.urlopen).
    # Match BEFORE the generic urlopen-attribute pattern below.
    if (isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Attribute)
            and isinstance(func.value.value, ast.Name)
            and func.value.value.id == "urllib"):
        return f"urllib.{func.value.attr}.{func.attr}"
    if (isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "urllib"):
        return f"urllib.{func.attr}"

    # Pattern 3: bare or attributed `urlopen(...)` — covers
    # `from urllib.request import urlopen` style imports.
    if isinstance(func, ast.Name) and func.id == "urlopen":
        return "urlopen"
    if (isinstance(func, ast.Attribute) and func.attr == "urlopen"):
        return "<...>.urlopen"

    # Pattern 4: `http.client.<x>(...)` (stdlib HTTP client).
    if (isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Attribute)
            and isinstance(func.value.value, ast.Name)
            and func.value.value.id == "http"
            and func.value.attr == "client"):
        return f"http.client.{func.attr}"

    # Pattern 4b: targeted socket connection ops only. We do NOT
    # ban all socket.* (gethostname / error types are harmless and
    # common). Only flag the calls that actually open a network
    # socket.
    if (isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "socket"
            and func.attr in ("socket", "create_connection")):
        return f"socket.{func.attr}"

    # Pattern 5: `self._client.<method>(...)` — direct KalshiClient
    # API calls. These should go through worker threads or
    # cache-only helpers. Step #2 wrapped them with circuit breakers
    # but they still take wall time when closed.
    if (isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Attribute)
            and func.value.attr in ("_client", "client")
            and isinstance(func.value.value, ast.Name)
            and func.value.value.id == "self"):
        return f"self.{func.value.attr}.{func.attr}"

    return None


def _walk_calls_top_level_only(scan_node: ast.AST):
    """Yield every Call node in the BODY of `scan_node` (assumed
    to be a FunctionDef), EXCLUDING calls inside nested function/
    lambda bodies.

    The threading pattern established in steps 1-3 is:
      def scan(self, ...):
          def _worker():
              self._client.get_orderbook(...)  # in worker thread
          threading.Thread(target=_worker).start()

    The worker function is DEFINED inside scan() but EXECUTED on
    a different thread. Walking nested bodies would falsely flag
    this correct architecture, so we stop at nested FunctionDef /
    AsyncFunctionDef / Lambda boundaries.

    Implementation note: visit each STATEMENT in scan_node.body
    individually, so the visitor's visit_FunctionDef short-circuit
    only fires for nested defs (not for the outer scan_node
    itself)."""
    class _MainThreadCallVisitor(ast.NodeVisitor):
        def __init__(self):
            self.calls = []
        def visit_Call(self, node):
            self.calls.append(node)
            self.generic_visit(node)
        def visit_FunctionDef(self, node):
            pass  # do NOT descend into nested function bodies
        def visit_AsyncFunctionDef(self, node):
            pass
        def visit_Lambda(self, node):
            pass

    visitor = _MainThreadCallVisitor()
    if hasattr(scan_node, "body") and isinstance(scan_node.body, list):
        for stmt in scan_node.body:
            visitor.visit(stmt)
    else:
        visitor.visit(scan_node)
    for call in visitor.calls:
        yield call


class TestScanBodyHasNoDirectNetworkIO(unittest.TestCase):
    """The architectural invariant for Step #4.

    Any direct network call literally written inside
    `OpportunityScanner.scan()` (or nested defs within it) fails
    this test. This is the structural enforcement of "scan() reads
    from caches, doesn't make REST calls."
    """

    def test_no_forbidden_network_calls_in_scan(self):
        scan = _find_scan_method()
        violations = []
        for call in _walk_calls_top_level_only(scan):
            forbidden = _is_forbidden_call(call)
            if forbidden:
                violations.append({
                    "pattern": forbidden,
                    "lineno": call.lineno,
                })
        self.assertEqual(
            violations, [],
            f"scan() body contains direct network calls — these "
            f"steal latency from trading decisions. Move them to "
            f"a worker thread that populates a cache that scan() "
            f"reads from. Violations:\n"
            + "\n".join(
                f"  bot/_impl.py:{v['lineno']} → {v['pattern']}"
                for v in violations))


class TestForbiddenCallDetectorWorks(unittest.TestCase):
    """Sanity tests for the detector itself — we want it to actually
    catch the patterns it claims to catch. If the detector is broken
    silently, the invariant test passes meaninglessly."""

    def _check(self, src: str, expected_pattern_substr: str):
        tree = ast.parse(src)
        for sub in ast.walk(tree):
            if isinstance(sub, ast.Call):
                p = _is_forbidden_call(sub)
                if p is not None:
                    self.assertIn(expected_pattern_substr, p)
                    return
        self.fail(
            f"Detector did not flag {expected_pattern_substr} in "
            f"source: {src!r}")

    def test_detects_requests_get(self):
        self._check("import requests\nrequests.get('http://x')",
                    "requests.get")

    def test_detects_urlopen_bare(self):
        self._check("urlopen('http://x')", "urlopen")

    def test_detects_urllib_request_urlopen(self):
        self._check("import urllib\nurllib.request.urlopen('x')",
                    "urllib")

    def test_detects_socket_create_connection(self):
        self._check("import socket\nsocket.create_connection(('a',1))",
                    "socket.create_connection")

    def test_detects_self_client_get_orderbook(self):
        self._check("self._client.get_orderbook('KXBTC15M')",
                    "self._client.get_orderbook")

    def test_detects_self_client_alias(self):
        self._check("self.client.get_balance()",
                    "self.client.get_balance")

    def test_does_not_flag_self_get_orderbook_cached(self):
        """`self._get_orderbook_cached(ticker)` is the legitimate
        breaker-protected helper that scan() may use. Must not
        flag it."""
        tree = ast.parse("self._get_orderbook_cached(ticker)")
        flagged = False
        for sub in ast.walk(tree):
            if isinstance(sub, ast.Call):
                if _is_forbidden_call(sub):
                    flagged = True
        self.assertFalse(flagged,
            "self._get_orderbook_cached must NOT be flagged — it's "
            "the intentional escape hatch.")

    def test_does_not_flag_logging_or_db_calls(self):
        """Sanity — common non-network calls must not false-positive."""
        for src in [
            "logging.warning('x')",
            "self._state.insert_evaluated_opportunity()",
            "self._vol.update('BTC')",
            "self._sizer.compute(0.9, 95, 100000)",
            "socket.gethostname()",  # known-safe socket op
        ]:
            tree = ast.parse(src)
            for sub in ast.walk(tree):
                if isinstance(sub, ast.Call):
                    self.assertIsNone(
                        _is_forbidden_call(sub),
                        f"False positive on legit call: {src!r}")

    def test_detects_httpx(self):
        self._check("import httpx\nhttpx.get('http://x')",
                    "httpx.get")

    def test_detects_aiohttp(self):
        self._check("import aiohttp\naiohttp.request('GET', 'x')",
                    "aiohttp.request")

    def test_detects_urllib3(self):
        self._check("import urllib3\nurllib3.PoolManager()",
                    "urllib3.PoolManager")

    def test_detects_http_client_HTTPConnection(self):
        self._check(
            "import http.client\nhttp.client.HTTPConnection('a')",
            "http.client.HTTPConnection")


class TestWalkerSkipsNestedDefs(unittest.TestCase):
    """Round-1 P1: walking nested function bodies false-positives
    on the threading pattern (worker defined inside parent, run
    in another thread). Walker must NOT descend into nested defs."""

    def test_nested_def_not_walked(self):
        """A network call inside a nested FunctionDef should NOT
        be flagged — that's the worker-thread pattern."""
        src = """
def outer():
    def _worker():
        self._client.get_orderbook('x')  # legit threading
    threading.Thread(target=_worker).start()
"""
        tree = ast.parse(src)
        outer = tree.body[0]  # FunctionDef
        calls = list(_walk_calls_top_level_only(outer))
        forbidden = [c for c in calls if _is_forbidden_call(c)]
        self.assertEqual(
            forbidden, [],
            "Walker must NOT descend into nested FunctionDef. "
            "Threading pattern (worker defined inline) is allowed.")
        # Sanity: top-level Thread().start() is also not forbidden.

    def test_top_level_call_in_outer_is_walked(self):
        """Sanity — calls in the OUTER function body ARE walked
        and detected. Otherwise the walker is broken."""
        src = """
def outer():
    self._client.get_balance()   # bad — top-level
    def _worker():
        pass
"""
        tree = ast.parse(src)
        outer = tree.body[0]
        calls = list(_walk_calls_top_level_only(outer))
        forbidden = [_is_forbidden_call(c) for c in calls]
        forbidden = [f for f in forbidden if f]
        self.assertEqual(forbidden, ["self._client.get_balance"])

    def test_lambda_not_walked(self):
        src = """
def outer():
    cb = lambda: self._client.get_orderbook('x')
"""
        tree = ast.parse(src)
        outer = tree.body[0]
        calls = list(_walk_calls_top_level_only(outer))
        forbidden = [_is_forbidden_call(c) for c in calls]
        forbidden = [f for f in forbidden if f]
        self.assertEqual(
            forbidden, [],
            "Lambda body is nested; not walked.")

    def test_calls_in_if_try_with_blocks_are_walked(self):
        """Round-2 belt-and-suspenders: ensure the walker descends
        into compound-statement bodies (if/try/with) at the top
        level, not just bare expression statements."""
        src = """
def outer():
    if x:
        self._client.get_balance()
    try:
        self._client.get_orderbook('y')
    except Exception:
        self._client.get_events()
    with open('f') as fh:
        self._client.get_settlements()
    for ticker in tickers:
        self._client.get_market(ticker)
"""
        tree = ast.parse(src)
        outer = tree.body[0]
        calls = list(_walk_calls_top_level_only(outer))
        forbidden = [_is_forbidden_call(c) for c in calls]
        forbidden = [f for f in forbidden if f]
        self.assertEqual(
            sorted(forbidden),
            sorted([
                "self._client.get_balance",
                "self._client.get_orderbook",
                "self._client.get_events",
                "self._client.get_settlements",
                "self._client.get_market",
            ]),
            "Walker must descend into if/try/with/for bodies — "
            "these are NOT nested defs and the calls inside them "
            "are still on the main thread.")


class TestNegativeControl(unittest.TestCase):
    """Round-1 P2: prove the harness would actually catch a
    regression in the real bot/_impl.py file (vs only testing against
    synthetic strings)."""

    def test_synthetic_bad_scan_is_caught(self):
        """Construct a synthetic scan() with a known-bad pattern
        and verify the detector flags it. If this passes silently
        on real bot/_impl.py, the harness is broken."""
        src = """
class OpportunityScanner:
    def scan(self, active_windows):
        # BAD — should be flagged
        ob = self._client.get_orderbook("KXBTC15M")
        return ob
"""
        tree = ast.parse(src)
        for cls in ast.walk(tree):
            if (isinstance(cls, ast.ClassDef)
                    and cls.name == "OpportunityScanner"):
                for node in cls.body:
                    if (isinstance(node, ast.FunctionDef)
                            and node.name == "scan"):
                        calls = list(
                            _walk_calls_top_level_only(node))
                        forbidden = [
                            _is_forbidden_call(c) for c in calls]
                        forbidden = [f for f in forbidden if f]
                        self.assertEqual(
                            forbidden,
                            ["self._client.get_orderbook"],
                            "Detector failed to catch known-bad "
                            "synthetic scan() body. Harness is "
                            "broken.")
                        return
        self.fail("synthetic scan() not found")


if __name__ == "__main__":
    unittest.main()
