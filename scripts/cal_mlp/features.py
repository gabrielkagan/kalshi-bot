"""Constants module for cal_mlp pipeline.

Single source of truth for feature schema, bucketization, asset floors,
clipping, and DROP_PREDICATES order. NO I/O, NO torch, NO pandas — only
primitive constants and pure helpers.

Phase 2/3/4/5/6/7 all import from here. Phase 7 startup parity-asserts
ASSET_FLOORS against bot.py:219-225.
"""
from __future__ import annotations

import hashlib
import json
from typing import NamedTuple


# ---------------------------------------------------------------------------
# Bucketization
# ---------------------------------------------------------------------------

PRICE_BIN_CUTOFFS = [80, 90, 96]   # → 4 tiers (0..3); right=True
STC_BIN_CUTOFFS = [120, 300, 600]  # → 4 buckets (0..3); right=True

DIGITIZE_RIGHT = True              # bleed cell convention: (price_tier=3, stc_bucket=2)


# ---------------------------------------------------------------------------
# Per-asset entry-floor (mirrors bot.py:219-225)
# ---------------------------------------------------------------------------

ASSET_FLOORS = {
    'BTC': 88,
    'ETH': 90,
    'SOL': 86,
    'XRP': 92,
}
GLOBAL_MIN_ENTRY_PRICE = 75   # bot.py:219 — sub-floor opt-in via --include-sub-floor

# Bit C (86ba0jn2b, 2026-05-19) — EXTENSION list for assets that the
# bot scans live but DO NOT YET have a SHIPPED cal_mlp v1.1 production
# bundle on the live `CURRENT` pointer. T4-promotion in the BOT (live
# trading via `raw_prob × MARKET_BLEND_W`) is INDEPENDENT of cal_mlp
# v1.1 bundle promotion (which is gated by the umbrella's Bit D
# Brier-head-to-head decision). HYPE/DOGE are already T4-promoted in
# the bot (2026-05-14, `bot/constants.py:HYPE_15M_SHADOW=False` +
# `HYPE_MIN_ENTRY_PRICE=90` / `DOGE_MIN_ENTRY_PRICE=85`) but have NO
# live cal_mlp v1.1 bundle — they bypass cal_mlp at serve time per
# CLAUDE.md ("cal_mlp training arc retired"). The EXT/CORE split is
# the cal_mlp PIPELINE's view of the asset, NOT the bot's T4 state.
#
# CRITICAL: ASSET_FLOORS_EXT is INTENTIONALLY OMITTED from
# `compute_cfg_fp()`'s canonical dict — that's the entire point of the
# EXT/CORE split. Adding a new asset here is a 1-line edit that does
# NOT rotate cfg_fp_production (=345978797274721f) and does NOT
# invalidate the 4 existing BTC/ETH/SOL/XRP production bundles.
#
# Lifecycle:
#   - New Kalshi crypto rollout (Coinbase has the spot feed; bot
#     scans the asset's KX*15M markets) → add to ASSET_FLOORS_EXT at
#     the cal_mlp extract floor for that asset (defaults to
#     GLOBAL_MIN_ENTRY_PRICE=75 for thin-data assets; tighter values
#     can be chosen per-asset if training-data abundance allows).
#   - When the asset's cal_mlp v1.1 bundle ships to production (Bit D
#     gate flip — `CURRENT` pointer at `models/cal_mlp_<ASSET>/`
#     points at a v1.1_production-recipe bundle that the bot now
#     consumes at serve time), MOVE the entry from ASSET_FLOORS_EXT to
#     ASSET_FLOORS in the SAME commit (this IS the cfg_fp-rotation
#     event for production; all 4+ production bundles need to be
#     re-extracted+re-trained against the new identity).
#   - `test_asset_floors_ext_extensibility.test_core_and_ext_are_disjoint`
#     pins the no-overlap invariant.
#
# Initial Bit-C population: HYPE + DOGE (T1-onboarded 2026-05-10;
# T4-promoted in bot 2026-05-14 via raw_prob direct-promote; cal_mlp
# v1.1 bundles being trained now in this Bit, awaiting Bit D's
# Brier-head-to-head gate before CURRENT-pointer flip).
# BNB: T1 shadow 2026-05-17 → T4 live 2026-05-19 via P2.4 (86b9zmj37,
# sibling to P2.3). BNB does NOT have a cal_mlp v1.1 bundle yet —
# T4 promotion went via raw_prob + MARKET_BLEND_W_BY_ASSET["BNB"]=0.20
# direct-promote (same precedent as HYPE/DOGE). If/when a BNB cal_mlp
# v1.1 bundle is added, this floor-list applies. Future additions
# (no specific commit): SHIB/ADA/etc. as Kalshi rolls out new markets.
#
# Floor-value note: the EXT floor (75 here) is the cal_mlp EXTRACT
# floor (rows below this are dropped pre-training). It is DELIBERATELY
# DIFFERENT from the bot's RUNTIME serving floor (`bot/constants.py`
# `HYPE_MIN_ENTRY_PRICE=90` / `DOGE_MIN_ENTRY_PRICE=85`) — the cal_mlp
# pipeline extracts at the permissive floor to maximize thin-T1
# training data; the bot never invokes cal_mlp at serve time on prices
# below its runtime floor, so the 75-89c HYPE training rows just give
# the model a broader empirical view without affecting serve-time
# inputs. If a future Bit decides train/serve floor alignment is the
# right call, raising EXT to match bot/constants.py is a 1-line edit.
ASSET_FLOORS_EXT = {
    'HYPE': 75,
    'DOGE': 75,
}

