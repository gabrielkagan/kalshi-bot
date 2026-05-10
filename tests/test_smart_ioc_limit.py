"""Smart IOC limit picker — multi-level depth aware with EV ceiling.

Background (Apr 25 2026):
The Apr 23 WS schema fix (0ddcaf8) made the orderbook visible to scan,
which inadvertently TIGHTENED the IOC submit limit. Pre-fix the bot
fell back to NBBO yes_ask (often 1-3c above orderbook best_ask) and
swept multiple price levels; post-fix the bot uses orderbook
best_ask exactly and matches only top-of-book.

Live BTC market sample (Apr 25):
  best YES ask = 66c (qty=1)
  YES ask 67c = 1
  YES ask 69c = 151  ← deep level, just 3c above best
  YES ask 71c = 1
  fillable thru 99c: 154 contracts

The bot sees best=66c, submits IOC at limit=66c, fills 1 contract,
misses 153 contracts of liquidity sitting 3c away. This drove the
50% drop in 15M position size.

This file pins the new behavior: `_pick_ioc_limit_for_depth(...)`
walks the ladder from best_yes_ask upward, returns the smallest
limit that delivers ≥ target_qty cumulative depth. Hard caps:
  - max_bump_cents (operational ceiling, e.g., 3c)
  - edge_ceiling_price (don't pay so much that edge drops below
    MIN_EDGE_PCT — caller computes this from calibrated_prob)
  - max_price (MAX_ENTRY_PRICE = 99)

Algorithm: walk levels at YES prices in [best_yes_ask, cap], summing
qty. First level where cumul ≥ target_qty wins. If no level inside
the cap delivers target, return the highest level we reached (still
better than best_yes_ask alone) — Kalshi auto-cancels surplus at $0.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BOT_PY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot/executor.py")  # Bit 9.1 (2026-05-10): retargeted to bot/executor.py — OrderExecutor extracted from bot/_impl.py


def _ob(no_levels):
    """Build a minimal Kalshi-shape orderbook from a list of
    (no_price_cents, qty) tuples. Returns dict with 'no' key."""
    return {"no": [list(level) for level in no_levels], "yes": []}


class TestPickIocLimitBasics(unittest.TestCase):
    """Pure correctness on the depth-walking algorithm."""

    def setUp(self):
        import bot
        self.fn = bot.OrderExecutor._pick_ioc_limit_for_depth

    def test_empty_orderbook_returns_best_ask_unchanged(self):
        """No NO bids → no fillable depth at any level → return
        best_yes_ask unchanged. Caller will discover the empty
        book via PHANTOM_ABORT."""
        result = self.fn(
            _ob([]),
            best_yes_ask=66,
            target_qty=100,
            max_bump_cents=3,
            edge_ceiling_price=99,
        )
        self.assertEqual(result, 66)

    def test_target_hit_at_best_ask_returns_best(self):
        """Top-of-book has exactly target qty. No need to bump.
        OB: NO 34c × 100 = YES 66c × 100. target=100 → limit=66."""
        result = self.fn(
            _ob([(34, 100)]),
            best_yes_ask=66,
            target_qty=100,
            max_bump_cents=3,
            edge_ceiling_price=99,
        )
        self.assertEqual(result, 66)

    def test_target_hit_at_second_level(self):
        """Production case: 1 at 66c, 151 at 69c. target=100.
        Cumul at 66 = 1, cumul at 67 = 1, cumul at 69 = 152 ≥ 100.
        → limit=69."""
        ob = _ob([(34, 1), (33, 1), (31, 151)])  # YES 66, 67, 69
        result = self.fn(
            ob,
            best_yes_ask=66,
            target_qty=100,
            max_bump_cents=3,
            edge_ceiling_price=99,
        )
        self.assertEqual(
            result, 69,
            f"Expected limit=69 (covers 1+1+151=153 ≥ 100), got {result}")

    def test_target_hit_exactly_at_first_level(self):
        """Cumul at first level == target → return that level."""
        ob = _ob([(34, 50), (33, 100)])  # YES 66=50, 67=100
        # target=50, hit at 66c
        self.assertEqual(
            self.fn(ob, 66, 50, 3, 99), 66)
        # target=51, need to walk to 67c
        self.assertEqual(
            self.fn(ob, 66, 51, 3, 99), 67)

    def test_target_not_hit_within_max_bump_returns_highest_reachable(self):
        """All levels within max_bump have insufficient depth. Return
        the highest level we walked to — still better than best_ask
        alone, and Kalshi auto-cancels surplus at $0."""
        ob = _ob([(34, 1), (33, 2), (32, 3)])  # YES 66=1, 67=2, 68=3
        # target=1000, max_bump=3 → reach up to 68. cumul=6 < 1000.
        # Highest level we reached = 68.
        result = self.fn(
            ob,
            best_yes_ask=66,
            target_qty=1000,
            max_bump_cents=2,  # so cap = 66+2 = 68
            edge_ceiling_price=99,
        )
        self.assertEqual(
            result, 68,
            "When target not hit, return the highest level inside "
            "max_bump rather than best_ask. Larger limit = more "
            "potential fills (Kalshi auto-cancels surplus).")

    def test_max_bump_caps_walk(self):
        """Even if a deep level exists slightly above max_bump,
        don't reach for it."""
        ob = _ob([(34, 1), (33, 1), (29, 500)])  # YES 66=1, 67=1, 71=500
        # target=100, max_bump=3 → cap at 69. Levels in [66,69]: 66,67.
        # cumul = 2. Doesn't hit target. Return highest reached = 67.
        result = self.fn(
            ob,
            best_yes_ask=66,
            target_qty=100,
            max_bump_cents=3,
            edge_ceiling_price=99,
        )
        self.assertLessEqual(
            result, 69,
            "max_bump_cents must hard-cap the walk.")
        # And the deep 71c level is unreachable.
        self.assertNotEqual(
            result, 71,
            "Deep level above max_bump must NOT be selected.")

    def test_edge_ceiling_caps_walk(self):
        """edge_ceiling_price is the EV cap — paying more than that
        would reduce edge below the MIN_EDGE_PCT floor."""
        ob = _ob([(34, 1), (33, 1), (31, 500)])  # YES 66=1, 67=1, 69=500
        # max_bump=3 would allow walk to 69. But edge_ceiling=67
        # caps it. Result must not exceed 67.
        result = self.fn(
            ob,
            best_yes_ask=66,
            target_qty=100,
            max_bump_cents=3,
            edge_ceiling_price=67,  # tighter than max_bump
        )
        self.assertLessEqual(
            result, 67,
            f"edge_ceiling_price=67 must hard-cap the walk; got {result}.")

    def test_max_price_cap_enforced(self):
        """MAX_ENTRY_PRICE = 99 is the hard ceiling regardless of
        max_bump and edge_ceiling."""
        ob = _ob([(0, 500)])  # YES ask 100c, 500qty (boundary)
        result = self.fn(
            ob,
            best_yes_ask=98,
            target_qty=200,
            max_bump_cents=5,  # would allow up to 103
            edge_ceiling_price=110,  # also too permissive
            max_price=99,
        )
        self.assertLessEqual(
            result, 99,
            "max_price=99 must hard-cap; we never submit IOC > 99c.")

    def test_zero_target_returns_best_ask(self):
        """target_qty=0 is degenerate — no need to bump."""
        ob = _ob([(34, 100)])
        result = self.fn(
            ob, best_yes_ask=66, target_qty=0,
            max_bump_cents=3, edge_ceiling_price=99,
        )
        self.assertEqual(result, 66)

    def test_negative_qty_levels_skipped(self):
        """Defensive: malformed levels with negative qty must not
        contribute to cumul."""
        ob = _ob([(34, -50), (33, 100)])  # YES 66 has bad qty -50
        # target=100, real depth is at 67c (qty=100). Cumul at 66=0,
        # cumul at 67=100 → limit=67.
        result = self.fn(
            ob, 66, 100, 3, 99)
        self.assertEqual(
            result, 67,
            "Negative qty must be skipped, not counted as fillable.")

    def test_dollar_form_prices_normalized(self):
        """Some code paths use 0.34 (dollars) instead of 34 (cents)."""
        ob = {"no": [[0.34, 1], [0.31, 200]], "yes": []}
        # YES 66=1, YES 69=200
        result = self.fn(ob, 66, 100, 3, 99)
        self.assertEqual(result, 69)

    def test_dict_form_levels(self):
        """Some payloads use {price, quantity} dict form."""
        ob = {"no": [{"price": 34, "quantity": 1},
                     {"price": 31, "quantity": 200}],
              "yes": []}
        result = self.fn(ob, 66, 100, 3, 99)
        self.assertEqual(result, 69)

    def test_levels_below_best_ask_ignored(self):
        """A NO bid at price 50 = YES ask at 50c, which is BELOW
        our best_yes_ask=66. These are sub-floor asks. The picker
        should NOT include them in the walk (the bump should only
        go UPWARD from best_yes_ask).

        Note: Kalshi's IOC at limit ≥ 66 would still match these
        sub-floor asks at 50c — Variant B sub-floor sweep, which is
        intentionally re-enabled per the no_clamp policy. The picker
        just doesn't COUNT them toward the depth target because
        we want the LIMIT to control max-paid-price, not depth source.
        """
        ob = _ob([(50, 1000), (34, 1)])  # YES 50=1000, YES 66=1
        # target=10. Skip the 50c level (below best). At 66c we
        # only have 1. Don't hit target within max_bump=3. Return
        # the highest reachable inside the cap (66).
        result = self.fn(
            ob, best_yes_ask=66, target_qty=10,
            max_bump_cents=3, edge_ceiling_price=99,
        )
        self.assertGreaterEqual(
            result, 66,
            "Sub-floor levels (YES price < best_yes_ask) must not "
            "drag the limit DOWN.")
        self.assertLessEqual(
            result, 69,
            "Limit must stay within max_bump.")

    def test_best_ask_at_99_no_bump_possible(self):
        """If best_yes_ask is already at MAX_ENTRY_PRICE=99, no
        bump is possible. Return 99 unchanged."""
        ob = _ob([(1, 1000)])  # YES 99=1000
        result = self.fn(
            ob, best_yes_ask=99, target_qty=500,
            max_bump_cents=3, edge_ceiling_price=99,
            max_price=99,
        )
        self.assertEqual(result, 99)

    def test_max_bump_zero_returns_best_ask(self):
        """max_bump_cents=0 means 'no smart bump' — degenerates to
        the original behavior of submitting at best_ask."""
        ob = _ob([(34, 1), (31, 500)])
        result = self.fn(
            ob, 66, 100,
            max_bump_cents=0,
            edge_ceiling_price=99,
        )
        self.assertEqual(
            result, 66,
            "max_bump=0 must short-circuit to best_yes_ask.")

    def test_edge_ceiling_below_best_ask_returns_best_ask(self):
        """If edge_ceiling_price < best_yes_ask, the candidate
        shouldn't have been generated — but defensive: return
        best_yes_ask, never less."""
        ob = _ob([(34, 100)])
        result = self.fn(
            ob, 66, 100,
            max_bump_cents=3,
            edge_ceiling_price=64,  # below best_yes_ask
        )
        self.assertGreaterEqual(
            result, 66,
            "Picker must never return a price below best_yes_ask.")


