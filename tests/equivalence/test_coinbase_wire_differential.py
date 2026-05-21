"""D2.3 — Pillar 3 load-bearing differential test for coinbase_wire.

Ticket 86b9zkppt (2026-05-17). Mirror of
``tests/equivalence/test_kalshi_wire_differential.py``. The advisor's
"two sides of the same coin" claim becomes load-bearing here: a recorded
Coinbase Exchange WS frame sequence is fed to two SHARED
``coinbase_wire.ws_client.WSClient`` instances configured identically to
how (post-D2.3) ``bot/feeds/coinbase.py`` and (already-shipped)
``collector/coinbase_archiver.py`` consume the transport. Both consumers
MUST observe byte-identical raw frames + microsecond-equivalent wire-
receipt timestamps.

What "byte-identical" pins:
  - Same JSON-string from the wire (no normalization, no key-reordering)
  - Same ``Frame.wire_recv_ts`` (modulo tiny clock-skew across consumers,
    bounded by a 50ms test tolerance; the assertion uses microsecond-
    precision but with the 50ms band because the two clients run in the
    same process so clock skew is from asyncio task scheduling on CI
    runners, not NTP. Expected steady-state delta is ~5ms; the 50ms band
    absorbs CI scheduler jitter)
  - Same parsed dict
  - Same envelope shape (the D0.3 §2 6-field contract — pinned in the
    sister ``tests/contracts/test_coinbase_wire_envelope.py``)
  - Same ``msg_type`` extracted from the envelope (Coinbase Exchange WS
    dispatch key; per-product ``sequence`` lands on Frame.sequence_num
    when present)

What this test DOES NOT pin (out of scope for D2.3):
  - Bot's downstream spot-buffer state machine (the bot's own _prices
    + _buffers state is already covered by sibling tests of
    bot/feeds/coinbase.py)
  - Reconnect/cleanup timing — covered by the dedicated R3/P0-A sister
    test below (``test_session_end_callback_fires_before_backoff_sleep``)
  - **Bot-side subscribe-payload SHAPE (narrow ``channels=("ticker",)``
    vs the wire's wider post-D2.5 5-channel ``DEFAULT_CHANNELS``)** — the mock
    server emits ``RECORDED_FRAME_SEQUENCE`` to every connection
    regardless of what the consumer subscribes to, so this differential
    cannot detect a future widening of the bot's narrow subscribe set.
    That invariant is pinned separately by
    ``tests/contracts/test_bot_feeds_coinbase_delegates_to_wire.py::
    test_bot_feeds_coinbase_narrow_subscribe_scope_is_ticker_only``
    (AST walker on the ``_BOT_CHANNELS`` module-level tuple literal).

**DO NOT auto-regenerate snapshots in this directory** (per root
CLAUDE.md / tests/equivalence/REGEN.md). The differential output IS the
contract.
"""
from __future__ import annotations

import asyncio
import threading
import time
from typing import List, Tuple

import pytest
import websockets

from coinbase_wire.ws_client import Frame, WSClient


# ─── Recorded frame fixture — minimal Coinbase Exchange WS exchange ────────
# Pre-recorded from a synthetic Coinbase Exchange WS session (deterministic;
# no live deps). Each entry: (delay_ms_after_prev_frame, raw_payload).
#
# Frame shapes mirror the production Coinbase Exchange WS protocol per
# docs.cloud.coinbase.com/exchange/docs/websocket-channels:
#   - ``type=subscriptions``: subscribe-ack with cumulative subscribed map
#   - ``type=ticker``: real-time best-bid/best-ask + last price; carries
#     per-product ``sequence`` monotonic
#   - ``type=heartbeat``: server liveness frame per subscribed product
#   - ``type=status``: product online/offline transition (rare)
#
# Bottom frame is a second ticker on the same product to verify ``sequence``
# is observed monotonically across two consumers reading the same wire.
RECORDED_FRAME_SEQUENCE: List[Tuple[int, str]] = [
    (0,
     '{"type":"subscriptions","channels":[{"name":"ticker",'
     '"product_ids":["BTC-USD","ETH-USD"]},{"name":"heartbeat",'
     '"product_ids":["BTC-USD"]}]}'),
    (5,
     '{"type":"ticker","sequence":1001,"product_id":"BTC-USD",'
     '"price":"65432.10","best_bid":"65432.00","best_ask":"65432.20",'
     '"time":"2026-05-17T22:00:00.123456Z"}'),
    (15,
     '{"type":"heartbeat","sequence":1002,"product_id":"BTC-USD",'
     '"last_trade_id":987654,"time":"2026-05-17T22:00:00.150000Z"}'),
    (20,
     '{"type":"ticker","sequence":501,"product_id":"ETH-USD",'
     '"price":"3210.50","best_bid":"3210.40","best_ask":"3210.60",'
     '"time":"2026-05-17T22:00:00.180000Z"}'),
    (50,
     '{"type":"status","products":[{"id":"BTC-USD","status":"online",'
     '"trading_disabled":false}],"currencies":[]}'),
    (10,
     '{"type":"ticker","sequence":1003,"product_id":"BTC-USD",'
     '"price":"65431.55","best_bid":"65431.50","best_ask":"65431.70",'
     '"time":"2026-05-17T22:00:00.250000Z"}'),
]


