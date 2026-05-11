"""Ladder-escalation-after-IOC-partial regression tests.

Background — Apr 26 2026:
After an IOC partial-fills (e.g., wanted 50ct, got 2 because real top of
book was thin), the bot used to either rest a maker tail at the original
price (passive) or move on. For high-conviction strategies, the next
price level up often has real depth that we'd happily take — the
strategy already endorsed entries at any price within its band. Resting
at the original price assumes the level will refill; ladder escalation
actively reaches up one tick to capture available liquidity.

Decision: ship live with kill-switch env var (no shadow). Ship narrow:
  - SINGLE retry only (N=1). Multi-step adds surface for ~$0.10/ct of
    marginal EV gain, not worth the bug surface yet.
  - +1¢ offset per step.
  - Same eligibility set as MAKER_TAIL (8 live IOC strategies).
  - Capped at strategy's MAX_ENTRY_PRICE (e.g., decided_t2 cannot
    escalate past 96¢ even though global cap is 99¢).
  - Coexists with maker tail: if escalation also partial-fills, fall
    through to maker tail at ORIGINAL price (catches anyone returning
    to the original level).
  - Recursion guard: escalation cannot re-escalate.

What this file pins:
  - Constants: LADDER_ESCALATION_ENABLED, MAX_STEPS=1, OFFSET=1,
    MIN_REMAINDER=5, ELIGIBLE_STRATEGIES set.
  - Eligibility gate: only the 8 live IOC strategies escalate.
  - Per-strategy max-price cap: decided_t2 stops at 96, others at 99.
  - Recursion guard: same-tick re-escalation prevented.
  - Coexistence with maker tail: escalation partial → maker tail at
    original price (not escalated price).
  - Wiring: _submit_taker invokes _maybe_ladder_escalate before
    _maybe_post_maker_tail.

Companion: kb/decisions/ladder-escalation-after-ioc-partial.md (TBD).
"""

import ast
import os
import sys
import unittest
from unittest.mock import MagicMock, patch
import bot.executor  # noqa: F401
import bot.constants  # noqa: F401

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

BOT_PY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "bot/executor.py")  # Bit 9.1 (2026-05-10): retargeted to bot/executor.py — OrderExecutor extracted from bot/_impl.py


def _make_executor():
    """Build a real OrderExecutor with mocked I/O. Mirrors test_maker_tail."""
    import bot
    client = MagicMock()
    state = MagicMock()
    logger = MagicMock()
    ml = MagicMock()
    feed = MagicMock()
    feed.is_connected = True
    feed.pop_fills.return_value = []
    ex = bot.executor.OrderExecutor(
        client=client, state=state, logger=logger,
        main_loop=ml, kalshi_feed=feed)
    # Default: orderbook fetch returns a deep book (escalated level
    # has plenty of depth). Tests that need to exercise the
    # phantom-abort path override this on `ex._client.get_orderbook`.
    client.get_orderbook = MagicMock(return_value={
        "yes": [[95, 200], [96, 200], [97, 200], [98, 200],
                [99, 200], [100, 1]],
        "no": [],
    })
    # Default: no existing positions so ticker-cap re-check passes.
    state.get_open_positions = MagicMock(return_value=[])
    return ex


def _candidate(strategy="terminal_momentum_98", asset="ETH", price=98,
               size=50, stc=200.0, side="yes", cal_prob=0.94):
    return {
        "ticker": f"KX{asset}15M-LADDER-X",
        "event_ticker": f"KX{asset}15M-LADDER-EVT",
        "asset": asset,
        "best_yes_ask": price,
        "balance_at_scan": 100_000,
        "position_size": size,
        "strategy": strategy,
        "best_ask_source": "orderbook",
        "ob_snapshot": {"ask_depth": 200, "best_ask": price},
        "seconds_to_close": stc,
        "side": side,
        "calibrated_prob": cal_prob,
    }


# ────────────────────────────────────────────────────────────────────
# Class 1: constants
# ────────────────────────────────────────────────────────────────────


class TestConstants(unittest.TestCase):

    def test_kill_switch_constant_exists(self):
        import bot
        self.assertTrue(
            hasattr(bot.constants, "LADDER_ESCALATION_ENABLED"),
            "LADDER_ESCALATION_ENABLED must exist as an env-var-gated "
            "module-level flag — required to disable the feature without "
            "a code change if it misbehaves in prod.")

    def test_max_steps_is_one(self):
        import bot
        self.assertEqual(
            getattr(bot.constants, "LADDER_ESCALATION_MAX_STEPS", None), 1,
            "First-ship is single-retry only. Multi-step exposes more "
            "surface for marginal EV gain; revisit after 14d data.")

    def test_offset_is_one_cent(self):
        import bot
        self.assertEqual(
            getattr(bot.constants, "LADDER_ESCALATION_OFFSET", None), 1,
            "+1¢ per step. The maker-tail behavior is at original price; "
            "ladder is the active alternative one tick up.")

    def test_min_remainder_constant_exists(self):
        import bot
        # Mirror MAKER_TAIL_MIN_REMAINDER. Below this, API + state
        # overhead exceeds expected EV gain.
        self.assertEqual(
            getattr(bot.constants, "LADDER_ESCALATION_MIN_REMAINDER", None), 5,
            "MIN_REMAINDER=5 mirrors maker-tail; below this the API + "
            "state overhead exceeds expected EV gain.")

    def test_eligibility_set_matches_maker_tail(self):
        """Initial eligibility = MAKER_TAIL set. These 8 strategies have
        already been vetted as 'we want more size on partial fills'."""
        import bot
        eligible = getattr(
            bot.constants, "LADDER_ESCALATION_ELIGIBLE_STRATEGIES", set())
        for s in [
            "decided_t1", "decided_t1b",
            "decided_t2", "decided_t2_z25",
            "terminal_momentum_98", "terminal_momentum_99",
            "weekend_discount", "overnight_discount",
        ]:
            self.assertIn(
                s, eligible,
                f"Strategy {s!r} is in MAKER_TAIL_ELIGIBLE_STRATEGIES; "
                f"ladder escalation should mirror.")

    def test_eligibility_excludes_unsuitable(self):
        import bot
        eligible = getattr(
            bot.constants, "LADDER_ESCALATION_ELIGIBLE_STRATEGIES", set())
        for s in [
            "lpne",                # STC 10-120s too tight for retry
            "weather_no_live",     # 1-ct fixed, never partials
            "hourly_no_live",      # disabled
            "confirmation_addon",  # overlay, different mechanics
            "terminal_momentum_95",  # off / loss-making
            "terminal_momentum_96",
            "terminal_momentum_97",
        ]:
            self.assertNotIn(
                s, eligible,
                f"Strategy {s!r} must NOT escalate.")


