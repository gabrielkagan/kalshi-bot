"""VolatilityEngine — Realized Kernel + Deribit DVOL volatility engine.

Extracted from bot/_impl.py in Sprint 6 Bit 6.1 (2026-05-09). The
class is the first leaf in the Sprint 6 ``bot/engines/`` subpackage
(math-layer engines that consume feeds + fetchers and emit per-asset
volatility estimates). Subsequent bits move ``ProbabilityEngine``
(Bit 6.2) and ``CalibrationEngine`` (Bit 6.3) into sibling submodules.

Realized-Kernel (Barndorff-Nielsen 2008) microstructure-noise-robust
volatility estimator with bipower-variation jump separation, EGARCH
variance-space blending, Deribit DVOL diagnostics (diagnostic-only
since Bit V.4, 2026-06-12 — IV is never blended into sigma),
and a two-tier (legacy + adaptive) jump regime detector. Maintains
its own per-asset rolling buffers of 5-second log returns (up to 15
min) and persists state across restarts via two JSON sidecar files
(``rk_state.json`` for return buffers, ``jump_adaptive_state.json``
for the EWMA-percentile detector).

Imports are deliberate: stdlib (``json``, ``logging``, ``math``,
``os``, ``time``, ``collections.deque``, ``typing``) +
``bot.constants`` (34 explicit names — every RK / JUMP / VOL /
DERIBIT / BETA tunable; the IV stress-override tunable was deleted in Bit V.4) + ``config`` (``ASSETS`` plus the 3
EGARCH_* names that still live in ``bot/config.py`` because the EGARCH
blend predates Bit 3.1 constant-extraction) + ``models``
(``compute_tv_rk_weights``, the time-varying RK-weight schedule
that ships with the EGARCH model family) + sibling
``bot.feeds.coinbase.CoinbaseFeed`` (Bit 4.5a, the ``feed:
CoinbaseFeed`` ``__init__`` parameter annotation that L33 pins) +
sibling ``bot.fetchers.deribit.DeribitDVOLFetcher`` (Bit 4.4, the
``Optional[DeribitDVOLFetcher]`` ``__init__`` parameter annotation
that L33 pins). Does NOT import ``bot._impl`` (would create a
circular import — ``_impl`` re-exports this class via
``from bot.engines import VolatilityEngine``). The
``EGARCHEstimator`` and ``MincerZarnowitzTracker`` constructor
annotations remain string-quoted forward refs because both classes
still live in ``models.py`` and importing them eagerly would couple
this leaf to the full models module.

Construction site: ``MainLoop.__init__`` in ``bot/_impl.py`` does
``self.vol = VolatilityEngine(self.feed, dvol_fetcher=self.dvol_fetcher,
egarch_estimator=self.egarch_estimator, mz_tracker=self.mz_tracker)``.
Downstream consumer: ``OpportunityScanner.__init__`` accepts
``vol: VolatilityEngine`` and calls ``self.vol.update(asset, …)``
once per scan tick. The static methods ``_parzen_kernel``,
``_estimate_noise_variance``, ``_realized_quarticity``,
``_optimal_rk_bandwidth``, ``_realized_kernel``, and
``_bipower_variation`` are exercised directly by
``tests/integration/test_vol_engine.py`` via ``from bot.engines.volatility import
VolatilityEngine`` (post-Bit-9.3-iii.b, 2026-05-11; the ``_BotProxy`` chain
that previously routed ``from bot import VolatilityEngine`` through
``bot._impl.VolatilityEngine → bot.engines.VolatilityEngine →
bot.engines.volatility.VolatilityEngine`` is retired).
"""

import json
import logging
import math
import os
import time
from collections import deque
from typing import Dict, List, Optional, Tuple

