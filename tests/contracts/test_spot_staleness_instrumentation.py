"""Bit S.1 — CoinbaseFeed spot-staleness instrumentation contract tests.

ClickUp [86ba1wrcg] umbrella [86ba1wrad]. Plan doc at
`kb/decisions/bit-s-1-spot-staleness-instrumentation-plan.md`.

Discovered 2026-05-21: the bot reads `CoinbaseFeed._prices[asset]` (last
WS-streamed trade price, no timestamp) and uses it directly in
`ProbabilityEngine.compute()`. During illiquidity (zero trades for N
minutes) the price persists unchanged in the dict. The bot cannot
distinguish "fresh price" from "price from 10 minutes ago." On Coinbase
BNB-USD this matters: 34% of 1-min windows over May 9-21 had no trades.

This Bit adds INSTRUMENTATION ONLY — no behavior change:
  - `CoinbaseFeed._price_ts: Dict[str, float]` (monotonic seconds)
  - `CoinbaseFeed.get_price_with_ts(asset) -> Optional[Tuple[float, float]]`
  - `evaluated_opportunities.spot_staleness_seconds REAL` column
  - `StateManager.insert_evaluated_opportunity` accepts `spot_staleness_seconds` kwarg
  - `bot/scanner/__init__.py` Coinbase `else` branch at ~line 1634 (reach:
    `_pt in (None, "15m", "hourly")`) computes and writes per-asset staleness
    to `StateManager._scan_spot_staleness_cache` for auto-fill across 115+ insert sites

S.3 is the production gate (deferred — S.2 RCA found zero settled trades
with proxy staleness ≥120s; no empirical PnL signal to justify thresholds today).

This file is RED before any production code lands. The TDD hook permits
edits to bot/feeds/coinbase.py + bot/state.py + bot/scanner/__init__.py
once this test has been edited in the session.
"""
from __future__ import annotations

import ast
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# Mock heavy deps that the import chain might pull in transitively.
_HEAVY = (
    "websockets", "websocket",
    "cryptography",
    "cryptography.hazmat",
    "cryptography.hazmat.primitives",
    "cryptography.hazmat.primitives.serialization",
    "cryptography.hazmat.primitives.hashes",
    "cryptography.hazmat.primitives.asymmetric",
    "cryptography.hazmat.primitives.asymmetric.padding",
)
for _m in _HEAVY:
    sys.modules.setdefault(_m, MagicMock())


REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

COINBASE_PATH = REPO_ROOT / "bot" / "feeds" / "coinbase.py"
STATE_PATH = REPO_ROOT / "bot" / "state.py"
SCANNER_PATH = REPO_ROOT / "bot" / "scanner" / "__init__.py"


# ----------------------------------------------------------------------
# 1. CoinbaseFeed._price_ts dict exists and tracks monotonic timestamps
# ----------------------------------------------------------------------

def test_coinbase_feed_init_creates_price_ts_dict():
    """`CoinbaseFeed.__init__` initializes `_price_ts: Dict[str, float]`
    alongside `_prices`. AST-level check — doesn't import the module.
    Handles both plain `self.x = ...` (ast.Assign) and annotated
    `self.x: T = ...` (ast.AnnAssign) forms."""
    tree = ast.parse(COINBASE_PATH.read_text())
    init_assigns: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == "__init__"):
            continue
        for stmt in ast.walk(node):
            # Plain `self.x = value`
            if isinstance(stmt, ast.Assign):
                for tgt in stmt.targets:
                    if (isinstance(tgt, ast.Attribute)
                            and isinstance(tgt.value, ast.Name)
                            and tgt.value.id == "self"):
                        init_assigns.append(tgt.attr)
            # Annotated `self.x: T = value`
            elif isinstance(stmt, ast.AnnAssign):
                tgt = stmt.target
                if (isinstance(tgt, ast.Attribute)
                        and isinstance(tgt.value, ast.Name)
                        and tgt.value.id == "self"):
                    init_assigns.append(tgt.attr)
    assert "_price_ts" in init_assigns, (
        f"CoinbaseFeed.__init__ must initialize self._price_ts; got {init_assigns}"
    )


