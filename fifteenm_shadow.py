"""15M Shadow Engine — two alternative approaches for 15M crypto markets.

Approach 1 (RecalibratedEGARCH): Per-asset temperature scaling + market blend + edge band filters.
  Uses the SAME blended_rv from the live pipeline (doesn't re-fit EGARCH). Applies per-asset
  post-processing learned from historical settled evaluations.

Approach 2 (LightGBM): Per-asset binary classifier predicting settlement outcome from features
  already available in the pipeline (blended_rv, z_score, market_price, STC, spread, etc.).
  Trained on settled evaluated_opportunities, isotonic-calibrated, retrained daily.

Both approaches are shadow-only — they cannot place orders and have no access to KalshiClient.
Results logged to fifteenm_shadow_signals table for post-hoc comparison with live baseline.
"""

import datetime
import json
import logging
import math
import os
import sqlite3
import time
from typing import Dict, List, Optional, Tuple

DB_PATH = os.environ.get("BOT_DB_PATH", "state.db")
FIFTEENM_SHADOW_ENABLED = True

# ── Approach 1 Defaults ─────────────────────────────────────────────────────
# Per-asset temperature (T > 1 = soften overconfident probs)
DEFAULT_TEMPERATURES = {"BTC": 1.15, "ETH": 1.25, "SOL": 1.05, "XRP": 1.30}
# Per-asset market blend weight (higher = more market, less model)
DEFAULT_BLEND_W = {"BTC": 0.50, "ETH": 0.50, "SOL": 0.45, "XRP": 0.60}
# Per-asset overconfidence bias (pp to subtract before Kelly)
DEFAULT_DEBIAS = {"BTC": 0.02, "ETH": 0.04, "SOL": 0.01, "XRP": 0.05}
# Edge band blacklist: (min_edge, max_edge) ranges to block per asset
DEFAULT_EDGE_BLACKLIST: Dict[str, List[Tuple[float, float]]] = {
    "XRP": [(0.028, 0.038)],  # XRP 3.0-3.5% edge zone is anti-predictive
}

# ── Approach 2 Defaults ─────────────────────────────────────────────────────
LGBM_MIN_TRAINING_ROWS = 200
LGBM_RETRAIN_INTERVAL = 86400  # seconds (daily)

# ── Shared Defaults ──────────────────────────────────────────────────────────
SHADOW_BANKROLL = 50000  # cents ($500 simulated bankroll)
MAX_KELLY_FRACTION = 0.25  # quarter-Kelly
MAX_RISK_CAP = 0.03  # 3% bankroll hard cap per signal
MIN_DEBIASED_EDGE = 0.015  # 1.5pp minimum edge after debiasing
FEE_MULTIPLIER = 0.0  # Kalshi charges $0 on maker fills