# ─── Mock Coinbase Exchange WS server (asyncio) ─────────────────────────────


class MockCoinbaseServer:
    """A minimal WS server that emits ``RECORDED_FRAME_SEQUENCE`` to every
    connecting client. Runs on its own asyncio loop in a daemon thread so
    the test can sync-await its readiness.

    Mirrors the ``MockKalshiServer`` shape from the sister kalshi_wire
    differential test. The server is deliberately dumb: it accepts client
    subscribe frames but doesn't validate them (the test exercises the
    INCOMING-frame side only — both consumers see the same recorded
    sequence regardless of what they sent).
    """

    def __init__(self):
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._server = None
        self._port: int | None = None
        self._ready = threading.Event()
        self._stop = threading.Event()

    @property
    def port(self) -> int:
        assert self._port is not None, "Server not yet started"
        return self._port

    async def _handler(self, ws):
        """Server-side handler: drain incoming, then emit recorded sequence."""
        async def _drain():
            try:
                async for _ in ws:
                    pass
            except Exception:
                pass
        drain_task = asyncio.create_task(_drain())
        try:
            for delay_ms, payload in RECORDED_FRAME_SEQUENCE:
                if delay_ms > 0:
                    await asyncio.sleep(delay_ms / 1000.0)
                await ws.send(payload)
            # Hold the connection open briefly so consumers finish parsing.
            await asyncio.sleep(0.2)
        finally:
            drain_task.cancel()
            try:
                await drain_task
            except (asyncio.CancelledError, Exception):
                pass

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)

        async def _serve():
            self._server = await websockets.serve(
                self._handler, "127.0.0.1", 0)
            self._port = self._server.sockets[0].getsockname()[1]
            self._ready.set()
            try:
                await asyncio.Future()  # run forever
            except asyncio.CancelledError:
                pass

        try:
            self._loop.run_until_complete(_serve())
        except Exception:
            self._ready.set()  # unblock the test even on startup failure
        finally:
            try:
                if self._server is not None:
                    self._server.close()
                pending = asyncio.all_tasks(self._loop)
                for t in pending:
                    t.cancel()
            except Exception:
                pass
            self._loop.close()

    def start(self, timeout_s: float = 5.0):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=timeout_s):
            raise RuntimeError("MockCoinbaseServer failed to become ready")

    def stop(self):
        # Cleanly close the server + drain pending tasks before stopping
        # the loop, to avoid the "Task was destroyed but it is pending!"
        # RuntimeWarning that the kalshi-side mock server also emits but
        # we get to avoid now. ``server.close()`` returns immediately;
        # ``wait_closed()`` awaits the final socket close.
        async def _shutdown():
            try:
                if self._server is not None:
                    self._server.close()
                    try:
                        await self._server.wait_closed()
                    except Exception:
                        pass
            finally:
                self._loop.stop()
        if self._loop is not None and not self._loop.is_closed():
            try:
                asyncio.run_coroutine_threadsafe(
                    _shutdown(), self._loop)
            except RuntimeError:
                # Loop already closed; nothing to drain.
                pass
        if self._thread is not None:
            self._thread.join(timeout=3.0)


# ─── Frame-collector helpers ────────────────────────────────────────────────


