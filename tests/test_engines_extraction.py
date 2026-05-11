"""Bit 6.1 + 6.2 + 6.3 — VolatilityEngine + ProbabilityEngine + CalibrationEngine
extracted from bot/_impl.py to bot/engines/. Sprint 6 closes here.

Bit 6.1 (2026-05-09):
  VolatilityEngine → bot/engines/volatility.py
  (Sprint 6 Bit 6.1; first leaf in the new bot/engines/ subpackage. Establishes
  the bot/engines/__init__.py shim.)

Bit 6.2 (2026-05-09):
  ProbabilityEngine → bot/engines/probability.py
  (Sprint 6 Bit 6.2; second leaf — Student-t / NIG win-prob CDF + adaptive
  calibration cascade. Bit 6.2 originally added a `from bot import _impl as
  _bot_impl` late-binding pattern; Bit 6.3 path-B lifted that and lifted the
  matching .importlinter `bot.engines.probability -> bot._impl` carve-out.)

Bit 6.3 (2026-05-10):
  CalibrationEngine → bot/engines/calibration.py
  (Sprint 6 Bit 6.3; third leaf — adaptive 3-method calibrator. Class body is
  byte-for-byte. Path-B refactor ALSO relocated _CALIBRATION_ENGINE +
  _CAL_REGISTRY + _derive_subtype + _derive_asset_filter + _resolve_cal_engine
  alongside the class; both bot/_impl.py and bot/engines/probability.py reach
  them via top-level `from bot.engines import calibration as _cal_state` plus
  `_cal_state.X` attribute access.)

Locks the contract between bot/_impl.py (which does
``from bot.engines import VolatilityEngine, ProbabilityEngine, CalibrationEngine``
plus ``from bot.engines import calibration as _cal_state``) and the
bot/engines/ subpackage. Mirrors tests/test_feeds_extraction.py (Bit 4.5a/4.5b)
and tests/test_fetchers_extraction.py (Bit 4.4).

Class-specific notes:
- VolatilityEngine.__init__ has `feed: CoinbaseFeed` annotation (Bit 4.5a) and
  `Optional[DeribitDVOLFetcher]` annotation (Bit 4.4) — both must continue
  to resolve from explicit sibling imports in bot/engines/volatility.py.
- VolatilityEngine.__init__ has `Optional['EGARCHEstimator']` and
  `Optional['MincerZarnowitzTracker']` forward-ref annotations — both classes
  still live in models.py post-Bit-6.1 so the strings remain quoted.
- In-class static-method self-references (`VolatilityEngine._realized_quarticity`,
  `VolatilityEngine._parzen_kernel`) survive the verbatim move.

L33 (Bit 4.4): wrong-class attribution in extraction breadcrumbs is a recurring
drift class. Pin consumer-class identity with positive + negative regression
tests.

L38 (Bit 4.5b): AST tests that walk a class body break at extraction time.
This file is the new home for AST walks that target VolatilityEngine; the
tests in tests/test_fetchers_extraction.py and tests/test_feeds_extraction.py
that previously walked bot/_impl.py for VolatilityEngine were retargeted to
bot/engines/volatility.py in the same atomic commit.
"""
from __future__ import annotations

import ast
import importlib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
VOLATILITY_PY = REPO_ROOT / "bot" / "engines" / "volatility.py"
PROBABILITY_PY = REPO_ROOT / "bot" / "engines" / "probability.py"
CALIBRATION_PY = REPO_ROOT / "bot" / "engines" / "calibration.py"
ENGINES_INIT = REPO_ROOT / "bot" / "engines" / "__init__.py"
BOT_PY = REPO_ROOT / "bot" / "_impl.py"


# ─── 1. Files exist + imports ───────────────────────────────────────────────


def test_engines_subpackage_init_exists():
    assert ENGINES_INIT.is_file()


def test_volatility_module_exists():
    assert VOLATILITY_PY.is_file()


def test_subpackage_imports():
    importlib.import_module("bot.engines")


def test_subpackage_exports_volatility_engine():
    import bot.engines
    assert hasattr(bot.engines, "VolatilityEngine")


# ─── 2. Identity preservation across re-export chain ────────────────────────


def test_volatility_identity_through_bot_impl():
    """bot._impl.VolatilityEngine is bot.engines.VolatilityEngine
    is bot.engines.volatility.VolatilityEngine. The MainLoop construction
    site (`self.vol = VolatilityEngine(...)`), OpportunityScanner type
    annotation (`vol: VolatilityEngine`), and tests/test_vol_engine.py
    static-method calls (`from bot import VolatilityEngine`) all rely
    on these three references being the same object."""
    import bot._impl as b
    import bot.engines as be
    import bot.engines.volatility as bev
    assert b.VolatilityEngine is be.VolatilityEngine is bev.VolatilityEngine


def test_volatility_identity_through_bot_proxy():
    """bot.VolatilityEngine resolves through `bot._BotProxy` to the
    canonical class object. Production paths use this chain."""
    import bot
    import bot.engines.volatility as bev
    assert bot.VolatilityEngine is bev.VolatilityEngine


# ─── 3. Drift guards (AST) ──────────────────────────────────────────────────


@pytest.mark.parametrize("class_name", ["VolatilityEngine"])
def test_class_not_defined_in_bot_impl(class_name):
    """Future drift guard: catches "I'll just add it back to _impl.py".

    Mirrors test_feeds_extraction.py / test_fetchers_extraction.py. The
    re-import chain in bot/_impl.py is the only place the name should
    resolve from.
    """
    tree = ast.parse(BOT_PY.read_text(), filename=str(BOT_PY))
    classdefs = [
        node for node in ast.iter_child_nodes(tree)
        if isinstance(node, ast.ClassDef) and node.name == class_name
    ]
    assert classdefs == [], (
        f"{class_name} ClassDef found at module scope in bot/_impl.py "
        f"(line {classdefs[0].lineno if classdefs else '?'}). The class was "
        f"extracted to bot/engines/volatility.py in Bit 6.1 — re-introducing "
        f"it breaks the import chain and identity preservation."
    )


def test_volatility_engine_class_defined_at_module_scope():
    """Positive pin: VolatilityEngine ClassDef IS at module scope of
    bot/engines/volatility.py (not nested, not under a try/except)."""
    tree = ast.parse(VOLATILITY_PY.read_text(), filename=str(VOLATILITY_PY))
    classdefs = [
        node for node in ast.iter_child_nodes(tree)
        if isinstance(node, ast.ClassDef) and node.name == "VolatilityEngine"
    ]
    assert len(classdefs) == 1, (
        f"Expected exactly 1 module-scope VolatilityEngine ClassDef in "
        f"bot/engines/volatility.py, got {len(classdefs)}."
    )


def test_bot_impl_imports_engines_subpackage():
    """bot/_impl.py must import VolatilityEngine from bot.engines.

    AST-based to avoid false matches inside docstrings/comments.
    """
    tree = ast.parse(BOT_PY.read_text(), filename=str(BOT_PY))
    imported = set()
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "bot.engines":
            for alias in node.names:
                imported.add(alias.name)
    assert "VolatilityEngine" in imported, (
        "bot/_impl.py is missing `from bot.engines import VolatilityEngine`. "
        "Without it, MainLoop's `self.vol = VolatilityEngine(...)` and "
        "OpportunityScanner's `vol: VolatilityEngine` annotation both break."
    )


# ─── 4. Constants resolve from canonical sources ────────────────────────────


