"""Tests for OrderExecutor._extract_book_levels.

Helper produces a compact JSON snapshot of the top-N YES-side ladder
(yes_bids + derived yes_asks) from a raw Kalshi orderbook dict.

Why derived YES asks: Kalshi stores BIDS on both sides (yes/no). The
YES ask we'd buy at is 100 - no_bid_price. Storing the derived YES
ladder makes forensic queries direct (no mental flip in SQL).

Used at trade-creation, IOC submit, fill, and position-monitor points
to capture book state for forensic analysis (e.g., the XRP TM-96 N→1
destruction on a 1-ct ask stub on 2026-04-25).
"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from bot.executor import OrderExecutor


class TestNoneAndEmpty(unittest.TestCase):
    def test_none_input_returns_none(self):
        self.assertIsNone(OrderExecutor._extract_book_levels(None))

    def test_empty_dict_returns_empty_ladder(self):
        result = OrderExecutor._extract_book_levels({})
        self.assertIsNotNone(result)
        self.assertEqual(json.loads(result), {"yes_bids": [], "yes_asks": []})

    def test_missing_yes_key(self):
        parsed = json.loads(OrderExecutor._extract_book_levels({"no": [[4, 1]]}))
        self.assertEqual(parsed["yes_bids"], [])
        self.assertEqual(parsed["yes_asks"], [[96, 1]])

    def test_missing_no_key(self):
        parsed = json.loads(OrderExecutor._extract_book_levels({"yes": [[87, 100]]}))
        self.assertEqual(parsed["yes_bids"], [[87, 100]])
        self.assertEqual(parsed["yes_asks"], [])


class TestXRPRealCase(unittest.TestCase):
    """Reconstruct the XRP TM-96 trade book (id 293490, 2026-04-25 20:55:30).
    Best YES bid 87c × 3377, best YES ask 96c × 1ct (= no_bid at 4c × 1ct)."""

    def test_xrp_tm96_book_reads_correctly(self):
        ob = {
            "yes": [[87, 3377], [86, 200], [85, 150]],
            "no":  [[4, 1], [3, 1], [2, 5]],
        }
        parsed = json.loads(OrderExecutor._extract_book_levels(ob))
        self.assertEqual(parsed["yes_bids"], [[87, 3377], [86, 200], [85, 150]])
        self.assertEqual(parsed["yes_asks"], [[96, 1], [97, 1], [98, 5]])


class TestEntryFormats(unittest.TestCase):
    def test_dict_entries(self):
        ob = {
            "yes": [{"price": 87, "quantity": 100}],
            "no":  [{"price": 4, "quantity": 50}],
        }
        parsed = json.loads(OrderExecutor._extract_book_levels(ob))
        self.assertEqual(parsed["yes_bids"], [[87, 100]])
        self.assertEqual(parsed["yes_asks"], [[96, 50]])

    def test_float_price_under_1_converted_to_cents(self):
        ob = {"yes": [[0.87, 100]], "no": [[0.04, 50]]}
        parsed = json.loads(OrderExecutor._extract_book_levels(ob))
        self.assertEqual(parsed["yes_bids"], [[87, 100]])
        self.assertEqual(parsed["yes_asks"], [[96, 50]])

    def test_int_price_kept_as_cents(self):
        ob = {"yes": [[87, 100]], "no": [[4, 50]]}
        parsed = json.loads(OrderExecutor._extract_book_levels(ob))
        self.assertEqual(parsed["yes_bids"][0][0], 87)
        self.assertEqual(parsed["yes_asks"][0][0], 96)


class TestOrdering(unittest.TestCase):
    def test_yes_bids_sorted_desc(self):
        ob = {"yes": [[80, 5], [87, 100], [85, 20]], "no": []}
        parsed = json.loads(OrderExecutor._extract_book_levels(ob))
        self.assertEqual([lvl[0] for lvl in parsed["yes_bids"]], [87, 85, 80])

    def test_yes_asks_sorted_asc(self):
        # NO bids 4/6/2 → YES asks 96/94/98 → sorted asc: 94, 96, 98
        ob = {"yes": [], "no": [[4, 10], [6, 20], [2, 30]]}
        parsed = json.loads(OrderExecutor._extract_book_levels(ob))
        self.assertEqual([lvl[0] for lvl in parsed["yes_asks"]], [94, 96, 98])


class TestCapN(unittest.TestCase):
    def test_default_n_is_10(self):
        ob = {"yes": [[100 - i, i + 1] for i in range(15)], "no": []}
        parsed = json.loads(OrderExecutor._extract_book_levels(ob))
        self.assertEqual(len(parsed["yes_bids"]), 10)

    def test_explicit_n_caps_both_sides(self):
        ob = {"yes": [[80 + i, 1] for i in range(20)],
              "no":  [[i, 1] for i in range(20)]}
        parsed = json.loads(OrderExecutor._extract_book_levels(ob, n=3))
        self.assertEqual(len(parsed["yes_bids"]), 3)
        self.assertEqual(len(parsed["yes_asks"]), 3)

    def test_cap_keeps_best_levels_not_first_n(self):
        ob = {"yes": [[50, 1], [99, 1], [60, 1], [80, 1], [70, 1]], "no": []}
        parsed = json.loads(OrderExecutor._extract_book_levels(ob, n=2))
        self.assertEqual([lvl[0] for lvl in parsed["yes_bids"]], [99, 80])


class TestMalformed(unittest.TestCase):
    def test_skips_unrecognized_entry_types(self):
        ob = {
            "yes": [[87, 100], "garbage", None, [85, 50]],
            "no":  [[4, 10], 42],
        }
        parsed = json.loads(OrderExecutor._extract_book_levels(ob))
        self.assertEqual(parsed["yes_bids"], [[87, 100], [85, 50]])
        self.assertEqual(parsed["yes_asks"], [[96, 10]])

    def test_short_list_entry_skipped(self):
        ob = {"yes": [[87], [85, 50]], "no": []}
        parsed = json.loads(OrderExecutor._extract_book_levels(ob))
        self.assertEqual(parsed["yes_bids"], [[85, 50]])

    def test_dict_missing_quantity_skipped(self):
        # Logging "level exists at 87c with 0 contracts" misleads downstream
        # depth-at-best queries. Drop the row entirely.
        ob = {"yes": [{"price": 87}, [85, 50]], "no": []}
        parsed = json.loads(OrderExecutor._extract_book_levels(ob))
        self.assertEqual(parsed["yes_bids"], [[85, 50]])

    def test_non_numeric_price_skipped(self):
        ob = {"yes": [["abc", 5], [87, 100]], "no": []}
        parsed = json.loads(OrderExecutor._extract_book_levels(ob))
        self.assertEqual(parsed["yes_bids"], [[87, 100]])

    def test_nan_qty_skipped(self):
        ob = {"yes": [[87, float("nan")], [85, 50]], "no": []}
        parsed = json.loads(OrderExecutor._extract_book_levels(ob))
        self.assertEqual(parsed["yes_bids"], [[85, 50]])

    def test_inf_qty_skipped_not_raised(self):
        # int(float('inf')) raises OverflowError — must be caught
        ob = {"yes": [[87, float("inf")], [85, 50]], "no": []}
        parsed = json.loads(OrderExecutor._extract_book_levels(ob))
        self.assertEqual(parsed["yes_bids"], [[85, 50]])

    def test_negative_qty_skipped(self):
        ob = {"yes": [[87, -5], [85, 50]], "no": []}
        parsed = json.loads(OrderExecutor._extract_book_levels(ob))
        self.assertEqual(parsed["yes_bids"], [[85, 50]])

    def test_bool_as_price_skipped(self):
        # bool is an int subtype in Python; must NOT be silently parsed as 0/1.
        ob = {"yes": [[True, 50], [False, 50], [87, 100]], "no": []}
        parsed = json.loads(OrderExecutor._extract_book_levels(ob))
        self.assertEqual(parsed["yes_bids"], [[87, 100]])

    def test_bool_as_qty_skipped(self):
        ob = {"yes": [[87, True], [85, 50]], "no": []}
        parsed = json.loads(OrderExecutor._extract_book_levels(ob))
        self.assertEqual(parsed["yes_bids"], [[85, 50]])


class TestNonDictInput(unittest.TestCase):
    """ob_data is contractually a dict. Defensively handle other types
    rather than raise AttributeError on .get() on hot path."""

    def test_list_input_returns_none(self):
        self.assertIsNone(OrderExecutor._extract_book_levels([]))
        self.assertIsNone(OrderExecutor._extract_book_levels([[87, 1]]))

    def test_string_input_returns_none(self):
        self.assertIsNone(OrderExecutor._extract_book_levels("error"))


class TestDuplicateLevels(unittest.TestCase):
    """Kalshi WS deltas can leave duplicate price levels in the book.
    Helper must merge them so downstream depth queries are correct."""

    def test_duplicate_yes_bid_prices_merged(self):
        ob = {"yes": [[87, 100], [87, 50], [85, 20]], "no": []}
        parsed = json.loads(OrderExecutor._extract_book_levels(ob))
        self.assertEqual(parsed["yes_bids"], [[87, 150], [85, 20]])

    def test_duplicate_no_bid_prices_merged_to_yes_asks(self):
        # Two NO bids at 4c → one YES ask at 96c with summed qty
        ob = {"yes": [], "no": [[4, 1], [4, 5]]}
        parsed = json.loads(OrderExecutor._extract_book_levels(ob))
        self.assertEqual(parsed["yes_asks"], [[96, 6]])


class TestDefensive(unittest.TestCase):
    def test_no_bid_at_or_above_100_excluded(self):
        # NO bid >= 100c → YES ask <= 0 → nonsensical, drop.
        ob = {"yes": [], "no": [[100, 1], [101, 1], [99, 5]]}
        parsed = json.loads(OrderExecutor._extract_book_levels(ob))
        self.assertEqual(parsed["yes_asks"], [[1, 5]])

    def test_negative_price_skipped(self):
        ob = {"yes": [[-5, 1], [87, 100]], "no": []}
        parsed = json.loads(OrderExecutor._extract_book_levels(ob))
        self.assertEqual(parsed["yes_bids"], [[87, 100]])

    def test_yes_bid_above_100_excluded(self):
        # YES bid > 100c is malformed (max valid YES price is 99c).
        ob = {"yes": [[150, 1], [87, 100]], "no": []}
        parsed = json.loads(OrderExecutor._extract_book_levels(ob))
        self.assertEqual(parsed["yes_bids"], [[87, 100]])

    def test_float_price_exactly_1_0_is_100c_not_1c(self):
        # Float 1.0 in probability format = 100c (a winning ticket bid).
        # Boundary condition that the legacy `< 1.0` check gets wrong.
        ob = {"yes": [[1.0, 5]], "no": []}
        parsed = json.loads(OrderExecutor._extract_book_levels(ob))
        self.assertEqual(parsed["yes_bids"], [[100, 5]])


class TestJSONShape(unittest.TestCase):
    def test_returns_string(self):
        result = OrderExecutor._extract_book_levels({"yes": [[87, 1]], "no": []})
        self.assertIsInstance(result, str)

    def test_json_is_compact_no_whitespace(self):
        result = OrderExecutor._extract_book_levels({"yes": [[87, 1]], "no": []})
        self.assertNotIn(" ", result)

    def test_json_top_level_keys(self):
        result = OrderExecutor._extract_book_levels({"yes": [[87, 1]], "no": [[4, 1]]})
        parsed = json.loads(result)
        self.assertEqual(set(parsed.keys()), {"yes_bids", "yes_asks"})


if __name__ == "__main__":
    unittest.main()
