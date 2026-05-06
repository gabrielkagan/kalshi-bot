#!/usr/bin/env python3
"""Standalone tests for HAR/EGARCH buffer persistence across restarts.

Copies minimal estimator logic inline to avoid importing bot.py.

NOTE: HAR model was deleted from production. HAR tests here cover dead code
but are kept to verify the buffer persistence pattern (EGARCH uses the same
pattern and is still live).

DRIFT RISK: The EGARCHEstimator here is a simplified inline copy focused on
buffer save/load. The production class (models.py) has additional fields
(version, last_refit_per_asset, threading locks). If the state format changes,
these tests may drift.

Run: python3 test_buffer_persistence.py
"""

import json
import math
import os
import sys
import tempfile
import time
import unittest
from collections import deque
from typing import Dict, List, Optional, Any
from unittest.mock import patch, MagicMock
import logging

# ── Constants (mirrored from bot.py) ────────────────────────────────────

ASSETS = ["BTC", "ETH", "SOL", "XRP"]
HAR_OBSERVATION_MAXLEN = 288
HAR_OBSERVATION_INTERVAL = 300
HAR_BUFFER_SAVE_INTERVAL = 300.0
EGARCH_RETURN_MAXLEN = 10800
EGARCH_BUFFER_SAVE_INTERVAL = 300.0

# Use temp dir for test state files
_TMPDIR = tempfile.mkdtemp()
HAR_STATE_PATH = os.path.join(_TMPDIR, "har_state_test.json")
EGARCH_STATE_PATH = os.path.join(_TMPDIR, "egarch_state_test.json")


# ═══════════════════════════════════════════════════════════════════════════
#  Minimal HAREstimator (save/load + record_observation buffer logic)
# ═══════════════════════════════════════════════════════════════════════════

class HAREstimator:
    def __init__(self):
        self._observations: Dict[str, deque] = {
            a: deque(maxlen=HAR_OBSERVATION_MAXLEN) for a in ASSETS
        }
        self._last_obs_time: Dict[str, float] = {}
        self._last_refit: float = 0.0
        self._last_buffer_save: float = 0.0
        self._active_model: Dict[str, str] = {a: "fixed" for a in ASSETS}
        self._coefficients: Dict[str, Dict[str, List[float]]] = {a: {} for a in ASSETS}
        self._qlike_scores: Dict[str, Dict[str, float]] = {a: {} for a in ASSETS}
        self._load_state()

    def record_observation(self, asset: str, obs: dict) -> None:
        """Simplified: just append obs dict and check periodic save."""
        self._observations[asset].append(obs)
        now = time.time()
        if now - self._last_buffer_save >= HAR_BUFFER_SAVE_INTERVAL:
            self._save_state()
            self._last_buffer_save = now

    def _load_state(self) -> None:
        try:
            with open(HAR_STATE_PATH, "r") as f:
                state = json.load(f)
            for asset in ASSETS:
                if asset in state.get("active_model", {}):
                    self._active_model[asset] = state["active_model"][asset]
                if asset in state.get("coefficients", {}):
                    self._coefficients[asset] = state["coefficients"][asset]
                if asset in state.get("qlike_scores", {}):
                    self._qlike_scores[asset] = state["qlike_scores"][asset]
            self._last_refit = state.get("last_refit", 0.0)
            # Restore observation buffers
            obs_data = state.get("observations", {})
            now = time.time()
            oldest_age = 0.0
            for asset in ASSETS:
                try:
                    asset_obs = obs_data.get(asset, [])
                    if not isinstance(asset_obs, list):
                        raise ValueError(f"expected list, got {type(asset_obs).__name__}")
                    for obs in asset_obs:
                        if not isinstance(obs, dict):
                            raise ValueError(f"expected dict, got {type(obs).__name__}")
                        self._observations[asset].append(obs)
                    if asset_obs:
                        first_ts = asset_obs[0].get("ts", now)
                        oldest_age = max(oldest_age, now - first_ts)
                except Exception as oe:
                    logging.warning("HAR observations load failed for %s: %s (starting fresh)", asset, oe)
                    self._observations[asset].clear()
            counts = {a: len(self._observations[a]) for a in ASSETS}
            if any(counts.values()):
                logging.info(
                    "HAR observations restored: BTC=%d ETH=%d SOL=%d XRP=%d (oldest=%.0fs ago)",
                    counts["BTC"], counts["ETH"], counts["SOL"], counts["XRP"], oldest_age)
        except (FileNotFoundError, json.JSONDecodeError):
            pass
        except Exception as e:
            logging.warning("HAREstimator: failed to load state: %s", e)

    def _save_state(self) -> None:
        state = {
            "active_model": self._active_model,
            "coefficients": self._coefficients,
            "qlike_scores": self._qlike_scores,
            "last_refit": self._last_refit,
            "observations": {a: list(self._observations[a]) for a in ASSETS},
        }
        try:
            tmp = HAR_STATE_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(state, f, indent=2)
            os.replace(tmp, HAR_STATE_PATH)
        except Exception as e:
            logging.warning("HAREstimator: failed to save state: %s", e)


