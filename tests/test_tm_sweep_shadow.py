"""Tests for tm_sweep_shadow — counterfactual sweep PnL instrumentation.

Question this shadow answers: when TM partial-fills at 96/98/99c (single-price
IOC), would a sequential sweep into the next eligible TM tier have been
profitable? Captures pre/post-fill orderbook depths at all four TM-relevant
price levels (96/97/98/99) per execution; on settlement, computes a
counterfactual sweep PnL skipping 97c by design (97 is in TM_NEGATIVE_EV_TIERS;
sweeping past it is allowed, taking it is not).

Captured fields, trigger conditions, settlement flow, and counterfactual
formula are documented in this test file's class docstrings.
"""

import json
import os
import re
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BOT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot.py")


def _read_bot():
    with open(BOT_PATH) as f:
        return f.read()


# ════════════════════════════════════════════════════════════════════════
# Pure helper tests — depth extraction
# ════════════════════════════════════════════════════════════════════════

class TestDepthsAtTiers(unittest.TestCase):
    """tm_sweep_extract_depths(yes_asks, tiers) → {tier: qty}, 0 if missing."""

    def setUp(self):
        from bot import tm_sweep_extract_depths
        self.fn = tm_sweep_extract_depths

    def test_all_four_tiers_present(self):
        yes_asks = [[96, 2], [97, 5], [98, 10], [99, 20]]
        out = self.fn(yes_asks, tiers=(96, 97, 98, 99))
        self.assertEqual(out, {96: 2, 97: 5, 98: 10, 99: 20})

    def test_missing_tier_returns_zero_not_none(self):
        yes_asks = [[96, 2], [99, 20]]  # 97 and 98 absent
        out = self.fn(yes_asks, tiers=(96, 97, 98, 99))
        self.assertEqual(out, {96: 2, 97: 0, 98: 0, 99: 20})

    def test_empty_list_all_zeros(self):
        out = self.fn([], tiers=(96, 97, 98, 99))
        self.assertEqual(out, {96: 0, 97: 0, 98: 0, 99: 0})

    def test_extra_tiers_outside_request_ignored(self):
        yes_asks = [[50, 100], [96, 2], [99, 20]]  # 50c not requested
        out = self.fn(yes_asks, tiers=(96, 97, 98, 99))
        self.assertEqual(out, {96: 2, 97: 0, 98: 0, 99: 20})

    def test_duplicate_price_levels_summed(self):
        # If yes_asks has two entries at same price (shouldn't happen post
        # _extract_book_levels merge, but defend against bad inputs)
        yes_asks = [[98, 5], [98, 7]]
        out = self.fn(yes_asks, tiers=(98,))
        self.assertEqual(out, {98: 12})

    def test_none_input_returns_all_zeros(self):
        out = self.fn(None, tiers=(96, 97, 98, 99))
        self.assertEqual(out, {96: 0, 97: 0, 98: 0, 99: 0})


# ════════════════════════════════════════════════════════════════════════
# Pure helper tests — counterfactual PnL
# ════════════════════════════════════════════════════════════════════════

