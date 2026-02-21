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
import threading
import asyncio
import random
import logging
from collections import deque
from typing import Optional, Dict, List, Set, Tuple

import requests
import websockets
from scipy.stats import t as student_t
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
MIN_ENTRY_PRICE = 85              # cents
MAX_ENTRY_PRICE = 97              # cents
MAX_CONTRACTS_PER_TRADE = 5
MAX_RISK_PER_TRADE = 0.03        # 3% of balance
MIN_SECONDS_BEFORE_CLOSE = 0
MAX_SECONDS_BEFORE_CLOSE = 240
ONE_ASSET_PER_WINDOW = True

# ─── API Configuration ───────────────────────────────────────────────────────
BASE_URL = ("https://api.elections.kalshi.com" if os.environ.get("KALSHI_ENV") == "production"
            else "https://demo-api.kalshi.co")
API_PATH_PREFIX = "/trade-api/v2"
READ_RATE_LIMIT = 20              # per second
WRITE_RATE_LIMIT = 8              # per second

# ─── File Paths ──────────────────────────────────────────────────────────────
DB_PATH = "state.db"
SCAN_JOURNAL = "scan_journal.jsonl"
TRADE_JOURNAL = "trade_journal.jsonl"
SETTLEMENT_JOURNAL = "settlement_journal.jsonl"
ORDER_JOURNAL = "order_journal.jsonl"

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

# ─── Probability Engine ──────────────────────────────────────────────────────
SECONDS_PER_YEAR = 365.25 * 24 * 3600  # crypto trades 24/7
STUDENT_T_DF = 4                  # degrees of freedom for t-distribution
BETA_SLOPE = 0.85                 # logistic calibration (<1 compresses extremes)
MAX_EFFECTIVE_PROB = 0.93         # hard cap on calibrated probability
Z_SCORE_MAX = 8.0                 # refuse to trade if |z| > 8 (vol estimate wrong)
DISCREPANCY_PROB = 0.90           # model says >90% but...
DISCREPANCY_PRICE = 75            # ...market is below 75¢ → refuse

# ─── Opportunity Scanner ────────────────────────────────────────────────────
MIN_EDGE_PCT = 5.0                # model prob must exceed market by ≥5 pp
ORDERBOOK_CACHE_TTL = 5.0         # seconds to cache orderbook responses
MAX_OB_FETCHES_PER_TICK = 4       # cap API calls for orderbooks per tick
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

