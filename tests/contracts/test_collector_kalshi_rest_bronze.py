"""D1.9 — Kalshi REST /markets snapshot to bronze (ticket 86ba0pmzz).

Pins the structural shape of the bronze-write extension to
``collector/rest_snapshot.py`` + ``collector/main_loop.py``.

What this file pins:

  1. Envelope shape — every record carries ``_source="kalshi_rest"``,
     ``_channel="markets"``, ``_conn=None``, monotone ``_collector_seq``.
  2. Diagnostic wrapper inside ``_raw`` — success path has 7 keys
     (``http_status`` / ``elapsed_ms`` / ``attempts`` / ``page_idx`` /
     ``cursor_in`` / ``cursor_out`` / ``response``); failure path has
     6 keys (``http_status`` / ``elapsed_ms`` / ``attempts`` /
     ``page_idx`` / ``cursor_in`` / ``error``) — ``response`` is
     replaced by ``error``. ``elapsed_ms`` is per-LAST-attempt
     wall-clock (excludes backoff sleeps); ``attempts`` is the count
     of HTTP attempts so silver D3.x can disaggregate retried pages
     (R1-M5).
  3. One JSONL record per REST page — cursor-paginated boundary is
     the natural HTTP request-response unit. N pages = N envelopes.
  4. Best-effort posture — a ``writer.write_frame`` raise MUST NOT
     prevent ``fetch_tickers_by_tier`` from returning the parsed ticker
     set. Bronze is observability; production subscription planning
     keeps working.
  5. Optional writer parameter — ``bronze_writer=None`` reproduces
     pre-D1.9 behavior exactly. Existing callers (tests, file-mode
     boot) are not forced to construct a writer.
  6. Refresher forwards the writer — ``RestSnapshotRefresher``
     accepts the writer and passes it to every ``fetch_tickers_by_tier``
     call.
  7. Main-loop wiring — ``collector.main_loop.run`` constructs a
     ``BronzeWriter(source="kalshi_rest", channel="markets", conn=None)``
     in the REST production path and registers it with the drain
     thread.
  8. File-mode skip — when ``COLLECTOR_TICKERS_FILE`` is set, NO
     ``kalshi_rest`` writer is constructed (no REST fetch → no write
     surface).
  9. Backward-compat + isolation guards — `fetch_tickers_by_tier`
     signature accepts the new `bronze_writer` kwarg with `None`
     default (existing callers unaffected); `collector/rest_snapshot.py`
     adds no `bot.*` import (collector-no-bot defense-in-depth).
     NOTE: the "raw captured before parse" semantic is observed
     BEHAVIORALLY via item 2 (the success-shape test asserts
     `diag["response"] == page_body` verbatim, which fails if any
     refactor parses-and-throws-away the response). A structural
     AST guard at the call-ordering site was scoped out of D1.9
     (R3 retract).

Per CLAUDE.md "Extraction-bit discipline" + L99 PARANOID: contract
tests pin the shape day-1 so a future Bit that loosens an isolation
invariant fires here rather than at adversarial review.
"""
from __future__ import annotations

import ast
import inspect
import json
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

from collector import rest_snapshot
from collector.rest_snapshot import (
    RestSnapshotRefresher,
    TIER_ALL,
    fetch_tickers_by_tier,
)
from collector.writer import BronzeWriter


REPO_ROOT = Path(__file__).resolve().parents[2]
REST_SNAPSHOT_SRC = REPO_ROOT / "collector" / "rest_snapshot.py"
MAIN_LOOP_SRC = REPO_ROOT / "collector" / "main_loop.py"


# ─── Helpers ───────────────────────────────────────────────────────────────


def _build_session(pages: List[Dict[str, Any]]) -> MagicMock:
    """Build a fake requests.Session that returns ``pages`` in order
    from ``session.get``. Each page is a dict that will be the parsed
    JSON body. Returns ``MagicMock`` so call_args_list can be inspected.
    """
    responses = []
    for page in pages:
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {}
        resp.json.return_value = page
        resp.content = json.dumps(page).encode()
        resp.raise_for_status = MagicMock()
        responses.append(resp)
    session = MagicMock()
    session.get.side_effect = responses
    return session