class TestCounterfactualPnL(unittest.TestCase):
    """tm_sweep_counterfactual_pnl(unfilled, entry_tier, depths, market_result)
    → (total_pnl_cents, breakdown_legs).

    Sweep model: sequential IOC at each eligible tier in (98, 99). Skips 97
    (TM_NEGATIVE_EV_TIERS). Skips tiers <= entry_tier (don't look down).
    Each leg fills min(remaining_unfilled, depth_at_tier).

    PnL per leg:
      win  → take * (100 - tier) - taker_fee(take, tier)
      loss → -(take * tier + taker_fee(take, tier))

    Breakdown leg shape: {"tier": int, "ct": int, "payoff": int}.
    """

    def setUp(self):
        from bot import tm_sweep_counterfactual_pnl
        self.fn = tm_sweep_counterfactual_pnl

    def test_full_fill_no_unfilled_zero_pnl_empty_legs(self):
        pnl, legs = self.fn(
            unfilled=0, entry_tier=96,
            depths={96: 2, 97: 5, 98: 10, 99: 20},
            market_result="yes")
        self.assertEqual(pnl, 0)
        self.assertEqual(legs, [])

    def test_partial_fill_win_sweeps_98_then_99(self):
        # 50ct requested, 2 filled at 96, 48 unfilled.
        # Depth: 5@98, 10@99 → take 5@98, 10@99, 33 still missing (no further tier).
        from bot import calculate_taker_fee
        pnl, legs = self.fn(
            unfilled=48, entry_tier=96,
            depths={96: 0, 97: 7, 98: 5, 99: 10},
            market_result="yes")
        # Leg 98c: 5ct × (100-98) - fee = 10 - calculate_taker_fee(5, 98)
        # Leg 99c: 10ct × (100-99) - fee = 10 - calculate_taker_fee(10, 99)
        f98 = calculate_taker_fee(5, 98)
        f99 = calculate_taker_fee(10, 99)
        expected = (5 * 2 - f98) + (10 * 1 - f99)
        self.assertEqual(pnl, expected)
        self.assertEqual(legs, [
            {"tier": 98, "ct": 5, "payoff": 5 * 2 - f98},
            {"tier": 99, "ct": 10, "payoff": 10 * 1 - f99},
        ])

    def test_partial_fill_loss_sweeps_98_then_99(self):
        from bot import calculate_taker_fee
        pnl, legs = self.fn(
            unfilled=48, entry_tier=96,
            depths={96: 0, 97: 7, 98: 5, 99: 10},
            market_result="no")
        f98 = calculate_taker_fee(5, 98)
        f99 = calculate_taker_fee(10, 99)
        expected = -(5 * 98 + f98) + -(10 * 99 + f99)
        self.assertEqual(pnl, expected)
        self.assertEqual(legs, [
            {"tier": 98, "ct": 5, "payoff": -(5 * 98 + f98)},
            {"tier": 99, "ct": 10, "payoff": -(10 * 99 + f99)},
        ])

    def test_skip_97_even_with_depth(self):
        # 7ct at 97 should be IGNORED. cf_pnl computed only from 98/99.
        from bot import calculate_taker_fee
        pnl, legs = self.fn(
            unfilled=10, entry_tier=96,
            depths={96: 0, 97: 100, 98: 3, 99: 4},
            market_result="yes")
        # 97 is skipped. Take 3@98 + 4@99 = 7 contracts, 3 still unfilled.
        f98 = calculate_taker_fee(3, 98)
        f99 = calculate_taker_fee(4, 99)
        expected = (3 * 2 - f98) + (4 * 1 - f99)
        self.assertEqual(pnl, expected)
        self.assertEqual(set(leg["tier"] for leg in legs), {98, 99})
        self.assertNotIn(97, [leg["tier"] for leg in legs])

    def test_entry_at_98_only_sweeps_99(self):
        # Entry was at 98c. Don't look DOWN at 96/97. Only 99 is eligible.
        from bot import calculate_taker_fee
        pnl, legs = self.fn(
            unfilled=10, entry_tier=98,
            depths={96: 100, 97: 100, 98: 0, 99: 5},
            market_result="yes")
        f99 = calculate_taker_fee(5, 99)
        self.assertEqual(pnl, 5 * 1 - f99)
        self.assertEqual(legs, [{"tier": 99, "ct": 5, "payoff": 5 * 1 - f99}])

    def test_entry_at_99_no_sweep_possible(self):
        # Already at top tier. Nothing higher to sweep into. Empty.
        pnl, legs = self.fn(
            unfilled=10, entry_tier=99,
            depths={96: 100, 97: 100, 98: 100, 99: 0},
            market_result="yes")
        self.assertEqual(pnl, 0)
        self.assertEqual(legs, [])

    def test_unfilled_exceeds_total_sweep_depth(self):
        # 100 unfilled, only 5+5=10 available. Sweep takes all 10. Remaining 90 lost.
        from bot import calculate_taker_fee
        pnl, legs = self.fn(
            unfilled=100, entry_tier=96,
            depths={96: 0, 97: 0, 98: 5, 99: 5},
            market_result="yes")
        f98 = calculate_taker_fee(5, 98)
        f99 = calculate_taker_fee(5, 99)
        self.assertEqual(pnl, (5 * 2 - f98) + (5 * 1 - f99))
        self.assertEqual([leg["ct"] for leg in legs], [5, 5])

    def test_unfilled_partially_consumed_by_98_only(self):
        # Only 3 unfilled, 5 at 98 → all 3 fill at 98, no 99 leg.
        from bot import calculate_taker_fee
        pnl, legs = self.fn(
            unfilled=3, entry_tier=96,
            depths={96: 0, 97: 0, 98: 5, 99: 5},
            market_result="yes")
        f98 = calculate_taker_fee(3, 98)
        self.assertEqual(pnl, 3 * 2 - f98)
        self.assertEqual(legs, [{"tier": 98, "ct": 3, "payoff": 3 * 2 - f98}])

    def test_zero_depth_at_eligible_tiers_no_legs(self):
        pnl, legs = self.fn(
            unfilled=50, entry_tier=96,
            depths={96: 0, 97: 0, 98: 0, 99: 0},
            market_result="yes")
        self.assertEqual(pnl, 0)
        self.assertEqual(legs, [])

    def test_market_result_yes_alias_all_yes(self):
        # Settlement returns 'yes' or 'all_yes' for wins. Both should be treated as wins.
        pnl_yes, _ = self.fn(unfilled=10, entry_tier=96,
                             depths={98: 5, 99: 0}, market_result="yes")
        pnl_all_yes, _ = self.fn(unfilled=10, entry_tier=96,
                                 depths={98: 5, 99: 0}, market_result="all_yes")
        self.assertEqual(pnl_yes, pnl_all_yes)
        self.assertGreater(pnl_yes, 0)

    def test_market_result_no_alias_all_no(self):
        pnl_no, _ = self.fn(unfilled=10, entry_tier=96,
                            depths={98: 5, 99: 0}, market_result="no")
        pnl_all_no, _ = self.fn(unfilled=10, entry_tier=96,
                                depths={98: 5, 99: 0}, market_result="all_no")
        self.assertEqual(pnl_no, pnl_all_no)
        self.assertLess(pnl_no, 0)

    def test_unrecognized_result_returns_zero_pnl(self):
        # Defensive: if settlement somehow returns 'void' or an unknown string,
        # produce 0 pnl rather than crash or guess.
        pnl, legs = self.fn(unfilled=10, entry_tier=96,
                            depths={98: 5, 99: 0}, market_result="void")
        self.assertEqual(pnl, 0)
        self.assertEqual(legs, [])