# P2.1.a-3 (2026-05-13, ticket 86b9wuhhr) — HYPE/DOGE T1 shadow assets in
# the REPLAY recipe namespace. Kept SEPARATE so the replay-corpus
# pipeline (`extract_data_replay.py` + `compute_cfg_fp_replay()`) has
# its own asset-floor universe (cfg_fp_replay=9347942aaba71146). Adding
# a new asset to ASSET_FLOORS_REPLAY rotates cfg_fp_replay; the EXT
# pattern above is the production-recipe equivalent that explicitly
# does NOT rotate cfg_fp.
ASSET_FLOORS_REPLAY = {
    'HYPE': 75,
    'DOGE': 75,
    # Bit F (2026-05-21, ticket 86ba1wpck) — BNB added to replay recipe.
    # Rotates cfg_fp_replay from `9347942aaba71146` → new fingerprint;
    # pin captured in tests/contracts/test_p2_1_a_3_corpus_snapshots.py.
    'BNB': 75,
}


# ---------------------------------------------------------------------------
# Settlement whitelist (bot.py:4351 _OK_RESULTS)
# ---------------------------------------------------------------------------

SETTLEMENT_WHITELIST = ('yes', 'all_yes', 'no', 'all_no')
SETTLEMENT_YES_VALUES = ('yes', 'all_yes')


# ---------------------------------------------------------------------------
# Raw-prob clipping for the skip-term (R-p2-impl-2#C1)
# ---------------------------------------------------------------------------

RAW_PROB_CLIP_EPS = 1e-6
# logit(eps) ≈ -13.8156, logit(1-eps) ≈ +13.8156


# ---------------------------------------------------------------------------
# Sigma winsorize cap (R-p7-deploy-r11)
# ---------------------------------------------------------------------------
# At terminal STC (seconds_to_close → 0) the spot_distance_to_strike_sigma
# denominator (volatility × sqrt(STC/5) × 100) collapses, producing pseudo-
# infinite z-scores up to ±3,337 in production data. Without winsorization,
# z-scoring across the column inflates std by 100×+ and collapses real signal.
#
# Cap chosen at 25 — above the empirical max benign value (~19) but well below
# the 30+ outlier tail. Applied at every serve/extract surface BEFORE deriving
# abs_spot_distance_to_strike_sigma and time_decayed_proximity, so all three
# features see the clipped value. cfg_fp captures this constant.
#
# Application sites (lock-step, post-A.1b 2026-05-12):
#   extract_data.build_feature_frame  (train, DataFrame)
#   post_hoc_processor._process_row    (serve, scalar)
#   integration.should_block_tm96      (serve sync gate, on
#                                       compute_derived_features return)
#   bot/state.py:1723                  (pre-DB-write, on
#                                       compute_derived_features return)
SIGMA_WINSOR_ABS_CAP = 25.0


