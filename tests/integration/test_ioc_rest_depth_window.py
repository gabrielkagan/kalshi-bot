"""Rolling-window REST depth smoothing for IOC pre-submit drift check.

Background — Apr 25 2026:
Commit a56ecc7 (Apr 24) added pre-IOC REST depth verification. When WS
cache claims a non-trivial depth and a single fresh REST call returns
< 50% of cached, the order count is clamped to REST. Intent: prevent
phantom-depth submits like the Apr 24 incident (WS=765, REST=1, fill=1).

Observed regression — position sizes dropped ~65% (avg 77→27 contracts)
across all assets in the 24h post-deploy. Root cause: REST is itself
volatile. The probe `WS_DRIFT_PROBE_REST_STABILITY` shows two REST
calls 1s apart can disagree by hundreds of contracts — a single REST
sample is not authoritative.

This test file pins the new behavior: record REST observations into a
per-ticker rolling buffer (window = IOC_DRIFT_CHECK_REST_WINDOW_S);
clamp uses MAX over the window. Phantom WS still triggers (REST stays
low, peak stays low). Transient REST blips do NOT trigger (peak
preserves an earlier higher reading).

See kb/failures/ws-cache-drift-silent-scan-2026-04-24.md and
kb/decisions/no-floor-relaxation-on-ws-fix.md for full context.
"""

import ast
import os
import sys
import time
import unittest
from unittest.mock import MagicMock
import bot.constants  # noqa: F401
import bot.executor  # noqa: F401

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

BOT_PY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "bot/executor.py")  # Bit 9.1 (2026-05-10): retargeted to bot/executor.py — OrderExecutor extracted from bot/_impl.py


def _make_executor():
    """Construct a minimal OrderExecutor that can exercise the
    rolling-window helper without touching network or DB."""
    import bot
    import bot.executor  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.executor.X access)
    ex = bot.executor.OrderExecutor.__new__(bot.executor.OrderExecutor)
    ex._client = MagicMock()
    ex._state = MagicMock()
    ex._logger = MagicMock()
    ex._ml = MagicMock()
    ex._kalshi_feed = None
    # Initialize the rolling-buffer dict — the implementation should
    # do this in __init__, but since we bypass __init__, set it here.
    ex._rest_depth_observations = {}
    return ex


class TestConstantDefined(unittest.TestCase):
    """A new module-level constant must define the rolling-window
    duration so it can be tuned without touching logic."""

    def test_window_constant_defined(self):
        src = ""
        if os.path.exists(BOT_PY):
            with open(BOT_PY) as f:
                src = f.read()
        self.assertIn(
            "IOC_DRIFT_CHECK_REST_WINDOW_S", src,
            "bot/_impl.py must define IOC_DRIFT_CHECK_REST_WINDOW_S as a "
            "module-level constant for the rolling-window REST "
            "depth smoothing on the IOC drift check.")


class TestRollingBuffer(unittest.TestCase):
    """Pure unit tests on the rolling-window helper. The helper must:
      - record a (now, depth) observation per ticker
      - prune samples older than the window
      - return the MAX depth across the surviving window
      - return None for unknown tickers / empty buffer"""

    def setUp(self):
        self.ex = _make_executor()

    def test_unknown_ticker_returns_none(self):
        self.assertIsNone(
            self.ex._rest_depth_window_max("KXBTC15M-FOO"))

    def test_single_observation_returns_that_depth(self):
        self.ex._record_rest_depth_observation("KXBTC-1", 100)
        self.assertEqual(
            self.ex._rest_depth_window_max("KXBTC-1"), 100)

    def test_window_max_is_peak_across_recent_samples(self):
        """With samples [30, 100, 30] all inside the window, the
        helper must return 100 — the peak, not the most recent and
        not the average. This is the core anti-flicker guarantee."""
        self.ex._record_rest_depth_observation("KXBTC-2", 30)
        self.ex._record_rest_depth_observation("KXBTC-2", 100)
        self.ex._record_rest_depth_observation("KXBTC-2", 30)
        self.assertEqual(
            self.ex._rest_depth_window_max("KXBTC-2"), 100,
            "Peak across the window must dominate transient lows. "
            "Without this, a single REST blip clamps the order.")

    def test_old_samples_are_dropped(self):
        """Samples older than IOC_DRIFT_CHECK_REST_WINDOW_S must be
        pruned. We can't easily fast-forward time in a unit test, so
        we manipulate the buffer's timestamps directly to simulate
        an old observation, then add a fresh low one. Implementation
        uses `time.monotonic()` for cutoff math (R1 P1 fix), so the
        synthetic timestamp must also be monotonic-domain."""
        import bot
        import bot.constants  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.constants.X access)
        from collections import deque
        old_ts = (time.monotonic()
                  - bot.constants.IOC_DRIFT_CHECK_REST_WINDOW_S - 5.0)
        # Manually insert an old sample.
        self.ex._rest_depth_observations["KXBTC-3"] = deque(
            [(old_ts, 1000)])
        # Now record a fresh low sample — old high should be pruned.
        self.ex._record_rest_depth_observation("KXBTC-3", 30)
        peak = self.ex._rest_depth_window_max("KXBTC-3")
        self.assertEqual(
            peak, 30,
            f"Old samples must expire — expected peak=30 (only the "
            f"fresh sample), got {peak}. The 1000 from before the "
            f"window should be pruned.")

    def test_all_low_samples_returns_low(self):
        """The Apr 24 phantom-WS pattern: REST consistently returns
        low because the book genuinely IS thin. The peak must
        reflect that — only [30, 30, 30] in window → peak=30."""
        for _ in range(5):
            self.ex._record_rest_depth_observation("KXBTC-4", 30)
        self.assertEqual(
            self.ex._rest_depth_window_max("KXBTC-4"), 30,
            "All-low samples must collapse to the low peak. "
            "Otherwise we'd never trigger the clamp on real phantom.")

    def test_per_ticker_isolation(self):
        """Observations on KXBTC must not leak into KXSOL's window."""
        self.ex._record_rest_depth_observation("KXBTC-5", 1000)
        self.ex._record_rest_depth_observation("KXSOL-5", 30)
        self.assertEqual(
            self.ex._rest_depth_window_max("KXBTC-5"), 1000)
        self.assertEqual(
            self.ex._rest_depth_window_max("KXSOL-5"), 30,
            "BTC's high observation must not leak into SOL's peak.")


