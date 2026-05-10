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

BOT_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot/_impl.py")


def _read_bot():
    """Bit 3.1+3.2 (+7.1): returns concat of bot/_impl.py + bot/constants.py +
    bot/helpers/*.py + bot/state.py source. Tests that look for CONSTANT = value
    definitions (post-Bit-3.1 in bot/constants.py), helper-function bodies
    (post-Bit-3.2 in bot/helpers/*.py), StateManager methods + tm_sweep_shadow
    DDL (post-Bit-7.1 in bot/state.py), or class / scan-site / log-string
    patterns (still in bot/_impl.py) all find their targets in the
    concatenated source.
    """
    parts = []
    with open(BOT_PATH) as f:
        parts.append(f.read())
    # Bit 8.1 (2026-05-10): append scanner source to parts too.
    _scanner_path = os.path.join(os.path.dirname(BOT_PATH), "scanner", "__init__.py")
    if os.path.isfile(_scanner_path):
        with open(_scanner_path) as _f:
            parts.append(_f.read())
    # Bit 9.1 (2026-05-10): OrderExecutor extracted to bot/executor.py.
    # Append its source so audits that grep for OrderExecutor content survive the move.
    _executor_path = os.path.join(os.path.dirname(BOT_PATH), "executor.py") if "BOT_PATH" in globals() else os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot", "executor.py")
    if os.path.isfile(_executor_path):
        with open(_executor_path) as _f:
            parts.append(_f.read())
    constants_path = os.path.join(os.path.dirname(BOT_PATH), "constants.py")
    if os.path.exists(constants_path):
        with open(constants_path) as f:
            parts.append(f.read())
    helpers_dir = os.path.join(os.path.dirname(BOT_PATH), "helpers")
    if os.path.isdir(helpers_dir):
        for fname in sorted(os.listdir(helpers_dir)):
            if fname.endswith(".py"):
                with open(os.path.join(helpers_dir, fname)) as f:
                    parts.append(f.read())
    state_path = os.path.join(os.path.dirname(BOT_PATH), "state.py")
    if os.path.exists(state_path):
        with open(state_path) as f:
            parts.append(f.read())
    # Bit 8.1 (2026-05-10): scanner moved to bot/scanner/__init__.py.
    # Append its source so audits that grep for OpportunityScanner content survive the move.
    _scanner_path = os.path.join(os.path.dirname(BOT_PATH), "scanner", "__init__.py")
    if os.path.isfile(_scanner_path):
        with open(_scanner_path) as _f:
            parts.append(_f.read())
    return "\n".join(parts)


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
        `or 0` alone catches None/0 but lets negatives through.

        Bit 9.1 (2026-05-10): _execute_tm_taker is in OrderExecutor (now in
        bot/executor.py). Use _read_bot() which concats both files.
        """
        source = _read_bot()
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
        # Bit 3.1: TM_SWEEP_SHADOW_ENABLED definition lives in bot/constants.py
        # post-extraction; the gate (_execute_tm_taker body) stays in
        # bot/_impl.py. _read_bot() returns concat so both regexes match.
        source = _read_bot()
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
        prelude = body[max(0, insert_idx - 2400):insert_idx]
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


# ════════════════════════════════════════════════════════════════════════
# cf_pnl_cents_with_97 — production-realism column
# ════════════════════════════════════════════════════════════════════════

class TestCounterfactualWith97(unittest.TestCase):
    """Kalshi IOCs cannot skip 97c — a sweep with limit=98 fills 97 first.
    The original cf_pnl_cents column models an idealized "skip 97" sweep that
    isn't implementable. cf_pnl_cents_with_97 models what production would
    actually capture: includes 97 fills."""

    def setUp(self):
        from bot import tm_sweep_counterfactual_pnl
        self.fn = tm_sweep_counterfactual_pnl

    def test_with_97_sweeps_97_when_depth_present(self):
        from bot import calculate_taker_fee
        # Entry at 96, unfilled=48, depths 97c=10, 98c=5, 99c=20.
        # With sweep_tiers=(97,98,99): take 10@97, 5@98, 20@99 — total 35ct fills.
        pnl, legs = self.fn(
            unfilled=48, entry_tier=96,
            depths={97: 10, 98: 5, 99: 20},
            market_result="yes",
            sweep_tiers=(97, 98, 99))
        f97 = calculate_taker_fee(10, 97)
        f98 = calculate_taker_fee(5, 98)
        f99 = calculate_taker_fee(20, 99)
        expected = (10 * 3 - f97) + (5 * 2 - f98) + (20 * 1 - f99)
        self.assertEqual(pnl, expected)
        self.assertEqual([leg["tier"] for leg in legs], [97, 98, 99])

    def test_with_97_loss_path_97_amplifies_loss(self):
        # 97c loss = -97 per ct. 98c loss = -98 per ct. 99c loss = -99 per ct.
        # On a loss, sweeping 97 makes the loss WORSE, not better. Critical.
        from bot import calculate_taker_fee
        pnl_with, _ = self.fn(
            unfilled=10, entry_tier=96,
            depths={97: 5, 98: 0, 99: 0},
            market_result="no",
            sweep_tiers=(97, 98, 99))
        pnl_without, _ = self.fn(
            unfilled=10, entry_tier=96,
            depths={97: 5, 98: 0, 99: 0},
            market_result="no",
            sweep_tiers=(98, 99))
        # With 97: lost 5 contracts at 97c. Without 97: no fills, no loss.
        self.assertLess(pnl_with, pnl_without)
        # Specifically: f97 = calculate_taker_fee(5,97), pnl_with = -(5*97 + f97)
        f97 = calculate_taker_fee(5, 97)
        self.assertEqual(pnl_with, -(5 * 97 + f97))
        self.assertEqual(pnl_without, 0)

    def test_with_97_entry_98_does_not_sweep_97_or_98(self):
        # Entry at 98. sweep_tiers=(97,98,99) but tier <= entry filters those out.
        # Only 99 should fire.
        from bot import calculate_taker_fee
        pnl, legs = self.fn(
            unfilled=10, entry_tier=98,
            depths={97: 100, 98: 100, 99: 5},
            market_result="yes",
            sweep_tiers=(97, 98, 99))
        f99 = calculate_taker_fee(5, 99)
        self.assertEqual(pnl, 5 * 1 - f99)
        self.assertEqual([leg["tier"] for leg in legs], [99])


class TestSchemaWith97Column(unittest.TestCase):
    """cf_pnl_cents_with_97 must be present after StateManager init,
    on both fresh and pre-existing DBs (idempotent migration)."""

    def setUp(self):
        self.tmp_db = "/tmp/test_tm_sweep_with97_schema.db"
        if os.path.exists(self.tmp_db):
            os.unlink(self.tmp_db)

    def tearDown(self):
        if os.path.exists(self.tmp_db):
            os.unlink(self.tmp_db)

    def test_fresh_db_has_with97_column(self):
        from bot import StateManager
        state = StateManager(db_path=self.tmp_db)
        cols = {r["name"] for r in state.conn.execute(
            "PRAGMA table_info(tm_sweep_shadow)").fetchall()}
        self.assertIn("cf_pnl_cents_with_97", cols)
        state.conn.close()

    def test_alter_migration_idempotent(self):
        """If the table exists WITHOUT the column (legacy DB), StateManager
        init must add it. Re-init must not error."""
        from bot import StateManager
        # Create legacy table without the column.
        conn = sqlite3.connect(self.tmp_db)
        conn.execute("""
            CREATE TABLE tm_sweep_shadow (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                event_ticker TEXT NOT NULL,
                asset TEXT NOT NULL,
                entry_time TEXT NOT NULL,
                entry_price_cents INTEGER NOT NULL,
                requested_count INTEGER NOT NULL,
                filled_count INTEGER NOT NULL,
                unfilled_count INTEGER NOT NULL,
                depth_at_entry_pre_fill INTEGER,
                depth_96c_pre INTEGER, depth_97c_pre INTEGER,
                depth_98c_pre INTEGER, depth_99c_pre INTEGER,
                depth_96c_post INTEGER, depth_97c_post INTEGER,
                depth_98c_post INTEGER, depth_99c_post INTEGER,
                seconds_to_close REAL,
                calibrated_prob REAL,
                buf_pct REAL,
                best_ask_source TEXT,
                status TEXT NOT NULL DEFAULT 'open',
                market_result TEXT,
                settled_at TEXT,
                cf_pnl_cents INTEGER,
                cf_breakdown_json TEXT
            )
        """)
        conn.commit()
        conn.close()
        # First StateManager init: adds the column.
        state = StateManager(db_path=self.tmp_db)
        cols = {r["name"] for r in state.conn.execute(
            "PRAGMA table_info(tm_sweep_shadow)").fetchall()}
        self.assertIn("cf_pnl_cents_with_97", cols,
                      "ALTER migration must add cf_pnl_cents_with_97")
        state.conn.close()
        # Second init: must be idempotent (no error from re-adding the column).
        state2 = StateManager(db_path=self.tmp_db)
        state2.conn.close()


class TestSettlementWritesBothCfColumns(unittest.TestCase):
    """update_tm_sweep_shadow_on_settlement must compute and write BOTH
    cf_pnl_cents (sweep_tiers=98,99) and cf_pnl_cents_with_97 (sweep_tiers=97,98,99)."""

    def setUp(self):
        from bot import StateManager
        self.tmp_db = "/tmp/test_tm_sweep_with97_settle.db"
        if os.path.exists(self.tmp_db):
            os.unlink(self.tmp_db)
        self.state = StateManager(db_path=self.tmp_db)

    def tearDown(self):
        self.state.conn.close()
        if os.path.exists(self.tmp_db):
            os.unlink(self.tmp_db)

    def _insert(self, **kw):
        defaults = {
            "ticker": "KXXRP15M-X", "event_ticker": "EV", "asset": "XRP",
            "entry_time": "2026-04-27T00:00:00Z",
            "entry_price_cents": 96, "requested_count": 50,
            "filled_count": 2, "unfilled_count": 48,
            "depth_at_entry_pre_fill": 2,
            "depth_96c_pre": 2, "depth_97c_pre": 10,
            "depth_98c_pre": 5, "depth_99c_pre": 20,
            "depth_96c_post": 0, "depth_97c_post": 10,
            "depth_98c_post": 5, "depth_99c_post": 20,
            "seconds_to_close": 200.0, "calibrated_prob": 0.93,
            "buf_pct": 0.1, "best_ask_source": "orderbook",
        }
        defaults.update(kw)
        self.state.insert_tm_sweep_shadow_row(**defaults)

    def test_settlement_writes_both_columns_on_win(self):
        from bot import calculate_taker_fee
        self._insert()
        self.state.update_tm_sweep_shadow_on_settlement(
            ticker="KXXRP15M-X", market_result="yes")
        r = self.state.conn.execute(
            "SELECT cf_pnl_cents, cf_pnl_cents_with_97 FROM tm_sweep_shadow"
        ).fetchone()
        # Without 97: 5@98 + 20@99 → unfilled=48 minus 25 filled = 23 wasted
        # Wait — 48 unfilled, take 5@98, take 20@99 = 25 filled, 23 wasted.
        # cf_pnl_cents = (5*2 - f98) + (20*1 - f99)
        f98 = calculate_taker_fee(5, 98)
        f99 = calculate_taker_fee(20, 99)
        expected_without = (5 * 2 - f98) + (20 * 1 - f99)
        self.assertEqual(r["cf_pnl_cents"], expected_without)
        # With 97: 10@97 + 5@98 + 20@99
        f97 = calculate_taker_fee(10, 97)
        expected_with = (10 * 3 - f97) + (5 * 2 - f98) + (20 * 1 - f99)
        self.assertEqual(r["cf_pnl_cents_with_97"], expected_with)
        # Sanity: with 97 should be larger on a win (more depth taken).
        self.assertGreater(r["cf_pnl_cents_with_97"], r["cf_pnl_cents"])

    def test_settlement_writes_both_columns_on_loss(self):
        # On a loss, with-97 should be MORE NEGATIVE (we sweep into a losing
        # position, taking more loss).
        self._insert()
        self.state.update_tm_sweep_shadow_on_settlement(
            ticker="KXXRP15M-X", market_result="no")
        r = self.state.conn.execute(
            "SELECT cf_pnl_cents, cf_pnl_cents_with_97 FROM tm_sweep_shadow"
        ).fetchone()
        self.assertLess(r["cf_pnl_cents"], 0)
        self.assertLess(r["cf_pnl_cents_with_97"], r["cf_pnl_cents"],
                        "with_97 must be MORE negative on loss "
                        "(97c liquidity is taken AND lost)")


class TestBackfillExistingRows(unittest.TestCase):
    """Existing settled rows have cf_pnl_cents populated but cf_pnl_cents_with_97
    NULL. A one-time backfill must populate them from stored depth_97c_post."""

    def setUp(self):
        from bot import StateManager
        self.tmp_db = "/tmp/test_tm_sweep_with97_backfill.db"
        if os.path.exists(self.tmp_db):
            os.unlink(self.tmp_db)
        self.state = StateManager(db_path=self.tmp_db)

    def tearDown(self):
        self.state.conn.close()
        if os.path.exists(self.tmp_db):
            os.unlink(self.tmp_db)

    def test_backfill_populates_settled_rows_with_null_with97(self):
        from bot import calculate_taker_fee
        # Insert a row, settle it WITHOUT writing with_97 (simulate legacy).
        self.state.insert_tm_sweep_shadow_row(
            ticker="KXBACK-1", event_ticker="E", asset="BTC",
            entry_time="2026-04-26T12:00:00Z",
            entry_price_cents=96, requested_count=50,
            filled_count=0, unfilled_count=50,
            depth_96c_post=0, depth_97c_post=10,
            depth_98c_post=5, depth_99c_post=20)
        # Manually settle with only the original cf column populated.
        self.state.conn.execute(
            "UPDATE tm_sweep_shadow SET status='settled', market_result='yes', "
            "cf_pnl_cents=999, cf_breakdown_json='[]', settled_at='2026-04-26T12:15:00Z' "
            "WHERE ticker='KXBACK-1'")
        self.state.conn.commit()
        # Pre-condition: with_97 is NULL.
        r0 = self.state.conn.execute(
            "SELECT cf_pnl_cents_with_97 FROM tm_sweep_shadow WHERE ticker='KXBACK-1'"
        ).fetchone()
        self.assertIsNone(r0["cf_pnl_cents_with_97"])
        # Run backfill.
        self.state.backfill_tm_sweep_with_97()
        # Post-condition: with_97 is computed.
        r = self.state.conn.execute(
            "SELECT cf_pnl_cents, cf_pnl_cents_with_97 FROM tm_sweep_shadow "
            "WHERE ticker='KXBACK-1'").fetchone()
        self.assertIsNotNone(r["cf_pnl_cents_with_97"])
        # Should equal: take 10@97, 5@98, 20@99 (50 unfilled, 35 swept).
        f97 = calculate_taker_fee(10, 97)
        f98 = calculate_taker_fee(5, 98)
        f99 = calculate_taker_fee(20, 99)
        expected = (10 * 3 - f97) + (5 * 2 - f98) + (20 * 1 - f99)
        self.assertEqual(r["cf_pnl_cents_with_97"], expected)
        # Original cf_pnl_cents must NOT be overwritten.
        self.assertEqual(r["cf_pnl_cents"], 999,
                         "backfill must not touch existing cf_pnl_cents")

    def test_backfill_skips_open_rows(self):
        # Open rows have cf_pnl_cents=NULL; backfill should leave with_97 NULL.
        self.state.insert_tm_sweep_shadow_row(
            ticker="KXOPEN-1", event_ticker="E", asset="BTC",
            entry_time="2026-04-26T12:00:00Z",
            entry_price_cents=96, requested_count=50,
            filled_count=0, unfilled_count=50,
            depth_97c_post=10, depth_98c_post=5, depth_99c_post=20)
        self.state.backfill_tm_sweep_with_97()
        r = self.state.conn.execute(
            "SELECT status, cf_pnl_cents_with_97 FROM tm_sweep_shadow"
        ).fetchone()
        self.assertEqual(r["status"], "open")
        self.assertIsNone(r["cf_pnl_cents_with_97"])

    def test_backfill_idempotent(self):
        # Running twice must not change values.
        self._setup_settled_row()
        self.state.backfill_tm_sweep_with_97()
        v1 = self.state.conn.execute(
            "SELECT cf_pnl_cents_with_97 FROM tm_sweep_shadow"
        ).fetchone()[0]
        self.state.backfill_tm_sweep_with_97()
        v2 = self.state.conn.execute(
            "SELECT cf_pnl_cents_with_97 FROM tm_sweep_shadow"
        ).fetchone()[0]
        self.assertEqual(v1, v2)

    def test_backfill_skips_already_populated_rows(self):
        # If a row already has with_97, backfill must NOT overwrite it.
        self._setup_settled_row()
        self.state.conn.execute(
            "UPDATE tm_sweep_shadow SET cf_pnl_cents_with_97=42")
        self.state.conn.commit()
        self.state.backfill_tm_sweep_with_97()
        r = self.state.conn.execute(
            "SELECT cf_pnl_cents_with_97 FROM tm_sweep_shadow"
        ).fetchone()
        self.assertEqual(r["cf_pnl_cents_with_97"], 42)

    def _setup_settled_row(self):
        self.state.insert_tm_sweep_shadow_row(
            ticker="KXBACK-1", event_ticker="E", asset="BTC",
            entry_time="2026-04-26T12:00:00Z",
            entry_price_cents=96, requested_count=50,
            filled_count=0, unfilled_count=50,
            depth_96c_post=0, depth_97c_post=10,
            depth_98c_post=5, depth_99c_post=20)
        self.state.conn.execute(
            "UPDATE tm_sweep_shadow SET status='settled', market_result='yes', "
            "cf_pnl_cents=999, cf_breakdown_json='[]', settled_at='2026-04-26T12:15:00Z' "
            "WHERE ticker='KXBACK-1'")
        self.state.conn.commit()


class TestWith97AdversarialRegressions(unittest.TestCase):
    """Adversary round 3 findings on the with_97 column."""

    def setUp(self):
        from bot import StateManager
        self.tmp_db = "/tmp/test_tm_sweep_with97_adversarial.db"
        if os.path.exists(self.tmp_db):
            os.unlink(self.tmp_db)
        self.state = StateManager(db_path=self.tmp_db)

    def tearDown(self):
        self.state.conn.close()
        if os.path.exists(self.tmp_db):
            os.unlink(self.tmp_db)

    def test_A3_backfill_skips_unrecognized_market_result(self):
        """Adversary A3: a settled row with market_result='void' (or NULL,
        or some Kalshi-renamed string) must NOT get cf_pnl_cents_with_97=0
        silently — that would be confidently wrong data. Skip the row instead."""
        # Insert a row, mark settled with a weird market_result.
        self.state.insert_tm_sweep_shadow_row(
            ticker="KXVOID-1", event_ticker="E", asset="BTC",
            entry_time="2026-04-26T12:00:00Z",
            entry_price_cents=96, requested_count=50,
            filled_count=0, unfilled_count=50,
            depth_97c_post=10, depth_98c_post=5, depth_99c_post=20)
        self.state.conn.execute(
            "UPDATE tm_sweep_shadow SET status='settled', market_result='void', "
            "cf_pnl_cents=0, cf_breakdown_json='[]', settled_at='X' "
            "WHERE ticker='KXVOID-1'")
        self.state.conn.commit()
        self.state.backfill_tm_sweep_with_97()
        r = self.state.conn.execute(
            "SELECT cf_pnl_cents_with_97 FROM tm_sweep_shadow "
            "WHERE ticker='KXVOID-1'").fetchone()
        self.assertIsNone(r["cf_pnl_cents_with_97"],
                          "void/unknown market_result must leave with_97 NULL, "
                          "not write a misleading 0")

    def test_A3_backfill_skips_null_market_result(self):
        """NULL market_result also must not produce a 0."""
        self.state.insert_tm_sweep_shadow_row(
            ticker="KXNULL-1", event_ticker="E", asset="BTC",
            entry_time="2026-04-26T12:00:00Z",
            entry_price_cents=96, requested_count=50,
            filled_count=0, unfilled_count=50,
            depth_97c_post=10, depth_98c_post=5, depth_99c_post=20)
        # Settled with NULL market_result (impossible via the live path,
        # but defensive against historical or migrated rows).
        self.state.conn.execute(
            "UPDATE tm_sweep_shadow SET status='settled', "
            "cf_pnl_cents=0, cf_breakdown_json='[]', settled_at='X' "
            "WHERE ticker='KXNULL-1'")
        self.state.conn.commit()
        self.state.backfill_tm_sweep_with_97()
        r = self.state.conn.execute(
            "SELECT cf_pnl_cents_with_97 FROM tm_sweep_shadow "
            "WHERE ticker='KXNULL-1'").fetchone()
        self.assertIsNone(r["cf_pnl_cents_with_97"])

    def test_A5_invariant_with97_ge_cf_on_win_property_based(self):
        """Adversary A5: across many depth configurations, with_97 >= cf_pnl
        on a YES win (sweeping 97 only adds positive payoff legs)."""
        from bot import tm_sweep_counterfactual_pnl
        # Sweep over a grid of depth configs; verify monotonicity.
        for d97 in (0, 1, 5, 50, 500):
            for d98 in (0, 1, 5, 50):
                for d99 in (0, 1, 5, 50):
                    for unfilled in (0, 5, 50, 500):
                        cf, _ = tm_sweep_counterfactual_pnl(
                            unfilled=unfilled, entry_tier=96,
                            depths={97: d97, 98: d98, 99: d99},
                            market_result="yes",
                            sweep_tiers=(98, 99))
                        cf_w97, _ = tm_sweep_counterfactual_pnl(
                            unfilled=unfilled, entry_tier=96,
                            depths={97: d97, 98: d98, 99: d99},
                            market_result="yes",
                            sweep_tiers=(97, 98, 99))
                        self.assertGreaterEqual(
                            cf_w97, cf,
                            f"WIN invariant violated: with_97={cf_w97} < "
                            f"cf={cf} at d97={d97} d98={d98} d99={d99} "
                            f"unfilled={unfilled}")

    def test_A5_invariant_zero_97_depth_means_equal_cf(self):
        """Tight always-true invariant: when 97c depth is zero, including 97
        in sweep_tiers must yield IDENTICAL cf_pnl to excluding it (the
        97 leg has no contracts to take).

        Note: when 97c depth > 0, the directional relationship between cf_w97
        and cf is NOT monotonic — 97 fills can DISPLACE 99 fills on small
        unfilled remainders (Kalshi sweeps low→high), so a loss can be
        smaller with 97 included. The earlier wrong invariant was caught by
        this property-based test before shipping; documenting it here so a
        future maintainer doesn't 'fix' the 'inconsistency' by reverting."""
        from bot import tm_sweep_counterfactual_pnl
        for d98 in (0, 1, 5, 50):
            for d99 in (0, 1, 5, 50):
                for unfilled in (0, 5, 50, 500):
                    for result in ("yes", "no"):
                        cf, _ = tm_sweep_counterfactual_pnl(
                            unfilled=unfilled, entry_tier=96,
                            depths={97: 0, 98: d98, 99: d99},
                            market_result=result,
                            sweep_tiers=(98, 99))
                        cf_w97, _ = tm_sweep_counterfactual_pnl(
                            unfilled=unfilled, entry_tier=96,
                            depths={97: 0, 98: d98, 99: d99},
                            market_result=result,
                            sweep_tiers=(97, 98, 99))
                        self.assertEqual(
                            cf, cf_w97,
                            f"Zero-97-depth equivalence violated at "
                            f"d98={d98} d99={d99} unfilled={unfilled} {result}")

    def test_A4_post_migration_column_exists_assertion(self):
        """Adversary A4: if ALTER silently fails the column is missing and
        every downstream write fails with 'no such column'. The migration
        path must verify the column exists post-ALTER and surface failure
        loud (raise) rather than the current silent log.warning.

        Bit 7.1 retarget (2026-05-10): _create_tables moved to bot/state.py;
        use _read_bot() concat to find it regardless of file."""
        source = _read_bot()
        # Slice _create_tables function.
        start = source.find("def _create_tables")
        end = source.find("\n    def ", start + 10)
        body = source[start:end]
        # The migration block must verify post-ALTER. Look for a re-read of
        # PRAGMA table_info or an explicit assertion that the column exists.
        # Either: a second PRAGMA after ALTER, or an explicit raise on missing.
        # Search across newlines (DOTALL).
        self.assertRegex(
            body,
            r"(?s)cf_pnl_cents_with_97.{0,500}raise|"
            r"raise.{0,500}cf_pnl_cents_with_97",
            "Post-ALTER must verify column exists and raise on missing — "
            "silent ALTER failure produces 'no such column' downstream "
            "(adversary A4)")