VOLATILITY_BOT_CONSTANTS = (
    "BETA_LOOKBACK_RETURNS",
    "DERIBIT_DVOL_CURRENCIES",
    "IV_RV_SPREAD_THRESHOLD",
    "JUMP_ADAPTIVE_DECAY_CAP",
    "JUMP_ADAPTIVE_DECAY_MAX_BOOST",
    "JUMP_ADAPTIVE_DECAY_MIN_BOOST",
    "JUMP_ADAPTIVE_DECAY_TAU",
    "JUMP_ADAPTIVE_EWMA_INIT_RETURNS",
    "JUMP_ADAPTIVE_EWMA_LAMBDA",
    "JUMP_ADAPTIVE_MAG_CAP",
    "JUMP_ADAPTIVE_MAG_SCALE_BASE",
    "JUMP_ADAPTIVE_MAX_HISTORY",
    "JUMP_ADAPTIVE_PCTILE_LEVEL",
    "JUMP_ADAPTIVE_PCTILE_MIN_OBS",
    "JUMP_ADAPTIVE_PCTILE_WINDOW",
    "JUMP_ADAPTIVE_SAVE_INTERVAL",
    "JUMP_ADAPTIVE_SHADOW_MODE",
    "JUMP_ADAPTIVE_SIGMA_MULT",
    "JUMP_ADAPTIVE_STATE_PATH",
    "JUMP_ADAPTIVE_SUBSAMPLE",
    "JUMP_DECAY_MAX_BOOST",
    "JUMP_DECAY_MIN_BOOST",
    "JUMP_DECAY_TAU",
    "JUMP_MAX_HISTORY",
    "JUMP_THRESHOLD_MULTIPLIER",
    "RK_ADAPTIVE_SHADOW_MODE",
    "RK_BANDWIDTH_MAX_FRACTION",
    "RK_CSTAR_FLAT_TOP_PARZEN",
    "RK_MIN_RETURNS_FOR_ADAPTIVE",
    "RK_NOISE_VAR_FLOOR",
    "RK_TV_SHADOW_MODE",
    "VOL_BLEND_WEIGHTS",
    "VOL_WINDOW_15MIN",
    "VOL_WINDOW_1MIN",
    "VOL_WINDOW_5MIN",
)


@pytest.mark.parametrize("name", VOLATILITY_BOT_CONSTANTS)
def test_volatility_constants_resolve_from_bot_constants(name):
    """All RK / JUMP / VOL / DERIBIT / IV / BETA tunables live in bot.constants
    per Bit 3.1. The volatility module imports them explicitly."""
    import bot.constants
    import bot.engines.volatility as bev
    assert getattr(bev, name) is getattr(bot.constants, name), (
        f"bot.engines.volatility.{name} drifted from bot.constants.{name}. "
        f"Either the constant moved (then update the import) or two "
        f"separate definitions exist (then collapse to bot.constants)."
    )


VOLATILITY_CONFIG_CONSTANTS = (
    "ASSETS",
    "EGARCH_BLEND_LOG_INTERVAL",
    "EGARCH_BLEND_SHADOW_MODE",
    "EGARCH_RV_RATIO_CLAMP",
    "VOL_RETURN_INTERVAL",
)


@pytest.mark.parametrize("name", VOLATILITY_CONFIG_CONSTANTS)
def test_volatility_config_constants_resolve_from_config(name):
    """ASSETS, EGARCH_*, and VOL_RETURN_INTERVAL live in config.py (not
    bot.constants — pre-Bit-3.1 EGARCH blend; ASSETS is a primitive). Pin
    the source so a future maintainer doesn't accidentally re-import from
    bot.constants and silently shadow."""
    import config
    import bot.engines.volatility as bev
    assert getattr(bev, name) is getattr(config, name), (
        f"bot.engines.volatility.{name} drifted from config.{name}."
    )


def test_compute_tv_rk_weights_from_models():
    """compute_tv_rk_weights lives in models.py (not bot.constants — it's a
    function, not a constant). Pin the source."""
    import bot.models as models
    import bot.engines.volatility as bev
    assert bev.compute_tv_rk_weights is models.compute_tv_rk_weights


# ─── 5. Annotation-consumer pins (L33 from Bit 4.4) ─────────────────────────


def test_volatility_init_annotates_coinbase_feed_in_volatility_py():
    """Positive pin: `feed: CoinbaseFeed` annotation lives on
    VolatilityEngine.__init__ in bot/engines/volatility.py post-Bit-6.1.
    Mirrors the load-bearing pin in test_feeds_extraction.py (which
    was retargeted in this same commit)."""
    src = VOLATILITY_PY.read_text()
    tree = ast.parse(src)
    vol_engine = next(
        node for node in ast.iter_child_nodes(tree)
        if isinstance(node, ast.ClassDef) and node.name == "VolatilityEngine"
    )
    init = next(
        n for n in vol_engine.body
        if isinstance(n, ast.FunctionDef) and n.name == "__init__"
    )
    init_src = ast.get_source_segment(src, init) or ""
    assert "feed: CoinbaseFeed" in init_src, (
        "VolatilityEngine.__init__ no longer annotates `feed: CoinbaseFeed` "
        "in bot/engines/volatility.py. The breadcrumb in bot/_impl.py:104 "
        "(Bit 4.5a + 4.5b) and bot/feeds/__init__.py docstring rely on it."
    )


def test_volatility_init_annotates_deribit_dvol_fetcher_in_volatility_py():
    """Positive pin: `Optional[DeribitDVOLFetcher]` annotation lives on
    VolatilityEngine.__init__ in bot/engines/volatility.py post-Bit-6.1.
    Mirrors the load-bearing pin in test_fetchers_extraction.py."""
    src = VOLATILITY_PY.read_text()
    tree = ast.parse(src)
    vol_engine = next(
        node for node in ast.iter_child_nodes(tree)
        if isinstance(node, ast.ClassDef) and node.name == "VolatilityEngine"
    )
    init = next(
        n for n in vol_engine.body
        if isinstance(n, ast.FunctionDef) and n.name == "__init__"
    )
    init_src = ast.get_source_segment(src, init) or ""
    assert "Optional[DeribitDVOLFetcher]" in init_src, (
        "VolatilityEngine.__init__ no longer annotates dvol_fetcher with "
        "Optional[DeribitDVOLFetcher]. The breadcrumb in bot/_impl.py:103 "
        "(Bit 4.4) and bot/fetchers/__init__.py docstring rely on it."
    )


def test_volatility_init_keeps_egarch_mz_forward_refs_quoted():
    """Negative pin: EGARCHEstimator and MincerZarnowitzTracker remain
    string-quoted forward refs because both classes still live in models.py
    post-Bit-6.1. If a future bit moves them into bot/engines/, drop the
    quotes and update this test."""
    src = VOLATILITY_PY.read_text()
    assert "Optional['EGARCHEstimator']" in src, (
        "Quoted forward-ref Optional['EGARCHEstimator'] missing from "
        "bot/engines/volatility.py. EGARCHEstimator still lives in models.py "
        "so the annotation must stay string-quoted to avoid an import."
    )
    assert "Optional['MincerZarnowitzTracker']" in src, (
        "Quoted forward-ref Optional['MincerZarnowitzTracker'] missing from "
        "bot/engines/volatility.py."
    )


# Bit 8.1 (2026-05-10): OpportunityScanner moved to bot/scanner/__init__.py.
# Walk both files when looking for OpportunityScanner content.
_SCANNER_PY = REPO_ROOT / "bot" / "scanner" / "__init__.py"


def _find_classdef_across_impl_and_scanner(class_name):
    """Find a ClassDef by name; search bot/_impl.py + bot/scanner/__init__.py."""
    for path in (BOT_PY, _SCANNER_PY):
        if not path.is_file():
            continue
        src = path.read_text()
        tree = ast.parse(src)
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                return node, src
    return None, None


def test_opportunity_scanner_still_annotates_vol_volatility_engine():
    """L33 positive pin: OpportunityScanner.__init__ has `vol: VolatilityEngine`
    annotation — verifies the consumer-class identity. Post-Bit-8.1 lives in
    bot/scanner/__init__.py (was bot/_impl.py pre-extraction)."""
    scanner, src = _find_classdef_across_impl_and_scanner("OpportunityScanner")
    assert scanner is not None, (
        "OpportunityScanner ClassDef missing from both bot/_impl.py and bot/scanner/__init__.py."
    )
    init = next(
        (
            n for n in scanner.body
            if isinstance(n, ast.FunctionDef) and n.name == "__init__"
        ),
        None,
    )
    assert init is not None, "OpportunityScanner.__init__ missing."
    init_src = ast.get_source_segment(src, init) or ""
    assert "vol: VolatilityEngine" in init_src, (
        "OpportunityScanner.__init__ no longer annotates `vol: VolatilityEngine`. "
        "If renamed (then update breadcrumb) or removed (then drop the Bit 6.1 "
        "noqa breadcrumb in bot/_impl.py)."
    )