def _run_one_consumer(url: str, capture: List[Frame], stop_after_s: float):
    """Spin up one WSClient pointed at ``url``, collect frames into
    ``capture``, stop after ``stop_after_s`` seconds.

    Coinbase Exchange WS public channels need no auth — the wire library
    has no api_key/private_key kwargs (mirror of D2.1.5 public-only
    scoping). The mock server accepts the subscribe frame the wire
    library's default ``on_session_start`` dispatches but ignores its
    contents.
    """
    done = threading.Event()

    def on_frame(frame: Frame) -> None:
        capture.append(frame)
        # The mock emits len(RECORDED_FRAME_SEQUENCE) frames; stop once
        # we have them.
        if len(capture) >= len(RECORDED_FRAME_SEQUENCE):
            done.set()

    client = WSClient(
        on_frame=on_frame,
        url=url,
        # Disable silence watchdog during the test window (3s) so a slow
        # CI runner can't trip a force-reconnect mid-capture.
        silence_grace_s=300.0,
        silence_timeout_s=300.0,
        watchdog_check_interval=60.0,
    )
    client.start()
    done.wait(timeout=stop_after_s)
    client.stop()
    # Give the stop signal time to drain.
    time.sleep(0.2)


# ─── Tests ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def mock_server():
    """One-shot mock server, reused across the differential tests."""
    server = MockCoinbaseServer()
    server.start()
    yield server
    server.stop()


def test_bot_and_collector_capture_byte_identical_frames(mock_server):
    """Differential — two WSClient consumers see the same raw-frame
    sequence from the mock server.

    Steps:
      1. The shared module-scope mock_server is already emitting
         RECORDED_FRAME_SEQUENCE to each new connection
      2. Two WSClient instances connect concurrently (bot-style +
         collector-style — both go through the same coinbase_wire
         library so the only differential is process-internal
         asyncio scheduling)
      3. Each captures frames via ``on_frame=callback``
      4. After ≤3s, assert both captured exactly
         len(RECORDED_FRAME_SEQUENCE) frames
      5. For each i: assert bot_frames[i].raw == collector_frames[i].raw
      6. For each i: |bot_frames[i].wire_recv_ts -
         collector_frames[i].wire_recv_ts| ≤ 50ms
      7. For each i: parsed dicts match (parse is identical when raw is)
    """
    url = f"ws://127.0.0.1:{mock_server.port}"
    bot_frames: List[Frame] = []
    collector_frames: List[Frame] = []

    bot_thread = threading.Thread(
        target=_run_one_consumer, args=(url, bot_frames, 3.0))
    collector_thread = threading.Thread(
        target=_run_one_consumer, args=(url, collector_frames, 3.0))
    bot_thread.start()
    collector_thread.start()
    bot_thread.join(timeout=5.0)
    collector_thread.join(timeout=5.0)

    expected_n = len(RECORDED_FRAME_SEQUENCE)
    assert len(bot_frames) == expected_n, (
        f"bot consumer captured {len(bot_frames)} frames, expected "
        f"{expected_n}: {[f.raw[:40] for f in bot_frames]}"
    )
    assert len(collector_frames) == expected_n, (
        f"collector consumer captured {len(collector_frames)} frames, "
        f"expected {expected_n}: {[f.raw[:40] for f in collector_frames]}"
    )

    for i, (b, c) in enumerate(zip(bot_frames, collector_frames)):
        assert b.raw == c.raw, (
            f"frame[{i}] raw differs:\n  bot       = {b.raw!r}\n  "
            f"collector = {c.raw!r}"
        )
        delta_ms = abs(b.wire_recv_ts - c.wire_recv_ts) * 1000.0
        assert delta_ms < 50.0, (
            f"frame[{i}] wire_recv_ts diverged by {delta_ms:.2f}ms "
            f"(bot={b.wire_recv_ts}, collector={c.wire_recv_ts}). "
            "Two consumers in the same process should clock-align within "
            "~5ms; >50ms means asyncio scheduling broke the timing contract."
        )
        assert b.parsed == c.parsed, (
            f"frame[{i}] parsed differs:\n  bot       = {b.parsed}\n  "
            f"collector = {c.parsed}"
        )
        assert b.msg_type == c.msg_type, (
            f"frame[{i}] msg_type differs:\n  bot       = {b.msg_type!r}"
            f"\n  collector = {c.msg_type!r}"
        )
        assert b.sequence_num == c.sequence_num, (
            f"frame[{i}] sequence_num differs:\n  bot       = "
            f"{b.sequence_num!r}\n  collector = {c.sequence_num!r}"
        )


