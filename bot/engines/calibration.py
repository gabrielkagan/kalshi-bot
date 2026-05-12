"""CalibrationEngine — data-driven 3-method calibrator for raw → cal probabilities.

Extracted from bot/_impl.py in Sprint 6 Bit 6.3 (2026-05-10). Third leaf
in the Sprint 6 ``bot/engines/`` subpackage; sibling of
``bot.engines.volatility.VolatilityEngine`` (Bit 6.1) and
``bot.engines.probability.ProbabilityEngine`` (Bit 6.2). The class body
is a **byte-for-byte** transplant of the 29-method class — there is no
late-binding deviation in the class itself (free-variable analysis
returned zero suspect free-name hits; the class never reads
``_CALIBRATION_ENGINE``, ``_CAL_REGISTRY``, or ``_resolve_cal_engine``).

**Bit 6.3 path-B refactor** ALSO relocated the calibration runtime
state from ``bot/_impl.py`` into THIS module, alongside the class:
``_CALIBRATION_ENGINE: Optional[CalibrationEngine]`` (singleton —
bare-typed since the class is in scope here, no quoted forward ref
needed; pre-Bit-6.3 this lived in bot/_impl.py with a quoted
``Optional['CalibrationEngine']`` annotation), ``_CAL_REGISTRY: Dict[
str, CalibrationEngine]`` (registry dict), and the helpers
``_derive_subtype`` / ``_derive_asset_filter`` / ``_resolve_cal_engine``.
The singleton is mutated by ``MainLoop.__init__`` (3 instantiation
sites — search anchor ``self.calibration = CalibrationEngine()``) and
read by callers OUTSIDE this class — search anchors
``_cal_state._CALIBRATION_ENGINE`` (singleton reads) and
``_cal_state._resolve_cal_engine`` (resolver calls) in
``bot/_impl.py``. Both ``bot/_impl.py`` and ``bot/engines/probability.py``
reach the relocated names via top-level
``from bot.engines import calibration as _cal_state``; module-attribute
access preserves singleton-mutation freshness without late-binding.
The path-B move LIFTED the Bit 6.2 ``from bot import _impl as
_bot_impl`` late-binding inside ProbabilityEngine and REMOVED the
matching ``.importlinter`` ``bot.engines.probability -> bot._impl``
ignore_imports carve-out (Pillar 2).

Implements three calibration methods plus an STC-aware Platt and a
shadow temperature-scaling pipeline:

- **Platt Scaling**: 2-parameter logistic (A, B) — default, needs ≥200
  samples.
- **Beta Calibration**: 3-parameter (a, b, c) — needs ≥500 samples.
- **Online BLR**: Bayesian linear regression with Laplace approximation
  — needs ≥50 samples.
- **STC-aware Platt**: 3-parameter (A, B, C) where C scales by
  ``log(STC/300)`` so the effective slope shrinks at high TTC.
- **Temperature scaling** (shadow pipeline): 1-parameter, fits T>0
  monotonic; gradient-free grid search.

Until enough data is collected, ``calibrate()`` falls back to
``CalibrationEngine._fallback_calibrate`` (identical to
``ProbabilityEngine._calibrate`` — fixed β=0.85 logistic compression
then hard cap). ``backtest_adaptive_vs_fixed`` is the held-out Brier
diff harness used by the cal_mlp tooling.

The cal_mlp feature-transform lock-step rule (``scripts/cal_mlp/extract_data.py`` /
``post_hoc_processor.py`` / ``integration.py`` /
``features.py``; canonical helper ``bot/helpers/derived_features.py``)
operates entirely within ``scripts/cal_mlp/`` (plus the helper home)
and is **NOT** affected by this extraction —
CalibrationEngine has no feature-derivation code (no
``SIGMA_WINSOR_ABS_CAP``, no ``hour_sin``/``hour_cos``, no
``prob_breakeven_gap``, no sigma derivation). Features go INTO this
class via ``add_observation()``/``load_training_data_from_db()`` from
upstream consumers. Regression seal: ``tests/integration/test_calmlp_*`` (12 files)
pass post-extraction without retargets.

Imports are deliberate: stdlib (``math``, ``json``, ``os``, ``time``,
``datetime``, ``timezone``, ``deque``, ``Optional``, ``Dict``,
``logging``) + ``bot.constants`` (10 names — ``CALIBRATION_BRIER_WINDOW``,
``CALIBRATION_MIN_SAMPLES_BETA``, ``CALIBRATION_MIN_SAMPLES_BLR``,
``CALIBRATION_MIN_SAMPLES_PLATT``, ``CALIBRATION_RETRAIN_INTERVAL``,
``CALIBRATION_STATE_PATH``, ``MARKET_BLEND_W``, ``MIN_EDGE_PCT``,
``SHADOW_BLEND_W``, ``SHADOW_CAL_PIPELINE``) + ``config`` (3 names —
``BETA_SLOPE``, ``MAX_EFFECTIVE_PROB``, ``NUMERICAL_SAFETY_CEILING``;
the L39 partition was the Plan-agent CRITICAL catch in pre-flight) +
``market_config`` (``get_cal_excluded_types``) + ``models``
(``calculate_taker_fee``). Strict ban: numpy / scipy / torch / sklearn
/ pandas (none used). The forbidden-imports gate in
``tests/contracts/test_engines_extraction.py`` enforces this.

The ``state: "StateManager"`` annotation on
``load_training_data_from_db`` is a **string-quoted forward ref**;
keeping it quoted avoids a circular import (``bot.engines.calibration``
→ ``bot._impl`` → ``bot.engines`` → back). Body uses ``state.conn`` via
duck typing.

Construction sites: ``MainLoop.__init__`` instantiates
CalibrationEngine three times — once for the legacy 15M
``_CALIBRATION_ENGINE`` singleton, and twice in a loop populating the
``_CAL_REGISTRY`` dict for non-15M product types. Post-Bit-6.3 path-B,
both the singleton and the registry dict live alongside the class in
THIS module (search anchor: ``# ─── Calibration runtime state (Bit 6.3
path-B refactor`` near the bottom of the file); ``MainLoop.__init__``
mutates them via ``_cal_state._CALIBRATION_ENGINE = self.calibration``
and ``_cal_state._CAL_REGISTRY[reg_key] = engine`` (where
``_cal_state`` is the ``from bot.engines import calibration as
_cal_state`` alias at the top of ``bot/_impl.py``).
"""

