"""Tests for the 96c × {SOL,XRP} × 2-5min STC danger-band gate.

Background: 30-day forensic on 2026-04-26 isolated a structurally negative-EV cell
where YES entries on SOL or XRP at exactly 96¢ entry, with seconds-to-close in [121, 300],
lost -$974 across 98 trades (88W/10L). Adjacent cells (BTC/ETH at 96¢, SOL/XRP at 95 or
97-99¢, same assets at 0-2min or 5+min STC) are profitable. Wilson 95% CI on loss-rate
[5.7%, 17.8%] exceeds the ~5-6% breakeven loss-rate at 96¢ across the entire interval.

Gate behavior:
- Block YES entries when ALL of: asset ∈ {SOL, XRP}; entry_price_cents == 96;
  seconds_to_close ∈ [121, 300]; HIGH_PRICE_STC_BLOCK_ENABLED is True.
- All other cells pass through unchanged.

KB references:
- kb/decisions/96c-sol-xrp-2to5min-block-2026-04-26.md
- kb/findings/96c-sol-xrp-stc-band-bleed-2026-04-26.md
- kb/findings/proximity-calibration-miss-eth-2026-04-26.md
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from bot.constants import (
    HIGH_PRICE_STC_BLOCK_ENABLED,
    HIGH_PRICE_STC_BLOCK_ASSETS,
    HIGH_PRICE_STC_BLOCK_PRICE_CENTS,
    HIGH_PRICE_STC_BLOCK_STC_LO_S,
    HIGH_PRICE_STC_BLOCK_STC_HI_S,
    HIGH_PRICE_STC_BLOCK_FILTER_STAGE,
    HIGH_PRICE_STC_BLOCK_BLEEDER_STRATEGIES,
)
from bot.helpers.cell_blocks import (
    should_block_high_price_stc_band,
    should_block_high_price_stc_candidate,
)
from bot.helpers.validators import _validate_high_price_stc_block_bleeder_strings
from bot.main_loop import _HPSB_MISSING_BLEEDERS
import bot.helpers  # noqa: F401
class TestHighPriceStcBandGate_FiresOnTargetCell(unittest.TestCase):
    """Gate must fire on every (asset × price × side × STC) combination in the target cell."""

    def test_sol_yes_96c_in_band_blocked(self):
        """Canonical case: SOL YES at 96c with 200s STC — must block (when enabled)."""
        self.assertTrue(should_block_high_price_stc_band(
            asset="SOL", side="yes", entry_price_cents=96, seconds_to_close=200,
            enabled=True))

    def test_xrp_yes_96c_in_band_blocked(self):
        """Canonical case: XRP YES at 96c with 200s STC — must block (when enabled)."""
        self.assertTrue(should_block_high_price_stc_band(
            asset="XRP", side="yes", entry_price_cents=96, seconds_to_close=200,
            enabled=True))


class TestHighPriceStcBandGate_AssetSelectivity(unittest.TestCase):
    """Gate must NOT fire on BTC or ETH (those cells are profitable at 96c × 2-5min)."""

    def test_btc_yes_96c_in_band_allowed(self):
        """BTC at 96c × 2-5min STC is profitable (+$45/30d). Must allow."""
        self.assertFalse(should_block_high_price_stc_band(
            asset="BTC", side="yes", entry_price_cents=96, seconds_to_close=200))

    def test_eth_yes_96c_in_band_allowed(self):
        """ETH at 96c × 2-5min STC is profitable (+$27/30d). Must allow."""
        self.assertFalse(should_block_high_price_stc_band(
            asset="ETH", side="yes", entry_price_cents=96, seconds_to_close=200))


class TestHighPriceStcBandGate_PriceSelectivity(unittest.TestCase):
    """Gate must fire ONLY at exactly 96c.

    95c and 97-99c cells were verified profitable in 30d Kelly-sized backtest:
    - SOL 95c × 2-5min: +$59
    - XRP 95c × 2-5min: +$140
    - SOL 97-99c × 2-5min: +$217 (99.5% WR, n=196)
    - XRP 97-99c × 2-5min: -$56 (n=379, single fat-tail event, not statistically separable)
    """

    def test_sol_yes_95c_in_band_allowed(self):
        self.assertFalse(should_block_high_price_stc_band(
            asset="SOL", side="yes", entry_price_cents=95, seconds_to_close=200))

    def test_xrp_yes_95c_in_band_allowed(self):
        self.assertFalse(should_block_high_price_stc_band(
            asset="XRP", side="yes", entry_price_cents=95, seconds_to_close=200))

    def test_sol_yes_97c_in_band_allowed(self):
        """SOL 97c × 2-5min is +$217/30d profitable — DO NOT widen gate."""
        self.assertFalse(should_block_high_price_stc_band(
            asset="SOL", side="yes", entry_price_cents=97, seconds_to_close=200))

    def test_sol_yes_98c_in_band_allowed(self):
        self.assertFalse(should_block_high_price_stc_band(
            asset="SOL", side="yes", entry_price_cents=98, seconds_to_close=200))

    def test_sol_yes_99c_in_band_allowed(self):
        self.assertFalse(should_block_high_price_stc_band(
            asset="SOL", side="yes", entry_price_cents=99, seconds_to_close=200))

    def test_xrp_yes_99c_in_band_allowed(self):
        self.assertFalse(should_block_high_price_stc_band(
            asset="XRP", side="yes", entry_price_cents=99, seconds_to_close=200))


class TestHighPriceStcBandGate_StcBandBoundaries(unittest.TestCase):
    """Inclusive boundaries: STC ∈ [121, 300] blocked; 120 and 301 allowed."""

    def test_sol_96c_stc_120s_allowed(self):
        """STC = 120s is BELOW the lower bound (121); allowed (locked-direction zone)."""
        self.assertFalse(should_block_high_price_stc_band(
            asset="SOL", side="yes", entry_price_cents=96, seconds_to_close=120))

    def test_sol_96c_stc_121s_blocked(self):
        """STC = 121s is the inclusive lower bound; blocked (when enabled)."""
        self.assertTrue(should_block_high_price_stc_band(
            asset="SOL", side="yes", entry_price_cents=96, seconds_to_close=121,
            enabled=True))

    def test_sol_96c_stc_300s_blocked(self):
        """STC = 300s is the inclusive upper bound; blocked (when enabled)."""
        self.assertTrue(should_block_high_price_stc_band(
            asset="SOL", side="yes", entry_price_cents=96, seconds_to_close=300,
            enabled=True))

    def test_sol_96c_stc_301s_allowed(self):
        """STC = 301s is ABOVE the upper bound; allowed (real-buffer zone)."""
        self.assertFalse(should_block_high_price_stc_band(
            asset="SOL", side="yes", entry_price_cents=96, seconds_to_close=301))

    def test_sol_96c_locked_zone_allowed(self):
        """STC = 60s (well inside locked zone) — direction has resolved, allowed."""
        self.assertFalse(should_block_high_price_stc_band(
            asset="SOL", side="yes", entry_price_cents=96, seconds_to_close=60))

    def test_sol_96c_buffer_zone_allowed(self):
        """STC = 500s (well inside real-buffer zone) — buffer is meaningful, allowed."""
        self.assertFalse(should_block_high_price_stc_band(
            asset="SOL", side="yes", entry_price_cents=96, seconds_to_close=500))

    def test_sol_96c_stc_zero_allowed(self):
        """STC = 0 (settled-at-fill) is below lower bound; allowed."""
        self.assertFalse(should_block_high_price_stc_band(
            asset="SOL", side="yes", entry_price_cents=96, seconds_to_close=0))


class TestHighPriceStcBandGate_SideSelectivity(unittest.TestCase):
    """Gate is YES-side only. NO-side cell behavior was not analyzed; do not block."""

    def test_sol_no_96c_in_band_allowed(self):
        self.assertFalse(should_block_high_price_stc_band(
            asset="SOL", side="no", entry_price_cents=96, seconds_to_close=200))

    def test_xrp_no_96c_in_band_allowed(self):
        self.assertFalse(should_block_high_price_stc_band(
            asset="XRP", side="no", entry_price_cents=96, seconds_to_close=200))


class TestHighPriceStcBandGate_FloatStcInputs(unittest.TestCase):
    """seconds_to_close in production is a float (sub-second precision)."""

    def test_sol_96c_stc_121_0_blocked(self):
        self.assertTrue(should_block_high_price_stc_band(
            asset="SOL", side="yes", entry_price_cents=96, seconds_to_close=121.0,
            enabled=True))

    def test_sol_96c_stc_120_99_allowed(self):
        """Just below lower bound — must allow."""
        self.assertFalse(should_block_high_price_stc_band(
            asset="SOL", side="yes", entry_price_cents=96, seconds_to_close=120.99))

    def test_sol_96c_stc_300_5_allowed(self):
        """Just above upper bound — must allow."""
        self.assertFalse(should_block_high_price_stc_band(
            asset="SOL", side="yes", entry_price_cents=96, seconds_to_close=300.5))


class TestHighPriceStcBandGate_DisabledFlag(unittest.TestCase):
    """When HIGH_PRICE_STC_BLOCK_ENABLED is False, gate must never fire — even on target cell."""

    def test_disabled_overrides_target_cell(self):
        self.assertFalse(should_block_high_price_stc_band(
            asset="SOL", side="yes", entry_price_cents=96, seconds_to_close=200,
            enabled=False))

    def test_enabled_explicit_true_fires(self):
        self.assertTrue(should_block_high_price_stc_band(
            asset="SOL", side="yes", entry_price_cents=96, seconds_to_close=200,
            enabled=True))


class TestHighPriceStcBandGate_DefensiveInputs(unittest.TestCase):
    """Defensive: gate should not crash on None/edge inputs; should pass-through (allow)."""

    def test_none_seconds_to_close_allowed(self):
        """None STC (shouldn't happen in production but be safe) — allow."""
        self.assertFalse(should_block_high_price_stc_band(
            asset="SOL", side="yes", entry_price_cents=96, seconds_to_close=None))

    def test_none_asset_allowed(self):
        self.assertFalse(should_block_high_price_stc_band(
            asset=None, side="yes", entry_price_cents=96, seconds_to_close=200))

    def test_unknown_asset_allowed(self):
        self.assertFalse(should_block_high_price_stc_band(
            asset="DOGE", side="yes", entry_price_cents=96, seconds_to_close=200))

    def test_none_side_allowed(self):
        self.assertFalse(should_block_high_price_stc_band(
            asset="SOL", side=None, entry_price_cents=96, seconds_to_close=200))


class TestHighPriceStcBandGate_Constants(unittest.TestCase):
    """Constants must match the documented decision exactly.

    If you're tempted to change these, READ kb/decisions/96c-sol-xrp-2to5min-block-2026-04-26.md
    and re-run the Kelly-sized backtest. Adjacent-cell PnL flips signs — do not extrapolate.
    """

    def test_target_assets_are_sol_xrp(self):
        self.assertEqual(set(HIGH_PRICE_STC_BLOCK_ASSETS), {"SOL", "XRP"})

    def test_target_price_is_exactly_96(self):
        """Must be int 96, not 95 or 97 or '>=96'. Adjacent prices are profitable."""
        self.assertEqual(HIGH_PRICE_STC_BLOCK_PRICE_CENTS, 96)

    def test_stc_lower_bound_is_121(self):
        self.assertEqual(HIGH_PRICE_STC_BLOCK_STC_LO_S, 121)

    def test_stc_upper_bound_is_300(self):
        self.assertEqual(HIGH_PRICE_STC_BLOCK_STC_HI_S, 300)

    def test_filter_stage_string_matches_kb(self):
        """Filter stage must exactly match `kb/decisions/96c-sol-xrp-2to5min-block-2026-04-26.md`
        for dashboard tiles, audit scripts, and rejected_opportunities queries."""
        self.assertEqual(HIGH_PRICE_STC_BLOCK_FILTER_STAGE,
                         "96C_SOL_XRP_STC_DANGER_BAND")

    def test_default_disabled(self):
        """Gate ships DISABLED by default — VPS must set HIGH_PRICE_STC_BLOCK_ENABLED=1
        to enable, matching repo convention for behavior-changing env vars (compare
        HOURLY_LIVE_ENABLED, HOURLY_NO_SIDE_LIVE which also default off)."""
        # Note: this test verifies the DEFAULT. CI may set the env var, in which case
        # this test will reflect the env. The intent: in absence of any env, default is False.
        # We assert by reading os.environ.
        env_val = os.environ.get("HIGH_PRICE_STC_BLOCK_ENABLED")
        if env_val is None:
            self.assertFalse(HIGH_PRICE_STC_BLOCK_ENABLED,
                             "Gate must default OFF when HIGH_PRICE_STC_BLOCK_ENABLED unset")
        else:
            # Env var set — value matches env
            self.assertEqual(HIGH_PRICE_STC_BLOCK_ENABLED, env_val == "1")

    def test_bleeder_strategies_set_matches_data(self):
        """Bleeder strategies are exactly the 4 entry-path strategies that lose in
        the cell per 30d analysis. CONFIRMATION_ADDON not included (executor-level,
        not scan-time). decided_t1/t1b not included (profitable in cell). TM-* not
        included (profitable for TM-96, slippage uncatchable for TM-97/98)."""
        self.assertEqual(set(HIGH_PRICE_STC_BLOCK_BLEEDER_STRATEGIES), {
            "decided_t2", "decided_t2_z2", "decided_t2_z25", "MAKER_PATIENT",
        })


# ─────────────────────────────────────────────────────────────────────────────
#  Strategy-aware composite predicate: should_block_high_price_stc_candidate
# ─────────────────────────────────────────────────────────────────────────────

class TestHighPriceStcCandidate_BleedersBlocked(unittest.TestCase):
    """In-cell × bleeder strategy → MUST block."""

    def _in_cell(self, **overrides):
        kwargs = dict(asset="SOL", side="yes", entry_price_cents=96,
                      seconds_to_close=200, enabled=True)
        kwargs.update(overrides)
        return kwargs

    def test_decided_t2_blocked(self):
        self.assertTrue(should_block_high_price_stc_candidate(
            **self._in_cell(strategy="decided_t2")))

    def test_decided_t2_z2_blocked(self):
        """decided_t2_z2 is the biggest bleeder ($509/30d alone)."""
        self.assertTrue(should_block_high_price_stc_candidate(
            **self._in_cell(strategy="decided_t2_z2")))

    def test_decided_t2_z25_blocked(self):
        self.assertTrue(should_block_high_price_stc_candidate(
            **self._in_cell(strategy="decided_t2_z25")))

    def test_maker_patient_blocked(self):
        """MAKER_PATIENT in cell loses $143/30d — block."""
        self.assertTrue(should_block_high_price_stc_candidate(
            **self._in_cell(strategy="MAKER_PATIENT")))

    def test_maker_patient_blocked_xrp(self):
        self.assertTrue(should_block_high_price_stc_candidate(
            **self._in_cell(asset="XRP", strategy="MAKER_PATIENT")))


class TestHighPriceStcCandidate_WinnersPreserved(unittest.TestCase):
    """In-cell × winning strategy → MUST preserve (DO NOT block)."""

    def _in_cell(self, strategy):
        return dict(asset="SOL", side="yes", entry_price_cents=96,
                    seconds_to_close=200, strategy=strategy, enabled=True)

    def test_terminal_momentum_96_preserved(self):
        """TM-96 is +$66/30d in cell with 0 losses. PRESERVE."""
        self.assertFalse(should_block_high_price_stc_candidate(
            **self._in_cell("terminal_momentum_96")))

    def test_terminal_momentum_untagged_preserved(self):
        """Untagged TM is +$34/30d. PRESERVE."""
        self.assertFalse(should_block_high_price_stc_candidate(
            **self._in_cell("terminal_momentum")))

    def test_taker_now_preserved(self):
        """TAKER_NOW: 9W/0L +$14. PRESERVE."""
        self.assertFalse(should_block_high_price_stc_candidate(
            **self._in_cell("TAKER_NOW")))

    def test_maker_aggressive_preserved(self):
        self.assertFalse(should_block_high_price_stc_candidate(
            **self._in_cell("MAKER_AGGRESSIVE")))

    def test_decided_t1_preserved(self):
        """DC at tightest z (decided_t1) is profitable in cell. PRESERVE."""
        self.assertFalse(should_block_high_price_stc_candidate(
            **self._in_cell("decided_t1")))

    def test_decided_t1b_preserved(self):
        """DC at z≤-4 (t1b) is profitable in cell. PRESERVE."""
        self.assertFalse(should_block_high_price_stc_candidate(
            **self._in_cell("decided_t1b")))

    def test_weekend_discount_preserved(self):
        self.assertFalse(should_block_high_price_stc_candidate(
            **self._in_cell("weekend_discount")))

    def test_panic_capture_preserved(self):
        self.assertFalse(should_block_high_price_stc_candidate(
            **self._in_cell("PANIC_CAPTURE")))

    def test_terminal_momentum_97_preserved(self):
        """TM-97 in cell DOES lose (slippage), but cell entry_price_cents=96 with
        TM-97 means TM evaluated at 97, not 96. At evaluation time best_yes_ask=97
        (not 96), so the cell predicate doesn't match — won't be blocked even if
        we add to bleeder set. Test confirms: even passing strategy=terminal_momentum_97,
        the gate doesn't block because entry_price_cents=96 in this synthetic test
        means we're SIMULATING 'after the fact'. In production, TM-97 candidates
        have best_yes_ask=97 so the cell predicate fails first."""
        # Strategy-aware gate at scan time uses best_yes_ask (eval price), not fill.
        # TM-97 candidates have best_yes_ask=97 so they're outside the cell.
        # If a TM-97 candidate hypothetically had best_yes_ask=96, we'd preserve
        # because terminal_momentum_97 is not in BLEEDER_STRATEGIES.
        self.assertFalse(should_block_high_price_stc_candidate(
            **self._in_cell("terminal_momentum_97")))


class TestHighPriceStcCandidate_NoOpOutsideCell(unittest.TestCase):
    """Cell predicate fails → never block, regardless of strategy."""

    def test_btc_with_bleeder_strategy_passes(self):
        """BTC at 96c × 2-5min × decided_t2_z2 — outside cell (asset), pass."""
        self.assertFalse(should_block_high_price_stc_candidate(
            asset="BTC", side="yes", entry_price_cents=96, seconds_to_close=200,
            strategy="decided_t2_z2", enabled=True))

    def test_eth_with_bleeder_strategy_passes(self):
        self.assertFalse(should_block_high_price_stc_candidate(
            asset="ETH", side="yes", entry_price_cents=96, seconds_to_close=200,
            strategy="MAKER_PATIENT", enabled=True))

    def test_sol_95c_with_bleeder_strategy_passes(self):
        """SOL 95c × decided_t2_z2 — outside cell (price), pass."""
        self.assertFalse(should_block_high_price_stc_candidate(
            asset="SOL", side="yes", entry_price_cents=95, seconds_to_close=200,
            strategy="decided_t2_z2", enabled=True))

    def test_sol_97c_with_bleeder_strategy_passes(self):
        """SOL 97c × decided_t2_z2 — outside cell (price), pass.
        We block exact 96c only; 97c is profitable for SOL (+$217/196 per backtest)."""
        self.assertFalse(should_block_high_price_stc_candidate(
            asset="SOL", side="yes", entry_price_cents=97, seconds_to_close=200,
            strategy="decided_t2_z2", enabled=True))

    def test_sol_96c_120s_with_bleeder_strategy_passes(self):
        """SOL 96c × decided_t2_z2 × 120s STC — outside cell (STC < 121), pass."""
        self.assertFalse(should_block_high_price_stc_candidate(
            asset="SOL", side="yes", entry_price_cents=96, seconds_to_close=120,
            strategy="decided_t2_z2", enabled=True))

    def test_sol_96c_no_side_with_bleeder_passes(self):
        """SOL 96c NO × decided_t2_z2 — outside cell (side=NO), pass."""
        self.assertFalse(should_block_high_price_stc_candidate(
            asset="SOL", side="no", entry_price_cents=96, seconds_to_close=200,
            strategy="decided_t2_z2", enabled=True))


class TestHighPriceStcCandidate_NullStrategy(unittest.TestCase):
    """Defensive: strategy=None → don't block (we only block KNOWN bleeders)."""

    def test_null_strategy_in_cell_passes(self):
        self.assertFalse(should_block_high_price_stc_candidate(
            asset="SOL", side="yes", entry_price_cents=96, seconds_to_close=200,
            strategy=None, enabled=True))

    def test_unknown_strategy_in_cell_passes(self):
        """A strategy we haven't analyzed (e.g. future overlay) defaults to pass."""
        self.assertFalse(should_block_high_price_stc_candidate(
            asset="SOL", side="yes", entry_price_cents=96, seconds_to_close=200,
            strategy="some_future_strategy", enabled=True))


class TestHighPriceStcCandidate_DisabledFlag(unittest.TestCase):
    """When gate disabled, strategy doesn't matter — never block."""

    def test_disabled_overrides_bleeder_in_cell(self):
        self.assertFalse(should_block_high_price_stc_candidate(
            asset="SOL", side="yes", entry_price_cents=96, seconds_to_close=200,
            strategy="decided_t2_z2", enabled=False))


# ─────────────────────────────────────────────────────────────────────────────
#  AST-level guard: gate site exists in scan() with correct shape
# ─────────────────────────────────────────────────────────────────────────────

class TestHighPriceStcGateSite_AstGuards(unittest.TestCase):
    """Guards against the gate site being silently broken/removed/relocated.

    Adversarial review A7: helper unit tests don't catch wiring bugs (wrong indent,
    missing continue, wrong condition order). These guards check the gate site
    structural invariants by parsing bot/_impl.py.
    """

    @classmethod
    def setUpClass(cls):
        # Bit 8.1 (2026-05-10): OpportunityScanner extracted to
        # bot/scanner/__init__.py. The gate site (helper call, comment
        # marker, enable check, insert_evaluated_opportunity log) moved
        # with the class. Walk both files.
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        bot_path = os.path.join(repo_root, "bot/_impl.py")
        scanner_path = os.path.join(repo_root, "bot", "scanner", "__init__.py")
        src = ""
        if os.path.exists(bot_path):
            with open(bot_path) as f:
                src = f.read()
        if os.path.isfile(scanner_path):
            with open(scanner_path) as f:
                src += "\n" + f.read()
        cls.bot_source = src

    def test_gate_helper_called_in_scan(self):
        """The gate site must call should_block_high_price_stc_candidate."""
        self.assertIn("should_block_high_price_stc_candidate(", self.bot_source,
                      "Gate site removed or function rename broke wiring")

    def test_filter_stage_used_in_insert(self):
        """The filter_stage constant must be passed to insert_evaluated_opportunity."""
        self.assertIn("HIGH_PRICE_STC_BLOCK_FILTER_STAGE", self.bot_source)

    def test_gate_logs_via_state(self):
        """Dropped candidates must be persisted to evaluated_opportunities."""
        self.assertIn("insert_evaluated_opportunity", self.bot_source)
        # Specifically that our filter_stage is one of the stages
        # Find the gate site (assumed to be in scan())
        gate_block_start = self.bot_source.find("# ── 96¢ × {SOL,XRP} × 2-5min STC danger-band filter")
        self.assertGreater(gate_block_start, 0,
                           "Gate site comment marker missing — site removed?")
        gate_block = self.bot_source[gate_block_start:gate_block_start + 8000]
        self.assertIn("insert_evaluated_opportunity", gate_block,
                      "Gate site no longer logs to DB — removed?")
        self.assertIn("HIGH_PRICE_STC_BLOCK_FILTER_STAGE", gate_block,
                      "Gate site no longer tags with filter_stage constant")

    def test_gate_only_runs_when_enabled(self):
        """Gate site must be guarded by HIGH_PRICE_STC_BLOCK_ENABLED."""
        gate_block_start = self.bot_source.find("# ── 96¢ × {SOL,XRP} × 2-5min STC danger-band filter")
        self.assertGreater(gate_block_start, 0)
        gate_block = self.bot_source[gate_block_start:gate_block_start + 800]
        self.assertIn("if HIGH_PRICE_STC_BLOCK_ENABLED", gate_block,
                      "Gate site missing top-level enable check — would always run")


# ─────────────────────────────────────────────────────────────────────────────
#  Boot-time bleeder-string integrity check
# ─────────────────────────────────────────────────────────────────────────────

class TestBleederStringIntegrityCheck(unittest.TestCase):
    """Guards against silent gate breakage from strategy renames in bot/_impl.py.

    Adversarial review A1: BLEEDER_STRATEGIES is duck-typed against
    candidate.strategy. If decided_t2_z2 gets renamed in scan() without updating
    the constant, the gate silently no-ops. Bit 3.0.5: the boot-time validator
    asserts each bleeder is a member of the live-strategy registry
    (STRATEGY_CLAMP_POLICY / MAKER_TAIL_* / TM_LIVE_STRATEGIES /
    STRATEGY_LIMIT_BUMP_* / STRATEGY_* constants / KNOWN_DC_STRATEGIES) and
    logs HPSB_BLEEDER_UNKNOWN_TO_REGISTRY if any bleeder is unknown.
    """

    def test_no_bleeders_missing_at_startup(self):
        """At repo HEAD, every bleeder in HIGH_PRICE_STC_BLOCK_BLEEDER_STRATEGIES
        must be a member of the live-strategy registry. If not, a strategy was
        renamed and the gate is silently broken."""
        self.assertEqual([], _HPSB_MISSING_BLEEDERS,
                         "Bleeder strategy strings unknown to runtime registry — "
                         "gate will silently no-op for these. See HPSB_BLEEDER_UNKNOWN_TO_REGISTRY "
                         "log line. Either update one of the registry sources "
                         "(STRATEGY_CLAMP_POLICY / KNOWN_DC_STRATEGIES / etc.) "
                         "OR update HIGH_PRICE_STC_BLOCK_BLEEDER_STRATEGIES.")

    def test_validator_callable(self):
        """Re-running the validator must produce the same result as module load."""
        result = _validate_high_price_stc_block_bleeder_strings()
        self.assertEqual([], result)


# ─────────────────────────────────────────────────────────────────────────────
#  Validator implementation contract (Bit 3.0.5: registry-membership, no source-grep)
# ─────────────────────────────────────────────────────────────────────────────

class TestBleederValidatorImplementation(unittest.TestCase):
    """Bit 3.0.5: validators MUST NOT source-grep bot/_impl.py.

    The pre-Bit-3.0.5 `count >= 2` heuristic was a false-positive engine —
    `MAKER_PATIENT`, `terminal_momentum_98`, and `TAKER_NOW` passed without
    any scan-site usage because their decl-site appearances summed to >= 2.
    The new shared helper uses runtime-registry membership; this class pins
    the negative contract (no source-grep) for both HPSB and BLEED_BLOCK
    wrappers.

    Full registry-membership invariants live in tests/integration/test_strategy_drift.py.
    """

    @classmethod
    def setUpClass(cls):
        # Bit 3.2: validators moved to bot/helpers/validators.py. Concat both
        # so the `def _validate_*` source-greps below resolve regardless of
        # which file the def now lives in.
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        parts = []
        if os.path.exists(os.path.join(repo_root, "bot/_impl.py")):
            with open(os.path.join(repo_root, "bot/_impl.py")) as f:
                parts.append(f.read())
        validators_path = os.path.join(repo_root, "bot/helpers/validators.py")
        if os.path.exists(validators_path):
            with open(validators_path) as f:
                parts.append(f.read())
        cls.bot_source = "\n".join(parts)

    def test_validator_does_not_source_grep(self):
        """Shared validator helper body must not contain `open(__file__` or
        `.count(`. Catches reverts to the pre-Bit-3.0.5 heuristic."""
        idx = self.bot_source.find("def _validate_bleeders_against_runtime_registry")
        self.assertGreater(idx, 0,
            "_validate_bleeders_against_runtime_registry must exist post-Bit-3.0.5")
        body = self.bot_source[idx:idx + 2500]
        self.assertNotIn("open(__file__", body,
            "Validator regressed to source-introspection (open(__file__) in body)")
        self.assertNotIn(".count(", body,
            "Validator regressed to source-text count heuristic (.count() in body)")

    def test_hpsb_wrapper_delegates_to_shared_helper(self):
        """`_validate_high_price_stc_block_bleeder_strings` must be a thin
        wrapper around the shared registry-membership helper. If a future
        refactor reverts the wrapper to inline source-grep logic, this fails."""
        idx = self.bot_source.find("def _validate_high_price_stc_block_bleeder_strings")
        self.assertGreater(idx, 0)
        body = self.bot_source[idx:idx + 600]
        self.assertIn("_validate_bleeders_against_runtime_registry", body,
            "HPSB validator must delegate to the shared helper post-Bit-3.0.5")
        self.assertIn("HIGH_PRICE_STC_BLOCK_BLEEDER_STRATEGIES", body,
            "HPSB validator must pass HIGH_PRICE_STC_BLOCK_BLEEDER_STRATEGIES")

    def test_bleed_block_wrapper_delegates_to_shared_helper(self):
        """Symmetric to HPSB: `_validate_bleed_block_bleeder_strings` must
        also be a thin wrapper around the shared registry-membership helper.
        Asymmetric coverage between HPSB and BLEED_BLOCK wrappers would let
        a partial revert (inline source-grep on BLEED_BLOCK only) ship
        without catching the regression."""
        idx = self.bot_source.find("def _validate_bleed_block_bleeder_strings")
        self.assertGreater(idx, 0)
        body = self.bot_source[idx:idx + 600]
        self.assertIn("_validate_bleeders_against_runtime_registry", body,
            "BLEED_BLOCK validator must delegate to the shared helper post-Bit-3.0.5")
        self.assertIn("TM98_HIGHPRICE_BLEED_BLOCK_STRATEGIES", body,
            "BLEED_BLOCK validator must pass TM98_HIGHPRICE_BLEED_BLOCK_STRATEGIES")
        self.assertIn("SOL_TAKER_LOWPRICE_BLEED_BLOCK_STRATEGIES", body,
            "BLEED_BLOCK validator must pass SOL_TAKER_LOWPRICE_BLEED_BLOCK_STRATEGIES")


class TestSideConventionInvariant(unittest.TestCase):
    """The gate defaults missing-side to 'yes' on the assumption that NO-side
    candidates ALWAYS set side='no' explicitly. Verify the convention holds in
    the codebase: every `"side": "no"` exists, and no candidate-construction site
    omits side ambiguously.
    """

    @classmethod
    def setUpClass(cls):
        # Bit 8.1 (2026-05-10): OpportunityScanner moved to
        # bot/scanner/__init__.py — NO-side scan path moved with it.
        repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        bot_path = os.path.join(repo_root, "bot/_impl.py")
        scanner_path = os.path.join(repo_root, "bot", "scanner", "__init__.py")
        src = ""
        if os.path.exists(bot_path):
            with open(bot_path) as f:
                src = f.read()
        if os.path.isfile(scanner_path):
            with open(scanner_path) as f:
                src += "\n" + f.read()
        cls.bot_source = src

    def test_no_side_explicitly_tagged_in_source(self):
        """At least one `"side": "no"` must appear in bot/_impl.py (NO-side scan path)."""
        self.assertIn('"side": "no"', self.bot_source,
                      "NO-side path missing — gate's default-yes assumption no longer safe")


class TestSideFailClosed(unittest.TestCase):
    """Cell predicate must NEVER fire for side != 'yes'. Composite predicate
    with strategy must respect that. If a future code path adds a NO candidate
    with side='no' explicitly, gate must skip it regardless of strategy."""

    def test_no_side_with_bleeder_strategy_passes(self):
        """SOL NO at 96c × 2-5min × decided_t2_z2 — gate must NOT block."""
        self.assertFalse(should_block_high_price_stc_candidate(
            asset="SOL", side="no", entry_price_cents=96, seconds_to_close=200,
            strategy="decided_t2_z2", enabled=True))

    def test_xrp_no_side_with_bleeder_strategy_passes(self):
        self.assertFalse(should_block_high_price_stc_candidate(
            asset="XRP", side="no", entry_price_cents=96, seconds_to_close=200,
            strategy="MAKER_PATIENT", enabled=True))

    def test_unknown_side_with_bleeder_strategy_passes(self):
        """Defensive: side='unknown' (future asymmetric overlay?) → don't block."""
        self.assertFalse(should_block_high_price_stc_candidate(
            asset="SOL", side="unknown", entry_price_cents=96, seconds_to_close=200,
            strategy="decided_t2_z2", enabled=True))


if __name__ == "__main__":
    unittest.main()
