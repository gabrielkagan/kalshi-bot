"""Sprint 13 Bit 13.5 — vps_mcp_server.py canned tools expand (2026-05-12).

Adds 4 read-only canned tools to `scripts/vps_mcp_server.py`:

  1. `get_settled_outcomes(asset, since_minutes, limit)` — recent
     `settled_trades` rows with NET PnL (gross − fees) and W/L
     breakdown.
  2. `get_active_window_summary()` — latest evaluation per
     product_type + NULL counts on `raw_prob`/`calibrated_prob`
     across the last 30 min. DB-only (no live API call); mirrors
     the post-deploy-verify pattern from bit-12.1-fu1.
  3. `get_journal_tail(file_basename, lines)` — tail of an
     allow-listed `*.jsonl` journal file. Allow-list is sourced
     from `bot/constants.py` so adding journals is one-file. No
     arbitrary path injection.
  4. `get_error_summary(since_minutes)` — journalctl trawl,
     bucket by signature (last 120 chars), with pre-existing-
     pattern suppression list (KXUELGAME 400, SPX-price-stale,
     etc.) baked in.

All tools are READ-ONLY:
  - SQL uses positional parameters; no f-string SQL.
  - `subprocess.run` is list-form everywhere; no `shell=True`.
  - `get_journal_tail` allow-lists basenames and rejects any
    path-separator char (no traversal).

The MCP server runs on the VPS; this test does NOT exercise the
MCP wire protocol. It imports the script module under a
`sys.modules` stub for `mcp.server` / `mcp.types` (the dep is
present on the VPS but not in the local dev env) and exercises
the underlying async helpers directly with a tmp_path sqlite DB.

Operator restart-MCP-server action required post-deploy — see
`kb/decisions/bit-13.5-shipped-may12.md` closeout doc.
"""
from __future__ import annotations

import asyncio
import ast
import importlib.util
import json
import os
import sqlite3
import sys
import types
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SERVER_PY = REPO_ROOT / "scripts" / "vps_mcp_server.py"


# ---------------------------------------------------------------------------
# Stub `mcp` package so the script can be imported in a dev env that doesn't
# have the real dep installed.
# ---------------------------------------------------------------------------

class _StubTool:
    def __init__(self, name="", description="", inputSchema=None, **kw):
        self.name = name
        self.description = description
        self.inputSchema = inputSchema or {}


class _StubTextContent:
    def __init__(self, type="text", text=""):
        self.type = type
        self.text = text


class _StubServer:
    """Minimal stand-in for mcp.server.Server.

    Captures the @list_tools and @call_tool decorated coroutines on the
    instance so the tests can invoke them directly.
    """

    def __init__(self, name):
        self.name = name
        self._list_tools_fn = None
        self._call_tool_fn = None

    def list_tools(self):
        def decorator(fn):
            self._list_tools_fn = fn
            return fn
        return decorator

    def call_tool(self):
        def decorator(fn):
            self._call_tool_fn = fn
            return fn
        return decorator

    def create_initialization_options(self):
        return {}

    async def run(self, *a, **kw):  # pragma: no cover — not exercised
        return None


def _install_mcp_stub():
    """Install minimal mcp.* stubs into sys.modules before importing the script."""
    mcp_pkg = types.ModuleType("mcp")
    mcp_server_pkg = types.ModuleType("mcp.server")
    mcp_server_pkg.Server = _StubServer
    mcp_stdio = types.ModuleType("mcp.server.stdio")

    async def _fake_stdio_server():  # pragma: no cover
        class _Ctx:
            async def __aenter__(self):
                return (None, None)

            async def __aexit__(self, *a):
                return False
        return _Ctx()

    # `async with stdio_server() as ...` — needs a callable returning an async ctx mgr.
    class _StdioCM:
        async def __aenter__(self):
            return (None, None)

        async def __aexit__(self, *a):
            return False

    def stdio_server():
        return _StdioCM()

    mcp_stdio.stdio_server = stdio_server
    mcp_types = types.ModuleType("mcp.types")
    mcp_types.Tool = _StubTool
    mcp_types.TextContent = _StubTextContent

    sys.modules["mcp"] = mcp_pkg
    sys.modules["mcp.server"] = mcp_server_pkg
    sys.modules["mcp.server.stdio"] = mcp_stdio
    sys.modules["mcp.types"] = mcp_types