def test_coinbase_feed_has_get_price_with_ts_method():
    """AST guard: `def get_price_with_ts` exists in the CoinbaseFeed class body."""
    tree = ast.parse(COINBASE_PATH.read_text())
    method_names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "CoinbaseFeed":
            for item in node.body:
                if isinstance(item, ast.FunctionDef):
                    method_names.append(item.name)
    assert "get_price_with_ts" in method_names, (
        f"CoinbaseFeed must define get_price_with_ts; got {sorted(method_names)}"
    )


def test_get_price_with_ts_returns_none_for_unseen_asset():
    """Behavioral: a freshly constructed feed returns None for any asset.

    NOTE: We construct the feed with a temp persist_path to avoid
    touching the production state buffer. We do NOT spin the sampler;
    the constructor populates dicts only."""
    # Avoid module-level CoinbaseFeed buffer-load side effects: pass
    # a path that doesn't exist so `_load_buffers_from_disk` skips.
    from bot.feeds.coinbase import CoinbaseFeed
    with tempfile.TemporaryDirectory() as tmp:
        feed = CoinbaseFeed(persist_path=os.path.join(tmp, "spot_buffer.json"))
        # Don't start sampler; just probe.
        assert feed.get_price_with_ts("BNB") is None


def test_get_price_with_ts_returns_tuple_after_tick():
    """Behavioral: after a WS frame populates `_prices[asset] = price`
    AND `_price_ts[asset] = mono_ts`, `get_price_with_ts(asset)` returns
    `(price, mono_ts)`. We bypass the WS frame by writing the dicts
    directly under the lock (mirrors what the WS thread does)."""
    from bot.feeds.coinbase import CoinbaseFeed
    with tempfile.TemporaryDirectory() as tmp:
        feed = CoinbaseFeed(persist_path=os.path.join(tmp, "spot_buffer.json"))
        mono_now = time.monotonic()
        with feed._lock:
            feed._prices["BNB"] = 612.34
            feed._price_ts["BNB"] = mono_now
        result = feed.get_price_with_ts("BNB")
        assert result is not None, "expected (price, ts) tuple, got None"
        assert result[0] == pytest.approx(612.34)
        assert result[1] == pytest.approx(mono_now, abs=1e-6)


def test_get_price_backward_compat_returns_float_only():
    """Backward compat: `get_price` still returns just the price (no break)."""
    from bot.feeds.coinbase import CoinbaseFeed
    with tempfile.TemporaryDirectory() as tmp:
        feed = CoinbaseFeed(persist_path=os.path.join(tmp, "spot_buffer.json"))
        with feed._lock:
            feed._prices["BTC"] = 67000.0
            feed._price_ts["BTC"] = time.monotonic()
        assert feed.get_price("BTC") == pytest.approx(67000.0)


# ----------------------------------------------------------------------
# 2. WS frame handler populates _price_ts alongside _prices
# ----------------------------------------------------------------------

def test_ws_frame_handler_sets_price_ts_alongside_prices():
    """AST guard: the function in coinbase.py that writes `self._prices[asset]
    = price` (the WS-frame path) ALSO writes `self._price_ts[asset] = ...`
    on the same path. This is the lock-step that makes staleness measurable."""
    tree = ast.parse(COINBASE_PATH.read_text())
    # Walk every function body; for each function that writes to
    # `self._prices[...]`, verify the same body writes to `self._price_ts[...]`.
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        writes_prices = False
        writes_price_ts = False
        for stmt in ast.walk(node):
            if isinstance(stmt, ast.Assign):
                for tgt in stmt.targets:
                    if (isinstance(tgt, ast.Subscript)
                            and isinstance(tgt.value, ast.Attribute)
                            and isinstance(tgt.value.value, ast.Name)
                            and tgt.value.value.id == "self"):
                        if tgt.value.attr == "_prices":
                            writes_prices = True
                        elif tgt.value.attr == "_price_ts":
                            writes_price_ts = True
        if writes_prices:
            assert writes_price_ts, (
                f"function CoinbaseFeed.{node.name} writes self._prices[...] "
                f"but does NOT write self._price_ts[...] — staleness lock-step broken"
            )


# ----------------------------------------------------------------------
# 3. evaluated_opportunities has spot_staleness_seconds column
# ----------------------------------------------------------------------

def _build_temp_state_db():
    """Spin a temp StateManager that creates the production schema."""
    from bot.state import StateManager
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    sm = StateManager(db_path=tmp.name)
    return sm, tmp.name