def test_no_other_class_annotates_volatility_engine():
    """Negative pin: only OpportunityScanner.__init__ has a parameter
    annotated `vol: VolatilityEngine` (across bot/_impl.py + bot/scanner).
    Catches wrong-class attribution drift (L33)."""
    consumers = []
    for path in (BOT_PY, _SCANNER_PY):
        if not path.is_file():
            continue
        src = path.read_text()
        tree = ast.parse(src)
        for node in ast.iter_child_nodes(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for inner in node.body:
                if not (isinstance(inner, ast.FunctionDef) and inner.name == "__init__"):
                    continue
                init_src = ast.get_source_segment(src, inner) or ""
                if "VolatilityEngine" in init_src and "vol: VolatilityEngine" in init_src:
                    consumers.append(node.name)
    assert consumers == ["OpportunityScanner"], (
        f"Expected only OpportunityScanner to have a `vol: VolatilityEngine` "
        f"annotation, got: {consumers}. Update the breadcrumb in bot/_impl.py "
        f"to enumerate the additional consumer(s)."
    )


# ─── 6. Method-presence pins (byte-for-byte preservation) ───────────────────


VOLATILITY_METHODS = (
    "__init__",
    "_load_rk_state",
    "save_rk_state",
    "_maybe_save_rk_state",
    "update",
    "_record_jump_event",
    "_adaptive_subsample_return",
    "_adaptive_jump_test",
    "_record_adaptive_jump_event",
    "_adaptive_decay_multiplier",
    "_save_adaptive_state",
    "_load_adaptive_state",
    "_parzen_kernel",
    "_estimate_noise_variance",
    "_realized_quarticity",
    "_optimal_rk_bandwidth",
    "_realized_kernel",
    "_bipower_variation",
    "_estimate_beta",
    "_get_implied_vol",
    "_get_implied_vol_hourly",
    "_compute",
)


@pytest.mark.parametrize("method_name", VOLATILITY_METHODS)
def test_volatility_engine_has_method(method_name):
    """All 22 methods survive the byte-for-byte move."""
    import bot.engines.volatility as bev
    assert hasattr(bev.VolatilityEngine, method_name), (
        f"VolatilityEngine.{method_name} missing post-Bit-6.1 extraction."
    )


VOLATILITY_STATIC_METHODS = (
    "_parzen_kernel",
    "_estimate_noise_variance",
    "_realized_quarticity",
    "_optimal_rk_bandwidth",
    "_realized_kernel",
    "_bipower_variation",
)


@pytest.mark.parametrize("method_name", VOLATILITY_STATIC_METHODS)
def test_volatility_engine_static_methods_remain_static(method_name):
    """The kernel-and-statistics @staticmethod decorators survive the move.
    tests/test_vol_engine.py calls these as `VolatilityEngine._method(...)`
    with no instance — if the decorator is dropped, all 30+ call sites in
    that test file silently become unbound-method TypeError."""
    src = VOLATILITY_PY.read_text()
    tree = ast.parse(src)
    vol_engine = next(
        node for node in ast.iter_child_nodes(tree)
        if isinstance(node, ast.ClassDef) and node.name == "VolatilityEngine"
    )
    method = next(
        (n for n in vol_engine.body
         if isinstance(n, ast.FunctionDef) and n.name == method_name),
        None,
    )
    assert method is not None, f"VolatilityEngine.{method_name} missing"
    decorator_names = [
        d.id for d in method.decorator_list if isinstance(d, ast.Name)
    ]
    assert "staticmethod" in decorator_names, (
        f"VolatilityEngine.{method_name} no longer has @staticmethod decorator. "
        f"tests/test_vol_engine.py calls it as `VolatilityEngine.{method_name}(...)` "
        f"with no instance — dropping @staticmethod silently breaks all those calls."
    )


def test_volatility_engine_uses_class_static_self_refs():
    """Search-anchor pin: VolatilityEngine._realized_quarticity and
    VolatilityEngine._parzen_kernel are referenced by class name (not
    self.) inside instance methods (`_optimal_rk_bandwidth`, `_realized_kernel`).
    These survive verbatim move because both are @staticmethod on the same
    class. Anti-rename guard: catches a `self._method` rewrite."""
    src = VOLATILITY_PY.read_text()
    assert "VolatilityEngine._realized_quarticity" in src, (
        "VolatilityEngine._realized_quarticity self-reference missing in "
        "bot/engines/volatility.py — likely rewritten to self._realized_quarticity, "
        "which would break in-class static-method resolution."
    )
    assert "VolatilityEngine._parzen_kernel" in src, (
        "VolatilityEngine._parzen_kernel self-reference missing in "
        "bot/engines/volatility.py — same drift as _realized_quarticity above."
    )


# ─── 7. Forbidden import guard (per-module allow-list) ─────────────────────


# Each engine module bans the C-extension numerical libraries that cache
# OpenBLAS thread count at module-import time. The bot/_thread_env-pinned
# chain (`OMP_NUM_THREADS=1` set before any C-extension import) is the
# threading-contention fix per kb/failures/cal-mlp-torch-thread-contention-apr29.md.
#
# ProbabilityEngine (Bit 6.2) is the deliberate exception: it imports
# scipy.stats.t / norminvgauss for Student-t and NIG CDF evaluation. The
# scipy.stats sub-module is pure Python over NumPy + a minimal C
# extension that does NOT call the BLAS thread-cache initializer at
# import (verified empirically on the bot/_thread_env contention regression
# tests). To keep the gate enforced for all OTHER engines, we
# parametrize per-module — adding a new engine without an explicit allow
# entry inherits the strict ban.
ENGINE_FORBIDDEN_IMPORTS = (
    # (module_path, friendly_name, forbidden_libraries)
    (VOLATILITY_PY, "bot/engines/volatility.py",
        ("numpy", "scipy", "torch", "sklearn", "pandas")),
    (PROBABILITY_PY, "bot/engines/probability.py",
        ("numpy", "torch", "sklearn", "pandas")),  # scipy.stats allowed
    (CALIBRATION_PY, "bot/engines/calibration.py",
        ("numpy", "scipy", "torch", "sklearn", "pandas")),  # Bit 6.3 strict ban
)


@pytest.mark.parametrize("module_path,friendly,forbidden",
                         ENGINE_FORBIDDEN_IMPORTS)
def test_no_forbidden_numerical_imports(module_path, friendly, forbidden):
    """Each engine module's allow-list is enforced. Adding a new engine
    requires explicitly listing its allowed numerical libraries here, so
    the OpenBLAS-thread-cache contention guard remains active by default.
    Bit 6.2 (probability) deliberately allows scipy.stats; Bit 6.1
    (volatility) bans the entire numerical stack."""
    src = module_path.read_text()
    for name in forbidden:
        assert f"import {name}" not in src, (
            f"{friendly} imports {name} — forbidden. The numerical-library "
            f"OpenBLAS-thread-cache contention guard "
            f"(kb/failures/cal-mlp-torch-thread-contention-apr29.md) requires "
            f"this module to use only its allow-listed scientific imports. "
            f"If a new dependency is genuinely needed, add it to "
            f"ENGINE_FORBIDDEN_IMPORTS with empirical evidence the import "
            f"does NOT trigger BLAS thread-cache initialization."
        )
        assert f"from {name}" not in src, (
            f"{friendly} uses `from {name} import ...` — forbidden per the "
            f"per-module allow-list above."
        )


def test_probability_imports_scipy_stats():
    """Positive pin: bot/engines/probability.py DOES import scipy.stats.
    Locks the deliberate exception above — if a future maintainer rewrites
    _cdf_complement to drop scipy in favor of math.erf or similar, this
    test should be updated and the entry in ENGINE_FORBIDDEN_IMPORTS
    tightened to match."""
    src = PROBABILITY_PY.read_text()
    assert "from scipy.stats import" in src, (
        "bot/engines/probability.py no longer imports from scipy.stats — "
        "either the implementation changed (then tighten ENGINE_FORBIDDEN_IMPORTS) "
        "or the file was corrupted. ProbabilityEngine._cdf_complement uses "
        "scipy.stats.t.cdf and scipy.stats.norminvgauss.cdf for Student-t and "
        "NIG distribution selection per dist_config.json."
    )


# ─── 8. RK_STATE_PATH class attribute survives ──────────────────────────────


def test_volatility_engine_rk_state_path_class_attr():
    """RK_STATE_PATH ('rk_state.json') and RK_SAVE_INTERVAL (60.0) are CLASS
    attributes on VolatilityEngine, not module-level. They survive byte-for-byte
    because the class body moved verbatim — but a future refactor could
    silently demote them to module-level and break `self.RK_STATE_PATH` access."""
    import bot.engines.volatility as bev
    assert bev.VolatilityEngine.RK_STATE_PATH == "rk_state.json"
    assert bev.VolatilityEngine.RK_SAVE_INTERVAL == 60.0


# ════════════════════════════════════════════════════════════════════════════
# Bit 6.2 — ProbabilityEngine
# ════════════════════════════════════════════════════════════════════════════


# ─── 9. ProbabilityEngine: file exists + identity + drift guards ────────────


def test_probability_module_exists():
    assert PROBABILITY_PY.is_file()


def test_subpackage_exports_probability_engine():
    import bot.engines
    assert hasattr(bot.engines, "ProbabilityEngine")


def test_probability_identity_through_bot_impl():
    """bot._impl.ProbabilityEngine is bot.engines.ProbabilityEngine
    is bot.engines.probability.ProbabilityEngine. The 19 bare-name
    `ProbabilityEngine.X(...)` call sites in bot/_impl.py and the 11
    `from bot import ProbabilityEngine` test imports all rely on
    these three references being the same object."""
    import bot._impl as b
    import bot.engines as be
    import bot.engines.probability as bep
    assert b.ProbabilityEngine is be.ProbabilityEngine is bep.ProbabilityEngine


def test_probability_identity_through_bot_proxy():
    """bot.ProbabilityEngine resolves through bot._BotProxy to the
    canonical class object. tests/test_probability_engine.py uses
    `from bot import ProbabilityEngine` 11 times."""
    import bot
    import bot.engines.probability as bep
    assert bot.ProbabilityEngine is bep.ProbabilityEngine


@pytest.mark.parametrize("class_name", ["ProbabilityEngine"])
def test_probability_class_not_defined_in_bot_impl(class_name):
    """Future drift guard: catches "I'll just add it back to _impl.py".

    The re-import chain in bot/_impl.py is the only place the name should
    resolve from after Bit 6.2.
    """
    tree = ast.parse(BOT_PY.read_text(), filename=str(BOT_PY))
    classdefs = [
        node for node in ast.iter_child_nodes(tree)
        if isinstance(node, ast.ClassDef) and node.name == class_name
    ]
    assert classdefs == [], (
        f"{class_name} ClassDef found at module scope in bot/_impl.py "
        f"(line {classdefs[0].lineno if classdefs else '?'}). The class was "
        f"extracted to bot/engines/probability.py in Bit 6.2 — re-introducing "
        f"it breaks the import chain and identity preservation."
    )


def test_probability_engine_class_defined_at_module_scope():
    """Positive pin: ProbabilityEngine ClassDef IS at module scope of
    bot/engines/probability.py (not nested, not under a try/except)."""
    tree = ast.parse(PROBABILITY_PY.read_text(), filename=str(PROBABILITY_PY))
    classdefs = [
        node for node in ast.iter_child_nodes(tree)
        if isinstance(node, ast.ClassDef) and node.name == "ProbabilityEngine"
    ]
    assert len(classdefs) == 1, (
        f"Expected exactly 1 module-scope ProbabilityEngine ClassDef in "
        f"bot/engines/probability.py, got {len(classdefs)}."
    )


def test_bot_impl_imports_probability_engine_from_engines():
    """bot/_impl.py must import ProbabilityEngine from bot.engines.

    AST-based to avoid false matches inside docstrings/comments. The 19
    bare-name `ProbabilityEngine.X(...)` call sites in bot/_impl.py rely
    on this re-export.
    """
    tree = ast.parse(BOT_PY.read_text(), filename=str(BOT_PY))
    imported = set()
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "bot.engines":
            for alias in node.names:
                imported.add(alias.name)
    assert "ProbabilityEngine" in imported, (
        "bot/_impl.py is missing `from bot.engines import ProbabilityEngine`. "
        "Without it, the 19 bare-name `ProbabilityEngine.X(...)` call sites "
        "(scan loop edge computation, counterfactual probability, dynamic "
        "cap lookup) all break."
    )


# ─── 10. ProbabilityEngine: constants resolve from canonical sources ────────


PROBABILITY_BOT_CONSTANTS = (
    "DISCREPANCY_PRICE",
    "DISCREPANCY_PROB",
    "DYNAMIC_CAP_SCHEDULE",
    "FIFTEEN_M_CALIBRATION_ENABLED",
    "HOURLY_DYNAMIC_CAP_SCHEDULE",
)


@pytest.mark.parametrize("name", PROBABILITY_BOT_CONSTANTS)
def test_probability_constants_resolve_from_bot_constants(name):
    """DISCREPANCY_*, DYNAMIC_CAP_SCHEDULE, HOURLY_DYNAMIC_CAP_SCHEDULE,
    and FIFTEEN_M_CALIBRATION_ENABLED live in bot.constants per Bit 3.1.
    The probability module imports them explicitly."""
    import bot.constants
    import bot.engines.probability as bep
    assert getattr(bep, name) is getattr(bot.constants, name), (
        f"bot.engines.probability.{name} drifted from bot.constants.{name}. "
        f"Either the constant moved (then update the import) or two "
        f"separate definitions exist (then collapse to bot.constants)."
    )


PROBABILITY_CONFIG_CONSTANTS = (
    "BETA_SLOPE",
    "DIST_CONFIG",
    "MAX_EFFECTIVE_PROB",
    "STUDENT_T_DF",
)


@pytest.mark.parametrize("name", PROBABILITY_CONFIG_CONSTANTS)
def test_probability_config_constants_resolve_from_config(name):
    """BETA_SLOPE, MAX_EFFECTIVE_PROB, STUDENT_T_DF live in config.py
    (pre-Bit-3.1, predate constant extraction); DIST_CONFIG is loaded
    from dist_config.json by config._load_dist_config(). All four
    must resolve from config, NOT bot.constants. L39 catch from
    Bit 6.2 pre-flight: getting this wrong is exactly the failure mode."""
    import config
    import bot.engines.probability as bep
    assert getattr(bep, name) is getattr(config, name), (
        f"bot.engines.probability.{name} drifted from config.{name}. "
        f"L39 lesson from Bit 6.1: per-name source verification belongs in "
        f"pre-flight; this test locks the verified partition."
    )


def test_probability_get_market_config_from_market_config():
    """get_market_config lives in market_config.py:222 — the
    MarketTypeConfig accessor used to read cal_eligible /
    cal_engine_enabled / temperature_* / cal_subtypes per product type."""
    import market_config
    import bot.engines.probability as bep
    assert bep.get_market_config is market_config.get_market_config


# ─── 11. ProbabilityEngine: method-presence + decorator pins ────────────────


PROBABILITY_METHODS = (
    "_cdf_complement",
    "compute",
    "counterfactual_prob",
    "_dynamic_cap",
    "_calibrate",
)


@pytest.mark.parametrize("method_name", PROBABILITY_METHODS)
def test_probability_engine_has_method(method_name):
    """All 5 staticmethods survive the byte-shifted move (the two methods
    that touch _CALIBRATION_ENGINE / _resolve_cal_engine internally
    introduce late-binding `from bot import _impl as _bot_impl` lookups
    — non-byte-for-byte but documented in the closeout)."""
    import bot.engines.probability as bep
    assert hasattr(bep.ProbabilityEngine, method_name), (
        f"ProbabilityEngine.{method_name} missing post-Bit-6.2 extraction."
    )


@pytest.mark.parametrize("method_name", PROBABILITY_METHODS)
def test_probability_engine_methods_remain_static(method_name):
    """All 5 ProbabilityEngine methods are @staticmethod. tests/test_probability_engine.py
    calls them as `ProbabilityEngine._method(...)` with no instance and the
    19 bare-name `ProbabilityEngine.X(...)` call sites in bot/_impl.py do
    the same — dropping @staticmethod silently breaks every call."""
    src = PROBABILITY_PY.read_text()
    tree = ast.parse(src)
    prob_engine = next(
        node for node in ast.iter_child_nodes(tree)
        if isinstance(node, ast.ClassDef) and node.name == "ProbabilityEngine"
    )
    method = next(
        (n for n in prob_engine.body
         if isinstance(n, ast.FunctionDef) and n.name == method_name),
        None,
    )
    assert method is not None, f"ProbabilityEngine.{method_name} missing"
    decorator_names = [
        d.id for d in method.decorator_list if isinstance(d, ast.Name)
    ]
    assert "staticmethod" in decorator_names, (
        f"ProbabilityEngine.{method_name} no longer has @staticmethod decorator. "
        f"All 5 methods are static — dropping the decorator silently breaks "
        f"both bot/_impl.py call sites and tests/test_probability_engine.py."
    )


def test_probability_engine_uses_class_static_self_refs():
    """Search-anchor pin: ProbabilityEngine.{_cdf_complement, _dynamic_cap,
    _calibrate} are referenced by class name (not self.) inside the
    instance methods compute() and counterfactual_prob(). These survive
    verbatim move because all are @staticmethod on the same class.
    Anti-rename guard: catches a `self._method` rewrite that would
    silently break in-class static-method resolution."""
    src = PROBABILITY_PY.read_text()
    assert "ProbabilityEngine._cdf_complement" in src, (
        "ProbabilityEngine._cdf_complement self-reference missing in "
        "bot/engines/probability.py — likely rewritten to "
        "self._cdf_complement, which would break in-class static-method "
        "resolution."
    )
    assert "ProbabilityEngine._dynamic_cap" in src, (
        "ProbabilityEngine._dynamic_cap self-reference missing — same "
        "drift as _cdf_complement above."
    )
    assert "ProbabilityEngine._calibrate" in src, (
        "ProbabilityEngine._calibrate self-reference missing — same "
        "drift as _cdf_complement above."
    )


# ─── 12. ProbabilityEngine: path-B module-attr access pattern (Bit 6.3) ────
#
# Bit 6.2 originally used a late-binding `from bot import _impl as _bot_impl`
# pattern inside compute() and counterfactual_prob() because _CALIBRATION_ENGINE
# + _resolve_cal_engine lived in bot/_impl.py BELOW the line-109 engines
# re-export. Bit 6.3 path-B (2026-05-10) relocated those names to
# bot/engines/calibration.py; ProbabilityEngine now reaches them via top-level
# `from bot.engines import calibration as _cal_state` plus `_cal_state.X`
# attribute access. The .importlinter `bot.engines.probability -> bot._impl`
# carve-out was removed in the same commit.


def test_probability_uses_top_level_cal_state_alias():
    """Search-anchor pin for the path-B module-attribute access pattern.
    ProbabilityEngine reaches the mutable _CALIBRATION_ENGINE singleton
    + _resolve_cal_engine resolver via `from bot.engines import
    calibration as _cal_state` at module top + `_cal_state.X` access.
    Module-attribute access preserves the mutable-singleton freshness
    guarantee (every read sees the current value because we go through
    the module reference). A future refactor that drops the alias and
    inlines a top-level `from bot.engines.calibration import _CALIBRATION_ENGINE`
    would break the freshness guarantee — this test catches that."""
    src = PROBABILITY_PY.read_text()
    assert "from bot.engines import calibration as _cal_state" in src, (
        "bot/engines/probability.py no longer imports calibration as "
        "_cal_state at module top. The Bit 6.3 path-B refactor relies "
        "on this alias to reach the mutable _CALIBRATION_ENGINE "
        "singleton + _resolve_cal_engine resolver via module-attribute "
        "access. If reverted to the Bit 6.2 late-binding pattern "
        "(`from bot import _impl as _bot_impl`), restore the "
        ".importlinter `bot.engines.probability -> bot._impl` "
        "ignore_imports carve-out + revert the companion contracts "
        "test in the same commit."
    )
    assert "_cal_state._CALIBRATION_ENGINE" in src, (
        "bot/engines/probability.py no longer accesses "
        "_cal_state._CALIBRATION_ENGINE. The mutable-singleton "
        "freshness guarantee depends on this attribute access pattern."
    )
    assert "_cal_state._resolve_cal_engine" in src, (
        "bot/engines/probability.py no longer accesses "
        "_cal_state._resolve_cal_engine through the alias."
    )


def test_probability_no_late_binding_or_bot_impl_import():
    """Negative pin: probability.py no longer late-binds bot._impl (the
    Bit 6.2 pattern). Bit 6.3 path-B lifted it. Both forms are forbidden:

    - Top-level ``from bot._impl import ...`` (would cycle: bot._impl
      imports bot.engines at line ~109 BEFORE probability.py finishes
      loading; this is the historic cycle the late-binding worked around).
    - Top-level ``import bot._impl``.
    - Method-body ``from bot import _impl as _bot_impl`` (the Bit 6.2
      workaround — now dead code; if it reappears, the carve-out it
      was paired with should be restored too).

    The .importlinter contract enforces the first two via the
    engines-no-impl rule (Bit 6.3 dropped the
    `bot.engines.probability -> bot._impl` ignore_imports carve-out).
    This test adds AST + literal-string defenses so the lint-imports
    contract isn't the only gate."""
    src = PROBABILITY_PY.read_text()
    tree = ast.parse(src, filename=str(PROBABILITY_PY))
    # Top-level ImportFrom / Import: no bot._impl.
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module != "bot._impl", (
                f"bot/engines/probability.py has a top-level "
                f"`from bot._impl import ...` at line {node.lineno}. "
                f"Path-B expects `from bot.engines import calibration "
                f"as _cal_state` instead."
            )
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "bot._impl", (
                    f"bot/engines/probability.py has a top-level "
                    f"`import bot._impl` at line {node.lineno}. "
                    f"Path-B expects no bot._impl access at all."
                )
    # Method-body literal: catches the Bit 6.2 late-binding shape.
    # NOTE: the docstring may legitimately MENTION the historical
    # pattern as text; we tolerate `from bot import _impl as
    # _bot_impl` only inside docstring/comment contexts. AST walk
    # already covered the import-statement form above; the literal
    # check below is only for hand-typed CODE that uses it.
    # Strategy: search for `_bot_impl.` (attribute access on the
    # alias). If it exists, the alias is being used as live code.
    assert "_bot_impl." not in src, (
        "bot/engines/probability.py uses `_bot_impl.X` attribute "
        "access — that's the Bit 6.2 late-binding pattern, lifted by "
        "Bit 6.3 path-B. Use `_cal_state.X` (post-Bit-6.3) instead, "
        "and ensure the .importlinter carve-out and companion tests "
        "are NOT also re-introduced."
    )


def test_probability_module_attr_access_observes_calibration_engine_mutation():
    """Behavioral regression: the path-B module-attribute access
    pattern means mutations to `bot.engines.calibration._CALIBRATION_ENGINE`
    are observable inside ProbabilityEngine methods. If a future
    refactor caches the reference at module load (e.g., changes the
    code from `_cal_state._CALIBRATION_ENGINE` to a top-level
    `from bot.engines.calibration import _CALIBRATION_ENGINE`), the
    sentinel set after import would not be visible to the cached
    reference and this test fails."""
    import bot.engines.calibration as cal_mod
    import bot.engines.probability as bep

    saved = cal_mod._CALIBRATION_ENGINE
    sentinel_seen = []

    class _Sentinel:
        active_method = "sentinel"

        def is_learned_method_active(self):
            sentinel_seen.append("is_learned_method_active")
            return False

        def calibrate(self, raw, cap, seconds_to_close):
            sentinel_seen.append(("calibrate", raw, cap, seconds_to_close))
            return 0.42

    try:
        cal_mod._CALIBRATION_ENGINE = _Sentinel()
        # counterfactual_prob's `_cal_state._CALIBRATION_ENGINE is not None`
        # branch routes to the sentinel's calibrate.
        out = bep.ProbabilityEngine.counterfactual_prob(
            spot=100.0, threshold=99.0, seconds_remaining=600.0,
            alt_blended_rv=0.001, asset=None, product_type=None,
        )
        assert out == 0.42, (
            "Module-attribute access did not see the mutated "
            "_CALIBRATION_ENGINE sentinel — counterfactual_prob "
            "returned the fallback _calibrate value instead of the "
            "sentinel.calibrate(...) value. The mutable-singleton "
            "freshness guarantee is broken."
        )
        assert any(
            isinstance(s, tuple) and s[0] == "calibrate"
            for s in sentinel_seen
        ), "Sentinel.calibrate was never invoked."
    finally:
        cal_mod._CALIBRATION_ENGINE = saved


# ════════════════════════════════════════════════════════════════════════════
# Bit 6.3 — CalibrationEngine (+ path-B singleton/helper relocation)
# ════════════════════════════════════════════════════════════════════════════
#
# Byte-for-byte class body extraction. Free-variable analysis of the class
# body returns ZERO suspect hits — the class itself never reads
# _CALIBRATION_ENGINE / _CAL_REGISTRY / _resolve_cal_engine.
#
# Bit 6.3 path-B (2026-05-10) ALSO relocated the calibration runtime state
# from bot/_impl.py to bot/engines/calibration.py alongside the class:
#   _CALIBRATION_ENGINE: Optional[CalibrationEngine] (singleton — bare-typed
#                                                    post-path-B since the
#                                                    class is in scope)
#   _CAL_REGISTRY: Dict[str, CalibrationEngine] (registry dict)
#   _derive_subtype, _derive_asset_filter, _resolve_cal_engine (helpers)
# Both bot/_impl.py and bot/engines/probability.py reach them via top-level
# `from bot.engines import calibration as _cal_state` + `_cal_state.X`. The
# .importlinter `bot.engines.probability -> bot._impl` carve-out (Pillar 2)
# was removed in the same commit.
#
# Pre-flight checks (L39 + L40 + L38) all returned 0 hits per
# kb/decisions/bit-6.3-plan-may10.md.


# ─── 13. CalibrationEngine: file exists + identity + drift guards ───────────


def test_calibration_module_exists():
    assert CALIBRATION_PY.is_file()


def test_subpackage_exports_calibration_engine():
    import bot.engines
    assert hasattr(bot.engines, "CalibrationEngine")


def test_calibration_identity_through_bot_impl():
    """bot._impl.CalibrationEngine is bot.engines.CalibrationEngine
    is bot.engines.calibration.CalibrationEngine. The 3 instantiation
    sites in MainLoop.__init__ + the in-class self-references
    (CalibrationEngine._fallback_calibrate, ._solve_3x3) + the
    `from bot import CalibrationEngine` test imports all rely on these
    three references being the same object."""
    import bot._impl as b
    import bot.engines as be
    import bot.engines.calibration as bec
    assert b.CalibrationEngine is be.CalibrationEngine is bec.CalibrationEngine


def test_calibration_identity_through_bot_proxy():
    """bot.CalibrationEngine resolves through bot._BotProxy to the
    canonical class object. tests/test_calibration_engine.py uses
    `from bot import CalibrationEngine` 11 times in setUpClass blocks."""
    import bot
    import bot.engines.calibration as bec
    assert bot.CalibrationEngine is bec.CalibrationEngine


def test_calibration_module_attribute_post_extraction():
    """Locks __module__ to the new file. If a future refactor accidentally
    re-defines the class in bot/_impl.py (or subclasses it elsewhere), the
    proxy chain might silently route to the wrong object."""
    import bot
    assert bot.CalibrationEngine.__module__ == "bot.engines.calibration", (
        f"bot.CalibrationEngine.__module__ = "
        f"{bot.CalibrationEngine.__module__!r}, expected "
        f"'bot.engines.calibration'."
    )


@pytest.mark.parametrize("class_name", ["CalibrationEngine"])
def test_calibration_class_not_defined_in_bot_impl(class_name):
    """Future drift guard: catches "I'll just add it back to _impl.py".

    The re-import chain in bot/_impl.py is the only place the name should
    resolve from after Bit 6.3.
    """
    tree = ast.parse(BOT_PY.read_text(), filename=str(BOT_PY))
    classdefs = [
        node for node in ast.iter_child_nodes(tree)
        if isinstance(node, ast.ClassDef) and node.name == class_name
    ]
    assert classdefs == [], (
        f"{class_name} ClassDef found at module scope in bot/_impl.py "
        f"(line {classdefs[0].lineno if classdefs else '?'}). The class was "
        f"extracted to bot/engines/calibration.py in Bit 6.3 — re-introducing "
        f"it breaks the import chain and identity preservation."
    )


def test_calibration_engine_class_defined_at_module_scope():
    """Positive pin: CalibrationEngine ClassDef IS at module scope of
    bot/engines/calibration.py (not nested, not under a try/except)."""
    tree = ast.parse(CALIBRATION_PY.read_text(), filename=str(CALIBRATION_PY))
    classdefs = [
        node for node in ast.iter_child_nodes(tree)
        if isinstance(node, ast.ClassDef) and node.name == "CalibrationEngine"
    ]
    assert len(classdefs) == 1, (
        f"Expected exactly 1 module-scope CalibrationEngine ClassDef in "
        f"bot/engines/calibration.py, got {len(classdefs)}."
    )


def test_bot_impl_imports_calibration_engine_from_engines():
    """bot/_impl.py must import CalibrationEngine from bot.engines.

    AST-based to avoid false matches inside docstrings/comments. The 3
    bare-name `CalibrationEngine(...)` instantiation sites in
    MainLoop.__init__ rely on this re-export.
    """
    tree = ast.parse(BOT_PY.read_text(), filename=str(BOT_PY))
    imported = set()
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "bot.engines":
            for alias in node.names:
                imported.add(alias.name)
    assert "CalibrationEngine" in imported, (
        "bot/_impl.py is missing `from bot.engines import CalibrationEngine`. "
        "Without it, the 3 bare-name `CalibrationEngine(...)` construction "
        "sites in MainLoop.__init__ break. Note: the singleton + registry "
        "module-level annotations were path-B-relocated to "
        "bot/engines/calibration.py with bare-typed annotations; this "
        "import is needed for the construction sites only."
    )


# ─── 14. CalibrationEngine: constants resolve from canonical sources ────────


CALIBRATION_BOT_CONSTANTS = (
    "CALIBRATION_BRIER_WINDOW",
    "CALIBRATION_MIN_SAMPLES_BETA",
    "CALIBRATION_MIN_SAMPLES_BLR",
    "CALIBRATION_MIN_SAMPLES_PLATT",
    "CALIBRATION_RETRAIN_INTERVAL",
    "CALIBRATION_STATE_PATH",
    "MARKET_BLEND_W",
    "MIN_EDGE_PCT",
    "SHADOW_BLEND_W",
    "SHADOW_CAL_PIPELINE",
)


@pytest.mark.parametrize("name", CALIBRATION_BOT_CONSTANTS)
def test_calibration_constants_resolve_from_bot_constants(name):
    """All 10 CALIBRATION_* tunables + MARKET_BLEND_W / MIN_EDGE_PCT /
    SHADOW_BLEND_W / SHADOW_CAL_PIPELINE live in bot.constants per Bit 3.1.
    The calibration module imports them explicitly."""
    import bot.constants
    import bot.engines.calibration as bec
    assert getattr(bec, name) is getattr(bot.constants, name), (
        f"bot.engines.calibration.{name} drifted from bot.constants.{name}. "
        f"Either the constant moved (then update the import) or two "
        f"separate definitions exist (then collapse to bot.constants)."
    )


CALIBRATION_CONFIG_CONSTANTS = (
    "BETA_SLOPE",
    "MAX_EFFECTIVE_PROB",
    "NUMERICAL_SAFETY_CEILING",
)


@pytest.mark.parametrize("name", CALIBRATION_CONFIG_CONSTANTS)
def test_calibration_config_constants_resolve_from_config(name):
    """BETA_SLOPE, MAX_EFFECTIVE_PROB, NUMERICAL_SAFETY_CEILING live in
    config.py (pre-Bit-3.1, predate constant extraction). All three must
    resolve from config, NOT bot.constants. L39 catch from Bit 6.3
    pre-flight: this exactly mirrors the Bit 6.2 partition lesson."""
    import config
    import bot.engines.calibration as bec
    assert getattr(bec, name) is getattr(config, name), (
        f"bot.engines.calibration.{name} drifted from config.{name}. "
        f"L39 lesson from Bit 6.1: per-name source verification belongs in "
        f"pre-flight; this test locks the verified partition."
    )


def test_calibration_get_cal_excluded_types_from_market_config():
    """get_cal_excluded_types lives in market_config.py — the helper that
    enumerates product types excluded from calibration training."""
    import market_config
    import bot.engines.calibration as bec
    assert bec.get_cal_excluded_types is market_config.get_cal_excluded_types


def test_calibration_calculate_taker_fee_from_models():
    """calculate_taker_fee lives in models.py. CalibrationEngine uses it
    inside backtest_adaptive_vs_fixed for fee-aware Brier comparison."""
    import bot.models as models
    import bot.engines.calibration as bec
    assert bec.calculate_taker_fee is models.calculate_taker_fee


# ─── 15. CalibrationEngine: method-presence + decorator pins ────────────────


CALIBRATION_METHODS = (
    "__init__",
    "_load_state",
    "_save_state",
    "calibrate",
    "_fallback_calibrate",
    "_fit_temperature",
    "_temperature_predict",
    "shadow_calibration_pipeline",
    "add_observation",
    "maybe_retrain",
    "load_training_data_from_db",
    "_platt_predict",
    "_train_platt",
    "_stc_platt_predict",
    "_train_stc_platt",
    "_beta_cal_predict",
    "_train_beta_cal",
    "_solve_3x3",
    "_blr_predict",
    "_train_blr",
    "rolling_brier_score",
    "_compute_brier_on_observations",
    "is_learned_method_active",
    "_apply_uncertainty_shrinkage",
    "_compute_brier_for_method",
    "_bucket_observation",
    "get_empirical_bucket_stats",
    "get_diagnostics",
    "backtest_adaptive_vs_fixed",
)


@pytest.mark.parametrize("method_name", CALIBRATION_METHODS)
def test_calibration_engine_has_method(method_name):
    """All 29 methods survive the byte-for-byte move."""
    import bot.engines.calibration as bec
    assert hasattr(bec.CalibrationEngine, method_name), (
        f"CalibrationEngine.{method_name} missing post-Bit-6.3 extraction."
    )


CALIBRATION_STATIC_METHODS = (
    "_fallback_calibrate",
    "_solve_3x3",
)


@pytest.mark.parametrize("method_name", CALIBRATION_STATIC_METHODS)
def test_calibration_engine_static_methods_remain_static(method_name):
    """The 2 @staticmethod decorators (_fallback_calibrate + _solve_3x3)
    survive the move. CalibrationEngine.calibrate() falls back to
    `CalibrationEngine._fallback_calibrate(...)` as a class-level
    bare-name lookup — if @staticmethod is dropped, the call becomes
    unbound-method TypeError."""
    src = CALIBRATION_PY.read_text()
    tree = ast.parse(src)
    cal_engine = next(
        node for node in ast.iter_child_nodes(tree)
        if isinstance(node, ast.ClassDef) and node.name == "CalibrationEngine"
    )
    method = next(
        (n for n in cal_engine.body
         if isinstance(n, ast.FunctionDef) and n.name == method_name),
        None,
    )
    assert method is not None, f"CalibrationEngine.{method_name} missing"
    decorator_names = [
        d.id for d in method.decorator_list if isinstance(d, ast.Name)
    ]
    assert "staticmethod" in decorator_names, (
        f"CalibrationEngine.{method_name} no longer has @staticmethod "
        f"decorator. CalibrationEngine.calibrate() calls "
        f"_fallback_calibrate as a bare-name class lookup; "
        f"_train_beta_cal calls _solve_3x3 the same way. Dropping "
        f"@staticmethod silently breaks both."
    )


def test_calibration_engine_uses_class_static_self_refs():
    """Search-anchor pin: CalibrationEngine.{_fallback_calibrate,
    _solve_3x3} are referenced by class name (not self.) inside instance
    methods. These survive verbatim move because both are @staticmethod
    on the same class. Anti-rename guard."""
    src = CALIBRATION_PY.read_text()
    assert "CalibrationEngine._fallback_calibrate" in src, (
        "CalibrationEngine._fallback_calibrate self-reference missing in "
        "bot/engines/calibration.py — likely rewritten to "
        "self._fallback_calibrate, which would break in-class static-method "
        "resolution."
    )
    assert "CalibrationEngine._solve_3x3" in src, (
        "CalibrationEngine._solve_3x3 self-reference missing in "
        "bot/engines/calibration.py — same drift as _fallback_calibrate."
    )


# ─── 16. CalibrationEngine: byte-for-byte regression seal (no late-binding) ─


def test_calibration_no_top_level_bot_impl_import():
    """Negative pin for the byte-for-byte status. CalibrationEngine is a
    pure leaf — free-variable analysis returned zero references to
    _CALIBRATION_ENGINE / _CAL_REGISTRY / _resolve_cal_engine. A future
    "let's be consistent with ProbabilityEngine" refactor that adds a
    `from bot._impl import ...` (or `from bot import _impl as _bot_impl`)
    inside any CalibrationEngine method is wrong: this class doesn't
    need late-binding because it doesn't read those names. This test
    catches the accidental introduction of unnecessary late-binding."""
    src = CALIBRATION_PY.read_text()
    tree = ast.parse(src, filename=str(CALIBRATION_PY))

    # Module-level: no `from bot._impl import ...` and no `import bot._impl`.
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module != "bot._impl", (
                f"bot/engines/calibration.py has a top-level "
                f"`from bot._impl import ...` at line {node.lineno}. "
                f"CalibrationEngine is a byte-for-byte extraction with NO "
                f"reference to bot._impl runtime state — adding such an "
                f"import is unnecessary and may cause circular-import "
                f"failure at module load."
            )
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "bot._impl", (
                    f"bot/engines/calibration.py has a top-level "
                    f"`import bot._impl` at line {node.lineno}. Same "
                    f"issue as above."
                )

    # Method-level: no late-binding `from bot import _impl as _bot_impl`
    # anywhere. Bit 6.2 introduced this pattern for ProbabilityEngine
    # because it actively reads the mutable singleton; CalibrationEngine
    # does NOT and shouldn't.
    assert "from bot import _impl as _bot_impl" not in src, (
        "bot/engines/calibration.py uses the late-binding pattern "
        "`from bot import _impl as _bot_impl` — but CalibrationEngine "
        "doesn't reference any bot._impl runtime state (free-variable "
        "analysis confirms zero hits on _CALIBRATION_ENGINE / "
        "_CAL_REGISTRY / _resolve_cal_engine). If this test fails, "
        "either a behavior was added that genuinely needs late-binding "
        "(then update kb/decisions/bit-6.3-plan-may10.md and this test) "
        "or the import is unnecessary (then remove it)."
    )


# ─── 17. CalibrationEngine: L33 module-level forward-ref pins ───────────────


def test_bot_impl_no_longer_owns_calibration_runtime_state():
    """Bit 6.3 path-B (2026-05-10) relocated _CALIBRATION_ENGINE +
    _CAL_REGISTRY + _derive_subtype + _derive_asset_filter +
    _resolve_cal_engine OUT of bot/_impl.py into bot/engines/calibration.py.

    Negative pin: bot/_impl.py no longer DEFINES these names. The
    module-attribute access pattern via `_cal_state` (search anchor:
    `from bot.engines import calibration as _cal_state`) is the only
    way bot/_impl.py reaches them now. If this test fails, either:
    (a) a rebase/merge restored the old definitions (revert), or
    (b) someone re-introduced the singleton ownership in this file
    (which would also re-require the .importlinter carve-out for
    probability.py — undesirable per the path-B refactor)."""
    import bot._impl
    assert not hasattr(bot._impl, "_CALIBRATION_ENGINE"), (
        "bot._impl._CALIBRATION_ENGINE exists — Bit 6.3 path-B "
        "relocated this singleton to bot/engines/calibration.py. "
        "If this test fails, the relocation was reverted; restore "
        "by removing the definition from bot/_impl.py and ensuring "
        "the alias `_cal_state` is used."
    )
    assert not hasattr(bot._impl, "_CAL_REGISTRY"), (
        "bot._impl._CAL_REGISTRY exists — Bit 6.3 path-B relocated."
    )
    assert not hasattr(bot._impl, "_resolve_cal_engine"), (
        "bot._impl._resolve_cal_engine exists — Bit 6.3 path-B relocated."
    )
    assert not hasattr(bot._impl, "_derive_subtype"), (
        "bot._impl._derive_subtype exists — Bit 6.3 path-B relocated."
    )
    assert not hasattr(bot._impl, "_derive_asset_filter"), (
        "bot._impl._derive_asset_filter exists — Bit 6.3 path-B relocated."
    )


def test_calibration_module_owns_runtime_state():
    """Positive pin (Bit 6.3 path-B): bot/engines/calibration.py owns
    the runtime state (_CALIBRATION_ENGINE, _CAL_REGISTRY) and the
    three helpers (_derive_subtype, _derive_asset_filter,
    _resolve_cal_engine). The annotations are bare-typed (NOT
    string-quoted) because CalibrationEngine is in scope at the
    module level."""
    import bot.engines.calibration as cal
    # State exists.
    assert hasattr(cal, "_CALIBRATION_ENGINE"), (
        "bot/engines/calibration.py is missing _CALIBRATION_ENGINE — "
        "Bit 6.3 path-B should have relocated it from bot/_impl.py."
    )
    assert hasattr(cal, "_CAL_REGISTRY"), "_CAL_REGISTRY missing"
    assert callable(cal._resolve_cal_engine), "_resolve_cal_engine missing"
    assert callable(cal._derive_subtype), "_derive_subtype missing"
    assert callable(cal._derive_asset_filter), "_derive_asset_filter missing"
    # Initial state.
    assert cal._CALIBRATION_ENGINE is None
    assert cal._CAL_REGISTRY == {}
    # Source-level: annotations are bare-typed (no quoted forward refs
    # because the class is in scope).
    src = CALIBRATION_PY.read_text()
    assert "_CALIBRATION_ENGINE: Optional[CalibrationEngine]" in src, (
        "_CALIBRATION_ENGINE annotation in bot/engines/calibration.py "
        "is not bare-typed `Optional[CalibrationEngine]` — Bit 6.3 path-B "
        "expected the move to drop the string-quoted forward ref since "
        "CalibrationEngine is now in scope."
    )
    assert "_CAL_REGISTRY: Dict[str, CalibrationEngine]" in src, (
        "_CAL_REGISTRY annotation in bot/engines/calibration.py is not "
        "bare-typed `Dict[str, CalibrationEngine]`."
    )


def test_bot_impl_uses_cal_state_alias():
    """Positive pin (Bit 6.3 path-B): bot/_impl.py imports the
    calibration module as `_cal_state` and accesses runtime state
    via that alias. If reverted to bare-name access, the
    `_CALIBRATION_ENGINE` (etc.) bare names would resolve via
    `from bot.engines import *`-style namespace pollution
    (which doesn't happen — the imports are explicit) or as
    NameError. This test pins the alias as the access pattern."""
    src = BOT_PY.read_text()
    assert "from bot.engines import calibration as _cal_state" in src, (
        "bot/_impl.py is missing `from bot.engines import calibration "
        "as _cal_state`. Path-B requires this alias to reach the "
        "relocated _CALIBRATION_ENGINE / _CAL_REGISTRY / _resolve_cal_engine."
    )
    # Sanity: bare-name reads of the relocated names should be GONE
    # (everything goes through `_cal_state.X`). Allow refs inside
    # comments + the relocation breadcrumb line where the names
    # appear as documentation, not code.
    tree = ast.parse(src, filename=str(BOT_PY))
    bare_reads = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id in {
                "_CALIBRATION_ENGINE",
                "_CAL_REGISTRY",
                "_resolve_cal_engine",
                "_derive_subtype",
                "_derive_asset_filter",
            }:
                bare_reads.append((node.id, node.lineno))
    assert not bare_reads, (
        f"bot/_impl.py has bare-name AST Load for path-B-relocated "
        f"names: {bare_reads}. Every read should go through "
        f"`_cal_state.X`. The bare-name comment in the breadcrumb at "
        f"line ~169 is fine (comment, not code) — but a Load node "
        f"means actual code-path bare access, which would NameError."
    )


def test_no_class_in_bot_impl_init_annotates_calibration_engine():
    """L33 negative pin: no class.__init__ in bot/_impl.py has a
    parameter annotated `CalibrationEngine` or `Optional[CalibrationEngine]`.
    The 3 type-annotation sites for CalibrationEngine in bot/_impl.py are
    all module-scope (not inside a class init): `_CALIBRATION_ENGINE`,
    `_CAL_REGISTRY`, `_resolve_cal_engine` return. If a future class is
    added with `cal: CalibrationEngine` in __init__, this test fires —
    add it to the breadcrumb in the line-109 re-export."""
    src = BOT_PY.read_text()
    tree = ast.parse(src)
    consumers = []
    for node in ast.iter_child_nodes(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for inner in node.body:
            if not (isinstance(inner, ast.FunctionDef) and inner.name == "__init__"):
                continue
            init_src = ast.get_source_segment(src, inner) or ""
            if (": CalibrationEngine" in init_src
                    or "Optional[CalibrationEngine]" in init_src
                    or "Optional['CalibrationEngine']" in init_src
                    or 'Optional["CalibrationEngine"]' in init_src):
                consumers.append(node.name)
    assert consumers == [], (
        f"Expected NO class __init__ in bot/_impl.py to annotate a "
        f"parameter as CalibrationEngine, got: {consumers}. Either a "
        f"new consumer was added (then update the breadcrumb at "
        f"bot/_impl.py:109 to enumerate it, mirroring "
        f"`vol: VolatilityEngine` on OpportunityScanner) or this is a "
        f"drift class (per L33). Update both this test and the breadcrumb "
        f"in the same atomic commit."
    )


# ─── 18. CalibrationEngine: lifecycle smoke (instantiation + state) ─────────


def test_calibration_engine_instantiates_with_temp_state(tmp_path):
    """Behavioral regression: __init__ + _load_state + _save_state survive
    the byte-for-byte move. Constructor signature
    (state_path, label, accepted_stages) must work and the
    fixed_beta fallback active_method must be preserved."""
    import bot.engines.calibration as bec

    state_path = str(tmp_path / "calibration_state.json")
    eng = bec.CalibrationEngine(
        state_path=state_path,
        label="TestCalEngine",
        accepted_stages=("candidate",),
    )
    # No state file yet → falls back to fixed_beta.
    assert eng.active_method == "fixed_beta"
    assert eng._label == "TestCalEngine"
    assert eng._accepted_stages == ("candidate",)
    # _save_state writes; _load_state on a fresh instance reads.
    eng._save_state()
    eng2 = bec.CalibrationEngine(state_path=state_path,
                                  label="TestCalEngine2")
    assert eng2.active_method == "fixed_beta"


def test_calibration_engine_fallback_calibrate_matches_probability_calibrate():
    """CalibrationEngine._fallback_calibrate and ProbabilityEngine._calibrate
    must produce identical outputs — the docstring on
    CalibrationEngine._fallback_calibrate explicitly states this. If
    either implementation drifts (e.g., a future bit changes BETA_SLOPE
    in only one of the two), the calibration cascade fallback diverges
    from the ProbabilityEngine fallback."""
    import bot.engines.calibration as bec
    import bot.engines.probability as bep

    for raw in (0.10, 0.50, 0.85, 0.95, 0.99):
        cap = 0.93
        cal_out = bec.CalibrationEngine._fallback_calibrate(raw, cap)
        prob_out = bep.ProbabilityEngine._calibrate(raw, cap)
        assert abs(cal_out - prob_out) < 1e-9, (
            f"CalibrationEngine._fallback_calibrate({raw}) = {cal_out} "
            f"but ProbabilityEngine._calibrate({raw}) = {prob_out}. "
            f"The two implementations drifted — the byte-for-byte "
            f"contract is broken."
        )