@pytest.fixture(scope="module")
def server_module():
    """Load scripts/vps_mcp_server.py as a module, with `mcp` stubbed."""
    _install_mcp_stub()
    spec = importlib.util.spec_from_file_location("vps_mcp_server_under_test", SERVER_PY)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def seeded_db(tmp_path, server_module, monkeypatch):
    """Create a state.db with minimal table shapes the new tools need."""
    db_path = tmp_path / "state.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE settled_trades (
            ticker TEXT PRIMARY KEY,
            event_ticker TEXT,
            asset TEXT,
            market_result TEXT,
            side TEXT,
            count INTEGER,
            entry_price_cents INTEGER,
            revenue_cents INTEGER,
            fee_cents INTEGER,
            pnl_cents INTEGER,
            settled_at TEXT,
            product_type TEXT
        );
        CREATE TABLE evaluated_opportunities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ticker TEXT,
            event_ticker TEXT,
            asset TEXT,
            filter_stage TEXT,
            rejection_reason TEXT,
            evaluation_time TEXT,
            spot_price REAL,
            threshold REAL,
            volatility REAL,
            market_price INTEGER,
            seconds_to_close REAL,
            calibrated_prob REAL,
            raw_prob REAL,
            edge REAL,
            ofa_adjustment REAL,
            status TEXT,
            market_result TEXT,
            counterfactual_pnl REAL,
            product_type TEXT
        );
        """
    )
    # Seed settled_trades: 2 BTC wins + 1 ETH loss, all in last 30 min
    conn.executemany(
        "INSERT INTO settled_trades VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            ("BTC-A", "EV-A", "BTC", "yes", "yes", 10, 50, 1000, 10, 500,
             "2026-05-12 09:30:00", "15m"),
            ("BTC-B", "EV-B", "BTC", "yes", "yes", 8, 60, 800, 8, 320,
             "2026-05-12 09:45:00", "15m"),
            ("ETH-A", "EV-C", "ETH", "no", "yes", 5, 70, 0, 5, -350,
             "2026-05-12 09:50:00", "15m"),
            # Old row outside window:
            ("BTC-OLD", "EV-OLD", "BTC", "yes", "yes", 2, 30, 200, 2, 140,
             "2026-01-01 00:00:00", "15m"),
        ],
    )
    # Seed evaluated_opportunities: latest per product_type
    conn.executemany(
        "INSERT INTO evaluated_opportunities "
        "(ticker, asset, filter_stage, evaluation_time, raw_prob, calibrated_prob, product_type) "
        "VALUES (?,?,?,?,?,?,?)",
        [
            ("T1", "BTC", "candidate", "2026-05-12 09:55:00", 0.55, 0.54, "15m"),
            ("T2", "BTC", "candidate", "2026-05-12 09:50:00", None, None, "15m"),
            ("T3", "ETH", "candidate", "2026-05-12 09:54:00", 0.60, 0.59, "hourly"),
            ("T4", "BTC", "candidate", "2026-05-12 09:56:00", 0.61, None, "spx_hourly"),
        ],
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(server_module, "DB_PATH", str(db_path))
    return db_path


# ---------------------------------------------------------------------------
# 1. Tool registration — each new tool name appears in list_tools()
# ---------------------------------------------------------------------------

NEW_TOOL_NAMES = [
    "get_settled_outcomes",
    "get_active_window_summary",
    "get_journal_tail",
    "get_error_summary",
]


def test_new_tools_registered(server_module):
    """Each of the 4 new canned tools is in the list_tools() output."""
    list_fn = server_module.server._list_tools_fn
    assert list_fn is not None, "list_tools decorator was not captured"
    tools = asyncio.run(list_fn())
    registered_names = {t.name for t in tools}
    for name in NEW_TOOL_NAMES:
        assert name in registered_names, f"Tool '{name}' missing from list_tools()"


def test_each_new_tool_has_input_schema(server_module):
    """Each new tool's inputSchema is a valid object schema with the required-key
    field set (even if empty)."""
    list_fn = server_module.server._list_tools_fn
    tools = {t.name: t for t in asyncio.run(list_fn())}
    for name in NEW_TOOL_NAMES:
        t = tools[name]
        schema = t.inputSchema
        assert isinstance(schema, dict), f"{name}: inputSchema must be a dict"
        assert schema.get("type") == "object", (
            f"{name}: inputSchema.type must be 'object'"
        )
        assert "properties" in schema, f"{name}: inputSchema missing 'properties'"


def test_new_tools_dispatched_in_call_tool(server_module):
    """`call_tool` dispatch must route each new tool name to a handler."""
    src = SERVER_PY.read_text()
    for name in NEW_TOOL_NAMES:
        assert f'"{name}"' in src, (
            f"call_tool dispatch literal missing for '{name}'"
        )


# ---------------------------------------------------------------------------
# 2. SQL injection guards — no f-string SQL in any tool, all params parametrized
# ---------------------------------------------------------------------------

def _walk_sql_str_nodes(tree):
    """Yield (lineno, value) for any sqlite execute() first-arg string literal."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            attr = None
            if isinstance(func, ast.Attribute):
                attr = func.attr
            if attr in ("execute", "executemany", "executescript"):
                if node.args:
                    arg0 = node.args[0]
                    yield arg0