class TestSmoothedFetcher(unittest.TestCase):
    """The smoothed fetcher wraps `_rest_best_ask_depth`: it records
    the fresh sample, then returns (peak, fresh) tuple. The peak is
    used for size clamp (anti-flicker), the fresh sample is used for
    PHANTOM_ABORT (real-time empty book detection — Round 1 P0 #2)."""

    def setUp(self):
        self.ex = _make_executor()

    def test_fetcher_returns_tuple_of_peak_and_fresh(self):
        """One historical high (100) + one fresh low (30) → peak=100,
        fresh=30. Without smoothing the call site would clamp to
        fresh=30 and over-clamp the order."""
        self.ex._record_rest_depth_observation("KXBTC-S1", 100)
        self.ex._rest_best_ask_depth = MagicMock(return_value=30)
        peak, fresh = self.ex._rest_best_ask_depth_smoothed("KXBTC-S1")
        self.assertEqual(peak, 100,
                         f"Peak over window must be 100, got {peak}")
        self.assertEqual(fresh, 30,
                         f"Fresh sample must be 30, got {fresh}")

    def test_fresh_sample_is_zero_when_book_empty(self):
        """R1 P0 #2: fresh=0 must be exposed for PHANTOM_ABORT to
        fire. Without this, peak-only smoothing masks the
        catastrophic-tail signal."""
        self.ex._record_rest_depth_observation("KXBTC-S1B", 800)
        self.ex._rest_best_ask_depth = MagicMock(return_value=0)
        peak, fresh = self.ex._rest_best_ask_depth_smoothed(
            "KXBTC-S1B")
        self.assertEqual(
            fresh, 0,
            "fresh==0 is the real-time signal that the book is empty "
            "RIGHT NOW. The peak (800 from earlier) is irrelevant "
            "for PHANTOM_ABORT — fresh must be exposed independently.")

    def test_fetcher_returns_fresh_when_history_empty(self):
        """First call for a ticker — only the fresh sample exists.
        Peak = fresh."""
        self.ex._rest_best_ask_depth = MagicMock(return_value=42)
        peak, fresh = self.ex._rest_best_ask_depth_smoothed("KXSOL-S2")
        self.assertEqual(peak, 42)
        self.assertEqual(fresh, 42)

    def test_fetcher_returns_none_on_rest_error_with_no_history(self):
        """If REST errors AND we have no history, return (None, None)
        so the caller falls back to cached depth (existing behavior)."""
        self.ex._rest_best_ask_depth = MagicMock(return_value=None)
        peak, fresh = self.ex._rest_best_ask_depth_smoothed("KXETH-S3")
        self.assertIsNone(peak)
        self.assertIsNone(fresh)

    def test_fetcher_returns_history_peak_on_rest_error(self):
        """REST errors but we have prior observations — peak still
        returns from history; fresh is None signaling 'no real-time
        signal available'."""
        self.ex._record_rest_depth_observation("KXBTC-S4", 200)
        self.ex._record_rest_depth_observation("KXBTC-S4", 50)
        self.ex._rest_best_ask_depth = MagicMock(return_value=None)
        peak, fresh = self.ex._rest_best_ask_depth_smoothed("KXBTC-S4")
        self.assertEqual(
            peak, 200,
            "REST error with prior history must return historical "
            "peak, not None. None would force fallback to cached "
            "depth, losing the value of recent observations.")
        self.assertIsNone(
            fresh,
            "fresh must be None on REST error so PHANTOM_ABORT "
            "doesn't fire on stale history.")


