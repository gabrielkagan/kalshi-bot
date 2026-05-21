"""P1-B-brutalist Phase B1 slice 2/3 — BronzeArchiver substring parsing.

Ticket `86ba1qbf4` (2026-05-20). With `WSClient(parse_on_demand=True)`
the BronzeArchiver receives `Frame(raw, wire_recv_ts)` with
`msg_type/sid/seq/parsed` ALL None. The collector must do
substring-based extraction of the minimal fields it routes on:

  - `_substring_detect_ack(raw)` — True iff raw contains an ack-type
    marker in the leading window. Mirrors `_SUBSCRIBE_ACK_TYPES`.
  - `_substring_extract_sid(raw)` — extract sid integer via regex
    on the raw frame string.

These helpers MUST be cheap (no json.loads, no dict alloc) to deliver
the brutalist throughput win. Ack frames are rare + small, so they can
still pay json.loads on the ack body inside `_handle_subscribe_ack`
(needed for cmd_id binding); data frames must skip json.loads
entirely.

Additionally, `BronzeArchiver.__init__` must construct
`WSClient(parse_on_demand=True)` so the wire stops calling json.loads
on every frame.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WS_CONNECTION_PY = REPO_ROOT / "collector" / "ws_connection.py"


def _read_source() -> str:
    assert WS_CONNECTION_PY.exists()
    return WS_CONNECTION_PY.read_text()


def test_substring_detect_ack_helper_exists():
    """Module must expose a substring-based ack-detection helper."""
    source = _read_source()
    tree = ast.parse(source)
    accepted = {
        "_substring_detect_ack",
        "_is_ack_substring",
        "_detect_ack_substring",
        "_substring_is_ack",
    }
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in accepted:
            found = True
            break
    assert found, (
        f"Expected a module-level substring-based ack helper "
        f"(one of {sorted(accepted)}). Phase B1 slice 2 requires "
        "this helper to avoid json.loads on every data frame."
    )


def test_substring_extract_sid_helper_exists():
    """Module must expose a substring-based sid-extraction helper."""
    source = _read_source()
    tree = ast.parse(source)
    accepted = {
        "_substring_extract_sid",
        "_extract_sid_substring",
        "_substring_sid",
    }
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in accepted:
            found = True
            break
    assert found, (
        f"Expected a module-level substring-based sid extractor "
        f"(one of {sorted(accepted)}). Phase B1 slice 2."
    )


def test_substring_detect_ack_matches_known_ack_types():
    """The helper must return True for the canonical Kalshi subscribe-
    ack types (``_SUBSCRIBE_ACK_TYPES`` = ``{"subscribed", "ok"}``) and
    False for both data frames AND non-subscribe-ack message types.

    R1 hardening (2026-05-20): an earlier draft over-matched
    ``"error"`` and ``"subscriptions"``. ``error`` is a generic
    command-response shape (NOT a subscribe-ack); pre-B1 it routed to
    the ``_unrouted`` bronze partition for silver-side diagnostic
    capture, and we preserve that semantics by NOT matching here.
    ``subscriptions`` is a Coinbase Exchange WS shape with no analog
    in the Kalshi protocol; including it cross-contaminated the
    Kalshi-side regex with a Coinbase pattern.
    """
    from collector.ws_connection import (  # type: ignore[attr-defined]
        _SUBSCRIBE_ACK_TYPES,
        _substring_detect_ack,
    )
    assert _SUBSCRIBE_ACK_TYPES == frozenset({"subscribed", "ok"}), (
        "Canonical Kalshi subscribe-ack set drifted — update both this "
        "test and the regex if the protocol genuinely extended."
    )
    # Positive cases — both canonical Kalshi ack shapes.
    assert _substring_detect_ack(
        '{"id":1,"type":"subscribed","msg":{"channel":"orderbook_delta","sid":42}}'
    )
    assert _substring_detect_ack(
        '{"id":2,"type":"ok","sid":42,"seq":1}'
    )
    # Negative cases — error + subscriptions are NOT subscribe-acks;
    # data frames are not acks either.
    assert not _substring_detect_ack(
        '{"id":3,"type":"error","msg":{"reason":"bad ticker"}}'
    ), "type=error must route through the data-frame path to _unrouted"
    assert not _substring_detect_ack(
        '{"type":"subscriptions","msg":{"channels":["orderbook_delta"]}}'
    ), "type=subscriptions is a Coinbase shape, not Kalshi"
    assert not _substring_detect_ack(
        '{"type":"orderbook_delta","sid":42,"seq":100,"msg":{"yes":[[50,100]],"no":[]}}'
    )
    assert not _substring_detect_ack(
        '{"type":"trade","sid":42,"seq":101,"msg":{"price":50,"count":1}}'
    )


def test_substring_extract_sid_returns_int():
    """The helper must return the sid as int from various frame shapes."""
    from collector.ws_connection import (  # type: ignore[attr-defined]
        _substring_extract_sid,
    )
    assert (
        _substring_extract_sid(
            '{"type":"orderbook_delta","sid":42,"seq":1,"msg":{}}'
        )
        == 42
    )
    assert (
        _substring_extract_sid(
            '{"id":1,"type":"subscribed","msg":{"channel":"orderbook_delta","sid":99}}'
        )
        == 99
    )
    assert _substring_extract_sid('{"type":"subscriptions"}') is None


def test_bronze_archiver_constructs_wsclient_with_parse_on_demand_true():
    """``BronzeArchiver.__init__`` must construct ``WSClient(...,
    parse_on_demand=True)``.

    AST guard: the WSClient constructor call inside __init__ must
    include a literal True for parse_on_demand.
    """
    source = _read_source()
    tree = ast.parse(source)
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "BronzeArchiver":
            for item in node.body:
                if (
                    isinstance(item, ast.FunctionDef)
                    and item.name == "__init__"
                ):
                    for sub in ast.walk(item):
                        if (
                            isinstance(sub, ast.Call)
                            and isinstance(sub.func, ast.Name)
                            and sub.func.id == "WSClient"
                        ):
                            for kw in sub.keywords:
                                if (
                                    kw.arg == "parse_on_demand"
                                    and isinstance(kw.value, ast.Constant)
                                    and kw.value.value is True
                                ):
                                    found = True
                                    break
    assert found, (
        "BronzeArchiver.__init__ must construct WSClient(..., "
        "parse_on_demand=True). Phase B1 slice 3."
    )


def test_on_frame_uses_substring_ack_detection():
    """``_on_frame`` must use the substring helper for ack detection,
    NOT ``frame.msg_type in _SUBSCRIBE_ACK_TYPES``.
    """
    source = _read_source()
    tree = ast.parse(source)
    on_frame_src = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "BronzeArchiver":
            for item in node.body:
                if (
                    isinstance(item, ast.FunctionDef)
                    and item.name == "_on_frame"
                ):
                    on_frame_src = ast.get_source_segment(source, item) or ""
                    break
    assert on_frame_src is not None
    accepted = (
        "_substring_detect_ack",
        "_is_ack_substring",
        "_detect_ack_substring",
        "_substring_is_ack",
    )
    found = any(name in on_frame_src for name in accepted)
    assert found, (
        f"BronzeArchiver._on_frame must call a substring ack helper "
        f"(one of {accepted}) instead of relying on Frame.msg_type."
    )
