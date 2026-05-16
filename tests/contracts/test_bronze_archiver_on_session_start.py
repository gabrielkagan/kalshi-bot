"""D1.3 — BronzeArchiver gains on_session_start + sid→channel mapping
(ticket 86b9ypn72, 2026-05-16).

D1.2 closeout R1-C3 acceptance criterion: D1.2 ships wire-up; D1.3 ships
first-bronze-flow. The hand-off lands here — ``BronzeArchiver`` constructor
accepts pre-built subscribe frames + a cmd_id→channel map, dispatches
the subscribes on ``WSClient.on_session_start``, and binds sid→channel on
subscribe-ack so subsequent data frames route to the channel-specific
``BronzeWriter`` instance.

What this file pins:

  1. Constructor accepts ``writers_by_channel`` (dict keyed by Optional[str]
     channel name → BronzeWriter), ``subscribe_frames`` (sequence of pre-built
     payloads), and ``cmd_id_to_channel`` (mapping for ack correlation).
  2. AST-walk: ``on_session_start=`` kwarg is passed to ``WSClient(...)``
     so the callback actually fires.
  3. On session_start, every subscribe frame is dispatched via
     ``self._wire.send_frame`` (mirrors bot/feeds/kalshi.py reconnect-time
     re-subscribe).
  4. Subscribe-ack handling: ``type=subscribed`` and ``type=ok`` frames
     bind their ``sid`` to the channel that issued the originating cmd_id
     (per Kalshi WS spec — subscribed/ok both carry the sid).
  5. Data-frame routing: a frame with ``sid`` matched in the sid→channel
     map produces an envelope with ``_channel`` populated (not None) and
     routes to ``writers_by_channel[channel]``.
  6. Unmatched-sid fallback: a data frame whose sid isn't in the map yet
     (race between subscribe-burst and first data frame) routes to the
     ``_unrouted`` writer at ``writers_by_channel[None]``.
  7. ``on_session_end`` clears the sid→channel map so the next session
     (sids are session-scoped per Kalshi) starts clean — matches the
     R3/P0-A invariant from kalshi_wire.WSClient.
"""
from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
WS_CONNECTION_PATH = REPO_ROOT / "collector" / "ws_connection.py"


# ─── 1. Constructor surface ──────────────────────────────────────────────────


def test_bronze_archiver_accepts_writers_by_channel_kwarg():
    """Constructor signature exposes the channel-aware dispatch dict."""
    from collector.ws_connection import BronzeArchiver
    import inspect
    sig = inspect.signature(BronzeArchiver.__init__)
    assert "writers_by_channel" in sig.parameters, (
        "BronzeArchiver.__init__ must accept writers_by_channel "
        "(dict[Optional[str], BronzeWriter]) as a keyword argument."
    )


def test_bronze_archiver_accepts_subscribe_frames_kwarg():
    from collector.ws_connection import BronzeArchiver
    import inspect
    sig = inspect.signature(BronzeArchiver.__init__)
    assert "subscribe_frames" in sig.parameters, (
        "BronzeArchiver.__init__ must accept subscribe_frames "
        "(Sequence[Dict]) so on_session_start can dispatch them."
    )


def test_bronze_archiver_accepts_cmd_id_to_channel_kwarg():
    from collector.ws_connection import BronzeArchiver
    import inspect
    sig = inspect.signature(BronzeArchiver.__init__)
    assert "cmd_id_to_channel" in sig.parameters, (
        "BronzeArchiver.__init__ must accept cmd_id_to_channel "
        "(Mapping[int, str]) for subscribe-ack → sid→channel binding."
    )


# ─── 2. AST: on_session_start kwarg passed to WSClient ───────────────────────