def test_no_fstring_sql_in_new_tool_helpers(server_module):
    """f-string interpolation into execute() is a SQL-injection smell.

    EXCEPTION: the existing `_tool_get_data_health` builds NULL-rate
    queries by iterating over a FIXED Python literal allow-list
    (`["raw_prob", "egarch_sigma", "rk_sigma", "calibrated_prob"]`).
    Those f-strings cannot accept attacker input — the loop iterates
    over a module-local literal, not over `arguments[...]`. Pre-Bit-13.5
    behavior preserved.

    Rule for THIS Bit's new tools: zero f-string execute() calls AND
    zero f-string SQL building anywhere in the helper body (catches
    both direct `conn.execute(f"...")` and the indirect
    `sql = f"..."` + `conn.execute(sql)` shape).
    """
    src = SERVER_PY.read_text()
    tree = ast.parse(src)

    # Collect ALL execute*() string-arg nodes, then map to enclosing-function
    # name to test only the new tool helpers.
    new_helper_names = {
        "_tool_get_settled_outcomes",
        "_tool_get_active_window_summary",
        "_tool_get_journal_tail",
        "_tool_get_error_summary",
    }

    # SQL verbs that mark a string as SQL-shaped for this scan.
    import re as _re
    sql_verb_re = _re.compile(
        r"\b(SELECT|FROM|WHERE|GROUP BY|ORDER BY)\b", _re.IGNORECASE
    )

    offenders = []
    for func_node in ast.walk(tree):
        if not isinstance(func_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if func_node.name not in new_helper_names:
            continue
        # Pass 1: direct execute()-arg checks.
        for arg0 in _walk_sql_str_nodes(func_node):
            if isinstance(arg0, ast.JoinedStr):  # f-string
                offenders.append((func_node.name, arg0.lineno, "execute(f'..')"))
            elif isinstance(arg0, ast.BinOp) and isinstance(arg0.op, (ast.Mod, ast.Add)):
                offenders.append((func_node.name, arg0.lineno, "execute('..' + 'x')"))

        # Pass 2: ANY f-string anywhere in the helper whose collapsed
        # value contains a SQL verb (indirect SQL-build via a temp var).
        for inner in ast.walk(func_node):
            if not isinstance(inner, ast.JoinedStr):
                continue
            parts = []
            for v in inner.values:
                if isinstance(v, ast.Constant) and isinstance(v.value, str):
                    parts.append(v.value)
                else:
                    parts.append("?")
            collapsed = "".join(parts)
            if sql_verb_re.search(collapsed):
                offenders.append(
                    (func_node.name, inner.lineno, "f-string SQL build")
                )

    assert not offenders, (
        f"f-string / % / + SQL building forbidden in new tool helpers: {offenders}"
    )


# ---------------------------------------------------------------------------
# 3. get_journal_tail — allow-list enforcement (no path traversal)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "evil_input",
    [
        "../../../etc/passwd",
        "/etc/passwd",
        "scan_journal.jsonl/../../../etc/passwd",
        "..\\..\\..\\windows\\system32\\config\\sam",
        "subdir/scan_journal.jsonl",
        "",  # empty
        "scan_journal.txt",  # wrong ext
        "secret_file.jsonl",  # not allow-listed
    ],
)
def test_get_journal_tail_rejects_path_traversal(server_module, evil_input):
    """Any path-separator, traversal, or non-allow-listed name must be rejected."""
    fn = getattr(server_module, "_tool_get_journal_tail", None)
    assert fn is not None, "_tool_get_journal_tail missing"
    result = asyncio.run(fn({"file_basename": evil_input}))
    assert isinstance(result, list) and result, "tool must return a list of TextContent"
    text = result[0].text.lower()
    assert (
        "error" in text or "not allowed" in text or "not in allow-list" in text
    ), f"Evil input {evil_input!r} returned unexpected payload: {text[:200]}"