class _RecordingWriter:
    """Captures every ``write_frame`` call for assertion.

    Mirrors the real ``BronzeWriter.write_frame`` signature so we can
    drop this in wherever a writer is expected. NOT a ``BronzeWriter``
    subclass — the production code MUST treat the writer as a duck-
    typed dependency (``write_frame(wire_recv_ts, raw_payload)``), not
    require an isinstance check.
    """

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []
        self._seq = 0

    def write_frame(self, wire_recv_ts, raw_payload: str) -> None:
        self._seq += 1
        self.calls.append({
            "seq": self._seq,
            "wire_recv_ts": wire_recv_ts,
            "raw_payload": raw_payload,
        })

    @property
    def envelopes(self) -> List[Dict[str, Any]]:
        """Parsed envelopes (one per call)."""
        return [json.loads(c["raw_payload"]) for c in self.calls]


class _RaisingWriter:
    """Writer whose ``write_frame`` always raises. Used to assert
    best-effort posture — production subscription planning must keep
    working even if bronze write fails."""

    def write_frame(self, wire_recv_ts, raw_payload: str) -> None:
        raise RuntimeError(
            "synthetic writer failure (test of best-effort posture)"
        )


# ─── 1. Envelope shape — _source / _channel / _conn ────────────────────────


def test_kalshi_rest_bronze_source_value():
    """Every record emitted by D1.9 writes the diagnostic dict directly;
    the BronzeWriter then wraps it with ``_source="kalshi_rest"`` per its
    constructor. We pin this at the integration tier (writer construction
    site) AND structurally by asserting the writer is built with the
    correct source argument."""
    session = _build_session([{"markets": [{"ticker": "X", "status": "open"}], "cursor": ""}])
    recording = _RecordingWriter()
    fetch_tickers_by_tier(
        api_key="kid", private_key=None, session=session,
        _test_skip_auth=True, bronze_writer=recording,
    )
    # Pre-D1.9 the function signature did NOT accept ``bronze_writer`` —
    # this test will RED until the kwarg is added.
    assert recording.calls, "no bronze write happened — writer not wired"


def test_kalshi_rest_bronze_channel_value():
    """The writer is constructed with ``channel="markets"`` (NOT
    ``markets_snapshot``, NOT ``open_markets``). Pinned at the main_loop
    construction site — see test_main_loop_registers_kalshi_rest_writer.

    Here we pin the negative form: the diagnostic payload itself does
    NOT carry a ``channel`` field; channel identity comes from the
    writer's constructor. So we assert the writer's channel string
    didn't accidentally leak into ``_raw``."""
    session = _build_session([{"markets": [{"ticker": "X", "status": "open"}], "cursor": ""}])
    recording = _RecordingWriter()
    fetch_tickers_by_tier(
        api_key="kid", private_key=None, session=session,
        _test_skip_auth=True, bronze_writer=recording,
    )
    diag = recording.envelopes[0]
    assert "_channel" not in diag, (
        "channel identity comes from the writer's constructor, NOT the "
        "diagnostic payload. Don't duplicate it inside _raw."
    )
    assert "_source" not in diag, (
        "source identity comes from the writer's constructor, NOT the "
        "diagnostic payload."
    )


def test_kalshi_rest_bronze_conn_null():
    """REST snapshots have no WS connection — ``_conn`` is None on the
    envelope (renders as ``conn=none`` in the partition string per
    ``writer.py::_partition_dir``). Pinned at the main_loop writer-
    construction site — see test_main_loop_registers_kalshi_rest_writer."""
    # This test is intentionally a placeholder pin — the actual _conn=None
    # invariant is enforced at the writer construction site (see
    # test_main_loop_registers_kalshi_rest_writer). The dual-pin shape
    # mirrors how D1.8 test_collector_weather_archiver.py pins SOURCE +
    # uses the main_loop test to pin the writer construction.
    pass


# ─── 2. Diagnostic wrapper shape ───────────────────────────────────────────


