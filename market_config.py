"""Centralized market-type configuration for all product types.

Each market type (15M crypto, hourly crypto, SPX hourly, weather) is described
by a single MarketTypeConfig instance.  The bot reads config values from these
frozen dataclasses instead of product_type-specific if/elif branches.

During migration, validate_market_configs() asserts that every config value
matches the corresponding legacy constant in bot.py.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Optional


@dataclass(frozen=True)
class MarketTypeConfig:
    """Immutable configuration for one market product type."""

    product_type: str                    # "15m", "hourly", "spx_hourly", "weather"
    enabled: bool = True
    observation_only: bool = False

    # ── Price range ──
    min_entry_price: int = 87
    max_entry_price: int = 99

    # ── Time window ──
    min_seconds_before_close: float = 0
    max_seconds_before_close: float = 300

    # ── Sizing ──
    max_risk_per_trade: float = 0.25
    kelly_fraction: float = 1.0          # 1.0 = full Kelly (no fractional scaling)

    # ── Calibration ──
    market_blend_w: float = 0.40
    temperature_t: float = 1.0           # 1.0 = no scaling
    temperature_enabled: bool = False
    cal_eligible: bool = True            # Include in CalibrationEngine training?
    use_hourly_dynamic_cap: bool = False

    # ── Fees ──
    fee_multiplier_taker: float = 0.07
    fee_multiplier_maker: float = 0.0175

    # ── Per-window risk management (None = no limit) ──
    excluded_assets: FrozenSet[str] = field(default_factory=frozenset)
    min_stc_entry: Optional[float] = None
    max_stc_entry: Optional[float] = None
    max_positions_per_window: Optional[int] = None
    max_window_risk: Optional[float] = None

    # ── Observation gate ──
    # The filter_stage string used when logging observation entries to DB.
    # e.g. "hourly_observation", "spx_observation", "weather_observation"
    observation_filter_label: str = ""


# ── Registry ────────────────────────────────────────────────────────────────

MARKET_CONFIGS: Dict[str, MarketTypeConfig] = {
    "15m": MarketTypeConfig(
        product_type="15m",
        enabled=True,
        observation_only=False,
        min_entry_price=87,
        max_entry_price=99,
        min_seconds_before_close=0,
        max_seconds_before_close=900,
        max_risk_per_trade=0.25,
        kelly_fraction=1.0,
        market_blend_w=0.40,
        temperature_t=1.0,
        temperature_enabled=False,
        cal_eligible=True,
        use_hourly_dynamic_cap=False,
        fee_multiplier_taker=0.07,
        fee_multiplier_maker=0.0175,
    ),
    "hourly": MarketTypeConfig(
        product_type="hourly",
        enabled=True,
        observation_only=True,
        min_entry_price=50,
        max_entry_price=99,
        min_seconds_before_close=0,
        max_seconds_before_close=1800,
        max_risk_per_trade=0.15,
        kelly_fraction=0.25,
        market_blend_w=0.40,
        temperature_t=1.45,
        temperature_enabled=True,
        cal_eligible=False,
        use_hourly_dynamic_cap=True,
        fee_multiplier_taker=0.07,
        fee_multiplier_maker=0.0175,
        excluded_assets=frozenset(),      # Empty in observation mode
        min_stc_entry=300,
        max_stc_entry=1800,
        max_positions_per_window=2,
        max_window_risk=0.15,
        observation_filter_label="hourly_observation",
    ),
    "spx_hourly": MarketTypeConfig(
        product_type="spx_hourly",
        enabled=True,
        observation_only=True,
        min_entry_price=70,
        max_entry_price=99,
        min_seconds_before_close=300,
        max_seconds_before_close=1800,
        max_risk_per_trade=0.15,
        kelly_fraction=0.25,
        market_blend_w=0.40,
        temperature_t=1.0,
        temperature_enabled=False,
        cal_eligible=False,
        use_hourly_dynamic_cap=False,
        fee_multiplier_taker=0.035,
        fee_multiplier_maker=0.0175,
        max_positions_per_window=2,
        max_window_risk=0.15,
        observation_filter_label="spx_observation",
    ),
    "weather": MarketTypeConfig(
        product_type="weather",
        enabled=True,
        observation_only=True,
        min_entry_price=10,
        max_entry_price=99,
        min_seconds_before_close=3600,
        max_seconds_before_close=86400,
        max_risk_per_trade=0.10,
        kelly_fraction=0.25,
        market_blend_w=0.20,
        temperature_t=1.0,
        temperature_enabled=False,
        cal_eligible=False,
        use_hourly_dynamic_cap=False,
        fee_multiplier_taker=0.07,
        fee_multiplier_maker=0.0175,
        observation_filter_label="weather_observation",
    ),
    "sports": MarketTypeConfig(
        product_type="sports",
        enabled=True,
        observation_only=True,
        min_entry_price=1,
        max_entry_price=99,
        min_seconds_before_close=0,
        max_seconds_before_close=0,     # N/A for sports — game-level markets
        max_risk_per_trade=0.10,
        kelly_fraction=0.25,
        market_blend_w=0.0,             # No blend — Bayesian model only
        temperature_t=1.0,
        temperature_enabled=False,
        cal_eligible=False,
        use_hourly_dynamic_cap=False,
        fee_multiplier_taker=0.07,
        fee_multiplier_maker=0.0175,
        observation_filter_label="sports_observation",
    ),
}


def get_market_config(product_type: Optional[str] = None) -> MarketTypeConfig:
    """Look up config by product_type.  None / unknown → 15M default."""
    return MARKET_CONFIGS.get(product_type or "15m", MARKET_CONFIGS["15m"])


def get_cal_excluded_types() -> set:
    """Return set of product_type strings that are NOT eligible for calibration training."""
    return {k for k, cfg in MARKET_CONFIGS.items() if not cfg.cal_eligible}


def validate_market_configs() -> None:
    """Assert every config value matches the corresponding bot.py constant.

    Called at startup.  If any value drifts, the bot crashes immediately
    rather than silently using wrong parameters.
    """
    # Import bot constants locally to avoid circular import at module level
    import bot  # noqa: F811

    # ── 15M ──
    cfg = MARKET_CONFIGS["15m"]
    assert cfg.min_entry_price == bot.MIN_ENTRY_PRICE, (
        f"15m min_entry: {cfg.min_entry_price} != {bot.MIN_ENTRY_PRICE}")
    assert cfg.max_entry_price == bot.MAX_ENTRY_PRICE, (
        f"15m max_entry: {cfg.max_entry_price} != {bot.MAX_ENTRY_PRICE}")
    assert cfg.max_risk_per_trade == bot.MAX_RISK_PER_TRADE, (
        f"15m max_risk: {cfg.max_risk_per_trade} != {bot.MAX_RISK_PER_TRADE}")
    assert cfg.min_seconds_before_close == bot.MIN_SECONDS_BEFORE_CLOSE, (
        f"15m min_stc: {cfg.min_seconds_before_close} != {bot.MIN_SECONDS_BEFORE_CLOSE}")
    assert cfg.max_seconds_before_close == bot.MAX_SECONDS_BEFORE_CLOSE, (
        f"15m max_stc: {cfg.max_seconds_before_close} != {bot.MAX_SECONDS_BEFORE_CLOSE}")
    assert cfg.market_blend_w == bot.MARKET_BLEND_W, (
        f"15m blend_w: {cfg.market_blend_w} != {bot.MARKET_BLEND_W}")
    assert cfg.observation_only == bot.OBSERVATION_MODE, (
        f"15m obs_only: {cfg.observation_only} != {bot.OBSERVATION_MODE}")

    # ── Hourly ──
    cfg_h = MARKET_CONFIGS["hourly"]
    assert cfg_h.observation_only == bot.HOURLY_OBSERVATION_ONLY, (
        f"hourly obs_only: {cfg_h.observation_only} != {bot.HOURLY_OBSERVATION_ONLY}")
    assert cfg_h.min_entry_price == bot.HOURLY_MIN_ENTRY_PRICE, (
        f"hourly min_entry: {cfg_h.min_entry_price} != {bot.HOURLY_MIN_ENTRY_PRICE}")
    assert cfg_h.max_entry_price == bot.MAX_ENTRY_PRICE, (
        f"hourly max_entry: {cfg_h.max_entry_price} != {bot.MAX_ENTRY_PRICE}")
    assert cfg_h.min_seconds_before_close == bot.HOURLY_MIN_SECONDS_BEFORE_CLOSE, (
        f"hourly min_stc: {cfg_h.min_seconds_before_close} != {bot.HOURLY_MIN_SECONDS_BEFORE_CLOSE}")
    assert cfg_h.max_seconds_before_close == bot.HOURLY_MAX_SECONDS_BEFORE_CLOSE, (
        f"hourly max_stc: {cfg_h.max_seconds_before_close} != {bot.HOURLY_MAX_SECONDS_BEFORE_CLOSE}")
    assert cfg_h.max_risk_per_trade == bot.HOURLY_MAX_RISK_PER_TRADE, (
        f"hourly max_risk: {cfg_h.max_risk_per_trade} != {bot.HOURLY_MAX_RISK_PER_TRADE}")
    assert cfg_h.kelly_fraction == bot.HOURLY_KELLY_FRACTION, (
        f"hourly kelly_f: {cfg_h.kelly_fraction} != {bot.HOURLY_KELLY_FRACTION}")
    assert cfg_h.market_blend_w == bot.HOURLY_MARKET_BLEND_W, (
        f"hourly blend_w: {cfg_h.market_blend_w} != {bot.HOURLY_MARKET_BLEND_W}")
    assert cfg_h.temperature_t == bot.HOURLY_TEMPERATURE_T, (
        f"hourly temp_t: {cfg_h.temperature_t} != {bot.HOURLY_TEMPERATURE_T}")
    assert cfg_h.temperature_enabled == bot.HOURLY_TEMPERATURE_ENABLED, (
        f"hourly temp_enabled: {cfg_h.temperature_enabled} != {bot.HOURLY_TEMPERATURE_ENABLED}")
    assert cfg_h.min_stc_entry == bot.HOURLY_MIN_STC_ENTRY, (
        f"hourly min_stc_entry: {cfg_h.min_stc_entry} != {bot.HOURLY_MIN_STC_ENTRY}")
    assert cfg_h.max_stc_entry == bot.HOURLY_MAX_STC_ENTRY, (
        f"hourly max_stc_entry: {cfg_h.max_stc_entry} != {bot.HOURLY_MAX_STC_ENTRY}")
    assert cfg_h.excluded_assets == frozenset(bot.HOURLY_EXCLUDED_ASSETS), (
        f"hourly excluded: {cfg_h.excluded_assets} != {bot.HOURLY_EXCLUDED_ASSETS}")
    assert cfg_h.max_positions_per_window == bot.HOURLY_MAX_POSITIONS_PER_WINDOW, (
        f"hourly max_pos: {cfg_h.max_positions_per_window} != {bot.HOURLY_MAX_POSITIONS_PER_WINDOW}")
    assert cfg_h.max_window_risk == bot.HOURLY_MAX_WINDOW_RISK, (
        f"hourly max_wrisk: {cfg_h.max_window_risk} != {bot.HOURLY_MAX_WINDOW_RISK}")

    # ── SPX Hourly ──
    cfg_s = MARKET_CONFIGS["spx_hourly"]
    assert cfg_s.observation_only == bot.SPX_HOURLY_OBSERVATION_ONLY, (
        f"spx obs_only: {cfg_s.observation_only} != {bot.SPX_HOURLY_OBSERVATION_ONLY}")
    assert cfg_s.min_entry_price == bot.SPX_HOURLY_MIN_ENTRY_PRICE, (
        f"spx min_entry: {cfg_s.min_entry_price} != {bot.SPX_HOURLY_MIN_ENTRY_PRICE}")
    assert cfg_s.max_entry_price == bot.SPX_HOURLY_MAX_ENTRY_PRICE, (
        f"spx max_entry: {cfg_s.max_entry_price} != {bot.SPX_HOURLY_MAX_ENTRY_PRICE}")
    assert cfg_s.min_seconds_before_close == bot.SPX_HOURLY_MIN_SECONDS_BEFORE_CLOSE, (
        f"spx min_stc: {cfg_s.min_seconds_before_close} != {bot.SPX_HOURLY_MIN_SECONDS_BEFORE_CLOSE}")
    assert cfg_s.max_seconds_before_close == bot.SPX_HOURLY_MAX_SECONDS_BEFORE_CLOSE, (
        f"spx max_stc: {cfg_s.max_seconds_before_close} != {bot.SPX_HOURLY_MAX_SECONDS_BEFORE_CLOSE}")
    assert cfg_s.max_risk_per_trade == bot.SPX_HOURLY_MAX_RISK_PER_TRADE, (
        f"spx max_risk: {cfg_s.max_risk_per_trade} != {bot.SPX_HOURLY_MAX_RISK_PER_TRADE}")
    assert cfg_s.kelly_fraction == bot.SPX_HOURLY_KELLY_FRACTION, (
        f"spx kelly_f: {cfg_s.kelly_fraction} != {bot.SPX_HOURLY_KELLY_FRACTION}")
    assert cfg_s.market_blend_w == bot.SPX_HOURLY_MARKET_BLEND_W, (
        f"spx blend_w: {cfg_s.market_blend_w} != {bot.SPX_HOURLY_MARKET_BLEND_W}")
    assert cfg_s.temperature_t == bot.SPX_HOURLY_TEMPERATURE_T, (
        f"spx temp_t: {cfg_s.temperature_t} != {bot.SPX_HOURLY_TEMPERATURE_T}")
    assert cfg_s.fee_multiplier_taker == bot.SPX_HOURLY_FEE_MULTIPLIER_TAKER, (
        f"spx fee_taker: {cfg_s.fee_multiplier_taker} != {bot.SPX_HOURLY_FEE_MULTIPLIER_TAKER}")
    assert cfg_s.fee_multiplier_maker == bot.SPX_HOURLY_FEE_MULTIPLIER_MAKER, (
        f"spx fee_maker: {cfg_s.fee_multiplier_maker} != {bot.SPX_HOURLY_FEE_MULTIPLIER_MAKER}")
    assert cfg_s.max_positions_per_window == bot.SPX_HOURLY_MAX_POSITIONS_PER_WINDOW, (
        f"spx max_pos: {cfg_s.max_positions_per_window} != {bot.SPX_HOURLY_MAX_POSITIONS_PER_WINDOW}")
    assert cfg_s.max_window_risk == bot.SPX_HOURLY_MAX_WINDOW_RISK, (
        f"spx max_wrisk: {cfg_s.max_window_risk} != {bot.SPX_HOURLY_MAX_WINDOW_RISK}")

    # ── Weather ──
    cfg_w = MARKET_CONFIGS["weather"]
    assert cfg_w.observation_only == bot.WEATHER_OBSERVATION_ONLY, (
        f"weather obs_only: {cfg_w.observation_only} != {bot.WEATHER_OBSERVATION_ONLY}")
    assert cfg_w.min_entry_price == bot.WEATHER_MIN_ENTRY_PRICE, (
        f"weather min_entry: {cfg_w.min_entry_price} != {bot.WEATHER_MIN_ENTRY_PRICE}")
    assert cfg_w.max_entry_price == bot.WEATHER_MAX_ENTRY_PRICE, (
        f"weather max_entry: {cfg_w.max_entry_price} != {bot.WEATHER_MAX_ENTRY_PRICE}")
    assert cfg_w.min_seconds_before_close == bot.WEATHER_MIN_SECONDS_BEFORE_CLOSE, (
        f"weather min_stc: {cfg_w.min_seconds_before_close} != {bot.WEATHER_MIN_SECONDS_BEFORE_CLOSE}")
    assert cfg_w.max_seconds_before_close == bot.WEATHER_MAX_SECONDS_BEFORE_CLOSE, (
        f"weather max_stc: {cfg_w.max_seconds_before_close} != {bot.WEATHER_MAX_SECONDS_BEFORE_CLOSE}")
    assert cfg_w.max_risk_per_trade == bot.WEATHER_MAX_RISK_PER_TRADE, (
        f"weather max_risk: {cfg_w.max_risk_per_trade} != {bot.WEATHER_MAX_RISK_PER_TRADE}")
    assert cfg_w.kelly_fraction == bot.WEATHER_KELLY_FRACTION, (
        f"weather kelly_f: {cfg_w.kelly_fraction} != {bot.WEATHER_KELLY_FRACTION}")
    assert cfg_w.market_blend_w == bot.WEATHER_MARKET_BLEND_W, (
        f"weather blend_w: {cfg_w.market_blend_w} != {bot.WEATHER_MARKET_BLEND_W}")

    # ── Sports ──
    cfg_sp = MARKET_CONFIGS["sports"]
    assert cfg_sp.observation_only is True, (
        "sports observation_only must be True — never live without explicit promotion")
    assert cfg_sp.observation_only == bot.SPORTS_OBSERVATION_ONLY, (
        f"sports obs_only: {cfg_sp.observation_only} != {bot.SPORTS_OBSERVATION_ONLY}")
    assert cfg_sp.cal_eligible is False, "sports must NOT be cal_eligible"

    # ── Cross-type invariants ──
    assert MARKET_CONFIGS["15m"].cal_eligible is True, "15m must be cal_eligible"
    for pt in ("hourly", "spx_hourly", "weather", "sports"):
        assert not MARKET_CONFIGS[pt].cal_eligible, f"{pt} should not be cal_eligible"

    # Observation filter labels match exact DB strings
    assert MARKET_CONFIGS["hourly"].observation_filter_label == "hourly_observation"
    assert MARKET_CONFIGS["spx_hourly"].observation_filter_label == "spx_observation"
    assert MARKET_CONFIGS["weather"].observation_filter_label == "weather_observation"
    assert MARKET_CONFIGS["sports"].observation_filter_label == "sports_observation"

    import logging
    logging.info("MARKET_CONFIGS: all %d configs validated against constants", len(MARKET_CONFIGS))
