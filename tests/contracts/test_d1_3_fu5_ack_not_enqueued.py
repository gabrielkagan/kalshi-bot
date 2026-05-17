"""D1.3-fu5 — subscribe-ack frames MUST NOT enqueue into write_queue
(ticket 86b9zky3u, 2026-05-17).

RCA: D1.3-fu4 (`86b9zk4hz`) added a bounded `queue.Queue(maxsize=10_000)`
between BronzeArchiver._on_frame (asyncio thread) and a daemon worker
thread doing build_envelope + writer.write. The maxsize bound was set
on COUNT, not BYTES. Each queue item is `(Frame, channel, seq)`.

For typical DATA frames `Frame.raw` is ~200 bytes. For Kalshi cumulative
subscribe-acks (`type=subscribed` / `type=ok`) `Frame.raw` grows to up
to 5 MB because Kalshi includes the cumulative subscribed-ticker list
in every ack (`msg.market_tickers: [...]` grows per-sid as more
subscribes succeed; cmd_id=1068 ack ≈ 5 MB).

Subscribe burst: 1068 subs × 7 conns = 7,476 acks queued before worker
can drain. Total queue memory: up to ~22 GiB worst case. With
`MemoryMax=512M` cgroup limit, kernel SIGKILLs the process → systemd
restart loop. Production hit this for ~3 hours 2026-05-17 14:07-17:15 UTC
until operator applied the band-aid (MemoryMax 1024M).

Real fix (this Bit): when `frame.msg_type in _SUBSCRIBE_ACK_TYPES`, do
the sid binding synchronously (unchanged — must stay race-free) THEN
RETURN. Acks no longer flow into the write queue.

Bronze coverage justification (per kb/failures/collector-oom-via-ack-queue-may17.md):
acks are protocol metadata (subscribe-confirmation), not market data.
No silver/gold pipeline use case consumes ack frames. Operator can
still get ack volumetrics via collector logs OR a future small ack-
stats sidecar log if needed.

Pins (this file):
  1. type=subscribed ack does NOT increment queue size
  2. type=ok ack does NOT increment queue size
  3. Sid binding STILL happens for type=subscribed (regression guard)
  4. Sid binding STILL happens for type=ok (regression guard)
  5. Data frame AFTER an ack DOES enqueue (regression guard — we only
     skip ack frames, not all frames)
  6. Ack does NOT increment _collector_seq (otherwise unrouted partition
     has seq gaps; symmetrical to skipping the write)
  7. AST guard: `_on_frame` has an early-return inside the
     `if frame.msg_type in _SUBSCRIBE_ACK_TYPES:` branch
  8. Memory-regression: enqueuing N=1000 mock-acks with 5 MB Frame.raw
     each leaves queue size 0 (pre-fix would queue all 1000 → ~5 GB)
  9. New _ack_frames_processed counter increments per-ack (observability)
"""
from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import MagicMock

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
WS_CONNECTION_PATH = REPO_ROOT / "collector" / "ws_connection.py"


# ─── Fixture helpers (mirror existing test_bronze_archiver_on_session_start
#     pattern — see that file's docstring for the WSClient-mock rationale) ──


def _make_archiver(monkeypatch, **overrides):
    from collector import ws_connection as wc

    monkeypatch.setattr(wc, "load_private_key", lambda _p: object())
    fake_wire = MagicMock(name="WSClient")
    fake_wire.is_connected = True
    monkeypatch.setattr(wc, "WSClient", MagicMock(return_value=fake_wire))

    writer_orderbook = MagicMock(name="writer_orderbook_delta")
    writer_unrouted = MagicMock(name="writer_unrouted")
    writers_by_channel = {
        None: writer_unrouted,
        "orderbook_delta": writer_orderbook,
    }
    subscribe_frames = overrides.pop("subscribe_frames", [
        {"id": 10, "cmd": "subscribe",
         "params": {"channels": ["orderbook_delta"], "market_tickers": ["T1"]}},
    ])
    cmd_id_to_channel = overrides.pop("cmd_id_to_channel", {10: "orderbook_delta"})

    archiver = wc.BronzeArchiver(
        api_key="K",
        private_key_path="/nonexistent.pem",
        writers_by_channel=writers_by_channel,
        subscribe_frames=subscribe_frames,
        cmd_id_to_channel=cmd_id_to_channel,
        conn_id="A",
        **overrides,
    )
    return archiver, fake_wire, writers_by_channel


