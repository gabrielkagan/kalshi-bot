"""Burst-capacity contract for BronzeArchiver (ticket 86ba1xraq).

RCA — 2026-05-21 universal-mode soak under cap-raise commit fec5d1c2:
during the hourly REST-refresh-induced reconnect cascade, conn C dropped
1290 frames at 11:20:50-11:21 UTC. Smoking gun journalctl:

    May 21 11:20:50 BronzeArchiver write_queue full (conn=C seq=1887283)
        — dropping frame; total dropped=1
    May 21 11:20:51 BronzeArchiver write_queue full (conn=C seq=1888282)
        — dropping frame; total dropped=1000

999 frames dropped in 1 second on the unlucky conn (conn C — highest
ack volume + cascade load apex). Memory peak unchanged at 1189 MB —
NOT memory pressure. Producer (asyncio thread post-reconnect) at
~1000 frames/sec exceeds worker drain (~957 frames/sec, dominated by
zstd compress + disk IO) for ~30s burst window. Queue overflows the
10K cap → ``put_nowait`` raises ``queue.Full`` → ``_dropped_frames``
increments.

The strict 0-drops acceptance gate for the universal-mode soak requires
the default ``_DEFAULT_WRITE_QUEUE_MAXSIZE`` to absorb a 30K-frame
burst without dropping. The measured peak overflow was ~11.3K (10K cap
+ 1290 dropped). A 30K test margin is ~2.6× over measured peak — proves
the new default has headroom for current load + 2× universe growth.

Pins (this file):

  1. ``_DEFAULT_WRITE_QUEUE_MAXSIZE`` is the canonical default constant
     (catch accidental revert / divergence).
  2. Default value is ``50_000`` — bumped from ``10_000`` 2026-05-21
     (this Bit) to absorb the post-reconnect data burst on conn C.
  3. Burst-absorption contract: a default-configured BronzeArchiver MUST
     absorb a 30K-frame burst with ``_dropped_frames == 0``. This test
     FAILS on the pre-bump default (10_000) — that's the RED-to-GREEN
     gate that proves the Bit closed the drop class.
  4. Peak-instrumentation contract: ``_write_queue_peak`` tracks the
     high-water mark and is exposed as ``write_queue_peak_size`` in
     ``get_health_snapshot()``. Future capacity planning is data-driven
     (CLAUDE.md "Data-driven changes only").
  5. Peak resets to 0 on worker respawn — matches the existing
     ``_dropped_frames`` reset invariant for per-session observability.

NOT pinned (separate test files):

  - The ``write_queue_maxsize`` kwarg surface itself — pinned at
    ``test_bronze_archiver_worker_thread.py::
    test_bronze_archiver_accepts_write_queue_maxsize_kwarg``.
  - ``schema_version`` of the bronze_health sidecar — pinned at
    ``test_bronze_health_sidecar.py`` (additive field; version stays 1).
"""
from __future__ import annotations

import inspect
import queue
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
WS_CONNECTION_PATH = REPO_ROOT / "collector" / "ws_connection.py"


# ─── Fixture helpers (duplicated from test_bronze_archiver_worker_thread.py) ──
# Intentionally local-copy rather than a shared conftest helper: the existing
# helpers in the sibling test file are private (underscore-prefixed); coupling
# this Bit's regression test to that file's internals would create a hidden
# refactor-blocker. Duplication is the cheaper cost.


