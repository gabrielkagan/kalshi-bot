"""Bit 3.2 — feature helpers extracted from bot/_impl.py to bot/helpers/.

Locks the contract between bot/_impl.py (which does
`from bot.helpers import *` after the constants star-import) and the
bot/helpers/ subpackage (the new home for moved helper functions).

Mirrors tests/test_constants_extraction.py (Bit 3.1).

L2 (Bit 3.0.5): tests call production directly. No reimplementing the
contract in test helpers.
"""
import ast
import importlib
from pathlib import Path

import pytest
import bot.constants  # noqa: F401

REPO_ROOT = Path(__file__).resolve().parents[1]

# Public (star-imported) helpers, by submodule.
PUBLIC_HELPERS = {
    "time_features": ["compute_time_regime_features"],
    "derived_features": ["compute_derived_features"],
    "tm_sweep": [
        "tm_sweep_extract_depths",
        "tm_sweep_counterfactual_pnl",
        "tm_compute_contracts",
    ],
    "sizing": ["buffer_sizing_multiplier", "get_min_edge"],
    "cell_blocks": [
        "should_block_high_price_stc_band",
        "should_block_high_price_stc_candidate",
        "should_block_tm98_highprice_bleed_candidate",
        "should_block_sol_taker_lowprice_bleed_candidate",
        "should_exclude_weather_no_ticker",
    ],
    "strings": [
        "dollars_str_to_cents",
        "cents_to_dollars_str",
        "fp_str_to_int",
        "int_to_fp_str",
    ],
    "strategy": ["evaluate_execution_strategy"],
}

# Underscore-prefixed (explicit re-export) helpers, by submodule.
# Star-import skips these → bot/_impl.py needs an explicit
# `from bot.helpers.<sub> import (...)` block (Bit 3.1 L3).
UNDERSCORE_HELPERS = {
    "validators": [
        "_validate_bleeders_against_runtime_registry",
        "_validate_high_price_stc_block_bleeder_strings",
        "_validate_bleed_block_bleeder_strings",
    ],
    "breakers": [
        "_extract_tick_error_location",
        "_kalshi_breaker_success",
        "_kalshi_series_key",
        "_kalshi_breaker",
        "_breaker_config",
    ],
}

ALL_SUBMODULES = list(PUBLIC_HELPERS) + list(UNDERSCORE_HELPERS)


# ─── 1. Helper files exist ──────────────────────────────────────────────────


def test_helpers_dir_exists():
    assert (REPO_ROOT / "bot" / "helpers").is_dir()


def test_helpers_init_exists():
    assert (REPO_ROOT / "bot" / "helpers" / "__init__.py").is_file()


def test_each_submodule_file_exists():
    for sub in ALL_SUBMODULES:
        path = REPO_ROOT / "bot" / "helpers" / f"{sub}.py"
        assert path.is_file(), f"missing bot/helpers/{sub}.py"


# ─── 2. Each submodule imports cleanly ──────────────────────────────────────


def test_each_submodule_imports():
    for sub in ALL_SUBMODULES:
        importlib.import_module(f"bot.helpers.{sub}")


def test_helpers_package_imports():
    importlib.import_module("bot.helpers")


# ─── 3. Public re-export contract (star-import path) ────────────────────────


def test_each_public_helper_in_bot_impl():
    """bot._impl.X is bot.helpers.<sub>.X for every public helper.

    External callers do `from bot import X` (proxy → bot._impl.X) →
    resolution depends on star-imported bindings in bot._impl's __dict__.
    """
    import bot._impl as b
    for sub, names in PUBLIC_HELPERS.items():
        mod = importlib.import_module(f"bot.helpers.{sub}")
        for name in names:
            assert hasattr(b, name), f"bot._impl missing {name}"
            assert hasattr(mod, name), f"bot.helpers.{sub} missing {name}"
            assert getattr(b, name) is getattr(mod, name), (
                f"identity mismatch: bot._impl.{name} vs bot.helpers.{sub}.{name}"
            )


def test_each_public_helper_resolves_via_canonical_module():
    """Every public helper resolves via its canonical bot.helpers.<sub> module.

    Bit 9.3-iii.b (2026-05-11): pre-retirement this checked `hasattr(bot, X)` via
    _BotProxy → bot._impl.X (resolved through `from bot.helpers import *`).
    Post-retirement the canonical home is bot.helpers.<sub> directly.
    """
    for sub, names in PUBLIC_HELPERS.items():
        mod = importlib.import_module(f"bot.helpers.{sub}")
        for name in names:
            assert hasattr(mod, name), f"bot.helpers.{sub} missing {name}"


# ─── 4. Underscore re-export contract (Bit 3.1 L3) ──────────────────────────


