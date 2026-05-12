"""Tests for fee calculation functions (models.py).

Guards against:
- Rounding errors in fee computation (ceil vs floor vs round)
- Per-contract vs per-order rounding (Kalshi applies ceil to TOTAL, not per contract)
- Symmetry violations (fee at price p must equal fee at price 100-p)
- Zero/boundary edge cases that could cause division errors
- SPX finance category discount (fee_mult_taker=0.035)
"""

import math
import unittest

from bot.models import calculate_fee, calculate_taker_fee, calculate_maker_fee


class TestCalculateFee(unittest.TestCase):
    """Core fee formula: ceil(fee_mult_taker × count × price × (100 - price) / 100)."""

    def test_known_values(self):
        """Verify against hand-computed Kalshi fee examples.

        Formula: ceil(0.07 × count × price_cents × (100 - price_cents) / 100)
        Result is in cents. At typical 1-contract sizes, fees are tiny.
        """
        # 1 contract at 90c: ceil(0.07 * 1 * 90 * 10 / 100) = ceil(0.63) = 1
        self.assertEqual(calculate_fee(1, 90, True), 1)
        # 10 contracts at 90c: ceil(0.07 * 10 * 90 * 10 / 100) = ceil(6.3) = 7
        self.assertEqual(calculate_fee(10, 90, True), 7)
        # 1 contract at 50c: ceil(0.07 * 1 * 50 * 50 / 100) = ceil(1.75) = 2
        self.assertEqual(calculate_fee(1, 50, True), 2)
        # 50 contracts at 93c: ceil(0.07 * 50 * 93 * 7 / 100) = ceil(22.785) = 23
        self.assertEqual(calculate_fee(50, 93, True), 23)

    def test_maker_always_zero(self):
        """Maker fee is always $0 regardless of count or price."""
        for price in [50, 86, 90, 95, 99]:
            for count in [1, 10, 100]:
                self.assertEqual(calculate_fee(count, price, False), 0,
                                 f"Maker fee should be 0 at {price}c × {count}")

    def test_ceil_not_floor(self):
        """Fee uses ceiling, not floor or round. Guards against rounding bug."""
        # 10 contracts at 86c: 0.07 * 10 * 86 * 14 / 100 = 8.428 → ceil = 9, floor = 8
        self.assertEqual(calculate_fee(10, 86, True), 9)
        # 5 contracts at 50c: 0.07 * 5 * 50 * 50 / 100 = 8.75 → ceil = 9, floor = 8
        self.assertEqual(calculate_fee(5, 50, True), 9)

    def test_ceil_on_total_not_per_contract(self):
        """Ceil applied to TOTAL fee, not per-contract then multiplied.

        Guards against the bug where fee(n, p) != n * fee(1, p).
        Kalshi formula: ceil(rate × COUNT × p × (100-p) / 100).
        """
        # 10 contracts at 93c: ceil(0.07 * 10 * 93 * 7 / 100) = ceil(4.557) = 5
        total = calculate_fee(10, 93, True)
        self.assertEqual(total, 5)
        # Per-contract would be: 10 * ceil(0.07 * 1 * 93 * 7 / 100) = 10 * ceil(0.4557) = 10 * 1 = 10
        per_contract_sum = 10 * calculate_fee(1, 93, True)
        self.assertEqual(per_contract_sum, 10)
        # They differ — total ceil is cheaper
        self.assertLess(total, per_contract_sum)

    def test_symmetry(self):
        """Fee at price p equals fee at price (100 - p). p*(100-p) is symmetric."""
        for p in range(1, 100):
            fee_p = calculate_fee(1, p, True)
            fee_complement = calculate_fee(1, 100 - p, True)
            self.assertEqual(fee_p, fee_complement,
                             f"Symmetry broken: fee({p}) = {fee_p} != fee({100-p}) = {fee_complement}")

    def test_boundary_prices(self):
        """Extreme prices: 1c and 99c produce small but nonzero fees."""
        # 1c: ceil(0.07 * 1 * 1 * 99 / 100) = ceil(0.0693) = 1
        self.assertEqual(calculate_fee(1, 1, True), 1)
        # 99c: same by symmetry
        self.assertEqual(calculate_fee(1, 99, True), 1)

    def test_zero_count(self):
        """Zero contracts → zero fee (not an error)."""
        self.assertEqual(calculate_fee(0, 90, True), 0)

    def test_spx_discount(self):
        """SPX finance category: fee_mult_taker=0.035 (half of crypto's 0.07).

        At larger sizes the discount is visible.
        """
        # 50 contracts at 90c crypto: ceil(0.07 * 50 * 90 * 10 / 100) = ceil(31.5) = 32
        # 50 contracts at 90c SPX:    ceil(0.035 * 50 * 90 * 10 / 100) = ceil(15.75) = 16
        crypto_fee = calculate_fee(50, 90, True, fee_mult_taker=0.07)
        spx_fee = calculate_fee(50, 90, True, fee_mult_taker=0.035)
        self.assertEqual(crypto_fee, 32)
        self.assertEqual(spx_fee, 16)
        self.assertLess(spx_fee, crypto_fee)

    def test_max_fee_at_50c(self):
        """Fee is maximized at price=50 (product p*(100-p) peaks at 2500)."""
        fee_50 = calculate_fee(1, 50, True)
        for p in [1, 25, 75, 86, 90, 95, 99]:
            self.assertGreaterEqual(fee_50, calculate_fee(1, p, True),
                                    f"50c fee should be >= {p}c fee")


class TestConvenienceFunctions(unittest.TestCase):
    """calculate_taker_fee and calculate_maker_fee wrappers."""

    def test_taker_matches_core(self):
        """calculate_taker_fee matches calculate_fee with is_taker=True."""
        for count in [1, 5, 20]:
            for price in [86, 90, 95, 99]:
                self.assertEqual(
                    calculate_taker_fee(count, price),
                    calculate_fee(count, price, True),
                )

    def test_maker_always_zero(self):
        """calculate_maker_fee always returns 0."""
        self.assertEqual(calculate_maker_fee(1, 90), 0)
        self.assertEqual(calculate_maker_fee(100, 50), 0)


class TestFeeEdgeCases(unittest.TestCase):
    """Edge cases and regression guards."""

    def test_large_contract_count(self):
        """Large orders don't overflow or produce negative fees."""
        fee = calculate_fee(1000, 90, True)
        expected = math.ceil(0.07 * 1000 * 90 * 10 / 100)
        self.assertEqual(fee, expected)
        self.assertGreater(fee, 0)

    def test_fee_monotonic_in_count(self):
        """More contracts → higher or equal total fee."""
        for p in [86, 90, 95]:
            prev = 0
            for c in range(1, 20):
                fee = calculate_fee(c, p, True)
                self.assertGreaterEqual(fee, prev,
                                        f"Fee not monotonic at {p}c: f({c})={fee} < f({c-1})={prev}")
                prev = fee

    def test_return_type_is_int(self):
        """Fee must be int (cents), not float."""
        fee = calculate_fee(1, 90, True)
        self.assertIsInstance(fee, int)


if __name__ == "__main__":
    unittest.main()
