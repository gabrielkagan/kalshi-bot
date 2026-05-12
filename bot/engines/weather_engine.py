"""Weather market engine — NWP ensemble fetcher, probability model, Kalshi integration."""

import json
import math
import os
import time
import logging
import datetime
import sqlite3
import threading
from datetime import timezone
from typing import Dict, List, Optional, Tuple

import requests

from bot.db_writer_registry import tracked_write  # ops: db-locked RCA instrumentation 2026-05-08

# Ensemble cache file — persists _last_ensemble across bot restarts so a cold
# start doesn't leave us fully dark while Open-Meteo 429-throttles the first
# fetch cycle. Weather changes slowly (poll interval is 15 min), so stale-by-
# a-few-hours data is still usable. See kb/failures/weather-engine-cold-start.md
WEATHER_ENSEMBLE_CACHE_FILE = "weather_ensemble_cache.json"

# ─── Configuration ────────────────────────────────────────────────────────────

# Open-Meteo ensemble API (free, no key needed)
OPEN_METEO_ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
OPEN_METEO_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Polling interval (weather changes slowly)
WEATHER_POLL_INTERVAL = 900  # 15 minutes

# Ensemble member counts
GFS_MEMBERS = 31
ECMWF_MEMBERS = 51
TOTAL_ENSEMBLE_MEMBERS = GFS_MEMBERS + ECMWF_MEMBERS  # 82

# Bias correction EWMA
BIAS_EWMA_LAMBDA = 0.90  # ~7-day half-life with daily updates
BIAS_MIN_SIGNALS = 30    # Min settled signals before applying bias correction per city
                         # Computing correction from <30 signals is just fitting noise

# Per-city ensemble std correction factors (error_to_std_ratio from Mar 1-8 audit).
# Ensemble spread systematically underestimates true forecast uncertainty.
# Multiply ensemble_std by this factor before computing probabilities.
# Default 1.5x for cities without enough verification data yet.
WEATHER_STD_CORRECTION_DEFAULT = 1.5
WEATHER_STD_CORRECTION: Dict[str, float] = {
    "SFO": 3.3,   # MAE=4.85, std=1.47 → ratio 3.3x
    "PHI": 1.6,   # MAE=3.70, std=2.32 → ratio 1.6x
    "MIA": 1.4,   # MAE=1.30, std=0.92 → ratio 1.4x
    "LAS": 1.2,   # MAE=3.13, std=2.72 → ratio 1.2x
    "DCA": 1.0,   # MAE=2.82, std=2.84 → already calibrated
    "DAL": 1.0,   # MAE=2.55, std=2.58 → already calibrated
    "NYC": 0.8,   # MAE=1.72, std=2.09 → overdispersive
    "PHX": 0.4,   # MAE=0.62, std=1.55 → very overdispersive
    "CHI": 0.8,   # MAE=1.73, std=2.14 → overdispersive
    "MIN": 0.4,   # MAE=0.97, std=2.28 → very overdispersive
    "DEN": 0.4,   # MAE=0.70, std=1.82 → very overdispersive
    "OKC": 0.3,   # MAE=0.70, std=2.79 → very overdispersive
    "ATL": 0.4,   # MAE=0.72, std=1.74 → very overdispersive
    "HOU": 0.6,   # MAE=0.95, std=1.63 → overdispersive
    "AUS": 0.5,   # MAE=1.18, std=2.28 → overdispersive
    "SEA": 0.9,   # MAE=1.31, std=1.47 → close
    "LAX": 1.0,   # MAE=1.74, std=1.80 → calibrated
    "BOS": 1.3,   # MAE=1.63, std=1.27 → slightly underdispersive
    "MSY": 1.3,   # MAE=2.16, std=1.71 → slightly underdispersive
}

# Shadow trade signal focus: which market types and cities to evaluate
WEATHER_SHADOW_FOCUS_MARKET_TYPES = {"lower_tail"}
WEATHER_SHADOW_FOCUS_CITIES = {"DCA", "MIN", "PHI", "BOS", "NYC"}