class TestRecordObservationHardening(unittest.TestCase):
    """R1 P1 hardening: clock-jump immunity (use monotonic clock),
    nonsensical-sample rejection (bounds check), buffer cleanup
    (drop empty deques to bound memory growth across the lifetime
    of the process)."""

    def setUp(self):
        self.ex = _make_executor()

    def test_uses_monotonic_clock_not_wall_clock(self):
        """The window math must use `time.monotonic()`, not
        `time.time()`. Wall-clock NTP jumps backwards corrupt the
        cutoff math; the VPS has been logging clock_drift_detected
        warnings every 30s with 5–14s drift."""
        # AST-walk: `_record_rest_depth_observation` and
        # `_rest_depth_window_max` must call `time.monotonic`, not
        # `time.time`, for cutoff calculations.
        if os.path.exists(BOT_PY):
            with open(BOT_PY) as f:
                tree = ast.parse(f.read())
        seen = {}  # name -> (uses_monotonic, uses_time_time)
        for cls in ast.walk(tree):
            if (not isinstance(cls, ast.ClassDef)
                    or cls.name != "OrderExecutor"):
                continue
            for fn in cls.body:
                if (not isinstance(fn, ast.FunctionDef)
                        or fn.name not in (
                            "_record_rest_depth_observation",
                            "_rest_depth_window_max")):
                    continue
                uses_monotonic = False
                uses_wall = False
                for sub in ast.walk(fn):
                    if not isinstance(sub, ast.Call):
                        continue
                    if isinstance(sub.func, ast.Attribute):
                        if (sub.func.attr == "monotonic"
                                and isinstance(sub.func.value, ast.Name)
                                and sub.func.value.id == "time"):
                            uses_monotonic = True
                        if (sub.func.attr == "time"
                                and isinstance(sub.func.value, ast.Name)
                                and sub.func.value.id == "time"):
                            uses_wall = True
                seen[fn.name] = (uses_monotonic, uses_wall)
        self.assertEqual(
            len(seen), 2,
            f"Expected to find both helpers; got: {sorted(seen)}")
        for name, (mono, wall) in seen.items():
            self.assertTrue(
                mono,
                f"{name} must call time.monotonic() — NTP jumps "
                f"break wall-clock cutoff math.")
            self.assertFalse(
                wall,
                f"{name} must NOT call time.time() for window math "
                f"— use monotonic instead.")

    def test_negative_depth_is_rejected(self):
        """A negative sample must be silently dropped (not stored).
        Defensive against schema-drift bugs that might produce
        negative deltas."""
        self.ex._record_rest_depth_observation("KXBTC-N1", -50)
        self.assertIsNone(
            self.ex._rest_depth_window_max("KXBTC-N1"),
            "Negative sample must not be stored.")

    def test_huge_depth_is_rejected(self):
        """R2 [A5] tightened bound: > 100k. A nonsensically-large
        sample (e.g., REST returning sum-of-levels rather than
        top-of-book = ~10–50× the realistic peak) must be rejected.
        Kalshi best-ask depths are typically <50k."""
        self.ex._record_rest_depth_observation("KXBTC-N2", 200_000)
        self.assertIsNone(
            self.ex._rest_depth_window_max("KXBTC-N2"),
            "Sample > 100k must not be stored.")

    def test_dropped_sample_logs_warning_once_per_ticker(self):
        """R2 [A7]: out-of-bound samples are dropped silently AT
        THE BUFFER LEVEL, but a once-per-ticker WARNING must fire
        so operators see if schema drift is poisoning the buffer."""
        with self.assertLogs(level="WARNING") as cm:
            self.ex._record_rest_depth_observation("KXBTC-N4", -5)
            self.ex._record_rest_depth_observation("KXBTC-N4", -10)
            self.ex._record_rest_depth_observation("KXBTC-N4", 200_000)
        warnings = [r for r in cm.records
                    if "REST_DEPTH_SAMPLE_DROPPED" in r.getMessage()
                    and "KXBTC-N4" in r.getMessage()]
        self.assertEqual(
            len(warnings), 1,
            f"Expected exactly 1 WARNING (once-per-ticker dedup), "
            f"got {len(warnings)}. Three bad samples for the same "
            f"ticker should produce one log line, not three.")

    def test_zero_depth_is_stored(self):
        """Zero IS valid — it represents 'best-ask level has 0
        contracts'. Don't reject zero (only negative/huge)."""
        self.ex._record_rest_depth_observation("KXBTC-N3", 0)
        self.assertEqual(
            self.ex._rest_depth_window_max("KXBTC-N3"), 0,
            "Zero must be stored as a valid observation.")

    def test_dict_entry_dropped_when_buffer_expires(self):
        """R1 P0 #3: bound dict growth. After all observations
        expire, the dict entry must be removed so settled markets
        don't accumulate forever (15M markets cycle every 15 min)."""
        import bot
        import bot.constants  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.constants.X access)
        from collections import deque
        ticker = "KXBTC-EXPIRED"
        # Insert an expired sample directly.
        old_ts = (time.monotonic()
                  - bot.constants.IOC_DRIFT_CHECK_REST_WINDOW_S - 5.0)
        self.ex._rest_depth_observations[ticker] = deque(
            [(old_ts, 100)])
        # Read should prune AND drop the dict entry.
        peak = self.ex._rest_depth_window_max(ticker)
        self.assertIsNone(peak)
        self.assertNotIn(
            ticker, self.ex._rest_depth_observations,
            f"Dict entry for {ticker} must be removed after all "
            f"samples expire — otherwise dead 15M tickers leak "
            f"forever. Current keys: "
            f"{list(self.ex._rest_depth_observations)}")