# ════════════════════════════════════════════════════════════════════════
# DB schema tests
# ════════════════════════════════════════════════════════════════════════

class TestSchema(unittest.TestCase):
    """tm_sweep_shadow table created at StateManager init with required columns."""

    def setUp(self):
        from bot import StateManager
        self.tmp_db = "/tmp/test_tm_sweep_shadow_schema.db"
        if os.path.exists(self.tmp_db):
            os.unlink(self.tmp_db)
        self.state = StateManager(db_path=self.tmp_db)

    def tearDown(self):
        self.state.conn.close()
        if os.path.exists(self.tmp_db):
            os.unlink(self.tmp_db)

    def test_table_exists(self):
        rows = self.state.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='tm_sweep_shadow'"
        ).fetchall()
        self.assertEqual(len(rows), 1, "tm_sweep_shadow table must be created")

    def test_required_columns(self):
        cols = {r["name"] for r in self.state.conn.execute(
            "PRAGMA table_info(tm_sweep_shadow)").fetchall()}
        required = {
            "id", "ticker", "event_ticker", "asset",
            "entry_time", "entry_price_cents",
            "requested_count", "filled_count", "unfilled_count",
            "depth_at_entry_pre_fill",
            "depth_96c_pre", "depth_97c_pre", "depth_98c_pre", "depth_99c_pre",
            "depth_96c_post", "depth_97c_post", "depth_98c_post", "depth_99c_post",
            "seconds_to_close", "calibrated_prob", "buf_pct",
            "best_ask_source",
            "status", "market_result", "settled_at",
            "cf_pnl_cents", "cf_breakdown_json",
        }
        missing = required - cols
        self.assertEqual(missing, set(), f"Missing columns: {missing}")

    def test_indexes_created(self):
        idx = {r["name"] for r in self.state.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='tm_sweep_shadow'"
        ).fetchall()}
        self.assertIn("idx_tmss_ticker", idx)
        self.assertIn("idx_tmss_status", idx)