# ═══════════════════════════════════════════════════════════════════════════
#  Minimal EGARCHEstimator (save/load + record_return buffer logic)
# ═══════════════════════════════════════════════════════════════════════════

class EGARCHEstimator:
    def __init__(self):
        self._returns: Dict[str, deque] = {
            a: deque(maxlen=EGARCH_RETURN_MAXLEN) for a in ASSETS
        }
        self._params: Dict[str, Optional[Dict]] = {a: None for a in ASSETS}
        self._log_var: Dict[str, Optional[float]] = {a: None for a in ASSETS}
        self._sigma: Dict[str, Optional[float]] = {a: None for a in ASSETS}
        self._last_refit: float = time.time()
        self._n_updates: Dict[str, int] = {a: 0 for a in ASSETS}
        self._mle_loglik: Dict[str, Optional[float]] = {a: None for a in ASSETS}
        self._mle_converged: Dict[str, bool] = {a: False for a in ASSETS}
        self._last_buffer_save: float = 0.0
        self._load_state()

    def record_return(self, asset: str, log_return: float):
        self._returns[asset].append(log_return)
        now = time.time()
        if now - self._last_buffer_save >= EGARCH_BUFFER_SAVE_INTERVAL:
            self._save_state()
            self._last_buffer_save = now

    def _load_state(self):
        if not os.path.exists(EGARCH_STATE_PATH):
            return
        try:
            with open(EGARCH_STATE_PATH, "r") as f:
                state = json.load(f)
            for asset in ASSETS:
                adata = state.get(asset)
                if adata and adata.get("params"):
                    self._params[asset] = adata["params"]
                    self._log_var[asset] = adata.get("log_var")
                    self._sigma[asset] = adata.get("sigma")
                    self._n_updates[asset] = adata.get("n_updates", 0)
                    self._mle_loglik[asset] = adata.get("mle_loglik")
                    self._mle_converged[asset] = adata.get("mle_converged", False)
                # Restore return buffer
                if adata:
                    try:
                        for r in adata.get("returns", []):
                            self._returns[asset].append(r)
                    except Exception as re:
                        logging.warning("EGARCH returns load failed for %s: %s (starting fresh)", asset, re)
                        self._returns[asset].clear()
            self._last_refit = state.get("last_refit", 0.0)
        except Exception as e:
            logging.warning("EGARCH state load failed: %s", e)

    def _save_state(self):
        state = {"last_refit": self._last_refit}
        for asset in ASSETS:
            state[asset] = {
                "params": self._params[asset],
                "log_var": self._log_var[asset],
                "sigma": self._sigma[asset],
                "n_updates": self._n_updates[asset],
                "mle_loglik": self._mle_loglik[asset],
                "mle_converged": self._mle_converged[asset],
                "returns": list(self._returns[asset]),
            }
        tmp_path = EGARCH_STATE_PATH + ".tmp"
        try:
            with open(tmp_path, "w") as f:
                json.dump(state, f, indent=2)
            os.replace(tmp_path, EGARCH_STATE_PATH)
        except Exception as e:
            logging.warning("EGARCH state save failed: %s", e)


