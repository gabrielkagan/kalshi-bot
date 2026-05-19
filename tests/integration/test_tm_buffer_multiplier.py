"""TM buf-multiplier sizing (Sim B) — wide-buffer scale-up tests.

Sim B replaces flat sizing at wide buf_pct with a multiplicative scale-up,
keeping the TM_THIN_BUFFER_CONTRACT_CAP=50 backstop intact for buf<0.20%.

Data justification (30d, phantom-corrected, ticket 86ba0v6z1):
- buf<0.20% (CAPPED): n=698, −$12, −0.04¢/ct → keep cap; multiplier=1.0
- buf 0.20-0.40%: n=262, +$53, +0.26¢/ct → multiplier=1.0 (no change)
- buf 0.40-0.80%: n=71, +$58, +1.40¢/ct → multiplier=2.0 (scale up)
- buf ≥0.80%: n=24, +$24, +1.67¢/ct → multiplier=3.0 (scale up)

KB plan: kb/decisions/tm-buf-multiplier-sizing-plan.md
"""
from __future__ import annotations

import unittest


class TestTMBufferMultiplierConstant(unittest.TestCase):
    """The new constant must exist with the data-justified shape."""

    def test_constant_is_defined(self):
        from bot import constants
        self.assertTrue(hasattr(constants, "TM_BUFFER_SIZE_MULTIPLIER"),
                        "TM_BUFFER_SIZE_MULTIPLIER must be defined in bot.constants")

    def test_constant_is_sorted_tuples_ascending_by_floor(self):
        from bot.constants import TM_BUFFER_SIZE_MULTIPLIER
        floors = [t[0] for t in TM_BUFFER_SIZE_MULTIPLIER]
        self.assertEqual(floors, sorted(floors),
                         "TM_BUFFER_SIZE_MULTIPLIER must be sorted ascending by floor")

    def test_constant_starts_at_zero_floor(self):
        """The first entry must cover buf_pct=0.0 — otherwise lookup is undefined at thin buffer."""
        from bot.constants import TM_BUFFER_SIZE_MULTIPLIER
        self.assertEqual(TM_BUFFER_SIZE_MULTIPLIER[0][0], 0.0)

    def test_constant_thin_buffer_multiplier_is_one(self):
        """At thin buffer (<0.20%), multiplier MUST be 1.0 — the 50-ct cap does the bounding.
        Raising the multiplier here would push trades back into the catastrophic-loss class.

        Mirrors the canonical resolution order (reversed walk, last floor wins) so a
        future reshuffle of TM_BUFFER_SIZE_MULTIPLIER ordering still validates the
        semantic 'which entry resolves at buf=0.10%' rather than 'first list entry'."""
        from bot.constants import TM_BUFFER_SIZE_MULTIPLIER
        thin_mult = next(m for floor, m in reversed(TM_BUFFER_SIZE_MULTIPLIER) if floor <= 0.10)
        self.assertEqual(thin_mult, 1.0,
                         "Thin-buffer multiplier must remain 1.0; cap is the bound")

    def test_constant_wide_buffer_scales_up(self):
        """Wide buffer (≥0.40%) multiplier must exceed 1.0 — that's the Bit's purpose."""
        from bot.constants import TM_BUFFER_SIZE_MULTIPLIER
        # Find multiplier at buf=0.50% (in 0.40-0.80% band per plan)
        sorted_entries = sorted(TM_BUFFER_SIZE_MULTIPLIER)
        applicable = [m for floor, m in sorted_entries if floor <= 0.50]
        self.assertGreater(applicable[-1], 1.0,
                           "Multiplier at buf=0.50% must exceed 1.0 (Sim B purpose)")