# ────────────────────────────────────────────────────────────────────
# Class 2: per-strategy max-price cap
# ────────────────────────────────────────────────────────────────────


class TestPerStrategyMaxPrice(unittest.TestCase):
    """The escalated price must not exceed the strategy's MAX_ENTRY_PRICE.

    decided_t2 / decided_t2_z25 are capped at DECIDED_CONTRACT_T2_MAX_PRICE
    (96¢). Others use the global MAX_ENTRY_PRICE (99¢).
    """

    def setUp(self):
        # LADDER_ESCALATION_ENABLED defaults to OFF (codebase pattern).
        # Tests in this class assert behavior when ENABLED — patch ON.
        self._enabled_patch = patch("bot.executor.LADDER_ESCALATION_ENABLED", True)
        self._enabled_patch.start()
        self.addCleanup(self._enabled_patch.stop)
        self.ex = _make_executor()

    def test_decided_t2_at_max_price_does_not_escalate(self):
        """decided_t2 partial at 96¢ → no escalation (already at strategy max)."""
        cand = _candidate(strategy="decided_t2", price=96, size=50)
        result = self.ex._maybe_ladder_escalate(
            candidate=cand, original_limit=96, remaining=41, ioc_filled=9)
        self.assertFalse(
            result.get("escalated", False),
            "decided_t2 at 96¢ is already at the strategy's price band "
            "ceiling. Escalating to 97¢ would put us in territory the "
            "strategy never endorsed.")
        self.ex._client.place_order.assert_not_called()

    def test_decided_t2_below_max_escalates_within_band(self):
        """decided_t2 partial at 95¢ → escalate to 96¢ (still within band)."""
        cand = _candidate(strategy="decided_t2", price=95, size=50)
        self.ex._client.place_order.return_value = {
            "order": {"order_id": "OID-T2-LAD"}}
        result = self.ex._maybe_ladder_escalate(
            candidate=cand, original_limit=95, remaining=41, ioc_filled=9)
        self.assertTrue(result.get("escalated", False))

    def test_global_max_blocks_escalation_at_99(self):
        """Any strategy at 99¢ cannot escalate (global cap)."""
        cand = _candidate(strategy="decided_t1", price=99, size=50)
        result = self.ex._maybe_ladder_escalate(
            candidate=cand, original_limit=99, remaining=41, ioc_filled=9)
        self.assertFalse(
            result.get("escalated", False),
            "99¢ is the global MAX_ENTRY_PRICE — no escalation possible.")

    def test_tm_98_escalates_to_99(self):
        """TM_98 partial at 98¢ → escalate to 99¢ (under global cap)."""
        cand = _candidate(strategy="terminal_momentum_98", price=98, size=50)
        self.ex._client.place_order.return_value = {
            "order": {"order_id": "OID-TM-LAD"}}
        result = self.ex._maybe_ladder_escalate(
            candidate=cand, original_limit=98, remaining=48, ioc_filled=2)
        self.assertTrue(result.get("escalated", False))


# ────────────────────────────────────────────────────────────────────
# Class 3: gates that suppress escalation
# ────────────────────────────────────────────────────────────────────


class TestEscalationGates(unittest.TestCase):

    def setUp(self):
        # LADDER_ESCALATION_ENABLED defaults to OFF (codebase pattern).
        # Tests in this class assert behavior when ENABLED — patch ON.
        self._enabled_patch = patch("bot.executor.LADDER_ESCALATION_ENABLED", True)
        self._enabled_patch.start()
        self.addCleanup(self._enabled_patch.stop)
        self.ex = _make_executor()

    def test_zero_fill_does_not_escalate(self):
        """Zero IOC fill = phantom-abort-style or empty book; do not
        escalate into another phantom level."""
        cand = _candidate(strategy="terminal_momentum_98", price=98)
        result = self.ex._maybe_ladder_escalate(
            candidate=cand, original_limit=98, remaining=50, ioc_filled=0)
        self.assertFalse(result.get("escalated", False))
        self.ex._client.place_order.assert_not_called()

    def test_remainder_under_min_no_escalate(self):
        cand = _candidate(strategy="terminal_momentum_98", price=98)
        result = self.ex._maybe_ladder_escalate(
            candidate=cand, original_limit=98, remaining=4, ioc_filled=46)
        self.assertFalse(
            result.get("escalated", False),
            "Remainder<5: API + state overhead > expected EV.")

    def test_ineligible_strategy_no_escalate(self):
        cand = _candidate(strategy="lpne", price=85, size=50)
        result = self.ex._maybe_ladder_escalate(
            candidate=cand, original_limit=85, remaining=20, ioc_filled=30)
        self.assertFalse(result.get("escalated", False))
        self.ex._client.place_order.assert_not_called()

    def test_no_strategy_field_no_escalate(self):
        """Defensive: candidate missing strategy field shouldn't raise."""
        cand = _candidate(price=98)
        cand.pop("strategy", None)
        result = self.ex._maybe_ladder_escalate(
            candidate=cand, original_limit=98, remaining=41, ioc_filled=9)
        self.assertFalse(result.get("escalated", False))

    def test_kill_switch_disables_escalation(self):
        cand = _candidate(strategy="terminal_momentum_98", price=98)
        with patch("bot.executor.LADDER_ESCALATION_ENABLED", False):
            result = self.ex._maybe_ladder_escalate(
                candidate=cand, original_limit=98, remaining=41,
                ioc_filled=9)
        self.assertFalse(
            result.get("escalated", False),
            "When LADDER_ESCALATION_ENABLED=False, no escalation runs "
            "regardless of strategy/gates.")
        self.ex._client.place_order.assert_not_called()


