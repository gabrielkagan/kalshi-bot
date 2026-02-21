#!/usr/bin/env python3
"""Kalshi cryptocurrency prediction market trading bot."""

import os
import sys
import time
import json
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
ASSETS = ["BTC", "ETH", "SOL", "XRP"]
SERIES_TICKERS = {
    "BTC": "KXBTC",
    "ETH": "KXETH",
    "SOL": "KXSOL",
    "XRP": "KXXRP",
}
MIN_ENTRY_PRICE = 85              # cents
MAX_ENTRY_PRICE = 97              # cents
MAX_CONTRACTS_PER_TRADE = 5
MAX_RISK_PER_TRADE = 0.03        # 3% of balance
MIN_SECONDS_BEFORE_CLOSE = 15
MAX_SECONDS_BEFORE_CLOSE = 120
ONE_ASSET_PER_WINDOW = True

# ─── API Configuration ───────────────────────────────────────────────────────
BASE_URL = "https://demo-api.kalshi.co"
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
SETTLEMENT_CHECK_SECONDS = 60.0

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


# ═════════════════════════════════════════════════════════════════════════════
#  Fee Helpers
# ═════════════════════════════════════════════════════════════════════════════

def calculate_taker_fee(count: int, price_cents: int) -> int:
    """Taker fee = ceil(0.07 * C * P * (1-P)). Ceil on TOTAL, not per contract."""
    p = price_cents / 100.0
    return math.ceil(0.07 * count * p * (1.0 - p))


def calculate_maker_fee(count: int, price_cents: int) -> int:
    """Maker fee = ceil(0.0175 * C * P * (1-P)). Ceil on TOTAL, not per contract."""
    p = price_cents / 100.0
    return math.ceil(0.0175 * count * p * (1.0 - p))


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
        """'KXBTC-26FEB2114-B95000' -> 'BTC'"""
        prefix = ticker.split("-")[0]
        return prefix[2:] if prefix.startswith("KX") else prefix

    @staticmethod
    def _event_ticker_from_ticker(ticker: str) -> str:
        """'KXBTC-26FEB2114-B95000' -> 'KXBTC-26FEB2114'"""
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
        self._shutdown = threading.Event()
        self._active_windows: List[Dict] = []
        self._last_market_refresh: float = 0.0
        self._last_settlement_check: float = 0.0

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
        logging.info(f"Connected to Kalshi. Balance: ${balance_cents / 100:.2f}")

        # Reconcile local state with API
        self.state.reconcile_with_api(self.client)

        # Check for settlements that happened while bot was down
        self._check_settlements()

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

    def _check_settlements(self):
        open_positions = self.state.get_open_positions()
        if not open_positions:
            return

        min_ts = int(
            (datetime.datetime.utcnow() - datetime.timedelta(hours=24)).timestamp()
        )
        resp = self.client.get_settlements(min_ts=min_ts)
        if not resp or "settlements" not in resp:
            return

        our_tickers = {p["ticker"] for p in open_positions}
        for s in resp["settlements"]:
            if s["ticker"] in our_tickers:
                self.state.record_settlement(s)
                self.logger.log_settlement({
                    "ticker": s["ticker"],
                    "event_ticker": s.get("event_ticker", ""),
                    "result": s.get("market_result", ""),
                    "revenue_cents": s.get("revenue", 0),
                    "settled_time": s.get("settled_time", ""),
                })
                logging.info(
                    f"Settlement: {s['ticker']} -> {s.get('market_result')}"
                )

    # ── Main Tick ─────────────────────────────────────────────────────────

    def _tick(self):
        now = time.time()

        # Refresh market list periodically
        if now - self._last_market_refresh >= MARKET_REFRESH_SECONDS:
            self._refresh_active_windows()

        # Check settlements periodically
        if now - self._last_settlement_check >= SETTLEMENT_CHECK_SECONDS:
            self._check_settlements()
            self._last_settlement_check = now

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