# ═══════════════════════════════════════════════════════════════════════════
#  Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _make_obs(ts=None, dvol_sq=None):
    """Generate a realistic HAR observation dict."""
    t = ts or time.time()
    return {
        "ts": t,
        "rv1_sq": 1.23e-8,
        "rv5_sq": 2.34e-8,
        "rv15_sq": 3.45e-8,
        "jump_sq": 0.5e-9,
        "sv_pos_1": 6.1e-9, "sv_neg_1": 6.2e-9,
        "sv_pos_5": 1.1e-8, "sv_neg_5": 1.2e-8,
        "sv_pos_15": 1.7e-8, "sv_neg_15": 1.8e-8,
        "dvol_sq": dvol_sq,
    }


def _cleanup_files():
    for p in [HAR_STATE_PATH, HAR_STATE_PATH + ".tmp",
              EGARCH_STATE_PATH, EGARCH_STATE_PATH + ".tmp"]:
        try:
            os.remove(p)
        except FileNotFoundError:
            pass


# ═══════════════════════════════════════════════════════════════════════════
#  Tests
# ═══════════════════════════════════════════════════════════════════════════

class TestHARBufferRoundtrip(unittest.TestCase):
    """Category A: HAR Save/Load Roundtrip"""

    def setUp(self):
        _cleanup_files()

    def tearDown(self):
        _cleanup_files()

    def test_save_load_observations(self):
        """Save 50 observations, reload into new estimator, verify all restored."""
        est = HAREstimator()
        for i in range(50):
            est.record_observation("BTC", _make_obs(ts=time.time() - 50 + i))
        est._save_state()

        est2 = HAREstimator()  # loads from file
        self.assertEqual(len(est2._observations["BTC"]), 50)
        # Verify field values match
        for i in range(50):
            self.assertEqual(est2._observations["BTC"][i]["rv1_sq"],
                             est._observations["BTC"][i]["rv1_sq"])
            self.assertAlmostEqual(est2._observations["BTC"][i]["ts"],
                                   est._observations["BTC"][i]["ts"])

    def test_deque_maxlen_respected(self):
        """Save 300 observations (> maxlen 288), load → only latest 288 kept."""
        est = HAREstimator()
        for i in range(300):
            est._observations["ETH"].append(_make_obs(ts=time.time() - 300 + i))
        est._save_state()

        est2 = HAREstimator()
        # Deque maxlen truncates on append during load
        self.assertEqual(len(est2._observations["ETH"]), 288)

    def test_backward_compat_no_observations_key(self):
        """Load a state file WITHOUT observations key → empty, no crash."""
        state = {
            "active_model": {"BTC": "level_har"},
            "coefficients": {"BTC": {"level_har": [0.1, 0.2, 0.3, 0.4]}},
            "qlike_scores": {},
            "last_refit": time.time() - 100,
        }
        with open(HAR_STATE_PATH, "w") as f:
            json.dump(state, f)

        est = HAREstimator()
        self.assertEqual(len(est._observations["BTC"]), 0)
        self.assertEqual(est._active_model["BTC"], "level_har")
        self.assertEqual(est._coefficients["BTC"]["level_har"], [0.1, 0.2, 0.3, 0.4])

    def test_corrupt_observations_graceful(self):
        """One asset has non-list observations → skip that asset, others OK."""
        est = HAREstimator()
        for i in range(10):
            est.record_observation("BTC", _make_obs())
            est.record_observation("ETH", _make_obs())
        est._save_state()

        # Corrupt BTC observations to be a string
        with open(HAR_STATE_PATH, "r") as f:
            state = json.load(f)
        state["observations"]["BTC"] = "corrupt"
        with open(HAR_STATE_PATH, "w") as f:
            json.dump(state, f)

        est2 = HAREstimator()
        # BTC should be empty (failed), ETH should be restored
        self.assertEqual(len(est2._observations["BTC"]), 0)
        self.assertEqual(len(est2._observations["ETH"]), 10)