# Cities with Kalshi weather markets
WEATHER_CITIES: Dict[str, Dict] = {
    "NYC": {
        "name": "New York City",
        "lat": 40.7128,
        "lon": -74.0060,
        "series_ticker": "KXHIGHNY",
        "nws_station": "KNYC",
    },
    "CHI": {
        "name": "Chicago",
        "lat": 41.8781,
        "lon": -87.6298,
        "series_ticker": "KXHIGHCHI",
        "nws_station": "KORD",
    },
    "MIA": {
        "name": "Miami",
        "lat": 25.7617,
        "lon": -80.1918,
        "series_ticker": "KXHIGHMIA",
        "nws_station": "KMIA",
    },
    "DEN": {
        "name": "Denver",
        "lat": 39.7392,
        "lon": -104.9903,
        "series_ticker": "KXHIGHDEN",
        "nws_station": "KDEN",
    },
    "LAX": {
        "name": "Los Angeles",
        "lat": 34.0522,
        "lon": -118.2437,
        "series_ticker": "KXHIGHLAX",
        "nws_station": "KLAX",
    },
    "AUS": {
        "name": "Austin",
        "lat": 30.2672,
        "lon": -97.7431,
        "series_ticker": "KXHIGHAUS",
        "nws_station": "KAUS",
    },
    "ATL": {
        "name": "Atlanta",
        "lat": 33.7490,
        "lon": -84.3880,
        "series_ticker": "KXHIGHTATL",
        "nws_station": "KATL",
    },
    "SFO": {
        "name": "San Francisco",
        "lat": 37.7749,
        "lon": -122.4194,
        "series_ticker": "KXHIGHTSFO",
        "nws_station": "KSFO",
    },
    "DAL": {
        "name": "Dallas",
        "lat": 32.7767,
        "lon": -96.7970,
        "series_ticker": "KXHIGHTDAL",
        "nws_station": "KDFW",
    },
    "PHX": {
        "name": "Phoenix",
        "lat": 33.4484,
        "lon": -112.0740,
        "series_ticker": "KXHIGHTPHX",
        "nws_station": "KPHX",
    },
    "PHI": {
        "name": "Philadelphia",
        "lat": 39.9526,
        "lon": -75.1652,
        "series_ticker": "KXHIGHPHIL",
        "nws_station": "KPHL",
    },
    "MIN": {
        "name": "Minneapolis",
        "lat": 44.9778,
        "lon": -93.2650,
        "series_ticker": "KXHIGHTMIN",
        "nws_station": "KMSP",
    },
    "SEA": {
        "name": "Seattle",
        "lat": 47.6062,
        "lon": -122.3321,
        "series_ticker": "KXHIGHTSEA",
        "nws_station": "KSEA",
    },
    "HOU": {
        "name": "Houston",
        "lat": 29.7604,
        "lon": -95.3698,
        "series_ticker": "KXHIGHTHOU",
        "nws_station": "KIAH",
    },
    "BOS": {
        "name": "Boston",
        "lat": 42.3601,
        "lon": -71.0589,
        "series_ticker": "KXHIGHTBOS",
        "nws_station": "KBOS",
    },
    "LAS": {
        "name": "Las Vegas",
        "lat": 36.1699,
        "lon": -115.1398,
        "series_ticker": "KXHIGHTLV",
        "nws_station": "KLAS",
    },
    "OKC": {
        "name": "Oklahoma City",
        "lat": 35.4676,
        "lon": -97.5164,
        "series_ticker": "KXHIGHTOKC",
        "nws_station": "KOKC",
    },
    "DCA": {
        "name": "Washington DC",
        "lat": 38.9072,
        "lon": -77.0369,
        "series_ticker": "KXHIGHTDC",
        "nws_station": "KDCA",
    },
    "MSY": {
        "name": "New Orleans",
        "lat": 29.9511,
        "lon": -90.0715,
        "series_ticker": "KXHIGHTNOLA",
        "nws_station": "KMSY",
    },
}

# Max cities to scan per day (all 19 in observation mode for faster data collection)
MAX_CITIES_PER_DAY = 19


# ═════════════════════════════════════════════════════════════════════════════
#  Weather Ensemble Fetcher
# ═════════════════════════════════════════════════════════════════════════════

