"""P1-A hot-path optimization contract guards (ticket 86ba1pqqx, 2026-05-20).

Pins the 4 hot-path optimizations in ``collector/writer.py::BronzeWriter.write``
that close the universal-mode drop gap. Each test is an AST guard that fails
RED if the code regresses to its pre-P1-A shape.

Background (from kb/decisions/p1-collector-universal-tuning-plan.md RC#1):
the 2026-05-20 universal-mode flip at 705K tickers dropped 166K frames in
3 minutes. Code RCA found 4 GIL-bound inefficiencies in the per-frame
writer path that don't release the GIL during their work. Removing them
recovers ~50% per-frame worker CPU.

The 4 fixes are:
  1. Drop writer-side ``strptime`` round-trip of the ISO timestamp the
     SAME process serialized 2 lines earlier in ``build_envelope``.
     Pass ``wire_recv_ts`` as a datetime alongside the envelope through
     the queue tuple.
  2. Drop per-frame ``self._in_flight_fh.flush()``. D0.3 §7 says
     durability is the uploader's job at rotation time; per-frame flush
     adds thousands of syscalls/sec for zero invariant benefit.
  3. Replace stdlib ``json.dumps`` with ``orjson.dumps``. 3-5× faster;
     both release the GIL identically; orjson returns bytes directly
     (saves the ``.encode("utf-8")`` call).
  4. Skip envelope re-validation (3 ``envelope.get`` + 3 equality checks
     against the writer's own constructor args). Producer constructs the
     envelope from those SAME constants 2 lines earlier in
     ``BronzeArchiver._on_frame``; the writer's re-check cannot mismatch
     in production. Gate behind ``_VALIDATE_ENVELOPE`` constant
     (default-True-in-test / False-in-prod via ``__debug__`` or
     equivalent) so tests still cover the validation path.

A regression to any of these is a HIGH-severity drop-rate regression.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
WRITER_PY = REPO_ROOT / "collector" / "writer.py"


def _read_writer_source() -> str:
    assert WRITER_PY.exists(), (
        f"{WRITER_PY.relative_to(REPO_ROOT)} missing — P1-A pins the "
        "hot-path shape of this file. If it moved, update the test."
    )
    return WRITER_PY.read_text()


def _get_write_method_body() -> str:
    """Extract the source text of ``BronzeWriter.write`` (NOT write_frame,
    NOT _rotate). Returns the body lines as a single string for substring
    + AST checks scoped specifically to the per-frame write path.

    write_frame and _rotate are deliberately excluded:
      - write_frame is a thin wrapper that calls build_envelope then write
      - _rotate runs ONCE per ~5min and is allowed to do strptime / flush
        (different perf class — not the per-frame hot path)
    """
    source = _read_writer_source()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ClassDef)
            and node.name == "BronzeWriter"
        ):
            for item in node.body:
                if (
                    isinstance(item, ast.FunctionDef)
                    and item.name == "write"
                ):
                    return ast.get_source_segment(source, item) or ""
    raise AssertionError(
        "Could not locate BronzeWriter.write — class or method renamed? "
        "Update this contract test if so."
    )


# ── Test #1: per-frame strptime is gone ─────────────────────────────────


def test_write_strptime_is_back_compat_fallback_only():
    """``datetime.strptime`` MAY appear in ``write()`` — but ONLY inside
    a back-compat fallback branch gated on ``wire_recv_ts is None``.

    Production path post-P1-A: ``wire_recv_ts`` flows alongside the
    envelope through the queue tuple in ``BronzeArchiver._on_frame`` and
    is passed to ``write(envelope, wire_recv_ts=...)`` as a datetime.
    The fast path skips strptime entirely.

    Back-compat path: callers that don't yet pass the datetime (tests,
    ``write_frame``, weather/espn/coinbase archivers pre-migration) fall
    back to the ISO-parse. This is intentional — migrating every caller
    is a follow-up Bit; the hot Kalshi production path benefits today.

    AST shape requirement:
      - strptime call MUST be inside an `if wire_recv_ts is None:` block
      - strptime call MUST NOT be at the top level of write()
        (the pre-P1-A shape that ran unconditionally on every frame)
    """
    source = _read_writer_source()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ClassDef)
            and node.name == "BronzeWriter"
        ):
            for item in node.body:
                if (
                    isinstance(item, ast.FunctionDef)
                    and item.name == "write"
                ):
                    # Walk the top-level statements; any strptime call
                    # found at depth 0 is the pre-P1-A regression.
                    for stmt in item.body:
                        if isinstance(stmt, ast.Assign):
                            for sub in ast.walk(stmt):
                                if (
                                    isinstance(sub, ast.Attribute)
                                    and sub.attr == "strptime"
                                ):
                                    raise AssertionError(
                                        "Unconditional datetime.strptime at the top "
                                        "level of BronzeWriter.write — the pre-P1-A "
                                        "regression. strptime must be nested under "
                                        "`if wire_recv_ts is None:`. See "
                                        "kb/decisions/p1-collector-universal-tuning-plan.md "
                                        "RC#1 fix #1."
                                    )
                    # Verify the back-compat If-block exists and contains
                    # a strptime call inside it.
                    found_back_compat_strptime = False
                    for stmt in item.body:
                        if (
                            isinstance(stmt, ast.If)
                            and isinstance(stmt.test, ast.Compare)
                            and isinstance(stmt.test.left, ast.Name)
                            and stmt.test.left.id == "wire_recv_ts"
                            and len(stmt.test.ops) == 1
                            and isinstance(stmt.test.ops[0], ast.Is)
                            and isinstance(stmt.test.comparators[0], ast.Constant)
                            and stmt.test.comparators[0].value is None
                        ):
                            for sub in ast.walk(stmt):
                                if (
                                    isinstance(sub, ast.Attribute)
                                    and sub.attr == "strptime"
                                ):
                                    found_back_compat_strptime = True
                                    break
                    assert found_back_compat_strptime, (
                        "Expected `if wire_recv_ts is None:` back-compat "
                        "branch containing strptime, but did not find it. "
                        "Either the fast path is broken (no datetime to use) "
                        "or back-compat was incorrectly removed (would break "
                        "weather/espn/coinbase callers that don't yet pass "
                        "wire_recv_ts). See P1-A plan-doc for migration scope."
                    )
                    return
    raise AssertionError("Could not locate BronzeWriter.write.")


# ── Test #2: per-frame fh.flush() is gone ───────────────────────────────


def test_write_does_not_flush_per_frame():
    """``self._in_flight_fh.flush()`` must NOT appear in ``write()``.

    D0.3 §7 says bronze durability is the uploader's contract at
    rotation time (the writer's _rotate does flush + fsync + zstd).
    Per-frame flush adds thousands of syscalls/sec across N writers for
    zero invariant benefit — data is already in the kernel page cache
    after fh.write(), and a power-fail loses it either way (no fsync
    happens).

    Allowed in: ``_rotate`` (flushes before fsync+close, correct), and
    ``close`` (forces final rotation). Forbidden in: ``write`` per-frame.
    """
    write_body = _get_write_method_body()
    # Look for any .flush() call on _in_flight_fh inside write().
    # Use regex so we catch both "self._in_flight_fh.flush()" and
    # accidental indirection like "fh = self._in_flight_fh; fh.flush()".
    forbidden_pattern = re.compile(
        r"_in_flight_fh\s*\.\s*flush\s*\(", re.MULTILINE
    )
    assert not forbidden_pattern.search(write_body), (
        "self._in_flight_fh.flush() found inside BronzeWriter.write — "
        "per-frame flush is the largest single hot-path waste (potentially "
        "thousands of syscalls/sec). Flush belongs in _rotate / close. "
        "See kb/decisions/p1-collector-universal-tuning-plan.md RC#1 fix #2."
    )


# ── Test #3: orjson is used for the per-frame serialize ─────────────────


def test_write_uses_orjson_not_stdlib_json():
    """``write()`` must serialize the envelope via ``orjson.dumps``, NOT
    stdlib ``json.dumps``.

    orjson is 3-5× faster, releases the GIL identically, and returns
    bytes directly (eliminating the trailing ``.encode("utf-8")``).
    Drop-in for our envelope shape (str/int/None/dict values).

    The module-level ``import orjson`` is also pinned so a regression
    can't shadow the import while keeping the call-site green.
    """
    source = _read_writer_source()
    assert "import orjson" in source, (
        "collector/writer.py must `import orjson` at module level for "
        "the per-frame serialize. Add orjson to requirements.txt. See "
        "kb/decisions/p1-collector-universal-tuning-plan.md RC#1 fix #3."
    )

    write_body = _get_write_method_body()
    assert "orjson.dumps" in write_body, (
        "BronzeWriter.write must call orjson.dumps for the per-frame "
        "serialize (3-5× faster than stdlib json, returns bytes directly)."
    )
    # Negative pin: stdlib json.dumps must not appear in the hot path.
    # Use word-boundary regex so `orjson.dumps` (which CONTAINS the
    # substring `json.dumps`) does not trigger a false-positive.
    stdlib_json_dumps_pattern = re.compile(r"(?<!or)\bjson\.dumps\b")
    assert not stdlib_json_dumps_pattern.search(write_body), (
        "Stdlib json.dumps still appears in BronzeWriter.write — replace "
        "with orjson.dumps. Stdlib json is the 3-5× slower path."
    )


# ── Test #4: envelope re-validation is gated (not unconditional) ────────


def test_write_envelope_revalidation_is_gated():
    """The 3 ``envelope.get(...)`` + 3 equality-check validations in
    ``write()`` must be gated behind a debug/test toggle, NOT
    unconditional in the hot path.

    The producer (``BronzeArchiver._on_frame``) constructs the envelope
    from the SAME constants 2 lines earlier — the writer's re-check
    cannot mismatch in production. Gating saves 3 dict lookups + 3 cmps
    per frame for zero invariant-protection value.

    Accepted gate shapes:
      - ``if __debug__:`` (Python's compile-time flag; -O strips it)
      - ``if _VALIDATE_ENVELOPE:`` (module-level constant)
      - ``if self._validate_envelope:`` (instance attribute)
      - any explicit ``if <gate>: ...envelope.get(...)`` block

    Forbidden: bare top-level ``if envelope.get("_source") != self.source:``
    at the start of write() (the pre-P1-A shape).
    """
    write_body = _get_write_method_body()
    # Look for the canonical pre-P1-A shape: validation at the top of
    # write() body, not nested under any gate. Use ast to inspect the
    # If-statement at depth 0 within write().
    tree = ast.parse(write_body)
    # write_body parses as a FunctionDef; first node is the def itself.
    fn_node = tree.body[0]
    assert isinstance(fn_node, ast.FunctionDef)
    # Walk the top-level statements of the function body. The pre-P1-A
    # shape had three sequential top-level `if envelope.get("_X") != self.X`
    # ValueError-raising blocks at the start of the function.
    forbidden_validators_at_top = 0
    for stmt in fn_node.body:
        if (
            isinstance(stmt, ast.If)
            and isinstance(stmt.test, ast.Compare)
            and isinstance(stmt.test.left, ast.Call)
            and isinstance(stmt.test.left.func, ast.Attribute)
            and stmt.test.left.func.attr == "get"
            and isinstance(stmt.test.left.func.value, ast.Name)
            and stmt.test.left.func.value.id == "envelope"
        ):
            forbidden_validators_at_top += 1
    assert forbidden_validators_at_top == 0, (
        f"Found {forbidden_validators_at_top} top-level `if envelope.get(...) != ...` "
        "validators in BronzeWriter.write — these add 3 dict lookups + 3 "
        "cmps per frame for zero invariant-protection value (producer "
        "constructs the envelope from the same constants 2 lines earlier). "
        "Gate behind `if __debug__:` or a module-level _VALIDATE_ENVELOPE "
        "constant. See kb/decisions/p1-collector-universal-tuning-plan.md "
        "RC#1 fix #4."
    )


# ── Test #5: write() signature accepts wire_recv_ts ─────────────────────


def test_write_signature_accepts_wire_recv_ts():
    """``BronzeWriter.write`` must accept ``wire_recv_ts`` as a keyword
    arg so the producer (BronzeArchiver) can pass the datetime it
    already has, eliminating the strptime parse in writer.

    The arg should default to ``None`` for back-compat with callers
    (tests, write_frame) that don't yet pass it — those callers fall
    back to parsing from the envelope.
    """
    source = _read_writer_source()
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.ClassDef)
            and node.name == "BronzeWriter"
        ):
            for item in node.body:
                if (
                    isinstance(item, ast.FunctionDef)
                    and item.name == "write"
                ):
                    arg_names = [a.arg for a in item.args.args]
                    # Also include kwonly args
                    arg_names += [a.arg for a in item.args.kwonlyargs]
                    assert "wire_recv_ts" in arg_names, (
                        "BronzeWriter.write signature must accept "
                        "`wire_recv_ts` so the producer can pass the "
                        "datetime alongside the envelope, eliminating "
                        "the per-frame strptime. See "
                        "kb/decisions/p1-collector-universal-tuning-plan.md "
                        "RC#1 fix #1."
                    )
                    return
    raise AssertionError(
        "Could not locate BronzeWriter.write to inspect signature."
    )