def test_evaluated_opportunities_has_spot_staleness_seconds_column():
    """The migration loop in `bot/state.py::_create_tables` MUST add
    `spot_staleness_seconds REAL` to evaluated_opportunities."""
    sm, path = _build_temp_state_db()
    try:
        cols = [row[1] for row in sm.conn.execute(
            "PRAGMA table_info(evaluated_opportunities)"
        ).fetchall()]
        assert "spot_staleness_seconds" in cols, (
            f"evaluated_opportunities is missing spot_staleness_seconds; "
            f"cols = {sorted(cols)}"
        )
    finally:
        sm.conn.close()
        os.unlink(path)


def test_spot_staleness_seconds_column_type_is_real():
    """Confirm column type is REAL (sqlite affinity for float)."""
    sm, path = _build_temp_state_db()
    try:
        info = sm.conn.execute(
            "PRAGMA table_info(evaluated_opportunities)"
        ).fetchall()
        ts_row = [r for r in info if r[1] == "spot_staleness_seconds"]
        assert ts_row, "spot_staleness_seconds missing"
        assert ts_row[0][2].upper() == "REAL", (
            f"spot_staleness_seconds type should be REAL; got {ts_row[0][2]}"
        )
    finally:
        sm.conn.close()
        os.unlink(path)


# ----------------------------------------------------------------------
# 4. insert_evaluated_opportunity signature accepts spot_staleness_seconds kwarg
# ----------------------------------------------------------------------

def test_insert_evaluated_opportunity_accepts_spot_staleness_kwarg():
    """`StateManager.insert_evaluated_opportunity` signature includes
    `spot_staleness_seconds: Optional[float] = None` kwarg."""
    tree = ast.parse(STATE_PATH.read_text())
    sig_kwarg_names: list[str] = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.FunctionDef)
                and node.name == "insert_evaluated_opportunity"):
            args = node.args
            sig_kwarg_names = [a.arg for a in args.args + args.kwonlyargs]
            break
    assert sig_kwarg_names, "insert_evaluated_opportunity not found in bot/state.py"
    assert "spot_staleness_seconds" in sig_kwarg_names, (
        f"insert_evaluated_opportunity missing spot_staleness_seconds kwarg; "
        f"got {sig_kwarg_names}"
    )


def test_insert_evaluated_opportunity_persists_spot_staleness():
    """Behavioral: passing `spot_staleness_seconds=12.5` writes 12.5 to the row."""
    sm, path = _build_temp_state_db()
    try:
        sm.insert_evaluated_opportunity(
            ticker="KXBNB15MTEST-T-12345",
            event_ticker="KXBNB15MTEST",
            asset="BNB",
            filter_stage="candidate",
            spot_price=612.0,
            spot_staleness_seconds=12.5,
            product_type="15m",
        )
        row = sm.conn.execute(
            "SELECT spot_staleness_seconds FROM evaluated_opportunities "
            "WHERE ticker='KXBNB15MTEST-T-12345'"
        ).fetchone()
        assert row is not None, "insert did not persist row"
        assert row[0] == pytest.approx(12.5)
    finally:
        sm.conn.close()
        os.unlink(path)


def test_insert_evaluated_opportunity_defaults_spot_staleness_to_null():
    """Behavioral: passing no spot_staleness writes NULL (Optional default)."""
    sm, path = _build_temp_state_db()
    try:
        sm.insert_evaluated_opportunity(
            ticker="KXBNB15MTEST-T-67890",
            event_ticker="KXBNB15MTEST",
            asset="BNB",
            filter_stage="candidate",
            product_type="15m",
        )
        row = sm.conn.execute(
            "SELECT spot_staleness_seconds FROM evaluated_opportunities "
            "WHERE ticker='KXBNB15MTEST-T-67890'"
        ).fetchone()
        assert row is not None
        assert row[0] is None, (
            f"spot_staleness_seconds default must be NULL; got {row[0]}"
        )
    finally:
        sm.conn.close()
        os.unlink(path)


# ----------------------------------------------------------------------
# 5. scanner wires staleness at the Coinbase scan-path site
#    (reach: `_pt in (None, "15m", "hourly")`)
# ----------------------------------------------------------------------