class TestAstWiringDriftCheck(unittest.TestCase):
    """AST regression: the IOC drift-check call site must use the
    smoothed (windowed) fetcher, NOT the raw `_rest_best_ask_depth`.
    A future refactor could silently revert to single-sample by
    calling the raw helper directly. This test catches that."""

    def test_drift_check_uses_smoothed_helper(self):
        if os.path.exists(BOT_PY):
            with open(BOT_PY) as f:
                tree = ast.parse(f.read())
        # Find the OrderExecutor method that ACTUALLY logs
        # IOC_CACHE_DRIFT (i.e., contains a `logging.warning` Call
        # whose first arg is a string starting with that marker).
        # Matching by string presence is brittle — docstrings/
        # cross-references in other methods would false-match.
        target_fn = None
        for cls in ast.walk(tree):
            if (not isinstance(cls, ast.ClassDef)
                    or cls.name != "OrderExecutor"):
                continue
            for fn in cls.body:
                if not isinstance(fn, ast.FunctionDef):
                    continue
                # Only consider Call nodes — skip Strs/Constants in
                # docstrings.
                for sub in ast.walk(fn):
                    if not isinstance(sub, ast.Call):
                        continue
                    if not sub.args:
                        continue
                    first = sub.args[0]
                    if (isinstance(first, ast.Constant)
                            and isinstance(first.value, str)
                            and first.value.startswith(
                                "IOC_CACHE_DRIFT")):
                        target_fn = fn
                        break
                if target_fn:
                    break
            if target_fn:
                break
        self.assertIsNotNone(
            target_fn,
            "Couldn't find the OrderExecutor method that emits the "
            "`IOC_CACHE_DRIFT` log line — wiring assertion can't run.")
        # The function must call _rest_best_ask_depth_smoothed (the
        # new smoothed helper) — NOT the raw _rest_best_ask_depth
        # directly.
        called_smoothed = False
        called_raw = False
        for sub in ast.walk(target_fn):
            if not isinstance(sub, ast.Call):
                continue
            if isinstance(sub.func, ast.Attribute):
                if sub.func.attr == "_rest_best_ask_depth_smoothed":
                    called_smoothed = True
                elif sub.func.attr == "_rest_best_ask_depth":
                    called_raw = True
        self.assertTrue(
            called_smoothed,
            "IOC drift-check site must call "
            "`_rest_best_ask_depth_smoothed` so the windowed peak "
            "(not a single sample) is used as the clamp authority.")
        self.assertFalse(
            called_raw,
            "IOC drift-check site must NOT call the raw "
            "`_rest_best_ask_depth` directly — that's the regression "
            "the windowed helper is meant to prevent. Use the "
            "_smoothed wrapper.")


class TestInitWiresBuffer(unittest.TestCase):
    """The rolling buffer dict must be initialized in
    `OrderExecutor.__init__`. Otherwise the first call to
    `_record_rest_depth_observation` AttributeErrors and crashes
    the IOC submit path."""

    def test_init_assigns_rest_depth_observations(self):
        if os.path.exists(BOT_PY):
            with open(BOT_PY) as f:
                tree = ast.parse(f.read())
        for cls in ast.walk(tree):
            if (not isinstance(cls, ast.ClassDef)
                    or cls.name != "OrderExecutor"):
                continue
            for fn in cls.body:
                if (not isinstance(fn, ast.FunctionDef)
                        or fn.name != "__init__"):
                    continue
                for sub in ast.walk(fn):
                    # Plain `self.x = ...`
                    if isinstance(sub, ast.Assign):
                        for tgt in sub.targets:
                            if (isinstance(tgt, ast.Attribute)
                                    and isinstance(tgt.value, ast.Name)
                                    and tgt.value.id == "self"
                                    and tgt.attr == "_rest_depth_observations"):
                                return  # found
                    # Annotated `self.x: T = ...` (the actual form
                    # used in the implementation, since the buffer
                    # is typed as Dict[str, deque]).
                    elif isinstance(sub, ast.AnnAssign):
                        tgt = sub.target
                        if (isinstance(tgt, ast.Attribute)
                                and isinstance(tgt.value, ast.Name)
                                and tgt.value.id == "self"
                                and tgt.attr == "_rest_depth_observations"):
                            return  # found
                self.fail(
                    "OrderExecutor.__init__ must assign "
                    "`self._rest_depth_observations = {}` (or a "
                    "deque-of-deques container). Without this, the "
                    "first call to `_record_rest_depth_observation` "
                    "raises AttributeError.")
        self.fail("OrderExecutor not found")


