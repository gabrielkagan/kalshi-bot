"""Bit 6.1 + 6.2 — VolatilityEngine + ProbabilityEngine extracted from bot/_impl.py to bot/engines/.

Bit 6.1 (2026-05-09):
  VolatilityEngine → bot/engines/volatility.py
  (Sprint 6 Bit 6.1; first leaf in the new bot/engines/ subpackage. Establishes
  the bot/engines/__init__.py shim.)

Bit 6.2 (2026-05-09):
  ProbabilityEngine → bot/engines/probability.py
  (Sprint 6 Bit 6.2; second leaf — Student-t / NIG win-prob CDF + adaptive
  calibration cascade. CalibrationEngine still inline pending Bit 6.3.)

Locks the contract between bot/_impl.py (which does
``from bot.engines import VolatilityEngine, ProbabilityEngine`` after the
bot.feeds import) and the bot/engines/ subpackage. Mirrors
tests/test_feeds_extraction.py (Bit 4.5a/4.5b) and
tests/test_fetchers_extraction.py (Bit 4.4).

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
    import models
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


def test_opportunity_scanner_still_annotates_vol_volatility_engine():
    """L33 positive pin: OpportunityScanner.__init__ has `vol: VolatilityEngine`
    annotation — verifies the consumer-class identity. Lives in bot/_impl.py."""
    src = BOT_PY.read_text()
    tree = ast.parse(src)
    scanner = next(
        (
            node for node in ast.iter_child_nodes(tree)
            if isinstance(node, ast.ClassDef) and node.name == "OpportunityScanner"
        ),
        None,
    )
    assert scanner is not None, "OpportunityScanner ClassDef missing from bot/_impl.py."
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
    annotated `vol: VolatilityEngine`. Catches wrong-class attribution
    drift (L33)."""
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


# ─── 12. ProbabilityEngine: late-binding regression (Bit 6.2 deviation) ────


def test_probability_late_binding_search_anchors():
    """Search-anchor pin for the late-binding pattern. ProbabilityEngine
    deviates from byte-for-byte: it accesses bot._impl._CALIBRATION_ENGINE
    and bot._impl._resolve_cal_engine via `from bot import _impl as
    _bot_impl` inside compute() and counterfactual_prob(). A future
    "cleanup" refactor might inline these as top-level imports — that
    would either ImportError at module load (line 109 of bot/_impl.py
    triggers engines import BEFORE _CALIBRATION_ENGINE/_resolve_cal_engine
    are defined) or capture a stale `None` snapshot of the mutable
    singleton. This test catches both regressions."""
    src = PROBABILITY_PY.read_text()
    assert "from bot import _impl as _bot_impl" in src, (
        "bot/engines/probability.py no longer uses the late-binding "
        "pattern `from bot import _impl as _bot_impl`. _CALIBRATION_ENGINE "
        "is a MUTABLE module-level singleton in bot/_impl.py; capturing "
        "it once at module load would freeze the initial None reference. "
        "_resolve_cal_engine is defined at bot/_impl.py:212, AFTER the "
        "line-109 `from bot.engines import ProbabilityEngine` re-export "
        "— a top-level import would ImportError at load time."
    )
    assert "_bot_impl._CALIBRATION_ENGINE" in src, (
        "bot/engines/probability.py no longer accesses "
        "_bot_impl._CALIBRATION_ENGINE through the late-binding alias. "
        "The mutable-singleton freshness guarantee depends on this "
        "attribute access pattern."
    )
    assert "_bot_impl._resolve_cal_engine" in src, (
        "bot/engines/probability.py no longer accesses "
        "_bot_impl._resolve_cal_engine through the late-binding alias."
    )


def test_probability_no_top_level_bot_impl_import():
    """Negative pin for the late-binding pattern. A top-level
    `from bot._impl import _CALIBRATION_ENGINE` or
    `from bot._impl import _resolve_cal_engine` (or the equivalent
    `import bot._impl`) at module scope would ImportError at module
    load: bot/_impl.py:~105 triggers the bot.engines import BEFORE
    bot._impl finishes executing lines 164 / 208 where these names
    are defined. The late-binding-inside-method pattern is the
    correct fix; this test guards against accidental relocation."""
    tree = ast.parse(PROBABILITY_PY.read_text(), filename=str(PROBABILITY_PY))
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module != "bot._impl", (
                f"bot/engines/probability.py has a top-level "
                f"`from bot._impl import ...` at line {node.lineno}. "
                f"This causes a circular-import failure at module load. "
                f"Use the late-binding pattern `from bot import _impl as "
                f"_bot_impl` INSIDE the method body instead."
            )
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "bot._impl", (
                    f"bot/engines/probability.py has a top-level "
                    f"`import bot._impl` at line {node.lineno}. Same "
                    f"circular-import failure mode as above."
                )


def test_probability_late_binding_observes_calibration_engine_mutation():
    """Behavioral regression: the late-binding pattern means _CALIBRATION_ENGINE
    mutations in bot._impl are observable inside ProbabilityEngine methods.
    If a future refactor caches the reference at module load (e.g. as a
    module-level alias), this test fails because the sentinel set after
    import would not be visible to the late-binding lookup."""
    import bot._impl as bot_impl
    import bot.engines.probability as bep

    saved = bot_impl._CALIBRATION_ENGINE
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
        bot_impl._CALIBRATION_ENGINE = _Sentinel()
        # counterfactual_prob's `_CALIBRATION_ENGINE is not None` branch
        # routes to the sentinel's calibrate.
        out = bep.ProbabilityEngine.counterfactual_prob(
            spot=100.0, threshold=99.0, seconds_remaining=600.0,
            alt_blended_rv=0.001, asset=None, product_type=None,
        )
        assert out == 0.42, (
            "Late-binding lookup did not see the mutated _CALIBRATION_ENGINE "
            "sentinel — counterfactual_prob returned the fallback _calibrate "
            "value instead of the sentinel.calibrate(...) value. The mutable-"
            "singleton freshness guarantee is broken."
        )
        assert any(
            isinstance(s, tuple) and s[0] == "calibrate"
            for s in sentinel_seen
        ), "Sentinel.calibrate was never invoked."
    finally:
        bot_impl._CALIBRATION_ENGINE = saved