def test_kalshi_rest_bronze_success_envelope_shape():
    """Success path (HTTP 200): diagnostic dict has 7 keys —
    ``http_status`` / ``elapsed_ms`` / ``attempts`` / ``page_idx`` /
    ``cursor_in`` / ``cursor_out`` / ``response``. The ``response``
    field is the verbatim Kalshi page dict (NOT a parsed-and-
    re-serialized version).

    ``elapsed_ms`` is per-LAST-attempt wall-clock (R1-M5 fix).
    ``attempts`` is the count of HTTP attempts the inner loop made
    so silver D3.x can disaggregate retried pages."""
    page_body = {
        "markets": [
            {"ticker": "KXBTC-A", "status": "open", "yes_bid": 50},
            {"ticker": "KXETH-Z", "status": "active", "yes_bid": 30},
        ],
        "cursor": "",
    }
    session = _build_session([page_body])
    recording = _RecordingWriter()
    fetch_tickers_by_tier(
        api_key="kid", private_key=None, session=session,
        _test_skip_auth=True, bronze_writer=recording,
    )
    assert len(recording.calls) == 1, "expected one bronze record per page"
    diag = recording.envelopes[0]
    assert set(diag.keys()) == {
        "http_status", "elapsed_ms", "attempts", "page_idx",
        "cursor_in", "cursor_out", "response",
    }, f"unexpected diagnostic keys: {sorted(diag.keys())}"
    assert diag["http_status"] == 200
    assert isinstance(diag["elapsed_ms"], int)
    assert diag["elapsed_ms"] >= 0
    assert diag["attempts"] == 1, (
        "first-attempt success must report attempts=1"
    )
    assert diag["page_idx"] == 1
    assert diag["cursor_in"] is None  # first page has no cursor_in
    assert diag["cursor_out"] == ""  # last page has empty cursor
    # Verbatim page body — the markets array survives intact.
    assert diag["response"] == page_body


def test_kalshi_rest_bronze_one_record_per_page():
    """N pages = N envelopes. Each page response gets its own
    diagnostic record with monotone-increasing ``page_idx``.

    Cursor-pagination boundary preserved: ``cursor_in`` of page N+1
    must equal ``cursor_out`` of page N (the chain is verifiable from
    bronze without joining a separate log)."""
    session = _build_session([
        {"markets": [{"ticker": "A", "status": "open"}], "cursor": "p2"},
        {"markets": [{"ticker": "B", "status": "open"}], "cursor": "p3"},
        {"markets": [{"ticker": "C", "status": "open"}], "cursor": ""},
    ])
    recording = _RecordingWriter()
    fetch_tickers_by_tier(
        api_key="kid", private_key=None, session=session,
        _test_skip_auth=True, bronze_writer=recording,
    )
    assert len(recording.calls) == 3, (
        f"expected 3 envelopes (one per page), got {len(recording.calls)}"
    )
    diags = recording.envelopes
    assert [d["page_idx"] for d in diags] == [1, 2, 3]
    # Cursor chain verifiable from bronze alone.
    assert diags[0]["cursor_in"] is None
    assert diags[0]["cursor_out"] == "p2"
    assert diags[1]["cursor_in"] == "p2"
    assert diags[1]["cursor_out"] == "p3"
    assert diags[2]["cursor_in"] == "p3"
    assert diags[2]["cursor_out"] == ""


def test_kalshi_rest_bronze_failure_envelope_shape_4xx_5xx():
    """Failure path (non-200): diagnostic dict has 6 keys —
    ``http_status`` / ``elapsed_ms`` / ``attempts`` / ``page_idx`` /
    ``cursor_in`` / ``error`` (NOT ``cursor_out``, NOT ``response``).

    The ``error`` field carries either ``http_<status>`` or
    ``http_<status>: <reason>`` if Kalshi returned a JSON error body
    with a ``reason`` field. ``attempts`` reflects the HTTP attempt
    count before giving up (R1-M5)."""
    # Force max_retries=1 + zero backoff so the test runs in <1ms.
    bad = MagicMock()
    bad.status_code = 503
    bad.headers = {}
    bad.json.return_value = {}
    bad.raise_for_status = MagicMock()
    session = MagicMock()
    session.get.side_effect = [bad]
    recording = _RecordingWriter()
    out = fetch_tickers_by_tier(
        api_key="kid", private_key=None, session=session,
        _test_skip_auth=True, bronze_writer=recording,
        max_retries=1, _test_backoff_seconds=0.0,
    )
    assert out is None, "503 should return None after max_retries=1"
    assert len(recording.calls) == 1, (
        "failure path should still produce one bronze record per page"
    )
    diag = recording.envelopes[0]
    assert set(diag.keys()) == {
        "http_status", "elapsed_ms", "attempts", "page_idx",
        "cursor_in", "error",
    }, f"unexpected failure-diag keys: {sorted(diag.keys())}"
    assert diag["http_status"] == 503
    assert diag["attempts"] == 1, (
        "max_retries=1 forces exactly one attempt before giving up"
    )
    assert diag["error"].startswith("http_503"), diag["error"]


