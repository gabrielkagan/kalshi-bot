"""F/U TM_99 zero-fill investigation — IOC submit ladder diagnostic.

ETH TM_99 fill rate dropped from ~7/day (Apr 17-22) to 0/day starting
Apr 24, after the Apr 23 19:46 UTC WS schema fix exposed real prices.
Lifecycle snapshots show real `yes_asks_top=100c` while bot's
`_best_yes_ask_cents` derived 99c from cross-side `100 - no_bid_top`
(no_bid=1c). The bot bid 99c IOC, didn't cross the 100c yes_asks
ladder, 0 fills.

Two competing hypotheses for why pre-fix worked but post-fix doesn't:
  - HYP A: Kalshi's matching engine fills YES BUYs only against the
    explicit yes_asks ladder, NOT against the synthetic cross-side
    derivation. Pre-fix worked because of NBBO REST quirk.
  - HYP B: Kalshi matches both ladders, but no_bid at 1c is too thin
    (1ct) and gets sniped by other takers before our IOC arrives.

To distinguish, we add a diagnostic at IOC submit recording yes_ask_top
AND no_bid_top AND chosen bid. Pure observability — no behavior change.
After collecting data on next ETH TM_99 attempts, the divergence
pattern + thin-no-bid pattern will tell us which hypothesis to fix.

Tests cover the pure helper `OrderExecutor._compute_ladder_diag(ob)`
that extracts the diagnostic fields. The wiring at the IOC submit site
is covered by an AST audit + integration assertion.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import bot


class TestComputeLadderDiagHelper(unittest.TestCase):
    """Pure helper test — extracts yes_ask_top + no_bid_top from
    cached orderbook, computes cross-side derivation and divergence."""

    def test_empty_orderbook_returns_all_none(self):
        out = bot.executor.OrderExecutor._compute_ladder_diag(None)
        self.assertIsNone(out["yes_ask_top_price"])
        self.assertIsNone(out["no_bid_top_price"])
        self.assertIsNone(out["cross_side_ask"])
        self.assertFalse(out["diverges"])

    def test_eth_tm99_zero_fill_signature(self):
        """The exact signature observed in lifecycle snapshots for
        the failing ETH TM_99 IOCs: yes_asks_top=100 with real depth,
        no_bid_top=1 → cross-side derives 99. Ladders DIVERGE.
        Bot bid 99 (cross-side), didn't cross yes_asks=100, 0 fills."""
        ob = {
            "yes": [[100, 104]],
            "no": [[1, 50]],
        }
        out = bot.executor.OrderExecutor._compute_ladder_diag(ob)
        self.assertEqual(out["yes_ask_top_price"], 100)
        self.assertEqual(out["yes_ask_top_qty"], 104)
        self.assertEqual(out["no_bid_top_price"], 1)
        self.assertEqual(out["no_bid_top_qty"], 50)
        self.assertEqual(out["cross_side_ask"], 99)
        self.assertTrue(
            out["diverges"],
            "yes_asks_top=100 vs cross_side_ask=99 → diverges=True. "
            "This is the F/U TM_99 zero-fill signature.")

    def test_aligned_ladders_no_divergence(self):
        """Both ladders agree at 99c — typical healthy fill case
        (BTC TM_99 yes_asks=[[99,6077]], no_bid=1)."""
        ob = {"yes": [[99, 6077]], "no": [[1, 50]]}
        out = bot.executor.OrderExecutor._compute_ladder_diag(ob)
        self.assertEqual(out["yes_ask_top_price"], 99)
        self.assertEqual(out["cross_side_ask"], 99)
        self.assertFalse(out["diverges"])

    def test_yes_asks_only_no_bids_empty(self):
        """no_bids ladder empty — cross-side undefined.
        diverges should be False (can't diverge if one side missing)."""
        ob = {"yes": [[95, 10]], "no": []}
        out = bot.executor.OrderExecutor._compute_ladder_diag(ob)
        self.assertEqual(out["yes_ask_top_price"], 95)
        self.assertIsNone(out["no_bid_top_price"])
        self.assertIsNone(out["cross_side_ask"])
        self.assertFalse(out["diverges"])

    def test_no_bids_only_yes_asks_empty(self):
        ob = {"yes": [], "no": [[5, 10]]}
        out = bot.executor.OrderExecutor._compute_ladder_diag(ob)
        self.assertIsNone(out["yes_ask_top_price"])
        self.assertEqual(out["no_bid_top_price"], 5)
        self.assertEqual(out["cross_side_ask"], 95)
        self.assertFalse(out["diverges"])

    def test_multi_level_yes_picks_lowest_ask(self):
        """yes_asks_top is the LOWEST ask price across all levels
        (best price for buyer). Order in the input shouldn't matter."""
        ob = {
            "yes": [[99, 10], [95, 5], [97, 3]],
            "no": [],
        }
        out = bot.executor.OrderExecutor._compute_ladder_diag(ob)
        self.assertEqual(out["yes_ask_top_price"], 95)
        self.assertEqual(out["yes_ask_top_qty"], 5)

    def test_multi_level_no_picks_highest_bid(self):
        """no_bid_top is the HIGHEST no_bid price (best cross-side
        derivation). Order in the input shouldn't matter."""
        ob = {
            "yes": [],
            "no": [[1, 10], [3, 5], [2, 8]],
        }
        out = bot.executor.OrderExecutor._compute_ladder_diag(ob)
        self.assertEqual(out["no_bid_top_price"], 3)
        self.assertEqual(out["no_bid_top_qty"], 5)
        self.assertEqual(out["cross_side_ask"], 97)

    def test_floating_point_dollar_format(self):
        """Apr 23 Kalshi 2026 schema: prices arrive as floats
        (0.99 = 99c, 0.01 = 1c). Helper must convert to cents,
        matching `_best_yes_ask_cents` behavior."""
        ob = {"yes": [[0.99, 50]], "no": [[0.01, 100]]}
        out = bot.executor.OrderExecutor._compute_ladder_diag(ob)
        self.assertEqual(out["yes_ask_top_price"], 99)
        self.assertEqual(out["no_bid_top_price"], 1)
        self.assertEqual(out["cross_side_ask"], 99)
        self.assertFalse(out["diverges"])

    def test_malformed_entries_dont_crash(self):
        """Bad data shouldn't crash the diagnostic. Skip malformed
        entries, return whatever's parsable."""
        ob = {
            "yes": [["bad", "data"], [99, 50], None],
            "no": None,
        }
        out = bot.executor.OrderExecutor._compute_ladder_diag(ob)
        self.assertEqual(out["yes_ask_top_price"], 99)
        self.assertIsNone(out["no_bid_top_price"])

    def test_missing_keys_safe(self):
        """Cache might not always have both yes/no keys."""
        out = bot.executor.OrderExecutor._compute_ladder_diag({})
        self.assertIsNone(out["yes_ask_top_price"])
        self.assertIsNone(out["no_bid_top_price"])
        self.assertFalse(out["diverges"])

    def test_string_prices_handled(self):
        """R-review [A3]: schema drift defense. Kalshi has historically
        sent string-typed prices. Helper must coerce via float()."""
        ob = {"yes": [["0.99", 50]], "no": [["0.01", 100]]}
        out = bot.executor.OrderExecutor._compute_ladder_diag(ob)
        self.assertEqual(out["yes_ask_top_price"], 99)
        self.assertEqual(out["no_bid_top_price"], 1)

    def test_price_one_dot_zero_treated_as_cents(self):
        """R-review [A2]: float 1.0 is ambiguous (1c or 100c?).
        For Kalshi binary 0-100c, treat as cents. Document the choice."""
        ob = {"yes": [[1.0, 50]], "no": [[1.0, 100]]}
        out = bot.executor.OrderExecutor._compute_ladder_diag(ob)
        # 1.0 → 1c (we treat float in [1.0, ∞) as already cents)
        self.assertEqual(out["yes_ask_top_price"], 1)
        self.assertEqual(out["no_bid_top_price"], 1)

    def test_one_side_empty_flag(self):
        """R-review [A4]: tri-state signal. yes_asks empty + no_bids
        present → one_side_empty=True (uninformative for divergence
        check, distinct from real-alignment case)."""
        ob = {"yes": [], "no": [[5, 10]]}
        out = bot.executor.OrderExecutor._compute_ladder_diag(ob)
        self.assertTrue(out["one_side_empty"])
        self.assertFalse(out["diverges"])  # no comparison possible
        # Both sides present → not empty
        ob2 = {"yes": [[99, 10]], "no": [[1, 10]]}
        out2 = bot.executor.OrderExecutor._compute_ladder_diag(ob2)
        self.assertFalse(out2["one_side_empty"])
        # Both sides absent → not empty (it's symmetric absence)
        ob3 = {"yes": [], "no": []}
        out3 = bot.executor.OrderExecutor._compute_ladder_diag(ob3)
        self.assertFalse(out3["one_side_empty"])


