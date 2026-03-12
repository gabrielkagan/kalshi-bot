"""S&P 500 hourly market engine — price feed, volatility model, market hours."""

import os
import json
import math
import time
import logging
import datetime
import threading
from datetime import timezone
from collections import deque
from typing import Dict, List, Optional

import requests

# ─── Configuration ────────────────────────────────────────────────────────────

SPX_SERIES_TICKER = "KXINXU"
SPX_ASSET = "SPX"

# Polygon.io WebSocket
POLYGON_WS_URL = "wss://socket.polygon.io/indices"
POLYGON_REST_URL = "https://api.polygon.io"

# Finnhub fallback (free tier, SPY as proxy)
FINNHUB_REST_URL = "https://finnhub.io/api/v1"
SPY_TO_SPX_RATIO = 10.03  # approximate SPY * 10.03 ≈ SPX

# VIX polling interval
VIX_POLL_INTERVAL = 60  # seconds

# Price feed settings
PRICE_BUFFER_SIZE = 300  # 5 minutes of second-by-second data
STALE_PRICE_THRESHOLD = 60  # seconds before price considered stale
RECONNECT_BASE_DELAY = 1.0
RECONNECT_MAX_DELAY = 60.0

# Polygon circuit breaker: back off for 5 min on 403/auth failure
POLYGON_BACKOFF_SECONDS = 300
# Fallback poll interval when Polygon is down (stay under Finnhub 60/min limit)
FINNHUB_ONLY_POLL_INTERVAL = 3.0
# Observability thresholds
NO_PRICE_ERROR_THRESHOLD = 60       # log ERROR after 60 consecutive failures
NO_PRICE_CRITICAL_THRESHOLD = 300   # log CRITICAL after 300 consecutive failures

# EGARCH settings
SPX_EGARCH_GAMMA_BOUNDS = (-0.30, -0.05)  # leverage asymmetry 4x stronger than crypto
SPX_EGARCH_REFIT_INTERVAL = 14400  # 4 hours (less volatile)
SPX_EGARCH_STATE_FILE = "spx_egarch_state.json"
SPX_EGARCH_MIN_OBSERVATIONS = 100

# Seasonal filter
SPX_SEASONAL_STATE_FILE = "spx_seasonal_state.json"
SEASONAL_EWMA_LAMBDA = 0.97  # ~23-day half-life
SEASONAL_HALF_HOUR_BUCKETS = 13  # 09:30-10:00, ..., 15:30-16:00

# Realized kernel settings
SPX_RK_PARZEN_BANDWIDTH = 10
SPX_MZ_WINDOW = 50  # Mincer-Zarnowitz window for EGARCH blend

# VIX divergence threshold for implied vol integration
VIX_DIVERGENCE_THRESHOLD = 0.30  # 30% divergence triggers shift toward implied

# Market warmup: skip first N minutes after open
WARMUP_MINUTES = 10


# ═════════════════════════════════════════════════════════════════════════════
#  NYSE Holiday Calendar & Market Hours
# ═════════════════════════════════════════════════════════════════════════════

NYSE_HOLIDAYS_2026 = {
    "2026-01-01",  # New Year's Day
    "2026-01-19",  # MLK Day
    "2026-02-16",  # Presidents Day
    "2026-04-03",  # Good Friday
    "2026-05-25",  # Memorial Day
    "2026-07-03",  # Independence Day (observed)
    "2026-09-07",  # Labor Day
    "2026-11-26",  # Thanksgiving
    "2026-12-25",  # Christmas
}

# FOMC statement release dates 2026
FOMC_DATES_2026 = {
    "2026-01-28", "2026-03-18", "2026-04-29", "2026-06-10",
    "2026-07-29", "2026-09-16", "2026-10-28", "2026-12-09",
}