def apply_sigma_winsor(sd):
    """Clip a single spot_distance_to_strike_sigma value to ±SIGMA_WINSOR_ABS_CAP.

    Centralized helper so all four sites that touch sigma at serve/extract
    time apply IDENTICAL clipping (extract_data + post_hoc_processor +
    should_block_tm96 + bot/state.py:1723 pre-DB-write). Without this,
    train (extract) clipped while serve read raw values from DB →
    train/serve skew, model trained on ±25 saw ±3,337 in prod.

    Returns:
        - None if input is None (NULL passthrough for missing-indicator path)
        - clipped value otherwise

    NaN-safe: NaN compared with `>` returns False, so NaN passes through
    unchanged (downstream NULL-imputation handles it).

    Use the module-level lookup `features.SIGMA_WINSOR_ABS_CAP` so test
    monkey-patches (and any future hot-reload) are honored at call time
    rather than baked-in via `from features import` at the call site.
    """
    if sd is None:
        return None
    cap = SIGMA_WINSOR_ABS_CAP
    if sd > cap:
        return cap
    if sd < -cap:
        return -cap
    return sd


# ---------------------------------------------------------------------------
# Hour-of-day cyclic encoding (Sprint A Bit 1b — 86b9veppa)
# ---------------------------------------------------------------------------
# Centralized helper for the `hour_sin = sin(2π·h/24)` / `hour_cos = cos(2π·h/24)`
# encoding used by both train-extract paths (extract_data.py + sim_pnl.py
# DataFrame-side) and serve-extract paths (post_hoc_processor.py +
# integration.py scalar). Lock-step with
# `bot.helpers.derived_features.compute_hour_sin_cos` (scalar-only) — both
# must produce identical numbers for the same `hour` so train and serve
# distributions match exactly.
#
# numpy is imported INSIDE the function body so this module's top-level
# import surface stays stdlib-only (per the module docstring: "NO I/O, NO
# torch, NO pandas — only primitive constants and pure helpers"). DataFrame
# callers already have numpy in scope, so the deferred import is free.

def compute_hour_features(hour):
    """Cyclic 24h hour-of-day encoding. Returns (sin, cos).

    Accepts:
      - `None` → `(None, None)` (NULL passthrough, mirrors canonical helper).
      - Scalar int/float → tuple of two floats.
      - numpy/pandas Series of hours → tuple of two numpy arrays.

    Caller is responsible for any `% 24` modulo on the input — this helper
    does NOT modulo internally, mirroring
    `bot.helpers.derived_features.compute_hour_sin_cos` which assumes
    inputs are already in [0, 24).

    Lock-step: the scalar branch (including the `None` passthrough) is
    byte-identical to `bot.helpers.derived_features.compute_hour_sin_cos`
    (math.sin/cos with `angle = 2π·h/24`). The vector branch uses numpy
    with the same formula for DataFrame-side extract paths.
    """
    if hour is None:
        return (None, None)
    import math
    import numpy as np
    arr = np.asarray(hour)
    if arr.ndim == 0:
        h = float(arr)
        angle = 2.0 * math.pi * h / 24.0
        return (math.sin(angle), math.cos(angle))
    h = arr.astype(float)
    angle = 2.0 * np.pi * h / 24.0
    return (np.sin(angle), np.cos(angle))


# ---------------------------------------------------------------------------
# Continuous feature set — z-scored after per-column transform unless
# transform == 'identity_no_zscore'.
# ---------------------------------------------------------------------------

# R-p7-deploy-r6 v1 REDUCED FEATURE SET (8 features):
# The full feature set requires 11 columns (yes_spread_cents, spot_momentum_*,
# btc_spot_change_5m_bps, btc_realized_vol_15m, window_*, minutes_above_strike,
# spot_coinbase_kraken_gap_bps, kalshi_flow_depth_velocity) that were only
# added to bot.py Apr 19/23. Plus z_score is 31% NULL even on candidate
# stages. v1 ships now with the 8 features that have ≥67 days of clean
# history; v2 retrains in ~30d once Apr-19-cohort accumulates 30d; v3 in
# ~150d (or ~60d at K=2) with the full set. See
# kb/decisions/p2-cal-mlp-v1v2v3-retraining-plan.md for the staging plan.
CONT_FEATURE_COLS = [
    'market_price',                            # log_cents_to_dollars → z
    'seconds_to_close',                        # identity → z
    'spot_distance_to_strike_sigma',           # identity → z
    'abs_spot_distance_to_strike_sigma',       # identity → z (symmetry prior)
    'time_decayed_proximity',                  # identity → z; engineered as
                                               # spot_distance × (1 - stc/900)
    'prob_breakeven_gap',                      # identity → z
    'hour_sin', 'hour_cos',                    # identity_no_zscore (analytical)
]

CONT_FEATURE_TRANSFORMS = {
    'market_price': 'log_cents_to_dollars',
    'hour_sin': 'identity_no_zscore',
    'hour_cos': 'identity_no_zscore',
    # all others: 'identity'
}

