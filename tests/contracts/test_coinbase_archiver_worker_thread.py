"""D2.2 — CoinbaseArchiver applies D1.3-fu4 worker-thread decouple from
day 1 (ticket 86b9zkppk, 2026-05-17).

Background: the Kalshi side initially shipped BronzeArchiver._on_frame
doing inline writer.write on the asyncio thread (D1.2). Under burst
load the loop blocked past the keepalive-ping-timeout window → WS lib
raised 1011 → reconnect storm. D1.3-fu4 closed that class by moving
build_envelope + writer.write onto a dedicated worker thread.

D2.2 applies the lesson FROM DAY ONE rather than re-paying the cost
on the Coinbase side. The same invariants must hold:

  1. Constructor accepts ``write_queue_maxsize`` with a sensible default
     (≥ 1000 — enough buffering for typical bursts).
  2. ``self._write_queue`` is a bounded ``queue.Queue`` (NOT unbounded).
  3. ``_on_frame`` calls ``put_nowait`` (NEVER blocking ``put`` without
     ``block=False``). A blocking put on a stalled queue would re-stall
     the asyncio loop.
  4. ``_on_frame`` catches ``queue.Full`` and bumps a drop counter.
  5. ``start()`` spawns a daemon worker thread.
  6. ``stop()`` posts a sentinel + joins the worker (pending frames flush
     before shutdown).
  7. The worker survives ``writer.write`` exceptions (one bad writer
     cannot stall the rest of the bronze pipeline).
  8. Functional: ``_on_frame`` returns in single-digit ms even when the
     writer blocks for seconds (asyncio loop liveness).
"""
from __future__ import annotations

import ast
import inspect
import queue
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock


REPO_ROOT = Path(__file__).resolve().parents[2]
COINBASE_ARCHIVER_PATH = REPO_ROOT / "collector" / "coinbase_archiver.py"


# ─── Test fixture helpers ────────────────────────────────────────────────────


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


def _fake_frame(msg_type, *, raw=None, wire_recv_ts=1_700_000_000.0,
                parsed=None):
    import json
    from coinbase_wire.ws_client import Frame
    if parsed is None:
        parsed = {"type": msg_type} if msg_type is not None else {}
    if raw is None:
        raw = json.dumps(parsed)
    return Frame(
        wire_recv_ts=wire_recv_ts,
        raw=raw,
        parsed=parsed,
        channel=None,
        msg_type=msg_type,
        sequence_num=None,
    )


def _wait_for(predicate, *, timeout=5.0, interval=0.005):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


# ─── 1. Constructor: write_queue_maxsize kwarg + bounded queue ───────────────


def test_coinbase_archiver_accepts_write_queue_maxsize_kwarg():
    from collector.coinbase_archiver import CoinbaseArchiver
    sig = inspect.signature(CoinbaseArchiver.__init__)
    assert "write_queue_maxsize" in sig.parameters
    default = sig.parameters["write_queue_maxsize"].default
    assert isinstance(default, int) and default >= 1000, (
        f"write_queue_maxsize default must be ≥ 1000 (a defensive floor; "
        f"the production default is 10K which gives ~30-50s buffering "
        f"at the post-D2.5 reconciled ~200-300 frames/sec aggregate load "
        f"per the R0 reachability spike — see "
        f"collector/coinbase_archiver.py queue-capacity comment); got "
        f"default={default!r}."
    )


def test_coinbase_archiver_allocates_bounded_write_queue(monkeypatch):
    archiver, _, _ = _make_archiver(monkeypatch, write_queue_maxsize=5)
    assert hasattr(archiver, "_write_queue")
    q = archiver._write_queue
    assert isinstance(q, queue.Queue)
    assert q.maxsize == 5


def test_coinbase_archiver_rejects_unbounded_queue(monkeypatch):
    """queue.Queue(maxsize<=0) is UNBOUNDED in stdlib semantics —
    defeats the backpressure invariant. Reject at construction.
    """
    from collector.coinbase_archiver import CoinbaseArchiver
    import pytest as _pytest
    fake_wire = MagicMock(name="WSClient")
    from collector import coinbase_archiver as ca
    monkeypatch.setattr(ca, "WSClient", MagicMock(return_value=fake_wire))
    writers = {None: MagicMock()}
    with _pytest.raises(ValueError):
        CoinbaseArchiver(
            writers_by_channel=writers,
            conn_id="A",
            write_queue_maxsize=0,
        )