# ────────────────────────────────────────────────────────────────────
# Class 4: recursion guard
# ────────────────────────────────────────────────────────────────────


class TestRecursionGuard(unittest.TestCase):
    """N=1 means we escalate ONCE per scan tick. The escalated IOC must
    not itself escalate again (which would silently uplift to N=2)."""

    def setUp(self):
        # LADDER_ESCALATION_ENABLED defaults to OFF (codebase pattern).
        # Tests in this class assert behavior when ENABLED — patch ON.
        self._enabled_patch = patch("bot.executor.LADDER_ESCALATION_ENABLED", True)
        self._enabled_patch.start()
        self.addCleanup(self._enabled_patch.stop)
        self.ex = _make_executor()

    def test_already_escalated_candidate_does_not_escalate(self):
        """If candidate carries _is_ladder_retry=True, the helper must
        not produce another escalation attempt — that's the recursion
        boundary that keeps N=1 honest."""
        cand = _candidate(strategy="terminal_momentum_98", price=99)
        cand["_is_ladder_retry"] = True
        result = self.ex._maybe_ladder_escalate(
            candidate=cand, original_limit=99, remaining=41, ioc_filled=9)
        self.assertFalse(
            result.get("escalated", False),
            "_is_ladder_retry=True means we're already inside an "
            "escalation. Another escalation would silently upgrade to "
            "N=2 — must be blocked.")


# ────────────────────────────────────────────────────────────────────
# Class 5: coexistence with maker tail
# ────────────────────────────────────────────────────────────────────


class TestMakerTailCoexistence(unittest.TestCase):
    """When an escalation also partial-fills, fall through to maker tail
    at the ORIGINAL price (not the escalated price). The original price
    is more likely to refill via natural rotation; the escalated price
    is by definition the price where the bot just had to step UP for
    fillable depth, so resting there is less attractive."""

    def setUp(self):
        # LADDER_ESCALATION_ENABLED defaults to OFF (codebase pattern).
        # Tests in this class assert behavior when ENABLED — patch ON.
        self._enabled_patch = patch("bot.executor.LADDER_ESCALATION_ENABLED", True)
        self._enabled_patch.start()
        self.addCleanup(self._enabled_patch.stop)
        self.ex = _make_executor()

    def test_submit_taker_invokes_ladder_before_maker_tail(self):
        """AST regression: in _submit_taker, _maybe_ladder_escalate must
        be called BEFORE _maybe_post_maker_tail. Order matters — we
        prefer active ladder over passive tail."""
        with open(BOT_PY) as f:
            src = f.read()
        tree = ast.parse(src)
        submit_fn = None
        for cls in ast.walk(tree):
            if (not isinstance(cls, ast.ClassDef)
                    or cls.name != "OrderExecutor"):
                continue
            for fn in cls.body:
                if (isinstance(fn, ast.FunctionDef)
                        and fn.name == "_submit_taker"):
                    submit_fn = fn
                    break
        self.assertIsNotNone(submit_fn)
        body = ast.unparse(submit_fn)
        ladder_pos = body.find("_maybe_ladder_escalate")
        tail_pos = body.find("_maybe_post_maker_tail")
        self.assertGreater(
            ladder_pos, -1,
            "_submit_taker must invoke _maybe_ladder_escalate.")
        self.assertGreater(
            tail_pos, -1,
            "_submit_taker must still invoke _maybe_post_maker_tail "
            "(coexistence is the spec — escalation does not replace "
            "the tail).")
        self.assertLess(
            ladder_pos, tail_pos,
            "_maybe_ladder_escalate must be called BEFORE "
            "_maybe_post_maker_tail in _submit_taker. Ordering matters: "
            "active reach first, then passive rest.")


# ────────────────────────────────────────────────────────────────────
# Class 6: telemetry
# ────────────────────────────────────────────────────────────────────


class TestTelemetry(unittest.TestCase):

    def setUp(self):
        # LADDER_ESCALATION_ENABLED defaults to OFF (codebase pattern).
        # Tests in this class assert behavior when ENABLED — patch ON.
        self._enabled_patch = patch("bot.executor.LADDER_ESCALATION_ENABLED", True)
        self._enabled_patch.start()
        self.addCleanup(self._enabled_patch.stop)
        self.ex = _make_executor()

    def test_session_counter_exists(self):
        """We need to count escalation attempts in the session for
        post-deploy validation. Mirrors _session_maker_tails_posted."""
        self.assertTrue(
            hasattr(self.ex, "_session_ladder_escalations")
            or hasattr(self.ex, "_session_ladder_escalations_attempted"),
            "OrderExecutor must expose a session counter for ladder "
            "escalations so post-deploy verification can confirm the "
            "feature is firing.")


# ────────────────────────────────────────────────────────────────────
# Class 7: AST shape
# ────────────────────────────────────────────────────────────────────


class TestAstShape(unittest.TestCase):

    def test_helper_method_exists_on_executor(self):
        with open(BOT_PY) as f:
            tree = ast.parse(f.read())
        found = False
        for cls in ast.walk(tree):
            if (not isinstance(cls, ast.ClassDef)
                    or cls.name != "OrderExecutor"):
                continue
            for fn in cls.body:
                if (isinstance(fn, ast.FunctionDef)
                        and fn.name == "_maybe_ladder_escalate"):
                    found = True
                    break
        self.assertTrue(
            found,
            "OrderExecutor must define _maybe_ladder_escalate — that's "
            "the callable test surface for every gate above.")