class MarketHoursGuard:
    """NYSE regular trading hours checker."""

    @staticmethod
    def _is_dst(dt_utc: datetime.datetime) -> bool:
        """True if US Eastern is in DST (EDT) at the given UTC time.

        US DST: starts 2nd Sunday of March at 2:00 AM ET,
                ends 1st Sunday of November at 2:00 AM ET.
        """
        year = dt_utc.year
        # Find 2nd Sunday in March
        mar1 = datetime.date(year, 3, 1)
        # days until first Sunday: (6 - weekday) % 7
        first_sun_mar = mar1 + datetime.timedelta(days=(6 - mar1.weekday()) % 7)
        second_sun_mar = first_sun_mar + datetime.timedelta(days=7)
        # DST starts at 2:00 AM EST = 07:00 UTC
        dst_start_utc = datetime.datetime(year, 3, second_sun_mar.day, 7, 0,
                                          tzinfo=datetime.timezone.utc)

        # Find 1st Sunday in November
        nov1 = datetime.date(year, 11, 1)
        first_sun_nov = nov1 + datetime.timedelta(days=(6 - nov1.weekday()) % 7)
        # DST ends at 2:00 AM EDT = 06:00 UTC
        dst_end_utc = datetime.datetime(year, 11, first_sun_nov.day, 6, 0,
                                        tzinfo=datetime.timezone.utc)

        return dst_start_utc <= dt_utc < dst_end_utc

    @staticmethod
    def _now_et() -> datetime.datetime:
        """Current time in US Eastern (handles DST correctly)."""
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        offset = datetime.timedelta(hours=-4 if MarketHoursGuard._is_dst(now_utc) else -5)
        return now_utc.astimezone(datetime.timezone(offset))

    @staticmethod
    def is_market_open() -> bool:
        """True if current time is within NYSE RTH (9:30-16:00 ET), not a holiday."""
        now_et = MarketHoursGuard._now_et()

        date_str = now_et.strftime("%Y-%m-%d")
        if date_str in NYSE_HOLIDAYS_2026:
            return False

        # Weekday check (0=Monday, 6=Sunday)
        if now_et.weekday() >= 5:
            return False

        market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
        market_close = now_et.replace(hour=16, minute=0, second=0, microsecond=0)
        return market_open <= now_et <= market_close

    @staticmethod
    def is_within_buffer() -> bool:
        """True if within 5-minute buffer around market hours (9:25-16:05 ET)."""
        now_et = MarketHoursGuard._now_et()

        date_str = now_et.strftime("%Y-%m-%d")
        if date_str in NYSE_HOLIDAYS_2026:
            return False
        if now_et.weekday() >= 5:
            return False

        buffer_open = now_et.replace(hour=9, minute=25, second=0, microsecond=0)
        buffer_close = now_et.replace(hour=16, minute=5, second=0, microsecond=0)
        return buffer_open <= now_et <= buffer_close

    @staticmethod
    def is_fomc_day() -> bool:
        today_str = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return today_str in FOMC_DATES_2026

    @staticmethod
    def minutes_since_open() -> Optional[int]:
        """Minutes since market open, or None if market closed."""
        now_et = MarketHoursGuard._now_et()
        if now_et.weekday() >= 5:
            return None
        market_open = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
        if now_et < market_open:
            return None
        return int((now_et - market_open).total_seconds() / 60)


# ═════════════════════════════════════════════════════════════════════════════
#  Intraday Seasonal Filter
# ═════════════════════════════════════════════════════════════════════════════

# Initial U-shape factors (open high, midday low, close high)
_DEFAULT_SEASONAL = [
    1.50,  # 09:30-10:00
    1.30,  # 10:00-10:30
    1.10,  # 10:30-11:00
    0.90,  # 11:00-11:30
    0.80,  # 11:30-12:00
    0.70,  # 12:00-12:30
    0.70,  # 12:30-13:00
    0.75,  # 13:00-13:30
    0.80,  # 13:30-14:00
    0.90,  # 14:00-14:30
    1.00,  # 14:30-15:00
    1.20,  # 15:00-15:30
    1.30,  # 15:30-16:00
]