def test_collector_ws_connection_passes_on_session_start_to_wsclient():
    """AST walk: collector/ws_connection.py constructs WSClient with the
    on_session_start kwarg pointing at an internal callback. Without this,
    the subscribe frames never dispatch and bronze never flows (D1.2 R1-C3
    deferred surface).
    """
    src = WS_CONNECTION_PATH.read_text()
    tree = ast.parse(src)
    wsclient_calls: list[tuple[int, set[str]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            fn = node.func
            fn_name = None
            if isinstance(fn, ast.Name):
                fn_name = fn.id
            elif isinstance(fn, ast.Attribute):
                fn_name = fn.attr
            if fn_name == "WSClient":
                kwargs = {kw.arg for kw in node.keywords if kw.arg}
                wsclient_calls.append((node.lineno, kwargs))
    assert wsclient_calls, (
        "No WSClient(...) call found in collector/ws_connection.py — "
        "BronzeArchiver must construct one."
    )
    for lineno, kwargs in wsclient_calls:
        assert "on_session_start" in kwargs, (
            f"collector/ws_connection.py:{lineno} constructs WSClient(...) "
            f"WITHOUT on_session_start= (kwargs: {sorted(kwargs)}). D1.3 "
            "needs the session-start callback to dispatch subscribe frames "
            "— without it, first-bronze-flow never happens."
        )
        assert "on_session_end" in kwargs, (
            f"collector/ws_connection.py:{lineno} constructs WSClient(...) "
            f"WITHOUT on_session_end= (kwargs: {sorted(kwargs)}). D1.3 needs "
            "the session-end callback to clear sid→channel state (sids are "
            "Kalshi-session-scoped per kalshi_wire R3/P0-A invariant)."
        )


# ─── 3. on_session_start dispatches all subscribe frames ─────────────────────


def _make_archiver(monkeypatch, **overrides):
    """Helper: build a BronzeArchiver with a mocked WSClient and dummy writers.

    The BronzeArchiver's _wire (WSClient) is replaced after construction so
    on_session_start dispatches via the mock's ``send_frame``.
    """
    from collector import ws_connection as wc

    # Mock load_private_key BEFORE the BronzeArchiver ctor runs so a missing
    # PEM doesn't error before we can swap _wire.
    monkeypatch.setattr(
        wc, "load_private_key", lambda _p: object()
    )
    # Mock WSClient so the asyncio thread never spawns.
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
        {"id": 11, "cmd": "subscribe",
         "params": {"channels": ["trade"], "market_tickers": ["T1"]}},
    ])
    cmd_id_to_channel = overrides.pop("cmd_id_to_channel", {
        10: "orderbook_delta",
        11: "trade",
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


def test_on_session_start_dispatches_every_subscribe_frame(monkeypatch):
    """All N subscribe frames pass through ``self._wire.send_frame`` on
    session_start, in the order the planner emitted them.
    """
    archiver, fake_wire, _ = _make_archiver(monkeypatch)
    # Invoke the callback the WSClient would call.
    archiver._on_session_start()
    # send_frame should be called once per subscribe frame, with the same
    # payload dict.
    assert fake_wire.send_frame.call_count == 2
    args_list = [c.args[0] for c in fake_wire.send_frame.call_args_list]
    assert args_list[0]["id"] == 10
    assert args_list[1]["id"] == 11
    assert args_list[0]["params"]["channels"] == ["orderbook_delta"]
    assert args_list[1]["params"]["channels"] == ["trade"]


def test_on_session_start_swallows_send_frame_exceptions(monkeypatch):
    """If WSClient.send_frame raises (race with disconnect), the callback
    keeps iterating remaining frames rather than letting one bad frame
    abort the whole session-start dispatch. Mirrors bot/feeds/kalshi.py's
    try/except wrapper around _send_ob_subscribe in _on_session_start.
    """
    archiver, fake_wire, _ = _make_archiver(monkeypatch)
    fake_wire.send_frame.side_effect = [ConnectionError("WS not connected"), None]
    # Should NOT raise.
    archiver._on_session_start()
    assert fake_wire.send_frame.call_count == 2, (
        "second subscribe frame skipped after first raised — should iterate "
        "all frames defensively."
    )


# ─── 4. sid→channel binding on subscribe-ack ─────────────────────────────────


def _fake_frame(raw_dict, *, msg_type=None, sid=None, seq=None):
    """Build a fake kalshi_wire.Frame for callback-driven dispatch."""
    import json
    from kalshi_wire.ws_client import Frame
    raw = json.dumps(raw_dict)
    return Frame(
        wire_recv_ts=1_700_000_000.0,
        raw=raw,
        parsed=raw_dict,
        msg_type=msg_type if msg_type is not None else raw_dict.get("type"),
        sid=sid if sid is not None else raw_dict.get("sid"),
        seq=seq if seq is not None else raw_dict.get("seq"),
    )


def test_subscribed_ack_binds_sid_to_channel(monkeypatch):
    """``type=subscribed`` carries sid in ``msg.sid``; the cmd_id echoed
    back lets us match to the channel we issued. After the ack, future
    data frames with this sid resolve to the correct channel.
    """
    archiver, _, writers = _make_archiver(monkeypatch)
    # Kalshi's type=subscribed shape: {"id": cmd_id, "type": "subscribed",
    # "msg": {"channel": "orderbook_delta", "sid": 42}}
    ack = _fake_frame(
        {"id": 10, "type": "subscribed",
         "msg": {"channel": "orderbook_delta", "sid": 42}},
    )
    archiver._on_frame(ack)
    # Now a data frame with sid=42 should route to orderbook_delta writer.
    data = _fake_frame(
        {"sid": 42, "seq": 1, "type": "orderbook_delta",
         "msg": {"market_ticker": "T1", "price_dollars": "0.55",
                 "delta_fp": "10", "side": "yes"}},
    )
    archiver._on_frame(data)
    # The orderbook_delta writer should have been called with an envelope
    # whose _channel is "orderbook_delta".
    assert writers["orderbook_delta"].call_count + len(
        writers["orderbook_delta"].call_args_list
    ) >= 1 or writers["orderbook_delta"].write.call_count >= 1, (
        "orderbook_delta writer was not invoked after sid binding"
    )


def test_ok_ack_binds_sid_to_channel(monkeypatch):
    """``type=ok`` is the post-establishment ack shape — sid lives at the
    envelope top level, not inside msg.
    """
    archiver, _, writers = _make_archiver(monkeypatch)
    ack = _fake_frame(
        {"id": 11, "type": "ok", "sid": 99, "seq": 5,
         "msg": {"market_tickers": ["T1"]}},
    )
    archiver._on_frame(ack)
    data = _fake_frame(
        {"sid": 99, "seq": 1, "type": "trade",
         "msg": {"market_ticker": "T1", "price": "0.50", "size": "10"}},
    )
    archiver._on_frame(data)
    # writers["trade"] should have a .write() call where envelope._channel="trade"
    assert writers["trade"].call_count + writers["trade"].write.call_count >= 1


# ─── 5. data-frame routing via sid→channel map ───────────────────────────────


def test_data_frame_with_unmapped_sid_routes_to_unrouted_writer(monkeypatch):
    """A data frame arriving BEFORE the subscribe-ack lands (race window)
    has a sid we haven't bound to a channel yet. Route to the _unrouted
    writer (None-key) so we don't drop the frame — bronze captures
    everything, the unrouted partition keeps the bytes for silver QA.
    """
    archiver, _, writers = _make_archiver(monkeypatch)
    # No prior subscribe-ack → sid=42 is unmapped.
    data = _fake_frame(
        {"sid": 42, "seq": 1, "type": "orderbook_delta",
         "msg": {"market_ticker": "T1"}},
    )
    archiver._on_frame(data)
    assert writers[None].write.call_count == 1, (
        "Unmapped sid did not route to the _unrouted writer."
    )
    # Envelope routed to _unrouted must carry _channel=None to match
    # the writer's constructor channel.
    envelope = writers[None].write.call_args.args[0]
    assert envelope["_channel"] is None


def test_data_frame_with_mapped_sid_envelope_channel_populated(monkeypatch):
    """After subscribe-ack binds sid=42 → orderbook_delta, the envelope
    for sid=42 data frames has ``_channel="orderbook_delta"`` (not None).
    This is the load-bearing D1.3 outcome — bronze partitions stop going
    to ``_unrouted/`` once sid mapping is established.
    """
    archiver, _, writers = _make_archiver(monkeypatch)
    archiver._on_frame(_fake_frame(
        {"id": 10, "type": "subscribed",
         "msg": {"channel": "orderbook_delta", "sid": 42}},
    ))
    archiver._on_frame(_fake_frame(
        {"sid": 42, "seq": 1, "type": "orderbook_delta",
         "msg": {"market_ticker": "T1"}},
    ))
    envelope = writers["orderbook_delta"].write.call_args.args[0]
    assert envelope["_channel"] == "orderbook_delta", (
        f"envelope _channel should be `orderbook_delta` after sid binding; "
        f"got {envelope.get('_channel')!r}"
    )


# ─── 6. on_session_end clears sid→channel map (R3/P0-A invariant) ───────────


def test_on_session_end_clears_sid_to_channel_map(monkeypatch):
    """Sids are Kalshi-session-scoped — a fresh WS session gets fresh sids
    starting at the same low numbers. If we don't clear, a stale binding
    from session N would mis-route data frames from session N+1.
    """
    archiver, _, _ = _make_archiver(monkeypatch)
    archiver._on_frame(_fake_frame(
        {"id": 10, "type": "subscribed",
         "msg": {"channel": "orderbook_delta", "sid": 42}},
    ))
    # Before session_end: sid=42 mapped.
    assert archiver._sid_to_channel.get(42) == "orderbook_delta"
    archiver._on_session_end()
    # After session_end: cleared.
    assert 42 not in archiver._sid_to_channel, (
        "sid_to_channel still contains stale binding after on_session_end — "
        "violates the R3/P0-A session-scoped reset invariant inherited from "
        "kalshi_wire.WSClient."
    )


# ─── 7. wire_recv_ts is still captured at ingress (D1.2 R1-C1 carryover) ────


def test_data_frame_envelope_carries_frame_wire_recv_ts(monkeypatch):
    """The R1-C1 invariant from D1.2 stays load-bearing in D1.3: even with
    channel-aware routing, the envelope's ``_wire_recv_ts`` must come from
    ``Frame.wire_recv_ts`` (set at WSClient ingress BEFORE json.loads), NOT
    from a fresh ``datetime.now()`` call at envelope-build time.
    """
    archiver, _, writers = _make_archiver(monkeypatch)
    archiver._on_frame(_fake_frame(
        {"id": 10, "type": "subscribed",
         "msg": {"channel": "orderbook_delta", "sid": 42}},
    ))
    # Use a distinctive wire_recv_ts to verify it survives routing.
    from kalshi_wire.ws_client import Frame
    frame = Frame(
        wire_recv_ts=1_700_000_001.234567,
        raw='{"sid":42,"seq":1,"type":"orderbook_delta","msg":{"market_ticker":"T1"}}',
        parsed={"sid": 42, "seq": 1, "type": "orderbook_delta",
                "msg": {"market_ticker": "T1"}},
        msg_type="orderbook_delta",
        sid=42, seq=1,
    )
    archiver._on_frame(frame)
    envelope = writers["orderbook_delta"].write.call_args.args[0]
    # ISO-8601 µs serialization: "1970-08-15T01:53:21.234567Z" — first
    # 7 chars match year/month/day so we can spot-check the suffix.
    ts_str = envelope["_wire_recv_ts"]
    assert ts_str.endswith(".234567Z"), (
        f"wire_recv_ts µs precision lost: {ts_str}. The R1-C1 invariant "
        "requires Frame.wire_recv_ts to flow into build_envelope unchanged."
    )