class TestPickIocLimitProductionScenario(unittest.TestCase):
    """The exact live scenario observed Apr 25:
      best YES ask = 66c (qty=1)
      67c = 1
      69c = 151
      71c = 1
    Sizer wants ~100 contracts.
    Pre-fix limit = 66 → fill 1 (the production bug).
    With smart picker (max_bump=3, no edge cap) → limit = 69 →
    fill up to 153."""

    def test_production_btc_sample_unlocks_deep_level(self):
        import bot
        ob = _ob([(34, 1), (33, 1), (31, 151), (29, 1)])
        result = bot.OrderExecutor._pick_ioc_limit_for_depth(
            ob,
            best_yes_ask=66,
            target_qty=100,
            max_bump_cents=3,
            edge_ceiling_price=99,
        )
        self.assertEqual(
            result, 69,
            f"Live BTC scenario: must bump limit to 69 to unlock "
            f"the 151-contract level. Got {result}.")

    def test_production_btc_sample_with_tight_edge(self):
        """Same scenario, but candidate has thin edge — model_prob
        only 0.69, so edge_ceiling = 69-1(fee)-1(min_edge) = 67.
        Picker must NOT bump to 69 (would zero edge)."""
        import bot
        ob = _ob([(34, 1), (33, 1), (31, 151), (29, 1)])
        result = bot.OrderExecutor._pick_ioc_limit_for_depth(
            ob,
            best_yes_ask=66,
            target_qty=100,
            max_bump_cents=3,
            edge_ceiling_price=67,
        )
        self.assertLessEqual(
            result, 67,
            "Edge ceiling must prevent the bump from eroding "
            "all of the edge.")


