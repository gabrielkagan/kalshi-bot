"""Kalshi WS consumer — D1.1.5 Phase 4 (ticket 86b9zdhz2, 2026-05-16).

Thin consumer of ``kalshi_wire.ws_client.WSClient`` that pipes raw Kalshi
WS frames into the bronze JSONL writer (``collector/writer.py``). Per the
2026-05-16 AMENDMENT to ``kb/decisions/data-corpus-architecture.md`` §5,
``kalshi_wire/`` is the shared transport that both this module AND
``bot/feeds/kalshi.py`` consume — the advisor's "two sides of the same
coin" symmetry that makes bronze byte-equivalent to what the bot itself
saw on the wire.

D1.1.5 ships the WIRE-UP skeleton: ``BronzeArchiver`` instantiates a
``WSClient`` with an ``on_frame`` callback that builds the D0.3 §2
6-field envelope via ``build_envelope`` and forwards to a writer
callable. The writer body (``collector/writer.py`` `BronzeWriter`) lands
at D1.2 — until then ``BronzeArchiver.run()`` raises NotImplementedError
diagnosable as "scaffolding present, body pending D1.2".

NO ``bot.*`` imports (pinned by ``collector-no-bot`` import-linter
contract). Auth + WS transport reach into ``kalshi_wire/`` only.

Pre-Phase-4: this module was a 12-LOC docstring-only stub plus
``collector/auth.py`` (also docstring-only) duplicating RSA-PSS-SHA256
"per D0.3 §5 paragraph 6". The AMENDMENT SUPERSEDES that paragraph;
``collector/auth.py`` is DELETED in the same Bit and auth flows through
``kalshi_wire.auth.load_private_key`` directly.
"""
from __future__ import annotations

import logging
import threading
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
    (which lands at D1.2).

    D1.1.5 Phase 4: this class is the scaffold. ``run()`` raises
    NotImplementedError until the D1.2 writer + subscription_manager
    wire-up complete. The frame-routing path IS exercised end-to-end
    by ``tests/equivalence/test_kalshi_wire_differential.py`` (the
    Pillar 3 load-bearing test).

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

        **D1.1.5 scaffold-scope note**: ``channel=None`` because the
        per-sid → channel-name mapping (mirroring KalshiFeed's
        ``_ticker_to_sid``) is D1.2 work — it requires the
        ``collector/subscription_manager.py`` body to know which sid
        belongs to which channel. Frames captured at the SCAFFOLD
        stage are written with ``_channel = None``; D1.2 will replace
        with the proper mapping at the same time it wires the WS
        subscribe-ack handler. Per D0.3 §2: "WS channel name
        (orderbook_delta / trade / market_lifecycle_v2) or None for
        REST snapshots" — None is the documented falsy fallback shape.
        """
        with self._lock:
            self._collector_seq += 1
            seq = self._collector_seq
        try:
            envelope = build_envelope(
                raw=frame.raw,
                source="kalshi_ws",
                channel=None,
                conn=self._conn_id,
                collector_seq=seq,
            )
            self._writer(envelope)
        except Exception:
            logging.warning(
                "BronzeArchiver writer failed for frame seq=%d "
                "(D1.1.5 scaffold; writer body lands at D1.2)",
                seq, exc_info=True)

    def run(self) -> None:
        """Run the WS capture loop — D1.2 implementation target.

        D1.1.5 Phase 4: the WSClient + on_frame plumbing IS exercised
        (the differential test uses this shape), but the cooperating
        ``subscription_manager.py`` + production-grade lifecycle wiring
        lands at D1.2. Raise so ``python -m collector`` is diagnosable
        as "scaffolding present, body pending D1.2" rather than
        silent-no-op.
        """
        raise NotImplementedError(
            "collector/ws_connection.BronzeArchiver.run() is a D1.1.5 "
            "Phase 4 scaffold. Real implementation lands at D1.2 "
            "(ticket 86b9ypn5q). See kb/decisions/"
            "data-corpus-architecture.md §5 for the process shape."
        )

    def start(self) -> None:
        """Start the underlying WSClient — used by the differential test
        and any future D1.2 wire-up. Sync (matches WSClient.start)."""
        self._wire.start()

    def stop(self) -> None:
        """Stop the underlying WSClient."""
        self._wire.stop()