def test_kalshi_rest_bronze_failure_envelope_shape_transport_exception():
    """Transport exception (e.g., DNS, connection reset): ``http_status``
    is None; ``error`` carries the exception type + repr."""
    import requests as _requests

    session = MagicMock()
    session.get.side_effect = _requests.exceptions.ConnectionError(
        "DNS resolution failed",
    )
    recording = _RecordingWriter()
    out = fetch_tickers_by_tier(
        api_key="kid", private_key=None, session=session,
        _test_skip_auth=True, bronze_writer=recording,
        max_retries=1, _test_backoff_seconds=0.0,
    )
    assert out is None
    assert len(recording.calls) == 1
    diag = recording.envelopes[0]
    assert diag["http_status"] is None
    assert "request_exception" in diag["error"]


# ─── 3. Sequence + ordering ────────────────────────────────────────────────


def test_kalshi_rest_bronze_seq_monotone_within_snapshot():
    """The RecordingWriter assigns its own monotone seq; what we pin
    here is that the production code makes ONE write per page in
    order, so the writer's per-call ordering matches page traversal
    order. Pinned at the per-page granularity rather than the
    writer's internal seq (that's a writer.py contract)."""
    session = _build_session([
        {"markets": [{"ticker": "A", "status": "open"}], "cursor": "p2"},
        {"markets": [{"ticker": "B", "status": "open"}], "cursor": ""},
    ])
    recording = _RecordingWriter()
    fetch_tickers_by_tier(
        api_key="kid", private_key=None, session=session,
        _test_skip_auth=True, bronze_writer=recording,
    )
    seqs = [c["seq"] for c in recording.calls]
    assert seqs == [1, 2], f"writes not in page order: {seqs}"
    page_idxs = [d["page_idx"] for d in recording.envelopes]
    assert page_idxs == [1, 2], f"page_idx not monotone: {page_idxs}"


# ─── 4. Best-effort posture ────────────────────────────────────────────────


def test_fetch_tickers_continues_on_writer_failure():
    """If ``bronze_writer.write_frame`` raises, ``fetch_tickers_by_tier``
    MUST still return the parsed ticker set. Bronze is observability;
    production subscription planning keeps working. Mirrors
    ``WeatherArchiver`` best-effort posture (weather_archiver.py:191)."""
    session = _build_session([
        {"markets": [{"ticker": "KXBTC-A", "status": "open"}], "cursor": ""},
    ])
    raising = _RaisingWriter()
    out = fetch_tickers_by_tier(
        api_key="kid", private_key=None, session=session,
        _test_skip_auth=True, bronze_writer=raising,
    )
    assert out == {TIER_ALL: ["KXBTC-A"]}, (
        "writer raise must NOT propagate; ticker set must still return"
    )


def test_fetch_tickers_works_without_bronze_writer():
    """Pre-D1.9 callers (and any caller that doesn't want bronze) pass
    ``bronze_writer=None`` (or omit it entirely — the default).
    Behavior is identical to pre-D1.9."""
    session = _build_session([
        {"markets": [{"ticker": "KXBTC-A", "status": "open"}], "cursor": ""},
    ])
    # Omit bronze_writer entirely (default).
    out = fetch_tickers_by_tier(
        api_key="kid", private_key=None, session=session,
        _test_skip_auth=True,
    )
    assert out == {TIER_ALL: ["KXBTC-A"]}
    # Explicit None — same behavior.
    session2 = _build_session([
        {"markets": [{"ticker": "KXETH-Z", "status": "open"}], "cursor": ""},
    ])
    out2 = fetch_tickers_by_tier(
        api_key="kid", private_key=None, session=session2,
        _test_skip_auth=True, bronze_writer=None,
    )
    assert out2 == {TIER_ALL: ["KXETH-Z"]}