class TestEdgeCeilingCalculation(unittest.TestCase):
    """The CALLER computes edge_ceiling_price as:
        floor(calibrated_prob * 100 - fee_1c - min_edge_cents).
    There's no separate helper for this yet — tests pin the
    expected math so the wiring in _submit_taker is correct."""

    def test_high_edge_yields_room_for_bump(self):
        """prob=0.95, best_ask=85, fee=1, min_edge=1 →
        ceiling = floor(95) - 1 - 1 = 93. Plenty of room above 85."""
        prob, fee_1c, min_edge_cents = 0.95, 1, 1
        ceiling = int(prob * 100) - fee_1c - min_edge_cents
        self.assertEqual(ceiling, 93)

    def test_thin_edge_yields_no_room(self):
        """prob=0.66, best_ask=64, fee=1, min_edge=1 →
        ceiling = 66-1-1 = 64. No room above best_ask."""
        prob, fee_1c, min_edge_cents = 0.66, 1, 1
        ceiling = int(prob * 100) - fee_1c - min_edge_cents
        self.assertEqual(ceiling, 64)


class TestStrategyReserveOverrides(unittest.TestCase):
    """Per-strategy edge reserve — STRATEGY_LIMIT_BUMP_RESERVE_CENTS.
    Default is B (reserve=0, break-even after fee). High-conviction
    strategies (DC tiers, addons) override to reserve=-1, allowing
    the picker to bump up to floor(prob*100) — i.e., the model's
    view of fair value before fees. Worst-case fill at that limit
    has edge = -1c (fee-cost on margin)."""

    def test_default_reserve_constant_defined(self):
        import bot
        self.assertTrue(
            hasattr(bot, "STRATEGY_LIMIT_BUMP_DEFAULT_RESERVE"),
            "Default reserve constant must be defined.")
        self.assertEqual(
            bot.STRATEGY_LIMIT_BUMP_DEFAULT_RESERVE, 0,
            "Default reserve must be 0 (B: break-even after fee). "
            "Going lower without per-strategy gating violates the "
            "math of IOC fill EV.")

    def test_strategy_reserve_overrides_dict_defined(self):
        import bot
        self.assertTrue(
            hasattr(bot, "STRATEGY_LIMIT_BUMP_RESERVE_CENTS"),
            "Per-strategy reserve override dict must be defined.")
        self.assertIsInstance(
            bot.STRATEGY_LIMIT_BUMP_RESERVE_CENTS, dict)

    def test_decided_contract_tiers_in_aggressive_bucket(self):
        """All four DC tiers should have reserve=-1 (aggressive).
        Round 3 [A1]: keys MUST match the short-form `strategy`
        field set on actual DC candidates (decided_t1/t1b/t2/t2_z25),
        NOT the long-form _dc_tier name (decided_contract_t1...).
        Long form was the bug — silently missed the lookup,
        rendering the override dead code."""
        import bot
        # Short-form strategy strings — these are what candidate.get('strategy')
        # actually returns (set in scan() via _dc_strat = {long: short}).
        expected = {
            "decided_t1",
            "decided_t1b",
            "decided_t2",
            "decided_t2_z25",
        }
        for strat in expected:
            self.assertEqual(
                bot.STRATEGY_LIMIT_BUMP_RESERVE_CENTS.get(strat), -1,
                f"DC tier {strat!r} (SHORT form, what candidates "
                f"actually carry) must have reserve=-1.")
        # And the LONG forms must NOT be present — they'd be dead
        # weight that misleads future readers.
        long_forms = {
            "decided_contract_t1",
            "decided_contract_t1b",
            "decided_contract_t2",
            "decided_contract_t2_z25",
        }
        for strat in long_forms:
            self.assertNotIn(
                strat, bot.STRATEGY_LIMIT_BUMP_RESERVE_CENTS,
                f"Long-form key {strat!r} must NOT be in the dict "
                f"— candidates carry the short form. R3 [A1] "
                f"regression: long-form keys silently miss lookup.")

    def test_decided_t2_z2_intentionally_excluded(self):
        """Round 4 [A1]: `decided_t2_z2` MUST NOT be in the
        aggressive bucket. T2-Z2 was shadowed Apr 1 2026 after
        -$313 on 47 trades — promoting it to aggressive bump
        would amplify the loss if re-enabled by env var. The
        comment in STRATEGY_LIMIT_BUMP_RESERVE_CENTS explains
        the intentional omission; this test pins it."""
        import bot
        self.assertNotIn(
            "decided_t2_z2", bot.STRATEGY_LIMIT_BUMP_RESERVE_CENTS,
            "decided_t2_z2 is INTENTIONALLY EXCLUDED — shadowed "
            "for being unprofitable. If re-validated and re-enabled "
            "in the future, decide separately whether to add an "
            "aggressive reserve. Don't include 'because the other "
            "DC tiers have it'.")

    def test_dc_candidate_strategy_strings_exist_in_codebase(self):
        """End-to-end pin: the strings in STRATEGY_LIMIT_BUMP_RESERVE_CENTS
        for DC tiers must be the actual values that scan() writes
        to candidate['strategy']. If a future refactor renames the
        DC strategy strings (e.g., decided_t1 → dc_t1), this test
        catches it."""
        with open(BOT_PY) as f:
            src = f.read()
        # The mapping in scan() must contain each key.
        for short_form in ("decided_t1", "decided_t1b",
                           "decided_t2", "decided_t2_z25"):
            self.assertIn(
                f'"{short_form}"', src,
                f"Short-form DC strategy string {short_form!r} not "
                f"found in bot/_impl.py — refactor risk: the dict's keys "
                f"won't match what candidate.get('strategy') returns.")

    def test_addons_in_aggressive_bucket(self):
        """CONFIRMATION_ADDON and DIP_ADDON extend already-trusted
        bets. Reserve=-1."""
        import bot
        for strat in ("CONFIRMATION_ADDON", "DIP_ADDON"):
            self.assertEqual(
                bot.STRATEGY_LIMIT_BUMP_RESERVE_CENTS.get(strat), -1,
                f"Addon {strat!r} must have reserve=-1.")

    def test_no_strategy_has_reserve_below_minus_fee(self):
        """Reserve below -1 (or below -fee_1c) means worst-fill edge
        is structurally negative beyond the fee. That's never +EV
        regardless of WR. Hard rule: no strategy goes below -1."""
        import bot
        for strat, reserve in bot.STRATEGY_LIMIT_BUMP_RESERVE_CENTS.items():
            self.assertGreaterEqual(
                reserve, -1,
                f"Strategy {strat!r} has reserve={reserve}. Below "
                f"-1 implies worst-fill edge < -fee, which is "
                f"structurally unprofitable. Reject.")

    def test_default_strategies_not_in_override_dict(self):
        """Strategies not specifically high-conviction must NOT be
        in the override dict — they get the default (0). Pinning
        this prevents accidental over-aggression."""
        import bot
        forbidden_in_overrides = {
            "terminal_momentum_96", "terminal_momentum_98",
            "terminal_momentum_99",
            "weekend_discount", "overnight_discount",
            "MAKER_AGGRESSIVE", "PANIC_CAPTURE", "TAKER_NOW",
            "low_price_near_expiry", "bracket_no",
        }
        for strat in forbidden_in_overrides:
            self.assertNotIn(
                strat, bot.STRATEGY_LIMIT_BUMP_RESERVE_CENTS,
                f"Strategy {strat!r} must not have a reserve "
                f"override — calibration-driven strategies and "
                f"generic execution paths stay on default (B).")


