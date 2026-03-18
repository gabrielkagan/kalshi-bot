"""Hourly Alt Shadow Strategies — Market-Making (A) and HAR-RV (B).

Runs two experimental shadow strategies for BTC, ETH, SOL, XRP hourly markets
in parallel with the existing EGARCH pipeline. Structurally cannot place
real orders — has no access to KalshiClient or any order submission code.

Architecture:
  - Gets called from bot.py's hourly observation gate with market state data
  - Logs all signals to its own SQLite table (hourly_alt_shadow_signals)
  - Logs to its own JSONL journal (hourly_alt_shadow_journal.jsonl)
  - Settlement resolution happens via bot.py's existing settlement loop
  - Firebase push reads from the SQLite table

Created: 2026-03-06
Purpose: 5-7 day data collection experiment
"""

import json
import math
import time
import logging
import sqlite3
import datetime
from datetime import timezone
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# ─── Configuration ────────────────────────────────────────────────────────────

# Master switch — set to False to disable all shadow strategy evaluation
HOURLY_ALT_SHADOW_ENABLED = True

# Target assets — includes BTC for comparative data (EGARCH vs HAR-RV)
# XRP excluded: EGARCH 38.7% WR (-57.6pp overconfident), MM -$72 PnL.
# Raw hourly data still collected via main EGARCH pipeline for future research.
ALT_SHADOW_ASSETS = {"BTC", "ETH", "SOL"}

# ── Strategy A: Market-Making Config ──────────────────────────────────────────

MM_ENABLED = True

# Per-asset spread buffers (cents each side of midpoint)
# Conservative starting values — will be tuned with data
MM_SPREAD_BUFFER = {
    "BTC": 2,   # 2c each side (most liquid)
    "ETH": 3,   # 3c each side
    "SOL": 4,   # 4c each side (wider — less liquid)
    "XRP": 5,   # 5c each side (widest — least liquid)
}

# Minimum spread required to participate (cents)
# If actual spread < this, skip — not enough room to capture
MM_MIN_SPREAD = {
    "BTC": 3,
    "ETH": 4,
    "SOL": 5,
    "XRP": 6,
}

# Price band limits (cents) — avoid extremes
MM_MIN_PRICE = 20   # Don't trade below 20c
MM_MAX_PRICE = 95   # Don't trade above 95c

# Time remaining filter (seconds)
MM_MIN_STC = 300    # At least 5 min before settlement
MM_MAX_STC = 3600   # At most 60 min before settlement

# Shadow bankroll (cents) — separate from real bankroll
MM_SHADOW_BANKROLL_CENTS = 100000  # $1000 notional
MM_MAX_POSITION_PCT = 0.10         # Max 10% of shadow bankroll per position
MM_KELLY_FRACTION = 0.25           # Quarter-Kelly

# ── Strategy B: HAR-RV Config ────────────────────────────────────────────────

HARRV_ENABLED = True

# Per-asset starting coefficients (literature priors, will be updated with OLS)
# HAR model: RV_forecast = beta0 + beta1*RV_1h + beta2*RV_1d + beta3*RV_1w
HARRV_PRIORS = {
    "BTC": {"beta0": 0.00005, "beta1": 0.45, "beta2": 0.35, "beta3": 0.20},
    "ETH": {"beta0": 0.0001, "beta1": 0.40, "beta2": 0.35, "beta3": 0.25},
    "SOL": {"beta0": 0.0002, "beta1": 0.35, "beta2": 0.35, "beta3": 0.30},
    "XRP": {"beta0": 0.0003, "beta1": 0.30, "beta2": 0.35, "beta3": 0.35},
}

# Per-asset temperature scaling (from research brief)
HARRV_TEMPERATURE = {
    "BTC": 1.45,   # Same as main hourly EGARCH temperature
    "ETH": 2.76,   # Research-recommended for ETH hourly
    "SOL": 4.0,    # SOL needs aggressive scaling
    "XRP": 5.0,    # XRP needs very aggressive scaling
}

# Per-asset market blend weights (model/market) — higher market weight for altcoins
HARRV_MARKET_BLEND = {
    "BTC": 0.40,   # 60% model, 40% market (BTC EGARCH is well-calibrated)
    "ETH": 0.60,   # 40% model, 60% market
    "SOL": 0.70,   # 30% model, 70% market
    "XRP": 0.80,   # 20% model, 80% market
}

# Multi-gate abstention thresholds
HARRV_MIN_EDGE = {
    "BTC": 0.003,   # 0.3% minimum edge (tighter — BTC better calibrated)
    "ETH": 0.005,   # 0.5% minimum edge
    "SOL": 0.008,   # 0.8% minimum edge
    "XRP": 0.010,   # 1.0% minimum edge
}

HARRV_MAX_EDGE = {
    "BTC": 0.040,   # 4.0% max edge (wider — BTC less prone to inversion)
    "ETH": 0.030,   # 3.0% max edge (edge inversion protection)
    "SOL": 0.025,   # 2.5% max edge
    "XRP": 0.020,   # 2.0% max edge
}

HARRV_MAX_CONFIDENCE = {
    "BTC": 0.95,   # Block predictions above 95% (BTC well-calibrated at high prob)
    "ETH": 0.92,   # Block predictions above 92%
    "SOL": 0.88,   # Block predictions above 88%
    "XRP": 0.85,   # Block predictions above 85%
}

# Price band blacklist — known catastrophic zones from historical data
HARRV_PRICE_BLACKLIST = {
    "BTC": [],            # No known dead zones for BTC hourly
    "ETH": [(80, 84)],   # 0W/24L dead zone
    "SOL": [(90, 94)],   # 38% WR catastrophic zone
    "XRP": [(70, 79)],   # 15% WR disaster zone
}

# Volatility regime gate — skip when vol spike > this multiple of trailing avg
HARRV_VOL_SPIKE_THRESHOLD = 3.0

# Time remaining filter
HARRV_MIN_STC = 300     # At least 5 min
HARRV_MAX_STC = 3600    # At most 60 min

# Shadow bankroll (cents) — separate from Strategy A and real bankroll
HARRV_SHADOW_BANKROLL_CENTS = 100000  # $1000 notional
HARRV_MAX_POSITION_PCT = 0.10
HARRV_KELLY_FRACTION = 0.25

# HAR-RV data requirements
HARRV_MIN_RETURNS_1H = 60     # At least 60 returns for hourly RV (5-sec interval = 5 min)
HARRV_MIN_RETURNS_1D = 720    # At least 720 returns for daily RV (1 hour of 5-sec)
HARRV_MIN_OLS_OBS = 50        # Minimum observations to start OLS estimation
HARRV_OLS_WINDOW = 168        # 7 days of hourly observations for rolling OLS

# Fee calculation (crypto hourly — same as main bot)
FEE_MULT_TAKER = 0.07
FEE_MULT_MAKER = 0.0  # Kalshi charges $0 on maker fills

