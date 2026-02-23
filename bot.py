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
MIN_ENTRY_PRICE = 80              # cents
MAX_ENTRY_PRICE = 99              # cents
MAX_CONTRACTS_PER_TRADE = 20
MAX_RISK_PER_TRADE = 1.00        # 100% of balance (observation mode — no real trades)
MIN_SECONDS_BEFORE_CLOSE = 0
MAX_SECONDS_BEFORE_CLOSE = 300    # start scanning 5 min before close
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
JUMP_VOL_MULTIPLIER = 2.0         # multiply vol by 2x during elevated regime
JUMP_DECAY_SECONDS = 60.0         # elevated regime lasts 60s

# ─── Deribit DVOL Integration ────────────────────────────────────────────────
DERIBIT_DVOL_URL = "https://www.deribit.com/api/v2/public/get_volatility_index_data"
DERIBIT_DVOL_CURRENCIES = {"BTC": "BTC", "ETH": "ETH"}
DVOL_FETCH_INTERVAL = 60.0        # seconds between DVOL fetches
DVOL_CACHE_TTL = 120.0            # stale after 2 min
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

# ─── Calibration Engine ─────────────────────────────────────────────────────
CALIBRATION_STATE_PATH = "calibration_state.json"
CALIBRATION_MIN_SAMPLES_PLATT = 200
CALIBRATION_MIN_SAMPLES_BETA = 500
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
MIN_EDGE_PCT = 0.25               # model prob must exceed market by ≥0.25 pp (observation mode — collect data at all edge levels)
ORDERBOOK_CACHE_TTL = 5.0         # seconds to cache orderbook responses
MAX_OB_FETCHES_PER_TICK = 6       # cap API calls for orderbooks per tick (Advanced tier)
BALANCE_CACHE_TTL = 30.0          # seconds to cache balance