def test_each_underscore_helper_in_bot_impl():
    """`from bot.helpers import *` skips underscored names → explicit re-exports
    required so KalshiClient class-body decoration (`@_kalshi_breaker` etc.) and
    boot-time validator invocations resolve at module-import time.
    """
    import bot._impl as b
    for sub, names in UNDERSCORE_HELPERS.items():
        mod = importlib.import_module(f"bot.helpers.{sub}")
        for name in names:
            assert hasattr(b, name), (
                f"bot._impl missing {name}; explicit `from bot.helpers.{sub} "
                f"import {name}` required (star-import skips underscored names)"
            )
            assert hasattr(mod, name), f"bot.helpers.{sub} missing {name}"
            assert getattr(b, name) is getattr(mod, name), (
                f"identity mismatch: bot._impl.{name} vs bot.helpers.{sub}.{name}"
            )


def test_each_underscore_helper_resolves_via_canonical_module():
    """Underscore-prefixed helpers resolve via their canonical bot.helpers.<sub> module.

    Bit 9.3-iii.b (2026-05-11): pre-retirement `from bot import _kalshi_breaker`
    routed through _BotProxy → bot._impl._kalshi_breaker. Post-retirement
    callers reach `from bot.helpers.breakers import _kalshi_breaker` directly.
    """
    for sub, names in UNDERSCORE_HELPERS.items():
        mod = importlib.import_module(f"bot.helpers.{sub}")
        for name in names:
            assert hasattr(mod, name), (
                f"bot.helpers.{sub} missing {name}; canonical-home pin"
            )


# ─── 5. Moved helpers absent from bot/_impl.py module scope ─────────────────


def test_moved_helpers_not_defined_in_bot_impl():
    """Future drift guard: catches "I'll just add it back to _impl.py"."""
    bot_impl = REPO_ROOT / "bot" / "_impl.py"
    tree = ast.parse(bot_impl.read_text(), filename=str(bot_impl))
    module_level_funcdefs = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    all_moved = []
    for names in list(PUBLIC_HELPERS.values()) + list(UNDERSCORE_HELPERS.values()):
        all_moved.extend(names)
    leaked = sorted(set(module_level_funcdefs) & set(all_moved))
    assert leaked == [], (
        f"these helpers should be in bot/helpers/, not bot/_impl.py: {leaked}"
    )


# ─── 6. No numerical-library imports at module load ─────────────────────────


def test_no_forbidden_numerical_imports_in_helpers():
    """bot/helpers/*.py must not import numpy/scipy/torch/sklearn/pandas at
    module-load time. Preserves bot._thread_env-first invariant
    (bot/CLAUDE.md "Threading + numerical libraries"); helpers are loaded by
    `from bot.helpers import *` AFTER bot._thread_env, so a numerical import
    here is harmless for runtime ordering, but we forbid it as a discipline
    to keep the helpers pure-stdlib + bot.constants only.
    """
    forbidden = {"numpy", "scipy", "torch", "sklearn", "pandas"}
    helpers_dir = REPO_ROOT / "bot" / "helpers"
    for path in helpers_dir.glob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    assert top not in forbidden, (
                        f"bot/helpers/{path.name}: forbidden import {alias.name}"
                    )
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    top = node.module.split(".")[0]
                    assert top not in forbidden, (
                        f"bot/helpers/{path.name}: forbidden from-import {node.module}"
                    )


# ─── 7. No circular bot._impl imports ───────────────────────────────────────


def test_no_circular_bot_impl_imports_in_helpers():
    """bot/helpers/*.py must not `from bot._impl import ...` or `import bot._impl`
    — would create a runtime cycle (bot._impl does `from bot.helpers import *`).
    """
    helpers_dir = REPO_ROOT / "bot" / "helpers"
    for path in helpers_dir.glob("*.py"):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module != "bot._impl", (
                    f"bot/helpers/{path.name}: forbidden `from bot._impl import "
                    f"...` would create cycle"
                )
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name != "bot._impl", (
                        f"bot/helpers/{path.name}: forbidden `import bot._impl` "
                        f"would create cycle"
                    )


# ─── 8. Boot-time validator runtime bindings live in bot/boot.py post-9.3-iii.a ─


def test_hpsb_validator_binding_in_bot_boot():
    """Post-Bit-9.3-iii.a (2026-05-11): the boot-time invocation
    `_HPSB_MISSING_BLEEDERS = _validate_high_price_stc_block_bleeder_strings()`
    lives in clean-leaf bot/boot.py, not bot/_impl.py. bot/_impl.py re-exports
    via `from bot.boot import (...)` to preserve the proxy chain."""
    src = (REPO_ROOT / "bot" / "boot.py").read_text()
    assert (
        "_HPSB_MISSING_BLEEDERS = _validate_high_price_stc_block_bleeder_strings()"
        in src
    )


def test_bleed_block_validator_binding_in_bot_boot():
    src = (REPO_ROOT / "bot" / "boot.py").read_text()
    assert (
        "_BLEED_BLOCK_MISSING_BLEEDERS = _validate_bleed_block_bleeder_strings()"
        in src
    )


