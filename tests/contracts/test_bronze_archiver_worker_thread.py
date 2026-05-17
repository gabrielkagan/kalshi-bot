"""D1.3-fu4 — ``BronzeArchiver._on_frame`` decoupled from JSONL write via
worker thread (ticket 86b9zk4hz, 2026-05-17).

Predecessor: D1.3-fu3 raised ``ping_timeout`` from 10s → 30s as a stopgap
against the 1011 keepalive-ping-timeout storm. Production verified the
stopgap INSUFFICIENT — 82 × 1011 errors in 32 min, collector self-restart
every ~30 min, D1.6 health monitor firing ~288 alerts/day.

Root cause (RCA): post-D1.3-fu1's ``ws_max_size`` uncap, Kalshi sends 3-4
MiB subscribe-ack messages over 7 concurrent WS connections. The asyncio
thread does ALL of:

  1. Frame parse (kalshi_wire pre-populates ``Frame.parsed``)
  2. sid → channel lookup (lock-held)
  3. collector_seq allocation (lock-held)
  4. ``build_envelope(...)`` (JSON re-serialize for envelope)
  5. ``writer.write(envelope)`` (zstd compress + disk IO)

Steps 4-5 are heavy. Under burst load (subscribe storm OR backlog drain)
the loop blocks > 30s → ping pong cycle misses → WS lib raises 1011 →
reconnect. Reconnect re-subscribes → bigger ack → bigger drain → loop.

Fix: introduce a bounded ``queue.Queue`` + single worker thread. Steps
1-3 stay synchronous (sid binding race-free + monotonic seq preserved).
Steps 4-5 are enqueued + worker drains. On queue full, drop + counter
+ throttled warning. Shutdown: drain via sentinel + join.

Pins:

  1. ``BronzeArchiver.__init__`` accepts ``write_queue_maxsize`` kwarg with
     a sensible default (≥ 1000 frames; 10s+ buffering at typical load).
  2. The constructor allocates a ``queue.Queue`` with that maxsize on
     ``self._write_queue`` (or equivalent attribute).
  3. ``_on_frame`` MUST call ``self._write_queue.put_nowait(...)`` (NOT
     blocking put) and MUST catch ``queue.Full`` — otherwise a stalled
     worker re-blocks the asyncio loop, defeating the whole fix.
  4. A drop counter (``self._dropped_frames`` or equiv) increments when
     queue.Full fires.
  5. ``start()`` spawns a daemon worker thread that drains
     ``self._write_queue``.
  6. ``stop()`` signals the worker (sentinel) + joins so pending frames
     flush before shutdown.
  7. The worker handles ``writer.write`` exceptions WITHOUT dying (one
     bad writer cannot stall the rest of the bronze pipeline).
  8. Functional: ``_on_frame`` returns in single-digit ms even when the
     writer blocks for seconds (asyncio loop liveness).
  9. Functional: frames are written in monotonic ``collector_seq`` order
     (single FIFO worker + lock-held seq allocation preserve ordering).
 10. Functional: subscribe-ack handling stays synchronous in ``_on_frame``
     (sid→channel binding must be visible to subsequent data frames
     dispatched within the same asyncio tick; deferring to the worker
     would race).

NOT pinned here (the D1.3-fu3 stopgap pin lives at
``test_collector_ws_client_ping_timeout.py`` — ping_timeout=30s stays as
defense-in-depth post-D1.3-fu4).
"""
from __future__ import annotations

import ast
import inspect
import queue
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
WS_CONNECTION_PATH = REPO_ROOT / "collector" / "ws_connection.py"


# ─── Test fixture helpers ───────────────────────────────────────────────────


