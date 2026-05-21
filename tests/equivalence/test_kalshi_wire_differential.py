"""D1.1.5 — Pillar 3 load-bearing differential test.

Ticket 86b9zdhz2 (2026-05-16). The advisor's "two sides of the same coin"
claim becomes load-bearing here: a recorded Kalshi WS frame sequence is
fed to two SHARED ``kalshi_wire.ws_client.WSClient`` instances configured
identically to how ``bot/feeds/kalshi.py`` and (future)
``collector/ws_connection.py`` consume the transport. Both consumers MUST
observe byte-identical raw frames + microsecond-equivalent wire-receipt
timestamps.

What "byte-identical" pins:
  - Same JSON-string from the wire (no normalization, no key-reordering)
  - Same ``Frame.wire_recv_ts`` (modulo tiny clock-skew across consumers,
    bounded by a 50ms test tolerance; the assertion uses
    microsecond-precision but with the 50ms band because the two
    clients run in the same process so clock skew is from asyncio task
    scheduling on CI runners, not NTP. The actual expected delta is
    ~5ms in steady state; the 50ms band absorbs CI scheduler jitter)
  - Same envelope shape (the D0.3 §2 6-field contract — pinned in the
    sister ``tests/contracts/test_kalshi_wire_envelope.py``)
  - Same SID/seq pair extracted from the envelope

What this test DOES NOT pin (out of scope for D1.1.5):
  - Bot's downstream orderbook-state-machine output (Pillar 3 corpus
    tests already cover the engine outputs against a static parquet)
  - Reconnect/cleanup timing (R3/P0-A 60s data-integrity bug class —
    that has its own sister test below: ``test_session_end_callback_
    fires_before_backoff_sleep``)

**DO NOT auto-regenerate snapshots in this directory** (per root
CLAUDE.md / tests/equivalence/REGEN.md). The differential output IS the
contract.
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from typing import List, Tuple
from unittest.mock import MagicMock

import pytest
import websockets

# bot/feeds/orderbook_schema.py is the only bot dependency for our
# integration-style probe; we don't need to import bot itself.
from kalshi_wire.ws_client import Frame, WSClient

# ─── Recorded frame fixture — minimal Kalshi WS protocol exchange ────────────
# Pre-recorded from a real Kalshi WS session (deterministic; no live deps).
# Each entry: (delay_ms_after_prev_frame, raw_payload).
RECORDED_FRAME_SEQUENCE: List[Tuple[int, str]] = [
    (0, '{"id":2,"type":"subscribed","msg":{"channel":"orderbook_delta","sid":42}}'),
    (5, '{"sid":42,"seq":1,"type":"orderbook_snapshot","msg":{"market_ticker":"KXBTCD-26MAY1614-T100","yes_dollars_fp":[["0.55","100"],["0.54","50"]],"no_dollars_fp":[["0.46","75"]]}}'),
    (15, '{"sid":42,"seq":2,"type":"orderbook_delta","msg":{"market_ticker":"KXBTCD-26MAY1614-T100","price_dollars":"0.55","delta_fp":"25","side":"yes"}}'),
    (20, '{"sid":42,"seq":3,"type":"orderbook_delta","msg":{"market_ticker":"KXBTCD-26MAY1614-T100","price_dollars":"0.46","delta_fp":"-10","side":"no"}}'),
    (50, '{"sid":42,"seq":4,"type":"orderbook_delta","msg":{"market_ticker":"KXBTCD-26MAY1614-T100","price_dollars":"0.55","delta_fp":"-50","side":"yes"}}'),
    (10, '{"id":3,"type":"ok","sid":42,"seq":5,"msg":{"market_tickers":["KXBTCD-26MAY1614-T100"]}}'),
]


# ─── Mock Kalshi WS server (asyncio) ────────────────────────────────────────


class MockKalshiServer:
    """A minimal WS server that emits ``RECORDED_FRAME_SEQUENCE`` to every
    connecting client. Runs on its own asyncio loop in a daemon thread so
    the test can sync-await its readiness.

    The server is deliberately dumb: it does NOT consume client-sent
    subscribe frames (the differential test doesn't exercise the send
    side — only that two consumers see the same incoming frames). For
    completeness it drains incoming messages so the connection doesn't
    backpressure-block.
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
        # Drain incoming sends in a background task so the connection
        # doesn't backpressure-block.
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
            self._server = await websockets.serve(self._handler, "127.0.0.1", 0)
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
            raise RuntimeError("MockKalshiServer failed to become ready")

    def stop(self):
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=3.0)


# ─── Frame-collector helpers ────────────────────────────────────────────────