def test_get_journal_tail_allow_list_is_explicit_in_source(server_module):
    """The allow-list must be a Python literal in the source (not user-driven)."""
    src = SERVER_PY.read_text()
    # Look for the allow-list constant by convention.
    assert "_JOURNAL_ALLOWLIST" in src, (
        "Module must define _JOURNAL_ALLOWLIST as an explicit literal"
    )


# ---------------------------------------------------------------------------
# 4. No `shell=True` anywhere; subprocess calls are list-form
# ---------------------------------------------------------------------------

def test_no_shell_true_anywhere():
    src = SERVER_PY.read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "shell":
            # only fail if literal True
            if isinstance(node.value, ast.Constant) and node.value.value is True:
                pytest.fail(
                    f"shell=True forbidden (line {getattr(node, 'lineno', '?')})"
                )


# ---------------------------------------------------------------------------
# 5. Behavioral pins — each new tool returns the expected shape on the seeded DB
# ---------------------------------------------------------------------------

def test_get_settled_outcomes_net_pnl_and_filter(server_module, seeded_db):
    """Returns recent settled trades with W/L breakdown using NET pnl (gross-fees)."""
    fn = server_module._tool_get_settled_outcomes
    # very wide window so it includes seeded rows regardless of clock skew
    result = asyncio.run(fn({"since_minutes": 24 * 60 * 365 * 10, "limit": 50}))
    payload = json.loads(result[0].text)
    assert "rows" in payload, f"missing 'rows' key: {payload}"
    assert "summary" in payload, f"missing 'summary' key: {payload}"
    summary = payload["summary"]
    # 4 rows total (3 modern + 1 old); not asset-filtered so all 4 must appear
    assert summary["n"] == 4, summary
    # 3 wins (pnl>0 after fees), 1 loss (pnl<0)
    # BTC-A: 500-10=490 W; BTC-B: 320-8=312 W; ETH-A: -350-5=-355 L; BTC-OLD: 140-2=138 W
    assert summary["wins"] == 3
    assert summary["losses"] == 1
    # Net total: 490+312-355+138 = 585
    assert summary["net_pnl_cents"] == 585

    # Asset filter
    result = asyncio.run(fn({"asset": "BTC", "since_minutes": 24 * 60 * 365 * 10}))
    payload = json.loads(result[0].text)
    assert payload["summary"]["n"] == 3
    assert payload["summary"]["losses"] == 0


def test_get_settled_outcomes_asset_filter_uses_param(server_module, seeded_db):
    """Asset filter must use a `?` parameter (no string interpolation)."""
    # Behavioral proof: a SQL-injection-style asset value must NOT execute
    # arbitrary SQL — it just returns 0 rows for that "asset".
    fn = server_module._tool_get_settled_outcomes
    result = asyncio.run(fn({"asset": "BTC'; DROP TABLE settled_trades; --"}))
    payload = json.loads(result[0].text)
    assert payload["summary"]["n"] == 0
    # Re-query without injection — table must still exist
    result2 = asyncio.run(fn({"since_minutes": 24 * 60 * 365 * 10}))
    payload2 = json.loads(result2[0].text)
    assert payload2["summary"]["n"] == 4


def test_get_active_window_summary_shape(server_module, seeded_db):
    """Returns latest_per_product_type + null_counts on raw_prob/calibrated_prob."""
    fn = server_module._tool_get_active_window_summary
    result = asyncio.run(fn({}))
    payload = json.loads(result[0].text)
    assert "latest_per_product_type" in payload
    assert "null_counts_last_30min" in payload
    # The null_counts dict must have entries for the two key cols.
    nulls = payload["null_counts_last_30min"]
    assert "raw_prob" in nulls
    assert "calibrated_prob" in nulls


def test_get_journal_tail_reads_allowlisted_file(server_module, tmp_path, monkeypatch):
    """Allow-listed file is read; lines returned in tail order."""
    # Write a fake scan_journal.jsonl in a tmp directory
    fake_journal = tmp_path / "scan_journal.jsonl"
    fake_journal.write_text("\n".join(f'{{"line": {i}}}' for i in range(100)) + "\n")
    monkeypatch.setattr(server_module, "REPO_PATH", str(tmp_path))

    fn = server_module._tool_get_journal_tail
    result = asyncio.run(fn({"file_basename": "scan_journal.jsonl", "lines": 5}))
    out = result[0].text
    # Should contain the last 5 lines (99 inclusive down to 95)
    assert '"line": 99' in out
    assert '"line": 95' in out
    assert '"line": 94' not in out