# ─── Strategy Timeouts ───────────────────────────────────────────────────
MAKER_PATIENT_TIMEOUT = 30.0      # seconds before re-evaluating patient maker
MAKER_AGGRESSIVE_TIMEOUT = 10.0   # seconds before re-evaluating aggressive maker


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
                logging.warning(f"Rate limited, sleeping {retry_after}s")
                time.sleep(retry_after)
                return self._request(method, path, params, json_body)
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
            "type": "limit",
        }
        if yes_price is not None:
            body["yes_price"] = yes_price
        if no_price is not None:
            body["no_price"] = no_price
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
        entry["ts"] = datetime.datetime.utcnow().isoformat() + "Z"
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
        """)
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
        now = datetime.datetime.utcnow().isoformat() + "Z"
        self._reconcile_positions(client, now)
        self._reconcile_orders(client, now)
        self.conn.commit()
        logging.info("State reconciliation complete")

    def _reconcile_positions(self, client: KalshiClient, now: str):
        api_resp = client.get_positions()
        if not api_resp or "market_positions" not in api_resp:
            logging.warning("Could not fetch positions for reconciliation")
            return

        api_tickers: Set[str] = set()
        for pos in api_resp["market_positions"]:
            ticker = pos["ticker"]
            api_tickers.add(ticker)
            position_count = pos.get("position", 0)

            if position_count == 0:
                self.conn.execute(
                    "DELETE FROM positions WHERE ticker = ?", (ticker,))
                continue

            side = "yes" if position_count > 0 else "no"
            count = abs(position_count)
            cost = pos.get("market_exposure", 0)
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
        if not api_resp or "orders" not in api_resp:
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
            price = order.get("yes_price", 0) or order.get("no_price", 0)

            self.conn.execute("""
                INSERT INTO pending_orders (order_id, client_order_id, ticker,
                    event_ticker, asset, side, action, count, price_cents,
                    status, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,'resting',?,?)
            """, (oid, order.get("client_order_id", ""), ticker,
                  event_ticker, asset, order["side"], order["action"],
                  order.get("remaining_count", 0), price,
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
        now = datetime.datetime.utcnow().isoformat() + "Z"

        pos = self.conn.execute(
            "SELECT * FROM positions WHERE ticker=?", (ticker,)
        ).fetchone()
        if not pos:
            return

        result = settlement.get("market_result", "")
        revenue = settlement.get("revenue", 0)
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

    # ── Bot Order Lifecycle ─────────────────────────────────────────────

    def insert_bot_order(self, client_order_id: str, ticker: str,
                         event_ticker: str, asset: str, side: str,
                         count: int, price_cents: int, is_taker: bool):
        """Insert a new bot-initiated order with status='pending'."""
        now = datetime.datetime.utcnow().isoformat() + "Z"
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
        now = datetime.datetime.utcnow().isoformat() + "Z"
        self.conn.execute("""
            UPDATE pending_orders SET order_id=?, status='resting', updated_at=?
            WHERE client_order_id=? AND status='pending'
        """, (order_id, now, client_order_id))
        self.conn.commit()

    def mark_order_status(self, order_id: str, status: str):
        """Update order status (filled, canceled, api_error)."""
        now = datetime.datetime.utcnow().isoformat() + "Z"
        self.conn.execute("""
            UPDATE pending_orders SET status=?, updated_at=?
            WHERE order_id=? OR client_order_id=?
        """, (status, now, order_id, order_id))
        self.conn.commit()

    def record_position_from_fill(self, ticker: str, event_ticker: str,
                                  asset: str, side: str, count: int,
                                  price_cents: int):
        """Record a new open position from a fill."""
        now = datetime.datetime.utcnow().isoformat() + "Z"
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
        now = datetime.datetime.utcnow().isoformat() + "Z"
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
#  VolatilityEngine
# ═════════════════════════════════════════════════════════════════════════════

class VolatilityEngine:
    """Realized volatility from 5-second log returns with jump detection.

    Maintains its own rolling buffer of log returns per asset (up to 15 min).
    The price buffer in CoinbaseFeed only holds 5 min of 1-second snapshots,
    but this engine accumulates 5-second returns over a longer horizon.
    """

    def __init__(self, feed: CoinbaseFeed):
        self._feed = feed
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

    def _compute(self, asset: str, now: float) -> Optional[Dict]:
        returns = self._returns[asset]
        if len(returns) < 2:
            return None

        returns_list = list(returns)

        # Compute RV for each window: sqrt(mean(r^2))
        rv_1min = self._window_rv(returns_list, VOL_WINDOW_1MIN)
        rv_5min = self._window_rv(returns_list, VOL_WINDOW_5MIN)
        rv_15min = self._window_rv(returns_list, VOL_WINDOW_15MIN)

        # Blend: 0.5 * 1min + 0.3 * 5min + 0.2 * 15min
        w1, w5, w15 = VOL_BLEND_WEIGHTS
        blended = w1 * rv_1min + w5 * rv_5min + w15 * rv_15min

        # Jump regime check
        regime = "normal"
        jump_expiry = self._jump_until.get(asset, 0)
        if now < jump_expiry:
            regime = "elevated"
            blended *= JUMP_VOL_MULTIPLIER

        return {
            "rv_1min": rv_1min,
            "rv_5min": rv_5min,
            "rv_15min": rv_15min,
            "blended_rv": blended,
            "regime": regime,
            "num_returns": len(returns),
            "jump_seconds_remaining": round(max(0, jump_expiry - now), 1),
        }

    @staticmethod
    def _window_rv(returns: List[float], window: int) -> float:
        """Realized volatility = sqrt(mean(r^2)) over the last `window` returns."""
        subset = returns[-window:] if len(returns) >= window else returns
        if not subset:
            return 0.0
        sum_sq = sum(r * r for r in subset)
        return math.sqrt(sum_sq / len(subset))


# ═════════════════════════════════════════════════════════════════════════════
#  ProbabilityEngine
# ═════════════════════════════════════════════════════════════════════════════

class ProbabilityEngine:
    """Compute win probability from spot price, strike, time, and volatility.

    Uses Student-t CDF (df=4) for fat-tailed z-score mapping, then applies
    beta calibration via logistic compression to cap at 93%.
    """

    @staticmethod
    def compute(spot: float, threshold: float, seconds_remaining: float,
                blended_rv: float,
                market_price_cents: Optional[int] = None) -> Dict:
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

        # ── Raw probability via Student-t CDF (df=4) ────────────────────
        # P(price stays above threshold) = P(move > threshold - spot)
        # = P(Z > z_score) = 1 - CDF(z_score)
        raw_prob = 1.0 - student_t.cdf(z_score, df=STUDENT_T_DF)
        result["raw_prob"] = round(raw_prob, 6)

        # ── Beta calibration: logistic compression + cap ─────────────────
        calibrated_prob = ProbabilityEngine._calibrate(raw_prob)
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
    def _calibrate(raw_prob: float) -> float:
        """Apply logistic compression then hard cap at MAX_EFFECTIVE_PROB.

        Maps raw_prob through: logit → scale by BETA_SLOPE → inverse logit → cap.
        This pulls extreme probabilities toward 0.5 and caps at 93%.
        """
        # Clamp to avoid log(0) in logit
        p = max(0.001, min(0.999, raw_prob))
        logit = math.log(p / (1.0 - p))
        scaled_logit = BETA_SLOPE * logit
        compressed = 1.0 / (1.0 + math.exp(-scaled_logit))
        return min(compressed, MAX_EFFECTIVE_PROB)


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

        # b = net odds = profit per dollar risked = (100 - price) / price
        b = (100 - price_cents) / price_cents
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
                 sizer: PositionSizer):
        self._client = client
        self._state = state
        self._feed = feed
        self._vol = vol
        self._logger = logger
        self._sizer = sizer
        # Orderbook cache: ticker -> (data, fetch_time)
        self._ob_cache: Dict[str, Tuple[Optional[Dict], float]] = {}
        # Balance cache: (balance_cents, fetch_time)
        self._balance_cache: Tuple[Optional[int], float] = (None, 0.0)

    # ── Public entry point ────────────────────────────────────────────────

    def scan(self, active_windows: List[Dict]) -> Optional[Dict]:
        """Evaluate all windows/markets, return best candidate or None."""
        now = time.time()
        ob_fetches_this_tick = 0
        candidates: List[Dict] = []

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

                # Pre-filter: compute probability without market price
                prob_result = ProbabilityEngine.compute(
                    spot, threshold, seconds_remaining, blended_rv
                )
                cal_prob = prob_result.get("calibrated_prob")
                if cal_prob is None:
                    continue

                # Skip if calibrated prob too low to ever produce an edge
                min_prob_needed = (MIN_ENTRY_PRICE + MIN_EDGE_PCT) / 100.0
                if cal_prob < min_prob_needed:
                    continue

                # Fetch orderbook (cached, rate-limited)
                ob_data, was_fresh = self._get_orderbook_cached(ticker)
                if was_fresh:
                    ob_fetches_this_tick += 1
                if ob_data is None:
                    continue

                best_ask = self._best_yes_ask_cents(ob_data)
                if best_ask is None:
                    continue

                # Filter: ask must be in entry price range
                if not (MIN_ENTRY_PRICE <= best_ask <= MAX_ENTRY_PRICE):
                    continue

                # Re-run probability with market price for sanity check
                prob_with_market = ProbabilityEngine.compute(
                    spot, threshold, seconds_remaining, blended_rv,
                    market_price_cents=best_ask
                )
                if not prob_with_market.get("tradeable"):
                    continue

                final_prob = prob_with_market["calibrated_prob"]
                z_score = prob_with_market["z_score"]
                edge = final_prob - best_ask / 100.0

                # Filter: edge must meet minimum
                if edge < MIN_EDGE_PCT / 100.0:
                    continue

                # Compute position size via Kelly criterion
                balance = self._get_balance_cached()
                if balance is None or balance <= 0:
                    continue
                sizing = self._sizer.compute(final_prob, best_ask, balance)
                if sizing["contracts"] <= 0:
                    continue

                # Compute orderbook depth for strategy engine
                ask_depth = OrderExecutor._best_ask_depth(ob_data)
                total_depth = OrderExecutor._total_ob_depth(ob_data)

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
                    "convergence_velocity": 0,  # no history at scan time
                    "edge": edge,
                }
                strategy, strategy_scores = evaluate_execution_strategy(
                    strategy_data
                )

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
                })

                if strategy == STRATEGY_WAIT:
                    continue

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
                })

                # Respect per-tick orderbook fetch cap
                if ob_fetches_this_tick >= MAX_OB_FETCHES_PER_TICK:
                    break
            if ob_fetches_this_tick >= MAX_OB_FETCHES_PER_TICK:
                break

        if not candidates:
            return None

        best = max(candidates, key=lambda c: c["edge"])
        self._logger.log_scan({
            "type": "opportunity",
            "candidates_found": len(candidates),
            "chosen_strategy": best.get("strategy"),
            **{k: v for k, v in best.items() if k not in ("strategy_scores", "ob_snapshot")},
        })
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
        orderbook = ob_data.get("orderbook", ob_data) if ob_data else None
        self._ob_cache[ticker] = (orderbook, now)
        return (orderbook, True)

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
        balance = resp.get("balance", 0)
        self._balance_cache = (balance, now)
        return balance


# ═════════════════════════════════════════════════════════════════════════════
#  OrderExecutor
# ═════════════════════════════════════════════════════════════════════════════

class OrderExecutor:
    """Execute trades using the intelligent strategy engine.

    On entry, evaluate_execution_strategy() determines the initial approach:
    MAKER_PATIENT, MAKER_AGGRESSIVE, TAKER_NOW, or PANIC_CAPTURE.

    Each tick re-evaluates with fresh orderbook data.  If conditions escalate
    (e.g. MAKER_PATIENT → TAKER_NOW because the book is converging), the
    executor upgrades in-flight without waiting for fixed time thresholds.

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
        self._entry_strategy: Optional[str] = None

    @property
    def has_active_order(self) -> bool:
        return self._active_order is not None

    # ── Public interface ──────────────────────────────────────────────────

    def execute(self, candidate: Dict) -> Optional[Dict]:
        """Execute using the strategy pre-computed by the scanner.

        The scanner already called evaluate_execution_strategy() with full
        orderbook depth data and embedded the result in the candidate dict.
        TAKER_NOW and PANIC_CAPTURE act immediately; maker strategies set up
        a resting order that tick() monitors and may upgrade.
        """
        if self._active_order is not None:
            return None
        self._ask_history.clear()

        strategy = candidate.get("strategy", STRATEGY_MAKER_PATIENT)
        scores = candidate.get("strategy_scores", {})
        self._entry_strategy = strategy

        self._logger.log_order({
            "action": "strategy_executing",
            "strategy": strategy,
            "scores": scores,
            "ticker": candidate["ticker"],
            "z_score": candidate.get("z_score"),
            "seconds_to_close": candidate.get("seconds_to_close"),
            "best_yes_ask": candidate.get("best_yes_ask"),
            "ob_snapshot": candidate.get("ob_snapshot"),
        })
        logging.info(
            f"Strategy: {strategy} for {candidate['ticker']} "
            f"(certainty={scores.get('certainty')}, ob={scores.get('orderbook')}, "
            f"urgency={scores.get('urgency')}, composite={scores.get('composite')})"
        )

        if OBSERVATION_MODE:
            logging.info(
                f"OBSERVATION MODE: Would place order for {candidate['ticker']} "
                f"at {candidate.get('best_yes_ask', '?')}¢ for "
                f"{candidate.get('position_size', '?')} contracts using {strategy}"
            )
            return None

        if strategy == STRATEGY_TAKER_NOW:
            return self._submit_taker(candidate)

        if strategy == STRATEGY_PANIC_CAPTURE:
            self._submit_panic_from_candidate(candidate)
            return None

        # MAKER_PATIENT or MAKER_AGGRESSIVE — place maker order
        self._submit_maker(candidate, aggressive=(strategy == STRATEGY_MAKER_AGGRESSIVE))
        return None

    def tick(self) -> Optional[Dict]:
        """Called each main-loop tick.  Re-evaluates execution strategy with
        fresh orderbook data and upgrades in-flight if conditions warrant.
        """
        if self._active_order is None:
            return None

        now = time.time()
        if now - self._last_poll < MAKER_POLL_INTERVAL:
            return None
        self._last_poll = now

        order = self._active_order

        # 1. Check for fill (maker or panic)
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

        # 3. Panic orders just wait for fill or expiry
        if order.get("is_panic"):
            return None

        # 4. Fetch orderbook and re-evaluate strategy
        ob_data = self._client.get_orderbook(order["ticker"], depth=5)
        best_ask = None
        ask_depth = 999
        total_depth = 999
        if ob_data:
            best_ask = OpportunityScanner._best_yes_ask_cents(ob_data)
            ask_depth = self._best_ask_depth(ob_data)
            total_depth = self._total_ob_depth(ob_data)
            if best_ask is not None:
                self._ask_history.append((now, best_ask))

        velocity = self._convergence_velocity()
        candidate = order["candidate"]

        market_data = {
            "z_score": candidate.get("z_score", 0),
            "calibrated_prob": candidate.get("calibrated_prob", 0),
            "spot": candidate.get("spot", 0),
            "threshold": candidate.get("threshold", 0),
            "seconds_to_close": remaining,
            "blended_rv": candidate.get("blended_rv", 0),
            "vol_regime": candidate.get("vol_regime", "normal"),
            "best_yes_ask": best_ask,
            "best_ask_depth": ask_depth,
            "total_ob_depth": total_depth,
            "convergence_velocity": velocity,
            "edge": candidate.get("edge", 0),
        }
        strategy, scores = evaluate_execution_strategy(market_data)

        # 5. Act on strategy upgrade
        if strategy == STRATEGY_PANIC_CAPTURE:
            logging.info(
                f"Strategy upgrade → PANIC_CAPTURE for {order['ticker']} "
                f"(composite={scores['composite']})"
            )
            self._execute_panic_capture(order)
            return None

        if strategy == STRATEGY_TAKER_NOW:
            logging.info(
                f"Strategy upgrade → TAKER_NOW for {order['ticker']} "
                f"(composite={scores['composite']})"
            )
            return self._escalate_to_taker(order, remaining, reason="strategy_taker_now")

        # 6. Timeout based on entry strategy
        timeout = (MAKER_AGGRESSIVE_TIMEOUT
                   if self._entry_strategy == STRATEGY_MAKER_AGGRESSIVE
                   else MAKER_PATIENT_TIMEOUT)
        if elapsed >= timeout:
            # Time's up for this maker — escalate to taker
            return self._escalate_to_taker(order, remaining, reason="maker_timeout")

        return None

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
            for entry in ob_data.get(side, []):
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

        order_id = resp.get("order", {}).get("order_id", client_oid)
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

        order_id = resp.get("order", {}).get("order_id", client_oid)
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
        ob_data = self._client.get_orderbook(ticker, depth=5)
        if ob_data is None:
            logging.warning(f"Escalation aborted: orderbook fetch failed for {ticker}")
            return None

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

        order_id = resp.get("order", {}).get("order_id", client_oid)
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

        order_id = resp.get("order", {}).get("order_id", client_oid)
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
        if not resp or "fills" not in resp:
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

        # Extract fill details (fall back to order values)
        fill_count = fill.get("count", order["count"])
        fill_price = fill.get("yes_price", order["price_cents"])

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

    # ── Startup ──────────────────────────────────────────────────────────

    def startup(self):
        """Initialize watermark to 24h ago, load dedup set, sweep once."""
        self._last_check_ts = int(
            (datetime.datetime.utcnow() - datetime.timedelta(hours=24)).timestamp()
        )
        self._load_processed_tickers()
        self._poll()

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
        self._last_check_ts = int(datetime.datetime.utcnow().timestamp())

        # Refresh balance after processing settlements
        if processed_any:
            balance_resp = self._client.get_balance()
            if balance_resp:
                new_balance = balance_resp.get("balance", 0)
                logging.info(
                    f"Balance after settlements: ${new_balance / 100:.2f}"
                )

    # ── Process a single settlement ──────────────────────────────────────

    def _process_settlement(self, settlement: Dict):
        """Record outcome, P&L, and log to journal."""
        ticker = settlement["ticker"]
        market_result = settlement.get("market_result", "")
        revenue = settlement.get("revenue", 0)

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


