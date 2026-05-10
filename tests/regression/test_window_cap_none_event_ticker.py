"""Regression: per-window cap aggregator must not raise on None event_ticker.

Tick error 2026-05-08 14:44:36 at bot/_impl.py:20132:<genexpr> —
`'NoneType' object has no attribute 'split'`. Cause: `dict.get(key, default)`
returns the value (which can be None) when the key is present; the default
only fires when the key is *absent*. The genexpr at line 20132 chained
`.split(...)` directly onto `p.get("event_ticker", "")`, so a transiently
None event_ticker (the same race documented in `_window_timeslot` at
line 19682 via the WINDOW_TIMESLOT_NULL warning) raised AttributeError.

Regression introduced in commit 6ac7aed (risk architecture overhaul) which
swapped `p.get("event_ticker") == _event_ticker` (None-safe) for
`p.get("event_ticker", "").split("-", 1)[-1] == _timeslot` (None-unsafe).

Postmortem: kb/failures/transient-none-event-ticker-may08.md.

Guards three layers:
1. AST/source guard (broad) — bot/_impl.py must not chain any string method
   off `.get(key, "")` for key in {`event_ticker`, `ticker`}. Tighter than
   regex on `.split` alone — also catches `.startswith`, `.lower`, etc.
2. Direct staticmethod test — imports the production source-of-truth
   (`OrderExecutor._existing_window_cost_for_timeslot`) and exercises it
   against positions with None / missing-key / empty-string event_tickers.
3. Observability + outer-guard tests — confirm the WINDOW_CAP_NULL_EVENT_TICKER
   warning fires on None and the candidate-side `if _event_ticker:` guard
   short-circuits the cap when the candidate has no event_ticker.
"""
import ast
import logging
import os
import re
import unittest

BOT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "bot/_impl.py",
)


def _read_bot():
    """Bit 9.1 (2026-05-10): includes bot/executor.py — OrderExecutor extracted."""
    import os
    parts = []
    with open(BOT_PATH) as f:
        parts.append(f.read())
    _executor_path = os.path.join(os.path.dirname(BOT_PATH), "executor.py")
    if os.path.isfile(_executor_path):
        with open(_executor_path) as f:
            parts.append(f.read())
    return "\n".join(parts)


# ── AST/source-level guards ──────────────────────────────────────────────


class TestNoStringMethodChainedOnGetWithEmptyDefault(unittest.TestCase):
    """Broader bug-class guard: `.get(key, "")` followed by ANY string method
    is the same footgun. Catches `.split`, `.startswith`, `.endswith`,
    `.lower`, `.upper`, `.replace`, `.strip`, `.find`, `.format`, etc.

    Walks the AST so multi-line call sites (
        x.get(\\n  "event_ticker",\\n  ""\\n).split(\\n)
    ) are caught — a regex on raw source would miss those.
    """

    GUARDED_KEYS = ("event_ticker", "ticker")

    @staticmethod
    def _is_get_with_empty_default(call, guarded_keys):
        """True iff `call` is `<x>.get(<KEY>, "")` for a string KEY in
        `guarded_keys`. Also catches the keyword form `.get(<KEY>, default="")`."""
        if not (isinstance(call.func, ast.Attribute)
                and call.func.attr == "get"):
            return None
        # Positional: get(KEY) or get(KEY, default)
        key_arg = None
        if call.args:
            if isinstance(call.args[0], ast.Constant) and isinstance(call.args[0].value, str):
                key_arg = call.args[0].value
        if key_arg is None or key_arg not in guarded_keys:
            return None
        # Default: positional 2nd arg OR keyword `default=`
        default_value = None
        has_default = False
        if len(call.args) >= 2 and isinstance(call.args[1], ast.Constant):
            default_value = call.args[1].value
            has_default = True
        else:
            for kw in call.keywords:
                if kw.arg == "default" and isinstance(kw.value, ast.Constant):
                    default_value = kw.value.value
                    has_default = True
                    break
        if not has_default or default_value != "":
            return None
        return key_arg

    def _walk_unsafe_get_default_empty(self, source):
        """Yield (lineno, key, op_kind, op_detail) for every unsafe chain:
          - Attribute access:  .get(KEY, "").<attr>(...)
          - Subscript:         .get(KEY, "")[idx]
        where KEY is in GUARDED_KEYS. Both forms TypeError on a None value."""
        tree = ast.parse(source)
        for node in ast.walk(tree):
            # Form 1: Attribute on a Call
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Call):
                key = self._is_get_with_empty_default(node.value, self.GUARDED_KEYS)
                if key is not None:
                    yield (node.lineno, key, "attr", node.attr)
                    continue
            # Form 2: Subscript on a Call
            if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Call):
                key = self._is_get_with_empty_default(node.value, self.GUARDED_KEYS)
                if key is not None:
                    yield (node.lineno, key, "subscript", "[…]")

    def test_no_unsafe_chain_for_event_ticker_or_ticker(self):
        source = _read_bot()
        hits = list(self._walk_unsafe_get_default_empty(source))
        if hits:
            details = "\n".join(
                f"  bot/_impl.py:{ln} — .get({key!r}, '')"
                f"{'.' + detail + '(...)' if kind == 'attr' else detail}"
                for ln, key, kind, detail in hits
            )
            self.fail(
                f"Found {len(hits)} unsafe `.get(key, '')` chain(s); a "
                f"transiently-None value TypeErrors here.\n"
                f"Coerce first via `(x.get({hits[0][1]!r}) or '')…` "
                f"or extract a defensive helper:\n{details}"
            )