import datetime
import json
import logging
import math
import os
import time
from collections import deque
from datetime import timezone
from typing import Dict, Optional

from bot.constants import (
    CALIBRATION_BRIER_WINDOW,
    CALIBRATION_MIN_SAMPLES_BETA,
    CALIBRATION_MIN_SAMPLES_BLR,
    CALIBRATION_MIN_SAMPLES_PLATT,
    CALIBRATION_RETRAIN_INTERVAL,
    CALIBRATION_STATE_PATH,
    MARKET_BLEND_W,
    MIN_EDGE_PCT,
    SHADOW_BLEND_W,
    SHADOW_CAL_PIPELINE,
)
from config import (
    BETA_SLOPE,
    MAX_EFFECTIVE_PROB,
    NUMERICAL_SAFETY_CEILING,
)
from market_config import get_cal_excluded_types, get_market_config
from bot.models import calculate_taker_fee


class CalibrationEngine:
    """Data-driven calibration replacing fixed β=0.85 Platt scaling.

    Implements three calibration methods:
    - Platt Scaling: 2-parameter logistic (A, B) — default, needs 200+ samples
    - Beta Calibration: 3-parameter (a, b, c) — needs 500+ samples
    - Online BLR: Bayesian linear regression with Laplace approx — needs 50+ samples

    Until enough data is collected, falls back to the existing fixed β=0.85.
    """

    def __init__(self, state_path: str = CALIBRATION_STATE_PATH,
                 label: str = "CalibrationEngine",
                 accepted_stages: Optional[tuple] = None):
        self.state_path = state_path
        self._label = label
        self._accepted_stages = accepted_stages  # None = accept all stages
        self.active_method: str = "fixed_beta"  # current method in use
        self._observations: deque = deque(maxlen=500)  # (raw_prob, binary_outcome)
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

        # STC-aware Platt: sigmoid(A * logit(p) + B + C * log(STC/300))
        self._stc_platt_A: float = BETA_SLOPE
        self._stc_platt_B: float = 0.0
        self._stc_platt_C: float = 0.0  # STC coefficient (negative = reduce prob at high STC)
        self._stc_platt_trained: bool = False

        # Previous Brier score for regression check
        self._prev_brier: Optional[float] = None

        # Temperature scaling (shadow pipeline)
        self._temperature: Optional[float] = None
        self._temperature_brier: Optional[float] = None

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

            if "temperature" in state:
                self._temperature = state["temperature"].get("value")
                self._temperature_brier = state["temperature"].get("brier")

            if "stc_platt" in state:
                self._stc_platt_A = state["stc_platt"].get("A", BETA_SLOPE)
                self._stc_platt_B = state["stc_platt"].get("B", 0.0)
                self._stc_platt_C = state["stc_platt"].get("C", 0.0)
                self._stc_platt_trained = state["stc_platt"].get("trained", False)

            # observations are loaded from DB in load_training_data_from_db()

            if "prev_brier" in state:
                self._prev_brier = state["prev_brier"]

            logging.info(
                "%s loaded: method=%s, observations=%d, "
                "platt_trained=%s, beta_trained=%s, blr_trained=%s",
                self._label, self.active_method, len(self._observations),
                self._platt_trained, self._beta_trained, self._blr_trained,
            )
        except FileNotFoundError:
            logging.info("%s: No state found, starting fresh (fixed_beta fallback)", self._label)
        except Exception as e:
            logging.warning("%s: Error loading state: %s", self._label, e)

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
            "temperature": {
                "value": self._temperature,
                "brier": self._temperature_brier,
            },
            "stc_platt": {
                "A": self._stc_platt_A,
                "B": self._stc_platt_B,
                "C": self._stc_platt_C,
                "trained": self._stc_platt_trained,
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
            logging.warning("%s: failed to save state: %s", self._label, e)

    # ── Inference ──────────────────────────────────────────────────────────

    def calibrate(self, raw_prob: float, cap: float,
                  seconds_to_close: Optional[float] = None) -> float:
        """Calibrate raw_prob using the active method. Sub-ms, called per evaluation."""
        if self.active_method == "stc_platt" and self._stc_platt_trained:
            result = self._stc_platt_predict(raw_prob, seconds_to_close)
        elif self.active_method == "platt" and self._platt_trained:
            result = self._platt_predict(raw_prob)
        elif self.active_method == "beta_cal" and self._beta_trained:
            result = self._beta_cal_predict(raw_prob)
        elif self.active_method == "temperature" and self._temperature is not None:
            result = self._temperature_predict(raw_prob, self._temperature)
        elif self.active_method == "blr" and self._blr_trained:
            result = self._blr_predict(raw_prob)
        else:
            return CalibrationEngine._fallback_calibrate(raw_prob, cap)
        # Learned method active: apply cap, uncertainty shrinkage, safety ceiling
        result = min(result, cap)
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

    # ── Temperature Scaling (Shadow Pipeline) ─────────────────────────────

    def _fit_temperature(self) -> Optional[float]:
        """Fit temperature parameter T minimizing Brier score. T>0, monotonic."""
        if len(self._observations) < CALIBRATION_MIN_SAMPLES_BLR:
            return None
        obs = list(self._observations)
        best_t, best_brier = 1.0, float('inf')
        # Grid search + refinement (fast, <100 evaluations)
        for t_candidate in [x / 100.0 for x in range(50, 200, 5)]:  # 0.50 to 1.95
            brier_sum = 0.0
            for _obs_item in obs:
                raw_p, outcome = _obs_item[0], _obs_item[1]

                p = max(0.001, min(0.999, raw_p))
                logit_p = math.log(p / (1.0 - p))
                pred = 1.0 / (1.0 + math.exp(-logit_p / t_candidate))
                brier_sum += (pred - outcome) ** 2
            avg_brier = brier_sum / len(obs)
            if avg_brier < best_brier:
                best_brier = avg_brier
                best_t = t_candidate
        # Refine around best
        for t_candidate in [best_t + d / 1000.0 for d in range(-50, 51, 5)]:
            if t_candidate <= 0.01:
                continue
            brier_sum = 0.0
            for _obs_item in obs:
                raw_p, outcome = _obs_item[0], _obs_item[1]

                p = max(0.001, min(0.999, raw_p))
                logit_p = math.log(p / (1.0 - p))
                pred = 1.0 / (1.0 + math.exp(-logit_p / t_candidate))
                brier_sum += (pred - outcome) ** 2
            avg_brier = brier_sum / len(obs)
            if avg_brier < best_brier:
                best_brier = avg_brier
                best_t = t_candidate
        return best_t

    def _temperature_predict(self, raw_prob: float, temperature: float) -> float:
        """Apply temperature scaling: sigmoid(logit(p) / T)."""
        p = max(0.001, min(0.999, raw_prob))
        logit_p = math.log(p / (1.0 - p))
        return 1.0 / (1.0 + math.exp(-logit_p / temperature))

    def shadow_calibration_pipeline(self, raw_prob: float, best_ask: int,
                                     seconds_remaining: float,
                                     ofa_adjustment: float = 0.0) -> Optional[Dict]:
        """Compute alternative calibration pipeline in shadow mode.

        Changes vs production:
        1. Temperature scaling instead of Beta Cal
        2. No market-price blending (SHADOW_BLEND_W=0.0)
        3. Excludes cap-era data (handled by _fit_temperature using filtered obs)
        """
        if not SHADOW_CAL_PIPELINE or raw_prob is None:
            return None
        try:
            # Step 1: Temperature scaling (or raw if not fitted)
            if self._temperature is not None:
                cal_prob = self._temperature_predict(raw_prob, self._temperature)
            else:
                cal_prob = raw_prob

            # Apply uncertainty shrinkage (same as production)
            cal_prob = self._apply_uncertainty_shrinkage(cal_prob)
            cal_prob = max(0.001, min(NUMERICAL_SAFETY_CEILING, cal_prob))

            # Step 2: OFA adjustment
            cal_prob = max(0.01, min(NUMERICAL_SAFETY_CEILING, cal_prob + ofa_adjustment))

            # Step 3: No market blend (or reduced blend)
            shadow_final = cal_prob  # SHADOW_BLEND_W = 0.0, no blend

            # Compute edge
            edge = shadow_final - best_ask / 100.0
            est_fee_1c = calculate_taker_fee(1, best_ask)
            fee_edge = edge - est_fee_1c / 100.0

            return {
                "prob": round(shadow_final, 6),
                "cal_prob_pre_blend": round(cal_prob, 6),
                "temperature": self._temperature,
                "temperature_brier": self._temperature_brier,
                "edge": round(edge, 6),
                "fee_edge": round(fee_edge, 6),
                "would_trade": fee_edge >= MIN_EDGE_PCT / 100.0,
                "blend_w": SHADOW_BLEND_W,
                "prod_blend_w": MARKET_BLEND_W,
            }
        except Exception:
            logging.warning("shadow_calibration_pipeline failed", exc_info=True)
            return None

    # ── Training Data Management ───────────────────────────────────────────

    def add_observation(self, raw_prob: float, outcome: int,
                        filter_stage: Optional[str] = None,
                        seconds_to_close: Optional[float] = None):
        """Append a (raw_prob, outcome, stc) triple and update rolling Brier.
        If accepted_stages is configured, silently skip non-accepted stages."""
        if self._accepted_stages and filter_stage and filter_stage not in self._accepted_stages:
            return
        self._observations.append((raw_prob, outcome, seconds_to_close))
        # Update rolling Brier with the *current* calibration prediction
        pred = self.calibrate(raw_prob, cap=1.0, seconds_to_close=seconds_to_close)
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
                    "%s: Platt trained — A=%.4f, B=%.4f, Brier=%.4f, n=%d",
                    self._label, self._platt_A, self._platt_B, trained_methods["platt"], n,
                )
            except Exception as e:
                logging.warning("%s: Platt training failed: %s", self._label, e)

        if n >= CALIBRATION_MIN_SAMPLES_BETA:
            try:
                self._train_beta_cal()
                self._beta_trained = True
                trained_methods["beta_cal"] = self._compute_brier_for_method("beta_cal")
                logging.info(
                    "%s: Beta Cal trained — a=%.4f, b=%.4f, c=%.4f, "
                    "Brier=%.4f, n=%d",
                    self._label, self._beta_a, self._beta_b, self._beta_c,
                    trained_methods["beta_cal"], n,
                )
            except Exception as e:
                logging.warning("%s: Beta Cal training failed: %s", self._label, e)

        if n >= CALIBRATION_MIN_SAMPLES_BLR:
            try:
                self._train_blr()
                self._blr_trained = True
                trained_methods["blr"] = self._compute_brier_for_method("blr")
                logging.info(
                    "%s: BLR trained — mu=[%.4f, %.4f], Brier=%.4f, n=%d",
                    self._label, self._blr_mu[0], self._blr_mu[1], trained_methods["blr"], n,
                )
            except Exception as e:
                logging.warning("%s: BLR training failed: %s", self._label, e)

        # ── Fit temperature scaling and include in competition ────────────
        try:
            temp = self._fit_temperature()
            if temp is not None:
                self._temperature = temp
                brier_sum = sum((self._temperature_predict(item[0], temp) - item[1]) ** 2
                                for item in self._observations)
                self._temperature_brier = brier_sum / len(self._observations)
                trained_methods["temperature"] = self._temperature_brier
                logging.info(
                    "%s: Temperature scaling fitted — T=%.4f, Brier=%.4f, n=%d",
                    self._label, temp, self._temperature_brier, n,
                )
        except Exception as e:
            logging.warning("%s: Temperature scaling failed: %s", self._label, e)

        # STC-aware Platt (needs observations with valid seconds_to_close)
        if n >= CALIBRATION_MIN_SAMPLES_PLATT:
            try:
                self._train_stc_platt()
                if self._stc_platt_C != 0.0:  # Only count if C was learned (not stuck at 0)
                    self._stc_platt_trained = True
                    trained_methods["stc_platt"] = self._compute_brier_for_method("stc_platt")
                    logging.info(
                        "%s: STC-Platt trained — A=%.4f, B=%.4f, C=%.4f, Brier=%.4f, n=%d",
                        self._label, self._stc_platt_A, self._stc_platt_B,
                        self._stc_platt_C, trained_methods["stc_platt"], n,
                    )
            except Exception as e:
                logging.warning("%s: STC-Platt training failed: %s", self._label, e)

        if not trained_methods:
            return False

        # ── Promote best Brier method ─────────────────────────────────────
        best_method = min(trained_methods, key=trained_methods.get)
        best_brier = trained_methods[best_method]

        # Regression guard: reject if best is worse than previous + margin
        if self._prev_brier is not None and best_brier > self._prev_brier + 0.01:
            logging.warning(
                "%s: promotion REJECTED — best Brier %.4f > prev %.4f + 0.01 "
                "(methods: %s)",
                self._label, best_brier, self._prev_brier, trained_methods,
            )
            return False

        old_method = self.active_method
        self.active_method = best_method
        self._prev_brier = best_brier

        logging.info(
            "%s: PROMOTED %s -> %s (Brier=%.4f, alternatives=%s)",
            self._label, old_method, best_method, best_brier,
            {k: round(v, 4) for k, v in trained_methods.items()},
        )

        self._save_state()
        return True

    def load_training_data_from_db(self, state: "StateManager",
                                   product_type_include: Optional[str] = None,
                                   asset_filter=None):
        """Rebuild training data from evaluated opportunities on startup.

        Args:
            product_type_include: If set, load ONLY this product type (for
                dedicated hourly/spx engines). Default None = existing behavior
                (exclude non-15M types).
            asset_filter: If set, additionally filter by asset column.
                str → single asset, list → IN clause. Default None = no asset filter.

        Note: rejected_opportunities (z-score rejections) are excluded because
        their bimodal raw_prob distribution (clustered at 0 and 1) contaminates
        Platt training — 2 NO outcomes at raw_prob≈1.0 create extreme log-loss
        pressure that drives Platt A well below 1.0, compressing all high-end
        calibrated probabilities (e.g. raw 0.95 → cal 0.89 instead of ~0.96).
        """
        try:
            self._observations.clear()

            cutoff = (datetime.datetime.now(timezone.utc)
                      - datetime.timedelta(days=14)).strftime("%Y-%m-%dT%H:%M:%S")
            if product_type_include:
                if asset_filter is not None:
                    if isinstance(asset_filter, list):
                        _placeholders = ",".join("?" for _ in asset_filter)
                        _cal_filter = f"AND product_type = ? AND asset IN ({_placeholders}) "
                        _cal_query_params = (product_type_include, *asset_filter, cutoff)
                    else:
                        _cal_filter = "AND product_type = ? AND asset = ? "
                        _cal_query_params = (product_type_include, asset_filter, cutoff)
                else:
                    # Existing behavior: filter by product_type only
                    _cal_filter = "AND product_type = ? "
                    _cal_query_params = (product_type_include, cutoff)
            else:
                # Existing behavior: exclude non-15M types
                _cal_excluded = get_cal_excluded_types()
                if _cal_excluded:
                    _excl_sorted = sorted(_cal_excluded)
                    _placeholders = ",".join("?" for _ in _excl_sorted)
                    _cal_filter = f"AND (product_type IS NULL OR product_type NOT IN ({_placeholders})) "
                    _cal_query_params = (*_excl_sorted, cutoff)
                else:
                    _cal_filter = ""
                    _cal_query_params = (cutoff,)
            # Stage filter: only accept specified stages (e.g., candidates for 15M)
            _stage_filter = ""
            _stage_params = ()
            if self._accepted_stages:
                _stage_placeholders = ",".join("?" for _ in self._accepted_stages)
                _stage_filter = f"AND filter_stage IN ({_stage_placeholders}) "
                _stage_params = tuple(self._accepted_stages)

            rows = state.conn.execute(
                "SELECT raw_prob, market_result, seconds_to_close "
                "FROM evaluated_opportunities "
                "WHERE status='settled' AND raw_prob IS NOT NULL "
                "AND market_result IS NOT NULL "
                + _cal_filter + _stage_filter +
                "AND evaluation_time > ? "
                "ORDER BY evaluation_time DESC LIMIT 500",
                (*_cal_query_params[:-1], *_stage_params, _cal_query_params[-1])
            ).fetchall()

            loaded = 0
            for row in rows:
                raw_p = row["raw_prob"]
                result = row["market_result"]
                stc = row["seconds_to_close"]
                if result in ("yes", "all_yes"):
                    binary = 1
                elif result in ("no", "all_no"):
                    binary = 0
                else:
                    continue
                self._observations.append((raw_p, binary, stc))
                loaded += 1

            logging.info(
                "%s: loaded %d observations from DB (total: %d)",
                self._label, loaded, len(self._observations),
            )

            # Attempt initial training if enough data
            if loaded > 0:
                self._last_retrain = 0.0  # force retrain check
                self.maybe_retrain()

        except Exception as e:
            logging.warning("%s: failed to load from DB: %s", self._label, e)

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
        for _obs_item in self._observations:
            raw_p, outcome = _obs_item[0], _obs_item[1]
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
                "%s: Platt params extreme (A=%.4f, B=%.4f), rejecting",
                self._label,
                A, B,
            )
            return

        self._platt_A = A
        self._platt_B = B

    # ── STC-Aware Platt ───────────────────────────────────────────────────

    def _stc_platt_predict(self, raw_prob: float,
                           seconds_to_close: Optional[float] = None) -> float:
        """P_cal = sigmoid(A * logit(p) + B + C * log(STC/300))."""
        p = max(0.001, min(0.999, raw_prob))
        logit_p = math.log(p / (1.0 - p))
        z = self._stc_platt_A * logit_p + self._stc_platt_B
        if seconds_to_close is not None and seconds_to_close > 0:
            z += self._stc_platt_C * math.log(seconds_to_close / 300.0)
        z = max(-20.0, min(20.0, z))
        return 1.0 / (1.0 + math.exp(-z))

    def _train_stc_platt(self):
        """Newton-Raphson for sigmoid(A * logit(p) + B + C * log(STC/300))."""
        valid = [(item[0], item[1], item[2]) for item in self._observations
                 if len(item) > 2 and item[2] is not None and item[2] > 0]
        if len(valid) < CALIBRATION_MIN_SAMPLES_PLATT:
            return
        A = self._stc_platt_A
        B = self._stc_platt_B
        C = self._stc_platt_C
        # Precompute features
        logits = []
        log_stcs = []
        targets = []
        for raw_p, outcome, stc in valid:
            p = max(0.001, min(0.999, raw_p))
            logits.append(math.log(p / (1.0 - p)))
            log_stcs.append(math.log(stc / 300.0))
            targets.append(float(outcome))
        n = len(logits)
        for _ in range(50):
            g = [0.0, 0.0, 0.0]
            H = [[0.0] * 3 for _ in range(3)]
            for i in range(n):
                z = A * logits[i] + B + C * log_stcs[i]
                z = max(-20.0, min(20.0, z))
                q = 1.0 / (1.0 + math.exp(-z))
                q = max(1e-10, min(1.0 - 1e-10, q))
                err = q - targets[i]
                w = q * (1.0 - q)
                phi = [logits[i], 1.0, log_stcs[i]]
                for j in range(3):
                    g[j] += err * phi[j]
                    for k in range(3):
                        H[j][k] += w * phi[j] * phi[k]
            # Solve 3x3 via Cramer's rule
            det = (H[0][0] * (H[1][1] * H[2][2] - H[1][2] * H[2][1])
                   - H[0][1] * (H[1][0] * H[2][2] - H[1][2] * H[2][0])
                   + H[0][2] * (H[1][0] * H[2][1] - H[1][1] * H[2][0]))
            if abs(det) < 1e-12:
                break
            rhs = [-g[0], -g[1], -g[2]]
            d0 = (rhs[0] * (H[1][1] * H[2][2] - H[1][2] * H[2][1])
                  - H[0][1] * (rhs[1] * H[2][2] - H[1][2] * rhs[2])
                  + H[0][2] * (rhs[1] * H[2][1] - H[1][1] * rhs[2])) / det
            d1 = (H[0][0] * (rhs[1] * H[2][2] - H[1][2] * rhs[2])
                  - rhs[0] * (H[1][0] * H[2][2] - H[1][2] * H[2][0])
                  + H[0][2] * (H[1][0] * rhs[2] - rhs[1] * H[2][0])) / det
            d2 = (H[0][0] * (H[1][1] * rhs[2] - rhs[1] * H[2][1])
                  - H[0][1] * (H[1][0] * rhs[2] - rhs[1] * H[2][0])
                  + rhs[0] * (H[1][0] * H[2][1] - H[1][1] * H[2][0])) / det
            A += d0
            B += d1
            C += d2
            if max(abs(d0), abs(d1), abs(d2)) < 1e-8:
                break
        if abs(A) > 5.0 or abs(B) > 5.0 or abs(C) > 5.0:
            logging.warning(
                "%s: STC-Platt params extreme (A=%.4f B=%.4f C=%.4f), rejecting",
                self._label, A, B, C)
            return
        self._stc_platt_A = A
        self._stc_platt_B = B
        self._stc_platt_C = C

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
        for _obs_item in self._observations:
            raw_p, outcome = _obs_item[0], _obs_item[1]
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
        for _obs_item in self._observations:
            raw_p, outcome = _obs_item[0], _obs_item[1]
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
        for _obs_item in self._observations:
            raw_p, outcome = _obs_item[0], _obs_item[1]
            pred = self.calibrate(raw_p, cap=1.0)
            total += (pred - outcome) ** 2
        return total / len(self._observations)

    def is_learned_method_active(self) -> bool:
        """Return True if a data-driven calibration method is active (not fixed_beta fallback)."""
        if self.active_method == "stc_platt" and self._stc_platt_trained:
            return True
        if self.active_method == "platt" and self._platt_trained:
            return True
        if self.active_method == "beta_cal" and self._beta_trained:
            return True
        if self.active_method == "temperature" and self._temperature is not None:
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
        for _obs_item in self._observations:
            raw_p, outcome = _obs_item[0], _obs_item[1]
            stc = _obs_item[2] if len(_obs_item) > 2 else None
            if method == "stc_platt":
                pred = self._stc_platt_predict(raw_p, stc)
            elif method == "platt":
                pred = self._platt_predict(raw_p)
            elif method == "beta_cal":
                pred = self._beta_cal_predict(raw_p)
            elif method == "temperature" and self._temperature is not None:
                pred = self._temperature_predict(raw_p, self._temperature)
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

        for _obs_item in self._observations:
            raw_p, outcome = _obs_item[0], _obs_item[1]
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
            "%s BACKTEST: old_brier=%.4f, new_brier=%.4f, "
            "improvement=%.4f, cap_truncated=%d/%d, high_prob_new=%d",
            self._label, result["old_brier"], result["new_brier"], result["brier_improvement"],
            cap_truncated, n, high_prob_markets,
        )
        return result


