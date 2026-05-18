"""Regression for B4 (86b9zudcc, 2026-05-18): WIN-side settlement balance-delta
cross-check.

The 2026-05-18 HYPE incident (kb/failures/ghost-fill-retry-overcount-may18.md)
exposed that ``SettlementTracker._process_settlement`` reads ``aggregate_count``
and ``combined_pnl`` from the local positions table, formats them straight
into the Telegram alert, and never asks Kalshi what cash actually moved.

This regression pins B4's WIN-side cross-check at the settlement reporting
boundary:

  - Capture pre-balance at the start of ``_process_settlement``.
  - Re-use the post-settlement ``get_balance()`` call already at the
    Telegram-alert site.
  - Compare ``balance_delta = post - pre`` against the locally-expected
    settlement credit:
      * WIN  → ``aggregate_count * 100`` (yes-side and no-side both pay
        100¢/contract at WIN settlement)
      * LOSS → ``0`` (no cash moves at LOSS settle)
  - If ``abs(expected_credit - balance_delta) > threshold``, log a
    ``SETTLEMENT_PNL_DIVERGENCE`` WARNING + append a ⚠️ ``KALSHI_DELTA=``
    tag to the alert.

LOSS-side over-counts (the actual HYPE incident shape) are structurally
invisible to this surface because LOSS settlements produce 0¢ cash motion.
The retroactive ``scripts/audit/phantom_pnl_audit.py`` (also part of B4)
covers the LOSS-side gap via Kalshi fills.

RCA discipline tied to this regression:
  - L99 ratchet: the original comparison ``balance_delta vs (combined_pnl -
    combined_fee)`` was rejected at R1-C1 because pnl is lifecycle-cumulative
    while balance_delta is settlement-window-only — the two cannot align in
    real settlements. The retracted comparison and its phrases are NOT
    revived anywhere in this test or the production code.
  - L105: balance_delta is a window-scoped API delta; pairing it with a
    cumulative local sink would have been a textbook L105 violation.

These tests are TDD-RED contracts (would fail if the threshold constant
were removed, the cross-check call site were removed, or the alert tag
were renamed):
"""
import logging
import os
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(
    0,
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
)

import bot.constants  # noqa: E402
import bot.notifier as _notifier  # noqa: E402
import bot.settlement  # noqa: E402
import bot.state  # noqa: E402


class _RecordingHandler(logging.Handler):
    """Captures all emitted records for inspection (assertNoLogs is 3.10+)."""

    def __init__(self, level=logging.DEBUG):
        super().__init__(level)
        self.records: list = []

    def emit(self, record):
        self.records.append(record)