from bot.constants import (
    BETA_LOOKBACK_RETURNS,
    DERIBIT_DVOL_CURRENCIES,
    JUMP_ADAPTIVE_DECAY_CAP,
    JUMP_ADAPTIVE_DECAY_MAX_BOOST,
    JUMP_ADAPTIVE_DECAY_MIN_BOOST,
    JUMP_ADAPTIVE_DECAY_TAU,
    JUMP_ADAPTIVE_EWMA_INIT_RETURNS,
    JUMP_ADAPTIVE_EWMA_LAMBDA,
    JUMP_ADAPTIVE_MAG_CAP,
    JUMP_ADAPTIVE_MAG_SCALE_BASE,
    JUMP_ADAPTIVE_MAX_HISTORY,
    JUMP_ADAPTIVE_PCTILE_LEVEL,
    JUMP_ADAPTIVE_PCTILE_MIN_OBS,
    JUMP_ADAPTIVE_PCTILE_WINDOW,
    JUMP_ADAPTIVE_SAVE_INTERVAL,
    JUMP_ADAPTIVE_SHADOW_MODE,
    JUMP_ADAPTIVE_SIGMA_MULT,
    JUMP_ADAPTIVE_STATE_PATH,
    JUMP_ADAPTIVE_SUBSAMPLE,
    JUMP_DECAY_MAX_BOOST,
    JUMP_DECAY_MIN_BOOST,
    JUMP_DECAY_TAU,
    JUMP_MAX_HISTORY,
    JUMP_THRESHOLD_MULTIPLIER,
    RK_ADAPTIVE_SHADOW_MODE,
    RK_BANDWIDTH_MAX_FRACTION,
    RK_CSTAR_FLAT_TOP_PARZEN,
    RK_MIN_RETURNS_FOR_ADAPTIVE,
    RK_NOISE_VAR_FLOOR,
    RK_TV_SHADOW_MODE,
    VOL_BLEND_WEIGHTS,
    VOL_WINDOW_15MIN,
    VOL_WINDOW_1MIN,
    VOL_WINDOW_5MIN,
)
from bot.config import (
    ASSETS,
    EGARCH_BLEND_LOG_INTERVAL,
    EGARCH_BLEND_SHADOW_MODE,
    EGARCH_RV_RATIO_CLAMP,
    VOL_RETURN_INTERVAL,
)
from bot.models import compute_tv_rk_weights

from bot.feeds.coinbase import CoinbaseFeed
from bot.fetchers.deribit import DeribitDVOLFetcher


