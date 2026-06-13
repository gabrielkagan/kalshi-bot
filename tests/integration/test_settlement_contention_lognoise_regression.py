"""Settlement-side db-contention log-noise regression (2026-06-13).

Follow-up to PR #169 (commit b1f0b3a8), which suppressed the contention
traceback at the three StateManager WARNING-envelope writers. Post-deploy
verification over a full 1h40m window revealed the DOMINANT journalctl
traceback source was NOT those writers (0 tracebacks, 28 clean one-liners)
but the settlement poller. TWO sinks were gated:

1. `_poll_rejections` (dominant, 157 of 164): `mark_rejection_settled`
   RE-RAISES the chronic single-writer/cursor-race contention up through
   `_process_rejection_settlement`, and the per-ticker `except Exception`
   logged it with `exc_info=True`. That handler wraps BOTH a network call
   (`client.get_market`, genuine → keep stack) AND the settle re-raise
   (contention → suppress), so the classifier is the right gate.
2. `_poll_evaluated_opportunities` Phase-2 chunk-commit handler
   (`eval_opp_settlement batch commit failed`, 1 of 164): wraps
   `mark_evaluated_opportunity_settled` + `conn.commit()`. Its Phase-1
   per-ticker handler (`Evaluated opp settlement check failed`) does NO DB
   writes, so it is intentionally left with full stacks for genuine bugs.

Fix: classify with the shipped `bot.state._is_known_db_contention(e)` and
pass `exc_info=not _is_known_db_contention(e)` at the two contention sinks —
known contention logs the one-liner without a stack, everything else
(network/genuine bug) keeps its traceback. Observability-only; control
flow unchanged.

Pre-fix RED: the contention case logs with a truthy exc_info → the
`not record.exc_info` assertion fails.
"""
from __future__ import annotations

import logging
import sqlite3
from unittest.mock import MagicMock

import pytest

from bot.settlement import SettlementTracker


def _make_tracker() -> SettlementTracker:
    """Minimal tracker — client/state/logger are MagicMocks; the handlers
    under test only touch _client.get_market + _process_rejection_settlement
    (which we override per-test to inject the exception class)."""
    return SettlementTracker(MagicMock(), MagicMock(), MagicMock())


# ─── 1. _poll_rejections — dominant source (157/164) ─────────────────────


def test_rejection_poll_contention_logs_without_traceback(caplog):
    """A `database is locked` re-raised from the settle path must log the
    one-liner WITHOUT a traceback (the chronic contention class is handled
    + accounted; the stack is pure journalctl noise at ~95/hr)."""
    t = _make_tracker()
    t._pending_rejection_tickers = {"KXBTC15M-26JUN130400-00"}
    t._settled_rejection_tickers = set()
    t._client.get_market.return_value = {"market": {"result": "yes"}}
    t._process_rejection_settlement = MagicMock(
        side_effect=sqlite3.OperationalError("database is locked")
    )

    with caplog.at_level(logging.WARNING):
        t._poll_rejections()  # must NOT raise

    recs = [r for r in caplog.records
            if "Rejection settlement check failed" in r.getMessage()]
    assert recs, "expected the per-ticker rejection-settle WARNING"
    assert all(not r.exc_info for r in recs), (
        "known DB contention must log the one-liner without a traceback"
    )


def test_rejection_poll_unexpected_keeps_traceback(caplog):
    """A non-contention exception (e.g. a network/parse bug) MUST keep its
    traceback — guards against over-broad suppression."""
    t = _make_tracker()
    t._pending_rejection_tickers = {"KXBTC15M-26JUN130400-00"}
    t._settled_rejection_tickers = set()
    t._client.get_market.return_value = {"market": {"result": "yes"}}
    t._process_rejection_settlement = MagicMock(
        side_effect=ValueError("unexpected settlement parse failure")
    )

    with caplog.at_level(logging.WARNING):
        t._poll_rejections()

    recs = [r for r in caplog.records
            if "Rejection settlement check failed" in r.getMessage()]
    assert recs, "expected the per-ticker rejection-settle WARNING"
    assert all(r.exc_info for r in recs), (
        "an unexpected exception must keep its traceback — only the known "
        "contention class is suppressed"
    )


# ─── 2. Source-level pin: both poll handlers use the classifier ──────────


def test_both_settlement_poll_handlers_use_contention_classifier():
    """The two settlement contention sinks must gate exc_info on
    _is_known_db_contention — not a hardcoded exc_info=True: the
    _poll_rejections per-ticker handler ('Rejection settlement check
    failed') and the _poll_evaluated_opportunities Phase-2 chunk-commit
    handler ('eval_opp_settlement batch commit failed')."""
    import inspect
    from bot import settlement

    src = inspect.getsource(settlement)
    assert "_is_known_db_contention" in src, (
        "settlement.py must import + use the shipped contention classifier"
    )
    # The two known contention-re-raise sinks must not hardcode exc_info=True.
    for marker in ("Rejection settlement check failed",
                   "eval_opp_settlement batch commit failed"):
        idx = src.index(marker)
        window = src[idx:idx + 400]
        assert "exc_info=not _is_known_db_contention" in window, (
            f"the handler logging {marker!r} must gate exc_info on the "
            f"contention classifier"
        )