class TestPickerIntegrationWithReserve(unittest.TestCase):
    """End-to-end: the helper accepts a `reserve_cents` parameter
    via `edge_ceiling_price` (caller computes ceiling). The
    integration in `_submit_taker` looks up per-strategy reserve
    and feeds the right ceiling. Test the math, not the wiring
    (wiring is covered by AST regression)."""

    def test_default_reserve_yields_break_even_ceiling(self):
        """prob=0.85, fee=1, default reserve=0 →
        edge_ceiling = floor(85) - 1 - 0 = 84.
        At limit=84, edge = 0 (break-even). Picker stops at 84."""
        import bot
        ob = _ob([(34, 1), (33, 1), (31, 200)])  # YES 66, 67, 69
        # Caller-side computation:
        prob, fee_1c, reserve = 0.85, 1, 0
        edge_ceiling = int(prob * 100) - fee_1c - reserve
        self.assertEqual(edge_ceiling, 84)
        result = bot.OrderExecutor._pick_ioc_limit_for_depth(
            ob, best_yes_ask=66, target_qty=100,
            max_bump_cents=3, edge_ceiling_price=edge_ceiling,
        )
        # 69c is well below 84c ceiling AND inside max_bump=3.
        # Picker reaches 69c (target hit there).
        self.assertEqual(result, 69)

    def test_aggressive_reserve_unlocks_higher_ceiling(self):
        """prob=0.85, fee=1, aggressive reserve=-1 →
        edge_ceiling = 85 - 1 - (-1) = 85.
        Same book, target=100 → still hits at 69 (cumul 153 ≥ 100)."""
        import bot
        ob = _ob([(34, 1), (33, 1), (31, 200)])
        prob, fee_1c, reserve = 0.85, 1, -1
        edge_ceiling = int(prob * 100) - fee_1c - reserve
        self.assertEqual(edge_ceiling, 85)
        result = bot.OrderExecutor._pick_ioc_limit_for_depth(
            ob, best_yes_ask=66, target_qty=100,
            max_bump_cents=3, edge_ceiling_price=edge_ceiling,
        )
        self.assertEqual(result, 69)

    def test_thin_edge_default_blocks_bump_aggressive_allows(self):
        """Edge case where DEFAULT (reserve=0) doesn't allow bump
        but AGGRESSIVE (reserve=-1) does.

        prob=0.68, fee=1, best_yes_ask=66.
        Default ceiling = 68-1-0 = 67. Picker walks 66→67 only.
        Aggressive ceiling = 68-1-(-1) = 68. Picker walks 66→68.

        Book: 1@66, 1@67, 200@68. Target=100.
        Default: cumul at 66=1, at 67=2 → fall short, return 67.
        Aggressive: cumul at 66=1, at 67=2, at 68=202 → return 68."""
        import bot
        ob = _ob([(34, 1), (33, 1), (32, 200)])  # YES 66, 67, 68
        # Default
        result_default = bot.OrderExecutor._pick_ioc_limit_for_depth(
            ob, best_yes_ask=66, target_qty=100,
            max_bump_cents=5,
            edge_ceiling_price=int(0.68 * 100) - 1 - 0,
        )
        self.assertEqual(
            result_default, 67,
            "Default reserve must cap limit at edge=0 ceiling.")
        # Aggressive
        result_aggressive = bot.OrderExecutor._pick_ioc_limit_for_depth(
            ob, best_yes_ask=66, target_qty=100,
            max_bump_cents=5,
            edge_ceiling_price=int(0.68 * 100) - 1 - (-1),
        )
        self.assertEqual(
            result_aggressive, 68,
            "Aggressive reserve must allow bumping to edge=-1c ceiling.")