# ════════════════════════════════════════════════════════════════════════
# DB insert + settlement update tests
# ════════════════════════════════════════════════════════════════════════

class TestInsertAndSettlement(unittest.TestCase):
    """End-to-end: insert helper writes a row; settlement update sets cf_pnl."""

    def setUp(self):
        from bot import StateManager
        self.tmp_db = "/tmp/test_tm_sweep_shadow_insert.db"
        if os.path.exists(self.tmp_db):
            os.unlink(self.tmp_db)
        self.state = StateManager(db_path=self.tmp_db)

    def tearDown(self):
        self.state.conn.close()
        if os.path.exists(self.tmp_db):
            os.unlink(self.tmp_db)

    def _insert_row(self, **overrides):
        """Helper: insert a row with sensible defaults and apply overrides."""
        row = {
            "ticker": "KXXRP15M-26APR261015-15",
            "event_ticker": "KXXRP15M-26APR261015",
            "asset": "XRP",
            "entry_time": "2026-04-26T14:11:51Z",
            "entry_price_cents": 96,
            "requested_count": 50,
            "filled_count": 2,
            "unfilled_count": 48,
            "depth_at_entry_pre_fill": 2,
            "depth_96c_pre": 2, "depth_97c_pre": 7,
            "depth_98c_pre": 5, "depth_99c_pre": 10,
            "depth_96c_post": 0, "depth_97c_post": 7,
            "depth_98c_post": 5, "depth_99c_post": 10,
            "seconds_to_close": 189.0,
            "calibrated_prob": 0.9328,
            "buf_pct": 0.077,
            "best_ask_source": "orderbook",
        }
        row.update(overrides)
        self.state.insert_tm_sweep_shadow_row(**row)

    def test_insert_partial_fill_row(self):
        self._insert_row()
        rows = self.state.conn.execute(
            "SELECT * FROM tm_sweep_shadow WHERE ticker=?",
            ("KXXRP15M-26APR261015-15",)).fetchall()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["asset"], "XRP")
        self.assertEqual(r["entry_price_cents"], 96)
        self.assertEqual(r["requested_count"], 50)
        self.assertEqual(r["filled_count"], 2)
        self.assertEqual(r["unfilled_count"], 48)
        self.assertEqual(r["status"], "open")
        self.assertIsNone(r["market_result"])
        self.assertIsNone(r["cf_pnl_cents"])

    def test_insert_full_fill_row(self):
        self._insert_row(filled_count=50, unfilled_count=0)
        r = self.state.conn.execute(
            "SELECT filled_count, unfilled_count, status FROM tm_sweep_shadow"
        ).fetchone()
        self.assertEqual(r["filled_count"], 50)
        self.assertEqual(r["unfilled_count"], 0)
        self.assertEqual(r["status"], "open")

    def test_insert_zero_fill_row(self):
        self._insert_row(filled_count=0, unfilled_count=50)
        r = self.state.conn.execute(
            "SELECT filled_count, unfilled_count FROM tm_sweep_shadow"
        ).fetchone()
        self.assertEqual(r["filled_count"], 0)
        self.assertEqual(r["unfilled_count"], 50)

    def test_settlement_update_win(self):
        from bot import calculate_taker_fee
        self._insert_row()  # 48 unfilled at 96c, 5@98 + 10@99 post
        self.state.update_tm_sweep_shadow_on_settlement(
            ticker="KXXRP15M-26APR261015-15", market_result="yes")
        r = self.state.conn.execute(
            "SELECT status, market_result, cf_pnl_cents, cf_breakdown_json, settled_at "
            "FROM tm_sweep_shadow"
        ).fetchone()
        self.assertEqual(r["status"], "settled")
        self.assertEqual(r["market_result"], "yes")
        f98 = calculate_taker_fee(5, 98)
        f99 = calculate_taker_fee(10, 99)
        expected = (5 * 2 - f98) + (10 * 1 - f99)
        self.assertEqual(r["cf_pnl_cents"], expected)
        self.assertIsNotNone(r["settled_at"])
        bd = json.loads(r["cf_breakdown_json"])
        self.assertEqual(len(bd), 2)
        self.assertEqual(bd[0]["tier"], 98)
        self.assertEqual(bd[1]["tier"], 99)

    def test_settlement_update_loss(self):
        self._insert_row()
        self.state.update_tm_sweep_shadow_on_settlement(
            ticker="KXXRP15M-26APR261015-15", market_result="no")
        r = self.state.conn.execute(
            "SELECT cf_pnl_cents, market_result FROM tm_sweep_shadow"
        ).fetchone()
        self.assertEqual(r["market_result"], "no")
        self.assertLess(r["cf_pnl_cents"], 0)

    def test_settlement_idempotent_second_call_is_no_op(self):
        # First call settles it. Second call must NOT corrupt state or
        # double-write (e.g. clobber settled_at to a new timestamp).
        self._insert_row()
        self.state.update_tm_sweep_shadow_on_settlement(
            ticker="KXXRP15M-26APR261015-15", market_result="yes")
        r1 = self.state.conn.execute(
            "SELECT cf_pnl_cents, settled_at FROM tm_sweep_shadow"
        ).fetchone()
        # Second call — should be no-op since status is already 'settled'.
        self.state.update_tm_sweep_shadow_on_settlement(
            ticker="KXXRP15M-26APR261015-15", market_result="yes")
        r2 = self.state.conn.execute(
            "SELECT cf_pnl_cents, settled_at FROM tm_sweep_shadow"
        ).fetchone()
        self.assertEqual(r1["cf_pnl_cents"], r2["cf_pnl_cents"])
        self.assertEqual(r1["settled_at"], r2["settled_at"])

    def test_settlement_full_fill_zero_cf_pnl(self):
        # Full fill → no unfilled remainder → cf_pnl is 0, breakdown is [].
        self._insert_row(filled_count=50, unfilled_count=0)
        self.state.update_tm_sweep_shadow_on_settlement(
            ticker="KXXRP15M-26APR261015-15", market_result="yes")
        r = self.state.conn.execute(
            "SELECT cf_pnl_cents, cf_breakdown_json FROM tm_sweep_shadow"
        ).fetchone()
        self.assertEqual(r["cf_pnl_cents"], 0)
        self.assertEqual(json.loads(r["cf_breakdown_json"]), [])

    def test_settlement_unrecognized_result_no_update(self):
        # 'void' / unknown result → row stays open, no PnL written.
        self._insert_row()
        self.state.update_tm_sweep_shadow_on_settlement(
            ticker="KXXRP15M-26APR261015-15", market_result="void")
        r = self.state.conn.execute(
            "SELECT status, cf_pnl_cents FROM tm_sweep_shadow"
        ).fetchone()
        self.assertEqual(r["status"], "open")
        self.assertIsNone(r["cf_pnl_cents"])

    def test_settlement_multiple_rows_same_ticker_all_updated(self):
        # Two TM entries on the same ticker (e.g. TM-96 then TM-98 stack).
        # Both rows should be settled by a single settlement event.
        self._insert_row(entry_price_cents=96, entry_time="2026-04-26T14:11:51Z")
        self._insert_row(entry_price_cents=98, entry_time="2026-04-26T14:13:00Z",
                         depth_96c_post=0, depth_98c_post=0, depth_99c_post=10)
        self.state.update_tm_sweep_shadow_on_settlement(
            ticker="KXXRP15M-26APR261015-15", market_result="yes")
        rows = self.state.conn.execute(
            "SELECT entry_price_cents, status FROM tm_sweep_shadow ORDER BY entry_time"
        ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["status"], "settled")
        self.assertEqual(rows[1]["status"], "settled")