class RecalibratedEGARCHApproach:
    """Approach 1: Per-asset temperature + blend + edge band filters."""

    def __init__(self, db_path: str = DB_PATH):
        self._db_path = db_path
        self.temperatures = dict(DEFAULT_TEMPERATURES)
        self.blend_weights = dict(DEFAULT_BLEND_W)
        self.debias = dict(DEFAULT_DEBIAS)
        self.edge_blacklist = {k: list(v) for k, v in DEFAULT_EDGE_BLACKLIST.items()}
        self._last_fit_time = 0.0
        self._fit_interval = 3600  # refit temperatures hourly
        self._signal_counts: Dict[str, int] = {}
        self._gate_failure_counts: Dict[str, int] = {}

    def _maybe_fit_temperatures(self):
        """Refit per-asset temperature from settled evaluated_opportunities."""
        now = time.time()
        if now - self._last_fit_time < self._fit_interval:
            return
        self._last_fit_time = now
        try:
            conn = sqlite3.connect(self._db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=10000")
            for asset in ("BTC", "ETH", "SOL", "XRP"):
                rows = conn.execute(
                    "SELECT calibrated_prob, market_result FROM evaluated_opportunities "
                    "WHERE asset = ? AND (product_type IS NULL OR product_type = '15m') "
                    "AND status = 'settled' "
                    "AND filter_stage IN ('candidate', 'xrp_shadow', 'stc_shadow', "
                    "  'stc_shadow_xrp', 'stc_shadow_no_xrp', 'stc_shadow_promoted') "
                    "AND calibrated_prob IS NOT NULL AND market_result IS NOT NULL",
                    (asset,)
                ).fetchall()
                if len(rows) < 50:
                    continue
                # Grid search for T that minimizes Brier score
                best_t, best_brier = self.temperatures.get(asset, 1.0), float("inf")
                for t_100 in range(80, 200, 5):  # T from 0.80 to 1.95
                    t = t_100 / 100.0
                    brier = 0.0
                    for r in rows:
                        p = r["calibrated_prob"]
                        # Apply temperature: logit transform
                        if 0 < p < 1:
                            logit = math.log(p / (1 - p))
                            p_t = 1.0 / (1.0 + math.exp(-logit / t))
                        else:
                            p_t = p
                        outcome = 1.0 if r["market_result"] in ("yes", "all_yes") else 0.0
                        brier += (p_t - outcome) ** 2
                    brier /= len(rows)
                    if brier < best_brier:
                        best_brier = brier
                        best_t = t
                self.temperatures[asset] = best_t
                # Compute overconfidence bias for debiasing
                total_pred = 0.0
                total_actual = 0.0
                for r in rows:
                    p = r["calibrated_prob"]
                    if 0 < p < 1:
                        logit = math.log(p / (1 - p))
                        p_t = 1.0 / (1.0 + math.exp(-logit / best_t))
                    else:
                        p_t = p
                    total_pred += p_t
                    total_actual += 1.0 if r["market_result"] in ("yes", "all_yes") else 0.0
                bias = (total_pred / len(rows)) - (total_actual / len(rows))
                self.debias[asset] = max(0.0, bias)
                logging.info("fifteenm_shadow A1 fit %s: T=%.2f debias=%.4f brier=%.4f n=%d",
                             asset, best_t, self.debias[asset], best_brier, len(rows))
            conn.close()
        except Exception:
            logging.debug("fifteenm_shadow A1 temperature fit failed", exc_info=True)

    def evaluate(self, asset: str, spot_price: float, threshold: float,
                 seconds_to_close: float, blended_rv: float,
                 egarch_sigma: Optional[float], market_price: int,
                 z_score: float, live_prob: float) -> Dict:
        """Evaluate using per-asset recalibrated EGARCH approach."""
        self._maybe_fit_temperatures()
        self._signal_counts[asset] = self._signal_counts.get(asset, 0) + 1

        t = self.temperatures.get(asset, 1.0)
        blend_w = self.blend_weights.get(asset, 0.40)

        # Step 1: Apply per-asset temperature to live_prob
        if 0 < live_prob < 1 and t != 1.0:
            logit = math.log(live_prob / (1 - live_prob))
            temp_prob = 1.0 / (1.0 + math.exp(-logit / t))
        else:
            temp_prob = live_prob

        # Step 2: Per-asset market blend
        market_prob = market_price / 100.0
        final_prob = (1 - blend_w) * temp_prob + blend_w * market_prob

        # Step 3: Compute edge
        edge = final_prob - (market_price / 100.0)
        est_fee = math.ceil(FEE_MULTIPLIER * 100 * (market_price / 100) * (1 - market_price / 100))
        fee_adjusted_edge = edge - est_fee / 100.0

        # Step 4: Check gates
        gates_passed = True
        gate_failures = []

        # Edge band blacklist
        edge_band_blocked = False
        for lo, hi in self.edge_blacklist.get(asset, []):
            if lo <= edge <= hi:
                edge_band_blocked = True
                gate_failures.append(f"edge_band_{lo:.3f}_{hi:.3f}")
                gates_passed = False
                break

        # Minimum debiased edge
        debias_pp = self.debias.get(asset, 0.0)
        debiased_prob = max(0.0, min(1.0, final_prob - debias_pp))
        debiased_edge = debiased_prob - (market_price / 100.0)
        if debiased_edge < MIN_DEBIASED_EDGE:
            gate_failures.append(f"debiased_edge_{debiased_edge:.4f}")
            gates_passed = False

        # Price band
        if not (86 <= market_price <= 99):
            gate_failures.append(f"price_{market_price}")
            gates_passed = False

        # STC range
        if not (0 < seconds_to_close <= 900):
            gate_failures.append(f"stc_{seconds_to_close:.0f}")
            gates_passed = False

        for gf in gate_failures:
            self._gate_failure_counts[gf] = self._gate_failure_counts.get(gf, 0) + 1

        # Step 5: Debiased Kelly sizing
        kelly_f = 0.0
        contracts = 0
        if gates_passed and debiased_edge > 0 and market_price < 100:
            p = debiased_prob
            b = (100 - market_price) / market_price
            kelly_raw = (p * b - (1 - p)) / b if b > 0 else 0
            kelly_f = max(0.0, kelly_raw * MAX_KELLY_FRACTION)
            risk = min(kelly_f, MAX_RISK_CAP)
            contracts = int(SHADOW_BANKROLL * risk / market_price) if market_price > 0 else 0

        return {
            "approach": "recalibrated_egarch",
            "raw_prob": live_prob,
            "temperature": t,
            "temp_prob": temp_prob,
            "blend_w": blend_w,
            "final_prob": final_prob,
            "edge": edge,
            "fee_adjusted_edge": fee_adjusted_edge,
            "edge_band_blocked": int(edge_band_blocked),
            "debiased_prob": debiased_prob,
            "debias_pp": debias_pp,
            "kelly_f": kelly_f,
            "contracts": contracts,
            "gates_passed": int(gates_passed),
            "gate_failures": ",".join(gate_failures) if gate_failures else None,
        }

    def get_metrics(self) -> Dict:
        return {
            "temperatures": dict(self.temperatures),
            "blend_weights": dict(self.blend_weights),
            "debias": dict(self.debias),
            "signal_counts": dict(self._signal_counts),
            "gate_failures": dict(self._gate_failure_counts),
        }


class LightGBMApproach:
    """Approach 2: Per-asset LightGBM binary classifier."""

    def __init__(self, db_path: str = DB_PATH):
        self._db_path = db_path
        self._models: Dict[str, object] = {}  # asset -> trained model
        self._calibrators: Dict[str, object] = {}  # asset -> isotonic calibrator
        self._model_versions: Dict[str, str] = {}
        self._last_train_time: Dict[str, float] = {}
        self._available = False
        self._signal_counts: Dict[str, int] = {}
        self._gate_failure_counts: Dict[str, int] = {}
        try:
            import lightgbm  # noqa: F401
            from sklearn.isotonic import IsotonicRegression  # noqa: F401
            self._available = True
            logging.info("fifteenm_shadow A2: LightGBM + sklearn available")
        except ImportError:
            logging.warning("fifteenm_shadow A2: lightgbm/sklearn not installed — approach disabled")

    def _get_features(self, asset: str, spot_price: float, threshold: float,
                      seconds_to_close: float, blended_rv: float,
                      market_price: int, z_score: float, live_prob: float,
                      best_bid: Optional[int], best_ask: Optional[int]) -> List[float]:
        """Extract feature vector from available pipeline data."""
        # Log-moneyness: how far spot is from threshold
        log_moneyness = math.log(spot_price / threshold) if threshold > 0 and spot_price > 0 else 0
        spread = (best_ask - best_bid) if best_bid is not None and best_ask is not None else 0
        hour_utc = datetime.datetime.now(datetime.timezone.utc).hour
        edge = live_prob - (market_price / 100.0)

        return [
            blended_rv,
            z_score,
            log_moneyness,
            market_price / 100.0,
            seconds_to_close,
            spread,
            hour_utc,
            edge,
            live_prob,
        ]

    def _maybe_train(self, asset: str):
        """Train/retrain model for asset if enough data and interval elapsed."""
        if not self._available:
            return
        now = time.time()
        if now - self._last_train_time.get(asset, 0) < LGBM_RETRAIN_INTERVAL:
            return
        self._last_train_time[asset] = now
        try:
            import lightgbm as lgb
            from sklearn.isotonic import IsotonicRegression
            import numpy as np

            conn = sqlite3.connect(self._db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=10000")
            # Use ALL settled 15M evals for training (not just candidates).
            # Rejected opportunities (price_out_of_range, insufficient_edge, etc.)
            # have known outcomes and valid features — more data = better classifier.
            rows = conn.execute(
                "SELECT calibrated_prob, market_price, volatility, z_score, "
                "  seconds_to_close, spot_price, threshold, market_result "
                "FROM evaluated_opportunities "
                "WHERE asset = ? AND (product_type IS NULL OR product_type = '15m') "
                "AND status = 'settled' "
                "AND calibrated_prob IS NOT NULL AND market_result IS NOT NULL "
                "AND market_price IS NOT NULL AND volatility IS NOT NULL",
                (asset,)
            ).fetchall()
            conn.close()

            if len(rows) < LGBM_MIN_TRAINING_ROWS:
                logging.info("fifteenm_shadow A2 %s: only %d rows, need %d",
                             asset, len(rows), LGBM_MIN_TRAINING_ROWS)
                return

            # Build feature matrix
            X = []
            y = []
            for r in rows:
                spot = r["spot_price"] or 0
                thresh = r["threshold"] or 0
                log_m = math.log(spot / thresh) if thresh > 0 and spot > 0 else 0
                mp = r["market_price"] or 0
                cp = r["calibrated_prob"] or 0
                edge = cp - (mp / 100.0)
                X.append([
                    r["volatility"] or 0,
                    r["z_score"] or 0,
                    log_m,
                    mp / 100.0,
                    r["seconds_to_close"] or 0,
                    0,  # spread not in settled data
                    0,  # hour not in settled data
                    edge,
                    cp,
                ])
                y.append(1.0 if r["market_result"] in ("yes", "all_yes") else 0.0)

            X = np.array(X)
            y = np.array(y)

            # Train/calibrate/test split: 60/20/20
            n = len(X)
            n_train = int(n * 0.6)
            n_cal = int(n * 0.2)
            X_train, y_train = X[:n_train], y[:n_train]
            X_cal, y_cal = X[n_train:n_train + n_cal], y[n_train:n_train + n_cal]
            X_test, y_test = X[n_train + n_cal:], y[n_train + n_cal:]

            if len(X_train) < 50 or len(X_cal) < 20:
                return

            # Train LightGBM
            params = {
                "objective": "binary",
                "metric": "binary_logloss",
                "num_leaves": 15,
                "learning_rate": 0.05,
                "feature_fraction": 0.8,
                "bagging_fraction": 0.8,
                "bagging_freq": 5,
                "verbose": -1,
                "n_jobs": 1,
            }
            dtrain = lgb.Dataset(X_train, label=y_train)
            model = lgb.train(params, dtrain, num_boost_round=100)

            # Isotonic calibration on calibration set
            cal_preds = model.predict(X_cal)
            calibrator = IsotonicRegression(out_of_bounds="clip")
            calibrator.fit(cal_preds, y_cal)

            # Evaluate on test set
            test_preds_raw = model.predict(X_test)
            test_preds = calibrator.predict(test_preds_raw)
            test_brier = float(np.mean((test_preds - y_test) ** 2))

            self._models[asset] = model
            self._calibrators[asset] = calibrator
            version = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
            self._model_versions[asset] = version
            logging.info("fifteenm_shadow A2 trained %s: n=%d test_brier=%.4f version=%s",
                         asset, len(rows), test_brier, version)

        except Exception:
            logging.debug("fifteenm_shadow A2 train failed for %s", asset, exc_info=True)

    def evaluate(self, asset: str, spot_price: float, threshold: float,
                 seconds_to_close: float, blended_rv: float,
                 market_price: int, z_score: float, live_prob: float,
                 best_bid: Optional[int], best_ask: Optional[int]) -> Dict:
        """Evaluate using LightGBM model."""
        self._signal_counts[asset] = self._signal_counts.get(asset, 0) + 1
        self._maybe_train(asset)

        if not self._available or asset not in self._models:
            return {
                "approach": "lightgbm",
                "raw_prob": None,
                "calibrated_prob": None,
                "edge": None,
                "fee_adjusted_edge": None,
                "kelly_f": 0.0,
                "contracts": 0,
                "model_version": None,
                "gates_passed": 0,
                "gate_failures": "no_model",
            }

        try:
            import numpy as np
            features = self._get_features(asset, spot_price, threshold,
                                          seconds_to_close, blended_rv,
                                          market_price, z_score, live_prob,
                                          best_bid, best_ask)
            X = np.array([features])
            raw_pred = float(self._models[asset].predict(X)[0])
            cal_pred = float(self._calibrators[asset].predict([raw_pred])[0])
            final_prob = cal_pred

            edge = final_prob - (market_price / 100.0)
            est_fee = math.ceil(FEE_MULTIPLIER * 100 * (market_price / 100) * (1 - market_price / 100))
            fee_adjusted_edge = edge - est_fee / 100.0

            # Gates
            gates_passed = True
            gate_failures = []

            if not (86 <= market_price <= 99):
                gate_failures.append(f"price_{market_price}")
                gates_passed = False
            if not (0 < seconds_to_close <= 900):
                gate_failures.append(f"stc_{seconds_to_close:.0f}")
                gates_passed = False
            if fee_adjusted_edge < MIN_DEBIASED_EDGE:
                gate_failures.append(f"edge_{fee_adjusted_edge:.4f}")
                gates_passed = False

            for gf in gate_failures:
                self._gate_failure_counts[gf] = self._gate_failure_counts.get(gf, 0) + 1

            # Kelly sizing
            kelly_f = 0.0
            contracts = 0
            if gates_passed and edge > 0 and market_price < 100:
                b = (100 - market_price) / market_price
                kelly_raw = (final_prob * b - (1 - final_prob)) / b if b > 0 else 0
                kelly_f = max(0.0, kelly_raw * MAX_KELLY_FRACTION)
                risk = min(kelly_f, MAX_RISK_CAP)
                contracts = int(SHADOW_BANKROLL * risk / market_price) if market_price > 0 else 0

            return {
                "approach": "lightgbm",
                "raw_prob": raw_pred,
                "calibrated_prob": cal_pred,
                "edge": edge,
                "fee_adjusted_edge": fee_adjusted_edge,
                "kelly_f": kelly_f,
                "contracts": contracts,
                "model_version": self._model_versions.get(asset),
                "gates_passed": int(gates_passed),
                "gate_failures": ",".join(gate_failures) if gate_failures else None,
            }
        except Exception:
            logging.debug("fifteenm_shadow A2 evaluate failed for %s", asset, exc_info=True)
            return {
                "approach": "lightgbm",
                "raw_prob": None,
                "calibrated_prob": None,
                "edge": None,
                "fee_adjusted_edge": None,
                "kelly_f": 0.0,
                "contracts": 0,
                "model_version": self._model_versions.get(asset),
                "gates_passed": 0,
                "gate_failures": "eval_error",
            }

    def get_metrics(self) -> Dict:
        return {
            "available": self._available,
            "models_trained": list(self._models.keys()),
            "model_versions": dict(self._model_versions),
            "signal_counts": dict(self._signal_counts),
            "gate_failures": dict(self._gate_failure_counts),
        }


GATING_THRESHOLDS = [0.10, 0.20, 0.30]  # gate if loss probability exceeds threshold
GATING_MIN_TRAINING_ROWS = 100
GATING_RETRAIN_INTERVAL = 86400  # daily


class EGARCHGatingApproach:
    """Approach 3: LightGBM gating model — predicts whether EGARCH will lose money.

    Unlike Approach 2 which predicts market outcomes (competing with market price),
    this model predicts our model's failures — information private to our system.
    Output: probability that EGARCH will lose money on this signal.
    """

    FEATURE_NAMES = [
        "blended_rv", "z_score", "log_moneyness", "market_prob",
        "seconds_to_close", "spread", "hour_utc", "edge", "live_prob",
        "egarch_sigma", "egarch_blend_weight", "fee_adjusted_edge",
        "kelly_recommended_risk", "price_tier", "asset_idx", "side_idx",
    ]

    def __init__(self, db_path: str = DB_PATH):
        self._db_path = db_path
        self._models: Dict[str, object] = {}  # "pooled" or asset -> model
        self._calibrators: Dict[str, object] = {}
        self._model_versions: Dict[str, str] = {}
        self._last_train_time: Dict[str, float] = {}
        self._available = False
        self._signal_counts: Dict[str, int] = {}
        self._no_side_training_counts: Dict[str, int] = {}
        self._feature_importances: Dict[str, Dict[str, float]] = {}
        try:
            import lightgbm  # noqa: F401
            from sklearn.isotonic import IsotonicRegression  # noqa: F401
            self._available = True
            logging.info("fifteenm_shadow A3 (gating): LightGBM + sklearn available")
        except ImportError:
            logging.warning("fifteenm_shadow A3: lightgbm/sklearn not installed — approach disabled")

    @staticmethod
    def _asset_to_idx(asset: str) -> int:
        return {"BTC": 0, "ETH": 1, "SOL": 2, "XRP": 3}.get(asset, -1)

    @staticmethod
    def _price_to_tier(price: int) -> int:
        if price < 86:
            return 0
        elif price <= 88:
            return 1  # known problematic tier (72.2% WR)
        elif price <= 90:
            return 2
        elif price <= 93:
            return 3
        elif price <= 95:
            return 4
        elif price <= 97:
            return 5
        else:
            return 6

    def _get_features(self, asset: str, spot_price: float, threshold: float,
                      seconds_to_close: float, blended_rv: float,
                      market_price: int, z_score: float, live_prob: float,
                      best_bid: Optional[int], best_ask: Optional[int],
                      egarch_sigma: Optional[float],
                      egarch_blend_weight: Optional[float],
                      fee_adjusted_edge: Optional[float],
                      kelly_f: Optional[float],
                      side: str = "yes") -> List[float]:
        """Extract feature vector for gating model."""
        log_moneyness = math.log(spot_price / threshold) if threshold > 0 and spot_price > 0 else 0
        spread = (best_ask - best_bid) if best_bid is not None and best_ask is not None else 0
        hour_utc = datetime.datetime.now(datetime.timezone.utc).hour

        # For NO-side: adjust price/prob perspective
        effective_price = market_price if side == "yes" else (100 - market_price)
        effective_prob = live_prob if side == "yes" else (1.0 - live_prob)
        edge = effective_prob - (effective_price / 100.0)

        return [
            blended_rv,
            z_score,
            log_moneyness,
            effective_price / 100.0,
            seconds_to_close,
            spread,
            hour_utc,
            edge,
            effective_prob,
            egarch_sigma or 0.0,
            egarch_blend_weight or 0.0,
            fee_adjusted_edge or edge,
            kelly_f or 0.0,
            self._price_to_tier(effective_price),
            self._asset_to_idx(asset),
            0 if side == "yes" else 1,
        ]

    def _maybe_train(self, asset: str = "pooled"):
        """Train gating model — pooled across all assets (asset is a feature)."""
        if not self._available:
            return
        now = time.time()
        if now - self._last_train_time.get(asset, 0) < GATING_RETRAIN_INTERVAL:
            return
        self._last_train_time[asset] = now
        try:
            import lightgbm as lgb
            from sklearn.isotonic import IsotonicRegression
            import numpy as np

            conn = sqlite3.connect(self._db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=10000")
            rows = conn.execute(
                "SELECT asset, calibrated_prob, market_price, volatility, z_score, "
                "  seconds_to_close, spot_price, threshold, market_result, "
                "  egarch_sigma, egarch_blend_weight, fee_adjusted_edge, "
                "  kelly_f, side "
                "FROM evaluated_opportunities "
                "WHERE (product_type IS NULL OR product_type = '15m') "
                "AND status = 'settled' "
                "AND calibrated_prob IS NOT NULL AND market_result IS NOT NULL "
                "AND market_price IS NOT NULL AND volatility IS NOT NULL"
            ).fetchall()
            conn.close()

            if len(rows) < GATING_MIN_TRAINING_ROWS:
                logging.info("fifteenm_shadow A3: only %d rows, need %d",
                             len(rows), GATING_MIN_TRAINING_ROWS)
                return

            X = []
            y = []
            no_side_count = 0
            for r in rows:
                asset_r = r["asset"]
                spot = r["spot_price"] or 0
                thresh = r["threshold"] or 0
                log_m = math.log(spot / thresh) if thresh > 0 and spot > 0 else 0
                mp = r["market_price"] or 0
                cp = r["calibrated_prob"] or 0
                side_val = r["side"] or "yes"

                effective_price = mp if side_val == "yes" else (100 - mp)
                effective_prob = cp if side_val == "yes" else (1.0 - cp)
                edge = effective_prob - (effective_price / 100.0)

                if side_val != "yes":
                    no_side_count += 1

                X.append([
                    r["volatility"] or 0,
                    r["z_score"] or 0,
                    log_m,
                    effective_price / 100.0,
                    r["seconds_to_close"] or 0,
                    0,  # spread not in settled data
                    0,  # hour not in settled data
                    edge,
                    effective_prob,
                    r["egarch_sigma"] or 0,
                    r["egarch_blend_weight"] or 0,
                    r["fee_adjusted_edge"] or edge,
                    r["kelly_f"] or 0,
                    self._price_to_tier(effective_price),
                    self._asset_to_idx(asset_r),
                    0 if side_val == "yes" else 1,
                ])

                # Target: did EGARCH lose money?
                # YES-side: loses when market_result is "no"/"all_no"
                # NO-side: loses when market_result is "yes"/"all_yes"
                result_yes = r["market_result"] in ("yes", "all_yes")
                if side_val == "yes":
                    lost = not result_yes  # YES trade loses on NO outcome
                else:
                    lost = result_yes  # NO trade loses on YES outcome
                y.append(1.0 if lost else 0.0)

            X = np.array(X)
            y = np.array(y)

            # Log NO-side data volume
            for a in ("BTC", "ETH", "SOL", "XRP"):
                a_no = sum(1 for r in rows if r["asset"] == a and (r["side"] or "yes") != "yes")
                self._no_side_training_counts[a] = a_no

            # Temporal split: 60/20/20
            n = len(X)
            n_train = int(n * 0.6)
            n_cal = int(n * 0.2)
            X_train, y_train = X[:n_train], y[:n_train]
            X_cal, y_cal = X[n_train:n_train + n_cal], y[n_train:n_train + n_cal]
            X_test, y_test = X[n_train + n_cal:], y[n_train + n_cal:]

            if len(X_train) < 50 or len(X_cal) < 20:
                return

            # Handle class imbalance (losses are rare ~5-15%)
            n_pos = int(y_train.sum())
            n_neg = len(y_train) - n_pos
            spw = n_neg / n_pos if n_pos > 0 else 1.0

            params = {
                "objective": "binary",
                "metric": "auc",
                "num_leaves": 15,
                "learning_rate": 0.05,
                "feature_fraction": 0.8,
                "bagging_fraction": 0.8,
                "bagging_freq": 5,
                "scale_pos_weight": spw,
                "verbose": -1,
                "n_jobs": 1,
            }
            dtrain = lgb.Dataset(X_train, label=y_train,
                                 feature_name=self.FEATURE_NAMES)
            model = lgb.train(params, dtrain, num_boost_round=150)

            # Isotonic calibration
            cal_preds = model.predict(X_cal)
            calibrator = IsotonicRegression(out_of_bounds="clip")
            calibrator.fit(cal_preds, y_cal)

            # Evaluate on test set
            test_preds_raw = model.predict(X_test)
            test_preds = calibrator.predict(test_preds_raw)
            test_brier = float(np.mean((test_preds - y_test) ** 2))

            # AUC
            from sklearn.metrics import roc_auc_score
            try:
                test_auc = float(roc_auc_score(y_test, test_preds))
            except ValueError:
                test_auc = 0.5

            # Feature importances
            importances = model.feature_importance(importance_type="gain")
            feat_imp = {}
            for i, name in enumerate(self.FEATURE_NAMES):
                if i < len(importances):
                    feat_imp[name] = float(importances[i])
            total_imp = sum(feat_imp.values()) or 1.0
            feat_imp = {k: round(v / total_imp, 4) for k, v in feat_imp.items()}
            self._feature_importances["pooled"] = feat_imp

            # Only replace model if it's better than random
            if test_auc >= 0.50:
                self._models["pooled"] = model
                self._calibrators["pooled"] = calibrator
                version = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
                self._model_versions["pooled"] = version
                logging.info(
                    "fifteenm_shadow A3 trained: n=%d (NO=%d) loss_rate=%.1f%% "
                    "test_brier=%.4f test_auc=%.3f version=%s",
                    len(rows), no_side_count, 100 * y.mean(),
                    test_brier, test_auc, version)
            else:
                logging.warning("fifteenm_shadow A3: AUC %.3f < 0.50, keeping old model", test_auc)

        except Exception:
            logging.debug("fifteenm_shadow A3 train failed", exc_info=True)

    def evaluate(self, asset: str, spot_price: float, threshold: float,
                 seconds_to_close: float, blended_rv: float,
                 market_price: int, z_score: float, live_prob: float,
                 best_bid: Optional[int], best_ask: Optional[int],
                 egarch_sigma: Optional[float] = None,
                 egarch_blend_weight: Optional[float] = None,
                 fee_adjusted_edge: Optional[float] = None,
                 kelly_f: Optional[float] = None,
                 side: str = "yes") -> Dict:
        """Evaluate gating model — returns loss probability and gate decisions."""
        key = f"{asset}_{side}"
        self._signal_counts[key] = self._signal_counts.get(key, 0) + 1
        self._maybe_train("pooled")

        no_model_result = {
            "approach": "egarch_gating",
            "gate_prob": None,
            "gate_10": None,
            "gate_20": None,
            "gate_30": None,
            "model_version": None,
            "side": side,
            "no_side_warning": None,
        }
        if not self._available or "pooled" not in self._models:
            no_model_result["gate_failures"] = "no_model"
            return no_model_result

        try:
            import numpy as np
            features = self._get_features(
                asset, spot_price, threshold, seconds_to_close, blended_rv,
                market_price, z_score, live_prob, best_bid, best_ask,
                egarch_sigma, egarch_blend_weight, fee_adjusted_edge, kelly_f,
                side=side)
            X = np.array([features])
            raw_pred = float(self._models["pooled"].predict(X)[0])
            cal_pred = float(self._calibrators["pooled"].predict([raw_pred])[0])

            # Gate decisions at each threshold
            result = {
                "approach": "egarch_gating",
                "gate_prob": cal_pred,
                "gate_prob_raw": raw_pred,
                "gate_10": int(cal_pred >= 0.10),
                "gate_20": int(cal_pred >= 0.20),
                "gate_30": int(cal_pred >= 0.30),
                "model_version": self._model_versions.get("pooled"),
                "side": side,
                "no_side_warning": None,
            }

            # Warn if NO-side predictions are unreliable
            no_count = self._no_side_training_counts.get(asset, 0)
            if side != "yes" and no_count < 20:
                result["no_side_warning"] = f"only_{no_count}_no_training_examples"

            return result
        except Exception:
            logging.debug("fifteenm_shadow A3 evaluate failed for %s %s",
                          asset, side, exc_info=True)
            no_model_result["gate_failures"] = "eval_error"
            return no_model_result

    def get_metrics(self) -> Dict:
        return {
            "available": self._available,
            "model_trained": "pooled" in self._models,
            "model_version": self._model_versions.get("pooled"),
            "signal_counts": dict(self._signal_counts),
            "no_side_training_counts": dict(self._no_side_training_counts),
            "feature_importances": dict(self._feature_importances),
        }


class FifteenMShadowEngine:
    """Shadow evaluation engine for 15M markets — three approaches."""

    def __init__(self, db_path: str = DB_PATH):
        self._db_path = db_path
        self._db_conn: Optional[sqlite3.Connection] = None
        self._seen: set = set()
        self._approach1 = RecalibratedEGARCHApproach(db_path)
        self._approach2 = LightGBMApproach(db_path)
        self._approach3 = EGARCHGatingApproach(db_path)
        logging.info("FifteenMShadowEngine initialized")

    def _ensure_db(self):
        if self._db_conn is not None:
            return
        self._db_conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._db_conn.row_factory = sqlite3.Row
        self._db_conn.execute("PRAGMA journal_mode=WAL")
        self._db_conn.execute("PRAGMA busy_timeout=10000")
        self._create_tables()

    def _create_tables(self):
        self._db_conn.execute("""
            CREATE TABLE IF NOT EXISTS fifteenm_shadow_signals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                event_ticker TEXT NOT NULL,
                asset TEXT NOT NULL,
                evaluation_time TEXT NOT NULL,
                -- Market state
                spot_price REAL,
                threshold REAL,
                seconds_to_close REAL,
                market_price INTEGER,
                best_bid INTEGER,
                best_ask INTEGER,
                -- Live baseline
                live_prob REAL,
                live_edge REAL,
                live_fee_edge REAL,
                -- Approach 1: Recalibrated EGARCH
                a1_raw_prob REAL,
                a1_temperature REAL,
                a1_temp_prob REAL,
                a1_blend_w REAL,
                a1_final_prob REAL,
                a1_edge REAL,
                a1_fee_edge REAL,
                a1_edge_band_blocked INTEGER,
                a1_debiased_prob REAL,
                a1_kelly_f REAL,
                a1_contracts INTEGER,
                a1_gates_passed INTEGER,
                a1_gate_failures TEXT,
                -- Approach 2: LightGBM
                a2_raw_prob REAL,
                a2_calibrated_prob REAL,
                a2_edge REAL,
                a2_fee_edge REAL,
                a2_kelly_f REAL,
                a2_contracts INTEGER,
                a2_model_version TEXT,
                a2_gates_passed INTEGER,
                a2_gate_failures TEXT,
                -- Market-only baseline
                market_only_prob REAL,
                -- Settlement
                status TEXT NOT NULL DEFAULT 'pending',
                market_result TEXT,
                live_pnl_cents INTEGER,
                a1_pnl_cents INTEGER,
                a2_pnl_cents INTEGER,
                market_only_pnl_cents INTEGER,
                settled_time TEXT
            )
        """)
        # Indices
        self._db_conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_15m_shadow_ticker "
            "ON fifteenm_shadow_signals(ticker)")
        self._db_conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_15m_shadow_status "
            "ON fifteenm_shadow_signals(status)")
        self._db_conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_15m_shadow_asset "
            "ON fifteenm_shadow_signals(asset)")
        self._db_conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_15m_shadow_dedup "
            "ON fifteenm_shadow_signals(ticker)")
        self._db_conn.commit()

        # Migration: add NO-side and A3 columns (safe to re-run)
        for col_def in [
            # NO-side live baseline
            ("no_live_prob", "REAL"), ("no_live_edge", "REAL"), ("no_live_fee_edge", "REAL"),
            # NO-side A1
            ("no_a1_final_prob", "REAL"), ("no_a1_edge", "REAL"), ("no_a1_fee_edge", "REAL"),
            ("no_a1_kelly_f", "REAL"), ("no_a1_contracts", "INTEGER"),
            ("no_a1_gates_passed", "INTEGER"), ("no_a1_gate_failures", "TEXT"),
            # NO-side A2
            ("no_a2_prob", "REAL"), ("no_a2_edge", "REAL"), ("no_a2_fee_edge", "REAL"),
            ("no_a2_kelly_f", "REAL"), ("no_a2_contracts", "INTEGER"),
            ("no_a2_gates_passed", "INTEGER"), ("no_a2_gate_failures", "TEXT"),
            # NO-side market baseline
            ("no_market_only_prob", "REAL"),
            # NO-side settlement PnL
            ("no_live_pnl_cents", "INTEGER"), ("no_a1_pnl_cents", "INTEGER"),
            ("no_a2_pnl_cents", "INTEGER"), ("no_market_only_pnl_cents", "INTEGER"),
            # Approach 3: EGARCH Gating Model (YES-side)
            ("a3_gate_prob", "REAL"), ("a3_gate_prob_raw", "REAL"),
            ("a3_gate_10", "INTEGER"), ("a3_gate_20", "INTEGER"), ("a3_gate_30", "INTEGER"),
            ("a3_model_version", "TEXT"), ("a3_no_side_warning", "TEXT"),
            # Approach 3: EGARCH Gating Model (NO-side)
            ("no_a3_gate_prob", "REAL"), ("no_a3_gate_prob_raw", "REAL"),
            ("no_a3_gate_10", "INTEGER"), ("no_a3_gate_20", "INTEGER"), ("no_a3_gate_30", "INTEGER"),
            ("no_a3_model_version", "TEXT"), ("no_a3_no_side_warning", "TEXT"),
            # A3 settlement PnL (counterfactual: what if we gated at each threshold?)
            ("a3_pnl_gate10_cents", "INTEGER"), ("a3_pnl_gate20_cents", "INTEGER"),
            ("a3_pnl_gate30_cents", "INTEGER"),
            ("no_a3_pnl_gate10_cents", "INTEGER"), ("no_a3_pnl_gate20_cents", "INTEGER"),
            ("no_a3_pnl_gate30_cents", "INTEGER"),
            # Actual NO ask from market NBBO (not derived from YES bid)
            ("no_ask", "INTEGER"),
        ]:
            try:
                self._db_conn.execute(
                    f"ALTER TABLE fifteenm_shadow_signals ADD COLUMN {col_def[0]} {col_def[1]}")
            except Exception:
                pass
        self._db_conn.commit()

    def evaluate_strike(self, asset: str, ticker: str, event_ticker: str,
                        spot_price: float, threshold: float,
                        seconds_to_close: float, market_price: int,
                        best_bid: Optional[int], best_ask: Optional[int],
                        blended_rv: float, egarch_sigma: Optional[float],
                        z_score: float, live_prob: float,
                        live_edge: float, live_fee_edge: float,
                        egarch_blend_weight: Optional[float] = None,
                        fee_adjusted_edge: Optional[float] = None,
                        kelly_f: Optional[float] = None,
                        no_ask: Optional[int] = None):
        """Run all three shadow approaches and log results. One entry per ticker."""
        if ticker in self._seen:
            return
        self._seen.add(ticker)

        # Approach 1: Recalibrated EGARCH
        a1 = self._approach1.evaluate(
            asset, spot_price, threshold, seconds_to_close,
            blended_rv, egarch_sigma, market_price, z_score, live_prob)

        # Approach 2: LightGBM
        a2 = self._approach2.evaluate(
            asset, spot_price, threshold, seconds_to_close,
            blended_rv, market_price, z_score, live_prob,
            best_bid, best_ask)

        # Approach 3: EGARCH Gating (YES-side)
        a3 = self._approach3.evaluate(
            asset, spot_price, threshold, seconds_to_close,
            blended_rv, market_price, z_score, live_prob,
            best_bid, best_ask, egarch_sigma, egarch_blend_weight,
            fee_adjusted_edge, kelly_f, side="yes")

        # Market-only baseline
        market_only_prob = market_price / 100.0

        # ── NO-side shadow evaluation ──
        # Use actual NO ask from market NBBO when available
        if no_ask is not None and no_ask > 0:
            no_price = no_ask
        else:
            no_price = None  # no NO ask available — skip NO-side
        no_live_prob = 1.0 - live_prob
        if no_price is not None:
            no_live_edge = no_live_prob - no_price / 100.0
            _no_fee_1c = math.ceil(FEE_MULTIPLIER * 1 * (no_price / 100) * (1 - no_price / 100) * 100)
            no_live_fee_edge = no_live_edge - _no_fee_1c / 100.0

            # NO-side A1: use 1-a1_final_prob as NO probability
            no_a1 = self._evaluate_no_side_approach(a1, no_price)

            # NO-side A2: use 1-a2_calibrated_prob as NO probability
            no_a2 = self._evaluate_no_side_approach(a2, no_price, is_lgbm=True)

            # Approach 3: EGARCH Gating (NO-side)
            no_a3 = self._approach3.evaluate(
                asset, spot_price, threshold, seconds_to_close,
                blended_rv, market_price, z_score, live_prob,
                best_bid, best_ask, egarch_sigma, egarch_blend_weight,
                fee_adjusted_edge, kelly_f, side="no")

            # NO-side market baseline
            no_market_only_prob = no_price / 100.0
        else:
            no_live_edge = None
            no_live_fee_edge = None
            _no_null = {"final_prob": None, "edge": None, "fee_adjusted_edge": None,
                        "kelly_f": None, "contracts": 0, "gates_passed": 0, "gate_failures": "no_ask_unavailable"}
            no_a1 = _no_null
            no_a2 = _no_null.copy()
            no_a3 = {"a3_gate_prob": None, "a3_loss_prob": None, "a3_recommendation": "skip",
                      "a3_model_status": "no_ask_unavailable", "a3_features_used": 0}
            no_market_only_prob = None

        self._log_signal(
            ticker=ticker,
            event_ticker=event_ticker,
            asset=asset,
            spot_price=spot_price,
            threshold=threshold,
            seconds_to_close=seconds_to_close,
            market_price=market_price,
            best_bid=best_bid,
            best_ask=best_ask,
            live_prob=live_prob,
            live_edge=live_edge,
            live_fee_edge=live_fee_edge,
            a1=a1,
            a2=a2,
            a3=a3,
            market_only_prob=market_only_prob,
            no_live_prob=no_live_prob,
            no_live_edge=no_live_edge,
            no_live_fee_edge=no_live_fee_edge,
            no_a1=no_a1,
            no_a2=no_a2,
            no_a3=no_a3,
            no_market_only_prob=no_market_only_prob,
            no_ask=no_ask,
        )

    @staticmethod
    def _evaluate_no_side_approach(yes_result: Dict, no_price: int,
                                   is_lgbm: bool = False) -> Dict:
        """Mirror a YES-side approach result to compute NO-side metrics."""
        # Get YES-side probability (different key for A1 vs A2)
        if is_lgbm:
            yes_prob = yes_result.get("calibrated_prob")
        else:
            yes_prob = yes_result.get("final_prob")

        if yes_prob is None or no_price <= 0 or no_price >= 100:
            return {"final_prob": None, "edge": None, "fee_adjusted_edge": None,
                    "kelly_f": None, "contracts": 0,
                    "gates_passed": 0, "gate_failures": "no_data"}

        no_prob = 1.0 - yes_prob
        no_edge = no_prob - no_price / 100.0
        _fee = math.ceil(FEE_MULTIPLIER * 1 * (no_price / 100) * (1 - no_price / 100) * 100)
        no_fee_edge = no_edge - _fee / 100.0

        # Gate check: price 86-99, fee-adjusted edge >= 1.5%
        failures = []
        if not (86 <= no_price <= 99):
            failures.append(f"price_{no_price}")
        if no_fee_edge < MIN_DEBIASED_EDGE:
            failures.append(f"edge_{no_fee_edge:.4f}")

        if failures:
            return {"final_prob": no_prob, "edge": no_edge, "fee_adjusted_edge": no_fee_edge,
                    "kelly_f": None, "contracts": 0,
                    "gates_passed": 0, "gate_failures": ",".join(failures)}

        # Kelly sizing
        b = (100 - no_price) / no_price if no_price > 0 else 0
        kelly_raw = (no_prob * b - (1 - no_prob)) / b if b > 0 else 0
        kelly_f = max(0, kelly_raw * MAX_KELLY_FRACTION)
        risk = min(kelly_f, MAX_RISK_CAP)
        contracts = int(SHADOW_BANKROLL * risk / no_price) if no_price > 0 else 0

        return {"final_prob": no_prob, "edge": no_edge, "fee_adjusted_edge": no_fee_edge,
                "kelly_f": kelly_f, "contracts": contracts,
                "gates_passed": 1, "gate_failures": None}

    def _log_signal(self, *, ticker, event_ticker, asset, spot_price, threshold,
                    seconds_to_close, market_price, best_bid, best_ask,
                    live_prob, live_edge, live_fee_edge, a1, a2, a3,
                    market_only_prob,
                    no_live_prob=None, no_live_edge=None, no_live_fee_edge=None,
                    no_a1=None, no_a2=None, no_a3=None, no_market_only_prob=None,
                    no_ask=None):
        """Write signal to DB."""
        self._ensure_db()
        now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        try:
            # Build NO-side values (may be None if not provided)
            _no_a1 = no_a1 or {}
            _no_a2 = no_a2 or {}
            _no_a3 = no_a3 or {}
            self._db_conn.execute("""
                INSERT OR REPLACE INTO fifteenm_shadow_signals (
                    ticker, event_ticker, asset, evaluation_time,
                    spot_price, threshold, seconds_to_close, market_price, best_bid, best_ask,
                    live_prob, live_edge, live_fee_edge,
                    a1_raw_prob, a1_temperature, a1_temp_prob, a1_blend_w,
                    a1_final_prob, a1_edge, a1_fee_edge, a1_edge_band_blocked,
                    a1_debiased_prob, a1_kelly_f, a1_contracts,
                    a1_gates_passed, a1_gate_failures,
                    a2_raw_prob, a2_calibrated_prob, a2_edge, a2_fee_edge,
                    a2_kelly_f, a2_contracts, a2_model_version,
                    a2_gates_passed, a2_gate_failures,
                    market_only_prob,
                    no_live_prob, no_live_edge, no_live_fee_edge,
                    no_a1_final_prob, no_a1_edge, no_a1_fee_edge,
                    no_a1_kelly_f, no_a1_contracts, no_a1_gates_passed, no_a1_gate_failures,
                    no_a2_prob, no_a2_edge, no_a2_fee_edge,
                    no_a2_kelly_f, no_a2_contracts, no_a2_gates_passed, no_a2_gate_failures,
                    no_market_only_prob,
                    a3_gate_prob, a3_gate_prob_raw, a3_gate_10, a3_gate_20, a3_gate_30,
                    a3_model_version, a3_no_side_warning,
                    no_a3_gate_prob, no_a3_gate_prob_raw, no_a3_gate_10, no_a3_gate_20, no_a3_gate_30,
                    no_a3_model_version, no_a3_no_side_warning,
                    no_ask,
                    status
                ) VALUES (
                    ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?,
                    ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?,
                    ?, ?,
                    ?,
                    ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?, ?,
                    ?, ?, ?,
                    ?, ?, ?, ?,
                    ?,
                    ?, ?, ?, ?, ?,
                    ?, ?,
                    ?, ?, ?, ?, ?,
                    ?, ?,
                    ?,
                    'pending'
                )
            """, (
                ticker, event_ticker, asset, now,
                spot_price, threshold, seconds_to_close, market_price, best_bid, best_ask,
                live_prob, live_edge, live_fee_edge,
                a1["raw_prob"], a1["temperature"], a1["temp_prob"], a1["blend_w"],
                a1["final_prob"], a1["edge"], a1["fee_adjusted_edge"], a1["edge_band_blocked"],
                a1["debiased_prob"], a1["kelly_f"], a1["contracts"],
                a1["gates_passed"], a1["gate_failures"],
                a2["raw_prob"], a2["calibrated_prob"], a2["edge"], a2["fee_adjusted_edge"],
                a2["kelly_f"], a2["contracts"], a2["model_version"],
                a2["gates_passed"], a2["gate_failures"],
                market_only_prob,
                no_live_prob, no_live_edge, no_live_fee_edge,
                _no_a1.get("final_prob"), _no_a1.get("edge"), _no_a1.get("fee_adjusted_edge"),
                _no_a1.get("kelly_f"), _no_a1.get("contracts", 0),
                _no_a1.get("gates_passed", 0), _no_a1.get("gate_failures"),
                _no_a2.get("final_prob"), _no_a2.get("edge"), _no_a2.get("fee_adjusted_edge"),
                _no_a2.get("kelly_f"), _no_a2.get("contracts", 0),
                _no_a2.get("gates_passed", 0), _no_a2.get("gate_failures"),
                no_market_only_prob,
                a3.get("gate_prob"), a3.get("gate_prob_raw"),
                a3.get("gate_10"), a3.get("gate_20"), a3.get("gate_30"),
                a3.get("model_version"), a3.get("no_side_warning"),
                _no_a3.get("gate_prob"), _no_a3.get("gate_prob_raw"),
                _no_a3.get("gate_10"), _no_a3.get("gate_20"), _no_a3.get("gate_30"),
                _no_a3.get("model_version"), _no_a3.get("no_side_warning"),
                no_ask,
            ))
            self._db_conn.commit()
        except Exception:
            logging.warning("fifteenm_shadow log_signal failed for %s", ticker, exc_info=True)

        # JSONL journal backup
        try:
            with open("fifteenm_shadow_journal.jsonl", "a") as f:
                f.write(json.dumps({
                    "ts": now, "ticker": ticker, "asset": asset,
                    "market_price": market_price, "stc": seconds_to_close,
                    "live_prob": live_prob, "live_edge": live_edge,
                    "a1_prob": a1["final_prob"], "a1_edge": a1["edge"],
                    "a1_gates": a1["gates_passed"], "a1_contracts": a1["contracts"],
                    "a2_prob": a2.get("calibrated_prob"), "a2_edge": a2.get("edge"),
                    "a2_gates": a2["gates_passed"], "a2_contracts": a2["contracts"],
                    "a3_gate_prob": a3.get("gate_prob"),
                    "a3_gate_10": a3.get("gate_10"), "a3_gate_20": a3.get("gate_20"),
                    "a3_gate_30": a3.get("gate_30"),
                    "no_a3_gate_prob": _no_a3.get("gate_prob"),
                }) + "\n")
        except Exception:
            pass

    def settle_signals(self, ticker: str, market_result: str):
        """Settle a shadow signal when the market resolves."""
        self._ensure_db()
        try:
            row = self._db_conn.execute(
                "SELECT * FROM fifteenm_shadow_signals WHERE ticker = ? AND status = 'pending'",
                (ticker,)
            ).fetchone()
            if not row:
                return

            result_yes = market_result in ("yes", "all_yes")
            result_no = market_result in ("no", "all_no")
            mp = row["market_price"] or 0
            # Use stored actual NO ask from market NBBO. Fallback to 100-best_bid for old data.
            _stored_no_ask = row["no_ask"] if "no_ask" in row.keys() else None
            if _stored_no_ask and _stored_no_ask > 0:
                no_mp = _stored_no_ask
            else:
                _bb = row["best_bid"]
                no_mp = (100 - _bb) if (_bb and _bb > 0) else (100 - mp)
            now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

            def _compute_pnl(contracts, price, is_no_side=False):
                """PnL for a position: win/loss depends on side."""
                if contracts <= 0:
                    return 0
                fee = math.ceil(FEE_MULTIPLIER * contracts * (price / 100) * (1 - price / 100) * 100)
                # NO-side wins when result is "no", YES-side wins when result is "yes"
                won = result_no if is_no_side else result_yes
                if won:
                    return (100 - price) * contracts - fee
                else:
                    return -price * contracts - fee

            live_pnl = _compute_pnl(1, mp)
            a1_pnl = _compute_pnl(row["a1_contracts"] or 0, mp)
            a2_pnl = _compute_pnl(row["a2_contracts"] or 0, mp)
            mkt_pnl = _compute_pnl(1, mp)

            # NO-side PnL
            no_live_pnl = _compute_pnl(1, no_mp, is_no_side=True)
            no_a1_pnl = _compute_pnl(row["no_a1_contracts"] or 0, no_mp, is_no_side=True)
            no_a2_pnl = _compute_pnl(row["no_a2_contracts"] or 0, no_mp, is_no_side=True)
            no_mkt_pnl = _compute_pnl(1, no_mp, is_no_side=True)

            # A3 gating counterfactual: PnL if gated at each threshold
            # If gated → PnL = 0 (trade blocked). If not gated → PnL = live_pnl.
            # YES-side
            a3_pnl_g10 = 0 if row["a3_gate_10"] else live_pnl
            a3_pnl_g20 = 0 if row["a3_gate_20"] else live_pnl
            a3_pnl_g30 = 0 if row["a3_gate_30"] else live_pnl
            # NO-side
            no_a3_pnl_g10 = 0 if row["no_a3_gate_10"] else no_live_pnl
            no_a3_pnl_g20 = 0 if row["no_a3_gate_20"] else no_live_pnl
            no_a3_pnl_g30 = 0 if row["no_a3_gate_30"] else no_live_pnl

            self._db_conn.execute(
                "UPDATE fifteenm_shadow_signals SET status='settled', market_result=?, "
                "live_pnl_cents=?, a1_pnl_cents=?, a2_pnl_cents=?, market_only_pnl_cents=?, "
                "no_live_pnl_cents=?, no_a1_pnl_cents=?, no_a2_pnl_cents=?, no_market_only_pnl_cents=?, "
                "a3_pnl_gate10_cents=?, a3_pnl_gate20_cents=?, a3_pnl_gate30_cents=?, "
                "no_a3_pnl_gate10_cents=?, no_a3_pnl_gate20_cents=?, no_a3_pnl_gate30_cents=?, "
                "settled_time=? WHERE ticker = ? AND status = 'pending'",
                (market_result, live_pnl, a1_pnl, a2_pnl, mkt_pnl,
                 no_live_pnl, no_a1_pnl, no_a2_pnl, no_mkt_pnl,
                 a3_pnl_g10, a3_pnl_g20, a3_pnl_g30,
                 no_a3_pnl_g10, no_a3_pnl_g20, no_a3_pnl_g30,
                 now, ticker)
            )
            self._db_conn.commit()
        except Exception:
            logging.debug("fifteenm_shadow settle failed for %s", ticker, exc_info=True)

    def cleanup_expired(self, active_tickers: set):
        """Remove stale ticker tracking."""
        self._seen = {t for t in self._seen if t in active_tickers}

    def get_dashboard_data(self) -> Dict:
        """Return structured data for dashboard."""
        self._ensure_db()
        result = {
            "enabled": True,
            "approach1": {
                "name": "recalibrated_egarch",
                "metrics": self._approach1.get_metrics(),
                "settled": {},
            },
            "approach2": {
                "name": "lightgbm",
                "metrics": self._approach2.get_metrics(),
                "settled": {},
            },
            "approach3": {
                "name": "egarch_gating",
                "metrics": self._approach3.get_metrics(),
                "settled": {},
            },
            "pending": {},
        }
        try:
            # Per-asset settled stats for Approach 1 & 2
            for asset in ("BTC", "ETH", "SOL", "XRP"):
                for approach_key, contracts_col, pnl_col, gates_col in [
                    ("approach1", "a1_contracts", "a1_pnl_cents", "a1_gates_passed"),
                    ("approach2", "a2_contracts", "a2_pnl_cents", "a2_gates_passed"),
                ]:
                    rows = self._db_conn.execute(
                        f"SELECT market_result, {pnl_col}, {gates_col} "
                        "FROM fifteenm_shadow_signals "
                        "WHERE asset = ? AND status = 'settled'",
                        (asset,)
                    ).fetchall()
                    if not rows:
                        continue
                    # Only count signals where gates passed
                    traded = [r for r in rows if r[gates_col]]
                    n = len(traded)
                    wins = sum(1 for r in traded if r["market_result"] in ("yes", "all_yes"))
                    pnl = sum(r[pnl_col] or 0 for r in traded)
                    result[approach_key]["settled"][asset] = {
                        "n": n,
                        "total_evaluated": len(rows),
                        "wins": wins,
                        "wr": round(wins / n * 100, 1) if n > 0 else 0,
                        "pnl_cents": pnl,
                    }

                # Approach 3 gating analysis per asset
                a3_rows = self._db_conn.execute(
                    "SELECT market_result, live_pnl_cents, a3_gate_prob, "
                    "  a3_gate_10, a3_gate_20, a3_gate_30, "
                    "  a3_pnl_gate10_cents, a3_pnl_gate20_cents, a3_pnl_gate30_cents "
                    "FROM fifteenm_shadow_signals "
                    "WHERE asset = ? AND status = 'settled' AND a3_gate_prob IS NOT NULL",
                    (asset,)
                ).fetchall()
                if a3_rows:
                    n_total = len(a3_rows)
                    live_pnl_total = sum(r["live_pnl_cents"] or 0 for r in a3_rows)
                    # Per-threshold analysis
                    thresholds_data = {}
                    for t_name, gate_col, pnl_col in [
                        ("10pct", "a3_gate_10", "a3_pnl_gate10_cents"),
                        ("20pct", "a3_gate_20", "a3_pnl_gate20_cents"),
                        ("30pct", "a3_gate_30", "a3_pnl_gate30_cents"),
                    ]:
                        gated = sum(1 for r in a3_rows if r[gate_col])
                        gated_pnl = live_pnl_total - sum(r[pnl_col] or 0 for r in a3_rows)
                        # Break down: losses avoided vs wins missed
                        losses_avoided = sum(1 for r in a3_rows
                                             if r[gate_col] and (r["live_pnl_cents"] or 0) < 0)
                        wins_missed = sum(1 for r in a3_rows
                                          if r[gate_col] and (r["live_pnl_cents"] or 0) > 0)
                        net_pnl = sum(r[pnl_col] or 0 for r in a3_rows)
                        thresholds_data[t_name] = {
                            "gated": gated,
                            "gate_rate": round(gated / n_total * 100, 1) if n_total > 0 else 0,
                            "losses_avoided": losses_avoided,
                            "wins_missed": wins_missed,
                            "net_pnl_cents": net_pnl,
                            "pnl_vs_baseline": net_pnl - live_pnl_total,
                        }
                    result["approach3"]["settled"][asset] = {
                        "n": n_total,
                        "live_pnl_cents": live_pnl_total,
                        "thresholds": thresholds_data,
                    }

            # Pending counts per asset
            pending = self._db_conn.execute(
                "SELECT asset, COUNT(*) as cnt FROM fifteenm_shadow_signals "
                "WHERE status = 'pending' GROUP BY asset"
            ).fetchall()
            for r in pending:
                result["pending"][r["asset"]] = r["cnt"]

            # NO-side shadow stats
            no_side = {}
            for asset in ("BTC", "ETH", "SOL", "XRP"):
                for approach_key, contracts_col, pnl_col, gates_col in [
                    ("no_approach1", "no_a1_contracts", "no_a1_pnl_cents", "no_a1_gates_passed"),
                    ("no_approach2", "no_a2_contracts", "no_a2_pnl_cents", "no_a2_gates_passed"),
                ]:
                    rows = self._db_conn.execute(
                        f"SELECT market_result, {pnl_col}, {gates_col} "
                        "FROM fifteenm_shadow_signals "
                        "WHERE asset = ? AND status = 'settled'",
                        (asset,)
                    ).fetchall()
                    if not rows:
                        continue
                    traded = [r for r in rows if r[gates_col]]
                    n = len(traded)
                    # NO-side wins when result is "no"
                    wins = sum(1 for r in traded if r["market_result"] in ("no", "all_no"))
                    pnl = sum(r[pnl_col] or 0 for r in traded)
                    no_side.setdefault(approach_key, {})[asset] = {
                        "n": n, "wins": wins,
                        "wr": round(wins / n * 100, 1) if n > 0 else 0,
                        "pnl_cents": pnl,
                    }

                # NO-side A3 gating
                no_a3_rows = self._db_conn.execute(
                    "SELECT market_result, no_live_pnl_cents, no_a3_gate_prob, "
                    "  no_a3_gate_10, no_a3_gate_20, no_a3_gate_30, "
                    "  no_a3_pnl_gate10_cents, no_a3_pnl_gate20_cents, no_a3_pnl_gate30_cents "
                    "FROM fifteenm_shadow_signals "
                    "WHERE asset = ? AND status = 'settled' AND no_a3_gate_prob IS NOT NULL",
                    (asset,)
                ).fetchall()
                if no_a3_rows:
                    n_total = len(no_a3_rows)
                    no_live_pnl_total = sum(r["no_live_pnl_cents"] or 0 for r in no_a3_rows)
                    thresholds_data = {}
                    for t_name, gate_col, pnl_col in [
                        ("10pct", "no_a3_gate_10", "no_a3_pnl_gate10_cents"),
                        ("20pct", "no_a3_gate_20", "no_a3_pnl_gate20_cents"),
                        ("30pct", "no_a3_gate_30", "no_a3_pnl_gate30_cents"),
                    ]:
                        gated = sum(1 for r in no_a3_rows if r[gate_col])
                        losses_avoided = sum(1 for r in no_a3_rows
                                             if r[gate_col] and (r["no_live_pnl_cents"] or 0) < 0)
                        wins_missed = sum(1 for r in no_a3_rows
                                          if r[gate_col] and (r["no_live_pnl_cents"] or 0) > 0)
                        net_pnl = sum(r[pnl_col] or 0 for r in no_a3_rows)
                        thresholds_data[t_name] = {
                            "gated": gated,
                            "gate_rate": round(gated / n_total * 100, 1) if n_total > 0 else 0,
                            "losses_avoided": losses_avoided,
                            "wins_missed": wins_missed,
                            "net_pnl_cents": net_pnl,
                            "pnl_vs_baseline": net_pnl - no_live_pnl_total,
                        }
                    no_side.setdefault("no_approach3", {})[asset] = {
                        "n": n_total,
                        "no_live_pnl_cents": no_live_pnl_total,
                        "thresholds": thresholds_data,
                    }
            result["no_side"] = no_side

        except Exception:
            logging.debug("fifteenm_shadow dashboard data failed", exc_info=True)

        return result