# v1 has NO missing-indicator columns — every feature in CONT_FEATURE_COLS
# is either always-present (market_price, seconds_to_close, prob_breakeven_gap)
# or analytical (hour_sin/cos) or near-always-present (~0.02% NULL on
# spot_distance_to_strike_sigma, which we filter out via Phase 2 contract).
# Reintroduced for v2 alongside the WS-fed momentum/realized-vol features.
MISSING_INDICATOR_COLS: list[str] = []

MISSING_INDICATOR_SOURCE_MAP: dict[str, str] = {}


N_CONT = len(CONT_FEATURE_COLS)


# ---------------------------------------------------------------------------
# DROP_PREDICATES — sequential exclusive bucketing (R-p2-spec-r4#C2)
# ---------------------------------------------------------------------------

# Each row failing the data-pull predicates buckets into the FIRST predicate
# it fails. Order is part of cfg_fp; reordering = schema_version bump.
DROP_PREDICATES_ORDER = [
    'non_15m_product_type',
    'sports_ticker',
    'null_market_price',
    'non_positive_market_price',
    'below_asset_floor',
    'null_raw_prob',
    'null_evaluation_time',
    'non_yes_no_result',
    'null_settled_time',
    'settled_after_cutoff',
    # R-p2-impl-r1#C7 + R2#C17: extended to catch null side/stc that would
    # silently corrupt outcome label or bucketization. Reordering = schema bump.
    'null_or_invalid_side',
    'null_seconds_to_close',
]


# ---------------------------------------------------------------------------
# Feature-schema fingerprint (cfg_fp)
# ---------------------------------------------------------------------------

PROVENANCE_FILTER_CHOICES = ('live_only', 'full_dataset', 'all')


def compute_cfg_fp(
    *,
    include_sub_floor: bool,
    provenance_filter: str = 'all',
) -> str:
    """sha256[:16] of the canonical extraction-policy JSON. Two extracts
    with the same cfg_fp produce the same parquet schema and the same
    bucketization. Phase 6 A/B refuses to compare bundles with different
    cfg_fp.

    `provenance_filter` is one of `PROVENANCE_FILTER_CHOICES` and bakes the
    SQL-side `data_provenance` filter into bundle identity — live_only
    vs full_dataset must produce distinct bundles per the v2 ablation
    runbook (`kb/decisions/v2-cal-mlp-deploy-runbook-may03.md`).

    Known cfg_fp values (Money Printer Roadmap Phase 2, P2.1.a-2 pin):

      178d14020bd21beb  v1 production (2026-04-28 CURRENT VPS bundles).
                        Trained at commit 7122693, BEFORE 'sigma_winsor_abs_cap'
                        was added to the canonical dict. Default flags
                        (include_sub_floor=False, provenance_filter='all').

      345978797274721f  v1.1 candidate (current HEAD, default flags). The
                        v1 → v1.1 delta is the single key
                        'sigma_winsor_abs_cap': SIGMA_WINSOR_ABS_CAP added
                        to the canonical dict by commit 7ad2464 ("4-site
                        sigma winsorize lock-step"). CONT_FEATURE_COLS was
                        UNCHANGED from 7122693 onward — all Wave 1
                        derivable features (hour_sin/cos,
                        prob_breakeven_gap, abs_spot_distance_to_strike_sigma,
                        time_decayed_proximity) were already in v1's recipe.

      1969b12c6c0c39bf  2026-05-03 unpromoted candidates on VPS. Same
                        feature recipe as 345978797274721f; differs only
                        by --include-sub-floor --provenance-filter=
                        full_dataset ablation flags per the v2 deploy
                        runbook. NOT a different feature recipe.

    These three hashes are pinned in tests/contracts/test_calmlp_lockstep.py
    as v1.1-retrain identity contracts (anchor 5). Updating any pin requires
    a sister update to that test file + the C0 ticket (ClickUp 86b9wuhhr)."""
    if provenance_filter not in PROVENANCE_FILTER_CHOICES:
        raise ValueError(
            f"provenance_filter must be one of {PROVENANCE_FILTER_CHOICES}; "
            f"got {provenance_filter!r}"
        )
    canonical = {
        'CONT_FEATURE_COLS': CONT_FEATURE_COLS,
        'CONT_FEATURE_TRANSFORMS': CONT_FEATURE_TRANSFORMS,
        'MISSING_INDICATOR_COLS': MISSING_INDICATOR_COLS,
        'PRICE_BIN_CUTOFFS': PRICE_BIN_CUTOFFS,
        'STC_BIN_CUTOFFS': STC_BIN_CUTOFFS,
        'digitize_right': DIGITIZE_RIGHT,
        'method_output_policy': 'raw_prob_only',
        'asset_floors': '__sub_floor_included__' if include_sub_floor else ASSET_FLOORS,
        'include_sub_floor': bool(include_sub_floor),
        'settlement_whitelist': list(SETTLEMENT_WHITELIST),
        'null_drop_threshold': 0.30,
        'null_imputation_policy': 'fold_train_mean_with_missing_indicator',
        'normstats_ddof': 1,
        'raw_prob_clip_eps': RAW_PROB_CLIP_EPS,
        'sigma_winsor_abs_cap': SIGMA_WINSOR_ABS_CAP,
        'drop_predicates_order': DROP_PREDICATES_ORDER,
        'loss_form': 'bce_w_calibration_residual_v1',
        'loss_w_floor': 1.0,
        'loss_w_multiplier': 4.0,
    }
    # Identity-preserving omission: when provenance_filter='all', the SQL
    # pull is identical to pre-change behavior (no WHERE clause for
    # data_provenance). Including the key in the canonical dict would
    # silently re-fingerprint every existing v1-reproduction call site
    # (e.g., `INCLUDE_SUB_FLOOR=0 run_pipeline.sh`). Only inject the key
    # when it actually changes the extracted dataset.
    if provenance_filter != 'all':
        canonical['provenance_filter'] = provenance_filter
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True).encode()
    ).hexdigest()[:16]