# ════════════════════════════════════════════════════════════════════════
# Wiring tests — guard against drift in the hot path
# ════════════════════════════════════════════════════════════════════════

class TestAdversarialRegressions(unittest.TestCase):
    """Regressions for adversary-reviewer findings (2026-04-26)."""

    def test_C2_post_snapshot_stored_raw_no_subtraction(self):
        """Adversarial C2 (round 2): the original fix subtracted filled_count
        from depth_<entry>c_post to correct WS-lag bias. But the WS race goes
        BOTH directions — Kalshi sometimes pushes the delta before the IOC HTTP
        ack returns, in which case the cache is already decremented and our
        subtraction double-decrements. Without WS sequence tracking we can't
        tell which side of the race we're on, so the safer design is to store
        raw post-fill depth and document the racy-ness for the analyst.

        cf_pnl is unaffected: it uses depth at sweep tiers (98/99) not at the
        entry tier (96), and a single-price IOC at 96c cannot consume from
        higher tiers — so 98/99 depths are NOT polluted by our own fill."""
        with open(BOT_PATH) as f:
            source = f.read()
        start = source.find("def _execute_tm_taker")
        end = source.find("\n    def ", start + 10)
        body = source[start:end]
        # Must NOT contain the subtraction.
        self.assertNotRegex(
            body,
            r"_tmss_post\[.+\]\s*-=\s*_tmss_filled",
            "post-snapshot must NOT subtract own fill (adversary A1: "
            "WS race direction is unknown — over/under-corrects randomly)")
        self.assertNotRegex(
            body,
            r"_tmss_post\[.+\]\s*=\s*max\(\s*0\s*,\s*_tmss_post\[.+\]\s*-\s*_tmss_filled\s*\)",
            "post-snapshot must NOT subtract own fill (adversary A1)")

    def test_C4_filled_count_handles_None_zero_and_negative(self):
        """Adversarial C4 + A2: filled_count from _submit_taker may be None
        (error sentinel), 0 (zero-fill), or a negative sentinel from a future
        error path. The coercion must clamp all to a non-negative int.
        `or 0` alone catches None/0 but lets negatives through."""
        with open(BOT_PATH) as f:
            source = f.read()
        start = source.find("def _execute_tm_taker")
        end = source.find("\n    def ", start + 10)
        body = source[start:end]
        # Must clamp to non-negative explicitly (max(0, ...) or equivalent).
        self.assertRegex(
            body,
            r"_tmss_filled\s*=\s*max\(\s*0\s*,\s*int\(",
            "filled_count must clamp to non-negative (adversary A2 — "
            "negative sentinel like -1 would corrupt unfilled_count)")

    def test_C9_env_var_kill_switch_and_gate_present(self):
        """Adversarial C9 + A5: the env-var kill switch must exist AND the
        gate must actually wrap the capture call in _execute_tm_taker."""
        with open(BOT_PATH) as f:
            source = f.read()
        # The constant must be env-var-toggleable.
        self.assertRegex(
            source,
            r'TM_SWEEP_SHADOW_ENABLED\s*=\s*os\.environ\.get\(\s*["\']TM_SWEEP_SHADOW_ENABLED["\']',
            "TM_SWEEP_SHADOW_ENABLED must be env-var-toggleable")
        # The gate must wrap the insert call (so flipping the env var
        # actually disables the capture, not just the constant).
        start = source.find("def _execute_tm_taker")
        end = source.find("\n    def ", start + 10)
        body = source[start:end]
        # Find the insert call and verify it's inside an `if TM_SWEEP_SHADOW_ENABLED:` block.
        # Find the actual call site (skip imports/method-name fragments).
        insert_idx = body.find("self._state.insert_tm_sweep_shadow_row(")
        self.assertGreater(insert_idx, 0, "insert call site not found")
        # The 1200 chars before must contain the gate (allows for documenting
        # comments between gate and call without making the test brittle).
        prelude = body[max(0, insert_idx - 1200):insert_idx]
        self.assertIn("if TM_SWEEP_SHADOW_ENABLED:", prelude,
                      "insert call must live inside `if TM_SWEEP_SHADOW_ENABLED:` "
                      "(adversary A5 — gate must enforce, not just exist)")

    def test_C8_payoff_is_int_not_float(self):
        """Adversarial A4: verify that calculate_taker_fee returns int and
        therefore payoff arithmetic in tm_sweep_counterfactual_pnl is int.
        Belt-and-suspenders runtime check, not just static reasoning."""
        from bot import tm_sweep_counterfactual_pnl, calculate_taker_fee
        # Verify the upstream contract.
        self.assertIsInstance(calculate_taker_fee(50, 96), int,
                              "calculate_taker_fee must return int "
                              "(payoff math depends on it)")
        # Verify a representative cf computation produces ints.
        pnl, legs = tm_sweep_counterfactual_pnl(
            unfilled=48, entry_tier=96,
            depths={98: 5, 99: 10}, market_result="yes")
        self.assertIsInstance(pnl, int, "cf_pnl must be int")
        for leg in legs:
            self.assertIsInstance(leg["payoff"], int, f"leg payoff must be int: {leg}")


