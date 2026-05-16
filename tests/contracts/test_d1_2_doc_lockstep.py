"""D1.2 + D1.3 + D1.4 sister-doc lockstep — break the prose-drift cycle proactively (L97 + L99).

Tickets `86b9ypn66` (D1.2) + `86b9ypn72` (D1.3) + `86b9ypn8r` (D1.4),
all 2026-05-16.
Created post-D1.2 R2 adversarial review which surfaced 4 MAJOR findings
all in the same drift class: tracked sister docs still saying
"D1.2-D1.5" / "raises NotImplementedError" / "land at D1.2" after D1.2
had shipped. **D1.3 extends the ratchet with PARANOID pattern coverage
per the D1.2 L99 lesson** — broader TRACKED_DOCS + STALE_PATTERNS at
day one (rather than waiting for an adversarial round to surface a
blind spot, then patching reactively).

L97 from P2.3 live-promotion (8 adv rounds for narrative-prose drift) +
the parent agent's recommendation: rather than depend on grep
discipline at each sub-Bit's adversarial round, pin the structural
invariant as a contract test. If a future Bit reintroduces stale
forward-looking phrasing, this test fires immediately at the contract
tier gate (~5s budget) instead of being caught at R2 or later.

Patterns checked (D1.2 carry + D1.3 additions):

D1.2 patterns (false after D1.2 SHIPPED):
- ``land at D1.2-D1.5``: false after D1.2 shipped — should be D1.3-D1.5
  or D1.3-D1.4 (whichever the surface narrows to).
- ``raises NotImplementedError`` + ``D1.2``: false now; D1.2 is the body
  ship.
- ``D1.2 target``: implies D1.2 hasn't shipped.

D1.3 patterns (false after D1.3 SHIPPED):
- ``D1.3 target``: implies D1.3 hasn't shipped (subscription_manager body).
- ``D1.3 will add``: forward-looking; D1.3 already shipped.
- ``D1.3 will generalize``: ditto for main_loop multi-conn.
- ``D1.3's acceptance criterion`` (forward-tense): D1.3 met the criterion.
- ``until D1.3 lands``: D1.3 landed.
- ``until D1.3 subscription_manager``: D1.3 shipped subscription_manager.
- ``no subscribe frames are sent``: false post-D1.3 (on_session_start dispatches).
- ``BronzeArchiver does NOT send any subscribe frames``: same.
- ``rest_snapshot.py, subscription_manager.py``: stale enumeration —
  post-D1.3 only rest_snapshot remains.
- ``land at D1.3-D1.4``: false; only D1.4 remains.
- ``land at D1.3 (``: forward-looking gerund.

If you're shipping D1.4+ and a NEW forward-looking phrase needs to land
("D1.4-D1.5"), update this test's allow-list AT THE SAME TIME. The test
is a ratchet, not a permanent ban.

If you're updating closeout docs in `kb/decisions/` that contain
narrative-history-only references to the old forward-looking phrases,
add the closeout path to ``HISTORICAL_NARRATIVE_PATHS`` below — but
prefer rephrasing the closeout to use past-tense (e.g., "the original
D1.3 target was…") so the ratchet stays load-bearing.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]

# Tracked sister docs that describe the collector/ + kalshi_wire/
# architecture. Adding a new doc to this list extends the ratchet.
# R3 (D1.2) expanded coverage to include sister test files + kalshi_wire/
# source after R2's set proved too narrow (4 blind spots surfaced).
# D1.3 adds: new D1.3 contract tests + collector/subscription_manager.py
# (whose body lands at D1.3 — historical narrative referencing "D1.3
# target" must flip).
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
    REPO_ROOT / "collector" / "subscription_manager.py",
    REPO_ROOT / "collector" / "rest_snapshot.py",
    REPO_ROOT / "kalshi_wire" / "__init__.py",
    REPO_ROOT / "kalshi_wire" / "auth.py",
    REPO_ROOT / "kalshi_wire" / "ws_client.py",
    REPO_ROOT / "tests" / "contracts" / "test_collector_no_bot_imports.py",
    REPO_ROOT / "tests" / "contracts" / "test_kalshi_wire_no_collector.py",
    REPO_ROOT / "tests" / "contracts" / "test_kalshi_wire_no_bot.py",
    REPO_ROOT / "tests" / "contracts" / "test_collector_ws_consumes_wire.py",
    REPO_ROOT / "tests" / "contracts" / "test_collector_subscription_manager.py",
    REPO_ROOT / "tests" / "contracts" / "test_bronze_archiver_on_session_start.py",
    REPO_ROOT / "tests" / "contracts" / "test_collector_rest_snapshot.py",
    REPO_ROOT / "tests" / "integration" / "test_collector_main_loop_wireup.py",
    REPO_ROOT / "tests" / "integration" / "test_collector_rest_snapshot_refresh_cycle.py",
]

# Patterns that are FALSE post-D1.2 SHIPPED. If any tracked doc above
# contains one of these literal substrings, the doc is stale.
# R3 (D1.2) added: "lands at D1.2", "at D1.2 the", "after D1.2 lands",
# "future D1.2", "until D1.2" — broader coverage of the same drift class.
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

# Patterns that are FALSE post-D1.3 SHIPPED. PARANOID coverage at day 1
# per L99 lesson — broader than the minimal set we'd patch reactively.
STALE_PATTERNS_POST_D1_3: list[str] = [
    "D1.3 target",
    "D1.3 will add",
    "D1.3 will generalize",
    "until D1.3 lands",
    "until D1.3 subscription_manager",
    "after D1.3 lands",
    "future D1.3",
    "lands at D1.3",
    "land at D1.3-D1.5",  # narrows to D1.4 post-D1.3
    "land at D1.3-D1.4",  # narrows to D1.4 post-D1.3
    "land at D1.3 (",
    # First-bronze-flow gating narrative — D1.3 closed this:
    "no subscribe frames are sent",
    "BronzeArchiver does NOT send any subscribe frames",
    "the WS connects but no data frames flow",
    "the WS connects but no data flows",
    "First-bronze-flow is D1.3's acceptance criterion",  # past-tense now
    "first-bronze-flow is D1.3's acceptance criterion",  # case variant
    # Stale enumeration — post-D1.3 only rest_snapshot is left.
    "rest_snapshot.py, subscription_manager.py",
    "rest_snapshot.py and subscription_manager.py",
    "subscription_manager.py, rest_snapshot.py",
    # Stale single-conn no-tier narrative — D1.3 generalized to multi-conn.
    "single-conn no-tier shape",
]

# Patterns that are FALSE post-D1.4 SHIPPED. PARANOID coverage at day 1
# per L99 lesson — extending the pattern set proactively rather than
# letting R-N rounds discover blind spots.
#
# Note: phrases that exclusively reference D1.5+ work (systemd deploy,
# operator decisions, etc.) MUST NOT be added here — those are still
# legitimately forward-looking after D1.4. Only patterns that became
# false at the moment D1.4 shipped go here.
STALE_PATTERNS_POST_D1_4: list[str] = [
    "D1.4 target",
    "D1.4 will add",
    "D1.4 will replace",
    "D1.4 (rest_snapshot, ",  # only-D1.5 unblocked phrasing post-D1.4
    "land at D1.4 (",
    "until D1.4 lands",
    "until D1.4 REST snapshot",
    "until D1.4 subscription_manager",  # belt-and-suspenders — was D1.3
    "after D1.4 lands",
    "future D1.4",
    "lands at D1.4",
    "D1.4 implementation target",
    "D1.4 replaces this",
    # Stub-fossil module docstring (rest_snapshot.py had a stub pre-D1.4).
    "REST-fallback redundancy for catalog refresh",
    "D1.4 (REST snapshot, 86b9ypn8r)  ← NEXT",  # pickup-chain phrasing
    # Stale single-file-pending narrative — post-D1.4 the rest_snapshot
    # body shipped; the "only rest_snapshot remains" framing is no
    # longer accurate as the pending-Bit description.
    "only rest_snapshot remains",
    "only D1.4 remains",
    # R1-M3 (D1.4 R1 adv finding): the original PARANOID set used
    # literal substring matching but missed parenthetical forms like
    # ``D1.4 (REST snapshot) will replace`` and ``before D1.4 REST
    # snapshot populates``. Extending coverage with the specific
    # phrases the R1 reviewer found across 3 collector source files.
    "D1.4 (REST snapshot) will replace",
    "D1.4 (REST snapshot) will populate",
    "D1.4 (REST snapshot) will add",
    "before D1.4 REST snapshot",
    "once D1.4 REST snapshot",
    "wait for D1.4 REST snapshot",
    "D1.4 REST snapshot populates",
    "D1.4 REST snapshot lands",
    # Phrases that explicitly tag the *production-default* as still being
    # the file seam are stale post-D1.4 (REST is the new default).
    "operator points COLLECTOR_TICKERS_FILE at",
    "COLLECTOR_TICKERS_FILE is the only seam",
    "no production file at it",
    # R2-M1/R2-Mn1/R3-M1 meta-pattern: when R1-M4 retracted the
    # "lock-protected against in-flight on_frame dispatch" overclaim at
    # the canonical docstring, sister surfaces (CLAUDE.md + bot_layout.md
    # + _replan_for_archivers docstring + the test-file module docstring)
    # echoed the now-retracted claim. Encode the retracted phrases as
    # ratchet patterns so a future Bit reintroducing them fires at the
    # contract tier instead of waiting for an adversarial round.
    "lock-protected atomic swap",
    "lock-protected against in-flight on_frame dispatch",
    # R3-M2 echo of R1-M2 retract: test/doc surfaces saying fetch
    # returns "empty ticker list / empty / partial map" on failure
    # paths are stale post-R1-M2 (fetch returns None on failure).
    "returns an empty ticker list",
    "empty page returns an empty",
    "return an empty / partial",
    "return an empty or partial",
    "return an empty/partial",
]


# The "raises NotImplementedError" mention is only allowed in
# tests/  + .md histories. In the live collector/ source files, no
# function body should still raise NotImplementedError post-D1.2.
# D1.3 carries this rule forward — subscription_manager.py body now
# exists, so any NIE in it is fresh staleness.
NIE_ALLOWED_FILES: set[str] = {
    # historical narrative — D1.1/D1.1.5 closeout docs may legitimately
    # reference the prior stub-with-NotImplementedError state.
    # Add here if a NEW historical doc is created.
}

# Paths in ``kb/decisions/`` that legitimately reference stale forward-
# looking phrases as historical narrative (closeout docs that quote what
# the pre-ship state was). Out of scope for this ratchet — they are not
# in TRACKED_DOCS, but listing here makes the boundary explicit. NEW
# closeouts should prefer past-tense phrasing ("the original D1.3 target
# was…") so the ratchet remains load-bearing.
HISTORICAL_NARRATIVE_PATHS: set[str] = set()


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


@pytest.mark.parametrize("pattern", STALE_PATTERNS_POST_D1_3)
def test_no_post_d1_3_stale_forward_looking_phrase(pattern: str):
    """No tracked doc should still say a D1.3-pending phrase after D1.3 shipped.

    PARANOID coverage at day-1 per L99 — extending the pattern set
    proactively rather than letting R-N rounds discover blind spots.
    """
    findings: list[str] = []
    for doc in TRACKED_DOCS:
        for lineno, line in _scan(doc, pattern):
            findings.append(f"{doc.relative_to(REPO_ROOT)}:{lineno}: {line}")
    assert not findings, (
        f"Stale D1.3-pending phrasing detected (pattern {pattern!r}):\n"
        + "\n".join(findings)
        + "\n\nL99 lesson (from D1.2 R3): lockstep ratchets must have "
        "PARANOID pattern coverage from day-1 to avoid reactive R-N "
        "round patches. If THIS pattern is a legitimate D1.4+ forward-"
        "looking phrase, narrow it (e.g., add a specific qualifier that "
        "won't match historical D1.3 prose)."
    )


def test_collector_and_kalshi_wire_source_files_have_no_notimplemented_post_d1_2():
    """Live collector/ + kalshi_wire/ source modules must not contain a
    function body that ``raise NotImplementedError``s — D1.2 shipped the
    bodies, D1.3 shipped subscription_manager. Tests under tests/contracts/
    are out of scope (they may legitimately reference NotImplementedError
    in docstring narrative)."""
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


@pytest.mark.parametrize("pattern", STALE_PATTERNS_POST_D1_4)
def test_no_post_d1_4_stale_forward_looking_phrase(pattern: str):
    """No tracked doc should still say a D1.4-pending phrase after D1.4 shipped.

    L99 PARANOID-at-day-1 ratchet extension for D1.4 (REST snapshot
    body + RestSnapshotRefresher + main_loop hourly refresh wiring).
    """
    findings: list[str] = []
    for doc in TRACKED_DOCS:
        for lineno, line in _scan(doc, pattern):
            findings.append(f"{doc.relative_to(REPO_ROOT)}:{lineno}: {line}")
    assert not findings, (
        f"Stale D1.4-pending phrasing detected (pattern {pattern!r}):\n"
        + "\n".join(findings)
        + "\n\nL99 lesson (D1.2 R3, reaffirmed D1.3): lockstep ratchets "
        "must have PARANOID pattern coverage from day-1. If THIS pattern "
        "is a legitimate D1.5+ forward-looking phrase, narrow it (e.g., "
        "add a qualifier that won't match historical D1.4 prose)."
    )


def test_d1_4_shipped_status_in_at_least_one_tracked_doc():
    """Positive assertion: at least one tracked doc explicitly marks D1.4
    as SHIPPED. Catches the inverse failure mode where staleness patterns
    pass (no D1.4 mention at all) but the docs haven't been updated."""
    shipped_re = re.compile(
        r"D1\.4\s+SHIPPED|D1\.4.*shipped|shipped.*D1\.4",
        re.IGNORECASE,
    )
    matched_docs: list[str] = []
    for doc in TRACKED_DOCS:
        if not doc.is_file():
            continue
        if shipped_re.search(doc.read_text()):
            matched_docs.append(str(doc.relative_to(REPO_ROOT)))
    assert matched_docs, (
        "No tracked doc claims D1.4 SHIPPED — staleness ratchets clean "
        "but nothing affirms the ship. Update at least one of:\n  "
        + "\n  ".join(str(d.relative_to(REPO_ROOT)) for d in TRACKED_DOCS)
    )


def test_d1_3_shipped_status_in_at_least_one_tracked_doc():
    """Positive assertion: at least one tracked doc explicitly marks D1.3
    as SHIPPED. Catches the inverse failure mode where staleness patterns
    pass (no D1.3 mention at all) but the docs haven't actually been
    updated to claim D1.3 SHIPPED.
    """
    shipped_re = re.compile(
        r"D1\.3\s+SHIPPED|D1\.3.*shipped|shipped.*D1\.3",
        re.IGNORECASE,
    )
    matched_docs: list[str] = []
    for doc in TRACKED_DOCS:
        if not doc.is_file():
            continue
        if shipped_re.search(doc.read_text()):
            matched_docs.append(str(doc.relative_to(REPO_ROOT)))
    assert matched_docs, (
        "No tracked doc claims D1.3 SHIPPED — staleness ratchets clean "
        "but nothing affirms the ship. Update at least one of:\n  "
        + "\n  ".join(str(d.relative_to(REPO_ROOT)) for d in TRACKED_DOCS)
    )
