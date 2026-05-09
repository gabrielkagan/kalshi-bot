"""Public API contract test for the bot package.

Validates that bot's public surface matches the committed snapshot at
``tests/contracts/public_api.json``. Three layers (see
``scripts/dump_public_api.py`` for full design):

1. Static walk of ``bot.*`` submodules (excluding ``bot._impl``).
2. Static walk of canonical classes still resident in ``bot/_impl.py``.
3. Runtime probe of ``bot.X`` attributes accessible via the ``_BotProxy``.

Scope: this is an **additive** structural gate. It catches:
- Removed/renamed re-exports at any subpackage ``__init__.py``
- Signature drift on public classes/functions
- Renamed methods on canonical classes (in ``bot._impl`` or extracted modules)
- Lost proxy attributes (e.g., a Bit silently drops ``from config import *``
  in ``bot/_impl.py`` and breaks ``mock.patch("bot.X")`` callers)

Scope: this does NOT replace the per-Bit ``test_*_extraction.py`` files,
which check ``bot/_impl.py`` source patterns, runtime identity
(``bot.X is bot.engines.foo.X``), decorator chain resolution, and
behavioral smoke. Both layers are needed.

If this test fails:
- Intentional surface change: regenerate via
  ``python3 scripts/dump_public_api.py`` (or ``make api-snapshot-regen``)
  and commit the resulting ``tests/contracts/public_api.json``.
- Unintentional: revert the change.
- "Diff is huge / paths look weird": you may have a griffe version skew.
  ``pip install -e '.[dev]'`` should pin to the right minor (1.14.x).
"""

from __future__ import annotations

import difflib
import json
from pathlib import Path

import pytest

SNAPSHOT_PATH = Path(__file__).parent / "public_api.json"
REGEN_CMD = "python3 scripts/dump_public_api.py"
DIFF_MAX_CHARS = 12000  # raised from 6k to surface more context on real diffs

# If the live snapshot has wildly more entries than the committed one, OR
# if any key contains a slash, the most likely cause is a griffe version
# skew (1.0 emits slash-paths and ~5.2k entries; 1.14 emits dot-paths and
# ~3.7k entries — same source: a ~1,500-entry delta). Routine extraction
# Bits could plausibly add/remove 200-500 entries (a class with ~20
# methods + a sibling extraction). Set the threshold high enough to
# avoid false-positive hints on real diffs; slash-path detection
# remains a hard signal regardless of count.
ENTRY_COUNT_SKEW_THRESHOLD = 1000


def _format_diff(expected: dict, actual: dict) -> str:
    expected_lines = json.dumps(expected, indent=2, sort_keys=True).splitlines(
        keepends=True
    )
    actual_lines = json.dumps(actual, indent=2, sort_keys=True).splitlines(
        keepends=True
    )
    diff = "".join(
        difflib.unified_diff(
            expected_lines,
            actual_lines,
            fromfile="committed snapshot",
            tofile="current public surface",
            n=3,
        )
    )
    if len(diff) > DIFF_MAX_CHARS:
        diff = diff[:DIFF_MAX_CHARS] + (
            f"\n... (diff truncated at {DIFF_MAX_CHARS} chars; "
            f"run `{REGEN_CMD}` and `git diff tests/contracts/public_api.json` "
            "for full context)\n"
        )
    return diff


def _looks_like_griffe_skew(expected: dict, actual: dict) -> str | None:
    """Return a hint string if the diff smells like a version mismatch."""
    delta = abs(len(actual) - len(expected))
    has_slash_keys = any("/" in k for k in actual.keys())
    if delta > ENTRY_COUNT_SKEW_THRESHOLD or has_slash_keys:
        return (
            f"Entry count delta {delta} (committed={len(expected)}, "
            f"current={len(actual)}); slash-paths detected={has_slash_keys}. "
            "This shape difference is consistent with a griffe-version skew "
            "(1.0.x emits slash-paths + ~5.2k entries; 1.14.x emits "
            "dot-paths + ~3.7k entries — same source). "
            "Run `pip install -e '.[dev]'` to install the pinned griffe "
            "(1.14.x) before regenerating the snapshot."
        )
    return None


def test_public_api_matches_snapshot() -> None:
    try:
        from scripts.dump_public_api import dump_bot_public_api
    except ImportError as exc:
        pytest.fail(
            "Cannot import scripts.dump_public_api. Likely cause: griffe is "
            "not installed. Install dev deps with `pip install -e '.[dev]'`.\n"
            f"Original error: {exc}"
        )

    if not SNAPSHOT_PATH.exists():
        pytest.fail(
            f"Public API snapshot is missing: {SNAPSHOT_PATH}\n"
            f"Generate it with: {REGEN_CMD}"
        )

    expected = json.loads(SNAPSHOT_PATH.read_text())
    actual = dump_bot_public_api()

    if actual == expected:
        return

    skew_hint = _looks_like_griffe_skew(expected, actual)
    diff = _format_diff(expected, actual)

    msg_parts = ["Public API drifted from the committed snapshot.\n"]
    if skew_hint:
        msg_parts.append("\nLIKELY CAUSE: " + skew_hint + "\n")
    msg_parts.append(f"\nDiff:\n{diff}\n")
    msg_parts.append(
        f"If intentional: regenerate via `{REGEN_CMD}` and commit the new snapshot.\n"
        "If unintentional: revert the surface change."
    )
    pytest.fail("".join(msg_parts))