def test_coinbase_archiver_initial_dropped_frames_zero(monkeypatch):
    archiver, _, _ = _make_archiver(monkeypatch)
    assert hasattr(archiver, "_dropped_frames")
    assert archiver._dropped_frames == 0


# ─── 2. AST: _on_frame uses put_nowait (NEVER blocking put) ──────────────────


def test_on_frame_uses_put_nowait_not_blocking_put():
    """AST guard: ``_on_frame`` MUST call ``put_nowait`` (or ``put`` with
    ``block=False``) on the write queue, NEVER blocking ``put``. A
    blocking put would re-introduce the asyncio stall this design is
    meant to eliminate from day-1.
    """
    src = COINBASE_ARCHIVER_PATH.read_text()
    tree = ast.parse(src)

    on_frame_func = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_on_frame":
            on_frame_func = node
            break
    assert on_frame_func is not None, (
        "_on_frame method not found in collector/coinbase_archiver.py."
    )

    put_nowait_calls: list[int] = []
    blocking_put_calls: list[int] = []
    for node in ast.walk(on_frame_func):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "put_nowait":
                put_nowait_calls.append(node.lineno)
            elif node.func.attr == "put":
                block_false = False
                for kw in node.keywords:
                    if kw.arg == "block":
                        if (isinstance(kw.value, ast.Constant)
                                and kw.value.value is False):
                            block_false = True
                if not block_false:
                    blocking_put_calls.append(node.lineno)

    assert put_nowait_calls, (
        "_on_frame does not call .put_nowait(...) on the write queue. "
        "D2.2 must apply the D1.3-fu4 worker-thread pattern from day-1."
    )
    assert not blocking_put_calls, (
        f"_on_frame calls .put(...) WITHOUT block=False at lines "
        f"{blocking_put_calls}. Blocking put re-introduces the 1011 "
        f"stall class — use put_nowait."
    )


def test_on_frame_catches_queue_full():
    """AST guard: ``_on_frame`` MUST handle ``queue.Full``."""
    src = COINBASE_ARCHIVER_PATH.read_text()
    tree = ast.parse(src)

    on_frame_func = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_on_frame":
            on_frame_func = node
            break
    assert on_frame_func is not None

    found_full_handler = False
    for node in ast.walk(on_frame_func):
        if isinstance(node, ast.ExceptHandler):
            etype = node.type
            if etype is None:  # bare except
                found_full_handler = True
                break
            if (isinstance(etype, ast.Attribute)
                    and etype.attr == "Full"):
                found_full_handler = True
                break
            if isinstance(etype, ast.Name) and etype.id == "Full":
                found_full_handler = True
                break
    assert found_full_handler, (
        "_on_frame does not catch queue.Full — unhandled exception → "
        "bubbles into WSClient → reconnect."
    )


# ─── 3. Functional: _on_frame returns quickly when worker blocks ─────────────


def test_on_frame_returns_quickly_when_writer_blocks(monkeypatch):
    """Whole point of the worker thread: even if writer.write blocks for
    seconds (slow disk / locked file), the asyncio thread MUST return
    from ``_on_frame`` in single-digit ms.
    """
    archiver, _, writers = _make_archiver(
        monkeypatch, write_queue_maxsize=10_000,
    )
    archiver.start()
    try:
        block_event = threading.Event()
        writers["ticker"].write.side_effect = (
            lambda _env: block_event.wait(timeout=5)
        )
        frame = _fake_frame(
            "ticker",
            parsed={"type": "ticker", "product_id": "BTC-USD"},
        )
        t0 = time.monotonic()
        archiver._on_frame(frame)
        elapsed = time.monotonic() - t0
        assert elapsed < 0.1, (
            f"_on_frame took {elapsed*1000:.1f}ms — should be ≤ 100ms "
            f"even with writer blocked."
        )
        block_event.set()
    finally:
        archiver.stop()


# ─── 4. Functional: worker drains queue → frames written ─────────────────────


def test_worker_drains_queue_frames_written(monkeypatch):
    archiver, _, writers = _make_archiver(monkeypatch)
    archiver.start()
    try:
        for i in range(5):
            archiver._on_frame(_fake_frame(
                "ticker",
                parsed={"type": "ticker", "product_id": "BTC-USD",
                        "sequence": i},
            ))
        assert _wait_for(
            lambda: writers["ticker"].write.call_count >= 5,
            timeout=2.0,
        ), (
            f"worker did not drain 5 frames within 2s — got "
            f"{writers['ticker'].write.call_count} writes."
        )
    finally:
        archiver.stop()