class TestWiring(unittest.TestCase):
    """Guard against the hookpoint silently disappearing or firing for the
    wrong strategies. These are AST/grep guards; the integration of the
    capture itself is verified via the schema + insert tests above."""

    def setUp(self):
        self.source = _read_bot()

    def test_enabled_flag_exists(self):
        self.assertIn("TM_SWEEP_SHADOW_ENABLED", self.source)

    def test_capture_called_in_execute_tm_taker(self):
        """The capture call must live inside _execute_tm_taker — not DC, not
        LPNE, not the main pipeline."""
        # Slice the function body. _execute_tm_taker is followed by another
        # `def _execute_` so we cut at the next def.
        start = self.source.find("def _execute_tm_taker")
        self.assertGreater(start, 0, "_execute_tm_taker not found")
        end = self.source.find("\n    def ", start + 10)
        body = self.source[start:end]
        self.assertIn("insert_tm_sweep_shadow_row", body,
                      "tm_sweep_shadow insert must be wired into _execute_tm_taker")

    def test_capture_NOT_in_dc_or_lpne_taker(self):
        for fn_name in ("_execute_dc_taker", "_execute_lpne_taker"):
            start = self.source.find(f"def {fn_name}")
            self.assertGreater(start, 0, f"{fn_name} not found")
            end = self.source.find("\n    def ", start + 10)
            body = self.source[start:end]
            self.assertNotIn("insert_tm_sweep_shadow_row", body,
                             f"{fn_name} must NOT call tm_sweep_shadow insert")

    def test_settlement_update_in_settlement_loop(self):
        """update_tm_sweep_shadow_on_settlement must be called from the same
        settlement-loop region that calls update for low_price_shadow_signals,
        so it benefits from the same ticker_results aggregation."""
        # The low_price settle block is a stable landmark.
        lps_idx = self.source.find("low_price_shadow_signals WHERE ticker=? AND status='open'")
        self.assertGreater(lps_idx, 0, "low_price_shadow settle block not found")
        # Look for the CALL form (with leading '.') — not the def — so we
        # match the settlement loop's invocation, not the method definition.
        tms_call_idx = self.source.find(".update_tm_sweep_shadow_on_settlement(")
        self.assertGreater(tms_call_idx, 0, "tm_sweep settle call site not found")
        self.assertLess(abs(tms_call_idx - lps_idx), 4000,
                        "tm_sweep settle call must live in the same settlement region as low_price_shadow")

    def test_skip_97_documented_in_constants(self):
        # The 97-skip is the LOAD-BEARING design choice. Must be documented
        # at the constants block where TM_SWEEP_TIERS is defined.
        # Find the constant block.
        m = re.search(r"TM_SWEEP_COUNTERFACTUAL_TIERS\s*=\s*[\(\[]([^\)\]]+)[\)\]]", self.source)
        self.assertIsNotNone(m, "TM_SWEEP_COUNTERFACTUAL_TIERS constant not found")
        tiers_text = m.group(1)
        self.assertNotIn("97", tiers_text,
                         "97 must NOT be in TM_SWEEP_COUNTERFACTUAL_TIERS (it's TM_NEGATIVE_EV)")
        self.assertIn("98", tiers_text)
        self.assertIn("99", tiers_text)


if __name__ == "__main__":
    unittest.main()
