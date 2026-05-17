"""D2.2 — CoinbaseArchiver constructor + on_session_start + msg_type→channel
dispatch (ticket 86b9zkppk, 2026-05-17).

D2.1.5 (PR #79, ticket 86b9zkpny) shipped ``coinbase_wire.WSClient`` with
no consumer. D2.2 lands ``collector/coinbase_archiver.py::CoinbaseArchiver``
— the consumer that pipes ``coinbase_wire.Frame`` through
``collector.writer.BronzeWriter`` → JSONL.zst → S3. First Coinbase
bronze-in-S3 chunk = this Bit's acceptance criterion.

Architecture mirror: ``collector/ws_connection.py::BronzeArchiver`` for
the Kalshi side. Adapted for Coinbase Exchange WS:

  - Single-conn (Coinbase Exchange WS doesn't shard the way Kalshi
    does — one connection covers all subscribed product_ids).
  - No sids / no cmd_id_to_channel binding. Coinbase dispatches via
    ``Frame.msg_type`` directly (each frame carries a top-level
    ``type``). The archiver maps msg_type → channel via a static
    ``DEFAULT_MSG_TYPE_TO_CHANNEL`` dict at construction.
  - source="coinbase_ws" on every envelope.
  - on_session_start builds the subscribe payload via the public
    ``coinbase_wire.auth.build_public_subscribe_message`` helper and
    dispatches via the public ``WSClient.send_frame`` API. The earlier
    R1 draft delegated to the wire's private
    ``_default_on_session_start``; that coupling was retracted at
    R1-M1 because a wire-side rename would silently strand the
    consumer (AttributeError swallowed by the wire's outer try/except,
    no subscribe dispatched, no bronze flow, 90s-late silence-
    watchdog alert).

Lessons applied from day-1 (carried from the Kalshi-bronze ship arc):
  - D1.3-fu4 worker-thread decouple: ``_on_frame`` enqueues to a bounded
    ``queue.Queue``; a daemon worker thread does ``build_envelope`` +
    ``writer.write``. The asyncio thread MUST return quickly so the
    keepalive-ping cycle is not starved.
  - D1.3-fu5 skip-ack-enqueue: subscribe-ack frames
    (``type=subscriptions``) are protocol metadata, not market data.
    They are NOT enqueued for bronze writing (avoids the OOM-via-large-
    ack class that bit the Kalshi side 2026-05-17).

What this file pins:

  1. Constructor signature exposes ``writers_by_channel`` (Optional[str]
     channel name → BronzeWriter; ``None`` key is the ``_unrouted``
     fallback) + ``conn_id`` (default ``"A"`` per Coinbase single-conn).
  2. Constructor allocates ``self._wire = WSClient(...)`` passing
     ``on_frame``, ``on_session_start``, ``on_session_end`` callbacks
     (AST-walk guard so a future refactor cannot silently drop the
     callback wiring).
  3. ``on_session_start`` constructs the subscribe payload using the
     public ``coinbase_wire.auth.build_public_subscribe_message``
     helper + dispatches it via the public ``WSClient.send_frame`` API.
     Coinbase Exchange WS lets a single subscribe batch all channels;
     CoinbaseArchiver issues exactly one send_frame per session-start.
     An AST guard forbids reaching into any wire-library private
     attribute (R1-M1 retract of the prior delegate-to-private-method
     coupling).
  4. ``msg_type`` → channel dispatch: data frames with a known
     ``msg_type`` produce envelopes whose ``_channel`` matches the
     dispatch table; frames with unknown/missing ``msg_type`` route
     to the ``None``-keyed ``_unrouted`` writer.
  5. ``source="coinbase_ws"`` on every envelope (verified end-to-end
     via writer's _source check + by inspecting envelopes in
     fixtures).
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path
from unittest.mock import MagicMock

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
COINBASE_ARCHIVER_PATH = REPO_ROOT / "collector" / "coinbase_archiver.py"


# ─── 1. Constructor surface ──────────────────────────────────────────────────


def test_coinbase_archiver_module_exists():
    """The module file exists post-D2.2."""
    assert COINBASE_ARCHIVER_PATH.is_file(), (
        f"{COINBASE_ARCHIVER_PATH.relative_to(REPO_ROOT)} not found — "
        "D2.2 must create the file."
    )


def test_coinbase_archiver_class_exists():
    from collector.coinbase_archiver import CoinbaseArchiver  # noqa: F401


def test_coinbase_archiver_accepts_writers_by_channel_kwarg():
    """Constructor signature exposes the channel-aware dispatch dict."""
    from collector.coinbase_archiver import CoinbaseArchiver
    sig = inspect.signature(CoinbaseArchiver.__init__)
    assert "writers_by_channel" in sig.parameters, (
        "CoinbaseArchiver.__init__ must accept writers_by_channel "
        "(dict[Optional[str], BronzeWriter]) as a keyword argument."
    )


def test_coinbase_archiver_accepts_conn_id_kwarg():
    """Constructor exposes ``conn_id`` (default ``"A"`` per Coinbase
    single-conn shape)."""
    from collector.coinbase_archiver import CoinbaseArchiver
    sig = inspect.signature(CoinbaseArchiver.__init__)
    assert "conn_id" in sig.parameters, (
        "CoinbaseArchiver.__init__ must accept conn_id; bronze envelopes "
        "carry the conn id so silver QA can detect per-conn outages."
    )
    default = sig.parameters["conn_id"].default
    assert default == "A", (
        f"conn_id default expected 'A' for Coinbase single-conn shape; "
        f"got {default!r}."
    )


# ─── 2. AST: callbacks passed to WSClient ────────────────────────────────────


def test_coinbase_archiver_passes_callbacks_to_wsclient():
    """AST walk: collector/coinbase_archiver.py constructs WSClient with
    on_frame + on_session_start + on_session_end kwargs pointing at
    internal callbacks. Without on_session_start, the archiver's
    customized subscribe (built via the public
    ``build_public_subscribe_message`` helper + ``WSClient.send_frame``
    per R1-M1) never dispatches. Without on_session_end, any per-session
    state would survive into the next session.
    """
    src = COINBASE_ARCHIVER_PATH.read_text()
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
        "No WSClient(...) call found in collector/coinbase_archiver.py — "
        "CoinbaseArchiver must construct one."
    )
    for lineno, kwargs in wsclient_calls:
        assert "on_frame" in kwargs, (
            f"collector/coinbase_archiver.py:{lineno} constructs WSClient(...) "
            f"WITHOUT on_frame= (kwargs: {sorted(kwargs)})."
        )
        assert "on_session_start" in kwargs, (
            f"collector/coinbase_archiver.py:{lineno} constructs WSClient(...) "
            f"WITHOUT on_session_start= (kwargs: {sorted(kwargs)}). Without "
            "the session-start callback, the archiver's customized subscribe "
            "(public-API build_public_subscribe_message + send_frame per "
            "R1-M1) never fires."
        )
        assert "on_session_end" in kwargs, (
            f"collector/coinbase_archiver.py:{lineno} constructs WSClient(...) "
            f"WITHOUT on_session_end= (kwargs: {sorted(kwargs)}). Per-session "
            "caches would survive reconnect without it."
        )


# ─── 3. Test fixture ─────────────────────────────────────────────────────────


_ARCHIVERS_TO_CLEANUP: list = []


@pytest.fixture(autouse=True)
def _stop_archivers():
    """Autouse teardown: stop every archiver ``_make_archiver`` spawned
    so the daemon write-worker is joined deterministically."""
    yield
    while _ARCHIVERS_TO_CLEANUP:
        archiver = _ARCHIVERS_TO_CLEANUP.pop()
        try:
            archiver.stop()
        except Exception:
            pass


def _make_archiver(monkeypatch, **overrides):
    """Build a CoinbaseArchiver with a mocked WSClient and dummy writers.

    The underlying ``WSClient`` is mocked so the asyncio thread never
    spawns. The write-worker (D1.3-fu4 subject) IS real and starts when
    archiver.start() is called.
    """
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
    archiver.start()
    _ARCHIVERS_TO_CLEANUP.append(archiver)
    return archiver, fake_wire, writers


def _fake_frame(msg_type, *, raw=None, wire_recv_ts=1_700_000_000.0,
                sequence_num=None, parsed=None):
    """Build a coinbase_wire.Frame for callback-driven dispatch."""
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
        sequence_num=sequence_num,
    )


def _wait_for_write(mock_writer, *, count=1, timeout=5.0, interval=0.005):
    """Bounded polling: returns True once mock_writer.write was called
    at least ``count`` times.
    """
    import time
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if mock_writer.write.call_count >= count:
            return True
        time.sleep(interval)
    return False


# ─── 4. msg_type → channel dispatch ──────────────────────────────────────────


def test_ticker_frame_routes_to_ticker_writer(monkeypatch):
    """type='ticker' frames dispatch to writers_by_channel['ticker']."""
    archiver, _, writers = _make_archiver(monkeypatch)
    archiver._on_frame(_fake_frame(
        "ticker",
        parsed={"type": "ticker", "product_id": "BTC-USD", "price": "50000"},
    ))
    assert _wait_for_write(writers["ticker"]), (
        "ticker frame did not reach writers['ticker'] within timeout."
    )
    envelope = writers["ticker"].write.call_args.args[0]
    assert envelope["_channel"] == "ticker"
    assert envelope["_source"] == "coinbase_ws"


def test_match_frame_routes_to_matches_writer(monkeypatch):
    """type='match' (trade tick) routes to the 'matches' channel writer
    — Coinbase Exchange WS sends ``match`` (singular) frames on the
    ``matches`` (plural) channel.
    """
    archiver, _, writers = _make_archiver(monkeypatch)
    archiver._on_frame(_fake_frame(
        "match",
        parsed={"type": "match", "product_id": "BTC-USD", "size": "0.1",
                "price": "50000"},
    ))
    assert _wait_for_write(writers["matches"]), (
        "match frame did not reach writers['matches'] — verify msg_type "
        "'match' is mapped to channel 'matches' in the dispatch table."
    )
    envelope = writers["matches"].write.call_args.args[0]
    assert envelope["_channel"] == "matches"


def test_heartbeat_frame_routes_to_heartbeat_writer(monkeypatch):
    archiver, _, writers = _make_archiver(monkeypatch)
    archiver._on_frame(_fake_frame(
        "heartbeat",
        parsed={"type": "heartbeat", "product_id": "BTC-USD",
                "last_trade_id": 100},
    ))
    assert _wait_for_write(writers["heartbeat"])
    envelope = writers["heartbeat"].write.call_args.args[0]
    assert envelope["_channel"] == "heartbeat"


def test_status_frame_routes_to_status_writer(monkeypatch):
    archiver, _, writers = _make_archiver(monkeypatch)
    archiver._on_frame(_fake_frame(
        "status",
        parsed={"type": "status", "products": []},
    ))
    assert _wait_for_write(writers["status"])
    envelope = writers["status"].write.call_args.args[0]
    assert envelope["_channel"] == "status"


def test_unknown_msg_type_routes_to_unrouted_writer(monkeypatch):
    """A frame with msg_type not in the dispatch table (e.g., a future
    Coinbase channel we haven't onboarded) routes to the _unrouted
    fallback so bronze captures the bytes for silver QA to investigate.
    """
    archiver, _, writers = _make_archiver(monkeypatch)
    archiver._on_frame(_fake_frame(
        "some_new_channel",
        parsed={"type": "some_new_channel"},
    ))
    assert _wait_for_write(writers[None]), (
        "unknown msg_type did not route to the _unrouted writer "
        "(writers[None]) within timeout."
    )
    envelope = writers[None].write.call_args.args[0]
    assert envelope["_channel"] is None, (
        f"envelope routed to _unrouted must carry _channel=None to match "
        f"the writer's constructor channel; got {envelope.get('_channel')!r}."
    )


def test_missing_msg_type_routes_to_unrouted_writer(monkeypatch):
    """A malformed frame with no ``type`` field (Frame.msg_type=None)
    routes to _unrouted rather than crashing.
    """
    archiver, _, writers = _make_archiver(monkeypatch)
    archiver._on_frame(_fake_frame(None, parsed={"no_type_field": True}))
    assert _wait_for_write(writers[None])


# ─── 5. wire_recv_ts captured at ingress (D0.3 §2 carryover) ─────────────────


def test_envelope_carries_frame_wire_recv_ts(monkeypatch):
    """The D0.3 §2 invariant: envelope ``_wire_recv_ts`` must come from
    ``Frame.wire_recv_ts`` (set at WSClient ingress BEFORE json.loads),
    NOT from a fresh ``datetime.now()`` at envelope-build time.
    """
    archiver, _, writers = _make_archiver(monkeypatch)
    archiver._on_frame(_fake_frame(
        "ticker",
        wire_recv_ts=1_700_000_001.234567,
        parsed={"type": "ticker", "product_id": "BTC-USD"},
    ))
    assert _wait_for_write(writers["ticker"])
    envelope = writers["ticker"].write.call_args.args[0]
    ts_str = envelope["_wire_recv_ts"]
    assert ts_str.endswith(".234567Z"), (
        f"wire_recv_ts µs precision lost: {ts_str}. D0.3 §2 requires "
        "Frame.wire_recv_ts to flow into build_envelope unchanged."
    )


# ─── 6. on_session_end clears archiver state ─────────────────────────────────


def test_on_session_end_does_not_raise(monkeypatch):
    """on_session_end runs cleanly even when there's no per-session
    state to reset (Coinbase has no sids, so the callback is mostly a
    placeholder for the R3/P0-A invariant)."""
    archiver, _, _ = _make_archiver(monkeypatch)
    # Should not raise; idempotent.
    archiver._on_session_end()
    archiver._on_session_end()


# ─── 6b. on_session_start dispatches subscribe via public API ────────────────


def test_on_session_start_dispatches_subscribe_via_send_frame(monkeypatch):
    """R1-M1 pin: the consumer MUST construct the subscribe payload
    itself and dispatch via the public ``WSClient.send_frame`` — NOT
    by reaching into ``self._wire._default_on_session_start()``
    (private method, no contract).

    Verifies that on_session_start results in exactly one
    ``send_frame(payload)`` call where ``payload`` is a Coinbase
    Exchange WS subscribe message (``type=subscribe`` + ``channels``
    list + ``product_ids`` list).
    """
    archiver, fake_wire, _ = _make_archiver(monkeypatch)
    archiver._on_session_start()
    assert fake_wire.send_frame.call_count == 1, (
        f"on_session_start should dispatch exactly one batched subscribe "
        f"frame; got {fake_wire.send_frame.call_count} calls."
    )
    payload = fake_wire.send_frame.call_args.args[0]
    assert isinstance(payload, dict)
    assert payload.get("type") == "subscribe"
    assert isinstance(payload.get("channels"), list)
    assert isinstance(payload.get("product_ids"), list)
    assert payload["channels"], "channels list must be non-empty"
    assert payload["product_ids"], "product_ids list must be non-empty"


def test_on_session_start_does_not_reach_into_private_wire_method(monkeypatch):
    """R1-M1 defense-in-depth: AST guard — ``_on_session_start`` must
    NOT reference any wire-library private attribute (single underscore
    + non-dunder name on the ``self._wire`` object). Private-method
    coupling is the silent-partial-failure class R1 caught.
    """
    import ast
    src = COINBASE_ARCHIVER_PATH.read_text()
    tree = ast.parse(src)
    on_session_start_fn = None
    for node in ast.walk(tree):
        if (isinstance(node, ast.FunctionDef)
                and node.name == "_on_session_start"):
            on_session_start_fn = node
            break
    assert on_session_start_fn is not None, (
        "_on_session_start not found in collector/coinbase_archiver.py."
    )

    forbidden_attrs: list[tuple[int, str]] = []
    for node in ast.walk(on_session_start_fn):
        if isinstance(node, ast.Attribute):
            # Is the value `self._wire` (Attribute whose value is Name(id=self)
            # and attr is _wire)?
            val = node.value
            if (isinstance(val, ast.Attribute)
                    and val.attr == "_wire"
                    and isinstance(val.value, ast.Name)
                    and val.value.id == "self"):
                # node.attr is the method/attr name being called on _wire.
                # Dunder names + non-leading-underscore names are OK; single-
                # leading-underscore + non-dunder is private and forbidden.
                attr = node.attr
                if (attr.startswith("_")
                        and not attr.startswith("__")):
                    forbidden_attrs.append((node.lineno, attr))
    assert not forbidden_attrs, (
        f"_on_session_start reaches into wire-library private attrs "
        f"{forbidden_attrs}. R1-M1 RCA: private-method coupling is a "
        f"silent-partial-failure class (wire-side rename → "
        f"AttributeError swallowed by the wire's outer try/except → "
        f"no subscribe → no bronze). Use only public WSClient API."
    )


# ─── 7. envelope _source is always coinbase_ws ────────────────────────────


def test_every_envelope_carries_source_coinbase_ws(monkeypatch):
    """Defense-in-depth: any envelope emitted by the archiver carries
    _source='coinbase_ws'. Kalshi-side would be 'kalshi_ws'; mixing
    them would corrupt the bronze partition path.
    """
    archiver, _, writers = _make_archiver(monkeypatch)
    for msg_type in ("ticker", "match", "heartbeat", "status"):
        archiver._on_frame(_fake_frame(
            msg_type,
            parsed={"type": msg_type, "product_id": "BTC-USD"},
        ))
    # Wait for each writer to receive at least one write.
    expected = {"ticker": "ticker", "match": "matches", "heartbeat": "heartbeat",
                "status": "status"}
    for _msg_type, channel in expected.items():
        assert _wait_for_write(writers[channel])
        env = writers[channel].write.call_args.args[0]
        assert env["_source"] == "coinbase_ws", (
            f"writer[{channel!r}] got envelope with _source="
            f"{env.get('_source')!r}, expected 'coinbase_ws'."
        )
