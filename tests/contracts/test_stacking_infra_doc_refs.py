"""B5-fu2 (ticket 86b9zygg6, 2026-05-18): stacking-infrastructure doc anchor pin.

`kb/concepts/stacking-infrastructure.md` carries a new "Per-Strategy
Stacking Gates" section that anchors readers to the B5 (ticket
86b9zudg2, commit 76f1d54c) TM intercept gates in
`bot/scanner/__init__.py` + the regression pin at
`tests/integration/test_tm_stack_decided_regression.py`. The two
anchor points are FILE PATHS + SYMBOL NAMES that the doc cites
verbatim. If a future refactor moves the symbols or renames the
files, this test fires the L97 stale-anchor signal at the
contracts tier (~5s budget) instead of leaving the doc to rot.

L97 lens: stale anchors in tracked docs are the most common rot
class — doc cites `bot/_impl.py::foo` long after `foo` moved to
`bot/scanner/__init__.py`. Pin the symbol-presence-in-file
invariant so the doc's anchor stays load-bearing.

L99 lens: this is a doc-only Bit, but pinning the anchor catches
the prose-drift class proactively rather than reactively at
adversarial-review time on a future refactor Bit.

If a future refactor INTENTIONALLY relocates the cited symbols or
files, update this test's CITED_* constants AT THE SAME TIME as
the doc + the moved code — same lockstep discipline as the
sister `test_d1_2_doc_lockstep.py`.
"""
from __future__ import annotations

from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]

DOC_PATH = REPO_ROOT / "kb" / "concepts" / "stacking-infrastructure.md"

# Files the doc anchors to. The doc cites these paths verbatim —
# if a refactor moves them, the doc reference rots and this test
# fires.
CITED_FILES: list[Path] = [
    REPO_ROOT / "bot" / "scanner" / "__init__.py",
    REPO_ROOT / "tests" / "integration" / "test_tm_stack_decided_regression.py",
]

# Symbols the doc cites in the B5-gate section. Each (file, token)
# pair: the token must appear textually in the file. These are
# AST-token level (variable names + comment anchors), not import
# names — grep-style presence check is sufficient for a doc-anchor
# pin.
CITED_SYMBOLS: list[tuple[Path, str]] = [
    # B5 gate 1: pre-B5 same-tick decided overlap
    (REPO_ROOT / "bot" / "scanner" / "__init__.py", "_tm_dc_overlap"),
    # B5 gate 2: cross-tick decided retry overlap (B5 addition)
    (REPO_ROOT / "bot" / "scanner" / "__init__.py", "_tm_dc_retry_overlap"),
    # B5 gate 3: same-price TM-on-TM
    (REPO_ROOT / "bot" / "scanner" / "__init__.py", "_tm_has_position"),
    # B5 gate 4: non-TM same-side entry-lock (B5 addition)
    (REPO_ROOT / "bot" / "scanner" / "__init__.py", "_tm_non_tm_position"),
    # B5 block boundary anchor cited verbatim by the doc
    (
        REPO_ROOT / "bot" / "scanner" / "__init__.py",
        "# ── Terminal Momentum intercept",
    ),
    # B5 regression pin: AST guard file exists + contains the
    # gate-2 + gate-4 tokens (the regression test scans the TM
    # intercept block for these and asserts presence)
    (
        REPO_ROOT / "tests" / "integration" / "test_tm_stack_decided_regression.py",
        "_tm_dc_retry_overlap",
    ),
    (
        REPO_ROOT / "tests" / "integration" / "test_tm_stack_decided_regression.py",
        "_tm_non_tm_position",
    ),
]


def test_doc_exists() -> None:
    """The doc itself must exist (it is tracked in git)."""
    assert DOC_PATH.is_file(), f"missing tracked doc: {DOC_PATH}"


@pytest.mark.parametrize("cited", CITED_FILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_doc_cited_file_exists(cited: Path) -> None:
    """Every file path the doc anchors to must exist on disk.

    L97 stale-anchor lens: the doc loses navigational value the
    moment a cited file is renamed/moved.
    """
    assert cited.is_file(), (
        f"stacking-infrastructure.md anchors to {cited.relative_to(REPO_ROOT)} "
        f"but the file is missing. Update the doc OR restore the file."
    )


@pytest.mark.parametrize(
    "cited_file,token",
    CITED_SYMBOLS,
    ids=lambda x: x.name if isinstance(x, Path) else x,
)
def test_doc_cited_symbol_present(cited_file: Path, token: str) -> None:
    """Every symbol the doc cites must appear textually in the
    cited file.

    Grep-level presence check (not AST resolution) — sufficient
    for a doc-anchor pin since the doc cites tokens by name, not
    by import path.
    """
    if not cited_file.is_file():
        pytest.skip(f"cited file missing: {cited_file}")
    text = cited_file.read_text(encoding="utf-8")
    assert token in text, (
        f"stacking-infrastructure.md cites `{token}` in "
        f"{cited_file.relative_to(REPO_ROOT)} but the token is "
        f"not present. Either the symbol was renamed/removed "
        f"(update the doc) or the file was rewritten (update the doc)."
    )


def test_doc_mentions_b5_ticket_anchor() -> None:
    """The doc references the B5 ticket ID + commit hash so a future
    reader can find the closing PR / postmortem without grep-fishing.

    Both anchors are cited verbatim in the doc body. If a future Bit
    rewrites the section and drops them, this test fires the L97
    stale-anchor signal."""
    text = DOC_PATH.read_text(encoding="utf-8")
    assert "86b9zudg2" in text, (
        "doc must reference B5 ticket ID `86b9zudg2` (commit 76f1d54c) "
        "for the TM-stack-decided fix anchor."
    )
    assert "76f1d54c" in text, (
        "doc must reference B5 commit hash `76f1d54c` so readers can "
        "navigate to the closing PR without grep-fishing."
    )