def test_scanner_calls_get_price_with_ts_for_15m():
    """AST guard: bot/scanner/__init__.py calls `self._feed.get_price_with_ts(asset)`
    at least once (the Coinbase scan-path `else` branch — reach covers
    `_pt in (None, "15m", "hourly")` — replacement for `get_price`).
    Test ID kept `_for_15m` for pytest collection stability; behavior is
    not 15M-restricted."""
    tree = ast.parse(SCANNER_PATH.read_text())
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if (isinstance(func, ast.Attribute)
                    and func.attr == "get_price_with_ts"):
                found = True
                break
    assert found, (
        "bot/scanner/__init__.py must call self._feed.get_price_with_ts(asset) "
        "at the Coinbase scan-path site (reach: _pt in (None, '15m', 'hourly'))"
    )


def test_scanner_passes_spot_staleness_to_insert_evaluated_opportunity():
    """AST guard: at least one `insert_evaluated_opportunity(...)` call in
    bot/scanner/__init__.py passes `spot_staleness_seconds=...`. Most
    insert sites auto-fill via the `_scan_spot_staleness_cache` (no per-
    call kwarg needed); the silent_spot_none branch explicitly passes
    the kwarg because `spot is None` short-circuits the cache write
    upstream. At least ONE call site must carry the kwarg as the
    explicit-kwarg-wins regression pin."""
    tree = ast.parse(SCANNER_PATH.read_text())
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if (isinstance(func, ast.Attribute)
                    and func.attr == "insert_evaluated_opportunity"):
                for kw in node.keywords:
                    if kw.arg == "spot_staleness_seconds":
                        found = True
                        break
            if found:
                break
    assert found, (
        "bot/scanner/__init__.py must pass spot_staleness_seconds=... "
        "to at least one insert_evaluated_opportunity call"
    )


# ----------------------------------------------------------------------
# 6. silent_spot_none branch regression — staleness column doesn't affect
#    the existing skip-on-None behavior
# ----------------------------------------------------------------------

def test_silent_spot_none_filter_stage_string_unchanged():
    """Regression: the existing skip-on-None branch still emits filter_stage
    = 'silent_spot_none'. Verifies the staleness-column add doesn't refactor
    away an existing filter_stage string downstream consumers grep for."""
    src = SCANNER_PATH.read_text()
    assert '"silent_spot_none"' in src or "'silent_spot_none'" in src, (
        "filter_stage='silent_spot_none' string literal removed from scanner"
    )


# ----------------------------------------------------------------------
# 7. R1 fix-up: auto-fill via per-asset cache covers ALL inserts
# ----------------------------------------------------------------------

def test_state_manager_has_scan_spot_staleness_cache():
    """`StateManager.__init__` initializes `_scan_spot_staleness_cache:
    Dict[str, float]`. AST-level — the cache is the read-side of the
    auto-fill plumbing that lets every insert_evaluated_opportunity call
    pick up staleness without threading the kwarg through 115+ sites."""
    tree = ast.parse(STATE_PATH.read_text())
    init_assigns: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name == "__init__"):
            continue
        for stmt in ast.walk(node):
            if isinstance(stmt, ast.Assign):
                for tgt in stmt.targets:
                    if (isinstance(tgt, ast.Attribute)
                            and isinstance(tgt.value, ast.Name)
                            and tgt.value.id == "self"):
                        init_assigns.append(tgt.attr)
            elif isinstance(stmt, ast.AnnAssign):
                tgt = stmt.target
                if (isinstance(tgt, ast.Attribute)
                        and isinstance(tgt.value, ast.Name)
                        and tgt.value.id == "self"):
                    init_assigns.append(tgt.attr)
    assert "_scan_spot_staleness_cache" in init_assigns, (
        f"StateManager.__init__ must initialize _scan_spot_staleness_cache; "
        f"got {init_assigns}"
    )