def test_frames_written_in_monotonic_seq_order(monkeypatch):
    """FIFO queue + single worker + lock-held seq allocation preserves
    monotonic ``_collector_seq`` ordering on the write side.
    """
    archiver, _, writers = _make_archiver(monkeypatch)
    archiver.start()
    try:
        N = 50
        for i in range(N):
            archiver._on_frame(_fake_frame(
                "ticker",
                parsed={"type": "ticker", "product_id": "BTC-USD",
                        "sequence": i},
            ))
        assert _wait_for(
            lambda: writers["ticker"].write.call_count >= N,
            timeout=2.0,
        )
        seqs = [
            call.args[0]["_collector_seq"]
            for call in writers["ticker"].write.call_args_list
        ]
        assert seqs == sorted(seqs), (
            f"writer received envelopes out of seq order: {seqs}."
        )
    finally:
        archiver.stop()


# ─── 5. Backpressure: drop counter on queue full ─────────────────────────────


def test_full_queue_drops_frame_and_increments_counter(monkeypatch):
    archiver, _, writers = _make_archiver(
        monkeypatch, write_queue_maxsize=2,
    )
    archiver.start()
    try:
        # Stall the writer; queue will fill at maxsize=2 then drop.
        block_event = threading.Event()
        writers["ticker"].write.side_effect = (
            lambda _env: block_event.wait(timeout=10)
        )
        N = 20
        EXPECTED_MIN_DROPS = N - 3  # 1 inflight + 2 queue = 3 absorbed
        for i in range(N):
            archiver._on_frame(_fake_frame(
                "ticker",
                parsed={"type": "ticker", "product_id": "BTC-USD",
                        "sequence": i + 100},
            ))
        assert archiver._dropped_frames >= EXPECTED_MIN_DROPS, (
            f"queue maxsize=2 + blocked worker + {N} frame burst should "
            f"have dropped at least {EXPECTED_MIN_DROPS} frames; got "
            f"_dropped_frames={archiver._dropped_frames}."
        )
        block_event.set()
    finally:
        archiver.stop()


# ─── 6. Lifecycle: start spawns worker, stop joins it ────────────────────────


def test_start_spawns_worker_thread(monkeypatch):
    archiver, fake_wire, _ = _make_archiver(monkeypatch)
    archiver.start()
    try:
        assert hasattr(archiver, "_write_worker")
        worker = archiver._write_worker
        assert isinstance(worker, threading.Thread)
        assert worker.is_alive()
        fake_wire.start.assert_called_once()
    finally:
        archiver.stop()


def test_stop_joins_worker_thread(monkeypatch):
    archiver, fake_wire, _ = _make_archiver(monkeypatch)
    archiver.start()
    worker = archiver._write_worker
    archiver.stop()
    assert not worker.is_alive(), (
        "Worker thread still alive after stop() — must signal + join."
    )
    fake_wire.stop.assert_called_once()


def test_shutdown_drains_pending_queue(monkeypatch):
    """Buffered frames at stop() must drain before exit."""
    archiver, _, writers = _make_archiver(monkeypatch)
    archiver.start()
    try:
        gate = threading.Event()
        writers["ticker"].write.side_effect = (
            lambda _env: gate.wait(timeout=5)
        )
        N = 30
        for i in range(N):
            archiver._on_frame(_fake_frame(
                "ticker",
                parsed={"type": "ticker", "product_id": "BTC-USD",
                        "sequence": i + 200},
            ))
        time.sleep(0.05)
        writers["ticker"].write.side_effect = None
        gate.set()
    finally:
        archiver.stop()
    final_count = writers["ticker"].write.call_count
    assert final_count >= N, (
        f"shutdown drain incomplete: wrote {final_count}, expected ≥ {N}."
    )


# ─── 7. Worker resilience: writer exceptions don't kill worker ───────────────


def test_worker_survives_writer_exception(monkeypatch):
    archiver, _, writers = _make_archiver(monkeypatch)
    archiver.start()
    try:
        writers["ticker"].write.side_effect = [
            RuntimeError("simulated disk full"),
            None, None, None, None,
        ]
        for i in range(5):
            archiver._on_frame(_fake_frame(
                "ticker",
                parsed={"type": "ticker", "product_id": "BTC-USD",
                        "sequence": i + 300},
            ))
        assert _wait_for(
            lambda: writers["ticker"].write.call_count >= 5,
            timeout=2.0,
        ), (
            f"worker stalled after exception — only "
            f"{writers['ticker'].write.call_count} writes."
        )
        assert archiver._write_worker.is_alive()
    finally:
        archiver.stop()
