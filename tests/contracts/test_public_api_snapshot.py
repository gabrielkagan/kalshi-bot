"""Public API contract test for the bot package.

Validates that bot's public surface (top-level package + all submodules
under bot.*, including their public classes/functions/signatures) matches
the committed snapshot at ``tests/contracts/public_api.json``.

This test is the structural successor to the per-module ``test_*_extraction.py``
files that scanned ``bot/_impl.py`` source via ``ast.parse``. Internal moves
that preserve the public surface (extraction with re-export) produce a
zero-diff result; surface changes show one-line review items.

If this test fails:
- If the surface change is intentional, regenerate via
  ``python3 scripts/dump_public_api.py`` (or ``make api-snapshot-regen``)
  and commit the resulting ``tests/contracts/public_api.json``.
- If unintentional, revert the change that caused the diff.
"""

from __future__ import annotations

import difflib
import json
from pathlib import Path

import pytest

SNAPSHOT_PATH = Path(__file__).parent / "public_api.json"
REGEN_CMD = "python3 scripts/dump_public_api.py"


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
    if len(diff) > 6000:
        diff = diff[:6000] + "\n... (diff truncated; regen + git diff for full)\n"

    pytest.fail(
        "Public API drifted from the committed snapshot.\n"
        f"\nDiff:\n{diff}\n"
        f"If intentional: regenerate via `{REGEN_CMD}` and commit the new snapshot.\n"
        "If unintentional: revert the surface change."
    )
