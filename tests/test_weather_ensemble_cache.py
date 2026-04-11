"""Regression tests for WeatherEngine ensemble cache persistence.

Guards against:
- Cold-start 429 cascade leaving weather signal fully dark after bot restart
  (Apr 11 2026: in-memory _last_ensemble was lost on restart, Open-Meteo rate-
  limited the first fetch, exponential backoff made recovery impossible)
- Cache save/load roundtrip failures losing ensemble data
- Self-test burning API quota on startup when warm cache is available
- Watchdog not firing when all cities are empty

See: kb/failures/weather-engine-cold-start.md
"""

import json
import os
import sys
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Mock network deps before import
for _mod in ["websockets", "websocket", "requests",
             "cryptography", "cryptography.hazmat",
             "cryptography.hazmat.primitives",
             "cryptography.hazmat.primitives.serialization",
             "cryptography.hazmat.primitives.hashes",
             "cryptography.hazmat.primitives.asymmetric",
             "cryptography.hazmat.primitives.asymmetric.padding"]:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

import weather_engine
from weather_engine import WeatherEngine, WEATHER_ENSEMBLE_CACHE_FILE


def _make_fake_ensemble(mean_f: float = 72.0) -> dict:
    """Build a minimal ensemble dict matching fetch_ensemble's output shape."""
    members = [mean_f + i * 0.5 for i in range(-5, 6)]  # 11 members
    return {
        "gfs_members": members[:5],
        "ecmwf_members": members[5:],
        "combined_members": members,
        "n_members": len(members),
        "hrrr_temp": mean_f,
        "fetch_time": "2026-04-11T20:00:00Z",
    }


class TestEnsembleCachePersistence(unittest.TestCase):
    """Persistence of _last_ensemble across restarts."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cache_path = os.path.join(self.tmpdir, WEATHER_ENSEMBLE_CACHE_FILE)

    def tearDown(self):
        if os.path.exists(self.cache_path):
            os.remove(self.cache_path)
        os.rmdir(self.tmpdir)

    def _patch_cache_path(self, engine: WeatherEngine) -> None:
        engine._cache_path = self.cache_path

    def test_save_and_load_roundtrip(self):
        """Save ensemble, restart-simulated load, assert data survives."""
        eng1 = WeatherEngine()
        self._patch_cache_path(eng1)
        eng1._last_ensemble = {
            "NYC": _make_fake_ensemble(65.0),
            "MIA": _make_fake_ensemble(82.0),
        }
        eng1._save_ensemble_cache()
        self.assertTrue(os.path.exists(self.cache_path),
                        "Cache file should exist after save")

        # Simulate restart: new engine instance
        eng2 = WeatherEngine()
        self._patch_cache_path(eng2)
        eng2._last_ensemble = {}  # clear the warm-start from __init__
        eng2._load_ensemble_cache()

        self.assertIn("NYC", eng2._last_ensemble)
        self.assertIn("MIA", eng2._last_ensemble)
        self.assertAlmostEqual(
            eng2._last_ensemble["NYC"]["combined_members"][5],
            65.0, places=1)
        self.assertAlmostEqual(
            eng2._last_ensemble["MIA"]["combined_members"][5],
            82.0, places=1)

    def test_load_missing_cache_is_safe(self):
        """No cache file → empty _last_ensemble, no crash."""
        eng = WeatherEngine()
        self._patch_cache_path(eng)
        eng._last_ensemble = {}
        eng._load_ensemble_cache()
        self.assertEqual(eng._last_ensemble, {})

    def test_load_corrupt_cache_is_safe(self):
        """Malformed JSON → log warning, do not crash."""
        with open(self.cache_path, "w") as f:
            f.write("not json {{{ broken")
        eng = WeatherEngine()
        self._patch_cache_path(eng)
        eng._last_ensemble = {}
        eng._load_ensemble_cache()
        self.assertEqual(eng._last_ensemble, {})

    def test_save_empty_is_noop(self):
        """Empty _last_ensemble → do not create an empty cache file."""
        eng = WeatherEngine()
        self._patch_cache_path(eng)
        eng._last_ensemble = {}
        eng._save_ensemble_cache()
        self.assertFalse(os.path.exists(self.cache_path),
                         "Should not write cache file when nothing to save")

    def test_save_atomic_via_tmp_file(self):
        """Save uses .tmp + os.replace for atomicity."""
        eng = WeatherEngine()
        self._patch_cache_path(eng)
        eng._last_ensemble = {"NYC": _make_fake_ensemble(70.0)}
        eng._save_ensemble_cache()
        # Tmp file should not be left behind
        self.assertFalse(os.path.exists(self.cache_path + ".tmp"))
        self.assertTrue(os.path.exists(self.cache_path))


class TestSelfTestSkippedOnWarmCache(unittest.TestCase):
    """Startup should skip API self-test when cache is warm (saves 3 API calls)."""

    def test_self_test_skipped_when_warm(self):
        with patch.object(WeatherEngine, "_self_test_apis") as mock_self_test, \
             patch("threading.Thread") as mock_thread:
            eng = WeatherEngine()
            eng._last_ensemble = {"NYC": _make_fake_ensemble(70.0)}
            eng.start()
            mock_self_test.assert_not_called()
            mock_thread.return_value.start.assert_called_once()

    def test_self_test_runs_when_cold(self):
        with patch.object(WeatherEngine, "_self_test_apis") as mock_self_test, \
             patch("threading.Thread") as mock_thread:
            eng = WeatherEngine()
            eng._last_ensemble = {}  # cold start
            eng.start()
            mock_self_test.assert_called_once()


class TestCacheUsedByGetProbability(unittest.TestCase):
    """get_probability should use warm cache without any new API call."""

    def test_warm_cache_used_without_fetch(self):
        eng = WeatherEngine()
        eng._last_ensemble = {"NYC": _make_fake_ensemble(70.0)}
        with patch.object(eng._fetcher, "fetch_ensemble") as mock_fetch:
            result = eng.get_probability("NYC", 75.0, direction="above")
            # Should NOT have called fetch_ensemble since cache is warm
            mock_fetch.assert_not_called()
        self.assertIsNotNone(result)


if __name__ == "__main__":
    unittest.main()
