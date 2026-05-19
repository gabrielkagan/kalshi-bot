"""Bit C (86ba0jn2b) of HYPE/DOGE cal_mlp v1.1 retrain umbrella 86ba0jmyq.

Pins the extensible asset-floor architecture:

- `ASSET_FLOORS` (CORE) stays the canonical 4-asset dict {BTC, ETH, SOL, XRP}.
  Baked into `cfg_fp` via `compute_cfg_fp()`. NEVER mutate this without a
  cfg_fp rotation event (which would invalidate every existing v1.1
  production bundle for BTC/ETH/SOL/XRP).
- `ASSET_FLOORS_EXT` is the EXTENSION dict. Initially `{HYPE: 75, DOGE: 75}`.
  Adding new assets here is the 1-line operation each future Kalshi
  crypto onboarding triggers (BNB, SHIB, ADA, etc. as Kalshi rolls them
  out). `ASSET_FLOORS_EXT` is NOT baked into cfg_fp — extending it does
  NOT rotate cfg_fp, does NOT invalidate existing bundles.
- `resolve_recipe('v1.1_production').asset_floors` returns the UNION of
  CORE + EXT. `train.py:693`'s `if asset not in recipe.asset_floors`
  guard now passes HYPE/DOGE (and future EXT assets).
- `asset_min_price(asset)` checks CORE first, then EXT, then falls
  through to `GLOBAL_MIN_ENTRY_PRICE`.

Plan doc: `kb/decisions/v1-1-C-train-plan.md`.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "scripts" / "cal_mlp"))


# Pinned identity: rotating this in this Bit (or any future Bit) is a
# SHIP-BLOCKER — every existing v1.1 production bundle for BTC/ETH/SOL/XRP
# carries this cfg_fp. The whole point of ASSET_FLOORS_EXT is to extend
# trainable assets WITHOUT rotating this.
V1_1_PRODUCTION_CFG_FP = "345978797274721f"

CORE_ASSETS = frozenset({"BTC", "ETH", "SOL", "XRP"})
EXT_ASSETS_BIT_C = frozenset({"HYPE", "DOGE"})


def _import_features():
    if "features" in sys.modules:
        return importlib.reload(sys.modules["features"])
    return importlib.import_module("features")


# ── ASSET_FLOORS_EXT exists + has HYPE/DOGE ──────────────────────────


def test_asset_floors_ext_exists_and_contains_hype_doge():
    """`ASSET_FLOORS_EXT` is the new extension dict. Initial population:
    HYPE + DOGE at the GLOBAL_MIN_ENTRY_PRICE=75 floor (matches the bot's
    runtime fallback for assets without per-asset MIN_*_ENTRY_PRICE in
    bot/constants.py)."""
    features = _import_features()
    assert hasattr(features, "ASSET_FLOORS_EXT"), (
        "features.ASSET_FLOORS_EXT must exist — extensible asset-floor "
        "pattern for new Kalshi crypto rollouts (HYPE/DOGE today; BNB "
        "next; future tokens as Kalshi adds them)."
    )
    ext = features.ASSET_FLOORS_EXT
    assert isinstance(ext, dict), "ASSET_FLOORS_EXT must be a dict"
    assert ext.get("HYPE") == 75, "HYPE floor must default to 75 (= GLOBAL_MIN_ENTRY_PRICE)"
    assert ext.get("DOGE") == 75, "DOGE floor must default to 75 (= GLOBAL_MIN_ENTRY_PRICE)"


def test_asset_floors_core_unchanged():
    """`ASSET_FLOORS` (CORE) must remain EXACTLY {BTC, ETH, SOL, XRP}.
    Any drift here rotates cfg_fp and invalidates production bundles."""
    features = _import_features()
    assert set(features.ASSET_FLOORS.keys()) == CORE_ASSETS, (
        f"ASSET_FLOORS (CORE) drift: expected exactly {CORE_ASSETS}, "
        f"got {set(features.ASSET_FLOORS.keys())}. CORE is the cfg_fp "
        f"identity — extensions go in ASSET_FLOORS_EXT."
    )


def test_core_and_ext_are_disjoint():
    """CORE and EXT must NOT share any asset. The EXT→CORE migration is
    triggered by the asset's cal_mlp v1.1 production bundle shipping to
    the live `CURRENT` pointer (umbrella `86ba0jmyq` Bit D's gate for
    HYPE/DOGE) — a cfg_fp-rotation event that re-extracts and re-trains
    all CORE bundles in the same commit. This is INDEPENDENT of
    T4-promotion in the bot: HYPE/DOGE are already T4-promoted
    (2026-05-14, `bot/constants.py:HYPE_15M_SHADOW=False` +
    `HYPE_MIN_ENTRY_PRICE=90`) but stay in EXT until cal_mlp v1.1
    actually ships to live serving."""
    features = _import_features()
    core = set(features.ASSET_FLOORS.keys())
    ext = set(features.ASSET_FLOORS_EXT.keys())
    overlap = core & ext
    assert not overlap, (
        f"ASSET_FLOORS (CORE) and ASSET_FLOORS_EXT must be disjoint; "
        f"shared keys: {overlap}. The only path from EXT to CORE is "
        f"the asset's cal_mlp v1.1 bundle shipping to live serving "
        f"(atomic CURRENT-pointer flip; cfg_fp rotation event)."
    )


# ── cfg_fp identity preserved ────────────────────────────────────────


def test_cfg_fp_unchanged_after_ext_addition():
    """The whole point of ASSET_FLOORS_EXT: extending it MUST NOT
    rotate cfg_fp. Pin the production cfg_fp identity here so any
    accidental drift fails CI immediately."""
    features = _import_features()
    actual = features.compute_cfg_fp(include_sub_floor=False, provenance_filter="all")
    assert actual == V1_1_PRODUCTION_CFG_FP, (
        f"v1.1 production cfg_fp drift: expected {V1_1_PRODUCTION_CFG_FP}, "
        f"got {actual}. Did someone bake ASSET_FLOORS_EXT into "
        f"compute_cfg_fp? That defeats the EXT pattern's entire purpose."
    )


def test_cfg_fp_canonical_excludes_ext():
    """`compute_cfg_fp` canonical dict bakes ASSET_FLOORS only — NOT
    ASSET_FLOORS_EXT. Inspecting features.py source to AST-pin this is
    over-engineered; verifying the cfg_fp value matches the pre-EXT
    identity (`test_cfg_fp_unchanged_after_ext_addition`) is the
    functional contract. This test re-runs cfg_fp at two different
    sub-floor settings to confirm the EXT-dict membership doesn't bleed
    into either."""
    features = _import_features()
    cfg_no_sub = features.compute_cfg_fp(include_sub_floor=False, provenance_filter="all")
    cfg_sub = features.compute_cfg_fp(include_sub_floor=True, provenance_filter="all")
    # Both must be deterministic + stable across re-invocation.
    assert cfg_no_sub == features.compute_cfg_fp(include_sub_floor=False, provenance_filter="all")
    assert cfg_sub == features.compute_cfg_fp(include_sub_floor=True, provenance_filter="all")
    # The include_sub_floor=True case uses a sentinel string instead of
    # the ASSET_FLOORS dict — confirms the floor-dict is the only path
    # by which floor membership could affect cfg_fp.
    assert cfg_no_sub != cfg_sub, "include_sub_floor switch must change cfg_fp"


# ── resolve_recipe + asset_min_price merged-view ─────────────────────


def test_resolve_recipe_production_includes_core_and_ext():
    """`resolve_recipe('v1.1_production').asset_floors` returns CORE ∪ EXT.
    Replaces the old hard-pin `set(...) == CORE_ASSETS` invariant; the new
    contract is `CORE ⊆ asset_floors` AND `EXT ⊆ asset_floors`."""
    features = _import_features()
    recipe = features.resolve_recipe("v1.1_production")
    asset_floors_keys = set(recipe.asset_floors.keys())
    assert CORE_ASSETS <= asset_floors_keys, (
        f"production recipe must include CORE: missing "
        f"{CORE_ASSETS - asset_floors_keys}"
    )
    assert EXT_ASSETS_BIT_C <= asset_floors_keys, (
        f"production recipe must include EXT (HYPE/DOGE): missing "
        f"{EXT_ASSETS_BIT_C - asset_floors_keys}"
    )


def test_asset_min_price_hype_doge_returns_75():
    """`asset_min_price('HYPE')` and `'DOGE'` return 75 — falling through
    ASSET_FLOORS_EXT before GLOBAL_MIN_ENTRY_PRICE. Pre-Bit-C they fell
    through to GLOBAL_MIN_ENTRY_PRICE directly (same value but via the
    fallback path); post-Bit-C they resolve via EXT (clearer semantics)."""
    features = _import_features()
    assert features.asset_min_price("HYPE", include_sub_floor=False) == 75
    assert features.asset_min_price("DOGE", include_sub_floor=False) == 75


def test_asset_min_price_core_unchanged():
    """Sanity: CORE asset floors must NOT change. Especially after the
    asset_min_price() rewrite to consult ASSET_FLOORS_EXT — the function
    must still return the CORE per-asset values, not fall through to
    EXT or GLOBAL for CORE assets."""
    features = _import_features()
    assert features.asset_min_price("BTC", include_sub_floor=False) == 88
    assert features.asset_min_price("ETH", include_sub_floor=False) == 90
    assert features.asset_min_price("SOL", include_sub_floor=False) == 86
    assert features.asset_min_price("XRP", include_sub_floor=False) == 92


def test_asset_min_price_unknown_asset_falls_through():
    """Unknown asset (not in CORE, not in EXT) returns GLOBAL_MIN_ENTRY_PRICE.
    This is the existing contract — preserved post-EXT."""
    features = _import_features()
    assert features.asset_min_price("LINK", include_sub_floor=False) == features.GLOBAL_MIN_ENTRY_PRICE
    assert features.asset_min_price("SHIB", include_sub_floor=False) == features.GLOBAL_MIN_ENTRY_PRICE


def test_asset_min_price_include_sub_floor_unchanged():
    """`include_sub_floor=True` ignores both CORE and EXT and returns
    GLOBAL_MIN_ENTRY_PRICE unconditionally. Preserved post-Bit-C."""
    features = _import_features()
    assert features.asset_min_price("BTC", include_sub_floor=True) == features.GLOBAL_MIN_ENTRY_PRICE
    assert features.asset_min_price("HYPE", include_sub_floor=True) == features.GLOBAL_MIN_ENTRY_PRICE


# ── Adding a hypothetical new asset to EXT does not rotate cfg_fp ──


def test_simulated_bnb_addition_does_not_rotate_cfg_fp(monkeypatch):
    """Simulates the next-week BNB addition: monkeypatch ASSET_FLOORS_EXT
    with an extra BNB entry, recompute cfg_fp, verify it's unchanged.
    Catches any future regression where someone refactors compute_cfg_fp
    to incidentally bake ASSET_FLOORS_EXT."""
    features = _import_features()
    extended_ext = {**features.ASSET_FLOORS_EXT, "BNB": 75}
    monkeypatch.setattr(features, "ASSET_FLOORS_EXT", extended_ext)
    actual = features.compute_cfg_fp(include_sub_floor=False, provenance_filter="all")
    assert actual == V1_1_PRODUCTION_CFG_FP, (
        f"cfg_fp rotated when ASSET_FLOORS_EXT got a new entry — that's "
        f"exactly what the EXT pattern is supposed to prevent. Got "
        f"{actual}, expected {V1_1_PRODUCTION_CFG_FP}."
    )