def _fake_frame(raw_dict, *, msg_type=None, sid=None, seq=None,
                wire_recv_ts=1_700_000_000.0, raw_override=None):
    """Build a fake kalshi_wire.Frame. raw_override lets memory-regression
    tests substitute a multi-MB payload without building a giant JSON dict."""
    import json
    from kalshi_wire.ws_client import Frame
    raw = raw_override if raw_override is not None else json.dumps(raw_dict)
    return Frame(
        wire_recv_ts=wire_recv_ts,
        raw=raw,
        parsed=raw_dict,
        msg_type=msg_type if msg_type is not None else raw_dict.get("type"),
        sid=sid if sid is not None else raw_dict.get("sid"),
        seq=seq if seq is not None else raw_dict.get("seq"),
    )


# ─── 1. ACK FRAMES DO NOT ENQUEUE (the load-bearing pin) ───────────────────


def test_subscribed_ack_does_not_enqueue(monkeypatch):
    """type=subscribed ack: sid binding only; queue size unchanged.

    Pre-D1.3-fu5: queued. Post-fu5: returns after _handle_subscribe_ack
    without touching the queue. This pin closes the OOM-via-large-ack
    class documented in kb/failures/collector-oom-via-ack-queue-may17.md.
    """
    archiver, _, _ = _make_archiver(monkeypatch)
    qsize_before = archiver._write_queue.qsize()
    ack = _fake_frame({
        "id": 10, "type": "subscribed",
        "msg": {"channel": "orderbook_delta", "sid": 42},
    })
    archiver._on_frame(ack)
    qsize_after = archiver._write_queue.qsize()
    assert qsize_after == qsize_before, (
        f"type=subscribed ack incorrectly enqueued (queue grew from "
        f"{qsize_before} to {qsize_after}). D1.3-fu5 fix regression — "
        f"acks must NOT flow into the write queue (reopens OOM-via-ack "
        f"class)."
    )


def test_ok_ack_does_not_enqueue(monkeypatch):
    """type=ok ack: same as type=subscribed. type=ok is the
    POST-establishment ack shape (sid at top level vs nested for
    subscribed); both must skip the queue.
    """
    archiver, _, _ = _make_archiver(monkeypatch)
    qsize_before = archiver._write_queue.qsize()
    ack = _fake_frame({
        "id": 10, "type": "ok", "sid": 42, "seq": 1,
        "msg": {"market_tickers": ["T1", "T2"]},
    })
    archiver._on_frame(ack)
    assert archiver._write_queue.qsize() == qsize_before, (
        f"type=ok ack incorrectly enqueued. D1.3-fu5 regression."
    )


# ─── 2. SID BINDING STILL HAPPENS (regression guard) ───────────────────────


def test_subscribed_ack_still_binds_sid(monkeypatch):
    """The sid → channel binding from a type=subscribed ack MUST still
    fire (load-bearing for routing subsequent data frames). The fix only
    skips the enqueue, not the binding.
    """
    archiver, _, _ = _make_archiver(monkeypatch)
    assert archiver._sid_to_channel.get(42) is None  # pre-condition
    ack = _fake_frame({
        "id": 10, "type": "subscribed",
        "msg": {"channel": "orderbook_delta", "sid": 42},
    })
    archiver._on_frame(ack)
    assert archiver._sid_to_channel.get(42) == "orderbook_delta", (
        "sid 42 was NOT bound to orderbook_delta after type=subscribed "
        "ack. D1.3-fu5 over-broad fix — must skip queue but KEEP binding."
    )