# ────────────────────────────────────────────────────────────────────
# Class 8: retry candidate plumbing (Round 2 — pin recursion details)
# ────────────────────────────────────────────────────────────────────


class TestRetryPlumbing(unittest.TestCase):
    """Pin the exact contents of the retry candidate passed to the
    recursive _submit_taker call. Without these, downstream defenses
    (smart-picker, drift-check, position recording) operate on wrong
    inputs."""

    def setUp(self):
        # LADDER_ESCALATION_ENABLED defaults to OFF (codebase pattern).
        # Tests in this class assert behavior when ENABLED — patch ON.
        self._enabled_patch = patch("bot.executor.LADDER_ESCALATION_ENABLED", True)
        self._enabled_patch.start()
        self.addCleanup(self._enabled_patch.stop)
        self.ex = _make_executor()

    def test_retry_strategy_is_preserved_not_renamed(self):
        """The retry must carry the ORIGINAL strategy name. Renaming
        to '*_lad1' would:
          - corrupt STRATEGY_CLAMP_POLICY lookup (retry would fall to
            STRATEGY_CLAMP_DEFAULT, breaking the no_clamp policy these
            strategies rely on).
          - corrupt STRATEGY_LIMIT_BUMP_RESERVE_CENTS lookup (retry
            uses default reserve, not strategy-tuned).
          - corrupt downstream analytics (settled_trades.strategy,
            researcher reports, dashboard breakdowns) by splitting
            attribution between original and *_lad1.
        Telemetry separation belongs in a separate field, NOT the
        strategy column."""
        cand = _candidate(strategy="terminal_momentum_98", price=98)
        captured = []

        def _fake_submit(c):
            captured.append(dict(c))  # snapshot at call time
            return {"filled_count": 5}

        self.ex._submit_taker = _fake_submit  # type: ignore
        result = self.ex._maybe_ladder_escalate(
            candidate=cand, original_limit=98, remaining=48, ioc_filled=2)
        self.assertTrue(result.get("escalated"))
        self.assertEqual(len(captured), 1)
        self.assertEqual(
            captured[0].get("strategy"), "terminal_momentum_98",
            "Retry candidate's strategy must equal the original. The "
            "_is_ladder_retry flag is the only marker — never rename.")

    def test_retry_carries_is_ladder_retry_flag(self):
        cand = _candidate(strategy="decided_t1", price=98)
        captured = []
        self.ex._submit_taker = lambda c: (
            captured.append(dict(c)) or {"filled_count": 5})
        self.ex._maybe_ladder_escalate(
            candidate=cand, original_limit=98, remaining=48, ioc_filled=2)
        self.assertTrue(
            captured[0].get("_is_ladder_retry"),
            "Retry candidate must carry _is_ladder_retry=True so "
            "recursive escalation and recursive maker-tail are "
            "blocked downstream.")

    def test_retry_position_size_equals_remaining(self):
        cand = _candidate(strategy="decided_t1", price=98, size=50)
        captured = []
        self.ex._submit_taker = lambda c: (
            captured.append(dict(c)) or {"filled_count": 5})
        self.ex._maybe_ladder_escalate(
            candidate=cand, original_limit=98, remaining=48, ioc_filled=2)
        self.assertEqual(
            captured[0].get("position_size"), 48,
            "Retry position_size must be the unfilled remainder, NOT "
            "the original Kelly target.")

    def test_retry_best_yes_ask_equals_escalated_limit(self):
        cand = _candidate(strategy="decided_t1", price=98)
        captured = []
        self.ex._submit_taker = lambda c: (
            captured.append(dict(c)) or {"filled_count": 5})
        self.ex._maybe_ladder_escalate(
            candidate=cand, original_limit=98, remaining=48, ioc_filled=2)
        self.assertEqual(
            captured[0].get("best_yes_ask"), 99,
            "Retry best_yes_ask must equal escalated limit so smart-"
            "picker / drift-check operate on the right reference.")

    def test_retry_zero_fill_does_not_subtract_from_unfilled(self):
        """When the retry submits but fills 0, escalated_filled must
        be 0 — the outer maker_tail then posts at original price for
        the full original unfilled count."""
        cand = _candidate(strategy="decided_t1", price=98)
        self.ex._submit_taker = lambda c: None  # API error / no fill
        result = self.ex._maybe_ladder_escalate(
            candidate=cand, original_limit=98, remaining=48, ioc_filled=2)
        self.assertEqual(result.get("escalated_filled", -1), 0,
                         "Retry returning None must surface 0 fills.")


# ────────────────────────────────────────────────────────────────────
# Class 9: kill switch default (Round 2 — A11)
# ────────────────────────────────────────────────────────────────────


class TestKillSwitchDefault(unittest.TestCase):
    """Codebase pattern is 'default off, opt-in via VPS env var' for
    new live features (HOURLY_LIVE_ENABLED=0, HOURLY_NO_SIDE_LIVE=0).
    Ladder escalation must follow the same pattern."""

    def test_default_when_env_var_unset_is_off(self):
        """If LADDER_ESCALATION_ENABLED env var is NOT set, the flag
        defaults to False. VPS must explicitly opt in via env."""
        # This is testable by the bot module's import-time evaluation
        # AND by reading the source's default literal.
        # Bit 3.1: LADDER_ESCALATION_ENABLED definition lives in
        # bot/constants.py post-extraction. Concatenate both sources so
        # the regex finds the assignment regardless of which file it lives in.
        with open(BOT_PY) as f:
            src = f.read()
        constants_path = os.path.join(os.path.dirname(BOT_PY), "constants.py")
        if os.path.exists(constants_path):
            with open(constants_path) as f:
                src += "\n" + f.read()
        # Match the env-var default-string literal.
        # We accept either "0" or False — but must NOT default to "1".
        # The pattern in the file is:
        #   os.environ.get("LADDER_ESCALATION_ENABLED", "<default>") == "1"
        import re
        m = re.search(
            r'LADDER_ESCALATION_ENABLED\s*=\s*\(\s*\n?\s*'
            r'os\.environ\.get\(\s*"LADDER_ESCALATION_ENABLED",\s*"([01])"\s*\)',
            src)
        self.assertIsNotNone(
            m, "LADDER_ESCALATION_ENABLED must be defined via "
            "os.environ.get with an explicit default string.")
        self.assertEqual(
            m.group(1), "0",
            "LADDER_ESCALATION_ENABLED must default to '0' (off). "
            "Codebase pattern is opt-in for new live features. "
            "Ship with explicit VPS env var to enable.")