def _make_archiver(monkeypatch, **overrides):
    """Mirror of test_bronze_archiver_on_session_start::_make_archiver.

    Returns (archiver, fake_wire, writers_by_channel). The underlying
    ``WSClient`` is mocked so the asyncio thread never spawns; the worker
    thread (the D1.3-fu4 subject) IS real and is started/stopped per-test.
    """
    from collector import ws_connection as wc

    monkeypatch.setattr(wc, "load_private_key", lambda _p: object())
    fake_wire = MagicMock(name="WSClient")
    fake_wire.is_connected = True
    monkeypatch.setattr(wc, "WSClient", MagicMock(return_value=fake_wire))

    writer_orderbook = MagicMock(name="writer_orderbook_delta")
    writer_trade = MagicMock(name="writer_trade")
    writer_unrouted = MagicMock(name="writer_unrouted")
    writers_by_channel = {
        None: writer_unrouted,
        "orderbook_delta": writer_orderbook,
        "trade": writer_trade,
    }
    subscribe_frames = overrides.pop("subscribe_frames", [
        {"id": 10, "cmd": "subscribe",
         "params": {"channels": ["orderbook_delta"], "market_tickers": ["T1"]}},
    ])
    cmd_id_to_channel = overrides.pop("cmd_id_to_channel", {
        10: "orderbook_delta",
    })

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
                wire_recv_ts=1_700_000_000.0):
    import json
    from kalshi_wire.ws_client import Frame
    raw = json.dumps(raw_dict)
    return Frame(
        wire_recv_ts=wire_recv_ts,
        raw=raw,
        parsed=raw_dict,
        msg_type=msg_type if msg_type is not None else raw_dict.get("type"),
        sid=sid if sid is not None else raw_dict.get("sid"),
        seq=seq if seq is not None else raw_dict.get("seq"),
    )


def _bind_sid(archiver, cmd_id, sid, channel="orderbook_delta"):
    """Trigger the subscribe-ack path so sid→channel is bound."""
    ack = _fake_frame(
        {"id": cmd_id, "type": "subscribed",
         "msg": {"channel": channel, "sid": sid}},
    )
    archiver._on_frame(ack)


# ─── 1. Constructor surface — write_queue_maxsize kwarg + queue attribute ──


def test_bronze_archiver_accepts_write_queue_maxsize_kwarg():
    """Constructor exposes ``write_queue_maxsize`` so callers/tests can
    tune the buffering window. Default must exist (callers shouldn't
    have to know the magic number)."""
    from collector.ws_connection import BronzeArchiver
    sig = inspect.signature(BronzeArchiver.__init__)
    assert "write_queue_maxsize" in sig.parameters, (
        "BronzeArchiver.__init__ must accept write_queue_maxsize as a "
        "keyword argument so the worker-queue capacity is tunable + the "
        "default is discoverable from the signature."
    )
    default = sig.parameters["write_queue_maxsize"].default
    assert isinstance(default, int) and default >= 1000, (
        f"write_queue_maxsize default must be ≥ 1000 (≥ 10s of buffering at "
        f"~100 frames/sec/conn); got default={default!r}. Lower values risk "
        f"unnecessary drops on transient bursts (subscribe storms, backlog "
        f"drain post-reconnect)."
    )


def test_bronze_archiver_allocates_bounded_write_queue(monkeypatch):
    """``__init__`` constructs ``self._write_queue`` as a bounded
    ``queue.Queue`` with the configured maxsize. Bounded is load-bearing:
    an unbounded queue would let backlog grow to OOM under sustained
    overload — the whole point of the fix is to bound + drop.
    """
    archiver, _, _ = _make_archiver(monkeypatch, write_queue_maxsize=5)
    assert hasattr(archiver, "_write_queue"), (
        "BronzeArchiver must allocate self._write_queue in __init__ — the "
        "worker drains from this queue."
    )
    q = archiver._write_queue
    assert isinstance(q, queue.Queue), (
        f"self._write_queue should be a queue.Queue; got {type(q)!r}."
    )
    assert q.maxsize == 5, (
        f"queue.Queue.maxsize did not propagate from write_queue_maxsize "
        f"kwarg: maxsize={q.maxsize}, expected 5."
    )


def test_bronze_archiver_initial_dropped_frames_zero(monkeypatch):
    """Drop counter starts at 0 so callers can monitor the deltabetween
    health-check ticks rather than booting from an unknown floor."""
    archiver, _, _ = _make_archiver(monkeypatch)
    assert hasattr(archiver, "_dropped_frames"), (
        "BronzeArchiver must expose self._dropped_frames so the queue-full "
        "drop class is observable (callers/health monitors read it)."
    )
    assert archiver._dropped_frames == 0


