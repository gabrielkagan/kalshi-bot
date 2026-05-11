"""SPX HAR-RV Shadow Strategy — Heterogeneous Autoregressive Realized Volatility.

Runs an experimental HAR-RV volatility model for SPX hourly contracts in parallel
with the existing EGARCH pipeline. Structurally cannot place real orders — has no
access to KalshiClient or any order submission code.

Architecture:
  - Accumulates its own return buffer from SPX price feed (separate from EGARCH)
  - Computes RV at three frequencies: 1h, 1d (trading day), 1w (5 trading days)
  - Forecasts next-hour RV: RV_f = β₀ + β₁·RV_1h + β₂·RV_1d + β₃·RV_1w
  - Converts RV forecast → probability via lognormal CDF
  - Applies temperature scaling + heavy market blend (market beats EGARCH per audit)
  - Multi-gate abstention system with per-gate logging
  - Logs all signals to own SQLite table (spx_harrv_shadow_signals)
  - Settlement via bot.py's existing settlement loop

Created: 2026-03-06
Purpose: 2-week data collection experiment — compare HAR-RV vs EGARCH vs market-only
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

from bot.db_writer_registry import tracked_write  # ops: db-locked RCA instrumentation 2026-05-08

# ─── Configuration ────────────────────────────────────────────────────────────

# Master switch
SPX_HARRV_SHADOW_ENABLED = True

# ── HAR-RV Model Config ──────────────────────────────────────────────────────

# Return buffer: 5-second returns during market hours
# 6.5h/day × 720 returns/h = 4680/day × 5 days = 23400/week
# Keep 10 trading days worth
RETURN_BUFFER_SIZE = 50000

# Minimum returns for each RV component
MIN_RETURNS_1H = 100      # ~8 min of 5-sec returns (conservative)
MIN_RETURNS_1D = 2000     # ~2.8 hours (partial day OK for startup)
MIN_RETURNS_1W = 10000    # ~2 trading days minimum

# SPX literature priors for HAR-RV coefficients
# Source: Corsi (2009), Andersen et al. (2007) — SPX/S&P 500 realized volatility
# These are starting values; OLS will override once we have enough data
HAR_PRIORS = {
    "beta0": 1e-7,     # intercept (small positive)
    "beta1": 0.40,     # hourly RV weight (strongest at short horizon)
    "beta2": 0.35,     # daily RV weight
    "beta3": 0.25,     # weekly RV weight (persistence)
}

# OLS estimation config
OLS_MIN_OBS = 50          # minimum observations to attempt OLS
OLS_WINDOW = 200          # rolling window for OLS estimation

# Temperature scaling
# SPX audit shows T=1.60 optimal Brier — model is overconfident
TEMPERATURE = 1.60

# Market blend weight
# Market-only baseline beats EGARCH at every SPX price tier (audit finding)
# Start with 70% market / 30% model — model gets low initial trust
MARKET_BLEND_W = 0.70

# Shadow bankroll (cents)
SHADOW_BANKROLL_CENTS = 100000  # $1000 notional

# ── Multi-Gate Abstention ─────────────────────────────────────────────────────

# Edge gates
MIN_EDGE = -1.0           # Disabled for shadow data collection (was 0.005, blocked all output)
MAX_EDGE = 0.030          # 3% edge inversion protection (high edge = model wrong)

# Confidence gate
MAX_CONFIDENCE = 0.95     # cap at 95% — model can't be this confident

# Price band gate (below 80c is catastrophic per SPX audit)
MIN_PRICE = 80
MAX_PRICE = 99

# Time-of-day gate (10am ET / 14:00-15:00 UTC has 66.7% WR — worst period)
# Gate fires for 10:00-10:59 ET (14:00-14:59 UTC during EDT, 15:00-15:59 EST)
OPENING_RUSH_GATE = True

# STC gate (short STC <600s loses per audit, only 1200s+ profitable)
MIN_STC = 600
MAX_STC = 1800

# Volatility regime gate
VOL_SPIKE_THRESHOLD = 3.0   # skip when RV_1h > 3x trailing average

# Kelly sizing
KELLY_FRACTION = 0.25        # quarter-Kelly
MAX_POSITION_PCT = 0.15      # max 15% of bankroll per trade

# Fee calculation (SPX is finance category — half of crypto)
FEE_MULT_TAKER = 0.035
FEE_MULT_MAKER = 0.0  # Kalshi charges $0 on maker fills

# Journal path
SHADOW_JOURNAL_PATH = "spx_harrv_shadow_journal.jsonl"

# Database path
DB_PATH = "state.db"

# SPX market hours (ET) — for RV window computation
# Regular hours: 9:30-16:00 ET
# We compute RV only from returns during trading hours
MARKET_OPEN_HOUR_ET = 9
MARKET_OPEN_MINUTE_ET = 30
MARKET_CLOSE_HOUR_ET = 16
MARKET_CLOSE_MINUTE_ET = 0
TRADING_SECONDS_PER_DAY = 6.5 * 3600  # 6.5 hours = 23400 seconds


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _norm_cdf(x: float) -> float:
    """Fast approximation of the standard normal CDF (Abramowitz & Stegun)."""
    if x > 8:
        return 1.0
    if x < -8:
        return 0.0
    a1, a2, a3, a4, a5 = 0.254829592, -0.284496736, 1.421413741, -1.453152027, 1.061405429
    p_const = 0.3275911
    sign = 1 if x >= 0 else -1
    x_abs = abs(x)
    t = 1.0 / (1.0 + p_const * x_abs)
    y = 1.0 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * math.exp(-x_abs * x_abs / 2.0)
    return 0.5 * (1.0 + sign * y)


def _is_et_opening_rush(ts: float) -> bool:
    """Check if timestamp falls in 10:00-10:59 ET (the worst intraday period)."""
    utc_dt = datetime.datetime.fromtimestamp(ts, tz=timezone.utc)
    # Approximate ET offset: UTC-4 (EDT Mar-Nov) or UTC-5 (EST Nov-Mar)
    month = utc_dt.month
    if 3 <= month <= 10:
        et_hour = utc_dt.hour - 4
    else:
        et_hour = utc_dt.hour - 5
    return et_hour == 10


# ═════════════════════════════════════════════════════════════════════════════
#  HAR-RV Shadow Model
# ═════════════════════════════════════════════════════════════════════════════

class SPXHARRVModel:
    """HAR-RV volatility model for SPX.

    Accumulates its own return buffer from the SPX price feed (separate from
    the EGARCH pipeline). Computes realized volatility at three frequencies
    and forecasts next-hour RV using HAR regression.

    This class has NO access to any order placement mechanism.
    """

    def __init__(self):
        # Return buffer: (timestamp, log_return) tuples
        self._returns: deque = deque(maxlen=RETURN_BUFFER_SIZE)

        # Last price for computing returns
        self._last_price: Optional[Tuple[float, float]] = None  # (price, timestamp)

        # RV observation history for OLS estimation
        # Each entry: (timestamp, rv_1h, rv_1d, rv_1w, realized_rv_next_1h)
        self._rv_history: deque = deque(maxlen=OLS_WINDOW * 2)

        # OLS coefficients (start with literature priors)
        self._coefficients: Dict[str, float] = dict(HAR_PRIORS)
        self._ols_n_obs: int = 0
        self._last_ols_fit: float = 0.0

        # Trailing hourly RV for regime detection
        self._trailing_rv: deque = deque(maxlen=50)  # ~50 trading hours

        # Metrics
        self._signal_count: int = 0
        self._gate_failures: Dict[str, int] = {}
        self._bankroll: int = SHADOW_BANKROLL_CENTS

        # State persistence — survive restarts for OLS convergence
        self._state_path = "spx_harrv_state.json"
        self._load_state()

    def _load_state(self):
        """Restore _returns, _rv_history, and _coefficients from disk."""
        try:
            with open(self._state_path, "r") as f:
                state = json.load(f)
            if "returns" in state:
                self._returns = deque(
                    [tuple(entry) for entry in state["returns"]],
                    maxlen=RETURN_BUFFER_SIZE
                )
                # Restore _last_price from last return entry for continuity
                if self._returns:
                    self._last_price = None  # will be set on next ingest_price call
            if "rv_history" in state:
                self._rv_history = deque(
                    [tuple(entry) for entry in state["rv_history"]],
                    maxlen=OLS_WINDOW * 2
                )
            if "coefficients" in state:
                self._coefficients = state["coefficients"]
            if "n_ols_obs" in state:
                self._ols_n_obs = state["n_ols_obs"]
            logging.info("SPX HAR-RV state loaded: %d returns, %d rv_history, n_ols=%d, method=%s",
                         len(self._returns), len(self._rv_history), self._ols_n_obs,
                         "ols" if self._ols_n_obs >= OLS_MIN_OBS else "prior")
        except FileNotFoundError:
            pass
        except Exception as e:
            logging.warning("SPX HAR-RV state load failed: %s", e)

    def _save_state(self):
        """Persist _returns, _rv_history, and _coefficients to disk."""
        try:
            state = {
                "returns": [list(entry) for entry in self._returns],
                "rv_history": [list(entry) for entry in self._rv_history],
                "coefficients": self._coefficients,
                "n_ols_obs": self._ols_n_obs,
                "saved_at": datetime.datetime.now(timezone.utc).isoformat(),
            }
            with open(self._state_path, "w") as f:
                json.dump(state, f)
        except Exception as e:
            logging.debug("SPX HAR-RV state save failed: %s", e)

    def ingest_price(self, price: float, timestamp: float):
        """Ingest a price tick and compute/store the log return."""
        if price <= 0:
            return

        last = self._last_price
        if last is not None:
            last_price, last_ts = last
            dt = timestamp - last_ts
            # Only record if roughly 1-5 second interval (SPX polls at 1s)
            if 0.5 <= dt <= 8.0 and last_price > 0:
                log_return = math.log(price / last_price)
                self._returns.append((timestamp, log_return))
                # Persist returns every 300 ticks (~5 min at 1/s) to survive restarts
                if len(self._returns) % 300 == 0:
                    self._save_state()

        self._last_price = (price, timestamp)

    def compute_rv_components(self) -> Optional[Dict]:
        """Compute realized volatility at three frequencies.

        Returns dict with rv_1h, rv_1d, rv_1w and component metadata,
        or None if insufficient data.

        RV windows are based on elapsed trading time, accounting for
        the fact that SPX only trades 6.5 hours/day.
        """
        if not self._returns:
            return None

        now = time.time()
        returns_list = list(self._returns)

        # RV_1h: last 1 hour of NON-ZERO returns only.
        # REST polling produces duplicate prices → log(p/p) = 0. These are noise,
        # not real zero-volatility readings. Filter them to compute RV from actual
        # price movements only. (Learned: 98% zeros from REST killed HAR-RV for
        # weeks — rv_1h=0 → sigma=0 → no signals. Mar 25-Apr 1 2026.)
        cutoff_1h = now - 3600
        _all_1h = [(ts, r) for ts, r in returns_list if ts >= cutoff_1h]
        returns_1h = [r for _, r in _all_1h if abs(r) > 1e-12]
        _total_1h = len(_all_1h)
        if len(returns_1h) < MIN_RETURNS_1H:
            if _total_1h > 0:
                logging.debug("HAR-RV: insufficient non-zero returns 1h: %d/%d (need %d)",
                              len(returns_1h), _total_1h, MIN_RETURNS_1H)
            return None
        rv_1h = sum(r ** 2 for r in returns_1h)

        # RV_1d: last trading day — filter zeros same as 1h
        cutoff_1d = now - 86400
        _all_1d = [(ts, r) for ts, r in returns_list if ts >= cutoff_1d]
        returns_1d = [r for _, r in _all_1d if abs(r) > 1e-12]
        n_1d = len(returns_1d)
        n_1h = len(returns_1h)
        rv_1d_imputed = False
        if n_1d >= MIN_RETURNS_1D:
            rv_1d = sum(r ** 2 for r in returns_1d) / max(1, n_1d / n_1h)
        else:
            rv_1d = rv_1h  # fallback
            rv_1d_imputed = True

        # RV_1w: last 5 trading days — filter zeros
        cutoff_1w = now - 5 * 86400
        _all_1w = [(ts, r) for ts, r in returns_list if ts >= cutoff_1w]
        returns_1w = [r for _, r in _all_1w if abs(r) > 1e-12]
        n_1w = len(returns_1w)
        rv_1w_imputed = False
        if n_1w >= MIN_RETURNS_1W:
            rv_1w = sum(r ** 2 for r in returns_1w) / max(1, n_1w / n_1h)
        else:
            rv_1w = rv_1d  # fallback
            rv_1w_imputed = True

        return {
            "rv_1h": rv_1h,
            "rv_1d": rv_1d,
            "rv_1w": rv_1w,
            "n_returns_1h": n_1h,
            "n_returns_1d": n_1d,
            "n_returns_1w": n_1w,
            "rv_1d_imputed": rv_1d_imputed,
            "rv_1w_imputed": rv_1w_imputed,
        }

    def forecast_rv(self, rv_components: Dict) -> Dict:
        """Forecast next-hour RV using HAR model.

        Returns dict with forecast, coefficients, and method (prior vs OLS).
        """
        rv_1h = rv_components["rv_1h"]
        rv_1d = rv_components["rv_1d"]
        rv_1w = rv_components["rv_1w"]

        c = self._coefficients
        forecast = (c["beta0"]
                    + c["beta1"] * rv_1h
                    + c["beta2"] * rv_1d
                    + c["beta3"] * rv_1w)

        # Floor at 1e-12 to avoid zero/negative
        forecast = max(1e-12, forecast)
        sigma = math.sqrt(forecast)

        is_ols = self._ols_n_obs >= OLS_MIN_OBS
        method = "ols" if is_ols else "prior"

        return {
            "rv_forecast": forecast,
            "sigma_forecast": sigma,
            "method": method,
            "coefficients": dict(c),
            "n_ols_obs": self._ols_n_obs,
        }

    def maybe_fit_ols(self):
        """Attempt to update HAR coefficients via OLS if enough history."""
        if len(self._rv_history) < OLS_MIN_OBS:
            return
        # Rate limit: refit at most every 30 minutes
        now = time.time()
        if now - self._last_ols_fit < 1800:
            return

        try:
            obs = list(self._rv_history)[-OLS_WINDOW:]
            n = len(obs)
            if n < OLS_MIN_OBS:
                return

            # OLS: Y = Xβ, β = (X'X)^{-1}X'Y
            # Y = realized rv next hour, X = [1, rv_1h, rv_1d, rv_1w]
            # Simple normal equations (4x4 system)
            X = [[1.0, o[1], o[2], o[3]] for o in obs]
            Y = [o[4] for o in obs]

            # X'X (4x4)
            k = 4
            XtX = [[sum(X[i][a] * X[i][b] for i in range(n)) for b in range(k)] for a in range(k)]
            XtY = [sum(X[i][a] * Y[i] for i in range(n)) for a in range(k)]

            # Solve via Gaussian elimination
            beta = self._solve_linear_system(XtX, XtY)
            if beta is None:
                return

            # Sanity check: coefficients should be non-negative (except intercept)
            if any(b < -0.5 for b in beta[1:]):
                logging.debug("SPX HAR-RV OLS: negative coefficient, keeping priors")
                return

            self._coefficients = {
                "beta0": beta[0],
                "beta1": beta[1],
                "beta2": beta[2],
                "beta3": beta[3],
            }
            self._ols_n_obs = n
            self._last_ols_fit = now
            logging.info("SPX HAR-RV OLS refit: n=%d β0=%.2e β1=%.4f β2=%.4f β3=%.4f",
                         n, beta[0], beta[1], beta[2], beta[3])
            self._save_state()

        except Exception as e:
            logging.debug("SPX HAR-RV OLS fit failed: %s", e)

    @staticmethod
    def _solve_linear_system(A: List[List[float]], b: List[float]) -> Optional[List[float]]:
        """Solve Ax = b via Gaussian elimination with partial pivoting."""
        n = len(b)
        # Augmented matrix
        M = [A[i][:] + [b[i]] for i in range(n)]

        for col in range(n):
            # Partial pivot
            max_row = max(range(col, n), key=lambda r: abs(M[r][col]))
            M[col], M[max_row] = M[max_row], M[col]

            if abs(M[col][col]) < 1e-20:
                return None  # singular

            for row in range(col + 1, n):
                factor = M[row][col] / M[col][col]
                for j in range(col, n + 1):
                    M[row][j] -= factor * M[col][j]

        # Back substitution
        x = [0.0] * n
        for i in range(n - 1, -1, -1):
            x[i] = (M[i][n] - sum(M[i][j] * x[j] for j in range(i + 1, n))) / M[i][i]

        return x

    def add_rv_observation(self, rv_1h: float, rv_1d: float, rv_1w: float,
                           realized_next_1h: float):
        """Record an RV observation for future OLS training."""
        self._rv_history.append((time.time(), rv_1h, rv_1d, rv_1w, realized_next_1h))
        # Save state periodically (every 10 observations)
        if len(self._rv_history) % 10 == 0:
            self._save_state()

    def compute_probability(self, spot: float, threshold: float,
                            seconds_remaining: float, sigma: float) -> Optional[Dict]:
        """Convert HAR-RV sigma into directional probability for a strike.

        Uses lognormal CDF (same approach as main bot's ProbabilityEngine).
        """
        if sigma <= 0 or seconds_remaining <= 0 or spot <= 0:
            return None

        # Scale sigma to remaining time
        time_fraction = seconds_remaining / 3600.0
        scaled_sigma = sigma * math.sqrt(time_fraction)

        if scaled_sigma <= 0:
            return None

        try:
            d2 = (math.log(spot / threshold) - 0.5 * scaled_sigma ** 2) / scaled_sigma
        except (ValueError, ZeroDivisionError):
            return None

        raw_prob = _norm_cdf(d2)

        # Temperature scaling
        if TEMPERATURE != 1.0 and TEMPERATURE > 0:
            p = max(0.001, min(0.999, raw_prob))
            z = math.log(p / (1.0 - p))
            z_scaled = z / TEMPERATURE
            scaled_prob = 1.0 / (1.0 + math.exp(-z_scaled))
        else:
            scaled_prob = raw_prob

        return {
            "raw_prob": raw_prob,
            "scaled_prob": scaled_prob,
            "temperature": TEMPERATURE,
            "d2": d2,
            "time_fraction": time_fraction,
        }

    def apply_market_blend(self, model_prob: float, market_price_cents: int) -> float:
        """Blend model probability with market-implied probability."""
        market_prob = market_price_cents / 100.0
        return (1.0 - MARKET_BLEND_W) * model_prob + MARKET_BLEND_W * market_prob

    def check_gates(self, final_prob: float, edge: float, fee_adjusted_edge: float,
                    market_price: int, seconds_to_close: float,
                    rv_components: Optional[Dict], timestamp: float) -> Tuple[bool, List[str]]:
        """Apply multi-gate abstention system.

        Returns (all_passed, list_of_failure_reasons).
        """
        failures = []

        # Gate 1: Minimum edge
        if fee_adjusted_edge < MIN_EDGE:
            failures.append(f"min_edge: {fee_adjusted_edge:.4f} < {MIN_EDGE:.4f}")

        # Gate 2: Maximum edge (inversion protection)
        if fee_adjusted_edge > MAX_EDGE:
            failures.append(f"max_edge: {fee_adjusted_edge:.4f} > {MAX_EDGE:.4f}")

        # Gate 3: Maximum confidence
        if final_prob > MAX_CONFIDENCE:
            failures.append(f"max_confidence: {final_prob:.4f} > {MAX_CONFIDENCE:.4f}")

        # Gate 4: Price band
        if market_price < MIN_PRICE:
            failures.append(f"min_price: {market_price}c < {MIN_PRICE}c")
        if market_price > MAX_PRICE:
            failures.append(f"max_price: {market_price}c > {MAX_PRICE}c")

        # Gate 5: STC range
        if seconds_to_close < MIN_STC:
            failures.append(f"min_stc: {seconds_to_close:.0f}s < {MIN_STC}s")
        if seconds_to_close > MAX_STC:
            failures.append(f"max_stc: {seconds_to_close:.0f}s > {MAX_STC}s")

        # Gate 6: Opening rush (10:00-10:59 ET)
        if OPENING_RUSH_GATE and _is_et_opening_rush(timestamp):
            failures.append("opening_rush: 10am ET window")

        # Gate 7: Vol spike detection
        if rv_components and len(self._trailing_rv) >= 10:
            rv_1h = rv_components.get("rv_1h", 0)
            avg_rv = sum(self._trailing_rv) / len(self._trailing_rv)
            if avg_rv > 0 and rv_1h > VOL_SPIKE_THRESHOLD * avg_rv:
                failures.append(f"vol_spike: rv_1h={rv_1h:.6f} > {VOL_SPIKE_THRESHOLD}x avg={avg_rv:.6f}")

        # Track gate failure stats
        for f in failures:
            gate_name = f.split(":")[0]
            self._gate_failures[gate_name] = self._gate_failures.get(gate_name, 0) + 1

        return (len(failures) == 0, failures)

    def evaluate(self, ticker: str, event_ticker: str,
                 spot_price: float, threshold: float,
                 seconds_to_close: float, market_price: int,
                 best_bid: Optional[int] = None, best_ask: Optional[int] = None,
                 egarch_prob: Optional[float] = None,
                 egarch_edge: Optional[float] = None,
                 no_ask: Optional[int] = None) -> Optional[Dict]:
        """Full HAR-RV evaluation pipeline for a single SPX strike.

        Returns a signal dict with all diagnostic fields, or None if
        insufficient data for HAR-RV computation.
        """
        # Step 1: Compute RV components
        rv_components = self.compute_rv_components()
        if rv_components is None:
            return None

        # Step 2: Forecast next-hour RV
        forecast = self.forecast_rv(rv_components)

        # Step 3: Compute probability
        prob_result = self.compute_probability(
            spot_price, threshold, seconds_to_close,
            forecast["sigma_forecast"])
        if prob_result is None:
            return None

        # Step 4: Apply market blend
        scaled_prob = prob_result["scaled_prob"]
        final_prob = self.apply_market_blend(scaled_prob, market_price)

        # Step 5: Compute edge
        market_prob = market_price / 100.0
        edge = final_prob - market_prob
        est_fee = math.ceil(FEE_MULT_TAKER * 1 * market_prob * (1 - market_prob))
        fee_adjusted_edge = edge - est_fee / 100.0

        # Market-only baseline
        mkt_only_prob = market_prob

        # Step 6: Check gates
        now = time.time()
        gates_passed, gate_failures = self.check_gates(
            final_prob, edge, fee_adjusted_edge,
            market_price, seconds_to_close, rv_components, now)

        # Step 7: Position sizing
        kelly_f = 0.0
        position = 0
        if fee_adjusted_edge > 0 and market_price < 100:
            be_wr = market_prob
            kelly_f = fee_adjusted_edge / (1.0 - be_wr)
            position = int(KELLY_FRACTION * kelly_f * self._bankroll / max(1, market_price))
            max_pos = int(self._bankroll * MAX_POSITION_PCT / max(1, market_price))
            position = max(0, min(position, max_pos))

        # ── NO-side evaluation ──
        # Use actual NO ask from market NBBO when available
        if no_ask is not None and no_ask > 0:
            no_price = no_ask
        else:
            no_price = None  # no actual NO ask available

        if no_price is not None:
            no_prob = 1.0 - final_prob
            no_market_prob = no_price / 100.0
            no_edge = no_prob - no_market_prob
            no_est_fee = math.ceil(FEE_MULT_TAKER * 1 * no_market_prob * (1 - no_market_prob))
            no_fee_adjusted_edge = no_edge - no_est_fee / 100.0

            # NO-side gates (reuse same gate logic with NO-side parameters)
            no_gate_failures = []
            # Gate 1: Minimum edge
            if no_fee_adjusted_edge < MIN_EDGE:
                no_gate_failures.append(f"min_edge: {no_fee_adjusted_edge:.4f} < {MIN_EDGE:.4f}")
            # Gate 2: Maximum edge (inversion protection)
            if no_fee_adjusted_edge > MAX_EDGE:
                no_gate_failures.append(f"max_edge: {no_fee_adjusted_edge:.4f} > {MAX_EDGE:.4f}")
            # Gate 3: Maximum confidence (NO-side confidence)
            if no_prob > MAX_CONFIDENCE:
                no_gate_failures.append(f"max_confidence: {no_prob:.4f} > {MAX_CONFIDENCE:.4f}")
            # Gate 4: Price band (applied to NO price)
            if no_price < MIN_PRICE:
                no_gate_failures.append(f"min_price: {no_price}c < {MIN_PRICE}c")
            if no_price > MAX_PRICE:
                no_gate_failures.append(f"max_price: {no_price}c > {MAX_PRICE}c")
            # Gate 5: STC range (same as YES)
            if seconds_to_close < MIN_STC:
                no_gate_failures.append(f"min_stc: {seconds_to_close:.0f}s < {MIN_STC}s")
            if seconds_to_close > MAX_STC:
                no_gate_failures.append(f"max_stc: {seconds_to_close:.0f}s > {MAX_STC}s")
            # Gate 6: Opening rush (same as YES)
            if OPENING_RUSH_GATE and _is_et_opening_rush(now):
                no_gate_failures.append("opening_rush: 10am ET window")
            # Gate 7: Vol spike (same as YES)
            if rv_components and len(self._trailing_rv) >= 10:
                rv_1h_val = rv_components.get("rv_1h", 0)
                avg_rv = sum(self._trailing_rv) / len(self._trailing_rv)
                if avg_rv > 0 and rv_1h_val > VOL_SPIKE_THRESHOLD * avg_rv:
                    no_gate_failures.append(f"vol_spike: rv_1h={rv_1h_val:.6f} > {VOL_SPIKE_THRESHOLD}x avg={avg_rv:.6f}")

            no_gates_passed = len(no_gate_failures) == 0

            # NO-side Kelly sizing
            no_kelly_f = 0.0
            no_position = 0
            if no_fee_adjusted_edge > 0 and no_price > 0 and no_price < 100:
                no_be_wr = no_market_prob
                no_kelly_f = no_fee_adjusted_edge / (1.0 - no_be_wr)
                no_position = int(KELLY_FRACTION * no_kelly_f * self._bankroll / max(1, no_price))
                no_max_pos = int(self._bankroll * MAX_POSITION_PCT / max(1, no_price))
                no_position = max(0, min(no_position, no_max_pos))
        else:
            # No actual NO ask — skip NO-side
            no_prob = 1.0 - final_prob
            no_edge = None
            no_fee_adjusted_edge = None
            no_kelly_f = 0.0
            no_position = 0
            no_gates_passed = False
            no_gate_failures = ["no_ask_unavailable"]

        self._signal_count += 1

        # Update trailing RV
        self._trailing_rv.append(rv_components["rv_1h"])

        # Try OLS refit
        self.maybe_fit_ols()

        return {
            "strategy": "spx_harrv_shadow",
            "asset": "SPX",
            "ticker": ticker,
            "event_ticker": event_ticker,
            "spot_price": spot_price,
            "threshold": threshold,
            "seconds_to_close": round(seconds_to_close, 1),
            "market_price": market_price,
            "best_bid": best_bid,
            "best_ask": best_ask,
            # HAR-RV diagnostics
            "rv_1h": rv_components["rv_1h"],
            "rv_1d": rv_components["rv_1d"],
            "rv_1w": rv_components["rv_1w"],
            "n_returns_1h": rv_components["n_returns_1h"],
            "n_returns_1d": rv_components["n_returns_1d"],
            "n_returns_1w": rv_components["n_returns_1w"],
            "rv_1d_imputed": rv_components["rv_1d_imputed"],
            "rv_1w_imputed": rv_components["rv_1w_imputed"],
            "rv_forecast": forecast["rv_forecast"],
            "sigma_forecast": forecast["sigma_forecast"],
            "har_method": forecast["method"],
            "n_ols_obs": forecast["n_ols_obs"],
            # Probability chain
            "raw_prob": prob_result["raw_prob"],
            "scaled_prob": prob_result["scaled_prob"],
            "temperature": prob_result["temperature"],
            "final_prob": final_prob,
            "market_blend_w": MARKET_BLEND_W,
            "mkt_only_prob": mkt_only_prob,
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
            "bankroll_cents": self._bankroll,
            # EGARCH baseline
            "egarch_prob": egarch_prob,
            "egarch_edge": egarch_edge,
            # NO-side evaluation
            "no_price": no_price,
            "no_prob": round(no_prob, 6),
            "no_edge": round(no_edge, 6),
            "no_fee_edge": round(no_fee_adjusted_edge, 6),
            "no_kelly_f": round(no_kelly_f, 4),
            "no_contracts": no_position,
            "no_gates_passed": no_gates_passed,
            "no_gate_failures": no_gate_failures,
        }

    def get_metrics(self) -> Dict:
        """Return rolling metrics for dashboard."""
        return {
            "signal_count": self._signal_count,
            "gate_failures": dict(self._gate_failures),
            "coefficients": dict(self._coefficients),
            "ols_obs": self._ols_n_obs,
            "return_buffer_size": len(self._returns),
            "trailing_rv_size": len(self._trailing_rv),
            "bankroll_cents": self._bankroll,
        }


# ═════════════════════════════════════════════════════════════════════════════
#  SPX HAR-RV Shadow Engine (orchestrator)
# ═════════════════════════════════════════════════════════════════════════════

class SPXHARRVShadowEngine:
    """Orchestrates the SPX HAR-RV shadow strategy and handles logging/settlement.

    This is the main entry point called from bot.py.
    It has NO access to KalshiClient or any order placement code.
    """

    def __init__(self, db_path: str = DB_PATH):
        self.model = SPXHARRVModel()
        self._db_path = db_path
        self._db_conn: Optional[sqlite3.Connection] = None
        self._journal_path = SHADOW_JOURNAL_PATH
        self._initialized = False
        # Dedup: avoid logging the same ticker more than once per window
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
            CREATE TABLE IF NOT EXISTS spx_harrv_shadow_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                event_ticker TEXT NOT NULL,
                asset TEXT NOT NULL DEFAULT 'SPX',
                evaluation_time TEXT NOT NULL,
                spot_price REAL,
                threshold REAL,
                seconds_to_close REAL,
                market_price INTEGER,
                best_bid INTEGER,
                best_ask INTEGER,

                -- HAR-RV fields
                rv_1h REAL,
                rv_1d REAL,
                rv_1w REAL,
                n_returns_1h INTEGER,
                n_returns_1d INTEGER,
                n_returns_1w INTEGER,
                rv_1d_imputed INTEGER DEFAULT 0,
                rv_1w_imputed INTEGER DEFAULT 0,
                rv_forecast REAL,
                sigma_forecast REAL,
                har_method TEXT,
                n_ols_obs INTEGER,

                -- Probability chain
                raw_prob REAL,
                scaled_prob REAL,
                temperature REAL,
                final_prob REAL,
                market_blend_w REAL,
                mkt_only_prob REAL,

                -- Edge
                edge REAL,
                fee_adjusted_edge REAL,
                est_fee_cents INTEGER,

                -- Gates
                gates_passed INTEGER DEFAULT 0,
                gate_failures TEXT,

                -- Sizing
                kelly_f REAL,
                shadow_contracts INTEGER,
                bankroll_cents INTEGER,

                -- EGARCH baseline (counterfactual)
                egarch_prob REAL,
                egarch_edge REAL,

                -- Settlement
                status TEXT NOT NULL DEFAULT 'pending',
                market_result TEXT,
                shadow_pnl_cents INTEGER,
                settled_time TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_spx_harrv_ticker
                ON spx_harrv_shadow_signals(ticker);
            CREATE INDEX IF NOT EXISTS idx_spx_harrv_status
                ON spx_harrv_shadow_signals(status);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_spx_harrv_dedup
                ON spx_harrv_shadow_signals(ticker);
        """)
        self._db_conn.commit()

        # ── Migration: add NO-side columns if missing ──
        self._migrate_no_side_columns()

    def _migrate_no_side_columns(self):
        """Add NO-side evaluation columns if they don't exist yet."""
        existing = {
            row[1] for row in
            self._db_conn.execute("PRAGMA table_info(spx_harrv_shadow_signals)").fetchall()
        }
        no_side_columns = [
            ("no_price", "INTEGER"),
            ("no_prob", "REAL"),
            ("no_edge", "REAL"),
            ("no_fee_edge", "REAL"),
            ("no_kelly_f", "REAL"),
            ("no_contracts", "INTEGER"),
            ("no_gates_passed", "INTEGER"),
            ("no_gate_failures", "TEXT"),
            ("no_pnl_cents", "INTEGER"),
        ]
        for col_name, col_type in no_side_columns:
            if col_name not in existing:
                self._db_conn.execute(
                    f"ALTER TABLE spx_harrv_shadow_signals ADD COLUMN {col_name} {col_type}"
                )
        self._db_conn.commit()

    def ingest_price(self, price: float, timestamp: float):
        """Pass SPX price data to HAR-RV model for return computation."""
        self.model.ingest_price(price, timestamp)

    def evaluate_strike(self, ticker: str, event_ticker: str,
                        spot_price: float, threshold: float,
                        seconds_to_close: float,
                        best_bid: Optional[int] = None, best_ask: Optional[int] = None,
                        market_price: Optional[int] = None,
                        egarch_prob: Optional[float] = None,
                        egarch_edge: Optional[float] = None,
                        no_ask: Optional[int] = None):
        """Evaluate a single SPX strike and log the result.

        Called from bot.py's SPX observation gate.
        """
        if not SPX_HARRV_SHADOW_ENABLED:
            return

        if market_price is None or market_price <= 0:
            return

        # Dedup within window
        key = ticker
        if key in self._seen:
            return
        # NOTE: _seen.add deferred until after successful DB write to avoid
        # permanent data loss when _log_signal fails (e.g. database is locked).

        # Run HAR-RV evaluation
        signal = self.model.evaluate(
            ticker=ticker, event_ticker=event_ticker,
            spot_price=spot_price, threshold=threshold,
            seconds_to_close=seconds_to_close,
            market_price=market_price,
            best_bid=best_bid, best_ask=best_ask,
            egarch_prob=egarch_prob, egarch_edge=egarch_edge,
            no_ask=no_ask)

        if signal is None:
            return  # Insufficient return data for HAR-RV

        # Log to DB — only mark as seen if write succeeds
        if self._log_signal(signal):
            self._seen.add(key)

        # Log to JSONL journal
        self._log_journal(signal)

    def _log_signal(self, signal: Dict):
        """Insert signal into SQLite table."""
        try:
            self._ensure_db()
            now = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

            gate_failures_str = "; ".join(signal.get("gate_failures", []))
            no_gate_failures_str = "; ".join(signal.get("no_gate_failures", []))

            self._db_conn.execute("""
                INSERT OR REPLACE INTO spx_harrv_shadow_signals
                    (ticker, event_ticker, asset, evaluation_time,
                     spot_price, threshold, seconds_to_close, market_price,
                     best_bid, best_ask,
                     rv_1h, rv_1d, rv_1w, n_returns_1h, n_returns_1d, n_returns_1w,
                     rv_1d_imputed, rv_1w_imputed,
                     rv_forecast, sigma_forecast, har_method, n_ols_obs,
                     raw_prob, scaled_prob, temperature, final_prob, market_blend_w, mkt_only_prob,
                     edge, fee_adjusted_edge, est_fee_cents,
                     gates_passed, gate_failures,
                     kelly_f, shadow_contracts, bankroll_cents,
                     egarch_prob, egarch_edge,
                     no_price, no_prob, no_edge, no_fee_edge,
                     no_kelly_f, no_contracts,
                     no_gates_passed, no_gate_failures,
                     status)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                signal["ticker"], signal["event_ticker"], "SPX", now,
                signal["spot_price"], signal["threshold"],
                signal["seconds_to_close"], signal["market_price"],
                signal.get("best_bid"), signal.get("best_ask"),
                signal["rv_1h"], signal["rv_1d"], signal["rv_1w"],
                signal["n_returns_1h"], signal["n_returns_1d"], signal["n_returns_1w"],
                int(signal.get("rv_1d_imputed", False)),
                int(signal.get("rv_1w_imputed", False)),
                signal["rv_forecast"], signal["sigma_forecast"],
                signal["har_method"], signal["n_ols_obs"],
                signal["raw_prob"], signal["scaled_prob"],
                signal["temperature"], signal["final_prob"],
                signal["market_blend_w"], signal.get("mkt_only_prob"),
                signal["edge"], signal["fee_adjusted_edge"], signal["est_fee_cents"],
                int(signal.get("gates_passed", False)),
                gate_failures_str,
                signal["kelly_f"], signal["shadow_contracts"], signal["bankroll_cents"],
                signal.get("egarch_prob"), signal.get("egarch_edge"),
                signal.get("no_price"),
                signal.get("no_prob"), signal.get("no_edge"), signal.get("no_fee_edge"),
                signal.get("no_kelly_f"), signal.get("no_contracts"),
                int(signal.get("no_gates_passed", False)),
                no_gate_failures_str,
                "pending",
            ))
            self._db_conn.commit()
            return True
        except Exception as e:
            try:
                self._db_conn.rollback()
            except Exception:
                pass
            logging.warning("spx_harrv_shadow DB insert failed: %s", e)
            return False

    def _log_journal(self, signal: Dict):
        """Append signal to JSONL journal as backup."""
        try:
            entry = dict(signal)
            entry["logged_at"] = datetime.datetime.now(timezone.utc).isoformat()
            # Convert non-serializable types
            if "gate_failures" in entry and isinstance(entry["gate_failures"], list):
                entry["gate_failures"] = "; ".join(entry["gate_failures"])
            if "no_gate_failures" in entry and isinstance(entry["no_gate_failures"], list):
                entry["no_gate_failures"] = "; ".join(entry["no_gate_failures"])
            with open(self._journal_path, "a") as f:
                f.write(json.dumps(entry, default=str) + "\n")
        except Exception:
            pass

    def settle_signals(self, ticker: str, market_result: str):
        """Called when a market settles. Update shadow signals for this ticker."""
        self._ensure_db()
        now = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        try:
            rows = self._db_conn.execute(
                "SELECT id, market_price, shadow_contracts, final_prob, no_contracts, no_price "
                "FROM spx_harrv_shadow_signals "
                "WHERE ticker=? AND status='pending'",
                (ticker,)
            ).fetchall()

            # ops: db-locked RCA 2026-05-08 — wrap the per-row UPDATE loop +
            # final commit. If `rows` is large this holds the writer lock for
            # the duration; the registry surfaces it on contention.
            with tracked_write("spx_harrv_shadow", f"settle_signals_n={len(rows)}"):
                for row in rows:
                    sig_id = row["id"]
                    contracts = row["shadow_contracts"] or 0
                    price = row["market_price"] or 0
                    no_ct_raw = row["no_contracts"] or 0

                    # YES-side PnL: use actual contracts if gated, otherwise 1-contract counterfactual
                    ct = contracts if contracts > 0 else 1
                    if market_result in ("yes", "all_yes"):
                        pnl = ct * (100 - price)
                    elif market_result in ("no", "all_no"):
                        pnl = -(ct * price)
                    else:
                        pnl = 0

                    # NO-side PnL: use stored NO ask if available, fallback for old data
                    no_price = row["no_price"] if row["no_price"] else (100 - price)
                    no_ct = no_ct_raw if no_ct_raw > 0 else 1
                    if market_result in ("no", "all_no"):
                        # NO wins: profit = (100 - no_price) per contract
                        no_pnl = no_ct * (100 - no_price)
                    elif market_result in ("yes", "all_yes"):
                        # NO loses: loss = no_price per contract
                        no_pnl = -(no_ct * no_price)
                    else:
                        no_pnl = 0

                    self._db_conn.execute(
                        "UPDATE spx_harrv_shadow_signals SET status='settled', "
                        "market_result=?, shadow_pnl_cents=?, no_pnl_cents=?, settled_time=? WHERE id=?",
                        (market_result, pnl, no_pnl, now, sig_id)
                    )

                self._db_conn.commit()
        except Exception as e:
            try:
                self._db_conn.rollback()
            except Exception:
                pass
            logging.warning("spx_harrv_shadow settle failed for %s: %s", ticker, e)

    def cleanup_expired(self, active_tickers: set):
        """Remove dedup entries for expired tickers."""
        expired = [t for t in self._seen if t not in active_tickers]
        for t in expired:
            self._seen.discard(t)

    def get_dashboard_data(self) -> Dict:
        """Return data for Firebase dashboard push."""
        self._ensure_db()

        metrics = self.model.get_metrics()

        # Query settled stats
        settled = {}
        try:
            rows = self._db_conn.execute("""
                SELECT COUNT(*) AS n,
                       SUM(CASE WHEN market_result IN ('yes','all_yes') THEN 1 ELSE 0 END) AS wins,
                       SUM(shadow_pnl_cents) AS total_pnl
                FROM spx_harrv_shadow_signals WHERE status='settled'
            """).fetchone()
            if rows and rows["n"] > 0:
                settled = {
                    "n": rows["n"],
                    "wins": rows["wins"] or 0,
                    "wr": round((rows["wins"] or 0) / rows["n"] * 100, 1),
                    "pnl_cents": rows["total_pnl"] or 0,
                }
        except Exception:
            pass

        pending = 0
        try:
            row = self._db_conn.execute(
                "SELECT COUNT(*) AS n FROM spx_harrv_shadow_signals WHERE status='pending'"
            ).fetchone()
            pending = row["n"] if row else 0
        except Exception:
            pass

        return {
            "enabled": SPX_HARRV_SHADOW_ENABLED,
            "metrics": metrics,
            "settled": settled,
            "pending": pending,
            "config": {
                "temperature": TEMPERATURE,
                "market_blend_w": MARKET_BLEND_W,
                "min_price": MIN_PRICE,
                "min_stc": MIN_STC,
                "max_stc": MAX_STC,
                "min_edge": MIN_EDGE,
                "max_edge": MAX_EDGE,
                "opening_rush_gate": OPENING_RUSH_GATE,
            },
        }