# ────────────────────────────────────────────────────────────────────
# Class 10: ob_snapshot freshness on retry (Round 2 — A6)
# ────────────────────────────────────────────────────────────────────


class TestRetryObSnapshotFreshness(unittest.TestCase):
    """The shallow dict() copy shares ob_snapshot by reference. The
    retry's downstream clamp/PHANTOM_ABORT/drift-check logic reads
    candidate['ob_snapshot']['ask_depth'] which was computed for the
    ORIGINAL price level. We must either refresh it for the +1¢ level
    or explicitly mark the retry to skip those gates."""

    def setUp(self):
        # LADDER_ESCALATION_ENABLED defaults to OFF (codebase pattern).
        # Tests in this class assert behavior when ENABLED — patch ON.
        self._enabled_patch = patch("bot.executor.LADDER_ESCALATION_ENABLED", True)
        self._enabled_patch.start()
        self.addCleanup(self._enabled_patch.stop)
        self.ex = _make_executor()

    def test_retry_either_refreshes_ob_snapshot_or_marks_skip(self):
        """Retry candidate must NOT carry the parent's ob_snapshot
        unmodified — the depth value applies to the wrong price level
        and would mis-fire PHANTOM_ABORT / drift-check.

        Acceptable outcomes:
          (a) ob_snapshot replaced/updated on the retry candidate
              (e.g., re-fetched for +1¢ level, or set to None to
              disable depth-based gates), OR
          (b) candidate carries an explicit flag like
              _skip_drift_check=True that downstream gates honor.
        """
        cand = _candidate(strategy="terminal_momentum_98", price=98)
        # Tag the original ob_snapshot so we can detect leakage.
        cand["ob_snapshot"]["_test_marker"] = "ORIGINAL"
        captured = []
        self.ex._submit_taker = lambda c: (
            captured.append(dict(c)) or {"filled_count": 5})
        self.ex._maybe_ladder_escalate(
            candidate=cand, original_limit=98, remaining=48, ioc_filled=2)
        retry_cand = captured[0]
        retry_ob = retry_cand.get("ob_snapshot") or {}
        # Either the snapshot is None / refreshed (no marker), OR
        # an explicit skip flag is set.
        leaked = retry_ob.get("_test_marker") == "ORIGINAL"
        skip_flag = retry_cand.get("_skip_drift_check") is True
        snapshot_refreshed = not leaked
        self.assertTrue(
            snapshot_refreshed or skip_flag,
            "Retry candidate must NOT inherit the parent's ob_snapshot "
            "unmodified (the depth applies to the wrong price level). "
            "Either refresh it for +1¢ or set _skip_drift_check=True.")


# ────────────────────────────────────────────────────────────────────
# Class 11: ticker risk cap re-check on retry (Round 2 — A1 partial)
# ────────────────────────────────────────────────────────────────────


class TestRetryRiskCapRecheck(unittest.TestCase):
    """Per-ticker and per-window risk caps are checked in execute()
    before the parent IOC fires. The retry adds size to the same
    ticker — must re-check that aggregate exposure stays inside cap.
    A retry that tipped over MAX_TICKER_RISK would silently bypass
    the architectural exposure ceiling."""

    def setUp(self):
        # LADDER_ESCALATION_ENABLED defaults to OFF (codebase pattern).
        # Tests in this class assert behavior when ENABLED — patch ON.
        self._enabled_patch = patch("bot.executor.LADDER_ESCALATION_ENABLED", True)
        self._enabled_patch.start()
        self.addCleanup(self._enabled_patch.stop)
        self.ex = _make_executor()

    def test_retry_aborts_when_ticker_cap_would_exceed(self):
        """If parent's just-recorded position + retry's intended cost
        > balance * MAX_TICKER_RISK, the retry must abort."""
        cand = _candidate(strategy="terminal_momentum_98", price=98,
                          size=50, asset="ETH")
        # Simulate: balance=100k, parent already recorded 2ct@98=$1.96
        # filled. Retry would add 48ct@99=$47.52. Total=$49.48.
        # MAX_TICKER_RISK=25% → cap=$25k. Far under. Should escalate.
        # Now mock a HUGE pre-existing position that would already be
        # at-cap.
        self.ex._state.get_open_positions = MagicMock(return_value=[
            {"ticker": cand["ticker"], "total_cost_cents": 2_500_000},
        ])
        # MAX_TICKER_RISK=0.25 × 100k = 25000 → in cents = 2_500_000.
        # Existing already at cap. Retry must skip.
        called = []
        self.ex._submit_taker = lambda c: (
            called.append(dict(c)) or {"filled_count": 5})
        result = self.ex._maybe_ladder_escalate(
            candidate=cand, original_limit=98, remaining=48, ioc_filled=2)
        self.assertFalse(
            result.get("escalated"),
            "Retry must abort when adding remaining*escalated_limit "
            "would push ticker exposure past MAX_TICKER_RISK.")
        self.assertEqual(
            len(called), 0,
            "Retry must NOT call _submit_taker when cap would exceed.")

    def test_retry_proceeds_when_well_under_ticker_cap(self):
        cand = _candidate(strategy="terminal_momentum_98", price=98,
                          size=50, asset="ETH")
        # No prior positions. Retry cost is small.
        self.ex._state.get_open_positions = MagicMock(return_value=[])
        called = []
        self.ex._submit_taker = lambda c: (
            called.append(dict(c)) or {"filled_count": 5})
        result = self.ex._maybe_ladder_escalate(
            candidate=cand, original_limit=98, remaining=48, ioc_filled=2)
        self.assertTrue(result.get("escalated"))
        self.assertEqual(len(called), 1)