class TestEGARCHBufferRoundtrip(unittest.TestCase):
    """Category B: EGARCH Save/Load Roundtrip"""

    def setUp(self):
        _cleanup_files()

    def tearDown(self):
        _cleanup_files()

    def test_save_load_returns(self):
        """Record 5000 returns, save, reload → verify all restored."""
        est = EGARCHEstimator()
        returns = [i * 1e-6 for i in range(5000)]
        for r in returns:
            est._returns["BTC"].append(r)
        est._save_state()

        est2 = EGARCHEstimator()
        self.assertEqual(len(est2._returns["BTC"]), 5000)
        for i in range(5000):
            self.assertEqual(est2._returns["BTC"][i], returns[i])

    def test_deque_maxlen_respected(self):
        """Save 12000 returns (> maxlen 10800), load → only latest 10800."""
        est = EGARCHEstimator()
        for i in range(12000):
            est._returns["SOL"].append(float(i))
        est._save_state()

        est2 = EGARCHEstimator()
        self.assertEqual(len(est2._returns["SOL"]), 10800)
        # Should be the latest 10800, i.e., 1200..11999
        self.assertEqual(est2._returns["SOL"][0], 1200.0)
        self.assertEqual(est2._returns["SOL"][-1], 11999.0)

    def test_backward_compat_no_returns_key(self):
        """Load state WITHOUT returns key → empty, params still loaded."""
        state = {
            "last_refit": time.time() - 200,
            "BTC": {
                "params": {"omega": -0.5, "alpha": 0.1, "gamma": 0.0, "beta": 0.95},
                "log_var": -20.0, "sigma": 0.001,
                "n_updates": 100, "mle_loglik": -5.0, "mle_converged": True,
            }
        }
        with open(EGARCH_STATE_PATH, "w") as f:
            json.dump(state, f)

        est = EGARCHEstimator()
        self.assertEqual(len(est._returns["BTC"]), 0)
        self.assertIsNotNone(est._params["BTC"])
        self.assertAlmostEqual(est._params["BTC"]["omega"], -0.5)

    def test_mixed_state_partial_restore(self):
        """Some assets have returns, others don't → partial restore."""
        est = EGARCHEstimator()
        for i in range(100):
            est._returns["BTC"].append(float(i) * 1e-6)
        # ETH has no returns
        est._save_state()

        est2 = EGARCHEstimator()
        self.assertEqual(len(est2._returns["BTC"]), 100)
        self.assertEqual(len(est2._returns["ETH"]), 0)


class TestPeriodicSaveTrigger(unittest.TestCase):
    """Category C: Periodic Save Trigger"""

    def setUp(self):
        _cleanup_files()

    def tearDown(self):
        _cleanup_files()

    def test_har_saves_after_interval(self):
        """HAR: _save_state called when interval elapsed."""
        est = HAREstimator()
        est._last_buffer_save = time.time() - HAR_BUFFER_SAVE_INTERVAL - 1  # expired
        with patch.object(est, '_save_state', wraps=est._save_state) as mock_save:
            est.record_observation("BTC", _make_obs())
            mock_save.assert_called_once()

    def test_egarch_saves_after_interval(self):
        """EGARCH: _save_state called when interval elapsed."""
        est = EGARCHEstimator()
        est._last_buffer_save = time.time() - EGARCH_BUFFER_SAVE_INTERVAL - 1
        with patch.object(est, '_save_state', wraps=est._save_state) as mock_save:
            est.record_return("BTC", 0.001)
            mock_save.assert_called_once()

    def test_no_premature_save(self):
        """No save before interval elapsed."""
        est = HAREstimator()
        est._last_buffer_save = time.time()  # just saved
        with patch.object(est, '_save_state') as mock_save:
            est.record_observation("BTC", _make_obs())
            mock_save.assert_not_called()


