"""Smell 3 follow-up — _calmlp_predictors cache + warmup orchestration relocated
from bot/_impl.py to scripts/cal_mlp/integration.py.

ClickUp 86b9vhcat. Plan: kb/decisions/smell-3-calmlp-predictors-relocation-plan-may10.md

Pre-relocation: bot/_impl.py:589-606 owned the dict construction + env-gated
warmup loop + boot-log emission. Post-relocation: integration.py owns the
dict construction + the warmup_predictor_cache() helper that returns
(enabled, warmed_count). bot/_impl.py imports both via the existing
`from integration import (...)` block at line 59 and emits the boot log
in the bot._impl logger namespace using the returned counters.

This file pins the relocation contract:
  - `integration._calmlp_predictors` exists with 4 BTC/ETH/SOL/XRP keys
  - `integration.warmup_predictor_cache()` exists and is a no-arg callable
    returning a (bool, int) tuple
  - bot/_impl.py NO LONGER constructs the dict or runs the warmup loop at
    module level (AST drift guards)
  - bot/_impl.py:59 import block lists both names
  - The 3 consumer sites in bot/_impl.py (search anchors, NOT line numbers
    per L41) still reference the bare name `_calmlp_predictors`
  - Logger namespace stays bot._impl (M3 — operator-runbook grep contract)
  - Hot env-flip semantics preserved (R-p7-cleanroom#H2 + R-p7-coldboot#C-S2):
    CALMLP_ENABLED=0 → instances constructed but not warmed; CALMLP_ENABLED=1
    → instances constructed AND warmed; flip mid-process → next
    warmup_predictor_cache() honors new value
  - warmup_predictor_cache() is idempotent (M1)

Threading invariant (existing pin in tests/test_cal_mlp_invariants.py):
  bot._thread_env must remain the FIRST non-stdlib import in bot/_impl.py.
  This relocation removes lines from the ~589-606 region (well below the
  import header) and does NOT perturb the ordering. The existing AST
  regression `test_thread_env_imported_before_numerical_libs_in_bot_impl`
  continues to cover that invariant; not duplicated here.

Mirrors patterns from tests/test_state_extraction.py (AST walks against
bot/_impl.py) and tests/test_cal_mlp_invariants.py (sys.path setup +
fresh-import semantics).
"""
from __future__ import annotations

import ast
import inspect
import os
import sys
from pathlib import Path
from typing import List
from unittest import mock

import pytest
import bot.boot  # noqa: F401


REPO_ROOT = Path(__file__).resolve().parent.parent
BOT_PY = REPO_ROOT / "bot" / "_impl.py"
INTEGRATION_PY = REPO_ROOT / "scripts" / "cal_mlp" / "integration.py"

# scripts/cal_mlp/ on sys.path for `import integration` (mirrors bot/_impl.py
# header + tests/test_cal_mlp_invariants.py:18)
_CAL_MLP_DIR = REPO_ROOT / "scripts" / "cal_mlp"
if str(_CAL_MLP_DIR) not in sys.path:
    sys.path.insert(0, str(_CAL_MLP_DIR))


# ────────────────────────────────────────────────────────── helpers (test-local)


def _read_bot_impl_ast() -> ast.AST:
    """AST-parse bot/_impl.py once per test (cheap; ~18.5K-line file parses
    in ~50ms)."""
    if not BOT_PY.exists():
        pytest.skip("bot/_impl.py removed (Bit 9.3-iii.c) — extraction-pin vacuous")
    return ast.parse(BOT_PY.read_text())


def _module_level_assignments(tree: ast.AST) -> List[ast.Assign]:
    """All module-scope `<name> = <value>` assignments (NOT inside any
    function/class body)."""
    return [n for n in tree.body if isinstance(n, ast.Assign)]


def _module_level_for_loops(tree: ast.AST) -> List[ast.For]:
    """All module-scope `for ... in ...:` loops (NOT inside any function/class
    body)."""
    return [n for n in tree.body if isinstance(n, ast.For)]


# ─────────────────────────────────────────────────────── files-exist + identity