class WeatherEnsembleFetcher:
    """Fetches NWP ensemble forecasts from Open-Meteo (GFS + ECMWF).

    Caches results per city/date since weather changes slowly (15-min granularity).
    """

    def __init__(self):
        self._cache: Dict[str, Dict] = {}  # key: "CITY_YYYY-MM-DD" -> ensemble data
        self._cache_ts: Dict[str, float] = {}
        self._lock = threading.Lock()
        self._backoff_until: float = 0.0  # timestamp: skip all fetches until this time
        self._consecutive_429s: int = 0

    def fetch_ensemble(self, city_code: str, target_date: Optional[str] = None) -> Optional[Dict]:
        """Fetch GFS + ECMWF ensemble for a city's daily high temperature.

        Returns dict with gfs_members, ecmwf_members, hrrr_temp, combined_members.
        """
        city = WEATHER_CITIES.get(city_code)
        if not city:
            return None

        if target_date is None:
            target_date = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%d")

        cache_key = f"{city_code}_{target_date}"

        with self._lock:
            cached = self._cache.get(cache_key)
            cached_ts = self._cache_ts.get(cache_key, 0)
            if cached and time.time() - cached_ts < WEATHER_POLL_INTERVAL:
                cache_age = time.time() - cached_ts
                if cache_age > 1800:  # 30 min
                    logging.warning("WeatherEnsemble: %s serving stale cache (%.0fs old)", city_code, cache_age)
                return cached
            # Backoff: skip fetches if recently rate-limited by Open-Meteo
            if time.time() < self._backoff_until:
                return cached  # return stale cache (or None) during backoff

        lat, lon = city["lat"], city["lon"]
        result = {}

        # Fetch GFS ensemble
        gfs_members = self._fetch_model_ensemble(lat, lon, "gfs_seamless", target_date)
        result["gfs_members"] = gfs_members
        time.sleep(2.0)  # Rate-limit: 19 cities × 3 calls = 57 calls; Open-Meteo ~30 req/min

        # Fetch ECMWF ensemble
        ecmwf_members = self._fetch_model_ensemble(lat, lon, "ecmwf_ifs025", target_date)
        result["ecmwf_members"] = ecmwf_members
        time.sleep(2.0)

        if not ecmwf_members:
            logging.warning("WeatherEnsemble: %s ECMWF returned no members (ecmwf_ifs025)", city_code)

        # Fetch HRRR deterministic (higher resolution, shorter range)
        # Sleep before HRRR to avoid timeout/SSL errors under rate-limit pressure
        time.sleep(2.0)
        hrrr_temp = self._fetch_hrrr(lat, lon, target_date)
        result["hrrr_temp"] = hrrr_temp

        # Combine all members
        combined = []
        if gfs_members:
            combined.extend(gfs_members)
        if ecmwf_members:
            combined.extend(ecmwf_members)
        result["combined_members"] = combined
        result["n_members"] = len(combined)
        result["fetch_time"] = datetime.datetime.now(timezone.utc).isoformat()

        if combined:
            _mean = sum(combined) / len(combined)
            _var = sum((m - _mean) ** 2 for m in combined) / len(combined)
            _std = math.sqrt(max(_var, 0.01))
            _hrrr_str = f"{result['hrrr_temp']:.1f}" if result.get("hrrr_temp") is not None else "None"
            logging.info("WeatherEnsemble: %s n=%d mean=%.1fF std=%.1fF hrrr=%sF gfs=%s ecmwf=%s",
                         city_code, len(combined), _mean, _std,
                         _hrrr_str,
                         len(gfs_members) if gfs_members else 0,
                         len(ecmwf_members) if ecmwf_members else 0)

        with self._lock:
            self._cache[cache_key] = result
            self._cache_ts[cache_key] = time.time()

        return result

    def _fetch_model_ensemble(self, lat: float, lon: float, model: str,
                              target_date: str) -> Optional[List[float]]:
        """Fetch ensemble members' daily high temperature from Open-Meteo."""
        if time.time() < self._backoff_until:
            return None
        t0 = time.time()
        try:
            resp = requests.get(OPEN_METEO_ENSEMBLE_URL, params={
                "latitude": lat,
                "longitude": lon,
                "daily": "temperature_2m_max",
                "models": model,
                "temperature_unit": "fahrenheit",
                "start_date": target_date,
                "end_date": target_date,
                "timezone": "America/New_York",
            }, timeout=15)
            elapsed = time.time() - t0

            if resp.status_code != 200:
                logging.warning("WeatherEnsemble: %s %s returned HTTP %d (%.1fs)",
                                model, target_date, resp.status_code, elapsed)
                if resp.status_code == 429:
                    self._consecutive_429s += 1
                    # Exponential backoff: 60s, 120s, 240s, 480s, max 900s (15 min)
                    backoff_secs = min(60 * (2 ** (self._consecutive_429s - 1)), 900)
                    self._backoff_until = time.time() + backoff_secs
                    logging.warning("WeatherEnsemble: 429 backoff #%d — pausing %.0fs",
                                    self._consecutive_429s, backoff_secs)
                return None

            data = resp.json()
            if data.get("error"):
                logging.warning("WeatherEnsemble: %s API error (%.1fs): %s",
                                model, elapsed, data.get("reason", "unknown"))
                return None

            # Open-Meteo ensemble returns multiple members in daily data
            daily = data.get("daily", {})
            members = []
            for key, values in daily.items():
                if key.startswith("temperature_2m_max") and isinstance(values, list):
                    for v in values:
                        if v is not None:
                            members.append(float(v))

            if not members:
                # Try hourly fallback: get max temp across hours for each member
                hourly = data.get("hourly", {})
                for key, values in hourly.items():
                    if key.startswith("temperature_2m") and isinstance(values, list):
                        valid = [float(v) for v in values if v is not None]
                        if valid:
                            members.append(max(valid))
                if members:
                    logging.info("WeatherEnsembleFetcher: %s used hourly fallback, %d members",
                                 model, len(members))

            if not members:
                logging.warning("WeatherEnsemble: %s %s returned 0 members (%.1fs)",
                                model, target_date, elapsed)
                return None

            _m = sum(members) / len(members)
            logging.debug("WeatherEnsembleFetcher: %s %d members, mean=%.1fF, range=[%.1f, %.1f]",
                          model, len(members), _m, min(members), max(members))

            # Successful fetch — reset backoff
            self._consecutive_429s = 0
            self._backoff_until = 0.0
            return members

        except requests.RequestException as e:
            elapsed = time.time() - t0
            logging.warning("WeatherEnsemble: %s %s fetch FAILED (%.1fs): %s",
                            model, target_date, elapsed, e)
            return None
        except Exception as e:
            elapsed = time.time() - t0
            logging.warning("WeatherEnsemble: %s %s parse error (%.1fs): %s",
                            model, target_date, elapsed, e)
            return None

    def fetch_observed_high(self, city_code: str, date_str: str) -> Optional[float]:
        """Fetch actual observed daily high temperature from Open-Meteo archive API.

        Args:
            city_code: City code (e.g., 'NYC', 'CHI')
            date_str: Date in YYYY-MM-DD format (must be yesterday or earlier)

        Returns:
            Observed daily high in °F, or None if unavailable.
        """
        city = WEATHER_CITIES.get(city_code)
        if not city:
            return None
        try:
            resp = requests.get("https://archive-api.open-meteo.com/v1/archive", params={
                "latitude": city["lat"],
                "longitude": city["lon"],
                "daily": "temperature_2m_max",
                "start_date": date_str,
                "end_date": date_str,
                "temperature_unit": "fahrenheit",
                "timezone": "America/New_York",
            }, timeout=15)
            if resp.status_code != 200:
                logging.warning("WeatherEnsemble: archive %s %s HTTP %d",
                                city_code, date_str, resp.status_code)
                return None
            data = resp.json()
            if data.get("error"):
                logging.warning("WeatherEnsemble: archive %s %s API error: %s",
                                city_code, date_str, data.get("reason", "unknown"))
                return None
            temps = data.get("daily", {}).get("temperature_2m_max", [])
            if temps and temps[0] is not None:
                observed = float(temps[0])
                logging.info("WeatherEnsemble: archive %s %s observed_high=%.1fF",
                             city_code, date_str, observed)
                return observed
            return None
        except Exception as e:
            logging.warning("WeatherEnsemble: archive fetch %s %s failed: %s",
                            city_code, date_str, e)
            return None

    def _fetch_hrrr(self, lat: float, lon: float, target_date: str) -> Optional[float]:
        """Fetch HRRR deterministic daily high temperature."""
        if time.time() < self._backoff_until:
            return None
        try:
            resp = requests.get(OPEN_METEO_FORECAST_URL, params={
                "latitude": lat,
                "longitude": lon,
                "daily": "temperature_2m_max",
                "models": "ncep_hrrr_conus",
                "temperature_unit": "fahrenheit",
                "start_date": target_date,
                "end_date": target_date,
                "timezone": "America/New_York",
            }, timeout=30)

            if resp.status_code != 200:
                logging.warning("WeatherEnsemble: HRRR fetch HTTP %d", resp.status_code)
                if resp.status_code == 429:
                    self._consecutive_429s += 1
                    backoff_secs = min(60 * (2 ** (self._consecutive_429s - 1)), 900)
                    self._backoff_until = time.time() + backoff_secs
                return None

            data = resp.json()
            if data.get("error"):
                logging.warning("WeatherEnsemble: HRRR API error: %s", data.get("reason", "unknown"))
                return None

            daily = data.get("daily", {})
            temps = daily.get("temperature_2m_max", [])
            if temps and temps[0] is not None:
                return float(temps[0])
            logging.warning("WeatherEnsemble: HRRR returned no temperature data for %s", target_date)
        except Exception as e:
            logging.warning("WeatherEnsemble: HRRR fetch failed: %s", e)
        return None