class TestTMSweepLive(unittest.TestCase):
    """TM sweep promoted from shadow to live (Apr 28 2026 decision).
    The smart IOC picker (_pick_ioc_limit_for_depth) already exists; what
    blocked it for TM was edge_ceiling = floor(prob*100) - fee - reserve,
    which collapses to ~92 for TM-96 (below entry tier → no bump).

    Promotion: when TM_SWEEP_LIVE_ENABLED, override _edge_ceiling for
    terminal_momentum strategies so the picker can bump up to MAX_ENTRY_PRICE
    (99c). Total position size unchanged — count is still capped by
    tm_compute_contracts. Just the IOC limit bumps so Kalshi sweeps."""

    def setUp(self):
        self.source = _read_bot()

    def test_env_var_kill_switch_exists(self):
        self.assertRegex(
            self.source,
            r'TM_SWEEP_LIVE_ENABLED\s*=\s*os\.environ\.get\(\s*["\']TM_SWEEP_LIVE_ENABLED["\']',
            "TM_SWEEP_LIVE_ENABLED must be env-var-toggleable for fast kill")

    def test_override_only_applies_to_terminal_momentum(self):
        """The edge_ceiling override must gate on the exact-set
        TM_LIVE_STRATEGIES (per adversary A2 — not startswith) — and
        not affect DC, MAKER, LPNE, or any other strategy."""
        start = self.source.find("def _submit_taker")
        end = self.source.find("\n    def ", start + 10)
        body = self.source[start:end]
        self.assertIn("TM_SWEEP_LIVE_ENABLED", body,
                      "_submit_taker must check TM_SWEEP_LIVE_ENABLED")
        self.assertIn("in TM_LIVE_STRATEGIES", body,
                      "override must gate on exact-set TM_LIVE_STRATEGIES")

    def test_override_lifts_ceiling_to_max_entry_price(self):
        """When the override fires, _edge_ceiling must be set to MAX_ENTRY_PRICE
        (99) so the smart picker can bump up to the hard cap."""
        start = self.source.find("def _submit_taker")
        end = self.source.find("\n    def ", start + 10)
        body = self.source[start:end]
        # Must assign MAX_ENTRY_PRICE to _edge_ceiling in the TM branch.
        # Use re.DOTALL via re.compile rather than inline (?s) — Python 3.11+
        # rejects (?s) when not at the very start of the pattern (the
        # alternation puts a second (?s) mid-pattern).
        import re as _re
        pattern = _re.compile(
            r"TM_SWEEP_LIVE_ENABLED.{0,500}_edge_ceiling\s*=\s*MAX_ENTRY_PRICE|"
            r"TM_LIVE_STRATEGIES.{0,500}_edge_ceiling\s*=\s*MAX_ENTRY_PRICE",
            _re.DOTALL)
        self.assertRegex(
            body, pattern,
            "_edge_ceiling must be set to MAX_ENTRY_PRICE in TM branch")

    def test_picker_bumps_96_to_99_with_override(self):
        """End-to-end picker test: entry=96, target_qty=50, depths covering
        97/98/99 → picker returns 99 (the cap that delivers 50 contracts)."""
        from bot import OrderExecutor, MAX_ENTRY_PRICE, IOC_LIMIT_MAX_BUMP_CENTS
        # NO-side bids translate to YES asks. NO bid at 4c = YES ask at 96c.
        ob_data = {
            "no": [
                [4, 2],    # YES ask at 96 with 2 contracts
                [3, 7],    # YES ask at 97 with 7
                [2, 5],    # YES ask at 98 with 5
                [1, 100],  # YES ask at 99 with 100
            ]
        }
        # With override: edge_ceiling = 99, max_bump = 3 → picker walks 96→99.
        # Cumul: 2 at 96, 9 at 97, 14 at 98, 114 at 99. target_qty=50 → 99.
        limit = OrderExecutor._pick_ioc_limit_for_depth(
            ob_data, best_yes_ask=96, target_qty=50,
            max_bump_cents=IOC_LIMIT_MAX_BUMP_CENTS,
            edge_ceiling_price=MAX_ENTRY_PRICE,
            max_price=MAX_ENTRY_PRICE)
        self.assertEqual(limit, 99,
                         "picker must return 99 when 96-tier alone insufficient")

    def test_picker_returns_lowest_sufficient_tier(self):
        """If 98c alone has enough depth, picker returns 98 (smallest)."""
        from bot import OrderExecutor, MAX_ENTRY_PRICE, IOC_LIMIT_MAX_BUMP_CENTS
        ob_data = {
            "no": [
                [4, 1],    # 96c × 1
                [2, 100],  # 98c × 100
            ]
        }
        limit = OrderExecutor._pick_ioc_limit_for_depth(
            ob_data, best_yes_ask=96, target_qty=50,
            max_bump_cents=IOC_LIMIT_MAX_BUMP_CENTS,
            edge_ceiling_price=MAX_ENTRY_PRICE,
            max_price=MAX_ENTRY_PRICE)
        self.assertEqual(limit, 98)

    def test_picker_caps_at_99_even_when_target_exceeds_total_depth(self):
        """If total depth in 96-99 is insufficient, picker returns highest
        in-cap tier (99) — never above MAX_ENTRY_PRICE."""
        from bot import OrderExecutor, MAX_ENTRY_PRICE, IOC_LIMIT_MAX_BUMP_CENTS
        ob_data = {"no": [[4, 1], [3, 1], [2, 1], [1, 1]]}  # 4 ct total
        limit = OrderExecutor._pick_ioc_limit_for_depth(
            ob_data, best_yes_ask=96, target_qty=500,
            max_bump_cents=IOC_LIMIT_MAX_BUMP_CENTS,
            edge_ceiling_price=MAX_ENTRY_PRICE,
            max_price=MAX_ENTRY_PRICE)
        self.assertEqual(limit, 99)

    def test_shadow_capture_continues_alongside_live_sweep(self):
        """tm_sweep_shadow capture must still fire when TM_SWEEP_LIVE_ENABLED.
        We need the shadow data stream uninterrupted to monitor whether the
        promotion was right (cf_pnl_with_97 vs realized fills)."""
        # This is enforced by the existing wiring tests that the insert call
        # lives inside the TM_SWEEP_SHADOW_ENABLED gate. The promotion must
        # NOT remove or short-circuit that gate.
        start = self.source.find("def _execute_tm_taker")
        end = self.source.find("\n    def ", start + 10)
        body = self.source[start:end]
        self.assertIn("insert_tm_sweep_shadow_row", body,
                      "shadow capture must still be wired in _execute_tm_taker")
        # Defensive: TM_SWEEP_LIVE_ENABLED must NOT gate the shadow insert.
        # The shadow runs whether live sweep is on or off.
        # Find the insert call's prelude.
        insert_idx = body.find("self._state.insert_tm_sweep_shadow_row(")
        prelude = body[max(0, insert_idx - 2400):insert_idx]
        # The gate before the insert must be TM_SWEEP_SHADOW_ENABLED, not LIVE.
        self.assertIn("if TM_SWEEP_SHADOW_ENABLED:", prelude)
        # The LIVE flag must NOT short-circuit the shadow.
        self.assertNotRegex(
            prelude,
            r"if\s+TM_SWEEP_LIVE_ENABLED.*insert_tm_sweep_shadow_row",
            "TM_SWEEP_LIVE_ENABLED must NOT gate the shadow insert "
            "— shadow runs regardless of live promotion state")