def test_wire_recv_ts_captured_before_json_parse(mock_server):
    """``Frame.wire_recv_ts`` must be set BEFORE ``json.loads(raw)``
    executes. D0.3 §2 spec: "Captured at frame ingress, BEFORE any
    deserialization. Cannot be reconstructed from ``_raw``."

    Proof: for every frame, ``wire_recv_ts > 0`` AND the timestamp lies
    BEFORE the post-test ``time.time()`` snapshot. If parsing happened
    before timestamp capture, a parse-failure would leave
    ``wire_recv_ts`` at its default (0.0).
    """
    url = f"ws://127.0.0.1:{mock_server.port}"
    frames: List[Frame] = []
    _run_one_consumer(url, frames, 3.0)
    end = time.time()
    assert len(frames) >= len(RECORDED_FRAME_SEQUENCE)
    for i, f in enumerate(frames):
        assert isinstance(f.wire_recv_ts, float), (
            f"frame[{i}] wire_recv_ts is not a float: {f.wire_recv_ts!r}")
        assert 0.0 < f.wire_recv_ts < end + 1.0, (
            f"frame[{i}] wire_recv_ts ({f.wire_recv_ts}) outside "
            f"plausible window (0, {end + 1.0})")


def test_msg_type_dispatch_key_populated_on_every_well_formed_frame(
        mock_server):
    """Coinbase Exchange WS uses ``type`` as the dispatch key (NO
    top-level ``channel`` field). Every well-formed frame the consumer
    receives must have ``Frame.msg_type`` set to the ``type`` value.

    This pins the dispatch-key contract: post-D2.3 consumers on the
    Coinbase side (the bot's CoinbaseFeed + the coinbase collector's
    CoinbaseArchiver) rely on ``frame.msg_type`` for routing. A wire-
    level bug that left ``msg_type=None`` despite a present ``type``
    field would cause both to mis-route silently. (Unrelated to the
    Kalshi-side P1-B-brutalist Phase B1 opt-in to ``parse_on_demand=
    True``, which sets ``msg_type=None`` deliberately on the kalshi-
    collector path; coinbase_wire has no equivalent flag at present.)
    """
    url = f"ws://127.0.0.1:{mock_server.port}"
    frames: List[Frame] = []
    _run_one_consumer(url, frames, 3.0)
    assert len(frames) >= len(RECORDED_FRAME_SEQUENCE)
    expected_types = {"subscriptions", "ticker", "heartbeat", "status"}
    seen_types = {f.msg_type for f in frames}
    assert expected_types <= seen_types, (
        f"missing msg_types from observed set: "
        f"missing={expected_types - seen_types}, seen={seen_types}"
    )
    for i, f in enumerate(frames):
        assert f.msg_type is not None, (
            f"frame[{i}] msg_type is None despite the recorded payload "
            f"carrying a top-level type field: raw={f.raw!r}"
        )
        assert isinstance(f.msg_type, str), (
            f"frame[{i}] msg_type is not a str: {f.msg_type!r}"
        )


def test_session_end_callback_fires_before_backoff_sleep():
    """R3/P0-A reconnect-cleanup data-integrity pin.

    The wire library MUST fire ``on_session_end`` BEFORE the backoff
    sleep so consumer per-session caches are cleared while
    ``is_connected`` reads False. Pre-D2.3 the bot's
    ``bot/feeds/coinbase.py`` _ws_loop set ``_connected = False`` BEFORE
    the backoff; post-D2.3 the wire owns that ordering and the bot
    delegates ``is_connected`` to ``self._wire.is_connected``. The same
    ordering contract carries forward.

    Test approach: spawn a WSClient pointed at a localhost that REFUSES
    the connection (port 1 — reserved, will ECONNREFUSED). The connect
    raises, ``on_session_end`` should fire, THEN the WSClient enters
    backoff sleep. We assert the callback fires within ~3s (generous
    for slow CI runners; the actual sleep is ~1s initial backoff).
    """
    end_called = threading.Event()

    def on_session_end():
        end_called.set()

    client = WSClient(
        on_frame=lambda f: None,
        url="ws://127.0.0.1:1",  # ECONNREFUSED — port 1 reserved
        on_session_end=on_session_end,
        silence_grace_s=300.0,
        silence_timeout_s=300.0,
        watchdog_check_interval=60.0,
    )
    client.start()
    fired = end_called.wait(timeout=3.0)
    client.stop()
    assert fired, (
        "on_session_end did not fire before backoff sleep — R3/P0-A "
        "reconnect-cleanup ordering regressed. Expected the callback to "
        "fire synchronously inside the exception path, BEFORE the "
        "backoff `wait_for(stop_event)` sleep."
    )