# Journal path
SHADOW_JOURNAL_PATH = "hourly_alt_shadow_journal.jsonl"

# ─── Database Path ────────────────────────────────────────────────────────────
# Uses the same state.db as the main bot (with PRAGMA busy_timeout for safety)
DB_PATH = "state.db"


# ═════════════════════════════════════════════════════════════════════════════
#  Strategy A: Market-Making Shadow Engine
# ═════════════════════════════════════════════════════════════════════════════

class MarketMakingShadow:
    """Shadow market-making strategy for altcoin hourly markets.

    Instead of predicting direction, exploits bid-ask spread in illiquid markets.
    Generates shadow buy/sell orders around the midpoint and tracks whether they
    would have been filled based on subsequent price movements.

    This class has NO access to any order placement mechanism.
    All "orders" are hypothetical and tracked in-memory/DB only.
    """

    def __init__(self):
        # Active shadow orders: ticker -> {buy: price, sell: price, ts: time, ...}
        self._shadow_orders: Dict[str, Dict] = {}
        # Shadow bankroll tracking per asset
        self._bankroll: Dict[str, int] = {a: MM_SHADOW_BANKROLL_CENTS for a in ALT_SHADOW_ASSETS}
        # Fill tracking
        self._fills: Dict[str, List[Dict]] = {a: [] for a in ALT_SHADOW_ASSETS}
        # Rolling metrics
        self._signal_count: Dict[str, int] = {a: 0 for a in ALT_SHADOW_ASSETS}
        self._skip_count: Dict[str, int] = {a: 0 for a in ALT_SHADOW_ASSETS}

    def evaluate(self, asset: str, ticker: str, event_ticker: str,
                 best_bid: Optional[int], best_ask: Optional[int],
                 spot_price: float, threshold: float,
                 seconds_to_close: float, ob_data: Optional[Dict]) -> Optional[Dict]:
        """Evaluate a market-making opportunity for a single strike.

        Returns a signal dict if this strike is eligible, None otherwise.
        The signal dict contains shadow order details for logging.
        """
        if not MM_ENABLED or asset not in ALT_SHADOW_ASSETS:
            return None

        # Time filter
        if seconds_to_close < MM_MIN_STC or seconds_to_close > MM_MAX_STC:
            return None

        # Need both bid and ask for spread calculation
        if best_bid is None or best_ask is None:
            return None
        if best_bid <= 0 or best_ask <= 0:
            return None

        # Price band filter
        mid = (best_bid + best_ask) / 2.0
        if mid < MM_MIN_PRICE or mid > MM_MAX_PRICE:
            return None

        # Spread check
        spread = best_ask - best_bid
        min_spread = MM_MIN_SPREAD.get(asset, 5)
        if spread < min_spread:
            self._skip_count[asset] = self._skip_count.get(asset, 0) + 1
            return None

        # Generate shadow orders
        buffer = MM_SPREAD_BUFFER.get(asset, 4)
        shadow_buy_price = max(1, int(mid - buffer))
        shadow_sell_price = min(99, int(mid + buffer))

        # Position sizing (quarter-Kelly on spread capture)
        # Implied edge from spread capture: (spread - 2*fee) / 2 / 100
        est_fee = math.ceil(FEE_MULT_MAKER * 1 * (mid / 100) * (1 - mid / 100))
        implied_edge = max(0, (spread - 2 * est_fee) / 2.0 / 100.0)
        if implied_edge <= 0:
            self._skip_count[asset] = self._skip_count.get(asset, 0) + 1
            return None

        # Kelly sizing on implied edge
        bankroll = self._bankroll.get(asset, MM_SHADOW_BANKROLL_CENTS)
        kelly_f = implied_edge / (1.0 - mid / 100.0) if mid < 100 else 0
        position = int(MM_KELLY_FRACTION * kelly_f * bankroll / max(1, shadow_buy_price))
        max_position = int(bankroll * MM_MAX_POSITION_PCT / max(1, shadow_buy_price))
        position = max(1, min(position, max_position))

        self._signal_count[asset] = self._signal_count.get(asset, 0) + 1

        return {
            "strategy": "mm_shadow",
            "asset": asset,
            "ticker": ticker,
            "event_ticker": event_ticker,
            "spot_price": spot_price,
            "threshold": threshold,
            "seconds_to_close": round(seconds_to_close, 1),
            "best_bid": best_bid,
            "best_ask": best_ask,
            "spread": spread,
            "midpoint": round(mid, 1),
            "shadow_buy_price": shadow_buy_price,
            "shadow_sell_price": shadow_sell_price,
            "spread_buffer": buffer,
            "implied_edge": round(implied_edge, 6),
            "kelly_f": round(kelly_f, 4),
            "shadow_contracts": position,
            "est_fee_cents": est_fee,
            "bankroll_cents": bankroll,
        }

    def check_fills(self, ticker: str, current_ask: int, current_bid: int) -> Dict:
        """Check if shadow orders would have been filled given current prices.

        A maker buy fills when the market ask drops to or below the buy price
        (someone hits our resting bid). Once filled, stays filled permanently.

        Returns dict with buy_filled/sell_filled booleans.
        """
        order = self._shadow_orders.get(ticker)
        if not order:
            return {"buy_filled": False, "sell_filled": False}

        # Sticky fills: once filled, stays filled for the rest of the window
        buy_already = order.get("_buy_filled", False)
        sell_already = order.get("_sell_filled", False)

        if not buy_already and current_ask > 0:
            if current_ask <= order.get("shadow_buy_price", 0):
                order["_buy_filled"] = True
                order["_buy_fill_ts"] = time.time()
                buy_already = True

        if not sell_already and current_bid > 0:
            if current_bid >= order.get("shadow_sell_price", 999):
                order["_sell_filled"] = True
                order["_sell_fill_ts"] = time.time()
                sell_already = True

        return {
            "buy_filled": buy_already,
            "sell_filled": sell_already,
            "buy_price": order.get("shadow_buy_price"),
            "sell_price": order.get("shadow_sell_price"),
        }

    def record_shadow_order(self, signal: Dict):
        """Record a shadow order for fill tracking."""
        ticker = signal.get("ticker", "")
        self._shadow_orders[ticker] = {
            "shadow_buy_price": signal.get("shadow_buy_price"),
            "shadow_sell_price": signal.get("shadow_sell_price"),
            "ts": time.time(),
            "asset": signal.get("asset"),
            "event_ticker": signal.get("event_ticker"),
            "_buy_filled": False,
            "_sell_filled": False,
        }

    def cleanup_expired(self, active_tickers: set):
        """Remove shadow orders for expired tickers."""
        expired = [t for t in self._shadow_orders if t not in active_tickers]
        for t in expired:
            del self._shadow_orders[t]

    def get_metrics(self) -> Dict:
        """Return rolling metrics for dashboard."""
        return {
            "signal_count": dict(self._signal_count),
            "skip_count": dict(self._skip_count),
            "active_shadow_orders": len(self._shadow_orders),
            "bankroll": dict(self._bankroll),
        }