class TestTMSweepLiveAdversarial(unittest.TestCase):
    """Adversary findings on the live promotion (Apr 28 2026)."""

    def setUp(self):
        self.source = _read_bot()

    def test_A1_user_decision_default_on(self):
        """Adversary A1 framing was for default-OFF, but user explicitly
        elected to ship default-ON after seeing the asymmetric tail math
        (4 rounds of adversarial review + guard-aware review). The env
        var still serves as a kill switch — set TM_SWEEP_LIVE_ENABLED=0
        and restart to disable."""
        self.assertRegex(
            self.source,
            r'TM_SWEEP_LIVE_ENABLED\s*=\s*os\.environ\.get\(\s*'
            r'["\']TM_SWEEP_LIVE_ENABLED["\']\s*,\s*["\']1["\']',
            "TM_SWEEP_LIVE_ENABLED must default to '1' (on) "
            "— user-elected after adversarial review (Apr 28 2026)")

    def test_A2_exact_match_strategy_set(self):
        """Adversary A2: startswith('terminal_momentum') matches dead paths
        (terminal_momentum_95, _97). Use exact-set match against current
        TM_PRICE_SET so re-adding 95/97 forces re-validation, not silent
        promotion of unvetted tiers."""
        # Constant must exist as an exact-set, derived from TM_PRICE_SET.
        self.assertRegex(
            self.source,
            r"TM_LIVE_STRATEGIES\s*=\s*frozenset",
            "TM_LIVE_STRATEGIES frozenset must exist for exact-match")
        # The override must use 'in TM_LIVE_STRATEGIES', not startswith.
        start = self.source.find("def _submit_taker")
        end = self.source.find("\n    def ", start + 10)
        body = self.source[start:end]
        self.assertIn("in TM_LIVE_STRATEGIES", body,
                      "override must use exact-set membership, not startswith")
        # Negative guard: startswith('terminal_momentum') must be GONE from
        # the override path (still allowed elsewhere e.g. shadow checks).
        # Find the TM_SWEEP_LIVE_ENABLED block and verify no startswith.
        # The override block is small — check immediate context.
        if "TM_SWEEP_LIVE_ENABLED" in body:
            override_idx = body.find("TM_SWEEP_LIVE_ENABLED")
            override_block = body[override_idx:override_idx + 400]
            self.assertNotIn(
                'startswith("terminal_momentum")', override_block,
                "override block must not use startswith() — adversary A2")

    def test_A2_TM_LIVE_STRATEGIES_excludes_dead_paths(self):
        """Imported value of TM_LIVE_STRATEGIES must not include 95 or 97."""
        from bot import TM_LIVE_STRATEGIES
        self.assertNotIn("terminal_momentum_95", TM_LIVE_STRATEGIES)
        self.assertNotIn("terminal_momentum_97", TM_LIVE_STRATEGIES)
        # And SHOULD include the live tiers from TM_PRICE_SET.
        from bot import TM_PRICE_SET
        for p in TM_PRICE_SET:
            self.assertIn(f"terminal_momentum_{p}", TM_LIVE_STRATEGIES)

    def test_A4_ladder_retry_skip_override(self):
        """Adversary A4: _is_ladder_retry recurses into _submit_taker with
        the same strategy. The override compounding with the +1¢ ladder
        escalation is untested — guard against it by skipping the override
        when the candidate is a ladder retry. Guard-aware reviewer asked
        for a stricter check that the gate's negation is correct (not just
        that the variable is mentioned)."""
        start = self.source.find("def _submit_taker")
        end = self.source.find("\n    def ", start + 10)
        body = self.source[start:end]
        # The override gate must include the EXACT literal `not _is_ladder_retry`
        # near the TM_SWEEP_LIVE_ENABLED check — proves the negation is correct,
        # not just that the variable is referenced.
        idx = body.find("TM_SWEEP_LIVE_ENABLED")
        self.assertGreater(idx, 0, "override block not found in _submit_taker")
        window = body[max(0, idx - 100):idx + 600]
        self.assertIn(
            "not _is_ladder_retry",
            window,
            "override gate must EXPLICITLY suppress on _is_ladder_retry "
            "(adversary A4 + guard-aware reviewer): ladder retry path must "
            "NOT compound smart-picker bump with ladder +1c escalation")

    def test_A6_risk_cap_uses_worst_case_fill_price(self):
        """Adversary A6: tm_compute_contracts uses scan-time price for
        max_by_risk = bankroll * risk_frac / price_cents. With sweep,
        actual capital deployed at swept tier (up to 99c) exceeds the
        per-asset risk cap. Compute count using worst-case fill price
        (MAX_ENTRY_PRICE) when TM_SWEEP_LIVE_ENABLED."""
        from bot import tm_compute_contracts, TM_BASE_CONTRACTS
        # Direct verification: at the same scan-time price, sweep-aware
        # sizing must produce <= count vs sweep-ignorant sizing.
        bankroll = 100000  # $1000 in cents
        # SOL has risk_frac=0.15. At price=96, max_by_risk = 100000*0.15/96 = 156.
        # At price=99 (worst case fill), max_by_risk = 100000*0.15/99 = 151.
        # So sweep-aware sizing must be <= sweep-ignorant.
        ignorant = tm_compute_contracts(96, 200, bankroll, "SOL")
        # If the sweep flag is honored, calling with explicit sweep=True
        # should reduce or equal count.
        # Implementation surface: param `risk_cap_price` defaulting to price.
        # When caller passes risk_cap_price=MAX_ENTRY_PRICE, count must clamp.
        try:
            sweep_aware = tm_compute_contracts(
                96, 200, bankroll, "SOL", risk_cap_price=99)
        except TypeError:
            self.fail("tm_compute_contracts must accept risk_cap_price kwarg")
        self.assertLessEqual(
            sweep_aware, ignorant,
            "sweep-aware sizing (risk_cap_price=99) must be <= ignorant "
            "(risk_cap_price=96) — adversary A6: risk cap dollars-at-risk "
            "must respect worst-case fill price")

    def test_A6_execute_tm_taker_passes_worst_case_when_sweep_live(self):
        """The caller in _execute_tm_taker must pass MAX_ENTRY_PRICE as
        risk_cap_price when TM_SWEEP_LIVE_ENABLED is on, otherwise the
        risk cap is computed at scan-time price even though we'll sweep
        higher."""
        start = self.source.find("def _execute_tm_taker")
        end = self.source.find("\n    def ", start + 10)
        body = self.source[start:end]
        # Find tm_compute_contracts calls inside _execute_tm_taker; at least
        # one must pass risk_cap_price=MAX_ENTRY_PRICE under sweep-live gate.
        self.assertRegex(
            body,
            r"risk_cap_price\s*=",
            "_execute_tm_taker must pass risk_cap_price to "
            "tm_compute_contracts when sweep is live (adversary A6)")