class TestSubmitTakerWiring(unittest.TestCase):
    """AST-level: `_submit_taker` must look up the per-strategy
    reserve from `STRATEGY_LIMIT_BUMP_RESERVE_CENTS` and pass the
    correct edge_ceiling_price to `_pick_ioc_limit_for_depth`."""

    def test_submit_taker_calls_picker(self):
        """The smart picker must be wired into _submit_taker.
        AST grep on the function body."""
        import ast
        with open(BOT_PY) as f:
            tree = ast.parse(f.read())
        target_fn = None
        for cls in ast.walk(tree):
            if (not isinstance(cls, ast.ClassDef)
                    or cls.name != "OrderExecutor"):
                continue
            for fn in cls.body:
                if (isinstance(fn, ast.FunctionDef)
                        and fn.name == "_submit_taker"):
                    target_fn = fn
                    break
        self.assertIsNotNone(target_fn, "_submit_taker not found")
        fn_src = ast.unparse(target_fn)
        self.assertIn(
            "_pick_ioc_limit_for_depth", fn_src,
            "_submit_taker must call _pick_ioc_limit_for_depth.")

    def test_submit_taker_references_per_strategy_reserve(self):
        """_submit_taker must look up STRATEGY_LIMIT_BUMP_RESERVE_CENTS
        (or the default constant) when computing edge_ceiling_price.
        Otherwise the per-strategy override is dead code."""
        with open(BOT_PY) as f:
            src = f.read()
        # Grep is fine here — the constant name is unique enough.
        self.assertIn(
            "STRATEGY_LIMIT_BUMP_RESERVE_CENTS", src,
            "STRATEGY_LIMIT_BUMP_RESERVE_CENTS must be referenced "
            "in bot/_impl.py — _submit_taker should look it up to get "
            "the per-strategy reserve.")
        # _submit_taker function must reference the constant.
        import ast
        tree = ast.parse(src)
        for cls in ast.walk(tree):
            if (not isinstance(cls, ast.ClassDef)
                    or cls.name != "OrderExecutor"):
                continue
            for fn in cls.body:
                if (isinstance(fn, ast.FunctionDef)
                        and fn.name == "_submit_taker"):
                    fn_src = ast.unparse(fn)
                    self.assertTrue(
                        "STRATEGY_LIMIT_BUMP_RESERVE_CENTS" in fn_src
                        or "STRATEGY_LIMIT_BUMP_DEFAULT_RESERVE" in fn_src,
                        "_submit_taker must reference the reserve "
                        "constant(s) — otherwise the per-strategy "
                        "override is dead code.")
                    return