def asset_min_price(asset: str, *, include_sub_floor: bool) -> int:
    """Per-asset entry floor; opt-in to global floor via --include-sub-floor.

    Bit C (86ba0jn2b, 2026-05-19): falls through ASSET_FLOORS_EXT before
    GLOBAL_MIN_ENTRY_PRICE so HYPE/DOGE (and future extension-list assets)
    resolve via the EXT path explicitly rather than the
    silent-unknown-asset path. Functional outcome is unchanged for both
    CORE and pre-EXT-listed assets — same numeric values returned — but
    the lookup semantics are clearer:
        CORE asset  → ASSET_FLOORS[asset]
        EXT asset   → ASSET_FLOORS_EXT[asset]
        unknown     → GLOBAL_MIN_ENTRY_PRICE (back-compat).
    """
    if include_sub_floor:
        return GLOBAL_MIN_ENTRY_PRICE
    if asset in ASSET_FLOORS:
        return ASSET_FLOORS[asset]
    return ASSET_FLOORS_EXT.get(asset, GLOBAL_MIN_ENTRY_PRICE)


# ---------------------------------------------------------------------------
# Bleed cell — locked across Phase 2/3/4/5/6
# ---------------------------------------------------------------------------

# np.digitize with right=True puts boundary values in the LOWER bin.
# So price_tier 3 = market_price ∈ [96, 100], stc_bucket 2 = stc ∈ (300, 600].
BLEED_CELL = (3, 2)


def is_bleed_cell(price_tier: int, stc_bucket: int) -> bool:
    return (price_tier, stc_bucket) == BLEED_CELL


# ---------------------------------------------------------------------------
# Replay-corpus recipe (HYPE/DOGE/BNB — Phase 2 replay backfill; BNB added Bit F)
# ---------------------------------------------------------------------------
# P2.1.a-3 (2026-05-13, ticket 86b9wuhhr) — pull path for HYPE/DOGE
# `historical_replay_calmlp` rows. Bit F (2026-05-21, ticket 86ba1wpck) widened
# to include BNB. The replay corpus has 21 cols post-Bit-F (19 pre-fu2 +
# `threshold REAL` ticket `86b9xtam7` + `spot_staleness_seconds REAL` ticket
# `86ba1wpck`) vs
# the 32 REQUIRED_SOURCE_COLS in extract_data.py; most bot-state features
# (market_price, vol_regime, z_score, momentum/realized-vol, NBBO, balance,
# strategy, side) are honest-NULL on replay rows by design (see
# scripts/backfill/crypto_replay_backfill.py docstring "Methodology
# gotchas"). Two production-recipe features are 100% NULL in replay:
#   - market_price        (replay's `predict()` uses entry_price_cents=0
#                          sentinel; `market_price` not stored)
#   - prob_breakeven_gap  (no historical Kalshi orderbook → can't derive)
#
# REPLAY recipe is therefore a strict subset of the v1.1 recipe with 4
# CONT_FEATURE_COLS (vs 8 in production). Other features derive from replay's
# (spot_at_evaluation, sigma_at_evaluation, strike_cents, close_time,
# evaluation_time) tuple via the same formulas extract_data.py uses.
#
# cfg_fp_replay is namespaced separately from cfg_fp — bundles produced
# under this recipe are NOT comparable to v1.1 production bundles. Phase
# 6 A/B refuses cross-recipe comparison via cfg_fp inequality, so the
# namespace separation is enforced naturally.

