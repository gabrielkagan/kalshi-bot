"""Bit V.1 — AST/structural pins for the estimator-parity tape-vol seam.

Postmortem: kb/failures/vol-engine-beta-dvol-deflation-jun12.md (L-VOL-1
"estimator parity is a contract" — the vol twin of the cal_mlp
feature-transform lock-step rule; idiom mirrors
tests/contracts/test_calmlp_lockstep.py + test_b1_scanner_wiring.py).

Pins (structural anchors, not line numbers):

1. ``bot/helpers/tape_rv.py`` exists, is helpers-leaf (stdlib-only
   imports), and ``trailing_rv300`` divides by ``(n-1)`` (sample stdev —
   the fairvalue_extract._realized_vol / 02_longshot_tick_floor._rv
   construction).
2. ``bot/scanner/__init__.py`` top-imports ``trailing_rv300`` from
   ``bot.helpers.tape_rv`` (not lazy) and calls it inside ``scan()``.
3. ``StateManager.__init__`` initializes ``_scan_tape_rv_cache``.
4. BOTH strategy-engine overlays (``_ls_engine.evaluate_market`` +
   ``_tw_engine.evaluate_market``) receive ``blended_rv=_strategy_vol``
   — the max-selected honest vol — NOT the raw ``blended_rv`` name. A
   refactor that reverts an overlay to the raw engine estimate goes RED
   here even if behavioral tests are green.
5. ``_strategy_vol`` is assigned via ``max(blended_rv, _tape_rv300)``
   somewhere in ``scan()`` (never price risk off the smaller estimate),
   where ``_tape_rv300`` is derived from the ``_scan_tape_rv_cache`` read
   (R1-MN4 tighten: the second ``max()`` arg must be the cache-derived
   name — a refactor that maxes against anything else goes RED).
6. R1-M1 (fix round) — EVENT-time staleness gate at the seam: the live
   CoinbaseFeed sampler re-stamps the last-known price every 1s, so the
   helper's BUFFER-time guard can never fire on a frozen feed. ``scan()``
   must gate the cache write on the Bit-S.1 event-time signal: it
   top-imports ``TAPE_RV_MAX_STALENESS_S`` from ``bot.helpers.tape_rv``
   and compares the ``_scan_spot_staleness_cache`` reading against it
   before trusting ``trailing_rv300``; the helper's ``max_staleness_s``
   default and the seam gate share that one constant (= 30.0, the
   validated backtest's abstention horizon — 02's ``STALE_S``).
7. R1-M1 (fix round) — ``bot/longshot.py::evaluate_market`` carries its
   own frozen/unmeasured-spot gate (the twaplock ``TWAPLOCK_SPOT_STALE``
   pattern): reads ``_scan_spot_staleness_cache`` and compares against
   ``C.LONGSHOT_MAX_SPOT_STALENESS_SECONDS`` (= 30.0, lockstep with the
   tape-rv horizon).
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCANNER_PATH = REPO_ROOT / "bot" / "scanner" / "__init__.py"
TAPE_RV_PATH = REPO_ROOT / "bot" / "helpers" / "tape_rv.py"
STATE_PATH = REPO_ROOT / "bot" / "state.py"
LONGSHOT_PATH = REPO_ROOT / "bot" / "longshot.py"

_STDLIB_ALLOWED = {"math", "bisect", "typing", "__future__"}


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _scan_func(tree: ast.Module) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "OpportunityScanner":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "scan":
                    return item
    raise AssertionError("OpportunityScanner.scan not found")


# ── 1. Helper module shape ───────────────────────────────────────────────────

def test_tape_rv_helper_exists():
    assert TAPE_RV_PATH.exists(), (
        "bot/helpers/tape_rv.py missing — Bit V.1 gates re-arm on the "
        "estimator-parity helper")


def test_tape_rv_helper_is_stdlib_only():
    """helpers-leaf, strictly: the parity helper imports NOTHING beyond
    stdlib (not even bot.constants) so research scripts can vendor or
    import it without dragging bot config."""
    tree = _tree(TAPE_RV_PATH)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                assert root in _STDLIB_ALLOWED, (
                    f"non-stdlib import in tape_rv.py: {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            root = (node.module or "").split(".")[0]
            assert root in _STDLIB_ALLOWED, (
                f"non-stdlib import in tape_rv.py: {node.module}")


def test_trailing_rv300_uses_sample_stdev_n_minus_1():
    """The /(n-1) divisor is the load-bearing parity choice (population
    /n would deflate every estimate ~0.8%). Pin the `len(...) - 1`
    denominator inside trailing_rv300."""
    tree = _tree(TAPE_RV_PATH)
    fn = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "trailing_rv300":
            fn = node
            break
    assert fn is not None, "trailing_rv300 not defined in tape_rv.py"
    found = False
    for node in ast.walk(fn):
        # var = sum(...) / (len(rets) - 1)
        if (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
                and isinstance(node.right, ast.BinOp)
                and isinstance(node.right.op, ast.Sub)
                and isinstance(node.right.left, ast.Call)
                and isinstance(node.right.left.func, ast.Name)
                and node.right.left.func.id == "len"
                and isinstance(node.right.right, ast.Constant)
                and node.right.right.value == 1):
            found = True
    assert found, (
        "trailing_rv300 must divide by (len(<rets>) - 1) — sample stdev "
        "parity with fairvalue_extract._realized_vol / 02's _rv")


# ── 2 + 3. Scanner import + cache init ───────────────────────────────────────

def test_scanner_top_imports_trailing_rv300():
    tree = _tree(SCANNER_PATH)
    for node in tree.body:
        if (isinstance(node, ast.ImportFrom)
                and node.module == "bot.helpers.tape_rv"
                and any(a.name == "trailing_rv300" for a in node.names)):
            return
    raise AssertionError(
        "bot/scanner/__init__.py must top-import trailing_rv300 from "
        "bot.helpers.tape_rv (not lazy)")


def test_scanner_calls_trailing_rv300_inside_scan():
    fn = _scan_func(_tree(SCANNER_PATH))
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "trailing_rv300"):
            return
    raise AssertionError(
        "OpportunityScanner.scan() must compute trailing_rv300 at the "
        "per-asset spot/vol seam")


def test_state_manager_initializes_scan_tape_rv_cache():
    tree = _tree(STATE_PATH)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "StateManager":
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "__init__":
                    for sub in ast.walk(item):
                        if (isinstance(sub, (ast.Assign, ast.AnnAssign))):
                            targets = (sub.targets
                                       if isinstance(sub, ast.Assign)
                                       else [sub.target])
                            for t in targets:
                                if (isinstance(t, ast.Attribute)
                                        and t.attr == "_scan_tape_rv_cache"):
                                    return
    raise AssertionError(
        "StateManager.__init__ must initialize _scan_tape_rv_cache "
        "(mirrors _scan_spot_staleness_cache)")


# ── 4 + 5. Overlay routing through the max() selection ──────────────────────

def _evaluate_market_calls(fn: ast.FunctionDef):
    out = {}
    for node in ast.walk(fn):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "evaluate_market"
                and isinstance(node.func.value, ast.Name)):
            out[node.func.value.id] = node
    return out


def test_both_overlays_pass_strategy_vol_not_raw_blended_rv():
    fn = _scan_func(_tree(SCANNER_PATH))
    calls = _evaluate_market_calls(fn)
    for engine_var in ("_ls_engine", "_tw_engine"):
        assert engine_var in calls, (
            f"{engine_var}.evaluate_market call not found in scan() — "
            "overlay structure changed; update this contract deliberately")
        kw = {k.arg: k.value for k in calls[engine_var].keywords}
        assert "blended_rv" in kw, f"{engine_var} call lost blended_rv kwarg"
        v = kw["blended_rv"]
        assert isinstance(v, ast.Name) and v.id == "_strategy_vol", (
            f"{engine_var}.evaluate_market receives "
            f"blended_rv={ast.dump(v)} — must be the Bit V.1 "
            "`_strategy_vol` (max of engine blended_rv and tape rv300). "
            "Passing the raw engine estimate re-opens the beta×DVOL "
            "deflation hole (kb/failures/vol-engine-beta-dvol-deflation-"
            "jun12.md)")


def test_strategy_vol_assigned_via_max_of_blended_and_tape():
    """R1-MN4 tighten: the SECOND max() arg must be the cache-derived
    ``_tape_rv300`` name (pinned cache-derived by the sister test below)
    — `max(blended_rv, <anything else>)` no longer satisfies this pin."""
    fn = _scan_func(_tree(SCANNER_PATH))
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "_strategy_vol" not in targets:
                continue
            v = node.value
            if (isinstance(v, ast.Call)
                    and isinstance(v.func, ast.Name)
                    and v.func.id == "max"
                    and len(v.args) == 2
                    and isinstance(v.args[0], ast.Name)
                    and v.args[0].id == "blended_rv"
                    and isinstance(v.args[1], ast.Name)
                    and v.args[1].id == "_tape_rv300"):
                return
    raise AssertionError(
        "scan() must assign _strategy_vol = max(blended_rv, _tape_rv300) "
        "on the rv300-available path — never price strategy risk off the "
        "smaller estimate, and the second arg must be the cache-derived "
        "_tape_rv300 name (R1-MN4)")


def test_tape_rv300_name_is_cache_derived():
    """Companion to the max() pin: ``_tape_rv300`` must be assigned from
    ``self._state._scan_tape_rv_cache.get(...)`` inside scan() — making
    the second max() arg provably the per-asset cache reading."""
    fn = _scan_func(_tree(SCANNER_PATH))
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            if "_tape_rv300" not in targets:
                continue
            v = node.value
            if (isinstance(v, ast.Call)
                    and isinstance(v.func, ast.Attribute)
                    and v.func.attr == "get"
                    and isinstance(v.func.value, ast.Attribute)
                    and v.func.value.attr == "_scan_tape_rv_cache"):
                return
    raise AssertionError(
        "_tape_rv300 must be read from self._state._scan_tape_rv_cache"
        ".get(asset) inside scan()")


# ── 6. Event-time staleness gate at the seam (R1-M1 fix round) ───────────────

def test_tape_rv_module_exports_staleness_constant_30s():
    """Single source of truth for the abstention horizon: the helper's
    module constant is 30.0 (02's STALE_S) and IS the default for the
    ``max_staleness_s`` kwarg."""
    import inspect

    from bot.helpers import tape_rv

    assert tape_rv.TAPE_RV_MAX_STALENESS_S == 30.0
    sig = inspect.signature(tape_rv.trailing_rv300)
    assert (sig.parameters["max_staleness_s"].default
            == tape_rv.TAPE_RV_MAX_STALENESS_S)


def test_scanner_top_imports_tape_rv_max_staleness_constant():
    tree = _tree(SCANNER_PATH)
    for node in tree.body:
        if (isinstance(node, ast.ImportFrom)
                and node.module == "bot.helpers.tape_rv"
                and any(a.name == "TAPE_RV_MAX_STALENESS_S"
                        for a in node.names)):
            return
    raise AssertionError(
        "bot/scanner/__init__.py must top-import TAPE_RV_MAX_STALENESS_S "
        "from bot.helpers.tape_rv — the seam's event-time gate and the "
        "helper's buffer-time guard share one horizon constant")


def test_scan_gates_tape_rv_on_event_time_staleness():
    """The seam must compare the Bit-S.1 event-time staleness reading
    against TAPE_RV_MAX_STALENESS_S inside scan() — the live sampler's
    1s re-stamping makes the helper's buffer-time guard blind to a
    frozen feed (R1-M1)."""
    fn = _scan_func(_tree(SCANNER_PATH))
    for node in ast.walk(fn):
        if isinstance(node, ast.Compare):
            names = {n.id for n in ast.walk(node)
                     if isinstance(n, ast.Name)}
            if "TAPE_RV_MAX_STALENESS_S" in names:
                return
    raise AssertionError(
        "scan() must compare the _scan_spot_staleness_cache reading "
        "against TAPE_RV_MAX_STALENESS_S before trusting trailing_rv300 "
        "— frozen-but-resampled feeds otherwise read rv300~0 and max() "
        "silently reverts to the broken blended_rv")


# ── 7. Longshot frozen/unmeasured-spot gate (R1-M1 fix round) ────────────────

def _longshot_evaluate_market(tree: ast.Module) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "LongshotEngine":
            for item in node.body:
                if (isinstance(item, ast.FunctionDef)
                        and item.name == "evaluate_market"):
                    return item
    raise AssertionError("LongshotEngine.evaluate_market not found")


def test_longshot_staleness_constant_lockstep_with_backtest_horizon():
    import bot.constants as C
    from bot.helpers import tape_rv

    assert C.LONGSHOT_MAX_SPOT_STALENESS_SECONDS == 30.0, (
        "30.0s is the validated backtest's abstention horizon "
        "(02_longshot_tick_floor STALE_S) — changing it breaks "
        "estimator/abstention parity (L-VOL-1)")
    assert (C.LONGSHOT_MAX_SPOT_STALENESS_SECONDS
            == tape_rv.TAPE_RV_MAX_STALENESS_S)


def test_longshot_evaluate_market_gates_on_staleness_cache():
    """Mirror of the twaplock TWAPLOCK_SPOT_STALE pattern: the engine
    reads the scanner-owned Bit-S.1 cache and compares against
    LONGSHOT_MAX_SPOT_STALENESS_SECONDS (pre-fix longshot had NO
    staleness gate — twaplock did)."""
    fn = _longshot_evaluate_market(_tree(LONGSHOT_PATH))
    reads_cache = any(
        isinstance(n, ast.Attribute)
        and n.attr == "_scan_spot_staleness_cache"
        for n in ast.walk(fn))
    assert reads_cache, (
        "LongshotEngine.evaluate_market must read "
        "_scan_spot_staleness_cache (R1-M1 — longshot had no spot "
        "staleness gate)")
    compares_constant = any(
        isinstance(n, ast.Attribute)
        and n.attr == "LONGSHOT_MAX_SPOT_STALENESS_SECONDS"
        for n in ast.walk(fn))
    assert compares_constant, (
        "LongshotEngine.evaluate_market must compare the reading against "
        "C.LONGSHOT_MAX_SPOT_STALENESS_SECONDS")