# ═════════════════════════════════════════════════════════════════════════════
#  Weather Probability Model
# ═════════════════════════════════════════════════════════════════════════════

class WeatherProbabilityModel:
    """Gaussian probability model from NWP ensemble spread.

    Pools GFS + ECMWF members (82 total), fits mean/std,
    applies EWMA bias correction from recent observation errors.
    Bias state is persisted to SQLite so it survives restarts.
    """

    def __init__(self, db_path: Optional[str] = None):
        # Per-city bias correction: EWMA of (actual - forecast) errors
        self._bias: Dict[str, float] = {}
        self._bias_count: Dict[str, int] = {}
        self._db_path = db_path
        self._bias_updated_keys: set = set()  # track (city, date) to deduplicate
        if db_path:
            self._init_bias_table()
            self._load_bias()

    def compute_probability(self, ensemble_data: Dict, threshold_f: float,
                            city_code: str, direction: str = "above",
                            market_type: Optional[str] = None,
                            bracket_bounds: Optional[Tuple[float, float]] = None) -> Optional[Dict]:
        """Compute probability for a weather market from ensemble data.

        Supports three market types:
          - "bracket": P(lower < X < upper) for range/bracket markets (B-prefix)
          - "lower_tail": P(X < threshold) for lower tail markets (T-prefix, low end)
          - "upper_tail": P(X > threshold) for upper tail markets (T-prefix, high end)
          - None: legacy direction-based (above/below)

        Args:
            ensemble_data: Output from WeatherEnsembleFetcher.fetch_ensemble()
            threshold_f: Temperature threshold in Fahrenheit
            city_code: City code for bias correction lookup
            direction: "above" or "below" (legacy, used when market_type is None)
            market_type: "bracket", "lower_tail", "upper_tail", or None
            bracket_bounds: (lower, upper) for bracket markets

        Returns dict with raw_prob, calibrated_prob, ensemble_mean, ensemble_std, etc.
        """
        members = ensemble_data.get("combined_members", [])
        if len(members) < 5:
            return None

        # Fit Gaussian to ensemble
        mean = sum(members) / len(members)
        variance = sum((m - mean) ** 2 for m in members) / len(members)
        raw_std = math.sqrt(max(variance, 0.01))  # floor at 0.01F to avoid division by zero

        # Apply per-city std correction (ensemble spread underestimates true uncertainty)
        std_correction = WEATHER_STD_CORRECTION.get(
            city_code, WEATHER_STD_CORRECTION_DEFAULT)
        std = raw_std * std_correction

        # Apply bias correction — only when city has enough settled signals
        # to produce a meaningful correction. Below threshold, leave uncorrected.
        _bias_count = self._bias_count.get(city_code, 0)
        if _bias_count >= BIAS_MIN_SIGNALS:
            bias = self._bias.get(city_code, 0.0)
        else:
            bias = 0.0
        corrected_mean = mean + bias

        # Compute probability based on market type
        if market_type == "bracket" and bracket_bounds:
            lower, upper = bracket_bounds
            z_lower = (lower - corrected_mean) / std
            z_upper = (upper - corrected_mean) / std
            raw_prob = self._normal_cdf(z_upper) - self._normal_cdf(z_lower)
        elif market_type == "lower_tail":
            z = (threshold_f - corrected_mean) / std
            raw_prob = self._normal_cdf(z)
        elif market_type == "upper_tail":
            z = (threshold_f - corrected_mean) / std
            raw_prob = 1.0 - self._normal_cdf(z)
        else:
            # Legacy: direction-based
            z = (threshold_f - corrected_mean) / std
            raw_prob = 1.0 - self._normal_cdf(z)
            if direction == "below":
                raw_prob = 1.0 - raw_prob

        # Clamp
        raw_prob = max(0.001, min(0.999, raw_prob))

        # Also compute uncorrected probability for comparison logging
        if market_type == "bracket" and bracket_bounds:
            lower, upper = bracket_bounds
            z_lower_uc = (lower - corrected_mean) / raw_std
            z_upper_uc = (upper - corrected_mean) / raw_std
            uncorrected_prob = self._normal_cdf(z_upper_uc) - self._normal_cdf(z_lower_uc)
        elif market_type == "lower_tail":
            z_uc = (threshold_f - corrected_mean) / raw_std
            uncorrected_prob = self._normal_cdf(z_uc)
        elif market_type == "upper_tail":
            z_uc = (threshold_f - corrected_mean) / raw_std
            uncorrected_prob = 1.0 - self._normal_cdf(z_uc)
        else:
            z_uc = (threshold_f - corrected_mean) / raw_std
            uncorrected_prob = 1.0 - self._normal_cdf(z_uc)
            if direction == "below":
                uncorrected_prob = 1.0 - uncorrected_prob
        uncorrected_prob = max(0.001, min(0.999, uncorrected_prob))

        return {
            "raw_prob": raw_prob,
            "calibrated_prob": raw_prob,  # CalEngine may override downstream
            "uncorrected_prob": uncorrected_prob,  # prob without std correction
            "ensemble_mean": round(mean, 1),
            "ensemble_std": round(std, 2),         # corrected std
            "raw_ensemble_std": round(raw_std, 2),  # original ensemble spread
            "std_correction_factor": std_correction,
            "bias_correction": round(bias, 2),
            "bias_count": _bias_count,  # how many updates learned; applied only when >= BIAS_MIN_SIGNALS
            "corrected_mean": round(corrected_mean, 1),
            "n_members": len(members),
            "hrrr_temp": ensemble_data.get("hrrr_temp"),
            "threshold": threshold_f,
            "market_type": market_type or direction,
        }

    def update_bias(self, city_code: str, actual_high: float, forecast_mean: float,
                    market_date: Optional[str] = None):
        """Update EWMA bias correction with an observation.

        Call after actual daily high is observed (typically next day).
        Deduplicates by (city, market_date) to avoid multiple updates per day.
        """
        # Deduplicate: only one bias update per city per market date
        if market_date:
            dedup_key = (city_code, market_date)
            if dedup_key in self._bias_updated_keys:
                return
            self._bias_updated_keys.add(dedup_key)

        error = actual_high - forecast_mean
        if city_code in self._bias:
            self._bias[city_code] = (
                BIAS_EWMA_LAMBDA * self._bias[city_code]
                + (1 - BIAS_EWMA_LAMBDA) * error
            )
            self._bias_count[city_code] = self._bias_count.get(city_code, 0) + 1
        else:
            self._bias[city_code] = error
            self._bias_count[city_code] = 1

        logging.info("WeatherProbabilityModel: %s bias updated to %.2fF (n=%d)",
                     city_code, self._bias[city_code], self._bias_count[city_code])
        self._save_bias(city_code)

    def _init_bias_table(self):
        """Create weather_bias table if it doesn't exist."""
        if not self._db_path:
            return
        try:
            conn = sqlite3.connect(self._db_path)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS weather_bias (
                    city_code TEXT PRIMARY KEY,
                    bias_value REAL NOT NULL,
                    bias_count INTEGER NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            conn.commit()
            conn.close()
        except Exception as e:
            logging.warning("WeatherProbabilityModel: failed to init bias table: %s", e)

    def _load_bias(self):
        """Load persisted bias corrections from SQLite on startup."""
        if not self._db_path:
            return
        try:
            conn = sqlite3.connect(self._db_path)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
            rows = conn.execute(
                "SELECT city_code, bias_value, bias_count FROM weather_bias"
            ).fetchall()
            conn.close()
            for city_code, bias_value, bias_count in rows:
                self._bias[city_code] = bias_value
                self._bias_count[city_code] = bias_count
            if rows:
                logging.info("WeatherProbabilityModel: loaded bias for %d cities from DB", len(rows))
        except Exception as e:
            logging.warning("WeatherProbabilityModel: failed to load bias: %s", e)

    def _save_bias(self, city_code: str):
        """Persist a single city's bias correction to SQLite."""
        if not self._db_path:
            return
        try:
          with tracked_write("weather_engine", f"save_bias_{city_code}"):  # ops: db-locked RCA 2026-05-08
            conn = sqlite3.connect(self._db_path)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=10000")
            conn.execute(
                "INSERT OR REPLACE INTO weather_bias (city_code, bias_value, bias_count, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (city_code, self._bias[city_code], self._bias_count[city_code],
                 datetime.datetime.now(timezone.utc).isoformat())
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logging.warning("WeatherProbabilityModel: failed to save bias for %s: %s", city_code, e)

    @staticmethod
    def _normal_cdf(z: float) -> float:
        """Standard normal CDF approximation. Accurate to ~1e-7."""
        # Using math.erfc for precision
        return 0.5 * math.erfc(-z / math.sqrt(2))


# ═════════════════════════════════════════════════════════════════════════════
#  Weather Window Discovery
# ═════════════════════════════════════════════════════════════════════════════

class WeatherWindowDiscovery:
    """Queries Kalshi for weather market events."""

    @staticmethod
    def discover_windows(client, cities: Optional[List[str]] = None) -> List[Dict]:
        """Query Kalshi for open weather events across cities.

        Returns windows with product_type='weather', asset='{city}_TEMP'.
        Weather markets settle daily — scan once per hour.
        """
        if cities is None:
            cities = list(WEATHER_CITIES.keys())

        now = datetime.datetime.now(timezone.utc)
        windows = []

        for city_code in cities[:MAX_CITIES_PER_DAY]:
            city = WEATHER_CITIES.get(city_code)
            if not city:
                continue

            series_ticker = city["series_ticker"]
            try:
                result = client.get_events(
                    series_ticker=series_ticker,
                    status="open",
                    with_nested_markets=True,
                    limit=10,
                )
                events = result.get("events") if result else None
                if not events:
                    continue

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
                        "asset": f"{city_code}_TEMP",
                        "event_ticker": event_ticker,
                        "close_time": close_time,
                        "seconds_to_close": seconds_to_close,
                        "markets": mkts,
                        "product_type": "weather",
                        "city_code": city_code,
                    })

            except Exception as e:
                logging.warning("WeatherWindowDiscovery: %s (%s) failed: %s",
                                city_code, series_ticker, e)

        if windows:
            mkt_count = sum(len(w["markets"]) for w in windows)
            logging.info("WeatherEngine: %d markets across %d windows (%d cities)",
                         mkt_count, len(windows), len(set(w["city_code"] for w in windows)))

        return windows