class TestColdStartGate(unittest.TestCase):
    """R2 [A1]: cold-start safety. The smoothed peak with only 1
    sample == fresh sample, so the clamp degenerates to single-
    sample clamping — the exact pre-fix bug. Require ≥2 samples
    in window before the smoothed clamp is trusted; fall through
    to existing policy on cold start. PHANTOM_ABORT (fresh=0)
    still fires regardless."""

    def setUp(self):
        self.ex = _make_executor()

    def test_window_count_is_zero_on_unknown_ticker(self):
        self.assertEqual(
            self.ex._rest_depth_window_count("KXBTC-COLD-1"), 0)

    def test_window_count_increments_with_each_observation(self):
        self.ex._record_rest_depth_observation("KXBTC-COLD-2", 100)
        self.assertEqual(
            self.ex._rest_depth_window_count("KXBTC-COLD-2"), 1)
        self.ex._record_rest_depth_observation("KXBTC-COLD-2", 200)
        self.assertEqual(
            self.ex._rest_depth_window_count("KXBTC-COLD-2"), 2)
        self.ex._record_rest_depth_observation("KXBTC-COLD-2", 50)
        self.assertEqual(
            self.ex._rest_depth_window_count("KXBTC-COLD-2"), 3)

    def test_window_count_excludes_expired_samples(self):
        """Expired samples must NOT count toward the cold-start gate."""
        import bot
        import bot.constants  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.constants.X access)
        from collections import deque
        old_ts = (time.monotonic()
                  - bot.constants.IOC_DRIFT_CHECK_REST_WINDOW_S - 5.0)
        self.ex._rest_depth_observations["KXBTC-COLD-3"] = deque(
            [(old_ts, 1000), (old_ts + 0.1, 1000)])
        # Both expired — count should be 0 even though dict has entries.
        self.assertEqual(
            self.ex._rest_depth_window_count("KXBTC-COLD-3"), 0,
            "Expired samples must not count as in-window for the "
            "cold-start gate.")

    def test_min_samples_constant_is_at_least_2(self):
        """The cold-start threshold must be ≥2 by definition: 1
        sample = no smoothing. A future refactor that lowered to 1
        would silently re-enable the pre-fix bug."""
        import bot
        import bot.executor  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.executor.X access)
        self.assertGreaterEqual(
            bot.executor.OrderExecutor._REST_DEPTH_MIN_SAMPLES_FOR_CLAMP, 2,
            "Smoothed clamp requires ≥2 samples; otherwise it "
            "degenerates to single-sample (the pre-fix bug).")

    def test_cold_start_branch_exists_in_submit_taker(self):
        """AST regression: the IOC drift-check call site must
        consult `_rest_depth_window_count` (or equivalent gate)
        before applying the clamp. Without this gate, cold-start
        IOCs use single-sample peak and re-introduce the regression."""
        if os.path.exists(BOT_PY):
            with open(BOT_PY) as f:
                tree = ast.parse(f.read())
        target_fn = None
        for cls in ast.walk(tree):
            if (not isinstance(cls, ast.ClassDef)
                    or cls.name != "OrderExecutor"):
                continue
            for fn in cls.body:
                if not isinstance(fn, ast.FunctionDef):
                    continue
                for sub in ast.walk(fn):
                    if (isinstance(sub, ast.Constant)
                            and isinstance(sub.value, str)
                            and sub.value.startswith(
                                "IOC_CACHE_DRIFT")):
                        target_fn = fn
                        break
                if target_fn:
                    break
            if target_fn:
                break
        self.assertIsNotNone(
            target_fn, "Couldn't locate IOC_CACHE_DRIFT emitter.")
        fn_src = ast.unparse(target_fn)
        self.assertIn(
            "_rest_depth_window_count", fn_src,
            "IOC drift-check site must call "
            "`_rest_depth_window_count` (or check the "
            "_REST_DEPTH_MIN_SAMPLES_FOR_CLAMP gate) before "
            "applying the smoothed clamp. R2 [A1] regression guard.")


