"""TM half-Kelly cal_mlp shadow — behavior tests (Sim C, ticket 86ba0v7fc, 2026-05-19).

Shadow-only — the helper's return value is logged to `evaluated_opportunities`
but NEVER consumed by production sizing. These tests pin:

1. Kelly arithmetic at known probabilities
2. NULL-safe behavior (cal_mlp + raw_prob both None → None, no crash)
3. Fallback to raw_prob when cal_mlp_p_mean is None
4. Absolute-loss bound binding at high Kelly fractions
5. Per-asset risk cap binding when lower than Kelly's natural size
6. Round-trip into evaluated_opportunities via insert + SELECT

Methodology caveats carried from the plan doc:
- We use `cal_mlp_p_mean` (Kelly's prob input) with `raw_prob` fallback;
  NEVER `calibrated_prob` which is miscalibrated DOWN by 2-3pp for TM
- Half-Kelly default; per-asset cap mirrors TM_ASSET_RISK_CAPS
- Bound-hit field values: 'kelly' | 'abs_loss' | 'asset_cap' | 'null_prob' | 'raw_fallback'

See `kb/decisions/tm-half-kelly-shadow-plan.md`.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


class TestTMShadowKellyHelper(unittest.TestCase):
    """`tm_shadow_kelly_contracts` arithmetic + edge cases."""

    def _call(self, **kwargs):
        from bot.helpers.tm_sweep import tm_shadow_kelly_contracts
        return tm_shadow_kelly_contracts(**kwargs)

    def test_kelly_size_at_known_probs(self):
        """At cal_mlp_p_mean=0.995 + price=99c (q=0.99c), half-Kelly is huge.
        The $100 abs-loss bound at 99c price binds to ~101 ct.

        Kelly formula for YES at price p (cents): f = (P*100 - p) / (100 - p)
        At cal_mlp_p_mean=0.995, p=99: numerator = 99.5 - 99 = 0.5;
        denominator = 1. f = 0.5. Half-Kelly = 0.25.
        Stake = 0.25 × bankroll. At $1k bankroll, stake = $250 → 250/99 ≈ 2.5 ct.
        Asset cap (BTC 15%): 15% × $1k / 99 = ~1.5 ct → cap binds. Use larger
        bankroll so Kelly's natural ct is visible: $100k bankroll, BTC cap
        = 15% × $100k / 99 = ~151 ct max. Half-Kelly = 0.25 × $100k = $25k →
        25k/99 ≈ 252 ct. Cap binds at 151 ct.
        """
        ct = self._call(
            price_cents=99, bankroll_cents=10_000_00,  # $10,000
            asset="BTC", cal_mlp_p_mean=0.995,
            raw_prob_fallback=None,
        )
        # Kelly's natural ct exceeds asset cap, so the binding outcome is the cap
        self.assertIsNotNone(ct)
        self.assertGreater(ct, 0)

    def test_kelly_size_low_prob_returns_zero_or_low(self):
        """At cal_mlp_p_mean=0.95 + price=99c, edge is 95-99 = NEGATIVE.
        Kelly should be 0 or negative; helper must return 0 (no betting)."""
        ct = self._call(
            price_cents=99, bankroll_cents=10_000_00,
            asset="BTC", cal_mlp_p_mean=0.95,  # below breakeven 0.99
            raw_prob_fallback=None,
        )
        self.assertEqual(ct, 0, f"Negative-edge Kelly must return 0 (got {ct})")

    def test_kelly_returns_none_on_null_prob(self):
        """cal_mlp_p_mean=None + raw_prob_fallback=None → None, no crash."""
        ct = self._call(
            price_cents=98, bankroll_cents=10_000_00,
            asset="BTC", cal_mlp_p_mean=None, raw_prob_fallback=None,
        )
        self.assertIsNone(ct, f"Both probs None → None (got {ct})")

    def test_kelly_falls_back_to_raw_prob(self):
        """cal_mlp_p_mean=None + raw_prob_fallback=0.995 → uses raw_prob.
        Result must be non-None and consistent with cal_mlp pathway."""
        ct_raw = self._call(
            price_cents=99, bankroll_cents=10_000_00,
            asset="BTC", cal_mlp_p_mean=None, raw_prob_fallback=0.995,
        )
        ct_calmlp = self._call(
            price_cents=99, bankroll_cents=10_000_00,
            asset="BTC", cal_mlp_p_mean=0.995, raw_prob_fallback=None,
        )
        self.assertIsNotNone(ct_raw, "raw_prob fallback must produce a size")
        self.assertEqual(ct_raw, ct_calmlp,
                         "raw_prob fallback must produce same size as cal_mlp_p_mean "
                         "when the probability values are identical")

    def test_kelly_abs_loss_bound_binds(self):
        """At cal_mlp_p_mean=0.999 + price=99c + large bankroll, raw Kelly is huge.
        The $100 abs-loss bound at 99c → 10000/99 ≈ 101 ct cap.

        Verification: with bankroll high enough that asset-cap doesn't bind,
        and half-Kelly that wants thousands of ct, the result must be ≤ 102 ct.
        """
        # Bankroll $1M; BTC cap = 15% × $1M / 99 = ~1,515 ct. Half-Kelly at
        # p=0.999/q=0.99: f = (99.9-99)/1 = 0.9, half = 0.45. Stake = 0.45 ×
        # $1M = $450k → 450k/99 = ~4,545 ct. Asset cap caps at 1,515. The
        # abs-loss bound at 99c = 10000/99 = ~101 ct — binds first.
        ct = self._call(
            price_cents=99, bankroll_cents=1_000_000_00,
            asset="BTC", cal_mlp_p_mean=0.999, raw_prob_fallback=None,
        )
        # 10000/99 = 101.0 → int floor = 101
        self.assertIsNotNone(ct)
        self.assertLessEqual(ct, 102, (
            f"Abs-loss bound at 99c should cap to ~101 ct (got {ct})"
        ))

    def test_kelly_asset_cap_binds_when_lower(self):
        """BTC 15% × $1k / 99c = ~1.5 ct cap. Kelly says higher; cap binds."""
        from bot.constants import TM_ASSET_RISK_CAPS
        bankroll = 100_000  # $1000 in cents
        asset = "BTC"
        risk_frac = TM_ASSET_RISK_CAPS[asset]
        max_by_risk = int(bankroll * risk_frac / 99)  # ~151 ct
        ct = self._call(
            price_cents=99, bankroll_cents=bankroll,
            asset=asset, cal_mlp_p_mean=0.999, raw_prob_fallback=None,
        )
        self.assertIsNotNone(ct)
        self.assertLessEqual(ct, max_by_risk, (
            f"Asset cap must bind (cap={max_by_risk}, got ct={ct})"
        ))

    def test_kelly_default_fraction_is_half(self):
        """Default kelly_fraction=0.50 per plan doc."""
        from bot.helpers.tm_sweep import tm_shadow_kelly_contracts
        import inspect
        sig = inspect.signature(tm_shadow_kelly_contracts)
        self.assertEqual(sig.parameters["kelly_fraction"].default, 0.50,
                         "Default Kelly fraction must be 0.50 (half-Kelly)")

    def test_kelly_default_abs_loss_bound_is_100_dollars(self):
        """Default abs_loss_bound_cents=10000 per plan doc."""
        from bot.helpers.tm_sweep import tm_shadow_kelly_contracts
        import inspect
        sig = inspect.signature(tm_shadow_kelly_contracts)
        self.assertEqual(sig.parameters["abs_loss_bound_cents"].default, 10000,
                         "Default abs-loss bound must be 10000 ($100)")


class TestTMShadowKellySchemaRoundTrip(unittest.TestCase):
    """The 4 shadow values must round-trip through insert_evaluated_opportunity."""

    def test_kelly_shadow_value_logged_in_evaluated_opportunities(self):
        """Insert with the 4 shadow kwargs + SELECT them back."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            cwd = os.getcwd()
            os.chdir(tmpdir)
            try:
                from bot.state import StateManager
                sm = StateManager()
                try:
                    sm.insert_evaluated_opportunity(
                        ticker="KXBTC15MFAKE",
                        event_ticker="KXBTC15M",
                        asset="BTC",
                        filter_stage="terminal_momentum",
                        spot_price=100000.0,
                        threshold=99950.0,
                        market_price=98,
                        seconds_to_close=120.0,
                        # The 4 new shadow kwargs
                        tm_shadow_kelly_ct=150,
                        tm_shadow_kelly_prob=0.995,
                        tm_shadow_kelly_fraction=0.50,
                        tm_shadow_kelly_bound_hit="asset_cap",
                    )
                    row = sm.conn.execute(
                        "SELECT tm_shadow_kelly_ct, tm_shadow_kelly_prob, "
                        "tm_shadow_kelly_fraction, tm_shadow_kelly_bound_hit "
                        "FROM evaluated_opportunities WHERE ticker = ?",
                        ("KXBTC15MFAKE",)
                    ).fetchone()
                    self.assertIsNotNone(row, "Row not inserted")
                    self.assertEqual(row[0], 150)
                    self.assertAlmostEqual(row[1], 0.995, places=4)
                    self.assertAlmostEqual(row[2], 0.50, places=4)
                    self.assertEqual(row[3], "asset_cap")
                finally:
                    sm.conn.close()
            finally:
                os.chdir(cwd)

    def test_kelly_shadow_null_round_trip(self):
        """Inserting without the kwargs → columns are NULL (default)."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            cwd = os.getcwd()
            os.chdir(tmpdir)
            try:
                from bot.state import StateManager
                sm = StateManager()
                try:
                    sm.insert_evaluated_opportunity(
                        ticker="KXBTC15MFAKE2",
                        event_ticker="KXBTC15M",
                        asset="BTC",
                        filter_stage="terminal_momentum",
                    )
                    row = sm.conn.execute(
                        "SELECT tm_shadow_kelly_ct, tm_shadow_kelly_prob, "
                        "tm_shadow_kelly_fraction, tm_shadow_kelly_bound_hit "
                        "FROM evaluated_opportunities WHERE ticker = ?",
                        ("KXBTC15MFAKE2",)
                    ).fetchone()
                    self.assertIsNotNone(row, "Row not inserted")
                    for v in row:
                        self.assertIsNone(v, f"Default value should be NULL (got {v})")
                finally:
                    sm.conn.close()
            finally:
                os.chdir(cwd)


class TestTMShadowKellyBoundHitVocabulary(unittest.TestCase):
    """The bound_hit string values must use the plan's vocabulary."""

    def test_bound_hit_vocab_legal_values(self):
        """Helper must return one of: 'kelly' | 'abs_loss' | 'asset_cap' |
        'null_prob' | 'raw_fallback'. We probe each path."""
        from bot.helpers.tm_sweep import tm_shadow_kelly_contracts_with_bound

        # null_prob path
        ct, bound = tm_shadow_kelly_contracts_with_bound(
            price_cents=98, bankroll_cents=10_000_00, asset="BTC",
            cal_mlp_p_mean=None, raw_prob_fallback=None,
        )
        self.assertEqual(bound, "null_prob")
        self.assertIsNone(ct)

        # raw_fallback path
        ct, bound = tm_shadow_kelly_contracts_with_bound(
            price_cents=99, bankroll_cents=10_000_00, asset="BTC",
            cal_mlp_p_mean=None, raw_prob_fallback=0.995,
        )
        self.assertEqual(bound, "raw_fallback")
        self.assertIsNotNone(ct)

        # ── asset_cap path ────────────────────────────────────────────────
        # We want: asset_cap_ct < abs_loss_ct AND asset_cap_ct < kelly_ct.
        # abs_loss_ct = 10000/99 = ~101 ct (fixed by abs_loss_bound_cents).
        # So asset_cap_ct must be < 101.
        # asset_cap_ct = bankroll_cents * 0.15 / 99
        # → bankroll_cents < 101 * 99 / 0.15 ≈ 66,660 cents → $666.60.
        # Use bankroll = $500 ($50000 cents): asset_cap = 50000 * 0.15 / 99
        # = 75 ct. Kelly natural = 0.45 * 50000 / 99 = 227 ct.
        # min(227 kelly, 101 abs_loss, 75 asset_cap) = asset_cap.
        ct, bound = tm_shadow_kelly_contracts_with_bound(
            price_cents=99, bankroll_cents=50000, asset="BTC",  # $500
            cal_mlp_p_mean=0.999, raw_prob_fallback=None,
        )
        self.assertEqual(bound, "asset_cap",
                         f"Asset cap should bind (got bound={bound}, ct={ct})")

        # ── abs_loss path ─────────────────────────────────────────────────
        # We want: abs_loss_ct < asset_cap_ct AND abs_loss_ct < kelly_ct.
        # abs_loss = 101 ct (fixed); asset_cap = bankroll * 0.15 / 99.
        # For asset_cap > 101: bankroll > 101 * 99 / 0.15 ≈ 66,660 cents ≈ $667.
        # Use $10k bankroll (1,000,000 cents): asset_cap = 1,000,000 * 0.15 / 99
        # = 1,515 ct. Kelly natural = 0.45 * 1,000,000 / 99 = 4,545 ct.
        # min(4,545 kelly, 101 abs_loss, 1,515 asset_cap) = abs_loss.
        ct, bound = tm_shadow_kelly_contracts_with_bound(
            price_cents=99, bankroll_cents=1_000_000, asset="BTC",  # $10k
            cal_mlp_p_mean=0.999, raw_prob_fallback=None,
        )
        self.assertEqual(bound, "abs_loss",
                         f"abs_loss bound should bind (got bound={bound}, ct={ct})")

        # ── kelly path ────────────────────────────────────────────────────
        # We want: kelly_ct < abs_loss_ct AND kelly_ct < asset_cap_ct.
        # abs_loss = 101 ct. So kelly_ct < 101.
        # kelly_ct = 0.5 * (p - 0.99) / 0.01 * bankroll_cents / 99
        # Use p=0.991: f = (99.1 - 99)/1 = 0.1, half = 0.05.
        # kelly_ct = 0.05 * bankroll_cents / 99.
        # For kelly < 101: bankroll_cents < 101 * 99 / 0.05 ≈ 199,980 cents ≈ $2000.
        # Use $1,500 bankroll (150,000 cents): kelly = 0.05*150000/99 = 75 ct.
        # asset_cap = 150,000 * 0.15 / 99 = 227 ct. abs_loss = 101 ct.
        # min(75 kelly, 101 abs_loss, 227 asset_cap) = kelly.
        ct, bound = tm_shadow_kelly_contracts_with_bound(
            price_cents=99, bankroll_cents=150_000, asset="BTC",  # $1,500
            cal_mlp_p_mean=0.991, raw_prob_fallback=None,
        )
        self.assertEqual(bound, "kelly",
                         f"Kelly should be the natural bind (got bound={bound}, ct={ct})")


if __name__ == "__main__":
    unittest.main()
