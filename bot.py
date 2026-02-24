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
from collections import deque
from typing import Optional, Dict, List, Set, Tuple

import requests
import websockets
from scipy.stats import t as student_t, norminvgauss
from cryptography.hazmat.primitives import serialization, hashes
from cryptography.hazmat.primitives.asymmetric import padding

# ─── Trading Configuration ───────────────────────────────────────────────────
OBSERVATION_MODE = True            # True = evaluate & log everything but place no orders
ASSETS = ["BTC", "ETH", "SOL", "XRP"]
SERIES_TICKERS = {
    "BTC": "KXBTC15M",
    "ETH": "KXETH15M",
    "SOL": "KXSOL15M",
    "XRP": "KXXRP15M",
}
MIN_ENTRY_PRICE = 85              # cents (data: 85-87c is 3/3 wins +49c; loss zone is 80-84c)
MAX_ENTRY_PRICE = 99              # cents
MAX_RISK_PER_TRADE = 0.50         # max 50% of bankroll at risk per trade (scales with balance)
MIN_SECONDS_BEFORE_CLOSE = 0
MAX_SECONDS_BEFORE_CLOSE = 240    # start scanning 4 min before close (data: 180-240s is 9W/1L; loss at 243s stays excluded)
ONE_ASSET_PER_WINDOW = True

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
DIST_CONFIG_PATH = "dist_config.json"

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
VOL_RETURN_INTERVAL = 5           # seconds between log returns
VOL_WINDOW_1MIN = 12              # 60s / 5s = 12 returns
VOL_WINDOW_5MIN = 60              # 300s / 5s = 60 returns
VOL_WINDOW_15MIN = 180            # 900s / 5s = 180 returns
VOL_BLEND_WEIGHTS = (0.5, 0.3, 0.2)  # 1min, 5min, 15min
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

# ─── EGARCH(1,1) Estimation ──────────────────────────────────────────────
EGARCH_SHADOW_MODE = True               # True = compute/log only, don't affect blended_rv
EGARCH_STATE_PATH = "egarch_state.json"
EGARCH_BUFFER_SAVE_INTERVAL = 300.0 # save return buffer to disk every 5 min
EGARCH_REFIT_INTERVAL = 7200            # 2h between MLE refits (match HAR)
EGARCH_MIN_RETURNS = 360                # 30 min of 5s returns before first MLE fit
EGARCH_RETURN_MAXLEN = 10800            # 15h of 5s returns for MLE window
EGARCH_WARMUP_RETURNS = 12              # 1 min before recursive update starts
EGARCH_E_ABS_Z = 0.7978845608           # E[|z|] for z ~ N(0,1) = sqrt(2/π)
EGARCH_LOG_VAR_FLOOR = -40.0            # exp(-40) ~ 4.25e-18 (prevents underflow)
EGARCH_LOG_VAR_CEILING = -10.0          # exp(-10) ~ 4.5e-5 (prevents explosive vol)
EGARCH_MLE_MAXITER = 200                # scipy L-BFGS-B iterations
EGARCH_OMEGA_BOUNDS = (-5.0, 0.0)
EGARCH_ALPHA_BOUNDS = (0.01, 0.5)
EGARCH_GAMMA_BOUNDS = (-0.3, 0.3)       # both leverage directions
EGARCH_BETA_BOUNDS = (0.80, 0.999)      # high persistence typical for crypto

# ─── EGARCH-RV Blend ───────────────────────────────────────────────────
EGARCH_BLEND_SHADOW_MODE = True        # True = log only, don't affect blended_rv
EGARCH_BLEND_STATE_PATH = "egarch_blend_state.json"

# Mincer-Zarnowitz R² tracker
MZ_WINDOW = 360                        # Rolling window: 360 ticks × 10s = 1 hour
MZ_MIN_OBS = 60                        # Need 10 min of data before R² is valid
MZ_RECOMPUTE_INTERVAL = 30.0           # Recompute R² every 30s (not every tick)

# Weight bounds per asset (from persistence analysis)
EGARCH_WEIGHT_BOUNDS = {
    "BTC": (0.15, 0.45),   # High persistence → more EGARCH weight
    "ETH": (0.10, 0.35),
    "SOL": (0.05, 0.20),   # Low persistence → less EGARCH weight
    "XRP": (0.05, 0.25),
}
EGARCH_WEIGHT_DEFAULT = 0.0            # Before MZ warmup: pure RV (safe default)

# Safety clamps
EGARCH_RV_RATIO_CLAMP = 3.0           # Reject EGARCH if σ_eg/σ_rv > 3 or < 1/3
EGARCH_BLEND_LOG_INTERVAL = 300.0     # Log blend diagnostics every 5 min

# ─── Adaptive RK Bandwidth (BN 2008/2009) ─────────────────────────────────
RK_ADAPTIVE_SHADOW_MODE = False          # False = adaptive H* drives blended_rv
RK_CSTAR_FLAT_TOP_PARZEN = 3.5134       # c* for flat-top Parzen kernel (BN 2009 Table 2)
RK_NOISE_VAR_FLOOR = 1e-20              # ω² floor (prevents zero/negative)
RK_BANDWIDTH_MAX_FRACTION = 1 / 3       # H* cap as fraction of n
RK_MIN_RETURNS_FOR_ADAPTIVE = 20        # need ≥20 returns for reliable γ̂(1)

# ─── HAR-WLS Estimation ──────────────────────────────────────────────────
HAR_OBSERVATION_INTERVAL = 300      # 5 min between observations (seconds)
HAR_OBSERVATION_MAXLEN = 288        # 24h of 5-min observations
HAR_REFIT_INTERVAL = 7200           # 2h between refits
HAR_MIN_OBSERVATIONS = 36           # 3h of data before first fit
HAR_STATE_PATH = "har_state.json"
HAR_BUFFER_SAVE_INTERVAL = 300.0    # save observation buffer to disk every 5 min
HAR_QLIKE_FALLBACK_THRESHOLD = 2.0  # fall back to fixed if QLIKE > this
HAR_SHADOW_MODE = True              # True = log only, False = use for actual blend
HAR_IV_REPLACES_DVOL_BLEND = False  # When True + HAR active IV model, replaces Step 4/5 blending
HAR_IV_MIN_DVOL_FRACTION = 0.70    # Need ≥70% non-None dvol_sq observations to fit IV models

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

# ─── Probability Engine ──────────────────────────────────────────────────────
SECONDS_PER_YEAR = 365.25 * 24 * 3600  # crypto trades 24/7
DVOL_ANNUALIZED_TO_5S = 1.0 / math.sqrt(SECONDS_PER_YEAR / VOL_RETURN_INTERVAL)
STUDENT_T_DF = 4                  # degrees of freedom for t-distribution
BETA_SLOPE = 0.85                 # logistic calibration (<1 compresses extremes)
MAX_EFFECTIVE_PROB = 0.93         # hard cap on calibrated probability (default / fallback)
NUMERICAL_SAFETY_CEILING = 0.999  # ceiling for learned calibration methods (replaces hard cap)

# ─── Calibration Engine ─────────────────────────────────────────────────────
CALIBRATION_STATE_PATH = "calibration_state.json"
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

MARKET_BLEND_W = 0.50            # weight on market-implied probability
ENDGAME_BLEND_PRICE = 96         # don't blend at or above this price (preserve endgame edge)

Z_SCORE_MAX = 12.0                # refuse to trade if |z| > 12 (vol estimate wrong)
DISCREPANCY_PROB = 0.90           # model says >90% but...
DISCREPANCY_PRICE = 75            # ...market is below 75¢ → refuse

# ─── Per-Asset Distribution Config ──────────────────────────────────────────

def _load_dist_config() -> Dict:
    """Load per-asset distribution config from dist_config.json.

    Returns dict keyed by asset name. Falls back to Student-t(df=4) if missing.
    """
    defaults = {"distribution": "student_t", "student_t_df": STUDENT_T_DF}
    config: Dict[str, Dict] = {}

    try:
        with open(DIST_CONFIG_PATH, "r") as f:
            raw = json.load(f)

        for asset_name, acfg in raw.get("assets", {}).items():
            entry = dict(defaults)
            if "distribution" in acfg:
                entry["distribution"] = acfg["distribution"]
            if "student_t_df" in acfg:
                entry["student_t_df"] = float(acfg["student_t_df"])
            if "nig_params" in acfg:
                p = acfg["nig_params"]
                entry["nig_a"] = float(p["a"])
                entry["nig_b"] = float(p["b"])
                entry["nig_loc"] = float(p.get("loc", 0.0))
                entry["nig_scale"] = float(p.get("scale", 1.0))
            config[asset_name] = entry

        logging.info(
            "Loaded dist config: %s",
            {a: f"{c['distribution']}(df={c.get('student_t_df')})" if c['distribution'] == 'student_t'
             else "nig" for a, c in config.items()}
        )
    except FileNotFoundError:
        logging.info("No %s found, using defaults (Student-t df=%d)", DIST_CONFIG_PATH, STUDENT_T_DF)
    except Exception as e:
        logging.warning("Error loading %s, using defaults: %s", DIST_CONFIG_PATH, e)

    return config

DIST_CONFIG = _load_dist_config()

_CALIBRATION_ENGINE: Optional["CalibrationEngine"] = None
_TELEGRAM: Optional["TelegramNotifier"] = None

# ─── Opportunity Scanner ────────────────────────────────────────────────────
MIN_EDGE_PCT = 1.0                # model prob must exceed market by ≥1.0 pp (data: gross edge>=1.5% is 9/10 wins; fee drag is the real filter)
ORDERBOOK_CACHE_TTL = 5.0         # seconds to cache orderbook responses
MAX_OB_FETCHES_PER_TICK = 6       # cap API calls for orderbooks per tick (Advanced tier)
BALANCE_CACHE_TTL = 30.0          # seconds to cache balance

# ─── Position Sizing ───────────────────────────────────────────────────────
# Edge-based tiered sizing: higher edge → more aggressive
SIZING_TIERS = [                  # (min_edge, risk_fraction)
    (0.05, 0.50),                 # edge ≥ 5%  → risk 50% of bankroll
    (0.03, 0.35),                 # edge ≥ 3%  → risk 35% of bankroll
    (0.015, 0.20),                # edge ≥ 1.5% → risk 20% of bankroll
]
DRAWDOWN_HALF_THRESHOLD = 0.90    # below 90% of starting balance → halve size
DRAWDOWN_QUARTER_THRESHOLD = 0.80 # below 80% → quarter size

# ─── Order Execution ──────────────────────────────────────────────────────
MAKER_PRICE_OFFSET = 1            # cents below fair value for maker orders
MAKER_POLL_INTERVAL = 2.0         # poll for maker fills every 2 seconds
ESCALATION_MAX_ENTRY = 99         # taker price cap during escalation (cents)
CONVERGENCE_WINDOW_SECONDS = 30.0 # seconds to measure price velocity
PANIC_BID_PRICE = 99              # resting bid price (cents)
MAKER_TIMEOUT_SECONDS = 30.0     # hard timeout for maker orders

# ─── Adaptive Escalation ─────────────────────────────────────────────────
ESCALATION_WAIT_LONG = 15.0       # maker wait when 60-300s to close
ESCALATION_WAIT_MEDIUM = 10.0     # maker wait when 30-60s to close
ESCALATION_WAIT_SHORT = 5.0       # maker wait when <30s to close


# ═════════════════════════════════════════════════════════════════════════════
#  Fee Helpers
# ═════════════════════════════════════════════════════════════════════════════

def calculate_fee(count: int, price_cents: int, is_taker: bool) -> int:
    """Fee in cents. Ceil applied to TOTAL, not per contract.

    Taker:  ceil(0.07   × count × price × (100−price) / 100)
    Maker:  ceil(0.0175 × count × price × (100−price) / 100)

    The division by 100 converts from the raw product (price in cents ×
    complement in cents) back to cents.  Equivalent to the CLAUDE.md formula
    ceil(rate × C × P × (1−P)) evaluated in dollars, then converted to cents.
    """
    rate = 0.07 if is_taker else 0.0175
    return math.ceil(rate * count * price_cents * (100 - price_cents) / 100)


def calculate_taker_fee(count: int, price_cents: int) -> int:
    """Convenience wrapper — taker fee in cents."""
    return calculate_fee(count, price_cents, is_taker=True)


def calculate_maker_fee(count: int, price_cents: int) -> int:
    """Convenience wrapper — maker fee in cents."""
    return calculate_fee(count, price_cents, is_taker=False)


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
    if best_ask is not None and MIN_ENTRY_PRICE <= best_ask <= ESCALATION_MAX_ENTRY:
        if composite >= 6.5:
            return _decide(STRATEGY_TAKER_NOW,
                            f"composite={composite:.1f}>=6.5")
        if velocity > 5 and MIN_ENTRY_PRICE <= best_ask <= MAX_ENTRY_PRICE:
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
    if edge > 0 and best_ask is not None and MIN_ENTRY_PRICE <= best_ask <= MAX_ENTRY_PRICE:
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
            if resp.status_code == 429:
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
                    client_order_id: Optional[str] = None) -> Optional[Dict]:
        body: Dict = {
            "ticker": ticker,
            "side": side,
            "action": action,
            "count": count,
            "count_fp": int_to_fp_str(count),
            "type": "limit",
        }
        if yes_price is not None:
            body["yes_price"] = yes_price
            body["yes_price_dollars"] = cents_to_dollars_str(yes_price)
        if no_price is not None:
            body["no_price"] = no_price
            body["no_price_dollars"] = cents_to_dollars_str(no_price)
        if client_order_id:
            body["client_order_id"] = client_order_id
        return self._request("POST", f"{API_PATH_PREFIX}/portfolio/orders",
                             json_body=body)

    def cancel_order(self, order_id: str) -> Optional[Dict]:
        return self._request("DELETE",
                             f"{API_PATH_PREFIX}/portfolio/orders/{order_id}")

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
            logging.debug(f"Telegram send failed: {e}")


# ═════════════════════════════════════════════════════════════════════════════
#  StateManager
# ═════════════════════════════════════════════════════════════════════════════

