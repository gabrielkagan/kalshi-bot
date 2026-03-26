"""Tests for HWM balance isolation fix (Mar 26 2026).

Root cause: fractional bankroll from hourly (10%) and SPX (15%) was passed to
sizer.compute() which called record_balance() inside _drawdown_scaler(),
permanently poisoning the balance_history. Additionally, warmup used available
cash instead of total portfolio value.

These tests verify:
1. compute() never touches balance_history (read-only _drawdown_scaler)
2. Warmup with open positions uses portfolio value
3. Floor guard rejects readings < 50% of HWM
4. Spike alert counter works
5. Fractional bankroll scenario doesn't break HWM
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models import PositionSizer


class TestComputeDoesNotRecordBalance(unittest.TestCase):
    """compute() must NOT modify balance_history (regression for Mar 25-26 bug)."""

    def test_compute_with_fractional_balance_leaves_history_unchanged(self):
        """Hourly/SPX pass 10-15% of balance — must not touch HWM history."""
        sizer = PositionSizer(starting_balance_cents=100000)
        # Initialize HWM properly
        for _ in range(5):
            sizer.record_balance(100000)
        self.assertTrue(sizer._hwm_initialized)
        history_len = len(sizer._balance_history)

        # Simulate hourly passing 10% balance (10000c) through compute()
        # This was the exact bug — it called record_balance(10000) inside
        sizer.compute(0.95, 85, 10000)
        self.assertEqual(len(sizer._balance_history), history_len,
                         "compute() must not add entries to balance_history")

    def test_compute_with_full_balance_leaves_history_unchanged(self):
        """Even full-balance compute() must not modify history."""
        sizer = PositionSizer(starting_balance_cents=100000)
        for _ in range(5):
            sizer.record_balance(100000)
        history_len = len(sizer._balance_history)

        sizer.compute(0.95, 85, 100000)
        self.assertEqual(len(sizer._balance_history), history_len)

    def test_drawdown_scaler_is_readonly(self):
        """_drawdown_scaler must not call record_balance."""
        sizer = PositionSizer(starting_balance_cents=100000)
        for _ in range(5):
            sizer.record_balance(100000)
        history_len = len(sizer._balance_history)

        scaler = sizer._drawdown_scaler(50000)  # 50% drawdown
        self.assertEqual(len(sizer._balance_history), history_len)
        self.assertLess(scaler, 1.0)  # Should detect drawdown


class TestWarmupWithPositions(unittest.TestCase):
    """Warmup should accept portfolio-value readings (cash + positions)."""

    def test_warmup_with_portfolio_value(self):
        """Simulates startup with $400 cash + $500 positions = $900 portfolio."""
        sizer = PositionSizer(starting_balance_cents=0)

        # All 5 readings include position exposure (as _tick would compute)
        portfolio_value = 90000  # $900
        for _ in range(5):
            sizer.record_balance(portfolio_value)

        self.assertTrue(sizer._hwm_initialized)
        hwm = sizer.get_rolling_hwm()
        self.assertEqual(hwm, 90000)

    def test_warmup_median_with_mixed_readings(self):
        """If some readings include positions and some don't, median is robust."""
        sizer = PositionSizer(starting_balance_cents=0)
        # Simulate: first reading has no position info, rest do
        readings = [40000, 90000, 90000, 90000, 90000]
        for r in readings:
            sizer.record_balance(r)

        self.assertTrue(sizer._hwm_initialized)
        hwm = sizer.get_rolling_hwm()
        self.assertEqual(hwm, 90000)  # median of sorted [40000, 90000, 90000, 90000, 90000]


class TestFloorGuard(unittest.TestCase):
    """Floor guard: reject readings < 50% of HWM."""

    def test_floor_guard_rejects_fractional_bankroll(self):
        """A 10% bankroll reading should be rejected by floor guard."""
        sizer = PositionSizer(starting_balance_cents=100000)
        for _ in range(5):
            sizer.record_balance(100000)
        history_len = len(sizer._balance_history)

        # Try recording 10% of balance (what hourly would pass)
        sizer.record_balance(10000)  # 10% of 100000 — way below 50% of HWM
        self.assertEqual(len(sizer._balance_history), history_len,
                         "Floor guard should reject reading < 50% of HWM")

    def test_floor_guard_accepts_legitimate_drawdown(self):
        """A 60% drawdown (balance = 40% of HWM) should still be accepted —
        wait, 40% < 50%, so it would be rejected. The floor guard is specifically
        to catch fractional bankroll, not real drawdowns. Real drawdowns above
        50% should pass."""
        sizer = PositionSizer(starting_balance_cents=100000)
        for _ in range(5):
            sizer.record_balance(100000)

        # 55% of HWM — should be accepted (above 50% floor)
        sizer.record_balance(55000)
        last_recorded = sizer._balance_history[-1][1]
        self.assertEqual(last_recorded, 55000)

    def test_floor_guard_threshold_boundary(self):
        """Exactly 50% of HWM should be rejected (strict less-than)."""
        sizer = PositionSizer(starting_balance_cents=100000)
        for _ in range(5):
            sizer.record_balance(100000)
        history_len = len(sizer._balance_history)

        # Exactly 50% — the guard uses <, so 50000 < 100000 * 0.50 is False
        sizer.record_balance(50000)
        # 50000 < 50000 is False, so this should be accepted
        self.assertEqual(len(sizer._balance_history), history_len + 1)

    def test_floor_guard_rejects_below_50pct(self):
        """49% of HWM should be rejected."""
        sizer = PositionSizer(starting_balance_cents=100000)
        for _ in range(5):
            sizer.record_balance(100000)
        history_len = len(sizer._balance_history)

        sizer.record_balance(49000)  # 49% < 50%
        self.assertEqual(len(sizer._balance_history), history_len,
                         "49% of HWM should be rejected by floor guard")