class IntradaySeasonalFilter:
    """EWMA-based intraday volatility seasonal factor.

    13 half-hour buckets covering 09:30-16:00 ET.
    Updates at the end of each bucket with realized variance from that bucket.
    """

    def __init__(self):
        self._factors = list(_DEFAULT_SEASONAL)
        self._bucket_returns: List[List[float]] = [[] for _ in range(SEASONAL_HALF_HOUR_BUCKETS)]
        self._load_state()

    def _get_bucket_index(self, ts: Optional[datetime.datetime] = None) -> Optional[int]:
        """Map timestamp to bucket index (0-12). None if outside market hours."""
        if ts is None:
            ts = datetime.datetime.now(
                datetime.timezone(datetime.timedelta(hours=-4))  # EDT
            )
        h, m = ts.hour, ts.minute
        if h < 9 or (h == 9 and m < 30) or h >= 16:
            return None
        minutes_since_open = (h - 9) * 60 + m - 30
        idx = minutes_since_open // 30
        return max(0, min(SEASONAL_HALF_HOUR_BUCKETS - 1, idx))

    def add_return(self, log_return: float, ts: Optional[datetime.datetime] = None):
        """Record a return for the current bucket."""
        idx = self._get_bucket_index(ts)
        if idx is not None:
            self._bucket_returns[idx].append(log_return)

    def end_bucket(self, bucket_idx: int):
        """Finalize a bucket: update its EWMA seasonal factor with realized variance."""
        returns = self._bucket_returns[bucket_idx]
        if len(returns) < 5:
            return  # not enough data
        realized_var = sum(r ** 2 for r in returns) / len(returns)
        # Normalize: factor = sqrt(realized_var) / avg_sqrt_var
        avg_var = sum(f ** 2 for f in self._factors) / SEASONAL_HALF_HOUR_BUCKETS
        if avg_var > 0:
            new_factor = math.sqrt(realized_var / avg_var)
            self._factors[bucket_idx] = (
                SEASONAL_EWMA_LAMBDA * self._factors[bucket_idx]
                + (1 - SEASONAL_EWMA_LAMBDA) * new_factor
            )
        self._bucket_returns[bucket_idx] = []
        self._save_state()

    def get_seasonal_factor(self, ts: Optional[datetime.datetime] = None) -> float:
        """Get the seasonal factor for the current time bucket."""
        idx = self._get_bucket_index(ts)
        if idx is None:
            return 1.0
        return self._factors[idx]

    def deseasonalize_return(self, log_return: float,
                             ts: Optional[datetime.datetime] = None) -> float:
        """Divide return by seasonal factor to get deseasonalized return."""
        factor = self.get_seasonal_factor(ts)
        if factor > 0:
            return log_return / factor
        return log_return

    def _load_state(self):
        try:
            with open(SPX_SEASONAL_STATE_FILE, "r") as f:
                state = json.load(f)
            factors = state.get("factors", [])
            if len(factors) == SEASONAL_HALF_HOUR_BUCKETS:
                self._factors = factors
                logging.info("IntradaySeasonalFilter: loaded state")
        except FileNotFoundError:
            pass
        except Exception as e:
            logging.warning("IntradaySeasonalFilter: load failed: %s", e)

    def _save_state(self):
        try:
            with open(SPX_SEASONAL_STATE_FILE, "w") as f:
                json.dump({"factors": self._factors,
                           "updated_at": datetime.datetime.now(timezone.utc).isoformat()}, f)
        except Exception as e:
            logging.warning("IntradaySeasonalFilter: save failed: %s", e)


# ═════════════════════════════════════════════════════════════════════════════
#  SPX EGARCH(1,1) Estimator
# ═════════════════════════════════════════════════════════════════════════════