def test_insert_auto_fills_spot_staleness_from_cache():
    """Behavioral: populate `_scan_spot_staleness_cache[asset]`, then call
    `insert_evaluated_opportunity` WITHOUT passing the kwarg — the persisted
    row should have the cache value. This is the contract that lets every
    filter_stage (candidate, decided_contract, insufficient_edge, …) pick up
    the staleness reading without per-call threading."""
    sm, path = _build_temp_state_db()
    try:
        sm._scan_spot_staleness_cache["BNB"] = 47.5
        sm.insert_evaluated_opportunity(
            ticker="KXBNB15MTEST-T-AUTOFILL",
            event_ticker="KXBNB15MTEST",
            asset="BNB",
            filter_stage="candidate",
            spot_price=612.0,
            product_type="15m",
            # NOTE: no spot_staleness_seconds kwarg
        )
        row = sm.conn.execute(
            "SELECT spot_staleness_seconds FROM evaluated_opportunities "
            "WHERE ticker='KXBNB15MTEST-T-AUTOFILL'"
        ).fetchone()
        assert row is not None
        assert row[0] == pytest.approx(47.5), (
            f"auto-fill from _scan_spot_staleness_cache broken; "
            f"expected 47.5, got {row[0]}"
        )
    finally:
        sm.conn.close()
        os.unlink(path)


def test_insert_explicit_kwarg_wins_over_cache():
    """Caller-supplied `spot_staleness_seconds` overrides the cache lookup."""
    sm, path = _build_temp_state_db()
    try:
        sm._scan_spot_staleness_cache["BNB"] = 47.5
        sm.insert_evaluated_opportunity(
            ticker="KXBNB15MTEST-T-EXPLICIT",
            event_ticker="KXBNB15MTEST",
            asset="BNB",
            filter_stage="candidate",
            spot_staleness_seconds=2.0,
            product_type="15m",
        )
        row = sm.conn.execute(
            "SELECT spot_staleness_seconds FROM evaluated_opportunities "
            "WHERE ticker='KXBNB15MTEST-T-EXPLICIT'"
        ).fetchone()
        assert row is not None
        assert row[0] == pytest.approx(2.0)
    finally:
        sm.conn.close()
        os.unlink(path)


def test_scanner_populates_spot_staleness_cache():
    """AST guard: bot/scanner/__init__.py writes to
    `self._state._scan_spot_staleness_cache[asset] = ...` at the Coinbase
    scan-path site (reach: `_pt in (None, "15m", "hourly")`). Mirrors
    the precedent set by `_scan_cx_gap_cache` writes."""
    tree = ast.parse(SCANNER_PATH.read_text())
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                # self._state._scan_spot_staleness_cache[asset] = ...
                if (isinstance(tgt, ast.Subscript)
                        and isinstance(tgt.value, ast.Attribute)
                        and tgt.value.attr == "_scan_spot_staleness_cache"):
                    found = True
                    break
        if found:
            break
    assert found, (
        "bot/scanner/__init__.py must write to "
        "self._state._scan_spot_staleness_cache[asset] at the Coinbase "
        "scan-path site (reach: _pt in (None, '15m', 'hourly'))"
    )


# ----------------------------------------------------------------------
# 8. M3 fix-up: behavioral test on the _on_frame WS handler
# ----------------------------------------------------------------------

def test_on_frame_sets_price_and_ts_atomically():
    """Construct a synthetic Coinbase ticker Frame, drive it through
    `_on_frame`, and verify both `_prices[asset]` and `_price_ts[asset]`
    landed. This pins the lock-step contract structurally (AST) AND
    behaviorally (runtime)."""
    from bot.feeds.coinbase import CoinbaseFeed
    # Frame dataclass shape per coinbase_wire.ws_client:
    # Frame(msg_type: str, parsed: Optional[dict], envelope: dict, raw: str, ts_mono: float).
    # We only need msg_type + parsed for the ticker path.

    class FakeFrame:
        def __init__(self, msg_type, parsed):
            self.msg_type = msg_type
            self.parsed = parsed

    with tempfile.TemporaryDirectory() as tmp:
        feed = CoinbaseFeed(persist_path=os.path.join(tmp, "spot_buffer.json"))
        t0 = time.monotonic()
        feed._on_frame(FakeFrame(
            msg_type="ticker",
            parsed={"product_id": "BTC-USD", "price": "67000.0"},
        ))
        t1 = time.monotonic()
        pair = feed.get_price_with_ts("BTC")
        assert pair is not None, "WS frame did not populate price+ts"
        price, ts = pair
        assert price == pytest.approx(67000.0)
        assert t0 <= ts <= t1, (
            f"_price_ts not set within the _on_frame call window; "
            f"t0={t0} ts={ts} t1={t1}"
        )
