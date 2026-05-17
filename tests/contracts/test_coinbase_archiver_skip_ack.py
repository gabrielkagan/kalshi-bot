"""D2.2 — CoinbaseArchiver applies D1.3-fu5 skip-ack-enqueue from day 1
(ticket 86b9zkppk, 2026-05-17).

Background: the Kalshi side (D1.3-fu5, ticket 86b9zky3u, 2026-05-17)
discovered that subscribe-ack frames carrying cumulative subscribed-
ticker payloads can reach ~5 MB each; queueing them at subscribe-burst
rate OOM'd the cgroup → SIGKILL → restart loop. Fix: ack frames bind
state synchronously then RETURN — they are NOT enqueued for bronze
writing.

D2.2 applies the same pattern from day-1 to the Coinbase side. Coinbase
Exchange WS sends a ``type=subscriptions`` ack on subscribe (carrying
the array of currently-subscribed channels + product_ids). The
cumulative-ack size is bounded by the configured product_id × channel
matrix and is much smaller than Kalshi's per-sid case (Coinbase's
single-conn design means the ack is once-per-session, not once-per-
subscribe-batch), but the SAME architectural pattern applies: acks
are protocol metadata, not market data, and have no silver/gold
consumer.

What this file pins:

  1. ``type=subscriptions`` ack does NOT enqueue.
  2. ``type=error`` (subscribe-failure) does NOT enqueue (also protocol
     metadata; surfacing it via log is sufficient — bronze captures
     the wire stream from the SUCCESSFUL data flow, not the
     handshake noise).
  3. Data frames AFTER an ack STILL enqueue (regression guard against
     over-broad fix).
  4. Ack does NOT consume a ``_collector_seq`` (symmetry with
     skip-enqueue — gaps in seq numbering would confuse silver QA).
  5. AST guard: ``_on_frame`` has an early-return inside the
     ``if frame.msg_type in _SUBSCRIBE_ACK_TYPES:`` branch.
  6. ``_ack_frames_processed`` observability counter increments per ack.
"""
from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import MagicMock


REPO_ROOT = Path(__file__).resolve().parents[2]
COINBASE_ARCHIVER_PATH = REPO_ROOT / "collector" / "coinbase_archiver.py"


# ─── Fixture helpers ─────────────────────────────────────────────────────────


def _make_archiver(monkeypatch, **overrides):
    from collector import coinbase_archiver as ca

    fake_wire = MagicMock(name="WSClient")
    fake_wire.is_connected = True
    monkeypatch.setattr(ca, "WSClient", MagicMock(return_value=fake_wire))

    writers = {
        None: MagicMock(name="writer_unrouted"),
        "ticker": MagicMock(name="writer_ticker"),
        "matches": MagicMock(name="writer_matches"),
        "heartbeat": MagicMock(name="writer_heartbeat"),
        "status": MagicMock(name="writer_status"),
    }
    archiver = ca.CoinbaseArchiver(
        writers_by_channel=writers,
        conn_id="A",
        **overrides,
    )
    return archiver, fake_wire, writers


def _fake_frame(parsed, *, msg_type=None, raw_override=None,
                wire_recv_ts=1_700_000_000.0):
    """Build a coinbase_wire.Frame for callback-driven dispatch.

    raw_override lets memory-regression tests substitute a multi-KB
    payload without building a giant JSON dict.
    """
    import json
    from coinbase_wire.ws_client import Frame
    raw = raw_override if raw_override is not None else json.dumps(parsed)
    return Frame(
        wire_recv_ts=wire_recv_ts,
        raw=raw,
        parsed=parsed,
        channel=None,
        msg_type=(msg_type if msg_type is not None
                  else parsed.get("type") if isinstance(parsed, dict)
                  else None),
        sequence_num=None,
    )


# ─── 1. ACK FRAMES DO NOT ENQUEUE ────────────────────────────────────────────


def test_subscriptions_ack_does_not_enqueue(monkeypatch):
    """type=subscriptions ack: queue size unchanged. Coinbase Exchange
    WS sends one of these after every (re)connect; it carries the
    cumulative {channel: [product_ids]} confirmation.
    """
    archiver, _, _ = _make_archiver(monkeypatch)
    qsize_before = archiver._write_queue.qsize()
    ack = _fake_frame({
        "type": "subscriptions",
        "channels": [
            {"name": "ticker", "product_ids": ["BTC-USD", "ETH-USD"]},
            {"name": "matches", "product_ids": ["BTC-USD", "ETH-USD"]},
        ],
    })
    archiver._on_frame(ack)
    assert archiver._write_queue.qsize() == qsize_before, (
        f"type=subscriptions ack incorrectly enqueued (queue grew from "
        f"{qsize_before}). D1.3-fu5 lesson — acks must NOT flow into "
        f"the write queue."
    )


def test_error_frame_does_not_enqueue(monkeypatch):
    """type=error (subscribe-failure ack) does NOT enqueue. Coinbase
    sends these when a subscribe fails (e.g., unauthorized channel).
    They are operational signal for the OPERATOR (log + investigate),
    not market data.
    """
    archiver, _, _ = _make_archiver(monkeypatch)
    qsize_before = archiver._write_queue.qsize()
    err = _fake_frame({
        "type": "error",
        "message": "Failed to subscribe",
        "reason": "user is not authorized for level2",
    })
    archiver._on_frame(err)
    assert archiver._write_queue.qsize() == qsize_before


# ─── 2. DATA FRAMES STILL FLOW THROUGH THE QUEUE ─────────────────────────────