# ────────────────────────────────────────────────────────────────────
# Class 12: Round 3 fixes — PHANTOM_ABORT, counters, boundary tests
# ────────────────────────────────────────────────────────────────────


class TestPhantomAbortOnRetry(unittest.TestCase):
    """A20 — Setting ob_snapshot=None silently disabled the upstream
    PHANTOM_ABORT guard. The retry path is precisely when this guard
    matters most: we just consumed top-of-book, +1¢ level may be
    empty too. _maybe_ladder_escalate must perform an explicit
    pre-retry depth check or refresh the snapshot, not silently
    bypass."""

    def setUp(self):
        self._enabled_patch = patch("bot.executor.LADDER_ESCALATION_ENABLED", True)
        self._enabled_patch.start()
        self.addCleanup(self._enabled_patch.stop)
        self.ex = _make_executor()

    def test_retry_aborts_when_escalated_level_has_zero_depth(self):
        """If a fresh REST/orderbook check on the escalated level
        shows zero depth, retry must abort (no submit)."""
        cand = _candidate(strategy="terminal_momentum_98", price=98)
        # Mock the orderbook fetch to return a book with 0 contracts
        # at 99c (the escalated level).
        self.ex._client.get_orderbook = MagicMock(return_value={
            "yes": [[100, 1]],  # only 100c has any depth; 99c is empty
            "no": [],
        })
        called = []
        self.ex._submit_taker = lambda c: (
            called.append(dict(c)) or {"filled_count": 5})
        result = self.ex._maybe_ladder_escalate(
            candidate=cand, original_limit=98, remaining=48, ioc_filled=2)
        self.assertFalse(
            result.get("escalated"),
            "Pre-retry phantom check must abort when escalated level "
            "shows zero depth. Otherwise we'd submit IOCs into empty "
            "books — the exact bug PHANTOM_ABORT exists to prevent.")
        self.assertEqual(
            len(called), 0,
            "_submit_taker must NOT be invoked when retry aborts on "
            "phantom check.")


class TestRetryCounterNotDoubleCount(unittest.TestCase):
    """A22 — _session_ioc_fills and _session_ioc_unfilled are signal-
    level counters. The retry IS a new IOC, but it's the same trading
    signal continuing — counting it as separate would inflate the
    'IOCs fired today' metric. Mirror the existing 'confirmation_addon'
    exclusion pattern."""

    def setUp(self):
        self._enabled_patch = patch("bot.executor.LADDER_ESCALATION_ENABLED", True)
        self._enabled_patch.start()
        self.addCleanup(self._enabled_patch.stop)

    def test_submit_taker_excludes_ladder_retry_from_session_counter(self):
        """AST regression: _submit_taker's `_session_ioc_fills += 1`
        must be guarded by NOT _is_ladder_retry, mirroring the
        existing entry_path != 'confirmation_addon' guard."""
        with open(BOT_PY) as f:
            tree = ast.parse(f.read())
        submit_fn = None
        for cls in ast.walk(tree):
            if (not isinstance(cls, ast.ClassDef)
                    or cls.name != "OrderExecutor"):
                continue
            for fn in cls.body:
                if (isinstance(fn, ast.FunctionDef)
                        and fn.name == "_submit_taker"):
                    submit_fn = fn
                    break
        self.assertIsNotNone(submit_fn)
        src = ast.unparse(submit_fn)
        # Heuristic: any `self._session_ioc_fills += 1` and
        # `self._session_ioc_unfilled += 1` must be near a guard that
        # references `_is_ladder_retry`. We don't enforce exact
        # placement; we enforce the guard string is present alongside.
        self.assertIn(
            "_is_ladder_retry", src,
            "_submit_taker must reference _is_ladder_retry to guard "
            "session counters from double-counting on ladder retries.")


class TestTickerCapBoundary(unittest.TestCase):
    """A21 — strengthen the ticker cap test with precise boundary."""

    def setUp(self):
        self._enabled_patch = patch("bot.executor.LADDER_ESCALATION_ENABLED", True)
        self._enabled_patch.start()
        self.addCleanup(self._enabled_patch.stop)
        self.ex = _make_executor()

    def test_retry_aborts_at_exact_cap_minus_one_cent(self):
        """Pin the off-by-one: existing cost = cap - retry_cost + 1c
        must abort. Verifies the comparison is `>` not `>=`, and the
        units (cents on both sides)."""
        # balance=100,000 cents = $1,000.
        # MAX_TICKER_RISK = 0.25 → cap = 25,000 cents.
        # retry_cost = 48 ct × 99c = 4,752 cents.
        # Set existing = cap - retry_cost + 1 = 25_000 - 4_752 + 1 = 20_249.
        # Retry would push total to 25_001 > 25_000 → must abort.
        cand = _candidate(strategy="terminal_momentum_98", price=98,
                          size=50, asset="ETH")
        cand["balance_at_scan"] = 100_000  # cents
        self.ex._state.get_open_positions = MagicMock(return_value=[
            {"ticker": cand["ticker"], "total_cost_cents": 20_249},
        ])
        called = []
        self.ex._submit_taker = lambda c: (
            called.append(dict(c)) or {"filled_count": 5})
        # Mock orderbook so phantom check passes (depth at 99c = 200).
        self.ex._client.get_orderbook = MagicMock(return_value={
            "yes": [[99, 200], [100, 1]], "no": []})
        result = self.ex._maybe_ladder_escalate(
            candidate=cand, original_limit=98, remaining=48, ioc_filled=2)
        self.assertFalse(
            result.get("escalated"),
            "Existing 20249c + retry 4752c = 25001c > 25000c cap → "
            "must abort. If this passes, the cap arithmetic is wrong "
            "(off-by-one or unit mismatch).")

    def test_retry_proceeds_at_exact_cap(self):
        """Companion: existing = cap - retry_cost exactly → must
        proceed (boundary inclusive on the safe side)."""
        cand = _candidate(strategy="terminal_momentum_98", price=98,
                          size=50, asset="ETH")
        cand["balance_at_scan"] = 100_000
        self.ex._state.get_open_positions = MagicMock(return_value=[
            {"ticker": cand["ticker"], "total_cost_cents": 20_248},
        ])
        # 20_248 + 4_752 = 25_000 == cap (not over). Should proceed.
        self.ex._client.get_orderbook = MagicMock(return_value={
            "yes": [[99, 200], [100, 1]], "no": []})
        called = []
        self.ex._submit_taker = lambda c: (
            called.append(dict(c)) or {"filled_count": 5})
        result = self.ex._maybe_ladder_escalate(
            candidate=cand, original_limit=98, remaining=48, ioc_filled=2)
        self.assertTrue(
            result.get("escalated"),
            "Existing + retry == cap exactly: must proceed (gate is "
            "strict `>` not `>=`).")


