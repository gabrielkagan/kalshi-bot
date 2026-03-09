#!/usr/bin/env python3
"""Standalone tests for adaptive jump detection — no pip dependencies required.

Copies adaptive jump logic inline to avoid importing bot.py which has heavy
dependencies (websockets, etc.).

DRIFT RISK: The AdaptiveJumpEngine logic is copied from VolatilityEngine in
bot.py. If the production code changes, these inline copies may drift out of
sync. The jump detection code has not been extracted to models.py yet because
it's embedded in VolatilityEngine (not a standalone class).

Run: python3 test_adaptive_jump.py
"""

import math
import json
import os
import sys
import time
import tempfile
from collections import deque
from typing import Dict, List, Optional, Tuple

# ── Constants (mirrored from bot.py) ────────────────────────────────────

ASSETS = ["BTC", "ETH", "SOL", "XRP"]
JUMP_ADAPTIVE_SHADOW_MODE = False
JUMP_ADAPTIVE_SUBSAMPLE = 3
JUMP_ADAPTIVE_EWMA_LAMBDA = 0.94
JUMP_ADAPTIVE_EWMA_INIT_RETURNS = 10
JUMP_ADAPTIVE_PCTILE_WINDOW = 180
JUMP_ADAPTIVE_PCTILE_LEVEL = 0.995
JUMP_ADAPTIVE_SIGMA_MULT = 4.0
JUMP_ADAPTIVE_PCTILE_MIN_OBS = 30
JUMP_ADAPTIVE_DECAY_TAU = 64.93
JUMP_ADAPTIVE_DECAY_MAX_BOOST = 1.5
JUMP_ADAPTIVE_DECAY_MIN_BOOST = 0.01
JUMP_ADAPTIVE_DECAY_CAP = 5.0
JUMP_ADAPTIVE_MAG_SCALE_BASE = 4.0
JUMP_ADAPTIVE_MAG_CAP = 3.0
JUMP_ADAPTIVE_MAX_HISTORY = 10

# ═══════════════════════════════════════════════════════════════════════════
#  Adaptive Jump Detection (copied from bot.py for standalone testing)
# ═══════════════════════════════════════════════════════════════════════════

class AdaptiveJumpEngine:
    """Minimal engine with only adaptive jump detection logic."""

    def __init__(self, state_path=None):
        self._returns: Dict[str, deque] = {a: deque(maxlen=180) for a in ASSETS}
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
        self._state_path = state_path or ""
        if self._state_path and os.path.exists(self._state_path):
            self._load_adaptive_state()

    def feed_5s_return(self, asset: str, log_return: float):
        """Simulate feeding a 5s return into the buffer."""
        self._returns[asset].append(log_return)

    def _adaptive_subsample_return(self, asset: str, log_return_5s: float, now: float) -> Optional[float]:
        self._adaptive_tick_counter[asset] = self._adaptive_tick_counter.get(asset, 0) + 1
        if self._adaptive_tick_counter[asset] % JUMP_ADAPTIVE_SUBSAMPLE != 0:
            return None
        returns = self._returns.get(asset)
        if returns is None or len(returns) < JUMP_ADAPTIVE_SUBSAMPLE:
            return None
        return sum(list(returns)[-JUMP_ADAPTIVE_SUBSAMPLE:])

    def _adaptive_jump_test(self, asset: str, return_15s: float, now: float) -> dict:
        abs_r = abs(return_15s)

        # Compute thresholds from HISTORICAL data (before appending current)
        n_hist = len(self._adaptive_abs_returns[asset])

        pre_ewma_var = self._adaptive_ewma_var.get(asset)
        if pre_ewma_var is not None and pre_ewma_var > 0:
            ewma_sigma = math.sqrt(pre_ewma_var)
            sigma_threshold = JUMP_ADAPTIVE_SIGMA_MULT * ewma_sigma
        else:
            ewma_sigma = 0.0
            sigma_threshold = float('inf')

        if n_hist >= JUMP_ADAPTIVE_PCTILE_MIN_OBS:
            sorted_abs = sorted(self._adaptive_abs_returns[asset])
            idx = min(int(JUMP_ADAPTIVE_PCTILE_LEVEL * len(sorted_abs)), len(sorted_abs) - 1)
            pctile_threshold = sorted_abs[idx]
        else:
            pctile_threshold = float('inf')

        # Append current return to buffers
        self._adaptive_returns_15s[asset].append(return_15s)
        self._adaptive_abs_returns[asset].append(abs_r)
        n_obs = len(self._adaptive_returns_15s[asset])

        # EWMA update (after threshold computation)
        r_sq = return_15s * return_15s
        if pre_ewma_var is None:
            if n_obs >= 2:
                buf = list(self._adaptive_returns_15s[asset])
                mean_r = sum(buf) / len(buf)
                ewma_var = sum((x - mean_r) ** 2 for x in buf) / (len(buf) - 1)
                self._adaptive_ewma_var[asset] = ewma_var
        else:
            ewma_var = JUMP_ADAPTIVE_EWMA_LAMBDA * pre_ewma_var + (1 - JUMP_ADAPTIVE_EWMA_LAMBDA) * r_sq
            self._adaptive_ewma_var[asset] = ewma_var

        effective_threshold = max(sigma_threshold, pctile_threshold)

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
        capped_ratio = min(JUMP_ADAPTIVE_MAG_CAP, magnitude_ratio)
        boost = JUMP_ADAPTIVE_DECAY_MAX_BOOST * capped_ratio / JUMP_ADAPTIVE_MAG_SCALE_BASE
        self._adaptive_jump_events[asset].append((timestamp, boost))
        if len(self._adaptive_jump_events[asset]) > JUMP_ADAPTIVE_MAX_HISTORY:
            del self._adaptive_jump_events[asset][:-JUMP_ADAPTIVE_MAX_HISTORY]
        self._adaptive_total_jumps[asset] = self._adaptive_total_jumps.get(asset, 0) + 1

    def _adaptive_decay_multiplier(self, asset: str, now: float) -> Tuple[float, str]:
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
        if not self._state_path:
            return
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
        tmp_path = self._state_path + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(state, f)
        os.replace(tmp_path, self._state_path)

    def _load_adaptive_state(self):
        if not os.path.exists(self._state_path):
            return
        try:
            with open(self._state_path, "r") as f:
                state = json.load(f)
        except (json.JSONDecodeError, ValueError):
            return
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
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════════════════
#  Tests (pytest-compatible)
# ═══════════════════════════════════════════════════════════════════════════