class TestTMSweepLiveAdversarialRound2(unittest.TestCase):
    """Round 2 adversary findings."""

    def test_A5_TM_PRICE_SET_is_frozenset_not_mutable_set(self):
        """Adversary R2 A5: TM_LIVE_STRATEGIES is frozen at import time
        from TM_PRICE_SET. If TM_PRICE_SET is a mutable set and gets
        mutated at runtime (test code, hot-reload), the two go out of
        sync silently. TM_PRICE_SET must be a frozenset."""
        from bot import TM_PRICE_SET
        self.assertIsInstance(TM_PRICE_SET, frozenset,
                              "TM_PRICE_SET must be frozenset to prevent "
                              "runtime drift from TM_LIVE_STRATEGIES")

    def test_A2_tm_compute_contracts_docstring_does_not_overclaim(self):
        """Adversary R2 A2: original docstring claimed risk_cap_price
        'respects the actual capital deployed at the highest swept tier.'
        It doesn't enforce a deployed-cap; it only shrinks the count
        slightly. Docstring must be honest about the advisory nature.

        Bit 3.2: tm_compute_contracts moved to bot/helpers/tm_sweep.py;
        use _read_bot() concat (bot/_impl.py + bot/constants.py + helpers)
        so the source-grep finds the def regardless of file location.
        """
        source = _read_bot()
        # Slice tm_compute_contracts.
        start = source.find("def tm_compute_contracts")
        end = source.find("\ndef ", start + 10)
        body = source[start:end]
        # Must not claim it "respects the actual capital deployed."
        self.assertNotIn(
            "respect the actual capital deployed",
            body,
            "docstring overclaims; risk_cap_price only sizes the COUNT, "
            "not actual cents-deployed")

    def test_A1_tm_96_lacks_maker_tail_or_ladder_documented(self):
        """Adversary R2 A1: terminal_momentum_96 is in TM_LIVE_STRATEGIES
        but NOT in MAKER_TAIL_ELIGIBLE_STRATEGIES or
        LADDER_ESCALATION_ELIGIBLE_STRATEGIES. Asymmetric coverage —
        a swept-but-partial tm_96 IOC has no maker-tail / retry fallback,
        unlike tm_98/99. The constants block must call this out so a
        future maintainer doesn't promote without resolving."""
        # Bit 3.1: TM_LIVE_STRATEGIES assignment lives in bot/constants.py;
        # use _read_bot() which returns the concatenated source.
        source = _read_bot()
        # The TM_LIVE_STRATEGIES block must mention tm_96 has no fallback,
        # OR add it to eligibility sets.
        from bot import TM_LIVE_STRATEGIES, MAKER_TAIL_ELIGIBLE_STRATEGIES
        if "terminal_momentum_96" in TM_LIVE_STRATEGIES:
            # Either tm_96 is in the eligibility sets, or the asymmetry
            # is documented at the TM_LIVE_STRATEGIES assignment.
            in_maker_tail = "terminal_momentum_96" in MAKER_TAIL_ELIGIBLE_STRATEGIES
            # Find the ASSIGNMENT line (not stray comment references).
            assignment_idx = source.find("TM_LIVE_STRATEGIES = frozenset")
            self.assertGreater(assignment_idx, 0, "assignment line not found")
            # Look 2000 chars BEFORE the assignment for the comment block.
            constant_block = source[max(0, assignment_idx - 2000):assignment_idx + 500]
            documented = ("no maker_tail" in constant_block
                          or "no fallback" in constant_block
                          or "no escalation" in constant_block
                          or "Asymmetric-coverage" in constant_block)
            self.assertTrue(
                in_maker_tail or documented,
                "tm_96 in TM_LIVE_STRATEGIES requires either MAKER_TAIL "
                "eligibility or explicit documentation of the asymmetry "
                "(adversary R2 A1)")