class TestTMBufferMultiplierApplication(unittest.TestCase):
    """tm_compute_contracts must apply the multiplier in the base formula
    while preserving the thin-buffer 50-ct cap and per-asset risk caps."""

    def _call(self, **kwargs):
        from bot.helpers.tm_sweep import tm_compute_contracts
        return tm_compute_contracts(**kwargs)

    def test_thin_buffer_cap_still_binds(self):
        """At buf_pct=0.10%, ct must still respect the 50-ct cap (backstop preserved)."""
        from bot.constants import TM_THIN_BUFFER_CONTRACT_CAP
        ct = self._call(price_cents=98, seconds_to_close=100,
                        bankroll_cents=100_000_00, asset="ETH", buf_pct=0.10)
        self.assertLessEqual(ct, TM_THIN_BUFFER_CONTRACT_CAP,
                             f"Thin-buffer cap must still bind (got ct={ct})")

    def test_wide_buffer_scales_higher_than_normal(self):
        """At buf_pct=0.50% (in 0.40-0.80% band), ct must STRICTLY exceed
        ct at buf_pct=0.30% (in 0.20-0.40% band where multiplier=1.0).

        Uses price=99 + large bankroll to avoid per-asset cap binding so
        the multiplier effect is visible. Base formula at price=99, STC=300
        (normal mult=1.0) = TM_BASE × 1 × 1.0 = 100 ct.
        At 2× multiplier → 200 ct. Asset cap at $10k bankroll BTC 15% / 99c
        = 15 ct... too low. Use $100M bankroll: 100M × 0.15 / 99 ≈ 151k ct."""
        ct_normal = self._call(price_cents=99, seconds_to_close=300,
                               bankroll_cents=100_000_000_00, asset="BTC", buf_pct=0.30)
        ct_wide = self._call(price_cents=99, seconds_to_close=300,
                             bankroll_cents=100_000_000_00, asset="BTC", buf_pct=0.50)
        self.assertGreater(ct_wide, ct_normal,
                           f"buf=0.50% must scale higher than buf=0.30% "
                           f"(got wide={ct_wide}, normal={ct_normal})")

    def test_very_wide_buffer_scales_highest(self):
        """At buf_pct=1.00% (in ≥0.80% band), ct must >= ct at buf_pct=0.50% (0.40-0.80% band)."""
        ct_wide = self._call(price_cents=99, seconds_to_close=300,
                             bankroll_cents=100_000_000_00, asset="BTC", buf_pct=0.50)
        ct_very_wide = self._call(price_cents=99, seconds_to_close=300,
                                  bankroll_cents=100_000_000_00, asset="BTC", buf_pct=1.00)
        self.assertGreaterEqual(ct_very_wide, ct_wide,
                                f"buf=1.00% must scale >= buf=0.50% "
                                f"(got very_wide={ct_very_wide}, wide={ct_wide})")

    def test_per_asset_risk_cap_still_binds(self):
        """With a realistic balance where the per-asset cap would bind, the multiplier
        must NOT bypass the cap — risk_frac × bankroll / price is the absolute upper bound."""
        from bot.constants import TM_ASSET_RISK_CAPS
        # BTC 15% × $1000 / 99c = ~151 ct max
        bankroll = 100_000  # $1000 in cents
        asset = "BTC"
        risk_frac = TM_ASSET_RISK_CAPS[asset]
        # max_by_risk uses risk_cap_price default = price_cents = 99
        max_by_risk = int(bankroll * risk_frac / 99)
        ct_very_wide = self._call(price_cents=99, seconds_to_close=300,
                                  bankroll_cents=bankroll, asset=asset, buf_pct=1.00)
        self.assertLessEqual(ct_very_wide, max_by_risk,
                             f"Per-asset cap must bind even at 3× multiplier "
                             f"(got ct={ct_very_wide}, cap={max_by_risk})")

    def test_buf_pct_none_preserves_legacy_behavior(self):
        """buf_pct=None must behave as if multiplier=1.0 (no scale-up) — backward compat."""
        ct_none = self._call(price_cents=99, seconds_to_close=300,
                             bankroll_cents=100_000_000_00, asset="BTC", buf_pct=None)
        ct_baseline = self._call(price_cents=99, seconds_to_close=300,
                                 bankroll_cents=100_000_000_00, asset="BTC", buf_pct=0.30)
        # buf=0.30% multiplier is 1.0 per plan; None should match.
        self.assertEqual(ct_none, ct_baseline,
                         "buf_pct=None must mirror buf=0.30% (both at multiplier=1.0)")

    def test_tm_max_contracts_hard_ceiling_still_binds(self):
        """Even at 3× multiplier with unlimited bankroll, TM_MAX_CONTRACTS=500 hard ceiling holds."""
        from bot.constants import TM_MAX_CONTRACTS
        ct = self._call(price_cents=96, seconds_to_close=100,  # margin=4, safe stc=1.5 → base=600
                        bankroll_cents=100_000_000_00, asset="BTC", buf_pct=1.00)
        self.assertLessEqual(ct, TM_MAX_CONTRACTS,
                             f"TM_MAX_CONTRACTS hard ceiling must bind (got ct={ct})")


class TestTMBufferMultiplierLockstep(unittest.TestCase):
    """scripts/cal_mlp/sim_pnl.py mirrors the TM sizing constants; both must move together.

    Per kb/decisions/tm-buf-multiplier-sizing-plan.md "Sister docs" + the existing
    TM sizing chain (sim_pnl.py:TM_THIN_BUFFER_PCT line ~293 mirrors bot/constants.py:1289).
    """

    def test_sim_pnl_buf_multiplier_constant_matches_canonical(self):
        """sim_pnl.py must mirror TM_BUFFER_SIZE_MULTIPLIER from bot.constants.

        Uses ast.literal_eval on the assignment RHS so the test handles nested
        tuples without regex paren-counting (a prior version of this test used
        a non-greedy `\\(.*?\\)` regex that truncated at the first inner `)`).
        """
        from bot.constants import TM_BUFFER_SIZE_MULTIPLIER as canonical
        import ast
        import os
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        repo_root = os.path.dirname(here)
        sim_pnl_path = os.path.join(repo_root, "scripts", "cal_mlp", "sim_pnl.py")
        with open(sim_pnl_path) as f:
            src = f.read()
        # Parse the module and find the assignment.
        tree = ast.parse(src)
        mirrored = None
        for node in ast.walk(tree):
            if (isinstance(node, ast.Assign)
                    and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id == "TM_BUFFER_SIZE_MULTIPLIER"):
                mirrored = ast.literal_eval(node.value)
                break
        self.assertIsNotNone(mirrored,
                             "TM_BUFFER_SIZE_MULTIPLIER assignment not found in sim_pnl.py")
        self.assertEqual(tuple(mirrored), tuple(canonical),
                         "sim_pnl.py mirror must match canonical TM_BUFFER_SIZE_MULTIPLIER")


if __name__ == "__main__":
    unittest.main()