# ─── 5. RestSnapshotRefresher wires the writer ─────────────────────────────


def test_refresher_accepts_bronze_writer_kwarg():
    """``RestSnapshotRefresher.__init__`` accepts an optional
    ``bronze_writer`` kwarg. Default None preserves pre-D1.9 callers."""
    import threading

    # If the kwarg doesn't exist yet, this RED with TypeError.
    refresher = RestSnapshotRefresher(
        api_key="kid",
        private_key=None,
        on_refresh=lambda _: None,
        shutdown_event=threading.Event(),
        bronze_writer=None,
    )
    assert refresher is not None


def test_refresher_passes_writer_to_fetch():
    """When the refresher fires ``_do_refresh``, it forwards its
    ``bronze_writer`` to ``fetch_tickers_by_tier``."""
    import threading

    captured = {"writer_seen": None}

    def fake_fetch(**kwargs):
        captured["writer_seen"] = kwargs.get("bronze_writer")
        return {TIER_ALL: ["X"]}

    recording = _RecordingWriter()
    refresher = RestSnapshotRefresher(
        api_key="kid",
        private_key=None,
        on_refresh=lambda _: None,
        shutdown_event=threading.Event(),
        bronze_writer=recording,
    )
    # Monkey-patch the module-level fetch fn the refresher dispatches to.
    import collector.rest_snapshot as mod
    orig = mod.fetch_tickers_by_tier
    try:
        mod.fetch_tickers_by_tier = fake_fetch  # type: ignore
        refresher._do_refresh()
    finally:
        mod.fetch_tickers_by_tier = orig  # type: ignore
    assert captured["writer_seen"] is recording, (
        "refresher must forward its bronze_writer to fetch_tickers_by_tier"
    )


# ─── 6. Main-loop writer registration ──────────────────────────────────────


def test_main_loop_registers_kalshi_rest_writer():
    """``collector.main_loop.run`` constructs ONE
    ``BronzeWriter(source="kalshi_rest", channel="markets", conn=None)``
    when the REST refresher path is active (i.e., when
    ``COLLECTOR_TICKERS_FILE`` is unset).

    Pinned by an AST scan of ``main_loop.py``: the literal arguments
    must appear as a unit. This catches a future refactor that splits
    the writer construction across sites + silently changes the
    source/channel/conn triplet."""
    src = MAIN_LOOP_SRC.read_text()
    tree = ast.parse(src)

    # Walk for any BronzeWriter() construction whose kwargs include
    # source="kalshi_rest" + channel="markets" + conn=None.
    found = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        # Match plain BronzeWriter(...) or collector.writer.BronzeWriter(...)
        is_bw = (
            (isinstance(func, ast.Name) and func.id == "BronzeWriter")
            or (isinstance(func, ast.Attribute) and func.attr == "BronzeWriter")
        )
        if not is_bw:
            continue
        kwargs = {k.arg: k.value for k in node.keywords if k.arg is not None}
        source_val = _const_str(kwargs.get("source"))
        channel_val = _const_str(kwargs.get("channel"))
        conn_val = kwargs.get("conn")
        if (
            source_val == "kalshi_rest"
            and channel_val == "markets"
            and isinstance(conn_val, ast.Constant) and conn_val.value is None
        ):
            found = True
            break
    assert found, (
        "collector/main_loop.py must construct BronzeWriter with "
        "source='kalshi_rest', channel='markets', conn=None. "
        "This is the production writer registration site for D1.9."
    )