class TestCounterIncrements(unittest.TestCase):
    """A26 — verify _session_ladder_escalations actually increments on
    attempt. Without this, a regression that drops the `+= 1` ships
    invisibly past TestTelemetry's hasattr check."""

    def setUp(self):
        self._enabled_patch = patch("bot.executor.LADDER_ESCALATION_ENABLED", True)
        self._enabled_patch.start()
        self.addCleanup(self._enabled_patch.stop)
        self.ex = _make_executor()

    def test_counter_increments_on_successful_attempt(self):
        cand = _candidate(strategy="terminal_momentum_98", price=98)
        self.ex._client.get_orderbook = MagicMock(return_value={
            "yes": [[99, 200], [100, 1]], "no": []})
        self.ex._submit_taker = lambda c: {"filled_count": 5}
        self.assertEqual(self.ex._session_ladder_escalations, 0)
        self.ex._maybe_ladder_escalate(
            candidate=cand, original_limit=98, remaining=48, ioc_filled=2)
        self.assertEqual(
            self.ex._session_ladder_escalations, 1,
            "Counter must increment by 1 per attempt. Pin the value, "
            "not just hasattr.")

    def test_counter_does_not_increment_on_skipped_attempt(self):
        """Gate skip (e.g., kill switch off) must NOT increment the
        counter — counter measures attempts that go through, not
        every call to the helper."""
        cand = _candidate(strategy="terminal_momentum_98", price=98)
        with patch("bot.executor.LADDER_ESCALATION_ENABLED", False):
            self.ex._maybe_ladder_escalate(
                candidate=cand, original_limit=98, remaining=48,
                ioc_filled=2)
        self.assertEqual(
            self.ex._session_ladder_escalations, 0,
            "Skipped attempts must not increment the attempt counter.")


# ────────────────────────────────────────────────────────────────────
# Class 13: Round 4 — orderbook shape robustness (A30, A35, A38)
# ────────────────────────────────────────────────────────────────────


