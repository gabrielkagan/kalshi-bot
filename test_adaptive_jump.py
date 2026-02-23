#!/usr/bin/env python3
"""Standalone tests for adaptive jump detection — no pip dependencies required.

Copies adaptive jump logic inline to avoid importing bot.py which has heavy
dependencies (websockets, etc.).

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
JUMP_ADAPTIVE_SHADOW_MODE = True
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
#  Tests
# ═══════════════════════════════════════════════════════════════════════════

passed = 0
failed = 0


def check(name, condition):
    global passed, failed
    if condition:
        passed += 1
        print(f"  PASS  {name}")
    else:
        failed += 1
        print(f"  FAIL  {name}")


# ── Category A: Subsampling ──────────────────────────────────────────────

print("\n=== Category A: Subsampling ===")

def test_15s_return_every_3rd_tick():
    eng = AdaptiveJumpEngine()
    asset = "BTC"
    returns_5s = [0.001, -0.002, 0.003]
    results = []
    for r in returns_5s:
        eng.feed_5s_return(asset, r)
        result = eng._adaptive_subsample_return(asset, r, time.time())
        results.append(result)
    # First two should be None, third should be sum
    check("15s return None on tick 1", results[0] is None)
    check("15s return None on tick 2", results[1] is None)
    expected_sum = sum(returns_5s)
    check("15s return = sum of 3 returns on tick 3",
          results[2] is not None and abs(results[2] - expected_sum) < 1e-12)

test_15s_return_every_3rd_tick()


def test_no_return_on_non_boundary():
    eng = AdaptiveJumpEngine()
    asset = "ETH"
    for i in range(5):
        r = 0.001 * (i + 1)
        eng.feed_5s_return(asset, r)
        result = eng._adaptive_subsample_return(asset, r, time.time())
        if (i + 1) % 3 != 0:
            check(f"No return on tick {i+1}", result is None)

test_no_return_on_non_boundary()


# ── Category B: EWMA & Percentile ────────────────────────────────────────

print("\n=== Category B: EWMA & Percentile ===")

def test_ewma_initialized_from_sample_variance():
    eng = AdaptiveJumpEngine()
    asset = "BTC"
    # Feed 2 returns so EWMA initializes
    r1 = eng._adaptive_jump_test(asset, 0.001, 1000.0)
    check("EWMA None after 1 obs", eng._adaptive_ewma_var[asset] is None)
    r2 = eng._adaptive_jump_test(asset, -0.002, 1015.0)
    # Sample variance of [0.001, -0.002]
    mean_r = (0.001 + (-0.002)) / 2
    expected_var = ((0.001 - mean_r)**2 + (-0.002 - mean_r)**2) / 1
    check("EWMA initialized from sample variance after n>=2",
          eng._adaptive_ewma_var[asset] is not None and
          abs(eng._adaptive_ewma_var[asset] - expected_var) < 1e-15)

test_ewma_initialized_from_sample_variance()


def test_ewma_converges_under_constant_returns():
    eng = AdaptiveJumpEngine()
    asset = "BTC"
    constant_return = 0.001
    for i in range(200):
        eng._adaptive_jump_test(asset, constant_return, 1000.0 + i * 15)
    # EWMA should converge to r^2 = 0.001^2 = 1e-6
    ewma = eng._adaptive_ewma_var[asset]
    check("EWMA converges under constant returns",
          abs(ewma - constant_return**2) < 1e-8)

test_ewma_converges_under_constant_returns()


def test_ewma_reacts_to_regime_change():
    eng = AdaptiveJumpEngine()
    asset = "BTC"
    # 50 calm returns
    for i in range(50):
        eng._adaptive_jump_test(asset, 0.0001, 1000.0 + i * 15)
    ewma_calm = eng._adaptive_ewma_var[asset]
    # 20 volatile returns
    for i in range(20):
        eng._adaptive_jump_test(asset, 0.01, 1750.0 + i * 15)
    ewma_volatile = eng._adaptive_ewma_var[asset]
    check("EWMA increases after volatile regime", ewma_volatile > ewma_calm * 10)

test_ewma_reacts_to_regime_change()


def test_pctile_correct():
    eng = AdaptiveJumpEngine()
    asset = "BTC"
    import random
    random.seed(42)
    returns = [random.gauss(0, 0.001) for _ in range(60)]
    for i, r in enumerate(returns):
        eng._adaptive_jump_test(asset, r, 1000.0 + i * 15)
    # Percentile is computed from HISTORICAL data (the 60 returns already in buffer)
    sorted_abs = sorted(abs(r) for r in returns)
    idx = min(int(0.995 * len(sorted_abs)), len(sorted_abs) - 1)
    expected_pctile = sorted_abs[idx]
    # The next call computes pctile from the 60 historical returns
    result = eng._adaptive_jump_test(asset, 0.0001, 1000.0 + 60 * 15)
    check("99.5th percentile matches sorted computation",
          abs(result["pctile_threshold"] - expected_pctile) < 1e-10)

test_pctile_correct()


def test_effective_threshold_is_max():
    eng = AdaptiveJumpEngine()
    asset = "BTC"
    for i in range(40):
        eng._adaptive_jump_test(asset, 0.001 * (1 + i % 3), 1000.0 + i * 15)
    result = eng._adaptive_jump_test(asset, 0.001, 1000.0 + 40 * 15)
    check("effective_threshold = max(sigma, pctile)",
          abs(result["effective_threshold"] -
              max(result["sigma_threshold"], result["pctile_threshold"])) < 1e-15)

test_effective_threshold_is_max()


# ── Category C: Jump Detection ───────────────────────────────────────────

print("\n=== Category C: Jump Detection ===")

def test_normal_return_no_trigger():
    eng = AdaptiveJumpEngine()
    asset = "BTC"
    # Build up EWMA with small returns
    for i in range(40):
        eng._adaptive_jump_test(asset, 0.0001, 1000.0 + i * 15)
    # 2x sigma should NOT trigger
    sigma = math.sqrt(eng._adaptive_ewma_var[asset])
    result = eng._adaptive_jump_test(asset, 2 * sigma, 1000.0 + 40 * 15)
    check("Normal return (2x sigma) does NOT trigger", not result["is_jump"])

test_normal_return_no_trigger()


def test_large_return_triggers():
    eng = AdaptiveJumpEngine()
    asset = "BTC"
    # Need >= PCTILE_MIN_OBS (30) so pctile isn't inf
    # Use enough obs so 99.5th pctile doesn't land on the spike itself
    for i in range(200):
        eng._adaptive_jump_test(asset, 0.0001, 1000.0 + i * 15)
    sigma = math.sqrt(eng._adaptive_ewma_var[asset])
    # 6x sigma should trigger (threshold is 4x, pctile of small returns is small at n=200)
    result = eng._adaptive_jump_test(asset, 6 * sigma, 1000.0 + 200 * 15)
    check("Large return (6x sigma) DOES trigger", result["is_jump"])

test_large_return_triggers()


def test_warmup_guard():
    eng = AdaptiveJumpEngine()
    asset = "BTC"
    # Only feed 5 returns (< EWMA_INIT_RETURNS=10)
    for i in range(5):
        eng._adaptive_jump_test(asset, 0.0001, 1000.0 + i * 15)
    # Even a huge return shouldn't trigger during warmup
    result = eng._adaptive_jump_test(asset, 1.0, 1000.0 + 5 * 15)
    check("Warmup guard: no trigger before EWMA_INIT_RETURNS", not result["is_jump"])

test_warmup_guard()


def test_pctile_warmup():
    eng = AdaptiveJumpEngine()
    asset = "BTC"
    # Feed exactly EWMA_INIT_RETURNS but < PCTILE_MIN_OBS
    for i in range(15):
        eng._adaptive_jump_test(asset, 0.0001, 1000.0 + i * 15)
    result = eng._adaptive_jump_test(asset, 0.0001, 1000.0 + 15 * 15)
    # Percentile should be infinity before enough obs
    check("Percentile is inf before PCTILE_MIN_OBS",
          result["pctile_threshold"] == float('inf'))

test_pctile_warmup()


# ── Category D: Magnitude & Decay ────────────────────────────────────────

print("\n=== Category D: Magnitude & Decay ===")

def test_boost_values():
    eng = AdaptiveJumpEngine()
    now = 1000.0
    # ratio=1.0 → boost = 1.5 * 1.0 / 4.0 = 0.375
    eng._record_adaptive_jump_event("BTC", now, 1.0)
    boost_1 = eng._adaptive_jump_events["BTC"][-1][1]
    check("Boost for ratio=1.0", abs(boost_1 - 0.375) < 1e-10)

    # ratio=2.0 → boost = 1.5 * 2.0 / 4.0 = 0.75
    eng._record_adaptive_jump_event("ETH", now, 2.0)
    boost_2 = eng._adaptive_jump_events["ETH"][-1][1]
    check("Boost for ratio=2.0", abs(boost_2 - 0.75) < 1e-10)

    # ratio=5.0 → capped at 3.0 → boost = 1.5 * 3.0 / 4.0 = 1.125
    eng._record_adaptive_jump_event("SOL", now, 5.0)
    boost_3 = eng._adaptive_jump_events["SOL"][-1][1]
    check("Boost for ratio=5.0 (capped at 3.0)", abs(boost_3 - 1.125) < 1e-10)

test_boost_values()


def test_single_jump_decay():
    eng = AdaptiveJumpEngine()
    now = 1000.0
    eng._record_adaptive_jump_event("BTC", now, 2.0)
    boost = eng._adaptive_jump_events["BTC"][0][1]  # 0.75

    # At t=0: multiplier = 1 + 0.75
    mult_0, regime_0 = eng._adaptive_decay_multiplier("BTC", now)
    check("Decay at t=0 is elevated", regime_0 == "elevated")
    check("Decay at t=0 value", abs(mult_0 - (1.0 + boost)) < 1e-6)

    # At 45s (half-life): multiplier = 1 + boost * 0.5
    mult_45, _ = eng._adaptive_decay_multiplier("BTC", now + 45.0)
    expected_45 = 1.0 + boost * math.exp(-45.0 / JUMP_ADAPTIVE_DECAY_TAU)
    check("Decay halves at ~45s", abs(mult_45 - expected_45) < 1e-6)

    # At 300s: should be near 1.0
    mult_300, regime_300 = eng._adaptive_decay_multiplier("BTC", now + 300.0)
    check("Decay near 1.0 at 300s", mult_300 < 1.01)

test_single_jump_decay()


def test_two_jump_stacking():
    eng = AdaptiveJumpEngine()
    now = 1000.0
    eng._record_adaptive_jump_event("BTC", now, 2.0)
    mult_single, _ = eng._adaptive_decay_multiplier("BTC", now)
    eng._record_adaptive_jump_event("BTC", now + 5.0, 1.5)
    mult_double, _ = eng._adaptive_decay_multiplier("BTC", now + 5.0)
    check("Two-jump stacking: combined > single", mult_double > mult_single)

test_two_jump_stacking()


def test_decay_cap():
    eng = AdaptiveJumpEngine()
    now = 1000.0
    # 10 simultaneous large jumps
    for i in range(10):
        eng._record_adaptive_jump_event("BTC", now, 5.0)  # Max ratio
    mult, regime = eng._adaptive_decay_multiplier("BTC", now)
    check("10 jumps capped at DECAY_CAP",
          mult == JUMP_ADAPTIVE_DECAY_CAP)
    check("10 jumps regime is elevated", regime == "elevated")

test_decay_cap()


# ── Category E: State Persistence ────────────────────────────────────────

print("\n=== Category E: State Persistence ===")

def test_save_load_roundtrip():
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
        check("Roundtrip: EWMA preserved",
              eng2._adaptive_ewma_var["BTC"] is not None and
              abs(eng2._adaptive_ewma_var["BTC"] - ewma_before) < 1e-15)
        check("Roundtrip: buffer length preserved",
              len(eng2._adaptive_returns_15s["BTC"]) == n_before)
    finally:
        os.unlink(path)

test_save_load_roundtrip()


def test_missing_state_file():
    eng = AdaptiveJumpEngine(state_path="/tmp/nonexistent_adaptive_state_12345.json")
    check("Missing file: starts fresh, no crash", eng._adaptive_ewma_var["BTC"] is None)

test_missing_state_file()


def test_corrupt_per_asset():
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False, mode="w") as f:
        path = f.name
        # BTC has valid data, ETH is corrupt
        json.dump({
            "BTC": {
                "ewma_var": 0.001,
                "tick_counter": 10,
                "total_jumps": 2,
                "returns_15s": [0.001, 0.002],
                "abs_returns": [0.001, 0.002],
                "jump_events": [],
            },
            "ETH": "corrupt_data",
            "SOL": {"ewma_var": "not_a_number"},
            "XRP": {},
            "saved_at": time.time(),
        }, f)
    try:
        eng = AdaptiveJumpEngine(state_path=path)
        check("Corrupt per-asset: BTC restored",
              eng._adaptive_ewma_var["BTC"] is not None and
              abs(eng._adaptive_ewma_var["BTC"] - 0.001) < 1e-15)
        check("Corrupt per-asset: ETH starts fresh",
              eng._adaptive_ewma_var["ETH"] is None)
    finally:
        os.unlink(path)

test_corrupt_per_asset()


def test_jump_events_survive_restart():
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
        check("Jump events survive: count",
              len(eng2._adaptive_jump_events["BTC"]) == 3)
        # Check boosts
        boost_0 = eng2._adaptive_jump_events["BTC"][0][1]
        expected_boost = JUMP_ADAPTIVE_DECAY_MAX_BOOST * 2.0 / JUMP_ADAPTIVE_MAG_SCALE_BASE
        check("Jump events survive: boost values",
              abs(boost_0 - expected_boost) < 1e-10)
    finally:
        os.unlink(path)

test_jump_events_survive_restart()


def test_ewma_var_preserved():
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
        path = f.name
    try:
        eng1 = AdaptiveJumpEngine(state_path=path)
        eng1._adaptive_ewma_var["BTC"] = 0.00012345
        eng1._save_adaptive_state()

        eng2 = AdaptiveJumpEngine(state_path=path)
        check("EWMA var exact match after save/load",
              eng2._adaptive_ewma_var["BTC"] is not None and
              abs(eng2._adaptive_ewma_var["BTC"] - 0.00012345) < 1e-15)
    finally:
        os.unlink(path)

test_ewma_var_preserved()


# ── Category F: Integration ──────────────────────────────────────────────

print("\n=== Category F: Integration ===")

def test_empty_events_normal():
    eng = AdaptiveJumpEngine()
    mult, regime = eng._adaptive_decay_multiplier("BTC", time.time())
    check("Empty events returns (1.0, 'normal')", mult == 1.0 and regime == "normal")

test_empty_events_normal()


def test_end_to_end():
    eng = AdaptiveJumpEngine()
    asset = "BTC"
    now = 10000.0
    jump_count = 0
    subsample_count = 0

    # 300 ticks: 250 calm, then 50 with spikes (all 3 returns in a group are spikes)
    for i in range(300):
        t = now + i * 5  # 5s ticks
        if i < 250:
            r = 0.00005 * (1 if i % 2 == 0 else -1)  # tiny oscillation
        else:
            # All spike ticks are large so the 15s sum is always large
            r = 0.05

        eng.feed_5s_return(asset, r)
        ret_15s = eng._adaptive_subsample_return(asset, r, t)
        if ret_15s is not None:
            subsample_count += 1
            result = eng._adaptive_jump_test(asset, ret_15s, t)
            if result["is_jump"]:
                eng._record_adaptive_jump_event(asset, t, result["magnitude_ratio"])
                jump_count += 1

    check("End-to-end: correct subsample count (300/3=100)", subsample_count == 100)
    check("End-to-end: jumps fired on spikes", jump_count > 0)

    # Check decay falls after time
    mult_now, _ = eng._adaptive_decay_multiplier(asset, now + 300 * 5)
    mult_later, _ = eng._adaptive_decay_multiplier(asset, now + 300 * 5 + 300)
    check("End-to-end: decay falls after spike period", mult_later < mult_now or mult_now == 1.0)

test_end_to_end()


# ── Edge Cases ───────────────────────────────────────────────────────────

print("\n=== Edge Cases ===")

def test_zero_returns():
    eng = AdaptiveJumpEngine()
    asset = "BTC"
    for i in range(40):
        eng._adaptive_jump_test(asset, 0.0, 1000.0 + i * 15)
    result = eng._adaptive_jump_test(asset, 0.0, 1000.0 + 40 * 15)
    check("Zero returns: no jump", not result["is_jump"])

test_zero_returns()


def test_negative_returns_trigger():
    eng = AdaptiveJumpEngine()
    asset = "BTC"
    for i in range(200):
        eng._adaptive_jump_test(asset, 0.0001, 1000.0 + i * 15)
    sigma = math.sqrt(eng._adaptive_ewma_var[asset])
    result = eng._adaptive_jump_test(asset, -6 * sigma, 1000.0 + 200 * 15)
    check("Negative return triggers jump", result["is_jump"])

test_negative_returns_trigger()


def test_very_old_events_decay():
    eng = AdaptiveJumpEngine()
    now = 1000.0
    eng._record_adaptive_jump_event("BTC", now, 3.0)
    # 10000s later — should be decayed to near zero
    mult, regime = eng._adaptive_decay_multiplier("BTC", now + 10000.0)
    check("Very old event decays to normal", regime == "normal" and mult == 1.0)

test_very_old_events_decay()


def test_deque_overflow():
    eng = AdaptiveJumpEngine()
    asset = "BTC"
    # Feed 200 returns (> maxlen 180)
    for i in range(200):
        eng._adaptive_jump_test(asset, 0.001, 1000.0 + i * 15)
    check("Deque overflow: capped at window size",
          len(eng._adaptive_returns_15s[asset]) == JUMP_ADAPTIVE_PCTILE_WINDOW)

test_deque_overflow()


# ═══════════════════════════════════════════════════════════════════════════
#  Summary
# ═══════════════════════════════════════════════════════════════════════════

print(f"\n{'='*60}")
print(f"  {passed} passed, {failed} failed, {passed + failed} total")
print(f"{'='*60}")
sys.exit(0 if failed == 0 else 1)