class TestEndToEndFlickerNotClamped(unittest.TestCase):
    """End-to-end: feed a flicker pattern through the IOC drift-check
    block and assert the order count is NOT clamped down. The
    helper-level tests prove the helper's logic; this test proves the
    wired path actually changes outcome.

    Constructs a candidate dict mimicking what `scan()` produces,
    seeds the smoothing buffer with prior high observations, then
    calls into the smoothed helper as `_submit_taker` would. Verifies
    the returned peak is the historical high (no clamp), not the
    fresh low (would clamp)."""

    def setUp(self):
        self.ex = _make_executor()

    def test_flicker_high_low_high_returns_high_peak(self):
        """3 consecutive REST samples [200, 30, 200] → peak=200.
        Without smoothing, the middle 30 would be authoritative
        and clamp the order. With smoothing, peak preserves 200."""
        # First call (history empty) → 200.
        self.ex._rest_best_ask_depth = MagicMock(return_value=200)
        peak1, _ = self.ex._rest_best_ask_depth_smoothed("KXBTC-E1")
        self.assertEqual(peak1, 200)
        # Second call (transient drop) → fresh=30, peak=200 (max
        # of [200, 30]).
        self.ex._rest_best_ask_depth = MagicMock(return_value=30)
        peak2, fresh2 = self.ex._rest_best_ask_depth_smoothed(
            "KXBTC-E1")
        self.assertEqual(
            peak2, 200,
            f"After a flicker drop, peak must still be 200 — "
            f"got {peak2}. Without windowed smoothing this would "
            f"return 30 and the IOC would clamp the order count.")
        self.assertEqual(fresh2, 30)
        # Third call (recovery) → 200 again.
        self.ex._rest_best_ask_depth = MagicMock(return_value=200)
        peak3, fresh3 = self.ex._rest_best_ask_depth_smoothed(
            "KXBTC-E1")
        self.assertEqual(peak3, 200)
        self.assertEqual(fresh3, 200)

    def test_persistent_low_eventually_dominates(self):
        """Once all-high samples expire from the window, persistent
        low samples should produce a low peak (real phantom case)."""
        import bot
        import bot.constants  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.constants.X access)
        from collections import deque
        # Pre-load expired high samples.
        old_ts = (time.monotonic()
                  - bot.constants.IOC_DRIFT_CHECK_REST_WINDOW_S - 1.0)
        self.ex._rest_depth_observations["KXBTC-E2"] = deque(
            [(old_ts, 1000), (old_ts + 0.1, 1000)])
        # Now record current low samples.
        self.ex._rest_best_ask_depth = MagicMock(return_value=30)
        peak, fresh = self.ex._rest_best_ask_depth_smoothed("KXBTC-E2")
        self.assertEqual(
            peak, 30,
            f"Once historical highs expire, peak must reflect the "
            f"current low samples — got {peak}.")
        self.assertEqual(fresh, 30)


class TestPhantomAbortEndToEnd(unittest.TestCase):
    """R2 [A6]: functional end-to-end test wiring fresh=0 through
    `_submit_taker`. Asserts that the abort path is actually taken
    (no place_order call, IOC_ABORT_PHANTOM logged) when REST
    returns 0 RIGHT NOW even if the historical peak is high. The
    helper-level `test_fresh_sample_is_zero_when_book_empty` tests
    the helper API, but only this functional test proves the wired
    behavior is what we claim."""

    def test_fresh_zero_triggers_phantom_abort_through_submit_taker(self):
        import bot
        import bot.executor  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.executor.X access)
        from unittest.mock import patch
        # Build a real OrderExecutor (existing _make_executor in
        # tests/integration/test_execution.py uses real __init__; we replicate
        # the minimum here to keep this test file self-contained).
        client = MagicMock()
        # Pre-load the buffer with a high history sample, then have
        # REST return 0 NOW. Without the fresh-zero check, smoothed
        # peak would still be high and PHANTOM_ABORT wouldn't fire.
        state = MagicMock()
        logger = MagicMock()
        ml = MagicMock()
        feed = MagicMock()
        feed.is_connected = True
        feed.pop_fills.return_value = []
        ex = bot.executor.OrderExecutor(
            client=client, state=state, logger=logger,
            main_loop=ml, kalshi_feed=feed)
        # Pre-warm with high samples so the cold-start gate does NOT
        # block the drift-check branch (R2 [A1]).
        ex._record_rest_depth_observation("KXBTC15M-X", 800)
        ex._record_rest_depth_observation("KXBTC15M-X", 800)
        # REST returns 0 NOW — phantom book.
        ob_resp_zero = {
            "orderbook": {
                "yes": [],  # no asks at all
                "no": [],
            },
        }
        client.get_orderbook.return_value = ob_resp_zero
        # Build a candidate that would fire IOC.
        candidate = {
            "ticker": "KXBTC15M-X",
            "event_ticker": "KXBTC15M-26APR25-EVT",
            "asset": "BTC",
            "best_yes_ask": 95,
            "balance_at_scan": 100_000,
            "position_size": 50,
            "strategy": "terminal_momentum_99",  # no_clamp
            "best_ask_source": "orderbook",
            "ob_snapshot": {"ask_depth": 800, "best_ask": 95},
            "seconds_to_close": 30.0,
            "side": "yes",
        }
        with patch("bot.executor.time") as mt, patch("bot.executor.fp_str_to_int", return_value=0):
            mt.time.return_value = 1000.0
            mt.monotonic.return_value = 1000.0
            mt.sleep = MagicMock()
            with self.assertLogs(level="WARNING") as cm:
                result = ex._submit_taker(candidate)
        self.assertIsNone(
            result,
            "PHANTOM_ABORT must short-circuit and return None.")
        client.place_order.assert_not_called()
        abort_lines = [r for r in cm.records
                       if "IOC_ABORT_PHANTOM" in r.getMessage()]
        self.assertGreaterEqual(
            len(abort_lines), 1,
            "Expected at least one IOC_ABORT_PHANTOM warning when "
            "fresh REST returns 0; got none.")