CONT_FEATURE_COLS_REPLAY = [
    # Note: `seconds_to_close` and `time_decayed_proximity` are STRUCTURALLY
    # CONSTANT for replay rows (RCA 2026-05-13 P2.1.a-3 first extract run).
    # `replay_market(market, ...)` evaluates each market exactly once at
    # `open_time`, so `evaluation_time == open_time` always and `stc =
    # close_time - evaluation_time = 900s` for every 15M market. Including
    # them would zero-out normstats (`fit_normstats` raises on std<1e-12).
    # Excluded entirely from the replay recipe; production v1.1 keeps both.
    'spot_distance_to_strike_sigma',           # derived: (spot - strike) / sigma_term
    'abs_spot_distance_to_strike_sigma',       # derived: |sd|
    'hour_sin', 'hour_cos',                    # already-stored in replay corpus
                                                # (canonical-helper-derived at backfill;
                                                # we re-derive + verify lock-step here)
]

CONT_FEATURE_TRANSFORMS_REPLAY = {
    'hour_sin': 'identity_no_zscore',
    'hour_cos': 'identity_no_zscore',
    # all others: 'identity'
}

# Replay rows lack market_price (no floor predicate) and product_type
# (replay table is 15M-only by construction). Predicates are pruned
# accordingly; reordering/adding is a recipe change and bumps cfg_fp_replay.
DROP_PREDICATES_ORDER_REPLAY = [
    'null_evaluation_time',
    'null_close_time',
    'null_spot_at_evaluation',
    'null_sigma_at_evaluation',
    'null_strike_cents',
    'non_yes_no_result',
    'settled_after_cutoff',
    'non_positive_seconds_to_close',  # derived; defensive against close_time<=eval_time
]

# Phase 2 v1 backfill stamps every row with `replay_phase2_v1`. Future
# Phase 2.5 / Phase 3 corpora would extend the tuple here AND bump cfg_fp.
REPLAY_PROVENANCE_FILTER_CHOICES = ('replay_phase2_v1',)

# Single source of truth for the replay-recipe namespace label. Referenced
# by `compute_cfg_fp_replay()`'s canonical dict, by `extract_data_replay.py`
# when stamping bundles, and by `tests/contracts/test_p2_1_a_3_corpus_
# snapshots.py` anchor 9. Promote to a constant per R2 MN3 so all four
# sites point at one literal and recipe-namespace bumps (e.g., replay_v2)
# are a single-line edit.
REPLAY_RECIPE_NAMESPACE = 'replay_v1'


# Production-recipe namespace label. Sister to REPLAY_RECIPE_NAMESPACE.
# Pre-P2.1.a-3 production bundles do NOT stamp `recipe_namespace` in
# extract_bundle.json (the field was introduced for replay extracts only);
# `resolve_recipe(None)` and `resolve_recipe('v1.1_production')` therefore
# resolve identically for back-compat with already-shipped production
# bundles. Future production extracts SHOULD stamp the field explicitly.
RECIPE_NAMESPACE_V1_1_PRODUCTION = 'v1.1_production'