# ─── 2. AST: _on_frame uses put_nowait (NEVER blocking put) ────────────────


def test_on_frame_uses_put_nowait_not_blocking_put():
    """AST guard: ``_on_frame`` MUST call ``put_nowait`` (or ``put`` with
    block=False) on the write queue, NEVER ``put`` with default blocking
    semantics. A blocking ``put`` would re-introduce exactly the asyncio
    stall this Bit is meant to eliminate.
    """
    src = WS_CONNECTION_PATH.read_text()
    tree = ast.parse(src)

    on_frame_func = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_on_frame":
            on_frame_func = node
            break
    assert on_frame_func is not None, (
        "_on_frame method not found in collector/ws_connection.py — sister "
        "tests depend on this method name; D1.3-fu4 must not rename it."
    )

    put_nowait_calls: list[int] = []
    blocking_put_calls: list[int] = []
    for node in ast.walk(on_frame_func):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "put_nowait":
                put_nowait_calls.append(node.lineno)
            elif node.func.attr == "put":
                # Allow only if block=False is explicitly passed; otherwise
                # the default block=True re-introduces the stall class.
                block_false = False
                for kw in node.keywords:
                    if kw.arg == "block":
                        if (isinstance(kw.value, ast.Constant)
                                and kw.value.value is False):
                            block_false = True
                if not block_false:
                    blocking_put_calls.append(node.lineno)

    assert put_nowait_calls, (
        "_on_frame does not call .put_nowait(...) on the write queue. The "
        "D1.3-fu4 contract REQUIRES non-blocking enqueue from the asyncio "
        "thread — a blocking put_nowait absent + no block=False put means "
        "either the queue isn't used (defeats the fix) OR a blocking put "
        "was used (re-introduces the stall)."
    )
    assert not blocking_put_calls, (
        f"_on_frame calls .put(...) WITHOUT block=False at lines "
        f"{blocking_put_calls}. A blocking put on a bounded queue can stall "
        f"the asyncio loop indefinitely if the worker is slow — re-opens "
        f"the 1011 storm class. Use put_nowait (or put(block=False)) and "
        f"handle queue.Full explicitly."
    )


def test_on_frame_catches_queue_full():
    """AST guard: ``_on_frame`` MUST handle ``queue.Full`` — otherwise
    an unhandled exception on a full queue would bubble back into
    ``WSClient`` and trigger a reconnect (re-opens the storm class).
    """
    src = WS_CONNECTION_PATH.read_text()
    tree = ast.parse(src)

    on_frame_func = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_on_frame":
            on_frame_func = node
            break
    assert on_frame_func is not None

    # Walk the function body for any ExceptHandler whose exception type
    # is `queue.Full`, `Full`, or a bare `except` (last resort).
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
        "_on_frame does not catch queue.Full (or any exception) around the "
        "enqueue site. Unhandled exception → bubbles into WSClient → "
        "reconnect → storm class re-opens. Add an `except queue.Full:` "
        "block that increments the drop counter."
    )


# ─── 3. Functional: _on_frame returns quickly when worker blocks ───────────


def test_on_frame_returns_quickly_when_writer_blocks(monkeypatch):
    """The whole point of the worker thread: even if writer.write blocks
    for seconds (slow disk / locked file / network rclone), the asyncio
    thread MUST return from ``_on_frame`` in single-digit ms.

    Today (pre-fix) the asyncio thread does writer.write inline → blocks
    here → ping-pong cycle misses → 1011.
    """
    archiver, _, writers = _make_archiver(
        monkeypatch, write_queue_maxsize=10_000,
    )
    archiver.start()
    try:
        _bind_sid(archiver, cmd_id=10, sid=42)

        # Make the writer block 5s — would hang _on_frame pre-fix.
        block_event = threading.Event()
        writers["orderbook_delta"].write.side_effect = (
            lambda _env: block_event.wait(timeout=5)
        )

        # Bind sid=42 → orderbook_delta routes its data frames to the
        # blocking writer. Now measure: _on_frame for a data frame must
        # return well under 100ms.
        data = _fake_frame(
            {"sid": 42, "seq": 1, "type": "orderbook_delta",
             "msg": {"market_ticker": "T1"}},
        )
        t0 = time.monotonic()
        archiver._on_frame(data)
        elapsed = time.monotonic() - t0
        assert elapsed < 0.1, (
            f"_on_frame took {elapsed*1000:.1f}ms — should be ≤ 100ms even "
            f"with writer blocked. Suggests writer.write is still being "
            f"called inline (not via the worker thread), which re-opens "
            f"the 1011 stall class."
        )
        block_event.set()  # let worker drain
    finally:
        archiver.stop()


