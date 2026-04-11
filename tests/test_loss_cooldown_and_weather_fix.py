"""Regression tests for loss burst cooldown and weather NO-side candidate unnesting.

Guards against:
- Loss cooldown constant regressions or accidental disable
- Weather NO-side live candidate being re-nested inside the shadow model-edge
  gate (original bug that kept weather at 0 trades Apr 4-11 2026; see
  kb/failures/weather-no-candidate-never-fires.md)
- Cooldown SQL query shape regressions (must select DISTINCT asset from
  settled_trades with product_type='15m' and pnl_cents<0)
"""

import ast
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot  # noqa: E402


BOT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot.py")


class TestLossCooldownConstants(unittest.TestCase):
    """Guards the loss cooldown feature flag and tuning."""

    def test_loss_cooldown_enabled(self):
        """LOSS_COOLDOWN_ENABLED must be True by default (the feature is the fix)."""
        self.assertTrue(bot.LOSS_COOLDOWN_ENABLED)

    def test_loss_cooldown_seconds_is_2h(self):
        """LOSS_COOLDOWN_SECONDS must be 7200 (2 hours).

        Counterfactual validation used 2h lockout (+$441/30d). Tightening or
        loosening this needs fresh data analysis.
        """
        self.assertEqual(bot.LOSS_COOLDOWN_SECONDS, 7200)

    def test_cooldown_query_is_15m_losses_only(self):
        """The cooldown SQL query must filter to 15M losses only."""
        with open(BOT_PATH) as f:
            src = f.read()
        # Find the cooldown query (uniquely identified by DISTINCT asset + pnl_cents<0)
        self.assertIn("SELECT DISTINCT asset FROM settled_trades", src)
        self.assertIn("product_type='15m'", src)
        self.assertIn("pnl_cents < 0", src)

    def test_cooldown_gate_is_in_scan_loop(self):
        """The cooldown gate must exist in scan() and short-circuit with continue."""
        with open(BOT_PATH) as f:
            src = f.read()
        self.assertIn("asset in self._cooldown_assets", src)
        # Cooldown must only apply to 15M (not hourly/weather/spx)
        self.assertIn('_pt in (None, "15m")', src)


class TestWeatherNoCandidateUnnested(unittest.TestCase):
    """Regression test for the weather NO candidate bug.

    The original bug: the live NO candidate block was nested inside the shadow
    model-edge gate `if _wn_no_fee_edge > 0:`. The model is structurally wrong
    on NO (predicts 3-16%, actual 79.7% WR), so the shadow gate never fired,
    which meant the live candidate never fired either — 0 weather trades
    between Apr 4-11 2026.

    The fix: move the live candidate OUT of the shadow gate so it runs based
    on the ASSUMED 0.70 probability, not the broken model prob.
    """

    def test_weather_no_candidate_uses_assumed_edge(self):
        """The live candidate must gate on assumed edge, not model edge."""
        with open(BOT_PATH) as f:
            src = f.read()
        self.assertIn(
            "_wn_assumed_edge = WEATHER_NO_ASSUMED_PROB - _no_ask_eq / 100.0 - _wn_no_fee / 100.0",
            src,
        )

    def test_weather_no_candidate_not_nested_in_shadow_edge_gate(self):
        """The live candidate must NOT be inside `if _wn_no_fee_edge > 0:`.

        Parses bot.py with AST, finds the `if _wn_no_fee_edge > 0:` statement,
        and asserts it contains NO `candidates.append` calls transitively.
        If this ever fails, the nesting bug has regressed.
        """
        with open(BOT_PATH) as f:
            tree = ast.parse(f.read())

        def walk_if_nodes(node):
            for child in ast.walk(node):
                if isinstance(child, ast.If):
                    yield child

        found_shadow_gate = False
        for ifnode in walk_if_nodes(tree):
            # Match `if _wn_no_fee_edge > 0:` (with or without extra conditions)
            node_src = ast.unparse(ifnode.test)
            if "_wn_no_fee_edge > 0" in node_src and "_wn_dedup" not in node_src:
                # This is the pure shadow-edge gate — check body contains no candidates.append
                found_shadow_gate = True
                body_src = "\n".join(ast.unparse(s) for s in ifnode.body)
                self.assertNotIn(
                    "candidates.append", body_src,
                    "REGRESSION: weather NO candidate is nested inside `if _wn_no_fee_edge > 0:`. "
                    "This re-introduces the bug that kept weather at 0 trades Apr 4-11 2026. "
                    "See kb/failures/weather-no-candidate-never-fires.md",
                )
            elif "_wn_no_fee_edge > 0" in node_src and "_wn_dedup" in node_src:
                # New combined gate `_wn_dedup not in seen and _wn_no_fee_edge > 0` — body is shadow only
                found_shadow_gate = True
                body_src = "\n".join(ast.unparse(s) for s in ifnode.body)
                self.assertNotIn(
                    "candidates.append", body_src,
                    "REGRESSION: weather NO candidate is nested inside the shadow-dedup gate. "
                    "See kb/failures/weather-no-candidate-never-fires.md",
                )
        self.assertTrue(
            found_shadow_gate,
            "Could not find the `_wn_no_fee_edge > 0` gate in bot.py — was it removed?",
        )

    def test_weather_no_assumed_prob_produces_positive_edge_at_40c(self):
        """WEATHER_NO_ASSUMED_PROB must be high enough that edge > 0 at 40c cap.

        Edge = prob - price/100 - fee/100. At NO=40c with ~0.6c taker fee,
        edge = 0.70 - 0.40 - 0.006 = 0.294. Must stay positive.
        """
        prob = bot.WEATHER_NO_ASSUMED_PROB
        max_price = bot.WEATHER_NO_MAX_PRICE
        fee_estimate = 0.02  # conservative upper bound (2c on 40c)
        edge = prob - max_price / 100.0 - fee_estimate
        self.assertGreater(
            edge, 0,
            f"Assumed-prob edge must be positive at NO={max_price}c floor: "
            f"prob={prob} - price={max_price/100} - fee={fee_estimate} = {edge}",
        )


if __name__ == "__main__":
    unittest.main()