class StateManager:
    """SQLite-backed persistent state. WAL mode for crash resilience."""

    def __init__(self, db_path: str = DB_PATH):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.row_factory = sqlite3.Row
        self._create_tables()

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
        ]:
            try:
                self.conn.execute(f"ALTER TABLE evaluated_opportunities ADD COLUMN {col_def[0]} {col_def[1]}")
            except sqlite3.OperationalError:
                pass  # column already exists
        self.conn.commit()

        # Migration: add new columns to rejected_opportunities (safe to re-run)
        for col_def in [
            ("raw_prob", "REAL"),
            ("market_result", "TEXT"),
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
        ]:
            try:
                self.conn.execute(f"ALTER TABLE settled_trades ADD COLUMN {col_def[0]} {col_def[1]}")
            except sqlite3.OperationalError:
                pass  # column already exists
        self.conn.commit()

        # Migration: add enrichment columns to positions
        for col_def in [
            ("strategy", "TEXT"),
            ("seconds_to_close", "REAL"),
            ("fill_latency_seconds", "REAL"),
            ("vol_regime", "TEXT"),
            ("calibrated_prob", "REAL"),
            ("edge", "REAL"),
            ("kelly_f", "REAL"),
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
            for suffix in ("15M", "1H", "1D"):
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
        if not api_resp or not api_resp.get("orders"):
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

    def record_settlement(self, settlement: Dict):
        ticker = settlement["ticker"]
        now = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        pos_row = self.conn.execute(
            "SELECT * FROM positions WHERE ticker=?", (ticker,)
        ).fetchone()
        if not pos_row:
            return
        pos = dict(pos_row)

        result = settlement.get("market_result", "")
        rev_d = settlement.get("revenue_dollars")
        revenue = dollars_str_to_cents(rev_d) if rev_d else (settlement.get("revenue") or 0)
        total_cost = pos["total_cost_cents"]
        pnl = revenue - total_cost
        fee = calculate_taker_fee(pos["count"], pos["avg_price_cents"])

        self.conn.execute("""
            INSERT OR REPLACE INTO settled_trades
                (ticker, event_ticker, asset, market_result, side, count,
                 entry_price_cents, revenue_cents, fee_cents, pnl_cents,
                 settled_at, strategy, seconds_to_close, fill_latency_seconds,
                 vol_regime, calibrated_prob, edge, kelly_f)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (ticker, pos["event_ticker"], pos["asset"], result,
              pos["side"], pos["count"], pos["avg_price_cents"],
              revenue, fee, pnl, now,
              pos.get("strategy"), pos.get("seconds_to_close"),
              pos.get("fill_latency_seconds"), pos.get("vol_regime"),
              pos.get("calibrated_prob"), pos.get("edge"), pos.get("kelly_f")))

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
                         calibrated_prob: Optional[float]):
        """Insert a rejected opportunity. INSERT OR IGNORE deduplicates by ticker."""
        now = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        self.conn.execute("""
            INSERT OR IGNORE INTO rejected_opportunities
                (ticker, event_ticker, asset, rejection_reason, rejection_time,
                 z_score, spot_price, threshold, volatility, market_price,
                 seconds_to_close, calibrated_prob, status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (ticker, event_ticker, asset, rejection_reason, now,
              z_score, spot_price, threshold, volatility, market_price,
              seconds_to_close, calibrated_prob, "pending"))
        self.conn.commit()

    def get_unsettled_rejections(self) -> List[Dict]:
        """Return all rejected opportunities with status='pending'."""
        rows = self.conn.execute(
            "SELECT * FROM rejected_opportunities WHERE status='pending'"
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_rejection_settled(self, ticker: str):
        """Set status='settled' for a rejected opportunity."""
        self.conn.execute(
            "UPDATE rejected_opportunities SET status='settled' WHERE ticker=?",
            (ticker,)
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
                                     fee_adjusted_edge: Optional[float] = None):
        """Insert an evaluated opportunity for settlement tracking."""
        now = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        try:
            self.conn.execute("""
                INSERT OR REPLACE INTO evaluated_opportunities
                    (ticker, event_ticker, asset, filter_stage, rejection_reason,
                     evaluation_time, spot_price, threshold, volatility,
                     market_price, seconds_to_close, calibrated_prob,
                     edge, ofa_adjustment, status,
                     strategy, position_size, kelly_f, z_score,
                     vol_regime, calibrated_prob_raw,
                     breakeven_wr, expected_value, drawdown_scaler,
                     ask_depth, best_ask_source, ofa_confidence,
                     raw_prob, calibration_method, old_system_prob,
                     fee_adjusted_edge)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (ticker, event_ticker, asset, filter_stage, rejection_reason,
                  now, spot_price, threshold, volatility, market_price,
                  seconds_to_close, calibrated_prob, edge, ofa_adjustment,
                  "pending",
                  strategy, position_size, kelly_f, z_score,
                  vol_regime, calibrated_prob_raw,
                  breakeven_wr, expected_value, drawdown_scaler,
                  ask_depth, best_ask_source, ofa_confidence,
                  raw_prob, calibration_method, old_system_prob,
                  fee_adjusted_edge))
            self.conn.commit()
        except Exception as e:
            logging.debug(f"insert_evaluated_opportunity failed: {e}")

    def get_unsettled_evaluated_opportunities(self) -> List[Dict]:
        """Return evaluated opportunities with status='pending' and a market_price."""
        rows = self.conn.execute(
            "SELECT * FROM evaluated_opportunities WHERE status='pending' AND market_price IS NOT NULL"
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_evaluated_opportunity_settled(self, opp_id: int,
                                             market_result: Optional[str] = None,
                                             counterfactual_pnl: Optional[int] = None):
        """Set status='settled' for an evaluated opportunity by id."""
        now = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        self.conn.execute(
            "UPDATE evaluated_opportunities SET status='settled', "
            "market_result=?, counterfactual_pnl=?, settled_time=? WHERE id=?",
            (market_result, counterfactual_pnl, now, opp_id)
        )
        self.conn.commit()

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
                                  edge=None, kelly_f=None):
        """Record a new open position from a fill."""
        now = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        cost = count * price_cents
        self.conn.execute("""
            INSERT OR REPLACE INTO positions
                (ticker, event_ticker, asset, side, count,
                 avg_price_cents, total_cost_cents, opened_at, updated_at, status,
                 strategy, seconds_to_close, fill_latency_seconds,
                 vol_regime, calibrated_prob, edge, kelly_f)
            VALUES (?,?,?,?,?,?,?,?,?,'open',?,?,?,?,?,?,?)
        """, (ticker, event_ticker, asset, side, count,
              price_cents, cost, now, now,
              strategy, seconds_to_close, fill_latency,
              vol_regime, calibrated_prob, edge, kelly_f))
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

    def __init__(self, cross_feed=None, coinglass=None):
        self._cross = cross_feed
        self._coinglass = coinglass

    def get_signals(self, asset: str) -> Dict:
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

        # 3. Sum and clamp
        total = sum(v for _, v in adjustments)
        total = max(-OFA_MAX_ADJUSTMENT, min(OFA_MAX_ADJUSTMENT, total))

        # 4. Confidence
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
            },
            "adjustments_applied": [
                f"{name}: {val:+.3f}" for name, val in adjustments
            ],
        }


# ═════════════════════════════════════════════════════════════════════════════
#  VolatilityEngine
# ═════════════════════════════════════════════════════════════════════════════

class VolatilityEngine:
    """Realized Kernel + HAR-RV + Deribit DVOL volatility engine.

    Uses microstructure-noise-robust Realized Kernel (Barndorff-Nielsen 2008),
    bipower variation for jump separation, and optional Deribit DVOL blending.
    Maintains its own rolling buffer of log returns per asset (up to 15 min).
    """

    def __init__(self, feed: CoinbaseFeed, dvol_fetcher: Optional[DeribitDVOLFetcher] = None,
                 har_estimator: Optional['HAREstimator'] = None,
                 egarch_estimator: Optional['EGARCHEstimator'] = None,
                 mz_tracker: Optional['MincerZarnowitzTracker'] = None):
        self._feed = feed
        self._dvol = dvol_fetcher
        self._har = har_estimator
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
        self._rk_delta_5_accum: Dict[str, List[float]] = {a: [] for a in ASSETS}
        self._rk_delta_15_accum: Dict[str, List[float]] = {a: [] for a in ASSETS}

    def update(self, asset: str) -> Optional[Dict]:
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
                estimate = self._compute(asset, now)
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

                self._cache[asset] = self._compute(asset, now)
            else:
                self._cache.setdefault(asset, None)
        elif asset not in self._cache:
            self._cache[asset] = self._compute(asset, now)

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

    def _compute(self, asset: str, now: float) -> Optional[Dict]:
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

        # Step 2b: HAR observation recording + semivariance computation
        har_blend_rv = None
        dvol_sq_for_har = None
        sv_pos_1 = sv_neg_1 = sv_pos_5 = sv_neg_5 = sv_pos_15 = sv_neg_15 = 0.0
        if self._har is not None:
            sv_pos_1, sv_neg_1 = HAREstimator._compute_semivariances(returns_list, VOL_WINDOW_1MIN)
            sv_pos_5, sv_neg_5 = HAREstimator._compute_semivariances(returns_list, VOL_WINDOW_5MIN)
            sv_pos_15, sv_neg_15 = HAREstimator._compute_semivariances(returns_list, VOL_WINDOW_15MIN)
            dvol_hourly = self._get_implied_vol_hourly(asset)
            dvol_sq_for_har = (dvol_hourly ** 2) if dvol_hourly is not None else None
            self._har.record_observation(asset, returns_list, rk_1min, rk_5min, rk_15min,
                                         bv_5min, dvol_sq=dvol_sq_for_har)

        # Step 3: HAR-RV blend (WLS-estimated or fixed weights)
        w1, w5, w15 = VOL_BLEND_WEIGHTS
        # Always compute fixed blend for counterfactual
        fixed_continuous_rv = w1 * rk_1min + w5 * rk_5min + w15 * rk_15min
        fixed_bv_blended = w1 * bv_1min + w5 * bv_5min + w15 * bv_15min
        fixed_jump_var = max(0.0, fixed_continuous_rv ** 2 - fixed_bv_blended ** 2)
        fixed_blend_rv = math.sqrt(fixed_bv_blended ** 2 + fixed_jump_var)

        if self._har is not None and self._har.is_active(asset):
            jump_sq = max(0.0, rk_5min ** 2 - bv_5min ** 2)
            rv_blended = self._har.get_blend(
                asset, rk_1min, rk_5min, rk_15min,
                jump_sq=jump_sq,
                sv_pos_1=sv_pos_1, sv_neg_1=sv_neg_1,
                sv_pos_5=sv_pos_5, sv_neg_5=sv_neg_5,
                sv_pos_15=sv_pos_15, sv_neg_15=sv_neg_15,
                dvol_sq=dvol_sq_for_har,
            )
            har_blend_rv = rv_blended
            # Still compute fixed-blend diagnostics
            continuous_rv = fixed_continuous_rv
            bv_blended = fixed_bv_blended
            jump_var = fixed_jump_var
        else:
            continuous_rv = fixed_continuous_rv
            bv_blended = fixed_bv_blended
            jump_var = fixed_jump_var
            rv_blended = fixed_blend_rv
            # In shadow mode, compute HAR prediction for logging only
            if self._har is not None:
                jump_sq = max(0.0, rk_5min ** 2 - bv_5min ** 2)
                har_blend_rv = self._har.get_har_prediction(
                    asset, rk_1min, rk_5min, rk_15min,
                    jump_sq=jump_sq,
                    sv_pos_1=sv_pos_1, sv_neg_1=sv_neg_1,
                    sv_pos_5=sv_pos_5, sv_neg_5=sv_neg_5,
                    sv_pos_15=sv_pos_15, sv_neg_15=sv_neg_15,
                    dvol_sq=dvol_sq_for_har,
                )

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

        # VRP diagnostic (variance risk premium)
        vrp = None
        if dvol_sq_for_har is not None and rk_5min > 0:
            vrp = dvol_sq_for_har - rk_5min ** 2

        # VRP regime logging (every 5 min)
        if vrp is not None and now - self._rk_last_summary.get(f"vrp_{asset}", 0) >= 300:
            premium = "positive" if vrp > 0 else "negative"
            rv5_sq = rk_5min ** 2
            ratio = dvol_sq_for_har / rv5_sq if rv5_sq > 0 else 0.0
            logging.info(
                "VRP %s: vrp=%.2e dvol_sq=%.2e rv5_sq=%.2e ratio=%.2f (premium=%s)",
                asset, vrp, dvol_sq_for_har, rv5_sq, ratio, premium,
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

        # HAR-IV shadow comparison
        har_iv_shadow_rv = None
        iv_model_names = {"har_iv", "har_j_iv", "log_har_iv", "har_vrp"}
        if self._har is not None and dvol_sq_for_har is not None:
            active_model = self._har._active_model.get(asset, "fixed")
            if active_model in iv_model_names and active_model in self._har._coefficients.get(asset, {}):
                jump_sq_s = max(0.0, rk_5min ** 2 - bv_5min ** 2)
                har_iv_shadow_rv = self._har.get_blend(
                    asset, rk_1min, rk_5min, rk_15min,
                    jump_sq=jump_sq_s,
                    sv_pos_1=sv_pos_1, sv_neg_1=sv_neg_1,
                    sv_pos_5=sv_pos_5, sv_neg_5=sv_neg_5,
                    sv_pos_15=sv_pos_15, sv_neg_15=sv_neg_15,
                    dvol_sq=dvol_sq_for_har,
                )

        # Production guard: HAR_IV_REPLACES_DVOL_BLEND
        har_iv_active_for_blend = (
            HAR_IV_REPLACES_DVOL_BLEND
            and not HAR_SHADOW_MODE
            and self._har is not None
            and self._har._active_model.get(asset, "fixed") in iv_model_names
            and dvol_sq_for_har is not None
            and har_iv_shadow_rv is not None
        )

        # Step 4: DVOL blending (if available)
        iv = self._get_implied_vol(asset)
        dvol_5s = iv  # for diagnostics
        iv_rv_spread = None
        iv_rv_blend_method = "rv_only"

        if har_iv_active_for_blend:
            # HAR-IV model replaces threshold blending
            blended = har_iv_shadow_rv
            iv_rv_blend_method = "har_iv"
        elif iv is not None and iv > 0 and rv_blended > 0:
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

        # HAR-IV shadow comparison logging (DEBUG)
        if har_iv_shadow_rv is not None and not har_iv_active_for_blend and blended > 0:
            delta = (har_iv_shadow_rv - blended) / blended
            logging.debug(
                "HAR-IV shadow %s: har_iv=%.8f threshold_blend=%.8f delta=%.4f method=%s model=%s vrp=%.2e",
                asset, har_iv_shadow_rv, blended, delta, iv_rv_blend_method,
                self._har._active_model.get(asset, "fixed") if self._har else "none",
                vrp if vrp is not None else 0.0,
            )

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
            # HAR diagnostics
            "har_model": self._har._active_model.get(asset, "fixed") if self._har else "fixed",
            "har_blend_rv": har_blend_rv,
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
            # HAR-IV diagnostics
            "dvol_sq_hourly": dvol_sq_for_har,
            "vrp": vrp,
            "har_iv_shadow_rv": har_iv_shadow_rv,
        }


# ═════════════════════════════════════════════════════════════════════════════
#  HAREstimator – WLS-estimated HAR-RV coefficients
# ═════════════════════════════════════════════════════════════════════════════

class HAREstimator:
    """Estimate HAR-RV blend weights via rolling WLS regression.

    Supports four model variants:
      - level_har: β0 + β1*RK1² + β5*RK5² + β15*RK15²
      - log_har:   exp(β0 + β1*log(RK1²) + β5*log(RK5²) + β15*log(RK15²))
      - har_j:     level_har + β_jump * jump²
      - har_semi:  β0 + Σ βi*semivariance_i (6 regressors)

    Refits every HAR_REFIT_INTERVAL seconds, selects best model by QLIKE.
    Falls back to fixed weights when insufficient data or sanity checks fail.
    """

    MODEL_NAMES = ("level_har", "log_har", "har_j", "har_semi",
                   "har_iv", "har_j_iv", "log_har_iv", "har_vrp")

    def __init__(self):
        self._observations: Dict[str, deque] = {
            a: deque(maxlen=HAR_OBSERVATION_MAXLEN) for a in ASSETS
        }
        self._last_obs_time: Dict[str, float] = {}
        self._last_refit: float = 0.0
        self._last_buffer_save: float = 0.0
        self._active_model: Dict[str, str] = {a: "fixed" for a in ASSETS}
        self._coefficients: Dict[str, Dict[str, List[float]]] = {a: {} for a in ASSETS}
        self._qlike_scores: Dict[str, Dict[str, float]] = {a: {} for a in ASSETS}
        self._load_state()
        active = {a: m for a, m in self._active_model.items() if m != "fixed"}
        age = round(time.time() - self._last_refit, 1) if self._last_refit > 0 else "never"
        logging.info("HAREstimator loaded: %s active models, state_age=%s", active, age)

    # ── Observation recording ─────────────────────────────────────────────

    def record_observation(self, asset: str, returns_list: List[float],
                           rk_1min: float, rk_5min: float, rk_15min: float,
                           bv_5min: float, dvol_sq: Optional[float] = None) -> None:
        """Record a 5-min observation for HAR estimation."""
        now = time.time()
        last = self._last_obs_time.get(asset, 0.0)
        if now - last < HAR_OBSERVATION_INTERVAL:
            return

        self._last_obs_time[asset] = now

        # Semivariances at all three windows
        sv_pos_1, sv_neg_1 = self._compute_semivariances(returns_list, VOL_WINDOW_1MIN)
        sv_pos_5, sv_neg_5 = self._compute_semivariances(returns_list, VOL_WINDOW_5MIN)
        sv_pos_15, sv_neg_15 = self._compute_semivariances(returns_list, VOL_WINDOW_15MIN)

        # Jump component
        jump_sq = max(0.0, rk_5min ** 2 - bv_5min ** 2)

        obs = {
            "ts": now,
            "rv1_sq": rk_1min ** 2,
            "rv5_sq": rk_5min ** 2,
            "rv15_sq": rk_15min ** 2,
            "jump_sq": jump_sq,
            "sv_pos_1": sv_pos_1, "sv_neg_1": sv_neg_1,
            "sv_pos_5": sv_pos_5, "sv_neg_5": sv_neg_5,
            "sv_pos_15": sv_pos_15, "sv_neg_15": sv_neg_15,
            "dvol_sq": dvol_sq,
        }
        self._observations[asset].append(obs)
        n_obs = len(self._observations[asset])

        logging.debug(
            "HAR obs: %s n=%d rv1=%.8f rv5=%.8f rv15=%.8f jump=%.8f sv+5=%.8f sv-5=%.8f dvol_sq=%s",
            asset, n_obs, obs["rv1_sq"], obs["rv5_sq"], obs["rv15_sq"],
            jump_sq, sv_pos_5, sv_neg_5,
            f"{dvol_sq:.8f}" if dvol_sq is not None else "None",
        )

        # Periodic buffer save
        now_save = time.time()
        if now_save - self._last_buffer_save >= HAR_BUFFER_SAVE_INTERVAL:
            self._save_state()
            self._last_buffer_save = now_save
            try:
                fsize = os.path.getsize(HAR_STATE_PATH) / 1024.0
            except OSError:
                fsize = 0.0
            logging.info(
                "HAR buffer saved: BTC=%d ETH=%d SOL=%d XRP=%d (file_size=%.1fKB)",
                len(self._observations["BTC"]), len(self._observations["ETH"]),
                len(self._observations["SOL"]), len(self._observations["XRP"]), fsize)

    # ── Prediction (hot path) ────────────────────────────────────────────

    def is_active(self, asset: str) -> bool:
        """True if a non-fixed model is active and not in shadow mode."""
        if HAR_SHADOW_MODE:
            return False
        return self._active_model.get(asset, "fixed") != "fixed"

    def get_blend(self, asset: str, rk_1min: float, rk_5min: float,
                  rk_15min: float, jump_sq: float = 0.0,
                  sv_pos_1: float = 0.0, sv_neg_1: float = 0.0,
                  sv_pos_5: float = 0.0, sv_neg_5: float = 0.0,
                  sv_pos_15: float = 0.0, sv_neg_15: float = 0.0,
                  dvol_sq: Optional[float] = None) -> float:
        """Return predicted RV using active model's coefficients."""
        model = self._active_model.get(asset, "fixed")
        coeffs = self._coefficients.get(asset, {}).get(model)
        if model == "fixed" or coeffs is None:
            w1, w5, w15 = VOL_BLEND_WEIGHTS
            return w1 * rk_1min + w5 * rk_5min + w15 * rk_15min

        if model == "level_har":
            val = coeffs[0] + coeffs[1] * rk_1min**2 + coeffs[2] * rk_5min**2 + coeffs[3] * rk_15min**2
            return math.sqrt(max(0.0, val))

        if model == "log_har":
            eps = 1e-20
            val = coeffs[0] + (coeffs[1] * math.log(max(eps, rk_1min**2))
                                + coeffs[2] * math.log(max(eps, rk_5min**2))
                                + coeffs[3] * math.log(max(eps, rk_15min**2)))
            return math.sqrt(max(0.0, math.exp(val)))

        if model == "har_j":
            val = (coeffs[0] + coeffs[1] * rk_1min**2 + coeffs[2] * rk_5min**2
                   + coeffs[3] * rk_15min**2 + coeffs[4] * jump_sq)
            return math.sqrt(max(0.0, val))

        if model == "har_semi":
            val = (coeffs[0] + coeffs[1] * sv_pos_1 + coeffs[2] * sv_neg_1
                   + coeffs[3] * sv_pos_5 + coeffs[4] * sv_neg_5
                   + coeffs[5] * sv_pos_15 + coeffs[6] * sv_neg_15)
            return math.sqrt(max(0.0, val))

        if model == "har_iv":
            if dvol_sq is None:
                return self._fallback_prediction(asset, rk_1min, rk_5min, rk_15min)
            val = coeffs[0] + coeffs[1] * rk_1min**2 + coeffs[2] * rk_5min**2 + coeffs[3] * rk_15min**2 + coeffs[4] * dvol_sq
            return math.sqrt(max(0.0, val))

        if model == "har_j_iv":
            if dvol_sq is None:
                return self._fallback_prediction(asset, rk_1min, rk_5min, rk_15min)
            val = (coeffs[0] + coeffs[1] * rk_1min**2 + coeffs[2] * rk_5min**2
                   + coeffs[3] * rk_15min**2 + coeffs[4] * jump_sq + coeffs[5] * dvol_sq)
            return math.sqrt(max(0.0, val))

        if model == "log_har_iv":
            if dvol_sq is None:
                return self._fallback_prediction(asset, rk_1min, rk_5min, rk_15min)
            eps = 1e-20
            val = (coeffs[0] + coeffs[1] * math.log(max(eps, rk_1min**2))
                   + coeffs[2] * math.log(max(eps, rk_5min**2))
                   + coeffs[3] * math.log(max(eps, rk_15min**2))
                   + coeffs[4] * math.log(max(eps, dvol_sq)))
            return math.sqrt(max(0.0, math.exp(val)))

        if model == "har_vrp":
            if dvol_sq is None:
                return self._fallback_prediction(asset, rk_1min, rk_5min, rk_15min)
            vrp = dvol_sq - rk_5min**2
            val = coeffs[0] + coeffs[1] * rk_1min**2 + coeffs[2] * rk_5min**2 + coeffs[3] * rk_15min**2 + coeffs[4] * vrp
            return math.sqrt(max(0.0, val))

        # Unknown model — fallback
        w1, w5, w15 = VOL_BLEND_WEIGHTS
        return w1 * rk_1min + w5 * rk_5min + w15 * rk_15min

    def _fallback_prediction(self, asset: str, rk_1min: float, rk_5min: float,
                             rk_15min: float) -> float:
        """Fallback when DVOL temporarily stale but IV model is active."""
        # Try level_har coefficients first
        coeffs = self._coefficients.get(asset, {}).get("level_har")
        if coeffs is not None:
            val = coeffs[0] + coeffs[1] * rk_1min**2 + coeffs[2] * rk_5min**2 + coeffs[3] * rk_15min**2
            return math.sqrt(max(0.0, val))
        # Fixed weights
        w1, w5, w15 = VOL_BLEND_WEIGHTS
        return w1 * rk_1min + w5 * rk_5min + w15 * rk_15min

    def get_har_prediction(self, asset: str, rk_1min: float, rk_5min: float,
                           rk_15min: float, jump_sq: float = 0.0,
                           sv_pos_1: float = 0.0, sv_neg_1: float = 0.0,
                           sv_pos_5: float = 0.0, sv_neg_5: float = 0.0,
                           sv_pos_15: float = 0.0, sv_neg_15: float = 0.0,
                           dvol_sq: Optional[float] = None) -> Optional[float]:
        """Return HAR prediction even in shadow mode (for logging). None if fixed."""
        model = self._active_model.get(asset, "fixed")
        if model == "fixed" or model not in self._coefficients.get(asset, {}):
            return None
        # Temporarily override shadow check
        return self.get_blend(asset, rk_1min, rk_5min, rk_15min,
                              jump_sq, sv_pos_1, sv_neg_1,
                              sv_pos_5, sv_neg_5, sv_pos_15, sv_neg_15,
                              dvol_sq=dvol_sq)

    # ── Refit logic ──────────────────────────────────────────────────────

    def maybe_refit(self) -> bool:
        """Check if it's time to refit. Returns True if any asset was refit."""
        now = time.time()
        if now - self._last_refit < HAR_REFIT_INTERVAL and self._last_refit > 0:
            return False

        any_refit = False
        for asset in ASSETS:
            obs = self._observations[asset]
            if len(obs) < HAR_MIN_OBSERVATIONS:
                continue
            self._refit_asset(asset, list(obs))
            any_refit = True

        if any_refit:
            self._last_refit = now
            self._save_state()
        return any_refit

    def _refit_asset(self, asset: str, obs: List[Dict]) -> None:
        """Fit all 4 model variants for one asset, select best by QLIKE."""
        n = len(obs)
        if n < 2:
            return

        # Build target: next observation's rv5_sq
        targets = [obs[i + 1]["rv5_sq"] for i in range(n - 1)]
        old_model = self._active_model.get(asset, "fixed")

        qlike_scores: Dict[str, float] = {}
        fitted_coeffs: Dict[str, List[float]] = {}

        # ── Fit level_har ─────────────────────────────────────────────
        X_level = [[1.0, obs[i]["rv1_sq"], obs[i]["rv5_sq"], obs[i]["rv15_sq"]]
                    for i in range(n - 1)]
        self._try_fit_model(asset, "level_har", X_level, targets,
                            qlike_scores, fitted_coeffs)

        # ── Fit log_har ───────────────────────────────────────────────
        eps = 1e-20
        X_log = [[1.0, math.log(max(eps, obs[i]["rv1_sq"])),
                   math.log(max(eps, obs[i]["rv5_sq"])),
                   math.log(max(eps, obs[i]["rv15_sq"]))]
                  for i in range(n - 1)]
        # Targets in log space
        log_targets = [math.log(max(eps, t)) for t in targets]
        c = self._fit_wls(X_log, log_targets,
                          [1.0 / math.sqrt(max(eps, t)) for t in targets])
        if c is not None:
            # Predict back in level space for QLIKE
            preds = []
            for i in range(n - 1):
                val = c[0] + c[1] * X_log[i][1] + c[2] * X_log[i][2] + c[3] * X_log[i][3]
                preds.append(math.exp(val))
            ql = self._compute_qlike(targets, preds)
            ok, reason = self._sanity_check_coeffs(c, "log_har")
            if ok and ql <= HAR_QLIKE_FALLBACK_THRESHOLD:
                qlike_scores["log_har"] = ql
                fitted_coeffs["log_har"] = c
            else:
                logging.warning(
                    "HAR refit %s: model log_har REJECTED — %s (coeffs=%s)",
                    asset, reason if not ok else f"QLIKE={ql:.4f}", [round(x, 6) for x in c],
                )

        # ── Fit har_j ─────────────────────────────────────────────────
        X_j = [[1.0, obs[i]["rv1_sq"], obs[i]["rv5_sq"], obs[i]["rv15_sq"],
                 obs[i]["jump_sq"]] for i in range(n - 1)]
        self._try_fit_model(asset, "har_j", X_j, targets,
                            qlike_scores, fitted_coeffs)

        # ── Fit har_semi ──────────────────────────────────────────────
        X_semi = [[1.0, obs[i]["sv_pos_1"], obs[i]["sv_neg_1"],
                    obs[i]["sv_pos_5"], obs[i]["sv_neg_5"],
                    obs[i]["sv_pos_15"], obs[i]["sv_neg_15"]]
                   for i in range(n - 1)]
        self._try_fit_model(asset, "har_semi", X_semi, targets,
                            qlike_scores, fitted_coeffs)

        # ── Fit IV-augmented models (when sufficient DVOL data) ──────
        dvol_available = [i for i in range(n - 1) if obs[i].get("dvol_sq") is not None]
        dvol_fraction = len(dvol_available) / (n - 1) if n > 1 else 0.0
        fit_iv_models = dvol_fraction >= HAR_IV_MIN_DVOL_FRACTION

        if fit_iv_models:
            iv_indices = dvol_available
            iv_targets = [targets[i] for i in iv_indices]

            # har_iv: [1, rv1_sq, rv5_sq, rv15_sq, dvol_sq]
            X_iv = [[1.0, obs[i]["rv1_sq"], obs[i]["rv5_sq"], obs[i]["rv15_sq"],
                      obs[i]["dvol_sq"]] for i in iv_indices]
            self._try_fit_model(asset, "har_iv", X_iv, iv_targets,
                                qlike_scores, fitted_coeffs)

            # har_j_iv: [1, rv1_sq, rv5_sq, rv15_sq, jump_sq, dvol_sq]
            X_j_iv = [[1.0, obs[i]["rv1_sq"], obs[i]["rv5_sq"], obs[i]["rv15_sq"],
                        obs[i]["jump_sq"], obs[i]["dvol_sq"]] for i in iv_indices]
            self._try_fit_model(asset, "har_j_iv", X_j_iv, iv_targets,
                                qlike_scores, fitted_coeffs)

            # log_har_iv: [1, log(rv1_sq), log(rv5_sq), log(rv15_sq), log(dvol_sq)]
            eps = 1e-20
            X_log_iv = [[1.0, math.log(max(eps, obs[i]["rv1_sq"])),
                          math.log(max(eps, obs[i]["rv5_sq"])),
                          math.log(max(eps, obs[i]["rv15_sq"])),
                          math.log(max(eps, obs[i]["dvol_sq"]))]
                         for i in iv_indices]
            log_iv_targets = [math.log(max(eps, t)) for t in iv_targets]
            c_log_iv = self._fit_wls(X_log_iv, log_iv_targets,
                                     [1.0 / math.sqrt(max(eps, t)) for t in iv_targets])
            if c_log_iv is not None:
                preds_log_iv = []
                for idx, i in enumerate(iv_indices):
                    val = sum(c_log_iv[j] * X_log_iv[idx][j] for j in range(len(c_log_iv)))
                    preds_log_iv.append(math.exp(val))
                ql_log_iv = self._compute_qlike(iv_targets, preds_log_iv)
                ok_log_iv, reason_log_iv = self._sanity_check_coeffs(c_log_iv, "log_har_iv")
                if ok_log_iv and ql_log_iv <= HAR_QLIKE_FALLBACK_THRESHOLD:
                    qlike_scores["log_har_iv"] = ql_log_iv
                    fitted_coeffs["log_har_iv"] = c_log_iv
                else:
                    logging.warning(
                        "HAR refit %s: model log_har_iv REJECTED — %s (coeffs=%s)",
                        asset, reason_log_iv if not ok_log_iv else f"QLIKE={ql_log_iv:.4f}",
                        [round(x, 6) for x in c_log_iv],
                    )

            # har_vrp: [1, rv1_sq, rv5_sq, rv15_sq, dvol_sq - rv5_sq]
            X_vrp = [[1.0, obs[i]["rv1_sq"], obs[i]["rv5_sq"], obs[i]["rv15_sq"],
                       obs[i]["dvol_sq"] - obs[i]["rv5_sq"]] for i in iv_indices]
            self._try_fit_model(asset, "har_vrp", X_vrp, iv_targets,
                                qlike_scores, fitted_coeffs)
        else:
            if n > 1:
                logging.info(
                    "HAR refit %s: IV models skipped (dvol_fraction=%.2f < %.2f, n_obs=%d)",
                    asset, dvol_fraction, HAR_IV_MIN_DVOL_FRACTION, n,
                )

        # ── Fixed-weights baseline QLIKE ──────────────────────────────
        w1, w5, w15 = VOL_BLEND_WEIGHTS
        fixed_preds = [(w1**2 * obs[i]["rv1_sq"] + w5**2 * obs[i]["rv5_sq"]
                         + w15**2 * obs[i]["rv15_sq"]
                         + 2*w1*w5*math.sqrt(max(0.0, obs[i]["rv1_sq"]*obs[i]["rv5_sq"]))
                         + 2*w1*w15*math.sqrt(max(0.0, obs[i]["rv1_sq"]*obs[i]["rv15_sq"]))
                         + 2*w5*w15*math.sqrt(max(0.0, obs[i]["rv5_sq"]*obs[i]["rv15_sq"])))
                        for i in range(n - 1)]
        fixed_qlike = self._compute_qlike(targets, fixed_preds)
        qlike_scores["fixed"] = fixed_qlike

        # ── Select best ──────────────────────────────────────────────
        best_model = "fixed"
        best_qlike = fixed_qlike
        for model_name in self.MODEL_NAMES:
            if model_name in qlike_scores and qlike_scores[model_name] < best_qlike:
                # Regression guard: reject if new QLIKE > prev + 0.1
                prev_qlike = self._qlike_scores.get(asset, {}).get(model_name)
                if prev_qlike is not None and qlike_scores[model_name] > prev_qlike + 0.1:
                    logging.warning(
                        "HAR refit %s: model %s REJECTED — QLIKE regression %.4f > prev %.4f + 0.1",
                        asset, model_name, qlike_scores[model_name], prev_qlike,
                    )
                    continue
                best_model = model_name
                best_qlike = qlike_scores[model_name]

        self._active_model[asset] = best_model
        self._qlike_scores[asset] = qlike_scores
        if best_model != "fixed" and best_model in fitted_coeffs:
            self._coefficients[asset][best_model] = fitted_coeffs[best_model]

        logging.info(
            "HAR refit %s: PROMOTED %s -> %s (QLIKE=%.4f, alternatives=%s, coeffs=%s, n=%d, dvol_frac=%.2f)",
            asset, old_model, best_model, best_qlike,
            {k: round(v, 4) for k, v in qlike_scores.items()},
            [round(c, 6) for c in fitted_coeffs.get(best_model, [])] if best_model != "fixed" else [],
            n, dvol_fraction,
        )
        logging.info(
            "HAR refit %s: fixed_baseline_qlike=%.4f, best_qlike=%.4f, improvement=%.1f%%",
            asset, fixed_qlike, best_qlike,
            100 * (fixed_qlike - best_qlike) / fixed_qlike if fixed_qlike > 0 else 0.0,
        )

        # Log when IV model wins over best non-IV model
        iv_model_names = {"har_iv", "har_j_iv", "log_har_iv", "har_vrp"}
        if best_model in iv_model_names:
            best_non_iv_ql = min(
                (qlike_scores.get(m, float("inf")) for m in self.MODEL_NAMES if m not in iv_model_names and m in qlike_scores),
                default=fixed_qlike,
            )
            logging.info(
                "HAR refit %s: IV model %s beats best non-IV (QLIKE %.4f vs %.4f, improvement=%.1f%%)",
                asset, best_model, best_qlike, best_non_iv_ql,
                100 * (best_non_iv_ql - best_qlike) / best_non_iv_ql if best_non_iv_ql > 0 else 0.0,
            )

        # Model transition logging
        if old_model != best_model:
            old_ql = self._qlike_scores.get(asset, {}).get(old_model, 0.0)
            logging.info(
                "HAR model change %s: %s → %s (prev_qlike=%.4f new_qlike=%.4f)",
                asset, old_model, best_model, old_ql, best_qlike,
            )

    def _try_fit_model(self, asset: str, model_name: str,
                       X: List[List[float]], targets: List[float],
                       qlike_scores: Dict[str, float],
                       fitted_coeffs: Dict[str, List[float]]) -> None:
        """Fit a model via WLS, check sanity, add to results if valid."""
        eps = 1e-20
        weights = [1.0 / math.sqrt(max(eps, t)) for t in targets]
        c = self._fit_wls(X, targets, weights)
        if c is None:
            return
        # Predict
        preds = [sum(c[j] * X[i][j] for j in range(len(c))) for i in range(len(X))]
        ql = self._compute_qlike(targets, preds)
        ok, reason = self._sanity_check_coeffs(c, model_name)
        if ok and ql <= HAR_QLIKE_FALLBACK_THRESHOLD:
            qlike_scores[model_name] = ql
            fitted_coeffs[model_name] = c
        else:
            logging.warning(
                "HAR refit %s: model %s REJECTED — %s (coeffs=%s)",
                asset, model_name, reason if not ok else f"QLIKE={ql:.4f}",
                [round(x, 6) for x in c],
            )

    # ── WLS solver (pure Python) ─────────────────────────────────────────

    @staticmethod
    def _fit_wls(X: List[List[float]], y: List[float],
                 w: List[float]) -> Optional[List[float]]:
        """Weighted least squares via normal equations: (X'WX)β = X'Wy.

        Pure Python, no numpy. Max matrix size 7x7 (HAR-semiRV).
        """
        n = len(y)
        if n == 0:
            return None
        p = len(X[0])

        # Build X'WX (p x p) and X'Wy (p x 1)
        XtWX = [[0.0] * p for _ in range(p)]
        XtWy = [0.0] * p

        for i in range(n):
            wi = w[i]
            xi = X[i]
            yi = y[i]
            for j in range(p):
                wxi_j = wi * xi[j]
                XtWy[j] += wxi_j * yi
                for k in range(j, p):
                    val = wxi_j * xi[k]
                    XtWX[j][k] += val
                    if k != j:
                        XtWX[k][j] += val

        return HAREstimator._gauss_eliminate(XtWX, XtWy)

    @staticmethod
    def _gauss_eliminate(A: List[List[float]], b: List[float]) -> Optional[List[float]]:
        """Gaussian elimination with partial pivoting. Returns None if singular."""
        n = len(b)
        # Augmented matrix
        M = [A[i][:] + [b[i]] for i in range(n)]

        for col in range(n):
            # Partial pivoting
            max_val = abs(M[col][col])
            max_row = col
            for row in range(col + 1, n):
                if abs(M[row][col]) > max_val:
                    max_val = abs(M[row][col])
                    max_row = row
            if max_val < 1e-15:
                return None  # Singular
            if max_row != col:
                M[col], M[max_row] = M[max_row], M[col]

            pivot = M[col][col]
            for row in range(col + 1, n):
                factor = M[row][col] / pivot
                for j in range(col, n + 1):
                    M[row][j] -= factor * M[col][j]

        # Back substitution
        x = [0.0] * n
        for i in range(n - 1, -1, -1):
            if abs(M[i][i]) < 1e-15:
                return None
            x[i] = M[i][n]
            for j in range(i + 1, n):
                x[i] -= M[i][j] * x[j]
            x[i] /= M[i][i]

        return x

    # ── QLIKE loss ───────────────────────────────────────────────────────

    @staticmethod
    def _compute_qlike(y_actual: List[float], y_predicted: List[float]) -> float:
        """QLIKE loss: mean(actual/predicted - log(actual/predicted) - 1).

        Both in variance scale. Handles zero/negative with penalty.
        """
        eps = 1e-20
        total = 0.0
        n = 0
        for a, p in zip(y_actual, y_predicted):
            a = max(eps, a)
            p = max(eps, p)
            ratio = a / p
            total += ratio - math.log(ratio) - 1.0
            n += 1
        return total / max(n, 1)

    # ── Semivariance ─────────────────────────────────────────────────────

    @staticmethod
    def _compute_semivariances(returns: List[float], window: int) -> tuple:
        """Compute positive and negative semivariances over the given window.

        sv_pos = sum(r² for r > 0) / n, sv_neg = sum(r² for r <= 0) / n
        """
        subset = returns[-window:] if len(returns) >= window else returns
        n = len(subset)
        if n == 0:
            return (0.0, 0.0)
        sv_pos = 0.0
        sv_neg = 0.0
        for r in subset:
            r2 = r * r
            if r > 0:
                sv_pos += r2
            else:
                sv_neg += r2
        return (sv_pos / n, sv_neg / n)

    # ── Sanity checks ────────────────────────────────────────────────────

    @staticmethod
    def _sanity_check_coeffs(coeffs: List[float], model_name: str) -> tuple:
        """Check coefficient sanity. Returns (ok: bool, reason: str)."""
        if not coeffs:
            return (False, "empty coefficients")

        intercept = coeffs[0]
        weights = coeffs[1:]

        # Log models exempt from non-negativity and sum checks
        log_models = {"log_har", "log_har_iv"}

        # Intercept bound (variance scale)
        if abs(intercept) > 0.001:
            return (False, f"intercept {intercept:.6f} exceeds ±0.001")

        # Non-negativity for RV weights
        if model_name not in log_models:
            for i, w in enumerate(weights):
                # har_vrp: last coefficient (VRP) can be negative (Bollerslev)
                if model_name == "har_vrp" and i == len(weights) - 1:
                    continue
                if w < 0:
                    return (False, f"weight[{i}]={w:.6f} is negative")

        # Upper bound per weight
        for i, w in enumerate(weights):
            if abs(w) > 1.5:
                return (False, f"weight[{i}]={w:.6f} exceeds ±1.5")

        # Sum of weights bound (skip for log models, different scale)
        if model_name not in log_models:
            wsum = sum(weights)
            # IV-augmented models get wider bound (2.5 vs 2.0)
            iv_augmented = {"har_iv", "har_j_iv", "har_vrp"}
            upper = 2.5 if model_name in iv_augmented else 2.0
            if wsum < 0.3 or wsum > upper:
                return (False, f"sum_weights={wsum:.4f} outside [0.3, {upper}]")

        return (True, "ok")

    # ── State persistence ────────────────────────────────────────────────

    def _load_state(self) -> None:
        """Load coefficients, active model, QLIKE, and observation buffers from JSON."""
        try:
            with open(HAR_STATE_PATH, "r") as f:
                state = json.load(f)
            for asset in ASSETS:
                if asset in state.get("active_model", {}):
                    self._active_model[asset] = state["active_model"][asset]
                if asset in state.get("coefficients", {}):
                    self._coefficients[asset] = state["coefficients"][asset]
                if asset in state.get("qlike_scores", {}):
                    self._qlike_scores[asset] = state["qlike_scores"][asset]
            self._last_refit = state.get("last_refit", 0.0)
            # Restore observation buffers
            obs_data = state.get("observations", {})
            now = time.time()
            oldest_age = 0.0
            for asset in ASSETS:
                try:
                    asset_obs = obs_data.get(asset, [])
                    if not isinstance(asset_obs, list):
                        raise ValueError(f"expected list, got {type(asset_obs).__name__}")
                    for obs in asset_obs:
                        if not isinstance(obs, dict):
                            raise ValueError(f"expected dict, got {type(obs).__name__}")
                        self._observations[asset].append(obs)
                    if asset_obs:
                        first_ts = asset_obs[0].get("ts", now)
                        oldest_age = max(oldest_age, now - first_ts)
                except Exception as oe:
                    logging.warning("HAR observations load failed for %s: %s (starting fresh)", asset, oe)
                    self._observations[asset].clear()
            counts = {a: len(self._observations[a]) for a in ASSETS}
            if any(counts.values()):
                logging.info(
                    "HAR observations restored: BTC=%d ETH=%d SOL=%d XRP=%d (oldest=%.0fs ago)",
                    counts["BTC"], counts["ETH"], counts["SOL"], counts["XRP"], oldest_age)
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        except Exception as e:
            logging.warning("HAREstimator: failed to load state: %s", e)

    def _save_state(self) -> None:
        """Save coefficients, active model, QLIKE, and observation buffers to JSON."""
        state = {
            "active_model": self._active_model,
            "coefficients": self._coefficients,
            "qlike_scores": self._qlike_scores,
            "last_refit": self._last_refit,
            "observations": {a: list(self._observations[a]) for a in ASSETS},
        }
        try:
            tmp = HAR_STATE_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(state, f, indent=2)
            os.replace(tmp, HAR_STATE_PATH)
        except Exception as e:
            logging.warning("HAREstimator: failed to save state: %s", e)

    # ── Diagnostics ──────────────────────────────────────────────────────

    def get_diagnostics(self) -> Dict:
        """Per-asset diagnostics for Firebase."""
        result = {}
        now = time.time()
        for asset in ASSETS:
            obs = self._observations[asset]
            n_obs = len(obs)
            model = self._active_model.get(asset, "fixed")
            ql = self._qlike_scores.get(asset, {})
            coeffs = self._coefficients.get(asset, {})

            diag: Dict[str, Any] = {
                "active_model": model,
                "n_observations": n_obs,
                "qlike_scores": {k: round(v, 4) for k, v in ql.items()} if ql else {},
                "coefficients": {k: [round(c, 6) for c in v] for k, v in coeffs.items()} if coeffs else {},
                "last_refit_age_s": round(now - self._last_refit, 1) if self._last_refit > 0 else None,
            }

            # QLIKE improvement vs fixed
            if "fixed" in ql and model != "fixed" and model in ql:
                fixed_ql = ql["fixed"]
                best_ql = ql[model]
                if fixed_ql > 0:
                    diag["qlike_vs_fixed_pct"] = round(100 * (best_ql - fixed_ql) / fixed_ql, 1)

            # Semivariance ratio (last observation)
            if n_obs > 0:
                last = obs[-1]
                sv_neg_5 = last.get("sv_neg_5", 0)
                sv_pos_5 = last.get("sv_pos_5", 0)
                if sv_pos_5 > 0:
                    diag["semivar_ratio_5min"] = round(sv_neg_5 / sv_pos_5, 2)

            # DVOL observation fraction
            if n_obs > 0:
                dvol_count = sum(1 for o in obs if o.get("dvol_sq") is not None)
                diag["dvol_obs_fraction"] = round(dvol_count / n_obs, 2)

            # Last VRP
            if n_obs > 0:
                last = obs[-1]
                dvol_sq_last = last.get("dvol_sq")
                rv5_sq_last = last.get("rv5_sq", 0)
                if dvol_sq_last is not None and rv5_sq_last > 0:
                    diag["vrp_last"] = dvol_sq_last - rv5_sq_last

            result[asset] = diag
        return result


# ═════════════════════════════════════════════════════════════════════════════
#  EGARCHEstimator – Conditional volatility via EGARCH(1,1)
# ═════════════════════════════════════════════════════════════════════════════

class EGARCHEstimator:
    """EGARCH(1,1) conditional volatility estimator.

    Model: log(σ²_t) = ω + α·(|z_{t-1}| - E[|z|]) + γ·z_{t-1} + β·log(σ²_{t-1})
    where z_t = r_t / σ_t

    Log-variance specification guarantees σ²>0 without parameter constraints.
    The γ parameter captures crypto's documented inverse leverage effect.
    """

    def __init__(self):
        self._returns: Dict[str, deque] = {
            a: deque(maxlen=EGARCH_RETURN_MAXLEN) for a in ASSETS
        }
        self._params: Dict[str, Optional[Dict]] = {a: None for a in ASSETS}
        self._log_var: Dict[str, Optional[float]] = {a: None for a in ASSETS}
        self._sigma: Dict[str, Optional[float]] = {a: None for a in ASSETS}
        self._last_refit: float = time.time()  # avoid wasteful first-tick refit
        self._n_updates: Dict[str, int] = {a: 0 for a in ASSETS}
        self._mle_loglik: Dict[str, Optional[float]] = {a: None for a in ASSETS}
        self._mle_converged: Dict[str, bool] = {a: False for a in ASSETS}
        self._last_buffer_save: float = 0.0
        self._lock = threading.Lock()  # protects param reads during MLE refit
        self._load_state()

    def record_return(self, asset: str, log_return: float):
        """Append return to MLE buffer."""
        self._returns[asset].append(log_return)
        now = time.time()
        if now - self._last_buffer_save >= EGARCH_BUFFER_SAVE_INTERVAL:
            self._save_state()
            self._last_buffer_save = now
            try:
                fsize = os.path.getsize(EGARCH_STATE_PATH) / 1024.0
            except OSError:
                fsize = 0.0
            logging.info(
                "EGARCH buffer saved: BTC=%d ETH=%d SOL=%d XRP=%d (file_size=%.1fKB)",
                len(self._returns["BTC"]), len(self._returns["ETH"]),
                len(self._returns["SOL"]), len(self._returns["XRP"]), fsize)

    def seed_variance(self, asset: str, rk_5min_sq: float):
        """First-time init: set log_var from realized kernel variance."""
        if self._log_var.get(asset) is not None:
            return
        if rk_5min_sq <= 0:
            return
        lv = math.log(rk_5min_sq)
        lv = max(EGARCH_LOG_VAR_FLOOR, min(EGARCH_LOG_VAR_CEILING, lv))
        self._log_var[asset] = lv
        self._sigma[asset] = math.exp(lv * 0.5)
        using_defaults = False
        if self._params[asset] is None:
            self._params[asset] = {
                "omega": lv * 0.05,
                "alpha": 0.10,
                "gamma": 0.0,
                "beta": 0.95,
            }
            using_defaults = True
        logging.info(
            "EGARCH %s: seeded from RK (rk_5min=%.6f, log_var=%.4f, using_defaults=%s)",
            asset, math.sqrt(rk_5min_sq), lv, using_defaults)

    def recursive_update(self, asset: str, log_return: float) -> Optional[float]:
        """O(1) recursive EGARCH update. Returns new σ or None."""
        params = self._params.get(asset)
        log_var = self._log_var.get(asset)
        if params is None or log_var is None:
            return None
        if len(self._returns.get(asset, [])) < EGARCH_WARMUP_RETURNS:
            return None

        omega = params["omega"]
        alpha = params["alpha"]
        gamma = params["gamma"]
        beta = params["beta"]

        sigma = math.exp(log_var * 0.5)
        if sigma <= 0:
            return None
        z = log_return / sigma
        raw_lv = omega + alpha * (abs(z) - EGARCH_E_ABS_Z) + gamma * z + beta * log_var
        new_log_var = max(EGARCH_LOG_VAR_FLOOR, min(EGARCH_LOG_VAR_CEILING, raw_lv))

        # Anomaly logging
        if raw_lv < EGARCH_LOG_VAR_FLOOR:
            logging.debug(
                "EGARCH %s: log_var clamped to FLOOR (was %.4f) — possible underflow",
                asset, raw_lv)
        elif raw_lv > EGARCH_LOG_VAR_CEILING:
            logging.debug(
                "EGARCH %s: log_var clamped to CEILING (was %.4f) — possible explosion",
                asset, raw_lv)

        self._log_var[asset] = new_log_var
        new_sigma = math.exp(new_log_var * 0.5)
        self._sigma[asset] = new_sigma
        self._n_updates[asset] = self._n_updates.get(asset, 0) + 1
        return new_sigma

    def get_sigma(self, asset: str) -> Optional[float]:
        """Return current conditional σ."""
        return self._sigma.get(asset)

    def is_active(self, asset: str) -> bool:
        """Returns False if shadow mode, or no successful MLE fit yet."""
        if EGARCH_SHADOW_MODE:
            return False
        return self._mle_converged.get(asset, False)

    def maybe_refit(self):
        """Check 2h timer, refit each asset via MLE if enough data."""
        now = time.time()
        if now - self._last_refit < EGARCH_REFIT_INTERVAL:
            return
        self._last_refit = now
        any_fit = False
        for asset in ASSETS:
            rets = self._returns.get(asset, deque())
            if len(rets) < EGARCH_MIN_RETURNS:
                logging.info(
                    "EGARCH refit %s: SKIPPED (n_returns=%d < %d)",
                    asset, len(rets), EGARCH_MIN_RETURNS)
                continue
            returns_list = list(rets)
            if self._mle_fit_asset(asset, returns_list):
                any_fit = True
        if any_fit:
            self._save_state()

    def _mle_fit_asset(self, asset: str, returns: list) -> bool:
        """Fit EGARCH(1,1) via scipy L-BFGS-B. Returns True on success."""
        try:
            from scipy.optimize import minimize
        except ImportError:
            logging.warning("EGARCH refit %s: scipy not available", asset)
            return False

        t0 = time.time()
        n = len(returns)
        sample_var = sum(r * r for r in returns) / n

        # Initial guess: previous params or heuristic
        old_params = self._params.get(asset)
        if old_params is not None:
            x0 = [old_params["omega"], old_params["alpha"],
                   old_params["gamma"], old_params["beta"]]
        else:
            x0 = [math.log(sample_var) * (1 - 0.95), 0.10, 0.0, 0.95]

        bounds = [
            EGARCH_OMEGA_BOUNDS,
            EGARCH_ALPHA_BOUNDS,
            EGARCH_GAMMA_BOUNDS,
            EGARCH_BETA_BOUNDS,
        ]

        try:
            result = minimize(
                EGARCHEstimator._neg_log_likelihood,
                x0, args=(returns,),
                method="L-BFGS-B",
                bounds=bounds,
                options={"maxiter": EGARCH_MLE_MAXITER, "ftol": 1e-10},
            )
        except Exception as e:
            logging.warning("EGARCH refit %s REJECTED: reason=exception %s", asset, e)
            return False

        omega, alpha, gamma, beta = result.x
        converged = result.success

        # Sanity check: unconditional log-var
        if abs(beta) >= 1.0:
            logging.warning(
                "EGARCH refit %s REJECTED: reason=beta>=1.0 (%.6f)", asset, beta)
            return False
        uncond_log_var = omega / (1.0 - beta)
        if uncond_log_var < EGARCH_LOG_VAR_FLOOR or uncond_log_var > EGARCH_LOG_VAR_CEILING:
            logging.warning(
                "EGARCH refit %s REJECTED: reason=uncond_log_var out of bounds (%.4f)",
                asset, uncond_log_var)
            return False

        # Parameter change detection
        if old_params is not None:
            d_omega = omega - old_params["omega"]
            d_alpha = alpha - old_params["alpha"]
            d_gamma = gamma - old_params["gamma"]
            d_beta = beta - old_params["beta"]
            logging.info(
                "EGARCH %s param delta: Δω=%.4f Δα=%.4f Δγ=%.4f Δβ=%.6f",
                asset, d_omega, d_alpha, d_gamma, d_beta)

        # Update params (lock protects concurrent reads from Firebase thread)
        new_params = {"omega": omega, "alpha": alpha, "gamma": gamma, "beta": beta}
        with self._lock:
            self._params[asset] = new_params
            self._mle_loglik[asset] = -result.fun
            self._mle_converged[asset] = converged
            self._log_var[asset] = uncond_log_var
            self._sigma[asset] = math.exp(uncond_log_var * 0.5)

        uncond_vol = math.exp(uncond_log_var * 0.5)
        half_life = (math.log(2) / (-math.log(beta))) * 5.0 if beta > 0 and beta < 1 else float('inf')
        elapsed_ms = (time.time() - t0) * 1000

        # Gamma sign interpretation
        if gamma > 0.01:
            gamma_sign = "positive=inverse_leverage"
        elif gamma < -0.01:
            gamma_sign = "negative=classic_leverage"
        else:
            gamma_sign = "near_zero"

        logging.info(
            "EGARCH refit %s: omega=%.4f alpha=%.4f gamma=%.4f beta=%.4f "
            "loglik=%.2f uncond_vol=%.8f half_life=%.1fs converged=%s n=%d elapsed_ms=%.1f",
            asset, omega, alpha, gamma, beta,
            -result.fun, uncond_vol, half_life, converged, n, elapsed_ms)
        logging.info("EGARCH %s gamma sign: %s", asset, gamma_sign)

        return True

    @staticmethod
    def _neg_log_likelihood(params, returns) -> float:
        """Negative log-likelihood for EGARCH(1,1)."""
        omega, alpha, gamma, beta = params
        n = len(returns)
        if n < 60:
            return 1e10

        # Init log_var from sample variance of first 60 returns
        sample_var = sum(r * r for r in returns[:60]) / 60.0
        if sample_var <= 0:
            sample_var = 1e-10
        log_var = math.log(sample_var)

        LOG_2PI = 1.8378770664093453  # log(2π)
        nll = 0.0
        e_abs_z = EGARCH_E_ABS_Z

        for i in range(n):
            r = returns[i]
            # NLL contribution: 0.5 * (log(2π) + log_var + r²/exp(log_var))
            var = math.exp(log_var)
            if var <= 0:
                var = 1e-30
            nll += 0.5 * (LOG_2PI + log_var + r * r / var)

            # EGARCH recursion
            sigma = math.sqrt(var)
            if sigma <= 0:
                sigma = 1e-15
            z = r / sigma
            log_var = omega + alpha * (abs(z) - e_abs_z) + gamma * z + beta * log_var
            log_var = max(-50.0, min(-5.0, log_var))

        return nll / n  # normalize for numerical stability

    def _load_state(self):
        """Load params and state from JSON file."""
        if not os.path.exists(EGARCH_STATE_PATH):
            logging.info("EGARCH loaded: 0 active, no state file")
            return
        try:
            with open(EGARCH_STATE_PATH, "r") as f:
                state = json.load(f)
            active_count = 0
            now = time.time()
            oldest_age = 0.0
            for asset in ASSETS:
                adata = state.get(asset)
                if adata and adata.get("params"):
                    self._params[asset] = adata["params"]
                    self._log_var[asset] = adata.get("log_var")
                    self._sigma[asset] = adata.get("sigma")
                    self._n_updates[asset] = adata.get("n_updates", 0)
                    self._mle_loglik[asset] = adata.get("mle_loglik")
                    self._mle_converged[asset] = adata.get("mle_converged", False)
                    active_count += 1
                    logging.info(
                        "EGARCH %s: restored params omega=%.4f alpha=%.4f "
                        "gamma=%.4f beta=%.4f log_var=%.4f",
                        asset,
                        adata["params"]["omega"], adata["params"]["alpha"],
                        adata["params"]["gamma"], adata["params"]["beta"],
                        adata.get("log_var", 0))
                # Restore return buffer
                if adata:
                    try:
                        for r in adata.get("returns", []):
                            self._returns[asset].append(r)
                    except Exception as re:
                        logging.warning("EGARCH returns load failed for %s: %s (starting fresh)", asset, re)
                        self._returns[asset].clear()
            self._last_refit = state.get("last_refit", 0.0)
            age = time.time() - self._last_refit if self._last_refit > 0 else float('inf')
            logging.info("EGARCH loaded: %d active, state_age=%.0fs", active_count, age)
            # Log restored return counts
            ret_counts = {a: len(self._returns[a]) for a in ASSETS}
            if any(ret_counts.values()):
                logging.info(
                    "EGARCH returns restored: BTC=%d ETH=%d SOL=%d XRP=%d",
                    ret_counts["BTC"], ret_counts["ETH"], ret_counts["SOL"], ret_counts["XRP"])
        except Exception as e:
            logging.warning("EGARCH state load failed: %s", e)

    def _save_state(self):
        """Save state and return buffers to JSON file (atomic write)."""
        state = {"last_refit": self._last_refit}
        for asset in ASSETS:
            state[asset] = {
                "params": self._params[asset],
                "log_var": self._log_var[asset],
                "sigma": self._sigma[asset],
                "n_updates": self._n_updates[asset],
                "mle_loglik": self._mle_loglik[asset],
                "mle_converged": self._mle_converged[asset],
                "returns": list(self._returns[asset]),
            }
        tmp_path = EGARCH_STATE_PATH + ".tmp"
        try:
            with open(tmp_path, "w") as f:
                json.dump(state, f, indent=2)
            os.replace(tmp_path, EGARCH_STATE_PATH)
        except Exception as e:
            logging.warning("EGARCH state save failed: %s", e)

    def get_diagnostics(self) -> Dict:
        """Per-asset diagnostics dict for Firebase (thread-safe)."""
        result = {}
        now = time.time()
        for asset in ASSETS:
            with self._lock:
                params = self._params.get(asset)
                log_var = self._log_var.get(asset)
                sigma = self._sigma.get(asset)
                mle_ll = self._mle_loglik.get(asset)
                mle_conv = self._mle_converged.get(asset, False)
            n_rets = len(self._returns.get(asset, []))
            n_upd = self._n_updates.get(asset, 0)

            diag: Dict = {
                "has_params": params is not None,
                "n_returns": n_rets,
                "n_updates": n_upd,
                "current_sigma": round(sigma, 10) if sigma is not None else None,
                "current_log_var": round(log_var, 4) if log_var is not None else None,
                "mle_loglik": round(mle_ll, 4) if mle_ll is not None else None,
                "mle_converged": mle_conv,
                "last_refit_age_s": round(now - self._last_refit, 1) if self._last_refit > 0 else None,
            }

            if params is not None:
                beta = params["beta"]
                omega = params["omega"]
                diag["params"] = {k: round(v, 6) for k, v in params.items()}
                if abs(beta) < 1.0:
                    uncond_lv = omega / (1.0 - beta)
                    diag["unconditional_vol"] = round(math.exp(uncond_lv * 0.5), 10)
                    if beta > 0 and beta < 1:
                        diag["half_life_seconds"] = round(
                            (math.log(2) / (-math.log(beta))) * 5.0, 1)
                    else:
                        diag["half_life_seconds"] = None
                else:
                    diag["unconditional_vol"] = None
                    diag["half_life_seconds"] = None
                diag["asymmetry_gamma"] = round(params["gamma"], 6)
            else:
                diag["params"] = None
                diag["unconditional_vol"] = None
                diag["half_life_seconds"] = None
                diag["asymmetry_gamma"] = None

            result[asset] = diag
        return result


# ═════════════════════════════════════════════════════════════════════════════
#  MincerZarnowitzTracker – Rolling R² for EGARCH forecast evaluation
# ═════════════════════════════════════════════════════════════════════════════

class MincerZarnowitzTracker:
    """Rolling Mincer-Zarnowitz R² for EGARCH forecast evaluation.

    Regression: σ²_realized = α + β·σ²_forecast + ε
    R² measures how well EGARCH forecasts explain realized variance.
    Higher R² → more weight to EGARCH in the blend.

    Also tracks QLIKE loss for shadow evaluation.
    """

    def __init__(self):
        # Rolling buffers: (forecast_var, realized_var) pairs
        self._pairs: Dict[str, deque] = {
            a: deque(maxlen=MZ_WINDOW) for a in ASSETS
        }
        self._r_squared: Dict[str, Optional[float]] = {a: None for a in ASSETS}
        self._qlike: Dict[str, Optional[float]] = {a: None for a in ASSETS}
        self._last_recompute: Dict[str, float] = {a: 0.0 for a in ASSETS}
        self._egarch_weight: Dict[str, float] = {a: EGARCH_WEIGHT_DEFAULT for a in ASSETS}
        self._load_state()

    def record(self, asset: str, egarch_var: float, realized_var: float):
        """Record a (forecast, realized) variance pair."""
        if egarch_var <= 0 or realized_var <= 0:
            return
        self._pairs[asset].append((egarch_var, realized_var))

    def maybe_recompute(self, asset: str, now: float) -> float:
        """Recompute R² and weight if enough time has passed. Returns current weight."""
        if now - self._last_recompute.get(asset, 0) < MZ_RECOMPUTE_INTERVAL:
            return self._egarch_weight[asset]

        self._last_recompute[asset] = now
        pairs = list(self._pairs[asset])
        n = len(pairs)

        if n < MZ_MIN_OBS:
            self._egarch_weight[asset] = EGARCH_WEIGHT_DEFAULT
            return EGARCH_WEIGHT_DEFAULT

        forecasts = [p[0] for p in pairs]
        actuals = [p[1] for p in pairs]

        # OLS: actual = alpha + beta * forecast
        mean_f = sum(forecasts) / n
        mean_a = sum(actuals) / n
        cov_fa = sum((f - mean_f) * (a - mean_a) for f, a in zip(forecasts, actuals)) / n
        var_f = sum((f - mean_f) ** 2 for f in forecasts) / n
        var_a = sum((a - mean_a) ** 2 for a in actuals) / n

        if var_f < 1e-30 or var_a < 1e-30:
            self._r_squared[asset] = 0.0
            self._egarch_weight[asset] = EGARCH_WEIGHT_DEFAULT
            return EGARCH_WEIGHT_DEFAULT

        r_sq = (cov_fa ** 2) / (var_f * var_a)
        r_sq = max(0.0, min(1.0, r_sq))
        self._r_squared[asset] = round(r_sq, 4)

        # Map R² to weight within asset-specific bounds (BEFORE QLIKE so weight is always set)
        lo, hi = EGARCH_WEIGHT_BOUNDS.get(asset, (0.05, 0.25))
        w = lo + r_sq * (hi - lo)
        self._egarch_weight[asset] = round(w, 4)

        # QLIKE for shadow evaluation
        try:
            self._qlike[asset] = round(HAREstimator._compute_qlike(actuals, forecasts), 6)
        except Exception:
            logging.warning("MZ tracker: QLIKE computation failed for %s", asset, exc_info=True)

        return self._egarch_weight[asset]

    def get_weight(self, asset: str) -> float:
        return self._egarch_weight.get(asset, EGARCH_WEIGHT_DEFAULT)

    def _load_state(self):
        try:
            with open(EGARCH_BLEND_STATE_PATH, "r") as f:
                state = json.load(f)
            for asset in ASSETS:
                if asset in state.get("r_squared", {}):
                    self._r_squared[asset] = state["r_squared"][asset]
                if asset in state.get("weights", {}):
                    self._egarch_weight[asset] = state["weights"][asset]
                if asset in state.get("pairs", {}):
                    for p in state["pairs"][asset][-MZ_WINDOW:]:
                        self._pairs[asset].append(tuple(p))
            logging.info("MZ tracker state loaded: R²=%s weights=%s",
                         self._r_squared, self._egarch_weight)
        except (FileNotFoundError, json.JSONDecodeError):
            logging.info("MZ tracker: no saved state, starting fresh")

    def save_state(self):
        try:
            state = {
                "r_squared": self._r_squared,
                "weights": self._egarch_weight,
                "qlike": self._qlike,
                "pairs": {a: list(self._pairs[a])[-MZ_WINDOW:] for a in ASSETS},
            }
            with open(EGARCH_BLEND_STATE_PATH, "w") as f:
                json.dump(state, f)
        except Exception:
            logging.debug("MZ tracker: save_state failed", exc_info=True)


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
                asset: Optional[str] = None) -> Dict:
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

        # ── Safety: refuse if z-score is absurdly large ──────────────────
        if abs(z_score) > Z_SCORE_MAX:
            result["reason"] = (
                f"|z_score|={abs(z_score):.1f} > {Z_SCORE_MAX} — "
                f"volatility estimate likely wrong, refusing to trade"
            )
            logging.warning(
                f"ProbabilityEngine: {result['reason']} "
                f"(spot={spot}, threshold={threshold}, rv={blended_rv:.8f})"
            )
            return result

        # ── Raw probability via configurable distribution CDF ────────────
        # P(price stays above threshold) = P(move > threshold - spot)
        # = P(Z > z_score) = 1 - CDF(z_score)
        raw_prob = ProbabilityEngine._cdf_complement(z_score, asset)
        result["raw_prob"] = round(raw_prob, 6)

        # ── Calibration: adaptive (if trained) or fixed β=0.85 ──────────
        dynamic_cap = ProbabilityEngine._dynamic_cap(seconds_remaining)
        if _CALIBRATION_ENGINE is not None:
            calibrated_prob = _CALIBRATION_ENGINE.calibrate(raw_prob, cap=dynamic_cap)
            result["calibration_method"] = _CALIBRATION_ENGINE.active_method
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
    def _dynamic_cap(seconds_remaining: float) -> float:
        """Return probability cap based on time to close."""
        for threshold_secs, cap in DYNAMIC_CAP_SCHEDULE:
            if seconds_remaining > threshold_secs:
                return cap
        return DYNAMIC_CAP_SCHEDULE[-1][1]  # smallest TTC bracket

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

    def __init__(self, state_path: str = CALIBRATION_STATE_PATH):
        self.state_path = state_path
        self.active_method: str = "fixed_beta"  # current method in use
        self._observations: List[Tuple[float, int]] = []  # (raw_prob, binary_outcome)
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

            # observations are loaded from DB in load_training_data_from_db()

            if "prev_brier" in state:
                self._prev_brier = state["prev_brier"]

            logging.info(
                "CalibrationEngine loaded: method=%s, observations=%d, "
                "platt_trained=%s, beta_trained=%s, blr_trained=%s",
                self.active_method, len(self._observations),
                self._platt_trained, self._beta_trained, self._blr_trained,
            )
        except FileNotFoundError:
            logging.info("No calibration state found, starting fresh (fixed_beta fallback)")
        except Exception as e:
            logging.warning("Error loading calibration state: %s", e)

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
            logging.warning("CalibrationEngine: failed to save state: %s", e)

    # ── Inference ──────────────────────────────────────────────────────────

    def calibrate(self, raw_prob: float, cap: float) -> float:
        """Calibrate raw_prob using the active method. Sub-ms, called per evaluation."""
        if self.active_method == "platt" and self._platt_trained:
            result = self._platt_predict(raw_prob)
        elif self.active_method == "beta_cal" and self._beta_trained:
            result = self._beta_cal_predict(raw_prob)
        elif self.active_method == "blr" and self._blr_trained:
            result = self._blr_predict(raw_prob)
        else:
            return CalibrationEngine._fallback_calibrate(raw_prob, cap)
        # Learned method active: uncertainty shrinkage + safety ceiling only (no hard cap)
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
                    "CalibrationEngine: Platt trained — A=%.4f, B=%.4f, Brier=%.4f, n=%d",
                    self._platt_A, self._platt_B, trained_methods["platt"], n,
                )
            except Exception as e:
                logging.warning("CalibrationEngine: Platt training failed: %s", e)

        if n >= CALIBRATION_MIN_SAMPLES_BETA:
            try:
                self._train_beta_cal()
                self._beta_trained = True
                trained_methods["beta_cal"] = self._compute_brier_for_method("beta_cal")
                logging.info(
                    "CalibrationEngine: Beta Cal trained — a=%.4f, b=%.4f, c=%.4f, "
                    "Brier=%.4f, n=%d",
                    self._beta_a, self._beta_b, self._beta_c,
                    trained_methods["beta_cal"], n,
                )
            except Exception as e:
                logging.warning("CalibrationEngine: Beta Cal training failed: %s", e)

        if n >= CALIBRATION_MIN_SAMPLES_BLR:
            try:
                self._train_blr()
                self._blr_trained = True
                trained_methods["blr"] = self._compute_brier_for_method("blr")
                logging.info(
                    "CalibrationEngine: BLR trained — mu=[%.4f, %.4f], Brier=%.4f, n=%d",
                    self._blr_mu[0], self._blr_mu[1], trained_methods["blr"], n,
                )
            except Exception as e:
                logging.warning("CalibrationEngine: BLR training failed: %s", e)

        if not trained_methods:
            return False

        # ── Promote best Brier method ─────────────────────────────────────
        best_method = min(trained_methods, key=trained_methods.get)
        best_brier = trained_methods[best_method]

        # Regression guard: reject if best is worse than previous + margin
        if self._prev_brier is not None and best_brier > self._prev_brier + 0.01:
            logging.warning(
                "CalibrationEngine: promotion REJECTED — best Brier %.4f > prev %.4f + 0.01 "
                "(methods: %s)",
                best_brier, self._prev_brier, trained_methods,
            )
            return False

        old_method = self.active_method
        self.active_method = best_method
        self._prev_brier = best_brier

        logging.info(
            "CalibrationEngine: PROMOTED %s -> %s (Brier=%.4f, alternatives=%s)",
            old_method, best_method, best_brier,
            {k: round(v, 4) for k, v in trained_methods.items()},
        )

        self._save_state()
        return True

    def load_training_data_from_db(self, state: "StateManager"):
        """Rebuild training data from evaluated opportunities on startup.

        Note: rejected_opportunities (z-score rejections) are excluded because
        their bimodal raw_prob distribution (clustered at 0 and 1) contaminates
        Platt training — 2 NO outcomes at raw_prob≈1.0 create extreme log-loss
        pressure that drives Platt A well below 1.0, compressing all high-end
        calibrated probabilities (e.g. raw 0.95 → cal 0.89 instead of ~0.96).
        """
        try:
            self._observations.clear()

            rows = state.conn.execute(
                "SELECT raw_prob, market_result FROM evaluated_opportunities "
                "WHERE status='settled' AND raw_prob IS NOT NULL "
                "AND market_result IS NOT NULL"
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
                "CalibrationEngine: loaded %d observations from DB (total: %d)",
                loaded, len(self._observations),
            )

            # Attempt initial training if enough data
            if loaded > 0:
                self._last_retrain = 0.0  # force retrain check
                self.maybe_retrain()

        except Exception as e:
            logging.warning("CalibrationEngine: failed to load from DB: %s", e)

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
                "CalibrationEngine: Platt params extreme (A=%.4f, B=%.4f), rejecting",
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
            "CalibrationEngine BACKTEST: old_brier=%.4f, new_brier=%.4f, "
            "improvement=%.4f, cap_truncated=%d/%d, high_prob_new=%d",
            result["old_brier"], result["new_brier"], result["brier_improvement"],
            cap_truncated, n, high_prob_markets,
        )
        return result


# ═════════════════════════════════════════════════════════════════════════════
#  PositionSizer
# ═════════════════════════════════════════════════════════════════════════════

class PositionSizer:
    """Edge-tiered position sizing with drawdown scaling.

    Sizing tiers (from SIZING_TIERS):
        edge ≥ 5%  → risk 50% of bankroll
        edge ≥ 3%  → risk 35% of bankroll
        edge ≥ 1.5% → risk 20% of bankroll

    Contracts = floor(bankroll × risk_fraction / price).

    Hard cap: MAX_RISK_PER_TRADE of bankroll (safety ceiling).
    Drawdown scaler: halves below 90%, quarters below 80%.
    """

    def __init__(self, starting_balance_cents: int = 0):
        self.starting_balance_cents = starting_balance_cents

    def compute(self, win_prob: float, price_cents: int,
                balance_cents: int) -> Dict:
        """Compute position size.

        Returns dict with: contracts, kelly_f, raw_contracts, drawdown_scaler, reason
        """
        result: Dict = {
            "contracts": 0,
            "kelly_f": 0.0,
            "raw_contracts": 0,
            "drawdown_scaler": 1.0,
            "reason": "",
        }

        if price_cents <= 0 or price_cents >= 100:
            result["reason"] = "invalid price"
            return result

        if balance_cents <= 0:
            result["reason"] = "no balance"
            return result

        # Compute fee-adjusted edge
        fee_1c = calculate_taker_fee(1, price_cents)
        b = (100 - price_cents - fee_1c) / (price_cents + fee_1c)
        p = win_prob
        q = 1.0 - p
        kelly_edge = (b * p - q) / b
        result["kelly_f"] = round(kelly_edge, 6)

        if kelly_edge <= 0:
            result["reason"] = "negative edge (Kelly <= 0)"
            return result

        # Edge-based tier selection: higher edge → larger risk fraction
        edge = win_prob - price_cents / 100.0
        risk_fraction = 0.0
        for min_edge, frac in SIZING_TIERS:
            if edge >= min_edge:
                risk_fraction = frac
                break

        if risk_fraction <= 0:
            result["reason"] = "edge below minimum tier"
            return result

        # Contracts = floor(bankroll × risk_fraction / price)
        raw_contracts = math.floor((balance_cents * risk_fraction) / price_cents)
        result["raw_contracts"] = raw_contracts

        if raw_contracts <= 0:
            result["reason"] = "risk budget rounds to 0 contracts"
            return result

        # Apply drawdown scaler
        scaler = self._drawdown_scaler(balance_cents)
        result["drawdown_scaler"] = scaler
        scaled_contracts = math.floor(raw_contracts * scaler)

        # Safety ceiling: MAX_RISK_PER_TRADE of bankroll
        max_by_risk = int((balance_cents * MAX_RISK_PER_TRADE) / price_cents)

        if max_by_risk < 1:
            result["reason"] = "balance too small for 1 contract within risk limit"
            return result

        contracts = min(scaled_contracts, max_by_risk)

        # Enforce minimum 1 when edge exists and risk budget allows
        contracts = max(contracts, 1)

        result["contracts"] = contracts
        result["reason"] = "ok"
        return result

    def _drawdown_scaler(self, balance_cents: int) -> float:
        """Scale position based on drawdown from starting balance."""
        if self.starting_balance_cents <= 0:
            return 1.0
        ratio = balance_cents / self.starting_balance_cents
        if ratio < DRAWDOWN_QUARTER_THRESHOLD:
            return 0.25
        if ratio < DRAWDOWN_HALF_THRESHOLD:
            return 0.5
        return 1.0


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
                 sizer: PositionSizer, order_flow: Optional[OrderFlowEngine] = None):
        self._client = client
        self._state = state
        self._feed = feed
        self._vol = vol
        self._logger = logger
        self._sizer = sizer
        self._order_flow = order_flow
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
        self._eval_opp_seen: Set[Tuple[str, str]] = set()

    # ── Public entry point ────────────────────────────────────────────────

    def scan(self, active_windows: List[Dict]) -> Optional[Dict]:
        """Evaluate all windows/markets, return best candidate or None."""
        now = time.time()
        ob_fetches_this_tick = 0
        candidates: List[Dict] = []

        # Clean up ask history and dedup set for tickers no longer in active windows
        active_tickers = set()
        for w in active_windows:
            for m in w.get("markets", []):
                active_tickers.add(m.get("ticker", ""))
        expired = [t for t in self._ticker_ask_history if t not in active_tickers]
        for t in expired:
            del self._ticker_ask_history[t]
        self._eval_opp_seen = {
            (tk, stage) for tk, stage in self._eval_opp_seen if tk in active_tickers
        }
        scan_stats: Dict[str, Dict[str, int]] = {
            a: {"evaluated": 0, "low_prob": 0, "no_orderbook": 0, "no_best_ask": 0,
                "price_out_of_range": 0, "insufficient_edge": 0, "zero_sizing": 0,
                "strategy_wait": 0, "candidates": 0}
            for a in ASSETS
        }

        # 1. Filter windows by time range
        time_ok_windows = [
            w for w in active_windows
            if MIN_SECONDS_BEFORE_CLOSE <= w["seconds_to_close"] <= MAX_SECONDS_BEFORE_CLOSE
        ]
        if not time_ok_windows:
            return None

        # 2. Get occupied timeslots (positions + resting orders)
        occupied = self._get_occupied_timeslots()

        # 3. Filter out windows whose timeslot is already occupied by another asset
        eligible_windows = []
        for w in time_ok_windows:
            ts = self._window_timeslot(w["event_ticker"])
            if ts in occupied:
                asset_in_slot = occupied[ts]
                if asset_in_slot != w["asset"]:
                    continue  # another asset occupies this timeslot
            eligible_windows.append(w)

        if not eligible_windows:
            return None

        # 4. Evaluate each market in each surviving window
        for window in eligible_windows:
            asset = window["asset"]
            spot = self._feed.get_price(asset)
            if spot is None or spot <= 0:
                continue

            vol_est = self._vol.update(asset)
            if vol_est is None or vol_est["blended_rv"] <= 0:
                continue

            blended_rv = vol_est["blended_rv"]
            seconds_remaining = window["seconds_to_close"]

            for mkt in window["markets"]:
                ticker = mkt.get("ticker", "")
                threshold = self._parse_threshold(mkt)
                if threshold is None:
                    continue

                scan_stats[asset]["evaluated"] += 1
                self._session_total_scanned += 1

                # Pre-filter: compute probability without market price
                prob_result = ProbabilityEngine.compute(
                    spot, threshold, seconds_remaining, blended_rv,
                    asset=asset
                )
                cal_prob = prob_result.get("calibrated_prob")
                raw_prob_pre = prob_result.get("raw_prob")
                calibration_method_pre = prob_result.get("calibration_method")
                if cal_prob is None:
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
                            "calibrated_prob": None,
                        }
                        self._state.insert_rejection(
                            ticker, window["event_ticker"], asset, reason,
                            prob_result.get("z_score"), spot, threshold,
                            blended_rv, rej_ask, seconds_remaining, None)
                        self._logger.log_rejection(rej_data)
                        logging.info(
                            f"Rejected opportunity: {ticker} — {reason}")
                    continue

                # Skip if calibrated prob too low to ever produce an edge
                min_prob_needed = (MIN_ENTRY_PRICE + MIN_EDGE_PCT) / 100.0
                if cal_prob < min_prob_needed:
                    scan_stats[asset]["low_prob"] += 1
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

                # Filter: ask must be in entry price range
                if not (MIN_ENTRY_PRICE <= best_ask <= MAX_ENTRY_PRICE):
                    scan_stats[asset]["price_out_of_range"] += 1
                    self._recent_opportunities.append({
                        "ticker": ticker, "asset": asset,
                        "seconds_to_close": round(seconds_remaining, 1),
                        "best_ask": best_ask, "edge_bps": None,
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
                            "rejection_reason": f"best_ask {best_ask}¢ outside [{MIN_ENTRY_PRICE}, {MAX_ENTRY_PRICE}]",
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
                                vol_regime=vol_est["regime"],
                                breakeven_wr=best_ask / 100.0,
                                ask_depth=ask_depth,
                                best_ask_source=best_ask_source,
                                raw_prob=raw_prob_pre,
                                calibration_method=calibration_method_pre)
                    except Exception:
                        pass
                    continue

                # Re-run probability with market price for sanity check
                prob_with_market = ProbabilityEngine.compute(
                    spot, threshold, seconds_remaining, blended_rv,
                    market_price_cents=best_ask,
                    asset=asset
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
                        }
                        self._state.insert_rejection(
                            ticker, window["event_ticker"], asset, reason,
                            prob_with_market.get("z_score"), spot, threshold,
                            blended_rv, best_ask, seconds_remaining,
                            prob_with_market.get("calibrated_prob"))
                        self._logger.log_rejection(rej_data)
                        logging.info(
                            f"Rejected opportunity: {ticker} — {reason}")
                    continue

                final_prob = prob_with_market["calibrated_prob"]
                z_score = prob_with_market["z_score"]
                raw_prob = prob_with_market.get("raw_prob")
                calibration_method = prob_with_market.get("calibration_method")

                # Order flow adjustment
                ofa_signals = None
                ofa_adjustment = 0.0
                if self._order_flow is not None:
                    try:
                        ofa_signals = self._order_flow.get_signals(asset)
                        ofa_adjustment = ofa_signals["prob_adjustment"]
                    except Exception:
                        logging.debug("OrderFlowEngine.get_signals failed", exc_info=True)
                calibrated_prob_raw = final_prob
                # Always compute dynamic cap for counterfactual logging
                _dyn_cap = ProbabilityEngine._dynamic_cap(seconds_remaining)
                if _CALIBRATION_ENGINE is not None and _CALIBRATION_ENGINE.is_learned_method_active():
                    # Learned method: no dynamic cap, use safety ceiling only
                    final_prob = max(0.01, min(NUMERICAL_SAFETY_CEILING, final_prob + ofa_adjustment))
                else:
                    final_prob = max(0.01, min(_dyn_cap, final_prob + ofa_adjustment))
                # Counterfactual: what the old system (fixed cap) would have produced
                _old_system_prob = max(0.01, min(_dyn_cap, calibrated_prob_raw + ofa_adjustment))
                if best_ask < ENDGAME_BLEND_PRICE:
                    _mkt = best_ask / 100.0
                    _old_system_prob = (1.0 - MARKET_BLEND_W) * _old_system_prob + MARKET_BLEND_W * _mkt

                # ── Market-price blending ──────────────────────────────────
                # For mid-range prices, blend model with market to temper overconfidence.
                # Skip blending for endgame (≥96c) where dynamic cap provides the edge.
                if best_ask < ENDGAME_BLEND_PRICE:
                    market_implied_prob = best_ask / 100.0
                    final_prob = (1.0 - MARKET_BLEND_W) * final_prob + MARKET_BLEND_W * market_implied_prob

                edge = final_prob - best_ask / 100.0

                # Fee-adjusted edge: subtract taker fee for 1 contract
                # (conservative — more contracts = lower per-contract fee)
                est_fee_1c = calculate_taker_fee(1, best_ask)
                fee_adjusted_edge = edge - est_fee_1c / 100.0

                # Filter: fee-adjusted edge must meet minimum
                if fee_adjusted_edge < MIN_EDGE_PCT / 100.0:
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
                            "rejection_reason": f"net_edge {fee_adjusted_edge:.4f} < min {MIN_EDGE_PCT / 100.0:.4f} (gross {edge:.4f}, fee {est_fee_1c}c)",
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
                        })
                        _dedup_key = (ticker, "insufficient_edge")
                        if _dedup_key not in self._eval_opp_seen:
                            self._eval_opp_seen.add(_dedup_key)
                            _ev = (final_prob * (100 - best_ask)) - ((1 - final_prob) * best_ask) - est_fee_1c
                            self._state.insert_evaluated_opportunity(
                                ticker, window["event_ticker"], asset,
                                "insufficient_edge",
                                rejection_reason=f"net_edge {fee_adjusted_edge:.4f} < min",
                                spot_price=spot, threshold=threshold,
                                volatility=blended_rv, market_price=best_ask,
                                seconds_to_close=seconds_remaining,
                                calibrated_prob=final_prob, edge=edge,
                                ofa_adjustment=ofa_adjustment,
                                z_score=z_score,
                                vol_regime=vol_est["regime"],
                                calibrated_prob_raw=calibrated_prob_raw,
                                breakeven_wr=best_ask / 100.0,
                                expected_value=round(_ev, 2),
                                ask_depth=ask_depth,
                                best_ask_source=best_ask_source,
                                ofa_confidence=ofa_signals["confidence"] if ofa_signals else "none",
                                raw_prob=raw_prob,
                                calibration_method=calibration_method,
                                old_system_prob=_old_system_prob,
                                fee_adjusted_edge=fee_adjusted_edge)
                    except Exception:
                        pass
                    continue

                # Compute position size via Kelly criterion
                balance = self._get_balance_cached()
                if balance is None or balance <= 0:
                    continue
                sizing = self._sizer.compute(final_prob, best_ask, balance)
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
                                fee_adjusted_edge=fee_adjusted_edge)
                    except Exception:
                        pass
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
                                fee_adjusted_edge=fee_adjusted_edge)
                    except Exception:
                        pass
                    continue

                scan_stats[asset]["candidates"] += 1
                self._session_total_candidates += 1
                self._session_asset_perf[asset]["opportunities_found"] += 1
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
                    })
                except Exception:
                    pass

                candidates.append({
                    "ticker": ticker,
                    "event_ticker": window["event_ticker"],
                    "asset": asset,
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
                    },
                    "calibrated_prob_raw": round(calibrated_prob_raw, 6),
                    "ofa_adjustment": round(ofa_adjustment, 6),
                    "ofa_confidence": ofa_signals["confidence"] if ofa_signals else "none",
                    "raw_prob": raw_prob,
                    "calibration_method": calibration_method,
                    "old_system_prob": round(_old_system_prob, 6),
                    "fee_adjusted_edge": round(fee_adjusted_edge, 6),
                })

                # Respect per-tick orderbook fetch cap
                if ob_fetches_this_tick >= MAX_OB_FETCHES_PER_TICK:
                    break
            if ob_fetches_this_tick >= MAX_OB_FETCHES_PER_TICK:
                break

        if not candidates:
            self._last_scan_stats = scan_stats
            return None

        # ── Single-asset-per-timeslot: pick highest edge per 15-min window ──
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
                                fee_adjusted_edge=c.get("fee_adjusted_edge"))
                    except Exception:
                        pass

        best = max(filtered, key=lambda c: c["edge"])
        self._logger.log_scan({
            "type": "opportunity",
            "candidates_evaluated": len(candidates),
            "candidates_after_single_asset": len(filtered),
            "chosen_strategy": best.get("strategy"),
            **{k: v for k, v in best.items() if k not in ("strategy_scores", "ob_snapshot")},
        })
        self._last_scan_stats = scan_stats
        return best

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
        """Return (orderbook_data, was_fresh_fetch). Uses TTL cache."""
        now = time.time()
        cached = self._ob_cache.get(ticker)
        if cached:
            data, fetch_time = cached
            if now - fetch_time < ORDERBOOK_CACHE_TTL:
                return (data, False)

        # Fresh fetch
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

    def _get_occupied_timeslots(self) -> Dict[str, str]:
        """Return {timeslot: asset} for timeslots with open positions or resting orders."""
        occupied: Dict[str, str] = {}

        for pos in self._state.get_open_positions():
            et = pos.get("event_ticker", "")
            asset = pos.get("asset", "")
            ts = self._window_timeslot(et)
            if ts:
                occupied[ts] = asset

        for order in self._state.get_resting_orders():
            et = order.get("event_ticker", "")
            asset = order.get("asset", "")
            ts = self._window_timeslot(et)
            if ts:
                occupied[ts] = asset

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
                 logger: Logger, main_loop=None):
        self._client = client
        self._state = state
        self._logger = logger
        self._ml = main_loop
        self._active_order: Optional[Dict] = None
        self._last_poll: float = 0.0
        self._ask_history: deque = deque(maxlen=30)

    @property
    def has_active_order(self) -> bool:
        return self._active_order is not None

    # ── Public interface ──────────────────────────────────────────────────

    def execute(self, candidate: Dict) -> Optional[Dict]:
        """Always submit maker order. Escalation to taker happens in tick()."""
        if self._active_order is not None:
            return None
        self._ask_history.clear()

        if OBSERVATION_MODE:
            logging.info(
                f"OBSERVATION MODE: Would place maker for {candidate['ticker']} "
                f"at {candidate.get('best_yes_ask', '?')}¢ for "
                f"{candidate.get('position_size', '?')} contracts"
            )
            try:
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
                })
                if _TELEGRAM:
                    _ba = candidate.get("best_yes_ask", "?")
                    _edge = candidate.get("edge")
                    _prob = candidate.get("calibrated_prob")
                    _sz = candidate.get("position_size", "?")
                    _edge_s = f"{_edge:.1%}" if _edge is not None else "?"
                    _prob_s = f"{_prob:.0%}" if _prob is not None else "?"
                    _TELEGRAM.send(
                        f"\U0001f4ca {candidate['ticker']} @ {_ba}c, "
                        f"edge={_edge_s}, prob={_prob_s}, size={_sz}",
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
                        fee_adjusted_edge=candidate.get("fee_adjusted_edge"))
            except Exception:
                pass
            return None

        self._submit_maker(candidate)
        return None

    def tick(self) -> Optional[Dict]:
        """Called each main-loop tick.  Polls for maker fill, then
        adaptively escalates to taker based on urgency if unfilled.
        """
        if self._active_order is None:
            return None

        now = time.time()
        if now - self._last_poll < MAKER_POLL_INTERVAL:
            return None
        self._last_poll = now

        order = self._active_order

        # 1. Check for maker fill
        fill = self._check_for_fill(order)
        if fill:
            self._on_fill(fill, order)
            self._active_order = None
            return fill

        elapsed = now - order["submit_time"]
        remaining = order["seconds_to_close_at_submit"] - elapsed

        # 2. Too close to expiry — cancel, don't escalate
        if remaining < MIN_SECONDS_BEFORE_CLOSE:
            self._cancel_active("close_approaching")
            return None

        # 3. Escalation: maker waited long enough?
        escalation_wait = self._escalation_wait(remaining)
        if elapsed >= escalation_wait:
            return self._escalate_to_taker(order, remaining)

        # 4. Hard timeout fallback
        if elapsed >= MAKER_TIMEOUT_SECONDS:
            self._cancel_active("timeout")

        return None

    @staticmethod
    def _escalation_wait(remaining: float) -> float:
        """Urgency-based maker wait before escalating to taker."""
        if remaining >= 60:
            return ESCALATION_WAIT_LONG     # 15s
        elif remaining >= 30:
            return ESCALATION_WAIT_MEDIUM   # 10s
        else:
            return ESCALATION_WAIT_SHORT    # 5s

    # ── Market Intelligence Helpers ───────────────────────────────────────

    def _convergence_velocity(self) -> float:
        """Upward price movement in cents over the convergence window."""
        if len(self._ask_history) < 2:
            return 0.0
        now = time.time()
        cutoff = now - CONVERGENCE_WINDOW_SECONDS
        oldest_price = None
        for ts, price in self._ask_history:
            if ts >= cutoff:
                oldest_price = price
                break
        if oldest_price is None:
            return 0.0
        latest_price = self._ask_history[-1][1]
        return latest_price - oldest_price

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

    # ── Panic Capture ──────────────────────────────────────────────────────

    def _execute_panic_capture(self, order: Dict):
        """Cancel maker and place resting 99¢ panic capture bid."""
        self._cancel_active("panic_capture")
        self._submit_panic_bid(order)

    def _submit_panic_bid(self, old_order: Dict):
        """Place resting 99¢ GTC limit bid for panic capture."""
        candidate = old_order["candidate"]
        ticker = old_order["ticker"]
        count = candidate["position_size"]
        price = PANIC_BID_PRICE
        balance = old_order["balance_at_entry"]
        remaining = (old_order["seconds_to_close_at_submit"]
                     - (time.time() - old_order["submit_time"]))

        client_oid = str(uuid.uuid4())

        self._state.insert_bot_order(
            client_oid, ticker, candidate["event_ticker"],
            candidate["asset"], "yes", count, price, False
        )

        resp = self._client.place_order(
            ticker=ticker, side="yes", action="buy",
            count=count, yes_price=price,
            client_order_id=client_oid
        )

        if resp is None:
            self._state.mark_order_status(client_oid, "api_error")
            logging.error(f"Panic capture order failed: {ticker}")
            return

        order_id = (resp.get("order") or {}).get("order_id", client_oid)
        self._state.confirm_order_submitted(client_oid, order_id)

        self._active_order = {
            "order_id": order_id,
            "client_order_id": client_oid,
            "ticker": ticker,
            "event_ticker": candidate["event_ticker"],
            "asset": candidate["asset"],
            "price_cents": price,
            "count": count,
            "is_taker": False,
            "is_panic": True,
            "submit_time": time.time(),
            "seconds_to_close_at_submit": remaining,
            "candidate": candidate,
            "balance_at_entry": balance,
        }
        self._last_poll = time.time()

        self._logger.log_order({
            "action": "panic_capture_submitted",
            "ticker": ticker,
            "order_id": order_id,
            "client_order_id": client_oid,
            "price_cents": price,
            "count": count,
            "z_score": candidate.get("z_score"),
        })
        logging.info(
            f"Panic capture: {ticker} {count}x @ {price}¢ "
            f"(z={candidate.get('z_score', 0):.1f}, remaining={remaining:.0f}s)"
        )

    def _submit_panic_from_candidate(self, candidate: Dict):
        """Place panic capture bid directly from a candidate (no active order)."""
        ticker = candidate["ticker"]
        count = candidate["position_size"]
        price = PANIC_BID_PRICE
        balance = candidate["balance_at_scan"]
        remaining = candidate["seconds_to_close"]

        client_oid = str(uuid.uuid4())

        self._state.insert_bot_order(
            client_oid, ticker, candidate["event_ticker"],
            candidate["asset"], "yes", count, price, False
        )

        resp = self._client.place_order(
            ticker=ticker, side="yes", action="buy",
            count=count, yes_price=price,
            client_order_id=client_oid
        )

        if resp is None:
            self._state.mark_order_status(client_oid, "api_error")
            logging.error(f"Panic capture order failed: {ticker}")
            return

        order_id = (resp.get("order") or {}).get("order_id", client_oid)
        self._state.confirm_order_submitted(client_oid, order_id)

        self._active_order = {
            "order_id": order_id,
            "client_order_id": client_oid,
            "ticker": ticker,
            "event_ticker": candidate["event_ticker"],
            "asset": candidate["asset"],
            "price_cents": price,
            "count": count,
            "is_taker": False,
            "is_panic": True,
            "submit_time": time.time(),
            "seconds_to_close_at_submit": remaining,
            "candidate": candidate,
            "balance_at_entry": balance,
        }
        self._last_poll = time.time()

        self._logger.log_order({
            "action": "panic_capture_submitted",
            "ticker": ticker,
            "order_id": order_id,
            "client_order_id": client_oid,
            "price_cents": price,
            "count": count,
            "z_score": candidate.get("z_score"),
        })
        logging.info(
            f"Panic capture: {ticker} {count}x @ {price}¢ "
            f"(z={candidate.get('z_score', 0):.1f}, remaining={remaining:.0f}s)"
        )

    def _escalate_to_taker(self, order: Dict, remaining: float,
                           reason: str = "escalation_wait") -> Optional[Dict]:
        """Cancel maker and re-submit as taker at current best ask."""
        ticker = order["ticker"]
        elapsed = time.time() - order["submit_time"]

        # Cancel the active maker order
        self._cancel_active(reason)

        # Re-fetch orderbook for current best ask
        ob_raw = self._client.get_orderbook(ticker, depth=5)
        if ob_raw is None:
            logging.warning(f"Escalation aborted: orderbook fetch failed for {ticker}")
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
            return None

        if best_ask < MIN_ENTRY_PRICE or best_ask > ESCALATION_MAX_ENTRY:
            logging.warning(
                f"Escalation aborted: price {best_ask}¢ out of range "
                f"[{MIN_ENTRY_PRICE}-{ESCALATION_MAX_ENTRY}¢] for {ticker}"
            )
            return None

        # Determine urgency tier for logging
        if remaining >= 60:
            tier = "long"
        elif remaining >= 30:
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
        })
        logging.info(
            f"Escalating to taker: {ticker} {order['count']}x "
            f"maker={order['price_cents']}¢ → taker={best_ask}¢ "
            f"(slip={price_slip}¢, tier={tier})"
        )

        # Build modified candidate with fresh best ask
        candidate = dict(order["candidate"])
        candidate["best_yes_ask"] = best_ask

        return self._submit_taker(candidate)

    # ── Maker ─────────────────────────────────────────────────────────────

    def _submit_maker(self, candidate: Dict, aggressive: bool = False):
        """Submit maker limit order below fair value.

        Patient: 1-2¢ below fair value (wider spread).
        Aggressive: always 1¢ below (tighter, more likely to fill).
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
        if price < MIN_ENTRY_PRICE:
            return

        client_oid = str(uuid.uuid4())

        # Persist before submission
        self._state.insert_bot_order(
            client_oid, ticker, candidate["event_ticker"],
            candidate["asset"], "yes", count, price, False
        )

        # Submit
        resp = self._client.place_order(
            ticker=ticker, side="yes", action="buy",
            count=count, yes_price=price,
            client_order_id=client_oid
        )

        if resp is None:
            self._state.mark_order_status(client_oid, "api_error")
            logging.error(f"Maker order submission failed: {ticker}")
            return

        order_id = (resp.get("order") or {}).get("order_id", client_oid)
        self._state.confirm_order_submitted(client_oid, order_id)
        if self._ml:
            self._ml._session_maker_submissions += 1

        self._active_order = {
            "order_id": order_id,
            "client_order_id": client_oid,
            "ticker": ticker,
            "event_ticker": candidate["event_ticker"],
            "asset": candidate["asset"],
            "price_cents": price,
            "count": count,
            "is_taker": False,
            "submit_time": time.time(),
            "seconds_to_close_at_submit": candidate["seconds_to_close"],
            "candidate": candidate,
            "balance_at_entry": balance,
        }
        self._last_poll = time.time()

        self._logger.log_order({
            "action": "maker_submitted",
            "ticker": ticker,
            "order_id": order_id,
            "client_order_id": client_oid,
            "price_cents": price,
            "count": count,
            "fair_value": fair_value,
        })
        logging.info(
            f"Maker order: {ticker} {count}x @ {price}¢ "
            f"(fair={fair_value}¢)"
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
        self._state.insert_bot_order(
            client_oid, ticker, candidate["event_ticker"],
            candidate["asset"], "yes", count, price, True
        )

        # Submit at best ask — crosses spread for immediate fill
        resp = self._client.place_order(
            ticker=ticker, side="yes", action="buy",
            count=count, yes_price=price,
            client_order_id=client_oid
        )

        if resp is None:
            self._state.mark_order_status(client_oid, "api_error")
            logging.error(f"Taker order submission failed: {ticker}")
            return None

        order_id = (resp.get("order") or {}).get("order_id", client_oid)
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
        }

        self._logger.log_order({
            "action": "taker_submitted",
            "ticker": ticker,
            "order_id": order_id,
            "client_order_id": client_oid,
            "price_cents": price,
            "count": count,
        })
        logging.info(f"Taker order: {ticker} {count}x @ {price}¢")

        # Brief wait then check fill
        time.sleep(1.0)
        fill = self._check_for_fill(order_info)
        if fill:
            self._on_fill(fill, order_info)
            return fill

        # Not filled — cancel
        self._client.cancel_order(order_id)
        self._state.mark_order_status(order_id, "canceled")
        self._logger.log_order({
            "action": "taker_canceled",
            "ticker": ticker,
            "order_id": order_id,
            "reason": "not_filled",
        })
        logging.warning(f"Taker order not filled, canceled: {ticker}")
        return None

    # ── Fill detection ────────────────────────────────────────────────────

    def _check_for_fill(self, order: Dict) -> Optional[Dict]:
        """Check if order has been filled via REST fills endpoint."""
        min_ts = int(order["submit_time"])
        resp = self._client.get_fills(
            ticker=order["ticker"], min_ts=min_ts
        )
        if not resp or not resp.get("fills"):
            return None

        for fill in resp["fills"]:
            if fill.get("order_id") == order["order_id"]:
                return fill
        return None

    # ── Fill handling ─────────────────────────────────────────────────────

    def _on_fill(self, fill: Dict, order: Dict):
        """Handle fill: update SQLite, log trade, record position."""
        order_id = order["order_id"]
        ticker = order["ticker"]
        candidate = order["candidate"]

        # Update order status
        self._state.mark_order_status(order_id, "filled")

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

        # Extract fill details — prefer FP/dollar fields, fall back to legacy
        fill_count = fp_str_to_int(fill.get("count_fp")) or (fill.get("count") or order["count"])
        fill_price_d = fill.get("yes_price_dollars")
        fill_price = dollars_str_to_cents(fill_price_d) if fill_price_d else (fill.get("yes_price") or order["price_cents"])

        # Record position in SQLite
        self._state.record_position_from_fill(
            ticker=ticker,
            event_ticker=order["event_ticker"],
            asset=order["asset"],
            side="yes",
            count=fill_count,
            price_cents=fill_price,
            strategy=candidate.get("strategy"),
            seconds_to_close=order.get("seconds_to_close_at_submit"),
            fill_latency=fill_latency,
            vol_regime=candidate.get("vol_regime"),
            calibrated_prob=candidate.get("calibrated_prob"),
            edge=candidate.get("edge"),
            kelly_f=candidate.get("kelly_f"),
        )

        # Log trade with all required fields
        is_taker = order["is_taker"]
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
        )

    # ── Cancel ────────────────────────────────────────────────────────────

    def _cancel_active(self, reason: str):
        """Cancel the active maker order."""
        if self._active_order is None:
            return

        order = self._active_order
        self._client.cancel_order(order["order_id"])
        self._state.mark_order_status(order["order_id"], "canceled")

        self._logger.log_order({
            "action": "maker_canceled",
            "ticker": order["ticker"],
            "order_id": order["order_id"],
            "reason": reason,
            "elapsed": round(time.time() - order["submit_time"], 1),
        })
        logging.info(
            f"Maker order canceled: {order['ticker']} reason={reason}"
        )
        self._active_order = None


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
                 logger: Logger):
        self._client = client
        self._state = state
        self._logger = logger
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
        open_positions = self._state.get_open_positions()
        if not open_positions:
            return

        resp = self._client.get_settlements(min_ts=self._last_check_ts)
        if not resp or "settlements" not in resp:
            return

        settlements = resp["settlements"]
        if not settlements:
            return

        our_tickers = {p["ticker"] for p in open_positions}
        processed_any = False

        for s in settlements:
            ticker = s.get("ticker", "")

            # Skip if already processed (dedup)
            if ticker in self._processed_tickers:
                continue

            # Only process settlements for our open positions
            if ticker not in our_tickers:
                continue

            self._process_settlement(s)
            processed_any = True

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
            logging.warning(
                f"SettlementTracker: unrecognized market_result "
                f"'{market_result}' for {ticker}"
            )

        # P&L from revenue (API is truth)
        total_cost = pos["total_cost_cents"]
        fee = calculate_taker_fee(pos["count"], pos["avg_price_cents"])
        pnl = revenue - total_cost

        # Record in SQLite via existing StateManager method
        self._state.record_settlement(settlement)

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
            "count": pos["count"],
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
            _TELEGRAM.send(f"{emoji} {outcome} {ticker} {sign}{pnl}c")

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
                logging.debug(
                    f"Rejection settlement check failed for {ticker}: {e}")

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

        self._state.mark_rejection_settled(ticker)
        self._settled_rejection_tickers.add(ticker)
        self._pending_rejection_tickers.discard(ticker)

        logging.info(
            f"Rejection settled: {ticker} -> {counterfactual_outcome} "
            f"(result={result}, would_have_profit={would_have_profit}¢)"
        )

    # ── Evaluated Opportunity Settlement ──────────────────────────────────

    def _poll_evaluated_opportunities(self):
        """Check if any evaluated opportunities have settled for counterfactual tracking."""
        try:
            rows = self._state.get_unsettled_evaluated_opportunities()
        except Exception as e:
            logging.debug(f"get_unsettled_evaluated_opportunities failed: {e}")
            return

        if not rows:
            return

        for row in rows:
            ticker = row["ticker"]
            opp_id = row["id"]
            try:
                resp = self._client.get_market(ticker)
                if not resp:
                    continue
                market = resp.get("market", resp)
                result = market.get("result", "")
                if not result:
                    continue

                entry_price = row["market_price"]
                if entry_price is None:
                    would_have_profit = None
                    taker_fee = 0
                    maker_fee = 0
                    pnl_taker = None
                    pnl_maker = None
                    count = row.get("position_size") or 1
                    counterfactual_outcome = "unknown_no_price"
                else:
                    count = row.get("position_size") or 1
                    taker_fee = calculate_taker_fee(count, int(entry_price))
                    maker_fee = calculate_maker_fee(count, int(entry_price))
                    if result in ("yes", "all_yes"):
                        pnl_taker = (100 - entry_price) * count - taker_fee
                        pnl_maker = (100 - entry_price) * count - maker_fee
                        counterfactual_outcome = "would_have_won"
                    elif result in ("no", "all_no"):
                        pnl_taker = -(entry_price * count + taker_fee)
                        pnl_maker = -(entry_price * count + maker_fee)
                        counterfactual_outcome = "would_have_lost"
                    else:
                        pnl_taker = None
                        pnl_maker = None
                        taker_fee = 0
                        maker_fee = 0
                        counterfactual_outcome = f"unknown_result_{result}"
                    would_have_profit = pnl_taker  # conservative (taker)

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

                self._state.mark_evaluated_opportunity_settled(
                    opp_id, market_result=result,
                    counterfactual_pnl=would_have_profit)

                # Feed to calibration engine
                raw_p = row.get("raw_prob")
                if raw_p is not None and result in ("yes", "all_yes", "no", "all_no"):
                    cal_binary = 1 if result in ("yes", "all_yes") else 0
                    if _CALIBRATION_ENGINE is not None:
                        _CALIBRATION_ENGINE.add_observation(raw_p, cal_binary)

                logging.info(
                    f"Evaluated opp settled: {ticker} ({row['filter_stage']}) "
                    f"-> {counterfactual_outcome} (profit={would_have_profit}¢)"
                )
            except Exception as e:
                logging.debug(f"Evaluated opp settlement check failed for {ticker}: {e}")


# ═════════════════════════════════════════════════════════════════════════════
#  Market Discovery
# ═════════════════════════════════════════════════════════════════════════════

def discover_active_windows(client: KalshiClient) -> List[Dict]:
    """
    Query Kalshi for currently open 15-minute crypto windows.

    Uses the events endpoint (GET /events) with status=open and
    with_nested_markets=true to find tradeable markets. The markets
    endpoint (GET /markets) with series_ticker only returns pre-created
    'initialized' markets on production, missing the active ones.

    Returns list of dicts with asset, event_ticker, close_time,
    seconds_to_close, and markets list.
    """
    now = datetime.datetime.now(timezone.utc)
    windows: List[Dict] = []

    for asset, series in SERIES_TICKERS.items():
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
            windows.append({
                "asset": asset,
                "event_ticker": event_ticker,
                "close_time": close_time,
                "seconds_to_close": seconds_to_close,
                "markets": mkts,
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
        self.har_estimator = HAREstimator()
        self.egarch_estimator = EGARCHEstimator()
        self.mz_tracker = MincerZarnowitzTracker()
        self.vol = VolatilityEngine(self.feed, dvol_fetcher=self.dvol_fetcher,
                                    har_estimator=self.har_estimator,
                                    egarch_estimator=self.egarch_estimator,
                                    mz_tracker=self.mz_tracker)
        self.sizer = PositionSizer()
        self.calibration = CalibrationEngine()
        global _CALIBRATION_ENGINE
        _CALIBRATION_ENGINE = self.calibration
        tg_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        tg_chat = os.environ.get("TELEGRAM_CHAT_ID", "")
        self.telegram = TelegramNotifier(tg_token, tg_chat)
        global _TELEGRAM
        _TELEGRAM = self.telegram
        self.cross_feed = CrossExchangeFeed(self.feed) if CROSS_EXCHANGE_ENABLED else None
        self.coinglass = CoinGlassFetcher()
        self.order_flow = OrderFlowEngine(
            cross_feed=self.cross_feed, coinglass=self.coinglass,
        )
        self.scanner = OpportunityScanner(
            self.client, self.state, self.feed, self.vol, self.logger,
            self.sizer, order_flow=self.order_flow,
        )
        self.executor = OrderExecutor(self.client, self.state, self.logger, main_loop=self)
        self.tracker = SettlementTracker(self.client, self.state, self.logger)
        self._shutdown = threading.Event()
        self._active_windows: List[Dict] = []
        self._last_market_refresh: float = 0.0
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

        # Start Firebase dashboard push (if configured)
        try:
            from firebase_push import FirebasePusher
            self.firebase = FirebasePusher(self)
            self.firebase.start()
        except Exception as e:
            logging.info(f"Firebase dashboard not available: {e}")
            self.firebase = None

        # Initial market scan
        self._refresh_active_windows()

        logging.info(
            f"Startup complete. Monitoring {len(ASSETS)} assets "
            f"({', '.join(ASSETS)})"
        )

    # ── Periodic Tasks ────────────────────────────────────────────────────

    def _refresh_active_windows(self):
        self._active_windows = discover_active_windows(self.client)
        self._last_market_refresh = time.time()
        logging.debug(f"Refreshed: {len(self._active_windows)} active windows")

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
                    f"\U0001f4c8 Daily ({yesterday}): {wins}W/{losses}L, PnL={total_pnl}c"
                )
        except Exception as e:
            logging.debug(f"_log_daily_summary failed: {e}")

    # ── Main Tick ─────────────────────────────────────────────────────────

    def _tick(self):
        now = time.time()

        # Refresh market list periodically
        if now - self._last_market_refresh >= MARKET_REFRESH_SECONDS:
            self._refresh_active_windows()

        # Check settlements periodically (self-throttled)
        self.tracker.tick()
        self._log_daily_summary()

        # Periodic calibration retrain check
        if self.calibration:
            self.calibration.maybe_retrain()

        # Periodic HAR-WLS refit
        if self.har_estimator:
            self.har_estimator.maybe_refit()

        # Periodic EGARCH MLE refit
        if self.egarch_estimator:
            self.egarch_estimator.maybe_refit()

        # Recompute seconds_to_close and log each window
        utc_now = datetime.datetime.now(timezone.utc)
        prices = self.feed.get_all_prices()
        for window in self._active_windows:
            seconds_to_close = (window["close_time"] - utc_now).total_seconds()
            window["seconds_to_close"] = seconds_to_close

            in_range = (
                MIN_SECONDS_BEFORE_CLOSE
                <= seconds_to_close
                <= MAX_SECONDS_BEFORE_CLOSE
            )

            asset = window["asset"]
            vol_estimate = self.vol.update(asset)

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
                    "har_model": vol_estimate.get("har_model", "fixed"),
                    "har_blend_rv": round(vol_estimate["har_blend_rv"], 8) if vol_estimate.get("har_blend_rv") is not None else None,
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
                    # HAR-IV diagnostics
                    "dvol_sq_hourly": round(vol_estimate["dvol_sq_hourly"], 10) if vol_estimate.get("dvol_sq_hourly") is not None else None,
                    "vrp": round(vol_estimate["vrp"], 10) if vol_estimate.get("vrp") is not None else None,
                    "har_iv_shadow_rv": round(vol_estimate["har_iv_shadow_rv"], 8) if vol_estimate.get("har_iv_shadow_rv") is not None else None,
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

        # Poll active executor order (maker fill check)
        self.executor.tick()

        # Run opportunity scanner (only if no active order)
        if not self.executor.has_active_order:
            candidate = self.scanner.scan(self._active_windows)
            if self.scanner._last_scan_stats:
                try:
                    self.logger.log_scan({
                        "type": "scan_summary",
                        "per_asset": self.scanner._last_scan_stats,
                        "had_candidate": candidate is not None,
                    })
                except Exception:
                    pass
            if candidate:
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

        logging.info("Entering main loop (observation mode)...")
        try:
            while not self._shutdown.is_set():
                loop_start = time.time()
                try:
                    self._tick()
                except Exception as e:
                    self._last_error = str(e)
                    self._last_error_time = time.time()
                    logging.error("Tick error", exc_info=True)
                    if _TELEGRAM:
                        _TELEGRAM.send(f"\u26a0\ufe0f Tick error: {str(e)[:200]}")
                    time.sleep(5)
                    continue

                elapsed = time.time() - loop_start
                sleep_time = max(0, SCAN_INTERVAL_SECONDS - elapsed)
                self._shutdown.wait(timeout=sleep_time)
        finally:
            self._cleanup()

    def _cleanup(self):
        logging.info("Shutting down...")
        if hasattr(self, 'har_estimator'):
            self.har_estimator._save_state()
            logging.info(
                "HAR buffer saved on shutdown: BTC=%d ETH=%d SOL=%d XRP=%d",
                len(self.har_estimator._observations["BTC"]),
                len(self.har_estimator._observations["ETH"]),
                len(self.har_estimator._observations["SOL"]),
                len(self.har_estimator._observations["XRP"]))
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
        if hasattr(self, 'firebase'):
            self.firebase.stop()
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