def test_predictor_cache_lives_in_integration_module():
    """`_calmlp_predictors` must be a module-level attribute of integration.py
    post-relocation."""
    import integration
    assert hasattr(integration, "_calmlp_predictors"), (
        "scripts/cal_mlp/integration.py is missing the relocated "
        "`_calmlp_predictors` dict. See "
        "kb/decisions/smell-3-calmlp-predictors-relocation-plan-may10.md "
        "Step 2 — the dict construction was moved here from bot/_impl.py."
    )


def test_predictor_cache_dict_has_four_assets():
    """The relocated dict must keep the same 4 BTC/ETH/SOL/XRP keys
    constructed via CalMLPPredictor(asset). Construction time-of-load (M1
    idempotency contract relies on this — re-imports do not re-create
    instances; imports are serialized by Python's import lock)."""
    import integration
    assert isinstance(integration._calmlp_predictors, dict)
    assert set(integration._calmlp_predictors.keys()) == {"BTC", "ETH", "SOL", "XRP"}, (
        f"_calmlp_predictors keys = {set(integration._calmlp_predictors.keys())!r}; "
        f"expected the 4-asset BTC/ETH/SOL/XRP set."
    )
    for asset, predictor in integration._calmlp_predictors.items():
        assert isinstance(predictor, integration.CalMLPPredictor), (
            f"_calmlp_predictors[{asset!r}] is {type(predictor).__name__}; "
            f"expected CalMLPPredictor instance."
        )


def test_predictor_cache_singleton_identity():
    """`from integration import _calmlp_predictors` from two import sites must
    yield the same dict object — singleton semantics. The module-level
    construction in integration.py runs ONCE; subsequent attribute access
    returns the same dict id."""
    import integration as i1
    from integration import _calmlp_predictors as ref2  # noqa: F811
    assert i1._calmlp_predictors is ref2, (
        "`_calmlp_predictors` is not the same dict via two import paths — "
        "module-level construction must produce a single dict, not re-construct."
    )


def test_warmup_predictor_cache_function_exists():
    """The warmup_predictor_cache() helper must exist on integration.py and
    be a no-arg callable."""
    import integration
    assert hasattr(integration, "warmup_predictor_cache"), (
        "scripts/cal_mlp/integration.py is missing the new "
        "`warmup_predictor_cache()` helper. See plan doc Step 2."
    )
    fn = integration.warmup_predictor_cache
    assert callable(fn)
    sig = inspect.signature(fn)
    assert list(sig.parameters) == [], (
        f"warmup_predictor_cache.signature = {sig}; expected no parameters "
        f"(it reads CALMLP_ENABLED from os.environ at call time so hot env "
        f"flips are honored)."
    )


def test_warmup_predictor_cache_returns_two_tuple():
    """Return shape is `(enabled: bool, warmed_count: int)` so the caller
    (bot/_impl.py module-load) can emit the boot log in its own logger
    namespace using the counters (M2 + M3 ownership split)."""
    import integration
    with mock.patch.dict(os.environ, {"CALMLP_ENABLED": "0"}, clear=False):
        result = integration.warmup_predictor_cache()
    assert isinstance(result, tuple) and len(result) == 2, (
        f"warmup_predictor_cache() = {result!r}; expected 2-tuple "
        f"(enabled, warmed_count)."
    )
    enabled, warmed = result
    assert isinstance(enabled, bool), f"enabled={enabled!r} type={type(enabled).__name__}; expected bool"
    assert isinstance(warmed, int), f"warmed={warmed!r} type={type(warmed).__name__}; expected int"


# ───────────────────────────────────────────────────────── drift guards (AST)


def test_calmlp_predictors_not_constructed_in_bot_impl():
    """Bot/_impl.py must NO LONGER have a module-scope
    `_calmlp_predictors = {...}` assignment. AST-walked, NOT grep'd, so
    string-literal occurrences in comments/docstrings don't false-positive."""
    tree = _read_bot_impl_ast()
    for assign in _module_level_assignments(tree):
        for target in assign.targets:
            if isinstance(target, ast.Name) and target.id == "_calmlp_predictors":
                raise AssertionError(
                    f"bot/_impl.py still has module-scope `_calmlp_predictors = ...` "
                    f"at line {assign.lineno}. Smell 3 fu requires this construction "
                    f"to live in scripts/cal_mlp/integration.py."
                )