class TestIOCSubmitDiagnosticWiring(unittest.TestCase):
    """AST guard: the IOC_SUBMIT_LADDER_DIAG log line must remain
    in `_submit_taker` so we don't accidentally drop the diagnostic
    in a future refactor. Distinguishes the call from the helper
    (the helper itself is testable above)."""

    def test_submit_taker_calls_compute_ladder_diag(self):
        """`_submit_taker` must reference `_compute_ladder_diag`
        (we use it for the IOC_SUBMIT_LADDER_DIAG log).

        Bit 9.1 (2026-05-10): retargeted to bot.executor.__file__ — OrderExecutor
        moved out of bot/_impl.py.
        """
        import ast
        import bot.executor
        with open(bot.executor.__file__) as f:
            tree = ast.parse(f.read())
        for cls in ast.walk(tree):
            if (isinstance(cls, ast.ClassDef)
                    and cls.name == "OrderExecutor"):
                for fn in cls.body:
                    if (isinstance(fn, ast.FunctionDef)
                            and fn.name == "_submit_taker"):
                        src = ast.unparse(fn)
                        self.assertIn(
                            "_compute_ladder_diag", src,
                            "_submit_taker must call "
                            "_compute_ladder_diag for the "
                            "IOC_SUBMIT_LADDER_DIAG log line.")
                        self.assertIn(
                            "IOC_SUBMIT_LADDER_DIAG", src,
                            "_submit_taker must emit "
                            "IOC_SUBMIT_LADDER_DIAG log line "
                            "(F/U TM_99 zero-fill diagnostic).")
                        # R-review [A1]/[A5]: must capture PRE and
                        # POST snapshots + fill_count for HYP A vs B
                        # discrimination. The log line includes "PRE:"
                        # and "POST:" prefixes.
                        self.assertIn(
                            "PRE:", src,
                            "Diagnostic must include PRE-IOC ladder "
                            "snapshot (R-review [A1]: required to "
                            "discriminate HYP A vs B).")
                        self.assertIn(
                            "POST:", src,
                            "Diagnostic must include POST-IOC ladder "
                            "snapshot (R-review [A1]: post-fill "
                            "no_bid qty change is the discriminator).")
                        self.assertIn(
                            "fill=", src,
                            "Diagnostic must include fill_count in "
                            "the same log line (R-review [A5]: "
                            "needed for single-grep correlation).")
                        return
        self.fail("OrderExecutor._submit_taker not found in bot/executor.py")


if __name__ == "__main__":
    unittest.main()