def test_validators_pass_at_runtime():
    """Boot-time validators return [] on the live runtime registries — proves
    the validators-import-then-invoke chain still works post-extraction.
    """
    import bot._impl as b
    assert b._HPSB_MISSING_BLEEDERS == [], (
        f"HPSB validator failed: {b._HPSB_MISSING_BLEEDERS}"
    )
    assert b._BLEED_BLOCK_MISSING_BLEEDERS == [], (
        f"Bleed-block validator failed: {b._BLEED_BLOCK_MISSING_BLEEDERS}"
    )


# ─── 9. KalshiClient class-body decoration resolves ─────────────────────────


def test_kalshi_client_imports():
    """KalshiClient class body uses @_kalshi_breaker / @_breaker_config at
    class-definition time. If the explicit underscore re-exports for breakers
    are missing or mis-ordered (after star-import but before class def at
    line ~1313), the import would raise NameError at class-body execution.
    """
    from bot._impl import KalshiClient
    assert KalshiClient is not None


def test_decorated_methods_callable():
    """get_balance is decorated with @_kalshi_breaker @_breaker_config(...).
    Decorator chain produces a callable wrapper.
    """
    from bot._impl import KalshiClient
    assert callable(KalshiClient.get_balance)
    assert callable(KalshiClient.get_orderbook)


# ─── 10. __init__.py star-imports each public submodule ─────────────────────


def test_init_star_imports_each_public_submodule():
    """bot/helpers/__init__.py must `from bot.helpers.<sub> import *` for
    each public submodule so `from bot.helpers import *` in bot/_impl.py
    pulls every public helper name in.
    """
    src = (REPO_ROOT / "bot" / "helpers" / "__init__.py").read_text()
    for sub in PUBLIC_HELPERS:
        assert f"from bot.helpers.{sub} import *" in src, (
            f"bot/helpers/__init__.py missing star-import of {sub}"
        )


# ─── 11. bot/_impl.py imports the helpers package ───────────────────────────


def test_bot_impl_star_imports_helpers():
    """bot/_impl.py must contain `from bot.helpers import *` after the
    constants star-import (Bit 3.1) and BEFORE the KalshiClient class
    definition (so class-body decorators resolve).
    """
    src = (REPO_ROOT / "bot" / "_impl.py").read_text()
    assert "from bot.helpers import *" in src


def test_bot_impl_explicit_underscore_reexport_validators():
    """bot/_impl.py must explicitly re-export underscore-prefixed validators
    via `from bot.helpers.validators import (...)` (star-import skips them).
    """
    src = (REPO_ROOT / "bot" / "_impl.py").read_text()
    assert "from bot.helpers.validators import" in src
    for name in UNDERSCORE_HELPERS["validators"]:
        assert name in src, f"explicit re-export of {name} missing from bot/_impl.py"


def test_bot_impl_explicit_underscore_reexport_breakers():
    src = (REPO_ROOT / "bot" / "_impl.py").read_text()
    assert "from bot.helpers.breakers import" in src
    for name in UNDERSCORE_HELPERS["breakers"]:
        assert name in src, f"explicit re-export of {name} missing from bot/_impl.py"


# ─── 12. Smoke: every helper actually executes ──────────────────────────────


def test_compute_time_regime_features_smoke():
    from bot.helpers.time_features import compute_time_regime_features
    out = compute_time_regime_features("2026-05-08T12:00:00Z")
    assert "hour_of_day_utc" in out
    assert out["hour_of_day_utc"] == 12


def test_compute_derived_features_smoke():
    from bot.helpers.derived_features import compute_derived_features
    out = compute_derived_features(
        spot_price=100.0, threshold=99.0, volatility=0.01,
        seconds_to_close=300, calibrated_prob=0.6,
        market_price_cents=55, kelly_contracts=10,
    )
    assert out["prob_breakeven_gap"] == pytest.approx(0.05)


def test_get_min_edge_smoke():
    from bot.helpers.sizing import get_min_edge
    assert get_min_edge(80) > 0


def test_dollars_str_to_cents_smoke():
    from bot.helpers.strings import dollars_str_to_cents
    assert dollars_str_to_cents("0.88") == 88
    assert dollars_str_to_cents(None) == 0


def test_kalshi_series_key_smoke():
    from bot.helpers.breakers import _kalshi_series_key
    assert _kalshi_series_key("KXBTC15M-26APR250000-00", "orderbook").startswith("kalshi_orderbook_KXBTC")


def test_extract_tick_error_location_smoke():
    from bot.helpers.breakers import _extract_tick_error_location
    try:
        raise ValueError("test")
    except ValueError as e:
        loc = _extract_tick_error_location(e)
    assert "test_helpers_extraction.py" in loc