# ═════════════════════════════════════════════════════════════════════════════
#  Weather Engine Facade
# ═════════════════════════════════════════════════════════════════════════════

class WeatherEngine:
    """Facade for weather components. Conforms to the expansion engine interface.

    Interface:
      - get_active_windows(client) -> List[Dict]
      - get_spot_price(asset) -> float  (returns ensemble mean temperature)
      - get_vol_estimate(asset, stc) -> Dict (returns ensemble_std as volatility proxy)
      - start() / stop()
    """

    def __init__(self, db_path: Optional[str] = None):
        self._fetcher = WeatherEnsembleFetcher()
        self._model = WeatherProbabilityModel(db_path=db_path)
        self._discovery = WeatherWindowDiscovery()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_ensemble: Dict[str, Dict] = {}  # city_code -> latest ensemble data
        self._last_ensemble_saved_at: float = 0.0  # unix time of last cache save
        self._started = False
        # Cache lives at REPO ROOT (NOT next to __file__) — pre-Sprint-10.1c
        # weather_engine.py was at repo root so `__file__` resolved there. The
        # 10.1c relocation to bot/engines/weather_engine.py would have silently
        # moved this path to bot/engines/weather_ensemble_cache.json, orphaning
        # the existing 21KB warm cache and cold-starting weather data on first
        # deploy restart. Anchor explicitly to the repo root (2 levels up from
        # bot/engines/) so the cache file location is invariant under the move.
        # Pinned by tests/integration/test_sprint_10_1c_weather_engine_move.py::test_weather_ensemble_cache_path_is_repo_root.
        _REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self._cache_path = os.path.join(_REPO_ROOT, WEATHER_ENSEMBLE_CACHE_FILE)
        # Warm-start from on-disk cache (weather changes slowly, stale OK)
        self._load_ensemble_cache()

    # ── Ensemble cache persistence ────────────────────────────────────────
    def _load_ensemble_cache(self) -> None:
        """Load previous ensemble data from disk on startup.

        Weather ensembles are useful for hours after fetch (forecasts target
        end-of-day high temperature, not instantaneous). If Open-Meteo is
        429-throttling new fetches, stale-but-warm is infinitely better than
        fully dark.
        """
        try:
            if not os.path.exists(self._cache_path):
                logging.info("WeatherEngine: no ensemble cache found (first run)")
                return
            with open(self._cache_path) as f:
                data = json.load(f)
            saved_at = float(data.get("saved_at", 0))
            ensembles = data.get("ensembles", {}) or {}
            if not ensembles:
                logging.info("WeatherEngine: ensemble cache is empty")
                return
            age_min = (time.time() - saved_at) / 60.0 if saved_at else -1
            self._last_ensemble = ensembles
            self._last_ensemble_saved_at = saved_at
            logging.info(
                "WeatherEngine: warm-started from cache — %d cities, %.1f min old",
                len(ensembles), age_min)
            if age_min > 360:  # 6h
                logging.warning(
                    "WeatherEngine: cache is %.1fh old — using until fresh fetches succeed",
                    age_min / 60.0)
        except Exception as e:
            logging.warning("WeatherEngine: failed to load ensemble cache: %s", e)

    def _save_ensemble_cache(self) -> None:
        """Persist current ensemble data to disk atomically."""
        try:
            if not self._last_ensemble:
                return  # nothing to save
            data = {
                "saved_at": time.time(),
                "ensembles": self._last_ensemble,
            }
            tmp_path = self._cache_path + ".tmp"
            with open(tmp_path, "w") as f:
                json.dump(data, f, default=str)
            os.replace(tmp_path, self._cache_path)
            self._last_ensemble_saved_at = data["saved_at"]
        except Exception as e:
            logging.warning("WeatherEngine: failed to save ensemble cache: %s", e)

    def start(self):
        """Start background fetcher thread.

        Skips API self-test when the cache is warm — self-test burns 3 extra
        API calls that contribute to the cold-start 429 cascade. A populated
        cache is sufficient proof that the model names worked recently.
        """
        if self._last_ensemble:
            logging.info(
                "WeatherEngine: skipping API self-test (warm cache: %d cities)",
                len(self._last_ensemble))
        else:
            self._self_test_apis()
        self._thread = threading.Thread(target=self._fetch_loop, daemon=True)
        self._thread.start()
        self._started = True
        logging.info("WeatherEngine: started")

    def _self_test_apis(self):
        """Validate that all Open-Meteo API model names return real data.

        Runs one test call per model at startup. Logs WARNING for each failure
        so broken integrations are immediately visible in production logs.
        """
        test_lat, test_lon = 40.7128, -74.0060  # NYC
        test_date = datetime.datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Test ensemble models (GFS, ECMWF)
        for model in ("gfs_seamless", "ecmwf_ifs025"):
            members = self._fetcher._fetch_model_ensemble(test_lat, test_lon, model, test_date)
            if members and len(members) > 0:
                logging.info("WeatherEngine self-test: %s OK (%d members)", model, len(members))
            else:
                logging.warning("WeatherEngine self-test: %s FAILED — returned no members. "
                                "Check model name against Open-Meteo docs.", model)
            time.sleep(1.0)  # Rate-limit between test calls

        # Test HRRR deterministic
        hrrr = self._fetcher._fetch_hrrr(test_lat, test_lon, test_date)
        if hrrr is not None:
            logging.info("WeatherEngine self-test: ncep_hrrr_conus OK (%.1fF)", hrrr)
        else:
            logging.warning("WeatherEngine self-test: ncep_hrrr_conus FAILED — returned no data. "
                            "Check model name against Open-Meteo docs.")

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self._started = False
        logging.info("WeatherEngine: stopped")

    def get_active_windows(self, client) -> List[Dict]:
        return self._discovery.discover_windows(client)

    def get_spot_price(self, asset: str) -> Optional[float]:
        """Return current ensemble mean temperature for the city.

        asset format: '{CITY}_TEMP' (e.g., 'NYC_TEMP')
        """
        city_code = asset.replace("_TEMP", "")
        ensemble = self._last_ensemble.get(city_code)
        if not ensemble:
            return None
        members = ensemble.get("combined_members", [])
        if not members:
            return None
        return sum(members) / len(members)

    def get_vol_estimate(self, asset: str, stc: float) -> Optional[Dict]:
        """Return ensemble standard deviation as volatility proxy.

        For weather, 'volatility' is the ensemble spread (forecast uncertainty).
        Normalized as std/mean to be dimensionless like crypto RV.
        """
        city_code = asset.replace("_TEMP", "")
        ensemble = self._last_ensemble.get(city_code)
        if not ensemble:
            return None

        members = ensemble.get("combined_members", [])
        if len(members) < 5:
            return None

        mean = sum(members) / len(members)
        variance = sum((m - mean) ** 2 for m in members) / len(members)
        std = math.sqrt(max(variance, 0.01))

        # Normalized vol (std / mean, keeping it dimensionless)
        blended_rv = std / max(abs(mean), 1.0)

        return {
            "blended_rv": blended_rv,
            "regime": "normal",  # weather doesn't have vol regimes
            "egarch_sigma": None,
            "egarch_blend_weight": None,
            "mz_r_squared": None,
            "ensemble_mean": mean,
            "ensemble_std": std,
            "n_members": len(members),
            "hrrr_temp": ensemble.get("hrrr_temp"),
        }

    def get_probability(self, city_code: str, threshold_f: float,
                        direction: str = "above",
                        market_type: Optional[str] = None,
                        bracket_bounds: Optional[Tuple[float, float]] = None) -> Optional[Dict]:
        """Compute probability for a weather threshold.

        This is the weather-specific probability computation (not via ProbabilityEngine).
        Supports bracket, lower_tail, and upper_tail market types.
        """
        ensemble = self._last_ensemble.get(city_code)
        if not ensemble:
            # Try fetching now
            ensemble = self._fetcher.fetch_ensemble(city_code)
            if ensemble:
                self._last_ensemble[city_code] = ensemble

        if not ensemble:
            return None

        return self._model.compute_probability(
            ensemble, threshold_f, city_code, direction,
            market_type=market_type, bracket_bounds=bracket_bounds)

    def _fetch_loop(self):
        """Background loop: refresh ensemble data for all cities every 15 minutes."""
        _watchdog_cycles_empty = 0
        while not self._stop.is_set():
            _any_new_fetch = False
            for city_code in WEATHER_CITIES:
                try:
                    ensemble = self._fetcher.fetch_ensemble(city_code)
                    if ensemble and ensemble.get("combined_members"):
                        self._last_ensemble[city_code] = ensemble
                        _any_new_fetch = True
                except Exception as e:
                    logging.warning("WeatherEngine: fetch for %s failed: %s", city_code, e)
                # Rate-limit: 19 cities × 3 API calls each = 57 calls per cycle.
                # Open-Meteo free tier throttles at ~30 req/min.
                # 3s between cities ≈ 1 call/s = well below burst limit.
                # (Was 1.0s — tightened to 3.0s after Apr 11 2026 429 cascade.)
                time.sleep(3.0)

            # Persist the cache after each full cycle if anything was refreshed
            if _any_new_fetch:
                self._save_ensemble_cache()
                _watchdog_cycles_empty = 0
            else:
                _watchdog_cycles_empty += 1

            # Watchdog: if all cities are empty OR we've had no new fetches for
            # multiple full cycles, the weather signal is dark. Log so it shows
            # in status dashboards and post-mortem audits.
            if not self._last_ensemble:
                logging.warning(
                    "WeatherEngine WATCHDOG: 0 cities have ensemble data after fetch cycle "
                    "— weather signal is fully dark")
            elif _watchdog_cycles_empty >= 2:
                _age_min = (time.time() - self._last_ensemble_saved_at) / 60.0 \
                    if self._last_ensemble_saved_at else -1
                logging.warning(
                    "WeatherEngine WATCHDOG: no new fetches for %d cycles "
                    "(~%d min). Using stale cache (%.1f min old).",
                    _watchdog_cycles_empty,
                    _watchdog_cycles_empty * WEATHER_POLL_INTERVAL // 60,
                    _age_min)

            self._stop.wait(WEATHER_POLL_INTERVAL)