class SPXEGARCHEstimator:
    """EGARCH(1,1) for SPX deseasonalized returns.

    Same math as crypto EGARCH but with:
      - SPX-specific gamma bounds (stronger leverage effect)
      - 4-hour refit interval
      - Market-hours-only returns
      - Separate state file
    """

    def __init__(self):
        # EGARCH(1,1) parameters: log(sigma^2_t) = omega + alpha*|z_{t-1}| + gamma*z_{t-1} + beta*log(sigma^2_{t-1})
        self._omega: float = -0.05
        self._alpha: float = 0.10
        self._gamma: float = -0.15  # negative = leverage effect
        self._beta: float = 0.98
        self._log_var: float = math.log(1e-6)  # initial log-variance
        self._n_updates: int = 0
        self._last_refit: float = 0.0
        self._returns_buffer: deque = deque(maxlen=2000)
        self._load_state()

    def recursive_update(self, deseasonalized_return: float) -> Optional[float]:
        """Update EGARCH with a new return. Returns current sigma estimate or None if warming up."""
        self._returns_buffer.append(deseasonalized_return)
        self._n_updates += 1

        if self._n_updates < 10:
            return None  # warmup

        # Standardized innovation
        sigma = math.sqrt(math.exp(self._log_var))
        z = deseasonalized_return / sigma if sigma > 1e-12 else 0.0

        # EGARCH recursion
        self._log_var = (
            self._omega
            + self._alpha * (abs(z) - math.sqrt(2.0 / math.pi))
            + self._gamma * z
            + self._beta * self._log_var
        )

        # Safety bounds on log-variance
        self._log_var = max(-30.0, min(0.0, self._log_var))

        return math.sqrt(math.exp(self._log_var))

    def maybe_refit(self) -> bool:
        """Refit EGARCH parameters via MLE if enough time has passed."""
        now = time.time()
        if now - self._last_refit < SPX_EGARCH_REFIT_INTERVAL:
            return False
        if len(self._returns_buffer) < SPX_EGARCH_MIN_OBSERVATIONS:
            return False

        try:
            from scipy.optimize import minimize

            returns = list(self._returns_buffer)
            n = len(returns)

            def neg_log_likelihood(params):
                omega, alpha, gamma, beta = params
                log_var = math.log(max(1e-12, sum(r ** 2 for r in returns[:10]) / 10))
                ll = 0.0
                for i in range(10, n):
                    var = math.exp(log_var)
                    if var < 1e-20:
                        return 1e12
                    z = returns[i] / math.sqrt(var)
                    log_var = (omega + alpha * (abs(z) - math.sqrt(2.0 / math.pi))
                               + gamma * z + beta * log_var)
                    log_var = max(-30.0, min(0.0, log_var))
                    ll += -0.5 * (math.log(2 * math.pi) + log_var + returns[i] ** 2 / math.exp(log_var))
                return -ll

            x0 = [self._omega, self._alpha, self._gamma, self._beta]
            bounds = [(-1.0, 1.0), (0.0, 0.5), SPX_EGARCH_GAMMA_BOUNDS, (0.8, 0.999)]
            result = minimize(neg_log_likelihood, x0, method="L-BFGS-B", bounds=bounds)

            if result.success:
                self._omega, self._alpha, self._gamma, self._beta = result.x
                self._last_refit = now
                self._save_state()
                logging.info(
                    "SPXEGARCHEstimator: refit omega=%.4f alpha=%.4f gamma=%.4f beta=%.4f",
                    self._omega, self._alpha, self._gamma, self._beta,
                )
                return True
        except Exception as e:
            logging.warning("SPXEGARCHEstimator: refit failed: %s", e)

        self._last_refit = now  # prevent retry storm
        return False

    def seed_from_vix(self, vix: float):
        """Seed opening log_var from VIX-implied vol (for overnight gap)."""
        # VIX is annualized %; convert to per-second variance
        annual_vol = vix / 100.0
        per_second_var = (annual_vol ** 2) / (252 * 6.5 * 3600)
        self._log_var = math.log(max(1e-12, per_second_var))
        logging.info("SPXEGARCHEstimator: seeded from VIX=%.1f, per_second_var=%.2e", vix, per_second_var)

    def get_sigma(self) -> Optional[float]:
        if self._n_updates < 10:
            return None
        return math.sqrt(math.exp(self._log_var))

    def _load_state(self):
        try:
            with open(SPX_EGARCH_STATE_FILE, "r") as f:
                state = json.load(f)
            self._omega = state.get("omega", self._omega)
            self._alpha = state.get("alpha", self._alpha)
            self._gamma = state.get("gamma", self._gamma)
            self._beta = state.get("beta", self._beta)
            self._log_var = state.get("log_var", self._log_var)
            self._n_updates = state.get("n_updates", 0)
            self._last_refit = state.get("last_refit", 0.0)
            logging.info("SPXEGARCHEstimator: loaded state (n=%d)", self._n_updates)
        except FileNotFoundError:
            pass
        except Exception as e:
            logging.warning("SPXEGARCHEstimator: load failed: %s", e)

    def _save_state(self):
        try:
            with open(SPX_EGARCH_STATE_FILE, "w") as f:
                json.dump({
                    "omega": self._omega, "alpha": self._alpha,
                    "gamma": self._gamma, "beta": self._beta,
                    "log_var": self._log_var, "n_updates": self._n_updates,
                    "last_refit": self._last_refit,
                    "updated_at": datetime.datetime.now(timezone.utc).isoformat(),
                }, f, indent=2)
        except Exception as e:
            logging.warning("SPXEGARCHEstimator: save failed: %s", e)


# ═════════════════════════════════════════════════════════════════════════════
#  SPX Price Feed
# ═════════════════════════════════════════════════════════════════════════════

