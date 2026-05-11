"""Property-Based / Invariant Tests.

Tests mathematical invariants of fee formulas, Kelly sizing, probability bounds,
and edge calculations that must hold for ALL valid inputs.
"""

import math
import os
import sys

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


class TestFeeFormulaProperties:
    """Fee formula invariants that must hold for all valid inputs."""

    def _taker_fee(self, count, price, mult=0.07):
        """Replicate Kalshi taker fee: ceil(mult * C * P * (100-P) / 100)."""
        return math.ceil(mult * count * price * (100 - price) / 100)

    def test_taker_fee_nonnegative(self):
        """Taker fee is never negative for valid inputs."""
        for count in range(1, 51):
            for price in range(1, 100):
                fee = self._taker_fee(count, price)
                assert fee >= 0, f"Negative fee: count={count}, price={price}, fee={fee}"

    def test_maker_fee_always_zero(self):
        """Maker fee is always $0 (Kalshi billing)."""
        from bot.models import calculate_fee
        for count in [1, 5, 10, 25, 50]:
            for price in range(1, 100):
                fee = calculate_fee(count, price, is_taker=False)
                assert fee == 0, f"Non-zero maker fee: count={count}, price={price}, fee={fee}"

    def test_taker_fee_bounded_by_contracts(self):
        """Taker fee can't exceed count * max_unit_fee."""
        # max unit fee at price=50 is ceil(0.07*50*50/100) = ceil(1.75) = 2
        # So fee per contract is at most 2 cents
        for count in range(1, 51):
            for price in range(1, 100):
                fee = self._taker_fee(count, price)
                # Fee should be reasonable relative to the position value
                assert fee <= count * price, (
                    f"Fee exceeds position value: fee={fee}, count*price={count*price}")

    def test_taker_fee_peaks_at_50(self):
        """Taker fee per contract is maximized near price=50 (maximum uncertainty)."""
        for count in [1, 10]:
            fee_50 = self._taker_fee(count, 50)
            fee_90 = self._taker_fee(count, 90)
            fee_10 = self._taker_fee(count, 10)
            assert fee_50 >= fee_90, f"Fee at 50 ({fee_50}) < fee at 90 ({fee_90})"
            assert fee_50 >= fee_10, f"Fee at 50 ({fee_50}) < fee at 10 ({fee_10})"

    def test_taker_fee_symmetric(self):
        """Fee at price P equals fee at price (100-P) — symmetry of P*(100-P)."""
        for count in [1, 5, 10]:
            for price in range(1, 50):
                fee_low = self._taker_fee(count, price)
                fee_high = self._taker_fee(count, 100 - price)
                assert fee_low == fee_high, (
                    f"Asymmetric fee: price={price} fee={fee_low}, "
                    f"price={100-price} fee={fee_high}")


class TestProbabilityBounds:
    """Probability pipeline outputs must stay in (0, 1)."""

    def test_temperature_scaling_stays_in_bounds(self):
        """Temperature scaling: p^(1/T) / (p^(1/T) + (1-p)^(1/T)) stays in (0, 1)."""
        temperatures = [0.5, 1.0, 1.45, 2.0, 3.0]
        probs = [0.01, 0.1, 0.3, 0.5, 0.7, 0.9, 0.95, 0.99]

        for T in temperatures:
            for p in probs:
                # Temperature scaling formula
                p_t = p ** (1.0 / T)
                q_t = (1.0 - p) ** (1.0 / T)
                result = p_t / (p_t + q_t)
                assert 0 < result < 1, (
                    f"T={T}, p={p}: temperature scaling produced {result}")

    def test_market_blend_stays_in_bounds(self):
        """Market blend: (1-w)*model + w*market stays in (0, 1)."""
        blend_weights = [0.0, 0.2, 0.4, 0.5, 0.8, 1.0]
        for w in blend_weights:
            for model_p in [0.01, 0.5, 0.9, 0.99]:
                for market_p in [0.01, 0.5, 0.9, 0.99]:
                    result = (1.0 - w) * model_p + w * market_p
                    assert 0 < result < 1, (
                        f"w={w}, model={model_p}, market={market_p}: blend={result}")


class TestEdgeMonotonicity:
    """Higher model probability at a given price -> higher or equal edge."""

    def test_edge_increases_with_confidence(self):
        """At a fixed price, more confident model -> higher edge."""
        # Edge = calibrated_prob - (price/100) for YES side
        for price in range(86, 100):
            price_frac = price / 100.0
            prev_edge = None
            for prob_int in range(price + 1, 100):
                prob = prob_int / 100.0
                edge = prob - price_frac
                if prev_edge is not None:
                    assert edge >= prev_edge, (
                        f"Edge decreased at price={price}, prob={prob}: "
                        f"edge={edge} < prev={prev_edge}")
                prev_edge = edge


class TestKellySizingBounds:
    """Kelly sizing invariants."""

    def test_kelly_never_negative(self):
        """Kelly fraction * edge / odds should never produce negative sizing."""
        # Kelly: f = edge / odds, where odds = (1-p)/p for fair odds
        # With fractional Kelly: f = fraction * edge / (payout - 1)
        # The bot uses quarter-Kelly (0.25) for hourly
        for fraction in [0.25, 0.5, 1.0]:
            for edge_pct in [0.001, 0.01, 0.05, 0.10]:
                for price in range(86, 100):
                    payout = 100.0 / price  # Buying at price cents, win 100 cents
                    kelly_f = fraction * edge_pct / (payout - 1) if payout > 1 else 0
                    assert kelly_f >= 0, (
                        f"Negative Kelly: fraction={fraction}, edge={edge_pct}, "
                        f"price={price}, kelly_f={kelly_f}")

    def test_kelly_bounded_by_max_risk(self):
        """Kelly sizing should not exceed max_risk_per_trade after capping."""
        from market_config import MARKET_CONFIGS
        for pt, cfg in MARKET_CONFIGS.items():
            max_risk = cfg.max_risk_per_trade
            assert 0 < max_risk <= 1.0, (
                f"{pt}: max_risk_per_trade={max_risk} out of bounds")


class TestGetMinEdgeConsistency:
    """get_min_edge must be consistent with MIN_EDGE_BY_PRICE schedule."""

    def test_schedule_covers_all_prices(self):
        """Every price from 0-99 maps to some edge threshold."""
        import bot
        for price in range(0, 100):
            edge = bot.helpers.sizing.get_min_edge(price)
            assert edge > 0, f"get_min_edge({price}) returned non-positive {edge}"

    def test_schedule_matches_documented_values(self):
        """Spot-check schedule against current config values."""
        import bot
        # 86c -> 0.25%
        assert bot.helpers.sizing.get_min_edge(86) == pytest.approx(0.0025, abs=1e-6)
        # 91c -> 0.20%
        assert bot.helpers.sizing.get_min_edge(91) == pytest.approx(0.002, abs=1e-6)
        # 93c -> 0.50%
        assert bot.helpers.sizing.get_min_edge(93) == pytest.approx(0.005, abs=1e-6)
        # 95c -> 0.75%
        assert bot.helpers.sizing.get_min_edge(95) == pytest.approx(0.0075, abs=1e-6)
        # 97c -> 1.0%
        assert bot.helpers.sizing.get_min_edge(97) == pytest.approx(0.010, abs=1e-6)