def _make_archiver(monkeypatch, **overrides):
    """Construct a BronzeArchiver with mocked WSClient + writers.

    The worker thread (the drain consumer) is real, started lazily by
    ``archiver.start()``. Tests in this file deliberately DO NOT call
    ``start()`` before injecting frames — that lets the queue accumulate
    so we can observe burst-absorption without the worker draining
    in parallel.
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
        conn_id="C",  # match the conn that dropped in the 2026-05-21 incident
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
    """Send a subscribe-ack through _on_frame to bind sid → channel."""
    ack = _fake_frame(
        {"id": cmd_id, "type": "subscribed",
         "msg": {"channel": channel, "sid": sid}},
    )
    archiver._on_frame(ack)


# ─── 1. Constant pin: default maxsize is 50_000 ──────────────────────────────


def test_default_write_queue_maxsize_is_50000():
    """Pin the canonical default to catch accidental revert.

    Bumped from 10_000 → 50_000 at ticket 86ba1xraq (2026-05-21) after
    the universal-mode soak under fec5d1c2 surfaced a 1290-frame drop
    on conn C during the hourly REST-refresh-induced reconnect cascade.
    The new value gives 4.4× margin over the measured peak burst.

    A future-Bit decision to change this constant must lockstep-update
    the surfaces enumerated below. Canonical list is maintained in
    `kb/decisions/bit-collector-reconnect-drop-elimination-plan.md`
    §"Sister-doc lockstep" — keep this enumeration and that one in sync.

      1. collector/ws_connection.py — rationale block above
         _DEFAULT_WRITE_QUEUE_MAXSIZE.
      2. collector/ws_connection.py — the constant value itself.
      3. collector/ws_connection.py — BronzeArchiver.__init__ kwarg
         docstring for write_queue_maxsize.
      4. collector/ws_connection.py — init-block peak-counter docstring
         (thread-safety model).
      5. collector/ws_connection.py — get_health_snapshot() docstring
         (8-key list).
      6. collector/ws_connection.py — start() docstring Re-entrancy
         semantics paragraph.
      7. agent_docs/bot_layout.md — main_loop.py entry (8-key snapshot
         mention).
      8. agent_docs/bot_layout.md — ws_connection.py entry (current
         value + shipped tag).
      9. agent_docs/bot_layout.md — coinbase_archiver.py entry
         (Kalshi-vs-Coinbase asymmetry annotation).
     10. tests/contracts/test_bronze_health_sidecar.py — both the
         module docstring AND the required-set in
         test_bronze_archiver_get_health_snapshot_returns_required_keys
         AND the JSON-flow-through guard for write_queue_peak_size.
     11. collector/coinbase_archiver.py — asymmetry-justification
         comment above its _DEFAULT_WRITE_QUEUE_MAXSIZE (which stays
         at 10_000; the comment must reflect that the Kalshi-side
         constant diverged at this Bit).
     12. THIS pin (the test you're reading).

    Explicitly RETRACTED from the original lockstep list (kept here as
    a historical record of the R1+R2 decision):

      - tests/contracts/test_d1_3_fu5_ack_not_enqueued.py:4 — was
        originally enumerated as a value-update site; on review
        confirmed as historical past-tense RCA narrative documenting
        D1.3-fu4's initial 10_000 maxsize. Changing that docstring
        would corrupt the D1.3-fu4 → D1.3-fu5 RCA narrative.

    Coinbase side (collector/coinbase_archiver.py:_DEFAULT_WRITE_QUEUE_MAXSIZE)
    is INTENTIONALLY DIVERGED from Kalshi post-86ba1xraq — different
    load class (single conn, no REST cascade, steady-state rate). Re-
    symmetrize only if a future Coinbase load measurement justifies.
    """
    from collector.ws_connection import _DEFAULT_WRITE_QUEUE_MAXSIZE
    assert _DEFAULT_WRITE_QUEUE_MAXSIZE == 50_000, (
        f"_DEFAULT_WRITE_QUEUE_MAXSIZE drifted: expected 50_000, got "
        f"{_DEFAULT_WRITE_QUEUE_MAXSIZE}. Reverting this constant without "
        f"updating the linked plan doc + sister docs would silently re-open "
        f"the conn-C drop class observed 2026-05-21 11:20:50 UTC."
    )


def test_default_write_queue_maxsize_propagates_to_archiver():
    """``BronzeArchiver()`` constructed without explicit kwarg uses the
    module-level default. inspect.signature returns the EVALUATED
    default expression — so the test catches divergence between the
    kwarg default and the module-level constant whether the kwarg uses
    a literal (e.g., `write_queue_maxsize=12345`) or a different
    constant (e.g., `write_queue_maxsize=SOME_OTHER_CONSTANT`). It does
    NOT distinguish those two failure modes from each other (both
    surface as a value mismatch), but either case is caught."""
    from collector.ws_connection import _DEFAULT_WRITE_QUEUE_MAXSIZE
    sig = inspect.signature(__import__(
        "collector.ws_connection", fromlist=["BronzeArchiver"]
    ).BronzeArchiver.__init__)
    default = sig.parameters["write_queue_maxsize"].default
    assert default == _DEFAULT_WRITE_QUEUE_MAXSIZE, (
        f"BronzeArchiver.__init__'s write_queue_maxsize default "
        f"({default}) diverged from the module-level "
        f"_DEFAULT_WRITE_QUEUE_MAXSIZE ({_DEFAULT_WRITE_QUEUE_MAXSIZE}). "
        f"The two must be lockstep — a divergence means callers using "
        f"the default get a different value than tests assume."
    )


# ─── 2. Burst absorption: 30K frames at default maxsize → zero drops ─────────


def test_archiver_absorbs_30k_burst_at_default_maxsize_without_drops(
    monkeypatch,
):
    """RED-to-GREEN regression test for ticket 86ba1xraq.

    Pre-bump (default=10_000): injecting 30K frames produces ~20K drops.
    Post-bump (default=50_000): injecting 30K frames produces 0 drops.

    The 30K burst size is ~2.6× the measured peak overflow on the 2026-05-21
    incident (11.3K, calculated from 10K cap + 1290 measured drops). The
    margin proves the new default has headroom for current load + 2×
    universe growth.

    Test mechanics: construct archiver WITHOUT calling start(), so the
    worker thread does not drain. All 30K frames accumulate in the
    queue. With the pre-bump 10K maxsize, frames 10001-30000 would hit
    queue.Full → ``_dropped_frames`` += 1 each. With the post-bump 50K
    maxsize, all 30K fit → ``_dropped_frames`` stays 0.
    """
    # Default maxsize — NO explicit kwarg. This is the contract: the
    # default must absorb the burst.
    archiver, _, _ = _make_archiver(monkeypatch)

    # Bind sid → orderbook_delta so data frames have a writer to route to.
    _bind_sid(archiver, cmd_id=10, sid=42)

    # Inject 30K data frames. Worker is NOT started, so queue accumulates
    # without draining. All puts must succeed — if maxsize < 30_000, we
    # hit queue.Full and _dropped_frames climbs.
    BURST_SIZE = 30_000
    for i in range(BURST_SIZE):
        archiver._on_frame(_fake_frame(
            {"sid": 42, "seq": i + 1, "type": "orderbook_delta",
             "msg": {"market_ticker": "T1"}},
        ))

    assert archiver._dropped_frames == 0, (
        f"Burst-absorption contract VIOLATED: injected {BURST_SIZE} frames "
        f"into a default-maxsize BronzeArchiver, got "
        f"_dropped_frames={archiver._dropped_frames}. "
        f"The default maxsize is too small for the universal-mode REST-refresh "
        f"cascade burst pattern (measured peak ~11.3K on conn C 2026-05-21). "
        f"Either the default constant was reverted below 30_000, or a refactor "
        f"changed the put_nowait semantics. See ticket 86ba1xraq RCA."
    )

    # Sanity: the queue should hold exactly BURST_SIZE — the _bind_sid
    # ack fires BEFORE the loop (not inside it) and acks are handled
    # inline without enqueueing (D1.3-fu5), so every data-frame put
    # 1:1 corresponds to a qsize increment. qsize() is documented racy
    # under concurrent producer/consumer but the worker is unstarted so
    # only the producer (this thread) writes to it.
    assert archiver._write_queue.qsize() == BURST_SIZE, (
        f"queue qsize={archiver._write_queue.qsize()} after injecting "
        f"{BURST_SIZE} frames; worker is unstarted so no drain has run. "
        f"Expected exactly {BURST_SIZE}. A lower qsize suggests frames "
        f"are being routed elsewhere or dropped silently — investigate."
    )


# ─── 3. Peak instrumentation ─────────────────────────────────────────────────


def test_write_queue_peak_initialized_to_zero(monkeypatch):
    """Peak counter starts at 0 so per-session observability has a known
    floor. Mirrors the ``_dropped_frames`` initialization invariant."""
    archiver, _, _ = _make_archiver(monkeypatch)
    assert hasattr(archiver, "_write_queue_peak"), (
        "BronzeArchiver must expose self._write_queue_peak so callers / "
        "health monitors can observe queue-saturation high-water marks. "
        "Critical for ticket 86ba1xraq's data-driven capacity planning: "
        "without peak instrumentation we cannot know whether the 50K cap "
        "has 4× margin or 2× margin under real load."
    )
    assert archiver._write_queue_peak == 0


def test_write_queue_peak_tracks_high_water_mark(monkeypatch):
    """Peak monotonically grows toward max observed qsize across puts.

    Mechanics: inject N frames without starting worker → qsize grows
    1 by 1 → peak ratchets up with each successful put. Peak must equal
    final qsize (no over-counting, no under-counting).
    """
    archiver, _, _ = _make_archiver(monkeypatch, write_queue_maxsize=1_000)
    _bind_sid(archiver, cmd_id=10, sid=42)

    N = 500
    for i in range(N):
        archiver._on_frame(_fake_frame(
            {"sid": 42, "seq": i + 1, "type": "orderbook_delta",
             "msg": {"market_ticker": "T1"}},
        ))

    # Worker unstarted + single producer thread → peak should track
    # qsize 1:1 across the loop. qsize() is documented racy but with no
    # consumer running there's no race here.
    assert archiver._write_queue_peak == N, (
        f"_write_queue_peak={archiver._write_queue_peak} after {N} puts; "
        f"expected exactly {N}. Peak is not tracking high-water mark — "
        f"capacity planning instrumentation is broken."
    )
    # Sanity: peak <= maxsize (can't exceed the bound).
    assert archiver._write_queue_peak <= archiver._write_queue.maxsize


def test_write_queue_peak_size_in_health_snapshot(monkeypatch):
    """``get_health_snapshot()`` exposes ``write_queue_peak_size`` so the
    bronze_health.json sidecar carries the high-water mark for the
    cron-driven monitor + future capacity-planning queries.

    Additive field — schema_version stays at 1 (per existing additive-
    field invariant: see test_bronze_health_sidecar.py docstring).
    """
    archiver, _, _ = _make_archiver(monkeypatch)
    snap = archiver.get_health_snapshot()
    assert "write_queue_peak_size" in snap, (
        f"get_health_snapshot() missing 'write_queue_peak_size' key. Got: "
        f"{sorted(snap.keys())}. The peak field MUST be in the sidecar so "
        f"operators can see queue-saturation trends without inspecting the "
        f"process. Without it the only signal is post-hoc dropped_frames > 0, "
        f"which is the very class we're trying to eliminate."
    )
    assert isinstance(snap["write_queue_peak_size"], int)
    assert snap["write_queue_peak_size"] == 0


def test_write_queue_peak_resets_on_worker_respawn(monkeypatch):
    """``start()`` on a stopped archiver resets ``_write_queue_peak`` to
    0 alongside ``_dropped_frames`` and ``_drop_log_counter``.

    Per-session observability invariant — the cron monitor reads peak
    deltas tick-to-tick; a peak that persists across restarts would
    make capacity-pressure invisible after the first storm.
    """
    archiver, fake_wire, _ = _make_archiver(monkeypatch, write_queue_maxsize=100)
    _bind_sid(archiver, cmd_id=10, sid=42)

    # Push a few frames so peak > 0.
    for i in range(50):
        archiver._on_frame(_fake_frame(
            {"sid": 42, "seq": i + 1, "type": "orderbook_delta",
             "msg": {"market_ticker": "T1"}},
        ))
    assert archiver._write_queue_peak > 0

    # Simulate worker death — ``start()`` re-spawn path resets counters.
    archiver._write_worker = None  # force re-spawn path on next start()
    fake_wire.start = MagicMock()
    archiver.start()

    assert archiver._write_queue_peak == 0, (
        f"_write_queue_peak={archiver._write_queue_peak} after worker "
        f"respawn; expected 0. Missing reset would make peak observability "
        f"misleading across same-process worker re-spawn (NOT the cgroup-"
        f"OOM-kill / systemd-restart scenario — that path starts a fresh "
        f"process where the counter is naturally 0; this test pins the "
        f"in-process worker-died-and-restarted code path)."
    )

    # Stop cleanly so the test fixture doesn't leak threads.
    archiver.stop()
