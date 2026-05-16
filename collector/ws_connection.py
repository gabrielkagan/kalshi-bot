"""Kalshi WS consumer — D1.1.5 + D1.2 (tickets 86b9zdhz2 + 86b9ypn66, 2026-05-16).

Thin consumer of ``kalshi_wire.ws_client.WSClient`` that pipes raw Kalshi
WS frames into the bronze JSONL writer (``collector/writer.py``). Per the
2026-05-16 AMENDMENT to ``kb/decisions/data-corpus-architecture.md`` §5,
``kalshi_wire/`` is the shared transport that both this module AND
``bot/feeds/kalshi.py`` consume — the "two sides of the same coin"
symmetry that makes bronze byte-equivalent to what the bot itself saw
on the wire.

D1.1.5 shipped the WIRE-UP skeleton (auth + WSClient consumer + on_frame
plumbing); D1.2 (ticket 86b9ypn66) shipped ``BronzeArchiver.run()`` body
(blocking run loop with signal-handler-installed shutdown) and wired the
``_wire_recv_ts`` capture-at-ingress invariant (D0.3 §2) by passing
``frame.wire_recv_ts`` into ``build_envelope``.

NO ``bot.*`` imports (pinned by ``collector-no-bot`` import-linter
contract). Auth + WS transport reach into ``kalshi_wire/`` only.

D1.3 will add the ``on_session_start`` callback that sends Kalshi
subscribe frames; until then, the WS connects but no data flows.
First-bronze-flow is D1.3's acceptance criterion.
"""
from __future__ import annotations

import logging
import signal
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Optional, Union

from kalshi_wire.auth import load_private_key  # noqa: F401 — re-exported for callers
from kalshi_wire.ws_client import Frame, WSClient, build_envelope


class BronzeArchiver:
    """Collector-side WSClient consumer — pipes Frame.raw to a writer.

    Architecture (per D0.3 §2 + §5 AMENDMENT 2026-05-16):
        WSClient (kalshi_wire) ──► on_frame(Frame) ──► build_envelope ──►
        writer(envelope_dict)

    The writer callable is the seam to ``collector/writer.py``'s
    ``BronzeWriter.write(envelope)``; injecting it via constructor
    keeps this module testable independent of the writer's body shape
    (shipped at D1.2).

    D1.2 status: ``run()`` body landed. The class still does NOT send
    subscribe frames (D1.3 owns ``on_session_start``); the WS connects
    but receives zero data until D1.3 lands. The frame-routing path IS
    exercised end-to-end by ``tests/equivalence/test_kalshi_wire_differential.py``
    (Pillar 3) + the D1.2 wire-up integration tests in
    ``tests/integration/test_collector_main_loop_wireup.py``.

    No ``bot.*`` imports — collector-no-bot contract.
    """

    def __init__(
        self,
        api_key: str,
        private_key_path: Union[str, Path],
        *,
        writer: Callable[[Dict], None],
        conn_id: str = "A",
        url: Optional[str] = None,
    ):
        self._api_key = api_key
        self._private_key = load_private_key(private_key_path)
        self._writer = writer
        self._conn_id = conn_id
        self._lock = threading.Lock()
        self._collector_seq = 0
        _wire_kwargs: Dict = {
            "api_key": api_key,
            "private_key": self._private_key,
            "on_frame": self._on_frame,
        }
        if url is not None:
            _wire_kwargs["url"] = url
        self._wire = WSClient(**_wire_kwargs)

    def _on_frame(self, frame: Frame) -> None:
        """WSClient callback: build the D0.3 §2 envelope around the
        raw payload and forward to the writer.

        The envelope is ALWAYS built — bronze stores every frame
        verbatim, no filtering, no transformation. Silver ETL (D2.x)
        dispatches on the ``_channel`` field downstream.

        D1.2 scope-note: ``channel=None`` is the documented D0.3 §2
        falsy fallback. The per-sid → channel-name mapping (mirroring
        KalshiFeed's ``_ticker_to_sid``) is D1.3 work — it requires the
        ``collector/subscription_manager.py`` body to know which sid
        belongs to which channel. Until D1.3 lands, frames are written
        with ``_channel = None`` and route to the
        ``<root>/<source>/_unrouted/`` partition (per BronzeWriter's
        channel=None fallback).
        """
        with self._lock:
            self._collector_seq += 1
            seq = self._collector_seq
        try:
            # CRITICAL D0.3 §2 invariant: _wire_recv_ts MUST be captured
            # at frame ingress, NOT at envelope-build time. WSClient
            # stamps Frame.wire_recv_ts BEFORE json.loads at the ws-recv
            # site; forwarding it here keeps bronze fidelity intact.
            # Default-None on build_envelope would silently substitute
            # datetime.now() — which is dispatch-callback-time, NOT
            # wire-ingress-time, and the difference grows under load.
            wire_recv_ts = datetime.fromtimestamp(
                frame.wire_recv_ts, tz=timezone.utc
            )
            envelope = build_envelope(
                raw=frame.raw,
                source="kalshi_ws",
                channel=None,
                conn=self._conn_id,
                collector_seq=seq,
                wire_recv_ts=wire_recv_ts,
            )
            self._writer(envelope)
        except Exception:
            logging.warning(
                "BronzeArchiver writer failed for frame seq=%d",
                seq, exc_info=True)

    def run(self, shutdown_event: Optional[threading.Event] = None) -> None:
        """Start the WS client and block until shutdown is signaled.

        D1.2 single-conn no-tier shape (per pickup-prompt): one WSClient
        per archiver, frames routed via _on_frame → writer callable.
        D1.3 will generalize to per-tier multi-conn via the subscription
        manager.

        ``shutdown_event``: if provided, ``run`` blocks on
        ``event.wait()``; caller drives shutdown. If None, installs
        SIGINT/SIGTERM handlers on the (assumed-main) thread and waits
        on an internal Event. Tests pass a controlled event to avoid
        signal-handler pollution.
        """
        owned_event = shutdown_event is None
        if owned_event:
            shutdown_event = threading.Event()
            # signal.signal raises ValueError on non-main thread; tolerate
            # so a test or alt-thread invocation falls back to event-only
            # control (test must call .stop() explicitly via the event).
            try:
                signal.signal(signal.SIGINT, lambda *_: shutdown_event.set())
                signal.signal(signal.SIGTERM, lambda *_: shutdown_event.set())
            except ValueError:
                logging.warning(
                    "BronzeArchiver.run(): could not install SIGINT/SIGTERM "
                    "handlers (non-main thread). Caller must drive shutdown "
                    "via the event."
                )

        self.start()
        try:
            shutdown_event.wait()
        finally:
            self.stop()

    def start(self) -> None:
        """Start the underlying WSClient — invoked by ``run()`` (D1.2)
        and exercised independently by the kalshi_wire differential test.
        Sync (matches WSClient.start)."""
        self._wire.start()

    def stop(self) -> None:
        """Stop the underlying WSClient."""
        self._wire.stop()