class RecipeSpec(NamedTuple):
    """Per-recipe routing quintet consumed by train.py / validate.py /
    conformal.py. `resolve_recipe(ns)` is the single dispatch entry — see
    `scripts/cal_mlp/features.py` module docstring + ClickUp 86b9xbd2u
    (P2.1.a-3-fu1) + 86b9xd9hn (P2.1.a-3-fu2) for the design.

    Fields:
      - namespace: canonical label ('v1.1_production' or 'replay_v1')
      - cont_feature_cols: per-recipe CONT_FEATURE_COLS (8 or 4)
      - cont_feature_transforms: per-col transform name dict
      - missing_indicator_cols: per-recipe MISSING_INDICATOR_COLS (both
        empty today; field kept for forward-compat with v2 retrain)
      - asset_floors: per-recipe asset-floor dict. Production namespace
        merges CORE (`ASSET_FLOORS` = {BTC, ETH, SOL, XRP}, baked into
        cfg_fp) + EXT (`ASSET_FLOORS_EXT` = {HYPE, DOGE, ...}, NOT in
        cfg_fp; extensible for future Kalshi crypto rollouts per Bit C
        86ba0jn2b 2026-05-19). Replay namespace = HYPE/DOGE/BNB
        (separate `ASSET_FLOORS_REPLAY`; BNB added Bit F `86ba1wpck`
        2026-05-21, rotated cfg_fp_replay 9347942aaba71146 →
        ea9c30477f844afa). Caller membership-tests
        `if asset not in recipe.asset_floors` to guard `--asset NAME`
        against `recipe_namespace=NS` mismatch.
      - categorical_feature_cols: per-recipe tuple of categorical column
        names the bundle's parquet ACTUALLY contains (subset of the
        4-tuple `(price_tier, stc_bucket, vol_regime_int, side_int)`
        consumed by `CalibrationMLP.forward`). For replay_v1, only
        `(stc_bucket, side_int)` are present — replay parquets lack
        `market_price` (so no `price_tier` digitization) and have no vol
        regime feed (so no `vol_regime_int`). Phase4Dataset defaults the
        absent categoricals to int64 zeros at construction. Added in
        P2.1.a-3-fu2 (86b9xd9hn) to close the KeyError gap fu1 surfaced
        without re-extracting replay bundles or bumping cfg_fp_replay.
    """
    namespace: str
    cont_feature_cols: tuple
    cont_feature_transforms: dict
    missing_indicator_cols: tuple
    asset_floors: dict
    categorical_feature_cols: tuple


def resolve_recipe(recipe_namespace):
    """Route a bundle's `recipe_namespace` field to its full RecipeSpec
    sextet — `namespace` / `cont_feature_cols` / `cont_feature_transforms`
    / `missing_indicator_cols` / `asset_floors` / `categorical_feature_cols`.

    Args:
        recipe_namespace: one of 'v1.1_production', 'replay_v1', or None.
            None resolves to production (back-compat for pre-P2.1.a-3
            bundles that don't stamp the field).

    Returns:
        RecipeSpec namedtuple. Tuples are intentionally returned rather
        than mutable lists so callers can't accidentally extend the
        recipe in place (which would silently drift cfg_fp pins).

    Raises:
        ValueError: unknown namespace. Silent fallback would let a
            typo'd bundle silently mis-train on the wrong feature set.
    """
    if recipe_namespace is None or recipe_namespace == RECIPE_NAMESPACE_V1_1_PRODUCTION:
        # Bit C (86ba0jn2b, 2026-05-19): asset_floors is the UNION of
        # ASSET_FLOORS (CORE, baked into cfg_fp) and ASSET_FLOORS_EXT
        # (EXTENSION, NOT in cfg_fp). The union is used ONLY by
        # `recipe.asset_floors` consumers (train.py:693 asset-membership
        # guard). `compute_cfg_fp()` continues to bake CORE only —
        # extending EXT does not rotate cfg_fp. Pinned by
        # `tests/contracts/test_asset_floors_ext_extensibility.py`.
        return RecipeSpec(
            namespace=RECIPE_NAMESPACE_V1_1_PRODUCTION,
            cont_feature_cols=tuple(CONT_FEATURE_COLS),
            cont_feature_transforms=dict(CONT_FEATURE_TRANSFORMS),
            missing_indicator_cols=tuple(MISSING_INDICATOR_COLS),
            asset_floors={**ASSET_FLOORS, **ASSET_FLOORS_EXT},
            # All four categoricals are present in production fold
            # parquets (extract_data.py digitizes market_price → price_tier
            # and stamps vol_regime_int from the vol-regime string).
            categorical_feature_cols=(
                'price_tier', 'stc_bucket', 'vol_regime_int', 'side_int',
            ),
        )
    if recipe_namespace == REPLAY_RECIPE_NAMESPACE:
        return RecipeSpec(
            namespace=REPLAY_RECIPE_NAMESPACE,
            cont_feature_cols=tuple(CONT_FEATURE_COLS_REPLAY),
            cont_feature_transforms=dict(CONT_FEATURE_TRANSFORMS_REPLAY),
            # Replay v1 has no missing-indicator cols (replay parquet
            # always has the 4 cont features present by construction);
            # field kept for forward-compat with future replay recipes.
            missing_indicator_cols=(),
            asset_floors=dict(ASSET_FLOORS_REPLAY),
            # P2.1.a-3-fu2 (86b9xd9hn) — replay parquets structurally
            # lack `price_tier` (no `market_price` → no PRICE_BIN_CUTOFFS
            # digitization) and `vol_regime_int` (no vol regime feed for
            # HYPE/DOGE replay). Phase4Dataset defaults both to int64
            # zeros so CalibrationMLP.forward's one-hots collapse to
            # constants ([1,0,0,0] and [1,0]) on those two axes.
            #
            # Truth-in-degeneracy note (R1 M3+M4): even the two listed
            # categoricals are effectively constant on replay rows:
            #   - `stc_bucket=3` always (replay's `replay_market(market)`
            #     evaluates at `open_time`, so `stc = close_time -
            #     evaluation_time = 900s` for every 15M market → bucket 3
            #     under STC_BIN_CUTOFFS=[120,300,600]).
            #   - `side_int=1` always (extract_data_replay.py:528
            #     hard-codes `np.int8(1)` per its "always YES side in
            #     replay" docstring — YES side per the production
            #     `extract_data.py:404` convention `side_int = (side ==
            #     'yes').astype(int8)`).
            # All four CalibrationMLP one-hots are therefore constant
            # vectors on replay data; only the ticker embedding (EMB_DIM=4)
            # provides non-degenerate categorical signal. Cont features +
            # ticker embedding are the entire trainable surface.
            # Re-extracting replay bundles to add proxy `price_tier` /
            # `vol_regime_int` would bump cfg_fp_replay (Option B in fu2's
            # design tree); deferred until Phase 5 Brier/ECE numbers show
            # the categorical collapse is load-bearing.
            categorical_feature_cols=('stc_bucket', 'side_int'),
        )
    raise ValueError(
        f"unknown recipe_namespace {recipe_namespace!r}; expected one of "
        f"({RECIPE_NAMESPACE_V1_1_PRODUCTION!r}, {REPLAY_RECIPE_NAMESPACE!r}). "
        f"A typo'd or future-recipe namespace would otherwise silently "
        f"fall through to production and corrupt training/validation."
    )