class TestShutdownPersistence(unittest.TestCase):
    """Category D: Shutdown Persistence"""

    def setUp(self):
        _cleanup_files()

    def tearDown(self):
        _cleanup_files()

    def test_shutdown_saves_both_buffers(self):
        """Simulate _cleanup saving both estimators' buffers."""
        har = HAREstimator()
        egarch = EGARCHEstimator()
        for i in range(20):
            har.record_observation("BTC", _make_obs())
            egarch._returns["ETH"].append(float(i) * 1e-6)

        # Simulate what _cleanup does
        har._save_state()
        egarch._save_state()

        # Verify files contain buffer data
        with open(HAR_STATE_PATH) as f:
            har_state = json.load(f)
        self.assertEqual(len(har_state["observations"]["BTC"]), 20)

        with open(EGARCH_STATE_PATH) as f:
            eg_state = json.load(f)
        self.assertEqual(len(eg_state["ETH"]["returns"]), 20)

    def test_shutdown_with_empty_buffers(self):
        """_cleanup with no data → no crash, state files written."""
        har = HAREstimator()
        egarch = EGARCHEstimator()
        har._save_state()
        egarch._save_state()

        self.assertTrue(os.path.exists(HAR_STATE_PATH))
        self.assertTrue(os.path.exists(EGARCH_STATE_PATH))

        with open(HAR_STATE_PATH) as f:
            har_state = json.load(f)
        for asset in ASSETS:
            self.assertEqual(len(har_state["observations"][asset]), 0)


class TestDataIntegrity(unittest.TestCase):
    """Category E: Data Integrity"""

    def setUp(self):
        _cleanup_files()

    def tearDown(self):
        _cleanup_files()

    def test_float_precision(self):
        """EGARCH returns with 15-digit precision survive JSON roundtrip."""
        est = EGARCHEstimator()
        precise_vals = [
            1.234567890123456e-10,
            -9.87654321098765e-11,
            3.14159265358979e-12,
        ]
        for v in precise_vals:
            est._returns["XRP"].append(v)
        est._save_state()

        est2 = EGARCHEstimator()
        for i, v in enumerate(precise_vals):
            self.assertEqual(est2._returns["XRP"][i], v,
                             f"Float precision lost at index {i}")

    def test_observation_dict_integrity(self):
        """HAR observations with all 12 fields including None dvol_sq survive roundtrip."""
        est = HAREstimator()
        obs = _make_obs(ts=1708700000.123456, dvol_sq=None)
        est.record_observation("SOL", obs)
        est._save_state()

        est2 = HAREstimator()
        restored = est2._observations["SOL"][0]
        self.assertIsNone(restored["dvol_sq"])
        self.assertEqual(restored["ts"], obs["ts"])
        self.assertEqual(restored["rv1_sq"], obs["rv1_sq"])
        self.assertEqual(restored["rv5_sq"], obs["rv5_sq"])
        self.assertEqual(restored["rv15_sq"], obs["rv15_sq"])
        self.assertEqual(restored["jump_sq"], obs["jump_sq"])
        self.assertEqual(restored["sv_pos_1"], obs["sv_pos_1"])
        self.assertEqual(restored["sv_neg_1"], obs["sv_neg_1"])
        self.assertEqual(restored["sv_pos_5"], obs["sv_pos_5"])
        self.assertEqual(restored["sv_neg_5"], obs["sv_neg_5"])
        self.assertEqual(restored["sv_pos_15"], obs["sv_pos_15"])
        self.assertEqual(restored["sv_neg_15"], obs["sv_neg_15"])
        self.assertEqual(len(restored), 12)


# ═══════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    unittest.main(verbosity=2)