class TestTMSweepDirectBumpFix(unittest.TestCase):
    """RCA fix for the no-op deploy of `e463244` (`05488a1`).

    The smart IOC picker block in _submit_taker is gated on _live_ob from
    scanner._get_orderbook_cached() with NO REST fallback. TM tickers
    consistently lack fresh WS cache when TM fires (488/488 production
    rows showed best_ask_source='market_nbbo'), so the picker — and thus
    the override that lifted edge_ceiling to MAX_ENTRY_PRICE — silently
    skipped. Result: 32 post-deploy TM fires with 2.4% / 0% / 100% fill
    rates at 96/98/99c respectively, zero IOC_LIMIT_BUMPED log lines.

    Fix: bypass the picker for TM. Set _ioc_limit_price = MAX_ENTRY_PRICE
    directly when (TM_SWEEP_LIVE_ENABLED and strategy in TM_LIVE_STRATEGIES
    and not _is_ladder_retry and price < MAX_ENTRY_PRICE). The picker's
    'find optimal limit' is over-engineered for TM — we always want the
    cap, and Kalshi auto-cancels surplus at $0 on unfilled IOC tail."""

    def setUp(self):
        self.source = _read_bot()

    def test_direct_bump_block_exists_in_submit_taker(self):
        start = self.source.find("def _submit_taker")
        end = self.source.find("\n    def ", start + 10)
        body = self.source[start:end]
        # Must reference the new direct-bump log line.
        self.assertIn("TM_SWEEP_DIRECT_BUMP", body,
                      "_submit_taker must contain the direct-bump branch "
                      "(picker bypass for TM when WS cache empty)")

    def test_direct_bump_does_not_require_live_ob(self):
        """The direct-bump branch must be OUTSIDE the `if _live_ob` block —
        that's the whole point. If it's inside, we've reproduced the same
        bug we're fixing."""
        start = self.source.find("def _submit_taker")
        end = self.source.find("\n    def ", start + 10)
        body = self.source[start:end]
        # Find the direct-bump branch.
        direct_idx = body.find("TM_SWEEP_DIRECT_BUMP:")
        self.assertGreater(direct_idx, 0)
        # Find the picker `if _live_ob` block end (the `try/except` around it).
        # Walk backwards from direct_idx looking for `if _live_ob`.
        # The direct-bump must come AFTER the picker block, not nested inside.
        # Heuristic: between the picker `if _live_ob and isinstance(price, int)`
        # and the direct-bump branch, there must be the `except Exception:` of
        # the picker's outer try.
        live_ob_idx = body.rfind("if _live_ob and isinstance(price, int)", 0, direct_idx)
        self.assertGreater(live_ob_idx, 0,
                           "picker block landmark not found — code structure changed?")
        between = body[live_ob_idx:direct_idx]
        # The picker block ends with its outer except. If direct-bump is
        # inside, we'd see the body of the picker block continuing. Verify
        # the direct-bump is OUTSIDE by checking the indentation context.
        # Look for the picker's outer "except Exception:" between the picker
        # start and the direct-bump.
        self.assertIn(
            "except Exception:",
            between,
            "direct-bump must live AFTER the picker's try/except block "
            "(outside the _live_ob gate) — otherwise it inherits the same "
            "bug it's fixing")

    def test_direct_bump_gates_on_TM_LIVE_STRATEGIES_exact_match(self):
        start = self.source.find("def _submit_taker")
        end = self.source.find("\n    def ", start + 10)
        body = self.source[start:end]
        direct_idx = body.find("TM_SWEEP_DIRECT_BUMP:")
        # Window 600 chars before the log line — should contain the gate.
        prelude = body[max(0, direct_idx - 1200):direct_idx]
        self.assertIn("TM_SWEEP_LIVE_ENABLED", prelude)
        self.assertIn("in TM_LIVE_STRATEGIES", prelude)
        self.assertIn("not _is_ladder_retry", prelude)

    def test_direct_bump_skips_when_price_already_at_max(self):
        """When entry tier IS already MAX_ENTRY_PRICE (99c), no bump is
        possible. Gate must include `price < MAX_ENTRY_PRICE`."""
        start = self.source.find("def _submit_taker")
        end = self.source.find("\n    def ", start + 10)
        body = self.source[start:end]
        direct_idx = body.find("TM_SWEEP_DIRECT_BUMP:")
        prelude = body[max(0, direct_idx - 1200):direct_idx]
        self.assertRegex(
            prelude,
            r"price\s*<\s*MAX_ENTRY_PRICE|"
            r"_ioc_limit_price\s*<\s*MAX_ENTRY_PRICE",
            "direct-bump gate must include price < MAX_ENTRY_PRICE so we "
            "don't no-op log on entry=99")

    def test_direct_bump_does_not_lower_existing_smart_limit(self):
        """If the smart picker DID fire (orderbook was available — rare for
        TM but possible) and chose _ioc_limit_price = some value, the
        direct-bump must not LOWER it. Specifically: if picker chose
        _ioc_limit_price=99 already, direct-bump is a no-op or set-to-same.
        Gate via `_ioc_limit_price < MAX_ENTRY_PRICE` ensures this."""
        start = self.source.find("def _submit_taker")
        end = self.source.find("\n    def ", start + 10)
        body = self.source[start:end]
        direct_idx = body.find("TM_SWEEP_DIRECT_BUMP:")
        prelude = body[max(0, direct_idx - 1200):direct_idx]
        self.assertIn(
            "_ioc_limit_price < MAX_ENTRY_PRICE",
            prelude,
            "direct-bump must guard `_ioc_limit_price < MAX_ENTRY_PRICE` "
            "so it can never lower a higher picker-chosen limit")

    def test_direct_bump_log_includes_strategy_and_prices(self):
        """The new log line must include strategy name and from→to prices
        so we can confirm in production whether the bump fires."""
        start = self.source.find("def _submit_taker")
        end = self.source.find("\n    def ", start + 10)
        body = self.source[start:end]
        # Find the log statement.
        idx = body.find("TM_SWEEP_DIRECT_BUMP:")
        # Window: 200 chars from the log line should have the format string.
        log_window = body[idx:idx + 400]
        self.assertIn("strategy", log_window.lower())
        self.assertIn("%d", log_window, "log must include numeric prices")

    def test_direct_bump_NOT_triggered_for_dc_or_lpne(self):
        """Other strategies that go through _submit_taker (DC tiers, LPNE,
        confirmation_addon) MUST NOT pick up the bump."""
        start = self.source.find("def _submit_taker")
        end = self.source.find("\n    def ", start + 10)
        body = self.source[start:end]
        # The gate must use `in TM_LIVE_STRATEGIES` which excludes DC/LPNE.
        # Cannot use startswith("terminal_momentum") (that was the A2 footgun
        # and would ALSO trip up here).
        direct_idx = body.find("TM_SWEEP_DIRECT_BUMP:")
        prelude = body[max(0, direct_idx - 800):direct_idx]
        self.assertNotIn(
            'startswith("terminal_momentum")',
            prelude,
            "direct-bump must NOT use startswith — exact-set match only")
        self.assertIn(
            "in TM_LIVE_STRATEGIES",
            prelude)


