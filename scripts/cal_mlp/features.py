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
# Continuous feature set — z-scored after per-column transform unless
# transform == 'identity_no_zscore'.
# ---------------------------------------------------------------------------

CONT_FEATURE_COLS = [
    'market_price',                            # log_cents_to_dollars → z
    'seconds_to_close',                        # identity → z
    'z_score',                                 # identity → z
    'yes_spread_cents',                        # identity → z
    'spot_momentum_60s_bps',                   # identity → z
    'spot_momentum_5m_bps',                    # identity → z
    'spot_realized_range_15m_bps',             # log1p_signed → z
    'btc_spot_change_5m_bps',                  # identity → z
    'btc_realized_vol_15m',                    # log1p → z
    'window_max_buf_pct',                      # identity → z
    'window_min_buf_pct',                      # identity → z
    'minutes_above_strike',                    # identity → z
    'spot_distance_to_strike_sigma',           # identity → z
    'abs_spot_distance_to_strike_sigma',       # identity → z (symmetry prior)
    'time_decayed_proximity',                  # identity → z; engineered as
                                               # spot_distance × (1 - stc/900)
    'prob_breakeven_gap',                      # identity → z
    'spot_coinbase_kraken_gap_bps',            # identity → z
    'kalshi_flow_depth_velocity',              # identity → z
    'log_balance_dollars',                     # log_cents_to_dollars → z
    'hour_sin', 'hour_cos',                    # identity_no_zscore
]

CONT_FEATURE_TRANSFORMS = {
    'market_price': 'log_cents_to_dollars',
    'spot_realized_range_15m_bps': 'log1p_signed',
    'btc_realized_vol_15m': 'log1p',
    'log_balance_dollars': 'log_cents_to_dollars',
    'hour_sin': 'identity_no_zscore',
    'hour_cos': 'identity_no_zscore',
    # all others: 'identity'
}

# WS-fed columns — populated by live websocket caches; their NULLs correlate
# with stress events (feed drops). Indicator captures the event.
MISSING_INDICATOR_COLS = [
    'spot_momentum_60s_bps_missing',
    'spot_momentum_5m_bps_missing',
    'spot_realized_range_15m_bps_missing',
    'btc_spot_change_5m_bps_missing',
    'btc_realized_vol_15m_missing',
    'spot_coinbase_kraken_gap_bps_missing',
    'kalshi_flow_depth_velocity_missing',
]

# Map from the source column to its missing-indicator column. Used by
# extract_data.py to compute indicators in lockstep with NULL detection.
MISSING_INDICATOR_SOURCE_MAP = {
    'spot_momentum_60s_bps_missing': 'spot_momentum_60s_bps',
    'spot_momentum_5m_bps_missing': 'spot_momentum_5m_bps',
    'spot_realized_range_15m_bps_missing': 'spot_realized_range_15m_bps',
    'btc_spot_change_5m_bps_missing': 'btc_spot_change_5m_bps',
    'btc_realized_vol_15m_missing': 'btc_realized_vol_15m',
    'spot_coinbase_kraken_gap_bps_missing': 'spot_coinbase_kraken_gap_bps',
    'kalshi_flow_depth_velocity_missing': 'kalshi_flow_depth_velocity',
}


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
]


# ---------------------------------------------------------------------------
# Feature-schema fingerprint (cfg_fp)
# ---------------------------------------------------------------------------

def compute_cfg_fp(*, include_sub_floor: bool) -> str:
    """sha256[:16] of the canonical extraction-policy JSON. Two extracts
    with the same cfg_fp produce the same parquet schema and the same
    bucketization. Phase 6 A/B refuses to compare bundles with different
    cfg_fp."""
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
        'drop_predicates_order': DROP_PREDICATES_ORDER,
        'loss_form': 'bce_w_calibration_residual_v1',
        'loss_w_floor': 1.0,
        'loss_w_multiplier': 4.0,
    }
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