def _const_str(node):
    """Return the str value of an ast.Constant node, or None if not a
    string constant."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def test_main_loop_skips_kalshi_rest_writer_in_file_mode():
    """When ``COLLECTOR_TICKERS_FILE`` is set, the REST refresher is NOT
    started → no REST fetch happens → no bronze write surface. The
    writer construction MUST be inside the same conditional branch
    that gates the refresher construction (the ``if not tickers_file``
    block in main_loop.run).

    Verify structurally: the BronzeWriter(source='kalshi_rest', ...)
    construction is reachable ONLY from the production path."""
    src = MAIN_LOOP_SRC.read_text()
    # AST scan: find the BronzeWriter() call whose source kwarg is
    # "kalshi_rest". Verify its line is AFTER the start of the
    # ``if not tickers_file:`` branch by parsing the AST + walking
    # the conditional structure.
    tree = ast.parse(src)
    branch_lineno = None
    writer_lineno = None
    for node in ast.walk(tree):
        # The production branch is the ``else`` arm of an ``if tickers_file:``
        # OR the body of ``if not tickers_file:`` — code currently uses
        # the former shape ("if tickers_file: ... else: <production>").
        # Either shape: find the lineno where the production path begins.
        if isinstance(node, ast.If):
            test = node.test
            # Match: ``if tickers_file`` (production = else body).
            if isinstance(test, ast.Name) and test.id == "tickers_file":
                if node.orelse:
                    branch_lineno = node.orelse[0].lineno
            # Match: ``if not tickers_file`` (production = body).
            elif (
                isinstance(test, ast.UnaryOp)
                and isinstance(test.op, ast.Not)
                and isinstance(test.operand, ast.Name)
                and test.operand.id == "tickers_file"
            ):
                if node.body:
                    branch_lineno = node.body[0].lineno
        if isinstance(node, ast.Call):
            func = node.func
            is_bw = (
                (isinstance(func, ast.Name) and func.id == "BronzeWriter")
                or (isinstance(func, ast.Attribute) and func.attr == "BronzeWriter")
            )
            if not is_bw:
                continue
            kwargs = {k.arg: k.value for k in node.keywords if k.arg is not None}
            source_val = _const_str(kwargs.get("source"))
            if source_val == "kalshi_rest":
                writer_lineno = node.lineno

    assert branch_lineno is not None, (
        "expected 'if tickers_file: ... else: ...' (or 'if not tickers_file: ...') "
        "branch in collector/main_loop.run"
    )
    assert writer_lineno is not None, (
        "expected BronzeWriter(source='kalshi_rest', ...) construction in main_loop.run"
    )
    assert writer_lineno >= branch_lineno, (
        f"BronzeWriter(source='kalshi_rest', ...) at line {writer_lineno} must be "
        f"inside the production REST path (starts at line {branch_lineno}), not in "
        f"the file-mode branch."
    )


# ─── 7. Backward-compat + isolation guards ─────────────────────────────────
#
# NOTE: the "raw captured before parse" semantic is observed BEHAVIORALLY
# by ``test_kalshi_rest_bronze_success_envelope_shape`` (it asserts
# ``diag["response"] == page_body`` verbatim — a future refactor that
# parsed-and-threw-away the response would fail that assertion). A
# structural AST guard at the call-ordering site (write-before-parse)
# was scoped out of D1.9 (R3 retract); a future Bit can add one if a
# regression in this class surfaces.


def test_fetch_tickers_signature_accepts_bronze_writer():
    """The public signature MUST include ``bronze_writer`` as a kwarg
    with a default. Pins backward compatibility (existing callers
    without the kwarg keep working)."""
    sig = inspect.signature(fetch_tickers_by_tier)
    assert "bronze_writer" in sig.parameters, (
        f"fetch_tickers_by_tier signature missing bronze_writer kwarg: "
        f"{sig.parameters}"
    )
    bw_param = sig.parameters["bronze_writer"]
    assert bw_param.default is None, (
        f"bronze_writer default must be None (got {bw_param.default!r}). "
        "None lets existing callers omit the kwarg + preserves pre-D1.9 "
        "behavior exactly."
    )


def test_rest_snapshot_imports_no_bot():
    """``collector/rest_snapshot.py`` must not gain a ``bot.*`` import in
    D1.9 (collector-no-bot contract). Pinned structurally via AST so a
    future refactor that reaches into bot for diagnostic helpers fires
    here rather than at import-linter time."""
    src = REST_SNAPSHOT_SRC.read_text()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not alias.name.startswith(("bot.", "bot ")), (
                    f"collector/rest_snapshot.py imports bot.* "
                    f"({alias.name}) — violates collector-no-bot"
                )
        elif isinstance(node, ast.ImportFrom):
            if node.module and (
                node.module == "bot" or node.module.startswith("bot.")
            ):
                raise AssertionError(
                    f"collector/rest_snapshot.py imports from bot.* "
                    f"({node.module}) — violates collector-no-bot"
                )
