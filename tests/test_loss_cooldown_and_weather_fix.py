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


BOT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot/_impl.py")


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

    def test_cooldown_uses_julianday_comparison(self):
        """The cooldown query MUST use julianday(), not string datetime comparison.

        Regression guard: settled_at is ISO-T format ('2026-04-11T...Z') and
        `datetime('now',...)` returns space-format ('2026-04-11 09:28'). Lex-
        comparing them treats ASCII 'T' (84) > space (32), so a naive
        `settled_at > datetime('now','-2 hours')` returns TRUE for any same-
        UTC-date loss — silently blocking the asset until UTC midnight.
        """
        with open(BOT_PATH) as f:
            src = f.read()
        self.assertIn("julianday(settled_at)", src,
                      "Cooldown query must use julianday(settled_at) for correct comparison")
        # Ensure the buggy pattern is NOT present in the cooldown query region
        # (find the query and check its body specifically)
        cooldown_marker = "SELECT DISTINCT asset FROM settled_trades"
        idx = src.index(cooldown_marker)
        query_region = src[idx:idx + 500]
        self.assertNotIn("datetime('now', '-' || ? || ' seconds')", query_region,
                         "Cooldown query must not use string datetime comparison (lex-bug)")

    def test_cooldown_gate_is_in_scan_loop(self):
        """The cooldown gate must exist in scan() and short-circuit with continue."""
        with open(BOT_PATH) as f:
            src = f.read()
        self.assertIn("asset in self._cooldown_assets", src)
        # Cooldown must only apply to 15M (not hourly/weather/spx)
        self.assertIn('_pt in (None, "15m")', src)


class TestWeatherNoCandidateInCorrectPath(unittest.TestCase):
    """Regression test for the weather NO candidate path.

    Bug history:
    1. Original: live NO candidate block was nested inside the shadow model-edge
       gate `if _wn_no_fee_edge > 0:`, which never fires because the model is
       structurally wrong on NO.
    2. First fix (wrong location): moved the candidate out of the shadow gate
       but kept it in the YES-side observation branch — also dead because
       weather YES evals fail insufficient_edge before reaching that branch.
    3. Final fix: moved to `_process_no_side_shadow()` where the 1050+ NO-side
       evals actually flow.

    Either regression should fail these tests.
    """

    def test_weather_no_candidate_lives_in_no_side_processor(self):
        """The live candidate gate must exist inside _process_no_side_shadow()."""
        with open(BOT_PATH) as f:
            src = f.read()
        # Find the function definition
        fn_marker = "def _process_no_side_shadow(self"
        self.assertIn(fn_marker, src)
        fn_idx = src.index(fn_marker)
        # Next method def marks end of this function
        next_def = src.find("\n    def ", fn_idx + 1)
        if next_def == -1:
            next_def = len(src)
        fn_body = src[fn_idx:next_def]

        # The weather NO live gate must exist in this function body
        self.assertIn("WEATHER_NO_SIDE_LIVE", fn_body,
                      "Weather NO live gate not found in _process_no_side_shadow")
        self.assertIn("WEATHER_NO_SIDE_MIN_STC", fn_body)
        self.assertIn("WEATHER_NO_MAX_PRICE", fn_body)
        self.assertIn("WEATHER_NO_ASSUMED_PROB", fn_body)
        self.assertIn('"weather_no_live"', fn_body,
                      "Strategy string 'weather_no_live' not found — candidate may not be appended")
        self.assertIn("candidates.append", fn_body,
                      "candidates.append not found in _process_no_side_shadow — "
                      "the live candidate is not being produced in the correct code path")

    def test_weather_no_candidate_not_in_yes_side_observation_gate(self):
        """The live candidate must NOT live inside the YES-side weather observation branch.

        That location is structurally dead for weather because YES evals fail
        insufficient_edge before reaching the observation branch (see bug #2).
        """
        with open(BOT_PATH) as f:
            tree = ast.parse(f.read())

        # Find any `if final_prob >= WEATHER_NO_SHADOW_MIN_YES_PROB ...` block
        # and assert it contains no weather_no_live candidate logic
        for node in ast.walk(tree):
            if isinstance(node, ast.If):
                test_src = ast.unparse(node.test)
                if "WEATHER_NO_SHADOW_MIN_YES_PROB" in test_src:
                    body_src = "\n".join(ast.unparse(s) for s in node.body)
                    self.assertNotIn(
                        "weather_no_live", body_src,
                        "REGRESSION: weather_no_live candidate is back in the YES-side "
                        "observation branch — this is structurally dead code. Move to "
                        "_process_no_side_shadow(). See kb/failures/weather-no-candidate-never-fires.md",
                    )

    def test_process_no_side_shadow_accepts_candidates_list(self):
        """The function signature must accept candidates list param for live append path."""
        import inspect
        sig = inspect.signature(bot.OpportunityScanner._process_no_side_shadow)
        self.assertIn("candidates", sig.parameters,
                      "_process_no_side_shadow must accept a 'candidates' parameter to support "
                      "the weather NO live path")

    def test_weather_no_assumed_prob_produces_positive_edge_at_40c(self):
        """WEATHER_NO_ASSUMED_PROB must be high enough that edge > 0 at 40c cap."""
        prob = bot.WEATHER_NO_ASSUMED_PROB
        max_price = bot.WEATHER_NO_MAX_PRICE
        fee_estimate = 0.02
        edge = prob - max_price / 100.0 - fee_estimate
        self.assertGreater(
            edge, 0,
            f"Assumed-prob edge must be positive at NO={max_price}c floor: "
            f"prob={prob} - price={max_price/100} - fee={fee_estimate} = {edge}",
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
