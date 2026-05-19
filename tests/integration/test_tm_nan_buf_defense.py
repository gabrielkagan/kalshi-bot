"""NaN buf_pct must not silently bypass the thin-buffer 50-ct cap.

Pre-fix: `if buf_pct is not None and buf_pct < TM_THIN_BUFFER_PCT:` evaluated
`NaN < 0.20 = False`, so a NaN buf_pct silently SKIPPED the thin-buffer cap,
defeating the catastrophic-tail backstop. Fix adds a `math.isnan` short-circuit.
Pre-fix ct=150 at the canonical fixture (price=99, stc=100, balance=$100k, ETH);
post-fix ct=50 (cap binds).

Filed as R1-N2 followup on Sim B (ticket 86ba0vpfd, 2026-05-19).
Pre-existing defense-in-depth gap; production reach was low (scanner at
bot/scanner/__init__.py:3450 computes `(spot - threshold) / threshold * 100
if threshold and threshold > 0 else 0`, which produces NaN only on NaN spot).

Sister mirror: scripts/cal_mlp/sim_pnl.py:_tm_size has the identical
pre-fix line; both must be updated lockstep.

Both production callers (scanner + executor at bot/executor.py:2631) consume
the same helper, so testing the helper directly covers both call sites.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

# sys.path setup matching the sibling convention in
# tests/integration/test_sim_pnl_strategy_sizing.py — module-level so the
# import is once-per-process and avoids per-test path mutation.
_REPO = Path(__file__).resolve().parents[2]
_SIM_PNL_DIR = _REPO / "scripts" / "cal_mlp"
if str(_SIM_PNL_DIR) not in sys.path:
    sys.path.insert(0, str(_SIM_PNL_DIR))


class TestTMNaNBufPctDefense(unittest.TestCase):
    """NaN buf_pct must respect the thin-buffer cap, not silently bypass it."""

    def test_nan_buf_pct_still_hits_thin_buffer_cap(self):
        """Canonical fixture: price=99, stc=100, bal=$100k, ETH.
        Pre-fix returns 150 (thin-buffer cap silently bypassed via NaN<0.20=False);
        post-fix returns 50 (cap binds via math.isnan short-circuit)."""
        from bot.helpers.tm_sweep import tm_compute_contracts
        from bot.constants import TM_THIN_BUFFER_CONTRACT_CAP

        # price=99 (margin=1), STC=100 (safe=1.5), NaN buf → buf_mult=1.0
        # → base = 100*1*1.5*1.0 = 150 (above 50-ct cap; asset cap at $100k
        # ETH 20%/99c = 202 — does not bind)
        ct = tm_compute_contracts(
            price_cents=99, seconds_to_close=100,
            bankroll_cents=100_000_00, asset="ETH",
            buf_pct=float('nan'),
        )
        self.assertLessEqual(
            ct, TM_THIN_BUFFER_CONTRACT_CAP,
            f"NaN buf_pct must hit thin-buffer cap (got ct={ct}, "
            f"cap={TM_THIN_BUFFER_CONTRACT_CAP}). Defense-in-depth: NaN < 0.20 "
            f"is False in IEEE 754, but unknown buffer should default to "
            f"the conservative thin-band cap, not bypass it.",
        )

    def test_sim_pnl_mirror_nan_buf_pct_still_hits_thin_buffer_cap(self):
        """Lockstep with sim_pnl mirror: same NaN defense applies.

        sys.path setup is module-level (matches sibling
        tests/integration/test_sim_pnl_strategy_sizing.py convention)."""
        import sim_pnl

        ct = sim_pnl._tm_size(
            price_cents=99, stc=100.0, balance_cents=100_000_00,
            asset='ETH', buf_pct=float('nan'),
        )
        self.assertLessEqual(
            ct, sim_pnl.TM_THIN_BUFFER_CONTRACT_CAP,
            f"sim_pnl mirror must apply the same NaN defense (got ct={ct})",
        )

    def test_none_buf_pct_still_bypasses_thin_buffer_cap(self):
        """REGRESSION GUARD: None means 'unknown buffer / legacy caller' and
        intentionally does NOT trigger the cap (per Sim B docstring + the
        existing legacy-behavior test). Only NaN is treated as conservative-unknown.

        Pin to EXACT-value parity with the matched-multiplier band buf=0.30%
        (defense against a future refactor that weakens None semantics in a
        way assertGreater would silently allow)."""
        from bot.helpers.tm_sweep import tm_compute_contracts

        ct_none = tm_compute_contracts(
            price_cents=99, seconds_to_close=100,
            bankroll_cents=100_000_00, asset="ETH",
            buf_pct=None,
        )
        ct_30_band = tm_compute_contracts(
            price_cents=99, seconds_to_close=100,
            bankroll_cents=100_000_00, asset="ETH",
            buf_pct=0.30,  # in 0.20-0.40% band → buf_mult=1.0×, same as None
        )
        # None and buf=0.30% must produce IDENTICAL output (both at 1.0× multiplier
        # AND neither at thin-buffer cap). Stronger guard than > cap alone —
        # catches the regression class where None silently maps to a non-1.0×
        # multiplier or quietly fires the cap.
        self.assertEqual(
            ct_none, ct_30_band,
            f"None buf_pct must mirror the 1.0× band (got None→{ct_none}, "
            f"0.30%→{ct_30_band})",
        )

    def test_finite_buf_above_threshold_skips_cap(self):
        """REGRESSION GUARD: finite buf_pct above 0.20% must still skip the cap.
        Renamed from `..._unaffected` to `..._skips_cap` for symmetry with the
        sister `..._still_capped` test below (R1-N3)."""
        from bot.helpers.tm_sweep import tm_compute_contracts
        from bot.constants import TM_THIN_BUFFER_CONTRACT_CAP

        ct = tm_compute_contracts(
            price_cents=99, seconds_to_close=100,
            bankroll_cents=100_000_00, asset="ETH",
            buf_pct=0.50,
        )
        self.assertGreater(
            ct, TM_THIN_BUFFER_CONTRACT_CAP,
            f"buf=0.50% (above threshold) must skip cap (got ct={ct})",
        )

    def test_finite_buf_below_threshold_still_capped(self):
        """REGRESSION GUARD: finite buf_pct below 0.20% must still hit the cap."""
        from bot.helpers.tm_sweep import tm_compute_contracts
        from bot.constants import TM_THIN_BUFFER_CONTRACT_CAP

        ct = tm_compute_contracts(
            price_cents=99, seconds_to_close=100,
            bankroll_cents=100_000_00, asset="ETH",
            buf_pct=0.10,
        )
        self.assertLessEqual(
            ct, TM_THIN_BUFFER_CONTRACT_CAP,
            f"buf=0.10% (below threshold) must still hit cap (got ct={ct})",
        )


if __name__ == "__main__":
    unittest.main()
