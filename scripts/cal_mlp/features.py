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
# the 30+ outlier tail. Applied in extract_data.build_feature_frame BEFORE
# deriving abs_spot_distance_to_strike_sigma and time_decayed_proximity, so
# all three features see the clipped value. cfg_fp captures this constant.
SIGMA_WINSOR_ABS_CAP = 25.0


def apply_sigma_winsor(sd):
    """Clip a single spot_distance_to_strike_sigma value to ±SIGMA_WINSOR_ABS_CAP.

    Centralized helper so all three sites that touch sigma at serve/extract
    time apply IDENTICAL clipping. Without this, train (extract) clipped
    while serve (post_hoc_processor + should_block_tm96) read raw values
    from DB → train/serve skew, model trained on ±25 saw ±3,337 in prod.

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
    runbook (`kb/decisions/v2-cal-mlp-deploy-runbook-may03.md`)."""
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
    """Per-asset entry floor; opt-in to global floor via --include-sub-floor."""
    if include_sub_floor:
        return GLOBAL_MIN_ENTRY_PRICE
    return ASSET_FLOORS.get(asset, GLOBAL_MIN_ENTRY_PRICE)


# ---------------------------------------------------------------------------
# Bleed cell — locked across Phase 2/3/4/5/6
# ---------------------------------------------------------------------------

# np.digitize with right=True puts boundary values in the LOWER bin.
# So price_tier 3 = market_price ∈ [96, 100], stc_bucket 2 = stc ∈ (300, 600].
BLEED_CELL = (3, 2)


def is_bleed_cell(price_tier: int, stc_bucket: int) -> bool:
    return (price_tier, stc_bucket) == BLEED_CELL
