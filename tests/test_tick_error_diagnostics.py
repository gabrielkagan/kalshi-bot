"""Regression tests for tick-error diagnostic instrumentation.

Incident: 2026-04-27 07:32 UTC — single tick error
"tuple index out of range" fired and self-recovered, but the only
information that reached the operator (Telegram) was the truncated
exception string. The full traceback went to stderr→journal but
journal retention/MCP query limits made it unrecoverable.

Post-fix contract:
  - `_extract_tick_error_location(exc)` returns 'basename.py:LINE:func'
    for the deepest frame of the exception's traceback.
  - Returns a literal '?' sentinel on any failure (no traceback,
    no frames, malformed exception) — must never raise from inside
    the error path.
  - Output is short enough to fit in a Telegram alert without
    consuming the 200-char str(e) budget.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _import_helper():
    """Lazy import so the test file can be collected even if bot/_impl.py
    has unrelated import-time issues."""
    from bot import _extract_tick_error_location
    return _extract_tick_error_location


def test_extracts_file_line_func_from_real_exception():
    """The smoke test: a real exception raised in a known function
    yields a string with that function's name and a non-zero line."""
    extract = _import_helper()

    def _provoke():
        t = (1, 2)
        return t[5]  # IndexError: tuple index out of range

    try:
        _provoke()
    except IndexError as e:
        loc = extract(e)

    assert "_provoke" in loc, f"Function name missing from: {loc!r}"
    assert "test_tick_error_diagnostics.py" in loc, (
        f"Expected this test file in location; got: {loc!r}"
    )
    # Format: "file.py:LINE:func"
    parts = loc.split(":")
    assert len(parts) == 3, f"Expected 3 colon-separated parts; got: {loc!r}"
    fn, lineno, func = parts
    assert fn.endswith(".py"), f"Expected .py basename; got: {fn!r}"
    assert lineno.isdigit() and int(lineno) > 0, (
        f"Expected positive line number; got: {lineno!r}"
    )
    assert func == "_provoke"


def test_returns_sentinel_for_exception_without_traceback():
    """An exception with no __traceback__ (constructed but not
    raised) must not crash the helper."""
    extract = _import_helper()
    e = IndexError("tuple index out of range")
    # No traceback attached — never raised
    loc = extract(e)
    assert loc == "?", f"Expected '?' sentinel; got: {loc!r}"


def test_returns_sentinel_for_none_input():
    """Defensive: if somehow None is passed instead of an exception,
    the helper must not crash."""
    extract = _import_helper()
    loc = extract(None)
    assert loc == "?", f"Expected '?' sentinel; got: {loc!r}"


def test_uses_basename_not_full_path():
    """Telegram has a 4096-char limit but tick-error alerts share
    space with str(e)[:200] and other context. Full path (e.g.,
    /home/botuser/kalshi-bot-repo/bot/_impl.py) is wasted bytes — basename
    suffices for routing."""
    extract = _import_helper()

    def _provoke():
        return [][0]

    try:
        _provoke()
    except IndexError as e:
        loc = extract(e)

    assert "/" not in loc, (
        f"Location should be basename-only, no path separators; got: {loc!r}"
    )
    assert "\\" not in loc, (
        f"Location should be basename-only, no Windows separators; got: {loc!r}"
    )


def test_deepest_frame_not_outermost():
    """The interesting frame is the call site that actually raised,
    not the outermost handler. Verify the helper returns the
    deepest frame (where the exception originated)."""
    extract = _import_helper()

    def _innermost_raises():
        return (1, 2)[10]

    def _middle():
        _innermost_raises()

    def _outer():
        _middle()

    try:
        _outer()
    except IndexError as e:
        loc = extract(e)

    assert "_innermost_raises" in loc, (
        f"Should report the deepest frame (_innermost_raises); "
        f"got: {loc!r}"
    )
    assert "_outer" not in loc, (
        f"Should not report outer frame; got: {loc!r}"
    )
    assert "_middle" not in loc, (
        f"Should not report middle frame; got: {loc!r}"
    )


def test_chained_exception_reports_original_raise_site():
    """`raise Y from X` patterns: the exception delivered to the
    handler is Y, but the operator wants to see where X originally
    raised — the wrapper's traceback only shows the re-raise line.
    Helper must walk __cause__/__context__."""
    extract = _import_helper()

    def _original_failure():
        return (1, 2)[99]

    def _wrapper():
        try:
            _original_failure()
        except IndexError as e:
            raise RuntimeError("wrapped failure") from e

    try:
        _wrapper()
    except RuntimeError as e:
        loc = extract(e)

    assert "_original_failure" in loc, (
        f"Should report the original IndexError site (_original_failure), "
        f"not the re-raise site (_wrapper); got: {loc!r}"
    )


def test_implicit_chained_exception_reports_original():
    """Implicit chaining: `except: raise NewError(...)` (no `from`)
    sets __context__ but not __cause__. Helper must still walk it."""
    extract = _import_helper()

    def _original_failure():
        return ()[0]

    def _wrapper():
        try:
            _original_failure()
        except IndexError:
            # Implicit chain via __context__
            raise RuntimeError("wrapped without from")

    try:
        _wrapper()
    except RuntimeError as e:
        loc = extract(e)

    assert "_original_failure" in loc, (
        f"Implicit-chained exception should still report origin; "
        f"got: {loc!r}"
    )


def test_output_fits_telegram_budget():
    """Any reasonable bot/_impl.py call site produces a location string
    well under 100 chars — leaves plenty of room for str(e)[:200]
    in the Telegram alert."""
    extract = _import_helper()

    def _a_function_with_a_reasonably_long_descriptive_name_for_realism():
        return (1,)[5]

    try:
        _a_function_with_a_reasonably_long_descriptive_name_for_realism()
    except IndexError as e:
        loc = extract(e)

    assert len(loc) < 200, (
        f"Location string {len(loc)} chars long; should fit Telegram budget"
    )
