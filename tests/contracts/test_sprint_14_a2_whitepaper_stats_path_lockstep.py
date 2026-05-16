"""Sprint 14-A2 follow-up — workflow scp ↔ build_whitepaper.py STATS_PATH lockstep.

PR #30 (sha 6ffacb6) moved whitepaper artifacts to docs/whitepaper/ but its
.github/workflows/whitepaper.yml line 65 scp target was NOT retargeted:

    scp botuser@$VPS:/tmp/whitepaper_stats.json ./whitepaper_stats.json

while build_whitepaper.py:25 reads from:

    STATS_PATH = docs/whitepaper/whitepaper_stats.json

The freshly-scp'd file at root was discarded; the build script read the
stale in-tree docs/whitepaper/whitepaper_stats.json. PDFs continued to
render with frozen 2026-05-16T19:01:30Z numbers across multiple auto-gen
commits before the gap was caught by adversarial review.

This contract test pins the workflow's scp destination to the path that
build_whitepaper.py actually reads from. A future move (e.g., relocating
docs/whitepaper/ again) must update BOTH sites in lockstep or this test
fails.

Scope: ONLY the whitepaper_stats.json scp ↔ STATS_PATH pair. Other
path constants (TEMPLATE_PATH, OUTPUT_PATH, etc.) are not pinned here
because they aren't received via scp — they're rendered locally by
build_whitepaper.py. If a future bug pattern emerges for those, add
sibling tests in this file.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "whitepaper.yml"
BUILD_SCRIPT_PATH = REPO_ROOT / "scripts" / "ops" / "build_whitepaper.py"


def _extract_workflow_scp_destination() -> str:
    """Return the local path the workflow's scp writes whitepaper_stats.json to.

    Parses the `scp <src> <dest>` line; tolerates either `./path` or `path`.
    """
    text = WORKFLOW_PATH.read_text(encoding="utf-8")
    # Match a line like:
    #   scp botuser@${{ secrets.VPS_HOST }}:/tmp/whitepaper_stats.json ./whitepaper_stats.json
    # The GitHub Actions ${{ ... }} expression contains spaces, so we cannot
    # use \S+ for the source. Anchor on `:/tmp/whitepaper_stats.json ` (with
    # trailing space) and capture the destination as the next non-whitespace.
    m = re.search(
        r":/tmp/whitepaper_stats\.json\s+(\S+)\s*$",
        text,
        flags=re.MULTILINE,
    )
    assert m is not None, (
        f"Could not locate the scp line for whitepaper_stats.json in "
        f"{WORKFLOW_PATH.relative_to(REPO_ROOT)}. If the workflow's stats "
        f"transport changed shape (e.g., rsync instead of scp), update this "
        f"test to match. The check itself remains structurally valid: the "
        f"destination of the CI-side fetch must equal build_whitepaper.py's "
        f"STATS_PATH."
    )
    dest = m.group(1).strip()
    # Normalise `./foo` → `foo` for comparison.
    if dest.startswith("./"):
        dest = dest[2:]
    return dest


def _load_build_whitepaper_stats_path() -> str:
    """Load scripts/ops/build_whitepaper.py and return STATS_PATH as a
    repo-relative POSIX-style path.
    """
    spec = importlib.util.spec_from_file_location(
        "_build_whitepaper_under_test", BUILD_SCRIPT_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    abs_path = Path(mod.STATS_PATH).resolve()
    rel = abs_path.relative_to(REPO_ROOT)
    return rel.as_posix()


def test_workflow_scp_destination_matches_build_script_stats_path() -> None:
    """The CI workflow scps whitepaper_stats.json from the VPS into the same
    repo-relative path that build_whitepaper.py reads from. If the two drift,
    the freshly-fetched file is discarded and the build silently uses the
    stale in-tree file — exactly the regression that landed between PR #30
    (2026-05-16 6ffacb6) and its R1 adversarial review."""
    workflow_dest = _extract_workflow_scp_destination()
    build_script_path = _load_build_whitepaper_stats_path()
    assert workflow_dest == build_script_path, (
        f"Drift between CI workflow scp destination and build_whitepaper.py "
        f"STATS_PATH:\n"
        f"  workflow ({WORKFLOW_PATH.relative_to(REPO_ROOT)}): "
        f"scp ... {workflow_dest}\n"
        f"  build script (scripts/ops/build_whitepaper.py STATS_PATH): "
        f"{build_script_path}\n"
        f"\n"
        f"The two must be the same repo-relative path. The workflow scps "
        f"VPS-generated stats to a local path; build_whitepaper.py reads "
        f"from STATS_PATH. If these differ, the build silently uses stale "
        f"in-tree data and PDFs/README freeze at the last matching state."
    )


def test_workflow_cat_path_matches_scp_destination() -> None:
    """The `cat <path>` step immediately after scp must reference the same
    file. A drift here (e.g., cat at root while scp writes to docs/whitepaper/)
    masks the bug by printing the WRONG file in CI logs — looks like fresh
    data but it's stale."""
    text = WORKFLOW_PATH.read_text(encoding="utf-8")
    # The cat is on the line immediately after the scp in the same step.
    # Same regex caveat as above: ${{ ... }} contains spaces; anchor on the
    # remote path's trailing segment and capture local destination + cat target.
    m = re.search(
        r":/tmp/whitepaper_stats\.json\s+(\S+)\s*\n\s*cat\s+(\S+)\s*$",
        text,
        flags=re.MULTILINE,
    )
    assert m is not None, (
        f"Could not locate the scp/cat pair in "
        f"{WORKFLOW_PATH.relative_to(REPO_ROOT)}. If the workflow shape "
        f"changed (extra step between scp and cat, different verifier), "
        f"this test needs an update."
    )
    scp_dest = m.group(1).strip()
    cat_target = m.group(2).strip()
    if scp_dest.startswith("./"):
        scp_dest = scp_dest[2:]
    if cat_target.startswith("./"):
        cat_target = cat_target[2:]
    assert scp_dest == cat_target, (
        f"Workflow scp writes to {scp_dest!r} but the following `cat` reads "
        f"from {cat_target!r}. Either both must be repo-root (legacy) or "
        f"both must be docs/whitepaper/ (post-Sprint-14-A2). A split is a "
        f"masked bug."
    )