def test_get_journal_tail_handles_missing_file(server_module, tmp_path, monkeypatch):
    """Allow-listed but non-existent file returns a clean error, doesn't crash."""
    monkeypatch.setattr(server_module, "REPO_PATH", str(tmp_path))
    fn = server_module._tool_get_journal_tail
    result = asyncio.run(fn({"file_basename": "scan_journal.jsonl", "lines": 5}))
    out = result[0].text.lower()
    assert "not found" in out or "no such file" in out or "missing" in out


def test_get_error_summary_returns_buckets(server_module, monkeypatch):
    """Mocks journalctl output; verifies bucketing + suppression."""
    fake_log = "\n".join([
        "INFO startup",
        "ERROR foo failed because X",
        "ERROR foo failed because X",  # duplicate signature
        "ERROR KXUELGAME-XXX returned 400 (suppressed)",
        "Traceback (most recent call last):",
        "  File foo.py line 1",
        "ValueError: bar",
        "INFO heartbeat",
    ])
    monkeypatch.setattr(server_module, "_run_cmd", lambda cmd, timeout=10: fake_log)
    fn = server_module._tool_get_error_summary
    result = asyncio.run(fn({"since_minutes": 30}))
    payload = json.loads(result[0].text)
    assert "buckets" in payload
    assert "total_error_lines" in payload
    assert "suppressed_count" in payload
    # The KXUELGAME 400 line is in the suppress list and must be excluded.
    assert payload["suppressed_count"] >= 1


# ---------------------------------------------------------------------------
# 6. Read-only invariant: no tool calls a known mutating method
# ---------------------------------------------------------------------------

def test_no_new_tool_calls_mutating_sql(server_module):
    """Static scan: new-tool helpers' SELECT/execute string args contain no
    INSERT/UPDATE/DELETE/DROP/ALTER/CREATE/REPLACE/ATTACH/DETACH keywords.

    We scope to string literals that are arguments to `execute*` calls
    inside the new tool helpers. Bare strings elsewhere (e.g. the
    `errors="replace"` kwarg of `open()` in `_tool_get_journal_tail`)
    are not SQL and not in scope.
    """
    src = SERVER_PY.read_text()
    tree = ast.parse(src)
    new_helper_names = {
        "_tool_get_settled_outcomes",
        "_tool_get_active_window_summary",
        "_tool_get_journal_tail",
        "_tool_get_error_summary",
    }
    import re as _re
    pattern = _re.compile(
        r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|REPLACE|ATTACH|DETACH)\b",
        _re.IGNORECASE,
    )
    for func_node in ast.walk(tree):
        if not isinstance(func_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if func_node.name not in new_helper_names:
            continue
        # Collect only string literals passed as the first arg to execute*().
        for inner in ast.walk(func_node):
            if not isinstance(inner, ast.Call):
                continue
            func = inner.func
            attr = func.attr if isinstance(func, ast.Attribute) else None
            if attr not in ("execute", "executemany", "executescript"):
                continue
            if not inner.args:
                continue
            arg0 = inner.args[0]
            # Plain string literal or JoinedStr (f-string) collapsed to value
            if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
                if pattern.search(arg0.value):
                    pytest.fail(
                        f"{func_node.name} execute() contains a mutating SQL keyword: "
                        f"{arg0.value[:80]!r}"
                    )
            elif isinstance(arg0, ast.JoinedStr):
                # Test_no_fstring_sql guards this separately, but defensively
                # scan literal parts here too.
                for part in arg0.values:
                    if isinstance(part, ast.Constant) and isinstance(part.value, str):
                        if pattern.search(part.value):
                            pytest.fail(
                                f"{func_node.name} execute() contains a mutating SQL keyword (f-string part): "
                                f"{part.value[:80]!r}"
                            )


# ---------------------------------------------------------------------------
# 7. Existing tools still register (regression seal — Bit 13.5 must not drop any)
# ---------------------------------------------------------------------------

EXISTING_TOOLS = {
    "query_db",
    "get_recent_logs",
    "get_bot_status",
    "get_data_health",
    "checkpoint_wal",
}


def test_existing_tools_still_registered(server_module):
    list_fn = server_module.server._list_tools_fn
    tools = {t.name for t in asyncio.run(list_fn())}
    for name in EXISTING_TOOLS:
        assert name in tools, f"Regression: existing tool '{name}' dropped"