def test_warmup_loop_not_in_bot_impl():
    """Bot/_impl.py must NO LONGER have a module-scope
    `for ... in _calmlp_predictors.values(): ... .warmup()` loop. AST-walked
    against module-scope only — function-body for-loops are unconstrained."""
    tree = _read_bot_impl_ast()
    for for_node in _module_level_for_loops(tree):
        # iter is the right-hand side of `for X in iter:`
        iter_src = ast.dump(for_node.iter)
        if "_calmlp_predictors" in iter_src:
            raise AssertionError(
                f"bot/_impl.py still has module-scope `for ... in "
                f"_calmlp_predictors.values()` at line {for_node.lineno}. "
                f"Smell 3 fu requires this orchestration to live in "
                f"scripts/cal_mlp/integration.py::warmup_predictor_cache()."
            )


def test_bot_impl_imports_predictor_cache_from_integration():
    """The `from integration import (...)` block in bot/_impl.py must list
    both `_calmlp_predictors` and `warmup_predictor_cache`."""
    tree = _read_bot_impl_ast()
    integration_imports: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "integration":
            for alias in node.names:
                # alias.name is the actual symbol; alias.asname is the local rename
                integration_imports.append(alias.name)
    assert "_calmlp_predictors" in integration_imports, (
        f"bot/_impl.py does not import `_calmlp_predictors` from integration. "
        f"current integration imports: {sorted(integration_imports)}. "
        f"Smell 3 fu Step 3 required adding it to the line-59 import block."
    )
    assert "warmup_predictor_cache" in integration_imports, (
        f"bot/_impl.py does not import `warmup_predictor_cache` from integration. "
        f"current integration imports: {sorted(integration_imports)}."
    )


# ─────────────────────────────────────────── hot env-flip behavioral contract


def test_warmup_returns_false_zero_when_env_disabled():
    """CALMLP_ENABLED=0 → warmup_predictor_cache() returns (False, 0).
    Predictors remain INSTANCE-constructed (kill-switch contract: pure attr-set
    init, never IO) but `.warmup()` is NOT called.

    R-p7-cleanroom#H2 + R-p7-coldboot#C-S2 — the "instances always present"
    half of the contract (so hot env=0→1 flip mid-process can lazy-load on
    first scan tick)."""
    import integration
    with mock.patch.dict(os.environ, {"CALMLP_ENABLED": "0"}, clear=False):
        enabled, warmed = integration.warmup_predictor_cache()
    assert enabled is False
    assert warmed == 0
    # Instances must still be present (attribute access proves construction
    # happened at module-load time, NOT inside warmup()).
    assert set(integration._calmlp_predictors.keys()) == {"BTC", "ETH", "SOL", "XRP"}


def test_warmup_predictor_cache_idempotent(monkeypatch):
    """M1 idempotency contract: calling warmup_predictor_cache() twice with
    the same env value must not double-load predictors. CalMLPPredictor.warmup()
    short-circuits via `no_current` when bundles are missing or already loaded;
    warmed_count must stay stable across consecutive calls."""
    import integration
    monkeypatch.setenv("CALMLP_ENABLED", "1")
    enabled_1, warmed_1 = integration.warmup_predictor_cache()
    enabled_2, warmed_2 = integration.warmup_predictor_cache()
    assert enabled_1 is True and enabled_2 is True
    assert warmed_2 == warmed_1, (
        f"Second warmup_predictor_cache() warmed_count={warmed_2}; first call "
        f"got {warmed_1}. Idempotency violated — multiple invocations should "
        f"settle on the same warmed_count."
    )