def test_data_frame_after_ack_still_enqueued(monkeypatch):
    """The fix scope is narrow: ONLY ack-class frames skip enqueue. Data
    frames (ticker, match, heartbeat, status) MUST still enqueue +
    dispatch via the worker.
    """
    archiver, _, _ = _make_archiver(monkeypatch)
    archiver._on_frame(_fake_frame({
        "type": "subscriptions",
        "channels": [{"name": "ticker", "product_ids": ["BTC-USD"]}],
    }))
    qsize_after_ack = archiver._write_queue.qsize()

    archiver._on_frame(_fake_frame({
        "type": "ticker", "product_id": "BTC-USD", "price": "50000",
    }))
    qsize_after_data = archiver._write_queue.qsize()
    assert qsize_after_data == qsize_after_ack + 1, (
        f"data frame did NOT enqueue ({qsize_after_ack} → "
        f"{qsize_after_data}). Skip-ack scope must be narrow."
    )


# ─── 3. ACK DOES NOT INCREMENT collector_seq ─────────────────────────────────


def test_ack_does_not_increment_collector_seq(monkeypatch):
    """Symmetry with skip-enqueue: if the ack doesn't reach the write
    path, it MUST NOT consume a seq number (otherwise the partition
    has monotonic-seq gaps that silver QA would flag).
    """
    archiver, _, _ = _make_archiver(monkeypatch)
    seq_before = archiver._collector_seq
    archiver._on_frame(_fake_frame({
        "type": "subscriptions",
        "channels": [{"name": "ticker", "product_ids": ["BTC-USD"]}],
    }))
    assert archiver._collector_seq == seq_before, (
        f"ack incorrectly incremented collector_seq "
        f"({seq_before} → {archiver._collector_seq})."
    )


def test_data_frame_after_acks_uses_next_seq(monkeypatch):
    """5 acks (none consume seq) + 1 data frame → data frame gets seq=1."""
    archiver, _, _ = _make_archiver(monkeypatch)
    for i in range(5):
        archiver._on_frame(_fake_frame({
            "type": "subscriptions",
            "channels": [{"name": "ticker",
                          "product_ids": [f"PROD-{i}"]}],
        }))
    archiver._on_frame(_fake_frame({
        "type": "ticker", "product_id": "BTC-USD",
    }))
    assert archiver._collector_seq == 1


# ─── 4. AST GUARD: early-return inside the ack branch ────────────────────────


def test_on_frame_returns_early_inside_ack_branch():
    """AST walk: ``_on_frame`` must contain an
    ``if frame.msg_type in _SUBSCRIBE_ACK_TYPES:`` block whose body
    includes a top-level ``return``. Defense-in-depth against a future
    refactor accidentally dropping the early-return.
    """
    src = COINBASE_ARCHIVER_PATH.read_text()
    tree = ast.parse(src)

    on_frame_func = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_on_frame":
            on_frame_func = node
            break
    assert on_frame_func is not None

    ack_branches: list[ast.If] = []
    for node in ast.walk(on_frame_func):
        if isinstance(node, ast.If):
            test_src = ast.unparse(node.test) if hasattr(ast, "unparse") else ""
            if "_SUBSCRIBE_ACK_TYPES" in test_src:
                ack_branches.append(node)
    assert ack_branches, (
        "_on_frame contains no `if ... in _SUBSCRIBE_ACK_TYPES:` branch "
        "— ack-handling fundamentally broken."
    )

    found_return = False
    for branch in ack_branches:
        for stmt in branch.body:
            if isinstance(stmt, ast.Return):
                found_return = True
                break
        if found_return:
            break
    assert found_return, (
        "_on_frame's ack-branch does NOT contain an early `return`. "
        "Fall-through means acks flow into the enqueue path."
    )


# ─── 5. MEMORY REGRESSION — pre-fix would queue large acks ───────────────────


def test_many_acks_with_huge_raw_does_not_grow_queue(monkeypatch):
    """Enqueue 200 mock-acks each with a ~50 KB Frame.raw. Without skip-
    ack, the queue would hold ~10 MB; with skip-ack the queue stays
    empty.
    """
    archiver, _, _ = _make_archiver(monkeypatch)
    fat_raw = "x" * 50_000
    for i in range(200):
        archiver._on_frame(_fake_frame(
            {"type": "subscriptions",
             "channels": [{"name": "ticker",
                           "product_ids": [f"PROD-{i}"]}]},
            raw_override=fat_raw,
        ))
    assert archiver._write_queue.qsize() == 0, (
        f"200 mock-acks left queue at size "
        f"{archiver._write_queue.qsize()} (expected 0)."
    )


# ─── 6. Observability: _ack_frames_processed counter ─────────────────────────


def test_ack_frames_processed_counter_attribute_exists(monkeypatch):
    archiver, _, _ = _make_archiver(monkeypatch)
    assert hasattr(archiver, "_ack_frames_processed")
    assert archiver._ack_frames_processed == 0


def test_ack_frames_processed_counter_increments_on_ack(monkeypatch):
    archiver, _, _ = _make_archiver(monkeypatch)
    archiver._on_frame(_fake_frame({
        "type": "subscriptions",
        "channels": [{"name": "ticker", "product_ids": ["BTC-USD"]}],
    }))
    archiver._on_frame(_fake_frame({
        "type": "error", "message": "Failed",
    }))
    assert archiver._ack_frames_processed == 2, (
        f"_ack_frames_processed = {archiver._ack_frames_processed}, "
        f"expected 2 after 2 ack-class frames."
    )


def test_ack_frames_processed_counter_not_incremented_by_data_frame(monkeypatch):
    archiver, _, _ = _make_archiver(monkeypatch)
    archiver._on_frame(_fake_frame({
        "type": "ticker", "product_id": "BTC-USD",
    }))
    assert archiver._ack_frames_processed == 0