# ─── Calibration runtime state (Bit 6.3 path-B refactor, 2026-05-10) ────────
#
# Mutable module-level singletons + helpers relocated from bot/_impl.py to
# lift the Bit 6.2 late-binding in bot/engines/probability.py. ProbabilityEngine
# now reaches them via top-level `from bot.engines import calibration as
# _cal_state`; bot/_impl.py uses the same alias. Module-attribute access is
# the standard Python idiom for shared mutable state (mutations on the module
# namespace are observed by every reader through the alias). The previous
# `from bot._impl import ...` pattern would either ImportError at module load
# or capture a stale `None`; this layer eliminates both failure modes.
#
# Removes the `.importlinter` `bot.engines.probability -> bot._impl`
# ignore_imports carve-out shipped in Pillar 2 (testing-foundation-sprint).

_CALIBRATION_ENGINE: Optional[CalibrationEngine] = None   # 15M ONLY — DO NOT TOUCH
_CAL_REGISTRY: Dict[str, CalibrationEngine] = {}          # non-15M engines by product_type


def _derive_subtype(product_type: str, asset: Optional[str]) -> Optional[str]:
    """Extract CalEngine subtype code from asset string.
    15M: 'BTC' → 'BTC'.  Weather: 'NYC_TEMP' → 'NYC'.  Sports: 'NBA' → 'basketball'."""
    if not asset:
        return None
    if product_type == "15m":
        return asset  # Direct: asset name IS the subtype
    if product_type == "weather":
        return asset.replace("_TEMP", "") if "_TEMP" in asset else None
    if product_type == "sports":
        try:
            from bot.engines.sports_data import LEAGUES  # Sprint 10.1a sibling-reorg (2026-05-11)
            for _lcfg in LEAGUES.values():
                if _lcfg.display_name == asset:
                    return _lcfg.sport_group
        except ImportError:
            pass
        logging.debug("_derive_subtype: no sport_group for asset=%s", asset)
        return None
    return None