def test_ok_ack_still_binds_sid(monkeypatch):
    """Mirror of subscribed-ack binding pin for the type=ok shape."""
    archiver, _, _ = _make_archiver(monkeypatch)
    ack = _fake_frame({
        "id": 10, "type": "ok", "sid": 99, "seq": 5,
        "msg": {"market_tickers": ["T1"]},
    })
    archiver._on_frame(ack)
    assert archiver._sid_to_channel.get(99) == "orderbook_delta", (
        "sid 99 was NOT bound after type=ok ack. D1.3-fu5 regression."
    )


# ─── 3. DATA FRAMES STILL FLOW THROUGH THE QUEUE ───────────────────────────


def test_data_frame_after_ack_still_enqueued(monkeypatch):
    """The fix scope is narrow: ONLY ack frames skip enqueue. Data
    frames (orderbook_delta, trade, market_lifecycle_v2) MUST still
    enqueue + dispatch via the worker. Regression guard against an
    over-broad fix.
    """
    archiver, _, _ = _make_archiver(monkeypatch)
    # First: bind sid via ack (does NOT enqueue per fu5).
    archiver._on_frame(_fake_frame({
        "id": 10, "type": "subscribed",
        "msg": {"channel": "orderbook_delta", "sid": 42},
    }))
    qsize_after_ack = archiver._write_queue.qsize()

    # Then: data frame (DOES enqueue).
    data = _fake_frame({
        "sid": 42, "seq": 1, "type": "orderbook_delta",
        "msg": {"market_ticker": "T1"},
    })
    archiver._on_frame(data)
    qsize_after_data = archiver._write_queue.qsize()
    assert qsize_after_data == qsize_after_ack + 1, (
        f"data frame did NOT enqueue (qsize {qsize_after_ack} → "
        f"{qsize_after_data}). D1.3-fu5 fix is too broad — only acks "
        f"should skip, not all frames."
    )


# ─── 4. ACK DOES NOT INCREMENT collector_seq ───────────────────────────────


def test_ack_does_not_increment_collector_seq(monkeypatch):
    """seq allocation symmetry: if the ack doesn't reach the write path,
    it MUST NOT consume a seq number (otherwise the _unrouted partition
    would have monotonic-seq gaps).
    """
    archiver, _, _ = _make_archiver(monkeypatch)
    seq_before = archiver._collector_seq
    archiver._on_frame(_fake_frame({
        "id": 10, "type": "subscribed",
        "msg": {"channel": "orderbook_delta", "sid": 42},
    }))
    assert archiver._collector_seq == seq_before, (
        f"ack incorrectly incremented collector_seq ({seq_before} → "
        f"{archiver._collector_seq}). Skip-enqueue must skip seq-alloc "
        f"too — otherwise data frames after acks have seq gaps that "
        f"silver QA would flag as data loss."
    )


def test_data_frame_after_ack_uses_next_unrouted_seq(monkeypatch):
    """Symmetry test: after N acks (which don't allocate seq) + 1 data
    frame, the data frame gets seq=1 (not N+1). This pins the symmetry
    of test_ack_does_not_increment_collector_seq.
    """
    archiver, _, _ = _make_archiver(monkeypatch)
    # 5 acks (none should consume seq)
    for i in range(5):
        archiver._on_frame(_fake_frame({
            "id": 10 + i, "type": "subscribed",
            "msg": {"channel": "orderbook_delta", "sid": 42 + i},
        }))
    # Now 1 data frame
    archiver._on_frame(_fake_frame({
        "sid": 42, "seq": 1, "type": "orderbook_delta",
        "msg": {"market_ticker": "T1"},
    }))
    assert archiver._collector_seq == 1, (
        f"5 acks + 1 data frame produced collector_seq="
        f"{archiver._collector_seq}, expected 1. Acks must skip "
        f"seq-alloc; data frame is the first seq-consumer."
    )


# ─── 5. AST GUARD: early-return inside the ack branch ──────────────────────