import unittest


class TestSubsampling(unittest.TestCase):
    """Category A: 15s return subsampling from 5s ticks."""

    def test_15s_return_every_3rd_tick(self):
        """Every 3rd tick produces a 15s return (sum of last 3)."""
        eng = AdaptiveJumpEngine()
        returns_5s = [0.001, -0.002, 0.003]
        results = []
        for r in returns_5s:
            eng.feed_5s_return("BTC", r)
            results.append(eng._adaptive_subsample_return("BTC", r, time.time()))
        self.assertIsNone(results[0])
        self.assertIsNone(results[1])
        self.assertIsNotNone(results[2])
        self.assertAlmostEqual(results[2], sum(returns_5s), places=12)

    def test_no_return_on_non_boundary(self):
        """Non-boundary ticks return None."""
        eng = AdaptiveJumpEngine()
        for i in range(5):
            r = 0.001 * (i + 1)
            eng.feed_5s_return("ETH", r)
            result = eng._adaptive_subsample_return("ETH", r, time.time())
            if (i + 1) % 3 != 0:
                self.assertIsNone(result, f"Tick {i+1} should be None")


class TestEWMAAndPercentile(unittest.TestCase):
    """Category B: EWMA variance and percentile threshold."""

    def test_ewma_initialized_from_sample_variance(self):
        """EWMA initializes from sample variance after 2 observations."""
        eng = AdaptiveJumpEngine()
        eng._adaptive_jump_test("BTC", 0.001, 1000.0)
        self.assertIsNone(eng._adaptive_ewma_var["BTC"])
        eng._adaptive_jump_test("BTC", -0.002, 1015.0)
        mean_r = (0.001 + (-0.002)) / 2
        expected_var = ((0.001 - mean_r)**2 + (-0.002 - mean_r)**2) / 1
        self.assertIsNotNone(eng._adaptive_ewma_var["BTC"])
        self.assertAlmostEqual(eng._adaptive_ewma_var["BTC"], expected_var, places=15)

    def test_ewma_converges_under_constant_returns(self):
        """EWMA converges to r² under constant returns."""
        eng = AdaptiveJumpEngine()
        constant_return = 0.001
        for i in range(200):
            eng._adaptive_jump_test("BTC", constant_return, 1000.0 + i * 15)
        self.assertAlmostEqual(eng._adaptive_ewma_var["BTC"], constant_return**2, places=8)

    def test_ewma_reacts_to_regime_change(self):
        """EWMA increases after volatile regime."""
        eng = AdaptiveJumpEngine()
        for i in range(50):
            eng._adaptive_jump_test("BTC", 0.0001, 1000.0 + i * 15)
        ewma_calm = eng._adaptive_ewma_var["BTC"]
        for i in range(20):
            eng._adaptive_jump_test("BTC", 0.01, 1750.0 + i * 15)
        self.assertGreater(eng._adaptive_ewma_var["BTC"], ewma_calm * 10)

    def test_pctile_correct(self):
        """99.5th percentile matches sorted computation."""
        eng = AdaptiveJumpEngine()
        import random
        random.seed(42)
        returns = [random.gauss(0, 0.001) for _ in range(60)]
        for i, r in enumerate(returns):
            eng._adaptive_jump_test("BTC", r, 1000.0 + i * 15)
        sorted_abs = sorted(abs(r) for r in returns)
        idx = min(int(0.995 * len(sorted_abs)), len(sorted_abs) - 1)
        expected_pctile = sorted_abs[idx]
        result = eng._adaptive_jump_test("BTC", 0.0001, 1000.0 + 60 * 15)
        self.assertAlmostEqual(result["pctile_threshold"], expected_pctile, places=10)

    def test_effective_threshold_is_max(self):
        """effective_threshold = max(sigma_threshold, pctile_threshold)."""
        eng = AdaptiveJumpEngine()
        for i in range(40):
            eng._adaptive_jump_test("BTC", 0.001 * (1 + i % 3), 1000.0 + i * 15)
        result = eng._adaptive_jump_test("BTC", 0.001, 1000.0 + 40 * 15)
        self.assertAlmostEqual(
            result["effective_threshold"],
            max(result["sigma_threshold"], result["pctile_threshold"]),
            places=15)


