---
name: test-writer
description: "Scaffold a failing regression test for a new bot/ extraction or new behavior. Mirrors target path into tests/, applies sibling conftest patterns + heavy-mod stubbing, runs pytest to confirm RED, hands off. Use when: \"scaffold a test for X\", \"start a TDD cycle\", \"write the failing test first\", \"I'm about to extract Y, write the test\"."
---

# /test-writer

Scaffolds a failing test file for a new bot extraction or new behavior, then runs it to confirm RED. Half of Pillar 4 (testing-foundation-sprint, ticket [86b9ve110](https://app.clickup.com/t/86b9ve110)). The other half is the `tdd_guard.py` PreToolUse hook that BLOCKS edits to `bot/**/*.py` until a test was written first — this skill is what unblocks that.

## When to use

- Before extracting a class/function out of `bot/_impl.py` ("write the failing test first")
- Before adding a new method/branch to a `bot/` module
- After the `tdd_guard` hook blocks an edit and you need to write the test it's asking for
- User says: "scaffold a test for X", "start the TDD cycle", "write the failing test", "TDD this"

## When NOT to use

- The change is doc-only / `git mv` / a refactor with property-based equivalence already proving behavior unchanged. Use the bypass markers instead (see `tests/CLAUDE.md`):
  - `KALSHI_TDD_BYPASS=1` env var (per-session)
  - `[no-tdd]` in HEAD commit subject (per-Bit)
- The target is in an already-extracted engine and the existing test file mirrors it cleanly — extend the existing test, don't scaffold a new one.

## Usage

```
/test-writer <target>
```

`<target>` can be:

| Form | Example | Mirror destination |
|---|---|---|
| Module | `bot/engines/calibration.py` | `tests/integration/test_calibration_engine.py` (or extend `tests/contracts/test_engines_extraction.py`) |
| Class | `bot/engines/calibration.py::CalibrationEngine` | same as module |
| Method | `bot/engines/calibration.py::CalibrationEngine.compute` | same as module — adds a new `test_compute_*` test there |
| Behavior | `"low-band passthrough must round-trip 0.5"` | author picks the right tier dir based on intent |

**Tier-aware destinations (post-Bit-12.2, 2026-05-11):**
- AST guards / negative-pin extraction tests / public_api / import-linter → `tests/contracts/`
- Pure repo/Makefile/pyproject invariants → `tests/unit/`
- Behavioral / multi-module / real-DB → `tests/integration/`
- Engine equivalence snapshots → `tests/equivalence/` (Pillar 3; regen is human-only)

## Steps

### 1. Resolve the mirror destination

Read 2-3 sibling tests of the target's neighborhood to discover the existing convention:

```bash
ls tests/integration tests/contracts | grep -i <target-keyword>
```

If a matching test already exists, **prefer extending it** over creating a new one. The Pillar 4 hook's "any test edit in this session" semantics means an Edit to an existing test file is enough — no need to scaffold from scratch.

If no obvious mirror exists, follow the project convention: `bot/<subpkg>/<module>.py → tests/integration/test_<module>_<subpkg>.py` (behavioral) or `tests/contracts/test_<module>_extraction.py` (AST/import contract).

### 2. Choose the right base patterns

Read these reference tests to copy fixture + heavy-mod patterns:

- `tests/contracts/test_engines_extraction.py` — AST/import-contract patterns for new `bot/engines/` extractions
- `tests/integration/test_calibration_engine.py` — heavy-dep mocking pattern (`websockets`, `cryptography`, etc.)
- `tests/equivalence/conftest.py` — calibration-singleton isolation pattern (autouse fixture nullifying `_CALIBRATION_ENGINE`)
- `tests/integration/test_volatility_engine.py` or `tests/contracts/test_engines_extraction.py` — pure-math static-method test patterns

### 3. Draft the test file

Use this skeleton for an extraction-class scaffold (adapt freely):

```python
"""Tests for <ClassName> (bot/<path>.py).

[<clickup-id>] <one-line context>.

Guards against:
- <drift class 1>
- <drift class 2>
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


# Mock heavy deps that bot/_impl.py imports at module level.
_HEAVY = (
    "websockets", "websocket", "requests",
    "cryptography", "cryptography.hazmat",
    "cryptography.hazmat.primitives",
    "cryptography.hazmat.primitives.serialization",
    "cryptography.hazmat.primitives.hashes",
    "cryptography.hazmat.primitives.asymmetric",
    "cryptography.hazmat.primitives.asymmetric.padding",
)
for _m in _HEAVY:
    sys.modules.setdefault(_m, MagicMock())


REPO_ROOT = Path(__file__).resolve().parents[2]  # tests/<tier>/X.py → repo root
TARGET_PY = REPO_ROOT / "bot" / "<path>.py"


def test_<target_class>_will_extract_to_<path>():
    """RED placeholder — replace with the real assertion(s) before
    your implementation pass. The TDD hook only requires a test file
    to be edited in the session; this skeleton satisfies that, but
    a tautological assertion does not satisfy the *intent*. Replace
    `assert False` with a real behavioral pin."""
    assert False, "scaffolded by /test-writer — write a real assertion"
```

**Important:** the `assert False` is a deliberate RED. Do NOT leave it in. The whole point of TDD is the assertion describes the behavior you're about to add — replace it with a real pin in the same response.

### 4. Run the test to confirm RED

```bash
python3 -m pytest tests/<tier>/test_<X>.py -x 2>&1 | tail -20
```

Expected: 1 failed, 0 passed. If it passes accidentally (e.g., the behavior already exists), the assertion is wrong — rewrite it.

### 5. Hand off

State to the user:

> Test scaffolded at `tests/<tier>/test_<X>.py`, RED on `<assertion>`. Ready for the implementation pass — the `tdd_guard` hook will allow edits to `bot/<path>.py` for the rest of this session.

The session-transcript scan in `tdd_guard.py` recognizes the test edit, so subsequent `bot/` edits go through.

## Anti-patterns

- **Don't scaffold a tautological test** (`assert True`, `assert isinstance(x, X)` without behavior). The hook will be satisfied but the test buys nothing.
- **Don't scaffold + immediately bypass**. If you need a bypass, use it directly — don't dilute the corpus with empty test files.
- **Don't scaffold against speculation**. The test should pin a specific contract you know you're about to break/add. If the contract isn't crisp yet, surface that as a question first.
- **Don't write a test that imports `bot._impl` at module top-level without heavy-dep mocking**. The mocks must come BEFORE any `from bot import …` so the import resolves the stubs.

## Reference

- Hook: `.claude/hooks/tdd_guard.py`
- Sister skill: `/ticket` (file a ClickUp ticket if the test reveals a deeper bug)
- Closeout (planned): `kb/decisions/testing-foundation-pillar-4-shipped-may09.md`
- Parent ticket: [86b9ve0wa](https://app.clickup.com/t/86b9ve0wa) (testing-foundation-sprint)