# ═════════════════════════════════════════════════════════════════════════════
#  Market Discovery
# ═════════════════════════════════════════════════════════════════════════════

def discover_active_windows(client: KalshiClient) -> List[Dict]:
    """
    Query Kalshi for currently open 15-minute crypto windows.
    Returns list of dicts with asset, event_ticker, close_time,
    seconds_to_close, and markets list.
    """
    now = datetime.datetime.utcnow()
    now_ts = int(now.timestamp())
    windows: List[Dict] = []

    for asset, series in SERIES_TICKERS.items():
        result = client.get_markets(
            series_ticker=series,
            status="open",
            min_close_ts=now_ts,
            limit=200,
        )
        if not result or "markets" not in result:
            continue

        # Group markets by event_ticker (each event = one 15-min window)
        events: Dict[str, List[Dict]] = {}
        for mkt in result["markets"]:
            et = mkt.get("event_ticker", "")
            if et not in events:
                events[et] = []
            events[et].append(mkt)

        for event_ticker, mkts in events.items():
            close_time_str = mkts[0].get("close_time", "")
            try:
                close_time = datetime.datetime.fromisoformat(
                    close_time_str.replace("Z", "+00:00")
                ).replace(tzinfo=None)
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

    return windows


# ═════════════════════════════════════════════════════════════════════════════
#  MainLoop
# ═════════════════════════════════════════════════════════════════════════════