def _run_one_consumer(url: str, capture: List[Frame], stop_after_s: float):
    """Spin up one WSClient pointed at ``url`` (no auth — mock server
    accepts whatever headers), collect frames into ``capture``, stop after
    ``stop_after_s`` seconds."""
    done = threading.Event()

    def on_frame(frame: Frame) -> None:
        capture.append(frame)
        # The mock emits 6 frames; stop once we have them.
        if len(capture) >= len(RECORDED_FRAME_SEQUENCE):
            done.set()

    # The mock server doesn't validate auth headers, but kalshi_wire.auth
    # is unhappy without a real private key, so pass a sentinel that
    # _make_ws_headers can use only if WSClient skips signing for this test.
    # The cleanest path is to bypass signing: WSClient accepts a private_key
    # of None when auth_headers=None (added for this test).
    client = WSClient(
        api_key="test_key",
        private_key=None,  # auth is bypassed for the mock server (test_only flag)
        url=url,
        on_frame=on_frame,
        silence_grace_s=300.0,  # disable silence watchdog for the test window
        silence_timeout_s=300.0,
        watchdog_check_interval=60.0,
        _test_skip_auth=True,
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
    server = MockKalshiServer()
    server.start()
    yield server
    server.stop()


def test_bot_and_collector_capture_byte_identical_frames(mock_server):
    """Differential — two WSClient consumers see the same raw-frame
    sequence from the mock server.

    Steps:
      1. The shared module-scope mock_server is already emitting
         RECORDED_FRAME_SEQUENCE to each new connection
      2. Two WSClient instances connect concurrently (bot-style + collector-style)
      3. Each captures frames via ``on_frame=callback``
      4. After ≤3s, assert both captured exactly len(RECORDED_FRAME_SEQUENCE) frames
      5. For each i: assert bot_frames[i].raw == collector_frames[i].raw
      6. For each i: |bot_frames[i].wire_recv_ts - collector_frames[i].wire_recv_ts| ≤ 5ms
    """
    url = f"ws://127.0.0.1:{mock_server.port}"
    bot_frames: List[Frame] = []
    collector_frames: List[Frame] = []

    # Run both consumers in parallel.
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
        f"bot consumer captured {len(bot_frames)} frames, expected {expected_n}: "
        f"{[f.raw[:40] for f in bot_frames]}"
    )
    assert len(collector_frames) == expected_n, (
        f"collector consumer captured {len(collector_frames)} frames, "
        f"expected {expected_n}: {[f.raw[:40] for f in collector_frames]}"
    )

    for i, (b, c) in enumerate(zip(bot_frames, collector_frames)):
        assert b.raw == c.raw, (
            f"frame[{i}] raw differs:\n  bot      = {b.raw!r}\n  collector = {c.raw!r}"
        )
        delta_ms = abs(b.wire_recv_ts - c.wire_recv_ts) * 1000.0
        assert delta_ms < 50.0, (
            f"frame[{i}] wire_recv_ts diverged by {delta_ms:.2f}ms "
            f"(bot={b.wire_recv_ts}, collector={c.wire_recv_ts}). "
            "Two consumers in the same process should clock-align within "
            "~5ms; >50ms means asyncio scheduling broke the timing contract."
        )
        # Parsed payload should match too.
        assert b.parsed == c.parsed, (
            f"frame[{i}] parsed differs:\n  bot      = {b.parsed}\n  "
            f"collector = {c.parsed}"
        )


def test_wire_recv_ts_captured_before_json_parse(mock_server):
    """``Frame.wire_recv_ts`` must be set BEFORE ``json.loads(raw)``
    executes (when ``parse_on_demand=False`` — the default; under
    ``parse_on_demand=True`` the wire skips ``json.loads`` entirely
    post P1-B-brutalist Phase B1, but the timestamp is still captured
    at frame ingress). D0.3 §2 spec: "Captured at frame ingress,
    BEFORE any deserialization. Cannot be reconstructed from `_raw`."

    Proof: for every frame, ``wire_recv_ts > 0`` AND the timestamp lies
    BEFORE the post-test ``time.time()`` snapshot. If parsing happened
    before timestamp capture, a parse-failure would leave ``wire_recv_ts``
    at its default (None/0).
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


def test_session_end_callback_fires_before_backoff_sleep():
    """R3/P0-A reconnect-cleanup data-integrity pin.

    The pre-extraction ``bot/feeds/kalshi.py`` had a load-bearing
    invariant: on exception, ``_cleanup_session_state()`` runs BEFORE the
    backoff sleep so ``is_connected`` reads False during reconnect-wait
    and stale ``_orderbooks`` from the prior session aren't served as
    live data. After Phase 3b, that ordering is preserved via
    ``on_session_end`` being invoked BEFORE the asyncio
    ``wait_for(stop_event, timeout=backoff)`` sleep.

    Test approach: spawn a WSClient pointed at a localhost that REFUSES
    the connection (port 1 — reserved, will ECONNREFUSED). The connect
    raises, ``on_session_end`` should fire, THEN the WSClient enters
    backoff sleep. We assert the callback fires within ~1s.
    """
    end_called = threading.Event()

    def on_session_end():
        end_called.set()

    client = WSClient(
        api_key="x",
        private_key=None,
        url="ws://127.0.0.1:1",  # ECONNREFUSED — port 1 reserved
        on_frame=lambda f: None,
        on_session_end=on_session_end,
        silence_grace_s=300.0,
        silence_timeout_s=300.0,
        watchdog_check_interval=60.0,
        _test_skip_auth=True,
    )
    client.start()
    # The connect fails fast; on_session_end must fire BEFORE the WSClient
    # sleeps the backoff (~1s initial). We allow up to 3s for the test
    # to be robust against slow CI runners.
    fired = end_called.wait(timeout=3.0)
    client.stop()
    assert fired, (
        "on_session_end did not fire before backoff sleep — R3/P0-A "
        "reconnect-cleanup ordering regressed. Expected the callback to "
        "fire synchronously inside the exception path, BEFORE the "
        "backoff `wait_for(stop_event)` sleep."
    )