class VolatilityEngine:
    """Realized Kernel + Deribit DVOL volatility engine.

    Uses microstructure-noise-robust Realized Kernel (Barndorff-Nielsen 2008),
    bipower variation for jump separation; Deribit DVOL is diagnostic-only
    (Bit V.4, 2026-06-12 — never blended into sigma).
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
        self._rk_last_save: float = 0.0
        self._load_rk_state()

    # ── RK state persistence (prevents cold-start vol underestimation) ──

    RK_STATE_PATH = "rk_state.json"
    RK_SAVE_INTERVAL = 60.0  # seconds between saves

    def _load_rk_state(self):
        """Load RK return buffers from disk. Restores blended vol instantly on restart."""
        if not os.path.exists(self.RK_STATE_PATH):
            logging.info("RK state: no file, starting cold")
            return
        try:
            with open(self.RK_STATE_PATH, "r") as f:
                state = json.load(f)
            loaded = 0
            for asset in ASSETS:
                returns = state.get(asset, {}).get("returns", [])
                if returns:
                    self._returns[asset].clear()
                    for r in returns:
                        self._returns[asset].append(r)
                    loaded += 1
            age = time.time() - state.get("saved_at", 0)
            logging.info("RK state loaded: %d assets restored, age=%.0fs (%s returns)",
                         loaded, age,
                         ", ".join(f"{a}={len(self._returns[a])}" for a in ASSETS))
        except Exception as e:
            logging.warning("RK state load failed: %s (starting cold)", e)

    def save_rk_state(self):
        """Persist RK return buffers to disk (atomic write)."""
        state = {"saved_at": time.time()}
        for asset in ASSETS:
            state[asset] = {"returns": list(self._returns[asset])}
        tmp = self.RK_STATE_PATH + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(state, f)
            os.replace(tmp, self.RK_STATE_PATH)
        except Exception as e:
            logging.warning("RK state save failed: %s", e)

    def _maybe_save_rk_state(self):
        """Throttled periodic save."""
        now = time.time()
        if now - self._rk_last_save >= self.RK_SAVE_INTERVAL:
            self.save_rk_state()
            self._rk_last_save = now

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
                self._maybe_save_rk_state()

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
                "Adaptive jump state saved: %s obs (file_size=%.1fKB)",
                ", ".join(f"{a}={len(self._adaptive_returns_15s[a])}" for a in ASSETS),
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
            "Adaptive jump state restored: %s obs, ewma_age=%.0fs",
            ", ".join(f"{a}={len(self._adaptive_returns_15s[a])}" for a in ASSETS),
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
        """Cross-asset beta: cov(r_asset, r_ref) / var(r_ref). Clamped [0.5, 3.0].

        DEAD CODE as of Bit V.2 (2026-06-12) — no production caller.
        Its only consumers were the beta×BTC-DVOL fabrication paths in
        ``_get_implied_vol`` / ``_get_implied_vol_hourly``, killed in Bit
        V.2. The estimator is unsalvageable for vol scaling: regression
        beta = corr×(σa/σb) understates the VOL RATIO at 5s horizons
        (Epps effect), the per-asset deques are appended at scan cadence
        with jitter so index-alignment ≠ time-alignment (covariance → 0),
        and the 0.5 clamp floor produced the cross-asset-identical
        ~8.5e-5 blended_rv cluster observed live on 2026-06-12.
        Postmortem: kb/failures/vol-engine-beta-dvol-deflation-jun12.md
        (local-only KB). Deletion is a follow-up Bit; kept here so the
        Bit V.2 diff stays reviewable.
        """
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
        """Get implied vol in per-5-second scale. BTC/ETH direct; None otherwise.

        Bit V.2 (2026-06-12): assets outside DERIBIT_DVOL_CURRENCIES
        have NO implied vol — the former ``btc_dvol × _estimate_beta``
        fabrication deflated alt vol up to ~6x (cross-asset-identical
        ~8.5e-5 cluster against a ~3e-4 tape) and the inverse-variance
        blend then locked onto the deflated value quadratically. Alts
        return None and take the existing rv/EGARCH fallback path.
        """
        if self._dvol is None:
            return None

        if asset in DERIBIT_DVOL_CURRENCIES:
            return self._dvol.get_dvol(asset)

        # Bit V.2: no beta-scaled fabrication for alts.
        return None

    def _get_implied_vol_hourly(self, asset: str) -> Optional[float]:
        """Get hourly-averaged implied vol in per-5-second scale. BTC/ETH direct; None otherwise.

        Bit V.2 (2026-06-12): same kill as ``_get_implied_vol`` — no
        beta-scaled BTC-DVOL fabrication for assets outside
        DERIBIT_DVOL_CURRENCIES.
        """
        if self._dvol is None:
            return None

        if asset in DERIBIT_DVOL_CURRENCIES:
            return self._dvol.get_dvol_hourly_avg(asset)

        # Bit V.2: no beta-scaled fabrication for alts.
        return None

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

        # Step 4: DVOL diagnostics (Bit V.4, 2026-06-12) — IV is NEVER
        # blended into sigma. The former `inverse_variance` branch was
        # dimensionally incoherent (w_iv rose with the RK term-structure
        # slope, i.e. exactly when realized vol was moving) and measured
        # QLIKE-negative on BTC/ETH journal counterfactuals; the former
        # `stress_override` branch (0.3·rv + 0.7·iv on (iv−rv)/rv > 0.5)
        # was a level-spread trigger that variance-risk-premium + the
        # diurnal trough satisfy every quiet evening — it fired on
        # 60-91% of BTC/ETH ticks through the 2026-06-12 soak night and
        # breached the vol-honesty band 1.8-2.7x. BTC/ETH now take the
        # same RV/EGARCH path as every other asset. The diagnostics
        # below stay live so a future event term can be fitted from
        # ΔDVOL shadow history; the contract is pinned by
        # tests/integration/test_vol_engine_iv_diagnostic_only_regression.py.
        iv = self._get_implied_vol(asset)
        dvol_5s = iv  # for diagnostics
        iv_rv_spread = None
        iv_rv_blend_method = "rv_only"

        if iv is not None and iv > 0 and rv_blended > 0:
            iv_rv_spread = (iv - rv_blended) / rv_blended

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
            "egarch_constrained_sigma": self._egarch.get_constrained_sigma(asset) if self._egarch else None,
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