# ═════════════════════════════════════════════════════════════════════════════
#  Strategy B: HAR-RV Shadow Engine
# ═════════════════════════════════════════════════════════════════════════════

class HARRVShadow:
    """Shadow HAR-RV volatility model for altcoin hourly markets.

    Replaces EGARCH with HAR-RV: decomposes realized volatility into
    multi-frequency components (hourly, daily, weekly) and forecasts
    next-hour volatility per asset. Avoids EGARCH leverage-direction mismatch.

    This class has NO access to any order placement mechanism.
    """

    def __init__(self):
        # Per-asset return buffers (5-second returns)
        # We need at minimum 7 days * 24h * 720 returns/h = 120,960 returns
        # But we'll start with what's available and grow
        self._return_buffers: Dict[str, deque] = {
            a: deque(maxlen=200000) for a in ALT_SHADOW_ASSETS
        }
        # Per-asset price history (for computing returns)
        self._last_price: Dict[str, Tuple[float, float]] = {}  # asset -> (price, timestamp)

        # Per-asset RV component history (for OLS estimation)
        # Each entry: (timestamp, rv_1h, rv_1d, rv_1w, realized_rv_next_1h)
        self._rv_history: Dict[str, deque] = {
            a: deque(maxlen=HARRV_OLS_WINDOW * 2) for a in ALT_SHADOW_ASSETS
        }

        # Per-asset OLS coefficients (start with priors, update with data)
        self._coefficients: Dict[str, Dict[str, float]] = {
            a: dict(HARRV_PRIORS.get(a, HARRV_PRIORS["ETH"]))
            for a in ALT_SHADOW_ASSETS
        }
        self._ols_n_obs: Dict[str, int] = {a: 0 for a in ALT_SHADOW_ASSETS}
        self._last_ols_fit: Dict[str, float] = {a: 0.0 for a in ALT_SHADOW_ASSETS}

        # Trailing volatility for regime detection
        self._trailing_rv: Dict[str, deque] = {
            a: deque(maxlen=168) for a in ALT_SHADOW_ASSETS  # 7 days of hourly
        }

        # Metrics
        self._signal_count: Dict[str, int] = {a: 0 for a in ALT_SHADOW_ASSETS}
        self._gate_failures: Dict[str, Dict[str, int]] = {
            a: {} for a in ALT_SHADOW_ASSETS
        }
        # Shadow bankroll
        self._bankroll: Dict[str, int] = {a: HARRV_SHADOW_BANKROLL_CENTS for a in ALT_SHADOW_ASSETS}

    def ingest_price(self, asset: str, price: float, timestamp: float):
        """Ingest a price tick and compute/store the log return.

        Call this from the main loop's price feed to build return history.
        """
        if asset not in ALT_SHADOW_ASSETS:
            return

        last = self._last_price.get(asset)
        if last is not None:
            last_price, last_ts = last
            dt = timestamp - last_ts
            # Only record if roughly 5-second interval (allow 3-8s)
            if 3.0 <= dt <= 8.0 and last_price > 0 and price > 0:
                log_return = math.log(price / last_price)
                self._return_buffers[asset].append((timestamp, log_return))

        self._last_price[asset] = (price, timestamp)

    def compute_rv_components(self, asset: str) -> Optional[Dict[str, float]]:
        """Compute realized volatility at three frequencies.

        Returns dict with rv_1h, rv_1d, rv_1w or None if insufficient data.
        RV = sum of squared returns over the window.
        """
        buf = self._return_buffers.get(asset)
        if not buf:
            return None

        now = time.time()
        returns_list = list(buf)  # (timestamp, log_return) tuples

        # RV_1h: last 1 hour of returns
        cutoff_1h = now - 3600
        returns_1h = [r for ts, r in returns_list if ts >= cutoff_1h]
        if len(returns_1h) < HARRV_MIN_RETURNS_1H:
            return None
        rv_1h = sum(r ** 2 for r in returns_1h)

        # RV_1d: average hourly RV over last 24 hours
        cutoff_1d = now - 86400
        returns_1d = [r for ts, r in returns_list if ts >= cutoff_1d]
        if len(returns_1d) < HARRV_MIN_RETURNS_1D:
            # Not enough data for daily — use what we have, annualized
            rv_1d = rv_1h  # fallback: assume today = this hour
        else:
            rv_1d = sum(r ** 2 for r in returns_1d) / max(1, len(returns_1d) / len(returns_1h))

        # RV_1w: average hourly RV over last 7 days
        cutoff_1w = now - 604800
        returns_1w = [r for ts, r in returns_list if ts >= cutoff_1w]
        if len(returns_1w) < HARRV_MIN_RETURNS_1D:
            rv_1w = rv_1d  # fallback
        else:
            rv_1w = sum(r ** 2 for r in returns_1w) / max(1, len(returns_1w) / len(returns_1h))

        return {
            "rv_1h": rv_1h,
            "rv_1d": rv_1d,
            "rv_1w": rv_1w,
            "n_returns_1h": len(returns_1h),
            "n_returns_1d": len(returns_1d),
            "n_returns_1w": len(returns_1w),
        }

    def forecast_rv(self, asset: str, rv_components: Dict[str, float]) -> Optional[Dict]:
        """Forecast next-hour RV using HAR model.

        Returns dict with forecast, coefficients used, and whether OLS or priors.
        """
        coeffs = self._coefficients.get(asset)
        if not coeffs:
            return None

        rv_1h = rv_components["rv_1h"]
        rv_1d = rv_components["rv_1d"]
        rv_1w = rv_components["rv_1w"]

        forecast = (coeffs["beta0"]
                    + coeffs["beta1"] * rv_1h
                    + coeffs["beta2"] * rv_1d
                    + coeffs["beta3"] * rv_1w)

        # Floor at 1e-10 to avoid zero/negative volatility
        forecast = max(1e-10, forecast)

        # Convert RV (variance) to sigma (std dev)
        sigma = math.sqrt(forecast)

        is_ols = self._ols_n_obs.get(asset, 0) >= HARRV_MIN_OLS_OBS
        method = "ols" if is_ols else "prior"

        return {
            "rv_forecast": forecast,
            "sigma_forecast": sigma,
            "method": method,
            "coefficients": dict(coeffs),
            "n_ols_obs": self._ols_n_obs.get(asset, 0),
        }

    def compute_probability(self, spot: float, threshold: float,
                            seconds_remaining: float, sigma: float,
                            asset: str) -> Optional[Dict]:
        """Convert HAR-RV sigma into directional probability for a strike.

        Uses lognormal CDF (same approach as main bot's ProbabilityEngine).
        """
        if sigma <= 0 or seconds_remaining <= 0 or spot <= 0:
            return None

        # Scale sigma to remaining time
        # sigma is hourly (from RV), scale to remaining seconds
        time_fraction = seconds_remaining / 3600.0
        scaled_sigma = sigma * math.sqrt(time_fraction)

        if scaled_sigma <= 0:
            return None

        # Lognormal probability: P(S_T > K) = N(d2) where d2 = (ln(S/K) - 0.5*sigma^2*T) / (sigma*sqrt(T))
        # Since we already scaled sigma, d2 = (ln(S/K) - 0.5*scaled_sigma^2) / scaled_sigma
        try:
            d2 = (math.log(spot / threshold) - 0.5 * scaled_sigma ** 2) / scaled_sigma
        except (ValueError, ZeroDivisionError):
            return None

        # Standard normal CDF approximation
        raw_prob = _norm_cdf(d2)

        # Ensure probability is for the "above" side
        # If threshold > spot, prob of staying above is lower
        # The lognormal CDF P(S_T > K) = N(d2) handles this automatically

        # Apply per-asset temperature scaling
        temp = HARRV_TEMPERATURE.get(asset, 2.0)
        if temp != 1.0 and temp > 0:
            p = max(0.001, min(0.999, raw_prob))
            z = math.log(p / (1.0 - p))
            z_scaled = z / temp
            scaled_prob = 1.0 / (1.0 + math.exp(-z_scaled))
        else:
            scaled_prob = raw_prob

        return {
            "raw_prob": raw_prob,
            "scaled_prob": scaled_prob,
            "temperature": temp,
            "sigma": sigma,
            "scaled_sigma": scaled_sigma,
            "d2": d2,
            "time_fraction": time_fraction,
        }

    def apply_market_blend(self, model_prob: float, market_price_cents: int,
                           asset: str) -> float:
        """Blend model probability with market-implied probability."""
        market_prob = market_price_cents / 100.0
        blend_w = HARRV_MARKET_BLEND.get(asset, 0.60)
        return (1.0 - blend_w) * model_prob + blend_w * market_prob

    def check_gates(self, asset: str, final_prob: float, edge: float,
                    fee_adjusted_edge: float, market_price: int,
                    seconds_to_close: float, rv_components: Optional[Dict]) -> Tuple[bool, List[str]]:
        """Apply multi-gate abstention system.

        Returns (passed, list_of_failure_reasons).
        All gates must pass for a signal to be "tradeable".
        """
        failures = []

        # Gate 1: Minimum edge
        min_edge = HARRV_MIN_EDGE.get(asset, 0.005)
        if fee_adjusted_edge < min_edge:
            failures.append(f"min_edge: {fee_adjusted_edge:.4f} < {min_edge:.4f}")

        # Gate 2: Maximum edge (edge inversion protection)
        max_edge = HARRV_MAX_EDGE.get(asset, 0.030)
        if fee_adjusted_edge > max_edge:
            failures.append(f"max_edge: {fee_adjusted_edge:.4f} > {max_edge:.4f}")

        # Gate 3: Maximum confidence
        max_conf = HARRV_MAX_CONFIDENCE.get(asset, 0.92)
        if final_prob > max_conf:
            failures.append(f"max_confidence: {final_prob:.4f} > {max_conf:.4f}")

        # Gate 4: Volatility regime (spike detection)
        if rv_components:
            rv_1h = rv_components.get("rv_1h", 0)
            trailing = self._trailing_rv.get(asset)
            if trailing and len(trailing) >= 24:
                avg_rv = sum(trailing) / len(trailing)
                if avg_rv > 0 and rv_1h > HARRV_VOL_SPIKE_THRESHOLD * avg_rv:
                    failures.append(f"vol_spike: rv_1h={rv_1h:.6f} > {HARRV_VOL_SPIKE_THRESHOLD}x avg={avg_rv:.6f}")

        # Gate 5: Time remaining
        if seconds_to_close < HARRV_MIN_STC:
            failures.append(f"min_stc: {seconds_to_close:.0f}s < {HARRV_MIN_STC}s")
        if seconds_to_close > HARRV_MAX_STC:
            failures.append(f"max_stc: {seconds_to_close:.0f}s > {HARRV_MAX_STC}s")

        # Gate 6: Price band blacklist
        blacklist = HARRV_PRICE_BLACKLIST.get(asset, [])
        for low, high in blacklist:
            if low <= market_price <= high:
                failures.append(f"price_blacklist: {market_price}c in [{low},{high}]")
                break

        # Track gate failure stats
        for f in failures:
            gate_name = f.split(":")[0]
            asset_gates = self._gate_failures.get(asset, {})
            asset_gates[gate_name] = asset_gates.get(gate_name, 0) + 1
            self._gate_failures[asset] = asset_gates

        return (len(failures) == 0, failures)

    def evaluate(self, asset: str, ticker: str, event_ticker: str,
                 spot_price: float, threshold: float,
                 seconds_to_close: float, market_price: int,
                 best_bid: Optional[int] = None,
                 egarch_prob: Optional[float] = None,
                 egarch_edge: Optional[float] = None,
                 no_ask: Optional[int] = None) -> Optional[Dict]:
        """Full HAR-RV evaluation pipeline for a single strike.

        Returns a signal dict with all diagnostic fields, or None if
        insufficient data for HAR-RV computation.
        """
        if not HARRV_ENABLED or asset not in ALT_SHADOW_ASSETS:
            return None

        # Step 1: Compute RV components
        rv_components = self.compute_rv_components(asset)
        if rv_components is None:
            return None

        # Step 2: Forecast next-hour RV
        forecast = self.forecast_rv(asset, rv_components)
        if forecast is None:
            return None

        # Step 3: Compute probability
        prob_result = self.compute_probability(
            spot_price, threshold, seconds_to_close,
            forecast["sigma_forecast"], asset)
        if prob_result is None:
            return None

        # Step 4: Apply market blend
        scaled_prob = prob_result["scaled_prob"]
        final_prob = self.apply_market_blend(scaled_prob, market_price, asset)

        # Step 5: Compute edge
        edge = final_prob - market_price / 100.0
        est_fee = math.ceil(FEE_MULT_TAKER * 1 * (market_price / 100.0) * (1 - market_price / 100.0))
        fee_adjusted_edge = edge - est_fee / 100.0

        # Step 6: Check gates
        gates_passed, gate_failures = self.check_gates(
            asset, final_prob, edge, fee_adjusted_edge,
            market_price, seconds_to_close, rv_components)

        # Step 7: Position sizing (even for gated signals, for counterfactual analysis)
        bankroll = self._bankroll.get(asset, HARRV_SHADOW_BANKROLL_CENTS)
        kelly_f = 0.0
        position = 0
        if fee_adjusted_edge > 0 and market_price < 100:
            be_wr = market_price / 100.0
            kelly_f = fee_adjusted_edge / (1.0 - be_wr)
            position = int(HARRV_KELLY_FRACTION * kelly_f * bankroll / max(1, market_price))
            max_pos = int(bankroll * HARRV_MAX_POSITION_PCT / max(1, market_price))
            position = max(0, min(position, max_pos))

        self._signal_count[asset] = self._signal_count.get(asset, 0) + 1

        # Update trailing RV for regime detection
        if rv_components:
            self._trailing_rv[asset].append(rv_components["rv_1h"])

        # ── NO-side evaluation ──
        no_result = self._evaluate_no_side(
            final_prob, market_price,
            asset=asset, yes_fee_edge=fee_adjusted_edge,
            seconds_to_close=seconds_to_close, rv_components=rv_components,
            bankroll=bankroll, no_ask_actual=no_ask, best_bid=best_bid)

        return {
            "strategy": "harrv_shadow",
            "asset": asset,
            "ticker": ticker,
            "event_ticker": event_ticker,
            "spot_price": spot_price,
            "threshold": threshold,
            "seconds_to_close": round(seconds_to_close, 1),
            "market_price": market_price,
            # HAR-RV diagnostics
            "rv_1h": rv_components["rv_1h"],
            "rv_1d": rv_components["rv_1d"],
            "rv_1w": rv_components["rv_1w"],
            "n_returns_1h": rv_components["n_returns_1h"],
            "n_returns_1d": rv_components["n_returns_1d"],
            "n_returns_1w": rv_components["n_returns_1w"],
            "rv_forecast": forecast["rv_forecast"],
            "sigma_forecast": forecast["sigma_forecast"],
            "har_method": forecast["method"],
            "har_coefficients": forecast["coefficients"],
            "n_ols_obs": forecast["n_ols_obs"],
            # Probability chain
            "raw_prob": prob_result["raw_prob"],
            "scaled_prob": prob_result["scaled_prob"],
            "temperature": prob_result["temperature"],
            "final_prob": final_prob,
            "market_blend_w": HARRV_MARKET_BLEND.get(asset, 0.60),
            # Edge
            "edge": round(edge, 6),
            "fee_adjusted_edge": round(fee_adjusted_edge, 6),
            "est_fee_cents": est_fee,
            # Gates
            "gates_passed": gates_passed,
            "gate_failures": gate_failures,
            # Sizing
            "kelly_f": round(kelly_f, 4),
            "shadow_contracts": position,
            "bankroll_cents": bankroll,
            # EGARCH baseline (for counterfactual)
            "egarch_prob": egarch_prob,
            "egarch_edge": egarch_edge,
            # NO-side HAR-RV
            "no_harrv_price": no_result["no_price"],
            "no_harrv_prob": no_result["no_prob"],
            "no_harrv_edge": no_result["no_edge"],
            "no_harrv_fee_edge": no_result["no_fee_edge"],
            "no_harrv_kelly_f": no_result["no_kelly_f"],
            "no_harrv_contracts": no_result["no_contracts"],
            "no_harrv_gates_passed": no_result["no_gates_passed"],
            "no_harrv_gate_failures": no_result["no_gate_failures"],
        }

    def _evaluate_no_side(self, yes_final_prob: float, market_price: int,
                          asset: str, yes_fee_edge: float,
                          seconds_to_close: float,
                          rv_components: Optional[Dict],
                          bankroll: int,
                          no_ask_actual: Optional[int] = None,
                          best_bid: Optional[int] = None) -> Dict:
        """Mirror YES-side HAR-RV probability to compute NO-side metrics.

        Pattern follows fifteenm_shadow.py's _evaluate_no_side_approach.
        Uses actual NO ask from market NBBO when available.
        """
        # Use actual NO ask from market NBBO
        if no_ask_actual is not None and no_ask_actual > 0:
            no_price = no_ask_actual
        else:
            # No actual NO ask available — return null result
            return {"no_price": None, "no_prob": None, "no_edge": None, "no_fee_edge": None,
                    "no_kelly_f": None, "no_contracts": 0,
                    "no_gates_passed": 0, "no_gate_failures": "no_ask_unavailable"}
        no_prob = 1.0 - yes_final_prob

        # Edge
        no_edge = no_prob - no_price / 100.0
        no_fee = math.ceil(FEE_MULT_TAKER * 1 * (no_price / 100.0) * (1 - no_price / 100.0) * 100)
        no_fee_edge = no_edge - no_fee / 100.0

        # Gate checks (mirror YES-side gates with NO-side values)
        failures = []

        # Price band: 86-99 for NO price
        if not (86 <= no_price <= 99):
            failures.append(f"price_{no_price}")

        # Minimum edge (use same per-asset thresholds)
        min_edge = HARRV_MIN_EDGE.get(asset, 0.005)
        if no_fee_edge < min_edge:
            failures.append(f"min_edge: {no_fee_edge:.4f} < {min_edge:.4f}")

        # Maximum edge (edge inversion protection)
        max_edge = HARRV_MAX_EDGE.get(asset, 0.030)
        if no_fee_edge > max_edge:
            failures.append(f"max_edge: {no_fee_edge:.4f} > {max_edge:.4f}")

        # Maximum confidence
        max_conf = HARRV_MAX_CONFIDENCE.get(asset, 0.92)
        if no_prob > max_conf:
            failures.append(f"max_confidence: {no_prob:.4f} > {max_conf:.4f}")

        # Volatility regime (same as YES-side)
        if rv_components:
            rv_1h = rv_components.get("rv_1h", 0)
            trailing = self._trailing_rv.get(asset)
            if trailing and len(trailing) >= 24:
                avg_rv = sum(trailing) / len(trailing)
                if avg_rv > 0 and rv_1h > HARRV_VOL_SPIKE_THRESHOLD * avg_rv:
                    failures.append(f"vol_spike: rv_1h={rv_1h:.6f}")

        # Time remaining
        if seconds_to_close < HARRV_MIN_STC:
            failures.append(f"min_stc: {seconds_to_close:.0f}s")
        if seconds_to_close > HARRV_MAX_STC:
            failures.append(f"max_stc: {seconds_to_close:.0f}s")

        # Price band blacklist (check NO-side market price)
        blacklist = HARRV_PRICE_BLACKLIST.get(asset, [])
        for low, high in blacklist:
            if low <= no_price <= high:
                failures.append(f"price_blacklist: {no_price}c in [{low},{high}]")
                break

        no_gates_passed = 1 if len(failures) == 0 else 0
        no_gate_failures_str = ",".join(failures) if failures else None

        # Kelly sizing (even for gated signals, for counterfactual)
        no_kelly_f = 0.0
        no_contracts = 0
        if no_fee_edge > 0 and no_price > 0 and no_price < 100:
            be_wr = no_price / 100.0
            no_kelly_f = no_fee_edge / (1.0 - be_wr)
            no_contracts = int(HARRV_KELLY_FRACTION * no_kelly_f * bankroll / max(1, no_price))
            max_pos = int(bankroll * HARRV_MAX_POSITION_PCT / max(1, no_price))
            no_contracts = max(0, min(no_contracts, max_pos))

        return {
            "no_price": no_price,
            "no_prob": round(no_prob, 6),
            "no_edge": round(no_edge, 6),
            "no_fee_edge": round(no_fee_edge, 6),
            "no_kelly_f": round(no_kelly_f, 4),
            "no_contracts": no_contracts,
            "no_gates_passed": no_gates_passed,
            "no_gate_failures": no_gate_failures_str,
        }

    def get_metrics(self) -> Dict:
        """Return rolling metrics for dashboard."""
        return {
            "signal_count": dict(self._signal_count),
            "gate_failures": {a: dict(g) for a, g in self._gate_failures.items()},
            "coefficients": {a: dict(c) for a, c in self._coefficients.items()},
            "ols_obs": dict(self._ols_n_obs),
            "return_buffer_sizes": {a: len(b) for a, b in self._return_buffers.items()},
            "trailing_rv_sizes": {a: len(b) for a, b in self._trailing_rv.items()},
            "bankroll": dict(self._bankroll),
        }