class TestPerWindowCapHelperHasObservability(unittest.TestCase):
    """Anchored at the per-window-cap site: the helper must (a) exist as a
    named function (not an inline genexpr), (b) emit a WINDOW_CAP-prefixed
    warning when None event_ticker is observed."""

    def test_helper_method_exists(self):
        source = _read_bot()
        self.assertIn(
            "_existing_window_cost_for_timeslot",
            source,
            "Per-window-cap aggregator must be extracted as a named helper "
            "method (`_existing_window_cost_for_timeslot`) so it is unit-"
            "testable and so observability/defensive-coerce live in one "
            "place — not buried in an inline genexpr."
        )

    def test_helper_logs_null_event_ticker(self):
        source = _read_bot()
        # The helper body must contain the observability warning so frequency
        # of the transient race is trackable.
        m = re.search(
            r'def\s+_existing_window_cost_for_timeslot\b'
            r'.*?(?=\n    (?:@staticmethod|def|class)\b|\Z)',
            source, re.DOTALL,
        )
        self.assertIsNotNone(
            m,
            "Could not locate _existing_window_cost_for_timeslot body — was "
            "it renamed or removed?"
        )
        body = m.group(0)
        self.assertIn(
            "WINDOW_CAP_NULL_EVENT_TICKER", body,
            "_existing_window_cost_for_timeslot must emit the "
            "WINDOW_CAP_NULL_EVENT_TICKER warning when None event_ticker "
            "is observed — otherwise the transient race silently shrinks "
            "the cap denominator with no operator visibility."
        )


# ── Direct-import runtime guards ─────────────────────────────────────────
#
# These tests import `bot._impl.OrderExecutor` and exercise the helper
# method that the production scan loop calls. CALMLP_ENABLED=0 is set
# in setUpModule to skip the module-level cal_mlp warmup (which requires
# pandas and ~5s of disk I/O) — the helper under test does not depend on
# cal_mlp.


def setUpModule():
    # Force CALMLP_ENABLED=0 unconditionally so importing bot._impl skips
    # the cal_mlp warmup (~1.3s + pandas requirement). `setdefault` would
    # leak a sibling test's CALMLP_ENABLED=1; we own this var for the
    # duration of this module.
    os.environ["CALMLP_ENABLED"] = "0"