def compute_cfg_fp_replay(*, provenance_filter: str = 'replay_phase2_v1') -> str:
    """sha256[:16] of the canonical replay-recipe extraction policy. Distinct
    from `compute_cfg_fp` — bundles produced via this fingerprint live in a
    SEPARATE namespace from v1.1 production bundles (see module docstring).

    Pinned in tests/contracts/test_p2_1_a_3_corpus_snapshots.py anchor 7.
    Any change to CONT_FEATURE_COLS_REPLAY / transforms / drop predicates /
    sigma_winsor / raw_prob_clip / provenance_filter shifts this hash and
    trips the test. Updating the pin requires a sister test update +
    documentation in the v1.1-retrain session resume doc."""
    if provenance_filter not in REPLAY_PROVENANCE_FILTER_CHOICES:
        raise ValueError(
            f"provenance_filter must be one of {REPLAY_PROVENANCE_FILTER_CHOICES}; "
            f"got {provenance_filter!r}"
        )
    canonical = {
        # Recipe namespace marker — explicit guard against accidental hash
        # collision with `compute_cfg_fp()` over the same constants.
        'recipe_namespace': REPLAY_RECIPE_NAMESPACE,
        'CONT_FEATURE_COLS_REPLAY': CONT_FEATURE_COLS_REPLAY,
        'CONT_FEATURE_TRANSFORMS_REPLAY': CONT_FEATURE_TRANSFORMS_REPLAY,
        'PRICE_BIN_CUTOFFS': PRICE_BIN_CUTOFFS,
        'STC_BIN_CUTOFFS': STC_BIN_CUTOFFS,
        'digitize_right': DIGITIZE_RIGHT,
        'method_output_policy': 'raw_prob_only',
        'asset_floors_replay': ASSET_FLOORS_REPLAY,
        'settlement_whitelist': list(SETTLEMENT_WHITELIST),
        'null_drop_threshold': 0.30,
        'null_imputation_policy': 'fold_train_mean_with_missing_indicator',
        'normstats_ddof': 1,
        'raw_prob_clip_eps': RAW_PROB_CLIP_EPS,
        'sigma_winsor_abs_cap': SIGMA_WINSOR_ABS_CAP,
        'drop_predicates_order': DROP_PREDICATES_ORDER_REPLAY,
        'loss_form': 'bce_w_calibration_residual_v1',
        'loss_w_floor': 1.0,
        'loss_w_multiplier': 4.0,
        'provenance_filter': provenance_filter,
    }
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True).encode()
    ).hexdigest()[:16]