class TestJumpDetection(unittest.TestCase):
    """Category C: Jump trigger logic."""

    def test_normal_return_no_trigger(self):
        """2x sigma does NOT trigger (threshold is 4x)."""
        eng = AdaptiveJumpEngine()
        for i in range(40):
            eng._adaptive_jump_test("BTC", 0.0001, 1000.0 + i * 15)
        sigma = math.sqrt(eng._adaptive_ewma_var["BTC"])
        result = eng._adaptive_jump_test("BTC", 2 * sigma, 1000.0 + 40 * 15)
        self.assertFalse(result["is_jump"])

    def test_large_return_triggers(self):
        """6x sigma triggers (threshold is 4x, pctile of small returns is small)."""
        eng = AdaptiveJumpEngine()
        for i in range(200):
            eng._adaptive_jump_test("BTC", 0.0001, 1000.0 + i * 15)
        sigma = math.sqrt(eng._adaptive_ewma_var["BTC"])
        result = eng._adaptive_jump_test("BTC", 6 * sigma, 1000.0 + 200 * 15)
        self.assertTrue(result["is_jump"])

    def test_warmup_guard(self):
        """No trigger before EWMA_INIT_RETURNS even with huge return."""
        eng = AdaptiveJumpEngine()
        for i in range(5):
            eng._adaptive_jump_test("BTC", 0.0001, 1000.0 + i * 15)
        result = eng._adaptive_jump_test("BTC", 1.0, 1000.0 + 5 * 15)
        self.assertFalse(result["is_jump"])

    def test_pctile_warmup(self):
        """Percentile is inf before PCTILE_MIN_OBS."""
        eng = AdaptiveJumpEngine()
        for i in range(15):
            eng._adaptive_jump_test("BTC", 0.0001, 1000.0 + i * 15)
        result = eng._adaptive_jump_test("BTC", 0.0001, 1000.0 + 15 * 15)
        self.assertEqual(result["pctile_threshold"], float('inf'))