class TestExistingWindowCostForTimeslotHelper(unittest.TestCase):
    """Direct-call tests on the production source-of-truth helper."""

    @classmethod
    def setUpClass(cls):
        # Import lazily so CALMLP_ENABLED env can be honored.
        from bot._impl import OrderExecutor  # noqa: WPS433
        cls.OrderExecutor = OrderExecutor

    def helper(self, positions, timeslot):
        # Call as a staticmethod via the class (skips Py3.9 bound-method
        # descriptor surprise from `cls.helper = ...` assignment).
        return self.OrderExecutor._existing_window_cost_for_timeslot(
            positions, timeslot)

    def test_none_event_ticker_does_not_crash(self):
        """Reproduces the exact 2026-05-08 14:44:36 production tick error.
        Pre-fix: `'NoneType' object has no attribute 'split'`.
        Post-fix: row skipped, no crash."""
        positions = [
            {"event_ticker": "KXBTC15M-26MAY081045", "total_cost_cents": 100,
             "ticker": "KXBTC15M-26MAY081045-90", "status": "open"},
            {"event_ticker": None, "total_cost_cents": 50,
             "ticker": "KXSOL15M-26MAY081045-50", "status": "open"},
            {"event_ticker": "KXETH15M-26MAY081045", "total_cost_cents": 75,
             "ticker": "KXETH15M-26MAY081045-80", "status": "open"},
        ]
        try:
            total = self.helper(positions, "26MAY081045")
        except AttributeError as e:
            self.fail(
                f"helper raised AttributeError on None event_ticker: {e} "
                f"(this is the exact 2026-05-08 14:44:36 production bug)"
            )
        # BTC + ETH match; None row skipped (not crashed AND not falsely matched).
        self.assertEqual(total, 175)

    def test_only_none_positions_returns_zero_no_crash(self):
        """All-None positions list must produce 0 (not crash, not match)."""
        positions = [
            {"event_ticker": None, "total_cost_cents": 50,
             "ticker": "X1", "status": "open"},
            {"event_ticker": None, "total_cost_cents": 25,
             "ticker": "X2", "status": "open"},
        ]
        self.assertEqual(self.helper(positions, "26MAY081045"), 0)

    def test_missing_event_ticker_key(self):
        """Positions dict without the event_ticker key at all — same defense."""
        positions = [{"total_cost_cents": 100, "ticker": "X1", "status": "open"}]
        self.assertEqual(self.helper(positions, "ANY"), 0)

    def test_none_total_cost_cents_skipped_not_crashed(self):
        """Same defense extended to total_cost_cents — schema is INTEGER NOT
        NULL, but the same race that surfaced None event_ticker plausibly
        surfaces None for other columns. Skip + warn rather than TypeError
        on `total += None`."""
        positions = [
            {"event_ticker": "KXBTC15M-26MAY081045", "total_cost_cents": 100,
             "ticker": "KXBTC15M-26MAY081045-90", "status": "open"},
            {"event_ticker": "KXETH15M-26MAY081045", "total_cost_cents": None,
             "ticker": "KXETH15M-26MAY081045-80", "status": "open"},
        ]
        try:
            total = self.helper(positions, "26MAY081045")
        except TypeError as e:
            self.fail(
                f"helper TypeError on None total_cost_cents: {e} — same "
                f"bug class as the event_ticker fix; defend defensively."
            )
        self.assertEqual(total, 100)

    def test_empty_string_event_ticker(self):
        """Empty string is falsy → treated like None (skip row, log warning)."""
        positions = [
            {"event_ticker": "", "total_cost_cents": 100,
             "ticker": "X1", "status": "open"},
        ]
        self.assertEqual(self.helper(positions, "26MAY081045"), 0)

    def test_cross_asset_same_timeslot_sums(self):
        """The whole point of the per-window cap: sum across BTC/ETH/SOL/XRP
        sharing the same 15-min timeslot."""
        positions = [
            {"event_ticker": "KXBTC15M-26MAY081045", "total_cost_cents": 100,
             "ticker": "KXBTC15M-26MAY081045-90", "status": "open"},
            {"event_ticker": "KXETH15M-26MAY081045", "total_cost_cents": 75,
             "ticker": "KXETH15M-26MAY081045-80", "status": "open"},
            {"event_ticker": "KXSOL15M-26MAY081045", "total_cost_cents": 50,
             "ticker": "KXSOL15M-26MAY081045-70", "status": "open"},
            {"event_ticker": "KXXRP15M-26MAY081100", "total_cost_cents": 60,
             "ticker": "KXXRP15M-26MAY081100-90", "status": "open"},
        ]
        self.assertEqual(self.helper(positions, "26MAY081045"), 225)
        self.assertEqual(self.helper(positions, "26MAY081100"), 60)
        self.assertEqual(self.helper(positions, "NONEXISTENT"), 0)

    def test_logs_warning_on_none_event_ticker(self):
        """Observability — without this log we cannot tell whether the race
        is firing once a day or once a minute."""
        positions = [
            {"event_ticker": None, "total_cost_cents": 50,
             "ticker": "KXSOL15M-26MAY081045-50", "status": "open"},
        ]
        with self.assertLogs(level="WARNING") as cm:
            self.helper(positions, "26MAY081045")
        # Must mention the named tag and the diagnostic context (ticker).
        self.assertTrue(
            any("WINDOW_CAP_NULL_EVENT_TICKER" in m for m in cm.output),
            f"helper did not emit WINDOW_CAP_NULL_EVENT_TICKER warning: "
            f"{cm.output}"
        )

    def test_no_warning_on_normal_positions(self):
        """Negative test — warning must NOT fire on healthy positions
        (avoids alert fatigue)."""
        positions = [
            {"event_ticker": "KXBTC15M-26MAY081045", "total_cost_cents": 100,
             "ticker": "KXBTC15M-26MAY081045-90", "status": "open"},
        ]
        # Use a logger handler that just captures records (assertNoLogs is
        # 3.10+, but assertLogs raises AssertionError if no records — we
        # use logger handler bypass instead).
        records = []

        class Handler(logging.Handler):
            def emit(self, record):
                records.append(record)

        h = Handler(level=logging.WARNING)
        root = logging.getLogger()
        root.addHandler(h)
        try:
            self.helper(positions, "26MAY081045")
        finally:
            root.removeHandler(h)
        warns = [r for r in records
                 if "WINDOW_CAP_NULL_EVENT_TICKER" in r.getMessage()]
        self.assertEqual(
            warns, [],
            f"unexpected WINDOW_CAP_NULL_EVENT_TICKER warning on healthy "
            f"positions: {warns}"
        )