class SPXPriceFeed:
    """SPX + VIX price feed via Polygon.io REST polling + Finnhub fallback.

    Uses REST polling rather than WebSocket for simplicity (1 req/sec during market hours).
    Polygon free tier allows 5 calls/min; paid tiers allow much more.
    """

    def __init__(self, polygon_key: Optional[str] = None, finnhub_key: Optional[str] = None):
        self._polygon_key = polygon_key or os.environ.get("POLYGON_API_KEY", "")
        self._finnhub_key = finnhub_key or os.environ.get("FINNHUB_API_KEY", "")
        self._prices: Dict[str, float] = {}
        self._buffers: Dict[str, deque] = {
            "SPX": deque(maxlen=PRICE_BUFFER_SIZE),
            "VIX": deque(maxlen=60),
        }
        self._last_update: Dict[str, float] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._reconnect_delay = RECONNECT_BASE_DELAY
        self._consecutive_stale: int = 0
        # Polygon circuit breaker
        self._polygon_backoff_until: float = 0.0
        self._polygon_in_backoff: bool = False
        # Observability
        self._consecutive_no_price: int = 0

    def start(self):
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()
        logging.info("SPXPriceFeed: started (polygon=%s, finnhub=%s)",
                     "yes" if self._polygon_key else "no",
                     "yes" if self._finnhub_key else "no")

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def get_price(self, symbol: str = "SPX") -> Optional[float]:
        with self._lock:
            return self._prices.get(symbol)

    def get_vix(self) -> Optional[float]:
        with self._lock:
            return self._prices.get("VIX")

    def get_returns(self, symbol: str = "SPX", n: int = 60) -> List[float]:
        """Get last N log returns from price buffer."""
        with self._lock:
            buf = list(self._buffers.get(symbol, []))
        if len(buf) < 2:
            return []
        returns = []
        for i in range(1, min(n + 1, len(buf))):
            if buf[i - 1] > 0:
                returns.append(math.log(buf[i] / buf[i - 1]))
        return returns

    def is_stale(self, symbol: str = "SPX") -> bool:
        with self._lock:
            last = self._last_update.get(symbol, 0)
        return time.time() - last > STALE_PRICE_THRESHOLD

    def _poll_loop(self):
        """Main polling loop: fetch SPX every second, VIX every 60s during market hours.

        Circuit breaker: when Polygon returns 403, back off for POLYGON_BACKOFF_SECONDS
        and slow the poll interval to FINNHUB_ONLY_POLL_INTERVAL (3s) to stay under
        Finnhub's 60 req/min free-tier limit.
        """
        vix_last = 0.0
        while not self._stop.is_set():
            try:
                if not MarketHoursGuard.is_within_buffer():
                    self._stop.wait(30)
                    continue

                now = time.time()

                # Check if Polygon backoff has expired
                if self._polygon_in_backoff and now >= self._polygon_backoff_until:
                    self._polygon_in_backoff = False
                    logging.info("SPXPriceFeed: Polygon backoff expired, re-enabling")

                # Fetch SPX — skip Polygon if in backoff
                spx = None
                if not self._polygon_in_backoff:
                    spx = self._fetch_spx_polygon()
                if spx is None:
                    spx = self._fetch_spx_finnhub()

                if spx is not None:
                    with self._lock:
                        self._prices["SPX"] = spx
                        self._buffers["SPX"].append(spx)
                        self._last_update["SPX"] = time.time()
                    self._reconnect_delay = RECONNECT_BASE_DELAY
                    self._consecutive_no_price = 0
                else:
                    self._consecutive_no_price += 1
                    cnt = self._consecutive_no_price
                    if cnt == NO_PRICE_CRITICAL_THRESHOLD:
                        logging.critical(
                            "SPXPriceFeed: %d consecutive poll cycles with no price update "
                            "(polygon_backoff=%s)", cnt, self._polygon_in_backoff)
                    elif cnt == NO_PRICE_ERROR_THRESHOLD:
                        logging.error(
                            "SPXPriceFeed: %d consecutive poll cycles with no price update "
                            "(polygon_backoff=%s)", cnt, self._polygon_in_backoff)

                # Fetch VIX (less frequently) — skip if Polygon is in backoff
                if not self._polygon_in_backoff and now - vix_last >= VIX_POLL_INTERVAL:
                    vix = self._fetch_vix_polygon()
                    if vix is not None:
                        with self._lock:
                            self._prices["VIX"] = vix
                            self._buffers["VIX"].append(vix)
                            self._last_update["VIX"] = time.time()
                    vix_last = now

                # Slow poll when in fallback-only mode to respect Finnhub rate limit
                poll_interval = FINNHUB_ONLY_POLL_INTERVAL if self._polygon_in_backoff else 1.0
                self._stop.wait(poll_interval)

            except Exception as e:
                logging.warning("SPXPriceFeed: poll error: %s", e)
                self._stop.wait(min(self._reconnect_delay, RECONNECT_MAX_DELAY))
                self._reconnect_delay = min(self._reconnect_delay * 2, RECONNECT_MAX_DELAY)

    def _fetch_spx_polygon(self) -> Optional[float]:
        """Fetch SPX last price from Polygon.io snapshot endpoint.

        Triggers circuit breaker on 403 (auth failure) — backs off for
        POLYGON_BACKOFF_SECONDS to avoid hammering a dead endpoint.
        """
        if not self._polygon_key:
            return None
        try:
            url = f"{POLYGON_REST_URL}/v3/snapshot/indices"
            resp = requests.get(url, params={
                "ticker.any_of": "I:SPX",
                "apiKey": self._polygon_key,
            }, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                results = data.get("results", [])
                if results:
                    value = results[0].get("value") or results[0].get("session", {}).get("close")
                    if value and value > 0:
                        return float(value)
            elif resp.status_code in (401, 403):
                self._polygon_in_backoff = True
                self._polygon_backoff_until = time.time() + POLYGON_BACKOFF_SECONDS
                logging.warning(
                    "SPXPriceFeed: Polygon returned %d — circuit breaker engaged, "
                    "backing off for %ds", resp.status_code, POLYGON_BACKOFF_SECONDS)
        except Exception as e:
            logging.debug("SPXPriceFeed: Polygon SPX fetch failed: %s", e)
        return None

    def _fetch_vix_polygon(self) -> Optional[float]:
        """Fetch VIX from Polygon.io.

        Also triggers circuit breaker on 403 (shares backoff state with SPX fetch).
        """
        if not self._polygon_key:
            return None
        try:
            url = f"{POLYGON_REST_URL}/v3/snapshot/indices"
            resp = requests.get(url, params={
                "ticker.any_of": "I:VIX",
                "apiKey": self._polygon_key,
            }, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                results = data.get("results", [])
                if results:
                    value = results[0].get("value") or results[0].get("session", {}).get("close")
                    if value and value > 0:
                        return float(value)
            elif resp.status_code in (401, 403):
                if not self._polygon_in_backoff:
                    self._polygon_in_backoff = True
                    self._polygon_backoff_until = time.time() + POLYGON_BACKOFF_SECONDS
                    logging.warning(
                        "SPXPriceFeed: Polygon VIX returned %d — circuit breaker engaged",
                        resp.status_code)
        except Exception as e:
            logging.debug("SPXPriceFeed: Polygon VIX fetch failed: %s", e)
        return None

    def _fetch_spx_finnhub(self) -> Optional[float]:
        """Fallback: fetch SPY price from Finnhub and multiply by ratio.

        On 429 (rate limited), triggers reconnect backoff to slow down polling.
        """
        if not self._finnhub_key:
            return None
        try:
            resp = requests.get(f"{FINNHUB_REST_URL}/quote", params={
                "symbol": "SPY",
                "token": self._finnhub_key,
            }, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                price = data.get("c")  # current price
                if price and price > 0:
                    return float(price) * SPY_TO_SPX_RATIO
            elif resp.status_code == 429:
                logging.warning(
                    "SPXPriceFeed: Finnhub rate limited (429) — triggering backoff "
                    "(delay=%.1fs)", self._reconnect_delay)
                # Bump reconnect delay so the outer loop slows down
                self._reconnect_delay = min(self._reconnect_delay * 2, RECONNECT_MAX_DELAY)
                raise ConnectionError("Finnhub 429 rate limited")
        except ConnectionError:
            raise  # re-raise to trigger backoff in _poll_loop
        except Exception as e:
            logging.debug("SPXPriceFeed: Finnhub fallback failed: %s", e)
        return None


# ═════════════════════════════════════════════════════════════════════════════
#  SPX Volatility Engine
# ═════════════════════════════════════════════════════════════════════════════

class SPXVolatilityEngine:
    """Realized Kernel + EGARCH blend for SPX, with VIX integration.

    Same Parzen flat-top kernel as crypto. Blends EGARCH with RK via
    Mincer-Zarnowitz R-squared tracking.

    When VIX-implied vol diverges >30% from RV, shifts toward implied.
    """

    def __init__(self, price_feed: "SPXPriceFeed", seasonal_filter: "IntradaySeasonalFilter",
                 egarch: "SPXEGARCHEstimator"):
        self._feed = price_feed
        self._seasonal = seasonal_filter
        self._egarch = egarch
        self._rk_estimates: deque = deque(maxlen=100)
        self._egarch_forecasts: deque = deque(maxlen=100)
        self._mz_r_squared: float = 0.5  # initial blend weight
        self._last_bucket_idx: Optional[int] = None
        self._last_egarch_update: float = 0.0
        # Mincer-Zarnowitz tracking: lists of (egarch_var_forecast, rk_var_realized)
        self._mz_pairs: deque = deque(maxlen=SPX_MZ_WINDOW)
        self._mz_recompute_interval: float = 300.0  # 5 minutes
        self._mz_last_recompute: float = 0.0

    def update(self, seconds_to_close: float) -> Optional[Dict]:
        """Compute blended volatility estimate for SPX.

        Returns dict matching crypto VolatilityEngine output format.
        """
        returns = self._feed.get_returns("SPX", n=120)
        if len(returns) < 10:
            return None

        # Check warmup period
        mins = MarketHoursGuard.minutes_since_open()
        if mins is not None and mins < WARMUP_MINUTES:
            # During warmup, use VIX-implied if available
            vix = self._feed.get_vix()
            if vix is not None:
                self._egarch.seed_from_vix(vix)

        # Deseasonalize returns and feed to EGARCH
        for r in returns[-5:]:  # only process recent returns to avoid re-processing
            deseas_r = self._seasonal.deseasonalize_return(r)
            self._seasonal.add_return(r)
            self._egarch.recursive_update(deseas_r)

        # Check bucket transition for seasonal filter
        current_bucket = self._seasonal._get_bucket_index()
        if self._last_bucket_idx is not None and current_bucket != self._last_bucket_idx:
            self._seasonal.end_bucket(self._last_bucket_idx)
        self._last_bucket_idx = current_bucket

        # Maybe refit EGARCH
        self._egarch.maybe_refit()

        # Realized Kernel estimate
        rk_rv = self._compute_realized_kernel(returns)
        if rk_rv is None or rk_rv <= 0:
            return None

        # EGARCH estimate (re-seasonalized)
        egarch_sigma = self._egarch.get_sigma()
        seasonal_factor = self._seasonal.get_seasonal_factor()
        egarch_rv = egarch_sigma * seasonal_factor if egarch_sigma else None

        # Blend RK and EGARCH via variance-space blend (matches crypto pattern)
        egarch_blend_var = None
        if egarch_rv and egarch_rv > 0:
            # Record MZ pair for R-squared tracking
            egarch_var = egarch_rv ** 2
            rk_var = rk_rv ** 2
            self._mz_pairs.append((egarch_var, rk_var))
            self._maybe_recompute_mz()

            # Use MZ R-squared as blend weight for EGARCH
            blend_weight = max(0.0, min(0.8, self._mz_r_squared))

            if blend_weight > 0:
                # Variance-space blend: avoids Jensen's inequality bias
                egarch_blend_var = blend_weight * egarch_var + (1 - blend_weight) * rk_var
                blended_rv = math.sqrt(egarch_blend_var)
            else:
                blended_rv = rk_rv
        else:
            blended_rv = rk_rv
            blend_weight = 0.0

        # VIX integration: shift toward implied when divergent
        vix = self._feed.get_vix()
        vix_implied_rv = None
        if vix is not None:
            # Convert VIX (annualized %) to per-tick vol matching our returns
            annual_vol = vix / 100.0
            vix_implied_rv = annual_vol / math.sqrt(252 * 6.5 * 3600)
            divergence = abs(blended_rv - vix_implied_rv) / max(blended_rv, 1e-12)
            if divergence > VIX_DIVERGENCE_THRESHOLD:
                # Shift 30% toward VIX-implied
                blended_rv = 0.70 * blended_rv + 0.30 * vix_implied_rv

        # Determine regime
        if blended_rv > rk_rv * 1.5:
            regime = "high"
        elif blended_rv < rk_rv * 0.7:
            regime = "low"
        else:
            regime = "normal"

        # Diagnostic: if we had all inputs but blend_var is still None, log it
        if egarch_blend_var is None and egarch_sigma and blend_weight > 0:
            logging.warning(
                "SPX egarch_blend_var is None despite sigma=%.3e bw=%.3f rk=%.3e sf=%.3f egarch_rv=%s",
                egarch_sigma, blend_weight, rk_rv, seasonal_factor, egarch_rv)

        return {
            "blended_rv": blended_rv,
            "regime": regime,
            "egarch_sigma": egarch_sigma,
            "egarch_blend_weight": blend_weight,
            "egarch_blend_var": egarch_blend_var,
            "mz_r_squared": self._mz_r_squared,
            "rk_rv": rk_rv,
            "vix_implied_rv": vix_implied_rv,
            "seasonal_factor": seasonal_factor,
            "n_returns": len(returns),
        }

    def _maybe_recompute_mz(self):
        """Recompute Mincer-Zarnowitz R-squared from stored (forecast, realized) pairs."""
        now = time.time()
        if now - self._mz_last_recompute < self._mz_recompute_interval:
            return
        self._mz_last_recompute = now

        pairs = list(self._mz_pairs)
        if len(pairs) < 20:
            return  # need minimum sample

        forecasts = [p[0] for p in pairs]
        realized = [p[1] for p in pairs]
        n = len(forecasts)
        mean_r = sum(realized) / n
        ss_tot = sum((r - mean_r) ** 2 for r in realized)
        if ss_tot < 1e-30:
            return  # no variance in realized — can't compute R²

        # OLS: realized = a + b * forecast + e
        mean_f = sum(forecasts) / n
        ss_xy = sum((f - mean_f) * (r - mean_r) for f, r in zip(forecasts, realized))
        ss_xx = sum((f - mean_f) ** 2 for f in forecasts)
        if ss_xx < 1e-30:
            return
        b = ss_xy / ss_xx
        a = mean_r - b * mean_f
        ss_res = sum((r - (a + b * f)) ** 2 for f, r in zip(forecasts, realized))
        r_squared = max(0.0, 1.0 - ss_res / ss_tot)
        old = self._mz_r_squared
        self._mz_r_squared = r_squared
        if abs(r_squared - old) > 0.05:
            logging.info("SPX MZ R²: %.4f → %.4f (n=%d)", old, r_squared, n)

    def _compute_realized_kernel(self, returns: List[float]) -> Optional[float]:
        """Parzen flat-top kernel estimator on returns."""
        n = len(returns)
        if n < 5:
            return None

        # Gamma_0: sum of squared returns
        gamma_0 = sum(r ** 2 for r in returns)

        # Autocovariance-based kernel
        H = min(SPX_RK_PARZEN_BANDWIDTH, n - 1)
        rk = gamma_0
        for h in range(1, H + 1):
            gamma_h = sum(returns[i] * returns[i - h] for i in range(h, n))
            # Parzen kernel weight
            x = h / (H + 1)
            if x <= 0.5:
                k = 1 - 6 * x ** 2 + 6 * x ** 3
            else:
                k = 2 * (1 - x) ** 3
            rk += 2 * k * gamma_h

        # Return annualized vol (per-tick)
        rv = max(0.0, rk / n)
        return math.sqrt(rv) if rv > 0 else None


# ═════════════════════════════════════════════════════════════════════════════
#  SPX Engine Facade
# ═════════════════════════════════════════════════════════════════════════════

class SPXEngine:
    """Facade for all SPX components. Conforms to the expansion engine interface.

    Interface:
      - get_active_windows(client) -> List[Dict]
      - get_spot_price(asset) -> float
      - get_vol_estimate(asset, stc) -> Dict
      - is_market_open() -> bool
      - start() / stop()
    """

    def __init__(self, polygon_key: Optional[str] = None, finnhub_key: Optional[str] = None):
        self._hours = MarketHoursGuard()
        self._feed = SPXPriceFeed(polygon_key=polygon_key, finnhub_key=finnhub_key)
        self._seasonal = IntradaySeasonalFilter()
        self._egarch = SPXEGARCHEstimator()
        self._vol = SPXVolatilityEngine(self._feed, self._seasonal, self._egarch)
        self._started = False
        self._consecutive_vol_none: int = 0

    def start(self):
        self._feed.start()
        self._started = True
        logging.info("SPXEngine: started")

    def stop(self):
        self._feed.stop()
        self._started = False
        logging.info("SPXEngine: stopped")

    def is_market_open(self) -> bool:
        return MarketHoursGuard.is_market_open()

    def get_spot_price(self, asset: str = "SPX") -> Optional[float]:
        if self._feed.is_stale("SPX"):
            self._feed._consecutive_stale += 1
            cnt = self._feed._consecutive_stale
            if cnt == 120 or (cnt > 120 and cnt % 600 == 0):
                logging.critical("SPX price feed stale for %d consecutive checks", cnt)
            else:
                logging.warning("SPXEngine: SPX price stale >%ds", STALE_PRICE_THRESHOLD)
            return None
        self._feed._consecutive_stale = 0
        return self._feed.get_price("SPX")

    def get_vix(self) -> Optional[float]:
        return self._feed.get_vix()

    def get_vol_estimate(self, asset: str, stc: float) -> Optional[Dict]:
        result = self._vol.update(stc)
        if result is None:
            self._consecutive_vol_none += 1
            cnt = self._consecutive_vol_none
            if cnt == 60 or (cnt > 60 and cnt % 300 == 0):
                logging.critical("SPX vol engine returned None for %d consecutive checks", cnt)
            return None
        self._consecutive_vol_none = 0
        return result

    def get_active_windows(self, client) -> List[Dict]:
        """Query Kalshi for KXINXU events. Returns windows with product_type='spx_hourly'."""
        if not MarketHoursGuard.is_market_open():
            return []

        now = datetime.datetime.now(timezone.utc)
        windows = []

        try:
            result = client.get_events(
                series_ticker=SPX_SERIES_TICKER,
                status="open",
                with_nested_markets=True,
                limit=100,
            )
            events = result.get("events") if result else None
            if not events:
                logging.info("SPXEngine: no open KXINXU events")
                return []

            for event in events:
                event_ticker = event.get("event_ticker", "")
                nested_markets = event.get("markets", [])
                if not isinstance(nested_markets, list):
                    continue
                mkts = [m for m in nested_markets if isinstance(m, dict)]
                if not mkts:
                    continue

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
                    "asset": SPX_ASSET,
                    "event_ticker": event_ticker,
                    "close_time": close_time,
                    "seconds_to_close": seconds_to_close,
                    "markets": mkts,
                    "product_type": "spx_hourly",
                })

            if windows:
                mkt_count = sum(len(w["markets"]) for w in windows)
                logging.info("SPXEngine: %d markets in %d windows", mkt_count, len(windows))
        except Exception as e:
            logging.warning("SPXEngine: window discovery failed: %s", e)

        return windows