class TestR1Fixes(unittest.TestCase):
    """Round 1 review surfaced 4 real issues. These tests pin the
    fixes."""

    def test_p0_1_dedicated_max_bump_constant(self):
        """R1 P0-1: max_bump_cents must be a dedicated constant
        (IOC_LIMIT_MAX_BUMP_CENTS), NOT the IOC_RETRY_OFFSET reused.
        IOC_RETRY_OFFSET=1c can't reach the production 69c level
        (3c above 66c best). Conflating the two couples unrelated
        behaviors."""
        import bot
        self.assertTrue(
            hasattr(bot, "IOC_LIMIT_MAX_BUMP_CENTS"),
            "Must define IOC_LIMIT_MAX_BUMP_CENTS as a dedicated "
            "constant separate from IOC_RETRY_OFFSET.")
        self.assertGreaterEqual(
            bot.IOC_LIMIT_MAX_BUMP_CENTS, 2,
            "max_bump must be ≥2c to reach typical level-2 depth. "
            "1c only reaches the next-level (often also thin).")
        # And: _submit_taker must reference IOC_LIMIT_MAX_BUMP_CENTS,
        # NOT IOC_RETRY_OFFSET, in the picker call.
        with open(BOT_PY) as f:
            tree = __import__("ast").parse(f.read())
        for cls in tree.body:
            import ast
            if (not isinstance(cls, ast.ClassDef)
                    or cls.name != "OrderExecutor"):
                continue
            for fn in cls.body:
                if (isinstance(fn, ast.FunctionDef)
                        and fn.name == "_submit_taker"):
                    fn_src = ast.unparse(fn)
                    self.assertIn(
                        "IOC_LIMIT_MAX_BUMP_CENTS", fn_src,
                        "_submit_taker must use "
                        "IOC_LIMIT_MAX_BUMP_CENTS for the picker's "
                        "max_bump_cents.")
                    return

    def test_p0_2_drift_flagged_ticker_bypassed(self):
        """R1 P0-2: if ticker is in scanner._ws_drift_cooldown
        (auto-flagged by WS_DRIFT_AUTO_FLAG), the picker must be
        bypassed — the WS cache it'd read is the same one that's
        been wrong. Source of phantom-bump risk."""
        with open(BOT_PY) as f:
            src = f.read()
        # _submit_taker must reference _ws_drift_cooldown to gate
        # the picker call.
        self.assertIn(
            "_ws_drift_cooldown", src,
            "_submit_taker must check scanner._ws_drift_cooldown "
            "to bypass the picker on drift-flagged tickers.")

    def test_p1_3_does_not_mutate_candidate_best_yes_ask(self):
        """R1 P1-3: the picker must NOT mutate
        candidate['best_yes_ask']. That value is used downstream
        for telemetry/audit/post-fill analysis. The bumped limit
        should be passed via a local var (e.g., _ioc_limit_price)
        only to the place_order call."""
        import ast
        with open(BOT_PY) as f:
            tree = ast.parse(f.read())
        for cls in tree.body:
            if (not isinstance(cls, ast.ClassDef)
                    or cls.name != "OrderExecutor"):
                continue
            for fn in cls.body:
                if (not isinstance(fn, ast.FunctionDef)
                        or fn.name != "_submit_taker"):
                    continue
                # Walk for any Assign / AugAssign that targets
                # candidate["best_yes_ask"] with a NEW value (the
                # picker output). Original assignment from candidate
                # to local `price` is fine; reassigning into the
                # candidate dict is NOT.
                for sub in ast.walk(fn):
                    if not isinstance(sub, ast.Assign):
                        continue
                    for tgt in sub.targets:
                        if not isinstance(tgt, ast.Subscript):
                            continue
                        # Match `candidate["best_yes_ask"] = ...`
                        if (isinstance(tgt.value, ast.Name)
                                and tgt.value.id == "candidate"):
                            sl = tgt.slice
                            sub_str = (
                                sl.value if isinstance(sl, ast.Constant)
                                else None)
                            if (isinstance(sub_str, str)
                                    and sub_str == "best_yes_ask"):
                                self.fail(
                                    f"_submit_taker mutates "
                                    f"candidate['best_yes_ask'] at "
                                    f"line {sub.lineno}. R1 P1-3 "
                                    f"regression: candidate state "
                                    f"must not change post-scan. "
                                    f"Use a local var for the "
                                    f"bumped IOC limit.")
                return

    def test_p1_5_phantom_abort_runs_after_smart_picker(self):
        """R1 P1-5 + R2 P1-C: PHANTOM_ABORT must run AFTER the
        smart picker's bump decision. Strengthened from text-find
        (which can be fooled by docstrings) to AST function-body
        line-number ordering: the `_pick_ioc_limit_for_depth` Call
        node must have a smaller lineno than the FIRST Call node
        whose first arg is a string starting with 'IOC_ABORT_PHANTOM'."""
        import ast
        with open(BOT_PY) as f:
            tree = ast.parse(f.read())
        target_fn = None
        for cls in ast.walk(tree):
            if (not isinstance(cls, ast.ClassDef)
                    or cls.name != "OrderExecutor"):
                continue
            for fn in cls.body:
                if (isinstance(fn, ast.FunctionDef)
                        and fn.name == "_submit_taker"):
                    target_fn = fn
                    break
        self.assertIsNotNone(target_fn, "_submit_taker not found")
        picker_lineno = None
        abort_lineno = None
        for sub in ast.walk(target_fn):
            if not isinstance(sub, ast.Call):
                continue
            # Picker call: `OrderExecutor._pick_ioc_limit_for_depth(...)`
            if (isinstance(sub.func, ast.Attribute)
                    and sub.func.attr == "_pick_ioc_limit_for_depth"
                    and picker_lineno is None):
                picker_lineno = sub.lineno
            # Abort call: any logging.warning whose first arg starts
            # with "IOC_ABORT_PHANTOM"
            if (isinstance(sub.func, ast.Attribute)
                    and sub.func.attr == "warning"
                    and sub.args
                    and isinstance(sub.args[0], ast.Constant)
                    and isinstance(sub.args[0].value, str)
                    and sub.args[0].value.startswith("IOC_ABORT_PHANTOM")
                    and abort_lineno is None):
                abort_lineno = sub.lineno
        self.assertIsNotNone(
            picker_lineno,
            "Picker call _pick_ioc_limit_for_depth not found in "
            "_submit_taker.")
        self.assertIsNotNone(
            abort_lineno,
            "PHANTOM_ABORT logging.warning not found in "
            "_submit_taker.")
        self.assertLess(
            picker_lineno, abort_lineno,
            f"Smart picker (line {picker_lineno}) must run BEFORE "
            f"PHANTOM_ABORT (line {abort_lineno}) so the abort "
            f"fires with the bumped limit in scope.")