class TestTMSweepDirectBumpAdversarialRound2(unittest.TestCase):
    """Round 2 adversary findings on the direct-bump fix."""

    def setUp(self):
        self.source = _read_bot()

    def test_A1_NO_side_bypass(self):
        """Adversary R2 A1: picker block bypasses for `_is_no_side` because
        candidate['best_yes_ask'] is actually no_price for NO-side trades.
        Direct-bump must inherit the same guard, otherwise a future TM_NO
        experiment would submit a 99c NO buy (~$1/contract overpay).
        After R3 A1 the gate moved into a `_direct_bump_fired = (...)`
        expression so widen window to capture that assignment."""
        start = self.source.find("def _submit_taker")
        end = self.source.find("\n    def ", start + 10)
        body = self.source[start:end]
        # Find the gate assignment, not the log line.
        gate_idx = body.find("_direct_bump_fired = (")
        self.assertGreater(gate_idx, 0, "gate assignment landmark not found")
        # Window is the gate expression itself (~400 chars).
        gate_block = body[gate_idx:gate_idx + 500]
        self.assertIn(
            "not _is_no_side",
            gate_block,
            "direct-bump gate must include `not _is_no_side` — "
            "otherwise a NO-side TM (future TM_NO experiment) would "
            "submit limit=99c as no_price → ~$1/contract overpay")

    def test_A3_log_fires_after_assignment_not_before(self):
        """Adversary R2 A3: the log line is the production audit trail.
        Place log AFTER the assignment so the line records FACT, not intent.
        Otherwise a log handler error/exception between the log call and
        assignment would create a misleading 'bumped to 99' record while
        actually submitting at scan-time price."""
        start = self.source.find("def _submit_taker")
        end = self.source.find("\n    def ", start + 10)
        body = self.source[start:end]
        # Find the direct-bump block; assignment to MAX_ENTRY_PRICE must come
        # BEFORE the log line.
        bump_idx = body.find("TM_SWEEP_DIRECT_BUMP:")
        self.assertGreater(bump_idx, 0)
        # The assignment "_ioc_limit_price = MAX_ENTRY_PRICE" should live
        # within ~200 chars of the log idx, on a PRECEDING line.
        assign_pattern = "_ioc_limit_price = MAX_ENTRY_PRICE"
        # Find the assignment in the direct-bump block (not anywhere else).
        # Block: from `if (TM_SWEEP_LIVE_ENABLED` 600 chars before the log
        # to ~300 chars after.
        block = body[max(0, bump_idx - 600):bump_idx + 300]
        assign_in_block = block.find(assign_pattern)
        log_in_block = block.find("TM_SWEEP_DIRECT_BUMP:")
        self.assertGreater(assign_in_block, 0,
                           "assignment must exist in the direct-bump block")
        self.assertLess(
            assign_in_block, log_in_block,
            "assignment must come BEFORE the log line (log records FACT)")

    def test_A4_shadow_records_direct_bump_applied(self):
        """Adversary R2 A4: tm_sweep_shadow captures entry_price_cents from
        scan-time price, but direct-bump submits at MAX_ENTRY_PRICE. The
        cf_pnl_cents computation assumes entry_tier == scan-time-price,
        which is no longer true for bumped rows. Add a `direct_bump_applied`
        column so analysts can filter — and so cf_pnl interpretation is
        correct per row."""
        # Schema check: column exists.
        from bot import StateManager
        tmp_db = "/tmp/test_direct_bump_column.db"
        if os.path.exists(tmp_db):
            os.unlink(tmp_db)
        try:
            state = StateManager(db_path=tmp_db)
            cols = {r["name"] for r in state.conn.execute(
                "PRAGMA table_info(tm_sweep_shadow)").fetchall()}
            self.assertIn(
                "direct_bump_applied", cols,
                "tm_sweep_shadow must have direct_bump_applied column "
                "(adversary R2 A4) so direct-bumped rows can be filtered "
                "from cf_pnl aggregations")
            state.conn.close()
        finally:
            if os.path.exists(tmp_db):
                os.unlink(tmp_db)

    def test_A4_capture_records_direct_bump_when_gates_pass(self):
        """The capture in _execute_tm_taker must compute the direct-bump
        prediction (same gate logic as _submit_taker) and pass it to
        insert_tm_sweep_shadow_row."""
        start = self.source.find("def _execute_tm_taker")
        end = self.source.find("\n    def ", start + 10)
        body = self.source[start:end]
        # Capture must reference direct_bump_applied or compute the same
        # gate logic when calling insert_tm_sweep_shadow_row.
        self.assertIn(
            "direct_bump_applied",
            body,
            "_execute_tm_taker must compute and pass direct_bump_applied "
            "to the shadow capture (adversary R2 A4)")