class TestPhantomAbortStillFires(unittest.TestCase):
    """R1 P0 #2 regression: AST-level guarantee that the
    PHANTOM_ABORT path checks the FRESH REST sample (not just the
    drift-corrected `_ask_depth`). Without this, a 4s-old peak of
    800 would mask a current `fresh=0` reading and a real empty
    book wouldn't trigger the abort."""

    def test_phantom_abort_branch_checks_rest_fresh(self):
        if os.path.exists(BOT_PY):
            with open(BOT_PY) as f:
                tree = ast.parse(f.read())
        # Locate the OrderExecutor method that emits IOC_ABORT_PHANTOM.
        target_fn = None
        for cls in ast.walk(tree):
            if (not isinstance(cls, ast.ClassDef)
                    or cls.name != "OrderExecutor"):
                continue
            for fn in cls.body:
                if not isinstance(fn, ast.FunctionDef):
                    continue
                for sub in ast.walk(fn):
                    if (isinstance(sub, ast.Constant)
                            and isinstance(sub.value, str)
                            and sub.value.startswith(
                                "IOC_ABORT_PHANTOM")):
                        target_fn = fn
                        break
                if target_fn:
                    break
            if target_fn:
                break
        self.assertIsNotNone(
            target_fn,
            "Couldn't locate IOC_ABORT_PHANTOM emitter.")
        # The function's source must reference `_rest_fresh` near
        # the abort-check (i.e., the abort condition must mention
        # the fresh sample, not just the cached/peak value).
        fn_src = ast.unparse(target_fn)
        self.assertIn(
            "_rest_fresh", fn_src,
            "PHANTOM_ABORT block must check `_rest_fresh` so a "
            "real-time empty book triggers abort even if the "
            "windowed peak is high. R1 P0 #2 regression guard.")