class MainLoop:
    """Continuous observation loop. Scans active windows every second."""

    def __init__(self):
        api_key = os.environ.get("KALSHI_API_KEY", "")
        private_key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH", "")
        if not api_key or not private_key_path:
            logging.critical(
                "KALSHI_API_KEY and KALSHI_PRIVATE_KEY_PATH must be set in environment"
            )
            sys.exit(1)

        self.client = KalshiClient(api_key, private_key_path)
        self.state = StateManager()
        self.logger = Logger()
        self.feed = CoinbaseFeed()
        self.vol = VolatilityEngine(self.feed)
        self.sizer = PositionSizer()
        self.scanner = OpportunityScanner(
            self.client, self.state, self.feed, self.vol, self.logger,
            self.sizer
        )
        self.executor = OrderExecutor(self.client, self.state, self.logger)
        self.tracker = SettlementTracker(self.client, self.state, self.logger)
        self._shutdown = threading.Event()
        self._active_windows: List[Dict] = []
        self._last_market_refresh: float = 0.0

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
        balance_cents = balance_resp.get("balance", 0)
        self.sizer.starting_balance_cents = balance_cents
        logging.info(f"Connected to Kalshi. Balance: ${balance_cents / 100:.2f}")

        # Reconcile local state with API
        self.state.reconcile_with_api(self.client)

        # Check for settlements that happened while bot was down
        self.tracker.startup()

        # Start Coinbase price feed
        self.feed.start()
        logging.info("Coinbase price feed starting...")

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

    # ── Main Tick ─────────────────────────────────────────────────────────

    def _tick(self):
        now = time.time()

        # Refresh market list periodically
        if now - self._last_market_refresh >= MARKET_REFRESH_SECONDS:
            self._refresh_active_windows()

        # Check settlements periodically (self-throttled)
        self.tracker.tick()

        # Recompute seconds_to_close and log each window
        utc_now = datetime.datetime.utcnow()
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
                })
            self.logger.log_scan(scan_entry)

        # Poll active executor order (maker fill check)
        self.executor.tick()

        # Run opportunity scanner (only if no active order)
        if not self.executor.has_active_order:
            candidate = self.scanner.scan(self._active_windows)
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
                except Exception:
                    logging.error("Tick error", exc_info=True)
                    time.sleep(5)
                    continue

                elapsed = time.time() - loop_start
                sleep_time = max(0, SCAN_INTERVAL_SECONDS - elapsed)
                self._shutdown.wait(timeout=sleep_time)
        finally:
            self._cleanup()

    def _cleanup(self):
        logging.info("Shutting down...")
        self.feed.stop()
        self.state.close()
        logging.info("Bot stopped.")


# ═════════════════════════════════════════════════════════════════════════════
#  Entrypoint
# ═════════════════════════════════════════════════════════════════════════════

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)

if __name__ == "__main__":
    bot = MainLoop()
    bot.run()