# ─── Position Sizing ───────────────────────────────────────────────────────
KELLY_FRACTION = 0.25             # quarter-Kelly
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

        pos = self.conn.execute(
            "SELECT * FROM positions WHERE ticker=?", (ticker,)
        ).fetchone()
        if not pos:
            return

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
                 settled_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """, (ticker, pos["event_ticker"], pos["asset"], result,
              pos["side"], pos["count"], pos["avg_price_cents"],
              revenue, fee, pnl, now))

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
                                     calibration_method: Optional[str] = None):
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
                     raw_prob, calibration_method)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (ticker, event_ticker, asset, filter_stage, rejection_reason,
                  now, spot_price, threshold, volatility, market_price,
                  seconds_to_close, calibrated_prob, edge, ofa_adjustment,
                  "pending",
                  strategy, position_size, kelly_f, z_score,
                  vol_regime, calibrated_prob_raw,
                  breakeven_wr, expected_value, drawdown_scaler,
                  ask_depth, best_ask_source, ofa_confidence,
                  raw_prob, calibration_method))
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
                                  price_cents: int):
        """Record a new open position from a fill."""
        now = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        cost = count * price_cents
        self.conn.execute("""
            INSERT OR REPLACE INTO positions
                (ticker, event_ticker, asset, side, count,
                 avg_price_cents, total_cost_cents, opened_at, updated_at, status)
            VALUES (?,?,?,?,?,?,?,?,?,'open')
        """, (ticker, event_ticker, asset, side, count,
              price_cents, cost, now, now))
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

    def _run(self):
        while not self._stop.is_set():
            for currency_key, currency in DERIBIT_DVOL_CURRENCIES.items():
                try:
                    dvol = self._fetch_latest_dvol(currency)
                    if dvol is not None:
                        dvol_5s = dvol * DVOL_ANNUALIZED_TO_5S
                        with self._lock:
                            self._cache[currency_key] = (dvol_5s, time.time())
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

    def __init__(self, feed: CoinbaseFeed, dvol_fetcher: Optional[DeribitDVOLFetcher] = None):
        self._feed = feed
        self._dvol = dvol_fetcher
        self._returns: Dict[str, deque] = {
            a: deque(maxlen=VOL_WINDOW_15MIN) for a in ASSETS
        }
        self._last_return_time: Dict[str, float] = {}
        self._jump_until: Dict[str, float] = {}
        self._cache: Dict[str, Optional[Dict]] = {}

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

                # Check for jump against current estimate (before updating cache)
                estimate = self._compute(asset, now)
                if estimate and estimate["blended_rv"] > 0:
                    if abs(log_return) > JUMP_THRESHOLD_MULTIPLIER * estimate["blended_rv"]:
                        self._jump_until[asset] = now + JUMP_DECAY_SECONDS
                        logging.info(
                            f"Jump detected: {asset} "
                            f"return={log_return:.6f} "
                            f"rv={estimate['blended_rv']:.6f}"
                        )

                self._cache[asset] = self._compute(asset, now)
            else:
                self._cache.setdefault(asset, None)
        elif asset not in self._cache:
            self._cache[asset] = self._compute(asset, now)

        return self._cache.get(asset)

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
    def _realized_kernel(returns: List[float], window: int) -> float:
        """Realized Kernel (Barndorff-Nielsen 2008) — microstructure-noise robust.

        RK = Σ_{h=-H}^{H} k(h/(H+1)) × γ(h)
        where γ(h) is the autocovariance at lag h.
        Returns per-return scale volatility (same unit as old _window_rv).
        """
        subset = returns[-window:] if len(returns) >= window else returns
        n = len(subset)
        if n < 2:
            return 0.0

        H = math.ceil(math.sqrt(n))

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

    # ── Core computation ─────────────────────────────────────────────────

    def _compute(self, asset: str, now: float) -> Optional[Dict]:
        returns = self._returns[asset]
        if len(returns) < 2:
            return None

        returns_list = list(returns)

        # Step 1: Realized Kernel at each window
        rk_1min = self._realized_kernel(returns_list, VOL_WINDOW_1MIN)
        rk_5min = self._realized_kernel(returns_list, VOL_WINDOW_5MIN)
        rk_15min = self._realized_kernel(returns_list, VOL_WINDOW_15MIN)

        # Step 2: HAR-RV blend on kernel estimates
        w1, w5, w15 = VOL_BLEND_WEIGHTS
        continuous_rv = w1 * rk_1min + w5 * rk_5min + w15 * rk_15min

        # Step 3: Bipower variation for jump separation
        bv_1min = self._bipower_variation(returns_list, VOL_WINDOW_1MIN)
        bv_5min = self._bipower_variation(returns_list, VOL_WINDOW_5MIN)
        bv_15min = self._bipower_variation(returns_list, VOL_WINDOW_15MIN)
        bv_blended = w1 * bv_1min + w5 * bv_5min + w15 * bv_15min

        jump_var = max(0.0, continuous_rv ** 2 - bv_blended ** 2)
        rv_blended = math.sqrt(bv_blended ** 2 + jump_var)

        # Track RV-only blended for diagnostics
        rv_only_blended = rv_blended
        blended = rv_blended

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

        # Step 6: Jump regime (existing, preserved)
        regime = "normal"
        jump_expiry = self._jump_until.get(asset, 0)
        if now < jump_expiry:
            regime = "elevated"
            blended *= JUMP_VOL_MULTIPLIER

        return {
            # Original 7 fields (backward-compatible)
            "rv_1min": rk_1min,
            "rv_5min": rk_5min,
            "rv_15min": rk_15min,
            "blended_rv": blended,
            "regime": regime,
            "num_returns": len(returns),
            "jump_seconds_remaining": round(max(0, jump_expiry - now), 1),
            # New diagnostic fields
            "bv_1min": bv_1min,
            "bv_5min": bv_5min,
            "bv_15min": bv_15min,
            "jump_component": math.sqrt(jump_var) if jump_var > 0 else 0.0,
            "dvol_5s": dvol_5s,
            "iv_rv_spread": iv_rv_spread,
            "iv_rv_blend_method": iv_rv_blend_method,
            "rv_only_blended": rv_only_blended,
        }


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
        return max(0.001, min(cap, result))

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

    def maybe_retrain(self) -> bool:
        """Hourly retrain check, gated by minimum sample sizes."""
        now = time.time()
        if now - self._last_retrain < CALIBRATION_RETRAIN_INTERVAL:
            return False
        self._last_retrain = now

        n = len(self._observations)
        retrained = False

        # Try Platt first (lowest data requirement)
        if n >= CALIBRATION_MIN_SAMPLES_PLATT:
            try:
                old_brier = self.rolling_brier_score()
                self._train_platt()

                # Brier regression check
                new_brier = self._compute_brier_on_observations()
                if self._prev_brier is not None and new_brier > self._prev_brier + 0.01:
                    logging.warning(
                        "CalibrationEngine: Platt retrain REJECTED — "
                        "Brier regression %.4f > %.4f + 0.01",
                        new_brier, self._prev_brier,
                    )
                    # Revert to fallback
                    self._platt_A = BETA_SLOPE
                    self._platt_B = 0.0
                    self._platt_trained = False
                    self.active_method = "fixed_beta"
                else:
                    self._platt_trained = True
                    if self.active_method == "fixed_beta":
                        self.active_method = "platt"
                    self._prev_brier = new_brier
                    retrained = True
                    logging.info(
                        "CalibrationEngine: Platt retrained — A=%.4f, B=%.4f, "
                        "Brier=%.4f, n=%d",
                        self._platt_A, self._platt_B, new_brier, n,
                    )
            except Exception as e:
                logging.warning("CalibrationEngine: Platt training failed: %s", e)

        # Try Beta Cal (higher data requirement)
        if n >= CALIBRATION_MIN_SAMPLES_BETA:
            try:
                self._train_beta_cal()
                self._beta_trained = True
                logging.info(
                    "CalibrationEngine: Beta Cal trained — a=%.4f, b=%.4f, c=%.4f, n=%d",
                    self._beta_a, self._beta_b, self._beta_c, n,
                )
            except Exception as e:
                logging.warning("CalibrationEngine: Beta Cal training failed: %s", e)

        # Try BLR (lowest data requirement but Bayesian)
        if n >= CALIBRATION_MIN_SAMPLES_BLR:
            try:
                self._train_blr()
                self._blr_trained = True
                logging.info(
                    "CalibrationEngine: BLR trained — mu=[%.4f, %.4f], n=%d",
                    self._blr_mu[0], self._blr_mu[1], n,
                )
            except Exception as e:
                logging.warning("CalibrationEngine: BLR training failed: %s", e)

        if retrained:
            self._save_state()
        return retrained

    def load_training_data_from_db(self, state: "StateManager"):
        """Rebuild training data from evaluated + rejected opportunities on startup."""
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

            # Also load from rejected_opportunities (z-score rejections with known outcomes)
            rej_rows = state.conn.execute(
                "SELECT raw_prob, market_result FROM rejected_opportunities "
                "WHERE status='settled' AND raw_prob IS NOT NULL "
                "AND market_result IS NOT NULL"
            ).fetchall()
            for row in rej_rows:
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

    def get_diagnostics(self) -> dict:
        """Return diagnostic info for logging."""
        return {
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
        }


# ═════════════════════════════════════════════════════════════════════════════
#  PositionSizer
# ═════════════════════════════════════════════════════════════════════════════

class PositionSizer:
    """Quarter-Kelly position sizing with drawdown scaling.

    Kelly fraction:
        f = 0.25 × ((b×p − q) / b)
    where b = (100−price)/price (net odds), p = win_prob, q = 1−p.

    Contracts = floor(f × bankroll / price_in_dollars).

    Hard limits: min 1 contract (if edge exists), max MAX_CONTRACTS_PER_TRADE,
    max MAX_RISK_PER_TRADE of bankroll at risk.

    If Kelly fraction is negative (no edge), returns 0 contracts.

    Drawdown scaler: halves position below 90% of starting balance,
    quarters it below 80%.
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

        # Fee-adjusted odds: subtract taker fee from win profit, add to loss
        fee_1c = calculate_taker_fee(1, price_cents)
        # b = net odds = profit per dollar risked (fee-adjusted)
        b = (100 - price_cents - fee_1c) / (price_cents + fee_1c)
        p = win_prob
        q = 1.0 - p

        # Full Kelly edge: (b*p - q) / b
        kelly_edge = (b * p - q) / b
        if kelly_edge <= 0:
            result["kelly_f"] = round(KELLY_FRACTION * kelly_edge, 6)
            result["reason"] = "negative edge (Kelly <= 0)"
            return result

        # Quarter-Kelly fraction
        f = KELLY_FRACTION * kelly_edge
        result["kelly_f"] = round(f, 6)

        # Convert to contracts: floor(f × bankroll_dollars / price_dollars)
        bankroll_dollars = balance_cents / 100.0
        price_dollars = price_cents / 100.0
        raw_contracts = math.floor(f * bankroll_dollars / price_dollars)
        result["raw_contracts"] = raw_contracts

        if raw_contracts <= 0:
            result["reason"] = "Kelly size rounds to 0"
            return result

        # Apply drawdown scaler
        scaler = self._drawdown_scaler(balance_cents)
        result["drawdown_scaler"] = scaler
        scaled_contracts = math.floor(raw_contracts * scaler)

        # Hard limit: max contracts that fit in MAX_RISK_PER_TRADE of bankroll
        max_by_risk = int((balance_cents * MAX_RISK_PER_TRADE) / price_cents)

        if max_by_risk < 1:
            result["reason"] = "balance too small for 1 contract within risk limit"
            return result

        # Apply all caps
        contracts = min(scaled_contracts, MAX_CONTRACTS_PER_TRADE, max_by_risk)

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
                _dyn_cap = ProbabilityEngine._dynamic_cap(seconds_remaining)
                final_prob = max(0.01, min(_dyn_cap, final_prob + ofa_adjustment))

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
                                calibration_method=calibration_method)
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
                                calibration_method=calibration_method)
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
                                calibration_method=calibration_method)
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
                                calibration_method=c.get("calibration_method"))
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
                 logger: Logger):
        self._client = client
        self._state = state
        self._logger = logger
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
                        calibration_method=candidate.get("calibration_method"))
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
        self.vol = VolatilityEngine(self.feed, dvol_fetcher=self.dvol_fetcher)
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
        self.executor = OrderExecutor(self.client, self.state, self.logger)
        self.tracker = SettlementTracker(self.client, self.state, self.logger)
        self._shutdown = threading.Event()
        self._active_windows: List[Dict] = []
        self._last_market_refresh: float = 0.0
        self._last_error: Optional[str] = None
        self._last_error_time: float = 0.0
        self._start_time: float = time.time()
        self._peak_balance: float = 0.0
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
        if _TELEGRAM and self.calibration.active_method != "fixed_beta":
            _TELEGRAM.send(
                f"\U0001f9e0 Calibration: {self.calibration.active_method} trained "
                f"({len(self.calibration._observations)} obs)"
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