class TestTMSweepDirectBumpAdversarialRound3(unittest.TestCase):
    """Round 3 adversary findings on the round 2 fixes."""

    def setUp(self):
        self.source = _read_bot()

    def test_R3A1_single_source_of_truth_via_candidate_flag(self):
        """Adversary R2 A1+A4: capture predicate duplicates the submit gate.
        If picker fires (rare for TM but possible), capture over-reports.
        Fix: _submit_taker writes candidate['_tm_direct_bump_fired']=True/False
        AFTER its gate runs; capture reads from candidate. Single source."""
        # _submit_taker must set the flag.
        start = self.source.find("def _submit_taker")
        end = self.source.find("\n    def ", start + 10)
        body = self.source[start:end]
        self.assertIn(
            'candidate["_tm_direct_bump_fired"]',
            body,
            "_submit_taker must record bump status to candidate as the "
            "single source of truth for the capture site")

    def test_R3A1_capture_reads_candidate_flag_not_re_predicts(self):
        """The capture in _execute_tm_taker must read candidate's flag,
        not re-evaluate the gate."""
        start = self.source.find("def _execute_tm_taker")
        end = self.source.find("\n    def ", start + 10)
        body = self.source[start:end]
        self.assertIn(
            '_tm_direct_bump_fired',
            body,
            "_execute_tm_taker capture must read candidate['_tm_direct_bump_fired']")
        # Capture must NOT re-implement the gate. Specifically: it must not
        # have all 5+ AND-clauses of the gate inline; reading the flag is
        # one expression.
        cap_idx = body.find("direct_bump_applied=")
        self.assertGreater(cap_idx, 0)
        # The capture's direct_bump_applied source should be the flag, not
        # an inline re-evaluation.
        cap_window = body[max(0, cap_idx - 200):cap_idx + 200]
        self.assertNotRegex(
            cap_window,
            r"direct_bump_applied=int\(\s*\n?\s*TM_SWEEP_LIVE_ENABLED",
            "capture must NOT re-evaluate the gate inline (R3 A1 — "
            "predicate duplication is fragile)")

    def test_R3A2_no_effective_entry_column_misleading_name_dropped(self):
        """Adversary R3 A3: the previously-proposed `effective_entry_price_cents`
        column was misleading — it stored the IOC LIMIT (e.g. 99c on bumped
        rows), not the actual fill price (which sweeps fills at 96/97/98 too).
        Naming it 'effective_entry' implied cost basis, which under-states PnL
        on bumped wins. Removed: analysts compute it as
        `CASE WHEN direct_bump_applied=1 THEN 99 ELSE entry_price_cents END`
        when needed, or use settled_trades for realized fill prices."""
        from bot import StateManager
        tmp_db = "/tmp/test_no_effective_entry_col.db"
        if os.path.exists(tmp_db):
            os.unlink(tmp_db)
        try:
            state = StateManager(db_path=tmp_db)
            cols = {r["name"] for r in state.conn.execute(
                "PRAGMA table_info(tm_sweep_shadow)").fetchall()}
            self.assertNotIn(
                "effective_entry_price_cents", cols,
                "effective_entry_price_cents must NOT exist — it was a "
                "misleading column name (stored LIMIT not FILL); R3 adversary "
                "A3 removed it. Use direct_bump_applied + entry_price_cents.")
            state.conn.close()
        finally:
            if os.path.exists(tmp_db):
                os.unlink(tmp_db)

    def test_R3A1_direct_bump_applied_docstring_clarifies_semantics(self):
        """Adversary R3 A1: `direct_bump_applied=1` means the bump policy
        was active at IOC SUBMIT time, NOT that a fill happened. Rows with
        `direct_bump_applied=1` + `filled_count=0` mean IOC was submitted
        with limit=99 but no liquidity (or place_order returned None).
        Docstring on the insert helper kwarg must surface this semantic.

        Bit 7.1 retarget (2026-05-10): insert_tm_sweep_shadow_row moved to
        bot/state.py; use _read_bot() concat to find it regardless of file."""
        source = _read_bot()
        # Find the insert_tm_sweep_shadow_row signature/docstring.
        idx = source.find("def insert_tm_sweep_shadow_row")
        self.assertGreater(idx, 0)
        # Check next 3000 chars for the clarifying language.
        block = source[idx:idx + 3000]
        import re as _re
        pattern = _re.compile(r"policy active|at submit time|not.*fill",
                              _re.IGNORECASE | _re.DOTALL)
        self.assertRegex(
            block, pattern,
            "direct_bump_applied docstring must clarify it's a policy/intent "
            "flag, not a fill confirmation (R3 A1)")


if __name__ == "__main__":
    unittest.main()