def _derive_asset_filter(product_type: str, subtype_code: str):
    """Map subtype code back to DB asset filter for load_training_data_from_db().
    Returns str for single-asset types, list for multi-league sport groups."""
    if product_type == "15m":
        return subtype_code  # "BTC" → "BTC"
    if product_type == "weather":
        return f"{subtype_code}_TEMP"  # "NYC" → "NYC_TEMP"
    if product_type == "sports":
        try:
            from bot.engines.sports_data import LEAGUES  # Sprint 10.1a sibling-reorg (2026-05-11)
            return [lcfg.display_name for lcfg in LEAGUES.values()
                    if lcfg.sport_group == subtype_code]
        except ImportError:
            return None
    return None


def _resolve_cal_engine(product_type: Optional[str],
                        asset: Optional[str] = None,
                        require_enabled: bool = False) -> Optional[CalibrationEngine]:
    """Look up the correct CalibrationEngine for a (product_type, asset) pair.
    Returns None for product_type=None (legacy, uses _CALIBRATION_ENGINE directly).
    For 15M with cal_subtypes: returns per-asset engine from registry.
    require_enabled=True: also returns None if cal_engine_enabled=False."""
    if product_type is None:
        return None
    _cfg = get_market_config(product_type)
    if require_enabled and not _cfg.cal_engine_enabled:
        return None
    # Types with subtypes: derive composite key
    if _cfg.cal_subtypes and asset:
        _sub = _derive_subtype(product_type, asset)
        if _sub:
            return _CAL_REGISTRY.get(f"{product_type}_{_sub}")
    # Bare product_type key (hourly, spx_hourly, or no subtypes match)
    return _CAL_REGISTRY.get(product_type)
