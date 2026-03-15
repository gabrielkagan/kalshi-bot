#!/usr/bin/env python3
"""Kalshi cryptocurrency prediction market trading bot."""

import os
import sys
import re
import time
import json
import uuid
import signal
import sqlite3
import math
import base64
import datetime
from datetime import timezone
import threading
import asyncio
import random
import logging
import inspect
from collections import deque
from typing import Optional, Dict, List, Set, Tuple

import requests
import websockets
from scipy.stats import t as student_t, norminvgauss
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding

from market_config import get_market_config, get_cal_excluded_types, validate_market_configs, MARKET_CONFIGS
from config import *  # noqa: F401,F403 — shared constants (single source of truth)
from models import (  # noqa: F401 — extracted pure-math classes
    EGARCHEstimator, MincerZarnowitzTracker, PositionSizer,
    calculate_fee, calculate_taker_fee, calculate_maker_fee,
    compute_tv_rk_weights, _student_t_e_abs_z, _compute_qlike,
)

# ─── Trading Configuration ───────────────────────────────────────────────────
OBSERVATION_MODE = False           # False = LIVE TRADING with real money
SERIES_TICKERS = {
    "BTC": "KXBTC15M",
    "ETH": "KXETH15M",
    "SOL": "KXSOL15M",
    "XRP": "KXXRP15M",
}
MIN_ENTRY_PRICE = 86              # cents (global floor — SOL uses this; BTC/ETH overridden below)
MAX_ENTRY_PRICE = 99              # cents
BTC_MIN_ENTRY_PRICE = 89          # cents (data: 86-88c below taker BE, 89c is 93.3% WR, +$87 PnL)
ETH_MIN_ENTRY_PRICE = 88          # cents (data: ETH 86-87c is 5W/1L -$23.69, single 87c loss wipes 5 wins)
XRP_MIN_ENTRY_PRICE = 92          # cents (data: XRP PnL negative at every floor <90c, PF=1.68 at >=92c)
XRP_MAX_RISK_PER_TRADE = 0.12    # XRP RK vol systematically underestimates → cap exposure (data: 53W/8L, net -$63)
XRP_15M_SHADOW = False            # XRP 15M promoted to live at 92c+ (data: 41W/2L 95.3% WR at >=92c)
XRP_SHADOW_MIN_PRICE = 88         # Shadow tier: 88c+ subset (86-87c is 84% WR but PnL-negative)
MIN_SECONDS_BEFORE_CLOSE = 0
MAX_SECONDS_BEFORE_CLOSE = 900    # scan 15 min before close (600-900s is shadow data collection)
STC_SHADOW_THRESHOLD = 600        # 15M trades above this STC are shadow-only (data: 500-600s 91.2% WR, +$47 marginal PnL)
ONE_ASSET_PER_WINDOW = False

# ─── Hourly Observation Mode ──────────────────────────────────────────────────
HOURLY_OBSERVATION_ENABLED = True     # Master switch for hourly data collection
HOURLY_OBSERVATION_ONLY = True        # REVERTED: 68.8% WR (need 90%+), 26pp overconfident, -$97 overnight
HOURLY_SERIES_TICKERS = {
    "BTC": "KXBTCD",
    "ETH": "KXETHD",
    "SOL": "KXSOLD",
    "XRP": "KXXRPD",
}
HOURLY_MAX_SECONDS_BEFORE_CLOSE = 1800  # 30 min before close
HOURLY_MIN_SECONDS_BEFORE_CLOSE = 0
HOURLY_MARKET_BLEND_W = 0.40            # Optimal Brier per 134K simulation (0.70 was second-worst)
HOURLY_MIN_ENTRY_PRICE = 50            # Lowered for data collection (was 70)
HOURLY_MAX_RISK_PER_TRADE = 0.15       # Conservative start (60% of 15M's 0.25)

# ─── Hourly Three-Layer Optimization (Researcher Recommendations) ─────────
HOURLY_TEMPERATURE_T = 1.45           # Temperature scaling: softens overconfident probs (T>1 = less confident)
HOURLY_TEMPERATURE_ENABLED = True     # Toggle for temperature scaling
HOURLY_CALIBRATION_ENABLED = False    # Disabled: hourly beta_cal is +44pp overconfident (93.2% predicted vs 49.2% actual, n=455). Passthrough+T=1.45 is nearly perfect (-2pp OC).
HOURLY_MIN_STC_ENTRY = 120            # Min STC for entry (2 min) — expanded for observation data collection
HOURLY_MAX_STC_ENTRY = 3600           # 60 min — expanded for observation data collection
HOURLY_EXCLUDED_ASSETS = set()         # Empty in observation mode — collect all asset data
HOURLY_MAX_POSITIONS_PER_WINDOW = 2   # Max concurrent hourly positions per time window (ENB ~1.3)

# ─── Hourly Config A (shadow promotion candidate) ────────────────────────────
# Filters applied as a SECOND insert (filter_stage='hourly_config_a') alongside
# the unfiltered baseline ('hourly_observation'). Does NOT affect live trading.
# Graduation criteria (all must hold for 7+ days post-filter):
#   - WR ≥ 78%
#   - Wilson 95% CI lower bound ≥ 72%
#   - Brier < 0.25
#   - No single day with WR < 60%
#   - Flat sim PnL positive
HOURLY_CONFIG_A_EXCLUDED = {'XRP'}    # XRP: 42% WR, -$89 sim PnL, 12-33pp below non-XRP every UTC bucket
HOURLY_CONFIG_A_MAX_EDGE = 0.007      # Edge ≤ 0.7%: filters out overconfident high-edge noise (8-15% edge = 32% WR)
HOURLY_MAX_WINDOW_RISK = 0.15         # Max aggregate risk across all hourly positions per window
# ─── BTC 70-89c wl2 Variant (promotion candidate) ───────────────────────────
# Backtest: 67t, 62W/5L, 92.5% WR, flat $7.47, Kelly $92.48, Brier 0.076
# 70-89c tier has 11pp margin over breakeven vs 2.7pp for 86c+
# Graduation: n>=100, WR>=88%, positive PnL, Wilson CI lower >= 3pp above breakeven
HOURLY_CONFIG_B_ASSET = 'BTC'
HOURLY_CONFIG_B_MIN_PRICE = 70
HOURLY_CONFIG_B_MAX_PRICE = 89
HOURLY_CONFIG_B_MAX_PER_WINDOW = 2    # Price-sorted: top 2 by price within window
# ─── Hourly Configs C–G (shadow promotion candidates, Mar 12 2026) ──────────
# Five diverse configs from hourly alpha research. All shadow-only —
# insert as filter_stage='hourly_config_X' alongside the unfiltered baseline.
# Graduation: WR≥72%, Wilson LB≥65%, Brier<0.30, 7+ days, PnL positive.
HOURLY_SHADOW_CONFIGS = [
    {"name": "hourly_config_c", "included_assets": {"BTC", "ETH"}, "min_stc": 600, "max_stc": 1800},
    {"name": "hourly_config_d", "excluded_assets": {"XRP"}, "max_edge": 0.05},
    {"name": "hourly_config_e", "included_assets": {"BTC", "ETH"}, "min_stc": 1200, "max_stc": 1800},
    {"name": "hourly_config_f", "max_edge": 0.012},
    {"name": "hourly_config_g", "included_assets": {"BTC"}, "min_stc": 900, "max_stc": 1800},
    {"name": "hourly_config_h", "temperature": 2.0, "blend_w": 0.0},
    {"name": "hourly_config_i", "included_assets": {"BTC", "ETH"}, "min_stc": 600, "max_stc": 1800, "temperature": 2.0, "blend_w": 0.0},
    {"name": "hourly_config_j", "temperature": 2.5, "blend_w": 0.0},
    {"name": "hourly_config_k", "temperature": 2.0, "blend_w": 0.20},
    {"name": "hourly_config_l", "excluded_assets": {"XRP"}, "temperature": 2.0, "blend_w": 0.0},
    {"name": "hourly_config_m", "included_assets": {"BTC", "ETH"}, "min_stc": 600, "max_stc": 1800, "temperature": 2.5, "blend_w": 0.0},
]
HOURLY_KELLY_FRACTION = 0.25          # Quarter-Kelly: 44% of growth rate, ~3% halving probability

# ─── SPX Hourly Observation Mode ──────────────────────────────────────────────
SPX_HOURLY_ENABLED = True
SPX_HOURLY_OBSERVATION_ONLY = True       # Shadow first — collect data before live
SPX_HOURLY_MIN_ENTRY_PRICE = 70
SPX_HOURLY_MAX_ENTRY_PRICE = 99
SPX_HOURLY_MAX_SECONDS_BEFORE_CLOSE = 1800
SPX_HOURLY_MIN_SECONDS_BEFORE_CLOSE = 300
SPX_HOURLY_MARKET_BLEND_W = 0.40
SPX_HOURLY_MAX_RISK_PER_TRADE = 0.15
SPX_HOURLY_TEMPERATURE_T = 1.0           # Start neutral, tune with data
SPX_HOURLY_KELLY_FRACTION = 0.25
SPX_HOURLY_FEE_MULTIPLIER_TAKER = 0.035  # Finance category: half of crypto's 0.07
SPX_HOURLY_FEE_MULTIPLIER_MAKER = 0.0  # Kalshi charges $0 on maker fills
SPX_HOURLY_MAX_POSITIONS_PER_WINDOW = 2  # Max concurrent SPX positions per hourly window
SPX_HOURLY_MAX_WINDOW_RISK = 0.15        # Max aggregate risk across SPX positions per window

# ─── Weather Observation Mode ─────────────────────────────────────────────────
WEATHER_ENABLED = True
WEATHER_OBSERVATION_ONLY = True
WEATHER_MIN_ENTRY_PRICE = 10
WEATHER_MAX_ENTRY_PRICE = 99
WEATHER_MAX_SECONDS_BEFORE_CLOSE = 86400  # Weather settles daily — always eligible
WEATHER_MIN_SECONDS_BEFORE_CLOSE = 3600   # At least 1 hour before settlement
WEATHER_MIN_STC_ENTRY = 3600.0           # 1h min for shadow trade signals
WEATHER_MAX_STC_ENTRY = 43200.0          # 12h max — audit: 4-12h calibrated, 12h+ catastrophic
WEATHER_MAX_RISK_PER_TRADE = 0.10
WEATHER_KELLY_FRACTION = 0.25
WEATHER_MARKET_BLEND_W = 0.20            # 80% model, 20% market (ensemble is primary signal)
WEATHER_MIN_EDGE_PCT = 0.001             # 0.1% — very low for max signal collection (observation-only)
WEATHER_CAL_ENGINE_ENABLED = True        # Per-city CalEngines learning in shadow
# ─── Weather Shadow Variants (Mar 12 2026) ──────────────────────────────────
# Two focused shadow configs alongside the uncapped baseline (weather_observation).
# Research: model well-calibrated <25% predicted (≤30c), catastrophically overconfident >40%.
#   - Capped30: price ≤30c — restricts to calibrated regime (+0.8pp to +4.3pp gap)
#   - ShortSTC: STC ≤8h — ensemble freshest, 46.7% WR vs 18.4% for 16-24h
# Both insert as filter_stage='weather_shadow_X' alongside uncapped baseline.
# Graduation: WR above breakeven, Wilson CI lower > BE, 30+ days, PnL positive.
WEATHER_SHADOW_CONFIGS = [
    {"name": "weather_shadow_capped30", "max_price": 30},
    {"name": "weather_shadow_short_stc", "max_stc": 28800},  # 8 hours
    {"name": "weather_shadow_capped30_short_stc", "max_price": 30, "max_stc": 28800},  # both filters
]
# Weather NO-side shadow: model overconfident on YES (+25.5pp at 75-90% bucket) → strong NO signal.
# Signal fires when YES prob ≥ 55% (cheap NO contracts) and NO edge after fees is positive.
# Fixed 1-contract sizing (Kelly oversizes on low-edge NO signals).
WEATHER_NO_SHADOW_MIN_YES_PROB = 0.55  # Only shadow when model is confident YES (NO is cheap)
# Weather NO-side live execution — bypasses WEATHER_OBSERVATION_ONLY for NO-side only.
# YES-side remains fully gated by WEATHER_OBSERVATION_ONLY = True.
# Data: 397 settled, 73.6% WR, +$181 sim PnL, 40pp+ cushion above breakeven.
# Gate: STC >= 8h (short STC NO loses), fixed 1-contract sizing, all 19 cities.
WEATHER_NO_SIDE_LIVE = False             # Kill switch — flip True on March 20 after 14-day gate
WEATHER_NO_SIDE_MIN_STC = 28800.0        # 8 hours — short STC NO-side loses money
HOURLY_MIN_EDGE_PCT = 0.001              # 0.1% — low for max signal collection (observation-only)

# ─── Sports Comeback Observation Mode ────────────────────────────────────
SPORTS_ENABLED = True
SPORTS_OBSERVATION_ONLY = True         # HARDCODED — never live without explicit promotion

# ─── API Configuration ───────────────────────────────────────────────────────
BASE_URL = ("https://api.elections.kalshi.com" if os.environ.get("KALSHI_ENV") == "production"
            else "https://demo-api.kalshi.co")
API_PATH_PREFIX = "/trade-api/v2"
READ_RATE_LIMIT = 30              # per second (Advanced tier)
WRITE_RATE_LIMIT = 30             # per second (Advanced tier)

# ─── File Paths ──────────────────────────────────────────────────────────────
DB_PATH = "state.db"
SCAN_JOURNAL = "scan_journal.jsonl"
TRADE_JOURNAL = "trade_journal.jsonl"
SETTLEMENT_JOURNAL = "settlement_journal.jsonl"
ORDER_JOURNAL = "order_journal.jsonl"
REJECTION_JOURNAL = "rejection_journal.jsonl"
OPPORTUNITY_JOURNAL = "opportunity_journal.jsonl"
EXECUTION_JOURNAL = "execution_journal.jsonl"
PERFORMANCE_JOURNAL = "performance_journal.jsonl"
FILL_MODEL_JOURNAL = "fill_model_journal.jsonl"

# ─── Loop Timing ─────────────────────────────────────────────────────────────
SCAN_INTERVAL_SECONDS = 1.0
MARKET_REFRESH_SECONDS = 30.0
SETTLEMENT_CHECK_SECONDS = 30.0

# ─── Coinbase WebSocket ──────────────────────────────────────────────────────
COINBASE_WS_URL = "wss://ws-feed.exchange.coinbase.com"
COINBASE_PRODUCTS = {
    "BTC": "BTC-USD",
    "ETH": "ETH-USD",
    "SOL": "SOL-USD",
    "XRP": "XRP-USD",
}
PRICE_BUFFER_SIZE = 300           # 5 minutes of 1-second snapshots

# ─── Volatility Engine ───────────────────────────────────────────────────────
# VOL_RETURN_INTERVAL → config.py
VOL_WINDOW_1MIN = 12              # 60s / 5s = 12 returns
VOL_WINDOW_5MIN = 60              # 300s / 5s = 60 returns
VOL_WINDOW_15MIN = 180            # 900s / 5s = 180 returns
VOL_BLEND_WEIGHTS = (0.5, 0.3, 0.2)  # 1min, 5min, 15min
RK_TV_SHADOW_MODE = False             # PROMOTED: time-varying RK weights drive live blend
JUMP_THRESHOLD_MULTIPLIER = 3.0   # return > 3x RV = jump

JUMP_DECAY_TAU = 432.7               # 300/ln(2), half-life = 300s
JUMP_DECAY_MAX_BOOST = 1.0           # boost starts at 1.0 (total = 2.0×)
JUMP_DECAY_MIN_BOOST = 0.01          # below this = regime "normal"
JUMP_MAX_HISTORY = 10                # max jump events per asset

# ─── Adaptive Jump Detection (Tier System) ────────────────────────────────
JUMP_ADAPTIVE_SHADOW_MODE = False       # False = adaptive drives regime, legacy at DEBUG
JUMP_ADAPTIVE_SUBSAMPLE = 3             # Every 3rd 5s tick = 15s returns
JUMP_ADAPTIVE_EWMA_LAMBDA = 0.94       # EWMA decay for variance
JUMP_ADAPTIVE_EWMA_INIT_RETURNS = 10   # Min 15s returns before EWMA trusted
JUMP_ADAPTIVE_PCTILE_WINDOW = 180      # 180 × 15s = 45 min rolling window
JUMP_ADAPTIVE_PCTILE_LEVEL = 0.995     # 99.5th percentile
JUMP_ADAPTIVE_SIGMA_MULT = 4.0         # |r| > 4σ_EWMA threshold
JUMP_ADAPTIVE_PCTILE_MIN_OBS = 30      # Min obs before percentile trusted
JUMP_ADAPTIVE_DECAY_TAU = 64.93        # 45/ln(2), half-life = 45s
JUMP_ADAPTIVE_DECAY_MAX_BOOST = 1.5    # Base boost per jump (magnitude-scaled)
JUMP_ADAPTIVE_DECAY_MIN_BOOST = 0.01   # Below this = "normal"
JUMP_ADAPTIVE_DECAY_CAP = 5.0          # Max total multiplier
JUMP_ADAPTIVE_MAG_SCALE_BASE = 4.0     # Magnitude scaling denominator
JUMP_ADAPTIVE_MAG_CAP = 3.0            # Cap magnitude ratio at 3x
JUMP_ADAPTIVE_MAX_HISTORY = 10         # Max events per asset
JUMP_ADAPTIVE_STATE_PATH = "jump_adaptive_state.json"
JUMP_ADAPTIVE_SAVE_INTERVAL = 300.0    # Save EWMA/percentile state every 5 min

# EGARCH, MZ, blend constants → config.py (imported via `from config import *`)

# ─── Adaptive RK Bandwidth (BN 2008/2009) ─────────────────────────────────
RK_ADAPTIVE_SHADOW_MODE = False          # False = adaptive H* drives blended_rv
RK_CSTAR_FLAT_TOP_PARZEN = 3.5134       # c* for flat-top Parzen kernel (BN 2009 Table 2)
RK_NOISE_VAR_FLOOR = 1e-20              # ω² floor (prevents zero/negative)
RK_BANDWIDTH_MAX_FRACTION = 1 / 3       # H* cap as fraction of n
RK_MIN_RETURNS_FOR_ADAPTIVE = 20        # need ≥20 returns for reliable γ̂(1)

# ─── Deribit DVOL Integration ────────────────────────────────────────────────
DERIBIT_DVOL_URL = "https://www.deribit.com/api/v2/public/get_volatility_index_data"
DERIBIT_DVOL_CURRENCIES = {"BTC": "BTC", "ETH": "ETH"}
DVOL_FETCH_INTERVAL = 60.0        # seconds between DVOL fetches
DVOL_CACHE_TTL = 120.0            # stale after 2 min
DVOL_HOURLY_AVG_MAXLEN = 60       # 60 fetches × 60s = ~1h rolling window
DVOL_HOURLY_AVG_MIN = 3           # Need ≥3 samples for meaningful average
DVOL_REQUEST_TIMEOUT = 5.0
# annualized → per-5-second: 1/sqrt(SECONDS_PER_YEAR / VOL_RETURN_INTERVAL)
# (computed after SECONDS_PER_YEAR is defined below)

# ─── IV-RV Regime Detection ──────────────────────────────────────────────────
IV_RV_SPREAD_THRESHOLD = 0.50     # if IV > RV by 50%, shift toward IV
BETA_LOOKBACK_RETURNS = 60        # 5 min of returns for cross-asset beta

# ─── Cross-Exchange Order Flow ──────────────────────────────────────────
CROSS_EXCHANGE_ENABLED = True
CROSS_EXCHANGE_SYMBOLS = {
    "BTC": {"binance": "btcusdt", "kraken": "BTC/USD", "bybit": "BTCUSDT"},
    "ETH": {"binance": "ethusdt", "kraken": "ETH/USD", "bybit": "ETHUSDT"},
    "SOL": {"binance": "solusdt", "kraken": "SOL/USD", "bybit": "SOLUSDT"},
    "XRP": {"binance": "xrpusdt", "kraken": "XRP/USD", "bybit": "XRPUSDT"},
}
BINANCE_WS_URL = "wss://stream.binance.com:9443/stream"
KRAKEN_WS_URL = "wss://ws.kraken.com/v2"
BYBIT_WS_URL = "wss://stream.bybit.com/v5/public/spot"
CROSS_EXCHANGE_BUFFER_SIZE = 15
CROSS_EXCHANGE_LEAD_THRESHOLD = 0.002     # 0.2% for single-exchange lead
CROSS_EXCHANGE_CONSENSUS_THRESHOLD = 0.003  # 0.3% for consensus
CROSS_EXCHANGE_CONSENSUS_MIN = 3
CROSS_EXCHANGE_STALE_SECONDS = 30.0

# ─── CoinGlass Derivatives ─────────────────────────────────────────────
COINGLASS_API_URL = "https://open-api-v3.coinglass.com/api"
COINGLASS_FETCH_INTERVAL = 600.0          # 10 min (100 calls/day budget)
COINGLASS_CACHE_TTL = 900.0               # stale after 15 min
COINGLASS_REQUEST_TIMEOUT = 10.0
COINGLASS_SYMBOLS = {"BTC": "BTC", "ETH": "ETH", "SOL": "SOL", "XRP": "XRP"}
FUNDING_RATE_EXTREME = 0.0005             # 0.05%/8h
FUNDING_RATE_ELEVATED = 0.0003            # 0.03%/8h

# ─── Order Flow Adjustments ────────────────────────────────────────────
OFA_CONSENSUS_BOOST = 0.02                # +2pp when 3+ exchanges confirm direction
OFA_CONSENSUS_REDUCE = -0.02              # -2pp when 3+ exchanges oppose direction
OFA_LEAD_BOOST = 0.01                     # +1pp for weaker single-exchange lead
OFA_EXTREME_FUNDING_REDUCE = -0.015       # -1.5pp for extreme funding
OFA_ELEVATED_FUNDING_REDUCE = -0.005      # -0.5pp for elevated funding
OFA_MAX_ADJUSTMENT = 0.03                 # cap total at +/-3pp

# ─── Kalshi Orderbook Flow Tracking ──────────────────────────────────────
KALSHI_OFT_ENABLED = True
KALSHI_OFT_SHADOW_MODE = True          # True = compute & log, don't affect prob_adjustment
KALSHI_OFT_BUFFER_SIZE = 60            # 60 snapshots × ~1s = ~1 minute history per ticker
KALSHI_OFT_MIN_SNAPSHOTS = 5           # Need ≥5 snapshots before computing signals
KALSHI_OFT_STALE_SECONDS = 120.0       # Evict tickers inactive for 2 minutes
KALSHI_OFT_IMBALANCE_STRONG = 0.7      # bid_qty / total_qty ≥ 0.7 = strong buy pressure
KALSHI_OFT_IMBALANCE_WEAK = 0.3        # bid_qty / total_qty ≤ 0.3 = strong sell pressure
KALSHI_OFT_DEPTH_DRAIN_PCT = -0.5      # Depth shrinking >50% over window = drain signal
KALSHI_OFT_LOG_INTERVAL = 300.0        # Log OFT diagnostics every 5 min

# Kalshi OFT probability adjustments (shadow mode initially)
OFA_KALSHI_IMBALANCE_BOOST = 0.01      # +1pp for strong buy imbalance
OFA_KALSHI_IMBALANCE_REDUCE = -0.01    # -1pp for strong sell imbalance
OFA_KALSHI_DEPTH_DRAIN_BOOST = 0.005   # +0.5pp when depth draining (convergence signal)
OFA_KALSHI_CONVERGENCE_BOOST = 0.005   # +0.5pp for rapid ask convergence (>0.5¢/s)

# Probability engine constants (SECONDS_PER_YEAR, DVOL_ANNUALIZED_TO_5S, STUDENT_T_DF,
# BETA_SLOPE, MAX_EFFECTIVE_PROB, NUMERICAL_SAFETY_CEILING) → config.py

# ─── Calibration Engine ─────────────────────────────────────────────────────
CALIBRATION_STATE_PATH = "calibration_state.json"
HOURLY_CALIBRATION_STATE_PATH = "hourly_calibration_state.json"
CALIBRATION_MIN_SAMPLES_PLATT = 200
CALIBRATION_MIN_SAMPLES_BETA = 350   # lowered from 500 (we have 370+ obs)
CALIBRATION_MIN_SAMPLES_BLR = 50
CALIBRATION_RETRAIN_INTERVAL = 3600    # seconds between retrain checks
CALIBRATION_BRIER_WINDOW = 500         # rolling Brier over last N outcomes

# Dynamic probability cap schedule (keyed by seconds_remaining)
# As expiry approaches, allow higher confidence from the model
DYNAMIC_CAP_SCHEDULE = [
    (600, 0.93),   # > 10 min: status quo cap
    (300, 0.95),   # 5–10 min: slightly relaxed
    (120, 0.97),   # 2–5 min: moderately relaxed
    (60,  0.985),  # 1–2 min: high confidence allowed
    (0,   0.995),  # < 1 min: near-certain allowed
]

# Hourly markets: 60-min windows. 1800s = 30 min out (scan start).
# At 30 min, deep-ITM hourly strikes are genuinely 95%+ likely.
# The 15M caps (0.93 at >10min) are too conservative for hourly.
# Data: 40 settled hourly insufficient_edge trades, 40W/0L (100% WR).
HOURLY_DYNAMIC_CAP_SCHEDULE = [
    (1800, 0.97),  # > 30 min: allow up to 97%
    (900,  0.98),  # 15-30 min
    (300,  0.99),  # 5-15 min
    (60,   0.995), # 1-5 min
    (0,    0.999), # < 1 min
]

MARKET_BLEND_W = 0.40             # 60% model, 40% market (data: model underconfident 0.8-2.1pp at 90%+)
ENDGAME_BLEND_PRICE = 96         # don't blend at or above this price (preserve endgame edge)

# ─── Shadow Calibration Pipeline ──────────────────────────────────────────────
SHADOW_CAL_PIPELINE = True   # REVERTED: no-blend system runs in shadow for monitoring
SHADOW_BLEND_W = 0.50        # Counterfactual: old system used 50% market blend
SHADOW_TEMP_SCALE = True     # Use temperature scaling instead of Beta Cal

Z_SCORE_MAX = 25.0                # refuse to trade if |z| > 25 (data: 0 losses in tradeable range up to z=25)
DISCREPANCY_PROB = 0.90           # model says >90% but...
DISCREPANCY_PRICE = 75            # ...market is below 75¢ → refuse

# DIST_CONFIG, _load_dist_config → config.py

_CALIBRATION_ENGINE: Optional["CalibrationEngine"] = None   # 15M ONLY — DO NOT TOUCH
_CAL_REGISTRY: Dict[str, "CalibrationEngine"] = {}          # non-15M engines by product_type
_TELEGRAM: Optional["TelegramNotifier"] = None


def _derive_subtype(product_type: str, asset: Optional[str]) -> Optional[str]:
    """Extract CalEngine subtype code from asset string.
    Weather: 'NYC_TEMP' → 'NYC'.  Sports: 'NBA' → 'basketball' (via sport_group)."""
    if not asset:
        return None
    if product_type == "weather":
        return asset.replace("_TEMP", "") if "_TEMP" in asset else None
    if product_type == "sports":
        try:
            from sports_data import LEAGUES
            for _lcfg in LEAGUES.values():
                if _lcfg.display_name == asset:
                    return _lcfg.sport_group
        except ImportError:
            pass
        logging.debug("_derive_subtype: no sport_group for asset=%s", asset)
        return None
    return None


def _derive_asset_filter(product_type: str, subtype_code: str):
    """Map subtype code back to DB asset filter for load_training_data_from_db().
    Returns str for single-asset types, list for multi-league sport groups."""
    if product_type == "weather":
        return f"{subtype_code}_TEMP"  # "NYC" → "NYC_TEMP"
    if product_type == "sports":
        try:
            from sports_data import LEAGUES
            return [lcfg.display_name for lcfg in LEAGUES.values()
                    if lcfg.sport_group == subtype_code]
        except ImportError:
            return None
    return None


def _resolve_cal_engine(product_type: Optional[str],
                        asset: Optional[str] = None,
                        require_enabled: bool = False) -> Optional["CalibrationEngine"]:
    """Look up the correct CalibrationEngine for a (product_type, asset) pair.
    Returns None for 15M (uses _CALIBRATION_ENGINE directly).
    require_enabled=True: also returns None if cal_engine_enabled=False."""
    if product_type in (None, "15m"):
        return None
    _cfg = get_market_config(product_type)
    if require_enabled and not _cfg.cal_engine_enabled:
        return None
    # Types with subtypes: derive composite key
    if _cfg.cal_subtypes and asset:
        _sub = _derive_subtype(product_type, asset)
        if _sub:
            return _CAL_REGISTRY.get(f"{product_type}_{_sub}")
    # Bare product_type key (hourly, spx_hourly, or no subtypes match)
    return _CAL_REGISTRY.get(product_type)


# ─── Opportunity Scanner ────────────────────────────────────────────────────
MIN_EDGE_PCT = 0.25               # flat fallback — matches lowest MIN_EDGE_BY_PRICE tier (was 0.7)

# Weekend Edge Discount — shadow-only counterfactual for Sat/Sun quiet markets
# When RV drops on weekends, model edges shrink below thresholds even though WR stays high.
# This logs what WOULD have traded at relaxed thresholds for graduation analysis.
# Graduation criteria (shadow → live):
#   - 4-6 weekends of data (~80-120 settled signals)
#   - WR >= 85% on settled markets
#   - No single asset dragging below 75% WR
#   - No edge inversion (lower tiers not dragging overall)
WEEKEND_EDGE_DISCOUNT = 0.60      # multiply MIN_EDGE_BY_PRICE by this on Sat/Sun
OVERNIGHT_EDGE_DISCOUNT = 0.60    # multiply MIN_EDGE_BY_PRICE by this during overnight quiet hours (04-11 UTC)
OVERNIGHT_QUIET_START = 4         # UTC hour — quiet zone starts (inclusive)
OVERNIGHT_QUIET_END = 11          # UTC hour — quiet zone ends (inclusive)

# ─── Overnight Low-Price Shadow ──────────────────────────────────────────
# Thesis: overnight market makers are slow/absent, so 50-85c YES contracts
# have stale pricing — model correctly puts outcomes at 90%+ probability.
# Settlement data: 50-69c overnight @ cal_prob>0.80 → 89.9% WR (n=325).
# Shadow-only — collects data for graduation analysis before live trading.
# Graduation criteria:
#   - ≥80 settled signals
#   - ≥85% WR on taker simulation
#   - No single asset below 75% WR (n≥10)
#   - Positive Kelly-sized sim PnL on taker
#   - No edge inversion by price tier
#   - ≥10 overnight sessions of data
OVERNIGHT_LP_SHADOW = True
OVERNIGHT_LP_MIN_ENTRY_PRICE = 50   # Lowest YES price to shadow-evaluate
OVERNIGHT_LP_MAX_ENTRY_PRICE = 85   # Highest (live 86+ pipeline unchanged)
OVERNIGHT_LP_MIN_CAL_PROB = 0.82    # Higher than live — forces high model confidence in untested price territory
OVERNIGHT_LP_MIN_EDGE_PCT = 0.10    # 10% min — symmetric payoffs need bigger edge than 86+c asymmetric
OVERNIGHT_LP_MAX_RISK_PER_TRADE = 0.10  # 10% (vs 25% live) — tighter for symmetric payoff
OVERNIGHT_LP_KELLY_FRACTION = 0.125 # Eighth-Kelly — extra conservative (calibration trained on 86-99c)
OVERNIGHT_LP_MIN_STC = 120         # At least 2 min to close (avoid scramble)
OVERNIGHT_LP_MAX_STC = 600         # Max 10 min — trade when outcome is nearly decided
OVERNIGHT_LP_HOURS_START = 0       # UTC hour — overnight LP window start (inclusive)
OVERNIGHT_LP_HOURS_END = 12        # UTC hour — overnight LP window end (exclusive)
OVERNIGHT_LP_VOL_SPIKE_MULT = 2.0  # Circuit breaker: skip if trailing vol > 2x overnight median
OVERNIGHT_LP_VOL_HISTORY_DAYS = 7  # Days of overnight vol history for median computation

# ─── Low-STC Sizing Cap (Fix #3) ─────────────────────────────────────────
# Data: 0-100s STC is -$84/14d (12W/2L). Catastrophic losses at very short STC
# wipe all gains. Halve position to limit downside on last-second reversals.
LOW_STC_SIZING_CAP = 0.50           # position multiplier when STC < threshold
LOW_STC_SIZING_CAP_THRESHOLD = 100  # seconds — apply cap below this STC

# ─── Decided Contract Shadow (Fix #2) ─────────────────────────────────────
# When z-score is very negative (spot far above strike) with short STC,
# the contract is essentially decided but the EGARCH pipeline can't compute
# edge because calibration squashes probability below market price.
# T1: z ≤ -5 → 32/32 = 100% WR. T2: z ≤ -3 at 93-96c → 52/54 = 96.3% WR.
DECIDED_CONTRACT_SHADOW = os.environ.get("DECIDED_CONTRACT_SHADOW", "1") == "1"
DECIDED_CONTRACT_Z_T1 = -5.0       # Tier 1 z threshold
DECIDED_CONTRACT_Z_T2 = -3.0       # Tier 2 z threshold (narrower price range)
DECIDED_CONTRACT_MIN_PRICE = 93     # Minimum ask price (cents) for decided signal
DECIDED_CONTRACT_T2_MAX_PRICE = 96  # T2 only applies up to 96c
DECIDED_CONTRACT_MAX_STC = 300      # Only within 5 minutes of close
# ── Decided Contract LIVE overlay ──
# Incremental strategy on top of main pipeline. Env-var kill switches (no deploy needed).
DECIDED_T1_ENABLED = os.environ.get("DECIDED_T1_ENABLED", "0") == "1"
DECIDED_T2_ENABLED = os.environ.get("DECIDED_T2_ENABLED", "0") == "1"
DECIDED_CONTRACT_RISK = 0.125               # Fixed 12.5% bankroll per signal
DECIDED_CONTRACT_MAX_WINDOW_RISK = 0.25     # 25% bankroll cap per settlement window

# ─── Relaxed Edge Shadow (Fix #1) ──────────────────────────────────────
# Edge thresholds at 88-93c may be too conservative. Data shows rejected trades
# at these prices win well above breakeven: 88c=97.2% WR, 89c=93.3%, 91c=94.4%.
# Shadow with halved thresholds to validate before promoting.
RELAXED_EDGE_SHADOW = os.environ.get("RELAXED_EDGE_SHADOW", "1") == "1"
RELAXED_EDGE_DISCOUNT = 0.50        # 50% of normal edge threshold (halved)
RELAXED_EDGE_MIN_PRICE = 88         # Lower bound of relaxed range
RELAXED_EDGE_MAX_PRICE = 93         # Upper bound (exclusive — 93+ has stricter thresholds for good reason)

# Price-dependent minimum edge: higher prices have worse asymmetry
# At 95c: 1 loss = 19 wins. At 87c: 1 loss = 6.7 wins.
MIN_EDGE_BY_PRICE = [
    (97, 0.020),   # 97-99c: need 2.0% edge (was 4.0% — halved: grid search 40W/1L at 0-0.7% edge)
    (95, 0.0125),  # 95-96c: need 1.25% edge (was 2.5% — halved)
    (93, 0.009),   # 93-94c: need 0.9% edge (was 1.8% — halved: 3 rejected winners at 0.95-1.23%)
    (91, 0.0035),  # 91-92c: need 0.35% edge (was 0.7% — halved)
    (89, 0.0025),  # 89-90c: need 0.25% edge (was 0.5% — halved: 2 rejected winners at 0.31-0.48%)
    (0,  0.0025),  # 86-88c: need 0.25% edge (was 0.5% — halved: 2 rejected winners at 0.26-0.49%)
]

def get_min_edge(entry_price_cents: int) -> float:
    """Return minimum fee-adjusted edge for a given entry price."""
    for price_floor, min_edge in MIN_EDGE_BY_PRICE:
        if entry_price_cents >= price_floor:
            return min_edge
    return 0.005

ORDERBOOK_CACHE_TTL = 5.0         # seconds to cache orderbook responses
MAX_OB_FETCHES_PER_TICK = 6       # cap API calls for orderbooks per tick (Advanced tier)
BALANCE_CACHE_TTL = 10.0          # seconds to cache balance

# Position sizing constants (SIZING_TIERS, DRAWDOWN_*, MAX_RISK_PER_TRADE) → config.py

# ─── Order Execution ──────────────────────────────────────────────────────
MAKER_PRICE_OFFSET = 1            # cents below fair value for maker orders
MAKER_POLL_INTERVAL = 2.0         # poll for maker fills every 2 seconds
ESCALATION_MAX_ENTRY = 99         # taker price cap during escalation (cents)
CONVERGENCE_WINDOW_SECONDS = 30.0 # seconds to measure price velocity
MAKER_TIMEOUT_SECONDS = 30.0     # hard timeout for maker orders

# ─── Direct Taker Threshold ──────────────────────────────────────────────
DIRECT_TAKER_THRESHOLD = 180.0    # seconds_to_close below this → skip maker, go IOC directly
                                  # Raised 75→180: 0% maker fill rate (26/26 escalated to taker), 9 missed candidates/day
MAKER_ONLY_THRESHOLD = 0.0        # seconds_to_close below this → maker only, no taker escalation
                                  # Set to 0: taker allowed at all STC (data: 14W/0L, 100% taker WR)
                                  # Was 90.0 — removed after verifying taker has zero losses

# ─── Per-Asset Taker Override ──────────────────────────────────────────
SOL_TAKER_FIRST = True            # SOL: bypass maker entirely, go direct IOC at all STC
                                  # Data: 44.7% maker fill rate, $101/wk missed, 95% unfilled WR
                                  # Taker fee delta ~$2/wk vs $101 missed — clear win

# ─── Adaptive Escalation ─────────────────────────────────────────────────
ESCALATION_WAIT_LONG = 15.0       # maker wait when >=180s to close
BTC_ESCALATION_WAIT_OVERRIDE = 7.0  # BTC: 7s instead of 15s at STC>=180s
                                    # Data: ask_confirmed avg 2.7s, escalation_wait avg 19.5s, slip 3.4c
ESCALATION_WAIT_MEDIUM = 7.0      # maker wait when 120-180s to close (86% fills within 7s)
ESCALATION_WAIT_SHORT = 5.0       # maker wait when 60-120s to close
EARLY_ESCALATION_MIN_MOVE = 2      # ask must move ≥2¢ above maker price to trigger

# ─── Post-only rejection → taker escalation ────────────────────────────
POST_ONLY_MAX_SAME_PRICE = 2          # Tier 1: max attempts at same maker price before degrading
POST_ONLY_DEGRADED_EXTRA_OFFSET = 1   # Tier 2: extra ¢ offset for degraded maker attempt
POST_ONLY_REJECTION_EXPIRY = 30.0     # Seconds before rejection count resets (stale data guard)

# ─── Confirmation Addon ─────────────────────────────────────────────────
ADDON_ENABLED = True
ADDON_MIN_PRICE_IMPROVEMENT = 3       # cents improvement from entry to trigger
ADDON_MIN_SECONDS_SINCE_FILL = 10.0   # seconds after fill before addon eligible
ADDON_MIN_STC_REMAINING = 45.0        # need ≥45s remaining at addon time
ADDON_SIZE_FRACTION = 0.50            # addon = 50% of original count
ADDON_MAX_PER_POSITION = 1            # max 1 addon per position
ADDON_MAX_ENTRY_PRICE = 98            # 98¢ cap — still profitable after fees

# ─── Dip Addon ────────────────────────────────────────────────────────
DIP_ADDON_ENABLED = True
DIP_ADDON_SHADOW_MODE = True              # PHASE 1: Log only, don't execute

# ─── Price Shadow — edge data for 70-85c markets ──────────────────────
PRICE_SHADOW_ENABLED = True        # Shadow-evaluate POR for edge data collection
PRICE_SHADOW_FLOOR = 70            # Lowest price to shadow-evaluate
NO_SIDE_MIN_ENTRY_PRICE = 5        # Lowest NO price for shadow data collection (all product types)
DIP_ADDON_MIN_DROP_CENTS = 3              # ask must drop ≥3¢ below entry
DIP_ADDON_MIN_SECONDS_SINCE_FILL = 5.0   # wait after fill before eligible
DIP_ADDON_MIN_STC_REMAINING = 90.0       # need ≥90s (aligns with maker-only threshold)
DIP_ADDON_SIZE_FRACTION = 0.50            # addon = 50% of original count
DIP_ADDON_MAX_PER_POSITION = 1            # max 1 dip addon per position
DIP_ADDON_MAX_TOTAL_RISK = 0.35           # original + addon ≤ 35% of bankroll
DIP_ADDON_MIN_ENTRY_PRICE = 80            # lowered to match hourly floor (80¢)
DIP_ADDON_SHADOW_FLOOR = 50              # shadow logs ALL dips down to 50¢ for data collection


# Fee helpers, compute_tv_rk_weights → imported from models.py

# ── FP / Dollar String Helpers ──────────────────────────────────────────────
def dollars_str_to_cents(s) -> int:
    """Convert dollar string like '0.8800' to integer cents (88)."""
    if s is None:
        return 0
    return round(float(s) * 100)


def cents_to_dollars_str(cents: int) -> str:
    """Convert integer cents (88) to dollar string '0.8800'."""
    return f"{cents / 100:.4f}"


def fp_str_to_int(s) -> int:
    """Convert FP string like '5.00' to integer (5)."""
    if s is None:
        return 0
    return int(round(float(s)))


def int_to_fp_str(n: int) -> str:
    """Convert integer (5) to FP string '5.00'."""
    return f"{n:.2f}"


# ═════════════════════════════════════════════════════════════════════════════
#  Execution Strategy Engine
# ═════════════════════════════════════════════════════════════════════════════

# Strategy constants — return values from evaluate_execution_strategy()
STRATEGY_WAIT = "WAIT"
STRATEGY_MAKER_PATIENT = "MAKER_PATIENT"
STRATEGY_MAKER_AGGRESSIVE = "MAKER_AGGRESSIVE"
STRATEGY_TAKER_NOW = "TAKER_NOW"
STRATEGY_PANIC_CAPTURE = "PANIC_CAPTURE"


def evaluate_execution_strategy(market_data: Dict) -> Tuple[str, Dict]:
    """Intelligent decision engine that evaluates current conditions and
    returns the optimal execution strategy.

    Args:
        market_data: Dict with keys:
            z_score (float): signed z-score from ProbabilityEngine
            calibrated_prob (float): calibrated win probability
            spot (float): current spot price
            threshold (float): strike/threshold price
            seconds_to_close (float): seconds remaining until close
            blended_rv (float): blended realized volatility
            vol_regime (str): "normal" or "elevated"
            best_yes_ask (int|None): current best ask in cents
            best_ask_depth (int): contracts at best ask level
            total_ob_depth (int): total orderbook depth (contracts)
            convergence_velocity (float): upward ask movement in cents/30s
            edge (float): calibrated_prob - market_price/100
            min_entry_price (int): product-type min entry price in cents
            max_entry_price (int): product-type max entry price in cents

    Returns:
        (strategy, scores) where strategy is one of STRATEGY_* constants
        and scores is a diagnostic dict with the component scores.
    """
    z = abs(market_data.get("z_score", 0))
    remaining = market_data.get("seconds_to_close", 999)
    best_ask = market_data.get("best_yes_ask")
    ask_depth = market_data.get("best_ask_depth", 999)
    total_depth = market_data.get("total_ob_depth", 999)
    velocity = market_data.get("convergence_velocity", 0)
    _min_price = market_data.get("min_entry_price", MIN_ENTRY_PRICE)
    _max_price = market_data.get("max_entry_price", MAX_ENTRY_PRICE)
    vol_regime = market_data.get("vol_regime", "normal")
    edge = market_data.get("edge", 0)
    spot = market_data.get("spot", 0)
    threshold = market_data.get("threshold", 0)
    blended_rv = market_data.get("blended_rv", 0)

    # ── 1. Outcome Certainty Score (0-10) ─────────────────────────────────
    # z-score contribution: maps |z| 0→0, 2→3, 3→5, 4→7, 5+→9
    certainty_z = min(9.0, z * 1.8)

    # Distance from threshold: how far is spot from strike in vol terms?
    # If spot is far above threshold (for "above" bets), outcome is more certain
    certainty_distance = 0.0
    if spot > 0 and blended_rv > 0 and remaining > 0:
        sigma_move = spot * blended_rv * math.sqrt(remaining / 5.0)
        if sigma_move > 0:
            # How many sigma away is threshold? (positive = spot above threshold)
            dist_sigma = (spot - threshold) / sigma_move
            # Map: 0σ→0, 1σ→2, 2σ→4, 3σ→6, 4+σ→8
            certainty_distance = min(8.0, max(0.0, dist_sigma * 2.0))

    # Vol trend: collapsing vol = more certain, spiking = less certain
    certainty_vol_adj = 0.0
    if vol_regime == "elevated":
        certainty_vol_adj = -1.5  # spiking vol = less certain

    certainty_score = min(10.0, max(0.0,
        0.5 * certainty_z + 0.4 * certainty_distance + 0.1 * 5.0
        + certainty_vol_adj
    ))

    # ── 2. Orderbook State Score (0-10) ───────────────────────────────────
    # Higher score = more urgency to take (thin/converging book)
    if best_ask is None:
        # Empty orderbook = extreme signal
        ob_score = 10.0
    else:
        # Depth score: fewer contracts = more urgent to take
        # 0 contracts → 10, 10 → 5, 50+ → 0
        depth_score = max(0.0, 10.0 - ask_depth * 0.2)

        # Total book thinness: <20 contracts total = very thin
        book_thin_score = max(0.0, min(10.0, (50 - total_depth) * 0.25))

        # Price level: higher ask = more converged = more urgent
        # 85¢→0, 90¢→3, 95¢→7, 99¢→10
        price_score = max(0.0, min(10.0, (best_ask - 85) * 0.71))

        # Convergence velocity: ask moving up fast
        velocity_score = min(10.0, max(0.0, velocity * 1.5))

        ob_score = (0.25 * depth_score + 0.20 * book_thin_score
                    + 0.25 * price_score + 0.30 * velocity_score)

    # ── 3. Urgency Score (0-10) ───────────────────────────────────────────
    # NOT a hard cutoff — continuous function of time remaining
    # 240s→1, 180s→2.5, 90s→5.5, 60s→6.8, 30s→8.5, 15s→9.5
    if remaining <= 0:
        urgency_time = 10.0
    elif remaining >= MAX_SECONDS_BEFORE_CLOSE:
        urgency_time = 1.0
    else:
        # Exponential curve: more urgency as time shrinks
        urgency_time = 10.0 - 9.0 * (remaining / MAX_SECONDS_BEFORE_CLOSE) ** 0.6

    # Combine time urgency with convergence signal
    urgency_convergence = min(3.0, velocity * 0.5)
    # Thin book adds urgency
    urgency_liquidity = 0.0
    if ask_depth < 10:
        urgency_liquidity = min(3.0, (10 - ask_depth) * 0.4)

    urgency_score = min(10.0, 0.6 * urgency_time
                        + 0.2 * urgency_convergence
                        + 0.2 * urgency_liquidity)

    # ── Composite & Decision ──────────────────────────────────────────────
    # Weighted composite — certainty matters most
    composite = (0.45 * certainty_score
                 + 0.25 * ob_score
                 + 0.30 * urgency_score)

    scores = {
        "certainty": round(certainty_score, 2),
        "certainty_detail": {
            "z_score": round(z, 4),
            "distance_from_threshold": round(certainty_distance, 2),
            "vol_trend": vol_regime,
            "vol_adj": round(certainty_vol_adj, 2),
        },
        "orderbook": round(ob_score, 2),
        "orderbook_detail": {
            "depth": ask_depth,
            "total_depth": total_depth,
            "price_level": best_ask,
            "convergence_velocity": round(velocity, 2),
        },
        "urgency": round(urgency_score, 2),
        "urgency_detail": {
            "time_remaining": round(remaining, 1),
            "convergence_velocity": round(velocity, 2),
            "liquidity_trend": round(urgency_liquidity, 2),
        },
        "composite": round(composite, 2),
        "strategy": None,   # filled below
        "reason": None,      # filled below
    }

    def _decide(strategy: str, reason: str) -> Tuple[str, Dict]:
        scores["strategy"] = strategy
        scores["reason"] = reason
        return (strategy, scores)

    # ── PANIC_CAPTURE: outcome obvious + book dried up ────────────────────
    if (certainty_score >= 7.0
            and (best_ask is None or best_ask > 95 or total_depth < 20)
            and z >= 3.5):
        return _decide(STRATEGY_PANIC_CAPTURE,
                        f"certainty={certainty_score:.1f} z={z:.1f} "
                        f"depth={total_depth} ask={best_ask}")

    # ── TAKER_NOW: conditions demand immediate execution ──────────────────
    if best_ask is not None and _min_price <= best_ask <= ESCALATION_MAX_ENTRY:
        if composite >= 6.5:
            return _decide(STRATEGY_TAKER_NOW,
                            f"composite={composite:.1f}>=6.5")
        if velocity > 5 and _min_price <= best_ask <= _max_price:
            return _decide(STRATEGY_TAKER_NOW,
                            f"velocity={velocity:.1f}>5 ask={best_ask}")
        if certainty_score >= 6.0 and ob_score >= 6.0:
            return _decide(STRATEGY_TAKER_NOW,
                            f"certainty={certainty_score:.1f}>=6 "
                            f"ob={ob_score:.1f}>=6")

    # ── MAKER_AGGRESSIVE: confident but book still has depth ──────────────
    if composite >= 4.5:
        return _decide(STRATEGY_MAKER_AGGRESSIVE,
                        f"composite={composite:.1f}>=4.5")
    if certainty_score >= 5.0 and urgency_score >= 5.0:
        return _decide(STRATEGY_MAKER_AGGRESSIVE,
                        f"certainty={certainty_score:.1f}>=5 "
                        f"urgency={urgency_score:.1f}>=5")

    # ── MAKER_PATIENT: normal conditions ──────────────────────────────────
    if edge > 0 and best_ask is not None and _min_price <= best_ask <= _max_price:
        return _decide(STRATEGY_MAKER_PATIENT,
                        f"edge={edge:.4f}>0 ask={best_ask}")

    # ── WAIT: not confident enough ────────────────────────────────────────
    return _decide(STRATEGY_WAIT,
                    f"composite={composite:.1f} edge={edge:.4f}")


# ═════════════════════════════════════════════════════════════════════════════
#  KalshiClient
# ═════════════════════════════════════════════════════════════════════════════

class KalshiClient:
    """Handles all Kalshi API communication with RSA-PSS auth and rate limiting."""

    def __init__(self, api_key: str, private_key_path: str):
        self.api_key = api_key
        self.private_key = self._load_private_key(private_key_path)
        self.session = requests.Session()
        self._read_timestamps: List[float] = []
        self._write_timestamps: List[float] = []
        self._rate_lock = threading.Lock()

    # ── Auth ──────────────────────────────────────────────────────────────

    @staticmethod
    def _load_private_key(key_path: str):
        with open(key_path, "rb") as f:
            return serialization.load_pem_private_key(f.read(), password=None)

    def _create_signature(self, timestamp_ms: str, method: str, path: str) -> str:
        """Sign timestamp_ms + METHOD + path (without query params) using RSA-PSS."""
        path_no_query = path.split("?")[0]
        message = f"{timestamp_ms}{method}{path_no_query}".encode("utf-8")
        sig = self.private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(sig).decode("utf-8")

    # ── Rate Limiting ─────────────────────────────────────────────────────

    def _rate_limit_wait(self, is_write: bool):
        with self._rate_lock:
            now = time.time()
            timestamps = self._write_timestamps if is_write else self._read_timestamps
            limit = WRITE_RATE_LIMIT if is_write else READ_RATE_LIMIT

            # Purge timestamps older than 1 second
            cutoff = now - 1.0
            while timestamps and timestamps[0] < cutoff:
                timestamps.pop(0)

            if len(timestamps) >= limit:
                sleep_time = timestamps[0] + 1.0 - now
                if sleep_time > 0:
                    time.sleep(sleep_time)
                # Purge again after sleeping
                now = time.time()
                cutoff = now - 1.0
                while timestamps and timestamps[0] < cutoff:
                    timestamps.pop(0)

            timestamps.append(time.time())

    # ── Core Request ──────────────────────────────────────────────────────

    def _request(self, method: str, path: str,
                 params: Optional[Dict] = None,
                 json_body: Optional[Dict] = None) -> Optional[Dict]:
        """
        Execute an authenticated request. Path must start with /trade-api/v2.
        Returns parsed JSON or None on failure. Never raises.
        """
        is_write = method in ("POST", "PUT", "DELETE")
        self._rate_limit_wait(is_write)

        timestamp_ms = str(int(time.time() * 1000))
        url = f"{BASE_URL}{path}"
        signature = self._create_signature(timestamp_ms, method, path)

        headers = {
            "KALSHI-ACCESS-KEY": self.api_key,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "Content-Type": "application/json",
        }

        try:
            resp = self.session.request(
                method, url,
                headers=headers,
                params=params,
                json=json_body,
                timeout=10,
            )
            # Clock drift detection from server Date header
            server_date = resp.headers.get("Date")
            if server_date:
                try:
                    from email.utils import parsedate_to_datetime
                    server_time = parsedate_to_datetime(server_date)
                    drift = abs((datetime.datetime.now(timezone.utc) - server_time).total_seconds())
                    if drift > 2.0:
                        logging.warning(f"clock_drift_detected: {drift:.1f}s vs server")
                except Exception:
                    pass

            if resp.status_code == 429:
                if method == "POST" and "/orders" in path:
                    logging.error(f"Rate limited on POST {path} — NOT retrying to prevent duplicate orders")
                    return None
                retry_after = float(resp.headers.get("Retry-After", "1"))
                retries = getattr(self, '_429_retries', 0) + 1
                if retries > 3:
                    logging.error(f"Rate limited {retries} times, giving up: {method} {path}")
                    self._429_retries = 0
                    return None
                self._429_retries = retries
                logging.warning(f"Rate limited, sleeping {retry_after}s (attempt {retries}/3)")
                time.sleep(retry_after)
                result = self._request(method, path, params, json_body)
                self._429_retries = 0
                return result
            if resp.status_code >= 400:
                body_text = resp.text[:500] if resp.text else "(empty)"
                logging.error(f"API error: {method} {path} -> {resp.status_code} body={body_text}")
            resp.raise_for_status()
            return resp.json() if resp.content else {}
        except requests.exceptions.RequestException as e:
            logging.error(f"API error: {method} {path} -> {e}")
            return None

    # ── Public API Methods ────────────────────────────────────────────────

    def get_balance(self) -> Optional[Dict]:
        return self._request("GET", f"{API_PATH_PREFIX}/portfolio/balance")

    def get_markets(self, series_ticker: Optional[str] = None,
                    status: Optional[str] = None,
                    min_close_ts: Optional[int] = None,
                    max_close_ts: Optional[int] = None,
                    cursor: Optional[str] = None,
                    limit: int = 200) -> Optional[Dict]:
        params: Dict = {"limit": limit}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if status:
            params["status"] = status
        if min_close_ts is not None:
            params["min_close_ts"] = min_close_ts
        if max_close_ts is not None:
            params["max_close_ts"] = max_close_ts
        if cursor:
            params["cursor"] = cursor
        return self._request("GET", f"{API_PATH_PREFIX}/markets", params=params)

    def get_events(self, series_ticker: Optional[str] = None,
                   status: Optional[str] = None,
                   with_nested_markets: bool = False,
                   limit: int = 100) -> Optional[Dict]:
        params: Dict = {"limit": limit}
        if series_ticker:
            params["series_ticker"] = series_ticker
        if status:
            params["status"] = status
        if with_nested_markets:
            params["with_nested_markets"] = "true"
        return self._request("GET", f"{API_PATH_PREFIX}/events", params=params)

    def get_market(self, ticker: str) -> Optional[Dict]:
        return self._request("GET", f"{API_PATH_PREFIX}/markets/{ticker}")

    def get_orderbook(self, ticker: str, depth: int = 10) -> Optional[Dict]:
        return self._request(
            "GET", f"{API_PATH_PREFIX}/markets/{ticker}/orderbook",
            params={"depth": depth},
        )

    def place_order(self, ticker: str, side: str, action: str, count: int,
                    yes_price: Optional[int] = None,
                    no_price: Optional[int] = None,
                    client_order_id: Optional[str] = None,
                    post_only: Optional[bool] = None,
                    time_in_force: Optional[str] = None) -> Optional[Dict]:
        body: Dict = {
            "ticker": ticker,
            "side": side,
            "action": action,
            "count": count,
            "type": "limit",
        }
        if yes_price is not None:
            body["yes_price"] = yes_price
        if no_price is not None:
            body["no_price"] = no_price
        if client_order_id:
            body["client_order_id"] = client_order_id
        if post_only is not None:
            body["post_only"] = post_only
        if time_in_force is not None:
            body["time_in_force"] = time_in_force
        return self._request("POST", f"{API_PATH_PREFIX}/portfolio/orders",
                             json_body=body)

    def cancel_order(self, order_id: str) -> Optional[Dict]:
        return self._request("DELETE",
                             f"{API_PATH_PREFIX}/portfolio/orders/{order_id}")

    def amend_order(self, order_id: str, ticker: str, side: str, action: str,
                    count: Optional[int] = None,
                    yes_price: Optional[int] = None,
                    no_price: Optional[int] = None) -> Optional[Dict]:
        """Amend an existing order in-place (price/count). Saves cancel+re-place."""
        body: Dict = {"ticker": ticker, "side": side, "action": action}
        if count is not None:
            body["count"] = count
        if yes_price is not None:
            body["yes_price"] = yes_price
        if no_price is not None:
            body["no_price"] = no_price
        return self._request("POST",
                             f"{API_PATH_PREFIX}/portfolio/orders/{order_id}/amend",
                             json_body=body)

    def get_queue_position(self, order_id: str) -> Optional[int]:
        """Get queue position for a resting order. Returns position or None."""
        resp = self._request("GET",
                             f"{API_PATH_PREFIX}/portfolio/orders/{order_id}/queue_position")
        if resp is None:
            return None
        return resp.get("queue_position")

    def get_orders(self, ticker: Optional[str] = None,
                   status: Optional[str] = None) -> Optional[Dict]:
        params: Dict = {}
        if ticker:
            params["ticker"] = ticker
        if status:
            params["status"] = status
        return self._request("GET", f"{API_PATH_PREFIX}/portfolio/orders",
                             params=params)

    def get_fills(self, ticker: Optional[str] = None,
                  min_ts: Optional[int] = None,
                  limit: int = 200) -> Optional[Dict]:
        params: Dict = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if min_ts is not None:
            params["min_ts"] = min_ts
        return self._request("GET", f"{API_PATH_PREFIX}/portfolio/fills",
                             params=params)

    def get_settlements(self, ticker: Optional[str] = None,
                        min_ts: Optional[int] = None,
                        limit: int = 200) -> Optional[Dict]:
        params: Dict = {"limit": limit}
        if ticker:
            params["ticker"] = ticker
        if min_ts is not None:
            params["min_ts"] = min_ts
        return self._request("GET", f"{API_PATH_PREFIX}/portfolio/settlements",
                             params=params)

    def get_positions(self, event_ticker: Optional[str] = None) -> Optional[Dict]:
        params: Dict = {}
        if event_ticker:
            params["event_ticker"] = event_ticker
        return self._request("GET", f"{API_PATH_PREFIX}/portfolio/positions",
                             params=params)


# ═════════════════════════════════════════════════════════════════════════════
#  Logger
# ═════════════════════════════════════════════════════════════════════════════

class Logger:
    """Structured JSONL logging with fill deduplication."""

    def __init__(self):
        self._logged_fill_ids: Set[str] = set()

    def _write_entry(self, filepath: str, entry: Dict):
        entry["ts"] = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        try:
            with open(filepath, "a") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except IOError as e:
            logging.error(f"Failed to write to {filepath}: {e}")

    def log_scan(self, data: Dict):
        self._write_entry(SCAN_JOURNAL, {"type": "scan", **data})

    def log_trade(self, data: Dict):
        self._write_entry(TRADE_JOURNAL, {"type": "trade", **data})

    def log_settlement(self, data: Dict):
        self._write_entry(SETTLEMENT_JOURNAL, {"type": "settlement", **data})

    def log_order(self, data: Dict):
        self._write_entry(ORDER_JOURNAL, {"type": "order", **data})

    def log_rejection(self, data: Dict):
        self._write_entry(REJECTION_JOURNAL, {"type": "rejection", **data})

    def log_opportunity(self, data: Dict):
        self._write_entry(OPPORTUNITY_JOURNAL, {"type": "opportunity", **data})

    def log_execution(self, data: Dict):
        self._write_entry(EXECUTION_JOURNAL, {"type": "execution", **data})

    def log_performance(self, data: Dict):
        self._write_entry(PERFORMANCE_JOURNAL, {"type": "performance", **data})

    def log_fill(self, fill: Dict) -> bool:
        """Log a fill, deduplicating by fill_id. Returns True if new."""
        fill_id = fill.get("fill_id", "")
        if fill_id in self._logged_fill_ids:
            return False
        self._logged_fill_ids.add(fill_id)
        self._write_entry(TRADE_JOURNAL, {"type": "fill", **fill})
        return True

    def load_logged_fill_ids(self):
        """Rebuild _logged_fill_ids from existing trade journal on startup."""
        try:
            with open(TRADE_JOURNAL, "r") as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                        if entry.get("type") == "fill" and "fill_id" in entry:
                            self._logged_fill_ids.add(entry["fill_id"])
                    except json.JSONDecodeError:
                        continue
        except FileNotFoundError:
            pass
        logging.info(f"Loaded {len(self._logged_fill_ids)} previously logged fill IDs")


class TelegramNotifier:
    """Fire-and-forget Telegram alerts via Bot API."""

    def __init__(self, bot_token: str, chat_id: str):
        self._url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        self._chat_id = chat_id
        self.enabled = bool(bot_token and chat_id)
        self._dedup: Dict[str, float] = {}

    def send(self, message: str, silent: bool = False, dedup_key: Optional[str] = None):
        if not self.enabled:
            return
        if dedup_key:
            now = time.time()
            if dedup_key in self._dedup and now - self._dedup[dedup_key] < 60:
                return
            self._dedup[dedup_key] = now
        text = message[:4096]
        threading.Thread(target=self._post, args=(text, silent), daemon=True).start()

    def _post(self, text: str, silent: bool):
        try:
            requests.post(self._url, json={
                "chat_id": self._chat_id,
                "text": text,
                "parse_mode": "Markdown",
                "disable_notification": silent,
            }, timeout=5)
        except Exception as e:
            logging.warning(f"Telegram send failed: {e}")


# ═════════════════════════════════════════════════════════════════════════════
#  StateManager
# ═════════════════════════════════════════════════════════════════════════════

class StateManager:
    """SQLite-backed persistent state. WAL mode for crash resilience."""

    def __init__(self, db_path: str = DB_PATH):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.row_factory = sqlite3.Row
        self._last_balance_cents: Optional[int] = None
        self._create_tables()
        # Seed balance cache from most recent DB value to avoid NULL gap after restart
        try:
            row = self.conn.execute(
                "SELECT available_balance_cents FROM evaluated_opportunities "
                "WHERE available_balance_cents IS NOT NULL ORDER BY evaluation_time DESC LIMIT 1"
            ).fetchone()
            if row:
                self._last_balance_cents = row[0]
        except Exception:
            pass

    def _create_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS positions (
                ticker TEXT PRIMARY KEY,
                event_ticker TEXT NOT NULL,
                asset TEXT NOT NULL,
                side TEXT NOT NULL,
                count INTEGER NOT NULL,
                avg_price_cents INTEGER NOT NULL,
                total_cost_cents INTEGER NOT NULL,
                opened_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open'
            );

            CREATE TABLE IF NOT EXISTS pending_orders (
                order_id TEXT PRIMARY KEY,
                client_order_id TEXT,
                ticker TEXT NOT NULL,
                event_ticker TEXT NOT NULL,
                asset TEXT NOT NULL,
                side TEXT NOT NULL,
                action TEXT NOT NULL,
                count INTEGER NOT NULL,
                price_cents INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'resting',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS settled_trades (
                ticker TEXT PRIMARY KEY,
                event_ticker TEXT NOT NULL,
                asset TEXT NOT NULL,
                market_result TEXT NOT NULL,
                side TEXT NOT NULL,
                count INTEGER NOT NULL,
                entry_price_cents INTEGER NOT NULL,
                revenue_cents INTEGER NOT NULL,
                fee_cents INTEGER NOT NULL,
                pnl_cents INTEGER NOT NULL,
                settled_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS garch_params (
                asset TEXT PRIMARY KEY,
                omega REAL,
                alpha REAL,
                beta REAL,
                last_variance REAL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS egarch_params (
                asset TEXT PRIMARY KEY,
                omega REAL NOT NULL,
                alpha REAL NOT NULL,
                gamma REAL NOT NULL,
                beta REAL NOT NULL,
                last_log_variance REAL,
                mle_loglik REAL,
                mle_converged INTEGER DEFAULT 0,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sports_platt_params (
                id INTEGER PRIMARY KEY DEFAULT 1,
                a REAL NOT NULL DEFAULT 1.0,
                b REAL NOT NULL DEFAULT 0.0,
                n_train INTEGER NOT NULL DEFAULT 0,
                h1_brier REAL,
                h2_brier_raw REAL,
                h2_brier_cal REAL,
                fitted INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_positions_asset
                ON positions(asset);
            CREATE INDEX IF NOT EXISTS idx_positions_status
                ON positions(status);
            CREATE INDEX IF NOT EXISTS idx_pending_orders_ticker
                ON pending_orders(ticker);
            CREATE INDEX IF NOT EXISTS idx_settled_trades_asset
                ON settled_trades(asset);

            CREATE TABLE IF NOT EXISTS rejected_opportunities (
                ticker TEXT PRIMARY KEY,
                event_ticker TEXT NOT NULL,
                asset TEXT NOT NULL,
                rejection_reason TEXT NOT NULL,
                rejection_time TEXT NOT NULL,
                z_score REAL,
                spot_price REAL,
                threshold REAL,
                volatility REAL,
                market_price INTEGER,
                seconds_to_close REAL,
                calibrated_prob REAL,
                status TEXT NOT NULL DEFAULT 'pending'
            );

            CREATE INDEX IF NOT EXISTS idx_rejected_status
                ON rejected_opportunities(status);

            CREATE TABLE IF NOT EXISTS evaluated_opportunities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                event_ticker TEXT NOT NULL,
                asset TEXT NOT NULL,
                filter_stage TEXT NOT NULL,
                rejection_reason TEXT,
                evaluation_time TEXT NOT NULL,
                spot_price REAL,
                threshold REAL,
                volatility REAL,
                market_price INTEGER,
                seconds_to_close REAL,
                calibrated_prob REAL,
                edge REAL,
                ofa_adjustment REAL,
                status TEXT NOT NULL DEFAULT 'pending',
                market_result TEXT,
                counterfactual_pnl INTEGER
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_eval_opp_ticker_stage
                ON evaluated_opportunities(ticker, filter_stage);
            CREATE INDEX IF NOT EXISTS idx_eval_opp_status
                ON evaluated_opportunities(status);
            CREATE INDEX IF NOT EXISTS idx_eval_opp_ticker
                ON evaluated_opportunities(ticker);
        """)
        self.conn.commit()

        # SOL Path C shadow table: compares live taker override vs hypothetical maker-with-escalation
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS sol_pathc_shadow (
                ticker TEXT PRIMARY KEY,
                evaluation_time TEXT,
                asset TEXT DEFAULT 'SOL',
                -- Live taker state (what actually happened)
                live_ask INTEGER,
                live_depth INTEGER,
                live_edge REAL,
                live_stc REAL,
                live_contracts INTEGER,
                live_entry_price INTEGER,
                live_cal_prob REAL,
                -- Path C maker hypothetical
                pathc_maker_price INTEGER,
                pathc_maker_offset INTEGER,
                pathc_depth_at_maker INTEGER,
                position_size INTEGER,
                -- Deferred observation (continuous monitoring during escalation window)
                obs_time TEXT,
                obs_elapsed_seconds REAL,
                obs_best_ask INTEGER,
                obs_depth INTEGER,
                obs_maker_would_fill INTEGER DEFAULT 0,
                obs_maker_price_touched INTEGER DEFAULT 0,
                -- Path C escalation taker hypothetical (if maker wouldn't fill)
                pathc_esc_ask INTEGER,
                pathc_esc_depth INTEGER,
                pathc_esc_edge REAL,
                -- Settlement
                status TEXT DEFAULT 'pending',
                market_result TEXT,
                settled_time TEXT,
                -- Counterfactual PnL
                live_pnl_cents INTEGER,
                pathc_maker_pnl_cents INTEGER,
                pathc_maker_contracts INTEGER,
                pathc_esc_pnl_cents INTEGER,
                pathc_esc_contracts INTEGER,
                pathc_best_pnl_cents INTEGER
            );
        """)
        self.conn.commit()

        # Sports shadow log table (independent from crypto)
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS sports_shadow_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id TEXT, sport TEXT, league TEXT,
                home_team TEXT, away_team TEXT, home_code TEXT, away_code TEXT,
                pregame_fav_code TEXT, pregame_fav_prob REAL,
                pregame_price_home REAL, pregame_price_away REAL, pregame_price_draw REAL,
                scheduled_start TEXT, outcome_type TEXT,
                home_score INTEGER, away_score INTEGER, fav_score INTEGER, underdog_score INTEGER,
                deficit INTEGER, period INTEGER, clock TEXT, time_remaining_pct REAL,
                game_status TEXT, red_cards_fav INTEGER, red_cards_underdog INTEGER,
                ticker TEXT, event_ticker TEXT, yes_bid INTEGER, yes_ask INTEGER,
                mid_price REAL, spread INTEGER, ask_depth INTEGER, bid_depth INTEGER,
                comeback_prob REAL, prior REAL, likelihood_ratio REAL,
                edge REAL, fee_adjusted_edge REAL,
                signal_fired INTEGER DEFAULT 0, filter_stage TEXT, rejection_reason TEXT,
                simulated_contracts INTEGER, simulated_risk REAL,
                evaluation_time TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ','now')),
                espn_latency_ms REAL, kalshi_latency_ms REAL,
                closing_price REAL,
                final_home_score INTEGER, final_away_score INTEGER,
                fav_won INTEGER, market_result TEXT, pnl_cents INTEGER,
                would_signal_50c INTEGER DEFAULT 0,
                would_signal_60c INTEGER DEFAULT 0,
                would_signal_70c INTEGER DEFAULT 0,
                would_signal_80c INTEGER DEFAULT 0,
                would_signal_pregame_55 INTEGER DEFAULT 0,
                would_signal_pregame_65 INTEGER DEFAULT 0,
                market_implied_prob REAL,
                pregame_capture_method TEXT,
                shadow_lr_scale_50_posterior REAL,
                shadow_lr_scale_50_signal INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_sports_shadow_game
                ON sports_shadow_log(game_id);
            CREATE INDEX IF NOT EXISTS idx_sports_shadow_league
                ON sports_shadow_log(league);
            CREATE INDEX IF NOT EXISTS idx_sports_shadow_signal
                ON sports_shadow_log(signal_fired);
        """)
        self.conn.commit()

        # Unified shadow view: combines 15M shadow engines + hourly alt shadows
        # Safe to re-run; depends on fifteenm_shadow_signals + hourly_alt_shadow_signals
        try:
            self.conn.executescript("""
                DROP VIEW IF EXISTS unified_shadow_signals;
                CREATE VIEW unified_shadow_signals AS
                SELECT 'fifteenm' AS source, 'A1_recal_egarch' AS approach,
                       asset, evaluation_time, market_price,
                       a1_final_prob AS prob, a1_fee_edge AS fee_edge,
                       a1_kelly_f AS kelly_f, a1_contracts AS contracts,
                       a1_gates_passed AS gates_passed, a1_pnl_cents AS pnl_cents,
                       status, market_result, settled_time
                FROM fifteenm_shadow_signals WHERE a1_final_prob IS NOT NULL
                UNION ALL
                SELECT 'fifteenm', 'A2_lightgbm',
                       asset, evaluation_time, market_price,
                       a2_calibrated_prob, a2_fee_edge,
                       a2_kelly_f, a2_contracts,
                       a2_gates_passed, a2_pnl_cents,
                       status, market_result, settled_time
                FROM fifteenm_shadow_signals WHERE a2_raw_prob IS NOT NULL
                UNION ALL
                SELECT 'fifteenm', 'A3_gating',
                       asset, evaluation_time, market_price,
                       a3_gate_prob, NULL, NULL, NULL,
                       a3_gate_10, a3_pnl_gate10_cents,
                       status, market_result, settled_time
                FROM fifteenm_shadow_signals WHERE a3_gate_prob IS NOT NULL
                UNION ALL
                SELECT 'fifteenm', 'A4_late_window',
                       asset, evaluation_time, market_price,
                       live_prob, a4_edge, NULL, NULL,
                       a4_gates_passed, a4_pnl_cents,
                       status, market_result, settled_time
                FROM fifteenm_shadow_signals WHERE a4_gates_passed = 1
                UNION ALL
                SELECT 'hourly_alt', strategy,
                       asset, evaluation_time, market_price,
                       final_prob, fee_adjusted_edge,
                       kelly_f, shadow_contracts,
                       gates_passed, shadow_pnl_cents,
                       status, market_result, settled_time
                FROM hourly_alt_shadow_signals;
            """)
            self.conn.commit()
        except Exception:
            logging.debug("unified_shadow_signals view creation skipped (tables may not exist yet)")

        # Migration: add new columns to sports_shadow_log (safe to re-run)
        for col_def in [
            ("would_signal_50c", "INTEGER DEFAULT 0"),
            ("would_signal_60c", "INTEGER DEFAULT 0"),
            ("would_signal_70c", "INTEGER DEFAULT 0"),
            ("would_signal_80c", "INTEGER DEFAULT 0"),
            ("would_signal_pregame_55", "INTEGER DEFAULT 0"),
            ("would_signal_pregame_65", "INTEGER DEFAULT 0"),
            ("market_implied_prob", "REAL"),
            ("pregame_capture_method", "TEXT"),
            ("shadow_lr_scale_50_posterior", "REAL"),
            ("shadow_lr_scale_50_signal", "INTEGER DEFAULT 0"),
            ("score_changed", "INTEGER"),
            ("sport_group", "TEXT"),
            ("sport_lr_scale", "REAL"),
            ("is_strong_config", "INTEGER DEFAULT 0"),
            ("platt_prob", "REAL"),
            ("platt_edge", "REAL"),
            ("platt_fee_adj_edge", "REAL"),
        ]:
            try:
                self.conn.execute(
                    f"ALTER TABLE sports_shadow_log ADD COLUMN {col_def[0]} {col_def[1]}")
            except Exception:
                pass  # Column already exists
        self.conn.commit()

        # Migration: add new columns to evaluated_opportunities (safe to re-run)
        for col_def in [
            ("strategy", "TEXT"),
            ("position_size", "INTEGER"),
            ("kelly_f", "REAL"),
            ("z_score", "REAL"),
            ("vol_regime", "TEXT"),
            ("calibrated_prob_raw", "REAL"),
            ("settled_time", "TEXT"),
            ("breakeven_wr", "REAL"),
            ("expected_value", "REAL"),
            ("drawdown_scaler", "REAL"),
            ("ask_depth", "INTEGER"),
            ("best_ask_source", "TEXT"),
            ("ofa_confidence", "TEXT"),
            ("raw_prob", "REAL"),
            ("calibration_method", "TEXT"),
            ("old_system_prob", "REAL"),
            ("fee_adjusted_edge", "REAL"),
            ("egarch_sigma", "REAL"),
            ("egarch_blend_sigma", "REAL"),
            ("egarch_blend_weight", "REAL"),
            ("mz_r_squared", "REAL"),
            ("shadow_tv_blend_rv", "REAL"),
            ("mz_shadow_sigmoid_w", "REAL"),
            ("mz_baseline_qlike", "REAL"),
            ("mz_qlike", "REAL"),
            ("counterfactual", "TEXT"),
            ("shadow_cal_prob", "REAL"),
            ("shadow_cal_fee_edge", "REAL"),
            ("shadow_cal_temperature", "REAL"),
            ("product_type", "TEXT"),
            # OFT signal columns
            ("oft_prob_adjustment", "REAL"),
            ("oft_imbalance_ratio", "REAL"),
            ("oft_n_snapshots", "INTEGER"),
            # Weather ensemble columns
            ("wx_ensemble_mean", "REAL"),
            ("wx_ensemble_std", "REAL"),
            ("wx_bias_correction", "REAL"),
            ("wx_n_members", "INTEGER"),
            ("wx_market_type", "TEXT"),
            ("wx_actual_high_temp", "REAL"),
            ("wx_no_side_edge", "REAL"),
            ("wx_hrrr_temp", "REAL"),
            ("wx_corrected_mean", "REAL"),
            # Hourly temperature scaling columns
            ("hourly_pre_temp_prob", "REAL"),
            ("hourly_applied_temp_t", "REAL"),
            # Hourly shadow instrumentation columns
            ("hourly_shadow_temp_2_0", "REAL"),
            ("hourly_shadow_temp_1_0", "REAL"),
            ("hourly_shadow_temp_2_5", "REAL"),
            ("hourly_shadow_blend_50", "REAL"),
            ("hourly_shadow_temp_1_75", "REAL"),
            ("hourly_shadow_temp_3_0", "REAL"),
            ("hourly_shadow_blend_20", "REAL"),
            ("hourly_shadow_blend_30", "REAL"),
            ("hourly_shadow_blend_60", "REAL"),
            ("hourly_post_temp_prob", "REAL"),
            # Balance at evaluation time
            ("available_balance_cents", "INTEGER"),
            # Order tracking columns
            ("order_id", "TEXT"),
            ("order_submitted_at", "TEXT"),
            ("order_outcome", "TEXT"),
            # NO-side shadow: trade direction (yes=buy YES contract, no=buy NO contract)
            ("side", "TEXT DEFAULT 'yes'"),
            # Shadow taker tracking: best ask at maker order submission time
            ("taker_ask_at_submit", "INTEGER"),
        ]:
            try:
                self.conn.execute(f"ALTER TABLE evaluated_opportunities ADD COLUMN {col_def[0]} {col_def[1]}")
            except sqlite3.OperationalError:
                pass  # column already exists
        self.conn.commit()

        # Migration: update unique index to include side (enables YES + NO rows per ticker/stage)
        # Check if index already includes side by trying to create the 3-column version;
        # if it succeeds the old 2-column index is replaced.
        try:
            self.conn.execute("DROP INDEX IF EXISTS idx_eval_opp_ticker_stage")
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_eval_opp_ticker_stage "
                "ON evaluated_opportunities(ticker, filter_stage, side)")
            self.conn.commit()
        except Exception:
            pass

        # Migration: add new columns to rejected_opportunities (safe to re-run)
        for col_def in [
            ("raw_prob", "REAL"),
            ("market_result", "TEXT"),
            ("egarch_sigma", "REAL"),
            ("egarch_blend_sigma", "REAL"),
            ("egarch_blend_weight", "REAL"),
            ("mz_r_squared", "REAL"),
            ("shadow_tv_blend_rv", "REAL"),
            ("mz_shadow_sigmoid_w", "REAL"),
            ("mz_baseline_qlike", "REAL"),
            ("mz_qlike", "REAL"),
            ("counterfactual", "TEXT"),
            ("product_type", "TEXT"),
            # OFT signal columns (for rejected markets with OFT data)
            ("oft_prob_adjustment", "REAL"),
            ("oft_imbalance_ratio", "REAL"),
            ("oft_n_snapshots", "INTEGER"),
        ]:
            try:
                self.conn.execute(f"ALTER TABLE rejected_opportunities ADD COLUMN {col_def[0]} {col_def[1]}")
            except sqlite3.OperationalError:
                pass  # column already exists
        self.conn.commit()

        # Migration: add enrichment columns to settled_trades
        for col_def in [
            ("strategy", "TEXT"),
            ("seconds_to_close", "REAL"),
            ("fill_latency_seconds", "REAL"),
            ("vol_regime", "TEXT"),
            ("calibrated_prob", "REAL"),
            ("edge", "REAL"),
            ("kelly_f", "REAL"),
            ("escalation_type", "TEXT"),
            ("maker_price_cents", "INTEGER"),
            ("maker_wait_seconds", "REAL"),
            ("product_type", "TEXT"),
        ]:
            try:
                self.conn.execute(f"ALTER TABLE settled_trades ADD COLUMN {col_def[0]} {col_def[1]}")
            except sqlite3.OperationalError:
                pass  # column already exists
        self.conn.commit()

        # One-time backfill: derive product_type from ticker prefix
        null_count = self.conn.execute(
            "SELECT COUNT(*) FROM settled_trades WHERE product_type IS NULL"
        ).fetchone()[0]
        if null_count > 0:
            self.conn.execute(
                "UPDATE settled_trades SET product_type='15m' WHERE product_type IS NULL "
                "AND (ticker LIKE 'KXBTC15M%' OR ticker LIKE 'KXETH15M%' "
                "OR ticker LIKE 'KXSOL15M%' OR ticker LIKE 'KXXRP15M%')")
            self.conn.execute(
                "UPDATE settled_trades SET product_type='hourly' WHERE product_type IS NULL "
                "AND (ticker LIKE 'KXBTCD%' OR ticker LIKE 'KXETHD%' "
                "OR ticker LIKE 'KXSOLD%' OR ticker LIKE 'KXXRPD%')")
            self.conn.execute(
                "UPDATE settled_trades SET product_type='spx_hourly' WHERE product_type IS NULL "
                "AND ticker LIKE 'KXSPX%'")
            self.conn.execute(
                "UPDATE settled_trades SET product_type='weather' WHERE product_type IS NULL "
                "AND ticker LIKE 'KXHIGH%'")
            self.conn.commit()
            logging.info(f"Backfilled product_type for {null_count} settled_trades rows")

        # Migration: add enrichment columns to positions
        for col_def in [
            ("strategy", "TEXT"),
            ("seconds_to_close", "REAL"),
            ("fill_latency_seconds", "REAL"),
            ("vol_regime", "TEXT"),
            ("calibrated_prob", "REAL"),
            ("edge", "REAL"),
            ("kelly_f", "REAL"),
            ("is_taker", "INTEGER"),
            ("fill_source", "TEXT"),
            ("execution_method", "TEXT"),
            ("escalation_type", "TEXT"),
            ("maker_price_cents", "INTEGER"),
            ("maker_wait_seconds", "REAL"),
        ]:
            try:
                self.conn.execute(f"ALTER TABLE positions ADD COLUMN {col_def[0]} {col_def[1]}")
            except sqlite3.OperationalError:
                pass  # column already exists
        self.conn.commit()

    # ── Ticker Parsing ────────────────────────────────────────────────────

    @staticmethod
    def _asset_from_ticker(ticker: str) -> str:
        """'KXBTC15M-26FEB211545-45' -> 'BTC'  (also handles 'KXBTC-...' legacy)"""
        prefix = ticker.split("-")[0]  # e.g. "KXBTC15M" or "KXBTC"
        if prefix.startswith("KX"):
            asset = prefix[2:]         # "BTC15M" or "BTC"
            # Strip known product suffixes
            for suffix in ("15M", "1H", "1D", "D"):
                if asset.endswith(suffix):
                    asset = asset[:-len(suffix)]
            return asset
        return prefix

    @staticmethod
    def _event_ticker_from_ticker(ticker: str) -> str:
        """'KXBTC15M-26FEB211545-45' -> 'KXBTC15M-26FEB211545'"""
        parts = ticker.split("-")
        if len(parts) >= 2:
            return f"{parts[0]}-{parts[1]}"
        return ticker

    # ── Reconciliation ────────────────────────────────────────────────────

    def reconcile_with_api(self, client: KalshiClient):
        """Sync local state with Kalshi API on startup. API always wins."""
        now = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        self._reconcile_positions(client, now)
        self._reconcile_orders(client, now)
        self.conn.commit()
        logging.info("State reconciliation complete")

    def _reconcile_positions(self, client: KalshiClient, now: str):
        api_resp = client.get_positions()
        if not api_resp or not api_resp.get("market_positions"):
            logging.warning("Could not fetch positions for reconciliation")
            return

        api_tickers: Set[str] = set()
        for pos in api_resp["market_positions"]:
            ticker = pos["ticker"]
            api_tickers.add(ticker)
            position_count = fp_str_to_int(pos.get("position_fp")) or (pos.get("position") or 0)

            if position_count == 0:
                self.conn.execute(
                    "DELETE FROM positions WHERE ticker = ?", (ticker,))
                continue

            side = "yes" if position_count > 0 else "no"
            count = abs(position_count)
            cost_d = pos.get("market_exposure_dollars")
            cost = dollars_str_to_cents(cost_d) if cost_d else (pos.get("market_exposure") or 0)
            avg_price = cost // count if count else 0

            existing = self.conn.execute(
                "SELECT 1 FROM positions WHERE ticker = ?", (ticker,)
            ).fetchone()

            if existing:
                self.conn.execute("""
                    UPDATE positions SET side=?, count=?, avg_price_cents=?,
                        total_cost_cents=?, updated_at=?, status='open'
                    WHERE ticker=?
                """, (side, count, avg_price, cost, now, ticker))
            else:
                asset = self._asset_from_ticker(ticker)
                event_ticker = self._event_ticker_from_ticker(ticker)
                self.conn.execute("""
                    INSERT INTO positions (ticker, event_ticker, asset, side,
                        count, avg_price_cents, total_cost_cents,
                        opened_at, updated_at, status)
                    VALUES (?,?,?,?,?,?,?,?,?,'open')
                """, (ticker, event_ticker, asset, side, count,
                      avg_price, cost, now, now))

        # Remove local positions not on API
        local_rows = self.conn.execute(
            "SELECT ticker FROM positions WHERE status='open'"
        ).fetchall()
        for row in local_rows:
            if row["ticker"] not in api_tickers:
                self.conn.execute("""
                    UPDATE positions SET status='closed', updated_at=?
                    WHERE ticker=?
                """, (now, row["ticker"]))

    def _reconcile_orders(self, client: KalshiClient, now: str):
        api_resp = client.get_orders(status="resting")
        if api_resp is None:
            logging.warning("Could not fetch orders for reconciliation")
            return

        api_order_ids: Set[str] = set()
        for order in api_resp["orders"]:
            oid = order["order_id"]
            api_order_ids.add(oid)

            existing = self.conn.execute(
                "SELECT 1 FROM pending_orders WHERE order_id=?", (oid,)
            ).fetchone()
            if existing:
                continue

            ticker = order["ticker"]
            asset = self._asset_from_ticker(ticker)
            event_ticker = self._event_ticker_from_ticker(ticker)
            # Prefer *_dollars fields (new FP API), fall back to legacy
            ypd = order.get("yes_price_dollars")
            npd = order.get("no_price_dollars")
            if ypd:
                price = dollars_str_to_cents(ypd)
            elif npd:
                price = dollars_str_to_cents(npd)
            else:
                price = order.get("yes_price", 0) or order.get("no_price", 0)

            remaining = fp_str_to_int(order.get("remaining_count_fp")) or (order.get("remaining_count") or 0)

            self.conn.execute("""
                INSERT INTO pending_orders (order_id, client_order_id, ticker,
                    event_ticker, asset, side, action, count, price_cents,
                    status, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,'resting',?,?)
            """, (oid, order.get("client_order_id", ""), ticker,
                  event_ticker, asset, order["side"], order["action"],
                  remaining, price,
                  order.get("created_time", now), now))

        # Mark local resting orders not on API as canceled
        local_rows = self.conn.execute(
            "SELECT order_id FROM pending_orders WHERE status='resting'"
        ).fetchall()
        for row in local_rows:
            if row["order_id"] not in api_order_ids:
                self.conn.execute("""
                    UPDATE pending_orders SET status='canceled', updated_at=?
                    WHERE order_id=?
                """, (now, row["order_id"]))

    # ── CRUD ──────────────────────────────────────────────────────────────

    def get_open_positions(self, asset: Optional[str] = None) -> List[Dict]:
        if asset:
            rows = self.conn.execute(
                "SELECT * FROM positions WHERE status='open' AND asset=?",
                (asset,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM positions WHERE status='open'"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_unsettled_positions(self) -> List[Dict]:
        """Return positions that are open or closed but not yet settled."""
        rows = self.conn.execute(
            "SELECT * FROM positions WHERE status IN ('open', 'closed')"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_resting_orders(self, ticker: Optional[str] = None) -> List[Dict]:
        if ticker:
            rows = self.conn.execute(
                "SELECT * FROM pending_orders WHERE status='resting' AND ticker=?",
                (ticker,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM pending_orders WHERE status='resting'"
            ).fetchall()
        return [dict(r) for r in rows]

    def record_settlement(self, settlement: Dict,
                          pnl_override: Optional[int] = None,
                          fee_override: Optional[int] = None):
        ticker = settlement["ticker"]
        now = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        pos_row = self.conn.execute(
            "SELECT * FROM positions WHERE ticker=?", (ticker,)
        ).fetchone()
        if not pos_row:
            return
        pos = dict(pos_row)

        # Derive product_type from ticker prefix
        product_type = None
        if any(ticker.startswith(p) for p in ("KXBTC15M", "KXETH15M", "KXSOL15M", "KXXRP15M")):
            product_type = "15m"
        elif any(ticker.startswith(p) for p in ("KXBTCD", "KXETHD", "KXSOLD", "KXXRPD")):
            product_type = "hourly"
        elif ticker.startswith("KXSPX"):
            product_type = "spx_hourly"
        elif ticker.startswith("KXHIGH"):
            product_type = "weather"

        result = settlement.get("market_result", "")
        rev_d = settlement.get("revenue_dollars")
        revenue = dollars_str_to_cents(rev_d) if rev_d else (settlement.get("revenue") or 0)
        total_cost = pos["total_cost_cents"]
        pnl = revenue - total_cost
        is_taker = bool(pos.get("is_taker"))
        fee = calculate_fee(pos["count"], pos["avg_price_cents"], is_taker=is_taker)

        # Cross-check P&L/fee consistency with caller (canary for divergence)
        if pnl_override is not None and abs(pnl - pnl_override) > 2:
            logging.error(
                f"PNL_MISMATCH {ticker}: record_settlement computed={pnl}, "
                f"tracker passed={pnl_override}, delta={pnl - pnl_override}")
        if fee_override is not None and abs(fee - fee_override) > 2:
            logging.error(
                f"FEE_MISMATCH {ticker}: record_settlement computed={fee}, "
                f"tracker passed={fee_override}, delta={fee - fee_override}")

        self.conn.execute("""
            INSERT OR REPLACE INTO settled_trades
                (ticker, event_ticker, asset, market_result, side, count,
                 entry_price_cents, revenue_cents, fee_cents, pnl_cents,
                 settled_at, strategy, seconds_to_close, fill_latency_seconds,
                 vol_regime, calibrated_prob, edge, kelly_f,
                 escalation_type, maker_price_cents, maker_wait_seconds,
                 product_type)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (ticker, pos["event_ticker"], pos["asset"], result,
              pos["side"], pos["count"], pos["avg_price_cents"],
              revenue, fee, pnl, now,
              pos.get("strategy"), pos.get("seconds_to_close"),
              pos.get("fill_latency_seconds"), pos.get("vol_regime"),
              pos.get("calibrated_prob"), pos.get("edge"), pos.get("kelly_f"),
              pos.get("escalation_type"), pos.get("maker_price_cents"),
              pos.get("maker_wait_seconds"),
              product_type))

        self.conn.execute("""
            UPDATE positions SET status='settled', updated_at=?
            WHERE ticker=?
        """, (now, ticker))
        self.conn.commit()

    # ── Rejected Opportunities ─────────────────────────────────────────

    def insert_rejection(self, ticker: str, event_ticker: str, asset: str,
                         rejection_reason: str, z_score: Optional[float],
                         spot_price: Optional[float], threshold: Optional[float],
                         volatility: Optional[float], market_price: Optional[int],
                         seconds_to_close: Optional[float],
                         calibrated_prob: Optional[float],
                         raw_prob: Optional[float] = None,
                         egarch_sigma: Optional[float] = None,
                         egarch_blend_sigma: Optional[float] = None,
                         egarch_blend_weight: Optional[float] = None,
                         mz_r_squared: Optional[float] = None,
                         shadow_tv_blend_rv: Optional[float] = None,
                         mz_shadow_sigmoid_w: Optional[float] = None,
                         mz_baseline_qlike: Optional[float] = None,
                         mz_qlike: Optional[float] = None,
                         counterfactual: Optional[str] = None,
                         product_type: Optional[str] = None,
                         oft_prob_adjustment: Optional[float] = None,
                         oft_imbalance_ratio: Optional[float] = None,
                         oft_n_snapshots: Optional[int] = None):
        """Insert a rejected opportunity. INSERT OR IGNORE keeps the first rejection reason."""
        now = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        self.conn.execute("""
            INSERT OR IGNORE INTO rejected_opportunities
                (ticker, event_ticker, asset, rejection_reason, rejection_time,
                 z_score, spot_price, threshold, volatility, market_price,
                 seconds_to_close, calibrated_prob, raw_prob, status,
                 egarch_sigma, egarch_blend_sigma, egarch_blend_weight, mz_r_squared,
                 shadow_tv_blend_rv, mz_shadow_sigmoid_w, mz_baseline_qlike, mz_qlike,
                 counterfactual, product_type,
                 oft_prob_adjustment, oft_imbalance_ratio, oft_n_snapshots)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (ticker, event_ticker, asset, rejection_reason, now,
              z_score, spot_price, threshold, volatility, market_price,
              seconds_to_close, calibrated_prob, raw_prob, "pending",
              egarch_sigma, egarch_blend_sigma, egarch_blend_weight, mz_r_squared,
              shadow_tv_blend_rv, mz_shadow_sigmoid_w, mz_baseline_qlike, mz_qlike,
              counterfactual, product_type,
              oft_prob_adjustment, oft_imbalance_ratio, oft_n_snapshots))
        self.conn.commit()

    def get_unsettled_rejections(self) -> List[Dict]:
        """Return all rejected opportunities with status='pending'."""
        rows = self.conn.execute(
            "SELECT * FROM rejected_opportunities WHERE status='pending'"
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_rejection_settled(self, ticker: str,
                              market_result: Optional[str] = None,
                              counterfactual: Optional[str] = None):
        """Set status='settled' and update result/counterfactual for a rejected opportunity."""
        self.conn.execute(
            """UPDATE rejected_opportunities
               SET status='settled', market_result=?, counterfactual=?
               WHERE ticker=?""",
            (market_result, counterfactual, ticker)
        )
        self.conn.commit()

    # ── Evaluated Opportunities ────────────────────────────────────────

    def insert_evaluated_opportunity(self, ticker: str, event_ticker: str,
                                     asset: str, filter_stage: str,
                                     rejection_reason: Optional[str] = None,
                                     spot_price: Optional[float] = None,
                                     threshold: Optional[float] = None,
                                     volatility: Optional[float] = None,
                                     market_price: Optional[int] = None,
                                     seconds_to_close: Optional[float] = None,
                                     calibrated_prob: Optional[float] = None,
                                     edge: Optional[float] = None,
                                     ofa_adjustment: Optional[float] = None,
                                     strategy: Optional[str] = None,
                                     position_size: Optional[int] = None,
                                     kelly_f: Optional[float] = None,
                                     z_score: Optional[float] = None,
                                     vol_regime: Optional[str] = None,
                                     calibrated_prob_raw: Optional[float] = None,
                                     breakeven_wr: Optional[float] = None,
                                     expected_value: Optional[float] = None,
                                     drawdown_scaler: Optional[float] = None,
                                     ask_depth: Optional[int] = None,
                                     best_ask_source: Optional[str] = None,
                                     ofa_confidence: Optional[str] = None,
                                     raw_prob: Optional[float] = None,
                                     calibration_method: Optional[str] = None,
                                     old_system_prob: Optional[float] = None,
                                     fee_adjusted_edge: Optional[float] = None,
                                     egarch_sigma: Optional[float] = None,
                                     egarch_blend_sigma: Optional[float] = None,
                                     egarch_blend_weight: Optional[float] = None,
                                     mz_r_squared: Optional[float] = None,
                                     shadow_tv_blend_rv: Optional[float] = None,
                                     mz_shadow_sigmoid_w: Optional[float] = None,
                                     mz_baseline_qlike: Optional[float] = None,
                                     mz_qlike: Optional[float] = None,
                                     counterfactual: Optional[str] = None,
                                     shadow_cal_prob: Optional[float] = None,
                                     shadow_cal_fee_edge: Optional[float] = None,
                                     shadow_cal_temperature: Optional[float] = None,
                                     product_type: Optional[str] = None,
                                     oft_prob_adjustment: Optional[float] = None,
                                     oft_imbalance_ratio: Optional[float] = None,
                                     oft_n_snapshots: Optional[int] = None,
                                     wx_ensemble_mean: Optional[float] = None,
                                     wx_ensemble_std: Optional[float] = None,
                                     wx_bias_correction: Optional[float] = None,
                                     wx_n_members: Optional[int] = None,
                                     wx_market_type: Optional[str] = None,
                                     wx_actual_high_temp: Optional[float] = None,
                                     wx_no_side_edge: Optional[float] = None,
                                     wx_hrrr_temp: Optional[float] = None,
                                     wx_corrected_mean: Optional[float] = None,
                                     hourly_pre_temp_prob: Optional[float] = None,
                                     hourly_applied_temp_t: Optional[float] = None,
                                     hourly_shadow_temp_2_0: Optional[float] = None,
                                     hourly_shadow_temp_1_0: Optional[float] = None,
                                     hourly_shadow_temp_2_5: Optional[float] = None,
                                     hourly_shadow_blend_50: Optional[float] = None,
                                     hourly_shadow_temp_1_75: Optional[float] = None,
                                     hourly_shadow_temp_3_0: Optional[float] = None,
                                     hourly_shadow_blend_20: Optional[float] = None,
                                     hourly_shadow_blend_30: Optional[float] = None,
                                     hourly_shadow_blend_60: Optional[float] = None,
                                     hourly_post_temp_prob: Optional[float] = None,
                                     available_balance_cents: Optional[int] = None,
                                     order_id: Optional[str] = None,
                                     order_submitted_at: Optional[str] = None,
                                     order_outcome: Optional[str] = None,
                                     side: str = "yes"):
        """Insert an evaluated opportunity for settlement tracking."""
        # Auto-fill balance from cache so ALL filter stages have a recent value
        if available_balance_cents is not None:
            self._last_balance_cents = available_balance_cents
        elif self._last_balance_cents is not None:
            available_balance_cents = self._last_balance_cents
        now = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        try:
            self.conn.execute("""
                INSERT INTO evaluated_opportunities
                    (ticker, event_ticker, asset, filter_stage, rejection_reason,
                     evaluation_time, spot_price, threshold, volatility,
                     market_price, seconds_to_close, calibrated_prob,
                     edge, ofa_adjustment, status,
                     strategy, position_size, kelly_f, z_score,
                     vol_regime, calibrated_prob_raw,
                     breakeven_wr, expected_value, drawdown_scaler,
                     ask_depth, best_ask_source, ofa_confidence,
                     raw_prob, calibration_method, old_system_prob,
                     fee_adjusted_edge,
                     egarch_sigma, egarch_blend_sigma, egarch_blend_weight, mz_r_squared,
                     shadow_tv_blend_rv, mz_shadow_sigmoid_w, mz_baseline_qlike, mz_qlike,
                     counterfactual,
                     shadow_cal_prob, shadow_cal_fee_edge, shadow_cal_temperature,
                     product_type,
                     oft_prob_adjustment, oft_imbalance_ratio, oft_n_snapshots,
                     wx_ensemble_mean, wx_ensemble_std, wx_bias_correction, wx_n_members,
                     wx_market_type, wx_actual_high_temp, wx_no_side_edge,
                     wx_hrrr_temp, wx_corrected_mean,
                     hourly_pre_temp_prob, hourly_applied_temp_t,
                     hourly_shadow_temp_2_0, hourly_shadow_temp_1_0, hourly_shadow_temp_2_5,
                     hourly_shadow_blend_50,
                     hourly_shadow_temp_1_75, hourly_shadow_temp_3_0,
                     hourly_shadow_blend_20, hourly_shadow_blend_30, hourly_shadow_blend_60,
                     hourly_post_temp_prob,
                     available_balance_cents,
                     order_id, order_submitted_at, order_outcome,
                     side)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(ticker, filter_stage, side) DO UPDATE SET
                    event_ticker=excluded.event_ticker, asset=excluded.asset,
                    rejection_reason=excluded.rejection_reason,
                    evaluation_time=excluded.evaluation_time,
                    spot_price=excluded.spot_price, threshold=excluded.threshold,
                    volatility=excluded.volatility, market_price=excluded.market_price,
                    seconds_to_close=excluded.seconds_to_close,
                    calibrated_prob=excluded.calibrated_prob,
                    edge=excluded.edge, ofa_adjustment=excluded.ofa_adjustment,
                    strategy=excluded.strategy, position_size=excluded.position_size,
                    kelly_f=excluded.kelly_f, z_score=excluded.z_score,
                    vol_regime=excluded.vol_regime,
                    calibrated_prob_raw=excluded.calibrated_prob_raw,
                    breakeven_wr=excluded.breakeven_wr,
                    expected_value=excluded.expected_value,
                    drawdown_scaler=excluded.drawdown_scaler,
                    ask_depth=excluded.ask_depth,
                    best_ask_source=excluded.best_ask_source,
                    ofa_confidence=excluded.ofa_confidence,
                    raw_prob=excluded.raw_prob,
                    calibration_method=excluded.calibration_method,
                    old_system_prob=excluded.old_system_prob,
                    fee_adjusted_edge=excluded.fee_adjusted_edge,
                    egarch_sigma=excluded.egarch_sigma,
                    egarch_blend_sigma=excluded.egarch_blend_sigma,
                    egarch_blend_weight=excluded.egarch_blend_weight,
                    mz_r_squared=excluded.mz_r_squared,
                    shadow_tv_blend_rv=excluded.shadow_tv_blend_rv,
                    mz_shadow_sigmoid_w=excluded.mz_shadow_sigmoid_w,
                    mz_baseline_qlike=excluded.mz_baseline_qlike,
                    mz_qlike=excluded.mz_qlike,
                    counterfactual=excluded.counterfactual,
                    shadow_cal_prob=excluded.shadow_cal_prob,
                    shadow_cal_fee_edge=excluded.shadow_cal_fee_edge,
                    shadow_cal_temperature=excluded.shadow_cal_temperature,
                    product_type=excluded.product_type,
                    oft_prob_adjustment=excluded.oft_prob_adjustment,
                    oft_imbalance_ratio=excluded.oft_imbalance_ratio,
                    oft_n_snapshots=excluded.oft_n_snapshots,
                    wx_ensemble_mean=excluded.wx_ensemble_mean,
                    wx_ensemble_std=excluded.wx_ensemble_std,
                    wx_bias_correction=excluded.wx_bias_correction,
                    wx_n_members=excluded.wx_n_members,
                    wx_market_type=excluded.wx_market_type,
                    wx_actual_high_temp=excluded.wx_actual_high_temp,
                    wx_no_side_edge=excluded.wx_no_side_edge,
                    wx_hrrr_temp=excluded.wx_hrrr_temp,
                    wx_corrected_mean=excluded.wx_corrected_mean,
                    hourly_pre_temp_prob=excluded.hourly_pre_temp_prob,
                    hourly_applied_temp_t=excluded.hourly_applied_temp_t,
                    hourly_shadow_temp_2_0=excluded.hourly_shadow_temp_2_0,
                    hourly_shadow_temp_1_0=excluded.hourly_shadow_temp_1_0,
                    hourly_shadow_temp_2_5=excluded.hourly_shadow_temp_2_5,
                    hourly_shadow_blend_50=excluded.hourly_shadow_blend_50,
                    hourly_shadow_temp_1_75=excluded.hourly_shadow_temp_1_75,
                    hourly_shadow_temp_3_0=excluded.hourly_shadow_temp_3_0,
                    hourly_shadow_blend_20=excluded.hourly_shadow_blend_20,
                    hourly_shadow_blend_30=excluded.hourly_shadow_blend_30,
                    hourly_shadow_blend_60=excluded.hourly_shadow_blend_60,
                    hourly_post_temp_prob=excluded.hourly_post_temp_prob,
                    available_balance_cents=excluded.available_balance_cents
            """, (ticker, event_ticker, asset, filter_stage, rejection_reason,
                  now, spot_price, threshold, volatility, market_price,
                  seconds_to_close, calibrated_prob, edge, ofa_adjustment,
                  "pending",
                  strategy, position_size, kelly_f, z_score,
                  vol_regime, calibrated_prob_raw,
                  breakeven_wr, expected_value, drawdown_scaler,
                  ask_depth, best_ask_source, ofa_confidence,
                  raw_prob, calibration_method, old_system_prob,
                  fee_adjusted_edge,
                  egarch_sigma, egarch_blend_sigma, egarch_blend_weight, mz_r_squared,
                  shadow_tv_blend_rv, mz_shadow_sigmoid_w, mz_baseline_qlike, mz_qlike,
                  counterfactual,
                  shadow_cal_prob, shadow_cal_fee_edge, shadow_cal_temperature,
                  product_type,
                  oft_prob_adjustment, oft_imbalance_ratio, oft_n_snapshots,
                  wx_ensemble_mean, wx_ensemble_std, wx_bias_correction, wx_n_members,
                  wx_market_type, wx_actual_high_temp, wx_no_side_edge,
                  wx_hrrr_temp, wx_corrected_mean,
                  hourly_pre_temp_prob, hourly_applied_temp_t,
                  hourly_shadow_temp_2_0, hourly_shadow_temp_1_0, hourly_shadow_temp_2_5,
                  hourly_shadow_blend_50,
                  hourly_shadow_temp_1_75, hourly_shadow_temp_3_0,
                  hourly_shadow_blend_20, hourly_shadow_blend_30, hourly_shadow_blend_60,
                  hourly_post_temp_prob,
                  available_balance_cents,
                  order_id, order_submitted_at, order_outcome,
                  side))
            self.conn.commit()
        except Exception as e:
            logging.warning(f"insert_evaluated_opportunity failed: {e}", exc_info=True)

    def update_evaluated_opportunity_order(self, ticker: str,
                                            order_id: Optional[str] = None,
                                            order_submitted_at: Optional[str] = None,
                                            order_outcome: Optional[str] = None,
                                            taker_ask_at_submit: Optional[int] = None):
        """Update order tracking fields on the candidate row for a ticker."""
        try:
            parts = []
            vals = []
            if order_id is not None:
                parts.append("order_id=?")
                vals.append(order_id)
            if order_submitted_at is not None:
                parts.append("order_submitted_at=?")
                vals.append(order_submitted_at)
            if order_outcome is not None:
                parts.append("order_outcome=?")
                vals.append(order_outcome)
            if taker_ask_at_submit is not None:
                parts.append("taker_ask_at_submit=?")
                vals.append(taker_ask_at_submit)
            if not parts:
                return
            vals.append(ticker)
            vals.append("candidate")
            self.conn.execute(
                f"UPDATE evaluated_opportunities SET {', '.join(parts)} "
                f"WHERE ticker=? AND filter_stage=?",
                tuple(vals)
            )
            self.conn.commit()
        except Exception as e:
            logging.warning(f"update_evaluated_opportunity_order failed: {e}", exc_info=True)

    def get_unsettled_evaluated_opportunities(self) -> List[Dict]:
        """Return evaluated opportunities with status='pending' and a market_price.
        Excludes synthetic sports tickers (SPORTS-*) that don't resolve via
        get_market().  Real Kalshi tickers (KXNBAGAME-*, etc.) are allowed."""
        rows = self.conn.execute(
            "SELECT * FROM evaluated_opportunities WHERE status='pending'"
            " AND market_price IS NOT NULL"
            " AND ticker NOT LIKE 'SPORTS-%'"
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_evaluated_opportunity_settled(self, opp_id: int,
                                             market_result: Optional[str] = None,
                                             counterfactual_pnl: Optional[int] = None,
                                             commit: bool = True):
        """Set status='settled' for an evaluated opportunity by id.
        Set commit=False to batch multiple updates in a single transaction."""
        now = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        self.conn.execute(
            "UPDATE evaluated_opportunities SET status='settled', "
            "market_result=?, counterfactual_pnl=?, settled_time=? WHERE id=?",
            (market_result, counterfactual_pnl, now, opp_id)
        )
        if commit:
            self.conn.commit()

    # ── SOL Path C Shadow ──────────────────────────────────────────────

    def insert_sol_pathc_shadow(self, ticker, evaluation_time, live_ask,
                                live_depth, live_edge, live_stc,
                                live_contracts, live_entry_price, live_cal_prob,
                                pathc_maker_price, pathc_maker_offset,
                                pathc_depth_at_maker, position_size):
        """Insert initial SOL Path C shadow row at taker submission time."""
        try:
            self.conn.execute("""
                INSERT OR REPLACE INTO sol_pathc_shadow
                    (ticker, evaluation_time, live_ask, live_depth,
                     live_edge, live_stc, live_contracts, live_entry_price,
                     live_cal_prob, pathc_maker_price, pathc_maker_offset,
                     pathc_depth_at_maker, position_size, status)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (ticker, evaluation_time, live_ask, live_depth,
                  live_edge, live_stc, live_contracts, live_entry_price,
                  live_cal_prob, pathc_maker_price, pathc_maker_offset,
                  pathc_depth_at_maker, position_size, "pending"))
            self.conn.commit()
        except Exception as e:
            logging.warning(f"insert_sol_pathc_shadow failed: {e}", exc_info=True)

    def update_sol_pathc_observation(self, ticker, obs_time, obs_elapsed,
                                     obs_best_ask, obs_depth,
                                     obs_maker_would_fill, obs_maker_price_touched,
                                     pathc_esc_ask, pathc_esc_depth, pathc_esc_edge):
        """Update deferred observation columns after escalation wait."""
        try:
            self.conn.execute("""
                UPDATE sol_pathc_shadow SET
                    obs_time=?, obs_elapsed_seconds=?, obs_best_ask=?,
                    obs_depth=?, obs_maker_would_fill=?,
                    obs_maker_price_touched=?,
                    pathc_esc_ask=?, pathc_esc_depth=?, pathc_esc_edge=?
                WHERE ticker=?
            """, (obs_time, obs_elapsed, obs_best_ask, obs_depth,
                  obs_maker_would_fill, obs_maker_price_touched,
                  pathc_esc_ask, pathc_esc_depth, pathc_esc_edge, ticker))
            self.conn.commit()
        except Exception as e:
            logging.warning(f"update_sol_pathc_observation failed: {e}", exc_info=True)

    def update_sol_pathc_touch(self, ticker):
        """Set obs_maker_price_touched=1 when ask drops to/below maker price."""
        try:
            self.conn.execute(
                "UPDATE sol_pathc_shadow SET obs_maker_price_touched=1 WHERE ticker=?",
                (ticker,))
            self.conn.commit()
        except Exception as e:
            logging.warning(f"update_sol_pathc_touch failed: {e}", exc_info=True)

    def settle_sol_pathc_shadow(self, ticker, market_result, live_pnl,
                                pathc_maker_pnl, pathc_maker_contracts,
                                pathc_esc_pnl, pathc_esc_contracts,
                                pathc_best_pnl):
        """Settle a SOL Path C shadow row with counterfactual PnL."""
        try:
            now = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            self.conn.execute("""
                UPDATE sol_pathc_shadow SET
                    status='settled', market_result=?, settled_time=?,
                    live_pnl_cents=?, pathc_maker_pnl_cents=?,
                    pathc_maker_contracts=?,
                    pathc_esc_pnl_cents=?, pathc_esc_contracts=?,
                    pathc_best_pnl_cents=?
                WHERE ticker=?
            """, (market_result, now, live_pnl, pathc_maker_pnl,
                  pathc_maker_contracts, pathc_esc_pnl, pathc_esc_contracts,
                  pathc_best_pnl, ticker))
            self.conn.commit()
        except Exception as e:
            logging.warning(f"settle_sol_pathc_shadow failed: {e}", exc_info=True)

    def get_pending_sol_pathc_shadows(self):
        """Get all pending sol_pathc_shadow rows for settlement."""
        try:
            rows = self.conn.execute(
                "SELECT * FROM sol_pathc_shadow WHERE status='pending'"
            ).fetchall()
            return [dict(r) for r in rows]
        except Exception:
            return []

    # ── Bot Order Lifecycle ─────────────────────────────────────────────

    def insert_bot_order(self, client_order_id: str, ticker: str,
                         event_ticker: str, asset: str, side: str,
                         count: int, price_cents: int, is_taker: bool):
        """Insert a new bot-initiated order with status='pending'."""
        now = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        self.conn.execute("""
            INSERT INTO pending_orders (order_id, client_order_id, ticker,
                event_ticker, asset, side, action, count, price_cents,
                status, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        """, (client_order_id, client_order_id, ticker,
              event_ticker, asset, side, "buy", count, price_cents,
              "pending", now, now))
        self.conn.commit()

    def confirm_order_submitted(self, client_order_id: str, order_id: str):
        """Update with server-assigned order_id, set status='resting'."""
        now = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        self.conn.execute("""
            UPDATE pending_orders SET order_id=?, status='resting', updated_at=?
            WHERE client_order_id=? AND status='pending'
        """, (order_id, now, client_order_id))
        self.conn.commit()

    def mark_order_status(self, order_id: str, status: str):
        """Update order status (filled, canceled, api_error)."""
        now = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        self.conn.execute("""
            UPDATE pending_orders SET status=?, updated_at=?
            WHERE order_id=? OR client_order_id=?
        """, (status, now, order_id, order_id))
        self.conn.commit()

    def record_position_from_fill(self, ticker: str, event_ticker: str,
                                  asset: str, side: str, count: int,
                                  price_cents: int, strategy=None,
                                  seconds_to_close=None, fill_latency=None,
                                  vol_regime=None, calibrated_prob=None,
                                  edge=None, kelly_f=None,
                                  is_taker=None, fill_source=None,
                                  execution_method=None,
                                  escalation_type=None, maker_price_cents=None,
                                  maker_wait_seconds=None):
        """Record or accumulate a position from a fill.

        If a position already exists for this ticker, accumulate:
        weighted-average price and sum of contracts/cost.
        """
        now = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        fill_cost = count * price_cents

        existing = self.conn.execute(
            "SELECT count, avg_price_cents, total_cost_cents, opened_at "
            "FROM positions WHERE ticker=? AND status='open'", (ticker,)
        ).fetchone()

        if existing:
            old_count = existing[0]
            old_cost = existing[2]
            new_count = old_count + count
            new_cost = old_cost + fill_cost
            new_avg = round(new_cost / new_count) if new_count else price_cents
            opened_at = existing[3]
            self.conn.execute("""
                UPDATE positions
                SET count=?, avg_price_cents=?, total_cost_cents=?,
                    is_taker=MAX(is_taker, ?), updated_at=?
                WHERE ticker=? AND status='open'
            """, (new_count, new_avg, new_cost, 1 if is_taker else 0, now, ticker))
        else:
            opened_at = now
            self.conn.execute("""
                INSERT OR REPLACE INTO positions
                    (ticker, event_ticker, asset, side, count,
                     avg_price_cents, total_cost_cents, opened_at, updated_at, status,
                     strategy, seconds_to_close, fill_latency_seconds,
                     vol_regime, calibrated_prob, edge, kelly_f,
                     is_taker, fill_source, execution_method,
                     escalation_type, maker_price_cents, maker_wait_seconds)
                VALUES (?,?,?,?,?,?,?,?,?,'open',?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (ticker, event_ticker, asset, side, count,
                  price_cents, fill_cost, now, now,
                  strategy, seconds_to_close, fill_latency,
                  vol_regime, calibrated_prob, edge, kelly_f,
                  1 if is_taker else 0, fill_source, execution_method,
                  escalation_type, maker_price_cents, maker_wait_seconds))
        self.conn.commit()

    def update_garch_params(self, asset: str, omega: float, alpha: float,
                            beta: float, last_variance: float):
        now = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        self.conn.execute("""
            INSERT OR REPLACE INTO garch_params
                (asset, omega, alpha, beta, last_variance, updated_at)
            VALUES (?,?,?,?,?,?)
        """, (asset, omega, alpha, beta, last_variance, now))
        self.conn.commit()

    def update_egarch_params(self, asset: str, omega: float, alpha: float,
                             gamma: float, beta: float,
                             last_log_variance: Optional[float] = None,
                             mle_loglik: Optional[float] = None,
                             mle_converged: bool = False):
        now = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        self.conn.execute("""
            INSERT OR REPLACE INTO egarch_params
                (asset, omega, alpha, gamma, beta, last_log_variance,
                 mle_loglik, mle_converged, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?)
        """, (asset, omega, alpha, gamma, beta, last_log_variance,
              mle_loglik, 1 if mle_converged else 0, now))
        self.conn.commit()

    def close(self):
        self.conn.close()


# ═════════════════════════════════════════════════════════════════════════════
#  CoinbaseFeed
# ═════════════════════════════════════════════════════════════════════════════

class CoinbaseFeed:
    """Coinbase WebSocket feed for real-time crypto prices.

    Runs an asyncio event loop in a daemon thread. Shares price data with
    the synchronous main loop via a lock-protected dict and deque buffers.
    """

    def __init__(self):
        self._prices: Dict[str, float] = {}
        self._buffers: Dict[str, deque] = {
            asset: deque(maxlen=PRICE_BUFFER_SIZE) for asset in ASSETS
        }
        self._lock = threading.Lock()
        self._connected = False
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_event: Optional[asyncio.Event] = None
        # Reverse lookup: "BTC-USD" -> "BTC"
        self._product_to_asset = {v: k for k, v in COINBASE_PRODUCTS.items()}

    # ── Public API (called from main thread) ──────────────────────────────

    def start(self):
        self._thread = threading.Thread(target=self._run_thread, daemon=True)
        self._thread.start()

    def stop(self):
        if self._loop and self._stop_event:
            self._loop.call_soon_threadsafe(self._stop_event.set)

    def get_price(self, asset: str) -> Optional[float]:
        with self._lock:
            return self._prices.get(asset)

    def get_all_prices(self) -> Dict[str, Optional[float]]:
        with self._lock:
            return {a: self._prices.get(a) for a in ASSETS}

    def get_buffer(self, asset: str) -> List[Tuple[float, float]]:
        with self._lock:
            return list(self._buffers.get(asset, []))

    @property
    def is_connected(self) -> bool:
        return self._connected

    # ── Background thread ─────────────────────────────────────────────────

    def _run_thread(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._stop_event = asyncio.Event()
        try:
            self._loop.run_until_complete(self._run())
        except Exception:
            logging.error("Coinbase feed thread crashed", exc_info=True)
        finally:
            self._loop.close()

    async def _run(self):
        """Top-level coroutine: run WS listener and snapshot sampler."""
        await asyncio.gather(
            self._ws_loop(),
            self._snapshot_loop(),
        )

    # ── WebSocket connection with reconnect ───────────────────────────────

    async def _ws_loop(self):
        backoff = 1.0
        max_backoff = 60.0

        while not self._stop_event.is_set():
            try:
                async with websockets.connect(COINBASE_WS_URL) as ws:
                    await ws.send(json.dumps({
                        "type": "subscribe",
                        "product_ids": list(COINBASE_PRODUCTS.values()),
                        "channels": ["ticker"],
                    }))
                    self._connected = True
                    backoff = 1.0  # reset on successful connect
                    logging.info("Coinbase feed connected")

                    async for raw in ws:
                        if self._stop_event.is_set():
                            break
                        self._handle_message(raw)

            except asyncio.CancelledError:
                break
            except Exception as e:
                self._connected = False
                jitter = backoff * random.uniform(0, 0.25)
                wait = backoff + jitter
                logging.warning(
                    f"Coinbase feed disconnected: {e} — "
                    f"reconnecting in {wait:.1f}s"
                )
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=wait
                    )
                    break  # stop_event was set during wait
                except asyncio.TimeoutError:
                    pass  # timeout elapsed, retry
                backoff = min(backoff * 2, max_backoff)

        self._connected = False
        logging.info("Coinbase feed stopped")

    def _handle_message(self, raw: str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        msg_type = data.get("type")
        if msg_type != "ticker":
            return

        product_id = data.get("product_id", "")
        price_str = data.get("price")
        asset = self._product_to_asset.get(product_id)
        if not asset or not price_str:
            return

        try:
            price = float(price_str)
        except (ValueError, TypeError):
            return

        with self._lock:
            self._prices[asset] = price

    # ── 1-second snapshot sampler ─────────────────────────────────────────

    async def _snapshot_loop(self):
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(), timeout=1.0
                )
                break  # stop_event was set
            except asyncio.TimeoutError:
                pass  # 1 second elapsed

            now = time.time()
            with self._lock:
                for asset, price in self._prices.items():
                    self._buffers[asset].append((now, price))


# ═════════════════════════════════════════════════════════════════════════════
#  KalshiFeed — Kalshi WebSocket for fills + orderbook deltas
# ═════════════════════════════════════════════════════════════════════════════

KALSHI_WS_URL = ("wss://api.elections.kalshi.com/trade-api/ws/v2"
                 if os.environ.get("KALSHI_ENV") == "production"
                 else "wss://demo-api.kalshi.co/trade-api/ws/v2")


class KalshiFeed:
    """Kalshi WebSocket feed for real-time fill notifications and orderbook data.

    Runs an asyncio event loop in a daemon thread (same pattern as CoinbaseFeed).
    Shares fill/orderbook data with the synchronous main loop via lock-protected state.

    Channels:
      - fill: instant fill notifications (subscribed once at connect)
      - orderbook_delta: real-time OB snapshots + deltas (per-ticker)
    """

    def __init__(self, api_key: str, private_key):
        self._api_key = api_key
        self._private_key = private_key
        self._lock = threading.Lock()
        self._connected = False
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_event: Optional[asyncio.Event] = None
        # Shared state (lock-protected)
        self._orderbooks: Dict[str, Dict] = {}
        self._recent_fills: deque = deque(maxlen=10000)
        self._subscribed_tickers: Set[str] = set()
        self._pending_subscribes: List[str] = []
        self._pending_unsubscribes: List[str] = []
        self._ws = None

    # ── Public API (called from main thread) ──────────────────────────────

    def start(self):
        self._thread = threading.Thread(target=self._run_thread, daemon=True)
        self._thread.start()

    def stop(self):
        if self._loop and self._stop_event:
            self._loop.call_soon_threadsafe(self._stop_event.set)

    def subscribe_ticker(self, ticker: str):
        with self._lock:
            if ticker not in self._subscribed_tickers:
                self._pending_subscribes.append(ticker)
                self._subscribed_tickers.add(ticker)

    def unsubscribe_ticker(self, ticker: str):
        with self._lock:
            if ticker in self._subscribed_tickers:
                self._pending_unsubscribes.append(ticker)
                self._subscribed_tickers.discard(ticker)
                self._orderbooks.pop(ticker, None)

    def get_orderbook(self, ticker: str) -> Optional[Dict]:
        with self._lock:
            return self._orderbooks.get(ticker)

    def pop_fills(self) -> List[Dict]:
        with self._lock:
            fills = list(self._recent_fills)
            self._recent_fills.clear()
            return fills

    @property
    def is_connected(self) -> bool:
        with self._lock:
            return self._connected

    def get_subscribed_count(self) -> int:
        with self._lock:
            return len(self._subscribed_tickers)

    def get_cached_ob_count(self) -> int:
        with self._lock:
            return len(self._orderbooks)

    def get_all_orderbooks(self) -> Dict[str, Dict]:
        """Return a shallow copy of all cached orderbooks (thread-safe).

        Used by DashboardSnapshotBuilder for dashboard visibility only.
        Does NOT affect trading, scanning, or order execution.
        """
        with self._lock:
            return dict(self._orderbooks)

    # ── Auth ───────────────────────────────────────────────────────────────

    def _create_ws_headers(self) -> Dict[str, str]:
        """Create auth headers for Kalshi WS handshake (same RSA-PSS as REST)."""
        timestamp_ms = str(int(time.time() * 1000))
        # WS auth signs: timestamp + "GET" + "/trade-api/ws/v2"
        message = f"{timestamp_ms}GET/trade-api/ws/v2".encode("utf-8")
        sig = self._private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        signature = base64.b64encode(sig).decode("utf-8")
        return {
            "KALSHI-ACCESS-KEY": self._api_key,
            "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
            "KALSHI-ACCESS-SIGNATURE": signature,
        }

    # ── Background thread ──────────────────────────────────────────────────

    def _run_thread(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._stop_event = asyncio.Event()
        try:
            self._loop.run_until_complete(self._ws_loop())
        except Exception:
            logging.error("Kalshi feed thread crashed", exc_info=True)
        finally:
            self._loop.close()

    async def _ws_loop(self):
        backoff = 1.0
        max_backoff = 60.0

        while not self._stop_event.is_set():
            try:
                headers = self._create_ws_headers()
                async with websockets.connect(
                    KALSHI_WS_URL,
                    additional_headers=headers,
                    ping_interval=30,
                    ping_timeout=10,
                ) as ws:
                    self._ws = ws
                    with self._lock:
                        self._connected = True
                    backoff = 1.0
                    logging.info(f"kalshi_ws_connected: url={KALSHI_WS_URL}")

                    # Subscribe to fills channel (all markets)
                    await ws.send(json.dumps({
                        "id": 1,
                        "cmd": "subscribe",
                        "params": {"channels": ["fill"]},
                    }))
                    logging.debug("kalshi_ws_subscribe: channel=fill")

                    # Re-subscribe to any tickers that were active before reconnect
                    with self._lock:
                        resub_tickers = list(self._subscribed_tickers)
                    for ticker in resub_tickers:
                        await self._send_ob_subscribe(ws, ticker)

                    # Message loop with periodic subscribe/unsubscribe processing
                    async for raw in ws:
                        if self._stop_event.is_set():
                            break
                        self._handle_message(raw)
                        # Process pending subscriptions
                        await self._process_pending_subs(ws)

            except asyncio.CancelledError:
                break
            except Exception as e:
                with self._lock:
                    self._connected = False
                    self._orderbooks.clear()
                self._ws = None
                jitter = backoff * random.uniform(0, 0.25)
                wait = backoff + jitter
                logging.warning(
                    f"kalshi_ws_disconnected: reason={e} reconnect_backoff={wait:.1f}s"
                )
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=wait
                    )
                    break
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, max_backoff)

        with self._lock:
            self._connected = False
        self._ws = None
        logging.info("Kalshi feed stopped")

    async def _send_ob_subscribe(self, ws, ticker: str):
        await ws.send(json.dumps({
            "id": 2,
            "cmd": "subscribe",
            "params": {
                "channels": ["orderbook_delta"],
                "market_tickers": [ticker],
            },
        }))
        logging.debug(f"kalshi_ws_subscribe: ticker={ticker} channel=orderbook_delta")

    async def _send_ob_unsubscribe(self, ws, ticker: str):
        await ws.send(json.dumps({
            "id": 3,
            "cmd": "unsubscribe",
            "params": {
                "channels": ["orderbook_delta"],
                "market_tickers": [ticker],
            },
        }))

    async def _process_pending_subs(self, ws):
        with self._lock:
            subs = list(self._pending_subscribes)
            self._pending_subscribes.clear()
            unsubs = list(self._pending_unsubscribes)
            self._pending_unsubscribes.clear()

        for ticker in subs:
            try:
                await self._send_ob_subscribe(ws, ticker)
            except Exception:
                logging.debug(f"Failed to subscribe to {ticker}", exc_info=True)

        for ticker in unsubs:
            try:
                await self._send_ob_unsubscribe(ws, ticker)
            except Exception:
                logging.debug(f"Failed to unsubscribe from {ticker}", exc_info=True)

    def _handle_message(self, raw: str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        msg_type = data.get("type")

        if msg_type == "fill":
            self._handle_fill(data)
        elif msg_type == "orderbook_snapshot":
            self._handle_ob_snapshot(data)
        elif msg_type == "orderbook_delta":
            self._handle_ob_delta(data)
        # Ignore subscription confirmations, errors, etc.

    def _handle_fill(self, data: Dict):
        """Process a fill notification from the WebSocket."""
        try:
            msg = data.get("msg", {})
            fill_info = {
                "order_id": msg.get("order_id"),
                "ticker": msg.get("ticker"),
                "side": msg.get("side"),
                "action": msg.get("action"),
                "count": msg.get("count"),
                "yes_price": msg.get("yes_price"),
                "no_price": msg.get("no_price"),
                "trade_id": msg.get("trade_id"),
                "ts": time.time(),
            }
            with self._lock:
                self._recent_fills.append(fill_info)
        except Exception:
            logging.warning("Failed to parse WS fill message", exc_info=True)

    def _handle_ob_snapshot(self, data: Dict):
        """Replace cached orderbook with full snapshot."""
        try:
            msg = data.get("msg", {})
            ticker = msg.get("market_ticker")
            if not ticker:
                return
            with self._lock:
                self._orderbooks[ticker] = {
                    "yes": msg.get("yes", []),
                    "no": msg.get("no", []),
                    "ts": time.time(),
                }
        except Exception:
            logging.warning("Failed to parse WS OB snapshot", exc_info=True)

    def _handle_ob_delta(self, data: Dict):
        """Apply incremental delta to cached orderbook."""
        try:
            msg = data.get("msg", {})
            ticker = msg.get("market_ticker")
            if not ticker:
                return
            with self._lock:
                ob = self._orderbooks.get(ticker)
                if ob is None:
                    # No snapshot yet — store delta as partial
                    self._orderbooks[ticker] = {
                        "yes": msg.get("yes", []),
                        "no": msg.get("no", []),
                        "ts": time.time(),
                    }
                    return
                # Apply delta: merge price levels
                for side in ("yes", "no"):
                    delta_levels = msg.get(side, [])
                    if not delta_levels:
                        continue
                    existing = {self._level_price(l): l for l in ob.get(side, [])}
                    for level in delta_levels:
                        price = self._level_price(level)
                        qty = self._level_qty(level)
                        if qty == 0:
                            existing.pop(price, None)
                        else:
                            existing[price] = level
                    ob[side] = list(existing.values())
                ob["ts"] = time.time()
        except Exception:
            logging.warning("Failed to apply WS OB delta", exc_info=True)

    @staticmethod
    def _level_price(level) -> int:
        if isinstance(level, (list, tuple)) and len(level) >= 1:
            return int(level[0])
        if isinstance(level, dict):
            return int(level.get("price", 0))
        return 0

    @staticmethod
    def _level_qty(level) -> int:
        if isinstance(level, (list, tuple)) and len(level) >= 2:
            return int(level[1])
        if isinstance(level, dict):
            return int(level.get("quantity", 0))
        return 0


# ═════════════════════════════════════════════════════════════════════════════
#  DeribitDVOLFetcher
# ═════════════════════════════════════════════════════════════════════════════

class DeribitDVOLFetcher:
    """Daemon thread that fetches Deribit DVOL index for BTC/ETH."""

    def __init__(self):
        self._cache: Dict[str, Tuple[float, float]] = {}   # asset → (dvol_5s, fetch_time)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._hourly_dvol: Dict[str, deque] = {
            a: deque(maxlen=DVOL_HOURLY_AVG_MAXLEN) for a in DERIBIT_DVOL_CURRENCIES
        }

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def get_dvol(self, asset: str) -> Optional[float]:
        """Return cached DVOL in per-5-second scale, or None if stale/missing."""
        with self._lock:
            entry = self._cache.get(asset)
        if entry is None:
            return None
        dvol_5s, fetch_time = entry
        if time.time() - fetch_time > DVOL_CACHE_TTL:
            return None
        return dvol_5s

    def get_dvol_hourly_avg(self, asset: str) -> Optional[float]:
        """Return 1h rolling average of DVOL (per-5s scale), or None if insufficient data."""
        with self._lock:
            buf = self._hourly_dvol.get(asset)
            if buf is None or len(buf) < DVOL_HOURLY_AVG_MIN:
                return None
            return sum(buf) / len(buf)

    def _run(self):
        while not self._stop.is_set():
            for currency_key, currency in DERIBIT_DVOL_CURRENCIES.items():
                try:
                    dvol = self._fetch_latest_dvol(currency)
                    if dvol is not None:
                        dvol_5s = dvol * DVOL_ANNUALIZED_TO_5S
                        with self._lock:
                            self._cache[currency_key] = (dvol_5s, time.time())
                            self._hourly_dvol[currency_key].append(dvol_5s)
                except Exception:
                    logging.debug(f"DVOL fetch failed for {currency}", exc_info=True)
            self._stop.wait(timeout=DVOL_FETCH_INTERVAL)

    def _fetch_latest_dvol(self, currency: str) -> Optional[float]:
        """Fetch latest DVOL from Deribit. Returns annualized vol as decimal (0.57 = 57%)."""
        now_ms = int(time.time() * 1000)
        one_hour_ago_ms = now_ms - 3600 * 1000
        params = {
            "currency": currency,
            "start_timestamp": one_hour_ago_ms,
            "end_timestamp": now_ms,
            "resolution": 1,
        }
        resp = requests.get(DERIBIT_DVOL_URL, params=params, timeout=DVOL_REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        result = data.get("result", {})
        candles = result.get("data", [])
        if not candles:
            return None
        # Each candle: [timestamp, open, high, low, close]
        last_candle = candles[-1]
        close_dvol = last_candle[4]   # close value
        return close_dvol / 100.0     # percentage → decimal


# ═════════════════════════════════════════════════════════════════════════════
#  CrossExchangeFeed
# ═════════════════════════════════════════════════════════════════════════════

class CrossExchangeFeed:
    """WebSocket feeds for Binance, Kraken, and Bybit spot prices.

    Runs a single daemon thread with one asyncio event loop managing 3 WebSocket
    connections. Records 1-second snapshots comparing other exchanges to Coinbase
    for lead/lag detection.
    """

    def __init__(self, coinbase_feed: CoinbaseFeed):
        self._coinbase = coinbase_feed
        self._prices: Dict[str, Dict[str, float]] = {
            "binance": {}, "kraken": {}, "bybit": {},
        }
        self._last_update: Dict[str, Dict[str, float]] = {
            "binance": {}, "kraken": {}, "bybit": {},
        }
        self._snapshots: Dict[str, deque] = {
            a: deque(maxlen=CROSS_EXCHANGE_BUFFER_SIZE) for a in ASSETS
        }
        self._lock = threading.Lock()
        self._connected: Dict[str, bool] = {
            "binance": False, "kraken": False, "bybit": False,
        }
        # Reverse lookups
        self._binance_map = {
            v["binance"].upper(): k for k, v in CROSS_EXCHANGE_SYMBOLS.items()
        }
        self._kraken_map = {
            v["kraken"]: k for k, v in CROSS_EXCHANGE_SYMBOLS.items()
        }
        self._bybit_map = {
            v["bybit"]: k for k, v in CROSS_EXCHANGE_SYMBOLS.items()
        }
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_event: Optional[asyncio.Event] = None

    # ── Public API ─────────────────────────────────────────────────────

    def start(self):
        self._thread = threading.Thread(target=self._run_thread, daemon=True)
        self._thread.start()

    def stop(self):
        if self._loop and self._stop_event:
            self._loop.call_soon_threadsafe(self._stop_event.set)

    def get_prices(self, asset: str) -> Dict[str, Optional[float]]:
        """Latest price per exchange, None if stale."""
        now = time.time()
        result: Dict[str, Optional[float]] = {}
        with self._lock:
            for ex in ("binance", "kraken", "bybit"):
                price = self._prices[ex].get(asset)
                ts = self._last_update[ex].get(asset, 0.0)
                if price is not None and (now - ts) < CROSS_EXCHANGE_STALE_SECONDS:
                    result[ex] = price
                else:
                    result[ex] = None
        return result

    def get_lead_lag(self, asset: str) -> Dict:
        """Consensus analysis over snapshot buffer."""
        with self._lock:
            snaps = list(self._snapshots.get(asset, []))

        if not snaps:
            return {
                "exchanges_above": 0, "exchanges_below": 0,
                "max_premium_pct": 0.0, "max_discount_pct": 0.0,
                "consensus_direction": "none", "exchange_premia": {},
            }

        # Compute average premium per exchange over buffer
        ex_totals: Dict[str, List[float]] = {"binance": [], "kraken": [], "bybit": []}
        for _ts, cb_price, ex_prices in snaps:
            if cb_price is None or cb_price <= 0:
                continue
            for ex, ep in ex_prices.items():
                if ep is not None:
                    ex_totals[ex].append((ep - cb_price) / cb_price)

        exchange_premia: Dict[str, float] = {}
        for ex, devs in ex_totals.items():
            if devs:
                exchange_premia[ex] = sum(devs) / len(devs)

        above = 0
        below = 0
        max_premium = 0.0
        max_discount = 0.0
        for ex, avg_dev in exchange_premia.items():
            if avg_dev > CROSS_EXCHANGE_LEAD_THRESHOLD:
                above += 1
                max_premium = max(max_premium, avg_dev)
            elif avg_dev < -CROSS_EXCHANGE_LEAD_THRESHOLD:
                below += 1
                max_discount = max(max_discount, abs(avg_dev))

        if above >= CROSS_EXCHANGE_CONSENSUS_MIN:
            direction = "above"
        elif below >= CROSS_EXCHANGE_CONSENSUS_MIN:
            direction = "below"
        elif above > 0 or below > 0:
            direction = "mixed"
        else:
            direction = "none"

        return {
            "exchanges_above": above,
            "exchanges_below": below,
            "max_premium_pct": max_premium,
            "max_discount_pct": max_discount,
            "consensus_direction": direction,
            "exchange_premia": exchange_premia,
        }

    # ── Background thread ──────────────────────────────────────────────

    def _run_thread(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._stop_event = asyncio.Event()
        try:
            self._loop.run_until_complete(self._run())
        except Exception:
            logging.error("CrossExchangeFeed thread crashed", exc_info=True)
        finally:
            self._loop.close()

    async def _run(self):
        await asyncio.gather(
            self._ws_binance(),
            self._ws_kraken(),
            self._ws_bybit(),
            self._snapshot_loop(),
        )

    # ── Binance WebSocket ──────────────────────────────────────────────

    async def _ws_binance(self):
        streams = "/".join(
            f"{v['binance']}@ticker" for v in CROSS_EXCHANGE_SYMBOLS.values()
        )
        url = f"{BINANCE_WS_URL}?streams={streams}"
        backoff = 1.0
        while not self._stop_event.is_set():
            try:
                async with websockets.connect(url) as ws:
                    self._connected["binance"] = True
                    backoff = 1.0
                    logging.info("Binance feed connected")
                    async for raw in ws:
                        if self._stop_event.is_set():
                            break
                        self._handle_binance(raw)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._connected["binance"] = False
                wait = backoff + backoff * random.uniform(0, 0.25)
                logging.warning(f"Binance feed disconnected: {e} — reconnecting in {wait:.1f}s")
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=wait)
                    break
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, 60.0)
        self._connected["binance"] = False

    def _handle_binance(self, raw: str):
        try:
            msg = json.loads(raw)
            data = msg.get("data", {})
            symbol = data.get("s", "")
            price_str = data.get("c")  # last price
            if not symbol or not price_str:
                return
            asset = self._binance_map.get(symbol)
            if asset is None:
                return
            price = float(price_str)
            with self._lock:
                self._prices["binance"][asset] = price
                self._last_update["binance"][asset] = time.time()
        except Exception:
            pass

    # ── Kraken WebSocket ───────────────────────────────────────────────

    async def _ws_kraken(self):
        symbols = [v["kraken"] for v in CROSS_EXCHANGE_SYMBOLS.values()]
        backoff = 1.0
        while not self._stop_event.is_set():
            try:
                async with websockets.connect(KRAKEN_WS_URL) as ws:
                    await ws.send(json.dumps({
                        "method": "subscribe",
                        "params": {"channel": "ticker", "symbol": symbols},
                    }))
                    self._connected["kraken"] = True
                    backoff = 1.0
                    logging.info("Kraken feed connected")
                    async for raw in ws:
                        if self._stop_event.is_set():
                            break
                        self._handle_kraken(raw)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._connected["kraken"] = False
                wait = backoff + backoff * random.uniform(0, 0.25)
                logging.warning(f"Kraken feed disconnected: {e} — reconnecting in {wait:.1f}s")
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=wait)
                    break
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, 60.0)
        self._connected["kraken"] = False

    def _handle_kraken(self, raw: str):
        try:
            msg = json.loads(raw)
            channel = msg.get("channel")
            if channel != "ticker":
                return
            for entry in msg.get("data", []):
                symbol = entry.get("symbol", "")
                price = entry.get("last")
                asset = self._kraken_map.get(symbol)
                if asset is None or price is None:
                    continue
                price = float(price)
                with self._lock:
                    self._prices["kraken"][asset] = price
                    self._last_update["kraken"][asset] = time.time()
        except Exception:
            pass

    # ── Bybit WebSocket ────────────────────────────────────────────────

    async def _ws_bybit(self):
        args = [f"tickers.{v['bybit']}" for v in CROSS_EXCHANGE_SYMBOLS.values()]
        backoff = 1.0
        while not self._stop_event.is_set():
            try:
                async with websockets.connect(BYBIT_WS_URL) as ws:
                    await ws.send(json.dumps({
                        "op": "subscribe",
                        "args": args,
                    }))
                    self._connected["bybit"] = True
                    backoff = 1.0
                    logging.info("Bybit feed connected")
                    async for raw in ws:
                        if self._stop_event.is_set():
                            break
                        self._handle_bybit(raw)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self._connected["bybit"] = False
                wait = backoff + backoff * random.uniform(0, 0.25)
                logging.warning(f"Bybit feed disconnected: {e} — reconnecting in {wait:.1f}s")
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=wait)
                    break
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, 60.0)
        self._connected["bybit"] = False

    def _handle_bybit(self, raw: str):
        try:
            msg = json.loads(raw)
            topic = msg.get("topic", "")
            if not topic.startswith("tickers."):
                return
            symbol = topic.replace("tickers.", "")
            data = msg.get("data", {})
            price_str = data.get("lastPrice")
            if not price_str:
                return
            asset = self._bybit_map.get(symbol)
            if asset is None:
                return
            price = float(price_str)
            with self._lock:
                self._prices["bybit"][asset] = price
                self._last_update["bybit"][asset] = time.time()
        except Exception:
            pass

    # ── 1-second snapshot sampler ──────────────────────────────────────

    async def _snapshot_loop(self):
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=1.0)
                break
            except asyncio.TimeoutError:
                pass
            now = time.time()
            for asset in ASSETS:
                cb_price = self._coinbase.get_price(asset)
                with self._lock:
                    ex_prices = {
                        ex: self._prices[ex].get(asset)
                        for ex in ("binance", "kraken", "bybit")
                    }
                    self._snapshots[asset].append((now, cb_price, ex_prices))


# ═════════════════════════════════════════════════════════════════════════════
#  CoinGlassFetcher
# ═════════════════════════════════════════════════════════════════════════════

class CoinGlassFetcher:
    """Daemon thread that fetches funding rates from CoinGlass API."""

    def __init__(self):
        self._api_key = os.environ.get("COINGLASS_API_KEY", "")
        self._cache: Dict[str, Dict] = {}  # asset -> {"funding_rate": float, "fetch_time": float}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        if not self._api_key:
            logging.info("COINGLASS_API_KEY not set — CoinGlass funding rates disabled")
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def get_funding_rate(self, asset: str) -> Optional[float]:
        """Return cached average funding rate, or None if stale/missing."""
        with self._lock:
            entry = self._cache.get(asset)
        if entry is None:
            return None
        if time.time() - entry["fetch_time"] > COINGLASS_CACHE_TTL:
            return None
        return entry["funding_rate"]

    def _run(self):
        while not self._stop.is_set():
            for asset, symbol in COINGLASS_SYMBOLS.items():
                try:
                    rate = self._fetch_funding(symbol)
                    if rate is not None:
                        with self._lock:
                            self._cache[asset] = {
                                "funding_rate": rate,
                                "fetch_time": time.time(),
                            }
                except Exception:
                    logging.debug(f"CoinGlass fetch failed for {symbol}", exc_info=True)
            self._stop.wait(timeout=COINGLASS_FETCH_INTERVAL)

    def _fetch_funding(self, symbol: str) -> Optional[float]:
        """Fetch current funding rates from CoinGlass, return average across exchanges."""
        url = f"{COINGLASS_API_URL}/futures/funding/current"
        headers = {"CG-API-KEY": self._api_key}
        resp = requests.get(
            url, params={"symbol": symbol}, headers=headers,
            timeout=COINGLASS_REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        body = resp.json()
        data_list = body.get("data", [])
        if not data_list:
            return None
        rates = []
        for entry in data_list:
            rate = entry.get("rate")
            if rate is not None:
                try:
                    rates.append(float(rate))
                except (ValueError, TypeError):
                    pass
        if not rates:
            return None
        return sum(rates) / len(rates)


# ═════════════════════════════════════════════════════════════════════════════
#  OrderFlowEngine
# ═════════════════════════════════════════════════════════════════════════════

class OrderFlowEngine:
    """Aggregates cross-exchange and derivatives signals into a probability adjustment."""

    def __init__(self, cross_feed=None, coinglass=None, kalshi_oft=None):
        self._cross = cross_feed
        self._coinglass = coinglass
        self._kalshi_oft = kalshi_oft

    def get_signals(self, asset: str, **kwargs) -> Dict:
        """Compute order flow adjustment for the given asset.

        Returns:
            {
                "prob_adjustment": float,
                "confidence": "high"|"moderate"|"low"|"none",
                "signals": {
                    "cross_exchange": {...lead_lag dict...},
                    "funding": {"rate": float|None, "level": str},
                },
                "adjustments_applied": [str, ...],
            }
        """
        adjustments: List[Tuple[str, float]] = []
        cross_exchange = {}
        funding_info = {"rate": None, "level": "unknown"}

        # 1. Cross-exchange consensus
        if self._cross is not None:
            try:
                lead_lag = self._cross.get_lead_lag(asset)
                cross_exchange = lead_lag
                direction = lead_lag.get("consensus_direction", "none")
                above = lead_lag.get("exchanges_above", 0)
                below = lead_lag.get("exchanges_below", 0)

                if direction == "above" and above >= CROSS_EXCHANGE_CONSENSUS_MIN:
                    adjustments.append((
                        f"consensus_above_{above}ex",
                        OFA_CONSENSUS_BOOST,
                    ))
                elif direction == "below" and below >= CROSS_EXCHANGE_CONSENSUS_MIN:
                    adjustments.append((
                        f"consensus_below_{below}ex",
                        OFA_CONSENSUS_REDUCE,
                    ))
                elif direction == "mixed":
                    # Weaker signal: at least one exchange leads
                    if above > below:
                        adjustments.append(("lead_above_mixed", OFA_LEAD_BOOST))
                    elif below > above:
                        adjustments.append(("lead_below_mixed", -OFA_LEAD_BOOST))
            except Exception:
                logging.debug("CrossExchangeFeed.get_lead_lag failed", exc_info=True)

        # 2. Funding rate
        if self._coinglass is not None:
            try:
                rate = self._coinglass.get_funding_rate(asset)
                if rate is not None:
                    abs_rate = abs(rate)
                    if abs_rate >= FUNDING_RATE_EXTREME:
                        funding_info = {"rate": rate, "level": "extreme"}
                        adjustments.append((
                            f"extreme_funding_{rate:+.6f}",
                            OFA_EXTREME_FUNDING_REDUCE,
                        ))
                    elif abs_rate >= FUNDING_RATE_ELEVATED:
                        funding_info = {"rate": rate, "level": "elevated"}
                        adjustments.append((
                            f"elevated_funding_{rate:+.6f}",
                            OFA_ELEVATED_FUNDING_REDUCE,
                        ))
                    else:
                        funding_info = {"rate": rate, "level": "normal"}
                else:
                    funding_info = {"rate": None, "level": "unknown"}
            except Exception:
                logging.debug("CoinGlassFetcher.get_funding_rate failed", exc_info=True)

        # 3. Kalshi orderbook flow
        kalshi_flow = {}
        if self._kalshi_oft is not None:
            try:
                ticker = kwargs.get("ticker")
                if ticker:
                    koft = self._kalshi_oft.get_signals(ticker)
                    if koft is not None:
                        kalshi_flow = koft
                        if not KALSHI_OFT_SHADOW_MODE and koft["prob_adjustment"] != 0:
                            adjustments.append(("kalshi_oft", koft["prob_adjustment"]))
            except Exception:
                logging.debug("KalshiOFT.get_signals failed", exc_info=True)

        # 4. Sum and clamp
        total = sum(v for _, v in adjustments)
        total = max(-OFA_MAX_ADJUSTMENT, min(OFA_MAX_ADJUSTMENT, total))

        # 5. Confidence
        abs_total = abs(total)
        if abs_total >= 0.015:
            confidence = "high"
        elif abs_total >= 0.005:
            confidence = "moderate"
        elif abs_total > 0:
            confidence = "low"
        else:
            confidence = "none"

        return {
            "prob_adjustment": total,
            "confidence": confidence,
            "signals": {
                "cross_exchange": cross_exchange,
                "funding": funding_info,
                "kalshi_orderbook": kalshi_flow,
            },
            "adjustments_applied": [
                f"{name}: {val:+.3f}" for name, val in adjustments
            ],
        }


class KalshiOrderFlowTracker:
    """Tracks Kalshi orderbook snapshots over time for flow signals.

    Records full depth-5 snapshots from the scanner's existing orderbook
    fetches (no additional API calls). Computes:
    - Bid/ask imbalance ratio (YES depth vs total)
    - Depth velocity (total depth change rate)
    - Spread dynamics (bid-ask spread trend)
    - Ask convergence velocity (cents/sec)
    """

    def __init__(self):
        self._snapshots: Dict[str, deque] = {}
        self._last_seen: Dict[str, float] = {}
        self._last_log: Dict[str, float] = {}

    def record_snapshot(self, ticker: str, ob_data: Dict, best_ask: int):
        """Record orderbook snapshot. Called from scanner after each OB fetch.

        ob_data format: {"no": [[price_cents, qty], ...], "yes": [[price_cents, qty], ...]}
        """
        now = time.time()
        if ticker not in self._snapshots:
            self._snapshots[ticker] = deque(maxlen=KALSHI_OFT_BUFFER_SIZE)

        # Sum depth per side
        yes_total_qty = 0
        no_total_qty = 0
        best_yes_bid_price = 0

        for entry in (ob_data.get("yes") or []):
            if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                price, qty = int(entry[0]), int(entry[1])
                yes_total_qty += qty
                if price > best_yes_bid_price:
                    best_yes_bid_price = price

        for entry in (ob_data.get("no") or []):
            if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                no_total_qty += int(entry[1])

        spread = (best_ask - best_yes_bid_price) if best_yes_bid_price > 0 else 99

        self._snapshots[ticker].append({
            "ts": now,
            "best_ask": best_ask,
            "best_yes_bid": best_yes_bid_price,
            "yes_total_qty": yes_total_qty,
            "no_total_qty": no_total_qty,
            "total_depth": yes_total_qty + no_total_qty,
            "spread": spread,
        })
        self._last_seen[ticker] = now

    def get_signals(self, ticker: str) -> Optional[Dict]:
        """Compute order flow signals from snapshot history. Returns None if insufficient data."""
        snaps = self._snapshots.get(ticker)
        if not snaps or len(snaps) < KALSHI_OFT_MIN_SNAPSHOTS:
            return None

        snap_list = list(snaps)
        latest = snap_list[-1]
        earliest = snap_list[0]
        time_span = latest["ts"] - earliest["ts"]
        if time_span <= 0:
            return None

        # 1. Imbalance: YES bids / total depth
        total_qty = latest["yes_total_qty"] + latest["no_total_qty"]
        imbalance = latest["yes_total_qty"] / total_qty if total_qty > 0 else 0.5

        if imbalance >= KALSHI_OFT_IMBALANCE_STRONG:
            imbalance_level = "strong_buy"
        elif imbalance <= KALSHI_OFT_IMBALANCE_WEAK:
            imbalance_level = "strong_sell"
        else:
            imbalance_level = "neutral"

        # 2. Depth velocity
        depth_velocity = (latest["total_depth"] - earliest["total_depth"]) / time_span
        depth_pct_change = ((latest["total_depth"] - earliest["total_depth"])
                           / earliest["total_depth"]) if earliest["total_depth"] > 0 else 0.0
        depth_drain = depth_pct_change < KALSHI_OFT_DEPTH_DRAIN_PCT

        # 3. Spread trend
        spread_trend = (latest["spread"] - earliest["spread"]) / time_span

        # 4. Ask velocity
        ask_velocity = (latest["best_ask"] - earliest["best_ask"]) / time_span

        # 5. Prob adjustment (shadow or live)
        adjustments = []
        if imbalance_level == "strong_buy":
            adjustments.append(("kalshi_imbalance_buy", OFA_KALSHI_IMBALANCE_BOOST))
        elif imbalance_level == "strong_sell":
            adjustments.append(("kalshi_imbalance_sell", OFA_KALSHI_IMBALANCE_REDUCE))
        if depth_drain and ask_velocity > 0:
            adjustments.append(("kalshi_depth_drain", OFA_KALSHI_DEPTH_DRAIN_BOOST))
        if ask_velocity > 0.5:
            adjustments.append(("kalshi_convergence", OFA_KALSHI_CONVERGENCE_BOOST))

        total_adj = max(-0.02, min(0.02, sum(v for _, v in adjustments)))

        # Confidence
        n_snaps = len(snap_list)
        if n_snaps >= 30 and total_qty >= 20:
            confidence = "high"
        elif n_snaps >= 15 or total_qty >= 10:
            confidence = "moderate"
        else:
            confidence = "low"

        result = {
            "imbalance_ratio": round(imbalance, 4),
            "imbalance_level": imbalance_level,
            "depth_velocity": round(depth_velocity, 2),
            "depth_drain": depth_drain,
            "depth_pct_change": round(depth_pct_change, 4),
            "spread_current": latest["spread"],
            "spread_trend": round(spread_trend, 4),
            "ask_velocity": round(ask_velocity, 4),
            "prob_adjustment": round(total_adj, 6),
            "adjustments_applied": [f"{n}: {v:+.3f}" for n, v in adjustments],
            "n_snapshots": n_snaps,
            "confidence": confidence,
        }

        # Periodic per-ticker diagnostic logging
        now = time.time()
        last_log = self._last_log.get(ticker, 0.0)
        if now - last_log >= KALSHI_OFT_LOG_INTERVAL:
            self._last_log[ticker] = now
            adj_str = ", ".join(f"{n}: {v:+.3f}" for n, v in adjustments) if adjustments else "none"
            logging.info(
                "KalshiOFT %s: imbal=%.3f (%s) depth_vel=%.1f depth_pct=%.1f%% "
                "spread=%d trend=%.3f ask_vel=%.3f adj=%.4f [%s] snaps=%d conf=%s shadow=%s",
                ticker, imbalance, imbalance_level, depth_velocity,
                depth_pct_change * 100, latest["spread"], spread_trend,
                ask_velocity, total_adj, adj_str, n_snaps, confidence,
                KALSHI_OFT_SHADOW_MODE,
            )

        return result

    def cleanup_stale(self, active_tickers: Set[str]):
        """Evict tickers no longer in active windows."""
        now = time.time()
        stale = [t for t, ts in self._last_seen.items()
                 if now - ts > KALSHI_OFT_STALE_SECONDS or t not in active_tickers]
        for t in stale:
            self._snapshots.pop(t, None)
            self._last_seen.pop(t, None)

    def get_tracked_count(self) -> int:
        return len(self._snapshots)


# ═════════════════════════════════════════════════════════════════════════════
#  VolatilityEngine
# ═════════════════════════════════════════════════════════════════════════════

class VolatilityEngine:
    """Realized Kernel + Deribit DVOL volatility engine.

    Uses microstructure-noise-robust Realized Kernel (Barndorff-Nielsen 2008),
    bipower variation for jump separation, and optional Deribit DVOL blending.
    Maintains its own rolling buffer of log returns per asset (up to 15 min).
    """

    def __init__(self, feed: CoinbaseFeed, dvol_fetcher: Optional[DeribitDVOLFetcher] = None,
                 egarch_estimator: Optional['EGARCHEstimator'] = None,
                 mz_tracker: Optional['MincerZarnowitzTracker'] = None):
        self._feed = feed
        self._dvol = dvol_fetcher
        self._egarch = egarch_estimator
        self._mz = mz_tracker
        self._egarch_blend_last_log: Dict[str, float] = {}
        self._returns: Dict[str, deque] = {
            a: deque(maxlen=VOL_WINDOW_15MIN) for a in ASSETS
        }
        self._last_return_time: Dict[str, float] = {}
        self._jump_events: Dict[str, List[float]] = {a: [] for a in ASSETS}
        self._cache: Dict[str, Optional[Dict]] = {}
        # ── Adaptive jump detection state ──
        self._adaptive_tick_counter: Dict[str, int] = {a: 0 for a in ASSETS}
        self._adaptive_returns_15s: Dict[str, deque] = {
            a: deque(maxlen=JUMP_ADAPTIVE_PCTILE_WINDOW) for a in ASSETS
        }
        self._adaptive_ewma_var: Dict[str, Optional[float]] = {a: None for a in ASSETS}
        self._adaptive_abs_returns: Dict[str, deque] = {
            a: deque(maxlen=JUMP_ADAPTIVE_PCTILE_WINDOW) for a in ASSETS
        }
        self._adaptive_jump_events: Dict[str, List] = {a: [] for a in ASSETS}
        self._adaptive_last_save: float = 0.0
        self._adaptive_total_jumps: Dict[str, int] = {a: 0 for a in ASSETS}
        self._load_adaptive_state()
        # Adaptive RK bandwidth diagnostics
        self._rk_noise_history: Dict[str, deque] = {
            a: deque(maxlen=720) for a in ASSETS  # 720 = 1h of 5s ticks
        }
        self._rk_prev_omega_sq: Dict[str, float] = {}
        self._rk_last_summary: Dict[str, float] = {a: 0.0 for a in ASSETS}
        self._rk_adaptive_diff_count: Dict[str, int] = {a: 0 for a in ASSETS}
        self._rk_delta_5_accum: Dict[str, deque] = {a: deque(maxlen=720) for a in ASSETS}
        self._rk_delta_15_accum: Dict[str, deque] = {a: deque(maxlen=720) for a in ASSETS}

    def update(self, asset: str, seconds_to_close: Optional[float] = None) -> Optional[Dict]:
        """Called every tick. Computes a new log return every 5s, returns vol estimate."""
        buf = self._feed.get_buffer(asset)
        if len(buf) < VOL_RETURN_INTERVAL + 1:
            return None

        now = time.time()
        last_time = self._last_return_time.get(asset, 0)

        # Only compute a new return every VOL_RETURN_INTERVAL seconds
        if now - last_time >= VOL_RETURN_INTERVAL:
            current_ts, current_price = buf[-1]
            target_ts = current_ts - VOL_RETURN_INTERVAL

            # Find the snapshot closest to 5 seconds ago
            past_price = None
            for ts, p in reversed(buf):
                if ts <= target_ts:
                    past_price = p
                    break

            if past_price and past_price > 0 and current_price > 0:
                log_return = math.log(current_price / past_price)
                self._returns[asset].append(log_return)
                self._last_return_time[asset] = now

                # Feed return to EGARCH
                if self._egarch is not None:
                    self._egarch.record_return(asset, log_return)

                # Adaptive jump detection (Tier 1: subsample to 15s)
                adaptive_result = None
                return_15s = self._adaptive_subsample_return(asset, log_return, now)
                if return_15s is not None:
                    adaptive_result = self._adaptive_jump_test(asset, return_15s, now)
                    if adaptive_result["is_jump"]:
                        boost = (JUMP_ADAPTIVE_DECAY_MAX_BOOST
                                 * min(JUMP_ADAPTIVE_MAG_CAP, adaptive_result["magnitude_ratio"])
                                 / JUMP_ADAPTIVE_MAG_SCALE_BASE)
                        self._record_adaptive_jump_event(asset, now, adaptive_result["magnitude_ratio"])
                        if JUMP_ADAPTIVE_SHADOW_MODE:
                            logging.info(
                                "Jump detected [adaptive-shadow]: %s r_15s=%.6f "
                                "ewma_σ=%.6f threshold=%.6f ratio=%.2f n=%d boost=%.3f",
                                asset, adaptive_result["return_15s"],
                                adaptive_result["ewma_sigma"],
                                adaptive_result["effective_threshold"],
                                adaptive_result["magnitude_ratio"],
                                adaptive_result["n_obs_15s"], boost)
                        else:
                            self._record_jump_event(asset, now)
                            logging.info(
                                "Jump detected [adaptive]: %s r_15s=%.6f "
                                "ewma_σ=%.6f threshold=%.6f ratio=%.2f n=%d boost=%.3f",
                                asset, adaptive_result["return_15s"],
                                adaptive_result["ewma_sigma"],
                                adaptive_result["effective_threshold"],
                                adaptive_result["magnitude_ratio"],
                                adaptive_result["n_obs_15s"], boost)
                    elif adaptive_result["n_obs_15s"] % 60 == 0 and adaptive_result["n_obs_15s"] > 0:
                        logging.info(
                            "Adaptive jump health %s: ewma_σ=%.6f pctile=%.6f "
                            "sigma_thresh=%.6f n=%d total_jumps=%d",
                            asset, adaptive_result["ewma_sigma"],
                            adaptive_result["pctile_threshold"]
                            if adaptive_result["pctile_threshold"] != float('inf') else 0.0,
                            adaptive_result["sigma_threshold"]
                            if adaptive_result["sigma_threshold"] != float('inf') else 0.0,
                            adaptive_result["n_obs_15s"],
                            self._adaptive_total_jumps.get(asset, 0))
                    # Periodic save
                    now_save = time.time()
                    if now_save - self._adaptive_last_save >= JUMP_ADAPTIVE_SAVE_INTERVAL:
                        self._save_adaptive_state()
                        self._adaptive_last_save = now_save

                # Check for jump against current estimate (before updating cache)
                estimate = self._compute(asset, now, seconds_to_close)
                if estimate and estimate["blended_rv"] > 0:
                    legacy_jump = abs(log_return) > JUMP_THRESHOLD_MULTIPLIER * estimate["blended_rv"]

                    # Legacy jump detection (3x blended_rv threshold)
                    if JUMP_ADAPTIVE_SHADOW_MODE:
                        # Adaptive in shadow: legacy test drives regime
                        if legacy_jump:
                            self._record_jump_event(asset, now)
                            logging.info(
                                "Jump detected [legacy]: %s return=%.6f rv=%.6f",
                                asset, log_return, estimate["blended_rv"])
                    else:
                        # Adaptive drives regime — legacy logged at DEBUG only
                        logging.debug(
                            "Jump test [legacy-debug]: %s return=%.6f rv=%.6f legacy=%s",
                            asset, log_return, estimate["blended_rv"], legacy_jump)

                # Seed EGARCH variance from RK if needed, then recursive update
                if self._egarch is not None:
                    if self._egarch._log_var.get(asset) is None and estimate and estimate.get("rv_5min", 0) > 0:
                        self._egarch.seed_variance(asset, estimate["rv_5min"] ** 2)
                    egarch_sigma = self._egarch.recursive_update(asset, log_return)
                    # Anomaly: sigma/rv ratio extreme (throttled to 1 per asset per 5 min)
                    if egarch_sigma and estimate and estimate.get("blended_rv", 0) > 0:
                        ratio = egarch_sigma / estimate["blended_rv"]
                        if ratio > 5.0 or ratio < 0.2:
                            throttle_key = f"egarch_ratio_{asset}"
                            if now - self._rk_last_summary.get(throttle_key, 0) >= 300:
                                self._rk_last_summary[throttle_key] = now
                                logging.warning(
                                    "EGARCH %s: sigma/rv ratio extreme (%.4f) — model may be diverging",
                                    asset, ratio)

                self._cache[asset] = self._compute(asset, now, seconds_to_close)
            else:
                self._cache.setdefault(asset, None)
        elif asset not in self._cache:
            self._cache[asset] = self._compute(asset, now, seconds_to_close)

        return self._cache.get(asset)

    def _record_jump_event(self, asset: str, timestamp: float):
        """Append jump timestamp, trim to JUMP_MAX_HISTORY."""
        events = self._jump_events.get(asset)
        if events is None:
            events = []
            self._jump_events[asset] = events
        events.append(timestamp)
        if len(events) > JUMP_MAX_HISTORY:
            del events[:-JUMP_MAX_HISTORY]

    # ── Adaptive jump detection methods ──────────────────────────────────

    def _adaptive_subsample_return(self, asset: str, log_return_5s: float, now: float) -> Optional[float]:
        """Subsample to 15s returns by summing every 3rd group of 5s returns."""
        self._adaptive_tick_counter[asset] = self._adaptive_tick_counter.get(asset, 0) + 1
        if self._adaptive_tick_counter[asset] % JUMP_ADAPTIVE_SUBSAMPLE != 0:
            return None
        # Sum the last SUBSAMPLE entries from the 5s return buffer
        returns = self._returns.get(asset)
        if returns is None or len(returns) < JUMP_ADAPTIVE_SUBSAMPLE:
            return None
        return sum(list(returns)[-JUMP_ADAPTIVE_SUBSAMPLE:])

    def _adaptive_jump_test(self, asset: str, return_15s: float, now: float) -> dict:
        """Core adaptive jump detection: EWMA variance + rolling percentile."""
        abs_r = abs(return_15s)

        # Compute thresholds from HISTORICAL data (before appending current return)
        n_hist = len(self._adaptive_abs_returns[asset])

        # Sigma threshold from pre-update EWMA
        pre_ewma_var = self._adaptive_ewma_var.get(asset)
        if pre_ewma_var is not None and pre_ewma_var > 0:
            ewma_sigma = math.sqrt(pre_ewma_var)
            sigma_threshold = JUMP_ADAPTIVE_SIGMA_MULT * ewma_sigma
        else:
            ewma_sigma = 0.0
            sigma_threshold = float('inf')

        # Rolling percentile from historical |returns| (before appending current)
        if n_hist >= JUMP_ADAPTIVE_PCTILE_MIN_OBS:
            sorted_abs = sorted(self._adaptive_abs_returns[asset])
            idx = min(int(JUMP_ADAPTIVE_PCTILE_LEVEL * len(sorted_abs)), len(sorted_abs) - 1)
            pctile_threshold = sorted_abs[idx]
        else:
            pctile_threshold = float('inf')

        # Now append current return to buffers
        self._adaptive_returns_15s[asset].append(return_15s)
        self._adaptive_abs_returns[asset].append(abs_r)
        n_obs = len(self._adaptive_returns_15s[asset])

        # EWMA variance update (after threshold computation)
        r_sq = return_15s * return_15s
        if pre_ewma_var is None:
            if n_obs >= 2:
                # Initialize from sample variance
                buf = list(self._adaptive_returns_15s[asset])
                mean_r = sum(buf) / len(buf)
                ewma_var = sum((x - mean_r) ** 2 for x in buf) / (len(buf) - 1)
                self._adaptive_ewma_var[asset] = ewma_var
        else:
            ewma_var = JUMP_ADAPTIVE_EWMA_LAMBDA * pre_ewma_var + (1 - JUMP_ADAPTIVE_EWMA_LAMBDA) * r_sq
            self._adaptive_ewma_var[asset] = ewma_var

        effective_threshold = max(sigma_threshold, pctile_threshold)

        # Jump detection (guarded by warmup)
        if n_obs >= JUMP_ADAPTIVE_EWMA_INIT_RETURNS and effective_threshold > 0 and effective_threshold != float('inf'):
            is_jump = abs_r > effective_threshold
        else:
            is_jump = False

        magnitude_ratio = abs_r / effective_threshold if effective_threshold > 0 and effective_threshold != float('inf') else 0.0

        return {
            "is_jump": is_jump,
            "ewma_sigma": ewma_sigma,
            "sigma_threshold": sigma_threshold,
            "pctile_threshold": pctile_threshold,
            "effective_threshold": effective_threshold,
            "magnitude_ratio": magnitude_ratio,
            "n_obs_15s": n_obs,
            "return_15s": return_15s,
        }

    def _record_adaptive_jump_event(self, asset: str, timestamp: float, magnitude_ratio: float):
        """Record an adaptive jump event with magnitude-scaled boost."""
        capped_ratio = min(JUMP_ADAPTIVE_MAG_CAP, magnitude_ratio)
        boost = JUMP_ADAPTIVE_DECAY_MAX_BOOST * capped_ratio / JUMP_ADAPTIVE_MAG_SCALE_BASE
        self._adaptive_jump_events[asset].append((timestamp, boost))
        if len(self._adaptive_jump_events[asset]) > JUMP_ADAPTIVE_MAX_HISTORY:
            del self._adaptive_jump_events[asset][:-JUMP_ADAPTIVE_MAX_HISTORY]
        self._adaptive_total_jumps[asset] = self._adaptive_total_jumps.get(asset, 0) + 1

    def _adaptive_decay_multiplier(self, asset: str, now: float) -> Tuple[float, str]:
        """Compute adaptive jump decay multiplier from event history."""
        events = self._adaptive_jump_events.get(asset, [])
        if not events:
            return (1.0, "normal")
        total_boost = sum(
            boost * math.exp(-(now - ts) / JUMP_ADAPTIVE_DECAY_TAU)
            for ts, boost in events if ts <= now
        )
        if total_boost > JUMP_ADAPTIVE_DECAY_MIN_BOOST:
            return (min(JUMP_ADAPTIVE_DECAY_CAP, 1.0 + total_boost), "elevated")
        return (1.0, "normal")

    def _save_adaptive_state(self):
        """Persist adaptive jump detection state to JSON (atomic write)."""
        state = {}
        for asset in ASSETS:
            state[asset] = {
                "ewma_var": self._adaptive_ewma_var.get(asset),
                "tick_counter": self._adaptive_tick_counter.get(asset, 0),
                "total_jumps": self._adaptive_total_jumps.get(asset, 0),
                "returns_15s": list(self._adaptive_returns_15s.get(asset, [])),
                "abs_returns": list(self._adaptive_abs_returns.get(asset, [])),
                "jump_events": list(self._adaptive_jump_events.get(asset, [])),
            }
        state["saved_at"] = time.time()
        tmp_path = JUMP_ADAPTIVE_STATE_PATH + ".tmp"
        try:
            with open(tmp_path, "w") as f:
                json.dump(state, f)
            os.replace(tmp_path, JUMP_ADAPTIVE_STATE_PATH)
            file_size = os.path.getsize(JUMP_ADAPTIVE_STATE_PATH) / 1024.0
            logging.info(
                "Adaptive jump state saved: BTC=%d ETH=%d SOL=%d XRP=%d obs (file_size=%.1fKB)",
                len(self._adaptive_returns_15s["BTC"]),
                len(self._adaptive_returns_15s["ETH"]),
                len(self._adaptive_returns_15s["SOL"]),
                len(self._adaptive_returns_15s["XRP"]),
                file_size)
        except Exception as e:
            logging.warning("Failed to save adaptive jump state: %s", e)

    def _load_adaptive_state(self):
        """Restore adaptive jump detection state from JSON on startup."""
        if not os.path.exists(JUMP_ADAPTIVE_STATE_PATH):
            logging.info("No adaptive jump state file found, starting fresh")
            return
        try:
            with open(JUMP_ADAPTIVE_STATE_PATH, "r") as f:
                state = json.load(f)
        except Exception as e:
            logging.warning("Failed to load adaptive jump state: %s (starting fresh)", e)
            return
        saved_at = state.get("saved_at", 0)
        for asset in ASSETS:
            try:
                adata = state.get(asset)
                if not isinstance(adata, dict):
                    continue
                ev = adata.get("ewma_var")
                if ev is not None and isinstance(ev, (int, float)):
                    self._adaptive_ewma_var[asset] = float(ev)
                tc = adata.get("tick_counter")
                if isinstance(tc, (int, float)):
                    self._adaptive_tick_counter[asset] = int(tc)
                tj = adata.get("total_jumps")
                if isinstance(tj, (int, float)):
                    self._adaptive_total_jumps[asset] = int(tj)
                r15 = adata.get("returns_15s")
                if isinstance(r15, list):
                    self._adaptive_returns_15s[asset] = deque(
                        [float(x) for x in r15 if isinstance(x, (int, float))],
                        maxlen=JUMP_ADAPTIVE_PCTILE_WINDOW)
                ar = adata.get("abs_returns")
                if isinstance(ar, list):
                    self._adaptive_abs_returns[asset] = deque(
                        [float(x) for x in ar if isinstance(x, (int, float))],
                        maxlen=JUMP_ADAPTIVE_PCTILE_WINDOW)
                je = adata.get("jump_events")
                if isinstance(je, list):
                    events = []
                    for item in je:
                        if isinstance(item, (list, tuple)) and len(item) == 2:
                            events.append((float(item[0]), float(item[1])))
                    self._adaptive_jump_events[asset] = events[-JUMP_ADAPTIVE_MAX_HISTORY:]
            except Exception as e:
                logging.warning("Adaptive jump state load failed for %s: %s (starting fresh)", asset, e)
        logging.info(
            "Adaptive jump state restored: BTC=%d ETH=%d SOL=%d XRP=%d obs, ewma_age=%.0fs",
            len(self._adaptive_returns_15s["BTC"]),
            len(self._adaptive_returns_15s["ETH"]),
            len(self._adaptive_returns_15s["SOL"]),
            len(self._adaptive_returns_15s["XRP"]),
            time.time() - saved_at if saved_at else 0)

    # ── Kernel & statistical methods ─────────────────────────────────────

    @staticmethod
    def _parzen_kernel(x: float) -> float:
        """Flat-top Parzen kernel for Realized Kernel estimator."""
        ax = abs(x)
        if ax <= 0.5:
            return 1.0
        if ax <= 1.0:
            # Smooth cubic taper: Hermite basis h00 maps [0.5, 1] → [1, 0]
            u = 2.0 * (ax - 0.5)  # maps [0.5, 1] → [0, 1]
            return 1.0 - 3.0 * u * u + 2.0 * u * u * u
        return 0.0

    @staticmethod
    def _estimate_noise_variance(returns: List[float]) -> float:
        """Estimate microstructure noise variance ω² from first-order autocovariance.

        ω² = max(FLOOR, -γ̂(1)) where γ̂(1) = (1/(n-1)) × Σ r_i × r_{i+1}
        Non-negative autocovariance (momentum) floors to RK_NOISE_VAR_FLOOR.
        """
        n = len(returns)
        if n < 2:
            return RK_NOISE_VAR_FLOOR
        gamma1 = sum(returns[i] * returns[i + 1] for i in range(n - 1)) / (n - 1)
        return max(RK_NOISE_VAR_FLOOR, -gamma1)

    @staticmethod
    def _realized_quarticity(returns: List[float], window: int) -> float:
        """Realized Quarticity: RQ = (n/3) × Σ r_i⁴ over the window subset."""
        subset = returns[-window:] if len(returns) >= window else returns
        n = len(subset)
        if n < 1:
            return 0.0
        return (n / 3.0) * sum(r ** 4 for r in subset)

    @staticmethod
    def _optimal_rk_bandwidth(returns: List[float], window: int, omega_sq: float) -> int:
        """BN (2008/2009) optimal bandwidth for flat-top Parzen kernel.

        H* = ceil(c* × ξ^(4/5) × n^(3/5)) where ξ² = ω² / √RQ.
        Falls back to ceil(√n) if insufficient data or degenerate inputs.
        """
        subset = returns[-window:] if len(returns) >= window else returns
        n = len(subset)
        if n < 2:
            return 0
        H_floor = math.ceil(math.sqrt(n))
        H_cap = math.floor(n * RK_BANDWIDTH_MAX_FRACTION)
        if n < RK_MIN_RETURNS_FOR_ADAPTIVE or omega_sq <= RK_NOISE_VAR_FLOOR:
            return H_floor
        rq = VolatilityEngine._realized_quarticity(returns, window)
        if rq <= 0.0:
            return H_floor
        sqrt_rq = math.sqrt(rq)
        xi_sq = omega_sq / sqrt_rq
        if xi_sq <= 0.0:
            return H_floor
        H_star = math.ceil(RK_CSTAR_FLAT_TOP_PARZEN * (xi_sq ** 0.8) * (n ** 0.6))
        return max(H_floor, min(H_star, H_cap))

    @staticmethod
    def _realized_kernel(returns: List[float], window: int, bandwidth: Optional[int] = None) -> float:
        """Realized Kernel (Barndorff-Nielsen 2008) — microstructure-noise robust.

        RK = Σ_{h=-H}^{H} k(h/(H+1)) × γ(h)
        where γ(h) is the autocovariance at lag h.
        Returns per-return scale volatility (same unit as old _window_rv).
        """
        subset = returns[-window:] if len(returns) >= window else returns
        n = len(subset)
        if n < 2:
            return 0.0

        H = bandwidth if bandwidth is not None else math.ceil(math.sqrt(n))

        rk = 0.0
        for h in range(-H, H + 1):
            weight = VolatilityEngine._parzen_kernel(h / (H + 1))
            if weight == 0.0:
                continue
            # Compute autocovariance γ(h)
            gamma_h = 0.0
            ah = abs(h)
            count = 0
            for j in range(ah, n):
                gamma_h += subset[j] * subset[j - ah]
                count += 1
            if count > 0:
                gamma_h /= count
            rk += weight * gamma_h

        return math.sqrt(max(0.0, rk))

    @staticmethod
    def _bipower_variation(returns: List[float], window: int) -> float:
        """Bipower variation — robust to jumps, estimates continuous-path vol.

        BV = (π/2) × (1/(n-1)) × Σ |r_j| × |r_{j+1}|
        Returns per-return scale volatility.
        """
        subset = returns[-window:] if len(returns) >= window else returns
        n = len(subset)
        if n < 2:
            return 0.0

        bv_sum = 0.0
        for j in range(n - 1):
            bv_sum += abs(subset[j]) * abs(subset[j + 1])

        bv = (math.pi / 2.0) * bv_sum / (n - 1)
        return math.sqrt(max(0.0, bv))

    def _estimate_beta(self, asset: str, reference: str = "BTC") -> float:
        """Cross-asset beta: cov(r_asset, r_ref) / var(r_ref). Clamped [0.5, 3.0]."""
        if asset == reference:
            return 1.0

        r_asset = list(self._returns.get(asset, []))
        r_ref = list(self._returns.get(reference, []))

        # Use last BETA_LOOKBACK_RETURNS from each
        r_asset = r_asset[-BETA_LOOKBACK_RETURNS:]
        r_ref = r_ref[-BETA_LOOKBACK_RETURNS:]

        n = min(len(r_asset), len(r_ref))
        if n < 10:
            return 1.0

        # Align to same length (most recent)
        r_asset = r_asset[-n:]
        r_ref = r_ref[-n:]

        mean_a = sum(r_asset) / n
        mean_r = sum(r_ref) / n

        cov = sum((r_asset[i] - mean_a) * (r_ref[i] - mean_r) for i in range(n)) / n
        var_r = sum((r_ref[i] - mean_r) ** 2 for i in range(n)) / n

        if var_r <= 0:
            return 1.0

        beta = cov / var_r
        return max(0.5, min(3.0, beta))

    def _get_implied_vol(self, asset: str) -> Optional[float]:
        """Get implied vol in per-5-second scale. BTC/ETH direct, SOL/XRP via beta."""
        if self._dvol is None:
            return None

        if asset in DERIBIT_DVOL_CURRENCIES:
            return self._dvol.get_dvol(asset)

        # SOL/XRP: scale BTC DVOL by cross-asset beta
        btc_dvol = self._dvol.get_dvol("BTC")
        if btc_dvol is None:
            return None
        beta = self._estimate_beta(asset, "BTC")
        return btc_dvol * beta

    def _get_implied_vol_hourly(self, asset: str) -> Optional[float]:
        """Get hourly-averaged implied vol in per-5-second scale. BTC/ETH direct, SOL/XRP via beta."""
        if self._dvol is None:
            return None

        if asset in DERIBIT_DVOL_CURRENCIES:
            return self._dvol.get_dvol_hourly_avg(asset)

        # SOL/XRP: scale BTC hourly avg DVOL by cross-asset beta
        btc_dvol_hourly = self._dvol.get_dvol_hourly_avg("BTC")
        if btc_dvol_hourly is None:
            return None
        beta = self._estimate_beta(asset, "BTC")
        return btc_dvol_hourly * beta

    # ── Core computation ─────────────────────────────────────────────────

    def _compute(self, asset: str, now: float, seconds_to_close: Optional[float] = None) -> Optional[Dict]:
        returns = self._returns[asset]
        if len(returns) < 2:
            return None

        returns_list = list(returns)

        # Noise variance estimation (once for all windows)
        omega_sq = self._estimate_noise_variance(returns_list)

        # Step 1: Fixed-bandwidth RK (always — production path)
        rk_1min = self._realized_kernel(returns_list, VOL_WINDOW_1MIN)
        rk_5min = self._realized_kernel(returns_list, VOL_WINDOW_5MIN)
        rk_15min = self._realized_kernel(returns_list, VOL_WINDOW_15MIN)

        # Adaptive bandwidth computation
        H_fixed_5 = math.ceil(math.sqrt(min(len(returns_list), VOL_WINDOW_5MIN))) if len(returns_list) >= 2 else 0
        H_fixed_15 = math.ceil(math.sqrt(min(len(returns_list), VOL_WINDOW_15MIN))) if len(returns_list) >= 2 else 0
        H_adaptive_5 = self._optimal_rk_bandwidth(returns_list, VOL_WINDOW_5MIN, omega_sq)
        H_adaptive_15 = self._optimal_rk_bandwidth(returns_list, VOL_WINDOW_15MIN, omega_sq)

        # Adaptive RK — compute with optimal bandwidth H*
        rk_fixed_5 = rk_5min   # Save fixed-H values for diagnostics
        rk_fixed_15 = rk_15min
        ark_5min = self._realized_kernel(returns_list, VOL_WINDOW_5MIN, bandwidth=H_adaptive_5) if H_adaptive_5 != H_fixed_5 else rk_5min
        ark_15min = self._realized_kernel(returns_list, VOL_WINDOW_15MIN, bandwidth=H_adaptive_15) if H_adaptive_15 != H_fixed_15 else rk_15min

        # When shadow mode is off, use adaptive values for all downstream
        if not RK_ADAPTIVE_SHADOW_MODE:
            rk_5min = ark_5min
            rk_15min = ark_15min

        # ── Adaptive RK logging ───────────────────────────────────────────
        # Track noise history for diagnostics
        self._rk_noise_history[asset].append(omega_sq)

        # Per-tick DEBUG: when adaptive H differs from fixed
        if H_adaptive_5 != H_fixed_5 or H_adaptive_15 != H_fixed_15:
            delta_5 = round((ark_5min - rk_fixed_5) / rk_fixed_5, 4) if rk_fixed_5 > 0 and H_adaptive_5 != H_fixed_5 else 0.0
            delta_15 = round((ark_15min - rk_fixed_15) / rk_fixed_15, 4) if rk_fixed_15 > 0 and H_adaptive_15 != H_fixed_15 else 0.0
            logging.debug(
                "RK adaptive %s: H_5=%d→%d H_15=%d→%d ω²=%.2e delta_5=%.4f delta_15=%.4f",
                asset, H_fixed_5, H_adaptive_5, H_fixed_15, H_adaptive_15,
                omega_sq, delta_5, delta_15
            )
            self._rk_adaptive_diff_count[asset] = self._rk_adaptive_diff_count.get(asset, 0) + 1
            if H_adaptive_5 != H_fixed_5 and rk_fixed_5 > 0:
                self._rk_delta_5_accum[asset].append(abs((ark_5min - rk_fixed_5) / rk_fixed_5))
            if H_adaptive_15 != H_fixed_15 and rk_fixed_15 > 0:
                self._rk_delta_15_accum[asset].append(abs((ark_15min - rk_fixed_15) / rk_fixed_15))

        # Noise regime change (INFO): ω² changes by >10× from previous tick
        prev_omega = self._rk_prev_omega_sq.get(asset)
        if prev_omega is not None and prev_omega > 0 and omega_sq > 0:
            omega_ratio = omega_sq / prev_omega
            if omega_ratio > 10.0 or omega_ratio < 0.1:
                logging.info(
                    "RK noise shift %s: ω²=%.2e (prev=%.2e, ratio=%.1f×)",
                    asset, omega_sq, prev_omega, omega_ratio
                )
        self._rk_prev_omega_sq[asset] = omega_sq

        # Bandwidth divergence alert (WARNING): adaptive > 2× fixed
        if H_adaptive_5 > 2 * H_fixed_5 and H_fixed_5 > 0:
            logging.warning(
                "RK bandwidth divergence %s: H_adaptive=%d vs H_fixed=%d (%.1f×) ω²=%.2e — high noise session",
                asset, H_adaptive_5, H_fixed_5, H_adaptive_5 / H_fixed_5, omega_sq
            )
        if H_adaptive_15 > 2 * H_fixed_15 and H_fixed_15 > 0:
            logging.warning(
                "RK bandwidth divergence %s: H_adaptive=%d vs H_fixed=%d (%.1f×) ω²=%.2e — high noise session",
                asset, H_adaptive_15, H_fixed_15, H_adaptive_15 / H_fixed_15, omega_sq
            )

        # Periodic summary (INFO, every 5 min)
        if now - self._rk_last_summary.get(asset, 0) >= 300:
            H_fixed_1 = math.ceil(math.sqrt(min(len(returns_list), VOL_WINDOW_1MIN))) if len(returns_list) >= 2 else 0
            H_adaptive_1 = H_fixed_1  # 1min always same (n < RK_MIN_RETURNS_FOR_ADAPTIVE)
            logging.info(
                "RK bandwidth %s: H_fixed=[%d,%d,%d] H_adaptive=[%d,%d,%d] ω²=%.2e ark_5=%.8f rk_5=%.8f ratio=%.4f",
                asset, H_fixed_1, H_fixed_5, H_fixed_15,
                H_adaptive_1, H_adaptive_5, H_adaptive_15,
                omega_sq, ark_5min, rk_5min,
                ark_5min / rk_5min if rk_5min > 0 else 0.0
            )
            self._rk_last_summary[asset] = now

        # Step 2: Bipower variation for jump separation
        bv_1min = self._bipower_variation(returns_list, VOL_WINDOW_1MIN)
        bv_5min = self._bipower_variation(returns_list, VOL_WINDOW_5MIN)
        bv_15min = self._bipower_variation(returns_list, VOL_WINDOW_15MIN)

        # DVOL squared for VRP diagnostic
        dvol_hourly = self._get_implied_vol_hourly(asset)
        dvol_sq = (dvol_hourly ** 2) if dvol_hourly is not None else None

        # Step 3: RK blend (TV weights when promoted, fixed weights when shadow)
        if not RK_TV_SHADOW_MODE and seconds_to_close is not None:
            w1, w5, w15 = compute_tv_rk_weights(seconds_to_close)
        else:
            w1, w5, w15 = VOL_BLEND_WEIGHTS
        continuous_rv = w1 * rk_1min + w5 * rk_5min + w15 * rk_15min
        bv_blended = w1 * bv_1min + w5 * bv_5min + w15 * bv_15min
        jump_var = max(0.0, continuous_rv ** 2 - bv_blended ** 2)
        fixed_blend_rv = math.sqrt(bv_blended ** 2 + jump_var)
        rv_blended = fixed_blend_rv

        # Step 3a: Counterfactual RK weights (opposite of live path)
        shadow_tv_blend_rv = None
        shadow_tv_weights = None
        if RK_TV_SHADOW_MODE and seconds_to_close is not None:
            # Shadow: TV weights not live, show what they would do
            tw1, tw5, tw15 = compute_tv_rk_weights(seconds_to_close)
            shadow_tv_weights = (round(tw1, 3), round(tw5, 3), round(tw15, 3))
            tv_continuous = tw1 * rk_1min + tw5 * rk_5min + tw15 * rk_15min
            tv_bv = tw1 * bv_1min + tw5 * bv_5min + tw15 * bv_15min
            tv_jump_var = max(0.0, tv_continuous ** 2 - tv_bv ** 2)
            shadow_tv_blend_rv = math.sqrt(tv_bv ** 2 + tv_jump_var)
        elif not RK_TV_SHADOW_MODE:
            # Promoted: TV weights ARE live, compute what fixed weights would do
            fw1, fw5, fw15 = VOL_BLEND_WEIGHTS
            shadow_tv_weights = (round(fw1, 3), round(fw5, 3), round(fw15, 3))
            fc = fw1 * rk_1min + fw5 * rk_5min + fw15 * rk_15min
            fb = fw1 * bv_1min + fw5 * bv_5min + fw15 * bv_15min
            fj = max(0.0, fc ** 2 - fb ** 2)
            shadow_tv_blend_rv = math.sqrt(fb ** 2 + fj)

        # Step 3b: EGARCH conditional volatility
        egarch_sigma = None
        if self._egarch is not None:
            egarch_sigma = self._egarch.get_sigma(asset)

        # Track RV-only blended for diagnostics
        rv_only_blended = rv_blended
        blended = rv_blended

        # Step 3c: EGARCH-RV variance-space blend
        egarch_blend_weight = 0.0
        egarch_blend_var = None
        egarch_blend_sigma = rv_blended  # safe default if blend not computed
        try:
            if (egarch_sigma is not None and egarch_sigma > 0
                    and rv_blended > 0 and self._mz is not None):
                ratio = egarch_sigma / rv_blended
                # Safety clamp: reject extreme divergence
                if 1.0 / EGARCH_RV_RATIO_CLAMP <= ratio <= EGARCH_RV_RATIO_CLAMP:
                    # Record MZ pair: EGARCH forecast var vs RV realized var
                    egarch_var = egarch_sigma ** 2
                    rv_var = rv_blended ** 2
                    self._mz.record(asset, egarch_var, rv_var)

                    # Get adaptive weight from MZ R²
                    w_eg = self._mz.maybe_recompute(asset, now)
                    egarch_blend_weight = w_eg

                    if w_eg > 0:
                        # Variance-space blend: avoids Jensen's inequality bias
                        egarch_blend_var = w_eg * egarch_var + (1.0 - w_eg) * rv_var
                        egarch_blend_sigma = math.sqrt(egarch_blend_var)

                        if not math.isfinite(egarch_blend_sigma) or egarch_blend_sigma <= 0:
                            egarch_blend_var = None
                            egarch_blend_sigma = rv_blended
                        elif not EGARCH_BLEND_SHADOW_MODE:
                            blended = egarch_blend_sigma

                    # Periodic logging + state save
                    if now - self._egarch_blend_last_log.get(asset, 0) >= EGARCH_BLEND_LOG_INTERVAL:
                        mz_r2 = self._mz._r_squared.get(asset)
                        mz_qlike = self._mz._qlike.get(asset)
                        logging.info(
                            "EGARCH blend %s: w_eg=%.3f ratio=%.3f R²=%s QLIKE=%s "
                            "rv=%.8f eg=%.8f blended=%.8f shadow=%s",
                            asset, w_eg, ratio,
                            f"{mz_r2:.4f}" if mz_r2 is not None else "warmup",
                            f"{mz_qlike:.6f}" if mz_qlike is not None else "n/a",
                            rv_blended, egarch_sigma,
                            egarch_blend_sigma if egarch_blend_var else rv_blended,
                            EGARCH_BLEND_SHADOW_MODE,
                        )
                        self._egarch_blend_last_log[asset] = now
                        self._mz.save_state()
                else:
                    if now - self._egarch_blend_last_log.get(asset, 0) >= EGARCH_BLEND_LOG_INTERVAL:
                        logging.warning(
                            "EGARCH blend %s: CLAMPED ratio=%.3f (limit=%.1f) — skipping blend",
                            asset, ratio, EGARCH_RV_RATIO_CLAMP,
                        )
                        self._egarch_blend_last_log[asset] = now
        except Exception:
            logging.warning("EGARCH blend %s failed, using rv_blended", asset, exc_info=True)

        # Sigmoid QLIKE counterfactual: what blended_rv would be using sigmoid weight
        mz_sigmoid_improvement = None
        mz_sigmoid_blend_rv = None
        egarch_ratio_clamped = False
        if egarch_sigma is not None and egarch_sigma > 0 and rv_blended > 0:
            ratio_check = egarch_sigma / rv_blended
            egarch_ratio_clamped = not (1.0 / EGARCH_RV_RATIO_CLAMP <= ratio_check <= EGARCH_RV_RATIO_CLAMP)
        if self._mz is not None and egarch_sigma is not None and egarch_sigma > 0 and rv_blended > 0:
            sig_w = self._mz._shadow_sigmoid_w.get(asset)
            if sig_w is not None and sig_w > 0:
                sig_blend_var = sig_w * (egarch_sigma ** 2) + (1.0 - sig_w) * (rv_blended ** 2)
                mz_sigmoid_blend_rv = math.sqrt(sig_blend_var)
            bl_q = self._mz._baseline_qlike.get(asset)
            eg_q = self._mz._qlike.get(asset)
            if bl_q is not None and eg_q is not None and bl_q > 1e-10:
                mz_sigmoid_improvement = round(max(0.0, (bl_q - eg_q) / bl_q), 6)

        # VRP diagnostic (variance risk premium)
        vrp = None
        if dvol_sq is not None and rk_5min > 0:
            vrp = dvol_sq - rk_5min ** 2

        # VRP regime logging (every 5 min)
        if vrp is not None and now - self._rk_last_summary.get(f"vrp_{asset}", 0) >= 300:
            premium = "positive" if vrp > 0 else "negative"
            rv5_sq = rk_5min ** 2
            ratio = dvol_sq / rv5_sq if rv5_sq > 0 else 0.0
            logging.info(
                "VRP %s: vrp=%.2e dvol_sq=%.2e rv5_sq=%.2e ratio=%.2f (premium=%s)",
                asset, vrp, dvol_sq, rv5_sq, ratio, premium,
            )
            self._rk_last_summary[f"vrp_{asset}"] = now

        # DVOL hourly average health logging (every 5 min)
        if self._dvol is not None and now - self._rk_last_summary.get(f"dvol_h_{asset}", 0) >= 300:
            dvol_hourly = self._get_implied_vol_hourly(asset)
            dvol_raw = self._get_implied_vol(asset)
            if dvol_hourly is not None and asset in DERIBIT_DVOL_CURRENCIES:
                buf = self._dvol._hourly_dvol.get(asset)
                n_samples = len(buf) if buf else 0
                if n_samples > 0 and dvol_hourly > 0:
                    spread = (max(buf) - min(buf)) / dvol_hourly
                    logging.info(
                        "DVOL hourly %s: avg=%.8f raw=%.8f samples=%d spread=%.4f",
                        asset, dvol_hourly, dvol_raw if dvol_raw else 0.0, n_samples, spread,
                    )
            self._rk_last_summary[f"dvol_h_{asset}"] = now

        # Step 4: DVOL blending (if available)
        iv = self._get_implied_vol(asset)
        dvol_5s = iv  # for diagnostics
        iv_rv_spread = None
        iv_rv_blend_method = "rv_only"

        if iv is not None and iv > 0 and rv_blended > 0:
            # Inverse-variance weighting
            var_rv = (rk_1min - rk_15min) ** 2   # spread as proxy for RV uncertainty
            var_iv = (iv * 0.10) ** 2             # 10% uncertainty on IV
            # Avoid division by zero
            if var_rv + var_iv > 0:
                w_rv = var_iv / (var_rv + var_iv)
                w_iv = var_rv / (var_rv + var_iv)
                blended = w_rv * rv_blended + w_iv * iv
                iv_rv_blend_method = "inverse_variance"

            # Step 5: IV-RV regime detection
            iv_rv_spread = (iv - rv_blended) / rv_blended
            if iv_rv_spread > IV_RV_SPREAD_THRESHOLD:
                blended = 0.3 * rv_blended + 0.7 * iv
                iv_rv_blend_method = "stress_override"

        # Step 6: Jump regime — exponential decay (legacy)
        regime = "normal"
        jump_multiplier = 1.0
        jump_events = self._jump_events.get(asset, [])
        if jump_events:
            total_boost = sum(
                JUMP_DECAY_MAX_BOOST * math.exp(-(now - ts) / JUMP_DECAY_TAU)
                for ts in jump_events if ts <= now
            )
            if total_boost > JUMP_DECAY_MIN_BOOST:
                regime = "elevated"
                jump_multiplier = min(4.0, 1.0 + total_boost)
                blended *= jump_multiplier

        # Step 6b: Adaptive jump decay
        adaptive_multiplier, adaptive_regime = self._adaptive_decay_multiplier(asset, now)
        if not JUMP_ADAPTIVE_SHADOW_MODE:
            # Adaptive drives regime — undo legacy and apply adaptive
            if jump_multiplier > 1.0:
                blended /= jump_multiplier
            regime = adaptive_regime
            jump_multiplier = adaptive_multiplier
            if adaptive_multiplier > 1.0:
                blended *= adaptive_multiplier

        # Backward-compat: compute approximate seconds remaining
        jump_seconds_remaining = 0.0
        if jump_events:
            age = now - max(jump_events)
            full_decay = -JUMP_DECAY_TAU * math.log(JUMP_DECAY_MIN_BOOST / JUMP_DECAY_MAX_BOOST)
            jump_seconds_remaining = max(0.0, full_decay - age)

        # Adaptive seconds remaining
        adaptive_seconds_remaining = 0.0
        adaptive_events = self._adaptive_jump_events.get(asset, [])
        if adaptive_events:
            newest_ts = max(ts for ts, _ in adaptive_events)
            # Time until the largest single boost decays below threshold
            max_boost = max(b for _, b in adaptive_events)
            if max_boost > JUMP_ADAPTIVE_DECAY_MIN_BOOST:
                full_decay_adaptive = -JUMP_ADAPTIVE_DECAY_TAU * math.log(
                    JUMP_ADAPTIVE_DECAY_MIN_BOOST / max_boost)
                adaptive_seconds_remaining = max(0.0, full_decay_adaptive - (now - newest_ts))

        return {
            # Original 7 fields (backward-compatible)
            "rv_1min": rk_1min,
            "rv_5min": rk_5min,
            "rv_15min": rk_15min,
            "blended_rv": blended,
            "regime": regime,
            "num_returns": len(returns),
            "jump_seconds_remaining": round(jump_seconds_remaining, 1),
            # New diagnostic fields
            "bv_1min": bv_1min,
            "bv_5min": bv_5min,
            "bv_15min": bv_15min,
            "jump_component": math.sqrt(jump_var) if jump_var > 0 else 0.0,
            "dvol_5s": dvol_5s,
            "iv_rv_spread": iv_rv_spread,
            "iv_rv_blend_method": iv_rv_blend_method,
            "rv_only_blended": rv_only_blended,
            "fixed_blend_rv": fixed_blend_rv,
            "jump_multiplier": round(jump_multiplier, 4),
            "jump_event_count": len(jump_events),
            # Adaptive jump diagnostics
            "adaptive_jump_multiplier": round(adaptive_multiplier, 4),
            "adaptive_jump_regime": adaptive_regime,
            "adaptive_jump_event_count": len(self._adaptive_jump_events.get(asset, [])),
            "adaptive_seconds_remaining": round(adaptive_seconds_remaining, 1),
            "adaptive_ewma_sigma": math.sqrt(self._adaptive_ewma_var[asset]) if self._adaptive_ewma_var.get(asset) else None,
            "adaptive_n_obs_15s": len(self._adaptive_returns_15s.get(asset, [])),
            # EGARCH diagnostics
            "egarch_sigma": egarch_sigma,
            "egarch_n_updates": self._egarch._n_updates.get(asset, 0) if self._egarch else 0,
            "egarch_log_var": self._egarch._log_var.get(asset) if self._egarch else None,
            # EGARCH blend diagnostics
            "egarch_blend_weight": egarch_blend_weight,
            "egarch_blend_var": egarch_blend_var,
            "egarch_blend_shadow": EGARCH_BLEND_SHADOW_MODE,
            "mz_r_squared": self._mz._r_squared.get(asset) if self._mz else None,
            "mz_qlike": self._mz._qlike.get(asset) if self._mz else None,
            "mz_baseline_qlike": self._mz._baseline_qlike.get(asset) if self._mz else None,
            "mz_shadow_sigmoid_w": self._mz._shadow_sigmoid_w.get(asset) if self._mz else None,
            # Adaptive RK bandwidth diagnostics
            "omega_sq": omega_sq,
            "rk_H_fixed_5": H_fixed_5,
            "rk_H_fixed_15": H_fixed_15,
            "rk_H_adaptive_5": H_adaptive_5,
            "rk_H_adaptive_15": H_adaptive_15,
            "ark_5min": ark_5min,
            "ark_15min": ark_15min,
            "rk_adaptive_delta_5": round((ark_5min - rk_fixed_5) / rk_fixed_5, 6) if rk_fixed_5 > 0 and H_adaptive_5 != H_fixed_5 else 0.0,
            "rk_adaptive_delta_15": round((ark_15min - rk_fixed_15) / rk_fixed_15, 6) if rk_fixed_15 > 0 and H_adaptive_15 != H_fixed_15 else 0.0,
            # DVOL diagnostics
            "dvol_sq_hourly": dvol_sq,
            "vrp": vrp,
            # Shadow time-varying RK weights
            "shadow_tv_blend_rv": shadow_tv_blend_rv,
            "shadow_tv_weights": shadow_tv_weights,
            # Sigmoid QLIKE counterfactual
            "mz_sigmoid_improvement": mz_sigmoid_improvement,
            "mz_sigmoid_blend_rv": mz_sigmoid_blend_rv,
            "egarch_ratio_clamped": egarch_ratio_clamped,
        }


# EGARCHEstimator, MincerZarnowitzTracker, PositionSizer, _student_t_e_abs_z,
# _compute_qlike, fee helpers, compute_tv_rk_weights → imported from models.py
# ═════════════════════════════════════════════════════════════════════════════
#  ProbabilityEngine
# ═════════════════════════════════════════════════════════════════════════════

class ProbabilityEngine:
    """Compute win probability from spot price, strike, time, and volatility.

    Supports per-asset distribution selection via dist_config.json:
    - Student-t CDF with configurable df per asset (default df=4)
    - NIG (Normal Inverse Gaussian) CDF with fitted parameters
    Falls back to Student-t(df=4) if no config file is present.
    """

    @staticmethod
    def _cdf_complement(z_score: float, asset: Optional[str] = None) -> float:
        """Compute 1 - CDF(z_score) using per-asset distribution config."""
        cfg = DIST_CONFIG.get(asset) if asset else None
        if cfg is None:
            return 1.0 - student_t.cdf(z_score, df=STUDENT_T_DF)

        if cfg.get("distribution") == "nig" and "nig_a" in cfg:
            val = 1.0 - norminvgauss.cdf(
                z_score, cfg["nig_a"], cfg["nig_b"],
                loc=cfg.get("nig_loc", 0.0),
                scale=cfg.get("nig_scale", 1.0),
            )
            return max(0.0, min(1.0, val))  # clamp float rounding
        return 1.0 - student_t.cdf(z_score, df=cfg.get("student_t_df", STUDENT_T_DF))

    @staticmethod
    def compute(spot: float, threshold: float, seconds_remaining: float,
                blended_rv: float,
                market_price_cents: Optional[int] = None,
                asset: Optional[str] = None,
                product_type: Optional[str] = None) -> Dict:
        """
        Compute calibrated win probability for a "price stays above threshold" bet.

        Args:
            spot: current price (e.g. 68500.0 for BTC)
            threshold: strike/threshold price the market resolves against
            seconds_remaining: seconds until market close
            blended_rv: blended realized vol (per-5-second log return scale)
            market_price_cents: current Kalshi YES price in cents (for sanity check)

        Returns dict with: z_score, raw_prob, calibrated_prob, tradeable, reason
        """
        result: Dict = {
            "z_score": None,
            "raw_prob": None,
            "calibrated_prob": None,
            "calibration_method": None,
            "tradeable": False,
            "reason": "",
        }

        # ── Guard: need valid inputs ─────────────────────────────────────
        if spot <= 0 or seconds_remaining <= 0 or blended_rv <= 0:
            result["reason"] = "invalid inputs (spot/time/vol <= 0)"
            return result

        # ── Annualize vol and compute z-score ────────────────────────────
        # blended_rv is std dev of 5-second log returns.
        # σ_annual = blended_rv × sqrt(seconds_per_year / 5)
        # σ_annual × sqrt(t_years) = blended_rv × sqrt(t_seconds / 5)
        # Denominator for z: spot × blended_rv × sqrt(t_seconds / 5)
        sigma_move = spot * blended_rv * math.sqrt(seconds_remaining / 5.0)

        if sigma_move <= 0:
            result["reason"] = "sigma_move is zero"
            return result

        z_score = (threshold - spot) / sigma_move
        result["z_score"] = round(z_score, 4)

        # ── Raw probability via configurable distribution CDF ────────────
        # P(price stays above threshold) = P(move > threshold - spot)
        # = P(Z > z_score) = 1 - CDF(z_score)
        raw_prob = ProbabilityEngine._cdf_complement(z_score, asset)
        result["raw_prob"] = round(raw_prob, 6)

        # ── Calibration: adaptive (if trained) or fixed β=0.85 ──────────
        dynamic_cap = ProbabilityEngine._dynamic_cap(seconds_remaining, product_type=product_type)
        _cal_cfg2 = get_market_config(product_type)
        _reg_engine = _resolve_cal_engine(product_type, asset, require_enabled=True)
        if _reg_engine is not None and _reg_engine.is_learned_method_active():
            calibrated_prob = _reg_engine.calibrate(raw_prob, cap=dynamic_cap)
            result["calibration_method"] = f"{product_type}_{_reg_engine.active_method}"
            # Shadow: what passthrough + temperature would have produced
            _pt_shadow = min(raw_prob, dynamic_cap)
            _temp_cfg = _cal_cfg2.temperature_t if _cal_cfg2.temperature_enabled else None
            if _temp_cfg and _temp_cfg != 1.0:
                _sp = max(0.001, min(0.999, _pt_shadow))
                _sz = math.log(_sp / (1.0 - _sp))
                _pt_shadow = 1.0 / (1.0 + math.exp(-_sz / _temp_cfg))
            result["shadow_cal_prob"] = round(_pt_shadow, 6)
            result["shadow_cal_temperature"] = _temp_cfg
        elif _cal_cfg2.cal_eligible and _CALIBRATION_ENGINE is not None:
            calibrated_prob = _CALIBRATION_ENGINE.calibrate(raw_prob, cap=dynamic_cap)
            result["calibration_method"] = _CALIBRATION_ENGINE.active_method
        elif not _cal_cfg2.cal_eligible:
            calibrated_prob = min(raw_prob, dynamic_cap)
            result["calibration_method"] = "passthrough"
        else:
            calibrated_prob = ProbabilityEngine._calibrate(raw_prob, cap=dynamic_cap)
            result["calibration_method"] = "fixed_beta"
        result["calibrated_prob"] = round(calibrated_prob, 6)

        # ── Sanity: model vs market discrepancy ──────────────────────────
        if market_price_cents is not None:
            if calibrated_prob > DISCREPANCY_PROB and market_price_cents < DISCREPANCY_PRICE:
                result["reason"] = (
                    f"model says {calibrated_prob:.1%} but market is "
                    f"{market_price_cents}¢ (< {DISCREPANCY_PRICE}¢) — refusing"
                )
                logging.warning(f"ProbabilityEngine: {result['reason']}")
                return result

        # ── All checks passed ────────────────────────────────────────────
        result["tradeable"] = True
        result["reason"] = "ok"
        return result

    @staticmethod
    def counterfactual_prob(spot: float, threshold: float, seconds_remaining: float,
                            alt_blended_rv: float, asset: Optional[str] = None,
                            product_type: Optional[str] = None) -> Optional[float]:
        """Compute calibrated_prob for a counterfactual blended_rv. Lightweight — no logging."""
        if spot <= 0 or seconds_remaining <= 0 or alt_blended_rv <= 0:
            return None
        sigma_move = spot * alt_blended_rv * math.sqrt(seconds_remaining / 5.0)
        if sigma_move <= 0:
            return None
        z = (threshold - spot) / sigma_move
        raw = ProbabilityEngine._cdf_complement(z, asset)
        cap = ProbabilityEngine._dynamic_cap(seconds_remaining, product_type=product_type)
        if _CALIBRATION_ENGINE is not None:
            return round(_CALIBRATION_ENGINE.calibrate(raw, cap=cap), 6)
        return round(ProbabilityEngine._calibrate(raw, cap=cap), 6)

    @staticmethod
    def _dynamic_cap(seconds_remaining: float, product_type: str = None) -> float:
        """Return probability cap based on time to close."""
        schedule = (HOURLY_DYNAMIC_CAP_SCHEDULE
                    if product_type in ("hourly", "spx_hourly", "weather")
                    else DYNAMIC_CAP_SCHEDULE)
        for threshold_secs, cap in schedule:
            if seconds_remaining > threshold_secs:
                return cap
        return schedule[-1][1]  # smallest TTC bracket

    @staticmethod
    def _calibrate(raw_prob: float, cap: float = MAX_EFFECTIVE_PROB) -> float:
        """Apply logistic compression then hard cap.

        Maps raw_prob through: logit → scale by BETA_SLOPE → inverse logit → cap.
        This pulls extreme probabilities toward 0.5 and caps at 93%.
        """
        # Clamp to avoid log(0) in logit
        p = max(0.001, min(0.999, raw_prob))
        logit = math.log(p / (1.0 - p))
        scaled_logit = BETA_SLOPE * logit
        compressed = 1.0 / (1.0 + math.exp(-scaled_logit))
        return min(compressed, cap)


# ═════════════════════════════════════════════════════════════════════════════
#  CalibrationEngine
# ═════════════════════════════════════════════════════════════════════════════

class CalibrationEngine:
    """Data-driven calibration replacing fixed β=0.85 Platt scaling.

    Implements three calibration methods:
    - Platt Scaling: 2-parameter logistic (A, B) — default, needs 200+ samples
    - Beta Calibration: 3-parameter (a, b, c) — needs 500+ samples
    - Online BLR: Bayesian linear regression with Laplace approx — needs 50+ samples

    Until enough data is collected, falls back to the existing fixed β=0.85.
    """

    def __init__(self, state_path: str = CALIBRATION_STATE_PATH,
                 label: str = "CalibrationEngine"):
        self.state_path = state_path
        self._label = label
        self.active_method: str = "fixed_beta"  # current method in use
        self._observations: deque = deque(maxlen=500)  # (raw_prob, binary_outcome)
        self._brier_scores: deque = deque(maxlen=CALIBRATION_BRIER_WINDOW)
        self._last_retrain: float = 0.0

        # Platt parameters: P_cal = 1 / (1 + exp(A * logit(p) + B))
        self._platt_A: float = BETA_SLOPE  # default = current fixed β
        self._platt_B: float = 0.0
        self._platt_trained: bool = False

        # Beta Cal parameters: logit(P_cal) = c + a * log(p) + b * log(1-p)
        self._beta_a: float = 1.0
        self._beta_b: float = -1.0
        self._beta_c: float = 0.0
        self._beta_trained: bool = False

        # Online BLR parameters: sigmoid(w * logit(p) + b)
        # Prior centered on identity: w=1, b=0
        self._blr_mu = [1.0, 0.0]  # [w, b] posterior mean
        self._blr_precision = [[1.0, 0.0], [0.0, 1.0]]  # 2x2 precision matrix (prior)
        self._blr_trained: bool = False

        # Previous Brier score for regression check
        self._prev_brier: Optional[float] = None

        # Temperature scaling (shadow pipeline)
        self._temperature: Optional[float] = None
        self._temperature_brier: Optional[float] = None

        # Empirical bucket tracking: keyed by prob range string
        self._empirical_buckets: Dict[str, deque] = {
            "0.80-0.85": deque(maxlen=200),
            "0.85-0.90": deque(maxlen=200),
            "0.90-0.93": deque(maxlen=200),
            "0.93-0.95": deque(maxlen=200),
            "0.95-0.97": deque(maxlen=200),
            "0.97-1.00": deque(maxlen=200),
        }

        self._load_state()

    # ── Persistence ────────────────────────────────────────────────────────

    def _load_state(self):
        """Load learned parameters from calibration_state.json."""
        try:
            with open(self.state_path, "r") as f:
                state = json.load(f)

            self.active_method = state.get("active_method", "fixed_beta")

            if "platt" in state:
                self._platt_A = state["platt"]["A"]
                self._platt_B = state["platt"]["B"]
                self._platt_trained = state["platt"].get("trained", False)

            if "beta_cal" in state:
                self._beta_a = state["beta_cal"]["a"]
                self._beta_b = state["beta_cal"]["b"]
                self._beta_c = state["beta_cal"]["c"]
                self._beta_trained = state["beta_cal"].get("trained", False)

            if "blr" in state:
                self._blr_mu = state["blr"]["mu"]
                self._blr_precision = state["blr"]["precision"]
                self._blr_trained = state["blr"].get("trained", False)

            if "temperature" in state:
                self._temperature = state["temperature"].get("value")
                self._temperature_brier = state["temperature"].get("brier")

            # observations are loaded from DB in load_training_data_from_db()

            if "prev_brier" in state:
                self._prev_brier = state["prev_brier"]

            logging.info(
                "%s loaded: method=%s, observations=%d, "
                "platt_trained=%s, beta_trained=%s, blr_trained=%s",
                self._label, self.active_method, len(self._observations),
                self._platt_trained, self._beta_trained, self._blr_trained,
            )
        except FileNotFoundError:
            logging.info("%s: No state found, starting fresh (fixed_beta fallback)", self._label)
        except Exception as e:
            logging.warning("%s: Error loading state: %s", self._label, e)

    def _save_state(self):
        """Atomically persist learned parameters."""
        state = {
            "active_method": self.active_method,
            "platt": {
                "A": self._platt_A,
                "B": self._platt_B,
                "trained": self._platt_trained,
            },
            "beta_cal": {
                "a": self._beta_a,
                "b": self._beta_b,
                "c": self._beta_c,
                "trained": self._beta_trained,
            },
            "blr": {
                "mu": self._blr_mu,
                "precision": self._blr_precision,
                "trained": self._blr_trained,
            },
            "temperature": {
                "value": self._temperature,
                "brier": self._temperature_brier,
            },
            "prev_brier": self._prev_brier,
            "saved_at": datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "n_observations": len(self._observations),
        }
        tmp_path = self.state_path + ".tmp"
        try:
            with open(tmp_path, "w") as f:
                json.dump(state, f, indent=2)
            os.replace(tmp_path, self.state_path)
        except Exception as e:
            logging.warning("%s: failed to save state: %s", self._label, e)

    # ── Inference ──────────────────────────────────────────────────────────

    def calibrate(self, raw_prob: float, cap: float) -> float:
        """Calibrate raw_prob using the active method. Sub-ms, called per evaluation."""
        if self.active_method == "platt" and self._platt_trained:
            result = self._platt_predict(raw_prob)
        elif self.active_method == "beta_cal" and self._beta_trained:
            result = self._beta_cal_predict(raw_prob)
        elif self.active_method == "temperature" and self._temperature is not None:
            result = self._temperature_predict(raw_prob, self._temperature)
        elif self.active_method == "blr" and self._blr_trained:
            result = self._blr_predict(raw_prob)
        else:
            return CalibrationEngine._fallback_calibrate(raw_prob, cap)
        # Learned method active: apply cap, uncertainty shrinkage, safety ceiling
        result = min(result, cap)
        result = self._apply_uncertainty_shrinkage(result)
        return max(0.001, min(NUMERICAL_SAFETY_CEILING, result))

    @staticmethod
    def _fallback_calibrate(raw_prob: float, cap: float) -> float:
        """Identical to ProbabilityEngine._calibrate — fixed β=0.85."""
        p = max(0.001, min(0.999, raw_prob))
        logit_p = math.log(p / (1.0 - p))
        scaled = BETA_SLOPE * logit_p
        compressed = 1.0 / (1.0 + math.exp(-scaled))
        return min(compressed, cap)

    # ── Temperature Scaling (Shadow Pipeline) ─────────────────────────────

    def _fit_temperature(self) -> Optional[float]:
        """Fit temperature parameter T minimizing Brier score. T>0, monotonic."""
        if len(self._observations) < CALIBRATION_MIN_SAMPLES_BLR:
            return None
        obs = list(self._observations)
        best_t, best_brier = 1.0, float('inf')
        # Grid search + refinement (fast, <100 evaluations)
        for t_candidate in [x / 100.0 for x in range(50, 200, 5)]:  # 0.50 to 1.95
            brier_sum = 0.0
            for raw_p, outcome in obs:
                p = max(0.001, min(0.999, raw_p))
                logit_p = math.log(p / (1.0 - p))
                pred = 1.0 / (1.0 + math.exp(-logit_p / t_candidate))
                brier_sum += (pred - outcome) ** 2
            avg_brier = brier_sum / len(obs)
            if avg_brier < best_brier:
                best_brier = avg_brier
                best_t = t_candidate
        # Refine around best
        for t_candidate in [best_t + d / 1000.0 for d in range(-50, 51, 5)]:
            if t_candidate <= 0.01:
                continue
            brier_sum = 0.0
            for raw_p, outcome in obs:
                p = max(0.001, min(0.999, raw_p))
                logit_p = math.log(p / (1.0 - p))
                pred = 1.0 / (1.0 + math.exp(-logit_p / t_candidate))
                brier_sum += (pred - outcome) ** 2
            avg_brier = brier_sum / len(obs)
            if avg_brier < best_brier:
                best_brier = avg_brier
                best_t = t_candidate
        return best_t

    def _temperature_predict(self, raw_prob: float, temperature: float) -> float:
        """Apply temperature scaling: sigmoid(logit(p) / T)."""
        p = max(0.001, min(0.999, raw_prob))
        logit_p = math.log(p / (1.0 - p))
        return 1.0 / (1.0 + math.exp(-logit_p / temperature))

    def shadow_calibration_pipeline(self, raw_prob: float, best_ask: int,
                                     seconds_remaining: float,
                                     ofa_adjustment: float = 0.0) -> Optional[Dict]:
        """Compute alternative calibration pipeline in shadow mode.

        Changes vs production:
        1. Temperature scaling instead of Beta Cal
        2. No market-price blending (SHADOW_BLEND_W=0.0)
        3. Excludes cap-era data (handled by _fit_temperature using filtered obs)
        """
        if not SHADOW_CAL_PIPELINE or raw_prob is None:
            return None
        try:
            # Step 1: Temperature scaling (or raw if not fitted)
            if self._temperature is not None:
                cal_prob = self._temperature_predict(raw_prob, self._temperature)
            else:
                cal_prob = raw_prob

            # Apply uncertainty shrinkage (same as production)
            cal_prob = self._apply_uncertainty_shrinkage(cal_prob)
            cal_prob = max(0.001, min(NUMERICAL_SAFETY_CEILING, cal_prob))

            # Step 2: OFA adjustment
            cal_prob = max(0.01, min(NUMERICAL_SAFETY_CEILING, cal_prob + ofa_adjustment))

            # Step 3: No market blend (or reduced blend)
            shadow_final = cal_prob  # SHADOW_BLEND_W = 0.0, no blend

            # Compute edge
            edge = shadow_final - best_ask / 100.0
            est_fee_1c = calculate_taker_fee(1, best_ask)
            fee_edge = edge - est_fee_1c / 100.0

            return {
                "prob": round(shadow_final, 6),
                "cal_prob_pre_blend": round(cal_prob, 6),
                "temperature": self._temperature,
                "temperature_brier": self._temperature_brier,
                "edge": round(edge, 6),
                "fee_edge": round(fee_edge, 6),
                "would_trade": fee_edge >= MIN_EDGE_PCT / 100.0,
                "blend_w": SHADOW_BLEND_W,
                "prod_blend_w": MARKET_BLEND_W,
            }
        except Exception:
            logging.warning("shadow_calibration_pipeline failed", exc_info=True)
            return None

    # ── Training Data Management ───────────────────────────────────────────

    def add_observation(self, raw_prob: float, outcome: int):
        """Append a (raw_prob, binary_outcome) pair and update rolling Brier."""
        self._observations.append((raw_prob, outcome))
        # Update rolling Brier with the *current* calibration prediction
        pred = self.calibrate(raw_prob, cap=1.0)
        brier = (pred - outcome) ** 2
        self._brier_scores.append(brier)
        # Bucket the calibrated prediction for empirical tracking
        self._bucket_observation(pred, outcome)

    def maybe_retrain(self) -> bool:
        """Hourly retrain check. Trains all eligible methods, promotes best Brier."""
        now = time.time()
        if now - self._last_retrain < CALIBRATION_RETRAIN_INTERVAL:
            return False
        self._last_retrain = now

        n = len(self._observations)
        if n < CALIBRATION_MIN_SAMPLES_BLR:
            return False

        # ── Train all eligible methods ────────────────────────────────────
        trained_methods: Dict[str, float] = {}  # method -> Brier

        if n >= CALIBRATION_MIN_SAMPLES_PLATT:
            try:
                self._train_platt()
                self._platt_trained = True
                trained_methods["platt"] = self._compute_brier_for_method("platt")
                logging.info(
                    "%s: Platt trained — A=%.4f, B=%.4f, Brier=%.4f, n=%d",
                    self._label, self._platt_A, self._platt_B, trained_methods["platt"], n,
                )
            except Exception as e:
                logging.warning("%s: Platt training failed: %s", self._label, e)

        if n >= CALIBRATION_MIN_SAMPLES_BETA:
            try:
                self._train_beta_cal()
                self._beta_trained = True
                trained_methods["beta_cal"] = self._compute_brier_for_method("beta_cal")
                logging.info(
                    "%s: Beta Cal trained — a=%.4f, b=%.4f, c=%.4f, "
                    "Brier=%.4f, n=%d",
                    self._label, self._beta_a, self._beta_b, self._beta_c,
                    trained_methods["beta_cal"], n,
                )
            except Exception as e:
                logging.warning("%s: Beta Cal training failed: %s", self._label, e)

        if n >= CALIBRATION_MIN_SAMPLES_BLR:
            try:
                self._train_blr()
                self._blr_trained = True
                trained_methods["blr"] = self._compute_brier_for_method("blr")
                logging.info(
                    "%s: BLR trained — mu=[%.4f, %.4f], Brier=%.4f, n=%d",
                    self._label, self._blr_mu[0], self._blr_mu[1], trained_methods["blr"], n,
                )
            except Exception as e:
                logging.warning("%s: BLR training failed: %s", self._label, e)

        # ── Fit temperature scaling and include in competition ────────────
        try:
            temp = self._fit_temperature()
            if temp is not None:
                self._temperature = temp
                brier_sum = sum((self._temperature_predict(rp, temp) - out) ** 2
                                for rp, out in self._observations)
                self._temperature_brier = brier_sum / len(self._observations)
                trained_methods["temperature"] = self._temperature_brier
                logging.info(
                    "%s: Temperature scaling fitted — T=%.4f, Brier=%.4f, n=%d",
                    self._label, temp, self._temperature_brier, n,
                )
        except Exception as e:
            logging.warning("%s: Temperature scaling failed: %s", self._label, e)

        if not trained_methods:
            return False

        # ── Promote best Brier method ─────────────────────────────────────
        best_method = min(trained_methods, key=trained_methods.get)
        best_brier = trained_methods[best_method]

        # Regression guard: reject if best is worse than previous + margin
        if self._prev_brier is not None and best_brier > self._prev_brier + 0.01:
            logging.warning(
                "%s: promotion REJECTED — best Brier %.4f > prev %.4f + 0.01 "
                "(methods: %s)",
                self._label, best_brier, self._prev_brier, trained_methods,
            )
            return False

        old_method = self.active_method
        self.active_method = best_method
        self._prev_brier = best_brier

        logging.info(
            "%s: PROMOTED %s -> %s (Brier=%.4f, alternatives=%s)",
            self._label, old_method, best_method, best_brier,
            {k: round(v, 4) for k, v in trained_methods.items()},
        )

        self._save_state()
        return True

    def load_training_data_from_db(self, state: "StateManager",
                                   product_type_include: Optional[str] = None,
                                   asset_filter=None):
        """Rebuild training data from evaluated opportunities on startup.

        Args:
            product_type_include: If set, load ONLY this product type (for
                dedicated hourly/spx engines). Default None = existing behavior
                (exclude non-15M types).
            asset_filter: If set, additionally filter by asset column.
                str → single asset, list → IN clause. Default None = no asset filter.

        Note: rejected_opportunities (z-score rejections) are excluded because
        their bimodal raw_prob distribution (clustered at 0 and 1) contaminates
        Platt training — 2 NO outcomes at raw_prob≈1.0 create extreme log-loss
        pressure that drives Platt A well below 1.0, compressing all high-end
        calibrated probabilities (e.g. raw 0.95 → cal 0.89 instead of ~0.96).
        """
        try:
            self._observations.clear()

            cutoff = (datetime.datetime.now(timezone.utc)
                      - datetime.timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%S")
            if product_type_include:
                if asset_filter is not None:
                    if isinstance(asset_filter, list):
                        _placeholders = ",".join("?" for _ in asset_filter)
                        _cal_filter = f"AND product_type = ? AND asset IN ({_placeholders}) "
                        _cal_query_params = (product_type_include, *asset_filter, cutoff)
                    else:
                        _cal_filter = "AND product_type = ? AND asset = ? "
                        _cal_query_params = (product_type_include, asset_filter, cutoff)
                else:
                    # Existing behavior: filter by product_type only
                    _cal_filter = "AND product_type = ? "
                    _cal_query_params = (product_type_include, cutoff)
            else:
                # Existing behavior: exclude non-15M types
                _cal_excluded = get_cal_excluded_types()
                if _cal_excluded:
                    _excl_sorted = sorted(_cal_excluded)
                    _placeholders = ",".join("?" for _ in _excl_sorted)
                    _cal_filter = f"AND (product_type IS NULL OR product_type NOT IN ({_placeholders})) "
                    _cal_query_params = (*_excl_sorted, cutoff)
                else:
                    _cal_filter = ""
                    _cal_query_params = (cutoff,)
            rows = state.conn.execute(
                "SELECT raw_prob, market_result FROM evaluated_opportunities "
                "WHERE status='settled' AND raw_prob IS NOT NULL "
                "AND market_result IS NOT NULL "
                + _cal_filter +
                "AND evaluation_time > ? "
                "ORDER BY evaluation_time DESC LIMIT 500",
                _cal_query_params
            ).fetchall()

            loaded = 0
            for row in rows:
                raw_p = row["raw_prob"]
                result = row["market_result"]
                if result in ("yes", "all_yes"):
                    binary = 1
                elif result in ("no", "all_no"):
                    binary = 0
                else:
                    continue
                self._observations.append((raw_p, binary))
                loaded += 1

            logging.info(
                "%s: loaded %d observations from DB (total: %d)",
                self._label, loaded, len(self._observations),
            )

            # Attempt initial training if enough data
            if loaded > 0:
                self._last_retrain = 0.0  # force retrain check
                self.maybe_retrain()

        except Exception as e:
            logging.warning("%s: failed to load from DB: %s", self._label, e)

    # ── Platt Scaling ──────────────────────────────────────────────────────

    def _platt_predict(self, raw_prob: float) -> float:
        """P_cal = 1 / (1 + exp(A * logit(p) + B))"""
        p = max(0.001, min(0.999, raw_prob))
        logit_p = math.log(p / (1.0 - p))
        return 1.0 / (1.0 + math.exp(-(self._platt_A * logit_p + self._platt_B)))

    def _train_platt(self):
        """Newton-Raphson optimization of (A, B) minimizing log-loss.

        Objective: minimize -sum[ y*log(q) + (1-y)*log(1-q) ]
        where q = sigmoid(A * logit(p) + B)
        """
        if not self._observations:
            return

        A, B = self._platt_A, self._platt_B

        # Precompute logits
        logits = []
        targets = []
        for raw_p, outcome in self._observations:
            p = max(0.001, min(0.999, raw_p))
            logits.append(math.log(p / (1.0 - p)))
            targets.append(float(outcome))

        n = len(logits)

        for iteration in range(50):
            # Compute gradient and Hessian
            g_A = 0.0
            g_B = 0.0
            h_AA = 0.0
            h_AB = 0.0
            h_BB = 0.0

            for i in range(n):
                z = A * logits[i] + B
                # Numerically stable sigmoid
                if z >= 0:
                    q = 1.0 / (1.0 + math.exp(-z))
                else:
                    ez = math.exp(z)
                    q = ez / (1.0 + ez)
                q = max(1e-10, min(1 - 1e-10, q))

                err = q - targets[i]
                g_A += err * logits[i]
                g_B += err
                w = q * (1.0 - q)
                h_AA += w * logits[i] * logits[i]
                h_AB += w * logits[i]
                h_BB += w

            # Solve 2x2 system: H @ delta = -g
            det = h_AA * h_BB - h_AB * h_AB
            if abs(det) < 1e-12:
                break

            dA = -(h_BB * g_A - h_AB * g_B) / det
            dB = -(h_AA * g_B - h_AB * g_A) / det

            A += dA
            B += dB

            if abs(dA) < 1e-8 and abs(dB) < 1e-8:
                break

        # Guardrail: reject if parameters are extreme
        if abs(A) > 5.0 or abs(B) > 5.0:
            logging.warning(
                "%s: Platt params extreme (A=%.4f, B=%.4f), rejecting",
                self._label,
                A, B,
            )
            return

        self._platt_A = A
        self._platt_B = B

    # ── Beta Calibration ───────────────────────────────────────────────────

    def _beta_cal_predict(self, raw_prob: float) -> float:
        """logit(P_cal) = c + a * log(p) + b * log(1-p)"""
        p = max(0.001, min(0.999, raw_prob))
        logit_out = self._beta_c + self._beta_a * math.log(p) + self._beta_b * math.log(1.0 - p)
        # Clamp to avoid overflow
        logit_out = max(-20.0, min(20.0, logit_out))
        return 1.0 / (1.0 + math.exp(-logit_out))

    def _train_beta_cal(self):
        """Newton-Raphson optimization of (a, b, c) minimizing log-loss.

        Model: q = sigmoid(a * log(p) + b * log(1-p) + c)
        """
        if not self._observations:
            return

        a, b, c = self._beta_a, self._beta_b, self._beta_c

        # Precompute features
        log_p = []
        log_1mp = []
        targets = []
        for raw_p, outcome in self._observations:
            p = max(0.001, min(0.999, raw_p))
            log_p.append(math.log(p))
            log_1mp.append(math.log(1.0 - p))
            targets.append(float(outcome))

        n = len(log_p)

        for iteration in range(100):
            # Gradient and 3x3 Hessian
            g = [0.0, 0.0, 0.0]  # d/d(a, b, c)
            H = [[0.0]*3 for _ in range(3)]

            for i in range(n):
                z = a * log_p[i] + b * log_1mp[i] + c
                if z >= 0:
                    q = 1.0 / (1.0 + math.exp(-z))
                else:
                    ez = math.exp(z)
                    q = ez / (1.0 + ez)
                q = max(1e-10, min(1 - 1e-10, q))

                err = q - targets[i]
                feats = [log_p[i], log_1mp[i], 1.0]

                for j in range(3):
                    g[j] += err * feats[j]
                    for k in range(3):
                        H[j][k] += q * (1.0 - q) * feats[j] * feats[k]

            # Solve 3x3 via Cramer's rule
            delta = CalibrationEngine._solve_3x3(H, [-g[0], -g[1], -g[2]])
            if delta is None:
                break

            a += delta[0]
            b += delta[1]
            c += delta[2]

            if all(abs(d) < 1e-8 for d in delta):
                break

        self._beta_a = a
        self._beta_b = b
        self._beta_c = c

    @staticmethod
    def _solve_3x3(A_mat, b_vec):
        """Solve 3x3 linear system using Cramer's rule. Returns None if singular."""
        def det3(m):
            return (m[0][0] * (m[1][1]*m[2][2] - m[1][2]*m[2][1])
                    - m[0][1] * (m[1][0]*m[2][2] - m[1][2]*m[2][0])
                    + m[0][2] * (m[1][0]*m[2][1] - m[1][1]*m[2][0]))

        d = det3(A_mat)
        if abs(d) < 1e-15:
            return None

        result = []
        for col in range(3):
            mod = [list(row) for row in A_mat]
            for row in range(3):
                mod[row][col] = b_vec[row]
            result.append(det3(mod) / d)
        return result

    # ── Online BLR ─────────────────────────────────────────────────────────

    def _blr_predict(self, raw_prob: float) -> float:
        """Posterior mean prediction: sigmoid(w * logit(p) + b)"""
        p = max(0.001, min(0.999, raw_prob))
        logit_p = math.log(p / (1.0 - p))
        z = self._blr_mu[0] * logit_p + self._blr_mu[1]
        z = max(-20.0, min(20.0, z))
        return 1.0 / (1.0 + math.exp(-z))

    def _train_blr(self):
        """Laplace approximation for Bayesian logistic regression.

        Prior: N([1, 0], I) — centered on identity calibration.
        Posterior: Laplace approximation at MAP estimate.
        """
        if not self._observations:
            return

        mu = list(self._blr_mu)

        # Precompute logits
        logits = []
        targets = []
        for raw_p, outcome in self._observations:
            p = max(0.001, min(0.999, raw_p))
            logits.append(math.log(p / (1.0 - p)))
            targets.append(float(outcome))

        n = len(logits)
        # Prior precision (identity)
        prior_mu = [1.0, 0.0]
        lam = 1.0  # prior precision scalar

        for iteration in range(50):
            g = [lam * (mu[0] - prior_mu[0]), lam * (mu[1] - prior_mu[1])]
            H = [[lam, 0.0], [0.0, lam]]

            for i in range(n):
                z = mu[0] * logits[i] + mu[1]
                if z >= 0:
                    q = 1.0 / (1.0 + math.exp(-z))
                else:
                    ez = math.exp(z)
                    q = ez / (1.0 + ez)
                q = max(1e-10, min(1 - 1e-10, q))

                err = q - targets[i]
                feats = [logits[i], 1.0]

                for j in range(2):
                    g[j] += err * feats[j]
                    for k in range(2):
                        H[j][k] += q * (1.0 - q) * feats[j] * feats[k]

            # Solve 2x2
            det = H[0][0] * H[1][1] - H[0][1] * H[1][0]
            if abs(det) < 1e-12:
                break

            d0 = -(H[1][1] * g[0] - H[0][1] * g[1]) / det
            d1 = -(H[0][0] * g[1] - H[1][0] * g[0]) / det

            mu[0] += d0
            mu[1] += d1

            if abs(d0) < 1e-8 and abs(d1) < 1e-8:
                break

        self._blr_mu = mu
        # Store posterior precision (Hessian at MAP)
        self._blr_precision = H

    # ── Metrics ────────────────────────────────────────────────────────────

    def rolling_brier_score(self) -> float:
        """Rolling Brier score over the last N outcomes."""
        if not self._brier_scores:
            return 1.0
        return sum(self._brier_scores) / len(self._brier_scores)

    def _compute_brier_on_observations(self) -> float:
        """Compute Brier score over all observations using current model."""
        if not self._observations:
            return 1.0
        total = 0.0
        for raw_p, outcome in self._observations:
            pred = self.calibrate(raw_p, cap=1.0)
            total += (pred - outcome) ** 2
        return total / len(self._observations)

    def is_learned_method_active(self) -> bool:
        """Return True if a data-driven calibration method is active (not fixed_beta fallback)."""
        if self.active_method == "platt" and self._platt_trained:
            return True
        if self.active_method == "beta_cal" and self._beta_trained:
            return True
        if self.active_method == "temperature" and self._temperature is not None:
            return True
        if self.active_method == "blr" and self._blr_trained:
            return True
        return False

    def _apply_uncertainty_shrinkage(self, cal_prob: float) -> float:
        """Shrink calibrated probability toward 0.5 based on model uncertainty.

        p_adj = 0.5 + (p_cal - 0.5) * (1 - u)
        where u = brier / sqrt(n). Well-calibrated model with plenty of data
        → nearly no shrinkage. High Brier or scarce data → conservative.
        """
        n = len(self._observations)
        if n < 50:
            u = 0.05  # conservative default when data is scarce
        else:
            u = self.rolling_brier_score() / math.sqrt(n)
        u = max(0.0, min(0.5, u))
        return 0.5 + (cal_prob - 0.5) * (1.0 - u)

    def _compute_brier_for_method(self, method: str) -> float:
        """Compute Brier score over all observations for a specific method."""
        if not self._observations:
            return 1.0
        total = 0.0
        for raw_p, outcome in self._observations:
            if method == "platt":
                pred = self._platt_predict(raw_p)
            elif method == "beta_cal":
                pred = self._beta_cal_predict(raw_p)
            elif method == "temperature" and self._temperature is not None:
                pred = self._temperature_predict(raw_p, self._temperature)
            elif method == "blr":
                pred = self._blr_predict(raw_p)
            else:
                pred = CalibrationEngine._fallback_calibrate(raw_p, cap=1.0)
            total += (max(0.001, min(0.999, pred)) - outcome) ** 2
        return total / len(self._observations)

    def _bucket_observation(self, pred: float, outcome: int):
        """Place a (pred, outcome) pair into the appropriate empirical bucket."""
        bucket_edges = [
            (0.80, 0.85, "0.80-0.85"),
            (0.85, 0.90, "0.85-0.90"),
            (0.90, 0.93, "0.90-0.93"),
            (0.93, 0.95, "0.93-0.95"),
            (0.95, 0.97, "0.95-0.97"),
            (0.97, 1.00, "0.97-1.00"),
        ]
        for lo, hi, key in bucket_edges:
            if lo <= pred < hi or (key == "0.97-1.00" and pred >= 0.97):
                self._empirical_buckets[key].append((pred, outcome))
                break

    def get_empirical_bucket_stats(self) -> Dict[str, dict]:
        """Return per-bucket stats: count, win_rate, avg_pred, calibration_gap."""
        stats = {}
        for key, bucket in self._empirical_buckets.items():
            if not bucket:
                stats[key] = {"count": 0, "win_rate": None, "avg_pred": None, "calibration_gap": None}
                continue
            preds = [p for p, _ in bucket]
            outcomes = [o for _, o in bucket]
            win_rate = sum(outcomes) / len(outcomes)
            avg_pred = sum(preds) / len(preds)
            stats[key] = {
                "count": len(bucket),
                "win_rate": round(win_rate, 4),
                "avg_pred": round(avg_pred, 4),
                "calibration_gap": round(win_rate - avg_pred, 4),
            }
        return stats

    def get_diagnostics(self) -> dict:
        """Return diagnostic info for logging."""
        diag = {
            "active_method": self.active_method,
            "n_observations": len(self._observations),
            "rolling_brier": round(self.rolling_brier_score(), 6),
            "platt_A": round(self._platt_A, 6),
            "platt_B": round(self._platt_B, 6),
            "platt_trained": self._platt_trained,
            "beta_a": round(self._beta_a, 6),
            "beta_b": round(self._beta_b, 6),
            "beta_c": round(self._beta_c, 6),
            "beta_trained": self._beta_trained,
            "blr_mu": [round(m, 6) for m in self._blr_mu],
            "blr_trained": self._blr_trained,
            "learned_method_active": self.is_learned_method_active(),
        }
        diag["empirical_buckets"] = self.get_empirical_bucket_stats()
        return diag

    def backtest_adaptive_vs_fixed(self) -> dict:
        """Replay all observations through old (fixed cap) vs new (learned + shrinkage) system.

        Called once on startup for diagnostics. Returns comparison dict.
        """
        if not self._observations or not self.is_learned_method_active():
            return {}

        old_brier_sum = 0.0
        new_brier_sum = 0.0
        cap_truncated = 0
        high_prob_markets = 0  # predictions > 0.93 under new system

        for raw_p, outcome in self._observations:
            # Old system: fixed beta fallback with 0.93 cap
            old_pred = CalibrationEngine._fallback_calibrate(raw_p, cap=MAX_EFFECTIVE_PROB)
            old_brier_sum += (old_pred - outcome) ** 2

            # New system: learned method + uncertainty shrinkage
            new_pred = self.calibrate(raw_p, cap=NUMERICAL_SAFETY_CEILING)
            new_brier_sum += (new_pred - outcome) ** 2

            # How many predictions were truncated by old cap?
            uncapped = CalibrationEngine._fallback_calibrate(raw_p, cap=1.0)
            if uncapped > MAX_EFFECTIVE_PROB:
                cap_truncated += 1

            if new_pred > MAX_EFFECTIVE_PROB:
                high_prob_markets += 1

        n = len(self._observations)
        result = {
            "n_observations": n,
            "old_brier": round(old_brier_sum / n, 6),
            "new_brier": round(new_brier_sum / n, 6),
            "brier_improvement": round((old_brier_sum - new_brier_sum) / n, 6),
            "cap_truncated_count": cap_truncated,
            "high_prob_new_count": high_prob_markets,
            "active_method": self.active_method,
        }

        logging.info(
            "%s BACKTEST: old_brier=%.4f, new_brier=%.4f, "
            "improvement=%.4f, cap_truncated=%d/%d, high_prob_new=%d",
            self._label, result["old_brier"], result["new_brier"], result["brier_improvement"],
            cap_truncated, n, high_prob_markets,
        )
        return result


# ═════════════════════════════════════════════════════════════════════════════
#  OpportunityScanner
# ═════════════════════════════════════════════════════════════════════════════

class OpportunityScanner:
    """Evaluate all markets across active windows and return the best trade candidate.

    Filters by: time-to-close, one-asset-per-window, probability pre-filter,
    orderbook price range, edge threshold, and position sizing.
    """

    def __init__(self, client: KalshiClient, state: StateManager,
                 feed: CoinbaseFeed, vol: VolatilityEngine, logger: Logger,
                 sizer: PositionSizer, order_flow: Optional[OrderFlowEngine] = None,
                 kalshi_oft: Optional[KalshiOrderFlowTracker] = None,
                 kalshi_feed=None, main_loop=None):
        self._client = client
        self._state = state
        self._feed = feed
        self._vol = vol
        self._logger = logger
        self._sizer = sizer
        self._order_flow = order_flow
        self._kalshi_oft = kalshi_oft
        self._kalshi_feed = kalshi_feed
        self._ml = main_loop
        # Orderbook cache: ticker -> (data, fetch_time)
        self._ob_cache: Dict[str, Tuple[Optional[Dict], float]] = {}
        # Balance cache: (balance_cents, fetch_time)
        self._balance_cache: Tuple[Optional[int], float] = (None, 0.0)
        # Scan stats from last scan() call
        self._last_scan_stats: Optional[Dict] = None
        # Session-level counters for dashboard
        self._session_strategy_counts: Dict[str, int] = {
            STRATEGY_WAIT: 0, STRATEGY_MAKER_PATIENT: 0,
            STRATEGY_MAKER_AGGRESSIVE: 0, STRATEGY_TAKER_NOW: 0,
            STRATEGY_PANIC_CAPTURE: 0,
        }
        self._session_asset_perf: Dict[str, Dict[str, int]] = {
            a: {"opportunities_found": 0, "times_selected": 0, "times_rejected": 0}
            for a in ASSETS
        }
        self._session_total_scanned: int = 0
        self._session_total_candidates: int = 0
        self._last_opportunity_ts: Optional[str] = None
        self._recent_opportunities: deque = deque(maxlen=20)
        self._ticker_ask_history: Dict[str, deque] = {}
        self._eval_opp_seen: set = set()  # 2-tuples (ticker, stage) or 3-tuples (ticker, stage, side)
        self._shadow_cal_last_log: Dict[str, float] = {}
        # Hourly per-window tracking (reset each scan tick)
        self._hourly_window_counts: Dict[str, int] = {}
        self._hourly_window_risk: Dict[str, float] = {}

        # Overnight LP shadow: per-asset vol history for circuit breaker
        # Stores (timestamp, blended_rv) tuples during overnight hours
        # ~8640 obs/night at 5s ticks × 7 days ≈ 60K max
        self._overnight_rv_history: Dict[str, deque] = {
            a: deque(maxlen=70000) for a in ASSETS
        }
        self._overnight_lp_vol_skip_count: int = 0  # session counter for dashboard

        # ── Startup assertion: _shadow_diag keys must be accepted by DB insert fns ──
        # Prevents the bug class where a new key in _shadow_diag causes a crash
        # at every **_shadow_diag splat into insert_rejection/insert_evaluated_opportunity.
        _SHADOW_DIAG_KEYS = {
            "egarch_sigma", "egarch_blend_sigma", "egarch_blend_weight",
            "mz_r_squared", "shadow_tv_blend_rv", "mz_shadow_sigmoid_w",
            "mz_baseline_qlike", "mz_qlike",
        }
        for _fn_name, _fn in [
            ("insert_rejection", self._state.insert_rejection),
            ("insert_evaluated_opportunity", self._state.insert_evaluated_opportunity),
        ]:
            _accepted = set(inspect.signature(_fn).parameters.keys())
            _unknown = _SHADOW_DIAG_KEYS - _accepted
            assert not _unknown, (
                f"_shadow_diag keys {_unknown} not accepted by {_fn_name}(). "
                f"Add them to the function signature + SQL or remove from _shadow_diag."
            )

        # ── Startup assertion: DB busy_timeout must be set ──
        # Prevents the bug class where a new sqlite3.connect() call forgets
        # PRAGMA busy_timeout, causing "database is locked" under contention.
        # (Learned: sports_engine.py missing busy_timeout → ~2000 errors/8hr, Mar 2 2026)
        _bt = self._state.conn.execute("PRAGMA busy_timeout").fetchone()[0]
        assert _bt >= 5000, (
            f"StateManager busy_timeout={_bt}ms is too low (need ≥5000). "
            f"Add: conn.execute('PRAGMA busy_timeout=30000')"
        )

        # ── Startup assertion: critical config values ──
        assert MARKET_BLEND_W == 0.40, f"MARKET_BLEND_W misconfigured: {MARKET_BLEND_W}"
        assert SHADOW_CAL_PIPELINE is True, "SHADOW_CAL_PIPELINE should be True"
        assert MAX_RISK_PER_TRADE == 0.25, f"MAX_RISK_PER_TRADE misconfigured: {MAX_RISK_PER_TRADE}"
        assert XRP_MAX_RISK_PER_TRADE <= MAX_RISK_PER_TRADE, (
            f"XRP risk {XRP_MAX_RISK_PER_TRADE} > 15M risk {MAX_RISK_PER_TRADE}")
        assert XRP_MAX_RISK_PER_TRADE >= 0.05, f"XRP_MAX_RISK_PER_TRADE too low: {XRP_MAX_RISK_PER_TRADE}"
        logging.info(
            "CONFIG_VERIFY: MARKET_BLEND_W=%.2f SHADOW_CAL_PIPELINE=%s "
            "MAX_RISK=%s XRP_MAX_RISK=%s XRP_15M_SHADOW=%s SIZING_TIERS=%s DRAWDOWN_HALF=%.2f DRAWDOWN_QUARTER=%.2f "
            "DRAWDOWN_HALT=%.2f MAKER_ONLY_THRESHOLD=%.0f",
            MARKET_BLEND_W, SHADOW_CAL_PIPELINE, MAX_RISK_PER_TRADE, XRP_MAX_RISK_PER_TRADE,
            XRP_15M_SHADOW, SIZING_TIERS, DRAWDOWN_HALF_THRESHOLD, DRAWDOWN_QUARTER_THRESHOLD,
            DRAWDOWN_HALT_THRESHOLD, MAKER_ONLY_THRESHOLD)

        # ── Hourly config verify ──
        if HOURLY_OBSERVATION_ENABLED:
            assert HOURLY_MARKET_BLEND_W >= 0.30, (
                f"HOURLY_MARKET_BLEND_W={HOURLY_MARKET_BLEND_W} too low")
            assert HOURLY_MIN_ENTRY_PRICE >= 50, (
                f"HOURLY_MIN_ENTRY_PRICE={HOURLY_MIN_ENTRY_PRICE} too low")
            assert HOURLY_MIN_ENTRY_PRICE <= MIN_ENTRY_PRICE, (
                f"HOURLY floor {HOURLY_MIN_ENTRY_PRICE} > 15M floor {MIN_ENTRY_PRICE}")
            assert HOURLY_MAX_RISK_PER_TRADE <= MAX_RISK_PER_TRADE, (
                f"HOURLY risk {HOURLY_MAX_RISK_PER_TRADE} > 15M risk {MAX_RISK_PER_TRADE}")
            assert HOURLY_TEMPERATURE_T > 0, "HOURLY_TEMPERATURE_T must be positive"
            assert 0 < HOURLY_KELLY_FRACTION <= 1.0, "HOURLY_KELLY_FRACTION must be in (0, 1]"
            assert HOURLY_MIN_STC_ENTRY < HOURLY_MAX_STC_ENTRY, "STC entry window invalid"
            assert HOURLY_MAX_POSITIONS_PER_WINDOW >= 1, "Must allow at least 1 position per window"
            logging.info(
                "CONFIG_VERIFY (hourly): ENABLED=%s OBS_ONLY=%s BLEND_W=%.2f "
                "MIN_ENTRY=%dc MAX_RISK=%.2f MAX_STC=%ds MIN_STC=%ds "
                "TEMP_T=%.2f KELLY_F=%.2f MIN_EDGE=%.3f%%",
                HOURLY_OBSERVATION_ENABLED, HOURLY_OBSERVATION_ONLY,
                HOURLY_MARKET_BLEND_W, HOURLY_MIN_ENTRY_PRICE,
                HOURLY_MAX_RISK_PER_TRADE, HOURLY_MAX_SECONDS_BEFORE_CLOSE,
                HOURLY_MIN_STC_ENTRY,
                HOURLY_TEMPERATURE_T, HOURLY_KELLY_FRACTION,
                HOURLY_MIN_EDGE_PCT * 100)

        # ── Dip addon config verify ──
        if DIP_ADDON_ENABLED:
            assert DIP_ADDON_MIN_DROP_CENTS >= 2, "dip addon drop too small"
            assert DIP_ADDON_MAX_TOTAL_RISK <= MAX_RISK_PER_TRADE * 2, (
                "dip addon risk too high")
            logging.info(
                "CONFIG_VERIFY (dip_addon): ENABLED=%s SHADOW=%s "
                "DROP=%dc STC=%.0fs RISK=%.2f FLOOR=%dc",
                DIP_ADDON_ENABLED, DIP_ADDON_SHADOW_MODE,
                DIP_ADDON_MIN_DROP_CENTS, DIP_ADDON_MIN_STC_REMAINING,
                DIP_ADDON_MAX_TOTAL_RISK, DIP_ADDON_MIN_ENTRY_PRICE)

        # ── Sports config verify ──
        if SPORTS_ENABLED:
            assert SPORTS_OBSERVATION_ONLY is True, (
                "SPORTS_OBSERVATION_ONLY must be True — never live without explicit promotion")
            logging.info(
                "CONFIG_VERIFY (sports): ENABLED=%s OBS_ONLY=%s",
                SPORTS_ENABLED, SPORTS_OBSERVATION_ONLY)

        # ── Validate market_config.py matches bot.py constants ──
        validate_market_configs()

    # ── V2 variant helper (shadow cal pipeline: temperature + no blend) ──

    def _insert_hourly_v2_variant(
        self, ticker: str, window: Dict, asset: str,
        raw_prob: Optional[float], best_ask: int, seconds_remaining: float,
        spot: float, threshold: float, blended_rv: float,
        ofa_adjustment: float, z_score: float, vol_est: Dict,
        calibrated_prob_raw: float, est_fee_1c: float,
        ask_depth: Optional[int], best_ask_source: Optional[str],
        _cf: Dict, _shadow_diag: Dict,
    ):
        """Insert a V2 variant row for hourly signals using the shadow cal pipeline.

        V2 uses temperature scaling + no market blend (vs V1's beta cal + 40% blend).
        Gets its own edge, sizing, and filter_stage for independent PnL simulation.
        Settlement works automatically since it shares the same table + ticker.
        """
        _v2_data = _cf.get("cal_pipeline") or _cf.get("old_cal_system")
        if not _v2_data:
            return
        _v2_prob = _v2_data.get("prob")
        if _v2_prob is None:
            return
        _v2_dedup = (ticker, "hourly_observation_v2")
        if _v2_dedup in self._eval_opp_seen:
            return
        self._eval_opp_seen.add(_v2_dedup)

        _v2_edge = _v2_prob - best_ask / 100.0
        _v2_fee_edge = _v2_data.get("fee_edge")
        if _v2_fee_edge is None:
            _v2_fee_edge = _v2_edge - est_fee_1c / 100.0
        _v2_ev = (_v2_prob * (100 - best_ask)) - ((1 - _v2_prob) * best_ask) - est_fee_1c

        # V2 sizing (Kelly with V2 probability)
        _v2_contracts = 0
        _v2_kelly_f = None
        _v2_drawdown = None
        _v2_balance = self._get_balance_cached()
        if _v2_balance and _v2_balance > 0:
            _v2_sizing = self._sizer.compute(_v2_prob, best_ask, _v2_balance)
            _v2_kelly_f = _v2_sizing["kelly_f"]
            _v2_contracts = _v2_sizing["contracts"]
            _v2_drawdown = _v2_sizing["drawdown_scaler"]
            _v2_scfg = get_market_config("hourly")
            if _v2_scfg.kelly_fraction < 1.0:
                _v2_contracts = max(1, int(_v2_contracts * _v2_scfg.kelly_fraction))
            _v2_max = int((_v2_balance * _v2_scfg.max_risk_per_trade) / best_ask)
            if _v2_contracts > _v2_max:
                _v2_contracts = max(1, _v2_max)

        try:
            self._state.insert_evaluated_opportunity(
                ticker, window["event_ticker"], asset,
                "hourly_observation_v2",
                spot_price=spot, threshold=threshold,
                volatility=blended_rv, market_price=best_ask,
                seconds_to_close=seconds_remaining,
                calibrated_prob=_v2_prob, edge=_v2_edge,
                ofa_adjustment=ofa_adjustment,
                z_score=z_score,
                vol_regime=vol_est["regime"],
                calibrated_prob_raw=calibrated_prob_raw,
                kelly_f=_v2_kelly_f,
                position_size=_v2_contracts,
                drawdown_scaler=_v2_drawdown,
                breakeven_wr=best_ask / 100.0,
                expected_value=round(_v2_ev, 2),
                ask_depth=ask_depth,
                best_ask_source=best_ask_source,
                raw_prob=raw_prob,
                calibration_method="shadow_cal_v2",
                fee_adjusted_edge=_v2_fee_edge,
                product_type="hourly",
                shadow_cal_temperature=_v2_data.get("temperature"),
                **_shadow_diag)
        except Exception:
            logging.warning("insert_evaluated_opportunity failed (hourly_observation_v2)", exc_info=True)

    # ── Public entry point ────────────────────────────────────────────────

    def scan(self, active_windows: List[Dict]) -> Optional[List[Dict]]:
        """Evaluate all windows/markets, return best candidate or None."""
        now = time.time()
        ob_fetches_this_tick = 0
        candidates: List[Dict] = []

        # Reset hourly per-window tracking each tick, seeded from existing positions
        self._hourly_window_counts = {}
        self._hourly_window_risk = {}
        self._config_b_window_counts = {}  # Config B (BTC 70-89c wl2) separate counter
        self._dc_window_risk = {}  # Decided contract per-window risk tracker
        self._dc_window_cap_skips = 0  # Session counter for window cap skips
        try:
            for pos in self._state.get_open_positions():
                evt = pos.get("event_ticker", "")
                if evt:
                    self._hourly_window_counts[evt] = self._hourly_window_counts.get(evt, 0) + 1
                    # Seed DC window risk from existing decided positions
                    strat = pos.get("strategy", "")
                    if strat in ("decided_t1", "decided_t2"):
                        _dc_cost = pos.get("count", 0) * pos.get("avg_price_cents", 0)
                        self._dc_window_risk[evt] = self._dc_window_risk.get(evt, 0.0) + _dc_cost
        except Exception:
            pass  # Non-critical: worst case is slight over-allocation

        # Clean up ask history and dedup set for tickers no longer in active windows
        active_tickers = set()
        for w in active_windows:
            for m in w.get("markets", []):
                active_tickers.add(m.get("ticker", ""))
        expired = [t for t in self._ticker_ask_history if t not in active_tickers]
        for t in expired:
            del self._ticker_ask_history[t]
        expired_ob = [t for t in self._ob_cache if t not in active_tickers]
        for t in expired_ob:
            del self._ob_cache[t]
        # Unsubscribe expired tickers from WS orderbook_delta
        if self._kalshi_feed:
            ws_expired = set(expired) | set(expired_ob)
            for t in ws_expired:
                try:
                    self._kalshi_feed.unsubscribe_ticker(t)
                except Exception:
                    pass
        self._eval_opp_seen = {
            key for key in self._eval_opp_seen if key[0] in active_tickers
        }
        if self._kalshi_oft is not None:
            try:
                self._kalshi_oft.cleanup_stale(active_tickers)
            except Exception:
                pass
        if self._ml and getattr(self._ml, "fifteenm_shadow", None):
            try:
                self._ml.fifteenm_shadow.cleanup_expired(active_tickers)
            except Exception:
                pass
        if self._ml and getattr(self._ml, "hourly_alt_shadow", None):
            try:
                self._ml.hourly_alt_shadow.cleanup_expired(active_tickers)
            except Exception:
                pass

        # Build ticker set for product types that skip WS orderbook subscription (too many strikes)
        _hourly_tickers = set()
        for w in active_windows:
            if w.get("product_type") in ("hourly", "spx_hourly", "weather"):
                for m in w.get("markets", []):
                    _hourly_tickers.add(m.get("ticker", ""))

        # Pre-subscribe all active tickers to WS and feed OFT from WS orderbooks
        if self._kalshi_feed and self._kalshi_feed.is_connected:
            for t in active_tickers:
                if t in _hourly_tickers:
                    continue  # skip WS subscription for hourly (too many strikes per event)
                try:
                    self._kalshi_feed.subscribe_ticker(t)
                except Exception:
                    pass
            # Feed OFT with any available WS orderbook data (zero API cost)
            if self._kalshi_oft is not None:
                for t in active_tickers:
                    try:
                        ws_ob = self._kalshi_feed.get_orderbook(t)
                        if ws_ob and now - ws_ob.get("ts", 0) < 30:
                            best_ask = self._best_yes_ask_cents(ws_ob)
                            if best_ask is not None:
                                self._kalshi_oft.record_snapshot(t, ws_ob, best_ask)
                    except Exception:
                        pass

        # Dynamic scan_stats: include all assets from active windows (SPX, weather, etc.)
        _all_scan_assets = set(ASSETS)
        for w in active_windows:
            _all_scan_assets.add(w["asset"])
        scan_stats: Dict[str, Dict[str, int]] = {
            a: {"evaluated": 0, "low_prob": 0, "no_orderbook": 0, "no_best_ask": 0,
                "price_out_of_range": 0, "insufficient_edge": 0, "zero_sizing": 0,
                "strategy_wait": 0, "candidates": 0}
            for a in _all_scan_assets
        }

        _price_shadow_queue = []
        _no_side_queue = []  # NO-side shadow: markets queued for NO evaluation
        _overnight_lp_queue = []  # Overnight low-price shadow: 50-85c YES during overnight hours

        # 1. Filter windows by time range (config-driven thresholds)
        time_ok_windows = []
        for w in active_windows:
            stc = w["seconds_to_close"]
            _tcfg = get_market_config(w.get("product_type"))
            if _tcfg.min_seconds_before_close <= stc <= _tcfg.max_seconds_before_close:
                time_ok_windows.append(w)
        if not time_ok_windows:
            return None

        # 2. Get occupied timeslots (positions + resting orders)
        occupied = self._get_occupied_timeslots()

        # 3. Filter out windows whose timeslot already has this SAME asset
        eligible_windows = []
        for w in time_ok_windows:
            if w.get("product_type") in ("hourly", "spx_hourly", "weather"):
                eligible_windows.append(w)
                continue  # hourly/spx/weather windows bypass timeslot logic
            ts = self._window_timeslot(w["event_ticker"])
            if ts in occupied and w["asset"] in occupied[ts]:
                continue  # this asset already has a position/order in this timeslot
            eligible_windows.append(w)

        if not eligible_windows:
            return None

        # 4. Evaluate each market in each surviving window
        for window in eligible_windows:
            asset = window["asset"]
            _pt = window.get("product_type")

            # Route price/vol to appropriate engine based on product type
            if _pt == "spx_hourly" and self._ml and getattr(self._ml, "spx_engine", None):
                spot = self._ml.spx_engine.get_spot_price(asset)
                if spot is None or spot <= 0:
                    continue
                seconds_remaining = window["seconds_to_close"]
                vol_est = self._ml.spx_engine.get_vol_estimate(asset, seconds_remaining)
            elif _pt == "weather" and self._ml and getattr(self._ml, "weather_engine", None):
                spot = self._ml.weather_engine.get_spot_price(asset)
                if spot is None or spot <= 0:
                    continue
                seconds_remaining = window["seconds_to_close"]
                vol_est = self._ml.weather_engine.get_vol_estimate(asset, seconds_remaining)
            else:
                spot = self._feed.get_price(asset)
                if spot is None or spot <= 0:
                    continue
                seconds_remaining = window["seconds_to_close"]
                vol_est = self._vol.update(asset, seconds_to_close=seconds_remaining)

            if vol_est is None or vol_est["blended_rv"] <= 0:
                continue

            blended_rv = vol_est["blended_rv"]

            # Extract shadow diagnostics for per-evaluation logging
            # _shadow_diag: fields that match insert_evaluated_opportunity/insert_rejection params
            _ebs_var = vol_est.get("egarch_blend_var")
            # Fallback: if engine didn't compute egarch_blend_var but has constituents, compute here
            if _ebs_var is None:
                _fb_sigma = vol_est.get("egarch_sigma")
                _fb_bw = vol_est.get("egarch_blend_weight")
                _fb_rk = vol_est.get("rk_rv")
                _fb_sf = vol_est.get("seasonal_factor", 1.0)
                if _fb_sigma and _fb_bw and _fb_bw > 0 and _fb_rk and _fb_rk > 0 and _fb_sf:
                    _fb_erv = _fb_sigma * _fb_sf
                    _ebs_var = _fb_bw * (_fb_erv ** 2) + (1 - _fb_bw) * (_fb_rk ** 2)
            _shadow_diag = {
                "egarch_sigma": vol_est.get("egarch_sigma"),
                "egarch_blend_sigma": math.sqrt(_ebs_var) if _ebs_var and _ebs_var > 0 else None,
                "egarch_blend_weight": vol_est.get("egarch_blend_weight"),
                "mz_r_squared": vol_est.get("mz_r_squared"),
                "shadow_tv_blend_rv": vol_est.get("shadow_tv_blend_rv"),
                "mz_shadow_sigmoid_w": vol_est.get("mz_shadow_sigmoid_w"),
                "mz_baseline_qlike": vol_est.get("mz_baseline_qlike"),
                "mz_qlike": vol_est.get("mz_qlike"),
            }
            # _shadow_extra_base: additional fields for log_opportunity (not in DB insert params)
            # Copied per-market to avoid OFT field bleed between tickers
            _shadow_extra_base = {
                "shadow_tv_weights": vol_est.get("shadow_tv_weights"),
                "mz_sigmoid_improvement": vol_est.get("mz_sigmoid_improvement"),
                "mz_sigmoid_blend_rv": vol_est.get("mz_sigmoid_blend_rv"),
                "egarch_n_updates": vol_est.get("egarch_n_updates"),
                "egarch_ratio_clamped": vol_est.get("egarch_ratio_clamped"),
            }

            for mkt in window["markets"]:
                ticker = mkt.get("ticker", "")
                threshold = self._parse_threshold(mkt)
                if threshold is None:
                    continue

                # Early NBBO price filter for multi-strike events (SPX: 60-400 markets).
                # Skip probability computation for strikes clearly outside entry range.
                if _pt in ("spx_hourly", "hourly", "weather"):
                    _nbbo_raw = mkt.get("yes_ask_dollars") or mkt.get("yes_ask")
                    if _nbbo_raw is not None:
                        _nbbo = dollars_str_to_cents(_nbbo_raw) if isinstance(_nbbo_raw, str) else int(_nbbo_raw)
                        _pcfg_early = get_market_config(_pt)
                        if _nbbo > 0 and not (_pcfg_early.min_entry_price <= _nbbo <= _pcfg_early.max_entry_price):
                            continue

                # Per-market copy of shadow extras (OFT fields added per-ticker below)
                _shadow_extra = dict(_shadow_extra_base)
                # OFT fields for DB insert — populated after ofa_signals computed
                _oft_db = {}

                scan_stats[asset]["evaluated"] += 1
                self._session_total_scanned += 1

                # Pre-filter: compute probability without market price
                if _pt == "weather" and self._ml and getattr(self._ml, "weather_engine", None):
                    # Weather uses ensemble-based Gaussian model, NOT lognormal ProbabilityEngine
                    _wx_city = asset.replace("_TEMP", "")
                    _wx_info = self._parse_weather_market_info(mkt)
                    _wx_mtype = _wx_info[0] if _wx_info else None
                    _wx_bounds = (_wx_info[1], _wx_info[2]) if (_wx_info and _wx_info[0] == "bracket") else None
                    _shadow_extra["wx_market_type"] = _wx_mtype
                    _wx_prob = self._ml.weather_engine.get_probability(
                        _wx_city, threshold,
                        market_type=_wx_mtype, bracket_bounds=_wx_bounds)
                    if _wx_prob is None:
                        continue
                    # R3: Ensemble quality gate — skip if no ensemble data available
                    if not _wx_prob.get("n_members"):
                        logging.debug("WEATHER_SKIP: %s no ensemble data (n_members=None/0)", ticker)
                        _shadow_extra["wx_ensemble_mean"] = None
                        _shadow_extra["wx_ensemble_std"] = None
                        _shadow_extra["wx_n_members"] = 0
                        _fs = "data_unavailable"
                        _dedup_key_ens = (ticker, _fs)
                        if _dedup_key_ens not in self._eval_opp_seen:
                            self._eval_opp_seen.add(_dedup_key_ens)
                            _nbbo_val = int(_nbbo) if _nbbo_raw is not None else None
                            self._state.insert_evaluated_opportunity(
                                ticker=ticker, event_ticker=window.get("event_ticker", ""),
                                asset=asset, product_type=_pt, filter_stage=_fs,
                                market_price=_nbbo_val,
                                wx_ensemble_mean=None, wx_ensemble_std=None,
                                wx_n_members=0,
                                wx_market_type=_shadow_extra.get("wx_market_type"),
                            )
                        continue
                    prob_result = {
                        "calibrated_prob": _wx_prob.get("calibrated_prob"),
                        "raw_prob": _wx_prob.get("raw_prob"),
                        "calibration_method": "weather_ensemble",
                        "tradeable": True,  # weather ensemble always tradeable (no z-score gate)
                        "z_score": 0.0,
                    }
                    # Store weather ensemble diagnostics for DB
                    _shadow_extra["wx_ensemble_mean"] = _wx_prob.get("ensemble_mean")
                    _shadow_extra["wx_ensemble_std"] = _wx_prob.get("ensemble_std")
                    _shadow_extra["wx_bias_correction"] = _wx_prob.get("bias_correction")
                    _shadow_extra["wx_n_members"] = _wx_prob.get("n_members")
                    _shadow_extra["wx_hrrr_temp"] = _wx_prob.get("hrrr_temp")
                    _shadow_extra["wx_corrected_mean"] = _wx_prob.get("corrected_mean")
                    logging.info(
                        "WEATHER_PROB: %s thresh=%.1fF ens_mean=%.1fF ens_std=%.2fF prob=%.4f type=%s",
                        ticker, threshold,
                        _wx_prob.get("ensemble_mean") or 0.0,
                        _wx_prob.get("ensemble_std") or 0.0,
                        _wx_prob.get("calibrated_prob") or 0.0,
                        _wx_mtype or "unknown")
                else:
                    prob_result = ProbabilityEngine.compute(
                        spot, threshold, seconds_remaining, blended_rv,
                        asset=asset, product_type=window.get("product_type")
                    )
                cal_prob = prob_result.get("calibrated_prob")
                raw_prob_pre = prob_result.get("raw_prob")
                calibration_method_pre = prob_result.get("calibration_method")
                if not prob_result.get("tradeable"):
                    reason = prob_result.get("reason", "")
                    if "z_score" in reason or "refusing" in reason:
                        # Fetch orderbook to record market price for counterfactual P&L
                        rej_ob, _ = self._get_orderbook_cached(ticker)
                        rej_ask = self._best_yes_ask_cents(rej_ob) if rej_ob else None
                        rej_data = {
                            "ticker": ticker,
                            "event_ticker": window["event_ticker"],
                            "asset": asset,
                            "rejection_reason": reason,
                            "z_score": prob_result.get("z_score"),
                            "spot_price": spot,
                            "threshold": threshold,
                            "volatility": blended_rv,
                            "market_price": rej_ask,
                            "seconds_to_close": round(seconds_remaining, 1),
                            "calibrated_prob": cal_prob,
                            "raw_prob": raw_prob_pre,
                            **_shadow_diag,
                            **_shadow_extra,
                        }
                        self._state.insert_rejection(
                            ticker, window["event_ticker"], asset, reason,
                            prob_result.get("z_score"), spot, threshold,
                            blended_rv, rej_ask, seconds_remaining, cal_prob,
                            raw_prob=raw_prob_pre,
                            product_type=window.get("product_type"),
                            **_oft_db, **_shadow_diag)
                        self._logger.log_rejection(rej_data)
                        logging.info(
                            f"Rejected opportunity: {ticker} — {reason}")
                    continue

                # Skip if calibrated prob too low to ever produce an edge
                _pcfg = get_market_config(window.get("product_type"))
                _min_price = _pcfg.min_entry_price
                min_prob_needed = (_min_price + MIN_EDGE_PCT) / 100.0
                if cal_prob < min_prob_needed:
                    scan_stats[asset]["low_prob"] += 1
                    if window.get("product_type") in ("hourly", "spx_hourly", "weather"):
                        # Volume control: count but don't log (many strikes are low_prob)
                        continue
                    try:
                        self._logger.log_opportunity({
                            "filter_stage": "low_probability",
                            "ticker": ticker,
                            "event_ticker": window["event_ticker"],
                            "asset": asset,
                            "rejection_reason": f"cal_prob {cal_prob:.4f} < min_needed {min_prob_needed:.4f}",
                            "spot_price": spot,
                            "threshold": threshold,
                            "volatility": blended_rv,
                            "seconds_to_close": round(seconds_remaining, 1),
                            "calibrated_prob": round(cal_prob, 6),
                            "raw_prob": round(raw_prob_pre, 6) if raw_prob_pre is not None else None,
                            **_shadow_diag,
                            **_shadow_extra,
                        })
                    except Exception:
                        pass
                    continue

                # Fetch orderbook (cached, rate-limited)
                ob_data, was_fresh = self._get_orderbook_cached(ticker)
                if was_fresh:
                    ob_fetches_this_tick += 1
                if ob_data is None:
                    # Try NBBO fallback before giving up (prefer *_dollars field)
                    mkt_yes_ask_raw = mkt.get("yes_ask_dollars") or mkt.get("yes_ask")
                    if mkt_yes_ask_raw:
                        if isinstance(mkt_yes_ask_raw, str):
                            mkt_yes_ask = dollars_str_to_cents(mkt_yes_ask_raw)
                        else:
                            mkt_yes_ask = int(mkt_yes_ask_raw)
                    else:
                        mkt_yes_ask = None
                    if mkt_yes_ask and mkt_yes_ask > 0:
                        ob_data = {}  # empty dict so downstream code works
                        logging.info(
                            "Orderbook unavailable for %s, will use market NBBO yes_ask=%d¢",
                            ticker, mkt_yes_ask,
                        )
                    else:
                        scan_stats[asset]["no_orderbook"] += 1
                        try:
                            self._logger.log_opportunity({
                                "filter_stage": "no_orderbook",
                                "ticker": ticker,
                                "event_ticker": window["event_ticker"],
                                "asset": asset,
                                "rejection_reason": "orderbook data unavailable and no market NBBO",
                                "spot_price": spot,
                                "threshold": threshold,
                                "volatility": blended_rv,
                                "seconds_to_close": round(seconds_remaining, 1),
                                "calibrated_prob": round(cal_prob, 6),
                                "mkt_yes_ask": mkt_yes_ask,
                                "raw_prob": round(raw_prob_pre, 6) if raw_prob_pre is not None else None,
                                **_shadow_diag,
                                **_shadow_extra,
                            })
                        except Exception:
                            pass
                        continue

                best_ask = self._best_yes_ask_cents(ob_data)
                best_ask_source = "orderbook"
                if best_ask is None:
                    # Fallback: use market's NBBO yes_ask (prefer *_dollars field)
                    mkt_yes_ask_raw = mkt.get("yes_ask_dollars") or mkt.get("yes_ask")
                    if mkt_yes_ask_raw:
                        if isinstance(mkt_yes_ask_raw, str):
                            mkt_yes_ask = dollars_str_to_cents(mkt_yes_ask_raw)
                        else:
                            mkt_yes_ask = int(mkt_yes_ask_raw)
                    else:
                        mkt_yes_ask = None
                    if mkt_yes_ask and mkt_yes_ask > 0:
                        best_ask = mkt_yes_ask
                        best_ask_source = "market_nbbo"
                        logging.info(
                            "Using market NBBO yes_ask=%d¢ for %s (orderbook NO bids empty)",
                            best_ask, ticker,
                        )
                if best_ask is not None:
                    if ticker not in self._ticker_ask_history:
                        self._ticker_ask_history[ticker] = deque(maxlen=300)
                    self._ticker_ask_history[ticker].append((time.time(), best_ask))
                if best_ask is None:
                    scan_stats[asset]["no_best_ask"] += 1
                    try:
                        self._logger.log_opportunity({
                            "filter_stage": "no_best_ask",
                            "ticker": ticker,
                            "event_ticker": window["event_ticker"],
                            "asset": asset,
                            "rejection_reason": "no best ask in orderbook or market NBBO",
                            "spot_price": spot,
                            "threshold": threshold,
                            "volatility": blended_rv,
                            "seconds_to_close": round(seconds_remaining, 1),
                            "calibrated_prob": round(cal_prob, 6),
                            "mkt_yes_ask": mkt.get("yes_ask"),
                            "raw_prob": round(raw_prob_pre, 6) if raw_prob_pre is not None else None,
                            **_shadow_diag,
                            **_shadow_extra,
                        })
                    except Exception:
                        pass
                    continue

                # Diagnostic: log when orderbook and market NBBO disagree
                try:
                    mkt_yes_ask_raw = mkt.get("yes_ask")
                    if mkt_yes_ask_raw and best_ask_source == "orderbook":
                        mkt_nbbo = int(mkt_yes_ask_raw)
                        if mkt_nbbo != best_ask:
                            logging.debug(
                                "NBBO mismatch %s: orderbook=%d¢ market_nbbo=%d¢ (diff=%d¢)",
                                ticker, best_ask, mkt_nbbo, abs(best_ask - mkt_nbbo),
                            )
                except Exception:
                    pass

                # Compute orderbook depth early (used in logging + strategy)
                ask_depth = OrderExecutor._best_ask_depth(ob_data)
                total_depth = OrderExecutor._total_ob_depth(ob_data)

                # ── MM fill simulation: check if shadow buy orders would fill ──
                # For hourly tickers with active MM shadow orders, check if the
                # current ask has dropped to/below the shadow buy price.
                if (_pt == "hourly"
                        and self._ml and getattr(self._ml, "hourly_alt_shadow", None)):
                    try:
                        _mm_bid = OrderExecutor._best_yes_bid(ob_data) if ob_data else None
                        self._ml.hourly_alt_shadow.check_mm_fills(
                            ticker, best_ask, _mm_bid or 0)
                    except Exception:
                        pass  # Fill check is advisory, don't break scan

                # Record orderbook snapshot for flow tracking
                try:
                    if self._kalshi_oft is not None and ob_data:
                        self._kalshi_oft.record_snapshot(ticker, ob_data, best_ask)
                except Exception:
                    pass

                # Log price snapshot for all markets with orderbook data
                try:
                    self._logger.log_scan({
                        "type": "price_snapshot",
                        "ticker": ticker,
                        "asset": asset,
                        "seconds_to_close": round(seconds_remaining, 1),
                        "best_ask": best_ask,
                        "best_ask_source": best_ask_source,
                        "best_ask_depth": ask_depth,
                        "total_ob_depth": total_depth,
                        "convergence_velocity": self._scanner_convergence_velocity(ticker),
                        "calibrated_prob": round(cal_prob, 6),
                    })
                except Exception:
                    pass

                # ── 15M Shadow Engine (pre-filter: all signals) ──
                # Evaluate shadow approaches for ALL 15M signals, not just those
                # passing the price filter.  _seen dedup in shadow engine prevents
                # double-eval if the signal also passes filters and hits the
                # post-filter call below.  Uses cal_prob (pre-temperature/blend)
                # as live_prob approximation — shadow approaches do their own cal.
                if (window.get("product_type") in (None, "15m")
                        and self._ml and getattr(self._ml, "fifteenm_shadow", None)
                        and best_ask is not None and cal_prob is not None):
                    try:
                        _15m_pre_bid = OrderExecutor._best_yes_bid(ob_data) if ob_data else None
                        _15m_pre_edge = cal_prob - best_ask / 100.0
                        _15m_pre_fee = calculate_fee(
                            1, best_ask, is_taker=True,
                            fee_mult_taker=get_market_config("15m").fee_multiplier_taker,
                            fee_mult_maker=get_market_config("15m").fee_multiplier_maker)
                        _15m_pre_fee_edge = _15m_pre_edge - _15m_pre_fee / 100.0
                        _15m_pre_no_ask_raw = mkt.get("no_ask_dollars") or mkt.get("no_ask")
                        _15m_pre_no_ask = (dollars_str_to_cents(_15m_pre_no_ask_raw) if isinstance(_15m_pre_no_ask_raw, str)
                                           else int(_15m_pre_no_ask_raw)) if _15m_pre_no_ask_raw is not None else None
                        self._ml.fifteenm_shadow.evaluate_strike(
                            asset=asset, ticker=ticker,
                            event_ticker=window["event_ticker"],
                            spot_price=spot, threshold=threshold,
                            seconds_to_close=seconds_remaining,
                            market_price=best_ask,
                            best_bid=_15m_pre_bid, best_ask=best_ask,
                            blended_rv=blended_rv,
                            egarch_sigma=vol_est.get("egarch_sigma"),
                            z_score=prob_result.get("z_score", 0.0),
                            live_prob=cal_prob,
                            live_edge=_15m_pre_edge,
                            live_fee_edge=_15m_pre_fee_edge,
                            egarch_blend_weight=_shadow_diag.get("egarch_blend_weight"),
                            fee_adjusted_edge=_15m_pre_fee_edge,
                            no_ask=_15m_pre_no_ask)
                    except Exception:
                        logging.warning("fifteenm_shadow pre-filter evaluate failed", exc_info=True)

                # Filter: ask must be in entry price range
                _pricecfg = get_market_config(window.get("product_type"))
                _entry_floor = _pricecfg.min_entry_price
                _entry_ceil = _pricecfg.max_entry_price
                if not (_entry_floor <= best_ask <= _entry_ceil):
                    scan_stats[asset]["price_out_of_range"] += 1
                    # Compute raw edge for instrumentation (pre-temperature, pre-blend)
                    _por_edge = cal_prob - best_ask / 100.0
                    _por_fee = calculate_fee(
                        1, best_ask, is_taker=True,
                        fee_mult_taker=_pricecfg.fee_multiplier_taker,
                        fee_mult_maker=_pricecfg.fee_multiplier_maker)
                    _por_fee_edge = _por_edge - _por_fee / 100.0
                    _por_z = prob_result.get("z_score")
                    self._recent_opportunities.append({
                        "ticker": ticker, "asset": asset,
                        "seconds_to_close": round(seconds_remaining, 1),
                        "best_ask": best_ask, "edge_bps": round(_por_edge * 10000) if _por_edge else None,
                        "chosen_strategy": None,
                        "rejection_reason": "price_out_of_range",
                        "ts": datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                    })
                    try:
                        self._logger.log_opportunity({
                            "filter_stage": "price_out_of_range",
                            "ticker": ticker,
                            "event_ticker": window["event_ticker"],
                            "asset": asset,
                            "rejection_reason": f"best_ask {best_ask}¢ outside [{_entry_floor}, {MAX_ENTRY_PRICE}]",
                            "spot_price": spot,
                            "threshold": threshold,
                            "volatility": blended_rv,
                            "market_price": best_ask,
                            "best_ask_source": best_ask_source,
                            "seconds_to_close": round(seconds_remaining, 1),
                            "calibrated_prob": round(cal_prob, 6),
                            "best_ask_depth": ask_depth,
                            "total_ob_depth": total_depth,
                            "convergence_velocity": self._scanner_convergence_velocity(ticker),
                            "raw_prob": round(raw_prob_pre, 6) if raw_prob_pre is not None else None,
                            **_shadow_diag,
                            **_shadow_extra,
                        })
                        _dedup_key = (ticker, "price_out_of_range")
                        if _dedup_key not in self._eval_opp_seen:
                            self._eval_opp_seen.add(_dedup_key)
                            self._state.insert_evaluated_opportunity(
                                ticker, window["event_ticker"], asset,
                                "price_out_of_range",
                                rejection_reason=f"best_ask {best_ask}¢ outside range",
                                spot_price=spot, threshold=threshold,
                                volatility=blended_rv, market_price=best_ask,
                                seconds_to_close=seconds_remaining,
                                calibrated_prob=cal_prob,
                                edge=_por_edge,
                                fee_adjusted_edge=_por_fee_edge,
                                z_score=_por_z,
                                vol_regime=vol_est["regime"],
                                breakeven_wr=best_ask / 100.0,
                                ask_depth=ask_depth,
                                best_ask_source=best_ask_source,
                                raw_prob=raw_prob_pre,
                                calibration_method=calibration_method_pre,
                                product_type=window.get("product_type"),
                                wx_ensemble_mean=_shadow_extra.get("wx_ensemble_mean"),
                                wx_ensemble_std=_shadow_extra.get("wx_ensemble_std"),
                                wx_bias_correction=_shadow_extra.get("wx_bias_correction"),
                                wx_n_members=_shadow_extra.get("wx_n_members"),
                                wx_market_type=_shadow_extra.get("wx_market_type"),
                                wx_hrrr_temp=_shadow_extra.get("wx_hrrr_temp"),
                                wx_corrected_mean=_shadow_extra.get("wx_corrected_mean"),
                                hourly_pre_temp_prob=None, hourly_applied_temp_t=None,
                                hourly_shadow_temp_2_0=None, hourly_shadow_temp_1_0=None,
                                hourly_shadow_temp_2_5=None, hourly_shadow_blend_50=None,
                                hourly_shadow_temp_1_75=None, hourly_shadow_temp_3_0=None,
                                hourly_shadow_blend_20=None, hourly_shadow_blend_30=None,
                                hourly_shadow_blend_60=None, hourly_post_temp_prob=None,
                                **_oft_db, **_shadow_diag)
                    except Exception:
                        logging.warning("insert_evaluated_opportunity failed (price_out_of_range)", exc_info=True)
                    if PRICE_SHADOW_ENABLED and PRICE_SHADOW_FLOOR <= best_ask < _entry_floor:
                        _price_shadow_queue.append({
                            "ticker": ticker,
                            "event_ticker": window["event_ticker"],
                            "asset": asset,
                            "best_ask": best_ask,
                            "spot": spot,
                            "threshold": threshold,
                            "blended_rv": blended_rv,
                            "seconds_remaining": seconds_remaining,
                            "vol_regime": vol_est["regime"],
                            "ask_depth": ask_depth,
                            "best_ask_source": best_ask_source,
                            "product_type": window.get("product_type"),
                            "_shadow_diag": _shadow_diag.copy(),
                            "_oft_db": _oft_db.copy(),
                        })
                    # Overnight LP shadow: queue 50-85c 15M contracts during overnight hours
                    _olp_utc_hour = datetime.datetime.now(timezone.utc).hour
                    if (OVERNIGHT_LP_SHADOW
                            and _pt in (None, "15m")
                            and OVERNIGHT_LP_HOURS_START <= _olp_utc_hour < OVERNIGHT_LP_HOURS_END
                            and OVERNIGHT_LP_MIN_ENTRY_PRICE <= best_ask <= OVERNIGHT_LP_MAX_ENTRY_PRICE
                            and OVERNIGHT_LP_MIN_STC <= seconds_remaining <= OVERNIGHT_LP_MAX_STC):
                        _overnight_lp_queue.append({
                            "ticker": ticker,
                            "event_ticker": window["event_ticker"],
                            "asset": asset,
                            "best_ask": best_ask,
                            "best_bid": mkt.get("yes_bid") or (100 - (mkt.get("no_ask") or 100)),
                            "spot": spot,
                            "threshold": threshold,
                            "blended_rv": blended_rv,
                            "seconds_remaining": seconds_remaining,
                            "vol_regime": vol_est["regime"],
                            "ask_depth": ask_depth,
                            "best_ask_source": best_ask_source,
                            "product_type": window.get("product_type"),
                            "_shadow_diag": _shadow_diag.copy(),
                            "_oft_db": _oft_db.copy(),
                        })
                    # NO-side shadow: read actual NO ask from market NBBO
                    _no_ask_por = None
                    _no_ask_raw = mkt.get("no_ask_dollars") or mkt.get("no_ask")
                    if _no_ask_raw is not None:
                        _no_ask_por = dollars_str_to_cents(_no_ask_raw) if isinstance(_no_ask_raw, str) else int(_no_ask_raw)
                    if _no_ask_por is not None and _no_ask_por <= 0:
                        _no_ask_por = None
                    if _no_ask_por is not None and NO_SIDE_MIN_ENTRY_PRICE <= _no_ask_por <= _entry_ceil:
                        _no_side_queue.append({
                            "ticker": ticker,
                            "event_ticker": window["event_ticker"],
                            "asset": asset,
                            "best_ask": best_ask,
                            "no_ask": _no_ask_por,
                            "spot": spot,
                            "threshold": threshold,
                            "blended_rv": blended_rv,
                            "seconds_remaining": seconds_remaining,
                            "vol_regime": vol_est["regime"],
                            "ask_depth": ask_depth,
                            "best_ask_source": best_ask_source,
                            "product_type": window.get("product_type"),
                            "_shadow_diag": _shadow_diag.copy(),
                            "_oft_db": _oft_db.copy(),
                            "_shadow_extra": _shadow_extra.copy(),
                            "final_prob": None,  # needs computation
                            "cal_prob": cal_prob,
                            "raw_prob": raw_prob_pre,
                            "calibration_method": calibration_method_pre,
                            "hourly_pre_temp_prob": None,  # POR path — before temp computation
                            "hourly_applied_temp_t": None,
                            "hourly_post_temp_prob": None,
                        })
                    continue

                # ── Per-asset price floor (15M only) ─────────────────────────
                # BTC 89c+: 86-88c below taker BE. ETH 88c+. SOL 86c global floor.
                # XRP 92c+: PnL-negative at every floor below 90c.
                # Shadow variants: ETH 76c, SOL 80c — forward validation of lower floors.
                _asset_floor = MIN_ENTRY_PRICE  # default (SOL)
                if _pt in (None, "15m"):
                    if asset == "BTC":
                        _asset_floor = BTC_MIN_ENTRY_PRICE
                    elif asset == "ETH":
                        _asset_floor = ETH_MIN_ENTRY_PRICE
                    elif asset == "XRP":
                        _asset_floor = XRP_MIN_ENTRY_PRICE
                if _pt in (None, "15m") and best_ask < _asset_floor:
                    _frs_edge = cal_prob - best_ask / 100.0
                    _frs_fee = calculate_fee(
                        1, best_ask, is_taker=True,
                        fee_mult_taker=_pricecfg.fee_multiplier_taker,
                        fee_mult_maker=_pricecfg.fee_multiplier_maker)
                    _frs_fee_edge = _frs_edge - _frs_fee / 100.0
                    # Tag aggressive-floor shadow variants for forward validation
                    _frs_stage = "floor_raise_shadow"
                    if asset == "ETH" and best_ask >= 76:
                        _frs_stage = "eth_low_floor_shadow"
                    elif asset == "SOL" and best_ask >= 80:
                        _frs_stage = "sol_low_floor_shadow"
                    _dedup_key = (ticker, _frs_stage)
                    if _dedup_key not in self._eval_opp_seen:
                        self._eval_opp_seen.add(_dedup_key)
                        self._state.insert_evaluated_opportunity(
                            ticker, window["event_ticker"], asset,
                            _frs_stage,
                            rejection_reason=f"{asset} floor {_asset_floor}c (global {MIN_ENTRY_PRICE}c), ask={best_ask}c",
                            spot_price=spot, threshold=threshold,
                            volatility=blended_rv, market_price=best_ask,
                            seconds_to_close=seconds_remaining,
                            calibrated_prob=cal_prob,
                            edge=_frs_edge,
                            fee_adjusted_edge=_frs_fee_edge,
                            z_score=prob_result.get("z_score"),
                            vol_regime=vol_est["regime"],
                            breakeven_wr=best_ask / 100.0,
                            ask_depth=ask_depth,
                            best_ask_source=best_ask_source,
                            raw_prob=raw_prob_pre,
                            calibration_method=calibration_method_pre,
                            product_type=window.get("product_type"),
                            **_oft_db, **_shadow_diag)
                    continue

                # Re-run probability with market price for sanity check
                if _pt == "weather":
                    # Weather: ensemble model doesn't use market price; skip z-score sanity check
                    prob_with_market = prob_result.copy()
                    prob_with_market["tradeable"] = True
                    prob_with_market["z_score"] = 0.0
                else:
                    prob_with_market = ProbabilityEngine.compute(
                        spot, threshold, seconds_remaining, blended_rv,
                        market_price_cents=best_ask,
                        asset=asset, product_type=window.get("product_type")
                    )
                if not prob_with_market.get("tradeable"):
                    reason = prob_with_market.get("reason", "")
                    if "z_score" in reason or "refusing" in reason:
                        rej_data = {
                            "ticker": ticker,
                            "event_ticker": window["event_ticker"],
                            "asset": asset,
                            "rejection_reason": reason,
                            "z_score": prob_with_market.get("z_score"),
                            "spot_price": spot,
                            "threshold": threshold,
                            "volatility": blended_rv,
                            "market_price": best_ask,
                            "seconds_to_close": round(seconds_remaining, 1),
                            "calibrated_prob": prob_with_market.get("calibrated_prob"),
                            "raw_prob": prob_with_market.get("raw_prob"),
                            **_shadow_diag,
                            **_shadow_extra,
                        }
                        self._state.insert_rejection(
                            ticker, window["event_ticker"], asset, reason,
                            prob_with_market.get("z_score"), spot, threshold,
                            blended_rv, best_ask, seconds_remaining,
                            prob_with_market.get("calibrated_prob"),
                            raw_prob=prob_with_market.get("raw_prob"),
                            product_type=window.get("product_type"),
                            **_oft_db, **_shadow_diag)
                        self._logger.log_rejection(rej_data)
                        logging.info(
                            f"Rejected opportunity: {ticker} — {reason}")
                    continue

                final_prob = prob_with_market["calibrated_prob"]
                z_score = prob_with_market["z_score"]
                raw_prob = prob_with_market.get("raw_prob")
                calibration_method = prob_with_market.get("calibration_method")

                # ── Temperature scaling (Layer 1) ──────────────
                _hourly_pre_temp_prob = None
                _tempcfg = get_market_config(window.get("product_type"))
                _temp_t = _tempcfg.temperature_t if _tempcfg.temperature_enabled else None
                _configured_temp_t = _temp_t  # record configured T for instrumentation (before CalEngine override)
                # T=1.0 is identity — skip scaling
                if _temp_t is not None and _temp_t == 1.0:
                    _temp_t = None
                # Skip temperature if registered engine is active (already calibrated)
                _reg_engine_t = _resolve_cal_engine(_pt, asset, require_enabled=True)
                if _reg_engine_t is not None and _reg_engine_t.is_learned_method_active():
                    _temp_t = None
                    _hourly_pre_temp_prob = final_prob  # still record for shadow instrumentation
                if _temp_t is not None:
                    _hourly_pre_temp_prob = final_prob
                    _p = max(0.001, min(0.999, final_prob))
                    _z = math.log(_p / (1.0 - _p))
                    _z_scaled = _z / _temp_t
                    final_prob = 1.0 / (1.0 + math.exp(-_z_scaled))

                # ── Shadow temperature + blend instrumentation (hourly + SPX) ──
                _hourly_shadow_temp_2_0 = None
                _hourly_shadow_temp_1_0 = None
                _hourly_shadow_temp_2_5 = None
                _hourly_shadow_temp_1_75 = None
                _hourly_shadow_temp_3_0 = None
                _hourly_shadow_blend_50 = None
                _hourly_shadow_blend_20 = None
                _hourly_shadow_blend_30 = None
                _hourly_shadow_blend_60 = None
                _hourly_post_temp_prob = None

                _shadow_base = _hourly_pre_temp_prob
                if _shadow_base is None and _pt == "spx_hourly":
                    _shadow_base = final_prob
                    _hourly_pre_temp_prob = final_prob
                    _temp_t = 1.0

                if _shadow_base is not None:
                    _sp = max(0.001, min(0.999, _shadow_base))
                    _sz = math.log(_sp / (1.0 - _sp))

                    # Temperature shadows (pre-blend)
                    _hourly_shadow_temp_2_0 = 1.0 / (1.0 + math.exp(-_sz / 2.0))
                    _hourly_shadow_temp_2_5 = 1.0 / (1.0 + math.exp(-_sz / 2.5))
                    _hourly_shadow_temp_1_75 = 1.0 / (1.0 + math.exp(-_sz / 1.75))
                    _hourly_shadow_temp_3_0 = 1.0 / (1.0 + math.exp(-_sz / 3.0))
                    if _pt == "spx_hourly":
                        _hourly_shadow_temp_1_0 = 1.0 / (1.0 + math.exp(-_sz / 1.5))
                    else:
                        _hourly_shadow_temp_1_0 = _shadow_base

                    # Post-temp prob: tempered value before OFA and blend
                    # For offline analysis: (1-W) * post_temp + W * (mkt/100) = any blend
                    _hourly_post_temp_prob = final_prob

                    # Blend shadows: full final = liveT + shadowW
                    # Uses final_prob (tempered, pre-OFA) — isolates T×W effect
                    if best_ask < ENDGAME_BLEND_PRICE:
                        _mkt_p = best_ask / 100.0
                        _hourly_shadow_blend_20 = 0.80 * final_prob + 0.20 * _mkt_p
                        _hourly_shadow_blend_30 = 0.70 * final_prob + 0.30 * _mkt_p
                        _hourly_shadow_blend_50 = 0.50 * final_prob + 0.50 * _mkt_p
                        _hourly_shadow_blend_60 = 0.40 * final_prob + 0.60 * _mkt_p

                # Order flow adjustment
                ofa_signals = None
                ofa_adjustment = 0.0
                if self._order_flow is not None:
                    try:
                        ofa_signals = self._order_flow.get_signals(asset, ticker=ticker)
                        ofa_adjustment = ofa_signals["prob_adjustment"]
                    except Exception:
                        logging.debug("OrderFlowEngine.get_signals failed", exc_info=True)
                calibrated_prob_raw = final_prob
                # Always compute dynamic cap for counterfactual logging
                _dyn_cap = ProbabilityEngine._dynamic_cap(seconds_remaining, product_type=window.get("product_type"))
                _reg_engine_c = _resolve_cal_engine(_pt, asset, require_enabled=True)
                if _reg_engine_c is not None and _reg_engine_c.is_learned_method_active():
                    _active_cal = _reg_engine_c
                elif get_market_config(_pt).cal_eligible:
                    _active_cal = _CALIBRATION_ENGINE
                else:
                    _active_cal = None
                if _active_cal is not None and _active_cal.is_learned_method_active():
                    # Learned method: no dynamic cap, use safety ceiling only
                    final_prob = max(0.01, min(NUMERICAL_SAFETY_CEILING, final_prob + ofa_adjustment))
                else:
                    final_prob = max(0.01, min(_dyn_cap, final_prob + ofa_adjustment))
                # Counterfactual: calibrated prob with dynamic cap applied
                # (after temp promotion, CF5 "old_cal_system" has the full Beta Cal + blend counterfactual)
                _old_system_prob = max(0.01, min(_dyn_cap, calibrated_prob_raw + ofa_adjustment))
                if best_ask < ENDGAME_BLEND_PRICE:
                    _mkt = best_ask / 100.0
                    _old_system_prob = (1.0 - MARKET_BLEND_W) * _old_system_prob + MARKET_BLEND_W * _mkt

                # ── Market-price blending ──────────────────────────────────
                # For mid-range prices, blend model with market to temper overconfidence.
                # Skip blending for endgame (≥96c) where dynamic cap provides the edge.
                _mcfg = get_market_config(window.get("product_type"))
                _effective_blend_w = _mcfg.market_blend_w
                if best_ask < ENDGAME_BLEND_PRICE:
                    market_implied_prob = best_ask / 100.0
                    final_prob = (1.0 - _effective_blend_w) * final_prob + _effective_blend_w * market_implied_prob

                edge = final_prob - best_ask / 100.0

                # Fee-adjusted edge: subtract taker fee for 1 contract
                # (conservative — more contracts = lower per-contract fee)
                # Uses config-driven fee multipliers (fixes SPX paying 2x correct taker fee)
                est_fee_1c = calculate_fee(1, best_ask, is_taker=True,
                                           fee_mult_taker=_mcfg.fee_multiplier_taker,
                                           fee_mult_maker=_mcfg.fee_multiplier_maker)
                fee_adjusted_edge = edge - est_fee_1c / 100.0

                # ── Weather NO-side shadow edge ──
                if _pt == "weather":
                    _no_prob = 1.0 - final_prob
                    # NO ask from market NBBO
                    _wx_no_ask_raw = mkt.get("no_ask_dollars") or mkt.get("no_ask")
                    _wx_no_ask = (dollars_str_to_cents(_wx_no_ask_raw) if isinstance(_wx_no_ask_raw, str)
                                  else int(_wx_no_ask_raw)) if _wx_no_ask_raw is not None else None
                    if _wx_no_ask is not None and _wx_no_ask <= 0:
                        _wx_no_ask = None
                    if _wx_no_ask is not None:
                        _no_fee = calculate_fee(1, _wx_no_ask, is_taker=True,
                                                fee_mult_taker=_mcfg.fee_multiplier_taker,
                                                fee_mult_maker=_mcfg.fee_multiplier_maker)
                        _shadow_extra["wx_no_side_edge"] = round(
                            _no_prob - _wx_no_ask / 100.0 - _no_fee / 100.0, 6)

                # ── NO-side shadow queue (all markets that reach edge computation) ──
                # NO ask from market NBBO
                _no_ask_eq_raw = mkt.get("no_ask_dollars") or mkt.get("no_ask")
                _no_ask_eq = (dollars_str_to_cents(_no_ask_eq_raw) if isinstance(_no_ask_eq_raw, str)
                              else int(_no_ask_eq_raw)) if _no_ask_eq_raw is not None else None
                if _no_ask_eq is not None and _no_ask_eq <= 0:
                    _no_ask_eq = None
                if _no_ask_eq is not None:
                    _no_side_queue.append({
                        "ticker": ticker,
                        "event_ticker": window["event_ticker"],
                        "asset": asset,
                        "best_ask": best_ask,
                        "no_ask": _no_ask_eq,
                        "spot": spot,
                        "threshold": threshold,
                        "blended_rv": blended_rv,
                        "seconds_remaining": seconds_remaining,
                        "vol_regime": vol_est["regime"],
                        "ask_depth": ask_depth,
                        "best_ask_source": best_ask_source,
                        "product_type": window.get("product_type"),
                        "_shadow_diag": _shadow_diag.copy(),
                        "_oft_db": _oft_db.copy(),
                        "_shadow_extra": _shadow_extra.copy(),
                        "final_prob": final_prob,  # already computed (temp+blend+cap)
                        "cal_prob": None,  # not needed — final_prob available
                        "raw_prob": raw_prob,
                        "calibration_method": calibration_method,
                        "hourly_pre_temp_prob": _hourly_pre_temp_prob,
                        "hourly_applied_temp_t": _configured_temp_t,
                        "hourly_post_temp_prob": _hourly_post_temp_prob,
                    })

                # ── Augment _shadow_diag with Kalshi OFT fields ──
                if ofa_signals:
                    # Kalshi-specific signals are nested under signals.kalshi_orderbook
                    _koft = ofa_signals.get("signals", {}).get("kalshi_orderbook", {})
                    _shadow_extra["oft_imbalance_ratio"] = _koft.get("imbalance_ratio")
                    _shadow_extra["oft_imbalance_level"] = _koft.get("imbalance_level")
                    _shadow_extra["oft_prob_adjustment"] = _koft.get("prob_adjustment")
                    _shadow_extra["oft_confidence"] = _koft.get("confidence")
                    _shadow_extra["oft_n_snapshots"] = _koft.get("n_snapshots")
                    _shadow_extra["oft_depth_velocity"] = _koft.get("depth_velocity")
                    # OFT fields for DB persistence
                    _oft_db = {
                        "oft_prob_adjustment": _koft.get("prob_adjustment"),
                        "oft_imbalance_ratio": _koft.get("imbalance_ratio"),
                        "oft_n_snapshots": _koft.get("n_snapshots"),
                    }
                    _shadow_extra["oft_ask_velocity"] = _koft.get("ask_velocity")

                # ── Counterfactual analysis: what would each shadow feature produce? ──
                _cf = {}

                # CF1: EGARCH blend ↔ RV-only counterfactual (bidirectional)
                if EGARCH_BLEND_SHADOW_MODE:
                    # Shadow: EGARCH blend not live, show what it would do
                    _cf_ebs = _shadow_diag.get("egarch_blend_sigma")
                    if _cf_ebs and _cf_ebs > 0:
                        _cf_prob = ProbabilityEngine.counterfactual_prob(
                            spot, threshold, seconds_remaining, _cf_ebs, asset, product_type=_pt)
                        if _cf_prob is not None:
                            _cf_edge = _cf_prob - best_ask / 100.0
                            _cf_fee_edge = _cf_edge - est_fee_1c / 100.0
                            _cf["egarch_blend"] = {
                                "prob": _cf_prob, "edge": round(_cf_edge, 6),
                                "fee_edge": round(_cf_fee_edge, 6),
                                "would_trade": _cf_fee_edge >= MIN_EDGE_PCT / 100.0,
                            }
                else:
                    # Promoted: EGARCH blend IS live, show what RV-only would do
                    _cf_rvo = vol_est.get("rv_only_blended")
                    if _cf_rvo and _cf_rvo > 0:
                        _cf_prob = ProbabilityEngine.counterfactual_prob(
                            spot, threshold, seconds_remaining, _cf_rvo, asset, product_type=_pt)
                        if _cf_prob is not None:
                            _cf_edge = _cf_prob - best_ask / 100.0
                            _cf_fee_edge = _cf_edge - est_fee_1c / 100.0
                            _cf["rv_only"] = {
                                "prob": _cf_prob, "edge": round(_cf_edge, 6),
                                "fee_edge": round(_cf_fee_edge, 6),
                                "would_trade": _cf_fee_edge >= MIN_EDGE_PCT / 100.0,
                            }

                # CF2: TV-RK ↔ fixed-RK counterfactual (bidirectional)
                _cf_tv = _shadow_diag.get("shadow_tv_blend_rv")
                if _cf_tv and _cf_tv > 0:
                    _cf_prob = ProbabilityEngine.counterfactual_prob(
                        spot, threshold, seconds_remaining, _cf_tv, asset, product_type=_pt)
                    if _cf_prob is not None:
                        _cf_edge = _cf_prob - best_ask / 100.0
                        _cf_fee_edge = _cf_edge - est_fee_1c / 100.0
                        _cf_key = "tv_rk" if RK_TV_SHADOW_MODE else "fixed_rk"
                        _cf[_cf_key] = {
                            "prob": _cf_prob, "edge": round(_cf_edge, 6),
                            "fee_edge": round(_cf_fee_edge, 6),
                            "would_trade": _cf_fee_edge >= MIN_EDGE_PCT / 100.0,
                        }

                # CF3: Sigmoid QLIKE blend as primary
                _cf_sig = _shadow_extra.get("mz_sigmoid_blend_rv")
                if _cf_sig and _cf_sig > 0:
                    _cf_prob = ProbabilityEngine.counterfactual_prob(
                        spot, threshold, seconds_remaining, _cf_sig, asset, product_type=_pt)
                    if _cf_prob is not None:
                        _cf_edge = _cf_prob - best_ask / 100.0
                        _cf_fee_edge = _cf_edge - est_fee_1c / 100.0
                        _cf["sigmoid_qlike"] = {
                            "prob": _cf_prob, "edge": round(_cf_edge, 6),
                            "fee_edge": round(_cf_fee_edge, 6),
                            "would_trade": _cf_fee_edge >= MIN_EDGE_PCT / 100.0,
                        }

                # CF4: Kalshi OFT adjusted prob
                if ofa_signals and KALSHI_OFT_SHADOW_MODE:
                    _koft_cf = ofa_signals.get("signals", {}).get("kalshi_orderbook", {})
                    _koft_adj = _koft_cf.get("prob_adjustment", 0)
                    if _koft_adj != 0:
                        _cf["kalshi_oft"] = {
                            "prob_adjustment": round(_koft_adj, 6),
                            "adj_prob": round(final_prob + _koft_adj, 6),
                            "imbalance_ratio": _koft_cf.get("imbalance_ratio"),
                            "imbalance_level": _koft_cf.get("imbalance_level"),
                            "depth_velocity": _koft_cf.get("depth_velocity"),
                            "ask_velocity": _koft_cf.get("ask_velocity"),
                            "confidence": _koft_cf.get("confidence"),
                            "n_snapshots": _koft_cf.get("n_snapshots"),
                        }

                # CF5: Old system counterfactual (Beta Cal + 50% market blend)
                if not SHADOW_CAL_PIPELINE and _CALIBRATION_ENGINE is not None and _CALIBRATION_ENGINE._beta_trained:
                    try:
                        _cf5_cal = _CALIBRATION_ENGINE._beta_cal_predict(raw_prob)
                        _cf5_cal = min(_cf5_cal, _dyn_cap)
                        _cf5_cal = _CALIBRATION_ENGINE._apply_uncertainty_shrinkage(_cf5_cal)
                        _cf5_cal = max(0.001, min(NUMERICAL_SAFETY_CEILING, _cf5_cal))
                        _cf5_cal = max(0.01, min(NUMERICAL_SAFETY_CEILING, _cf5_cal + ofa_adjustment))
                        if best_ask < ENDGAME_BLEND_PRICE:
                            _cf5_cal = 0.50 * _cf5_cal + 0.50 * (best_ask / 100.0)
                        _cf5_edge = _cf5_cal - best_ask / 100.0
                        _cf5_fee_edge = _cf5_edge - est_fee_1c / 100.0
                        _cf["old_cal_system"] = {
                            "prob": round(_cf5_cal, 6),
                            "edge": round(_cf5_edge, 6),
                            "fee_edge": round(_cf5_fee_edge, 6),
                            "would_trade": _cf5_fee_edge >= MIN_EDGE_PCT / 100.0,
                        }
                    except Exception:
                        pass
                elif SHADOW_CAL_PIPELINE and _CALIBRATION_ENGINE is not None:
                    _cf_cal = _CALIBRATION_ENGINE.shadow_calibration_pipeline(
                        raw_prob, best_ask, seconds_remaining, ofa_adjustment)
                    if _cf_cal is not None:
                        _cf["cal_pipeline"] = _cf_cal

                _cf_json = json.dumps(_cf) if _cf else None

                # Log divergences: cases where a shadow feature disagrees with production
                _live_would_trade = (fee_adjusted_edge >= MIN_EDGE_PCT / 100.0)
                for _cf_name, _cf_data in _cf.items():
                    _shadow_would = _cf_data.get("would_trade")
                    if _shadow_would is not None and _shadow_would != _live_would_trade:
                        logging.info(
                            "COUNTERFACTUAL DIVERGENCE %s %s: live=%s shadow=%s "
                            "live_edge=%.4f shadow_edge=%.4f",
                            ticker, _cf_name, _live_would_trade, _shadow_would,
                            fee_adjusted_edge, _cf_data.get("fee_edge", 0),
                        )

                # Log shadow calibration pipeline summary every 5 minutes per asset
                if SHADOW_CAL_PIPELINE and "cal_pipeline" in _cf:
                    try:
                        _cp = _cf["cal_pipeline"]
                        if now - self._shadow_cal_last_log.get(asset, 0) >= 300:
                            logging.info(
                                "shadow_cal_pipeline %s: prob=%.4f edge=%.4f fee_edge=%.4f "
                                "would_trade=%s temp=%.3f temp_brier=%.4f "
                                "prod_prob=%.4f prod_edge=%.4f blend_w=%.2f",
                                ticker, _cp["prob"], _cp["edge"], _cp["fee_edge"],
                                _cp["would_trade"], _cp.get("temperature") or 0,
                                _cp.get("temperature_brier") or 0,
                                final_prob, fee_adjusted_edge, MARKET_BLEND_W)
                            self._shadow_cal_last_log[asset] = now
                    except Exception:
                        pass

                # Filter: fee-adjusted edge must meet price-dependent minimum
                if _pt == "weather":
                    _min_edge = WEATHER_MIN_EDGE_PCT
                elif _pt == "hourly":
                    _min_edge = HOURLY_MIN_EDGE_PCT
                else:
                    _min_edge = get_min_edge(best_ask)
                if fee_adjusted_edge < _min_edge:
                    scan_stats[asset]["insufficient_edge"] += 1
                    self._recent_opportunities.append({
                        "ticker": ticker, "asset": asset,
                        "seconds_to_close": round(seconds_remaining, 1),
                        "best_ask": best_ask,
                        "edge_bps": round(fee_adjusted_edge * 10000),
                        "gross_edge_bps": round(edge * 10000),
                        "chosen_strategy": None,
                        "rejection_reason": "insufficient_edge",
                        "ts": datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                    })
                    try:
                        self._logger.log_opportunity({
                            "filter_stage": "insufficient_edge",
                            "ticker": ticker,
                            "event_ticker": window["event_ticker"],
                            "asset": asset,
                            "rejection_reason": f"net_edge {fee_adjusted_edge:.4f} < min {_min_edge:.4f} @{best_ask}c (gross {edge:.4f}, fee {est_fee_1c}c)",
                            "spot_price": spot,
                            "threshold": threshold,
                            "volatility": blended_rv,
                            "market_price": best_ask,
                            "best_ask_source": best_ask_source,
                            "seconds_to_close": round(seconds_remaining, 1),
                            "calibrated_prob": round(final_prob, 6),
                            "edge": round(edge, 6),
                            "fee_adjusted_edge": round(fee_adjusted_edge, 6),
                            "ofa_adjustment": round(ofa_adjustment, 6),
                            "raw_prob": round(raw_prob, 6) if raw_prob is not None else None,
                            "old_system_prob": round(_old_system_prob, 6),
                            "kalshi_oft": (ofa_signals or {}).get("signals", {}).get("kalshi_orderbook", {}),
                            "counterfactual": _cf,
                            **_shadow_diag,
                            **_shadow_extra,
                        })
                        _dedup_key = (ticker, "insufficient_edge")
                        if _dedup_key not in self._eval_opp_seen:
                            self._eval_opp_seen.add(_dedup_key)
                            _ev = (final_prob * (100 - best_ask)) - ((1 - final_prob) * best_ask) - est_fee_1c
                            # Compute sizing for instrumentation (pure math, no side effects)
                            _ie_kelly_f = None
                            _ie_position = None
                            _ie_drawdown = None
                            _ie_balance = self._get_balance_cached()
                            if _ie_balance and _ie_balance > 0:
                                _ie_sizing = self._sizer.compute(final_prob, best_ask, _ie_balance)
                                _ie_kelly_f = _ie_sizing["kelly_f"]
                                _ie_position = _ie_sizing["contracts"]
                                _ie_drawdown = _ie_sizing["drawdown_scaler"]
                                # Apply product-type Kelly fraction + risk cap
                                _ie_scfg = get_market_config(window.get("product_type"))
                                if _ie_scfg.kelly_fraction < 1.0:
                                    _ie_position = max(1, int(_ie_position * _ie_scfg.kelly_fraction))
                                _ie_type_max = int((_ie_balance * _ie_scfg.max_risk_per_trade) / best_ask)
                                if _ie_position > _ie_type_max:
                                    _ie_position = max(1, _ie_type_max)
                            # Compute strategy for instrumentation (pure computation, no side effects)
                            _ie_strategy = None
                            try:
                                _ie_scfg2 = get_market_config(window.get("product_type"))
                                _ie_strat_data = {
                                    "z_score": z_score,
                                    "calibrated_prob": final_prob,
                                    "spot": spot, "threshold": threshold,
                                    "seconds_to_close": seconds_remaining,
                                    "blended_rv": blended_rv,
                                    "vol_regime": vol_est["regime"],
                                    "best_yes_ask": best_ask,
                                    "best_ask_depth": ask_depth,
                                    "total_ob_depth": total_depth,
                                    "convergence_velocity": self._scanner_convergence_velocity(ticker),
                                    "edge": edge,
                                    "min_entry_price": _ie_scfg2.min_entry_price,
                                    "max_entry_price": _ie_scfg2.max_entry_price,
                                }
                                _ie_strategy, _ = evaluate_execution_strategy(_ie_strat_data)
                            except Exception:
                                logging.debug("IE strategy computation failed", exc_info=True)
                            self._state.insert_evaluated_opportunity(
                                ticker, window["event_ticker"], asset,
                                "insufficient_edge",
                                rejection_reason=f"net_edge {fee_adjusted_edge:.4f} < min",
                                spot_price=spot, threshold=threshold,
                                volatility=blended_rv, market_price=best_ask,
                                seconds_to_close=seconds_remaining,
                                calibrated_prob=final_prob, edge=edge,
                                ofa_adjustment=ofa_adjustment,
                                strategy=_ie_strategy,
                                z_score=z_score,
                                vol_regime=vol_est["regime"],
                                calibrated_prob_raw=calibrated_prob_raw,
                                kelly_f=_ie_kelly_f,
                                position_size=_ie_position,
                                drawdown_scaler=_ie_drawdown,
                                breakeven_wr=best_ask / 100.0,
                                expected_value=round(_ev, 2),
                                ask_depth=ask_depth,
                                best_ask_source=best_ask_source,
                                ofa_confidence=ofa_signals["confidence"] if ofa_signals else "none",
                                raw_prob=raw_prob,
                                calibration_method=calibration_method,
                                old_system_prob=_old_system_prob,
                                fee_adjusted_edge=fee_adjusted_edge,
                                counterfactual=_cf_json,
                                shadow_cal_prob=(_cf.get("old_cal_system") or _cf.get("cal_pipeline", {})).get("prob") if _cf else None,
                                shadow_cal_fee_edge=(_cf.get("old_cal_system") or _cf.get("cal_pipeline", {})).get("fee_edge") if _cf else None,
                                shadow_cal_temperature=(_cf.get("old_cal_system") or _cf.get("cal_pipeline", {})).get("temperature") if _cf else None,
                                product_type=window.get("product_type"),
                                wx_ensemble_mean=_shadow_extra.get("wx_ensemble_mean"),
                                wx_ensemble_std=_shadow_extra.get("wx_ensemble_std"),
                                wx_bias_correction=_shadow_extra.get("wx_bias_correction"),
                                wx_n_members=_shadow_extra.get("wx_n_members"),
                                wx_market_type=_shadow_extra.get("wx_market_type"),
                                wx_no_side_edge=_shadow_extra.get("wx_no_side_edge"),
                                wx_hrrr_temp=_shadow_extra.get("wx_hrrr_temp"),
                                wx_corrected_mean=_shadow_extra.get("wx_corrected_mean"),
                                hourly_pre_temp_prob=_hourly_pre_temp_prob,
                                hourly_applied_temp_t=_configured_temp_t,
                                hourly_shadow_temp_2_0=_hourly_shadow_temp_2_0,
                                hourly_shadow_temp_1_0=_hourly_shadow_temp_1_0,
                                hourly_shadow_temp_2_5=_hourly_shadow_temp_2_5,
                                hourly_shadow_blend_50=_hourly_shadow_blend_50,
                                hourly_shadow_temp_1_75=_hourly_shadow_temp_1_75,
                                hourly_shadow_temp_3_0=_hourly_shadow_temp_3_0,
                                hourly_shadow_blend_20=_hourly_shadow_blend_20,
                                hourly_shadow_blend_30=_hourly_shadow_blend_30,
                                hourly_shadow_blend_60=_hourly_shadow_blend_60,
                                hourly_post_temp_prob=_hourly_post_temp_prob,
                                **_oft_db, **_shadow_diag)
                    except Exception:
                        logging.warning("insert_evaluated_opportunity failed (hourly_observation)", exc_info=True)
                    # V2 variant: shadow cal pipeline (temperature + no blend)
                    if _pt == "hourly" and _cf:
                        self._insert_hourly_v2_variant(
                            ticker, window, asset, raw_prob, best_ask,
                            seconds_remaining, spot, threshold, blended_rv,
                            ofa_adjustment, z_score, vol_est,
                            calibrated_prob_raw, est_fee_1c,
                            ask_depth, best_ask_source, _cf, _shadow_diag)

                    # ── Weekend Edge Discount Shadow ──────────────────────────
                    # On Sat/Sun, re-evaluate 15M insufficient_edge rejections
                    # at relaxed thresholds (0.6x). Shadow-only — no orders.
                    if (_pt in (None, "15m")
                            and datetime.datetime.now(timezone.utc).weekday() >= 5
                            and best_ask >= MIN_ENTRY_PRICE):
                        _wknd_discounted_min = _min_edge * WEEKEND_EDGE_DISCOUNT
                        if fee_adjusted_edge >= _wknd_discounted_min:
                            # Would pass at discounted threshold — log as shadow candidate
                            _wknd_balance = self._get_balance_cached()
                            _wknd_kelly_f = None
                            _wknd_position = None
                            _wknd_ev = None
                            if _wknd_balance and _wknd_balance > 0:
                                _wknd_sizing = self._sizer.compute(final_prob, best_ask, _wknd_balance)
                                _wknd_kelly_f = _wknd_sizing["kelly_f"]
                                _wknd_position = _wknd_sizing["contracts"]
                                # Apply product-type Kelly fraction + risk cap
                                _wknd_scfg = get_market_config(window.get("product_type"))
                                if _wknd_scfg.kelly_fraction < 1.0:
                                    _wknd_position = max(1, int(_wknd_position * _wknd_scfg.kelly_fraction))
                                _wknd_type_max = int((_wknd_balance * _wknd_scfg.max_risk_per_trade) / best_ask)
                                if _wknd_position > _wknd_type_max:
                                    _wknd_position = max(1, _wknd_type_max)
                                _wknd_ev = round((final_prob * (100 - best_ask)) - ((1 - final_prob) * best_ask) - est_fee_1c, 2)
                            try:
                                self._logger.log_opportunity({
                                    "filter_stage": "weekend_discount_shadow",
                                    "ticker": ticker,
                                    "event_ticker": window["event_ticker"],
                                    "asset": asset,
                                    "side": "yes",
                                    "market_price": best_ask,
                                    "model_prob": round(final_prob, 6),
                                    "edge": round(edge, 6),
                                    "fee_adjusted_edge": round(fee_adjusted_edge, 6),
                                    "kelly_f": round(_wknd_kelly_f, 6) if _wknd_kelly_f else None,
                                    "position_size": _wknd_position,
                                    "expected_value": _wknd_ev,
                                    "seconds_to_close": round(seconds_remaining, 1),
                                    "spot_price": spot,
                                    "threshold": threshold,
                                    "volatility": blended_rv,
                                    "vol_regime": vol_est["regime"],
                                    "discount_factor": WEEKEND_EDGE_DISCOUNT,
                                    "original_min_edge": round(_min_edge, 6),
                                    "discounted_min_edge": round(_wknd_discounted_min, 6),
                                    "edge_vs_discounted": round(fee_adjusted_edge - _wknd_discounted_min, 6),
                                    "raw_prob": round(raw_prob, 6) if raw_prob is not None else None,
                                    "ofa_adjustment": round(ofa_adjustment, 6),
                                    "z_score": z_score,
                                })
                            except Exception:
                                logging.debug("weekend_discount_shadow log failed", exc_info=True)
                            # Also insert into evaluated_opportunities for settlement tracking
                            _wknd_dedup = (ticker, "weekend_discount_shadow")
                            if _wknd_dedup not in self._eval_opp_seen:
                                self._eval_opp_seen.add(_wknd_dedup)
                                try:
                                    self._state.insert_evaluated_opportunity(
                                        ticker, window["event_ticker"], asset,
                                        "weekend_discount_shadow",
                                        rejection_reason=f"shadow: edge {fee_adjusted_edge:.4f} >= discounted_min {_wknd_discounted_min:.4f} (orig {_min_edge:.4f} x {WEEKEND_EDGE_DISCOUNT})",
                                        spot_price=spot, threshold=threshold,
                                        volatility=blended_rv, market_price=best_ask,
                                        seconds_to_close=seconds_remaining,
                                        calibrated_prob=final_prob, edge=edge,
                                        ofa_adjustment=ofa_adjustment,
                                        z_score=z_score,
                                        vol_regime=vol_est["regime"],
                                        calibrated_prob_raw=calibrated_prob_raw,
                                        kelly_f=_wknd_kelly_f,
                                        position_size=_wknd_position,
                                        breakeven_wr=best_ask / 100.0,
                                        expected_value=_wknd_ev,
                                        ask_depth=ask_depth,
                                        best_ask_source=best_ask_source,
                                        raw_prob=raw_prob,
                                        calibration_method=calibration_method,
                                        fee_adjusted_edge=fee_adjusted_edge,
                                        product_type=window.get("product_type"),
                                        **_shadow_diag)
                                except Exception:
                                    logging.warning("insert_evaluated_opportunity failed (weekend_discount_shadow)", exc_info=True)

                    # ── Overnight Edge Discount Shadow ─────────────────────────
                    # On weekday quiet hours (04-11 UTC), re-evaluate 15M
                    # insufficient_edge at relaxed thresholds (0.6x). Shadow-only.
                    # Skip if weekend discount already applied (don't double-count).
                    _now_utc = datetime.datetime.now(timezone.utc)
                    _is_weekend = _now_utc.weekday() >= 5
                    if (_pt in (None, "15m")
                            and not _is_weekend
                            and OVERNIGHT_QUIET_START <= _now_utc.hour <= OVERNIGHT_QUIET_END
                            and best_ask >= MIN_ENTRY_PRICE):
                        _ovn_discounted_min = _min_edge * OVERNIGHT_EDGE_DISCOUNT
                        if fee_adjusted_edge >= _ovn_discounted_min:
                            _ovn_balance = self._get_balance_cached()
                            _ovn_kelly_f = None
                            _ovn_position = None
                            _ovn_ev = None
                            if _ovn_balance and _ovn_balance > 0:
                                _ovn_sizing = self._sizer.compute(final_prob, best_ask, _ovn_balance)
                                _ovn_kelly_f = _ovn_sizing["kelly_f"]
                                _ovn_position = _ovn_sizing["contracts"]
                                _ovn_scfg = get_market_config(window.get("product_type"))
                                if _ovn_scfg.kelly_fraction < 1.0:
                                    _ovn_position = max(1, int(_ovn_position * _ovn_scfg.kelly_fraction))
                                _ovn_type_max = int((_ovn_balance * _ovn_scfg.max_risk_per_trade) / best_ask)
                                if _ovn_position > _ovn_type_max:
                                    _ovn_position = max(1, _ovn_type_max)
                                _ovn_ev = round((final_prob * (100 - best_ask)) - ((1 - final_prob) * best_ask) - est_fee_1c, 2)
                            try:
                                self._logger.log_opportunity({
                                    "filter_stage": "overnight_discount_shadow",
                                    "ticker": ticker,
                                    "event_ticker": window["event_ticker"],
                                    "asset": asset,
                                    "side": "yes",
                                    "market_price": best_ask,
                                    "model_prob": round(final_prob, 6),
                                    "edge": round(edge, 6),
                                    "fee_adjusted_edge": round(fee_adjusted_edge, 6),
                                    "kelly_f": round(_ovn_kelly_f, 6) if _ovn_kelly_f else None,
                                    "position_size": _ovn_position,
                                    "expected_value": _ovn_ev,
                                    "seconds_to_close": round(seconds_remaining, 1),
                                    "spot_price": spot,
                                    "threshold": threshold,
                                    "volatility": blended_rv,
                                    "vol_regime": vol_est["regime"],
                                    "discount_factor": OVERNIGHT_EDGE_DISCOUNT,
                                    "original_min_edge": round(_min_edge, 6),
                                    "discounted_min_edge": round(_ovn_discounted_min, 6),
                                    "edge_vs_discounted": round(fee_adjusted_edge - _ovn_discounted_min, 6),
                                    "raw_prob": round(raw_prob, 6) if raw_prob is not None else None,
                                    "ofa_adjustment": round(ofa_adjustment, 6),
                                    "z_score": z_score,
                                })
                            except Exception:
                                logging.debug("overnight_discount_shadow log failed", exc_info=True)
                            _ovn_dedup = (ticker, "overnight_discount_shadow")
                            if _ovn_dedup not in self._eval_opp_seen:
                                self._eval_opp_seen.add(_ovn_dedup)
                                try:
                                    self._state.insert_evaluated_opportunity(
                                        ticker, window["event_ticker"], asset,
                                        "overnight_discount_shadow",
                                        rejection_reason=f"shadow: edge {fee_adjusted_edge:.4f} >= discounted_min {_ovn_discounted_min:.4f} (orig {_min_edge:.4f} x {OVERNIGHT_EDGE_DISCOUNT})",
                                        spot_price=spot, threshold=threshold,
                                        volatility=blended_rv, market_price=best_ask,
                                        seconds_to_close=seconds_remaining,
                                        calibrated_prob=final_prob, edge=edge,
                                        ofa_adjustment=ofa_adjustment,
                                        z_score=z_score,
                                        vol_regime=vol_est["regime"],
                                        calibrated_prob_raw=calibrated_prob_raw,
                                        kelly_f=_ovn_kelly_f,
                                        position_size=_ovn_position,
                                        breakeven_wr=best_ask / 100.0,
                                        expected_value=_ovn_ev,
                                        ask_depth=ask_depth,
                                        best_ask_source=best_ask_source,
                                        raw_prob=raw_prob,
                                        calibration_method=calibration_method,
                                        fee_adjusted_edge=fee_adjusted_edge,
                                        product_type=window.get("product_type"),
                                        **_shadow_diag)
                                except Exception:
                                    logging.warning("insert_evaluated_opportunity failed (overnight_discount_shadow)", exc_info=True)

                    # ── Decided Contract (overlay strategy + shadow) ─────────
                    # When z-score is very negative (spot far above strike) near expiry,
                    # the contract is essentially decided. EGARCH can't compute edge
                    # because calibration squashes prob below market price.
                    # T1 (z ≤ -5, 93-99c): 34/34 = 100% WR. T2 (z ≤ -3, 93-96c): 8/8 = 100% WR.
                    # Shadow always runs. Live overlay fires when DECIDED_T1/T2_ENABLED.
                    if (DECIDED_CONTRACT_SHADOW
                            and _pt in (None, "15m")
                            and z_score is not None
                            and best_ask >= DECIDED_CONTRACT_MIN_PRICE
                            and best_ask <= MAX_ENTRY_PRICE
                            and seconds_remaining < DECIDED_CONTRACT_MAX_STC):
                        _dc_tier = None
                        if z_score <= DECIDED_CONTRACT_Z_T1:
                            _dc_tier = "decided_contract_t1"
                        elif (z_score <= DECIDED_CONTRACT_Z_T2
                              and best_ask <= DECIDED_CONTRACT_T2_MAX_PRICE):
                            _dc_tier = "decided_contract_t2"

                        if _dc_tier:
                            # Fixed sizing: 12.5% risk (not Kelly — model edge is negative)
                            _dc_balance = self._get_balance_cached()
                            _dc_position = None
                            _dc_kelly_f = None
                            _dc_ev = None
                            if _dc_balance and _dc_balance > 0:
                                _dc_risk = DECIDED_CONTRACT_RISK
                                _dc_position = max(1, int((_dc_balance * _dc_risk) / best_ask))
                                # EV with assumed ~99% win prob for T1, ~96% for T2
                                _dc_assumed_p = 0.99 if _dc_tier == "decided_contract_t1" else 0.96
                                _dc_ev = round((_dc_assumed_p * (100 - best_ask))
                                               - ((1 - _dc_assumed_p) * best_ask) - est_fee_1c, 2)
                                _dc_kelly_f = round((_dc_assumed_p - best_ask / 100.0), 6)

                            # Always log shadow signal (continues accumulating shadow stats)
                            _dc_dedup = (ticker, _dc_tier)
                            if _dc_dedup not in self._eval_opp_seen:
                                self._eval_opp_seen.add(_dc_dedup)
                                try:
                                    self._state.insert_evaluated_opportunity(
                                        ticker, window["event_ticker"], asset,
                                        _dc_tier,
                                        rejection_reason=f"shadow: z={z_score:.1f} stc={seconds_remaining:.0f}s price={best_ask}c",
                                        spot_price=spot, threshold=threshold,
                                        volatility=blended_rv, market_price=best_ask,
                                        seconds_to_close=seconds_remaining,
                                        calibrated_prob=final_prob, edge=edge,
                                        ofa_adjustment=ofa_adjustment,
                                        z_score=z_score,
                                        vol_regime=vol_est["regime"],
                                        calibrated_prob_raw=calibrated_prob_raw,
                                        kelly_f=_dc_kelly_f,
                                        position_size=_dc_position,
                                        breakeven_wr=best_ask / 100.0,
                                        expected_value=_dc_ev,
                                        ask_depth=ask_depth,
                                        best_ask_source=best_ask_source,
                                        raw_prob=raw_prob,
                                        calibration_method=calibration_method,
                                        fee_adjusted_edge=fee_adjusted_edge,
                                        product_type=window.get("product_type"),
                                        **_shadow_diag)
                                except Exception:
                                    logging.warning("insert_evaluated_opportunity failed (%s)", _dc_tier, exc_info=True)

                            # ── Live overlay: queue as candidate if tier enabled ──
                            _dc_live_enabled = (
                                (_dc_tier == "decided_contract_t1" and DECIDED_T1_ENABLED)
                                or (_dc_tier == "decided_contract_t2" and DECIDED_T2_ENABLED))
                            if (_dc_live_enabled
                                    and not OBSERVATION_MODE
                                    and _dc_balance and _dc_balance > 0
                                    and _dc_position and _dc_position > 0):
                                # Per-window risk cap: 25% bankroll across all DC signals
                                _dc_wkey = window["event_ticker"]
                                _dc_existing_risk = self._dc_window_risk.get(_dc_wkey, 0.0)
                                _dc_this_cost = _dc_position * best_ask
                                _dc_max_cost = _dc_balance * DECIDED_CONTRACT_MAX_WINDOW_RISK
                                if _dc_existing_risk + _dc_this_cost > _dc_max_cost:
                                    # Reduce position to fit within cap
                                    _dc_remaining = _dc_max_cost - _dc_existing_risk
                                    if _dc_remaining >= best_ask:
                                        _dc_position = max(1, int(_dc_remaining / best_ask))
                                        _dc_this_cost = _dc_position * best_ask
                                    else:
                                        # Window cap exceeded — log skip and don't trade
                                        _dc_skip_dedup = (ticker, "decided_window_cap_skip")
                                        if _dc_skip_dedup not in self._eval_opp_seen:
                                            self._eval_opp_seen.add(_dc_skip_dedup)
                                            try:
                                                self._state.insert_evaluated_opportunity(
                                                    ticker, window["event_ticker"], asset,
                                                    "decided_window_cap_skip",
                                                    rejection_reason=f"window cap: existing={_dc_existing_risk:.0f}c max={_dc_max_cost:.0f}c tier={_dc_tier}",
                                                    spot_price=spot, threshold=threshold,
                                                    volatility=blended_rv, market_price=best_ask,
                                                    seconds_to_close=seconds_remaining,
                                                    calibrated_prob=final_prob, edge=edge,
                                                    z_score=z_score,
                                                    vol_regime=vol_est["regime"],
                                                    raw_prob=raw_prob,
                                                    fee_adjusted_edge=fee_adjusted_edge,
                                                    product_type=window.get("product_type"),
                                                    **_shadow_diag)
                                            except Exception:
                                                logging.warning("insert_evaluated_opportunity failed (decided_window_cap_skip)", exc_info=True)
                                        self._dc_window_cap_skips += 1
                                        logging.info("DC_WINDOW_CAP: %s %s skipped (existing=%.0fc max=%.0fc)",
                                                     _dc_tier, ticker, _dc_existing_risk, _dc_max_cost)
                                        _dc_live_enabled = False  # skip candidate below

                                # Cap by existing exposure on same ticker
                                if _dc_live_enabled:
                                    _dc_existing_exposure = 0
                                    for pos in self._state.get_open_positions():
                                        if pos["ticker"] == ticker:
                                            _dc_existing_exposure += pos["count"]
                                            break
                                    for resting in self._state.get_resting_orders(ticker=ticker):
                                        _dc_existing_exposure += resting["count"]
                                    if _dc_existing_exposure > 0:
                                        _dc_position = max(0, _dc_position - _dc_existing_exposure)

                                if _dc_live_enabled and _dc_position > 0:
                                    _dc_strat = "decided_t1" if _dc_tier == "decided_contract_t1" else "decided_t2"
                                    self._dc_window_risk[_dc_wkey] = _dc_existing_risk + _dc_position * best_ask
                                    logging.info("DC_CANDIDATE: %s %s %dx@%dc z=%.1f stc=%.0fs",
                                                 _dc_strat, ticker, _dc_position, best_ask, z_score, seconds_remaining)
                                    candidates.append({
                                        "ticker": ticker,
                                        "event_ticker": window["event_ticker"],
                                        "asset": asset,
                                        "product_type": window.get("product_type"),
                                        "spot": spot,
                                        "threshold": threshold,
                                        "seconds_to_close": round(seconds_remaining, 1),
                                        "blended_rv": blended_rv,
                                        "calibrated_prob": round(_dc_assumed_p, 6),
                                        "z_score": z_score,
                                        "best_yes_ask": best_ask,
                                        "best_ask_source": best_ask_source,
                                        "edge": round(_dc_assumed_p - best_ask / 100.0, 6),
                                        "position_size": _dc_position,
                                        "kelly_f": _dc_kelly_f,
                                        "drawdown_scaler": 1.0,
                                        "vol_regime": vol_est["regime"],
                                        "balance_at_scan": _dc_balance,
                                        "strategy": _dc_strat,
                                        "strategy_scores": {"certainty": 1.0, "certainty_detail": "decided",
                                                            "orderbook": 0.5, "orderbook_detail": "n/a",
                                                            "urgency": 1.0, "urgency_detail": "decided",
                                                            "composite": 1.0, "reason": "decided_contract"},
                                        "ob_snapshot": {
                                            "best_ask": best_ask,
                                            "ask_depth": ask_depth,
                                            "total_depth": total_depth,
                                            "best_bid": OrderExecutor._best_yes_bid(ob_data) if ob_data else None,
                                            "bid_depth": OrderExecutor._best_yes_bid_depth(ob_data) if ob_data else 0,
                                            "spread": (best_ask - OrderExecutor._best_yes_bid(ob_data))
                                                      if ob_data and OrderExecutor._best_yes_bid(ob_data) is not None else None,
                                        },
                                        "calibrated_prob_raw": round(calibrated_prob_raw, 6),
                                        "ofa_adjustment": round(ofa_adjustment, 6),
                                        "ofa_confidence": ofa_signals["confidence"] if ofa_signals else "none",
                                        "raw_prob": raw_prob,
                                        "calibration_method": calibration_method,
                                        "old_system_prob": round(_old_system_prob, 6),
                                        "fee_adjusted_edge": round(fee_adjusted_edge, 6),
                                        **_shadow_diag,
                                        **_shadow_extra,
                                    })

                    # ── Relaxed Edge Shadow (Fix #1) ──────────────────────────
                    # Edge thresholds at 88-93c may be too conservative.
                    # Data: insufficient_edge rejections at 88c=97.2% WR, 89c=93.3%,
                    # 91c=94.4% — all well above breakeven. Shadow with halved thresholds.
                    if (RELAXED_EDGE_SHADOW
                            and _pt in (None, "15m")
                            and best_ask >= RELAXED_EDGE_MIN_PRICE
                            and best_ask < RELAXED_EDGE_MAX_PRICE):
                        _rel_min_edge = _min_edge * RELAXED_EDGE_DISCOUNT
                        if fee_adjusted_edge >= _rel_min_edge:
                            _rel_balance = self._get_balance_cached()
                            _rel_position = None
                            _rel_kelly_f = None
                            _rel_ev = None
                            if _rel_balance and _rel_balance > 0:
                                _rel_sizing = self._sizer.compute(final_prob, best_ask, _rel_balance)
                                _rel_kelly_f = _rel_sizing["kelly_f"]
                                _rel_position = _rel_sizing["contracts"]
                                _rel_scfg = get_market_config(window.get("product_type"))
                                if _rel_scfg.kelly_fraction < 1.0:
                                    _rel_position = max(1, int(_rel_position * _rel_scfg.kelly_fraction))
                                _rel_type_max = int((_rel_balance * _rel_scfg.max_risk_per_trade) / best_ask)
                                if _rel_position > _rel_type_max:
                                    _rel_position = max(1, _rel_type_max)
                                _rel_ev = round((final_prob * (100 - best_ask))
                                                - ((1 - final_prob) * best_ask) - est_fee_1c, 2)

                            _rel_dedup = (ticker, "relaxed_edge_shadow")
                            if _rel_dedup not in self._eval_opp_seen:
                                self._eval_opp_seen.add(_rel_dedup)
                                try:
                                    self._state.insert_evaluated_opportunity(
                                        ticker, window["event_ticker"], asset,
                                        "relaxed_edge_shadow",
                                        rejection_reason=f"shadow: edge {fee_adjusted_edge:.4f} >= relaxed {_rel_min_edge:.4f} (orig {_min_edge:.4f} x {RELAXED_EDGE_DISCOUNT})",
                                        spot_price=spot, threshold=threshold,
                                        volatility=blended_rv, market_price=best_ask,
                                        seconds_to_close=seconds_remaining,
                                        calibrated_prob=final_prob, edge=edge,
                                        ofa_adjustment=ofa_adjustment,
                                        z_score=z_score,
                                        vol_regime=vol_est["regime"],
                                        calibrated_prob_raw=calibrated_prob_raw,
                                        kelly_f=_rel_kelly_f,
                                        position_size=_rel_position,
                                        breakeven_wr=best_ask / 100.0,
                                        expected_value=_rel_ev,
                                        ask_depth=ask_depth,
                                        best_ask_source=best_ask_source,
                                        raw_prob=raw_prob,
                                        calibration_method=calibration_method,
                                        fee_adjusted_edge=fee_adjusted_edge,
                                        product_type=window.get("product_type"),
                                        **_shadow_diag)
                                except Exception:
                                    logging.warning("insert_evaluated_opportunity failed (relaxed_edge_shadow)", exc_info=True)

                    continue

                # Compute position size via Kelly criterion
                balance = self._get_balance_cached()
                if balance is None or balance <= 0:
                    continue
                # Capital allocator: per-strategy budget (defaults to full balance)
                _strategy_key = window.get("product_type") or "crypto_15m"
                if _strategy_key == "15m":
                    _strategy_key = "crypto_15m"
                elif _strategy_key == "hourly":
                    _strategy_key = "crypto_hourly"
                _sizing_balance = balance
                if self._ml and getattr(self._ml, "capital_allocator", None):
                    try:
                        _sizing_balance = self._ml.capital_allocator.get_budget_cents(
                            _strategy_key, balance, locked_by_strategy=None)
                        if _sizing_balance <= 0:
                            _sizing_balance = balance  # fallback: never zero out live trading
                    except Exception:
                        _sizing_balance = balance
                sizing = self._sizer.compute(final_prob, best_ask, _sizing_balance)

                # Product-type-specific sizing: fractional Kelly + conservative per-trade risk cap
                _scfg = get_market_config(window.get("product_type"))
                if _scfg.kelly_fraction < 1.0:
                    _full_kelly_contracts = sizing["contracts"]
                    sizing["contracts"] = max(1, int(sizing["contracts"] * _scfg.kelly_fraction))
                    _type_max = int((balance * _scfg.max_risk_per_trade) / best_ask)
                    if sizing["contracts"] > _type_max:
                        sizing["contracts"] = max(1, _type_max)

                # Asset-specific risk cap (XRP RK vol systematically underestimates)
                if asset == "XRP" and _pt in (None, "15m"):
                    _xrp_max = int((_sizing_balance * XRP_MAX_RISK_PER_TRADE) / best_ask)
                    if sizing["contracts"] > _xrp_max >= 1:
                        logging.info("XRP risk cap: %d -> %d contracts (%.0f%% max risk)",
                                     sizing["contracts"], _xrp_max, XRP_MAX_RISK_PER_TRADE * 100)
                        sizing["contracts"] = _xrp_max

                # Low-STC sizing cap: halve position when STC < 100s
                # Data: 0-100s STC is -$84/14d (12W/2L, catastrophic losses wipe gains)
                if (_pt in (None, "15m") and seconds_remaining < LOW_STC_SIZING_CAP_THRESHOLD
                        and LOW_STC_SIZING_CAP < 1.0 and sizing["contracts"] > 0):
                    _pre_stc_cap = sizing["contracts"]
                    sizing["contracts"] = max(1, int(sizing["contracts"] * LOW_STC_SIZING_CAP))
                    if sizing["contracts"] < _pre_stc_cap:
                        logging.info("Low-STC cap: %d -> %d contracts (STC=%.0fs, cap=%.1fx)",
                                     _pre_stc_cap, sizing["contracts"], seconds_remaining, LOW_STC_SIZING_CAP)

                # Cap by existing exposure (positions + resting orders) to prevent
                # accumulation across scan ticks on the same ticker
                existing_exposure = 0
                for pos in self._state.get_open_positions():
                    if pos["ticker"] == ticker:
                        existing_exposure += pos["count"]
                        break
                for resting in self._state.get_resting_orders(ticker=ticker):
                    existing_exposure += resting["count"]
                if existing_exposure > 0:
                    sizing["contracts"] = max(0, sizing["contracts"] - existing_exposure)

                if sizing["contracts"] <= 0:
                    scan_stats[asset]["zero_sizing"] += 1
                    self._recent_opportunities.append({
                        "ticker": ticker, "asset": asset,
                        "seconds_to_close": round(seconds_remaining, 1),
                        "best_ask": best_ask,
                        "edge_bps": round(edge * 10000),
                        "chosen_strategy": None,
                        "rejection_reason": "zero_sizing",
                        "ts": datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                    })
                    try:
                        self._logger.log_opportunity({
                            "filter_stage": "zero_sizing",
                            "ticker": ticker,
                            "event_ticker": window["event_ticker"],
                            "asset": asset,
                            "rejection_reason": "position sizing yielded 0 contracts",
                            "spot_price": spot,
                            "threshold": threshold,
                            "volatility": blended_rv,
                            "market_price": best_ask,
                            "seconds_to_close": round(seconds_remaining, 1),
                            "calibrated_prob": round(final_prob, 6),
                            "edge": round(edge, 6),
                            "ofa_adjustment": round(ofa_adjustment, 6),
                            "raw_prob": round(raw_prob, 6) if raw_prob is not None else None,
                            "old_system_prob": round(_old_system_prob, 6),
                            "counterfactual": _cf,
                            **_shadow_diag,
                            **_shadow_extra,
                        })
                        _dedup_key = (ticker, "zero_sizing")
                        if _dedup_key not in self._eval_opp_seen:
                            self._eval_opp_seen.add(_dedup_key)
                            _ev = (final_prob * (100 - best_ask)) - ((1 - final_prob) * best_ask) - est_fee_1c
                            self._state.insert_evaluated_opportunity(
                                ticker, window["event_ticker"], asset,
                                "zero_sizing",
                                rejection_reason="0 contracts from sizing",
                                spot_price=spot, threshold=threshold,
                                volatility=blended_rv, market_price=best_ask,
                                seconds_to_close=seconds_remaining,
                                calibrated_prob=final_prob, edge=edge,
                                ofa_adjustment=ofa_adjustment,
                                z_score=z_score,
                                vol_regime=vol_est["regime"],
                                calibrated_prob_raw=calibrated_prob_raw,
                                kelly_f=sizing["kelly_f"],
                                position_size=0,
                                breakeven_wr=best_ask / 100.0,
                                expected_value=round(_ev, 2),
                                drawdown_scaler=sizing["drawdown_scaler"],
                                ask_depth=ask_depth,
                                best_ask_source=best_ask_source,
                                ofa_confidence=ofa_signals["confidence"] if ofa_signals else "none",
                                raw_prob=raw_prob,
                                calibration_method=calibration_method,
                                old_system_prob=_old_system_prob,
                                fee_adjusted_edge=fee_adjusted_edge,
                                counterfactual=_cf_json,
                                shadow_cal_prob=(_cf.get("old_cal_system") or _cf.get("cal_pipeline", {})).get("prob") if _cf else None,
                                shadow_cal_fee_edge=(_cf.get("old_cal_system") or _cf.get("cal_pipeline", {})).get("fee_edge") if _cf else None,
                                shadow_cal_temperature=(_cf.get("old_cal_system") or _cf.get("cal_pipeline", {})).get("temperature") if _cf else None,
                                product_type=window.get("product_type"),
                                wx_ensemble_mean=_shadow_extra.get("wx_ensemble_mean"),
                                wx_ensemble_std=_shadow_extra.get("wx_ensemble_std"),
                                wx_bias_correction=_shadow_extra.get("wx_bias_correction"),
                                wx_n_members=_shadow_extra.get("wx_n_members"),
                                wx_market_type=_shadow_extra.get("wx_market_type"),
                                wx_no_side_edge=_shadow_extra.get("wx_no_side_edge"),
                                wx_hrrr_temp=_shadow_extra.get("wx_hrrr_temp"),
                                wx_corrected_mean=_shadow_extra.get("wx_corrected_mean"),
                                hourly_pre_temp_prob=_hourly_pre_temp_prob,
                                hourly_applied_temp_t=_configured_temp_t,
                                hourly_shadow_temp_2_0=_hourly_shadow_temp_2_0,
                                hourly_shadow_temp_1_0=_hourly_shadow_temp_1_0,
                                hourly_shadow_temp_2_5=_hourly_shadow_temp_2_5,
                                hourly_shadow_blend_50=_hourly_shadow_blend_50,
                                hourly_shadow_temp_1_75=_hourly_shadow_temp_1_75,
                                hourly_shadow_temp_3_0=_hourly_shadow_temp_3_0,
                                hourly_shadow_blend_20=_hourly_shadow_blend_20,
                                hourly_shadow_blend_30=_hourly_shadow_blend_30,
                                hourly_shadow_blend_60=_hourly_shadow_blend_60,
                                hourly_post_temp_prob=_hourly_post_temp_prob,
                                **_oft_db, **_shadow_diag)
                    except Exception:
                        logging.warning("insert_evaluated_opportunity failed (spx/weather observation)", exc_info=True)
                    continue

                # Evaluate execution strategy for this market
                strategy_data = {
                    "z_score": z_score,
                    "calibrated_prob": final_prob,
                    "spot": spot,
                    "threshold": threshold,
                    "seconds_to_close": seconds_remaining,
                    "blended_rv": blended_rv,
                    "vol_regime": vol_est["regime"],
                    "best_yes_ask": best_ask,
                    "best_ask_depth": ask_depth,
                    "total_ob_depth": total_depth,
                    "convergence_velocity": self._scanner_convergence_velocity(ticker),
                    "edge": edge,
                    "min_entry_price": _entry_floor,
                    "max_entry_price": _entry_ceil,
                }
                strategy, strategy_scores = evaluate_execution_strategy(
                    strategy_data
                )
                if strategy in self._session_strategy_counts:
                    self._session_strategy_counts[strategy] += 1

                # Log strategy evaluation for every market evaluated
                self._logger.log_scan({
                    "type": "strategy_eval",
                    "ticker": ticker,
                    "asset": asset,
                    "seconds_to_close": round(seconds_remaining, 1),
                    "spot": spot,
                    "threshold": threshold,
                    "best_yes_ask": best_ask,
                    "edge": round(edge, 6),
                    "outcome_certainty_score": strategy_scores["certainty"],
                    "outcome_certainty_detail": strategy_scores["certainty_detail"],
                    "orderbook_state_score": strategy_scores["orderbook"],
                    "orderbook_state_detail": strategy_scores["orderbook_detail"],
                    "urgency_score": strategy_scores["urgency"],
                    "urgency_detail": strategy_scores["urgency_detail"],
                    "composite_score": strategy_scores["composite"],
                    "chosen_strategy": strategy,
                    "reason": strategy_scores["reason"],
                    "calibrated_prob_raw": round(calibrated_prob_raw, 6),
                    "ofa_adjustment": round(ofa_adjustment, 6),
                    "ofa_confidence": ofa_signals["confidence"] if ofa_signals else "none",
                    "ofa_adjustments_applied": ofa_signals["adjustments_applied"] if ofa_signals else [],
                })

                if strategy == STRATEGY_WAIT:
                    scan_stats[asset]["strategy_wait"] += 1
                    self._recent_opportunities.append({
                        "ticker": ticker, "asset": asset,
                        "seconds_to_close": round(seconds_remaining, 1),
                        "best_ask": best_ask,
                        "edge_bps": round(edge * 10000),
                        "chosen_strategy": "WAIT",
                        "rejection_reason": "strategy_wait",
                        "ts": datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                    })
                    try:
                        self._logger.log_opportunity({
                            "filter_stage": "strategy_wait",
                            "ticker": ticker,
                            "event_ticker": window["event_ticker"],
                            "asset": asset,
                            "rejection_reason": "strategy engine returned WAIT",
                            "spot_price": spot,
                            "threshold": threshold,
                            "volatility": blended_rv,
                            "market_price": best_ask,
                            "seconds_to_close": round(seconds_remaining, 1),
                            "calibrated_prob": round(final_prob, 6),
                            "edge": round(edge, 6),
                            "ofa_adjustment": round(ofa_adjustment, 6),
                            "composite_score": strategy_scores.get("composite"),
                            "raw_prob": round(raw_prob, 6) if raw_prob is not None else None,
                            "old_system_prob": round(_old_system_prob, 6),
                            "counterfactual": _cf,
                            **_shadow_diag,
                            **_shadow_extra,
                        })
                        _dedup_key = (ticker, "strategy_wait")
                        if _dedup_key not in self._eval_opp_seen:
                            self._eval_opp_seen.add(_dedup_key)
                            _ev = (final_prob * (100 - best_ask)) - ((1 - final_prob) * best_ask) - est_fee_1c
                            self._state.insert_evaluated_opportunity(
                                ticker, window["event_ticker"], asset,
                                "strategy_wait",
                                rejection_reason="WAIT strategy",
                                spot_price=spot, threshold=threshold,
                                volatility=blended_rv, market_price=best_ask,
                                seconds_to_close=seconds_remaining,
                                calibrated_prob=final_prob, edge=edge,
                                ofa_adjustment=ofa_adjustment,
                                strategy=strategy,
                                z_score=z_score,
                                vol_regime=vol_est["regime"],
                                calibrated_prob_raw=calibrated_prob_raw,
                                kelly_f=sizing["kelly_f"],
                                position_size=sizing["contracts"],
                                breakeven_wr=best_ask / 100.0,
                                expected_value=round(_ev, 2),
                                drawdown_scaler=sizing["drawdown_scaler"],
                                ask_depth=ask_depth,
                                best_ask_source=best_ask_source,
                                ofa_confidence=ofa_signals["confidence"] if ofa_signals else "none",
                                raw_prob=raw_prob,
                                calibration_method=calibration_method,
                                old_system_prob=_old_system_prob,
                                fee_adjusted_edge=fee_adjusted_edge,
                                counterfactual=_cf_json,
                                shadow_cal_prob=(_cf.get("old_cal_system") or _cf.get("cal_pipeline", {})).get("prob") if _cf else None,
                                shadow_cal_fee_edge=(_cf.get("old_cal_system") or _cf.get("cal_pipeline", {})).get("fee_edge") if _cf else None,
                                shadow_cal_temperature=(_cf.get("old_cal_system") or _cf.get("cal_pipeline", {})).get("temperature") if _cf else None,
                                product_type=window.get("product_type"),
                                wx_ensemble_mean=_shadow_extra.get("wx_ensemble_mean"),
                                wx_ensemble_std=_shadow_extra.get("wx_ensemble_std"),
                                wx_bias_correction=_shadow_extra.get("wx_bias_correction"),
                                wx_n_members=_shadow_extra.get("wx_n_members"),
                                wx_market_type=_shadow_extra.get("wx_market_type"),
                                wx_no_side_edge=_shadow_extra.get("wx_no_side_edge"),
                                wx_hrrr_temp=_shadow_extra.get("wx_hrrr_temp"),
                                wx_corrected_mean=_shadow_extra.get("wx_corrected_mean"),
                                hourly_pre_temp_prob=_hourly_pre_temp_prob,
                                hourly_applied_temp_t=_configured_temp_t,
                                hourly_shadow_temp_2_0=_hourly_shadow_temp_2_0,
                                hourly_shadow_temp_1_0=_hourly_shadow_temp_1_0,
                                hourly_shadow_temp_2_5=_hourly_shadow_temp_2_5,
                                hourly_shadow_blend_50=_hourly_shadow_blend_50,
                                hourly_shadow_temp_1_75=_hourly_shadow_temp_1_75,
                                hourly_shadow_temp_3_0=_hourly_shadow_temp_3_0,
                                hourly_shadow_blend_20=_hourly_shadow_blend_20,
                                hourly_shadow_blend_30=_hourly_shadow_blend_30,
                                hourly_shadow_blend_60=_hourly_shadow_blend_60,
                                hourly_post_temp_prob=_hourly_post_temp_prob,
                                **_oft_db, **_shadow_diag)
                    except Exception:
                        logging.warning("insert_evaluated_opportunity failed (strategy_wait)", exc_info=True)
                    # For observation-only product types, let signal flow through
                    # to observation gate — strategy timing isn't relevant for
                    # data collection.  strategy_wait is still logged above for
                    # counterfactual analysis.
                    _sw_cfg = get_market_config(window.get("product_type"))
                    if not _sw_cfg.observation_only:
                        continue
                    # else: fall through to config-driven filters → observation gate

                # ── Config-driven per-window filters (any market type can opt in) ──
                _fltcfg = get_market_config(window.get("product_type"))
                _flt_pt = _fltcfg.product_type

                # Layer 3a: Asset exclusion (config-driven)
                if _fltcfg.excluded_assets and asset in _fltcfg.excluded_assets:
                    _dedup_key = (ticker, f"{_flt_pt}_asset_excluded")
                    if _dedup_key not in self._eval_opp_seen:
                        self._eval_opp_seen.add(_dedup_key)
                        _ev = (final_prob * (100 - best_ask)) - ((1 - final_prob) * best_ask) - est_fee_1c
                        self._state.insert_evaluated_opportunity(
                            ticker, window["event_ticker"], asset, f"{_flt_pt}_asset_excluded",
                            spot_price=spot, threshold=threshold, volatility=blended_rv,
                            market_price=best_ask, seconds_to_close=seconds_remaining,
                            calibrated_prob=final_prob, edge=edge, z_score=z_score,
                            vol_regime=vol_est["regime"], raw_prob=raw_prob,
                            calibration_method=calibration_method, fee_adjusted_edge=fee_adjusted_edge,
                            breakeven_wr=best_ask / 100.0, expected_value=round(_ev, 2),
                            ask_depth=ask_depth, best_ask_source=best_ask_source,
                            product_type=window.get("product_type"),
                            hourly_pre_temp_prob=_hourly_pre_temp_prob,
                            hourly_applied_temp_t=_configured_temp_t,
                            hourly_shadow_temp_2_0=_hourly_shadow_temp_2_0,
                            hourly_shadow_temp_1_0=_hourly_shadow_temp_1_0,
                            hourly_shadow_temp_2_5=_hourly_shadow_temp_2_5,
                            hourly_shadow_blend_50=_hourly_shadow_blend_50,
                            hourly_shadow_temp_1_75=_hourly_shadow_temp_1_75,
                            hourly_shadow_temp_3_0=_hourly_shadow_temp_3_0,
                            hourly_shadow_blend_20=_hourly_shadow_blend_20,
                            hourly_shadow_blend_30=_hourly_shadow_blend_30,
                            hourly_shadow_blend_60=_hourly_shadow_blend_60,
                            hourly_post_temp_prob=_hourly_post_temp_prob,
                            **_oft_db, **_shadow_diag)
                    continue

                # Layer 2: STC timing restriction (config-driven)
                if (_fltcfg.min_stc_entry is not None
                        and (seconds_remaining < _fltcfg.min_stc_entry
                             or seconds_remaining > _fltcfg.max_stc_entry)):
                    _dedup_key = (ticker, f"{_flt_pt}_timing_restricted")
                    if _dedup_key not in self._eval_opp_seen:
                        self._eval_opp_seen.add(_dedup_key)
                        _ev = (final_prob * (100 - best_ask)) - ((1 - final_prob) * best_ask) - est_fee_1c
                        self._state.insert_evaluated_opportunity(
                            ticker, window["event_ticker"], asset, f"{_flt_pt}_timing_restricted",
                            spot_price=spot, threshold=threshold, volatility=blended_rv,
                            market_price=best_ask, seconds_to_close=seconds_remaining,
                            calibrated_prob=final_prob, edge=edge, z_score=z_score,
                            vol_regime=vol_est["regime"], raw_prob=raw_prob,
                            calibration_method=calibration_method, fee_adjusted_edge=fee_adjusted_edge,
                            breakeven_wr=best_ask / 100.0, expected_value=round(_ev, 2),
                            ask_depth=ask_depth, best_ask_source=best_ask_source,
                            product_type=window.get("product_type"),
                            wx_ensemble_mean=_shadow_extra.get("wx_ensemble_mean"),
                            wx_ensemble_std=_shadow_extra.get("wx_ensemble_std"),
                            wx_bias_correction=_shadow_extra.get("wx_bias_correction"),
                            wx_n_members=_shadow_extra.get("wx_n_members"),
                            wx_market_type=_shadow_extra.get("wx_market_type"),
                            wx_hrrr_temp=_shadow_extra.get("wx_hrrr_temp"),
                            wx_corrected_mean=_shadow_extra.get("wx_corrected_mean"),
                            wx_no_side_edge=_shadow_extra.get("wx_no_side_edge"),
                            hourly_pre_temp_prob=_hourly_pre_temp_prob,
                            hourly_applied_temp_t=_configured_temp_t,
                            hourly_shadow_temp_2_0=_hourly_shadow_temp_2_0,
                            hourly_shadow_temp_1_0=_hourly_shadow_temp_1_0,
                            hourly_shadow_temp_2_5=_hourly_shadow_temp_2_5,
                            hourly_shadow_blend_50=_hourly_shadow_blend_50,
                            hourly_shadow_temp_1_75=_hourly_shadow_temp_1_75,
                            hourly_shadow_temp_3_0=_hourly_shadow_temp_3_0,
                            hourly_shadow_blend_20=_hourly_shadow_blend_20,
                            hourly_shadow_blend_30=_hourly_shadow_blend_30,
                            hourly_shadow_blend_60=_hourly_shadow_blend_60,
                            hourly_post_temp_prob=_hourly_post_temp_prob,
                            **_oft_db, **_shadow_diag)
                    continue

                # Layer 3b: Per-window position limit (config-driven)
                if _fltcfg.max_positions_per_window is not None:
                    _wkey = window["event_ticker"]
                    if self._hourly_window_counts.get(_wkey, 0) >= _fltcfg.max_positions_per_window:
                        _dedup_key = (ticker, f"{_flt_pt}_window_limit")
                        if _dedup_key not in self._eval_opp_seen:
                            self._eval_opp_seen.add(_dedup_key)
                            _ev = (final_prob * (100 - best_ask)) - ((1 - final_prob) * best_ask) - est_fee_1c
                            self._state.insert_evaluated_opportunity(
                                ticker, window["event_ticker"], asset, f"{_flt_pt}_window_limit",
                                spot_price=spot, threshold=threshold, volatility=blended_rv,
                                market_price=best_ask, seconds_to_close=seconds_remaining,
                                calibrated_prob=final_prob, edge=edge, z_score=z_score,
                                vol_regime=vol_est["regime"], raw_prob=raw_prob,
                                calibration_method=calibration_method, fee_adjusted_edge=fee_adjusted_edge,
                                breakeven_wr=best_ask / 100.0, expected_value=round(_ev, 2),
                                ask_depth=ask_depth, best_ask_source=best_ask_source,
                                product_type=window.get("product_type"),
                                wx_ensemble_mean=_shadow_extra.get("wx_ensemble_mean"),
                                wx_ensemble_std=_shadow_extra.get("wx_ensemble_std"),
                                wx_bias_correction=_shadow_extra.get("wx_bias_correction"),
                                wx_n_members=_shadow_extra.get("wx_n_members"),
                                wx_market_type=_shadow_extra.get("wx_market_type"),
                                wx_hrrr_temp=_shadow_extra.get("wx_hrrr_temp"),
                                wx_corrected_mean=_shadow_extra.get("wx_corrected_mean"),
                                wx_no_side_edge=_shadow_extra.get("wx_no_side_edge"),
                                hourly_pre_temp_prob=_hourly_pre_temp_prob,
                                hourly_applied_temp_t=_configured_temp_t,
                                hourly_shadow_temp_2_0=_hourly_shadow_temp_2_0,
                                hourly_shadow_temp_1_0=_hourly_shadow_temp_1_0,
                                hourly_shadow_temp_2_5=_hourly_shadow_temp_2_5,
                                hourly_shadow_blend_50=_hourly_shadow_blend_50,
                                hourly_shadow_temp_1_75=_hourly_shadow_temp_1_75,
                                hourly_shadow_temp_3_0=_hourly_shadow_temp_3_0,
                                hourly_shadow_blend_20=_hourly_shadow_blend_20,
                                hourly_shadow_blend_30=_hourly_shadow_blend_30,
                                hourly_shadow_blend_60=_hourly_shadow_blend_60,
                                hourly_post_temp_prob=_hourly_post_temp_prob,
                                **_oft_db, **_shadow_diag)
                        continue

                # Layer 3c: Per-window aggregate risk cap (config-driven)
                if _fltcfg.max_window_risk is not None:
                    _wkey = window["event_ticker"]
                    _wrisk = self._hourly_window_risk.get(_wkey, 0.0)
                    _this_risk = (sizing["contracts"] * best_ask) / (balance if balance > 0 else 1)
                    if _wrisk + _this_risk > _fltcfg.max_window_risk:
                        _dedup_key = (ticker, f"{_flt_pt}_window_risk_cap")
                        if _dedup_key not in self._eval_opp_seen:
                            self._eval_opp_seen.add(_dedup_key)
                            _ev = (final_prob * (100 - best_ask)) - ((1 - final_prob) * best_ask) - est_fee_1c
                            self._state.insert_evaluated_opportunity(
                                ticker, window["event_ticker"], asset, f"{_flt_pt}_window_risk_cap",
                                spot_price=spot, threshold=threshold, volatility=blended_rv,
                                market_price=best_ask, seconds_to_close=seconds_remaining,
                                calibrated_prob=final_prob, edge=edge, z_score=z_score,
                                vol_regime=vol_est["regime"], raw_prob=raw_prob,
                                calibration_method=calibration_method, fee_adjusted_edge=fee_adjusted_edge,
                                breakeven_wr=best_ask / 100.0, expected_value=round(_ev, 2),
                                ask_depth=ask_depth, best_ask_source=best_ask_source,
                                product_type=window.get("product_type"),
                                wx_ensemble_mean=_shadow_extra.get("wx_ensemble_mean"),
                                wx_ensemble_std=_shadow_extra.get("wx_ensemble_std"),
                                wx_bias_correction=_shadow_extra.get("wx_bias_correction"),
                                wx_n_members=_shadow_extra.get("wx_n_members"),
                                wx_market_type=_shadow_extra.get("wx_market_type"),
                                wx_hrrr_temp=_shadow_extra.get("wx_hrrr_temp"),
                                wx_corrected_mean=_shadow_extra.get("wx_corrected_mean"),
                                wx_no_side_edge=_shadow_extra.get("wx_no_side_edge"),
                                hourly_pre_temp_prob=_hourly_pre_temp_prob,
                                hourly_applied_temp_t=_configured_temp_t,
                                hourly_shadow_temp_2_0=_hourly_shadow_temp_2_0,
                                hourly_shadow_temp_1_0=_hourly_shadow_temp_1_0,
                                hourly_shadow_temp_2_5=_hourly_shadow_temp_2_5,
                                hourly_shadow_blend_50=_hourly_shadow_blend_50,
                                hourly_shadow_temp_1_75=_hourly_shadow_temp_1_75,
                                hourly_shadow_temp_3_0=_hourly_shadow_temp_3_0,
                                hourly_shadow_blend_20=_hourly_shadow_blend_20,
                                hourly_shadow_blend_30=_hourly_shadow_blend_30,
                                hourly_shadow_blend_60=_hourly_shadow_blend_60,
                                hourly_post_temp_prob=_hourly_post_temp_prob,
                                **_oft_db, **_shadow_diag)
                        continue

                # ── WEATHER FOCUS FILTER ──
                # Shadow trade signals only for lower_tail in NE/Midwest cities.
                # Other weather markets still logged as raw data in earlier filter stages
                # (price_out_of_range, insufficient_edge, timing_restricted) but don't
                # reach the observation gate for shadow trade simulation.
                if _pt == "weather":
                    from weather_engine import WEATHER_SHADOW_FOCUS_MARKET_TYPES, WEATHER_SHADOW_FOCUS_CITIES
                    _wx_mtype_here = _shadow_extra.get("wx_market_type")
                    _wx_city_here = asset.replace("_TEMP", "") if asset else ""
                    _wx_excluded_reason = None
                    if _wx_mtype_here and _wx_mtype_here not in WEATHER_SHADOW_FOCUS_MARKET_TYPES:
                        _wx_excluded_reason = f"weather_excluded_mtype_{_wx_mtype_here}"
                    elif _wx_city_here not in WEATHER_SHADOW_FOCUS_CITIES:
                        _wx_excluded_reason = "weather_excluded_city"
                    if _wx_excluded_reason:
                        _dedup_key = (ticker, _wx_excluded_reason)
                        if _dedup_key not in self._eval_opp_seen:
                            self._eval_opp_seen.add(_dedup_key)
                            _ev = (final_prob * (100 - best_ask)) - ((1 - final_prob) * best_ask) - est_fee_1c
                            self._state.insert_evaluated_opportunity(
                                ticker, window["event_ticker"], asset, _wx_excluded_reason,
                                spot_price=spot, threshold=threshold, volatility=blended_rv,
                                market_price=best_ask, seconds_to_close=seconds_remaining,
                                calibrated_prob=final_prob, edge=edge, z_score=z_score,
                                vol_regime=vol_est["regime"], raw_prob=raw_prob,
                                calibration_method=calibration_method, fee_adjusted_edge=fee_adjusted_edge,
                                breakeven_wr=best_ask / 100.0, expected_value=round(_ev, 2),
                                ask_depth=ask_depth, best_ask_source=best_ask_source,
                                product_type=_pt,
                                wx_ensemble_mean=_shadow_extra.get("wx_ensemble_mean"),
                                wx_ensemble_std=_shadow_extra.get("wx_ensemble_std"),
                                wx_bias_correction=_shadow_extra.get("wx_bias_correction"),
                                wx_n_members=_shadow_extra.get("wx_n_members"),
                                wx_market_type=_shadow_extra.get("wx_market_type"),
                                wx_hrrr_temp=_shadow_extra.get("wx_hrrr_temp"),
                                wx_corrected_mean=_shadow_extra.get("wx_corrected_mean"),
                                wx_no_side_edge=_shadow_extra.get("wx_no_side_edge"),
                                kelly_f=sizing.get("kelly_f") if sizing else None,
                                position_size=sizing.get("contracts") if sizing else None,
                                drawdown_scaler=sizing.get("drawdown_scaler") if sizing else None,
                                **_oft_db, **_shadow_diag)
                        continue

                # ── GENERIC OBSERVATION GATE ──
                # Config-driven: any market type with observation_only=True is blocked here
                if _fltcfg.observation_only and _fltcfg.observation_filter_label:
                    _obs_label = _fltcfg.observation_filter_label
                    _obs_pt = window.get("product_type")
                    # Hourly-specific: journal logging with extra detail
                    if _obs_pt == "hourly":
                        try:
                            self._logger.log_opportunity({
                                "filter_stage": _obs_label,
                                "product_type": _obs_pt,
                                "ticker": ticker,
                                "event_ticker": window["event_ticker"],
                                "asset": asset,
                                "spot_price": spot, "threshold": threshold,
                                "volatility": blended_rv, "market_price": best_ask,
                                "seconds_to_close": round(seconds_remaining, 1),
                                "calibrated_prob": round(final_prob, 6),
                                "edge": round(edge, 6),
                                "fee_adjusted_edge": round(fee_adjusted_edge, 6),
                                "position_size": sizing["contracts"],
                                "kelly_f": sizing["kelly_f"],
                                "drawdown_scaler": sizing["drawdown_scaler"],
                                "calibrated_prob_raw": round(calibrated_prob_raw, 6),
                                "ofa_adjustment": round(ofa_adjustment, 6),
                                "strategy": strategy,
                                "raw_prob": round(raw_prob, 6) if raw_prob is not None else None,
                                "hourly_pre_temp_prob": round(_hourly_pre_temp_prob, 6) if _hourly_pre_temp_prob is not None else None,
                                "hourly_temp_t": _fltcfg.temperature_t if _fltcfg.temperature_enabled else None,
                                "old_system_prob": round(_old_system_prob, 6),
                                "counterfactual": _cf,
                                **_shadow_diag,
                            })
                        except Exception:
                            pass
                    _dedup_key = (ticker, _obs_label)
                    if _dedup_key not in self._eval_opp_seen:
                        self._eval_opp_seen.add(_dedup_key)
                        _ev = (final_prob * (100 - best_ask)) - ((1 - final_prob) * best_ask) - est_fee_1c
                        # Build type-specific extra kwargs for DB insert
                        _obs_extra = {}
                        if _obs_pt == "hourly":
                            _obs_extra.update(
                                counterfactual=_cf_json,
                                shadow_cal_prob=(_cf.get("old_cal_system") or _cf.get("cal_pipeline", {})).get("prob") if _cf else None,
                                shadow_cal_fee_edge=(_cf.get("old_cal_system") or _cf.get("cal_pipeline", {})).get("fee_edge") if _cf else None,
                                shadow_cal_temperature=(_cf.get("old_cal_system") or _cf.get("cal_pipeline", {})).get("temperature") if _cf else None,
                                hourly_pre_temp_prob=_hourly_pre_temp_prob,
                                hourly_applied_temp_t=_configured_temp_t,
                                hourly_shadow_temp_2_0=_hourly_shadow_temp_2_0,
                                hourly_shadow_temp_1_0=_hourly_shadow_temp_1_0,
                                hourly_shadow_temp_2_5=_hourly_shadow_temp_2_5,
                                hourly_shadow_blend_50=_hourly_shadow_blend_50,
                                hourly_shadow_temp_1_75=_hourly_shadow_temp_1_75,
                                hourly_shadow_temp_3_0=_hourly_shadow_temp_3_0,
                                hourly_shadow_blend_20=_hourly_shadow_blend_20,
                                hourly_shadow_blend_30=_hourly_shadow_blend_30,
                                hourly_shadow_blend_60=_hourly_shadow_blend_60,
                                hourly_post_temp_prob=_hourly_post_temp_prob,
                            )
                        elif _obs_pt == "spx_hourly":
                            # SPX-specific diagnostics: VIX, seasonal, data quality
                            _spx_diag = {
                                "vix_implied_rv": vol_est.get("vix_implied_rv"),
                                "seasonal_factor": vol_est.get("seasonal_factor"),
                                "n_returns": vol_est.get("n_returns"),
                                "rk_rv": vol_est.get("rk_rv"),
                            }
                            _obs_extra.update(
                                counterfactual=json.dumps(_spx_diag),
                                hourly_pre_temp_prob=_hourly_pre_temp_prob,
                                hourly_applied_temp_t=_configured_temp_t,
                                hourly_shadow_temp_2_0=_hourly_shadow_temp_2_0,
                                hourly_shadow_temp_1_0=_hourly_shadow_temp_1_0,
                                hourly_shadow_temp_2_5=_hourly_shadow_temp_2_5,
                                hourly_shadow_blend_50=_hourly_shadow_blend_50,
                                hourly_shadow_temp_1_75=_hourly_shadow_temp_1_75,
                                hourly_shadow_temp_3_0=_hourly_shadow_temp_3_0,
                                hourly_shadow_blend_20=_hourly_shadow_blend_20,
                                hourly_shadow_blend_30=_hourly_shadow_blend_30,
                                hourly_shadow_blend_60=_hourly_shadow_blend_60,
                                hourly_post_temp_prob=_hourly_post_temp_prob,
                            )
                        elif _obs_pt == "weather":
                            _obs_extra.update(
                                wx_ensemble_mean=vol_est.get("ensemble_mean"),
                                wx_ensemble_std=vol_est.get("ensemble_std"),
                                wx_bias_correction=_shadow_extra.get("wx_bias_correction"),
                                wx_n_members=vol_est.get("n_members"),
                                wx_market_type=_shadow_extra.get("wx_market_type"),
                                wx_no_side_edge=_shadow_extra.get("wx_no_side_edge"),
                                wx_hrrr_temp=_shadow_extra.get("wx_hrrr_temp"),
                                wx_corrected_mean=_shadow_extra.get("wx_corrected_mean"),
                            )
                        self._state.insert_evaluated_opportunity(
                            ticker, window["event_ticker"], asset, _obs_label,
                            spot_price=spot, threshold=threshold, volatility=blended_rv,
                            market_price=best_ask, seconds_to_close=seconds_remaining,
                            calibrated_prob=final_prob, edge=edge, z_score=z_score,
                            vol_regime=vol_est["regime"], raw_prob=raw_prob,
                            calibration_method=calibration_method, fee_adjusted_edge=fee_adjusted_edge,
                            breakeven_wr=best_ask / 100.0, expected_value=round(_ev, 2),
                            ask_depth=ask_depth, best_ask_source=best_ask_source,
                            position_size=sizing["contracts"],
                            kelly_f=sizing["kelly_f"],
                            drawdown_scaler=sizing["drawdown_scaler"],
                            calibrated_prob_raw=calibrated_prob_raw,
                            ofa_adjustment=ofa_adjustment,
                            strategy=strategy,
                            old_system_prob=_old_system_prob,
                            product_type=_obs_pt, **_obs_extra, **_oft_db, **_shadow_diag)
                    _obs_log_prefix = {"hourly": "HOURLY_OBS", "spx_hourly": "SPX_OBS", "weather": "WEATHER_OBS"}.get(_obs_pt, "OBS")
                    logging.info("%s: %s ask=%d edge=%.2f%% prob=%.1f%% stc=%.0fs",
                                 _obs_log_prefix, ticker, best_ask, fee_adjusted_edge * 100, final_prob * 100, seconds_remaining)
                    # ── Weather Shadow Variants (capped30, short_stc) ──
                    if _obs_pt == "weather":
                        for _wscfg in WEATHER_SHADOW_CONFIGS:
                            _wsname = _wscfg["name"]
                            if "max_price" in _wscfg and best_ask > _wscfg["max_price"]:
                                continue
                            if "max_stc" in _wscfg and seconds_remaining > _wscfg["max_stc"]:
                                continue
                            _ws_dedup = (ticker, _wsname)
                            if _ws_dedup in self._eval_opp_seen:
                                continue
                            self._eval_opp_seen.add(_ws_dedup)
                            _ws_ev = (final_prob * (100 - best_ask)) - ((1 - final_prob) * best_ask) - est_fee_1c
                            try:
                                self._state.insert_evaluated_opportunity(
                                    ticker, window["event_ticker"], asset,
                                    _wsname,
                                    spot_price=spot, threshold=threshold,
                                    volatility=blended_rv, market_price=best_ask,
                                    seconds_to_close=seconds_remaining,
                                    calibrated_prob=final_prob, edge=edge,
                                    ofa_adjustment=ofa_adjustment,
                                    z_score=z_score, vol_regime=vol_est["regime"],
                                    raw_prob=raw_prob,
                                    calibrated_prob_raw=calibrated_prob_raw,
                                    calibration_method=calibration_method,
                                    fee_adjusted_edge=fee_adjusted_edge,
                                    breakeven_wr=best_ask / 100.0,
                                    expected_value=round(_ws_ev, 2),
                                    ask_depth=ask_depth, best_ask_source=best_ask_source,
                                    position_size=sizing["contracts"],
                                    kelly_f=sizing["kelly_f"],
                                    drawdown_scaler=sizing["drawdown_scaler"],
                                    strategy=strategy, old_system_prob=_old_system_prob,
                                    product_type="weather",
                                    wx_ensemble_mean=_shadow_extra.get("wx_ensemble_mean"),
                                    wx_ensemble_std=_shadow_extra.get("wx_ensemble_std"),
                                    wx_bias_correction=_shadow_extra.get("wx_bias_correction"),
                                    wx_n_members=_shadow_extra.get("wx_n_members"),
                                    wx_market_type=_shadow_extra.get("wx_market_type"),
                                    wx_hrrr_temp=_shadow_extra.get("wx_hrrr_temp"),
                                    wx_corrected_mean=_shadow_extra.get("wx_corrected_mean"),
                                    wx_no_side_edge=_shadow_extra.get("wx_no_side_edge"),
                                    **_oft_db, **_shadow_diag)
                            except Exception:
                                logging.warning("insert_evaluated_opportunity failed (%s)", _wsname, exc_info=True)
                        # ── Weather NO-side shadow ──
                        # Model is +25.5pp overconfident on YES → strong NO signal.
                        # Fire when YES prob ≥ 55% and NO edge after fees is positive.
                        if final_prob >= WEATHER_NO_SHADOW_MIN_YES_PROB and _no_ask_eq is not None:
                            _wn_dedup = (ticker, "weather_no_shadow")
                            if _wn_dedup not in self._eval_opp_seen:
                                _wn_no_prob = 1.0 - final_prob
                                _wn_no_fee = calculate_fee(1, _no_ask_eq, is_taker=True,
                                                           fee_mult_taker=_mcfg.fee_multiplier_taker,
                                                           fee_mult_maker=_mcfg.fee_multiplier_maker)
                                _wn_no_edge = _wn_no_prob - _no_ask_eq / 100.0
                                _wn_no_fee_edge = _wn_no_edge - _wn_no_fee / 100.0
                                if _wn_no_fee_edge > 0:
                                    self._eval_opp_seen.add(_wn_dedup)
                                    _wn_ev = (_wn_no_prob * (100 - _no_ask_eq)) - ((1 - _wn_no_prob) * _no_ask_eq) - _wn_no_fee
                                    try:
                                        self._state.insert_evaluated_opportunity(
                                            ticker, window["event_ticker"], asset,
                                            "weather_no_shadow",
                                            spot_price=spot, threshold=threshold,
                                            volatility=blended_rv, market_price=_no_ask_eq,
                                            seconds_to_close=seconds_remaining,
                                            calibrated_prob=_wn_no_prob, edge=_wn_no_edge,
                                            ofa_adjustment=ofa_adjustment,
                                            z_score=z_score, vol_regime=vol_est["regime"],
                                            raw_prob=1.0 - raw_prob if raw_prob is not None else None,
                                            calibrated_prob_raw=1.0 - calibrated_prob_raw if calibrated_prob_raw is not None else None,
                                            calibration_method=calibration_method,
                                            fee_adjusted_edge=_wn_no_fee_edge,
                                            breakeven_wr=_no_ask_eq / 100.0,
                                            expected_value=round(_wn_ev, 2),
                                            ask_depth=ask_depth, best_ask_source=best_ask_source,
                                            position_size=1,  # Fixed 1-contract sizing
                                            kelly_f=0.0,
                                            drawdown_scaler=1.0,
                                            strategy=strategy, old_system_prob=_old_system_prob,
                                            product_type="weather", side="no",
                                            wx_ensemble_mean=_shadow_extra.get("wx_ensemble_mean"),
                                            wx_ensemble_std=_shadow_extra.get("wx_ensemble_std"),
                                            wx_bias_correction=_shadow_extra.get("wx_bias_correction"),
                                            wx_n_members=_shadow_extra.get("wx_n_members"),
                                            wx_market_type=_shadow_extra.get("wx_market_type"),
                                            wx_hrrr_temp=_shadow_extra.get("wx_hrrr_temp"),
                                            wx_corrected_mean=_shadow_extra.get("wx_corrected_mean"),
                                            wx_no_side_edge=_shadow_extra.get("wx_no_side_edge"),
                                            **_oft_db, **_shadow_diag)
                                    except Exception:
                                        logging.warning("insert_evaluated_opportunity failed (weather_no_shadow)", exc_info=True)
                                    # ── Weather NO-side live candidate ──
                                    # Bypasses WEATHER_OBSERVATION_ONLY for NO-side only.
                                    # Gates: WEATHER_NO_SIDE_LIVE, STC >= 8h, positive fee-adj edge.
                                    if (WEATHER_NO_SIDE_LIVE
                                            and seconds_remaining >= WEATHER_NO_SIDE_MIN_STC
                                            and _wn_no_fee_edge > 0):
                                        _wn_cand_dedup = (ticker, "weather_no_candidate")
                                        if _wn_cand_dedup not in self._eval_opp_seen:
                                            self._eval_opp_seen.add(_wn_cand_dedup)
                                            # Log as candidate in evaluated_opportunities
                                            try:
                                                self._state.insert_evaluated_opportunity(
                                                    ticker, window["event_ticker"], asset,
                                                    "candidate",
                                                    spot_price=spot, threshold=threshold,
                                                    volatility=blended_rv, market_price=_no_ask_eq,
                                                    seconds_to_close=seconds_remaining,
                                                    calibrated_prob=_wn_no_prob, edge=_wn_no_edge,
                                                    ofa_adjustment=ofa_adjustment,
                                                    z_score=z_score, vol_regime=vol_est["regime"],
                                                    raw_prob=1.0 - raw_prob if raw_prob is not None else None,
                                                    calibrated_prob_raw=1.0 - calibrated_prob_raw if calibrated_prob_raw is not None else None,
                                                    calibration_method=calibration_method,
                                                    fee_adjusted_edge=_wn_no_fee_edge,
                                                    breakeven_wr=_no_ask_eq / 100.0,
                                                    expected_value=round(_wn_ev, 2),
                                                    ask_depth=ask_depth, best_ask_source=best_ask_source,
                                                    position_size=1, kelly_f=0.0, drawdown_scaler=1.0,
                                                    strategy=strategy, old_system_prob=_old_system_prob,
                                                    product_type="weather", side="no",
                                                    wx_ensemble_mean=_shadow_extra.get("wx_ensemble_mean"),
                                                    wx_ensemble_std=_shadow_extra.get("wx_ensemble_std"),
                                                    wx_bias_correction=_shadow_extra.get("wx_bias_correction"),
                                                    wx_n_members=_shadow_extra.get("wx_n_members"),
                                                    wx_market_type=_shadow_extra.get("wx_market_type"),
                                                    wx_hrrr_temp=_shadow_extra.get("wx_hrrr_temp"),
                                                    wx_corrected_mean=_shadow_extra.get("wx_corrected_mean"),
                                                    wx_no_side_edge=_shadow_extra.get("wx_no_side_edge"),
                                                    **_oft_db, **_shadow_diag)
                                            except Exception:
                                                logging.warning("insert_evaluated_opportunity failed (weather_no_candidate)", exc_info=True)
                                            candidates.append({
                                                "ticker": ticker,
                                                "event_ticker": window["event_ticker"],
                                                "asset": asset,
                                                "product_type": "weather",
                                                "side": "no",
                                                "spot": spot,
                                                "threshold": threshold,
                                                "seconds_to_close": round(seconds_remaining, 1),
                                                "blended_rv": blended_rv,
                                                "calibrated_prob": round(_wn_no_prob, 6),
                                                "z_score": z_score,
                                                "best_yes_ask": _no_ask_eq,  # NO price for execution
                                                "best_ask_source": best_ask_source,
                                                "edge": round(_wn_no_edge, 6),
                                                "position_size": 1,  # Fixed 1-contract
                                                "kelly_f": 0.0,
                                                "drawdown_scaler": 1.0,
                                                "vol_regime": vol_est["regime"],
                                                "balance_at_scan": balance,
                                                "strategy": strategy,
                                                "strategy_scores": {},
                                                "ob_snapshot": {},
                                                "calibrated_prob_raw": round(1.0 - calibrated_prob_raw, 6) if calibrated_prob_raw is not None else None,
                                                "ofa_adjustment": round(ofa_adjustment, 6),
                                                "ofa_confidence": "none",
                                                "raw_prob": 1.0 - raw_prob if raw_prob is not None else None,
                                                "calibration_method": calibration_method,
                                                "old_system_prob": round(1.0 - _old_system_prob, 6),
                                                "fee_adjusted_edge": round(_wn_no_fee_edge, 6),
                                                "kalshi_oft_signals": {},
                                                "counterfactual_json": None,
                                            })
                                            logging.info(
                                                "WEATHER_NO_CANDIDATE: %s no_price=%d edge=%.2f%% "
                                                "no_prob=%.1f%% stc=%.0fs",
                                                ticker, _no_ask_eq, _wn_no_fee_edge * 100,
                                                _wn_no_prob * 100, seconds_remaining)
                    # V2 variant: shadow cal pipeline (temperature + no blend)
                    if _obs_pt == "hourly" and _cf:
                        self._insert_hourly_v2_variant(
                            ticker, window, asset, raw_prob, best_ask,
                            seconds_remaining, spot, threshold, blended_rv,
                            ofa_adjustment, z_score, vol_est,
                            calibrated_prob_raw, est_fee_1c,
                            ask_depth, best_ask_source, _cf, _shadow_diag)
                    # ── Config A shadow variant (no_XRP + edge ≤ 0.7%) ──
                    if (_obs_pt == "hourly"
                            and asset not in HOURLY_CONFIG_A_EXCLUDED
                            and fee_adjusted_edge <= HOURLY_CONFIG_A_MAX_EDGE):
                        _ca_dedup = (ticker, "hourly_config_a")
                        if _ca_dedup not in self._eval_opp_seen:
                            self._eval_opp_seen.add(_ca_dedup)
                            _ca_ev = (final_prob * (100 - best_ask)) - ((1 - final_prob) * best_ask) - est_fee_1c
                            try:
                                self._state.insert_evaluated_opportunity(
                                    ticker, window["event_ticker"], asset,
                                    "hourly_config_a",
                                    spot_price=spot, threshold=threshold,
                                    volatility=blended_rv, market_price=best_ask,
                                    seconds_to_close=seconds_remaining,
                                    calibrated_prob=final_prob, edge=edge,
                                    ofa_adjustment=ofa_adjustment,
                                    z_score=z_score, vol_regime=vol_est["regime"],
                                    raw_prob=raw_prob,
                                    calibrated_prob_raw=calibrated_prob_raw,
                                    calibration_method=calibration_method,
                                    fee_adjusted_edge=fee_adjusted_edge,
                                    breakeven_wr=best_ask / 100.0,
                                    expected_value=round(_ca_ev, 2),
                                    ask_depth=ask_depth, best_ask_source=best_ask_source,
                                    position_size=sizing["contracts"],
                                    kelly_f=sizing["kelly_f"],
                                    drawdown_scaler=sizing["drawdown_scaler"],
                                    strategy=strategy, old_system_prob=_old_system_prob,
                                    product_type="hourly",
                                    **_oft_db, **_shadow_diag)
                            except Exception:
                                logging.warning("insert_evaluated_opportunity failed (hourly_config_a)", exc_info=True)
                    # ── Config B: BTC 70-89c wl2 (promotion candidate) ──
                    # Tracks the high-alpha low-price tier for BTC hourly.
                    # Uses its own per-window counter (_cb_window_counts) with price-sorted
                    # top-2 selection (same as backtest methodology).
                    if (_obs_pt == "hourly"
                            and asset == HOURLY_CONFIG_B_ASSET
                            and HOURLY_CONFIG_B_MIN_PRICE <= best_ask <= HOURLY_CONFIG_B_MAX_PRICE
                            and fee_adjusted_edge > 0):
                        _cb_wkey = window["event_ticker"]
                        _cb_count = self._config_b_window_counts.get(_cb_wkey, 0)
                        if _cb_count < HOURLY_CONFIG_B_MAX_PER_WINDOW:
                            _cb_dedup = (ticker, "hourly_config_b")
                            if _cb_dedup not in self._eval_opp_seen:
                                self._eval_opp_seen.add(_cb_dedup)
                                self._config_b_window_counts[_cb_wkey] = _cb_count + 1
                                _cb_ev = (final_prob * (100 - best_ask)) - ((1 - final_prob) * best_ask) - est_fee_1c
                                try:
                                    self._state.insert_evaluated_opportunity(
                                        ticker, window["event_ticker"], asset,
                                        "hourly_config_b",
                                        spot_price=spot, threshold=threshold,
                                        volatility=blended_rv, market_price=best_ask,
                                        seconds_to_close=seconds_remaining,
                                        calibrated_prob=final_prob, edge=edge,
                                        ofa_adjustment=ofa_adjustment,
                                        z_score=z_score, vol_regime=vol_est["regime"],
                                        raw_prob=raw_prob,
                                        calibrated_prob_raw=calibrated_prob_raw,
                                        calibration_method=calibration_method,
                                        fee_adjusted_edge=fee_adjusted_edge,
                                        breakeven_wr=best_ask / 100.0,
                                        expected_value=round(_cb_ev, 2),
                                        ask_depth=ask_depth, best_ask_source=best_ask_source,
                                        position_size=sizing["contracts"],
                                        kelly_f=sizing["kelly_f"],
                                        drawdown_scaler=sizing["drawdown_scaler"],
                                        strategy=strategy, old_system_prob=_old_system_prob,
                                        product_type="hourly",
                                        **_oft_db, **_shadow_diag)
                                except Exception:
                                    logging.warning("insert_evaluated_opportunity failed (hourly_config_b)", exc_info=True)
                    # ── Configs C–G: data-driven shadow variants ──
                    if _obs_pt == "hourly":
                        for _scfg in HOURLY_SHADOW_CONFIGS:
                            _sname = _scfg["name"]
                            if "included_assets" in _scfg and asset not in _scfg["included_assets"]:
                                continue
                            if "excluded_assets" in _scfg and asset in _scfg["excluded_assets"]:
                                continue
                            if "min_stc" in _scfg and seconds_remaining < _scfg["min_stc"]:
                                continue
                            if "max_stc" in _scfg and seconds_remaining > _scfg["max_stc"]:
                                continue
                            if "max_edge" in _scfg and fee_adjusted_edge > _scfg["max_edge"]:
                                continue
                            _s_dedup = (ticker, _sname)
                            if _s_dedup in self._eval_opp_seen:
                                continue
                            self._eval_opp_seen.add(_s_dedup)
                            # Recompute prob for configs with custom temperature/blend
                            _s_final = final_prob
                            _s_edge = edge
                            _s_fee_edge = fee_adjusted_edge
                            if "temperature" in _scfg or "blend_w" in _scfg:
                                _s_base = _hourly_pre_temp_prob
                                if _s_base is not None:
                                    _s_t = _scfg.get("temperature", _configured_temp_t or 1.0)
                                    _sp = max(0.001, min(0.999, _s_base))
                                    _slz = math.log(_sp / (1.0 - _sp))
                                    _s_final = 1.0 / (1.0 + math.exp(-_slz / _s_t))
                                    _s_final = max(0.01, min(NUMERICAL_SAFETY_CEILING, _s_final + ofa_adjustment))
                                    _s_bw = _scfg.get("blend_w", _effective_blend_w)
                                    if best_ask < ENDGAME_BLEND_PRICE and _s_bw > 0:
                                        _s_final = (1.0 - _s_bw) * _s_final + _s_bw * (best_ask / 100.0)
                                    _s_edge = _s_final - best_ask / 100.0
                                    _s_fee_edge = _s_edge - est_fee_1c / 100.0
                            _s_ev = (_s_final * (100 - best_ask)) - ((1 - _s_final) * best_ask) - est_fee_1c
                            try:
                                self._state.insert_evaluated_opportunity(
                                    ticker, window["event_ticker"], asset,
                                    _sname,
                                    spot_price=spot, threshold=threshold,
                                    volatility=blended_rv, market_price=best_ask,
                                    seconds_to_close=seconds_remaining,
                                    calibrated_prob=_s_final, edge=_s_edge,
                                    ofa_adjustment=ofa_adjustment,
                                    z_score=z_score, vol_regime=vol_est["regime"],
                                    raw_prob=raw_prob,
                                    calibrated_prob_raw=calibrated_prob_raw,
                                    calibration_method=calibration_method,
                                    fee_adjusted_edge=_s_fee_edge,
                                    breakeven_wr=best_ask / 100.0,
                                    expected_value=round(_s_ev, 2),
                                    ask_depth=ask_depth, best_ask_source=best_ask_source,
                                    position_size=sizing["contracts"],
                                    kelly_f=sizing["kelly_f"],
                                    drawdown_scaler=sizing["drawdown_scaler"],
                                    strategy=strategy, old_system_prob=_old_system_prob,
                                    product_type="hourly",
                                    **_oft_db, **_shadow_diag)
                            except Exception:
                                logging.warning("insert_evaluated_opportunity failed (%s)", _sname, exc_info=True)
                    # Increment per-window counters even in observation mode so Layer 3b/3c
                    # limits work for counterfactual analysis (without this, counter stays 0
                    # and the limit is dead code — bug found by audit: 11 SPX positions in one window)
                    if _fltcfg.max_positions_per_window is not None:
                        _wkey = window["event_ticker"]
                        self._hourly_window_counts[_wkey] = self._hourly_window_counts.get(_wkey, 0) + 1
                        self._hourly_window_risk[_wkey] = self._hourly_window_risk.get(_wkey, 0.0) + \
                            (sizing["contracts"] * best_ask) / (balance if balance > 0 else 1)

                    # ── Hourly Alt Shadow Strategies (BTC/ETH/SOL/XRP) ──
                    # Evaluate Market-Making and HAR-RV shadow strategies in parallel
                    # with the existing EGARCH pipeline. Shadow-only, cannot place orders.
                    if (_obs_pt == "hourly"
                            and self._ml and getattr(self._ml, "hourly_alt_shadow", None)):
                        try:
                            _alt_bid = OrderExecutor._best_yes_bid(ob_data) if ob_data else None
                            _alt_no_ask_raw = mkt.get("no_ask_dollars") or mkt.get("no_ask")
                            _alt_no_ask = (dollars_str_to_cents(_alt_no_ask_raw) if isinstance(_alt_no_ask_raw, str)
                                           else int(_alt_no_ask_raw)) if _alt_no_ask_raw is not None else None
                            self._ml.hourly_alt_shadow.evaluate_strike(
                                asset=asset, ticker=ticker,
                                event_ticker=window["event_ticker"],
                                spot_price=spot, threshold=threshold,
                                seconds_to_close=seconds_remaining,
                                best_bid=_alt_bid, best_ask=best_ask,
                                market_price=best_ask, ob_data=ob_data,
                                egarch_prob=final_prob,
                                egarch_edge=fee_adjusted_edge,
                                no_ask=_alt_no_ask)
                        except Exception:
                            logging.debug("hourly_alt_shadow evaluate failed", exc_info=True)

                    # ── SPX HAR-RV Shadow Strategy ──
                    # Evaluate HAR-RV shadow strategy in parallel with EGARCH
                    if (_obs_pt == "spx_hourly"
                            and self._ml and getattr(self._ml, "spx_harrv_shadow", None)):
                        try:
                            _harv_bid = OrderExecutor._best_yes_bid(ob_data) if ob_data else None
                            _harv_no_ask_raw = mkt.get("no_ask_dollars") or mkt.get("no_ask")
                            _harv_no_ask = (dollars_str_to_cents(_harv_no_ask_raw) if isinstance(_harv_no_ask_raw, str)
                                            else int(_harv_no_ask_raw)) if _harv_no_ask_raw is not None else None
                            self._ml.spx_harrv_shadow.evaluate_strike(
                                ticker=ticker,
                                event_ticker=window["event_ticker"],
                                spot_price=spot, threshold=threshold,
                                seconds_to_close=seconds_remaining,
                                best_bid=_harv_bid, best_ask=best_ask,
                                market_price=best_ask,
                                egarch_prob=final_prob,
                                egarch_edge=fee_adjusted_edge,
                                no_ask=_harv_no_ask)
                        except Exception:
                            logging.debug("spx_harrv_shadow evaluate failed", exc_info=True)

                    continue  # DO NOT add to candidates — observation gate

                # ── 15M Shadow Engine (post-filter) ──
                # _seen dedup in shadow engine prevents double-eval with pre-filter call.
                if (window.get("product_type") in (None, "15m")
                        and self._ml and getattr(self._ml, "fifteenm_shadow", None)):
                    try:
                        _15m_bid = OrderExecutor._best_yes_bid(ob_data) if ob_data else None
                        _15m_no_ask_raw = mkt.get("no_ask_dollars") or mkt.get("no_ask")
                        _15m_no_ask = (dollars_str_to_cents(_15m_no_ask_raw) if isinstance(_15m_no_ask_raw, str)
                                       else int(_15m_no_ask_raw)) if _15m_no_ask_raw is not None else None
                        self._ml.fifteenm_shadow.evaluate_strike(
                            asset=asset, ticker=ticker,
                            event_ticker=window["event_ticker"],
                            spot_price=spot, threshold=threshold,
                            seconds_to_close=seconds_remaining,
                            market_price=best_ask,
                            best_bid=_15m_bid, best_ask=best_ask,
                            blended_rv=blended_rv,
                            egarch_sigma=vol_est.get("egarch_sigma"),
                            z_score=z_score, live_prob=final_prob,
                            live_edge=edge, live_fee_edge=fee_adjusted_edge,
                            egarch_blend_weight=_shadow_diag.get("egarch_blend_weight"),
                            fee_adjusted_edge=fee_adjusted_edge,
                            no_ask=_15m_no_ask)
                    except Exception:
                        logging.warning("fifteenm_shadow evaluate failed", exc_info=True)

                # ── STC SHADOW GATE (15M only) ──
                # Markets at 500-900s STC: log full evaluation for data collection, but don't trade.
                # 0-500s is LIVE. Non-XRP tagged "stc_shadow_promoted" for variant tracking.
                if window.get("product_type") in (None, "15m") and seconds_remaining > STC_SHADOW_THRESHOLD:
                    _stc_stage = "stc_shadow_no_xrp" if asset != "XRP" else "stc_shadow_xrp"
                    _dedup_key = (ticker, _stc_stage)
                    if _dedup_key not in self._eval_opp_seen:
                        self._eval_opp_seen.add(_dedup_key)
                        _ev = (final_prob * (100 - best_ask)) - ((1 - final_prob) * best_ask) - est_fee_1c
                        self._state.insert_evaluated_opportunity(
                            ticker, window["event_ticker"], asset, _stc_stage,
                            spot_price=spot, threshold=threshold, volatility=blended_rv,
                            market_price=best_ask, seconds_to_close=seconds_remaining,
                            calibrated_prob=final_prob, edge=edge, z_score=z_score,
                            vol_regime=vol_est["regime"], raw_prob=raw_prob,
                            calibration_method=calibration_method, fee_adjusted_edge=fee_adjusted_edge,
                            breakeven_wr=best_ask / 100.0, expected_value=round(_ev, 2),
                            ask_depth=ask_depth, best_ask_source=best_ask_source,
                            position_size=sizing["contracts"],
                            kelly_f=sizing["kelly_f"],
                            drawdown_scaler=sizing["drawdown_scaler"],
                            calibrated_prob_raw=calibrated_prob_raw,
                            ofa_adjustment=ofa_adjustment,
                            strategy=strategy,
                            old_system_prob=_old_system_prob,
                            product_type=window.get("product_type"),
                            **_oft_db, **_shadow_diag)
                    continue

                # ── XRP SHADOW GATE (15M only) ──
                # XRP 15M: -$32.97 all-time. Log for counterfactual, don't trade.
                if XRP_15M_SHADOW and asset == "XRP" and window.get("product_type") in (None, "15m"):
                    _dedup_key = (ticker, "xrp_shadow")
                    if _dedup_key not in self._eval_opp_seen:
                        self._eval_opp_seen.add(_dedup_key)
                        _ev = (final_prob * (100 - best_ask)) - ((1 - final_prob) * best_ask) - est_fee_1c
                        _xrp_88_tag = "xrp_88_eligible" if best_ask >= XRP_SHADOW_MIN_PRICE else "xrp_88_ineligible"
                        self._state.insert_evaluated_opportunity(
                            ticker, window["event_ticker"], asset, "xrp_shadow",
                            spot_price=spot, threshold=threshold, volatility=blended_rv,
                            market_price=best_ask, seconds_to_close=seconds_remaining,
                            calibrated_prob=final_prob, edge=edge, z_score=z_score,
                            vol_regime=vol_est["regime"], raw_prob=raw_prob,
                            calibration_method=calibration_method, fee_adjusted_edge=fee_adjusted_edge,
                            breakeven_wr=best_ask / 100.0, expected_value=round(_ev, 2),
                            ask_depth=ask_depth, best_ask_source=best_ask_source,
                            position_size=sizing["contracts"],
                            kelly_f=sizing["kelly_f"],
                            drawdown_scaler=sizing["drawdown_scaler"],
                            calibrated_prob_raw=calibrated_prob_raw,
                            ofa_adjustment=ofa_adjustment,
                            strategy=strategy,
                            old_system_prob=_old_system_prob,
                            product_type=window.get("product_type"),
                            counterfactual=_xrp_88_tag,
                            **_oft_db, **_shadow_diag)
                    continue

                # Track per-window counts for Layer 3b/3c limits (config-driven)
                if _fltcfg.max_positions_per_window is not None:
                    _wkey = window["event_ticker"]
                    self._hourly_window_counts[_wkey] = self._hourly_window_counts.get(_wkey, 0) + 1
                    self._hourly_window_risk[_wkey] = self._hourly_window_risk.get(_wkey, 0.0) + \
                        (sizing["contracts"] * best_ask) / (balance if balance > 0 else 1)

                scan_stats[asset]["candidates"] += 1
                self._session_total_candidates += 1
                self._session_asset_perf.setdefault(
                    asset, {"opportunities_found": 0, "times_selected": 0, "times_rejected": 0}
                )["opportunities_found"] += 1
                self._last_opportunity_ts = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                self._recent_opportunities.append({
                    "ticker": ticker,
                    "asset": asset,
                    "seconds_to_close": round(seconds_remaining, 1),
                    "best_ask": best_ask,
                    "edge_bps": round(edge * 10000),
                    "chosen_strategy": strategy,
                    "rejection_reason": None,
                    "ts": self._last_opportunity_ts,
                })
                try:
                    self._logger.log_opportunity({
                        "filter_stage": "candidate",
                        "ticker": ticker,
                        "event_ticker": window["event_ticker"],
                        "asset": asset,
                        "spot_price": spot,
                        "threshold": threshold,
                        "volatility": blended_rv,
                        "market_price": best_ask,
                        "best_ask_source": best_ask_source,
                        "seconds_to_close": round(seconds_remaining, 1),
                        "calibrated_prob": round(final_prob, 6),
                        "edge": round(edge, 6),
                        "position_size": sizing["contracts"],
                        "strategy": strategy,
                        "ofa_adjustment": round(ofa_adjustment, 6),
                        "raw_prob": round(raw_prob, 6) if raw_prob is not None else None,
                        "old_system_prob": round(_old_system_prob, 6),
                        "kalshi_oft": (ofa_signals or {}).get("signals", {}).get("kalshi_orderbook", {}),
                        "counterfactual": _cf,
                        **_shadow_diag,
                        **_shadow_extra,
                    })
                except Exception:
                    logging.warning("insert_evaluated_opportunity failed (candidate)", exc_info=True)

                candidates.append({
                    "ticker": ticker,
                    "event_ticker": window["event_ticker"],
                    "asset": asset,
                    "product_type": window.get("product_type"),
                    "spot": spot,
                    "threshold": threshold,
                    "seconds_to_close": round(seconds_remaining, 1),
                    "blended_rv": blended_rv,
                    "calibrated_prob": round(final_prob, 6),
                    "z_score": z_score,
                    "best_yes_ask": best_ask,
                    "best_ask_source": best_ask_source,
                    "edge": round(edge, 6),
                    "position_size": sizing["contracts"],
                    "kelly_f": sizing["kelly_f"],
                    "drawdown_scaler": sizing["drawdown_scaler"],
                    "vol_regime": vol_est["regime"],
                    "balance_at_scan": balance,
                    "strategy": strategy,
                    "strategy_scores": strategy_scores,
                    "ob_snapshot": {
                        "best_ask": best_ask,
                        "ask_depth": ask_depth,
                        "total_depth": total_depth,
                        "best_bid": OrderExecutor._best_yes_bid(ob_data) if ob_data else None,
                        "bid_depth": OrderExecutor._best_yes_bid_depth(ob_data) if ob_data else 0,
                        "spread": (best_ask - OrderExecutor._best_yes_bid(ob_data))
                                  if ob_data and OrderExecutor._best_yes_bid(ob_data) is not None else None,
                    },
                    "calibrated_prob_raw": round(calibrated_prob_raw, 6),
                    "ofa_adjustment": round(ofa_adjustment, 6),
                    "ofa_confidence": ofa_signals["confidence"] if ofa_signals else "none",
                    "raw_prob": raw_prob,
                    "calibration_method": calibration_method,
                    "old_system_prob": round(_old_system_prob, 6),
                    "fee_adjusted_edge": round(fee_adjusted_edge, 6),
                    "kalshi_oft_signals": (ofa_signals or {}).get("signals", {}).get("kalshi_orderbook", {}),
                    "counterfactual_json": _cf_json,
                    "shadow_cal_prob": (_cf.get("old_cal_system") or _cf.get("cal_pipeline", {})).get("prob") if _cf else None,
                    "shadow_cal_fee_edge": (_cf.get("old_cal_system") or _cf.get("cal_pipeline", {})).get("fee_edge") if _cf else None,
                    "shadow_cal_temperature": (_cf.get("old_cal_system") or _cf.get("cal_pipeline", {})).get("temperature") if _cf else None,
                    "hourly_pre_temp_prob": _hourly_pre_temp_prob,
                    "hourly_applied_temp_t": _configured_temp_t,
                    "hourly_shadow_temp_2_0": _hourly_shadow_temp_2_0,
                    "hourly_shadow_temp_1_0": _hourly_shadow_temp_1_0,
                    "hourly_shadow_temp_2_5": _hourly_shadow_temp_2_5,
                    "hourly_shadow_blend_50": _hourly_shadow_blend_50,
                    "hourly_shadow_temp_1_75": _hourly_shadow_temp_1_75,
                    "hourly_shadow_temp_3_0": _hourly_shadow_temp_3_0,
                    "hourly_shadow_blend_20": _hourly_shadow_blend_20,
                    "hourly_shadow_blend_30": _hourly_shadow_blend_30,
                    "hourly_shadow_blend_60": _hourly_shadow_blend_60,
                    "hourly_post_temp_prob": _hourly_post_temp_prob,
                    **_shadow_diag,
                    **_shadow_extra,
                })

                # Respect per-tick orderbook fetch cap
                if ob_fetches_this_tick >= MAX_OB_FETCHES_PER_TICK:
                    break
            if ob_fetches_this_tick >= MAX_OB_FETCHES_PER_TICK:
                break

        if PRICE_SHADOW_ENABLED and _price_shadow_queue:
            self._process_price_shadow(_price_shadow_queue)

        # NO-side shadow evaluation for all queued markets
        if _no_side_queue:
            self._process_no_side_shadow(_no_side_queue)

        # Overnight LP shadow evaluation for 50-85c contracts during overnight hours
        if _overnight_lp_queue:
            self._process_overnight_lp_shadow(_overnight_lp_queue)

        if not candidates:
            self._last_scan_stats = scan_stats
            return None

        # ── Separate decided contract candidates (additive overlay, bypass single-asset filter) ──
        _dc_candidates = [c for c in candidates if c.get("strategy", "").startswith("decided_")]
        _main_candidates = [c for c in candidates if not c.get("strategy", "").startswith("decided_")]
        candidates = _main_candidates  # single-asset filter only applies to main pipeline

        # ── Single-asset-per-timeslot: pick highest edge per 15-min window ──
        if ONE_ASSET_PER_WINDOW:
            # Group candidates by timeslot (shared across assets)
            by_timeslot: Dict[str, List[Dict]] = {}
            for c in candidates:
                ts = self._window_timeslot(c["event_ticker"])
                by_timeslot.setdefault(ts, []).append(c)

            # Keep only the single best-edge candidate per timeslot
            filtered: List[Dict] = []
            for ts, slot_candidates in by_timeslot.items():
                slot_candidates.sort(key=lambda c: c["edge"], reverse=True)
                winner = slot_candidates[0]
                filtered.append(winner)
                self._session_asset_perf[winner["asset"]]["times_selected"] += 1

                # Log which assets were rejected in favor of the winner
                if len(slot_candidates) > 1:
                    for c in slot_candidates[1:]:
                        self._session_asset_perf[c["asset"]]["times_rejected"] += 1
                    rejected = [
                        {"asset": c["asset"], "ticker": c["ticker"],
                         "edge": round(c["edge"], 6), "calibrated_prob": c["calibrated_prob"]}
                        for c in slot_candidates[1:]
                    ]
                    self._logger.log_scan({
                        "type": "single_asset_selection",
                        "timeslot": ts,
                        "chosen_asset": winner["asset"],
                        "chosen_ticker": winner["ticker"],
                        "chosen_edge": round(winner["edge"], 6),
                        "rejected_assets": rejected,
                        "reason": "single best asset per window (correlation-adjusted)",
                    })
                    for c in slot_candidates[1:]:
                        try:
                            self._logger.log_opportunity({
                                "filter_stage": "single_asset_selection",
                                "ticker": c["ticker"],
                                "event_ticker": c["event_ticker"],
                                "asset": c["asset"],
                                "rejection_reason": f"lost to {winner['asset']} (edge {winner['edge']:.4f} vs {c['edge']:.4f})",
                                "spot_price": c["spot"],
                                "threshold": c["threshold"],
                                "volatility": c["blended_rv"],
                                "market_price": c["best_yes_ask"],
                                "seconds_to_close": c["seconds_to_close"],
                                "calibrated_prob": c["calibrated_prob"],
                                "edge": c["edge"],
                                "ofa_adjustment": c.get("ofa_adjustment"),
                                "raw_prob": round(c["raw_prob"], 6) if c.get("raw_prob") is not None else None,
                                "old_system_prob": c.get("old_system_prob"),
                                "egarch_sigma": c.get("egarch_sigma"),
                                "egarch_blend_sigma": c.get("egarch_blend_sigma"),
                                "egarch_blend_weight": c.get("egarch_blend_weight"),
                                "mz_r_squared": c.get("mz_r_squared"),
                                "shadow_tv_blend_rv": c.get("shadow_tv_blend_rv"),
                                "mz_sigmoid_blend_rv": c.get("mz_sigmoid_blend_rv"),
                                "mz_sigmoid_improvement": c.get("mz_sigmoid_improvement"),
                                "shadow_tv_weights": c.get("shadow_tv_weights"),
                                "egarch_n_updates": c.get("egarch_n_updates"),
                                "egarch_ratio_clamped": c.get("egarch_ratio_clamped"),
                                "counterfactual": c.get("counterfactual_json"),
                            })
                            _dedup_key = (c["ticker"], "single_asset_selection")
                            if _dedup_key not in self._eval_opp_seen:
                                self._eval_opp_seen.add(_dedup_key)
                                _ba = c["best_yes_ask"]
                                _cp = c["calibrated_prob"]
                                _fee1 = calculate_taker_fee(1, _ba)
                                _ev = (_cp * (100 - _ba)) - ((1 - _cp) * _ba) - _fee1
                                self._state.insert_evaluated_opportunity(
                                    c["ticker"], c["event_ticker"], c["asset"],
                                    "single_asset_selection",
                                    rejection_reason=f"lost to {winner['asset']}",
                                    spot_price=c["spot"], threshold=c["threshold"],
                                    volatility=c["blended_rv"], market_price=_ba,
                                    seconds_to_close=c["seconds_to_close"],
                                    calibrated_prob=_cp, edge=c["edge"],
                                    ofa_adjustment=c.get("ofa_adjustment"),
                                    strategy=c.get("strategy"),
                                    z_score=c.get("z_score"),
                                    vol_regime=c.get("vol_regime"),
                                    calibrated_prob_raw=c.get("calibrated_prob_raw"),
                                    kelly_f=c.get("kelly_f"),
                                    position_size=c.get("position_size"),
                                    breakeven_wr=_ba / 100.0,
                                    expected_value=round(_ev, 2),
                                    drawdown_scaler=c.get("drawdown_scaler"),
                                    ask_depth=c.get("ob_snapshot", {}).get("ask_depth"),
                                    best_ask_source=c.get("best_ask_source"),
                                    ofa_confidence=c.get("ofa_confidence"),
                                    raw_prob=c.get("raw_prob"),
                                    calibration_method=c.get("calibration_method"),
                                    old_system_prob=c.get("old_system_prob"),
                                    fee_adjusted_edge=c.get("fee_adjusted_edge"),
                                    egarch_sigma=c.get("egarch_sigma"),
                                    egarch_blend_sigma=c.get("egarch_blend_sigma"),
                                    egarch_blend_weight=c.get("egarch_blend_weight"),
                                    mz_r_squared=c.get("mz_r_squared"),
                                    shadow_tv_blend_rv=c.get("shadow_tv_blend_rv"),
                                    mz_shadow_sigmoid_w=c.get("mz_shadow_sigmoid_w"),
                                    mz_baseline_qlike=c.get("mz_baseline_qlike"),
                                    mz_qlike=c.get("mz_qlike"),
                                    counterfactual=c.get("counterfactual_json"),
                                    shadow_cal_prob=c.get("shadow_cal_prob"),
                                    shadow_cal_fee_edge=c.get("shadow_cal_fee_edge"),
                                    shadow_cal_temperature=c.get("shadow_cal_temperature"),
                                    oft_prob_adjustment=c.get("oft_prob_adjustment"),
                                    oft_imbalance_ratio=c.get("oft_imbalance_ratio"),
                                    oft_n_snapshots=c.get("oft_n_snapshots"),
                                    hourly_pre_temp_prob=c.get("hourly_pre_temp_prob"),
                                    hourly_applied_temp_t=c.get("hourly_applied_temp_t"),
                                    hourly_shadow_temp_2_0=c.get("hourly_shadow_temp_2_0"),
                                    hourly_shadow_temp_1_0=c.get("hourly_shadow_temp_1_0"),
                                    hourly_shadow_temp_2_5=c.get("hourly_shadow_temp_2_5"),
                                    hourly_shadow_blend_50=c.get("hourly_shadow_blend_50"),
                                    hourly_shadow_temp_1_75=c.get("hourly_shadow_temp_1_75"),
                                    hourly_shadow_temp_3_0=c.get("hourly_shadow_temp_3_0"),
                                    hourly_shadow_blend_20=c.get("hourly_shadow_blend_20"),
                                    hourly_shadow_blend_30=c.get("hourly_shadow_blend_30"),
                                    hourly_shadow_blend_60=c.get("hourly_shadow_blend_60"),
                                    hourly_post_temp_prob=c.get("hourly_post_temp_prob"))
                        except Exception:
                            logging.warning("single_asset_selection insert failed for %s", c.get("ticker"), exc_info=True)
        else:
            filtered = candidates

        # ── Per-asset selection: best strike per asset for hourly ──
        hourly_cands = [c for c in filtered if c.get("product_type") == "hourly"]
        fifteenm_cands = [c for c in filtered if c.get("product_type") != "hourly"]

        selected: List[Dict] = []

        # Hourly: best edge per asset (up to 4 simultaneous)
        hourly_by_asset: Dict[str, List[Dict]] = {}
        for c in hourly_cands:
            hourly_by_asset.setdefault(c["asset"], []).append(c)
        for asset_key, asset_cands in hourly_by_asset.items():
            selected.append(max(asset_cands, key=lambda c: c["edge"]))

        # 15M: single global best (existing behavior)
        if fifteenm_cands:
            selected.append(max(fifteenm_cands, key=lambda c: c["edge"]))

        # Decided contract overlay: add all DC candidates (already window-capped in scan)
        # Priority by payoff: lower price = higher payoff, so sort ascending by price
        _dc_candidates.sort(key=lambda c: c["best_yes_ask"])
        selected.extend(_dc_candidates)

        if not selected:
            self._last_scan_stats = scan_stats
            return None

        # Log top pick for scan journal
        best = max(selected, key=lambda c: c["edge"])
        self._logger.log_scan({
            "type": "opportunity",
            "candidates_evaluated": len(candidates),
            "candidates_after_single_asset": len(filtered),
            "selected_count": len(selected),
            "chosen_strategy": best.get("strategy"),
            **{k: v for k, v in best.items() if k not in ("strategy_scores", "ob_snapshot")},
        })
        self._last_scan_stats = scan_stats
        return selected

    # ── Price shadow processor ───────────────────────────────────────────
    def _process_price_shadow(self, queue: list) -> None:
        """Shadow-evaluate 70-85c POR rejections to collect edge data.

        Runs AFTER both scan loops complete. Entire body in try/except —
        a crash here cannot affect candidate selection or trading.
        """
        try:
            for item in queue:
                ticker = item["ticker"]
                best_ask = item["best_ask"]
                spot = item["spot"]
                threshold = item["threshold"]
                blended_rv = item["blended_rv"]
                stc = item["seconds_remaining"]
                asset = item["asset"]
                _pt = item["product_type"]

                # Re-run probability with market price (z-score sanity check)
                prob_with_market = ProbabilityEngine.compute(
                    spot, threshold, stc, blended_rv,
                    market_price_cents=best_ask,
                    asset=asset, product_type=_pt,
                )
                if not prob_with_market.get("tradeable"):
                    continue

                final_prob = prob_with_market["calibrated_prob"]
                raw_prob = prob_with_market.get("raw_prob")
                calibration_method = prob_with_market.get("calibration_method")

                # Temperature scaling (Layer 1)
                _hourly_pre_temp_prob = None
                _tempcfg = get_market_config(_pt)
                _temp_t = _tempcfg.temperature_t if _tempcfg.temperature_enabled else None
                _configured_temp_t = _temp_t  # record configured T for instrumentation (before CalEngine override)
                if _temp_t is not None and _temp_t == 1.0:
                    _temp_t = None
                _reg_engine_t = _resolve_cal_engine(_pt, asset, require_enabled=True)
                if _reg_engine_t is not None and _reg_engine_t.is_learned_method_active():
                    _temp_t = None
                    _hourly_pre_temp_prob = final_prob
                if _temp_t is not None:
                    _hourly_pre_temp_prob = final_prob
                    _p = max(0.001, min(0.999, final_prob))
                    _z = math.log(_p / (1.0 - _p))
                    final_prob = 1.0 / (1.0 + math.exp(-_z / _temp_t))

                # Dynamic cap / learned ceiling
                _dyn_cap = ProbabilityEngine._dynamic_cap(stc, product_type=_pt)
                _reg_engine_c = _resolve_cal_engine(_pt, asset, require_enabled=True)
                if _reg_engine_c is not None and _reg_engine_c.is_learned_method_active():
                    final_prob = max(0.01, min(NUMERICAL_SAFETY_CEILING, final_prob))
                else:
                    final_prob = max(0.01, min(_dyn_cap, final_prob))

                # ── Shadow instrumentation ──
                _hourly_shadow_temp_2_0 = None
                _hourly_shadow_temp_1_0 = None
                _hourly_shadow_temp_2_5 = None
                _hourly_shadow_temp_1_75 = None
                _hourly_shadow_temp_3_0 = None
                _hourly_shadow_blend_50 = None
                _hourly_shadow_blend_20 = None
                _hourly_shadow_blend_30 = None
                _hourly_shadow_blend_60 = None
                _hourly_post_temp_prob = None

                _shadow_base_ps = _hourly_pre_temp_prob
                if _shadow_base_ps is None and _pt == "spx_hourly":
                    _shadow_base_ps = prob_with_market["calibrated_prob"]
                    _hourly_pre_temp_prob = _shadow_base_ps
                    _temp_t = 1.0

                if _shadow_base_ps is not None:
                    _sp = max(0.001, min(0.999, _shadow_base_ps))
                    _sz = math.log(_sp / (1.0 - _sp))
                    _hourly_shadow_temp_2_0 = 1.0 / (1.0 + math.exp(-_sz / 2.0))
                    _hourly_shadow_temp_2_5 = 1.0 / (1.0 + math.exp(-_sz / 2.5))
                    _hourly_shadow_temp_1_75 = 1.0 / (1.0 + math.exp(-_sz / 1.75))
                    _hourly_shadow_temp_3_0 = 1.0 / (1.0 + math.exp(-_sz / 3.0))
                    if _pt == "spx_hourly":
                        _hourly_shadow_temp_1_0 = 1.0 / (1.0 + math.exp(-_sz / 1.5))
                    else:
                        _hourly_shadow_temp_1_0 = _shadow_base_ps
                    _hourly_post_temp_prob = final_prob
                    # 70-85c always < ENDGAME_BLEND_PRICE
                    _mkt_p = best_ask / 100.0
                    _hourly_shadow_blend_20 = 0.80 * final_prob + 0.20 * _mkt_p
                    _hourly_shadow_blend_30 = 0.70 * final_prob + 0.30 * _mkt_p
                    _hourly_shadow_blend_50 = 0.50 * final_prob + 0.50 * _mkt_p
                    _hourly_shadow_blend_60 = 0.40 * final_prob + 0.60 * _mkt_p

                # Market blend (70-85c always < ENDGAME_BLEND_PRICE)
                _mcfg = get_market_config(_pt)
                _effective_blend_w = _mcfg.market_blend_w
                market_implied_prob = best_ask / 100.0
                final_prob = (1.0 - _effective_blend_w) * final_prob + _effective_blend_w * market_implied_prob

                edge = final_prob - best_ask / 100.0

                # Fee-adjusted edge
                est_fee_1c = calculate_fee(1, best_ask, is_taker=True,
                                           fee_mult_taker=_mcfg.fee_multiplier_taker,
                                           fee_mult_maker=_mcfg.fee_multiplier_maker)
                fee_adjusted_edge = edge - est_fee_1c / 100.0

                # Sizing + strategy for instrumentation (all in try/except — cannot break insert)
                _ps_kelly_f = None
                _ps_position = None
                _ps_drawdown = None
                _ps_strategy = None
                _ps_ev = None
                try:
                    _ps_ev = round(
                        (final_prob * (100 - best_ask)) - ((1 - final_prob) * best_ask) - est_fee_1c, 2)
                    _ps_balance = self._get_balance_cached()
                    if _ps_balance and _ps_balance > 0:
                        _ps_sizing = self._sizer.compute(final_prob, best_ask, _ps_balance)
                        _ps_kelly_f = _ps_sizing["kelly_f"]
                        _ps_position = _ps_sizing["contracts"]
                        _ps_drawdown = _ps_sizing["drawdown_scaler"]
                        # Apply product-type Kelly fraction + risk cap
                        _ps_scfg = get_market_config(_pt)
                        if _ps_scfg.kelly_fraction < 1.0:
                            _ps_position = max(1, int(_ps_position * _ps_scfg.kelly_fraction))
                        _ps_type_max = int((_ps_balance * _ps_scfg.max_risk_per_trade) / best_ask)
                        if _ps_position > _ps_type_max:
                            _ps_position = max(1, _ps_type_max)
                        _ps_strat_data = {
                            "z_score": prob_with_market.get("z_score"),
                            "calibrated_prob": final_prob,
                            "spot": spot, "threshold": threshold,
                            "seconds_to_close": stc, "blended_rv": blended_rv,
                            "vol_regime": item["vol_regime"],
                            "best_yes_ask": best_ask,
                            "best_ask_depth": item["ask_depth"],
                            "total_ob_depth": 0,
                            "convergence_velocity": 0,
                            "edge": edge,
                            "min_entry_price": _ps_scfg.min_entry_price,
                            "max_entry_price": _ps_scfg.max_entry_price,
                        }
                        _ps_strategy, _ = evaluate_execution_strategy(_ps_strat_data)
                except Exception:
                    logging.debug("price_shadow sizing/strategy failed", exc_info=True)

                # Dedup + DB insert
                _ps_stage = "price_shadow_no_xrp" if asset != "XRP" else "price_shadow_xrp"
                _dedup_key = (ticker, _ps_stage)
                if _dedup_key in self._eval_opp_seen:
                    continue
                self._eval_opp_seen.add(_dedup_key)
                self._state.insert_evaluated_opportunity(
                    ticker, item["event_ticker"], asset,
                    _ps_stage,
                    spot_price=spot, threshold=threshold,
                    volatility=blended_rv, market_price=best_ask,
                    seconds_to_close=stc,
                    calibrated_prob=final_prob,
                    edge=edge, fee_adjusted_edge=fee_adjusted_edge,
                    z_score=prob_with_market.get("z_score"),
                    vol_regime=item["vol_regime"],
                    breakeven_wr=best_ask / 100.0,
                    calibrated_prob_raw=prob_with_market["calibrated_prob"],
                    kelly_f=_ps_kelly_f,
                    position_size=_ps_position,
                    drawdown_scaler=_ps_drawdown,
                    strategy=_ps_strategy,
                    expected_value=_ps_ev,
                    raw_prob=raw_prob,
                    calibration_method=calibration_method,
                    ask_depth=item["ask_depth"],
                    best_ask_source=item["best_ask_source"],
                    product_type=_pt,
                    hourly_pre_temp_prob=_hourly_pre_temp_prob,
                    hourly_applied_temp_t=_configured_temp_t,
                    hourly_shadow_temp_2_0=_hourly_shadow_temp_2_0,
                    hourly_shadow_temp_1_0=_hourly_shadow_temp_1_0,
                    hourly_shadow_temp_2_5=_hourly_shadow_temp_2_5,
                    hourly_shadow_blend_50=_hourly_shadow_blend_50,
                    hourly_shadow_temp_1_75=_hourly_shadow_temp_1_75,
                    hourly_shadow_temp_3_0=_hourly_shadow_temp_3_0,
                    hourly_shadow_blend_20=_hourly_shadow_blend_20,
                    hourly_shadow_blend_30=_hourly_shadow_blend_30,
                    hourly_shadow_blend_60=_hourly_shadow_blend_60,
                    hourly_post_temp_prob=_hourly_post_temp_prob,
                    **item["_oft_db"], **item["_shadow_diag"])
        except Exception:
            logging.warning("price_shadow processing error", exc_info=True)

    def _process_overnight_lp_shadow(self, queue: list) -> None:
        """Shadow-evaluate 50-85c YES contracts during overnight hours (00-12 UTC).

        Thesis: overnight market makers are slow/absent, so cheap YES contracts
        have stale pricing. The model correctly predicts 90%+ probability on
        outcomes the market only quotes at 50-60c.

        Includes vol-spike circuit breaker (2x overnight median → skip asset)
        and dual execution simulation (taker at best_ask, maker at best_bid+1).

        Shadow-only — never places orders. Entire body in try/except so
        a crash here cannot affect candidate selection or live trading.
        """
        try:
            now_ts = time.time()
            _cutoff_ts = now_ts - OVERNIGHT_LP_VOL_HISTORY_DAYS * 86400

            for item in queue:
                ticker = item["ticker"]
                best_ask = item["best_ask"]
                best_bid = item.get("best_bid")
                spot = item["spot"]
                threshold = item["threshold"]
                blended_rv = item["blended_rv"]
                stc = item["seconds_remaining"]
                asset = item["asset"]
                _pt = item["product_type"]

                # ── Vol-spike circuit breaker ──
                # Prune entries older than OVERNIGHT_LP_VOL_HISTORY_DAYS
                while (self._overnight_rv_history[asset]
                       and self._overnight_rv_history[asset][0][0] < _cutoff_ts):
                    self._overnight_rv_history[asset].popleft()
                # Compute median from PRIOR history (before appending current value,
                # so the current observation doesn't bias the median toward itself)
                _rv_vals = [rv for _, rv in self._overnight_rv_history[asset]]
                # Record current blended_rv AFTER extracting prior history
                self._overnight_rv_history[asset].append((now_ts, blended_rv))
                # Need at least 100 prior observations (~8 min of 5s ticks) before
                # the breaker activates. On cold start / night one, the breaker is
                # disabled — we want data collection, not false blocks.
                _rv_median = None
                _vol_breaker_active = len(_rv_vals) >= 100
                if _vol_breaker_active:
                    _rv_sorted = sorted(_rv_vals)
                    _rv_median = _rv_sorted[len(_rv_sorted) // 2]
                if _vol_breaker_active and blended_rv > OVERNIGHT_LP_VOL_SPIKE_MULT * _rv_median:
                    self._overnight_lp_vol_skip_count += 1
                    try:
                        self._logger.log_opportunity({
                            "filter_stage": "overnight_lp_vol_skip",
                            "ticker": ticker,
                            "asset": asset,
                            "blended_rv": round(blended_rv, 8),
                            "overnight_median_rv": round(_rv_median, 8),
                            "spike_ratio": round(blended_rv / _rv_median, 4) if _rv_median > 0 else None,
                            "threshold_mult": OVERNIGHT_LP_VOL_SPIKE_MULT,
                            "seconds_to_close": round(stc, 1),
                            "market_price": best_ask,
                        })
                    except Exception:
                        pass
                    continue

                # Re-run probability engine with market price
                prob_result = ProbabilityEngine.compute(
                    spot, threshold, stc, blended_rv,
                    market_price_cents=best_ask,
                    asset=asset, product_type=_pt,
                )
                if not prob_result.get("tradeable"):
                    continue

                final_prob = prob_result["calibrated_prob"]
                raw_prob = prob_result.get("raw_prob")
                calibration_method = prob_result.get("calibration_method")
                z_score = prob_result.get("z_score")

                # Temperature scaling (same as main pipeline)
                _tempcfg = get_market_config(_pt)
                _temp_t = _tempcfg.temperature_t if _tempcfg.temperature_enabled else None
                if _temp_t is not None and _temp_t == 1.0:
                    _temp_t = None
                _reg_engine = _resolve_cal_engine(_pt, asset, require_enabled=True)
                if _reg_engine is not None and _reg_engine.is_learned_method_active():
                    _temp_t = None
                if _temp_t is not None:
                    _p = max(0.001, min(0.999, final_prob))
                    _z = math.log(_p / (1.0 - _p))
                    final_prob = 1.0 / (1.0 + math.exp(-_z / _temp_t))

                # Dynamic cap / learned ceiling
                _dyn_cap = ProbabilityEngine._dynamic_cap(stc, product_type=_pt)
                _reg_engine_c = _resolve_cal_engine(_pt, asset, require_enabled=True)
                if _reg_engine_c is not None and _reg_engine_c.is_learned_method_active():
                    final_prob = max(0.01, min(NUMERICAL_SAFETY_CEILING, final_prob))
                else:
                    final_prob = max(0.01, min(_dyn_cap, final_prob))

                # Market blend
                _mcfg = get_market_config(_pt)
                _effective_blend_w = _mcfg.market_blend_w
                market_implied_prob = best_ask / 100.0
                final_prob = (1.0 - _effective_blend_w) * final_prob + _effective_blend_w * market_implied_prob

                # ── Min cal_prob gate ──
                if final_prob < OVERNIGHT_LP_MIN_CAL_PROB:
                    continue

                # Edge computation
                edge = final_prob - best_ask / 100.0
                est_fee_1c = calculate_fee(1, best_ask, is_taker=True,
                                           fee_mult_taker=_mcfg.fee_multiplier_taker,
                                           fee_mult_maker=_mcfg.fee_multiplier_maker)
                fee_adjusted_edge = edge - est_fee_1c / 100.0

                # ── Min edge gate ──
                if fee_adjusted_edge < OVERNIGHT_LP_MIN_EDGE_PCT:
                    continue

                # ── Sizing (taker simulation — primary metric) ──
                _olp_kelly_f = None
                _olp_position_taker = None
                _olp_ev_taker = None
                _olp_drawdown = None
                _olp_balance = self._get_balance_cached()
                if _olp_balance and _olp_balance > 0:
                    _olp_sizing = self._sizer.compute(final_prob, best_ask, _olp_balance)
                    _olp_kelly_f = _olp_sizing["kelly_f"]
                    _olp_position_taker = _olp_sizing["contracts"]
                    _olp_drawdown = _olp_sizing["drawdown_scaler"]
                    # Apply overnight LP Kelly fraction + risk cap
                    if OVERNIGHT_LP_KELLY_FRACTION < 1.0:
                        _olp_position_taker = max(1, int(_olp_position_taker * OVERNIGHT_LP_KELLY_FRACTION))
                    _olp_type_max = int((_olp_balance * OVERNIGHT_LP_MAX_RISK_PER_TRADE) / best_ask)
                    if _olp_position_taker > _olp_type_max:
                        _olp_position_taker = max(1, _olp_type_max)
                    _olp_ev_taker = round(
                        (final_prob * (100 - best_ask)) - ((1 - final_prob) * best_ask) - est_fee_1c, 2)

                # ── Maker simulation ──
                # Post at best_bid + 1c (penny improvement)
                _olp_maker_price = None
                _olp_position_maker = None
                _olp_ev_maker = None
                _olp_maker_fee_adj_edge = None
                if best_bid is not None and best_bid > 0:
                    _olp_maker_price = best_bid + 1
                    if _olp_maker_price <= best_ask:  # sanity: maker must be below ask
                        _maker_edge = final_prob - _olp_maker_price / 100.0
                        _maker_fee_1c = calculate_fee(1, _olp_maker_price, is_taker=False,
                                                      fee_mult_taker=_mcfg.fee_multiplier_taker,
                                                      fee_mult_maker=_mcfg.fee_multiplier_maker)
                        _olp_maker_fee_adj_edge = _maker_edge - _maker_fee_1c / 100.0
                        if _olp_balance and _olp_balance > 0:
                            _m_sizing = self._sizer.compute(final_prob, _olp_maker_price, _olp_balance)
                            _olp_position_maker = _m_sizing["contracts"]
                            if OVERNIGHT_LP_KELLY_FRACTION < 1.0:
                                _olp_position_maker = max(1, int(_olp_position_maker * OVERNIGHT_LP_KELLY_FRACTION))
                            _m_type_max = int((_olp_balance * OVERNIGHT_LP_MAX_RISK_PER_TRADE) / _olp_maker_price)
                            if _olp_position_maker > _m_type_max:
                                _olp_position_maker = max(1, _m_type_max)
                            _olp_ev_maker = round(
                                (final_prob * (100 - _olp_maker_price)) -
                                ((1 - final_prob) * _olp_maker_price) - _maker_fee_1c, 2)
                    else:
                        _olp_maker_price = None  # bid+1 crossed the ask — no valid maker price

                # ── JSONL log ──
                try:
                    self._logger.log_opportunity({
                        "filter_stage": "overnight_lp_shadow",
                        "ticker": ticker,
                        "event_ticker": item["event_ticker"],
                        "asset": asset,
                        "side": "yes",
                        "market_price": best_ask,
                        "model_prob": round(final_prob, 6),
                        "edge": round(edge, 6),
                        "fee_adjusted_edge": round(fee_adjusted_edge, 6),
                        "kelly_f": round(_olp_kelly_f, 6) if _olp_kelly_f else None,
                        "taker_position_size": _olp_position_taker,
                        "taker_ev": _olp_ev_taker,
                        "maker_price": _olp_maker_price,
                        "maker_position_size": _olp_position_maker,
                        "maker_ev": _olp_ev_maker,
                        "maker_fee_adj_edge": round(_olp_maker_fee_adj_edge, 6) if _olp_maker_fee_adj_edge else None,
                        "best_bid": best_bid,
                        "seconds_to_close": round(stc, 1),
                        "spot_price": spot,
                        "threshold": threshold,
                        "volatility": blended_rv,
                        "vol_regime": item["vol_regime"],
                        "overnight_median_rv": round(_rv_median, 8) if _rv_median is not None else None,
                        "vol_spike_ratio": round(blended_rv / _rv_median, 4) if _rv_median and _rv_median > 0 else None,
                        "vol_breaker_active": _vol_breaker_active,
                        "raw_prob": round(raw_prob, 6) if raw_prob is not None else None,
                        "z_score": z_score,
                        "ask_depth": item["ask_depth"],
                    })
                except Exception:
                    logging.debug("overnight_lp_shadow log failed", exc_info=True)

                # ── DB insert (taker simulation as primary) ──
                _dedup_key = (ticker, "overnight_lp_shadow")
                if _dedup_key in self._eval_opp_seen:
                    continue
                self._eval_opp_seen.add(_dedup_key)
                try:
                    self._state.insert_evaluated_opportunity(
                        ticker, item["event_ticker"], asset,
                        "overnight_lp_shadow",
                        rejection_reason=(
                            f"shadow: cal_prob {final_prob:.4f} >= {OVERNIGHT_LP_MIN_CAL_PROB}, "
                            f"fee_adj_edge {fee_adjusted_edge:.4f} >= {OVERNIGHT_LP_MIN_EDGE_PCT}, "
                            f"taker@{best_ask}c maker@{_olp_maker_price}c"
                        ),
                        spot_price=spot, threshold=threshold,
                        volatility=blended_rv, market_price=best_ask,
                        seconds_to_close=stc,
                        calibrated_prob=final_prob, edge=edge,
                        fee_adjusted_edge=fee_adjusted_edge,
                        ofa_adjustment=None,
                        z_score=z_score,
                        vol_regime=item["vol_regime"],
                        calibrated_prob_raw=prob_result["calibrated_prob"],
                        kelly_f=_olp_kelly_f,
                        position_size=_olp_position_taker,
                        drawdown_scaler=_olp_drawdown,
                        breakeven_wr=best_ask / 100.0,
                        expected_value=_olp_ev_taker,
                        ask_depth=item["ask_depth"],
                        best_ask_source=item["best_ask_source"],
                        raw_prob=raw_prob,
                        calibration_method=calibration_method,
                        product_type=_pt,
                        **item["_oft_db"], **item["_shadow_diag"])
                except Exception:
                    logging.warning("insert_evaluated_opportunity failed (overnight_lp_shadow)", exc_info=True)
        except Exception:
            logging.warning("overnight_lp_shadow processing error", exc_info=True)

    def _process_no_side_shadow(self, queue: list) -> None:
        """Shadow-evaluate NO-side (buy NO contract) for all queued markets.

        Mirrors the YES-side evaluation: NO_prob = 1 - YES_prob,
        NO_ask from market NBBO. Runs through the same filter pipeline
        (price, edge, sizing) and logs to evaluated_opportunities with side='no'.

        Shadow-only — never places orders. Entire body in try/except so
        a crash here cannot affect candidate selection or live trading.
        """
        try:
            for item in queue:
                ticker = item["ticker"]
                best_ask = item["best_ask"]
                asset = item["asset"]
                _pt = item["product_type"]
                spot = item["spot"]
                threshold = item["threshold"]
                blended_rv = item["blended_rv"]
                stc = item["seconds_remaining"]

                # ── Compute YES-side final_prob if not pre-computed ──
                # (POR entries don't have final_prob yet — need temp+cap+blend)
                if item["final_prob"] is not None:
                    yes_final_prob = item["final_prob"]
                    raw_prob = item["raw_prob"]
                    calibration_method = item["calibration_method"]
                else:
                    # Re-run probability engine (same as _process_price_shadow)
                    prob_result = ProbabilityEngine.compute(
                        spot, threshold, stc, blended_rv,
                        market_price_cents=best_ask,
                        asset=asset, product_type=_pt,
                    )
                    if not prob_result.get("tradeable"):
                        continue
                    yes_final_prob = prob_result["calibrated_prob"]
                    raw_prob = prob_result.get("raw_prob")
                    calibration_method = prob_result.get("calibration_method")

                    # Temperature scaling (Layer 1)
                    _tempcfg = get_market_config(_pt)
                    _temp_t = _tempcfg.temperature_t if _tempcfg.temperature_enabled else None
                    if _temp_t is not None and _temp_t == 1.0:
                        _temp_t = None
                    _reg_engine = _resolve_cal_engine(_pt, asset, require_enabled=True)
                    if _reg_engine is not None and _reg_engine.is_learned_method_active():
                        _temp_t = None
                    if _temp_t is not None:
                        _p = max(0.001, min(0.999, yes_final_prob))
                        _z = math.log(_p / (1.0 - _p))
                        yes_final_prob = 1.0 / (1.0 + math.exp(-_z / _temp_t))

                    # Dynamic cap / learned ceiling
                    _dyn_cap = ProbabilityEngine._dynamic_cap(stc, product_type=_pt)
                    _reg_engine_c = _resolve_cal_engine(_pt, asset, require_enabled=True)
                    if _reg_engine_c is not None and _reg_engine_c.is_learned_method_active():
                        yes_final_prob = max(0.01, min(NUMERICAL_SAFETY_CEILING, yes_final_prob))
                    else:
                        yes_final_prob = max(0.01, min(_dyn_cap, yes_final_prob))

                    # Market blend
                    _mcfg = get_market_config(_pt)
                    if best_ask < ENDGAME_BLEND_PRICE:
                        mip = best_ask / 100.0
                        yes_final_prob = (1.0 - _mcfg.market_blend_w) * yes_final_prob + _mcfg.market_blend_w * mip

                # ── NO-side computation ──
                no_prob = 1.0 - yes_final_prob
                no_price = item["no_ask"]  # actual NO ask from market NBBO

                # Price filter for NO side (use lower floor for 15M shadow collection)
                _ncfg = get_market_config(_pt)
                _no_price_floor = NO_SIDE_MIN_ENTRY_PRICE  # universal floor for NO-side shadow collection
                if not (_no_price_floor <= no_price <= _ncfg.max_entry_price):
                    # NO price out of range — skip (don't log; too much volume for OOR)
                    continue

                # Edge computation
                no_edge = no_prob - no_price / 100.0
                no_fee_1c = calculate_fee(1, no_price, is_taker=True,
                                          fee_mult_taker=_ncfg.fee_multiplier_taker,
                                          fee_mult_maker=_ncfg.fee_multiplier_maker)
                no_fee_adj_edge = no_edge - no_fee_1c / 100.0

                # Edge filter (same price-dependent schedule, applied to NO price)
                if _pt == "weather":
                    _no_min_edge = WEATHER_MIN_EDGE_PCT
                elif _pt == "hourly":
                    _no_min_edge = HOURLY_MIN_EDGE_PCT
                else:
                    _no_min_edge = get_min_edge(no_price)

                # Determine filter stage
                _no_kelly_f = None
                _no_position = None
                _no_drawdown = None
                _no_ev = None
                if no_fee_adj_edge < _no_min_edge:
                    _no_filter_stage = "insufficient_edge"
                    _no_rej = f"NO net_edge {no_fee_adj_edge:.4f} < min {_no_min_edge:.4f} @{no_price}c"
                else:
                    # Compute sizing for instrumentation
                    _no_ev = round(
                        (no_prob * (100 - no_price)) - ((1 - no_prob) * no_price) - no_fee_1c, 2)
                    try:
                        _no_balance = self._get_balance_cached()
                        if _no_balance and _no_balance > 0:
                            _no_sizing = self._sizer.compute(no_prob, no_price, _no_balance)
                            _no_kelly_f = _no_sizing["kelly_f"]
                            _no_position = _no_sizing["contracts"]
                            _no_drawdown = _no_sizing["drawdown_scaler"]
                            if _ncfg.kelly_fraction < 1.0:
                                _no_position = max(1, int(_no_position * _ncfg.kelly_fraction))
                            _no_type_max = int((_no_balance * _ncfg.max_risk_per_trade) / no_price)
                            if _no_position > _no_type_max:
                                _no_position = max(1, _no_type_max)
                    except Exception:
                        logging.warning("no_side sizing failed", exc_info=True)

                    if _no_position is not None and _no_position <= 0:
                        _no_filter_stage = "zero_sizing"
                        _no_rej = "NO sizing yielded 0 contracts"
                    else:
                        # Passed all filters — assign appropriate shadow filter_stage
                        # Mirror YES-side taxonomy with no_side_ prefix
                        if _ncfg.observation_only and _ncfg.observation_filter_label:
                            _no_filter_stage = _ncfg.observation_filter_label
                        elif _pt in (None, "15m"):
                            _is_no_price_shadow = no_price < _ncfg.min_entry_price
                            if _is_no_price_shadow:
                                # NO price in shadow zone (70-85c) — mirrors YES price_shadow
                                _no_filter_stage = "no_side_price_shadow_no_xrp" if asset != "XRP" else "no_side_price_shadow_xrp"
                            elif stc > STC_SHADOW_THRESHOLD:
                                _no_filter_stage = "no_side_stc_shadow_no_xrp" if asset != "XRP" else "no_side_stc_shadow_xrp"
                            elif XRP_15M_SHADOW and asset == "XRP":
                                _no_filter_stage = "no_side_xrp_shadow"
                            else:
                                _no_filter_stage = "no_side_shadow"
                        else:
                            _no_filter_stage = "no_side_shadow"
                        _no_rej = None

                # Dedup + DB insert
                _dedup_key = (ticker, _no_filter_stage, "no")
                if _dedup_key in self._eval_opp_seen:
                    continue
                self._eval_opp_seen.add(_dedup_key)

                _sx = item.get("_shadow_extra", {})
                # Debug: warn if weather NO-side is missing ensemble data
                if _pt == "weather" and not _sx.get("wx_ensemble_mean"):
                    logging.warning(
                        "NO_SIDE_DIAG: weather %s missing wx_ensemble_mean in _shadow_extra, keys=%s",
                        ticker, list(_sx.keys()))
                # Hourly temperature fields (passed through queue)
                _no_hourly_pre = item.get("hourly_pre_temp_prob")
                _no_hourly_t = item.get("hourly_applied_temp_t")
                _no_hourly_post = item.get("hourly_post_temp_prob")
                self._state.insert_evaluated_opportunity(
                    ticker, item["event_ticker"], asset,
                    _no_filter_stage,
                    rejection_reason=_no_rej,
                    spot_price=spot, threshold=threshold,
                    volatility=blended_rv, market_price=no_price,
                    seconds_to_close=stc,
                    calibrated_prob=no_prob,
                    edge=no_edge,
                    fee_adjusted_edge=no_fee_adj_edge,
                    z_score=None,  # z-score is YES-side concept
                    vol_regime=item["vol_regime"],
                    raw_prob=1.0 - raw_prob if raw_prob is not None else None,
                    calibration_method=calibration_method,
                    breakeven_wr=no_price / 100.0,
                    expected_value=_no_ev,
                    kelly_f=_no_kelly_f,
                    position_size=_no_position,
                    drawdown_scaler=_no_drawdown,
                    ask_depth=item["ask_depth"],
                    best_ask_source=item["best_ask_source"],
                    product_type=_pt,
                    side="no",
                    wx_ensemble_mean=_sx.get("wx_ensemble_mean"),
                    wx_ensemble_std=_sx.get("wx_ensemble_std"),
                    wx_bias_correction=_sx.get("wx_bias_correction"),
                    wx_n_members=_sx.get("wx_n_members"),
                    wx_market_type=_sx.get("wx_market_type"),
                    wx_hrrr_temp=_sx.get("wx_hrrr_temp"),
                    wx_corrected_mean=_sx.get("wx_corrected_mean"),
                    wx_no_side_edge=_sx.get("wx_no_side_edge"),
                    hourly_pre_temp_prob=_no_hourly_pre,
                    hourly_applied_temp_t=_no_hourly_t,
                    hourly_post_temp_prob=_no_hourly_post,
                    **item["_oft_db"], **item["_shadow_diag"])
        except Exception:
            logging.warning("no_side_shadow processing error", exc_info=True)

    # ── Threshold parsing ─────────────────────────────────────────────────

    @staticmethod
    def _parse_threshold(market: Dict) -> Optional[float]:
        """Extract strike/threshold price from market data.

        Priority:
        1. floor_strike field (most reliable for strike-level markets)
        2. yes_sub_title "Price to beat: $X,XXX.XX" (15-minute up/down markets)
        3. Ticker pattern: KXBTC-...-B95000 -> 95000.0 (strike-level markets)
        4. Subtitle regex fallback: "above $95,000.00" -> 95000.0
        """
        # 1. floor_strike field
        floor_strike = market.get("floor_strike")
        if floor_strike is not None:
            try:
                val = float(floor_strike)
                if val > 0:
                    return val
            except (ValueError, TypeError):
                pass

        # 2. yes_sub_title: "Price to beat: $68,500.00" (15M markets)
        for field in ("yes_sub_title", "no_sub_title"):
            sub = market.get(field, "") or ""
            if "Price to beat" in sub and "TBD" not in sub:
                m = re.search(r"\$([0-9,]+\.?\d*)", sub)
                if m:
                    try:
                        return float(m.group(1).replace(",", ""))
                    except ValueError:
                        pass

        # 3. Ticker pattern: KXBTC-26FEB2114-B95000 (strike-level markets)
        ticker = market.get("ticker", "")
        parts = ticker.split("-")
        if len(parts) >= 3:
            strike_part = parts[-1]
            if strike_part.startswith("B") or strike_part.startswith("T"):
                try:
                    return float(strike_part[1:])
                except ValueError:
                    pass

        # 4. Subtitle regex: "above $95,000.00" or "above $0.55"
        subtitle = market.get("subtitle", "") or ""
        if subtitle:
            m = re.search(r"\$([0-9,]+\.?\d*)", subtitle)
            if m:
                try:
                    return float(m.group(1).replace(",", ""))
                except ValueError:
                    pass

        return None

    @staticmethod
    def _parse_weather_market_info(market: Dict):
        """Parse weather market type and bracket bounds from market dict.

        Returns (market_type, lower_bound, upper_bound) or None.
        market_type: "bracket" | "lower_tail" | "upper_tail"

        Detection uses floor_strike/cap_strike fields first, ticker prefix as fallback.
        Bracket (B-prefix): P(lower < X < upper)
        Tail (T-prefix): lower_tail if only cap_strike, upper_tail if only floor_strike
        """
        ticker = market.get("ticker", "")
        floor_strike = market.get("floor_strike")
        cap_strike = market.get("cap_strike")

        # Convert to float if present
        floor_val = None
        cap_val = None
        try:
            if floor_strike is not None:
                floor_val = float(floor_strike)
        except (ValueError, TypeError):
            pass
        try:
            if cap_strike is not None:
                cap_val = float(cap_strike)
        except (ValueError, TypeError):
            pass

        # Determine market type from ticker prefix
        parts = ticker.split("-")
        strike_part = parts[-1] if len(parts) >= 3 else ""

        if strike_part.startswith("B"):
            # Bracket market
            if floor_val is not None and cap_val is not None:
                return ("bracket", floor_val, cap_val)
            # Fallback: B{upper}, infer bounds from available data
            try:
                upper = float(strike_part[1:])
            except ValueError:
                return None
            lower = floor_val if floor_val is not None else upper - 2.0
            upper = cap_val if cap_val is not None else upper
            return ("bracket", lower, upper)

        elif strike_part.startswith("T"):
            try:
                t_val = float(strike_part[1:])
            except ValueError:
                return None
            # Use floor_strike/cap_strike to disambiguate tail direction
            # Lower tail (P(X < threshold)): cap_strike present, no floor
            # Upper tail (P(X > threshold)): floor_strike present, no cap
            if cap_val is not None and floor_val is None:
                return ("lower_tail", None, cap_val)
            if floor_val is not None and cap_val is None:
                return ("upper_tail", floor_val, None)
            # Fallback: use subtitle text
            subtitle = (market.get("subtitle") or "").lower()
            if "below" in subtitle or "under" in subtitle:
                return ("lower_tail", None, t_val)
            if "above" in subtitle or "over" in subtitle:
                return ("upper_tail", t_val, None)
            # Last resort: treat as upper tail (legacy behavior)
            return ("upper_tail", t_val, None)

        return None

    # ── Orderbook helpers ─────────────────────────────────────────────────

    @staticmethod
    def _best_yes_ask_cents(ob_data: Dict) -> Optional[int]:
        """Compute best YES ask = 100 - highest NO bid.

        Handles both legacy cents format and floating-point dollar format.
        """
        no_bids = ob_data.get("no", [])
        if not no_bids:
            return None

        # Each bid is [price, quantity]. Find highest NO bid price.
        best_no_bid = None
        for entry in no_bids:
            if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                price = entry[0]
            elif isinstance(entry, dict):
                price = entry.get("price", 0)
            else:
                continue

            # Handle FP dollar format (e.g. 0.15) vs cents format (e.g. 15)
            if isinstance(price, float) and price < 1.0:
                price_cents = round(price * 100)
            else:
                price_cents = int(price)

            if best_no_bid is None or price_cents > best_no_bid:
                best_no_bid = price_cents

        if best_no_bid is None or best_no_bid <= 0:
            return None

        return 100 - best_no_bid

    def _get_orderbook_cached(self, ticker: str) -> Tuple[Optional[Dict], bool]:
        """Return (orderbook_data, was_fresh_fetch). Uses TTL cache.

        Prefers real-time WS orderbook when available (zero API cost),
        falls back to REST fetch if WS data is missing or stale.
        """
        now = time.time()

        # Skip WS subscription for hourly tickers — too many strikes (75/asset),
        # they never get unsubscribed properly, and pollute the dashboard.
        _hourly_prefixes = tuple(HOURLY_SERIES_TICKERS.values())
        is_hourly = ticker.startswith(_hourly_prefixes)

        # Try WS orderbook first (free, real-time) — 15M only
        if self._kalshi_feed and self._kalshi_feed.is_connected and not is_hourly:
            ws_ob = self._kalshi_feed.get_orderbook(ticker)
            if ws_ob and now - ws_ob.get("ts", 0) < ORDERBOOK_CACHE_TTL * 2:
                # Subscribe if not already (ensures future deltas flow)
                self._kalshi_feed.subscribe_ticker(ticker)
                self._ob_cache[ticker] = (ws_ob, now)
                return (ws_ob, False)
            # No WS data yet — subscribe so it arrives for next scan
            self._kalshi_feed.subscribe_ticker(ticker)

        cached = self._ob_cache.get(ticker)
        if cached:
            data, fetch_time = cached
            if now - fetch_time < ORDERBOOK_CACHE_TTL:
                return (data, False)

        # REST fallback
        ob_data = self._client.get_orderbook(ticker, depth=5)
        # Prefer orderbook_fp (new FP format), fall back to orderbook (legacy)
        orderbook_fp = ob_data.get("orderbook_fp") if ob_data else None
        if orderbook_fp:
            orderbook = self._convert_orderbook_fp(orderbook_fp)
        else:
            orderbook = ob_data.get("orderbook", ob_data) if ob_data else None
        self._ob_cache[ticker] = (orderbook, now)
        return (orderbook, True)

    def _scanner_convergence_velocity(self, ticker: str) -> float:
        """Upward ask movement in cents over convergence window, from scan history."""
        history = self._ticker_ask_history.get(ticker)
        if not history or len(history) < 2:
            return 0.0
        now = time.time()
        cutoff = now - CONVERGENCE_WINDOW_SECONDS
        oldest_price = None
        for ts, price in history:
            if ts >= cutoff:
                oldest_price = price
                break
        if oldest_price is None:
            return 0.0
        return history[-1][1] - oldest_price

    @staticmethod
    def _convert_orderbook_fp(ob_fp: Dict) -> Dict:
        """Convert orderbook_fp format to internal cents format.

        Input:  {"no_dollars": [["0.1100", "205.00"], ...], "yes_dollars": [...]}
        Output: {"no": [[11, 205], ...], "yes": [[77, 200], ...]}
        """
        result = {}
        for side in ("yes", "no"):
            entries = ob_fp.get(f"{side}_dollars") or []
            converted = []
            for entry in entries:
                if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                    price_cents = round(float(entry[0]) * 100)
                    count = int(round(float(entry[1])))
                    converted.append([price_cents, count])
            result[side] = converted
        return result

    # ── Timeslot helpers ──────────────────────────────────────────────────

    @staticmethod
    def _window_timeslot(event_ticker: str) -> str:
        """Extract timeslot from event ticker.

        'KXBTC15M-26FEB211545' -> '26FEB211545'
        (same across assets: KXETH15M-26FEB211545 also gives '26FEB211545')
        """
        parts = event_ticker.split("-")
        if len(parts) >= 2:
            return parts[1]
        return event_ticker

    def _get_occupied_timeslots(self) -> Dict[str, set]:
        """Return {timeslot: set(assets)} for timeslots with open positions or resting orders."""
        occupied: Dict[str, set] = {}

        for pos in self._state.get_open_positions():
            et = pos.get("event_ticker", "")
            asset = pos.get("asset", "")
            ts = self._window_timeslot(et)
            if ts:
                occupied.setdefault(ts, set()).add(asset)

        for order in self._state.get_resting_orders():
            et = order.get("event_ticker", "")
            asset = order.get("asset", "")
            ts = self._window_timeslot(et)
            if ts:
                occupied.setdefault(ts, set()).add(asset)

        return occupied

    # ── Balance ───────────────────────────────────────────────────────────

    def _get_balance_cached(self) -> Optional[int]:
        """Get balance in cents, cached for BALANCE_CACHE_TTL seconds."""
        now = time.time()
        cached_balance, fetch_time = self._balance_cache
        if cached_balance is not None and now - fetch_time < BALANCE_CACHE_TTL:
            return cached_balance

        resp = self._client.get_balance()
        if resp is None:
            return cached_balance  # return stale if API fails
        balance = resp.get("balance") or 0
        self._balance_cache = (balance, now)
        return balance


# ═════════════════════════════════════════════════════════════════════════════
#  OrderExecutor
# ═════════════════════════════════════════════════════════════════════════════

class OrderExecutor:
    """Maker-first executor with adaptive taker escalation.

    Always enters via a maker limit order (1-2¢ below fair value).
    tick() polls for fills and, if unfilled, escalates to a taker order
    after an urgency-based wait window:
      - 60-300s to close → wait 15s
      - 30-60s  to close → wait 10s
      - <30s    to close → wait 5s

    On escalation: cancel maker, re-fetch orderbook, validate price
    is in [MIN_ENTRY_PRICE, ESCALATION_MAX_ENTRY], and submit taker.

    UUID client_order_id, persist to SQLite before submission,
    log fills to trade_journal.jsonl, record position on fill.
    """

    def __init__(self, client: KalshiClient, state: StateManager,
                 logger: Logger, main_loop=None, kalshi_feed=None):
        self._client = client
        self._state = state
        self._logger = logger
        self._ml = main_loop
        self._kalshi_feed = kalshi_feed
        self._active_orders: Dict[str, Dict] = {}  # asset → order dict
        self._recent_taker_tickers: Dict[str, float] = {}  # ticker → timestamp (cooldown after IOC)
        # Session counters for execution engine stats
        self._session_amend_attempts: int = 0
        self._session_amend_successes: int = 0
        self._session_ioc_fills: int = 0
        self._session_ioc_unfilled: int = 0
        self._session_ws_fills: int = 0
        self._session_rest_fills: int = 0
        self._session_post_only_rejections: int = 0
        # Post-only rejection → taker escalation tracking
        self._post_only_rejections: Dict[str, Tuple[int, float]] = {}  # ticker → (count, first_rejection_ts)
        self._session_post_only_degraded_attempts: int = 0
        self._session_post_only_taker_escalations: int = 0
        self._session_post_only_taker_fills: int = 0
        # Direct taker counters (for <60s candidates)
        self._session_direct_taker_attempts: int = 0
        self._session_direct_taker_fills: int = 0
        self._session_direct_taker_unfilled: int = 0
        self._session_direct_taker_skipped: int = 0
        # Confirmation addon state
        self._addon_eligible: Dict[str, Dict] = {}   # ticker → metadata
        self._addon_completed: set = set()            # tickers already addon'd
        self._session_addon_attempts: int = 0
        self._session_addon_fills: int = 0
        self._session_addon_unfilled: int = 0
        self._session_addon_skipped: int = 0
        # Dip addon state
        self._dip_addon_completed: set = set()         # tickers already dip-addon'd
        self._session_dip_addon_attempts: int = 0
        self._session_dip_addon_fills: int = 0
        self._session_dip_addon_shadow: int = 0
        self._session_dip_addon_skipped: int = 0
        self._escalating_assets: set = set()  # Fix 5: guard against re-entry during escalation
        self._kalshi_oft = None  # populated from scanner if available
        # SOL Path C shadow: pending observations {ticker → dict}
        self._sol_pathc_pending: Dict[str, Dict] = {}

    @property
    def _active_order(self) -> Optional[Dict]:
        """Backwards compat for dashboard_snapshot.py."""
        if not self._active_orders:
            return None
        return next(iter(self._active_orders.values()))

    @property
    def has_active_order(self) -> bool:
        return len(self._active_orders) > 0

    # ── Post-only rejection tracking ────────────────────────────────────

    def _get_post_only_rejection_count(self, ticker: str) -> int:
        """Get active rejection count for ticker. Returns 0 if expired or missing."""
        entry = self._post_only_rejections.get(ticker)
        if entry is None:
            return 0
        count, first_ts = entry
        if time.time() - first_ts > POST_ONLY_REJECTION_EXPIRY:
            self._post_only_rejections.pop(ticker, None)
            return 0
        return count

    def _record_post_only_rejection(self, ticker: str):
        """Increment rejection count for ticker. Starts fresh if expired."""
        now = time.time()
        entry = self._post_only_rejections.get(ticker)
        if entry is None or (now - entry[1] > POST_ONLY_REJECTION_EXPIRY):
            self._post_only_rejections[ticker] = (1, now)
        else:
            self._post_only_rejections[ticker] = (entry[0] + 1, entry[1])

    # ── Public interface ──────────────────────────────────────────────────

    def execute(self, candidate: Dict) -> Optional[Dict]:
        """Always submit maker order. Escalation to taker happens in tick()."""
        # Observation safety belt — should never reach here for obs-only types
        # Exception: weather NO-side bypasses observation_only when WEATHER_NO_SIDE_LIVE=True
        _exec_cfg = get_market_config(candidate.get("product_type"))
        if _exec_cfg.observation_only:
            _is_weather_no_live = (candidate.get("product_type") == "weather"
                                  and candidate.get("side") == "no"
                                  and WEATHER_NO_SIDE_LIVE)
            if not _is_weather_no_live:
                logging.error("SAFETY: %s candidate reached execute() — should never happen. Ticker=%s",
                              _exec_cfg.product_type, candidate.get("ticker"))
                return None

        asset = candidate["asset"]
        if asset in self._active_orders or asset in self._escalating_assets:
            return None

        # Cooldown: skip tickers recently attempted via synchronous IOC
        ticker = candidate["ticker"]
        cooldown_ts = self._recent_taker_tickers.get(ticker)
        if cooldown_ts is not None:
            if time.time() - cooldown_ts < 60:
                return None
            del self._recent_taker_tickers[ticker]

        if OBSERVATION_MODE:
            logging.info(
                f"OBSERVATION MODE: Would place maker for {candidate['ticker']} "
                f"at {candidate.get('best_yes_ask', '?')}¢ for "
                f"{candidate.get('position_size', '?')} contracts"
            )
            try:
                _fv = candidate.get("best_yes_ask")
                _obs_offset = (MAKER_PRICE_OFFSET if _fv and _fv >= 90
                               else MAKER_PRICE_OFFSET + 1) if _fv else MAKER_PRICE_OFFSET
                self._logger.log_execution({
                    "action": "observation_would_trade",
                    "ticker": candidate["ticker"],
                    "asset": candidate["asset"],
                    "event_ticker": candidate["event_ticker"],
                    "best_yes_ask": candidate.get("best_yes_ask"),
                    "position_size": candidate.get("position_size"),
                    "edge": candidate.get("edge"),
                    "calibrated_prob": candidate.get("calibrated_prob"),
                    "strategy": candidate.get("strategy"),
                    "seconds_to_close": candidate.get("seconds_to_close"),
                    "vol_regime": candidate.get("vol_regime"),
                    "ofa_adjustment": candidate.get("ofa_adjustment"),
                    "balance_at_scan": candidate.get("balance_at_scan"),
                    "execution_params": {
                        "maker_price": (_fv - _obs_offset) if _fv else None,
                        "maker_offset": _obs_offset,
                        "post_only": True,
                        "escalation_strategy": "cancel_replace_ioc",
                        "taker_time_in_force": "ioc",
                    },
                })
                if _TELEGRAM:
                    _ba = candidate.get("best_yes_ask", "?")
                    _edge = candidate.get("edge")
                    _prob = candidate.get("calibrated_prob")
                    _sz = candidate.get("position_size", "?")
                    _asset = candidate.get("asset", "?")
                    _edge_s = f"{_edge:.1%}" if _edge is not None else "?"
                    _prob_s = f"{_prob:.0%}" if _prob is not None else "?"
                    _cost = (_ba * _sz / 100) if isinstance(_ba, (int, float)) and isinstance(_sz, (int, float)) else 0
                    _TELEGRAM.send(
                        f"\U0001f4ca {_asset} {_sz}ct @ {_ba}c "
                        f"(${_cost:.2f}) edge={_edge_s} prob={_prob_s}",
                        dedup_key=candidate["ticker"],
                    )
                if not hasattr(self, '_last_obs_ticker') or self._last_obs_ticker != candidate['ticker']:
                    self._last_obs_ticker = candidate['ticker']
                    _ba = candidate.get("best_yes_ask")
                    _cp = candidate.get("calibrated_prob")
                    _fee1 = calculate_taker_fee(1, _ba) if _ba else 0
                    _ev = (_cp * (100 - _ba)) - ((1 - _cp) * _ba) - _fee1 if (_ba and _cp) else None
                    self._state.insert_evaluated_opportunity(
                        candidate["ticker"], candidate["event_ticker"],
                        candidate["asset"], "observation_trade",
                        spot_price=candidate.get("spot"),
                        threshold=candidate.get("threshold"),
                        volatility=candidate.get("blended_rv"),
                        market_price=_ba,
                        seconds_to_close=candidate.get("seconds_to_close"),
                        calibrated_prob=_cp,
                        edge=candidate.get("edge"),
                        ofa_adjustment=candidate.get("ofa_adjustment"),
                        strategy=candidate.get("strategy"),
                        position_size=candidate.get("position_size"),
                        kelly_f=candidate.get("kelly_f"),
                        z_score=candidate.get("z_score"),
                        vol_regime=candidate.get("vol_regime"),
                        calibrated_prob_raw=candidate.get("calibrated_prob_raw"),
                        breakeven_wr=_ba / 100.0 if _ba else None,
                        expected_value=round(_ev, 2) if _ev is not None else None,
                        drawdown_scaler=candidate.get("drawdown_scaler"),
                        ask_depth=candidate.get("ob_snapshot", {}).get("ask_depth"),
                        best_ask_source=candidate.get("best_ask_source"),
                        ofa_confidence=candidate.get("ofa_confidence"),
                        raw_prob=candidate.get("raw_prob"),
                        calibration_method=candidate.get("calibration_method"),
                        old_system_prob=candidate.get("old_system_prob"),
                        fee_adjusted_edge=candidate.get("fee_adjusted_edge"),
                        egarch_sigma=candidate.get("egarch_sigma"),
                        egarch_blend_sigma=candidate.get("egarch_blend_sigma"),
                        egarch_blend_weight=candidate.get("egarch_blend_weight"),
                        mz_r_squared=candidate.get("mz_r_squared"),
                        shadow_tv_blend_rv=candidate.get("shadow_tv_blend_rv"),
                        mz_shadow_sigmoid_w=candidate.get("mz_shadow_sigmoid_w"),
                        mz_baseline_qlike=candidate.get("mz_baseline_qlike"),
                        mz_qlike=candidate.get("mz_qlike"),
                        counterfactual=candidate.get("counterfactual_json"),
                        shadow_cal_prob=candidate.get("shadow_cal_prob"),
                        shadow_cal_fee_edge=candidate.get("shadow_cal_fee_edge"),
                        shadow_cal_temperature=candidate.get("shadow_cal_temperature"),
                        oft_prob_adjustment=candidate.get("oft_prob_adjustment"),
                        oft_imbalance_ratio=candidate.get("oft_imbalance_ratio"),
                        oft_n_snapshots=candidate.get("oft_n_snapshots"),
                        product_type=candidate.get("product_type"),
                        wx_ensemble_mean=candidate.get("wx_ensemble_mean"),
                        wx_ensemble_std=candidate.get("wx_ensemble_std"),
                        wx_bias_correction=candidate.get("wx_bias_correction"),
                        wx_n_members=candidate.get("wx_n_members"),
                        wx_market_type=candidate.get("wx_market_type"),
                        wx_hrrr_temp=candidate.get("wx_hrrr_temp"),
                        wx_corrected_mean=candidate.get("wx_corrected_mean"),
                        hourly_pre_temp_prob=candidate.get("hourly_pre_temp_prob"),
                        hourly_applied_temp_t=candidate.get("hourly_applied_temp_t"),
                        hourly_shadow_temp_2_0=candidate.get("hourly_shadow_temp_2_0"),
                        hourly_shadow_temp_1_0=candidate.get("hourly_shadow_temp_1_0"),
                        hourly_shadow_temp_2_5=candidate.get("hourly_shadow_temp_2_5"),
                        hourly_shadow_blend_50=candidate.get("hourly_shadow_blend_50"),
                        hourly_shadow_temp_1_75=candidate.get("hourly_shadow_temp_1_75"),
                        hourly_shadow_temp_3_0=candidate.get("hourly_shadow_temp_3_0"),
                        hourly_shadow_blend_20=candidate.get("hourly_shadow_blend_20"),
                        hourly_shadow_blend_30=candidate.get("hourly_shadow_blend_30"),
                        hourly_shadow_blend_60=candidate.get("hourly_shadow_blend_60"),
                        hourly_post_temp_prob=candidate.get("hourly_post_temp_prob"),
                        available_balance_cents=candidate.get("balance_at_scan"))
            except Exception as e:
                logging.error(f"OBSERVATION_DB_INSERT_FAILED: {candidate.get('ticker')}: {e}")
            return None

        # ── Log candidate to evaluated_opportunities (live mode) ──
        try:
            _ba = candidate.get("best_yes_ask")
            _cp = candidate.get("calibrated_prob")
            _fee1 = calculate_taker_fee(1, _ba) if _ba else 0
            _ev = (_cp * (100 - _ba)) - ((1 - _cp) * _ba) - _fee1 if (_ba and _cp) else None
            self._state.insert_evaluated_opportunity(
                candidate["ticker"], candidate["event_ticker"],
                candidate["asset"], "candidate",
                spot_price=candidate.get("spot"),
                threshold=candidate.get("threshold"),
                volatility=candidate.get("blended_rv"),
                market_price=_ba,
                seconds_to_close=candidate.get("seconds_to_close"),
                calibrated_prob=_cp,
                edge=candidate.get("edge"),
                ofa_adjustment=candidate.get("ofa_adjustment"),
                strategy=candidate.get("strategy"),
                position_size=candidate.get("position_size"),
                kelly_f=candidate.get("kelly_f"),
                z_score=candidate.get("z_score"),
                vol_regime=candidate.get("vol_regime"),
                calibrated_prob_raw=candidate.get("calibrated_prob_raw"),
                breakeven_wr=_ba / 100.0 if _ba else None,
                expected_value=round(_ev, 2) if _ev is not None else None,
                drawdown_scaler=candidate.get("drawdown_scaler"),
                ask_depth=candidate.get("ob_snapshot", {}).get("ask_depth"),
                best_ask_source=candidate.get("best_ask_source"),
                ofa_confidence=candidate.get("ofa_confidence"),
                raw_prob=candidate.get("raw_prob"),
                calibration_method=candidate.get("calibration_method"),
                old_system_prob=candidate.get("old_system_prob"),
                fee_adjusted_edge=candidate.get("fee_adjusted_edge"),
                egarch_sigma=candidate.get("egarch_sigma"),
                egarch_blend_sigma=candidate.get("egarch_blend_sigma"),
                egarch_blend_weight=candidate.get("egarch_blend_weight"),
                mz_r_squared=candidate.get("mz_r_squared"),
                shadow_tv_blend_rv=candidate.get("shadow_tv_blend_rv"),
                mz_shadow_sigmoid_w=candidate.get("mz_shadow_sigmoid_w"),
                mz_baseline_qlike=candidate.get("mz_baseline_qlike"),
                mz_qlike=candidate.get("mz_qlike"),
                counterfactual=candidate.get("counterfactual_json"),
                shadow_cal_prob=candidate.get("shadow_cal_prob"),
                shadow_cal_fee_edge=candidate.get("shadow_cal_fee_edge"),
                shadow_cal_temperature=candidate.get("shadow_cal_temperature"),
                oft_prob_adjustment=candidate.get("oft_prob_adjustment"),
                oft_imbalance_ratio=candidate.get("oft_imbalance_ratio"),
                oft_n_snapshots=candidate.get("oft_n_snapshots"),
                product_type=candidate.get("product_type"),
                wx_ensemble_mean=candidate.get("wx_ensemble_mean"),
                wx_ensemble_std=candidate.get("wx_ensemble_std"),
                wx_bias_correction=candidate.get("wx_bias_correction"),
                wx_n_members=candidate.get("wx_n_members"),
                wx_market_type=candidate.get("wx_market_type"),
                wx_no_side_edge=candidate.get("wx_no_side_edge"),
                wx_hrrr_temp=candidate.get("wx_hrrr_temp"),
                wx_corrected_mean=candidate.get("wx_corrected_mean"),
                hourly_pre_temp_prob=candidate.get("hourly_pre_temp_prob"),
                hourly_applied_temp_t=candidate.get("hourly_applied_temp_t"),
                hourly_shadow_temp_2_0=candidate.get("hourly_shadow_temp_2_0"),
                hourly_shadow_temp_1_0=candidate.get("hourly_shadow_temp_1_0"),
                hourly_shadow_temp_2_5=candidate.get("hourly_shadow_temp_2_5"),
                hourly_shadow_blend_50=candidate.get("hourly_shadow_blend_50"),
                hourly_shadow_temp_1_75=candidate.get("hourly_shadow_temp_1_75"),
                hourly_shadow_temp_3_0=candidate.get("hourly_shadow_temp_3_0"),
                hourly_shadow_blend_20=candidate.get("hourly_shadow_blend_20"),
                hourly_shadow_blend_30=candidate.get("hourly_shadow_blend_30"),
                hourly_shadow_blend_60=candidate.get("hourly_shadow_blend_60"),
                hourly_post_temp_prob=candidate.get("hourly_post_temp_prob"),
                available_balance_cents=candidate.get("balance_at_scan"))
        except Exception as e:
            logging.error(f"CANDIDATE_DB_INSERT_FAILED: {candidate.get('ticker')}: {e}")

        # ── SOL taker-first override ──────────────────────────────
        # SOL: bypass maker entirely, go direct IOC at all STC values.
        # Data: 44.7% maker fill rate, $101/wk missed, 95% unfilled WR.
        seconds_to_close = candidate.get("seconds_to_close")
        if SOL_TAKER_FIRST and candidate.get("asset") == "SOL":
            count = candidate["position_size"]
            price = candidate["best_yes_ask"]
            cal_prob = candidate["calibrated_prob"]

            if count <= 0:
                logging.warning("sol_taker_override_SKIPPED: %s position_size=%d", candidate["ticker"], count)
                return None

            taker_fee = calculate_taker_fee(count, price)
            net_edge = cal_prob - (price / 100.0) - (taker_fee / (count * 100.0))

            if net_edge < MIN_EDGE_PCT / 100.0:
                logging.info(
                    "sol_taker_override_SKIPPED: %s net_edge=%.4f < min=%.4f taker_fee=%d¢",
                    candidate["ticker"], net_edge, MIN_EDGE_PCT / 100.0, taker_fee)
                return None

            fresh_ask = self._get_addon_best_ask(candidate["ticker"])
            if fresh_ask is None:
                logging.info("sol_taker_override_SKIPPED: %s no asks on orderbook", candidate["ticker"])
                return None

            if fresh_ask != price:
                logging.info("sol_taker_override_price_update: %s scanner=%d¢ fresh=%d¢",
                             candidate["ticker"], price, fresh_ask)
                price = fresh_ask
                candidate["best_yes_ask"] = fresh_ask
                taker_fee = calculate_taker_fee(count, price)
                net_edge = cal_prob - (price / 100.0) - (taker_fee / (count * 100.0))
                if net_edge < MIN_EDGE_PCT / 100.0:
                    logging.info("sol_taker_override_SKIPPED: %s fresh_ask=%d¢ net_edge=%.4f < min",
                                 candidate["ticker"], price, net_edge)
                    return None

            logging.info(
                "sol_taker_override_ENTRY: %s %dx @ %d¢ "
                "seconds_to_close=%.0f net_edge=%.4f cal_prob=%.4f taker_fee=%d¢",
                candidate["ticker"], count, price,
                seconds_to_close or 0, net_edge, cal_prob, taker_fee)

            candidate["entry_path"] = "sol_taker_override"
            candidate["escalation_type"] = "sol_taker_override"
            self._recent_taker_tickers[candidate["ticker"]] = time.time()
            _order_submit_ts = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            self._session_direct_taker_attempts += 1
            result = self._submit_taker(candidate)
            if result is not None:
                logging.info("sol_taker_override_FILLED: %s", candidate["ticker"])
                self._session_direct_taker_fills += 1
                _taker_oid = result.get("order_id") if isinstance(result, dict) else None
                self._state.update_evaluated_opportunity_order(
                    candidate["ticker"], order_id=_taker_oid,
                    order_submitted_at=_order_submit_ts, order_outcome="filled",
                    taker_ask_at_submit=candidate.get("best_yes_ask"))
            else:
                logging.warning("sol_taker_override_UNFILLED: %s", candidate["ticker"])
                self._session_direct_taker_unfilled += 1
                self._state.update_evaluated_opportunity_order(
                    candidate["ticker"], order_submitted_at=_order_submit_ts,
                    order_outcome="unfilled",
                    taker_ask_at_submit=candidate.get("best_yes_ask"))

            # ── SOL Path C shadow: log what maker path would have done ──
            try:
                _pathc_fv = price  # current best ask (possibly refreshed)
                _pathc_offset = MAKER_PRICE_OFFSET if _pathc_fv >= 90 else MAKER_PRICE_OFFSET + 1
                _pathc_maker_price = _pathc_fv - _pathc_offset

                # Get depth at the hypothetical maker price level
                _pathc_depth = 0
                try:
                    scanner = self._ml.scanner if self._ml else None
                    if scanner:
                        _pc_ob, _ = scanner._get_orderbook_cached(candidate["ticker"])
                        if _pc_ob:
                            _pathc_depth = OpportunityScanner._best_ask_depth(_pc_ob)
                except Exception:
                    pass

                _pathc_pos_size = candidate["position_size"]
                _eval_time = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

                self._state.insert_sol_pathc_shadow(
                    ticker=candidate["ticker"],
                    evaluation_time=_eval_time,
                    live_ask=price,
                    live_depth=_pathc_depth,
                    live_edge=net_edge,
                    live_stc=seconds_to_close or 0,
                    live_contracts=count,
                    live_entry_price=price,
                    live_cal_prob=cal_prob,
                    pathc_maker_price=_pathc_maker_price,
                    pathc_maker_offset=_pathc_offset,
                    pathc_depth_at_maker=_pathc_depth,
                    position_size=_pathc_pos_size,
                )

                # Schedule for deferred observation (check every tick during escalation window)
                _esc_wait = ESCALATION_WAIT_LONG  # SOL uses default 15s
                self._sol_pathc_pending[candidate["ticker"]] = {
                    "start_time": time.time(),
                    "escalation_wait": _esc_wait,
                    "maker_price": _pathc_maker_price,
                    "position_size": _pathc_pos_size,
                    "cal_prob": cal_prob,
                    "touched": False,
                }
                logging.info(
                    "sol_pathc_shadow_LOGGED: %s maker_price=%d¢ offset=%d depth=%d pos_size=%d",
                    candidate["ticker"], _pathc_maker_price, _pathc_offset, _pathc_depth, _pathc_pos_size)
            except Exception:
                logging.warning("sol_pathc_shadow logging failed", exc_info=True)

            return result

        # ── Direct taker for <180s candidates ───────────────────────
        # Maker-only below 90s: block direct taker, fall through to maker
        if (seconds_to_close is not None
                and seconds_to_close < MAKER_ONLY_THRESHOLD
                and seconds_to_close < DIRECT_TAKER_THRESHOLD):
            logging.info(
                "direct_taker_BLOCKED_maker_only: %s seconds_to_close=%.0f",
                candidate["ticker"], seconds_to_close)
        elif seconds_to_close is not None and seconds_to_close < DIRECT_TAKER_THRESHOLD:
            count = candidate["position_size"]
            price = candidate["best_yes_ask"]
            cal_prob = candidate["calibrated_prob"]

            if count <= 0:
                logging.warning("direct_taker_SKIPPED: %s position_size=%d", candidate["ticker"], count)
                self._session_direct_taker_skipped += 1
                return None

            taker_fee = calculate_taker_fee(count, price)
            net_edge = cal_prob - (price / 100.0) - (taker_fee / (count * 100.0))

            if net_edge < MIN_EDGE_PCT / 100.0:
                logging.info(
                    "direct_taker_SKIPPED: %s net_edge=%.4f < min=%.4f "
                    "seconds_to_close=%.0f taker_fee=%d¢",
                    candidate["ticker"], net_edge, MIN_EDGE_PCT / 100.0,
                    seconds_to_close, taker_fee)
                self._session_direct_taker_skipped += 1
                return None

            # Verify actual liquidity before submitting IOC
            fresh_ask = self._get_addon_best_ask(candidate["ticker"])
            if fresh_ask is None:
                logging.info(
                    "direct_taker_SKIPPED: %s no asks on orderbook "
                    "seconds_to_close=%.0f",
                    candidate["ticker"], seconds_to_close)
                self._session_direct_taker_skipped += 1
                return None

            # Use fresh ask if it differs from scanner's (may be stale NBBO)
            if fresh_ask != price:
                logging.info(
                    "direct_taker_price_update: %s scanner=%d¢ fresh=%d¢",
                    candidate["ticker"], price, fresh_ask)
                price = fresh_ask
                candidate["best_yes_ask"] = fresh_ask
                taker_fee = calculate_taker_fee(count, price)
                net_edge = cal_prob - (price / 100.0) - (taker_fee / (count * 100.0))
                if net_edge < MIN_EDGE_PCT / 100.0:
                    logging.info(
                        "direct_taker_SKIPPED: %s fresh_ask=%d¢ net_edge=%.4f < min",
                        candidate["ticker"], price, net_edge)
                    self._session_direct_taker_skipped += 1
                    return None

            self._session_direct_taker_attempts += 1
            logging.info(
                "direct_taker_ENTRY: %s %dx @ %d¢ "
                "seconds_to_close=%.0f net_edge=%.4f cal_prob=%.4f taker_fee=%d¢",
                candidate["ticker"], count, price,
                seconds_to_close, net_edge, cal_prob, taker_fee)

            candidate["entry_path"] = "direct_taker"
            candidate["escalation_type"] = "direct_taker"
            self._recent_taker_tickers[candidate["ticker"]] = time.time()
            _order_submit_ts = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            result = self._submit_taker(candidate)
            if result is not None:
                self._session_direct_taker_fills += 1
                logging.info("direct_taker_FILLED: %s", candidate["ticker"])
                _taker_oid = result.get("order_id") if isinstance(result, dict) else None
                self._state.update_evaluated_opportunity_order(
                    candidate["ticker"], order_id=_taker_oid,
                    order_submitted_at=_order_submit_ts, order_outcome="filled")
            else:
                self._session_direct_taker_unfilled += 1
                logging.warning("direct_taker_UNFILLED: %s", candidate["ticker"])
                self._state.update_evaluated_opportunity_order(
                    candidate["ticker"], order_submitted_at=_order_submit_ts,
                    order_outcome="unfilled")
            return result

        # ── Three-tier post_only rejection escalation ──────────────
        ticker = candidate["ticker"]
        rejections = self._get_post_only_rejection_count(ticker)

        # Tier 3: Taker escalation (2 same-price + 1 degraded all failed)
        # Maker-only below 90s: block post-only taker escalation
        if (rejections >= POST_ONLY_MAX_SAME_PRICE + 1  # 3+
                and not (seconds_to_close is not None and seconds_to_close < MAKER_ONLY_THRESHOLD)):
            count = candidate["position_size"]
            price = candidate["best_yes_ask"]
            cal_prob = candidate["calibrated_prob"]

            if count <= 0:
                logging.warning("post_only_taker_SKIPPED: %s position_size=%d", ticker, count)
                self._post_only_rejections.pop(ticker, None)
                return None

            taker_fee = calculate_taker_fee(count, price)
            net_edge = cal_prob - (price / 100.0) - (taker_fee / (count * 100.0))

            if net_edge < MIN_EDGE_PCT / 100.0:
                logging.info(
                    "post_only_taker_SKIPPED: %s net_edge=%.4f < min=%.4f "
                    "taker_fee=%d¢ count=%d price=%d¢",
                    ticker, net_edge, MIN_EDGE_PCT / 100.0, taker_fee, count, price)
                self._post_only_rejections.pop(ticker, None)
                return None

            # Verify actual liquidity before submitting IOC
            fresh_ask = self._get_addon_best_ask(ticker)
            if fresh_ask is None:
                logging.info(
                    "post_only_taker_SKIPPED: %s no asks on orderbook", ticker)
                self._post_only_rejections.pop(ticker, None)
                return None

            if fresh_ask != price:
                logging.info(
                    "post_only_taker_price_update: %s scanner=%d¢ fresh=%d¢",
                    ticker, price, fresh_ask)
                price = fresh_ask
                candidate["best_yes_ask"] = fresh_ask
                taker_fee = calculate_taker_fee(count, price)
                net_edge = cal_prob - (price / 100.0) - (taker_fee / (count * 100.0))
                if net_edge < MIN_EDGE_PCT / 100.0:
                    logging.info(
                        "post_only_taker_SKIPPED: %s fresh_ask=%d¢ net_edge=%.4f < min",
                        ticker, price, net_edge)
                    self._post_only_rejections.pop(ticker, None)
                    return None

            logging.info(
                "post_only_taker_ESCALATION: %s %dx @ %d¢ "
                "rejections=%d net_edge=%.4f cal_prob=%.4f taker_fee=%d¢",
                ticker, count, price, rejections, net_edge, cal_prob, taker_fee)
            self._session_post_only_taker_escalations += 1
            candidate["entry_path"] = "post_only_taker"
            candidate["escalation_type"] = "post_only_taker"
            self._recent_taker_tickers[ticker] = time.time()
            _order_submit_ts = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            result = self._submit_taker(candidate)
            if result is not None:
                self._post_only_rejections.pop(ticker, None)
                self._session_post_only_taker_fills += 1
                logging.info("post_only_taker_FILLED: %s", ticker)
                _taker_oid = result.get("order_id") if isinstance(result, dict) else None
                self._state.update_evaluated_opportunity_order(
                    ticker, order_id=_taker_oid,
                    order_submitted_at=_order_submit_ts, order_outcome="filled")
            else:
                # Clear rejections to prevent hot retry loop on persistent API errors
                self._post_only_rejections.pop(ticker, None)
                logging.warning("post_only_taker_UNFILLED: %s (cleared rejections, will re-evaluate)", ticker)
                self._state.update_evaluated_opportunity_order(
                    ticker, order_submitted_at=_order_submit_ts,
                    order_outcome="unfilled")
            return result

        # Tier 2: Degraded maker (1¢ worse, one attempt)
        if rejections == POST_ONLY_MAX_SAME_PRICE:  # 2
            logging.info(
                "post_only_degraded_maker: %s rejections=%d, trying %d¢ worse",
                ticker, rejections, POST_ONLY_DEGRADED_EXTRA_OFFSET)
            self._session_post_only_degraded_attempts += 1
            self._submit_maker(candidate, degraded=True)
            _active = self._active_orders.get(candidate["asset"])
            if _active:
                self._state.update_evaluated_opportunity_order(
                    ticker, order_id=_active["order_id"],
                    order_submitted_at=datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                    taker_ask_at_submit=candidate.get("best_yes_ask"))
            return None

        # Tier 1: Normal maker (attempt 1 or 2)
        self._submit_maker(candidate)
        _active = self._active_orders.get(candidate["asset"])
        if _active:
            self._state.update_evaluated_opportunity_order(
                candidate["ticker"], order_id=_active["order_id"],
                order_submitted_at=datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                taker_ask_at_submit=candidate.get("best_yes_ask"))
        return None

    def tick(self) -> Optional[Dict]:
        """Called each main-loop tick.  Iterates all active orders,
        polls for fills, and handles escalation independently per order.
        """
        if not self._active_orders:
            return None

        # Drain WS fills once, group by order_id
        ws_fills_by_oid: Dict[str, list] = {}
        if self._kalshi_feed and self._kalshi_feed.is_connected:
            try:
                for ws_fill in self._kalshi_feed.pop_fills():
                    oid = ws_fill.get("order_id", "")
                    ws_fills_by_oid.setdefault(oid, []).append(ws_fill)
            except Exception:
                logging.warning("WS fill drain failed", exc_info=True)

        result = None
        for asset in list(self._active_orders):
            order = self._active_orders.get(asset)
            if order is None:
                continue  # removed by a prior iteration's escalation
            order_ws = ws_fills_by_oid.get(order.get("order_id", ""), [])
            r = self._tick_one(order, asset, order_ws)
            if r is not None:
                result = r
        return result

    def _tick_sol_pathc_observations(self):
        """Check orderbook every tick for pending SOL Path C shadow entries.

        During the escalation window (default 15s), continuously monitor the
        orderbook. If best ask ever touches the hypothetical maker price,
        set obs_maker_price_touched=1 (sticky). After escalation window expires,
        write final observation snapshot and remove from pending.
        """
        if not self._sol_pathc_pending:
            return

        now = time.time()
        completed = []

        for ticker, info in self._sol_pathc_pending.items():
            elapsed = now - info["start_time"]
            maker_price = info["maker_price"]

            # Fetch current orderbook
            obs_ask = self._get_addon_best_ask(ticker)
            obs_depth = 0
            if obs_ask is not None:
                try:
                    scanner = self._ml.scanner if self._ml else None
                    if scanner:
                        _ob, _ = scanner._get_orderbook_cached(ticker)
                        if _ob:
                            obs_depth = OpportunityScanner._best_ask_depth(_ob)
                except Exception:
                    pass

            # Check if ask has touched maker price (sticky boolean)
            if obs_ask is not None and obs_ask <= maker_price:
                if not info["touched"]:
                    info["touched"] = True
                    try:
                        self._state.update_sol_pathc_touch(ticker)
                    except Exception:
                        logging.warning("sol_pathc_touch update failed for %s", ticker, exc_info=True)

            maker_would_fill = 1 if (obs_ask is not None and obs_ask <= maker_price) else 0

            # After escalation window: write final observation and compute escalation snapshot
            if elapsed >= info["escalation_wait"]:
                # Compute escalation taker edge
                esc_edge = None
                if obs_ask is not None:
                    esc_taker_fee = calculate_taker_fee(info["position_size"], obs_ask)
                    esc_edge = info["cal_prob"] - (obs_ask / 100.0) - (esc_taker_fee / (info["position_size"] * 100.0))

                obs_time = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                try:
                    self._state.update_sol_pathc_observation(
                        ticker=ticker,
                        obs_time=obs_time,
                        obs_elapsed=round(elapsed, 1),
                        obs_best_ask=obs_ask,
                        obs_depth=obs_depth,
                        obs_maker_would_fill=maker_would_fill,
                        obs_maker_price_touched=1 if info["touched"] else 0,
                        pathc_esc_ask=obs_ask,
                        pathc_esc_depth=obs_depth,
                        pathc_esc_edge=esc_edge,
                    )
                    logging.info(
                        "sol_pathc_obs_FINAL: %s elapsed=%.1fs ask=%s depth=%d touched=%s esc_edge=%s",
                        ticker, elapsed, obs_ask, obs_depth, info["touched"],
                        f"{esc_edge:.4f}" if esc_edge is not None else "None")
                except Exception:
                    logging.warning("sol_pathc_observation write failed for %s", ticker, exc_info=True)

                completed.append(ticker)

        for ticker in completed:
            self._sol_pathc_pending.pop(ticker, None)

    def _tick_one(self, order: Dict, asset: str,
                  ws_fills: list) -> Optional[Dict]:
        """Handle one active order: poll for fill, escalate if needed."""
        now = time.time()
        if now - order["_last_poll"] < MAKER_POLL_INTERVAL:
            return None
        order["_last_poll"] = now

        # Reconcile cancel_pending orders: retry cancel via Kalshi API
        if order.get("cancel_pending"):
            try:
                cancel_resp = self._client.cancel_order(order["order_id"])
                if cancel_resp is not None:
                    logging.info(f"cancel_pending resolved: {order['ticker']} cancel succeeded on retry")
                    order.pop("cancel_pending", None)
                    self._state.mark_order_status(order["order_id"], "canceled")
                    self._active_orders.pop(asset, None)
                    self._state.update_evaluated_opportunity_order(
                        order["ticker"], order_outcome="canceled")
                    return None
                # Cancel still failing — check if order was already filled
                fills_resp = self._client.get_fills(ticker=order["ticker"])
                if fills_resp:
                    fills = fills_resp.get("fills", [])
                    for f in fills:
                        if f.get("order_id") == order["order_id"]:
                            logging.info(f"cancel_pending resolved: {order['ticker']} was filled")
                            order.pop("cancel_pending", None)
                            break  # Let normal fill detection handle it below
            except Exception as e:
                logging.error(f"cancel_pending reconciliation error for {order['ticker']}: {e}")

        # 0. Check WebSocket fills (pre-drained, zero API cost)
        for ws_fill in ws_fills:
            order["fill_source"] = "websocket"
            self._session_ws_fills += 1
            latency_ms = round((now - order["submit_time"]) * 1000, 1)
            logging.info(
                f"kalshi_ws_fill: {order['ticker']} order={order['order_id']} "
                f"latency={latency_ms}ms")
            self._on_fill(ws_fill, order)
            ws_trade_id = ws_fill.get("trade_id") or ws_fill.get("id")
            if not ws_trade_id:
                # Synthetic dedup key when trade_id missing — prevents REST double-count
                self._ws_fill_seq = getattr(self, '_ws_fill_seq', 0) + 1
                ws_trade_id = f"syn_{ws_fill.get('order_id','')}_{ws_fill.get('count','')}_{ws_fill.get('price','')}_{self._ws_fill_seq}"
                logging.warning(f"WS fill missing trade_id for {order['ticker']}, using synthetic key: {ws_trade_id}")
            order.setdefault("_seen_fill_ids", set()).add(ws_trade_id)
            if order.get("filled_so_far", 0) >= order["count"]:
                self._active_orders.pop(asset, None)
                self._state.update_evaluated_opportunity_order(
                    order["ticker"], order_outcome="filled")
                return ws_fill
            logging.info(
                f"Partial WS fill — keeping order active "
                f"({order['filled_so_far']}/{order['count']})")

        # 1. Check for maker fill via REST
        fill = self._check_for_fill(order)
        if fill:
            order["fill_source"] = "rest_poll"
            self._session_rest_fills += 1
            self._on_fill(fill, order)
            if order.get("filled_so_far", 0) >= order["count"]:
                self._active_orders.pop(asset, None)
                self._state.update_evaluated_opportunity_order(
                    order["ticker"], order_outcome="filled")
                return fill
            logging.info(
                f"Partial REST fill — keeping order active "
                f"({order['filled_so_far']}/{order['count']})")

        elapsed = now - order["submit_time"]
        remaining = order["seconds_to_close_at_submit"] - elapsed

        # 2. Too close to expiry — cancel, don't escalate
        if remaining < MIN_SECONDS_BEFORE_CLOSE:
            self._cancel_order(asset, "close_approaching")
            return None

        # 2.5 Queue position polling (~every 5s, rate-limit friendly)
        if now - order["_last_queue_poll"] >= 5.0:
            order["_last_queue_poll"] = now
            try:
                qpos = self._client.get_queue_position(order["order_id"])
                if qpos is not None:
                    order["queue_position"] = qpos
                    logging.debug(
                        f"queue_position_check: {order['ticker']} "
                        f"order={order['order_id']} position={qpos}")
            except Exception:
                pass  # Non-critical, don't disrupt flow

        # 3. Escalation: maker waited long enough? (skip if already escalated)
        # Maker-only below 90s: no taker escalation, let maker fill or expire
        if not order.get("escalated") and remaining >= MAKER_ONLY_THRESHOLD:
            # ── Early escalation: ask confirms thesis ──────────────
            current_ask = self._get_addon_best_ask(order["ticker"])
            if current_ask is not None:
                order["_ask_history"].append((now, current_ask))
                ask_move = current_ask - order["price_cents"]
                if ask_move >= EARLY_ESCALATION_MIN_MOVE:
                    candidate = order["candidate"]
                    cal_prob = candidate["calibrated_prob"]
                    count = order["count"]
                    taker_fee = calculate_taker_fee(count, current_ask)
                    net_edge = cal_prob - (current_ask / 100.0) - (taker_fee / (count * 100.0))
                    if net_edge >= MIN_EDGE_PCT / 100.0:
                        logging.info(
                            "early_escalation_TRIGGER: %s ask=%d¢ (maker=%d¢ +%d¢) "
                            "net_edge=%.4f elapsed=%.1fs",
                            order["ticker"], current_ask, order["price_cents"],
                            ask_move, net_edge, elapsed)
                        return self._escalate_to_taker(order, remaining,
                                                       reason="ask_confirmed")

            # ── Standard time-based escalation (existing code) ─────
            escalation_wait = self._escalation_wait(remaining, asset=order.get("asset", ""))
            # Queue-aware: escalate earlier if deep in queue and time is short
            queue_pos = order.get("queue_position")
            if queue_pos is not None and queue_pos > 20 and remaining < 60:
                escalation_wait = min(escalation_wait, 5.0)
            if elapsed >= escalation_wait:
                return self._escalate_to_taker(order, remaining)

        # 4. Hard timeout fallback
        if elapsed >= MAKER_TIMEOUT_SECONDS:
            self._cancel_order(asset, "timeout")

        return None

    @staticmethod
    def _escalation_wait(remaining: float, asset: str = "") -> float:
        """Urgency-based maker wait before escalating to taker."""
        if remaining >= 180:
            # BTC: shorter wait (7s vs 15s) — ask_confirmed avg 2.7s, slip 3.4c
            if asset == "BTC" and BTC_ESCALATION_WAIT_OVERRIDE is not None:
                return BTC_ESCALATION_WAIT_OVERRIDE
            return ESCALATION_WAIT_LONG     # 15s — ample time, let maker fill
        elif remaining >= 120:
            return ESCALATION_WAIT_MEDIUM   # 7s — 86% of fills happen within 7s
        else:
            return ESCALATION_WAIT_SHORT    # 5s — tight, quick escalation

    # ── Market Intelligence Helpers ───────────────────────────────────────

    @staticmethod
    def _best_ask_depth(ob_data: Dict) -> int:
        """Depth (contracts) at the best YES ask (= highest NO bid level)."""
        no_bids = ob_data.get("no", [])
        if not no_bids:
            return 0
        best_price = -1
        best_qty = 0
        for entry in no_bids:
            if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                price, qty = entry[0], int(entry[1])
            elif isinstance(entry, dict):
                price = entry.get("price", 0)
                qty = int(entry.get("quantity", 0))
            else:
                continue
            if isinstance(price, float) and price < 1.0:
                price_cents = round(price * 100)
            else:
                price_cents = int(price)
            if price_cents > best_price:
                best_price = price_cents
                best_qty = qty
        return best_qty

    @staticmethod
    def _total_ob_depth(ob_data: Dict) -> int:
        """Total depth (contracts) across all orderbook levels."""
        total = 0
        for side in ("no", "yes"):
            for entry in (ob_data.get(side) or []):
                if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                    total += int(entry[1])
                elif isinstance(entry, dict):
                    total += int(entry.get("quantity", 0))
        return total

    @staticmethod
    def _best_yes_bid(ob_data: Dict) -> Optional[int]:
        """Highest YES bid price in cents."""
        yes_bids = ob_data.get("yes", [])
        best = None
        for entry in yes_bids:
            if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                price = entry[0]
            elif isinstance(entry, dict):
                price = entry.get("price", 0)
            else:
                continue
            price_cents = round(price * 100) if isinstance(price, float) and price < 1.0 else int(price)
            if best is None or price_cents > best:
                best = price_cents
        return best

    @staticmethod
    def _best_yes_bid_depth(ob_data: Dict) -> int:
        """Depth at the highest YES bid."""
        yes_bids = ob_data.get("yes", [])
        best_price = -1
        best_qty = 0
        for entry in yes_bids:
            if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                price, qty = entry[0], int(entry[1])
            elif isinstance(entry, dict):
                price = entry.get("price", 0)
                qty = int(entry.get("quantity", 0))
            else:
                continue
            price_cents = round(price * 100) if isinstance(price, float) and price < 1.0 else int(price)
            if price_cents > best_price:
                best_price = price_cents
                best_qty = qty
        return best_qty

    # ── Repricing ─────────────────────────────────────────────────────────

    def _reprice_maker(self, new_price: int) -> bool:
        """Amend maker order to a new price. Returns True on success."""
        if self._active_order is None:
            return False
        order = self._active_order
        self._session_amend_attempts += 1
        try:
            _side = order.get("side", "yes")
            _price_kwarg = {"no_price": new_price} if _side == "no" else {"yes_price": new_price}
            resp = self._client.amend_order(
                order_id=order["order_id"], ticker=order["ticker"],
                side=_side, action="buy", count=order["count"],
                **_price_kwarg)
            if resp is None:
                logging.warning(
                    f"amend_failed_fallback: {order['ticker']} "
                    f"old={order['price_cents']}¢ new={new_price}¢")
                return False
            old_price = order["price_cents"]
            order["price_cents"] = new_price
            self._session_amend_successes += 1
            logging.info(
                f"amend_success: {order['ticker']} "
                f"{old_price}¢ → {new_price}¢ order={order['order_id']}")
            return True
        except Exception:
            logging.warning("Amend failed with exception", exc_info=True)
            return False

    def _escalate_to_taker(self, order: Dict, remaining: float,
                           reason: str = "escalation_wait") -> Optional[Dict]:
        """Escalate maker to taker via cancel-replace IOC."""
        ticker = order["ticker"]
        asset = order["asset"]
        self._escalating_assets.add(asset)
        try:
            return self._escalate_to_taker_inner(order, remaining, reason)
        finally:
            self._escalating_assets.discard(asset)

    def _escalate_to_taker_inner(self, order: Dict, remaining: float,
                                  reason: str = "escalation_wait") -> Optional[Dict]:
        """Inner escalation logic (guarded by _escalating_assets)."""
        ticker = order["ticker"]
        elapsed = time.time() - order["submit_time"]

        # Re-fetch orderbook for current best ask
        ob_raw = self._client.get_orderbook(ticker, depth=5)
        if ob_raw is None:
            logging.warning(f"Escalation aborted: orderbook fetch failed for {ticker}")
            self._cancel_order(order["asset"], reason)
            self._state.update_evaluated_opportunity_order(ticker, order_outcome="canceled")
            return None

        # Unwrap response envelope (same as _get_orderbook_cached)
        ob_fp = ob_raw.get("orderbook_fp") if ob_raw else None
        if ob_fp:
            ob_data = OpportunityScanner._convert_orderbook_fp(ob_fp)
        else:
            ob_data = ob_raw.get("orderbook") or ob_raw

        best_ask = OpportunityScanner._best_yes_ask_cents(ob_data)
        if best_ask is None:
            logging.warning(f"Escalation aborted: no asks on orderbook for {ticker}")
            self._cancel_order(order["asset"], reason)
            self._state.update_evaluated_opportunity_order(ticker, order_outcome="canceled")
            return None

        # Per-asset price floor for escalation (mirrors scanner + maker)
        _esc_floor = MIN_ENTRY_PRICE
        _esc_asset = order.get("asset")
        if _esc_asset == "BTC":
            _esc_floor = BTC_MIN_ENTRY_PRICE
        elif _esc_asset == "ETH":
            _esc_floor = ETH_MIN_ENTRY_PRICE
        elif _esc_asset == "XRP":
            _esc_floor = XRP_MIN_ENTRY_PRICE
        if best_ask < _esc_floor or best_ask > ESCALATION_MAX_ENTRY:
            logging.warning(
                f"Escalation aborted: price {best_ask}¢ out of range "
                f"[{_esc_floor}-{ESCALATION_MAX_ENTRY}¢] for {ticker}"
            )
            self._cancel_order(order["asset"], reason)
            self._state.update_evaluated_opportunity_order(ticker, order_outcome="canceled")
            return None

        # Determine urgency tier for logging
        if remaining >= 180:
            tier = "long"
        elif remaining >= 120:
            tier = "medium"
        else:
            tier = "short"

        price_slip = best_ask - order["price_cents"]
        self._logger.log_order({
            "action": "escalate_to_taker",
            "reason": reason,
            "ticker": ticker,
            "maker_price": order["price_cents"],
            "taker_price": best_ask,
            "price_slip": price_slip,
            "wait_time": round(elapsed, 1),
            "urgency_tier": tier,
            "remaining": round(remaining, 1),
            "execution_method": "cancel_replace_ioc",
        })

        # Cancel maker + submit taker IOC
        logging.info(
            f"escalation_cancel_replace: {ticker} "
            f"(maker={order['price_cents']}¢ → taker={best_ask}¢)")
        cancel_ok = self._cancel_order(order["asset"], reason)
        if not cancel_ok:
            logging.error(f"Cancel failed for {ticker} — NOT submitting taker to prevent double position")
            self._state.update_evaluated_opportunity_order(ticker, order_outcome="canceled")
            return None

        # Build modified candidate with fresh best ask
        candidate = dict(order["candidate"])
        candidate["best_yes_ask"] = best_ask
        candidate["entry_path"] = "escalation_ioc"
        candidate["escalation_type"] = reason
        candidate["maker_price_cents"] = order["price_cents"]
        candidate["maker_wait_seconds"] = round(elapsed, 1)
        filled = order.get("filled_so_far", 0)
        if filled > 0:
            candidate["position_size"] = max(1, candidate["position_size"] - filled)
            logging.info(
                f"escalation_partial_adjust: {ticker} "
                f"original={order['count']} filled={filled} "
                f"ioc_count={candidate['position_size']}")
        self._recent_taker_tickers[ticker] = time.time()

        result = self._submit_taker(candidate)
        if result is not None:
            _esc_oid = result.get("order_id") if isinstance(result, dict) else None
            self._state.update_evaluated_opportunity_order(
                ticker, order_id=_esc_oid, order_outcome="filled")
        else:
            self._state.update_evaluated_opportunity_order(
                ticker, order_outcome="unfilled")
        return result

    # ── Maker ─────────────────────────────────────────────────────────────

    def _submit_maker(self, candidate: Dict, aggressive: bool = False, degraded: bool = False):
        """Submit maker limit order below fair value.

        Patient: 1-2¢ below fair value (wider spread).
        Aggressive: always 1¢ below (tighter, more likely to fill).
        Degraded: extra offset after post_only rejections (Tier 2).
        """
        ticker = candidate["ticker"]
        count = candidate["position_size"]
        fair_value = candidate["best_yes_ask"]
        balance = candidate["balance_at_scan"]

        if aggressive:
            offset = MAKER_PRICE_OFFSET  # always 1¢
        else:
            # 1¢ offset for prices ≥ 90¢, else 2¢
            offset = MAKER_PRICE_OFFSET if fair_value >= 90 else MAKER_PRICE_OFFSET + 1
        price = fair_value - offset
        if degraded:
            price -= POST_ONLY_DEGRADED_EXTRA_OFFSET
        # Per-asset price floor (mirrors scanner check at ~L6560)
        _pt = candidate.get("product_type")
        _asset = candidate.get("asset")
        _mcfg_exec = get_market_config(_pt)
        _floor = _mcfg_exec.min_entry_price
        if _pt in (None, "15m"):
            if _asset == "BTC":
                _floor = BTC_MIN_ENTRY_PRICE
            elif _asset == "ETH":
                _floor = ETH_MIN_ENTRY_PRICE
            elif _asset == "XRP":
                _floor = XRP_MIN_ENTRY_PRICE
        if price < _floor:
            logging.warning("Maker price %dc below %s floor %dc for %s — skipping",
                            price, _asset, _floor, ticker)
            return

        client_oid = str(uuid.uuid4())

        # Persist before submission
        _side = candidate.get("side", "yes")
        self._state.insert_bot_order(
            client_oid, ticker, candidate["event_ticker"],
            candidate["asset"], _side, count, price, False
        )

        # Submit with post_only to guarantee maker fees (4x cheaper)
        _price_kwarg = {"no_price": price} if _side == "no" else {"yes_price": price}
        resp = self._client.place_order(
            ticker=ticker, side=_side, action="buy",
            count=count, client_order_id=client_oid,
            post_only=True, **_price_kwarg,
        )

        if resp is None:
            self._state.mark_order_status(client_oid, "api_error")
            self._record_post_only_rejection(ticker)
            rej_count = self._get_post_only_rejection_count(ticker)
            tier = "degraded" if degraded else "normal"
            logging.warning(
                "Maker order rejected (post_only): %s price=%d¢ tier=%s "
                "rej_count=%d/%d fair=%d¢",
                ticker, price, tier, rej_count,
                POST_ONLY_MAX_SAME_PRICE + 1, fair_value)
            self._session_post_only_rejections += 1
            return

        order_id = (resp.get("order") or {}).get("order_id", client_oid)
        self._state.confirm_order_submitted(client_oid, order_id)
        if self._ml:
            self._ml._session_maker_submissions += 1

        _now = time.time()
        order = {
            "order_id": order_id,
            "client_order_id": client_oid,
            "ticker": ticker,
            "event_ticker": candidate["event_ticker"],
            "asset": candidate["asset"],
            "side": _side,
            "price_cents": price,
            "count": count,
            "is_taker": False,
            "submit_time": _now,
            "seconds_to_close_at_submit": candidate["seconds_to_close"],
            "candidate": candidate,
            "balance_at_entry": balance,
            "entry_path": "maker",
            "_last_poll": _now,
            "_ask_history": deque(maxlen=30),
            "_last_queue_poll": 0.0,
        }
        self._active_orders[candidate["asset"]] = order

        # Clear rejection tracker on successful maker submission
        self._post_only_rejections.pop(ticker, None)

        self._logger.log_order({
            "action": "maker_submitted",
            "ticker": ticker,
            "order_id": order_id,
            "client_order_id": client_oid,
            "price_cents": price,
            "count": count,
            "fair_value": fair_value,
        })
        tier = "degraded" if degraded else ("aggressive" if aggressive else "patient")
        logging.info(
            f"Maker order: {ticker} {count}x @ {price}¢ "
            f"(fair={fair_value}¢, tier={tier})"
        )

    # ── Taker ─────────────────────────────────────────────────────────────

    def _submit_taker(self, candidate: Dict) -> Optional[Dict]:
        """Submit taker order at best ask. Blocks briefly to verify fill."""
        ticker = candidate["ticker"]
        count = candidate["position_size"]
        price = candidate["best_yes_ask"]
        balance = candidate["balance_at_scan"]

        client_oid = str(uuid.uuid4())

        # Persist before submission
        _side = candidate.get("side", "yes")
        self._state.insert_bot_order(
            client_oid, ticker, candidate["event_ticker"],
            candidate["asset"], _side, count, price, True
        )

        # Submit as IOC — exchange auto-cancels any unfilled remainder
        _price_kwarg = {"no_price": price} if _side == "no" else {"yes_price": price}
        resp = self._client.place_order(
            ticker=ticker, side=_side, action="buy",
            count=count, client_order_id=client_oid,
            time_in_force="immediate_or_cancel", **_price_kwarg,
        )

        if resp is None:
            self._state.mark_order_status(client_oid, "api_error")
            logging.error(f"Taker order submission failed: {ticker}")
            if candidate.get("entry_path") != "confirmation_addon":
                self._session_ioc_unfilled += 1
            return None

        order_id = (resp.get("order") or {}).get("order_id", client_oid)
        remaining_count = (resp.get("order") or {}).get("remaining_count", count)
        _order_fill_count = fp_str_to_int((resp.get("order") or {}).get("fill_count_fp")) or (
            (resp.get("order") or {}).get("fill_count") or 0)
        self._state.confirm_order_submitted(client_oid, order_id)

        order_info = {
            "order_id": order_id,
            "client_order_id": client_oid,
            "ticker": ticker,
            "event_ticker": candidate["event_ticker"],
            "asset": candidate["asset"],
            "price_cents": price,
            "count": count,
            "is_taker": True,
            "submit_time": time.time(),
            "seconds_to_close_at_submit": candidate["seconds_to_close"],
            "candidate": candidate,
            "balance_at_entry": balance,
            "execution_method": "ioc",
            "entry_path": candidate.get("entry_path", "direct_taker"),
        }

        self._logger.log_order({
            "action": "taker_submitted",
            "ticker": ticker,
            "order_id": order_id,
            "client_order_id": client_oid,
            "price_cents": price,
            "count": count,
            "time_in_force": "ioc",
        })
        logging.info(f"Taker IOC order: {ticker} {count}x @ {price}¢")

        # IOC resolves instantly; brief wait + collect ALL fill events.
        # An IOC can match against multiple resting orders, generating
        # multiple fill events.  _check_for_fill() returns one unseen
        # fill per call (tracks seen IDs), so loop until exhausted.
        time.sleep(0.3)
        total_filled = 0
        while True:
            fill = self._check_for_fill(order_info)
            if not fill:
                break
            fill_count = self._on_fill(fill, order_info)
            total_filled += fill_count

        # Second poll pass: catch late fills that arrived after initial 0.3s
        if total_filled > 0:
            time.sleep(0.5)
            while True:
                fill = self._check_for_fill(order_info)
                if not fill:
                    break
                fill_count = self._on_fill(fill, order_info)
                total_filled += fill_count

        if total_filled > 0:
            if candidate.get("entry_path") != "confirmation_addon":
                self._session_ioc_fills += 1
            unfilled = count - total_filled
            logging.info(
                f"ioc_taker_result: {ticker} filled={total_filled} "
                f"remaining={unfilled}"
                f"{'' if unfilled == 0 else ' [PARTIAL]'}")
            if unfilled > 0:
                logging.warning(
                    f"IOC partial fill: {ticker} wanted {count} got "
                    f"{total_filled} — {unfilled} contracts unfilled")
            return order_info

        # ── Ghost fill detection (Layer A): remaining_count from order response ──
        # Kalshi's matching engine returns remaining_count=0 when the order was
        # fully matched. If fill polling found nothing, the fills API has latency
        # but the contracts DO exist. Register a defensive position at the limit
        # price (conservative — actual fills are ≤ limit). Reconciliation at next
        # startup will correct prices from Kalshi's positions API.
        #
        # CRITICAL: For IOC orders, remaining_count=0 can also mean the order was
        # auto-canceled with zero fills. Must verify fill_count > 0 from the order
        # response to distinguish real ghost fills from unfilled IOC cancellations.
        # (Bug: false ghost fill on KXSOL15M-26MAR061400-00 cost -$39.16, Mar 6 2026)
        if remaining_count == 0 and _order_fill_count > 0:
            logging.error(
                f"GHOST_FILL_DETECTED: {ticker} remaining_count=0 but no fill "
                f"events from API — Kalshi matched all {count} contracts. "
                f"Registering defensive position at limit price {price}¢")
            self._state.record_position_from_fill(
                ticker=ticker,
                event_ticker=candidate["event_ticker"],
                asset=candidate["asset"],
                side="yes",
                count=count,
                price_cents=price,
                strategy=candidate.get("strategy"),
                seconds_to_close=order_info.get("seconds_to_close_at_submit"),
                fill_latency=round(time.time() - order_info["submit_time"], 3),
                vol_regime=candidate.get("vol_regime"),
                calibrated_prob=candidate.get("calibrated_prob"),
                edge=candidate.get("edge"),
                kelly_f=candidate.get("kelly_f"),
                is_taker=True,
                fill_source="ghost_fill",
                execution_method="ioc",
                escalation_type=candidate.get("escalation_type"),
                maker_price_cents=candidate.get("maker_price_cents"),
                maker_wait_seconds=candidate.get("maker_wait_seconds"),
            )
            self._state.mark_order_status(order_id, "filled")
            if candidate.get("entry_path") != "confirmation_addon":
                self._session_ioc_fills += 1
            return order_info

        # remaining_count=0 but fill_count=0: IOC was auto-canceled, not a ghost fill
        if remaining_count == 0 and _order_fill_count == 0:
            logging.info(
                f"IOC_CANCELED_NO_FILLS: {ticker} remaining_count=0 "
                f"fill_count=0 — order was canceled unfilled, not a ghost fill")

        # ── Ghost fill detection (Layer B): positions API verification ──
        # remaining_count > 0 suggests genuinely unfilled, but verify against
        # Kalshi's positions API in case of any untracked position.
        try:
            _pos_resp = self._client.get_positions()
            if _pos_resp and _pos_resp.get("market_positions"):
                for _pos in _pos_resp["market_positions"]:
                    if _pos.get("ticker") == ticker:
                        _pos_count = fp_str_to_int(_pos.get("position_fp")) or (_pos.get("position") or 0)
                        if _pos_count > 0:
                            _pos_cost_d = _pos.get("market_exposure_dollars")
                            _pos_cost = dollars_str_to_cents(_pos_cost_d) if _pos_cost_d else (_pos.get("market_exposure") or 0)
                            _pos_avg = _pos_cost // _pos_count if _pos_count else price
                            logging.error(
                                f"GHOST_FILL_DETECTED_VIA_POSITIONS: {ticker} "
                                f"fill polling found nothing, remaining_count={remaining_count}, "
                                f"but positions API shows {_pos_count} contracts "
                                f"(cost={_pos_cost}¢, avg={_pos_avg}¢)")
                            self._state.record_position_from_fill(
                                ticker=ticker,
                                event_ticker=candidate["event_ticker"],
                                asset=candidate["asset"],
                                side="yes",
                                count=_pos_count,
                                price_cents=_pos_avg,
                                strategy=candidate.get("strategy"),
                                seconds_to_close=order_info.get("seconds_to_close_at_submit"),
                                fill_latency=round(time.time() - order_info["submit_time"], 3),
                                vol_regime=candidate.get("vol_regime"),
                                calibrated_prob=candidate.get("calibrated_prob"),
                                edge=candidate.get("edge"),
                                kelly_f=candidate.get("kelly_f"),
                                is_taker=True,
                                fill_source="ghost_fill_positions_api",
                                execution_method="ioc",
                                escalation_type=candidate.get("escalation_type"),
                                maker_price_cents=candidate.get("maker_price_cents"),
                                maker_wait_seconds=candidate.get("maker_wait_seconds"),
                            )
                            self._state.mark_order_status(order_id, "filled")
                            if candidate.get("entry_path") != "confirmation_addon":
                                self._session_ioc_fills += 1
                            return order_info
        except Exception as e:
            logging.warning(f"Ghost fill positions API check failed for {ticker}: {e}")

        # IOC auto-cancels unfilled portion — no manual cancel needed
        self._state.mark_order_status(order_id, "canceled")
        if candidate.get("entry_path") != "confirmation_addon":
            self._session_ioc_unfilled += 1
        self._logger.log_order({
            "action": "taker_ioc_unfilled",
            "ticker": ticker,
            "order_id": order_id,
            "remaining_count": remaining_count,
        })
        logging.warning(f"Taker IOC not filled: {ticker} (remaining={remaining_count})")
        return None

    # ── Fill detection ────────────────────────────────────────────────────

    def _check_for_fill(self, order: Dict) -> Optional[Dict]:
        """Check if order has been filled via REST fills endpoint.

        Tracks seen fill IDs on the order dict to avoid double-counting
        partial fills on consecutive polls.
        """
        min_ts = int(order["submit_time"])
        resp = self._client.get_fills(
            ticker=order["ticker"], min_ts=min_ts
        )
        if not resp or not resp.get("fills"):
            return None

        seen = order.setdefault("_seen_fill_ids", set())
        for fill in resp["fills"]:
            fill_id = fill.get("trade_id") or fill.get("id")
            if not fill_id:
                logging.warning(f"REST fill missing trade_id/id for {order['ticker']} — skipping to avoid double-count")
                continue
            if fill.get("order_id") == order["order_id"] and fill_id not in seen:
                seen.add(fill_id)
                return fill
        return None

    # ── Fill handling ─────────────────────────────────────────────────────

    def _on_fill(self, fill: Dict, order: Dict) -> int:
        """Handle fill: update SQLite, log trade, record position.

        Returns the fill_count so callers can track partial vs complete fills.
        """
        order_id = order["order_id"]
        ticker = order["ticker"]
        candidate = order["candidate"]

        # Extract fill details — prefer FP/dollar fields, fall back to legacy
        raw_fill_count = fp_str_to_int(fill.get("count_fp")) or (fill.get("count") or order["count"])
        remaining = order["count"] - order.get("filled_so_far", 0)
        if raw_fill_count > remaining > 0:
            logging.warning(
                f"Fill count {raw_fill_count} exceeds remaining {remaining} for "
                f"{order['ticker']} — capping to {remaining}")
            fill_count = remaining
        else:
            fill_count = raw_fill_count
        fill_price_d = fill.get("yes_price_dollars")
        fill_price = dollars_str_to_cents(fill_price_d) if fill_price_d else (fill.get("yes_price") or order["price_cents"])

        # Track cumulative fills for partial fill detection
        order["filled_so_far"] = min(
            order.get("filled_so_far", 0) + fill_count,
            order["count"]
        )
        is_complete = order["filled_so_far"] >= order["count"]

        # Update order status only when fully filled
        if is_complete:
            self._state.mark_order_status(order_id, "filled")
        else:
            logging.info(
                f"Partial fill: {ticker} {fill_count}/{order['count']} "
                f"(cumulative {order['filled_so_far']}/{order['count']})")

        # Log fill model sample for ML training
        self._log_fill_model_sample(order, "filled", fill=fill)

        # Track fill latency
        fill_latency = round(time.time() - order["submit_time"], 3)
        try:
            if self._ml:
                self._ml._recent_fill_latencies.append(fill_latency)
                self._ml._session_fill_count += 1
                if not order.get("is_taker", True):
                    self._ml._session_maker_fills += 1
        except Exception:
            logging.debug("Fill latency tracking failed", exc_info=True)
        logging.info(f"Fill latency: {fill_latency:.3f}s ({'taker' if order.get('is_taker') else 'maker'})")

        # Record position in SQLite
        self._state.record_position_from_fill(
            ticker=ticker,
            event_ticker=order["event_ticker"],
            asset=order["asset"],
            side=order.get("side", "yes"),
            count=fill_count,
            price_cents=fill_price,
            strategy=candidate.get("strategy"),
            seconds_to_close=order.get("seconds_to_close_at_submit"),
            fill_latency=fill_latency,
            vol_regime=candidate.get("vol_regime"),
            calibrated_prob=candidate.get("calibrated_prob"),
            edge=candidate.get("edge"),
            kelly_f=candidate.get("kelly_f"),
            is_taker=order.get("is_taker", False),
            fill_source=order.get("fill_source", "rest_poll"),
            execution_method=order.get("execution_method", "maker"),
            escalation_type=candidate.get("escalation_type", "none"),
            maker_price_cents=candidate.get("maker_price_cents"),
            maker_wait_seconds=candidate.get("maker_wait_seconds"),
        )

        # Invalidate scanner balance cache so next tick gets fresh balance
        try:
            if self._ml and hasattr(self._ml, 'scanner'):
                self._ml.scanner._balance_cache = (None, 0.0)
        except Exception:
            pass

        # Log trade with all required fields
        is_taker = order.get("is_taker", False)
        cost_cents = fill_count * fill_price
        fee_cents = calculate_fee(fill_count, fill_price, is_taker=is_taker)

        self._logger.log_trade({
            "ticker": ticker,
            "direction": "yes",
            "price": fill_price,
            "count": fill_count,
            "cost": cost_cents,
            "fee": fee_cents,
            "z_score": candidate.get("z_score"),
            "p_calibrated": candidate.get("calibrated_prob"),
            "balance_at_entry": order["balance_at_entry"],
            "tier": "taker" if is_taker else "maker",
            "is_taker": is_taker,
            "is_panic": order.get("is_panic", False),
            "edge": candidate.get("edge"),
            "kelly_f": candidate.get("kelly_f"),
            "asset": order["asset"],
            "event_ticker": order["event_ticker"],
            "order_id": order_id,
            "client_order_id": order["client_order_id"],
            "strategy_used": candidate.get("strategy"),
            "decision_scores": candidate.get("strategy_scores"),
            "orderbook_snapshot": candidate.get("ob_snapshot"),
        })

        logging.info(
            f"FILL: {ticker} {fill_count}x @ {fill_price}¢ "
            f"({'taker' if is_taker else 'maker'}) "
            f"cost={cost_cents}¢ fee={fee_cents}¢"
            f"{'' if is_complete else ' [PARTIAL ' + str(order['filled_so_far']) + '/' + str(order['count']) + ']'}"
        )

        # Register for confirmation addon evaluation (only on complete fills)
        if is_complete:
            try:
                self._register_addon_eligible(order, fill_price, fill_latency)
            except Exception:
                logging.debug("addon registration failed", exc_info=True)

        return fill_count

    # ── Fill Model Logging ────────────────────────────────────────────────

    def _log_fill_model_sample(self, order: Dict, outcome: str,
                               fill: Optional[Dict] = None,
                               cancel_reason: Optional[str] = None):
        """Write one fill_model_sample to FILL_MODEL_JOURNAL for ML training."""
        try:
            candidate = order.get("candidate", {})
            now = time.time()
            elapsed = now - order["submit_time"]
            fill_latency = round(elapsed, 3) if outcome == "filled" else None
            ob_snap = candidate.get("ob_snapshot", {})

            sample = {
                "type": "fill_model_sample",
                "ts": datetime.datetime.utcnow().isoformat() + "Z",
                "ticker": order["ticker"],
                "asset": order["asset"],
                "outcome": outcome,
                "fill_latency_s": fill_latency,
                "fill_source": order.get("fill_source"),
                # Submission context
                "price_cents": order["price_cents"],
                "fair_value": candidate.get("best_yes_ask"),
                "offset_cents": (candidate.get("best_yes_ask", 0) - order["price_cents"])
                    if candidate.get("best_yes_ask") else None,
                "count": order["count"],
                "post_only": not order.get("is_taker", False),
                # Market context at submission
                "seconds_to_close": order.get("seconds_to_close_at_submit"),
                "vol_regime": candidate.get("vol_regime"),
                "blended_rv": candidate.get("blended_rv"),
                "ask_depth": ob_snap.get("ask_depth"),
                "total_ob_depth": ob_snap.get("total_depth"),
                "spread_at_submit": ob_snap.get("spread"),
                "bid_depth": ob_snap.get("bid_depth"),
                "convergence_velocity": candidate.get("convergence_velocity"),
                "z_score": candidate.get("z_score"),
                "edge": candidate.get("edge"),
                "kelly_f": candidate.get("kelly_f"),
                # Queue tracking
                "queue_position_initial": order.get("queue_position_initial"),
                "queue_position_final": order.get("queue_position"),
                # Execution details
                "execution_method": order.get("execution_method", "maker"),
                "entry_path": order.get("entry_path", "maker"),
                "cancel_reason": cancel_reason,
                "elapsed_seconds": round(elapsed, 1),
                # WS state
                "ws_connected": (self._kalshi_feed.is_connected
                                 if self._kalshi_feed else False),
                # Config stamps for regime-filtered analysis
                "maker_only_threshold": MAKER_ONLY_THRESHOLD,
            }

            with open(FILL_MODEL_JOURNAL, "a") as f:
                f.write(json.dumps(sample) + "\n")
        except Exception:
            logging.debug("fill_model_sample write failed", exc_info=True)

    # ── Confirmation Addon ─────────────────────────────────────────────────

    def _register_addon_eligible(self, order: Dict,
                                actual_fill_price: int = 0,
                                fill_latency: float = 0.0):
        """After a fill, register the position for addon evaluation.

        Uses actual fill price (not maker limit price) and corrects STC
        for fill latency so addon timing is accurate.

        Skips if the fill itself is an addon (prevents recursive registration).
        """
        if not ADDON_ENABLED:
            return
        candidate = order.get("candidate", {})
        # Don't re-register addon fills
        if candidate.get("entry_path") in ("confirmation_addon", "dip_addon"):
            return

        ticker = order["ticker"]
        # Use actual execution price, not the submitted limit price
        entry_price = actual_fill_price if actual_fill_price > 0 else order["price_cents"]
        fill_count = order.get("filled_so_far", order["count"])

        # Correct STC: subtract fill latency from submit-time STC
        stc_at_submit = order.get("seconds_to_close_at_submit")
        stc_at_fill = (stc_at_submit - fill_latency) if stc_at_submit is not None else None

        meta = {
            "ticker": ticker,
            "event_ticker": order["event_ticker"],
            "asset": order["asset"],
            "entry_price_cents": entry_price,
            "entry_count": fill_count,
            "fill_time": time.time(),
            "seconds_to_close_at_fill": stc_at_fill,
            "threshold": candidate.get("threshold"),
            "blended_rv": candidate.get("blended_rv"),
            "calibrated_prob": candidate.get("calibrated_prob"),
            "candidate": candidate,
        }
        self._addon_eligible[ticker] = meta
        logging.info(
            "addon_registered: %s entry=%d¢ count=%d stc=%.0f",
            ticker, entry_price, fill_count, stc_at_fill or 0)

    def _check_addon_opportunities(self):
        """Evaluate open positions for confirmation addon. Called from _tick."""
        if OBSERVATION_MODE:
            return  # Never execute addons in observation mode
        if not ADDON_ENABLED or not self._addon_eligible:
            return

        now = time.time()
        expired = []

        for ticker, meta in list(self._addon_eligible.items()):
            # Cleanup: remove entries >5min old
            if now - meta["fill_time"] > 300:
                expired.append(ticker)
                continue

            # Already addon'd this position
            if ticker in self._addon_completed:
                continue

            # Elapsed check
            elapsed = now - meta["fill_time"]
            if elapsed < ADDON_MIN_SECONDS_SINCE_FILL:
                continue

            # STC check
            stc_at_fill = meta.get("seconds_to_close_at_fill")
            if stc_at_fill is None:
                continue
            current_stc = stc_at_fill - elapsed
            if current_stc < ADDON_MIN_STC_REMAINING:
                self._session_addon_skipped += 1
                logging.info(
                    "addon_SKIP_stc: %s stc_remaining=%.0f < %.0f",
                    ticker, current_stc, ADDON_MIN_STC_REMAINING)
                expired.append(ticker)
                continue

            # Get current best ask
            current_ask = self._get_addon_best_ask(ticker)
            if current_ask is None:
                continue  # Deferred to next tick

            # Price improvement check
            improvement = current_ask - meta["entry_price_cents"]
            if improvement < ADDON_MIN_PRICE_IMPROVEMENT:
                continue  # Not enough improvement yet

            # Price cap
            if current_ask > ADDON_MAX_ENTRY_PRICE:
                self._session_addon_skipped += 1
                logging.info(
                    "addon_SKIP_price_cap: %s ask=%d¢ > %d¢",
                    ticker, current_ask, ADDON_MAX_ENTRY_PRICE)
                continue

            # Get current spot price
            asset = meta["asset"]
            spot = self._get_addon_spot(asset)
            if spot is None:
                continue

            # Recalculate probability with current spot and STC
            blended_rv = meta.get("blended_rv")
            try:
                if self._ml and hasattr(self._ml, 'vol'):
                    fresh_vol = self._ml.vol._cache.get(asset)
                    if fresh_vol and fresh_vol.get("blended_rv"):
                        blended_rv = fresh_vol["blended_rv"]
            except Exception:
                pass
            threshold = meta.get("threshold")
            if blended_rv is None or threshold is None:
                continue

            prob_result = ProbabilityEngine.compute(
                spot, threshold, current_stc, blended_rv, asset=asset,
                product_type=meta.get("candidate", {}).get("product_type"))
            cal_prob = prob_result.get("calibrated_prob")
            if cal_prob is None:
                continue

            # Taker edge check
            addon_count = max(1, int(meta["entry_count"] * ADDON_SIZE_FRACTION))
            taker_fee = calculate_taker_fee(addon_count, current_ask)
            net_edge = cal_prob - (current_ask / 100.0) - (taker_fee / (addon_count * 100.0))

            if net_edge < MIN_EDGE_PCT / 100.0:
                self._session_addon_skipped += 1
                logging.info(
                    "addon_SKIP_edge: %s net_edge=%.4f < %.4f ask=%d¢ "
                    "prob=%.4f fee=%d¢",
                    ticker, net_edge, MIN_EDGE_PCT / 100.0,
                    current_ask, cal_prob, taker_fee)
                continue

            # Balance check — addon cost capped at 50% of current balance
            balance = self._get_addon_balance()
            if balance is None:
                continue

            addon_cost = addon_count * current_ask
            max_addon_cost = int(balance * 0.50)
            if addon_cost > max_addon_cost:
                # Reduce count to fit within 50% of balance
                if current_ask > 0:
                    addon_count = max_addon_cost // current_ask
                if addon_count < 1:
                    self._session_addon_skipped += 1
                    logging.info(
                        "addon_SKIP_balance: %s cost=%d¢ > 50%% balance=%d¢",
                        ticker, addon_cost, balance)
                    continue
                addon_cost = addon_count * current_ask
                taker_fee = calculate_taker_fee(addon_count, current_ask)
                net_edge = cal_prob - (current_ask / 100.0) - (taker_fee / (addon_count * 100.0))
                if net_edge < MIN_EDGE_PCT / 100.0:
                    self._session_addon_skipped += 1
                    logging.info(
                        "addon_SKIP_edge_after_resize: %s count=%d edge=%.4f",
                        ticker, addon_count, net_edge)
                    continue

            # All checks passed — execute addon
            filled = self._execute_addon(
                meta, addon_count, current_ask, current_stc,
                cal_prob, net_edge, balance, spot)
            if filled:
                self._addon_completed.add(ticker)

        # Cleanup expired entries
        for t in expired:
            self._addon_eligible.pop(t, None)
        # Also clean completed tickers no longer in eligible
        for t in list(self._addon_completed):
            if t not in self._addon_eligible:
                self._addon_completed.discard(t)

    def _execute_addon(self, meta: Dict, count: int, price: int,
                       stc: float, prob: float, edge: float,
                       balance: int, spot: float) -> bool:
        """Submit taker IOC for confirmation addon. Returns True on fill."""
        ticker = meta["ticker"]
        self._session_addon_attempts += 1

        logging.info(
            "addon_TRIGGER: %s %dx @ %d¢ (entry=%d¢ +%d¢) "
            "stc=%.0f edge=%.4f prob=%.4f balance=%d¢ spot=%.2f",
            ticker, count, price, meta["entry_price_cents"],
            price - meta["entry_price_cents"],
            stc, edge, prob, balance, spot)

        # Build addon candidate for _submit_taker
        addon_candidate = {
            "ticker": ticker,
            "event_ticker": meta["event_ticker"],
            "asset": meta["asset"],
            "best_yes_ask": price,
            "position_size": count,
            "calibrated_prob": prob,
            "edge": edge,
            "seconds_to_close": stc,
            "balance_at_scan": balance,
            "entry_path": "confirmation_addon",
            "strategy": "CONFIRMATION_ADDON",
            "blended_rv": meta.get("blended_rv"),
            "threshold": meta.get("threshold"),
            "vol_regime": meta.get("candidate", {}).get("vol_regime"),
            "z_score": meta.get("candidate", {}).get("z_score"),
            "kelly_f": meta.get("candidate", {}).get("kelly_f"),
            "ob_snapshot": {},
            "original_entry_price": meta["entry_price_cents"],
            "original_entry_count": meta["entry_count"],
            "price_improvement": price - meta["entry_price_cents"],
        }

        if OBSERVATION_MODE:
            logging.info(
                "addon_OBSERVATION: %s %dx @ %d¢ — would submit taker IOC",
                ticker, count, price)
            self._logger.log_execution({
                "action": "addon_observation",
                "ticker": ticker,
                "asset": meta["asset"],
                "price": price,
                "count": count,
                "edge": edge,
                "prob": prob,
                "stc": stc,
                "original_entry_price": meta["entry_price_cents"],
                "price_improvement": price - meta["entry_price_cents"],
            })
            return True

        # Live: submit taker IOC
        result = self._submit_taker(addon_candidate)

        if result is not None:
            self._session_addon_fills += 1
            logging.info(
                "addon_FILLED: %s %dx @ %d¢ (+%d¢ from entry)",
                ticker, count, price,
                price - meta["entry_price_cents"])
            if _TELEGRAM:
                try:
                    _addon_cost = count * price / 100
                    _TELEGRAM.send(
                        f"\u2795 Addon: {meta.get('asset', '?')} {count}ct @ {price}c "
                        f"(${_addon_cost:.2f}, +{price - meta['entry_price_cents']}c slip)")
                except Exception:
                    pass

            self._logger.log_execution({
                "action": "addon_filled",
                "ticker": ticker,
                "asset": meta["asset"],
                "price": price,
                "count": count,
                "edge": edge,
                "prob": prob,
                "stc": stc,
                "original_entry_price": meta["entry_price_cents"],
                "price_improvement": price - meta["entry_price_cents"],
                "balance_after": balance - (count * price) - calculate_taker_fee(count, price),
            })
            return True
        else:
            self._session_addon_unfilled += 1
            self._addon_completed.add(ticker)  # Don't retry — single attempt
            logging.warning(
                "addon_UNFILLED: %s %dx @ %d¢", ticker, count, price)
            self._logger.log_execution({
                "action": "addon_unfilled",
                "ticker": ticker,
                "asset": meta["asset"],
                "price": price,
                "count": count,
            })
            return False

    def _get_addon_best_ask(self, ticker: str) -> Optional[int]:
        """Get best YES ask for ticker via scanner cache, then REST fallback."""
        try:
            scanner = self._ml.scanner if self._ml else None
            if scanner:
                ob_data, _ = scanner._get_orderbook_cached(ticker)
                if ob_data:
                    return OpportunityScanner._best_yes_ask_cents(ob_data)
        except Exception:
            logging.debug("addon orderbook cache lookup failed", exc_info=True)

        # REST fallback
        try:
            ob_resp = self._client.get_orderbook(ticker, depth=5)
            if ob_resp:
                orderbook_fp = ob_resp.get("orderbook_fp")
                if orderbook_fp and self._ml and hasattr(self._ml, 'scanner'):
                    ob_data = self._ml.scanner._convert_orderbook_fp(orderbook_fp)
                else:
                    ob_data = ob_resp.get("orderbook", ob_resp)
                if ob_data:
                    return OpportunityScanner._best_yes_ask_cents(ob_data)
        except Exception:
            logging.debug("addon orderbook REST fallback failed", exc_info=True)
        return None

    def _get_addon_spot(self, asset: str) -> Optional[float]:
        """Get current spot price for asset via feed."""
        try:
            if self._ml and hasattr(self._ml, 'scanner'):
                return self._ml.scanner._feed.get_price(asset)
        except Exception:
            logging.debug("addon spot price lookup failed", exc_info=True)
        return None

    def _get_addon_balance(self) -> Optional[int]:
        """Get current balance in cents."""
        try:
            if self._ml and hasattr(self._ml, 'scanner'):
                return self._ml.scanner._get_balance_cached()
        except Exception:
            logging.debug("addon balance lookup failed", exc_info=True)
        # Direct API fallback
        try:
            resp = self._client.get_balance()
            if resp:
                return resp.get("balance") or 0
        except Exception:
            logging.debug("addon balance API fallback failed", exc_info=True)
        return None

    # ── Dip Addon ──────────────────────────────────────────────────────────

    def _check_dip_addon_opportunities(self):
        """Check filled positions for dip buy opportunities.

        Two-tier logging:
          1. Shadow tier (>=50c): Every qualifying dip -> evaluated_opportunities
          2. Live tier (>=87c): Execution path (shadow or real)
        """
        if OBSERVATION_MODE:
            return  # Never execute dip addons in observation mode
        if not DIP_ADDON_ENABLED or not self._addon_eligible:
            return

        now = time.time()

        # Cleanup expired tickers from completed set
        for t in list(self._dip_addon_completed):
            if t not in self._addon_eligible:
                self._dip_addon_completed.discard(t)

        for ticker, meta in list(self._addon_eligible.items()):
            # Already dip-addon'd or expired
            if ticker in self._dip_addon_completed:
                continue
            if now - meta["fill_time"] > 300:
                continue

            # Don't dip-addon on addon fills
            if meta.get("candidate", {}).get("entry_path") in (
                    "confirmation_addon", "dip_addon"):
                continue

            # Time checks
            elapsed = now - meta["fill_time"]
            if elapsed < DIP_ADDON_MIN_SECONDS_SINCE_FILL:
                continue

            stc_at_fill = meta.get("seconds_to_close_at_fill")
            if stc_at_fill is None:
                continue
            current_stc = stc_at_fill - elapsed
            if current_stc < DIP_ADDON_MIN_STC_REMAINING:
                continue

            # Get current ask
            current_ask = self._get_addon_best_ask(ticker)
            if current_ask is None:
                continue

            # DIP CHECK: ask must drop >= threshold below entry
            drop = meta["entry_price_cents"] - current_ask
            if drop < DIP_ADDON_MIN_DROP_CENTS:
                continue

            # ── Shared computation (needed by both tiers) ──────────
            asset = meta["asset"]
            spot = self._get_addon_spot(asset)
            if spot is None:
                continue

            blended_rv = meta.get("blended_rv")
            try:
                if self._ml and hasattr(self._ml, 'vol'):
                    fresh_vol = self._ml.vol._cache.get(asset)
                    if fresh_vol and fresh_vol.get("blended_rv"):
                        blended_rv = fresh_vol["blended_rv"]
            except Exception:
                pass

            threshold = meta.get("threshold")
            if blended_rv is None or threshold is None:
                continue

            # Recompute probability at current spot/vol/stc
            prob_result = ProbabilityEngine.compute(
                spot, threshold, current_stc, blended_rv, asset=asset,
                product_type=meta.get("candidate", {}).get("product_type"))
            cal_prob = prob_result.get("calibrated_prob")
            if cal_prob is None:
                continue

            # Sizing (computed once, used by both tiers)
            addon_count = max(1, int(
                meta["entry_count"] * DIP_ADDON_SIZE_FRACTION))
            taker_fee = calculate_taker_fee(addon_count, current_ask)
            net_edge = (cal_prob - (current_ask / 100.0)
                        - (taker_fee / (addon_count * 100.0)))

            # ── TIER 1: Shadow observation (50c floor) ─────────────
            if current_ask >= DIP_ADDON_SHADOW_FLOOR:
                self._session_dip_addon_shadow += 1
                # OFT signals for dip addon
                _dip_oft_db = {}
                if self._kalshi_oft is not None:
                    try:
                        _dip_koft = self._kalshi_oft.get_signals(ticker)
                        if _dip_koft:
                            _dip_oft_db = {
                                "oft_prob_adjustment": _dip_koft.get("prob_adjustment"),
                                "oft_imbalance_ratio": _dip_koft.get("imbalance_ratio"),
                                "oft_n_snapshots": _dip_koft.get("n_snapshots"),
                            }
                    except Exception:
                        pass
                try:
                    self._state.insert_evaluated_opportunity(
                        ticker=ticker,
                        event_ticker=meta["event_ticker"],
                        asset=asset,
                        filter_stage="dip_addon_shadow",
                        spot_price=spot,
                        threshold=threshold,
                        volatility=blended_rv,
                        market_price=current_ask,
                        seconds_to_close=current_stc,
                        calibrated_prob=cal_prob,
                        edge=net_edge,
                        strategy="DIP_ADDON_SHADOW",
                        position_size=addon_count,
                        z_score=meta.get("candidate", {}).get("z_score"),
                        vol_regime=meta.get("candidate", {}).get(
                            "vol_regime"),
                        raw_prob=prob_result.get("raw_prob"),
                        fee_adjusted_edge=net_edge,
                        counterfactual=(
                            "entry=%dc drop=%dc orig_count=%d"
                            % (meta["entry_price_cents"], drop,
                               meta["entry_count"])),
                        product_type="dip_addon_shadow",
                        hourly_pre_temp_prob=None, hourly_applied_temp_t=None,
                        hourly_shadow_temp_2_0=None, hourly_shadow_temp_1_0=None,
                        hourly_shadow_temp_2_5=None, hourly_shadow_blend_50=None,
                        hourly_shadow_temp_1_75=None, hourly_shadow_temp_3_0=None,
                        hourly_shadow_blend_20=None, hourly_shadow_blend_30=None,
                        hourly_shadow_blend_60=None, hourly_post_temp_prob=None,
                        **_dip_oft_db,
                    )
                except Exception:
                    logging.debug("dip_addon shadow DB insert failed",
                                  exc_info=True)

                logging.info(
                    "dip_addon_SHADOW_OBS: %s ask=%dc entry=%dc drop=%dc "
                    "edge=%.4f prob=%.4f stc=%.0f count=%d",
                    ticker, current_ask, meta["entry_price_cents"], drop,
                    net_edge, cal_prob, current_stc, addon_count)

            # ── TIER 2: Live execution path (87c floor) ────────────
            # Mark completed after shadow log — one observation per ticker
            self._dip_addon_completed.add(ticker)

            if current_ask < DIP_ADDON_MIN_ENTRY_PRICE:
                logging.info("dip_addon_SKIP_floor: %s ask=%dc < %dc",
                             ticker, current_ask, DIP_ADDON_MIN_ENTRY_PRICE)
                continue

            # Edge check
            if net_edge < MIN_EDGE_PCT / 100.0:
                self._session_dip_addon_skipped += 1
                logging.info(
                    "dip_addon_SKIP_edge: %s edge=%.4f ask=%dc prob=%.4f",
                    ticker, net_edge, current_ask, cal_prob)
                continue

            # Balance + combined exposure check
            balance = self._get_addon_balance()
            if balance is None:
                continue

            original_cost = meta["entry_count"] * meta["entry_price_cents"]
            addon_cost = addon_count * current_ask
            total_exposure = original_cost + addon_cost
            max_allowed = int(
                (balance + original_cost) * DIP_ADDON_MAX_TOTAL_RISK)
            if total_exposure > max_allowed:
                addon_count = max(
                    0, (max_allowed - original_cost) // current_ask)
                if addon_count < 1:
                    self._session_dip_addon_skipped += 1
                    logging.info(
                        "dip_addon_SKIP_exposure: %s total=%dc > %dc",
                        ticker, total_exposure, max_allowed)
                    continue
                addon_cost = addon_count * current_ask
                taker_fee = calculate_taker_fee(addon_count, current_ask)
                net_edge = (cal_prob - (current_ask / 100.0)
                            - (taker_fee / (addon_count * 100.0)))
                if net_edge < MIN_EDGE_PCT / 100.0:
                    continue

            # ── EXECUTE (or shadow-log the live tier) ──────────────
            self._session_dip_addon_attempts += 1

            if DIP_ADDON_SHADOW_MODE:
                logging.info(
                    "dip_addon_LIVE_SHADOW: %s %dx @ %dc (entry=%dc -%dc) "
                    "stc=%.0f edge=%.4f prob=%.4f bal=%dc",
                    ticker, addon_count, current_ask,
                    meta["entry_price_cents"], drop, current_stc,
                    net_edge, cal_prob, balance)
                self._logger.log_execution({
                    "action": "dip_addon_live_shadow",
                    "ticker": ticker, "asset": asset,
                    "entry_price": meta["entry_price_cents"],
                    "dip_price": current_ask, "drop_cents": drop,
                    "addon_count": addon_count, "edge": net_edge,
                    "prob": cal_prob, "stc": current_stc,
                    "balance": balance,
                })
                return  # One per tick

            # LIVE: taker IOC, single attempt
            logging.info(
                "dip_addon_TRIGGER: %s %dx @ %dc (entry=%dc -%dc) "
                "stc=%.0f edge=%.4f prob=%.4f bal=%dc",
                ticker, addon_count, current_ask,
                meta["entry_price_cents"], drop, current_stc,
                net_edge, cal_prob, balance)

            addon_candidate = {
                "ticker": ticker,
                "event_ticker": meta["event_ticker"],
                "asset": asset,
                "best_yes_ask": current_ask,
                "position_size": addon_count,
                "calibrated_prob": cal_prob,
                "edge": net_edge,
                "seconds_to_close": current_stc,
                "balance_at_scan": balance,
                "entry_path": "dip_addon",
                "strategy": "DIP_ADDON",
                "blended_rv": blended_rv,
                "threshold": threshold,
                "vol_regime": meta.get("candidate", {}).get("vol_regime"),
                "z_score": meta.get("candidate", {}).get("z_score"),
                "kelly_f": meta.get("candidate", {}).get("kelly_f"),
                "ob_snapshot": {},
                "original_entry_price": meta["entry_price_cents"],
                "original_entry_count": meta["entry_count"],
                "price_drop": drop,
            }

            result = self._submit_taker(addon_candidate)
            if result is not None:
                self._session_dip_addon_fills += 1
                logging.info("dip_addon_FILLED: %s %dx @ %dc (-%dc)",
                             ticker, addon_count, current_ask, drop)
                if _TELEGRAM:
                    try:
                        _cost = addon_count * current_ask / 100
                        _TELEGRAM.send(
                            f"Dip addon: {asset} {addon_count}ct "
                            f"@ {current_ask}c "
                            f"(${_cost:.2f}, -{drop}c from entry)")
                    except Exception:
                        pass
            else:
                logging.warning("dip_addon_UNFILLED: %s %dx @ %dc",
                                ticker, addon_count, current_ask)
            return  # One per tick max

    # ── Cancel ────────────────────────────────────────────────────────────

    def _cancel_order(self, asset: str, reason: str) -> bool:
        """Cancel the active maker order for a specific asset.

        Returns True if cancel succeeded (safe to submit replacement).
        Returns False if cancel API failed (order may still be live).
        """
        order = self._active_orders.get(asset)
        if order is None:
            return True  # nothing to cancel

        filled = order.get("filled_so_far", 0)

        cancel_resp = self._client.cancel_order(order["order_id"])
        if cancel_resp is None:
            logging.error(f"Cancel API FAILED for {order['order_id']} — order may still be resting on exchange")
            # Don't mark canceled in DB — order may still be live on Kalshi
            order["cancel_pending"] = True
            # Do NOT pop — order may still be live, prevent double position
            return False
        else:
            status = "partial_canceled" if filled > 0 else "canceled"
            self._state.mark_order_status(order["order_id"], status)

        # Log fill model sample for canceled order
        self._log_fill_model_sample(order, "canceled", cancel_reason=reason)

        self._logger.log_order({
            "action": "maker_canceled",
            "ticker": order["ticker"],
            "order_id": order["order_id"],
            "reason": reason,
            "elapsed": round(time.time() - order["submit_time"], 1),
            "filled_so_far": filled,
        })
        logging.info(
            f"Maker order canceled: {order['ticker']} reason={reason}"
            f"{' (partial fill: ' + str(filled) + '/' + str(order['count']) + ')' if filled > 0 else ''}"
        )
        self._active_orders.pop(asset, None)
        # Update order outcome — skip if escalating (escalation handler sets outcome)
        if asset not in self._escalating_assets:
            _outcome = "partial_fill" if filled > 0 else "canceled"
            self._state.update_evaluated_opportunity_order(
                order["ticker"], order_outcome=_outcome)
        return True

    def _cancel_active(self, reason: str):
        """Cancel all active maker orders. Used by _reprice_maker compat."""
        for asset in list(self._active_orders):
            self._cancel_order(asset, reason)


# ═════════════════════════════════════════════════════════════════════════════
#  SettlementTracker
# ═════════════════════════════════════════════════════════════════════════════

class SettlementTracker:
    """Incremental settlement poller. API is the single source of truth.

    Polls GET /portfolio/settlements?min_ts={last_check} every 30 seconds.
    On settlement: look up trade in SQLite, record WIN/LOSS from market_result,
    calculate net P&L from revenue, log to settlement_journal.jsonl, update
    running balance.

    Never uses z-score heuristics or balance deltas to determine outcomes.
    """

    def __init__(self, client: KalshiClient, state: StateManager,
                 logger: Logger, main_loop=None):
        self._client = client
        self._state = state
        self._logger = logger
        self._ml = main_loop
        self._last_check_ts: int = 0
        self._last_poll_time: float = 0.0
        self._processed_tickers: Set[str] = set()
        self._pending_rejection_tickers: Set[str] = set()
        self._settled_rejection_tickers: Set[str] = set()

    # ── Startup ──────────────────────────────────────────────────────────

    def startup(self):
        """Initialize watermark to 24h ago, load dedup set, sweep once."""
        self._last_check_ts = int(
            (datetime.datetime.now(timezone.utc) - datetime.timedelta(hours=24)).timestamp()
        )
        self._load_processed_tickers()
        self._load_pending_rejections()
        self._poll()

    def _load_pending_rejections(self):
        """Load unsettled rejected tickers from DB."""
        rows = self._state.get_unsettled_rejections()
        self._pending_rejection_tickers = {r["ticker"] for r in rows}
        logging.info(
            f"SettlementTracker: loaded {len(self._pending_rejection_tickers)} "
            f"pending rejected opportunities"
        )

    def _load_processed_tickers(self):
        """Load already-settled tickers from DB for deduplication."""
        rows = self._state.conn.execute(
            "SELECT ticker FROM settled_trades"
        ).fetchall()
        self._processed_tickers = {row["ticker"] for row in rows}
        logging.info(
            f"SettlementTracker: loaded {len(self._processed_tickers)} "
            f"previously settled tickers"
        )

    # ── Tick (called every main-loop iteration) ──────────────────────────

    def tick(self):
        """Self-throttled: only polls every SETTLEMENT_CHECK_SECONDS."""
        now = time.time()
        if now - self._last_poll_time < SETTLEMENT_CHECK_SECONDS:
            return
        self._last_poll_time = now
        self._poll()
        self._poll_rejections()
        self._poll_evaluated_opportunities()

    # ── Core poll ────────────────────────────────────────────────────────

    def _poll(self):
        """Fetch new settlements from API and process them."""
        unsettled = self._state.get_unsettled_positions()
        if not unsettled:
            return

        resp = self._client.get_settlements(min_ts=self._last_check_ts)
        if not resp or "settlements" not in resp:
            return

        settlements = resp["settlements"]
        if not settlements:
            return

        our_tickers = {p["ticker"] for p in unsettled}
        processed_any = False

        for s in settlements:
            ticker = s.get("ticker", "")

            # Skip if already processed (dedup)
            if ticker in self._processed_tickers:
                continue

            # Only process settlements for our open positions
            if ticker not in our_tickers:
                continue

            try:
                self._process_settlement(s)
                processed_any = True
            except Exception as e:
                logging.error(f"Settlement processing failed for {ticker}: {e}", exc_info=True)

        # Advance watermark to now (even if nothing processed, to shrink window)
        self._last_check_ts = int(datetime.datetime.now(timezone.utc).timestamp())

        # Refresh balance after processing settlements
        if processed_any:
            balance_resp = self._client.get_balance()
            if balance_resp:
                new_balance = balance_resp.get("balance") or 0
                logging.info(
                    f"Balance after settlements: ${new_balance / 100:.2f}"
                )

    # ── Process a single settlement ──────────────────────────────────────

    def _process_settlement(self, settlement: Dict):
        """Record outcome, P&L, and log to journal."""
        ticker = settlement["ticker"]
        market_result = settlement.get("market_result", "")
        rev_d = settlement.get("revenue_dollars")
        revenue = dollars_str_to_cents(rev_d) if rev_d else (settlement.get("revenue") or 0)

        # Look up position in SQLite
        pos = self._state.conn.execute(
            "SELECT * FROM positions WHERE ticker=?", (ticker,)
        ).fetchone()
        if not pos:
            logging.warning(
                f"SettlementTracker: no position found for {ticker}"
            )
            return
        pos = dict(pos)  # sqlite3.Row doesn't support .get()

        # Determine WIN/LOSS from market_result only (API is truth)
        side = pos["side"]
        if market_result == "yes":
            outcome = "WIN" if side == "yes" else "LOSS"
        elif market_result == "no":
            outcome = "WIN" if side == "no" else "LOSS"
        elif market_result == "all_no":
            outcome = "WIN" if side == "no" else "LOSS"
        elif market_result == "all_yes":
            outcome = "WIN" if side == "yes" else "LOSS"
        else:
            outcome = "UNKNOWN"
            logging.critical(
                f"UNKNOWN market_result '{market_result}' for {ticker} "
                f"— skipping settlement to prevent bad P&L recording"
            )
            return

        # Cross-check: revenue=0 on a WIN is almost certainly a false position
        # (e.g., false ghost fill where Kalshi has no matching position).
        # Log critical and skip to prevent recording a phantom loss.
        recorded_count = pos["count"]
        total_cost = pos["total_cost_cents"]
        if outcome == "WIN" and revenue == 0 and recorded_count > 0:
            logging.critical(
                f"SETTLEMENT REVENUE ZERO ON WIN {ticker}: "
                f"market_result={market_result} side={side} count={recorded_count} "
                f"cost={total_cost}¢ fill_source={pos.get('fill_source')} — "
                f"Kalshi likely has no matching position. "
                f"Skipping settlement to prevent false -{total_cost}¢ loss.")
            return

        # Cross-check: detect count mismatch between internal tracking
        # and Kalshi settlement.  For YES wins, revenue = real_count * 100.
        if revenue > 0 and outcome == "WIN" and side == "yes":
            implied_count = revenue // 100
            if implied_count != recorded_count:
                logging.error(
                    f"SETTLEMENT COUNT MISMATCH {ticker}: "
                    f"internal={recorded_count} kalshi={implied_count} "
                    f"revenue={revenue}¢ — correcting position before settlement")
                recorded_count = implied_count
                total_cost = recorded_count * pos["avg_price_cents"]
                self._state.conn.execute(
                    "UPDATE positions SET count=?, total_cost_cents=? "
                    "WHERE ticker=?",
                    (recorded_count, total_cost, ticker))
                self._state.conn.commit()

        # P&L: use total_cost_cents from positions (precise) for normal case,
        # recomputed cost only when count was corrected above.
        is_taker = bool(pos.get("is_taker"))
        fee = calculate_fee(recorded_count, pos["avg_price_cents"], is_taker=is_taker)
        pnl = revenue - total_cost

        # Record in SQLite via existing StateManager method
        self._state.record_settlement(settlement, pnl_override=pnl, fee_override=fee)

        # Mark as processed for dedup
        self._processed_tickers.add(ticker)

        # Rich journal entry
        self._logger.log_settlement({
            "ticker": ticker,
            "event_ticker": pos["event_ticker"],
            "asset": pos["asset"],
            "outcome": outcome,
            "market_result": market_result,
            "side": side,
            "count": recorded_count,
            "entry_price_cents": pos["avg_price_cents"],
            "total_cost_cents": total_cost,
            "revenue_cents": revenue,
            "fee_cents": fee,
            "pnl_cents": pnl,
            "pnl_net_cents": pnl - fee,
            "settled_time": settlement.get("settled_time", ""),
        })

        logging.info(
            f"Settlement: {ticker} -> {outcome} "
            f"(market_result={market_result}, "
            f"revenue={revenue}¢, cost={total_cost}¢, "
            f"pnl={pnl}¢, fee={fee}¢)"
        )
        if _TELEGRAM:
            emoji = "\u2705" if outcome == "WIN" else "\u274c"
            sign = "+" if pnl >= 0 else ""
            pnl_dollars = pnl / 100
            bal_str = ""
            try:
                bal_resp = self._client.get_balance()
                if bal_resp:
                    bal_str = f" | Balance: ${bal_resp.get('balance', 0) / 100:.2f}"
            except Exception:
                pass
            _TELEGRAM.send(
                f"{emoji} {outcome} {pos['asset']} {recorded_count}ct "
                f"@{pos['avg_price_cents']}c {sign}${abs(pnl_dollars):.2f}{bal_str}"
            )

    # ── Rejection Settlement ─────────────────────────────────────────────

    def register_rejection_ticker(self, ticker: str):
        """Called by scanner when a new rejection is recorded."""
        self._pending_rejection_tickers.add(ticker)

    def _poll_rejections(self):
        """Check if any rejected-opportunity tickers have settled."""
        # Refresh from DB to pick up rejections inserted by scanner since last poll
        db_rows = self._state.get_unsettled_rejections()
        for r in db_rows:
            self._pending_rejection_tickers.add(r["ticker"])

        if not self._pending_rejection_tickers:
            return

        # Snapshot to iterate safely
        tickers_to_check = list(
            self._pending_rejection_tickers - self._settled_rejection_tickers
        )
        for ticker in tickers_to_check:
            try:
                resp = self._client.get_market(ticker)
                if not resp:
                    continue
                market = resp.get("market", resp)
                result = market.get("result", "")
                if result:
                    self._process_rejection_settlement(market, ticker)
            except Exception as e:
                logging.warning(
                    f"Rejection settlement check failed for {ticker}: {e}", exc_info=True)

    def _process_rejection_settlement(self, market: Dict, ticker: str):
        """Compute counterfactual P&L for a rejected opportunity that settled."""
        result = market.get("result", "")

        # Look up the rejection row from SQLite
        row = self._state.conn.execute(
            "SELECT * FROM rejected_opportunities WHERE ticker=?", (ticker,)
        ).fetchone()
        if not row:
            return

        entry_price = row["market_price"]
        # If we never had a market price (pre-filter rejection), skip P&L calc
        if entry_price is None:
            would_have_profit = None
            assumed_fee = 0
            counterfactual_outcome = "unknown_no_price"
        else:
            # Counterfactual: bought 1 YES contract at entry_price (include taker fee)
            # Note: rejected_opportunities are always YES-side. NO-side goes through
            # evaluated_opportunities which has its own side-aware settlement in
            # _poll_evaluated_opportunities().
            assumed_fee = calculate_taker_fee(1, int(entry_price))
            if result in ("yes", "all_yes"):
                would_have_profit = (100 - entry_price) - assumed_fee  # cents
                counterfactual_outcome = "would_have_won"
            elif result in ("no", "all_no"):
                would_have_profit = -(entry_price + assumed_fee)  # cents
                counterfactual_outcome = "would_have_lost"
            else:
                would_have_profit = None
                counterfactual_outcome = f"unknown_result_{result}"

        cf_json = json.dumps({
            "outcome": counterfactual_outcome,
            "would_have_profit_cents": would_have_profit,
            "assumed_fee_cents": assumed_fee,
            "entry_price": entry_price,
        })

        self._logger.log_rejection({
            "type": "rejection_settlement",
            "ticker": ticker,
            "event_ticker": row["event_ticker"],
            "asset": row["asset"],
            "rejection_reason": row["rejection_reason"],
            "market_result": result,
            "entry_price_if_traded": entry_price,
            "counterfactual_outcome": counterfactual_outcome,
            "would_have_profit_cents": would_have_profit,
            "assumed_fee_cents": assumed_fee,
            "assumed_contracts": 1,
            "z_score": row["z_score"],
            "spot_price": row["spot_price"],
            "threshold": row["threshold"],
        })

        self._state.mark_rejection_settled(ticker, market_result=result,
                                           counterfactual=cf_json)
        self._settled_rejection_tickers.add(ticker)
        self._pending_rejection_tickers.discard(ticker)

        logging.info(
            f"Rejection settled: {ticker} -> {counterfactual_outcome} "
            f"(result={result}, would_have_profit={would_have_profit}¢)"
        )

    # ── Evaluated Opportunity Settlement ──────────────────────────────────

    @staticmethod
    def _parse_weather_market_date(ticker: str) -> Optional[str]:
        """Extract the market date from a weather ticker as YYYY-MM-DD.

        Ticker format: KXHIGHNY-26FEB28-T50 → date segment '26FEB28' → '2026-02-28'
        """
        parts = ticker.split("-")
        if len(parts) < 2:
            return None
        raw = parts[1]  # e.g. '26FEB28', '26MAR01', '26MAR03'
        if len(raw) < 7:
            return None
        try:
            dt = datetime.datetime.strptime(raw, "%y%b%d")
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            return None

    @staticmethod
    def _estimate_actual_temp_from_bracket(ticker, threshold, spot_price=None):
        """Estimate actual temperature from a settled weather bracket for bias update.

        B-type: midpoint of bracket (threshold is floor_strike, brackets ~2°F wide)
        T-type: threshold ± 2°F based on tail direction (uses spot_price to disambiguate)
        Returns estimated actual temperature or None.
        """
        parts = ticker.split("-")
        if len(parts) < 3:
            return None
        strike_part = parts[-1]
        if strike_part.startswith("B"):
            # Bracket: threshold is floor_strike, bracket ~2°F wide → midpoint
            return threshold + 1.0
        elif strike_part.startswith("T"):
            # Tail: use spot_price (ensemble mean at evaluation time) to determine direction
            if spot_price is not None:
                if threshold < spot_price:
                    return threshold - 2.0  # lower tail: actual below threshold
                else:
                    return threshold + 2.0  # upper tail: actual above threshold
            return None  # can't determine tail direction without spot_price
        return None

    def _poll_evaluated_opportunities(self):
        """Check if any evaluated opportunities have settled for counterfactual tracking.
        Groups rows by ticker to avoid redundant API calls and batches DB commits."""
        try:
            rows = self._state.get_unsettled_evaluated_opportunities()
        except Exception as e:
            logging.warning(f"get_unsettled_evaluated_opportunities failed: {e}", exc_info=True)
            return

        if not rows:
            return

        # Volume warning — high pending count means observation modes are flooding the table
        if len(rows) > 50:
            logging.warning(
                "eval_opp_settlement: %d pending rows (>50 threshold) — "
                "check observation mode volume (weather=%d, hourly=%d, spx=%d, sports=%d, 15m=%d)",
                len(rows),
                sum(1 for r in rows if r.get("product_type") == "weather"),
                sum(1 for r in rows if r.get("product_type") == "hourly"),
                sum(1 for r in rows if r.get("product_type") == "spx_hourly"),
                sum(1 for r in rows if r.get("product_type") == "sports"),
                sum(1 for r in rows if r.get("product_type") == "15m"),
            )

        # Group rows by ticker — one API call per unique ticker
        from collections import defaultdict
        ticker_groups: dict = defaultdict(list)
        for row in rows:
            ticker_groups[row["ticker"]].append(row)

        # Fetch market result once per unique ticker
        ticker_results: dict = {}
        for ticker in ticker_groups:
            try:
                resp = self._client.get_market(ticker)
                if not resp:
                    continue
                market = resp.get("market", resp)
                result = market.get("result", "")
                if result:
                    ticker_results[ticker] = result
            except Exception as e:
                logging.warning(f"get_market failed for {ticker}: {e}")

        if not ticker_results:
            return

        logging.info("eval_opp_settlement: %d unique tickers settled (from %d pending rows)",
                     len(ticker_results), len(rows))

        # ── Phase 1: Compute settlement results in memory (NO DB writes) ──
        # This avoids holding a write lock during the computation + JSONL logging.
        # Each entry: (opp_id, ticker, result, row, would_have_profit, counterfactual_outcome,
        #              count, taker_fee, maker_fee, pnl_taker, pnl_maker)
        _settlement_batch: list = []
        _cal_observations: list = []  # (raw_p, cal_binary, _opp_pt, asset, filter_stage)
        _weather_updates: list = []   # (opp_id, ticker, row) — need API calls, done after commit
        for ticker, result in ticker_results.items():
            for row in ticker_groups[ticker]:
                opp_id = row["id"]
                try:
                    entry_price = row["market_price"]
                    _opp_pt = row.get("product_type")
                    if entry_price is None:
                        would_have_profit = None
                        taker_fee = 0
                        maker_fee = 0
                        pnl_taker = None
                        pnl_maker = None
                        count = row.get("position_size") or 1
                        counterfactual_outcome = "unknown_no_price"
                    elif _opp_pt == "weather" and entry_price < WEATHER_MIN_ENTRY_PRICE:
                        count = row.get("position_size") or 1
                        would_have_profit = 0
                        counterfactual_outcome = "untradeable_price"
                        taker_fee = 0
                        maker_fee = 0
                        pnl_taker = 0
                        pnl_maker = 0
                    else:
                        count = row.get("position_size") or 1
                        taker_fee = calculate_taker_fee(count, int(entry_price))
                        maker_fee = calculate_maker_fee(count, int(entry_price))
                        _opp_side = row.get("side") or "yes"
                        if _opp_side == "no":
                            _is_win = result in ("no", "all_no")
                            _is_loss = result in ("yes", "all_yes")
                        else:
                            _is_win = result in ("yes", "all_yes")
                            _is_loss = result in ("no", "all_no")
                        if _is_win:
                            pnl_taker = (100 - entry_price) * count - taker_fee
                            pnl_maker = (100 - entry_price) * count - maker_fee
                            counterfactual_outcome = "would_have_won"
                        elif _is_loss:
                            pnl_taker = -(entry_price * count + taker_fee)
                            pnl_maker = -(entry_price * count + maker_fee)
                            counterfactual_outcome = "would_have_lost"
                        else:
                            pnl_taker = None
                            pnl_maker = None
                            taker_fee = 0
                            maker_fee = 0
                            counterfactual_outcome = f"unknown_result_{result}"
                        would_have_profit = pnl_taker

                    # JSONL logging (no DB write)
                    self._logger.log_rejection({
                        "type": "evaluated_settlement",
                        "ticker": ticker,
                        "event_ticker": row["event_ticker"],
                        "asset": row["asset"],
                        "filter_stage": row["filter_stage"],
                        "rejection_reason": row.get("rejection_reason"),
                        "market_result": result,
                        "entry_price_if_traded": entry_price,
                        "counterfactual_outcome": counterfactual_outcome,
                        "would_have_profit_cents": would_have_profit,
                        "assumed_contracts": count,
                        "taker_fee_cents": taker_fee,
                        "maker_fee_cents": maker_fee,
                        "pnl_taker_cents": pnl_taker,
                        "pnl_maker_cents": pnl_maker,
                        "calibrated_prob": row.get("calibrated_prob"),
                        "edge": row.get("edge"),
                        "strategy": row.get("strategy"),
                        "position_size": row.get("position_size"),
                        "kelly_f": row.get("kelly_f"),
                        "vol_regime": row.get("vol_regime"),
                        "z_score": row.get("z_score"),
                        "raw_prob": row.get("raw_prob"),
                        "calibration_method": row.get("calibration_method"),
                        "fee_adjusted_edge": row.get("fee_adjusted_edge"),
                        "old_system_prob": row.get("old_system_prob"),
                    })

                    _settlement_batch.append((opp_id, ticker, result, row,
                                              would_have_profit, counterfactual_outcome))

                    # Prepare CalEngine observations
                    raw_p = row.get("raw_prob")
                    filter_stage = row.get("filter_stage", "")
                    _opp_side = row.get("side") or "yes"
                    if (raw_p is not None and _opp_side == "yes"
                            and result in ("yes", "all_yes", "no", "all_no")
                            and not filter_stage.endswith("_v2")):
                        cal_binary = 1 if result in ("yes", "all_yes") else 0
                        _cal_observations.append((raw_p, cal_binary, _opp_pt,
                                                  row.get("asset"), filter_stage))

                    # Queue weather temp fetches for after commit
                    if (_opp_pt == "weather" and result in ("yes", "all_yes", "no", "all_no")
                            and row.get("wx_actual_high_temp") is None):
                        _weather_updates.append((opp_id, ticker, row))

                    logging.info(
                        f"Evaluated opp settled: {ticker} ({row['filter_stage']}) "
                        f"-> {counterfactual_outcome} (profit={would_have_profit}¢)"
                    )
                except Exception as e:
                    logging.warning(f"Evaluated opp settlement check failed for {ticker}: {e}", exc_info=True)

        # ── Phase 2: Fast DB writes (short lock, no API calls) ──
        _settled_count = 0
        if _settlement_batch:
            try:
                for (opp_id, ticker, result, row,
                     would_have_profit, counterfactual_outcome) in _settlement_batch:
                    self._state.mark_evaluated_opportunity_settled(
                        opp_id, market_result=result,
                        counterfactual_pnl=would_have_profit,
                        commit=False)
                    _settled_count += 1
                self._state.conn.commit()
                logging.info("eval_opp_settlement: batch committed %d rows", _settled_count)
            except Exception as e:
                logging.warning("eval_opp_settlement batch commit failed: %s", e, exc_info=True)

        # ── Phase 3: Post-commit work (CalEngine, shadow settlement, weather) ──
        # These run AFTER the write lock is released.

        # Feed CalEngine observations
        for (raw_p, cal_binary, _opp_pt, _asset, filter_stage) in _cal_observations:
            _settle_engine = _resolve_cal_engine(_opp_pt, _asset)
            if _settle_engine is not None:
                _settle_engine.add_observation(raw_p, cal_binary)
            elif (filter_stage in ("candidate", "observation_trade",
                                   "hourly_observation", "spx_observation",
                                   "weather_observation")
                  and get_market_config(_opp_pt).cal_eligible):
                if _CALIBRATION_ENGINE is not None:
                    _CALIBRATION_ENGINE.add_observation(raw_p, cal_binary)

        # Settle shadow signals (per unique settled ticker)
        for ticker, result in ticker_results.items():
            if result not in ("yes", "all_yes", "no", "all_no"):
                continue
            if self._ml and getattr(self._ml, "fifteenm_shadow", None):
                try:
                    self._ml.fifteenm_shadow.settle_signals(ticker, result)
                except Exception:
                    logging.warning("fifteenm_shadow settle failed for %s", ticker, exc_info=True)

            if self._ml and getattr(self._ml, "hourly_alt_shadow", None):
                try:
                    self._ml.hourly_alt_shadow.settle_signals(ticker, result)
                except Exception:
                    logging.warning("hourly_alt_shadow settle failed for %s", ticker, exc_info=True)

            if self._ml and getattr(self._ml, "spx_harrv_shadow", None):
                try:
                    self._ml.spx_harrv_shadow.settle_signals(ticker, result)
                except Exception:
                    logging.warning("spx_harrv_shadow settle failed for %s", ticker, exc_info=True)

            # Settle SOL Path C shadow entry for this ticker
            try:
                _pc_row = self._state.conn.execute(
                    "SELECT * FROM sol_pathc_shadow WHERE ticker=? AND status='pending'",
                    (ticker,)).fetchone()
                if _pc_row:
                    _pc = dict(_pc_row)
                    _is_win = result in ("yes", "all_yes")
                    _live_price = _pc["live_entry_price"]
                    _live_contracts = _pc["live_contracts"]
                    _pos_size = _pc["position_size"]

                    _live_fee = calculate_taker_fee(_live_contracts, _live_price)
                    if _is_win:
                        _live_pnl = (100 - _live_price) * _live_contracts - _live_fee
                    else:
                        _live_pnl = -(_live_price * _live_contracts + _live_fee)

                    _maker_price = _pc["pathc_maker_price"]
                    _maker_depth = _pc["pathc_depth_at_maker"] or 0
                    _maker_touched = _pc["obs_maker_price_touched"] or 0
                    _maker_contracts = min(_pos_size, _maker_depth) if _maker_touched else 0
                    _maker_fee = calculate_maker_fee(_maker_contracts, _maker_price) if _maker_contracts > 0 else 0
                    if _maker_contracts > 0:
                        if _is_win:
                            _maker_pnl = (100 - _maker_price) * _maker_contracts - _maker_fee
                        else:
                            _maker_pnl = -(_maker_price * _maker_contracts + _maker_fee)
                    else:
                        _maker_pnl = 0

                    _esc_ask = _pc["pathc_esc_ask"]
                    _esc_depth = _pc["pathc_esc_depth"] or 0
                    if _esc_ask is not None and _esc_depth > 0:
                        _esc_contracts = min(_pos_size, _esc_depth)
                        _esc_fee = calculate_taker_fee(_esc_contracts, _esc_ask)
                        if _is_win:
                            _esc_pnl = (100 - _esc_ask) * _esc_contracts - _esc_fee
                        else:
                            _esc_pnl = -(_esc_ask * _esc_contracts + _esc_fee)
                    else:
                        _esc_contracts = 0
                        _esc_pnl = 0

                    if _maker_touched and _maker_contracts > 0:
                        _remainder = max(0, _pos_size - _maker_contracts)
                        if _remainder > 0 and _esc_ask is not None and _esc_depth > 0:
                            _rem_contracts = min(_remainder, _esc_depth)
                            _rem_fee = calculate_taker_fee(_rem_contracts, _esc_ask)
                            if _is_win:
                                _rem_pnl = (100 - _esc_ask) * _rem_contracts - _rem_fee
                            else:
                                _rem_pnl = -(_esc_ask * _rem_contracts + _rem_fee)
                        else:
                            _rem_pnl = 0
                        _best_pnl = _maker_pnl + _rem_pnl
                    else:
                        _best_pnl = _esc_pnl

                    self._state.settle_sol_pathc_shadow(
                        ticker=ticker, market_result=result,
                        live_pnl=_live_pnl,
                        pathc_maker_pnl=_maker_pnl,
                        pathc_maker_contracts=_maker_contracts,
                        pathc_esc_pnl=_esc_pnl,
                        pathc_esc_contracts=_esc_contracts,
                        pathc_best_pnl=_best_pnl)
                    logging.info(
                        "sol_pathc_settled: %s result=%s live_pnl=%d maker_pnl=%d esc_pnl=%d best_pnl=%d",
                        ticker, result, _live_pnl, _maker_pnl, _esc_pnl, _best_pnl)
            except Exception:
                logging.warning("sol_pathc_shadow settle failed for %s", ticker, exc_info=True)

        # Weather: fetch actual temps (API calls — after lock released)
        _wx_dirty = False
        for (opp_id, ticker, row) in _weather_updates:
            try:
                _wx_city = row["asset"].replace("_TEMP", "")
                _market_date = self._parse_weather_market_date(ticker)
                if _market_date:
                    _today = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%d")
                    if _market_date < _today:
                        _wx_eng = getattr(self._ml, "weather_engine", None) if self._ml else None
                        if _wx_eng:
                            _obs_high = _wx_eng._fetcher.fetch_observed_high(_wx_city, _market_date)
                            if _obs_high is not None:
                                self._state.conn.execute(
                                    "UPDATE evaluated_opportunities SET wx_actual_high_temp=? WHERE id=?",
                                    (_obs_high, opp_id))
                                _wx_dirty = True
                                logging.info("weather_observed_temp: %s %s %.1fF",
                                             _wx_city, _market_date, _obs_high)
                                forecast_mean = row.get("spot_price")
                                if forecast_mean:
                                    _wx_eng._model.update_bias(
                                        _wx_city, _obs_high, forecast_mean,
                                        market_date=_market_date)
                                    logging.info("weather_bias_update: %s %s actual=%.1fF forecast=%.1fF",
                                                 _wx_city, _market_date, _obs_high, forecast_mean)
            except Exception as e:
                logging.warning("weather_observed_temp fetch failed for %s: %s", ticker, e)
        if _wx_dirty:
            self._state.conn.commit()

        # Backfill wx_actual_high_temp for settled weather entries that missed it
        self._backfill_weather_actual_temps()

    def _backfill_weather_actual_temps(self):
        """Retry archive API fetch for settled weather entries missing wx_actual_high_temp."""
        try:
            rows = self._state.conn.execute(
                "SELECT id, ticker, asset, spot_price FROM evaluated_opportunities "
                "WHERE product_type='weather' AND status='settled' "
                "AND wx_actual_high_temp IS NULL LIMIT 10"
            ).fetchall()
        except Exception:
            return
        if not rows:
            return
        _today = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%d")
        _wx_eng = getattr(self._ml, "weather_engine", None) if self._ml else None
        if not _wx_eng:
            return
        for r in rows:
            opp_id, ticker, asset, forecast_mean = r
            try:
                _wx_city = asset.replace("_TEMP", "")
                _market_date = self._parse_weather_market_date(ticker)
                if not _market_date or _market_date >= _today:
                    continue
                _obs_high = _wx_eng._fetcher.fetch_observed_high(_wx_city, _market_date)
                if _obs_high is not None:
                    self._state.conn.execute(
                        "UPDATE evaluated_opportunities SET wx_actual_high_temp=? WHERE id=?",
                        (_obs_high, opp_id))
                    self._state.conn.commit()
                    logging.info("weather_backfill_temp: %s %s %.1fF", _wx_city, _market_date, _obs_high)
                    # Bias update with real observed temp
                    if forecast_mean:
                        _wx_eng._model.update_bias(
                            _wx_city, _obs_high, forecast_mean,
                            market_date=_market_date)
            except Exception as e:
                logging.warning("weather_backfill failed for %s: %s", ticker, e)


# ═════════════════════════════════════════════════════════════════════════════
#  Market Discovery
# ═════════════════════════════════════════════════════════════════════════════

def discover_active_windows(client: KalshiClient) -> List[Dict]:
    """
    Query Kalshi for currently open crypto windows (15M + hourly).

    Uses the events endpoint (GET /events) with status=open and
    with_nested_markets=true to find tradeable markets. The markets
    endpoint (GET /markets) with series_ticker only returns pre-created
    'initialized' markets on production, missing the active ones.

    Returns list of dicts with asset, event_ticker, close_time,
    seconds_to_close, markets list, and product_type.
    """
    now = datetime.datetime.now(timezone.utc)
    windows: List[Dict] = []

    # Build combined series list: 15M always, hourly when enabled
    series_list = [(a, s, "15m") for a, s in SERIES_TICKERS.items()]
    if HOURLY_OBSERVATION_ENABLED:
        series_list += [(a, s, "hourly") for a, s in HOURLY_SERIES_TICKERS.items()]

    for asset, series, product_type in series_list:
        result = client.get_events(
            series_ticker=series,
            status="open",
            with_nested_markets=True,
            limit=100,
        )
        events = result.get("events") if result else None
        if not events:
            logging.warning(
                f"Market discovery: {asset} ({series}) — API returned no data"
            )
            continue
        market_count = 0

        for event in events:
            event_ticker = event.get("event_ticker", "")
            nested_markets = event.get("markets", [])
            if not isinstance(nested_markets, list):
                continue

            # Filter to actual market dicts (not string references)
            mkts = [m for m in nested_markets if isinstance(m, dict)]
            if not mkts:
                continue

            market_count += len(mkts)

            close_time_str = mkts[0].get("close_time", "")
            try:
                close_time = datetime.datetime.fromisoformat(
                    close_time_str.replace("Z", "+00:00")
                )
            except (ValueError, AttributeError):
                continue

            seconds_to_close = (close_time - now).total_seconds()
            if seconds_to_close < 0:
                continue
            windows.append({
                "asset": asset,
                "event_ticker": event_ticker,
                "close_time": close_time,
                "seconds_to_close": seconds_to_close,
                "markets": mkts,
                "product_type": product_type,
            })

        if market_count == 0:
            logging.info(
                f"Market discovery: {asset} ({series}) — 0 open markets"
            )
        else:
            logging.info(
                f"Market discovery: {asset} ({series}) — "
                f"{market_count} markets in {len(events)} windows"
            )

    return windows


# ═════════════════════════════════════════════════════════════════════════════
#  MainLoop
# ═════════════════════════════════════════════════════════════════════════════

class MainLoop:
    """Continuous observation loop. Scans active windows every second."""

    def __init__(self):
        api_key = os.environ.get("KALSHI_API_KEY") or os.environ.get("KALSHI_API_KEY_ID", "")
        private_key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
        if not api_key or not private_key_path:
            logging.critical(
                "KALSHI_API_KEY (or KALSHI_API_KEY_ID) and KALSHI_PRIVATE_KEY_PATH must be set"
            )
            sys.exit(1)

        self.client = KalshiClient(api_key, private_key_path)
        self.state = StateManager()
        self.logger = Logger()
        self.feed = CoinbaseFeed()
        self.dvol_fetcher = DeribitDVOLFetcher()
        self.egarch_estimator = EGARCHEstimator()
        self.mz_tracker = MincerZarnowitzTracker()
        self.vol = VolatilityEngine(self.feed, dvol_fetcher=self.dvol_fetcher,
                                    egarch_estimator=self.egarch_estimator,
                                    mz_tracker=self.mz_tracker)
        self.sizer = PositionSizer()
        self.calibration = CalibrationEngine()
        global _CALIBRATION_ENGINE
        _CALIBRATION_ENGINE = self.calibration

        # Per-market CalEngines via registry
        global _CAL_REGISTRY
        _CAL_REGISTRY.clear()  # defensive: ensure clean state on restart
        self._cal_engines = {}
        self._cal_engine_meta = {}  # reg_key → (product_type, subtype_code_or_None)

        for _pt, _cfg in MARKET_CONFIGS.items():
            if _pt == "15m":
                continue  # 15M uses _CALIBRATION_ENGINE — NEVER in registry

            if _cfg.cal_subtypes:
                # Per-subtype engines (weather cities, sports groups)
                for _sub_code, _sub_path in _cfg.cal_subtypes.items():
                    _reg_key = f"{_pt}_{_sub_code}"
                    assert _sub_path != CALIBRATION_STATE_PATH, (
                        f"FATAL: {_reg_key} would share state file with 15M engine!")
                    _engine = CalibrationEngine(
                        state_path=_sub_path,
                        label=f"{_pt.capitalize()}_{_sub_code}Cal")
                    self._cal_engines[_reg_key] = _engine
                    _CAL_REGISTRY[_reg_key] = _engine
                    self._cal_engine_meta[_reg_key] = (_pt, _sub_code)
                    logging.info("CalEngine registered for '%s' (state: %s, enabled=%s)",
                                 _reg_key, _sub_path, _cfg.cal_engine_enabled)

            elif _cfg.cal_engine_state_path:
                # Single engine per product_type (hourly, spx_hourly)
                # Always instantiate so settlement can collect observations via add_observation().
                # require_enabled gate in _resolve_cal_engine() prevents disabled engines
                # from affecting predictions.
                assert _cfg.cal_engine_state_path != CALIBRATION_STATE_PATH, (
                    f"FATAL: {_pt} would share state file with 15M engine!")
                _engine = CalibrationEngine(
                    state_path=_cfg.cal_engine_state_path,
                    label=f"{_pt.capitalize()}Cal")
                self._cal_engines[_pt] = _engine
                _CAL_REGISTRY[_pt] = _engine
                self._cal_engine_meta[_pt] = (_pt, None)
                logging.info("CalEngine registered for '%s' (state: %s, enabled=%s)",
                             _pt, _cfg.cal_engine_state_path, _cfg.cal_engine_enabled)

            else:
                logging.info("CalEngine DISABLED for '%s': no state path, passthrough", _pt)

        assert "15m" not in _CAL_REGISTRY, "FATAL: 15M engine must never be in _CAL_REGISTRY"

        # Backward compat for dashboard_snapshot.py
        self.hourly_calibration = self._cal_engines.get("hourly")
        tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "")
        self.telegram = TelegramNotifier(tg_token, tg_chat)
        global _TELEGRAM
        _TELEGRAM = self.telegram
        self.cross_feed = CrossExchangeFeed(self.feed) if CROSS_EXCHANGE_ENABLED else None
        self.coinglass = CoinGlassFetcher()
        self.kalshi_oft = KalshiOrderFlowTracker() if KALSHI_OFT_ENABLED else None
        self.order_flow = OrderFlowEngine(
            cross_feed=self.cross_feed, coinglass=self.coinglass,
            kalshi_oft=self.kalshi_oft,
        )
        # Kalshi WebSocket feed for real-time fills + orderbook
        try:
            self.kalshi_feed = KalshiFeed(api_key, self.client.private_key)
        except Exception as e:
            logging.warning(f"KalshiFeed init failed: {e}")
            self.kalshi_feed = None
        # ── SPX Engine (conditional) ────────────────────────────────────
        self.spx_engine = None
        if SPX_HOURLY_ENABLED:
            try:
                from spx_engine import SPXEngine
                self.spx_engine = SPXEngine(
                    polygon_key=os.environ.get("POLYGON_API_KEY"),
                    finnhub_key=os.environ.get("FINNHUB_API_KEY"),
                )
                logging.info("SPX engine initialized")
            except Exception as e:
                logging.warning(f"SPX engine unavailable: {e}")

        # ── Weather Engine (conditional) ───────────────────────────────────
        self.weather_engine = None
        if WEATHER_ENABLED:
            try:
                from weather_engine import WeatherEngine
                self.weather_engine = WeatherEngine(db_path=DB_PATH)
                logging.info("Weather engine initialized")
            except Exception as e:
                logging.warning(f"Weather engine unavailable: {e}")

        # ── 15M Shadow Engine (recalibrated EGARCH + LightGBM) ─────────
        self.fifteenm_shadow = None
        try:
            from fifteenm_shadow import FifteenMShadowEngine, FIFTEENM_SHADOW_ENABLED
            if FIFTEENM_SHADOW_ENABLED:
                self.fifteenm_shadow = FifteenMShadowEngine(db_path=DB_PATH)
                logging.info("15M shadow engine initialized (recalibrated EGARCH + LightGBM)")
        except Exception as e:
            logging.warning(f"15M shadow engine unavailable: {e}")

        # ── Hourly Alt Shadow Engine (ETH/SOL/XRP shadow strategies) ──────
        self.hourly_alt_shadow = None
        try:
            from hourly_alt_shadow import HourlyAltShadowEngine, HOURLY_ALT_SHADOW_ENABLED
            if HOURLY_ALT_SHADOW_ENABLED and HOURLY_OBSERVATION_ENABLED:
                self.hourly_alt_shadow = HourlyAltShadowEngine(db_path=DB_PATH)
                logging.info("Hourly alt shadow engine initialized (MM + HAR-RV)")
        except Exception as e:
            logging.warning(f"Hourly alt shadow engine unavailable: {e}")

        # ── SPX HAR-RV Shadow Engine ────────────────────────────────────────
        self.spx_harrv_shadow = None
        if SPX_HOURLY_ENABLED:
            try:
                from spx_harrv_shadow import SPXHARRVShadowEngine, SPX_HARRV_SHADOW_ENABLED
                if SPX_HARRV_SHADOW_ENABLED:
                    self.spx_harrv_shadow = SPXHARRVShadowEngine(db_path=DB_PATH)
                    logging.info("SPX HAR-RV shadow engine initialized")
            except Exception as e:
                logging.warning(f"SPX HAR-RV shadow engine unavailable: {e}")

        # ── Sports Engine (conditional) ────────────────────────────────────
        self.sports_engine = None
        if SPORTS_ENABLED:
            try:
                from sports_engine import SportsEngine
                self.sports_engine = SportsEngine(
                    kalshi_client=self.client,
                    state_manager=self.state,
                    db_path=DB_PATH,
                )
                logging.info("Sports engine initialized")
            except Exception as e:
                logging.warning(f"Sports engine unavailable: {e}")

        # ── Capital Allocator (conditional) ────────────────────────────────
        self.capital_allocator = None
        try:
            from capital_allocator import CapitalAllocator
            _obs_strategies = set()
            for _k, _v in MARKET_CONFIGS.items():
                if _v.observation_only:
                    # Capital allocator uses "crypto_hourly" for hourly, product_type for others
                    _obs_strategies.add("crypto_hourly" if _k == "hourly" else _k)
            self.capital_allocator = CapitalAllocator(observation_strategies=_obs_strategies)
            logging.info("Capital allocator initialized")
        except Exception as e:
            logging.warning(f"Capital allocator unavailable: {e}")

        self.scanner = OpportunityScanner(
            self.client, self.state, self.feed, self.vol, self.logger,
            self.sizer, order_flow=self.order_flow,
            kalshi_oft=self.kalshi_oft,
            kalshi_feed=self.kalshi_feed,
            main_loop=self,
        )
        self.executor = OrderExecutor(
            self.client, self.state, self.logger,
            main_loop=self, kalshi_feed=self.kalshi_feed)
        self.executor._kalshi_oft = self.kalshi_oft
        self.tracker = SettlementTracker(self.client, self.state, self.logger,
                                         main_loop=self)
        self._shutdown = threading.Event()
        self._active_windows: List[Dict] = []
        self._discovery_ob_tickers: set = set()
        self._last_market_refresh: float = 0.0
        self._last_wal_checkpoint: float = 0.0
        self._last_error: Optional[str] = None
        self._last_error_time: float = 0.0
        self._start_time: float = time.time()
        self._peak_balance: float = 0.0
        self._balance_history: deque = deque(maxlen=8640)  # ~24h at 10s intervals
        self._recent_fill_latencies: deque = deque(maxlen=100)
        self._session_fill_count: int = 0
        self._session_maker_submissions: int = 0
        self._session_maker_fills: int = 0
        self._last_summary_date: Optional[str] = None
        self._observation_mode: bool = OBSERVATION_MODE

    # ── Signal Handling ───────────────────────────────────────────────────

    def _setup_signals(self):
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

    def _handle_signal(self, signum, _frame):
        name = signal.Signals(signum).name
        logging.info(f"Received {name}, shutting down gracefully...")
        self._shutdown.set()

    # ── Startup ───────────────────────────────────────────────────────────

    def startup(self):
        logging.info("Bot starting up...")

        # Load previously logged fill IDs
        self.logger.load_logged_fill_ids()

        # Verify API connectivity
        balance_resp = self.client.get_balance()
        if balance_resp is None:
            logging.critical("Cannot connect to Kalshi API — check credentials")
            sys.exit(1)
        balance_cents = balance_resp.get("balance") or 0
        self.sizer.starting_balance_cents = balance_cents
        self._peak_balance = balance_cents / 100
        logging.info(f"Connected to Kalshi. Balance: ${balance_cents / 100:.2f}")
        if _TELEGRAM:
            _TELEGRAM.send(f"\U0001f7e2 Bot started \u2014 Balance: ${balance_cents / 100:.2f}")

        # Reconcile local state with API
        self.state.reconcile_with_api(self.client)

        # Check for settlements that happened while bot was down
        self.tracker.startup()

        # Backfill raw_prob for calibration data
        self._backfill_calibration_data()

        # Load calibration training data from historical settlements
        self.calibration.load_training_data_from_db(self.state)

        # Run adaptive-vs-fixed backtest on startup
        backtest_result = self.calibration.backtest_adaptive_vs_fixed()
        if backtest_result:
            logging.info("Startup backtest result: %s", backtest_result)

        if _TELEGRAM and self.calibration.active_method != "fixed_beta":
            bt_msg = ""
            if backtest_result:
                bt_msg = (
                    f"\nBacktest: Brier {backtest_result['old_brier']:.4f} -> "
                    f"{backtest_result['new_brier']:.4f} "
                    f"({backtest_result['cap_truncated_count']} cap-truncated)"
                )
            _TELEGRAM.send(
                f"\U0001f9e0 Calibration: {self.calibration.active_method} trained "
                f"({len(self.calibration._observations)} obs){bt_msg}"
            )

        # Load per-market calibration training data (separate from 15M)
        for _reg_key, _engine in self._cal_engines.items():
            _load_pt, _sub_code = self._cal_engine_meta[_reg_key]
            _load_asset = _derive_asset_filter(_load_pt, _sub_code) if _sub_code else None
            _engine.load_training_data_from_db(
                self.state, product_type_include=_load_pt, asset_filter=_load_asset)
            _bt = _engine.backtest_adaptive_vs_fixed()
            if _bt:
                logging.info("Startup %s cal backtest: %s", _reg_key, _bt)
            logging.info("CONFIG_VERIFY (%s_cal): method=%s active=%s obs=%d",
                         _reg_key, _engine.active_method,
                         _engine.is_learned_method_active(), len(_engine._observations))

        # Start Coinbase price feed
        self.feed.start()
        logging.info("Coinbase price feed starting...")

        # Start Deribit DVOL fetcher
        self.dvol_fetcher.start()
        logging.info("Deribit DVOL fetcher starting...")

        # Start cross-exchange feeds
        if self.cross_feed:
            self.cross_feed.start()
            logging.info("Cross-exchange feed starting...")

        # Start CoinGlass funding rate fetcher
        self.coinglass.start()

        # Start Kalshi WebSocket feed (fills + orderbook)
        if self.kalshi_feed:
            try:
                self.kalshi_feed.start()
                logging.info("Kalshi WebSocket feed starting...")
            except Exception as e:
                logging.warning(f"Kalshi WebSocket feed failed to start: {e}")

        # Start SPX engine price feed (if enabled)
        if self.spx_engine:
            try:
                self.spx_engine.start()
                logging.info("SPX engine starting...")
            except Exception as e:
                logging.warning(f"SPX engine failed to start: {e}")
                self.spx_engine = None

        # Start Weather engine ensemble fetcher (if enabled)
        if self.weather_engine:
            try:
                self.weather_engine.start()
                logging.info("Weather engine starting...")
            except Exception as e:
                logging.warning(f"Weather engine failed to start: {e}")
                self.weather_engine = None

        # Start Sports engine (if enabled)
        if self.sports_engine:
            try:
                self.sports_engine.start()
                logging.info("Sports engine starting...")
            except Exception as e:
                logging.warning(f"Sports engine failed to start: {e}")
                self.sports_engine = None

        # Dashboard snapshot builder (used by Supabase syncer)
        try:
            from dashboard_snapshot import DashboardSnapshotBuilder
            self.snapshot_builder = DashboardSnapshotBuilder(self)
        except Exception as e:
            logging.info(f"Dashboard snapshot builder not available: {e}")
            self.snapshot_builder = None

        # Start Supabase syncer (if configured)
        try:
            from supabase_sync import SupabaseSyncer
            self.supabase_syncer = SupabaseSyncer(self)
            self.supabase_syncer.start()
        except Exception as e:
            logging.info(f"Supabase sync not available: {e}")
            self.supabase_syncer = None

        # Initial market scan
        self._refresh_active_windows()

        logging.info(
            f"Startup complete. Monitoring {len(ASSETS)} assets "
            f"({', '.join(ASSETS)})"
        )

    # ── Periodic Tasks ────────────────────────────────────────────────────

    def _refresh_active_windows(self):
        self._active_windows = discover_active_windows(self.client)

        # Merge SPX windows (if engine available and market open)
        if self.spx_engine and self.spx_engine.is_market_open():
            try:
                spx_windows = self.spx_engine.get_active_windows(self.client)
                self._active_windows.extend(spx_windows)
            except Exception as e:
                logging.warning(f"SPX window discovery failed: {e}")

        # Merge weather windows (if engine available)
        if self.weather_engine:
            try:
                wx_windows = self.weather_engine.get_active_windows(self.client)
                self._active_windows.extend(wx_windows)
            except Exception as e:
                logging.warning(f"Weather window discovery failed: {e}")

        self._last_market_refresh = time.time()
        n = len(self._active_windows)
        if n == 0:
            logging.warning("Market refresh returned 0 active windows — scanner idle")
        else:
            logging.debug(f"Refreshed: {n} active windows")

    def _subscribe_discovery_orderbooks(self):
        """Subscribe to WS orderbook_delta for all discovered tickers.

        Dashboard visibility ONLY — does NOT affect Scanner.scan(),
        execution, or any trading logic. Called every 30s after
        _refresh_active_windows(). Skips hourly tickers (observation only).
        """
        if not self.kalshi_feed or not self.kalshi_feed.is_connected:
            return
        try:
            active_tickers: set = set()
            for window in self._active_windows:
                if window.get("product_type") == "hourly":
                    continue  # skip hourly tickers from WS subscription
                for mkt in window.get("markets", []):
                    ticker = mkt.get("ticker", "")
                    if ticker:
                        active_tickers.add(ticker)

            # Unsubscribe expired tickers from previous cycle
            expired = self._discovery_ob_tickers - active_tickers
            for ticker in expired:
                try:
                    self.kalshi_feed.unsubscribe_ticker(ticker)
                except Exception:
                    pass

            # Subscribe to current active tickers (idempotent)
            for ticker in active_tickers:
                try:
                    self.kalshi_feed.subscribe_ticker(ticker)
                except Exception:
                    pass

            if expired:
                logging.info(
                    f"discovery_ob_cleanup: unsubscribed {len(expired)} expired tickers"
                )
            self._discovery_ob_tickers = active_tickers
        except Exception as e:
            logging.warning(f"discovery_ob_subscribe failed: {e}")

    # ── Calibration Backfill ─────────────────────────────────────────────

    def _backfill_calibration_data(self):
        """One-time backfill of raw_prob for calibration training data."""
        try:
            # Step A: Backfill evaluated_opportunities raw_prob
            rows = self.state.conn.execute(
                "SELECT id, asset, spot_price, threshold, volatility, "
                "seconds_to_close, z_score "
                "FROM evaluated_opportunities "
                "WHERE raw_prob IS NULL "
                "AND spot_price IS NOT NULL AND threshold IS NOT NULL "
                "AND volatility IS NOT NULL AND seconds_to_close IS NOT NULL"
            ).fetchall()

            eval_count = 0
            for row in rows:
                z = row["z_score"]
                if z is None:
                    spot = row["spot_price"]
                    thresh = row["threshold"]
                    vol = row["volatility"]
                    ttc = row["seconds_to_close"]
                    if vol <= 0 or ttc <= 0:
                        continue
                    sigma_move = spot * vol * math.sqrt(ttc / 5.0)
                    if sigma_move <= 0:
                        continue
                    z = (thresh - spot) / sigma_move
                raw_prob = ProbabilityEngine._cdf_complement(z, row["asset"])
                self.state.conn.execute(
                    "UPDATE evaluated_opportunities SET raw_prob = ? WHERE id = ?",
                    (raw_prob, row["id"]),
                )
                eval_count += 1
            if eval_count:
                self.state.conn.commit()
                logging.info("Backfill: updated raw_prob for %d evaluated_opportunities", eval_count)

            # Step B: Backfill rejected_opportunities raw_prob + market_result
            rej_results = {}
            if os.path.exists(REJECTION_JOURNAL):
                with open(REJECTION_JOURNAL, "r") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if entry.get("type") == "rejection_settlement":
                            rej_results[entry["ticker"]] = entry["market_result"]

            rej_rows = self.state.conn.execute(
                "SELECT ticker, asset, z_score FROM rejected_opportunities "
                "WHERE raw_prob IS NULL AND z_score IS NOT NULL"
            ).fetchall()

            rej_count = 0
            for row in rej_rows:
                z = row["z_score"]
                raw_prob = ProbabilityEngine._cdf_complement(z, row["asset"])
                market_result = rej_results.get(row["ticker"])
                self.state.conn.execute(
                    "UPDATE rejected_opportunities SET raw_prob = ?, market_result = ? "
                    "WHERE ticker = ?",
                    (raw_prob, market_result, row["ticker"]),
                )
                rej_count += 1
            if rej_count:
                self.state.conn.commit()
                logging.info("Backfill: updated raw_prob for %d rejected_opportunities", rej_count)

        except Exception as e:
            logging.warning("Backfill calibration data failed: %s", e)

    # ── Daily Summary ─────────────────────────────────────────────────────

    def _log_daily_summary(self):
        """Log aggregated daily performance metrics on date change."""
        try:
            today = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if self._last_summary_date is None:
                self._last_summary_date = today
                return
            if today == self._last_summary_date:
                return

            yesterday = self._last_summary_date
            self._last_summary_date = today

            # Aggregate settled trades for yesterday
            rows = self.state.conn.execute(
                "SELECT * FROM settled_trades WHERE settled_at LIKE ?",
                (yesterday + "%",)
            ).fetchall()

            total_pnl = 0
            total_fees = 0
            wins = 0
            losses = 0
            per_asset: Dict[str, Dict] = {}

            for row in rows:
                r = dict(row)
                pnl = r["pnl_cents"]
                total_pnl += pnl
                total_fees += r["fee_cents"]
                if pnl > 0:
                    wins += 1
                else:
                    losses += 1

                a = r["asset"]
                if a not in per_asset:
                    per_asset[a] = {"trades": 0, "wins": 0, "losses": 0, "pnl_cents": 0}
                per_asset[a]["trades"] += 1
                per_asset[a]["pnl_cents"] += pnl
                if pnl > 0:
                    per_asset[a]["wins"] += 1
                else:
                    per_asset[a]["losses"] += 1

            # Rejection counts for yesterday
            rej_rows = self.state.conn.execute(
                "SELECT rejection_reason, COUNT(*) as cnt FROM rejected_opportunities "
                "WHERE rejection_time LIKE ? GROUP BY rejection_reason",
                (yesterday + "%",)
            ).fetchall()
            rejection_counts = {r["rejection_reason"]: r["cnt"] for r in rej_rows}

            # Evaluated opportunity outcomes for yesterday
            eval_rows = self.state.conn.execute(
                "SELECT filter_stage, status, COUNT(*) as cnt FROM evaluated_opportunities "
                "WHERE evaluation_time LIKE ? GROUP BY filter_stage, status",
                (yesterday + "%",)
            ).fetchall()
            eval_counts = {}
            for r in eval_rows:
                stage = r["filter_stage"]
                if stage not in eval_counts:
                    eval_counts[stage] = {}
                eval_counts[stage][r["status"]] = r["cnt"]

            self.logger.log_performance({
                "summary_type": "daily",
                "date": yesterday,
                "total_trades": wins + losses,
                "wins": wins,
                "losses": losses,
                "total_pnl_cents": total_pnl,
                "total_fees_cents": total_fees,
                "per_asset": per_asset,
                "rejection_counts": rejection_counts,
                "evaluated_opportunity_counts": eval_counts,
            })
            logging.info(
                f"Daily summary ({yesterday}): {wins}W/{losses}L, "
                f"PnL={total_pnl}¢, fees={total_fees}¢"
            )
            if _TELEGRAM:
                _TELEGRAM.send(
                    f"\U0001f4c8 Daily ({yesterday}): {wins}W/{losses}L, "
                    f"PnL=${total_pnl / 100:+.2f}, fees=${total_fees / 100:.2f}"
                )
        except Exception as e:
            logging.debug(f"_log_daily_summary failed: {e}")

    # ── Main Tick ─────────────────────────────────────────────────────────

    def _tick(self):
        now = time.time()

        # Update peak balance from main thread (dashboard reads only)
        cached_bal = self.scanner._balance_cache[0]
        if cached_bal is not None:
            bal_dollars = cached_bal / 100.0
            if bal_dollars > self._peak_balance:
                self._peak_balance = bal_dollars

        # Refresh market list periodically
        if now - self._last_market_refresh >= MARKET_REFRESH_SECONDS:
            self._refresh_active_windows()
            self._subscribe_discovery_orderbooks()   # dashboard visibility

        # Check settlements periodically (self-throttled)
        self.tracker.tick()
        self._log_daily_summary()

        # Periodic WAL checkpoint (every 60s) — prevents WAL bloat that causes
        # "database is locked" across shadow engines with 7 concurrent connections.
        if now - self._last_wal_checkpoint >= 60.0:
            try:
                self.state.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self._last_wal_checkpoint = now
            except Exception:
                logging.debug("WAL checkpoint failed (busy)", exc_info=True)

        # Periodic calibration retrain check
        if self.calibration:
            self.calibration.maybe_retrain()
        for _rk, _eng in self._cal_engines.items():
            _eng.maybe_retrain()

        # Periodic EGARCH MLE refit
        if self.egarch_estimator:
            self.egarch_estimator.maybe_refit()

        # Recompute seconds_to_close and log each window (skip hourly vol diagnostics)
        utc_now = datetime.datetime.now(timezone.utc)
        prices = self.feed.get_all_prices()

        # Feed price data to hourly alt shadow engine for HAR-RV return computation
        if self.hourly_alt_shadow:
            _alt_ts = time.time()
            for _alt_asset, _alt_price in prices.items():
                if _alt_price is not None and _alt_price > 0:
                    try:
                        self.hourly_alt_shadow.ingest_price(_alt_asset, _alt_price, _alt_ts)
                    except Exception:
                        pass

        # Feed SPX price to HAR-RV shadow engine for return computation
        if self.spx_harrv_shadow and self.spx_engine:
            try:
                _spx_spot = self.spx_engine.get_spot_price("SPX")
                if _spx_spot is not None and _spx_spot > 0:
                    self.spx_harrv_shadow.ingest_price(_spx_spot, time.time())
            except Exception:
                pass

        for window in self._active_windows:
            seconds_to_close = (window["close_time"] - utc_now).total_seconds()
            window["seconds_to_close"] = seconds_to_close

            if window.get("product_type") == "hourly":
                continue  # volume control: skip scan journal writes for hourly

            in_range = (
                MIN_SECONDS_BEFORE_CLOSE
                <= seconds_to_close
                <= MAX_SECONDS_BEFORE_CLOSE
            )

            asset = window["asset"]
            vol_estimate = self.vol.update(asset, seconds_to_close=seconds_to_close)

            scan_entry = {
                "asset": asset,
                "event_ticker": window["event_ticker"],
                "seconds_to_close": round(seconds_to_close, 1),
                "in_trading_range": in_range,
                "num_markets": len(window["markets"]),
                "spot_price": prices.get(asset),
                "buffer_len": len(self.feed.get_buffer(asset)),
            }
            if vol_estimate:
                scan_entry.update({
                    "rv_1min": round(vol_estimate["rv_1min"], 8),
                    "rv_5min": round(vol_estimate["rv_5min"], 8),
                    "rv_15min": round(vol_estimate["rv_15min"], 8),
                    "blended_rv": round(vol_estimate["blended_rv"], 8),
                    "vol_regime": vol_estimate["regime"],
                    "vol_returns": vol_estimate["num_returns"],
                    "bv_1min": round(vol_estimate.get("bv_1min", 0), 8),
                    "jump_component": round(vol_estimate.get("jump_component", 0), 8),
                    "dvol_5s": round(vol_estimate["dvol_5s"], 8) if vol_estimate.get("dvol_5s") is not None else None,
                    "iv_rv_blend_method": vol_estimate.get("iv_rv_blend_method"),
                    "fixed_blend_rv": round(vol_estimate.get("fixed_blend_rv", 0), 8),
                    "jump_multiplier": vol_estimate.get("jump_multiplier", 1.0),
                    "jump_event_count": vol_estimate.get("jump_event_count", 0),
                    "egarch_sigma": round(vol_estimate["egarch_sigma"], 8) if vol_estimate.get("egarch_sigma") is not None else None,
                    "egarch_n_updates": vol_estimate.get("egarch_n_updates", 0),
                    "egarch_log_var": round(vol_estimate.get("egarch_log_var", 0), 4) if vol_estimate.get("egarch_log_var") is not None else None,
                    "egarch_vs_rv_ratio": round(vol_estimate["egarch_sigma"] / vol_estimate["blended_rv"], 4) if vol_estimate.get("egarch_sigma") and vol_estimate.get("blended_rv") and vol_estimate["blended_rv"] > 0 else None,
                    # Adaptive RK bandwidth
                    "omega_sq": vol_estimate.get("omega_sq"),
                    "rk_H_adaptive_5": vol_estimate.get("rk_H_adaptive_5"),
                    "rk_H_adaptive_15": vol_estimate.get("rk_H_adaptive_15"),
                    "rk_H_fixed_5": vol_estimate.get("rk_H_fixed_5"),
                    "rk_H_fixed_15": vol_estimate.get("rk_H_fixed_15"),
                    "ark_5min": round(vol_estimate.get("ark_5min", 0), 8) if vol_estimate.get("ark_5min") is not None else None,
                    "ark_15min": round(vol_estimate.get("ark_15min", 0), 8) if vol_estimate.get("ark_15min") is not None else None,
                    "rk_adaptive_delta_5": vol_estimate.get("rk_adaptive_delta_5", 0),
                    "rk_adaptive_delta_15": vol_estimate.get("rk_adaptive_delta_15", 0),
                    # DVOL diagnostics
                    "dvol_sq_hourly": round(vol_estimate["dvol_sq_hourly"], 10) if vol_estimate.get("dvol_sq_hourly") is not None else None,
                    "vrp": round(vol_estimate["vrp"], 10) if vol_estimate.get("vrp") is not None else None,
                    # Shadow TV RK weights
                    "shadow_tv_blend_rv": round(vol_estimate["shadow_tv_blend_rv"], 8) if vol_estimate.get("shadow_tv_blend_rv") is not None else None,
                    "shadow_tv_weights": vol_estimate.get("shadow_tv_weights"),
                })
            # Order flow snapshot
            if self.order_flow is not None:
                try:
                    ofa = self.order_flow.get_signals(asset)
                    scan_entry["ofa_adjustment"] = round(ofa["prob_adjustment"], 6)
                    scan_entry["ofa_confidence"] = ofa["confidence"]
                    cx = ofa["signals"].get("cross_exchange", {})
                    scan_entry["cross_ex_consensus"] = cx.get("consensus_direction")
                    scan_entry["cross_ex_above"] = cx.get("exchanges_above", 0)
                    fn = ofa["signals"].get("funding", {})
                    scan_entry["funding_rate"] = fn.get("rate")
                    scan_entry["funding_level"] = fn.get("level")
                except Exception:
                    pass
            self.logger.log_scan(scan_entry)

        # Poll active executor orders (maker fill check — one per asset)
        self.executor.tick()

        # SOL Path C shadow: check orderbook every tick during escalation window
        try:
            self.executor._tick_sol_pathc_observations()
        except Exception:
            logging.debug("sol_pathc_obs tick failed", exc_info=True)

        # Check confirmation addon opportunities on open positions
        try:
            self.executor._check_addon_opportunities()
        except Exception:
            logging.debug("addon check failed", exc_info=True)

        # Check dip addon opportunities on open positions
        try:
            self.executor._check_dip_addon_opportunities()
        except Exception:
            logging.debug("dip addon check failed", exc_info=True)

        # Run opportunity scanner (always — execute() rejects if asset already active)
        candidates = self.scanner.scan(self._active_windows)
        if self.scanner._last_scan_stats:
            try:
                self.logger.log_scan({
                    "type": "scan_summary",
                    "per_asset": self.scanner._last_scan_stats,
                    "had_candidate": candidates is not None,
                })
            except Exception:
                pass
        if candidates:
            for candidate in candidates:
                logging.info(
                    f"Opportunity: {candidate['ticker']} "
                    f"ask={candidate['best_yes_ask']}¢ "
                    f"edge={candidate['edge']:.2%} "
                    f"size={candidate['position_size']} "
                    f"prob={candidate['calibrated_prob']:.2%}"
                )
                self.executor.execute(candidate)

    # ── Run ───────────────────────────────────────────────────────────────

    def run(self):
        self._setup_signals()
        self.startup()

        mode = "LIVE" if not OBSERVATION_MODE else "observation"
        logging.info(f"Entering main loop ({mode} mode)...")
        _consecutive_errors = 0
        _incident_alerted = False
        try:
            while not self._shutdown.is_set():
                loop_start = time.time()
                try:
                    self._tick()
                except Exception as e:
                    self._last_error = str(e)
                    self._last_error_time = time.time()
                    _consecutive_errors += 1
                    logging.error("Tick error (%d consecutive)",
                                  _consecutive_errors, exc_info=True)

                    if _TELEGRAM:
                        if _consecutive_errors <= 1:
                            # First error: standard warning with dedup
                            _TELEGRAM.send(
                                f"\u26a0\ufe0f Tick error: {str(e)[:200]}",
                                dedup_key="tick_error")
                        elif _consecutive_errors == 3 and not _incident_alerted:
                            # 3 consecutive: CRITICAL escalation
                            _TELEGRAM.send(
                                f"\U0001f6a8 *INCIDENT: BOT BLOCKED*\n"
                                f"{_consecutive_errors} consecutive tick "
                                f"errors in {_consecutive_errors * 5}s\n"
                                f"Error: `{str(e)[:150]}`\n"
                                f"Auto-restart in 30s if not resolved.")
                            _incident_alerted = True
                        elif _consecutive_errors % 12 == 0 and _incident_alerted:
                            # Every 60s during sustained outage: update
                            _TELEGRAM.send(
                                f"\U0001f6a8 *INCIDENT ONGOING*: "
                                f"{_consecutive_errors} consecutive errors "
                                f"({_consecutive_errors * 5}s blocked)\n"
                                f"Error: `{str(e)[:150]}`")

                    # Auto-restart: 6 consecutive errors = 30s blocked
                    # systemd Restart=always brings us back up clean
                    if _consecutive_errors >= 6:
                        logging.critical(
                            "AUTO-RESTART: %d consecutive tick errors, "
                            "exiting for systemd restart",
                            _consecutive_errors)
                        if _TELEGRAM:
                            _TELEGRAM.send(
                                f"\U0001f504 *AUTO-RESTART*: "
                                f"{_consecutive_errors} consecutive errors "
                                f"({_consecutive_errors * 5}s blocked). "
                                f"Restarting now.")
                            time.sleep(1)  # let Telegram send
                        os._exit(1)

                    time.sleep(5)
                    continue

                # Successful tick — check for recovery
                if _consecutive_errors > 0:
                    if _incident_alerted and _TELEGRAM:
                        _TELEGRAM.send(
                            f"\u2705 *INCIDENT RECOVERED*: Bot resumed "
                            f"after {_consecutive_errors} consecutive "
                            f"errors ({_consecutive_errors * 5}s blocked)")
                    elif _consecutive_errors >= 2:
                        logging.warning(
                            "Recovered from %d consecutive tick errors",
                            _consecutive_errors)
                    _consecutive_errors = 0
                    _incident_alerted = False

                elapsed = time.time() - loop_start
                sleep_time = max(0, SCAN_INTERVAL_SECONDS - elapsed)
                self._shutdown.wait(timeout=sleep_time)
        finally:
            self._cleanup()

    def _cleanup(self):
        logging.info("Shutting down...")
        if hasattr(self, 'executor'):
            for asset in list(self.executor._active_orders):
                try:
                    self.executor._cancel_order(asset, "shutdown")
                except Exception:
                    pass
        if hasattr(self, 'egarch_estimator'):
            self.egarch_estimator._save_state()
            logging.info(
                "EGARCH buffer saved on shutdown: BTC=%d ETH=%d SOL=%d XRP=%d",
                len(self.egarch_estimator._returns["BTC"]),
                len(self.egarch_estimator._returns["ETH"]),
                len(self.egarch_estimator._returns["SOL"]),
                len(self.egarch_estimator._returns["XRP"]))
        if hasattr(self, 'mz_tracker'):
            self.mz_tracker.save_state()
            logging.info("MZ tracker state saved on shutdown")
        if hasattr(self, 'vol'):
            self.vol._save_adaptive_state()
            logging.info(
                "Adaptive jump state saved on shutdown: BTC=%d ETH=%d SOL=%d XRP=%d obs",
                len(self.vol._adaptive_returns_15s["BTC"]),
                len(self.vol._adaptive_returns_15s["ETH"]),
                len(self.vol._adaptive_returns_15s["SOL"]),
                len(self.vol._adaptive_returns_15s["XRP"]))
        if hasattr(self, 'kalshi_feed') and self.kalshi_feed:
            self.kalshi_feed.stop()
        # snapshot_builder has no thread — nothing to stop
        if hasattr(self, 'supabase_syncer') and self.supabase_syncer:
            self.supabase_syncer.stop()
        if hasattr(self, 'coinglass'):
            self.coinglass.stop()
        if hasattr(self, 'cross_feed') and self.cross_feed:
            self.cross_feed.stop()
        if hasattr(self, 'dvol_fetcher'):
            self.dvol_fetcher.stop()
        self.feed.stop()
        self.state.close()
        if _TELEGRAM:
            _TELEGRAM.send("\U0001f534 Bot shutting down")
        logging.info("Bot stopped.")


# ═════════════════════════════════════════════════════════════════════════════
#  Entrypoint
# ═════════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stderr)],
    force=True,
)

if __name__ == "__main__":
    bot = MainLoop()
    bot.run()