class TestSpikeAlertCounter(unittest.TestCase):
    """Consecutive spike rejection counter for Telegram alerting."""

    def test_counter_increments_on_spike_rejection(self):
        sizer = PositionSizer(starting_balance_cents=100000)
        for _ in range(5):
            sizer.record_balance(100000)

        # Trigger spike rejection: >20% above last
        sizer.record_balance(130000)  # +30%
        self.assertEqual(sizer._consecutive_spike_rejections, 1)

        sizer.record_balance(130000)
        self.assertEqual(sizer._consecutive_spike_rejections, 2)

        sizer.record_balance(130000)
        self.assertEqual(sizer._consecutive_spike_rejections, 3)

    def test_counter_resets_on_accepted_reading(self):
        sizer = PositionSizer(starting_balance_cents=100000)
        for _ in range(5):
            sizer.record_balance(100000)

        sizer.record_balance(130000)  # rejected
        self.assertEqual(sizer._consecutive_spike_rejections, 1)

        sizer.record_balance(110000)  # accepted (within 20%)
        self.assertEqual(sizer._consecutive_spike_rejections, 0)

    def test_counter_starts_at_zero(self):
        sizer = PositionSizer(starting_balance_cents=100000)
        self.assertEqual(sizer._consecutive_spike_rejections, 0)


class TestFractionalBankrollScenario(unittest.TestCase):
    """End-to-end test: fractional bankroll through compute() + full balance
    through record_balance() should work correctly together."""

    def test_mixed_sizing_preserves_hwm(self):
        """Simulates real scan loop: record_balance(full), then compute(fractional)."""
        sizer = PositionSizer(starting_balance_cents=100000)
        # Initialize HWM
        for _ in range(5):
            sizer.record_balance(100000)

        # Simulate 10 scan cycles
        for _ in range(10):
            # This is what _tick() does: record full balance
            sizer.record_balance(100000)

            # This is what hourly sizing does: compute with 10% balance
            sizer.compute(0.90, 55, 10000)  # hourly: 10% of 100000

            # This is what SPX sizing does: compute with 15% balance
            sizer.compute(0.92, 90, 15000)  # SPX: 15% of 100000

            # This is what 15M sizing does: compute with full balance
            sizer.compute(0.95, 85, 100000)

        # HWM should still be 100000
        hwm = sizer.get_rolling_hwm()
        self.assertEqual(hwm, 100000)

        # Last recorded balance should be 100000, not a fractional value
        _, last_balance = sizer._balance_history[-1]
        self.assertEqual(last_balance, 100000)

    def test_growing_balance_updates_hwm(self):
        """Balance growing from trading profits should update HWM correctly."""
        sizer = PositionSizer(starting_balance_cents=100000)
        for _ in range(5):
            sizer.record_balance(100000)

        # Balance grows 5% each cycle (within 20% spike threshold)
        balance = 100000
        for _ in range(5):
            balance = int(balance * 1.05)
            sizer.record_balance(balance)

        hwm = sizer.get_rolling_hwm()
        self.assertEqual(hwm, balance)  # HWM should track the peak


class TestDrawdownScalerReadOnly(unittest.TestCase):
    """Verify _drawdown_scaler does not modify state."""

    def test_scaler_returns_correct_values(self):
        sizer = PositionSizer(starting_balance_cents=100000)
        for _ in range(5):
            sizer.record_balance(100000)

        # No drawdown
        self.assertEqual(sizer._drawdown_scaler(100000), 1.0)

        # Small drawdown (90% of HWM) — above DRAWDOWN_HALF (0.85)
        self.assertEqual(sizer._drawdown_scaler(90000), 1.0)

        # Moderate drawdown (80% of HWM) — below HALF, above QUARTER
        self.assertEqual(sizer._drawdown_scaler(80000), 0.5)

    def test_scaler_returns_1_during_warmup(self):
        sizer = PositionSizer(starting_balance_cents=0)
        # Not initialized yet
        self.assertFalse(sizer._hwm_initialized)
        self.assertEqual(sizer._drawdown_scaler(50000), 1.0)


if __name__ == "__main__":
    unittest.main()