class TestOrderbookShapeRobustness(unittest.TestCase):
    """Production client.get_orderbook returns wrapped responses
    (Kalshi 2026 schema: {'orderbook': {'yes': [...], 'no': [...]}}).
    Older / WS-cache returns unwrap form ({'yes': [...]}). Gate 8
    must handle both, plus all defensive cases (None, {}, malformed
    levels, exceptions). Per CLAUDE.md: 'Kalshi silently renames
    REST keys; dump raw responses before 4th debug commit'."""

    def setUp(self):
        self._enabled_patch = patch("bot.executor.LADDER_ESCALATION_ENABLED", True)
        self._enabled_patch.start()
        self.addCleanup(self._enabled_patch.stop)
        self.ex = _make_executor()
        self._cand = _candidate(strategy="terminal_momentum_98", price=98)
        self._called = []
        self.ex._submit_taker = lambda c: (
            self._called.append(dict(c)) or {"filled_count": 5})

    def _run(self):
        return self.ex._maybe_ladder_escalate(
            candidate=self._cand, original_limit=98, remaining=48,
            ioc_filled=2)

    def test_wrapped_orderbook_response_works(self):
        """Production schema: {'orderbook': {'yes': [...], 'no': [...]}}.
        Gate 8 must unwrap (mirror prod pattern at bot/_impl.py:15841)."""
        self.ex._client.get_orderbook.return_value = {
            "orderbook": {
                "yes": [[99, 200], [100, 1]],
                "no": [],
            }
        }
        result = self._run()
        self.assertTrue(
            result.get("escalated"),
            "Wrapped {'orderbook': {...}} must unwrap to depth=200 "
            "and proceed.")

    def test_orderbook_fp_response_works(self):
        """Current Kalshi FP schema (Mar 2026 migration):
        {'orderbook_fp': {'yes_dollars': [['0.99','48.00'],...]}}.
        Without this unwrap path, every escalation silently aborts
        because top-level 'yes' is absent — feature dies in prod."""
        self.ex._client.get_orderbook.return_value = {
            "orderbook_fp": {
                "yes_dollars": [["0.99", "48.00"], ["1.00", "1.00"]],
                "no_dollars": [],
            }
        }
        result = self._run()
        self.assertTrue(
            result.get("escalated"),
            "FP-format must convert to yes=[[99, 48], [100, 1]] and "
            "proceed. Mirrors prod unwrap at bot/_impl.py:15810-15812 + "
            "16419-16421.")

    def test_unwrapped_orderbook_response_works(self):
        """WS-cache / older path: {'yes': [...]} directly. Both forms
        must work."""
        self.ex._client.get_orderbook.return_value = {
            "yes": [[99, 200]], "no": []}
        result = self._run()
        self.assertTrue(result.get("escalated"))

    def test_none_orderbook_aborts(self):
        """API failure / circuit breaker → None. Fail-closed."""
        self.ex._client.get_orderbook.return_value = None
        result = self._run()
        self.assertFalse(
            result.get("escalated"),
            "None orderbook = no fresh data → fail-closed skip.")
        self.assertEqual(len(self._called), 0)

    def test_empty_dict_orderbook_aborts(self):
        self.ex._client.get_orderbook.return_value = {}
        result = self._run()
        self.assertFalse(result.get("escalated"))

    def test_empty_yes_list_aborts(self):
        """Book exists but yes side is empty (rare but possible)."""
        self.ex._client.get_orderbook.return_value = {
            "yes": [], "no": []}
        result = self._run()
        self.assertFalse(
            result.get("escalated"),
            "Empty yes list = 0 depth → abort.")

    def test_yes_none_aborts(self):
        """Kalshi schema drift: 'yes' key present but value is None.
        Pattern called out in feedback_kalshi_schema_drift.md."""
        self.ex._client.get_orderbook.return_value = {
            "yes": None, "no": []}
        result = self._run()
        self.assertFalse(result.get("escalated"))

    def test_malformed_levels_skipped_not_raised(self):
        """Levels missing qty (e.g. [[99]]) or with non-int values
        must be skipped without raising. Other valid levels still
        count."""
        self.ex._client.get_orderbook.return_value = {
            "yes": [
                [99],              # missing qty — skip
                ["bad", 50],       # non-int price — skip
                [99, "bad"],       # non-int qty — skip
                [99, 100],         # valid — counts
                [],                # empty list — skip
            ],
            "no": [],
        }
        result = self._run()
        self.assertTrue(
            result.get("escalated"),
            "Malformed levels skipped silently; valid level (qty=100) "
            "passes the check.")

    def test_exception_during_orderbook_fetch_aborts(self):
        """Network error / breaker raises → fail-closed skip."""
        self.ex._client.get_orderbook.side_effect = RuntimeError("api err")
        result = self._run()
        self.assertFalse(
            result.get("escalated"),
            "Exception in fetch → fail-closed.")
        self.assertEqual(len(self._called), 0)

    def test_thin_nonzero_depth_at_limit_proceeds(self):
        """A37/A39: depth in [1, remaining-1] is the original phantom
        bug pattern (thin top of book). Currently Gate 8 only checks
        depth > 0. This test pins that behavior — we accept thin
        liquidity at the escalated level. If this is later tightened
        to require depth >= remaining, this test will fail and force
        a deliberate revisit."""
        self.ex._client.get_orderbook.return_value = {
            "yes": [[99, 3]], "no": []}  # only 3 of 48 wanted
        result = self._run()
        self.assertTrue(
            result.get("escalated"),
            "Current spec: depth>0 is sufficient. Submitting IOC for "
            "48 contracts when only 3 are available is acceptable — "
            "Kalshi's match engine will fill 3 and auto-cancel the "
            "rest. Maker tail then handles the residual.")


# ────────────────────────────────────────────────────────────────────
# Class 14: Round 4 — multi-skip-path counter coverage (A32)
# ────────────────────────────────────────────────────────────────────


class TestCounterSkipPaths(unittest.TestCase):
    """A32 — verify _session_ladder_escalations does NOT increment on
    any of the gate skip paths. Otherwise dashboards overcount."""

    def setUp(self):
        self._enabled_patch = patch("bot.executor.LADDER_ESCALATION_ENABLED", True)
        self._enabled_patch.start()
        self.addCleanup(self._enabled_patch.stop)
        self.ex = _make_executor()

    def _assert_no_increment(self, candidate, original_limit, remaining,
                             ioc_filled):
        before = self.ex._session_ladder_escalations
        self.ex._maybe_ladder_escalate(
            candidate=candidate, original_limit=original_limit,
            remaining=remaining, ioc_filled=ioc_filled)
        self.assertEqual(
            self.ex._session_ladder_escalations, before,
            "Counter must not increment on skipped attempt.")

    def test_no_increment_on_ineligible_strategy(self):
        cand = _candidate(strategy="lpne")
        self._assert_no_increment(cand, 85, 20, 30)

    def test_no_increment_on_zero_ioc_filled(self):
        cand = _candidate(strategy="terminal_momentum_98")
        self._assert_no_increment(cand, 98, 50, 0)

    def test_no_increment_on_remainder_under_min(self):
        cand = _candidate(strategy="terminal_momentum_98")
        self._assert_no_increment(cand, 98, 4, 46)

    def test_no_increment_on_recursion_guard(self):
        cand = _candidate(strategy="terminal_momentum_98")
        cand["_is_ladder_retry"] = True
        self._assert_no_increment(cand, 98, 48, 2)

    def test_no_increment_on_price_cap(self):
        cand = _candidate(strategy="decided_t2", price=96)
        self._assert_no_increment(cand, 96, 48, 2)

    def test_no_increment_on_ticker_cap_exceeded(self):
        cand = _candidate(strategy="terminal_momentum_98", price=98)
        self.ex._state.get_open_positions = MagicMock(return_value=[
            {"ticker": cand["ticker"], "total_cost_cents": 2_500_000}])
        self._assert_no_increment(cand, 98, 48, 2)

    def test_no_increment_on_phantom_abort(self):
        cand = _candidate(strategy="terminal_momentum_98", price=98)
        self.ex._client.get_orderbook = MagicMock(return_value={
            "yes": [[100, 1]], "no": []})  # no depth at <=99
        self._assert_no_increment(cand, 98, 48, 2)


if __name__ == "__main__":
    unittest.main()
