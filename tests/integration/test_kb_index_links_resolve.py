"""Regression tests pinning every wiki-link in kb/_index.md to a real file.

`kb/_index.md` is the curated agent-guide entry point for the knowledge base.
Per Bit 4.2.5.2 (May 2026), an audit found 8 orphan references — wiki-links
that pointed to files that had never been created and had no git history.
The orphans likely accumulated as session notes were renamed or merged
without `_index.md` being updated.

These tests run against the working tree (not git HEAD). If a maintainer
adds a `[[foo/bar.md]]` reference to a file they haven't created yet, the
suite trips loudly so the broken link is caught before the next reader
follows it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
KB_INDEX = REPO_ROOT / "kb" / "_index.md"
KB_DIR = REPO_ROOT / "kb"

WIKI_LINK_RE = re.compile(r"\[\[([^\]]+\.md)\]\]")


@pytest.fixture(scope="module")
def index_text():
    assert KB_INDEX.exists(), f"{KB_INDEX} missing — kb/_index.md is required."
    return KB_INDEX.read_text()


@pytest.fixture(scope="module")
def wiki_refs(index_text):
    return WIKI_LINK_RE.findall(index_text)


def test_index_has_at_least_one_wiki_link(wiki_refs):
    assert len(wiki_refs) > 0, "kb/_index.md has no [[…]] references at all."


def test_every_wiki_link_resolves_to_a_file(wiki_refs):
    """Each `[[<rel-path>.md]]` in kb/_index.md must point to a file
    that exists under kb/. Orphan references (file never created or
    deleted-and-not-cleaned-up) are blocked here.
    """
    missing = []
    for ref in wiki_refs:
        full = KB_DIR / ref
        if not full.exists():
            missing.append(ref)
    assert not missing, (
        f"{len(missing)} orphan reference(s) in kb/_index.md — these wiki-links "
        f"point to files that don't exist on disk:\n  - "
        + "\n  - ".join(sorted(missing))
        + "\nEither create the file, remove the reference, or rename to the "
        f"correct path."
    )


def test_no_duplicate_wiki_links(wiki_refs):
    """Each article should appear at most once in the curated index. Duplicate
    refs likely mean a section was reorganized without cleanup."""
    seen = {}
    duplicates = []
    for ref in wiki_refs:
        if ref in seen:
            duplicates.append(ref)
        else:
            seen[ref] = True
    assert not duplicates, (
        f"Duplicate wiki-links in kb/_index.md: {sorted(set(duplicates))}. "
        f"Each article should be referenced once."
    )


def test_section_counts_match_actual_entries(index_text):
    """Section headers like `## Failures (14)` must match the number of
    `- [[…]]` bullets that follow before the next `## ` header. Drift here
    is a common audit-noise source (the count is human-maintained)."""
    section_re = re.compile(
        r"^## (?P<title>[A-Z][^(]+?)\s*\((?P<count>\d+)\)\s*$",
        re.MULTILINE,
    )
    sections = list(section_re.finditer(index_text))
    assert sections, "No `## Heading (N)` sections found in kb/_index.md."
    mismatches = []
    for i, m in enumerate(sections):
        start = m.end()
        end = sections[i + 1].start() if i + 1 < len(sections) else len(index_text)
        body = index_text[start:end]
        # Count top-level bullets that contain a wiki-link (or that look like
        # archived plain-text bullets — we explicitly ALLOW the archived
        # section's wiki-style bullets).
        bullet_lines = [
            ln
            for ln in body.splitlines()
            if ln.startswith("- ") and "[[" in ln and ".md]]" in ln
        ]
        claimed = int(m.group("count"))
        actual = len(bullet_lines)
        if claimed != actual:
            mismatches.append(
                (m.group("title").strip(), claimed, actual)
            )
    assert not mismatches, (
        "kb/_index.md section counts drifted from actual bullet counts:\n  "
        + "\n  ".join(
            f"{title}: header says ({claimed}), actual is {actual}"
            for title, claimed, actual in mismatches
        )
    )