class TestSettlementBalanceDeltaCrossCheck(unittest.TestCase):
    """B4 WIN-side settlement balance-delta cross-check regression."""

    def _fresh_state(self):
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        self.addCleanup(os.unlink, tmp.name)
        sm = bot.state.StateManager(db_path=tmp.name)
        sm.conn.executescript("""
            DROP TABLE IF EXISTS positions;
            CREATE TABLE positions (
                ticker TEXT,
                event_ticker TEXT NOT NULL,
                asset TEXT NOT NULL,
                side TEXT NOT NULL,
                count INTEGER NOT NULL,
                avg_price_cents INTEGER NOT NULL,
                total_cost_cents INTEGER NOT NULL,
                opened_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                strategy TEXT,
                seconds_to_close REAL,
                fill_latency_seconds REAL,
                vol_regime TEXT,
                calibrated_prob REAL,
                edge REAL,
                kelly_f REAL,
                is_taker INTEGER,
                fill_source TEXT,
                execution_method TEXT,
                escalation_type TEXT,
                maker_price_cents INTEGER,
                maker_wait_seconds REAL,
                strategy_group TEXT DEFAULT 'main',
                is_stacked INTEGER DEFAULT 0,
                accumulated_fee_cents INTEGER DEFAULT 0,
                PRIMARY KEY (ticker, strategy_group)
            );
        """)
        sm.conn.commit()
        return sm

    def _seed_open(self, sm, *, ticker, count, avg, side="yes", asset="HYPE",
                   strategy_group="main", is_taker=1):
        sm.conn.execute(
            "INSERT INTO positions (ticker, event_ticker, asset, side, count, "
            "avg_price_cents, total_cost_cents, opened_at, updated_at, status, "
            "strategy_group, is_taker) VALUES "
            "(?, ?, ?, ?, ?, ?, ?, '2026-05-18T00:00:00Z', "
            "'2026-05-18T00:00:00Z', 'open', ?, ?)",
            (ticker, "KXHYPE15M-EVT", asset, side, count, avg,
             count * avg, strategy_group, is_taker),
        )
        sm.conn.commit()

    def _build_tracker(self, sm, *, balance_sequence):
        """SettlementTracker with mocked client.get_balance returning the
        given sequence (one item per call)."""
        client = MagicMock()
        client.get_balance.side_effect = list(balance_sequence)
        # Empty fills so the LOSS-side cross-check (the
        # ``if outcome == "LOSS" and len(positions) == 1 and
        # positions[0].get("is_taker"):`` block in ``_process_settlement``)
        # short-circuits without further interaction.
        client.get_fills.return_value = {"fills": []}
        # No expiration_value follow-up.
        client.get_market.return_value = None
        logger = MagicMock()
        logger.log_settlement = MagicMock()
        logger.log_rejection = MagicMock()
        tracker = bot.settlement.SettlementTracker(client, sm, logger,
                                                    main_loop=None)
        return tracker, client

    # ── Constant presence ────────────────────────────────────────────────

    def test_divergence_threshold_constant_exists_and_is_positive(self):
        """SETTLEMENT_PNL_DIVERGENCE_THRESHOLD_CENTS must be wired into
        bot.constants and be a positive int (cents).
        """
        self.assertTrue(
            hasattr(bot.constants, "SETTLEMENT_PNL_DIVERGENCE_THRESHOLD_CENTS"),
            "B4 fix must add SETTLEMENT_PNL_DIVERGENCE_THRESHOLD_CENTS "
            "to bot.constants",
        )
        threshold = bot.constants.SETTLEMENT_PNL_DIVERGENCE_THRESHOLD_CENTS
        self.assertIsInstance(threshold, int)
        self.assertGreater(threshold, 0)

    # ── Divergent WIN — primary regression ───────────────────────────────

    def test_divergent_multi_row_win_fires_warning_and_tags_alert(self):
        """Multi-row WIN with phantom-inflated local count: 2 stacked
        strategy_groups sum to 100ct locally, but Kalshi credits only 50ct.

        The existing single-row WIN-side count-mismatch check inside
        ``SettlementTracker._process_settlement`` — the
        ``if revenue > 0 and outcome == "WIN" and side == "yes":`` block —
        auto-corrects single-row mismatches via the ``elif len(positions)
        == 1:`` branch (UPDATE positions SET count=implied_count). After
        that auto-correction fires, ``aggregate_count`` equals
        Kalshi-truth, so the balance-delta check would see
        ``expected_credit == balance_delta`` → quiet. The balance-delta
        check therefore covers the **multi-row gap** (the
        ``SETTLEMENT_MULTI_MISMATCH`` arm just warns and never mutates),
        which is exactly the 2026-05-18 HYPE incident shape.

        The cross-check must:
          - log SETTLEMENT_PNL_DIVERGENCE (expected_credit=10000¢ vs
            balance_delta=5000¢ → divergence=+5000¢)
          - append ⚠️ KALSHI_DELTA= tag carrying the real +$50.00 figure
          - NOT suppress the alert
        """
        sm = self._fresh_state()
        # Two strategy_groups summing to 100ct YES @ 80¢. Kalshi credits
        # 5000¢ ($50.00) — implies real Kalshi count = 50ct.
        self._seed_open(sm, ticker="KXHYPE15M-T1", count=60, avg=80,
                        strategy_group="terminal_momentum_98")
        self._seed_open(sm, ticker="KXHYPE15M-T1", count=40, avg=80,
                        strategy_group="decided_t1")
        # Pre = $50.00 (5000¢), Post = $100.00 (10000¢) → balance_delta = +5000¢.
        # Expected credit (LOCAL aggregate) = 100 × 100 = 10000¢.
        # divergence = expected − delta = 10000 − 5000 = +5000¢ → flag.
        tracker, client = self._build_tracker(
            sm, balance_sequence=[
                {"balance": 5000},
                {"balance": 10000},
                {"balance": 10000},
                {"balance": 10000},
            ],
        )

        telegram = MagicMock()
        handler = _RecordingHandler()
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            with patch.object(_notifier, "_TELEGRAM", telegram):
                tracker._process_settlement({
                    "ticker": "KXHYPE15M-T1",
                    "market_result": "yes",  # YES-side WIN
                    "revenue": 5000,         # Kalshi truth: only paid 50ct × 100
                    "settled_time": "2026-05-18T05:30:00Z",
                })
        finally:
            root.removeHandler(handler)

        log_text = "\n".join(r.getMessage() for r in handler.records)
        self.assertIn(
            "SETTLEMENT_PNL_DIVERGENCE", log_text,
            f"WIN with phantom count must log SETTLEMENT_PNL_DIVERGENCE; "
            f"got:\n{log_text}")

        # Telegram alert MUST have fired (don't suppress).
        self.assertTrue(telegram.send.called,
                        "divergent settlement must still send the Telegram alert")
        sent_text = telegram.send.call_args.args[0]
        self.assertIn(
            "KALSHI_DELTA", sent_text,
            f"divergent alert must carry KALSHI_DELTA tag, got: {sent_text!r}")
        # Real balance movement +$50.00 must appear in the divergence tag.
        self.assertIn(
            "+$50.00", sent_text,
            f"divergence tag must surface real balance delta +$50.00, "
            f"got: {sent_text!r}")

    # ── Aligned WIN — happy-path negative ────────────────────────────────

    def test_aligned_win_does_not_warn_or_tag(self):
        """WIN with matching counts: local 50ct @ 80¢, Kalshi credits 5000¢.
        balance_delta = +5000¢, expected_credit = 5000¢ → divergence = 0¢.
        No warn; alert plain.
        """
        sm = self._fresh_state()
        self._seed_open(sm, ticker="KXHYPE15M-T2", count=50, avg=80)
        tracker, client = self._build_tracker(
            sm, balance_sequence=[
                {"balance": 0},
                {"balance": 5000},
                {"balance": 5000},
                {"balance": 5000},
            ],
        )

        telegram = MagicMock()
        handler = _RecordingHandler()
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            with patch.object(_notifier, "_TELEGRAM", telegram):
                tracker._process_settlement({
                    "ticker": "KXHYPE15M-T2",
                    "market_result": "yes",
                    "revenue": 5000,
                    "settled_time": "2026-05-18T05:30:00Z",
                })
        finally:
            root.removeHandler(handler)

        log_text = "\n".join(r.getMessage() for r in handler.records)
        self.assertNotIn(
            "SETTLEMENT_PNL_DIVERGENCE", log_text,
            "aligned WIN must NOT trigger divergence warning")
        self.assertTrue(telegram.send.called)
        sent_text = telegram.send.call_args.args[0]
        self.assertNotIn(
            "KALSHI_DELTA", sent_text,
            f"aligned alert must not carry divergence tag, got: {sent_text!r}")

    # ── LOSS — structurally below the surface ────────────────────────────

    def test_loss_with_steady_balance_does_not_warn(self):
        """LOSS produces $0 cash motion at settle: balance_delta = 0,
        expected_credit = 0 → divergence = 0 → no warn. This pins the
        intentional gap: LOSS-side phantoms are invisible to this surface
        (covered retroactively by scripts/audit/phantom_pnl_audit.py).
        """
        sm = self._fresh_state()
        self._seed_open(sm, ticker="KXHYPE15M-T3", count=100, avg=98)
        tracker, client = self._build_tracker(
            sm, balance_sequence=[
                {"balance": 4000},
                {"balance": 4000},
                {"balance": 4000},
                {"balance": 4000},
            ],
        )

        telegram = MagicMock()
        handler = _RecordingHandler()
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            with patch.object(_notifier, "_TELEGRAM", telegram):
                tracker._process_settlement({
                    "ticker": "KXHYPE15M-T3",
                    "market_result": "no",   # YES-side → LOSS
                    "revenue": 0,
                    "settled_time": "2026-05-18T05:30:00Z",
                })
        finally:
            root.removeHandler(handler)

        log_text = "\n".join(r.getMessage() for r in handler.records)
        self.assertNotIn(
            "SETTLEMENT_PNL_DIVERGENCE", log_text,
            "LOSS with steady balance must NOT fire divergence (this surface "
            "doesn't catch LOSS-side phantoms; phantom_pnl_audit does)")
        sent_text = telegram.send.call_args.args[0]
        self.assertNotIn("KALSHI_DELTA", sent_text)

    # ── API failure tolerance ────────────────────────────────────────────

    def test_balance_api_failure_skips_check_without_suppressing_alert(self):
        """If get_balance returns None at either capture point, the cross-check
        must short-circuit gracefully — no crash, no false-positive warning,
        and the Telegram alert MUST still fire (per ticket: don't suppress
        the report)."""
        sm = self._fresh_state()
        self._seed_open(sm, ticker="KXHYPE15M-T4", count=50, avg=80)
        client = MagicMock()
        client.get_balance.return_value = None
        client.get_fills.return_value = {"fills": []}
        client.get_market.return_value = None
        logger = MagicMock()
        logger.log_settlement = MagicMock()
        logger.log_rejection = MagicMock()
        tracker = bot.settlement.SettlementTracker(client, sm, logger,
                                                    main_loop=None)

        telegram = MagicMock()
        handler = _RecordingHandler()
        root = logging.getLogger()
        root.addHandler(handler)
        try:
            with patch.object(_notifier, "_TELEGRAM", telegram):
                tracker._process_settlement({
                    "ticker": "KXHYPE15M-T4",
                    "market_result": "yes",
                    "revenue": 5000,
                    "settled_time": "2026-05-18T05:30:00Z",
                })
        finally:
            root.removeHandler(handler)

        log_text = "\n".join(r.getMessage() for r in handler.records)
        self.assertNotIn(
            "SETTLEMENT_PNL_DIVERGENCE", log_text,
            "missing balance must NOT mis-fire divergence warning")
        self.assertTrue(telegram.send.called,
                        "missing balance must NOT suppress the Telegram alert")
        sent_text = telegram.send.call_args.args[0]
        self.assertNotIn(
            "KALSHI_DELTA", sent_text,
            "missing balance must NOT add a divergence tag")


if __name__ == "__main__":
    unittest.main()