def test_on_frame_returns_early_inside_ack_branch():
    """AST walk: `_on_frame` must contain an `if frame.msg_type in
    _SUBSCRIBE_ACK_TYPES:` block whose body includes a top-level
    `return` statement. Defense-in-depth against a future refactor
    accidentally dropping the early-return.
    """
    src = WS_CONNECTION_PATH.read_text()
    tree = ast.parse(src)

    on_frame_func = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_on_frame":
            on_frame_func = node
            break
    assert on_frame_func is not None, (
        "_on_frame method not found in collector/ws_connection.py — "
        "either the method was renamed (sister tests will break) OR "
        "this AST guard's lookup is stale."
    )

    # Find any `if` whose test references _SUBSCRIBE_ACK_TYPES.
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

    # At least one ack branch must contain a top-level Return.
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
        "D1.3-fu5 regression — fall-through means acks flow into the "
        "enqueue path → reopens OOM-via-ack class. See "
        "kb/failures/collector-oom-via-ack-queue-may17.md."
    )


# ─── 6. MEMORY REGRESSION — pre-fix would OOM ──────────────────────────────


def test_thousand_mock_acks_with_huge_raw_does_not_grow_queue(monkeypatch):
    """The reproduction-of-RCA test. Enqueue 1000 mock-acks each with
    Frame.raw the size of a real cumulative-subscribe ack (~5 MB; we use
    a smaller 50 KB payload here to keep the test fast — the SHAPE is
    what matters, not the byte count). Pre-fix: queue grows to 1000 ×
    ~50 KB = ~50 MB. Post-fix: queue stays empty.
    """
    archiver, _, _ = _make_archiver(monkeypatch)
    # 50 KB "ack body" — same SHAPE as a multi-MB cumulative ack but
    # smaller so the test runs fast.
    fat_raw = "x" * 50_000
    for i in range(1000):
        archiver._on_frame(_fake_frame(
            {"id": 100 + i, "type": "ok", "sid": 1000 + i, "seq": 1,
             "msg": {"market_tickers": []}},  # parsed dict stays small
            raw_override=fat_raw,
        ))
    qsize = archiver._write_queue.qsize()
    assert qsize == 0, (
        f"1000 mock-acks left queue at size {qsize} (expected 0). "
        f"Each ack at ~50 KB × 1000 = ~50 MB held in queue memory. "
        f"D1.3-fu5 regression — reopens OOM class."
    )


# ─── 7. Observability: _ack_frames_processed counter ──────────────────────


def test_ack_frames_processed_counter_attribute_exists(monkeypatch):
    """Per fu5 spec: add `_ack_frames_processed` counter that
    increments per-ack. Mirrors the `_dropped_frames` pattern for
    queue-overflow observability. Lets operator distinguish 'collector
    is healthy + receiving acks normally' from 'collector wedged + no
    activity at all'.
    """
    archiver, _, _ = _make_archiver(monkeypatch)
    assert hasattr(archiver, "_ack_frames_processed"), (
        "BronzeArchiver must expose self._ack_frames_processed "
        "(observability counter post-D1.3-fu5)."
    )
    assert archiver._ack_frames_processed == 0


def test_ack_frames_processed_counter_increments_on_ack(monkeypatch):
    archiver, _, _ = _make_archiver(monkeypatch)
    archiver._on_frame(_fake_frame({
        "id": 10, "type": "subscribed",
        "msg": {"channel": "orderbook_delta", "sid": 42},
    }))
    archiver._on_frame(_fake_frame({
        "id": 11, "type": "ok", "sid": 43, "seq": 1,
        "msg": {"market_tickers": ["T1"]},
    }))
    assert archiver._ack_frames_processed == 2, (
        f"_ack_frames_processed = {archiver._ack_frames_processed}, "
        f"expected 2 after 2 ack-class frames."
    )


def test_ack_frames_processed_counter_not_incremented_by_data_frame(monkeypatch):
    """Defensive: data frames do NOT increment the ack counter."""
    archiver, _, _ = _make_archiver(monkeypatch)
    archiver._on_frame(_fake_frame({
        "sid": 42, "seq": 1, "type": "orderbook_delta",
        "msg": {"market_ticker": "T1"},
    }))
    assert archiver._ack_frames_processed == 0