def test_hot_env_flip_zero_to_one_honored(monkeypatch):
    """The env=0 → env=1 mid-process flip must be honored on the next call:
    first invocation (env=0) returns (False, 0); second invocation after
    env flip (env=1) returns (True, N).

    This proves warmup_predictor_cache() reads CALMLP_ENABLED at CALL time,
    not at module-import time. Without this, an env=0 boot would permanently
    disable warmup even if the operator hot-flips to env=1 (which the
    R-p7-coldboot#C-S2 contract forbids)."""
    import integration
    monkeypatch.setenv("CALMLP_ENABLED", "0")
    enabled_0, warmed_0 = integration.warmup_predictor_cache()
    assert enabled_0 is False
    assert warmed_0 == 0

    monkeypatch.setenv("CALMLP_ENABLED", "1")
    enabled_1, _ = integration.warmup_predictor_cache()
    assert enabled_1 is True, (
        "After CALMLP_ENABLED=0→1 flip, warmup_predictor_cache() returned "
        "enabled=False — the function is reading the env value at the wrong "
        "time (e.g., captured at module-import time as a module-level constant)."
    )


def test_predictor_init_zero_io_post_relocation():
    """Lock the kill-switch contract that depends on CalMLPPredictor.__init__
    being pure attr-set (no IO). The relocated dict construction in
    integration.py constructs all 4 instances at module-import time even when
    CALMLP_ENABLED=0; if __init__ ever does file IO, env=0 boots would block
    on disk reads.

    Companion to tests/test_cal_mlp_invariants.py::test_cal_mlp_predictor_init_zero_io
    (existing pin); duplicated here so an agent post-relocation grepping for
    the contract finds it on the relocation file."""
    import integration
    src = inspect.getsource(integration.CalMLPPredictor.__init__)
    forbidden = ['open(', 'torch.load', 'flock', 'fcntl.', 'sqlite3.connect',
                 'json.load', 'pd.read_', 'pq.read_']
    found = [m for m in forbidden if m in src]
    assert not found, (
        f"CalMLPPredictor.__init__ has IO markers post-relocation: {found}. "
        f"This breaks the kill-switch contract — env=0 boots would block on IO."
    )


# ─────────────────────────────────────────── consumer-call-site preservation


def test_three_consumer_sites_still_reference_predictor_cache():
    """The 3 consumer sites must still reference the bare name
    `_calmlp_predictors` — the explicit named import in line-59 preserves
    bare-name resolution. Counted via grep, NOT line numbers (L41 — line
    numbers drift on every extraction).

    Bit 8.1 (2026-05-10): OpportunityScanner extracted to
    bot/scanner/__init__.py. The two scanner consumer sites
    (annotate-kwargs path + async-enqueue path) moved with the class.
    Bit 9.3 (2026-05-10): MainLoop extracted to bot/main_loop.py; the
    `predictors=_calmlp_predictors` kwarg moved with MainLoop. Walk
    all three files.

    Bit 9.3-iii.c (2026-05-11): bot/_impl.py was DELETED. The 3 consumer
    sites all live in bot/scanner/__init__.py + bot/main_loop.py. BOT_PY
    is now read-skip when absent (pre-deletion shape preserved as
    breadcrumb)."""
    src = BOT_PY.read_text() if BOT_PY.exists() else ""
    scanner_init = REPO_ROOT / "bot" / "scanner" / "__init__.py"
    if scanner_init.exists():
        src += "\n" + scanner_init.read_text()
    main_loop_py = REPO_ROOT / "bot" / "main_loop.py"
    if main_loop_py.exists():
        src += "\n" + main_loop_py.read_text()
    # Three known consumption shapes:
    #   1. predictor=_calmlp_predictors.get(asset)            (annotate kwargs path)
    #   2. predictor=_calmlp_predictors.get(asset)            (async-enqueue path)
    #   3. predictors=_calmlp_predictors,                     (start_post_hoc_processor kwarg)
    get_calls = src.count("_calmlp_predictors.get(")
    kwarg_uses = src.count("predictors=_calmlp_predictors")
    assert get_calls == 2, (
        f"_calmlp_predictors.get( occurs {get_calls}× in bot/_impl.py + bot/scanner/__init__.py; "
        f"expected 2 (scanner annotate-kwargs path + async-enqueue path). Possible regression "
        f"from rewriting consumer sites that the relocation should leave untouched."
    )
    assert kwarg_uses == 1, (
        f"predictors=_calmlp_predictors occurs {kwarg_uses}× in bot/_impl.py + bot/scanner/__init__.py; "
        f"expected 1 (start_post_hoc_processor MainLoop kwarg). Possible regression."
    )