class TestColdStartCatastrophicDrift(unittest.TestCase):
    """Apr 25 2026 regression: cold-start path (samples<2) was a silent
    bypass of all drift detection, so freshly-discovered tickers (~16/hr
    on 15M) submitted Kelly-size IOCs into stale-WS-cache phantom depth,
    accumulating 50+ micro-fills/day at 1-5 contracts each.

    The fix adds a CATASTROPHIC-DRIFT escape hatch: even on cold-start
    (no smoothing), if the single fresh REST sample shows ≥10x
    divergence from cache (IOC_DRIFT_CHECK_COLD_START_RATIO=0.1), apply
    the clamp anyway. Strict enough to avoid single-sample flicker
    false-positives; lax enough to catch the dominant phantom pattern
    (e.g. cache=86, fresh=1, ratio=0.012)."""

    def test_constant_defined_and_strict(self):
        import bot
        import bot.constants  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.constants.X access)
        self.assertTrue(
            hasattr(bot.constants, "IOC_DRIFT_CHECK_COLD_START_RATIO"),
            "IOC_DRIFT_CHECK_COLD_START_RATIO must be defined as a "
            "module-level constant for tunability.")
        self.assertLess(
            bot.constants.IOC_DRIFT_CHECK_COLD_START_RATIO,
            bot.constants.IOC_DRIFT_CHECK_DIVERGENCE_RATIO,
            "Cold-start ratio must be STRICTER (lower) than the "
            "smoothed-window ratio, or it would be no different from "
            "fully-trusting a single REST sample.")
        self.assertGreater(
            bot.constants.IOC_DRIFT_CHECK_COLD_START_RATIO, 0.0,
            "Cold-start ratio must be >0 (otherwise the branch never "
            "fires, defeating the fix).")

    def test_cold_start_branch_uses_constant(self):
        """AST regression: the cold-start branch in `_submit_taker`
        must reference IOC_DRIFT_CHECK_COLD_START_RATIO. A future
        refactor that removed the catastrophic-drift escape hatch
        would re-introduce the Apr 25 micro-fill bug."""
        if os.path.exists(BOT_PY):
            with open(BOT_PY) as f:
                tree = ast.parse(f.read())
        target_fn = None
        for cls in ast.walk(tree):
            if (not isinstance(cls, ast.ClassDef)
                    or cls.name != "OrderExecutor"):
                continue
            for fn in cls.body:
                if not isinstance(fn, ast.FunctionDef):
                    continue
                for sub in ast.walk(fn):
                    if (isinstance(sub, ast.Constant)
                            and isinstance(sub.value, str)
                            and sub.value.startswith(
                                "IOC_CACHE_COLD_START")):
                        target_fn = fn
                        break
                if target_fn:
                    break
            if target_fn:
                break
        self.assertIsNotNone(
            target_fn, "Couldn't locate IOC_CACHE_COLD_START emitter.")
        fn_src = ast.unparse(target_fn)
        self.assertIn(
            "IOC_DRIFT_CHECK_COLD_START_RATIO", fn_src,
            "Cold-start branch must consult "
            "IOC_DRIFT_CHECK_COLD_START_RATIO so catastrophic drift "
            "still triggers the clamp even with samples<2. Apr 25 "
            "2026 micro-fill regression guard.")
        self.assertIn(
            "IOC_CACHE_DRIFT_COLD", fn_src,
            "Cold-start branch must emit IOC_CACHE_DRIFT_COLD warning "
            "when catastrophic drift triggers — operators need a "
            "distinct signal vs the normal IOC_CACHE_DRIFT (smoothed).")

    def test_catastrophic_drift_triggers_clamp_end_to_end(self):
        """End-to-end: cold-start ticker (empty buffer), cache claims
        deep depth, fresh REST returns near-zero. Without the escape
        hatch, this is the exact path that produced 1ct micro-fills."""
        import bot
        import bot.executor  # noqa: F401 (Bit 9.3-iii.c — explicit submodule import; bot.executor.X access)
        from unittest.mock import patch
        client = MagicMock()
        state = MagicMock()
        logger = MagicMock()
        ml = MagicMock()
        feed = MagicMock()
        feed.is_connected = True
        feed.pop_fills.return_value = []
        ex = bot.executor.OrderExecutor(
            client=client, state=state, logger=logger,
            main_loop=ml, kalshi_feed=feed)
        # Cold-start: NO pre-warmed samples. The drift-check fetches
        # fresh=1 → buffer has 1 sample → samples<2 → cold-start branch.
        # Cache claims 86 (matches Apr 25 KXETH15M log).
        # _best_ask_depth reads the NO-bid side (= YES ask counterparty).
        # 1 NO contract bidding 1c → best YES ask at 99c with depth 1.
        ob_resp_thin = {
            "orderbook": {
                "yes": [],
                "no": [[1, 1]],
            },
        }
        client.get_orderbook.return_value = ob_resp_thin
        # IOC for 50ct. Cache says depth=86 (drift), real REST=1.
        # Strategy = terminal_momentum_99 → policy=no_clamp normally,
        # but catastrophic drift should override.
        candidate = {
            "ticker": "KXETH15M-COLDSTART",
            "event_ticker": "KXETH15M-26APR25-EVT",
            "asset": "ETH",
            "best_yes_ask": 99,
            "balance_at_scan": 100_000,
            "position_size": 50,
            "strategy": "terminal_momentum_99",
            "best_ask_source": "orderbook",
            "ob_snapshot": {"ask_depth": 86, "best_ask": 99},
            "seconds_to_close": 30.0,
            "side": "yes",
            "calibrated_prob": 0.95,
        }
        # place_order returns "filled successfully" — but we expect the
        # IOC_ABORT_THIN_CLAMP path to fire first (depth=1 < min=5).
        client.place_order.return_value = {
            "order": {"order_id": "ABC", "remaining_count": 50,
                      "fill_count": 0}}
        client.get_positions.return_value = {"market_positions": []}
        with patch("bot.executor.time") as mt, patch("bot.executor.fp_str_to_int", return_value=0):
            mt.time.return_value = 1000.0
            mt.monotonic.return_value = 1000.0
            mt.sleep = MagicMock()
            mt.perf_counter.return_value = 0.0
            with self.assertLogs(level="WARNING") as cm:
                ex._submit_taker(candidate)
        cold_drift_lines = [
            r for r in cm.records
            if "IOC_CACHE_DRIFT_COLD" in r.getMessage()]
        self.assertGreaterEqual(
            len(cold_drift_lines), 1,
            "Catastrophic divergence on cold-start (cache=86, fresh=1) "
            "must emit IOC_CACHE_DRIFT_COLD; got none. Without this, "
            "the bot trusts cache=86 and submits 50ct into a 1ct book.")
        # Either ABORT_THIN_CLAMP (1<5) or DRIFT_CLAMP fires next —
        # both are correct; the bug is "no clamp at all and submit 50".
        clamp_evidence = [
            r for r in cm.records
            if ("IOC_ABORT_THIN_CLAMP" in r.getMessage()
                or "IOC_DRIFT_CLAMP" in r.getMessage()
                or "IOC_SIZE_CLAMP" in r.getMessage())]
        self.assertGreaterEqual(
            len(clamp_evidence), 1,
            "After cold-start drift detection, the order must either "
            "abort or be clamped, not submit at the original Kelly "
            "size. Apr 25 2026 micro-fill regression guard.")


if __name__ == "__main__":
    unittest.main()