# ═════════════════════════════════════════════════════════════════════════════
#  Unified Shadow Engine (orchestrates both strategies)
# ═════════════════════════════════════════════════════════════════════════════

class HourlyAltShadowEngine:
    """Orchestrates both shadow strategies and handles logging/settlement.

    This is the main entry point called from bot.py.
    It has NO access to KalshiClient or any order placement code.
    """

    def __init__(self, db_path: str = DB_PATH):
        self.mm = MarketMakingShadow()
        self.harrv = HARRVShadow()
        self._db_path = db_path
        self._db_conn: Optional[sqlite3.Connection] = None
        self._journal_path = SHADOW_JOURNAL_PATH
        self._initialized = False
        # Dedup: avoid logging the same (ticker, strategy) more than once per window
        self._seen: set = set()

    def _ensure_db(self):
        """Lazily initialize DB connection and create table if needed."""
        if self._db_conn is not None:
            return
        self._db_conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._db_conn.execute("PRAGMA journal_mode=WAL")
        self._db_conn.execute("PRAGMA busy_timeout=30000")
        self._db_conn.row_factory = sqlite3.Row
        self._create_tables()
        self._initialized = True

    def _create_tables(self):
        """Create the shadow signal tracking table."""
        self._db_conn.executescript("""
            CREATE TABLE IF NOT EXISTS hourly_alt_shadow_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy TEXT NOT NULL,           -- 'mm_shadow' or 'harrv_shadow'
                ticker TEXT NOT NULL,
                event_ticker TEXT NOT NULL,
                asset TEXT NOT NULL,
                evaluation_time TEXT NOT NULL,
                spot_price REAL,
                threshold REAL,
                seconds_to_close REAL,
                market_price INTEGER,

                -- Strategy A (MM) fields
                best_bid INTEGER,
                best_ask INTEGER,
                spread INTEGER,
                midpoint REAL,
                shadow_buy_price INTEGER,
                shadow_sell_price INTEGER,
                spread_buffer INTEGER,
                mm_implied_edge REAL,
                mm_buy_filled INTEGER DEFAULT 0,
                mm_sell_filled INTEGER DEFAULT 0,
                mm_buy_fill_time TEXT,
                mm_sell_fill_time TEXT,

                -- Strategy B (HAR-RV) fields
                rv_1h REAL,
                rv_1d REAL,
                rv_1w REAL,
                n_returns_1h INTEGER,
                rv_forecast REAL,
                sigma_forecast REAL,
                har_method TEXT,
                raw_prob REAL,
                scaled_prob REAL,
                temperature REAL,
                final_prob REAL,
                market_blend_w REAL,
                edge REAL,
                fee_adjusted_edge REAL,
                gates_passed INTEGER DEFAULT 0,
                gate_failures TEXT,

                -- Shared fields
                kelly_f REAL,
                shadow_contracts INTEGER,
                bankroll_cents INTEGER,
                est_fee_cents INTEGER,

                -- EGARCH baseline (counterfactual)
                egarch_prob REAL,
                egarch_edge REAL,

                -- Settlement
                status TEXT NOT NULL DEFAULT 'pending',
                market_result TEXT,
                shadow_pnl_cents INTEGER,
                settled_time TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_alt_shadow_ticker
                ON hourly_alt_shadow_signals(ticker);
            CREATE INDEX IF NOT EXISTS idx_alt_shadow_strategy
                ON hourly_alt_shadow_signals(strategy);
            CREATE INDEX IF NOT EXISTS idx_alt_shadow_status
                ON hourly_alt_shadow_signals(status);
            CREATE INDEX IF NOT EXISTS idx_alt_shadow_asset
                ON hourly_alt_shadow_signals(asset);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_alt_shadow_dedup
                ON hourly_alt_shadow_signals(ticker, strategy);
        """)
        self._db_conn.commit()

        # ── Migration: NO-side HAR-RV columns ──
        for col_name, col_type in [
            ("no_harrv_price", "INTEGER"),
            ("no_harrv_prob", "REAL"),
            ("no_harrv_edge", "REAL"),
            ("no_harrv_fee_edge", "REAL"),
            ("no_harrv_kelly_f", "REAL"),
            ("no_harrv_contracts", "INTEGER"),
            ("no_harrv_gates_passed", "INTEGER"),
            ("no_harrv_gate_failures", "TEXT"),
            ("no_harrv_pnl_cents", "INTEGER"),
        ]:
            try:
                self._db_conn.execute(
                    f"ALTER TABLE hourly_alt_shadow_signals ADD COLUMN {col_name} {col_type}")
            except Exception:
                pass  # Column already exists
        self._db_conn.commit()

    def ingest_price(self, asset: str, price: float, timestamp: float):
        """Pass price data to HAR-RV engine for return computation."""
        self.harrv.ingest_price(asset, price, timestamp)

    def evaluate_strike(self, asset: str, ticker: str, event_ticker: str,
                        spot_price: float, threshold: float,
                        seconds_to_close: float,
                        best_bid: Optional[int], best_ask: Optional[int],
                        market_price: int,
                        ob_data: Optional[Dict] = None,
                        egarch_prob: Optional[float] = None,
                        egarch_edge: Optional[float] = None,
                        no_ask: Optional[int] = None) -> List[Dict]:
        """Evaluate both strategies for a single strike.

        Returns list of signal dicts (0-2 entries, one per strategy).
        Called from bot.py's hourly observation gate.
        """
        if not HOURLY_ALT_SHADOW_ENABLED:
            return []
        if asset not in ALT_SHADOW_ASSETS:
            return []

        signals = []

        # Dedup check
        mm_key = (ticker, "mm_shadow")
        harrv_key = (ticker, "harrv_shadow")

        # Strategy A: Market-Making
        if mm_key not in self._seen:
            try:
                mm_signal = self.mm.evaluate(
                    asset, ticker, event_ticker,
                    best_bid, best_ask,
                    spot_price, threshold,
                    seconds_to_close, ob_data)
                if mm_signal is not None:
                    signals.append(("mm", mm_key, mm_signal))
                    self.mm.record_shadow_order(mm_signal)
            except Exception as e:
                logging.warning("MM shadow evaluate failed for %s", ticker, exc_info=True)

        # Strategy B: HAR-RV
        if harrv_key not in self._seen:
            try:
                harrv_signal = self.harrv.evaluate(
                    asset, ticker, event_ticker,
                    spot_price, threshold,
                    seconds_to_close, market_price,
                    best_bid=best_bid,
                    egarch_prob=egarch_prob,
                    egarch_edge=egarch_edge,
                    no_ask=no_ask)
                if harrv_signal is not None:
                    signals.append(("harrv", harrv_key, harrv_signal))
            except Exception as e:
                logging.warning("HAR-RV shadow evaluate failed for %s", ticker, exc_info=True)

        # Persist signals — only add to _seen after successful DB write
        result_signals = []
        for _strategy, _dedup_key, sig in signals:
            try:
                if self._log_signal(sig):
                    self._seen.add(_dedup_key)
                result_signals.append(sig)
            except Exception as e:
                logging.debug("Shadow signal logging failed: %s", e)

        return result_signals

    def _log_signal(self, signal: Dict):
        """Write signal to DB and JSONL journal."""
        self._ensure_db()
        now = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        strategy = signal.get("strategy", "unknown")

        # DB insert
        try:
            self._db_conn.execute("""
                INSERT OR REPLACE INTO hourly_alt_shadow_signals
                    (strategy, ticker, event_ticker, asset, evaluation_time,
                     spot_price, threshold, seconds_to_close, market_price,
                     best_bid, best_ask, spread, midpoint,
                     shadow_buy_price, shadow_sell_price, spread_buffer, mm_implied_edge,
                     rv_1h, rv_1d, rv_1w, n_returns_1h,
                     rv_forecast, sigma_forecast, har_method,
                     raw_prob, scaled_prob, temperature, final_prob,
                     market_blend_w, edge, fee_adjusted_edge,
                     gates_passed, gate_failures,
                     kelly_f, shadow_contracts, bankroll_cents, est_fee_cents,
                     egarch_prob, egarch_edge,
                     no_harrv_price, no_harrv_prob, no_harrv_edge, no_harrv_fee_edge,
                     no_harrv_kelly_f, no_harrv_contracts,
                     no_harrv_gates_passed, no_harrv_gate_failures)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                        ?,?,?,?,?,?,?,?)
            """, (
                strategy,
                signal.get("ticker"),
                signal.get("event_ticker"),
                signal.get("asset"),
                now,
                signal.get("spot_price"),
                signal.get("threshold"),
                signal.get("seconds_to_close"),
                signal.get("market_price") or signal.get("best_ask"),
                signal.get("best_bid"),
                signal.get("best_ask"),
                signal.get("spread"),
                signal.get("midpoint"),
                signal.get("shadow_buy_price"),
                signal.get("shadow_sell_price"),
                signal.get("spread_buffer"),
                signal.get("implied_edge"),
                signal.get("rv_1h"),
                signal.get("rv_1d"),
                signal.get("rv_1w"),
                signal.get("n_returns_1h"),
                signal.get("rv_forecast"),
                signal.get("sigma_forecast"),
                signal.get("har_method"),
                signal.get("raw_prob"),
                signal.get("scaled_prob"),
                signal.get("temperature"),
                signal.get("final_prob"),
                signal.get("market_blend_w"),
                signal.get("edge"),
                signal.get("fee_adjusted_edge"),
                1 if signal.get("gates_passed") else 0,
                json.dumps(signal.get("gate_failures")) if signal.get("gate_failures") else None,
                signal.get("kelly_f"),
                signal.get("shadow_contracts"),
                signal.get("bankroll_cents"),
                signal.get("est_fee_cents"),
                signal.get("egarch_prob"),
                signal.get("egarch_edge"),
                signal.get("no_harrv_price"),
                signal.get("no_harrv_prob"),
                signal.get("no_harrv_edge"),
                signal.get("no_harrv_fee_edge"),
                signal.get("no_harrv_kelly_f"),
                signal.get("no_harrv_contracts"),
                signal.get("no_harrv_gates_passed"),
                signal.get("no_harrv_gate_failures"),
            ))
            self._db_conn.commit()
            _db_ok = True
        except Exception as e:
            try:
                self._db_conn.rollback()
            except Exception:
                pass
            logging.warning("hourly_alt_shadow DB insert failed: %s", e)
            _db_ok = False

        # JSONL journal (backup)
        try:
            entry = {"ts": now, **signal}
            # Remove non-serializable items
            if "har_coefficients" in entry:
                entry["har_coefficients"] = dict(entry["har_coefficients"])
            with open(self._journal_path, "a") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except Exception:
            logging.warning("hourly_alt_shadow journal write failed", exc_info=True)

        return _db_ok

    def settle_signals(self, ticker: str, market_result: str):
        """Called when a market settles. Update all shadow signals for this ticker.

        market_result: 'yes' or 'no'
        """
        self._ensure_db()
        now = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        try:
            rows = self._db_conn.execute(
                "SELECT id, strategy, market_price, shadow_contracts, shadow_buy_price, "
                "shadow_sell_price, final_prob, edge, "
                "no_harrv_contracts, no_harrv_price "
                "FROM hourly_alt_shadow_signals "
                "WHERE ticker=? AND status='pending'",
                (ticker,)
            ).fetchall()

            result_yes = market_result in ("yes", "all_yes")
            result_no = market_result in ("no", "all_no")

            for row in rows:
                sig_id = row["id"]
                strategy = row["strategy"]
                contracts = row["shadow_contracts"] or 0
                price = row["market_price"] or 0

                # Compute shadow PnL (YES-side)
                no_pnl = None
                if strategy == "mm_shadow":
                    # MM PnL depends on fill status
                    pnl = self._compute_mm_pnl(row, market_result)
                else:
                    # HAR-RV PnL: standard directional using Kelly-sized contracts
                    # If contracts=0 (gate failed / no edge), strategy wouldn't trade → PnL=0
                    ct = contracts
                    fee_yes = math.ceil(FEE_MULT_TAKER * ct * (price / 100.0) * (1 - price / 100.0) * 100)
                    if result_yes:
                        pnl = ct * (100 - price) - fee_yes
                    elif result_no:
                        pnl = -(ct * price) - fee_yes
                    else:
                        pnl = 0

                    # NO-side PnL
                    no_ct = row["no_harrv_contracts"] or 0
                    if no_ct > 0:
                        # Use stored NO ask if available, fallback for old data
                        try:
                            no_price = row["no_harrv_price"] if row["no_harrv_price"] else (100 - price)
                        except (IndexError, KeyError):
                            no_price = 100 - price  # column doesn't exist in old schema
                        fee_no = math.ceil(FEE_MULT_TAKER * no_ct * (no_price / 100.0) * (1 - no_price / 100.0) * 100)
                        # NO-side wins when result is "no"
                        if result_no:
                            no_pnl = no_ct * (100 - no_price) - fee_no
                        elif result_yes:
                            no_pnl = -(no_ct * no_price) - fee_no
                        else:
                            no_pnl = 0
                    else:
                        no_pnl = 0

                self._db_conn.execute(
                    "UPDATE hourly_alt_shadow_signals SET status='settled', "
                    "market_result=?, shadow_pnl_cents=?, no_harrv_pnl_cents=?, "
                    "settled_time=? WHERE id=?",
                    (market_result, pnl, no_pnl, now, sig_id)
                )

            self._db_conn.commit()
        except Exception as e:
            try:
                self._db_conn.rollback()
            except Exception:
                pass
            logging.warning("hourly_alt_shadow settle failed for %s: %s", ticker, e)

    def _compute_mm_pnl(self, row, market_result: str) -> int:
        """Compute market-making shadow PnL based on fill status.

        Only counts PnL if the buy order would have actually filled
        (mm_buy_filled=1). Unfilled orders get PnL=0 — no phantom profits.
        """
        buy_price = row["shadow_buy_price"]
        contracts = row["shadow_contracts"] or 0
        buy_filled = row["mm_buy_filled"] if row["mm_buy_filled"] else 0

        if not buy_filled or not buy_price or contracts <= 0:
            return 0  # No fill = no position = no PnL

        # Buy filled: PnL depends on settlement
        if market_result in ("yes", "all_yes"):
            return contracts * (100 - buy_price)
        elif market_result in ("no", "all_no"):
            return -(contracts * buy_price)
        return 0

    def check_mm_fills(self, ticker: str, best_ask: int, best_bid: int):
        """Check if any MM shadow orders for this ticker would have filled.

        Called from bot.py on each scan tick with current orderbook data.
        When a fill is detected, updates the DB row with fill status and timestamp.
        """
        result = self.mm.check_fills(ticker, best_ask, best_bid)
        if not result.get("buy_filled") and not result.get("sell_filled"):
            return  # No fills to record

        # Update DB with fill status
        self._ensure_db()
        now = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        try:
            updates = []
            params = []
            if result.get("buy_filled"):
                updates.append("mm_buy_filled = MAX(mm_buy_filled, 1)")
                updates.append("mm_buy_fill_time = COALESCE(mm_buy_fill_time, ?)")
                params.append(now)
            if result.get("sell_filled"):
                updates.append("mm_sell_filled = MAX(mm_sell_filled, 1)")
                updates.append("mm_sell_fill_time = COALESCE(mm_sell_fill_time, ?)")
                params.append(now)
            if updates:
                sql = (f"UPDATE hourly_alt_shadow_signals SET {', '.join(updates)} "
                       f"WHERE ticker=? AND strategy='mm_shadow' AND status='pending'")
                params.append(ticker)
                self._db_conn.execute(sql, params)
                self._db_conn.commit()
        except Exception as e:
            try:
                self._db_conn.rollback()
            except Exception:
                pass
            logging.warning("MM fill update failed for %s: %s", ticker, e)

    def cleanup_expired(self, active_tickers: set):
        """Clean up tracking state for expired tickers."""
        self.mm.cleanup_expired(active_tickers)
        # Clean up dedup set
        self._seen = {(tk, s) for tk, s in self._seen if tk in active_tickers}

    def get_dashboard_data(self) -> Dict:
        """Return data for Firebase dashboard push."""
        self._ensure_db()
        data = {
            "enabled": HOURLY_ALT_SHADOW_ENABLED,
            "mm": {
                "enabled": MM_ENABLED,
                "metrics": self.mm.get_metrics(),
            },
            "harrv": {
                "enabled": HARRV_ENABLED,
                "metrics": self.harrv.get_metrics(),
            },
        }

        # Query settled stats
        try:
            for strategy in ("mm_shadow", "harrv_shadow"):
                rows = self._db_conn.execute(
                    "SELECT asset, COUNT(*) as n, "
                    "SUM(CASE WHEN market_result IN ('yes','all_yes') THEN 1 ELSE 0 END) as wins, "
                    "SUM(shadow_pnl_cents) as total_pnl "
                    "FROM hourly_alt_shadow_signals "
                    "WHERE strategy=? AND status='settled' "
                    "GROUP BY asset",
                    (strategy,)
                ).fetchall()

                key = "mm" if strategy == "mm_shadow" else "harrv"
                settled = {}
                for r in rows:
                    asset = r["asset"]
                    n = r["n"]
                    wins = r["wins"] or 0
                    settled[asset] = {
                        "n": n,
                        "wins": wins,
                        "wr": round(wins / max(1, n) * 100, 1),
                        "pnl_cents": r["total_pnl"] or 0,
                    }
                data[key]["settled"] = settled

            # Pending signal counts
            pending = self._db_conn.execute(
                "SELECT strategy, asset, COUNT(*) as n "
                "FROM hourly_alt_shadow_signals WHERE status='pending' "
                "GROUP BY strategy, asset"
            ).fetchall()
            pending_data = {}
            for r in pending:
                s = "mm" if r["strategy"] == "mm_shadow" else "harrv"
                if s not in pending_data:
                    pending_data[s] = {}
                pending_data[s][r["asset"]] = r["n"]
            data["pending"] = pending_data

        except Exception as e:
            logging.debug("Shadow dashboard query failed: %s", e)

        return data


# ═════════════════════════════════════════════════════════════════════════════
#  Utility Functions
# ═════════════════════════════════════════════════════════════════════════════

def _norm_cdf(x: float) -> float:
    """Standard normal CDF approximation (Abramowitz & Stegun)."""
    # Use math.erfc for better accuracy
    return 0.5 * math.erfc(-x / math.sqrt(2))


def calculate_shadow_fee(contracts: int, price_cents: int,
                         is_taker: bool = True) -> int:
    """Calculate fee for shadow PnL computation. Mirrors bot.py's calculate_fee."""
    if not is_taker:
        return 0  # Kalshi charges $0 on maker fills
    if contracts <= 0 or price_cents <= 0 or price_cents >= 100:
        return 0
    p = price_cents / 100.0
    return math.ceil(FEE_MULT_TAKER * contracts * p * (1 - p))
