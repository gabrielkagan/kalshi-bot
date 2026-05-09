"""CrossExchangeFeed — Binance/Kraken/Bybit WS feeds for lead/lag detection.

Extracted from bot/_impl.py in Sprint 4 Bit 4.5a (2026-05-08). Daemon
thread that runs three WebSocket connections in a single asyncio event
loop, recording 1-second snapshots that compare each exchange's spot
price against Coinbase. The ``get_lead_lag`` getter returns a consensus
analysis (premium/discount counts, max premium/discount, direction).

Binance is geo-blocked on US-VPS deploys (HTTP 451). When
``BINANCE_FEED_ENABLED=0`` (the default), the Binance task is skipped
and ``CROSS_EXCHANGE_CONSENSUS_MIN`` is auto-lowered to 2 so Kraken+Bybit
can still trip consensus.

Imports are deliberate: stdlib + ``websockets`` + ``bot.constants``
(9 explicit names) + ``ASSETS`` from ``config.py`` + sibling
``CoinbaseFeed`` for the constructor type hint and ``.get_price()`` calls.
Does NOT import ``bot._impl`` (would create a circular import).
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import threading
import time
from collections import deque
from typing import Dict, List, Optional

import websockets

from bot.constants import (
    BINANCE_FEED_ENABLED,
    BINANCE_WS_URL,
    BYBIT_WS_URL,
    CROSS_EXCHANGE_BUFFER_SIZE,
    CROSS_EXCHANGE_CONSENSUS_MIN,
    CROSS_EXCHANGE_LEAD_THRESHOLD,
    CROSS_EXCHANGE_STALE_SECONDS,
    CROSS_EXCHANGE_SYMBOLS,
    KRAKEN_WS_URL,
)
from bot.feeds.coinbase import CoinbaseFeed
from config import ASSETS


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
        # R-bleed-1: BINANCE_FEED_ENABLED defaults OFF. US-VPS deploys are
        # geoblocked from stream.binance.com (HTTP 451) so the reconnect
        # loop fires every ~70s for the lifetime of the process. Coinbase +
        # Kraken still feed BTC for cross-asset spillover features.
        _tasks = [self._ws_kraken(), self._ws_bybit(), self._snapshot_loop()]
        if BINANCE_FEED_ENABLED:
            _tasks.insert(0, self._ws_binance())
        else:
            # R1-H1: lowered CONSENSUS_MIN to 2 to keep OFA consensus
            # signal alive on Kraken+Bybit. Make this auditable in logs.
            logging.info(
                "Binance feed DISABLED via BINANCE_FEED_ENABLED=0 "
                "(geoblocked on US-VPS deploys); CROSS_EXCHANGE_CONSENSUS_MIN "
                "auto-lowered to %d (Kraken+Bybit only)",
                CROSS_EXCHANGE_CONSENSUS_MIN)
        await asyncio.gather(*_tasks)

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