class TestMagnitudeAndDecay(unittest.TestCase):
    """Category D: Boost computation and exponential decay."""

    def test_boost_values(self):
        """Boost = MAX_BOOST × min(ratio, CAP) / SCALE_BASE."""
        eng = AdaptiveJumpEngine()
        now = 1000.0
        eng._record_adaptive_jump_event("BTC", now, 1.0)
        self.assertAlmostEqual(eng._adaptive_jump_events["BTC"][-1][1], 0.375, places=10)
        eng._record_adaptive_jump_event("ETH", now, 2.0)
        self.assertAlmostEqual(eng._adaptive_jump_events["ETH"][-1][1], 0.75, places=10)
        eng._record_adaptive_jump_event("SOL", now, 5.0)  # capped at 3.0
        self.assertAlmostEqual(eng._adaptive_jump_events["SOL"][-1][1], 1.125, places=10)

    def test_single_jump_decay(self):
        """Decay follows exp(-dt / tau)."""
        eng = AdaptiveJumpEngine()
        now = 1000.0
        eng._record_adaptive_jump_event("BTC", now, 2.0)
        boost = eng._adaptive_jump_events["BTC"][0][1]
        mult_0, regime_0 = eng._adaptive_decay_multiplier("BTC", now)
        self.assertEqual(regime_0, "elevated")
        self.assertAlmostEqual(mult_0, 1.0 + boost, places=6)
        mult_45, _ = eng._adaptive_decay_multiplier("BTC", now + 45.0)
        expected_45 = 1.0 + boost * math.exp(-45.0 / JUMP_ADAPTIVE_DECAY_TAU)
        self.assertAlmostEqual(mult_45, expected_45, places=6)
        mult_300, _ = eng._adaptive_decay_multiplier("BTC", now + 300.0)
        self.assertLess(mult_300, 1.01)

    def test_two_jump_stacking(self):
        """Two jumps stack: combined multiplier > single."""
        eng = AdaptiveJumpEngine()
        now = 1000.0
        eng._record_adaptive_jump_event("BTC", now, 2.0)
        mult_single, _ = eng._adaptive_decay_multiplier("BTC", now)
        eng._record_adaptive_jump_event("BTC", now + 5.0, 1.5)
        mult_double, _ = eng._adaptive_decay_multiplier("BTC", now + 5.0)
        self.assertGreater(mult_double, mult_single)

    def test_decay_cap(self):
        """10 simultaneous large jumps capped at DECAY_CAP."""
        eng = AdaptiveJumpEngine()
        now = 1000.0
        for _ in range(10):
            eng._record_adaptive_jump_event("BTC", now, 5.0)
        mult, regime = eng._adaptive_decay_multiplier("BTC", now)
        self.assertEqual(mult, JUMP_ADAPTIVE_DECAY_CAP)
        self.assertEqual(regime, "elevated")