class TestNoSideBypass(unittest.TestCase):
    """R2 [P0-A]: picker is YES-side only. NO-side candidates have
    `candidate["best_yes_ask"]` set to no_price (e.g., bracket_no
    NO entry at 36c). Walking the YES-ask ladder with a NO-price
    input is meaningless, and the bumped limit gets submitted as
    no_price — overpaying the NO entry. _submit_taker MUST bypass
    the picker for NO-side candidates."""

    def test_no_side_check_present_in_submit_taker(self):
        """AST: _submit_taker must check `candidate.get("side")` and
        bypass the picker on NO side. Without this the picker silently
        misbehaves on every NO-side IOC."""
        import ast
        with open(BOT_PY) as f:
            tree = ast.parse(f.read())
        target_fn = None
        for cls in ast.walk(tree):
            if (not isinstance(cls, ast.ClassDef)
                    or cls.name != "OrderExecutor"):
                continue
            for fn in cls.body:
                if (isinstance(fn, ast.FunctionDef)
                        and fn.name == "_submit_taker"):
                    target_fn = fn
                    break
        self.assertIsNotNone(target_fn)
        fn_src = ast.unparse(target_fn)
        # ast.unparse uses single quotes; check both forms.
        self.assertTrue(
            "_is_no_side" in fn_src,
            "_submit_taker must define `_is_no_side` to gate the "
            "picker for NO-side candidates. R2 [P0-A] regression.")
        self.assertTrue(
            "'no'" in fn_src or '"no"' in fn_src,
            "_submit_taker must reference the 'no' side string.")


class TestPickerWithMaxBump3(unittest.TestCase):
    """The default IOC_LIMIT_MAX_BUMP_CENTS=3 must reach the
    production 69c level (3c above 66c best)."""

    def test_max_bump_3_reaches_production_deep_level(self):
        import bot
        ob = _ob([(34, 1), (33, 1), (31, 151)])  # YES 66, 67, 69
        result = bot.OrderExecutor._pick_ioc_limit_for_depth(
            ob,
            best_yes_ask=66,
            target_qty=100,
            max_bump_cents=bot.IOC_LIMIT_MAX_BUMP_CENTS,
            edge_ceiling_price=99,
        )
        self.assertEqual(
            result, 69,
            f"With IOC_LIMIT_MAX_BUMP_CENTS={bot.IOC_LIMIT_MAX_BUMP_CENTS}, "
            f"picker must reach 69c (deep level). Got {result}.")


if __name__ == "__main__":
    unittest.main()