# ── Outer-guard test (m10) ───────────────────────────────────────────────


class TestOuterCandidateEventTickerGuard(unittest.TestCase):
    """The per-window-cap block is wrapped in `if _event_ticker:` (the
    candidate's own event_ticker). If a future edit removes that guard,
    a candidate with no event_ticker would invoke the helper on
    `_event_ticker.split(...)` at line 20127 — TypeError, not the same
    bug class but the same overall risk surface. This guard test pins
    the outer-guard structure."""

    def test_outer_event_ticker_guard_present(self):
        source = _read_bot()
        # Anchor on WINDOW_CAP_SKIPPED (unique log line in this block).
        idx = source.index("WINDOW_CAP_SKIPPED")
        # Slice 4 KB before — the outer guard must appear here. (1 KB
        # was too tight; the block has ~14 lines of indented comments
        # between the guard and the SKIPPED log line.)
        block = source[max(0, idx - 4096):idx]
        self.assertIn(
            "_event_ticker = candidate.get(\"event_ticker\")", block,
            "candidate.event_ticker must be captured before the per-window "
            "cap block (outer guard structure)."
        )
        self.assertIn(
            "if _event_ticker:", block,
            "Outer guard `if _event_ticker:` must wrap the per-window-cap "
            "block — without it, a candidate with no event_ticker would "
            "TypeError on `_event_ticker.split(...)`."
        )


if __name__ == "__main__":
    unittest.main()