class TestStatePersistence(unittest.TestCase):
    """Category E: Save/load roundtrip."""

    def test_save_load_roundtrip(self):
        """EWMA and buffer survive save/load."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            eng1 = AdaptiveJumpEngine(state_path=path)
            for i in range(50):
                eng1._adaptive_jump_test("BTC", 0.001 * (1 + (i % 5) * 0.1), 1000.0 + i * 15)
            ewma_before = eng1._adaptive_ewma_var["BTC"]
            n_before = len(eng1._adaptive_returns_15s["BTC"])
            eng1._save_adaptive_state()
            eng2 = AdaptiveJumpEngine(state_path=path)
            self.assertAlmostEqual(eng2._adaptive_ewma_var["BTC"], ewma_before, places=15)
            self.assertEqual(len(eng2._adaptive_returns_15s["BTC"]), n_before)
        finally:
            os.unlink(path)

    def test_missing_state_file(self):
        """Missing file starts fresh without crash."""
        eng = AdaptiveJumpEngine(state_path="/tmp/nonexistent_adaptive_state_12345.json")
        self.assertIsNone(eng._adaptive_ewma_var["BTC"])

    def test_corrupt_per_asset(self):
        """Corrupt per-asset data: valid assets restored, corrupt ones start fresh."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
            path = f.name
            json.dump({
                "BTC": {"ewma_var": 0.001, "tick_counter": 10, "total_jumps": 2,
                        "returns_15s": [0.001, 0.002], "abs_returns": [0.001, 0.002],
                        "jump_events": []},
                "ETH": "corrupt_data",
                "SOL": {"ewma_var": "not_a_number"},
                "XRP": {},
                "saved_at": time.time(),
            }, f)
        try:
            eng = AdaptiveJumpEngine(state_path=path)
            self.assertAlmostEqual(eng._adaptive_ewma_var["BTC"], 0.001, places=15)
            self.assertIsNone(eng._adaptive_ewma_var["ETH"])
        finally:
            os.unlink(path)

    def test_jump_events_survive_restart(self):
        """Jump events persist across save/load."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            eng1 = AdaptiveJumpEngine(state_path=path)
            now = 1000.0
            eng1._record_adaptive_jump_event("BTC", now, 2.0)
            eng1._record_adaptive_jump_event("BTC", now + 10, 1.5)
            eng1._record_adaptive_jump_event("BTC", now + 20, 3.0)
            eng1._save_adaptive_state()
            eng2 = AdaptiveJumpEngine(state_path=path)
            self.assertEqual(len(eng2._adaptive_jump_events["BTC"]), 3)
            expected_boost = JUMP_ADAPTIVE_DECAY_MAX_BOOST * 2.0 / JUMP_ADAPTIVE_MAG_SCALE_BASE
            self.assertAlmostEqual(eng2._adaptive_jump_events["BTC"][0][1], expected_boost, places=10)
        finally:
            os.unlink(path)

    def test_ewma_var_preserved(self):
        """EWMA var exact match after save/load."""
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            path = f.name
        try:
            eng1 = AdaptiveJumpEngine(state_path=path)
            eng1._adaptive_ewma_var["BTC"] = 0.00012345
            eng1._save_adaptive_state()
            eng2 = AdaptiveJumpEngine(state_path=path)
            self.assertAlmostEqual(eng2._adaptive_ewma_var["BTC"], 0.00012345, places=15)
        finally:
            os.unlink(path)


class TestIntegrationAndEdgeCases(unittest.TestCase):
    """Category F: Integration and edge cases."""

    def test_empty_events_normal(self):
        """No events → (1.0, 'normal')."""
        eng = AdaptiveJumpEngine()
        mult, regime = eng._adaptive_decay_multiplier("BTC", time.time())
        self.assertEqual(mult, 1.0)
        self.assertEqual(regime, "normal")

    def test_end_to_end(self):
        """300 ticks: 250 calm then 50 spikes → jumps fire, decay falls."""
        eng = AdaptiveJumpEngine()
        now = 10000.0
        jump_count = 0
        subsample_count = 0
        for i in range(300):
            t = now + i * 5
            r = 0.00005 * (1 if i % 2 == 0 else -1) if i < 250 else 0.05
            eng.feed_5s_return("BTC", r)
            ret_15s = eng._adaptive_subsample_return("BTC", r, t)
            if ret_15s is not None:
                subsample_count += 1
                result = eng._adaptive_jump_test("BTC", ret_15s, t)
                if result["is_jump"]:
                    eng._record_adaptive_jump_event("BTC", t, result["magnitude_ratio"])
                    jump_count += 1
        self.assertEqual(subsample_count, 100)
        self.assertGreater(jump_count, 0)
        mult_now, _ = eng._adaptive_decay_multiplier("BTC", now + 300 * 5)
        mult_later, _ = eng._adaptive_decay_multiplier("BTC", now + 300 * 5 + 300)
        self.assertTrue(mult_later < mult_now or mult_now == 1.0)

    def test_zero_returns(self):
        """Zero returns never trigger a jump."""
        eng = AdaptiveJumpEngine()
        for i in range(40):
            eng._adaptive_jump_test("BTC", 0.0, 1000.0 + i * 15)
        result = eng._adaptive_jump_test("BTC", 0.0, 1000.0 + 40 * 15)
        self.assertFalse(result["is_jump"])

    def test_negative_returns_trigger(self):
        """Large negative return triggers jump (abs value used)."""
        eng = AdaptiveJumpEngine()
        for i in range(200):
            eng._adaptive_jump_test("BTC", 0.0001, 1000.0 + i * 15)
        sigma = math.sqrt(eng._adaptive_ewma_var["BTC"])
        result = eng._adaptive_jump_test("BTC", -6 * sigma, 1000.0 + 200 * 15)
        self.assertTrue(result["is_jump"])

    def test_very_old_events_decay(self):
        """Very old event decays to normal."""
        eng = AdaptiveJumpEngine()
        eng._record_adaptive_jump_event("BTC", 1000.0, 3.0)
        mult, regime = eng._adaptive_decay_multiplier("BTC", 11000.0)
        self.assertEqual(regime, "normal")
        self.assertEqual(mult, 1.0)

    def test_deque_overflow(self):
        """Buffer capped at PCTILE_WINDOW size."""
        eng = AdaptiveJumpEngine()
        for i in range(200):
            eng._adaptive_jump_test("BTC", 0.001, 1000.0 + i * 15)
        self.assertEqual(len(eng._adaptive_returns_15s["BTC"]), JUMP_ADAPTIVE_PCTILE_WINDOW)


if __name__ == "__main__":
    unittest.main()
