"""Tests for HWM balance isolation fix (Mar 26 2026, updated Mar 29 2026).

Root cause v1 (Mar 25-26): fractional bankroll from hourly (10%) and SPX (15%)
was passed to record_balance() inside _drawdown_scaler(), poisoning history.

Root cause v2 (Mar 29): portfolio value (cash + position exposure) inflated HWM
when DC positions were open. After settlement, cash-only balance was 73% of HWM,
triggering ds=0.25 for ~7 days. Fix: record_balance() now receives cash only.

These tests verify:
1. compute() never touches balance_history (read-only _drawdown_scaler)
2. Warmup uses cash balance (not portfolio value)
3. Floor guard rejects readings < 50% of HWM
4. Spike alert counter works
5. Fractional bankroll scenario doesn't break HWM
6. DC position open/close does NOT inflate HWM (regression for Mar 29 bug)
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from bot.models import PositionSizer


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
        """_drawdown_scaler must not call record_balance, and uses portfolio balance
        from history for ratio (not the parameter)."""
        sizer = PositionSizer(starting_balance_cents=100000)
        for _ in range(5):
            sizer.record_balance(100000)
        history_len = len(sizer._balance_history)

        # Passing 50000 should NOT trigger drawdown — ratio uses history[-1]=100000
        scaler = sizer._drawdown_scaler(50000)
        self.assertEqual(len(sizer._balance_history), history_len)
        self.assertEqual(scaler, 1.0)  # Portfolio is fine, param is irrelevant

        # Record actual portfolio drawdown, THEN check
        sizer.record_balance(80000)  # 80% of HWM → below DRAWDOWN_HALF (0.85)
        scaler = sizer._drawdown_scaler(50000)  # param doesn't matter
        self.assertEqual(scaler, 0.5)


class TestWarmupWithCashBalance(unittest.TestCase):
    """Warmup should accept cash-only readings (not portfolio value)."""

    def test_warmup_with_cash_balance(self):
        """Simulates startup with $900 cash (positions NOT added to HWM)."""
        sizer = PositionSizer(starting_balance_cents=0)

        # All 5 readings are cash-only (as _tick now computes post-fix)
        cash_balance = 90000  # $900
        for _ in range(5):
            sizer.record_balance(cash_balance)

        self.assertTrue(sizer._hwm_initialized)
        hwm = sizer.get_rolling_hwm()
        self.assertEqual(hwm, 90000)

    def test_warmup_median_with_mixed_readings(self):
        """Median is robust against one outlier reading."""
        sizer = PositionSizer(starting_balance_cents=0)
        # Simulate: first reading is a stale cache, rest are accurate
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


class TestSpikeGuardRecovery(unittest.TestCase):
    """Spike guard must allow recovery near HWM after crash (Mar 30 2026 bug).

    Root cause: settlement timing crashes balance temporarily ($1,377→$1,051).
    Recovery to $1,400 is +33%, rejected by 20% spike guard. history stuck at
    crash value, ds=0.50 permanently. Fix: allow readings within 10% of HWM.
    """

    def test_spike_guard_allows_recovery_near_hwm(self):
        """After crash, recovery to near HWM should be accepted."""
        sizer = PositionSizer(starting_balance_cents=137700)
        for _ in range(5):
            sizer.record_balance(137700)  # HWM = $1,377

        # Crash to $1,051 (accepted — it's a drop, not a spike)
        sizer.record_balance(105100)
        self.assertEqual(sizer._balance_history[-1][1], 105100)

        # Recovery to $1,400 (near HWM of $1,377 — should be ACCEPTED)
        sizer.record_balance(140000)
        self.assertEqual(sizer._balance_history[-1][1], 140000,
                         "Recovery near HWM should bypass spike guard")

    def test_spike_guard_blocks_real_spike(self):
        """Genuine spike far above HWM should still be rejected."""
        sizer = PositionSizer(starting_balance_cents=140000)
        for _ in range(5):
            sizer.record_balance(140000)  # HWM = $1,400

        # Crash to $1,051
        sizer.record_balance(105100)

        # Spike to $2,000 — way above HWM * 1.10 ($1,540). Should be REJECTED.
        sizer.record_balance(200000)
        self.assertEqual(sizer._balance_history[-1][1], 105100,
                         "Spike far above HWM should be rejected")

    def test_recovery_after_crash_restores_ds(self):
        """Full crash-and-recovery cycle: ds should return to 1.0."""
        sizer = PositionSizer(starting_balance_cents=137700)
        for _ in range(5):
            sizer.record_balance(137700)

        # Crash → ds compresses
        sizer.record_balance(105100)
        ds_crashed = sizer._drawdown_scaler(105100)
        self.assertLess(ds_crashed, 1.0, "ds should compress after crash")

        # Recovery near HWM → ds should restore
        sizer.record_balance(140000)
        ds_recovered = sizer._drawdown_scaler(140000)
        self.assertEqual(ds_recovered, 1.0,
                         "ds should return to 1.0 after recovery near HWM")

    def test_spike_guard_allows_within_10pct_of_hwm(self):
        """Reading at exactly HWM * 1.10 should be accepted."""
        sizer = PositionSizer(starting_balance_cents=100000)
        for _ in range(5):
            sizer.record_balance(100000)  # HWM = $1,000

        sizer.record_balance(50000)  # crash to $500
        # Recovery to $1,100 = HWM * 1.10 — boundary, should be accepted
        sizer.record_balance(110000)
        self.assertEqual(sizer._balance_history[-1][1], 110000)

    def test_spike_guard_rejects_above_110pct_of_hwm(self):
        """Reading above HWM * 1.10 after crash should be rejected."""
        sizer = PositionSizer(starting_balance_cents=100000)
        for _ in range(5):
            sizer.record_balance(100000)  # HWM = $1,000

        sizer.record_balance(50000)  # crash to $500
        # Spike to $1,200 = HWM * 1.20 — above 1.10 threshold, should be rejected
        sizer.record_balance(120000)
        self.assertEqual(sizer._balance_history[-1][1], 50000,
                         "Reading >110% of HWM should still be rejected")


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

    def test_scaler_uses_portfolio_balance_not_parameter(self):
        """The key fix: scaler ratio uses recorded portfolio balance,
        not the balance_cents parameter (which may be fractional)."""
        sizer = PositionSizer(starting_balance_cents=100000)
        for _ in range(5):
            sizer.record_balance(100000)

        # Pass fractional balance (hourly 10%) — ratio should STILL be 1.0
        # because it uses _balance_history[-1] = 100000, not the 10000 param
        self.assertEqual(sizer._drawdown_scaler(10000), 1.0)

        # Pass SPX 15% balance — same: ratio = 100000/100000 = 1.0
        self.assertEqual(sizer._drawdown_scaler(15000), 1.0)

        # Pass available cash with positions open — still 1.0
        self.assertEqual(sizer._drawdown_scaler(40000), 1.0)

    def test_scaler_detects_real_portfolio_drawdown(self):
        """When the RECORDED portfolio balance drops, scaler kicks in."""
        sizer = PositionSizer(starting_balance_cents=100000)
        for _ in range(5):
            sizer.record_balance(100000)  # HWM = 100000

        # Portfolio drops to 80000 (20% drawdown)
        sizer.record_balance(80000)
        # Now _balance_history[-1] = 80000, HWM = 100000, ratio = 0.80
        # 0.80 < DRAWDOWN_HALF (0.85) → scaler = 0.5
        self.assertEqual(sizer._drawdown_scaler(80000), 0.5)

        # Even if passed balance is different, ratio uses recorded 80000
        self.assertEqual(sizer._drawdown_scaler(10000), 0.5)
        self.assertEqual(sizer._drawdown_scaler(100000), 0.5)

    def test_scaler_returns_1_during_warmup(self):
        sizer = PositionSizer(starting_balance_cents=0)
        # Not initialized yet
        self.assertFalse(sizer._hwm_initialized)
        self.assertEqual(sizer._drawdown_scaler(50000), 1.0)

    def test_scaler_empty_history_fallback(self):
        """If _balance_history is empty after warmup (shouldn't happen but safety),
        falls back to balance_cents parameter."""
        sizer = PositionSizer(starting_balance_cents=100000)
        for _ in range(5):
            sizer.record_balance(100000)
        # Force empty history (shouldn't happen in production)
        sizer._balance_history.clear()
        # With empty history, get_rolling_hwm returns starting_balance_cents
        # and ratio uses balance_cents fallback
        # HWM = starting_balance_cents = 100000 (set by warmup)
        scaler = sizer._drawdown_scaler(100000)
        self.assertEqual(scaler, 1.0)


class TestDrawdownScalerWithFractionalBankroll(unittest.TestCase):
    """End-to-end: fractional bankroll through compute() gets correct scaler."""

    def test_hourly_10pct_gets_scaler_1(self):
        """Hourly passes 10% balance → scaler should be 1.0, contracts based on 10%."""
        sizer = PositionSizer(starting_balance_cents=100000)
        for _ in range(5):
            sizer.record_balance(100000)

        # Hourly sizing: 10% of $1000 = $100 = 10000 cents
        result = sizer.compute(0.90, 55, 10000)
        self.assertEqual(result["drawdown_scaler"], 1.0,
                         "Hourly 10% bankroll should NOT trigger drawdown")
        self.assertGreater(result["contracts"], 0)

    def test_spx_15pct_gets_scaler_1(self):
        """SPX passes 15% balance → scaler should be 1.0."""
        sizer = PositionSizer(starting_balance_cents=100000)
        for _ in range(5):
            sizer.record_balance(100000)

        result = sizer.compute(0.92, 90, 15000)
        self.assertEqual(result["drawdown_scaler"], 1.0,
                         "SPX 15% bankroll should NOT trigger drawdown")

    def test_available_cash_with_positions_gets_scaler_1(self):
        """Available cash $400 with $700 in positions → scaler should be 1.0."""
        sizer = PositionSizer(starting_balance_cents=110000)
        for _ in range(5):
            sizer.record_balance(110000)

        # Available cash is only $400 because $700 is in positions
        result = sizer.compute(0.95, 88, 40000)
        self.assertEqual(result["drawdown_scaler"], 1.0,
                         "Available cash with positions open should NOT trigger drawdown")

    def test_real_drawdown_affects_all_products(self):
        """When portfolio actually drops, ALL products get scaled down."""
        sizer = PositionSizer(starting_balance_cents=100000)
        for _ in range(5):
            sizer.record_balance(100000)

        # Portfolio drops to 80000
        sizer.record_balance(80000)

        # 15M with full balance
        r1 = sizer.compute(0.95, 85, 80000)
        self.assertEqual(r1["drawdown_scaler"], 0.5)

        # Hourly with 10% of new balance
        r2 = sizer.compute(0.90, 55, 8000)
        self.assertEqual(r2["drawdown_scaler"], 0.5)

        # SPX with 15%
        r3 = sizer.compute(0.92, 90, 12000)
        self.assertEqual(r3["drawdown_scaler"], 0.5)


class TestDCPositionDoesNotInflateHWM(unittest.TestCase):
    """Regression test for Mar 29 2026 HWM inflation bug.

    Root cause: record_balance() received cash + position_exposure, inflating HWM
    when DC positions were open. After settlement, cash-only balance was 73% of HWM,
    compressing drawdown_scaler to 0.25 on a profitable account.

    Fix: record_balance() now receives cash only. Positions are NOT added.
    """

    def test_dc_position_open_does_not_inflate_hwm(self):
        """When DC opens 300ct at 96c, HWM should NOT spike.

        Old behavior: portfolio = $1,400 + $288 exposure = $1,688 → HWM = $1,688
        New behavior: cash = $1,400 → HWM stays at $1,400
        """
        sizer = PositionSizer(starting_balance_cents=140000)
        for _ in range(5):
            sizer.record_balance(140000)  # Cash-only: $1,400
        self.assertEqual(sizer.get_rolling_hwm(), 140000)

        # DC position opens — but we record cash only (no exposure added)
        # Cash stays at $1,400 (Kalshi doesn't drop available_balance on position open)
        sizer.record_balance(140000)
        self.assertEqual(sizer.get_rolling_hwm(), 140000,
                         "HWM should NOT inflate when DC position is open")

        # Drawdown scaler should be 1.0
        self.assertEqual(sizer._drawdown_scaler(140000), 1.0)

    def test_dc_settlement_updates_hwm_correctly(self):
        """After DC settles YES, cash grows by profit. HWM updates to new cash."""
        sizer = PositionSizer(starting_balance_cents=140000)
        for _ in range(5):
            sizer.record_balance(140000)

        # DC settles YES: cash grows by profit (e.g., 300ct × 4c = $12 profit)
        sizer.record_balance(141200)  # $1,412
        self.assertEqual(sizer.get_rolling_hwm(), 141200,
                         "HWM should update to new cash high after profitable settlement")

    def test_dc_loss_compresses_scaler_correctly(self):
        """After DC settles NO, cash drops. Scaler compresses on real loss."""
        sizer = PositionSizer(starting_balance_cents=140000)
        for _ in range(5):
            sizer.record_balance(140000)

        # DC settles NO: cash drops by entry cost (e.g., 300ct × 96c = $288 loss)
        sizer.record_balance(111200)  # $1,112 = 79.4% of HWM
        # 0.794 < 0.85 (DRAWDOWN_HALF) → ds = 0.5
        self.assertEqual(sizer._drawdown_scaler(111200), 0.5,
                         "Real loss should compress scaler correctly")

    def test_no_phantom_compression_after_settlement(self):
        """The core Mar 29 bug: profitable DC trade inflated HWM, then settlement
        dropped portfolio back to cash, compressing ds even though account grew.

        Old: cash=$1,400 → DC opens (portfolio=$1,688) → HWM=$1,688
             → DC settles YES (cash=$1,412) → ratio=1412/1688=0.84 → ds=0.5 ← WRONG

        New: cash=$1,400 → DC opens (cash stays $1,400) → HWM=$1,400
             → DC settles YES (cash=$1,412) → ratio=1412/1412=1.0 → ds=1.0 ← CORRECT
        """
        sizer = PositionSizer(starting_balance_cents=140000)
        for _ in range(5):
            sizer.record_balance(140000)

        # Simulate: cash stays flat while DC position is open
        for _ in range(10):  # 10 ticks with position open
            sizer.record_balance(140000)  # Cash unchanged
        self.assertEqual(sizer.get_rolling_hwm(), 140000)

        # DC settles YES: cash increases by profit
        sizer.record_balance(141200)  # $1,412
        self.assertEqual(sizer.get_rolling_hwm(), 141200)

        # Scaler should be 1.0 — no phantom compression
        self.assertEqual(sizer._drawdown_scaler(141200), 1.0,
                         "No phantom compression after profitable DC settlement")


if __name__ == "__main__":
    unittest.main()
