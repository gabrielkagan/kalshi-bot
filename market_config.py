"""Centralized market-type configuration for all product types.

Each market type (15M crypto, hourly crypto, SPX hourly, weather) is described
by a single MarketTypeConfig instance.  The bot reads config values from these
frozen dataclasses instead of product_type-specific if/elif branches.

During migration, validate_market_configs() asserts that every config value
matches the corresponding legacy constant in bot.py.
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Optional
import bot.constants  # noqa: F401
import bot.config as config  # noqa: F401


def _bot_hourly_live() -> bool:
    """Read HOURLY_LIVE_ENABLED env var without importing bot.py (avoids circular import)."""
    return os.environ.get("HOURLY_LIVE_ENABLED", "0") == "1"


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
    # Per-asset overrides for `market_blend_w`. When set (15M only post-P2.1.d),
    # `get_blend_w(asset)` returns the per-asset value; unknown assets fall
    # back to the scalar `market_blend_w`. Hourly/SPX/weather/sports leave
    # this None and route everything through the scalar.
    market_blend_w_by_asset: Optional[Dict[str, float]] = None
    temperature_t: float = 1.0           # 1.0 = no scaling
    temperature_enabled: bool = False
    cal_eligible: bool = True            # Include in CalibrationEngine training?
    use_hourly_dynamic_cap: bool = False
    cal_engine_enabled: bool = False     # Whether to instantiate a CalEngine for this market
    cal_engine_state_path: str = ""      # State file path (empty = no engine)
    cal_subtypes: Dict[str, str] = field(default_factory=dict)  # subtype_code → state_file_path

    # ── Fees ──
    fee_multiplier_taker: float = 0.07
    fee_multiplier_maker: float = 0.0  # Kalshi charges $0 on maker fills

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

    def get_blend_w(self, asset: Optional[str]) -> float:
        """Return the market-blend weight for ``asset``, falling back to the
        scalar ``market_blend_w`` when no per-asset override is configured
        (or when ``asset`` is None / not in the override map).

        Post-P2.1.d (2026-05-13) + P2.3 (2026-05-14) + P2.4 (2026-05-19) the
        15M MarketConfig pins ``market_blend_w_by_asset`` to
        MARKET_BLEND_W_BY_ASSET for all 7 production 15M assets
        (BTC/ETH/SOL/XRP/HYPE/DOGE/BNB); unknown assets + hourly/spx/weather/sports
        all use the scalar fallback."""
        if self.market_blend_w_by_asset and asset:
            return self.market_blend_w_by_asset.get(asset, self.market_blend_w)
        return self.market_blend_w


# ── Registry ────────────────────────────────────────────────────────────────

MARKET_CONFIGS: Dict[str, MarketTypeConfig] = {
    "15m": MarketTypeConfig(
        product_type="15m",
        enabled=True,
        observation_only=False,
        min_entry_price=75,
        max_entry_price=99,
        min_seconds_before_close=0,
        max_seconds_before_close=900,
        max_risk_per_trade=0.25,
        kelly_fraction=1.0,
        market_blend_w=0.40,
        # P2.1.d (2026-05-13) + P2.3 (2026-05-14) + P2.4 (2026-05-19): per-asset
        # weights for all 7 production 15M assets — operator-confirmed argmaxes with
        # interior-pull discipline. Sourced from
        # bot.constants.MARKET_BLEND_W_BY_ASSET; validated lock-step at
        # startup in validate_market_configs(). Unknown assets fall back to
        # market_blend_w=0.40 via get_blend_w().
        market_blend_w_by_asset=dict(bot.constants.MARKET_BLEND_W_BY_ASSET),
        temperature_t=1.0,
        temperature_enabled=False,
        cal_eligible=True,
        use_hourly_dynamic_cap=False,
        fee_multiplier_taker=0.07,
        fee_multiplier_maker=0.0,  # Kalshi charges $0 on maker fills
        cal_subtypes={              # Per-asset CalEngines (shadow — cal_engine_enabled=False)
            "BTC": "cal_15m_BTC.json",
            "ETH": "cal_15m_ETH.json",
            "SOL": "cal_15m_SOL.json",
            "XRP": "cal_15m_XRP.json",
        },
    ),
    "hourly": MarketTypeConfig(
        product_type="hourly",
        enabled=True,
        observation_only=not _bot_hourly_live(),  # Derived from HOURLY_LIVE_ENABLED env var
        min_entry_price=50,
        max_entry_price=59,                # Sub-60c only — edge lives at low prices
        min_seconds_before_close=0,
        max_seconds_before_close=1800,
        max_risk_per_trade=0.15,
        kelly_fraction=0.25,
        market_blend_w=0.40,
        temperature_t=1.45,
        temperature_enabled=True,
        cal_eligible=False,
        use_hourly_dynamic_cap=True,
        cal_engine_enabled=False,   # Disabled: hourly beta_cal +44pp overconfident; passthrough+T=1.45 is better
        cal_engine_state_path="hourly_calibration_state.json",
        fee_multiplier_taker=0.07,
        fee_multiplier_maker=0.0,  # Kalshi charges $0 on maker fills
        excluded_assets=frozenset({"SOL", "XRP", "HYPE", "DOGE", "BNB", "ADA", "BCH", "NEAR", "ZEC"}),  # NEAR/ZEC per 15M-shadow-only T1 2026-09-05 (86bbvdc8y); BTC+ETH only; HYPE/DOGE/BNB hourly excluded per 15M-only promotion design (HYPE/DOGE T4 P2.3 2026-05-14, BNB T4 P2.4 2026-05-19); ADA/BCH per 15M-shadow-only T1 2026-05-30 — lock-step with bot/constants.py:HOURLY_EXCLUDED_ASSETS (assertion below will crash startup on mismatch)
        min_stc_entry=600,                 # 10 min minimum
        max_stc_entry=1800,                # 30 min maximum
        max_positions_per_window=2,
        max_window_risk=0.15,
        observation_filter_label="hourly_observation",
    ),
    "spx_hourly": MarketTypeConfig(
        product_type="spx_hourly",
        enabled=True,
        observation_only=True,             # Reverted — Polygon 403 breaks vol engine
        min_entry_price=90,                # 90c+ floor (SPX-C: 90.9% WR)
        max_entry_price=99,
        min_seconds_before_close=300,
        max_seconds_before_close=1800,
        max_risk_per_trade=0.10,           # Conservative (down from 0.15)
        kelly_fraction=0.125,              # Eighth-Kelly
        market_blend_w=0.00,               # No blend — CalEngine only (SPX-D)
        temperature_t=1.0,
        temperature_enabled=False,
        cal_eligible=False,
        use_hourly_dynamic_cap=True,
        cal_engine_enabled=True,           # SPX-D: CalEngine learned temperature
        cal_engine_state_path="spx_hourly_calibration_state.json",
        fee_multiplier_taker=0.035,
        fee_multiplier_maker=0.0,  # Kalshi charges $0 on maker fills
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
        use_hourly_dynamic_cap=True,
        cal_engine_enabled=True,   # Enabled: per-city CalEngines learning in shadow
        fee_multiplier_taker=0.07,
        fee_multiplier_maker=0.0,  # Kalshi charges $0 on maker fills
        min_stc_entry=3600.0,      # 1h minimum (matching WEATHER_MIN_SECONDS_BEFORE_CLOSE)
        max_stc_entry=43200.0,     # 12h maximum (audit: 4-12h calibrated, 12h+ catastrophic)
        observation_filter_label="weather_observation",
        cal_subtypes={
            "NYC": "cal_weather_NYC.json",
            "CHI": "cal_weather_CHI.json",
            "MIA": "cal_weather_MIA.json",
            "DEN": "cal_weather_DEN.json",
            "LAX": "cal_weather_LAX.json",
            "AUS": "cal_weather_AUS.json",
            "ATL": "cal_weather_ATL.json",
            "SFO": "cal_weather_SFO.json",
            "DAL": "cal_weather_DAL.json",
            "PHX": "cal_weather_PHX.json",
            "PHI": "cal_weather_PHI.json",
            "MIN": "cal_weather_MIN.json",
            "SEA": "cal_weather_SEA.json",
            "HOU": "cal_weather_HOU.json",
            "BOS": "cal_weather_BOS.json",
            "LAS": "cal_weather_LAS.json",
            "OKC": "cal_weather_OKC.json",
            "DCA": "cal_weather_DCA.json",
            "MSY": "cal_weather_MSY.json",
        },
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
        fee_multiplier_maker=0.0,  # Kalshi charges $0 on maker fills
        observation_filter_label="sports_observation",
        cal_subtypes={
            "basketball": "cal_sports_basketball.json",
            "hockey": "cal_sports_hockey.json",
            "soccer": "cal_sports_soccer.json",
            "baseball": "cal_sports_baseball.json",
            "football": "cal_sports_football.json",
            "tennis": "cal_sports_tennis.json",
            "mma": "cal_sports_mma.json",
            "esports": "cal_sports_esports.json",
        },
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
    assert cfg.min_entry_price == bot.constants.MIN_ENTRY_PRICE, (
        f"15m min_entry: {cfg.min_entry_price} != {bot.constants.MIN_ENTRY_PRICE}")
    assert cfg.max_entry_price == bot.constants.MAX_ENTRY_PRICE, (
        f"15m max_entry: {cfg.max_entry_price} != {bot.constants.MAX_ENTRY_PRICE}")
    assert cfg.max_risk_per_trade == config.MAX_RISK_PER_TRADE, (
        f"15m max_risk: {cfg.max_risk_per_trade} != {config.MAX_RISK_PER_TRADE}")
    assert cfg.min_seconds_before_close == bot.constants.MIN_SECONDS_BEFORE_CLOSE, (
        f"15m min_stc: {cfg.min_seconds_before_close} != {bot.constants.MIN_SECONDS_BEFORE_CLOSE}")
    assert cfg.max_seconds_before_close == bot.constants.MAX_SECONDS_BEFORE_CLOSE, (
        f"15m max_stc: {cfg.max_seconds_before_close} != {bot.constants.MAX_SECONDS_BEFORE_CLOSE}")
    assert cfg.market_blend_w == bot.constants.MARKET_BLEND_W, (
        f"15m blend_w: {cfg.market_blend_w} != {bot.constants.MARKET_BLEND_W}")
    # P2.1.d (2026-05-13): per-asset blend-weight map lock-step. The dataclass
    # instance must mirror bot.constants.MARKET_BLEND_W_BY_ASSET exactly —
    # drift here means the runtime reads a different weight than the constant
    # advertised, breaking the rollback contract.
    assert cfg.market_blend_w_by_asset == bot.constants.MARKET_BLEND_W_BY_ASSET, (
        f"15m blend_w_by_asset drift: {cfg.market_blend_w_by_asset} != "
        f"{bot.constants.MARKET_BLEND_W_BY_ASSET}")
    assert cfg.observation_only == bot.constants.OBSERVATION_MODE, (
        f"15m obs_only: {cfg.observation_only} != {bot.constants.OBSERVATION_MODE}")

    # ── Hourly ──
    cfg_h = MARKET_CONFIGS["hourly"]
    assert cfg_h.observation_only == bot.constants.HOURLY_OBSERVATION_ONLY, (
        f"hourly obs_only: {cfg_h.observation_only} != {bot.constants.HOURLY_OBSERVATION_ONLY}")
    assert cfg_h.min_entry_price == bot.constants.HOURLY_MIN_ENTRY_PRICE, (
        f"hourly min_entry: {cfg_h.min_entry_price} != {bot.constants.HOURLY_MIN_ENTRY_PRICE}")
    assert cfg_h.max_entry_price == bot.constants.HOURLY_MAX_ENTRY_PRICE, (
        f"hourly max_entry: {cfg_h.max_entry_price} != {bot.constants.HOURLY_MAX_ENTRY_PRICE}")
    assert cfg_h.min_seconds_before_close == bot.constants.HOURLY_MIN_SECONDS_BEFORE_CLOSE, (
        f"hourly min_stc: {cfg_h.min_seconds_before_close} != {bot.constants.HOURLY_MIN_SECONDS_BEFORE_CLOSE}")
    assert cfg_h.max_seconds_before_close == bot.constants.HOURLY_MAX_SECONDS_BEFORE_CLOSE, (
        f"hourly max_stc: {cfg_h.max_seconds_before_close} != {bot.constants.HOURLY_MAX_SECONDS_BEFORE_CLOSE}")
    assert cfg_h.max_risk_per_trade == bot.constants.HOURLY_MAX_RISK_PER_TRADE, (
        f"hourly max_risk: {cfg_h.max_risk_per_trade} != {bot.constants.HOURLY_MAX_RISK_PER_TRADE}")
    assert cfg_h.kelly_fraction == bot.constants.HOURLY_KELLY_FRACTION, (
        f"hourly kelly_f: {cfg_h.kelly_fraction} != {bot.constants.HOURLY_KELLY_FRACTION}")
    assert cfg_h.market_blend_w == bot.constants.HOURLY_MARKET_BLEND_W, (
        f"hourly blend_w: {cfg_h.market_blend_w} != {bot.constants.HOURLY_MARKET_BLEND_W}")
    assert cfg_h.temperature_t == bot.constants.HOURLY_TEMPERATURE_T, (
        f"hourly temp_t: {cfg_h.temperature_t} != {bot.constants.HOURLY_TEMPERATURE_T}")
    assert cfg_h.temperature_enabled == bot.constants.HOURLY_TEMPERATURE_ENABLED, (
        f"hourly temp_enabled: {cfg_h.temperature_enabled} != {bot.constants.HOURLY_TEMPERATURE_ENABLED}")
    assert cfg_h.min_stc_entry == bot.constants.HOURLY_MIN_STC_ENTRY, (
        f"hourly min_stc_entry: {cfg_h.min_stc_entry} != {bot.constants.HOURLY_MIN_STC_ENTRY}")
    assert cfg_h.max_stc_entry == bot.constants.HOURLY_MAX_STC_ENTRY, (
        f"hourly max_stc_entry: {cfg_h.max_stc_entry} != {bot.constants.HOURLY_MAX_STC_ENTRY}")
    assert cfg_h.excluded_assets == frozenset(bot.constants.HOURLY_EXCLUDED_ASSETS), (
        f"hourly excluded: {cfg_h.excluded_assets} != {bot.constants.HOURLY_EXCLUDED_ASSETS}")
    assert cfg_h.max_positions_per_window == bot.constants.HOURLY_MAX_POSITIONS_PER_WINDOW, (
        f"hourly max_pos: {cfg_h.max_positions_per_window} != {bot.constants.HOURLY_MAX_POSITIONS_PER_WINDOW}")
    assert cfg_h.max_window_risk == bot.constants.HOURLY_MAX_WINDOW_RISK, (
        f"hourly max_wrisk: {cfg_h.max_window_risk} != {bot.constants.HOURLY_MAX_WINDOW_RISK}")

    # ── SPX Hourly ──
    cfg_s = MARKET_CONFIGS["spx_hourly"]
    assert cfg_s.observation_only == bot.constants.SPX_HOURLY_OBSERVATION_ONLY, (
        f"spx obs_only: {cfg_s.observation_only} != {bot.constants.SPX_HOURLY_OBSERVATION_ONLY}")
    assert cfg_s.min_entry_price == bot.constants.SPX_HOURLY_MIN_ENTRY_PRICE, (
        f"spx min_entry: {cfg_s.min_entry_price} != {bot.constants.SPX_HOURLY_MIN_ENTRY_PRICE}")
    assert cfg_s.max_entry_price == bot.constants.SPX_HOURLY_MAX_ENTRY_PRICE, (
        f"spx max_entry: {cfg_s.max_entry_price} != {bot.constants.SPX_HOURLY_MAX_ENTRY_PRICE}")
    assert cfg_s.min_seconds_before_close == bot.constants.SPX_HOURLY_MIN_SECONDS_BEFORE_CLOSE, (
        f"spx min_stc: {cfg_s.min_seconds_before_close} != {bot.constants.SPX_HOURLY_MIN_SECONDS_BEFORE_CLOSE}")
    assert cfg_s.max_seconds_before_close == bot.constants.SPX_HOURLY_MAX_SECONDS_BEFORE_CLOSE, (
        f"spx max_stc: {cfg_s.max_seconds_before_close} != {bot.constants.SPX_HOURLY_MAX_SECONDS_BEFORE_CLOSE}")
    assert cfg_s.max_risk_per_trade == bot.constants.SPX_HOURLY_MAX_RISK_PER_TRADE, (
        f"spx max_risk: {cfg_s.max_risk_per_trade} != {bot.constants.SPX_HOURLY_MAX_RISK_PER_TRADE}")
    assert cfg_s.kelly_fraction == bot.constants.SPX_HOURLY_KELLY_FRACTION, (
        f"spx kelly_f: {cfg_s.kelly_fraction} != {bot.constants.SPX_HOURLY_KELLY_FRACTION}")
    assert cfg_s.market_blend_w == bot.constants.SPX_HOURLY_MARKET_BLEND_W, (
        f"spx blend_w: {cfg_s.market_blend_w} != {bot.constants.SPX_HOURLY_MARKET_BLEND_W}")
    assert cfg_s.temperature_t == bot.constants.SPX_HOURLY_TEMPERATURE_T, (
        f"spx temp_t: {cfg_s.temperature_t} != {bot.constants.SPX_HOURLY_TEMPERATURE_T}")
    assert cfg_s.fee_multiplier_taker == bot.constants.SPX_HOURLY_FEE_MULTIPLIER_TAKER, (
        f"spx fee_taker: {cfg_s.fee_multiplier_taker} != {bot.constants.SPX_HOURLY_FEE_MULTIPLIER_TAKER}")
    assert cfg_s.fee_multiplier_maker == bot.constants.SPX_HOURLY_FEE_MULTIPLIER_MAKER, (
        f"spx fee_maker: {cfg_s.fee_multiplier_maker} != {bot.constants.SPX_HOURLY_FEE_MULTIPLIER_MAKER}")
    assert cfg_s.max_positions_per_window == bot.constants.SPX_HOURLY_MAX_POSITIONS_PER_WINDOW, (
        f"spx max_pos: {cfg_s.max_positions_per_window} != {bot.constants.SPX_HOURLY_MAX_POSITIONS_PER_WINDOW}")
    assert cfg_s.max_window_risk == bot.constants.SPX_HOURLY_MAX_WINDOW_RISK, (
        f"spx max_wrisk: {cfg_s.max_window_risk} != {bot.constants.SPX_HOURLY_MAX_WINDOW_RISK}")
    assert cfg_s.cal_engine_enabled is True, (
        "spx_hourly cal_engine_enabled must be True — SPX-D CalEngine is the live calibration")
    assert isinstance(bot.constants.SPX_HOURLY_BANKROLL_FRACTION, float), (
        f"SPX_HOURLY_BANKROLL_FRACTION must be float, got {type(bot.constants.SPX_HOURLY_BANKROLL_FRACTION)}")
    assert 0 < bot.constants.SPX_HOURLY_BANKROLL_FRACTION <= 1.0, (
        f"SPX_HOURLY_BANKROLL_FRACTION must be in (0, 1.0], got {bot.constants.SPX_HOURLY_BANKROLL_FRACTION}")

    # ── Weather ──
    cfg_w = MARKET_CONFIGS["weather"]
    assert cfg_w.observation_only == bot.constants.WEATHER_OBSERVATION_ONLY, (
        f"weather obs_only: {cfg_w.observation_only} != {bot.constants.WEATHER_OBSERVATION_ONLY}")
    assert cfg_w.min_entry_price == bot.constants.WEATHER_MIN_ENTRY_PRICE, (
        f"weather min_entry: {cfg_w.min_entry_price} != {bot.constants.WEATHER_MIN_ENTRY_PRICE}")
    assert cfg_w.max_entry_price == bot.constants.WEATHER_MAX_ENTRY_PRICE, (
        f"weather max_entry: {cfg_w.max_entry_price} != {bot.constants.WEATHER_MAX_ENTRY_PRICE}")
    assert cfg_w.min_seconds_before_close == bot.constants.WEATHER_MIN_SECONDS_BEFORE_CLOSE, (
        f"weather min_stc: {cfg_w.min_seconds_before_close} != {bot.constants.WEATHER_MIN_SECONDS_BEFORE_CLOSE}")
    assert cfg_w.max_seconds_before_close == bot.constants.WEATHER_MAX_SECONDS_BEFORE_CLOSE, (
        f"weather max_stc: {cfg_w.max_seconds_before_close} != {bot.constants.WEATHER_MAX_SECONDS_BEFORE_CLOSE}")
    assert cfg_w.max_risk_per_trade == bot.constants.WEATHER_MAX_RISK_PER_TRADE, (
        f"weather max_risk: {cfg_w.max_risk_per_trade} != {bot.constants.WEATHER_MAX_RISK_PER_TRADE}")
    assert cfg_w.kelly_fraction == bot.constants.WEATHER_KELLY_FRACTION, (
        f"weather kelly_f: {cfg_w.kelly_fraction} != {bot.constants.WEATHER_KELLY_FRACTION}")
    assert cfg_w.market_blend_w == bot.constants.WEATHER_MARKET_BLEND_W, (
        f"weather blend_w: {cfg_w.market_blend_w} != {bot.constants.WEATHER_MARKET_BLEND_W}")
    assert cfg_w.min_stc_entry == bot.constants.WEATHER_MIN_STC_ENTRY, (
        f"weather min_stc_entry: {cfg_w.min_stc_entry} != {bot.constants.WEATHER_MIN_STC_ENTRY}")
    assert cfg_w.max_stc_entry == bot.constants.WEATHER_MAX_STC_ENTRY, (
        f"weather max_stc_entry: {cfg_w.max_stc_entry} != {bot.constants.WEATHER_MAX_STC_ENTRY}")
    assert cfg_w.cal_engine_enabled == bot.constants.WEATHER_CAL_ENGINE_ENABLED, (
        f"weather cal_enabled: {cfg_w.cal_engine_enabled} != {bot.constants.WEATHER_CAL_ENGINE_ENABLED}")
    # Weather NO-side live constants exist and have valid types
    assert isinstance(bot.constants.WEATHER_NO_SIDE_LIVE, bool), (
        f"WEATHER_NO_SIDE_LIVE must be bool, got {type(bot.constants.WEATHER_NO_SIDE_LIVE)}")
    assert isinstance(bot.constants.WEATHER_NO_SIDE_MIN_STC, (int, float)), (
        f"WEATHER_NO_SIDE_MIN_STC must be numeric, got {type(bot.constants.WEATHER_NO_SIDE_MIN_STC)}")
    assert bot.constants.WEATHER_NO_SIDE_MIN_STC >= 3600, (
        f"WEATHER_NO_SIDE_MIN_STC must be >= 1h, got {bot.constants.WEATHER_NO_SIDE_MIN_STC}")
    assert isinstance(bot.constants.WEATHER_NO_CONTRACT_COUNT, int) and bot.constants.WEATHER_NO_CONTRACT_COUNT >= 1, (
        f"WEATHER_NO_CONTRACT_COUNT must be int >= 1, got {bot.constants.WEATHER_NO_CONTRACT_COUNT!r}")
    assert isinstance(bot.constants.WEATHER_NO_EXCLUDED_CITY_PREFIXES, frozenset), (
        f"WEATHER_NO_EXCLUDED_CITY_PREFIXES must be frozenset, got "
        f"{type(bot.constants.WEATHER_NO_EXCLUDED_CITY_PREFIXES).__name__}")

    # ── Sports ──
    cfg_sp = MARKET_CONFIGS["sports"]
    assert cfg_sp.observation_only is True, (
        "sports observation_only must be True — never live without explicit promotion")
    assert cfg_sp.observation_only == bot.constants.SPORTS_OBSERVATION_ONLY, (
        f"sports obs_only: {cfg_sp.observation_only} != {bot.constants.SPORTS_OBSERVATION_ONLY}")
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

    # ── CalEngine registry safety ──
    # 15M must NEVER use per-market CalEngine (it uses _CALIBRATION_ENGINE directly)
    assert not MARKET_CONFIGS["15m"].cal_engine_enabled, "FATAL: 15m must not use cal_engine_enabled"
    assert MARKET_CONFIGS["15m"].cal_engine_state_path == "", "FATAL: 15m must not set cal_engine_state_path"

    # Hourly cal_engine_enabled must match bot.py constant
    assert MARKET_CONFIGS["hourly"].cal_engine_enabled == bot.constants.HOURLY_CALIBRATION_ENABLED, (
        f"hourly cal_engine_enabled mismatch: {MARKET_CONFIGS['hourly'].cal_engine_enabled} != {bot.constants.HOURLY_CALIBRATION_ENABLED}")

    # ── CalEngine subtype invariants ──
    # Subtypes and single-engine state path are mutually exclusive.
    # cal_engine_enabled + cal_subtypes is OK: enables subtype engines for predictions.
    for pt, cfg in MARKET_CONFIGS.items():
        if cfg.cal_subtypes:
            assert not cfg.cal_engine_state_path, (
                f"FATAL: {pt} has both cal_engine_state_path and cal_subtypes — pick one")

    # No engine may share state file with 15M + all state paths unique
    _all_state_paths = []
    for pt, cfg in MARKET_CONFIGS.items():
        if cfg.cal_engine_state_path:
            assert cfg.cal_engine_state_path != bot.constants.CALIBRATION_STATE_PATH, (
                f"FATAL: {pt} shares state file with 15M engine!")
            _all_state_paths.append(cfg.cal_engine_state_path)
        for sub_code, sub_path in cfg.cal_subtypes.items():
            assert sub_path, f"FATAL: empty state path for {pt}/{sub_code}"
            assert sub_path != bot.constants.CALIBRATION_STATE_PATH, (
                f"FATAL: {pt}/{sub_code} shares state file with 15M!")
            _all_state_paths.append(sub_path)
    assert len(_all_state_paths) == len(set(_all_state_paths)), (
        f"FATAL: duplicate cal engine state paths: {_all_state_paths}")

    # Weather subtypes must match WEATHER_CITIES
    if MARKET_CONFIGS["weather"].cal_subtypes:
        from bot.engines.weather_engine import WEATHER_CITIES  # Sprint 10.1c sibling-reorg (2026-05-11)
        for sub in MARKET_CONFIGS["weather"].cal_subtypes:
            assert sub in WEATHER_CITIES, f"FATAL: weather subtype '{sub}' not in WEATHER_CITIES"

    # Sports subtypes must match SPORT_GROUPS
    if MARKET_CONFIGS["sports"].cal_subtypes:
        from bot.engines.sports_data import SPORT_GROUPS  # Sprint 10.1a sibling-reorg (2026-05-11)
        for sub in MARKET_CONFIGS["sports"].cal_subtypes:
            assert sub in SPORT_GROUPS, f"FATAL: sports subtype '{sub}' not in SPORT_GROUPS"

    import logging
    logging.info("MARKET_CONFIGS: all %d configs validated against constants", len(MARKET_CONFIGS))
