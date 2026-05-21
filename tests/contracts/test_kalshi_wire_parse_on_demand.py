"""P1-B-brutalist Phase B1 — WSClient ``parse_on_demand`` kwarg.

Ticket `86ba1qbf4` (2026-05-20). Architectural pivot: collector
needs to skip the per-frame ``json.loads`` in
``kalshi_wire/ws_client.py::_handle_raw_frame`` — at 705K-ticker
universal mode the orderbook_delta frames allocate 50-100 nested
dicts per frame, which is the dominant GIL-bound work that prevents
single-process universal-mode throughput.

The fix: ``parse_on_demand: bool = False`` constructor kwarg on
``WSClient``. When True (collector enables), skip ``json.loads`` and
build ``Frame(raw=raw, wire_recv_ts=now)`` with other fields None.
Consumer (BronzeArchiver) does substring-based parsing for the
minimal fields it needs (ack-type + sid). When False (bot keeps
default), behavior is byte-equivalent to pre-Bit ws_client.

This is Phase B1 of the brutalist Bit; Phase B2 (worker-side
byte-template) and Phase B3 (schema v2) are separate fix-up Bits.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WS_CLIENT_PY = REPO_ROOT / "kalshi_wire" / "ws_client.py"


def _read_source() -> str:
    assert WS_CLIENT_PY.exists()
    return WS_CLIENT_PY.read_text()


def _get_init_signature():
    """Return the AST args object for WSClient.__init__."""
    source = _read_source()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "WSClient":
            for item in node.body:
                if (
                    isinstance(item, ast.FunctionDef)
                    and item.name == "__init__"
                ):
                    return item.args
    raise AssertionError("Could not locate WSClient.__init__")


def test_init_signature_accepts_parse_on_demand_kwarg():
    """``WSClient.__init__`` must accept ``parse_on_demand: bool`` kwarg.

    Default MUST be False so existing consumers (bot's KalshiFeed) get
    unchanged behavior — they continue to receive parsed Frame objects.
    Only the collector opts into True for the GIL-bound parse skip.
    """
    args = _get_init_signature()
    kw_names = [a.arg for a in args.kwonlyargs]
    pos_names = [a.arg for a in args.args]
    all_names = kw_names + pos_names
    assert "parse_on_demand" in all_names, (
        "WSClient.__init__ must accept `parse_on_demand` kwarg. "
        "Phase B1 brutalist: collector sets True to skip per-frame "
        "json.loads (the dominant GIL-bound work at 705K tickers). "
        "Bot leaves default False for unchanged Frame.parsed access. "
        "See kb/decisions/p1-collector-universal-tuning-plan.md Phase B1."
    )


def test_parse_on_demand_defaults_to_false():
    """Default MUST be ``False`` so bot's KalshiFeed continues to receive
    parsed Frame objects (Frame.parsed, .msg_type, .sid, .seq all
    populated). Only callers that explicitly pass True opt into the
    raw-bytes-only Frame for the GIL-skip win.

    Flipping default to True would silently break the bot's trading
    decisions that read frame.parsed/frame.msg_type — sacred-boundary
    violation per the bot's no-async + parsed-Frame-consumer contract.
    """
    args = _get_init_signature()
    kw_args = args.kwonlyargs
    defaults = args.kw_defaults
    for arg, default in zip(kw_args, defaults):
        if arg.arg == "parse_on_demand":
            assert isinstance(default, ast.Constant) and default.value is False, (
                f"parse_on_demand default must be False (literal). "
                f"Got: {ast.dump(default) if default else 'None'}. "
                "Bot trading depends on parsed Frame; flipping default "
                "True would silently break it. Only the collector should "
                "opt into True."
            )
            return
    raise AssertionError(
        "parse_on_demand kwarg not found in WSClient.__init__ kwonly args."
    )


def test_handle_raw_frame_branches_on_parse_on_demand():
    """``WSClient._handle_raw_frame`` must conditionally skip
    ``json.loads`` when ``self._parse_on_demand`` is True.

    AST guard: the method body should reference ``self._parse_on_demand``
    OR the local equivalent, indicating it branches on the flag.

    Without this branch, the kwarg is silently ignored and Phase B1
    delivers zero throughput win.
    """
    source = _read_source()
    tree = ast.parse(source)
    handle_raw_body_src = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "WSClient":
            for item in node.body:
                if (
                    isinstance(item, ast.FunctionDef)
                    and item.name == "_handle_raw_frame"
                ):
                    handle_raw_body_src = ast.get_source_segment(
                        source, item
                    ) or ""
                    break
    assert handle_raw_body_src is not None, (
        "Could not locate WSClient._handle_raw_frame method."
    )
    assert "parse_on_demand" in handle_raw_body_src, (
        "_handle_raw_frame body does not reference parse_on_demand — "
        "the kwarg is ignored. Phase B1 brutalist requires the method "
        "to branch on the flag and skip json.loads when True."
    )


def test_handle_raw_frame_still_does_json_loads_in_default_branch():
    """Negative pin: in the default (parse_on_demand=False) branch,
    ``_handle_raw_frame`` MUST still call ``json.loads(raw)`` to
    populate ``Frame.parsed`` for bot consumers.

    The collector-side skip is conditional on the flag. Removing
    json.loads entirely would break the bot.
    """
    source = _read_source()
    # Look for json.loads call in the method (any branch).
    # AST traversal scoped to the method.
    tree = ast.parse(source)
    found = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "WSClient":
            for item in node.body:
                if (
                    isinstance(item, ast.FunctionDef)
                    and item.name == "_handle_raw_frame"
                ):
                    for sub in ast.walk(item):
                        if (
                            isinstance(sub, ast.Attribute)
                            and sub.attr == "loads"
                            and isinstance(sub.value, ast.Name)
                            and sub.value.id == "json"
                        ):
                            found = True
                            break
    assert found, (
        "_handle_raw_frame no longer calls json.loads — would break "
        "bot's parsed Frame consumers (KalshiFeed reads Frame.parsed "
        "for trading decisions). Phase B1 makes json.loads CONDITIONAL "
        "on parse_on_demand, not unconditional removal."
    )
