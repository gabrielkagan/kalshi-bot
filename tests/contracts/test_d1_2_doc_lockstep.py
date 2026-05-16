"""D1.2 sister-doc lockstep — break the prose-drift cycle proactively (L97).

Ticket `86b9ypn66` (2026-05-16). Created post-R2 adversarial review which
surfaced 4 MAJOR findings all in the same drift class: tracked sister
docs still saying "D1.2-D1.5" / "raises NotImplementedError" / "land at
D1.2" after D1.2 had shipped.

L97 from P2.3 live-promotion (8 adv rounds for narrative-prose drift) +
the parent agent's recommendation here: rather than depend on grep
discipline at each sub-Bit's adversarial round, pin the structural
invariant as a contract test. If a future Bit reintroduces stale
forward-looking phrasing, this test fires immediately at the contract
tier gate (~5s budget) instead of being caught at R2 or later.

The patterns checked here are the EXACT strings R2 flagged:

- ``land at D1.2-D1.5``: false after D1.2 shipped — should be D1.3-D1.5
  or D1.3-D1.4 (whichever the surface narrows to).
- ``raises NotImplementedError`` + ``D1.2``: false now; D1.2 is the body
  ship.
- ``D1.2 target``: implies D1.2 hasn't shipped.

If you're shipping D1.3+ and a NEW forward-looking phrase needs to land
("D1.3-D1.5"), update this test's allow-list AT THE SAME TIME. The test
is a ratchet, not a permanent ban.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]

# Tracked sister docs that describe the collector/ + kalshi_wire/
# architecture. Adding a new doc to this list extends the ratchet.
# R3 expanded coverage to include sister test files + kalshi_wire/
# source after R2's set proved too narrow (4 blind spots surfaced).
TRACKED_DOCS: list[Path] = [
    REPO_ROOT / "CLAUDE.md",
    REPO_ROOT / "CONTRIBUTING.md",
    REPO_ROOT / "README.md",
    REPO_ROOT / "README.template.md",
    REPO_ROOT / "agent_docs" / "bot_layout.md",
    REPO_ROOT / "collector" / "__init__.py",
    REPO_ROOT / "collector" / "__main__.py",
    REPO_ROOT / "collector" / "main_loop.py",
    REPO_ROOT / "collector" / "ws_connection.py",
    REPO_ROOT / "collector" / "writer.py",
    REPO_ROOT / "collector" / "uploader.py",
    REPO_ROOT / "kalshi_wire" / "__init__.py",
    REPO_ROOT / "kalshi_wire" / "auth.py",
    REPO_ROOT / "kalshi_wire" / "ws_client.py",
    REPO_ROOT / "tests" / "contracts" / "test_collector_no_bot_imports.py",
    REPO_ROOT / "tests" / "contracts" / "test_kalshi_wire_no_collector.py",
    REPO_ROOT / "tests" / "contracts" / "test_kalshi_wire_no_bot.py",
    REPO_ROOT / "tests" / "contracts" / "test_collector_ws_consumes_wire.py",
]

# Patterns that are FALSE post-D1.2 SHIPPED. If any tracked doc above
# contains one of these literal substrings, the doc is stale.
# R3 added: "lands at D1.2", "at D1.2 the", "after D1.2 lands",
# "future D1.2", "until D1.2" — broader coverage of the same drift
# class (D1.2 referred to as future-tense).
STALE_PATTERNS_POST_D1_2: list[str] = [
    "land at D1.2-D1.5",
    "D1.2-D1.5 per the Data Corpus",  # README/template specific
    "D1.2 target",
    "implementations land at D1.2-D1.5",
    "lands at D1.2",
    "at D1.2 the",
    "after D1.2 lands",
    "future D1.2",
    "until D1.2",
]

# The "raises NotImplementedError" mention is only allowed in
# tests/  + .md histories. In the live collector/ source files, no
# function body should still raise NotImplementedError post-D1.2.
NIE_ALLOWED_FILES: set[str] = {
    # historical narrative — D1.1/D1.1.5 closeout docs may legitimately
    # reference the prior stub-with-NotImplementedError state.
    # Add here if a NEW historical doc is created.
}


def _scan(path: Path, needle: str) -> list[tuple[int, str]]:
    if not path.is_file():
        return []
    hits: list[tuple[int, str]] = []
    for i, line in enumerate(path.read_text().splitlines(), start=1):
        if needle in line:
            hits.append((i, line.strip()))
    return hits


@pytest.mark.parametrize("pattern", STALE_PATTERNS_POST_D1_2)
def test_no_post_d1_2_stale_forward_looking_phrase(pattern: str):
    """No tracked doc should still say a D1.2-pending phrase after D1.2 shipped."""
    findings: list[str] = []
    for doc in TRACKED_DOCS:
        for lineno, line in _scan(doc, pattern):
            findings.append(f"{doc.relative_to(REPO_ROOT)}:{lineno}: {line}")
    assert not findings, (
        f"Stale D1.2-pending phrasing detected (pattern {pattern!r}):\n"
        + "\n".join(findings)
        + "\n\nL97 lesson: prose drift across sister docs is the most "
        "common adversarial-review finding. Update ALL tracked surfaces "
        "in the same commit as the body change."
    )


def test_collector_and_kalshi_wire_source_files_have_no_notimplemented_post_d1_2():
    """Live collector/ + kalshi_wire/ source modules must not contain a
    function body that ``raise NotImplementedError``s — D1.2 shipped the
    bodies. Tests under tests/contracts/ are out of scope (they may
    legitimately reference NotImplementedError in docstring narrative)."""
    findings: list[str] = []
    nie_re = re.compile(r"raise\s+NotImplementedError")
    for doc in TRACKED_DOCS:
        if doc.suffix != ".py":
            continue
        if doc.name in NIE_ALLOWED_FILES:
            continue
        rel = str(doc.relative_to(REPO_ROOT))
        # Only check collector/ and kalshi_wire/ live sources; tests are
        # narrative and may reference NIE in docstrings.
        if not (rel.startswith("collector/") or rel.startswith("kalshi_wire/")):
            continue
        for lineno, line in enumerate(doc.read_text().splitlines(), start=1):
            if nie_re.search(line):
                findings.append(f"{rel}:{lineno}: {line.strip()}")
    assert not findings, (
        f"Live collector/kalshi_wire source still raises NotImplementedError post-D1.2:\n"
        + "\n".join(findings)
    )