# ─────────────────────────────────────────── logger-namespace preservation (M3)


def test_boot_log_emitted_from_bot_boot_namespace(caplog):
    """M3 contract (updated Bit 9.3-iii.a, 2026-05-11): the `[CALMLP] enabled=...`
    boot log MUST emit from a bot.* logger namespace (NOT from integration). The
    namespace shifted from `bot._impl` to `bot.boot` in Bit 9.3-iii.a alongside
    the cal_mlp warmup relocation. Operator runbooks grep for `[CALMLP] enabled=`
    in journalctl — the message text is unchanged.

    SOURCE-LEVEL pin (the log fires once at bot.boot module-import time, before
    any test runs)."""
    src = (REPO_ROOT / "bot" / "boot.py").read_text()
    assert "[CALMLP] enabled=1 at boot, predictors_warmed=" in src, (
        "bot/boot.py is missing the `[CALMLP] enabled=1 at boot` log line. "
        "M3 requires the boot log to emit from a bot.* namespace, not "
        "from integration.py."
    )
    assert "[CALMLP] enabled=0 at boot" in src, (
        "bot/boot.py is missing the `[CALMLP] enabled=0 at boot` log line. "
        "Both env=0 and env=1 branches must emit so operator runbook greps "
        "for `[CALMLP] enabled=` cover both states."
    )
    assert "logging.getLogger(__name__).info(" in src or "_LOGGER.info(" in src, (
        "bot/boot.py is missing the module-scoped logger call surrounding "
        "the `[CALMLP] enabled=...` boot log."
    )

    # Negative pin: bot/_impl.py should NO LONGER emit the boot log.
    # Vacuously true post-Bit-9.3-iii.c (bot/_impl.py deleted entirely).
    if BOT_PY.exists():
        impl_src = BOT_PY.read_text()
        assert "[CALMLP] enabled=1 at boot, predictors_warmed=" not in impl_src, (
            "bot/_impl.py still emits the cal_mlp boot log — Bit 9.3-iii.a relocated "
            "this to bot/boot.py."
        )

    # Negative pin: integration.py must NOT emit the boot log line. If a
    # future agent moves the log emission into warmup_predictor_cache() to
    # consolidate, the operator-runbook namespace assumption breaks.
    integration_src = INTEGRATION_PY.read_text()
    assert "[CALMLP] enabled=1 at boot, predictors_warmed=" not in integration_src, (
        "integration.py contains the `[CALMLP] enabled=1 at boot` log line — "
        "this should emit from bot/_impl.py to preserve the bot._impl logger "
        "namespace per M3."
    )


# ─────────────────────────────────────────── __all__ list discipline (M4)


def test_warmup_predictor_cache_in_integration_all():
    """warmup_predictor_cache is a public-named helper; it should appear in
    integration.py's `__all__` for parity with the other public cal_mlp
    helpers (annotate_evaluation_kwargs, start_post_hoc_processor, etc.)."""
    import integration
    assert hasattr(integration, "__all__")
    assert "warmup_predictor_cache" in integration.__all__, (
        f"integration.__all__ does not include 'warmup_predictor_cache'. "
        f"current: {sorted(integration.__all__)!r}"
    )


def test_underscore_predictors_NOT_in_integration_all():
    """Underscore-prefixed names like `_calmlp_predictors` and
    `_POSTHOC_PROCESSOR` are NOT in `__all__` per integration.py convention.
    Adding them would break that convention without benefit (explicit-name
    imports in bot/_impl.py work regardless of __all__)."""
    import integration
    assert "_calmlp_predictors" not in integration.__all__, (
        "integration.__all__ contains '_calmlp_predictors' — underscore-prefixed "
        "names should not be in __all__. Match the `_POSTHOC_PROCESSOR` precedent."
    )
