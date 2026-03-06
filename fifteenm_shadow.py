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
FEE_MULTIPLIER = 0.0175  # maker fee rate


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
            conn = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=10000")
            for asset in ("BTC", "ETH", "SOL", "XRP"):
                rows = conn.execute(
                    "SELECT calibrated_prob, market_result FROM evaluated_opportunities "
                    "WHERE asset = ? AND product_type IS NULL AND status = 'settled' "
                    "AND filter_stage IN ('candidate', 'xrp_shadow', 'stc_shadow', "
                    "  'stc_shadow_no_xrp', 'stc_shadow_promoted') "
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

            conn = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=10000")
            rows = conn.execute(
                "SELECT calibrated_prob, market_price, volatility, z_score, "
                "  seconds_to_close, spot_price, threshold, market_result "
                "FROM evaluated_opportunities "
                "WHERE asset = ? AND product_type IS NULL AND status = 'settled' "
                "AND filter_stage IN ('candidate', 'xrp_shadow', 'stc_shadow', "
                "  'stc_shadow_no_xrp', 'stc_shadow_promoted') "
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


class FifteenMShadowEngine:
    """Shadow evaluation engine for 15M markets — two approaches."""

    def __init__(self, db_path: str = DB_PATH):
        self._db_path = db_path
        self._db_conn: Optional[sqlite3.Connection] = None
        self._seen: set = set()
        self._approach1 = RecalibratedEGARCHApproach(db_path)
        self._approach2 = LightGBMApproach(db_path)
        logging.info("FifteenMShadowEngine initialized")

    def _ensure_db(self):
        if self._db_conn is not None:
            return
        self._db_conn = sqlite3.connect(self._db_path)
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

    def evaluate_strike(self, asset: str, ticker: str, event_ticker: str,
                        spot_price: float, threshold: float,
                        seconds_to_close: float, market_price: int,
                        best_bid: Optional[int], best_ask: Optional[int],
                        blended_rv: float, egarch_sigma: Optional[float],
                        z_score: float, live_prob: float,
                        live_edge: float, live_fee_edge: float):
        """Run both shadow approaches and log results. One entry per ticker."""
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

        # Market-only baseline
        market_only_prob = market_price / 100.0

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
            market_only_prob=market_only_prob,
        )

    def _log_signal(self, *, ticker, event_ticker, asset, spot_price, threshold,
                    seconds_to_close, market_price, best_bid, best_ask,
                    live_prob, live_edge, live_fee_edge, a1, a2, market_only_prob):
        """Write signal to DB."""
        self._ensure_db()
        now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        try:
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
            ))
            self._db_conn.commit()
        except Exception:
            logging.debug("fifteenm_shadow log_signal failed for %s", ticker, exc_info=True)

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
            mp = row["market_price"] or 0
            now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

            def _compute_pnl(contracts, price):
                """PnL for a YES position: win = (100-price)*contracts, lose = -price*contracts, minus fee."""
                if contracts <= 0:
                    return 0
                fee = math.ceil(FEE_MULTIPLIER * contracts * (price / 100) * (1 - price / 100) * 100)
                if result_yes:
                    return (100 - price) * contracts - fee
                else:
                    return -price * contracts - fee

            live_pnl = _compute_pnl(1, mp)  # normalized to 1 contract for comparison
            a1_pnl = _compute_pnl(row["a1_contracts"] or 0, mp)
            a2_pnl = _compute_pnl(row["a2_contracts"] or 0, mp)
            mkt_pnl = _compute_pnl(1, mp)  # market-only = same as live baseline at same price

            self._db_conn.execute(
                "UPDATE fifteenm_shadow_signals SET status='settled', market_result=?, "
                "live_pnl_cents=?, a1_pnl_cents=?, a2_pnl_cents=?, market_only_pnl_cents=?, "
                "settled_time=? WHERE ticker = ? AND status = 'pending'",
                (market_result, live_pnl, a1_pnl, a2_pnl, mkt_pnl, now, ticker)
            )
            self._db_conn.commit()
        except Exception:
            logging.debug("fifteenm_shadow settle failed for %s", ticker, exc_info=True)

    def cleanup_expired(self, active_tickers: set):
        """Remove stale ticker tracking."""
        self._seen = {t for t in self._seen if t in active_tickers}

    def get_dashboard_data(self) -> Dict:
        """Return structured data for Firebase dashboard."""
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
            "pending": {},
        }
        try:
            # Per-asset settled stats for Approach 1
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

            # Pending counts per asset
            pending = self._db_conn.execute(
                "SELECT asset, COUNT(*) as cnt FROM fifteenm_shadow_signals "
                "WHERE status = 'pending' GROUP BY asset"
            ).fetchall()
            for r in pending:
                result["pending"][r["asset"]] = r["cnt"]

        except Exception:
            logging.debug("fifteenm_shadow dashboard data failed", exc_info=True)

        return result