# ─── 4. Functional: worker drains queue → frames written ───────────────────


def _wait_for(predicate, *, timeout=2.0, interval=0.005):
    """Poll predicate() until truthy or timeout — avoids racing the worker."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_worker_drains_queue_frames_written(monkeypatch):
    """Worker thread consumes the queue and dispatches to writers."""
    archiver, _, writers = _make_archiver(monkeypatch)
    archiver.start()
    try:
        _bind_sid(archiver, cmd_id=10, sid=42)
        for i in range(5):
            archiver._on_frame(_fake_frame(
                {"sid": 42, "seq": i + 1, "type": "orderbook_delta",
                 "msg": {"market_ticker": "T1"}},
            ))
        assert _wait_for(
            lambda: writers["orderbook_delta"].write.call_count >= 5,
            timeout=2.0,
        ), (
            f"worker did not drain 5 frames within 2s — got "
            f"{writers['orderbook_delta'].write.call_count} writes."
        )
    finally:
        archiver.stop()


def test_frames_written_in_monotonic_seq_order(monkeypatch):
    """FIFO queue + single worker + lock-held seq allocation MUST preserve
    the collector-side monotonic seq across the write side. A reordered
    write would corrupt bronze ordering invariants downstream silver QA
    depends on.
    """
    archiver, _, writers = _make_archiver(monkeypatch)
    archiver.start()
    try:
        _bind_sid(archiver, cmd_id=10, sid=42)
        N = 50
        for i in range(N):
            archiver._on_frame(_fake_frame(
                {"sid": 42, "seq": i + 1, "type": "orderbook_delta",
                 "msg": {"market_ticker": "T1"}},
            ))
        assert _wait_for(
            lambda: writers["orderbook_delta"].write.call_count >= N,
            timeout=2.0,
        ), (
            f"worker did not drain {N} frames within 2s — got "
            f"{writers['orderbook_delta'].write.call_count} writes."
        )
        envelopes = [
            call.args[0]
            for call in writers["orderbook_delta"].write.call_args_list
        ]
        seqs = [env["_collector_seq"] for env in envelopes]
        # The subscribe-ack that bound sid=42 also went through the queue
        # (subscribe-acks ARE written to bronze — they're part of the wire
        # trace per the existing _on_frame docstring). Skip ack seq when
        # checking ordering of the data frames.
        data_seqs = [
            envelopes[i]["_collector_seq"]
            for i in range(len(envelopes))
            if envelopes[i].get("_channel") == "orderbook_delta"
            and envelopes[i].get("collector_seq") != envelopes[0].get(
                "collector_seq")
        ] or seqs
        assert data_seqs == sorted(data_seqs), (
            f"writer received envelopes out of seq order: {data_seqs}. "
            f"FIFO queue + single worker + lock-held seq allocation should "
            f"preserve monotonic ordering."
        )
    finally:
        archiver.stop()


# ─── 5. Functional: backpressure — drop counter on queue full ──────────────


def test_full_queue_drops_frame_and_increments_counter(monkeypatch):
    """When the queue is at maxsize, _on_frame MUST drop new frames + bump
    the counter. A blocking put would re-stall the asyncio loop.
    """
    archiver, _, writers = _make_archiver(
        monkeypatch, write_queue_maxsize=2,
    )
    archiver.start()
    try:
        _bind_sid(archiver, cmd_id=10, sid=42)
        # The bind ack itself enqueues + drains to writers[None] (its
        # envelope channel is None because Frame.sid is None for the
        # subscribed-shape ack, where sid is nested in msg). Wait for
        # that drain so the queue starts empty for the burst.
        assert _wait_for(
            lambda: writers[None].write.call_count >= 1,
            timeout=2.0,
        )
        # Now stall the writer; queue will fill at maxsize=2 then drop.
        block_event = threading.Event()
        writers["orderbook_delta"].write.side_effect = (
            lambda _env: block_event.wait(timeout=10)
        )
        # Send many more frames than queue capacity. The first frame may
        # go inflight (worker picked it up), then 2 fill the queue, then
        # remaining drop.
        N = 20
        for i in range(N):
            archiver._on_frame(_fake_frame(
                {"sid": 42, "seq": i + 100, "type": "orderbook_delta",
                 "msg": {"market_ticker": "T1"}},
            ))
        # Verify some frames were dropped (counter incremented).
        assert archiver._dropped_frames > 0, (
            f"queue maxsize=2 + blocked worker + {N} frame burst should "
            f"have dropped frames, but _dropped_frames={archiver._dropped_frames}. "
            f"Suggests _on_frame is using a blocking put (re-stall) OR "
            f"silently swallowing queue.Full without bumping the counter."
        )
        block_event.set()
    finally:
        archiver.stop()


# ─── 6. Lifecycle: start spawns worker, stop joins it ──────────────────────


def test_start_spawns_worker_thread(monkeypatch):
    """``start()`` MUST spawn a worker thread before returning, so the
    first frame after start enters a draining queue (not a stalled one).
    """
    archiver, fake_wire, _ = _make_archiver(monkeypatch)
    archiver.start()
    try:
        assert hasattr(archiver, "_write_worker"), (
            "BronzeArchiver must expose self._write_worker (a Thread) "
            "after start() — sister tests assert on its liveness."
        )
        worker = archiver._write_worker
        assert isinstance(worker, threading.Thread), (
            f"self._write_worker should be a Thread; got {type(worker)!r}."
        )
        assert worker.is_alive(), (
            "Worker thread is not alive after start() — caller could push "
            "to a queue that nobody is draining."
        )
        # WSClient.start should still be called — start() of BronzeArchiver
        # must keep starting the wire (regression guard).
        fake_wire.start.assert_called_once()
    finally:
        archiver.stop()


def test_stop_joins_worker_thread(monkeypatch):
    """``stop()`` MUST join the worker so pending frames flush before
    process exit. A daemon thread that gets killed mid-drain would
    truncate the JSONL chunk → silver QA detects gap.
    """
    archiver, fake_wire, _ = _make_archiver(monkeypatch)
    archiver.start()
    worker = archiver._write_worker
    archiver.stop()
    assert not worker.is_alive(), (
        "Worker thread still alive after stop() — pending frames may not "
        "have drained. stop() must signal the worker (sentinel) and join."
    )
    # WSClient.stop should still be called — stop() must continue stopping
    # the wire (regression guard).
    fake_wire.stop.assert_called_once()


def test_shutdown_drains_pending_queue(monkeypatch):
    """If frames sit in the queue when stop() is called, the worker MUST
    drain them all before exiting. Otherwise we lose the buffered tail
    on every restart — bronze ordering invariants break.
    """
    archiver, _, writers = _make_archiver(monkeypatch)
    archiver.start()
    try:
        _bind_sid(archiver, cmd_id=10, sid=42)
        # Wait for the bind ack to drain (to writers[None] — the
        # subscribed-shape ack has Frame.sid=None so its envelope's
        # _channel is None) so the buffer test below measures only
        # the data-frame burst.
        assert _wait_for(
            lambda: writers[None].write.call_count >= 1,
            timeout=2.0,
        )
        # Now stall the writer so the queue accumulates, then unstall +
        # stop. The stop() drain semantics must NOT drop the buffered
        # frames.
        gate = threading.Event()
        writers["orderbook_delta"].write.side_effect = (
            lambda _env: gate.wait(timeout=5)
        )
        N = 30
        for i in range(N):
            archiver._on_frame(_fake_frame(
                {"sid": 42, "seq": i + 200, "type": "orderbook_delta",
                 "msg": {"market_ticker": "T1"}},
            ))
        # Wait long enough that the worker is genuinely sitting on the
        # blocked writer with frames behind it in the queue.
        time.sleep(0.05)
        # Now unblock + stop. The drain must complete cleanly.
        writers["orderbook_delta"].write.side_effect = None
        gate.set()
    finally:
        archiver.stop()
    # Post-stop: every queued frame must have been written. Allow for
    # the one drop-on-full edge: assert >= N (the bind ack adds 1).
    final_count = writers["orderbook_delta"].write.call_count
    assert final_count >= N, (
        f"shutdown drain incomplete: wrote {final_count} frames, expected "
        f"≥ {N} (the burst size). Stop() must drain pending queue, not "
        f"truncate."
    )


# ─── 7. Worker resilience: writer exceptions don't kill worker ─────────────


def test_worker_survives_writer_exception(monkeypatch):
    """If writer.write raises (e.g., disk full mid-write, malformed
    envelope), the worker MUST keep running so subsequent frames continue
    to drain. A worker that dies on first exception would silently stop
    all bronze writes — much worse than the symptom.
    """
    archiver, _, writers = _make_archiver(monkeypatch)
    archiver.start()
    try:
        _bind_sid(archiver, cmd_id=10, sid=42)
        # First data frame raises; subsequent succeed.
        writers["orderbook_delta"].write.side_effect = [
            RuntimeError("simulated disk full"),
            None, None, None, None,
        ]
        for i in range(5):
            archiver._on_frame(_fake_frame(
                {"sid": 42, "seq": i + 300, "type": "orderbook_delta",
                 "msg": {"market_ticker": "T1"}},
            ))
        # Worker should keep going — assert it processed all 5 attempts.
        assert _wait_for(
            lambda: writers["orderbook_delta"].write.call_count >= 5,
            timeout=2.0,
        ), (
            f"worker stalled after exception — only "
            f"{writers['orderbook_delta'].write.call_count} writes. "
            f"Exception handling in the worker loop must be try/except + "
            f"continue, not propagate."
        )
        assert archiver._write_worker.is_alive(), (
            "worker thread died after writer exception."
        )
    finally:
        archiver.stop()


# ─── 8. Subscribe-ack handling stays synchronous (binding race-free) ───────


def test_subscribe_ack_binding_visible_immediately_in_same_tick(monkeypatch):
    """The sid→channel binding from a subscribe-ack MUST be visible to
    any data frame dispatched within the same asyncio tick AFTER the ack.
    If we deferred the binding to the worker, a data frame arriving just
    after the ack but BEFORE the worker processed the ack would route to
    _unrouted — a load-bearing regression vs. D1.3.
    """
    archiver, _, writers = _make_archiver(monkeypatch)
    archiver.start()
    try:
        # Synchronous: ack THEN data frame in the same thread.
        ack = _fake_frame(
            {"id": 10, "type": "subscribed",
             "msg": {"channel": "orderbook_delta", "sid": 42}},
        )
        data = _fake_frame(
            {"sid": 42, "seq": 1, "type": "orderbook_delta",
             "msg": {"market_ticker": "T1"}},
        )
        archiver._on_frame(ack)
        archiver._on_frame(data)
        # Verify the data frame ROUTED to orderbook_delta (binding was
        # visible synchronously). The subscribe-ack itself routes to
        # writers[None] because Frame.sid is None for the nested-sid
        # ``subscribed`` shape — that's expected D1.3 behavior and
        # distinct from the binding-race regression this test guards.
        assert _wait_for(
            lambda: writers["orderbook_delta"].write.call_count >= 1,
            timeout=2.0,
        )
        # The _unrouted writer should have been hit EXACTLY ONCE (the
        # ack itself), not also by the data frame post-bind. A second
        # _unrouted write would mean the data frame's sid lookup raced
        # the binding update.
        assert writers[None].write.call_count == 1, (
            f"data frame routed to _unrouted despite prior subscribe-ack "
            f"(expected exactly 1 _unrouted write — the ack itself — got "
            f"{writers[None].write.call_count}). Binding lookup may have "
            f"been deferred to the worker (race) instead of resolved "
            f"synchronously in _on_frame."
        )
    finally:
        archiver.stop()
