"""Pillar 2 of the testing-foundation-sprint (ticket 86b9ve0yt).

Pins the import-linter wiring so a future edit can't silently
disable it:

1. ``import-linter`` is in ``[project.optional-dependencies].dev`` —
   the dev extras install path used by both CI workflows.
2. ``.importlinter`` exists at the repo root with the four contracts
   the Bit shipped (engines / fetchers / feeds / helpers boundaries)
   plus the documented ``ignore_imports`` carve-out for the Bit 6.2
   late-binding in ``bot/engines/probability.py``.
3. The ``lint-imports`` step is wired into ``.github/workflows/test.yml``
   AND ``.github/workflows/deploy.yml`` BEFORE the pytest step so a
   layering violation fails fast.
4. ``lint-imports`` exits 0 on the current tree (the contracts must
   reflect reality, not aspirations).
5. Removing the ``ignore_imports`` carve-out causes ``lint-imports`` to
   fail — proves the engines→_impl gate is real and the carve-out is
   load-bearing.
6. The ``helpers-leaf`` contract's ``forbidden_modules`` list covers
   every top-level ``bot/`` module (besides ``bot.helpers`` itself and
   the allowed leaf dep ``bot.constants``). Closes the
   enumerate-the-deny-list drift hazard by checking the deny-list at
   test time against the actual filesystem.

Companion to ``tests/contracts/test_public_api_snapshot.py`` (Pillar 1)
and the per-Bit ``test_*_extraction.py`` AST guards. import-linter sees
imports through the grimp graph; the AST guards see source patterns;
the public-API snapshot sees griffe's static surface walk + a runtime
proxy probe. All three layers are needed.

If this test fails:
- Intentional contract change: edit ``.importlinter`` AND update the
  expected-contract list / EXPECTED_LEAF_FORBIDDEN below in lock-step.
- ``lint-imports`` exits non-zero on current main: a code change
  introduced a layering violation. Either fix the import or — if the
  edge is genuinely required — add an explicit ``ignore_imports`` line
  in ``.importlinter`` with KB-doc rationale.
"""
from __future__ import annotations

import configparser
import os
import shutil
import site
import subprocess
import sys
import sysconfig
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
IMPORTLINTER_PATH = REPO_ROOT / ".importlinter"
PYPROJECT_PATH = REPO_ROOT / "pyproject.toml"
TEST_YML_PATH = REPO_ROOT / ".github" / "workflows" / "test.yml"
DEPLOY_YML_PATH = REPO_ROOT / ".github" / "workflows" / "deploy.yml"

EXPECTED_CONTRACTS = (
    "engines-no-impl",
    "fetchers-no-engines",
    "feeds-no-engines",
    "helpers-leaf",
)

# bot.constants is the only allowed internal dep for the helpers leaf.
# bot.helpers is the source of the contract — listing itself in
# forbidden_modules would block intra-package re-exports inside
# bot/helpers/__init__.py. Everything else under bot/ must be in
# forbidden_modules of the helpers-leaf contract.
LEAF_ALLOWED_DEPS = frozenset({"bot.helpers", "bot.constants"})

# bot/__init__.py and bot/__main__.py are the package proxy + runtime
# entrypoint. They live ABOVE the helpers layer; helpers can never
# legitimately import them, but listing them as forbidden_modules is
# a no-op (helpers doesn't import them today and no fix would route
# through them). They're excluded from the deny-list-coverage check
# so the regression test doesn't force noise into .importlinter.
NON_MODULE_NAMES = frozenset({"__init__", "__main__"})


# ─── Config-file helpers (used across multiple tests) ────────────────────────


def _read_importlinter() -> configparser.RawConfigParser:
    """Parse `.importlinter` as INI. Raises if file missing — the
    `test_importlinter_file_exists` test would have already flagged
    that case, but defensive parsing still catches surprises (file
    truncated to 0 bytes, broken section header, etc.)
    """
    cp = configparser.RawConfigParser()
    # `read` returns the list of files actually parsed; fail loudly if empty.
    parsed = cp.read(IMPORTLINTER_PATH)
    assert parsed, f".importlinter at {IMPORTLINTER_PATH} is missing or unreadable."
    return cp


def _multiline_values(cp: configparser.RawConfigParser, section: str, key: str) -> list[str]:
    """Return the non-empty stripped lines of a multi-line INI value.

    import-linter's ini format uses indented continuation lines, which
    configparser concatenates into a single value with embedded
    newlines. Splitting + stripping recovers the per-entry list.
    """
    raw = cp.get(section, key, fallback="")
    return [line.strip() for line in raw.splitlines() if line.strip()]


def _enumerate_top_level_bot_modules() -> set[str]:
    """Walk bot/ for top-level modules (.py files) and subpackages
    (dirs with __init__.py). Returns dotted names like 'bot._impl',
    'bot.engines'. Excludes proxy/entrypoint __init__/__main__ and
    the allowed leaf deps.
    """
    bot_dir = REPO_ROOT / "bot"
    modules: set[str] = set()
    for entry in bot_dir.iterdir():
        if entry.is_file() and entry.suffix == ".py":
            stem = entry.stem
            if stem in NON_MODULE_NAMES:
                continue
            modules.add(f"bot.{stem}")
        elif entry.is_dir() and (entry / "__init__.py").exists():
            # __pycache__ has no __init__.py so it won't pass this gate.
            modules.add(f"bot.{entry.name}")
    return modules - LEAF_ALLOWED_DEPS


# ─── 1. Dev-deps ─────────────────────────────────────────────────────────────


def test_import_linter_in_dev_deps():
    """``import-linter`` is in ``[project.optional-dependencies].dev``.

    Both CI workflows install via ``pip install -e '.[dev]'`` (Pillar 1
    hotfix `277e8ab` mirrored that into deploy.yml); the lint-imports
    binary won't be on PATH otherwise.
    """
    src = PYPROJECT_PATH.read_text()
    # Anchored on `>=` so a future name like `import-linter-helpers`
    # can't accidentally match this assertion.
    assert '"import-linter>=' in src, (
        "pyproject.toml dev extras missing import-linter pin. "
        "Pillar 2 (ticket 86b9ve0yt) requires it as a dev-only dep."
    )


def test_import_linter_pin_within_v2():
    """Pin floor + cap are in place (matches griffe pin discipline).

    A future bump to v3 needs deliberate review of the ``ignore_imports``
    + ``forbidden`` syntax (which has changed across major versions in
    similar tooling).
    """
    src = PYPROJECT_PATH.read_text()
    assert '"import-linter>=2.5,<3"' in src, (
        "import-linter pin must be `>=2.5,<3`. Edit deliberately when "
        "v3 ships and the contract syntax is reverified."
    )


# ─── 2. .importlinter file ──────────────────────────────────────────────────


def test_importlinter_file_exists():
    assert IMPORTLINTER_PATH.is_file(), (
        ".importlinter missing at repo root. Pillar 2 (ticket "
        "86b9ve0yt) ships this file; lint-imports has nothing to "
        "enforce without it."
    )


def test_importlinter_root_package_is_bot():
    """``root_package = bot`` anchors the grimp graph.

    Without this header, the contracts have no source tree to walk.
    """
    cp = _read_importlinter()
    assert cp.has_section("importlinter"), "missing top-level [importlinter] section"
    assert cp.get("importlinter", "root_package", fallback=None) == "bot", (
        ".importlinter [importlinter].root_package must be `bot`."
    )


@pytest.mark.parametrize("contract_id", EXPECTED_CONTRACTS)
def test_importlinter_declares_expected_contract(contract_id: str):
    cp = _read_importlinter()
    section = f"importlinter:contract:{contract_id}"
    assert cp.has_section(section), (
        f"Expected contract section [{section}] missing from "
        f".importlinter. If renamed, update EXPECTED_CONTRACTS in this "
        f"test in the same commit."
    )
    # Every contract has a type and source_modules; pin both to catch
    # accidental mis-labelings (e.g., 'forbidden' typo'd to 'forbiden'
    # silently disables the contract under import-linter v2).
    assert cp.get(section, "type", fallback=None) == "forbidden", (
        f"contract {contract_id}: type must be `forbidden`. Pillar 2 "
        f"explicitly uses forbidden semantics; switching to `layered` "
        f"or `independence` requires updating the test expectations."
    )


def test_importlinter_engines_carve_out_for_bit_6_2():
    """The Bit 6.2 late-binding carve-out is documented as
    ``ignore_imports`` of the engines-no-impl contract — NOT just as a
    comment somewhere in the file.

    Path (a) of the ticket Open Question — ship now with carve-out +
    rationale, file a separate ticket for path (b) refactor.

    Configparser-level check (not a substring scan) so a future edit
    that comments-out / moves the carve-out string but leaves the
    inline doc block intact CANNOT false-green this test.
    """
    cp = _read_importlinter()
    section = "importlinter:contract:engines-no-impl"
    assert cp.has_section(section), "engines-no-impl contract missing"
    ignore_imports = _multiline_values(cp, section, "ignore_imports")
    assert "bot.engines.probability -> bot._impl" in ignore_imports, (
        "ignore_imports for the Bit 6.2 late-binding pattern is "
        "missing from the [importlinter:contract:engines-no-impl] "
        "section's ignore_imports key.\n"
        "Either restore it (with KB-doc rationale) or remove the "
        "late-binding from bot/engines/probability.py and lift "
        "_CALIBRATION_ENGINE / _resolve_cal_engine into an injected "
        "dependency (path (b) refactor)."
    )


def test_helpers_leaf_forbidden_modules_covers_all_bot_top_level():
    """The helpers-leaf contract is enumerate-the-deny-list (a quirk
    of import-linter's ``forbidden`` type). To prevent silent drift —
    Bit 7.x ships ``bot/scheduler/``, helpers gain a back-edge to it,
    contract still says KEPT — this test walks ``bot/`` and asserts
    every top-level module/subpackage is in ``forbidden_modules``
    (besides ``bot.helpers`` itself and the allowed leaf dep
    ``bot.constants``).

    R1 finding: this test exists because the original contract
    enforces only the explicit list; the leaf rule per the ticket AC
    is "no internal bot.* imports beyond bot.constants" which the
    forbidden type CAN'T express directly. The walk closes the gap.
    """
    cp = _read_importlinter()
    section = "importlinter:contract:helpers-leaf"
    listed = set(_multiline_values(cp, section, "forbidden_modules"))
    required = _enumerate_top_level_bot_modules()
    missing = required - listed
    assert not missing, (
        "helpers-leaf.forbidden_modules is missing top-level bot/ "
        f"modules: {sorted(missing)}.\n"
        "Either append them to the forbidden_modules list in "
        ".importlinter, OR (if the new module is genuinely an allowed "
        "leaf dependency like bot.constants) add it to "
        "LEAF_ALLOWED_DEPS in this test in the same commit. New "
        "top-level bot/ modules MUST be appended in the same Bit that "
        "creates them — this is the trade-off for the deny-list "
        "approach."
    )
    # Defense-in-depth (R2-m3): also catch entries listed in the deny-
    # list that don't correspond to a real bot/ module. A typo'd entry
    # like `bot.engine` (singular) would silently land in the contract
    # and import-linter wouldn't flag it (it just resolves to nothing in
    # the import graph). Without this check, the contract drift would
    # only surface as a phantom-deny-list entry no one notices.
    extra = listed - required - LEAF_ALLOWED_DEPS
    assert not extra, (
        "helpers-leaf.forbidden_modules contains entries that don't "
        f"correspond to real top-level bot/ modules: {sorted(extra)}.\n"
        "Likely a typo (e.g. `bot.engine` instead of `bot.engines`) — "
        "import-linter would silently no-op the dead entry. Either "
        "remove it OR (if it's a planned-but-not-yet-extracted module) "
        "add it to LEAF_ALLOWED_DEPS in this test with a comment."
    )


# ─── 3. CI wiring ───────────────────────────────────────────────────────────


def _split_steps(workflow_content: str) -> list[str]:
    """Split a GitHub Actions workflow into per-step blocks.

    GitHub Actions always uses `- name:` for the step name (it's
    required for steps to render in the UI). We split on that marker,
    drop the prelude (everything before the first step), and return
    one block per step.

    Robust to YAML-comment additions BEFORE/INSIDE step bodies because
    we look at the first line of each block (the name itself), not at
    `find()`-style substring matches across the whole file.
    """
    parts = workflow_content.split("- name:")
    # parts[0] is everything before the first step (header, jobs:, etc.)
    return parts[1:]


def _step_index_by_name(steps: list[str], name_substring: str) -> int:
    """Return the 0-based index of the first step whose name (the line
    AFTER `- name:`) contains the given substring. Returns -1 if no
    step matches.
    """
    for i, block in enumerate(steps):
        first_line = block.split("\n", 1)[0]
        if name_substring in first_line:
            return i
    return -1


def _step_index_running_lint_imports(steps: list[str]) -> int:
    """Return the index of the first step whose body invokes lint-imports.

    Pillar 2 originally shipped a dedicated `- name: Import-linter`
    step with `run: lint-imports`. Pillar 5 (86b9ve11y) folded the
    gate into the contract tier — the workflow now has a step
    `- name: Contract tier ...` whose `run: make test-contract`
    invokes lint-imports as the second half of the recipe (see
    `Makefile::test-contract`). Both shapes are valid for the
    Pillar 2 invariant ("layering violations are gated in CI before
    the broad pytest"); this helper detects either.

    Detection is content-based, not name-based:
      (a) `run: lint-imports` — direct invocation (Pillar 2 shape).
      (b) `run: make test-contract` — Pillar 5 indirection where
          the Make recipe terminates with `$(LINT_IMPORTS)`.
    """
    for i, block in enumerate(steps):
        body = block.split("\n", 1)[1] if "\n" in block else ""
        if "run: lint-imports" in block or "run: lint-imports" in body:
            return i
        if "run: make test-contract" in block or "run: make test-contract" in body:
            return i
    return -1


def _step_index_running_broad_pytest(steps: list[str]) -> int:
    """Return the index of the broad-pytest step.

    Pillar 2 shipped `- name: Run blocking tests` with `pytest tests/`.
    Pillar 5 (86b9ve11y) split the historical broad-pytest into four
    tiers — the broad tier is now `make test-integration` (the catch-all
    that runs after unit + contract + equivalence). Detect either
    shape by content rather than step name.
    """
    for i, block in enumerate(steps):
        body = block.split("\n", 1)[1] if "\n" in block else ""
        text = block + "\n" + body
        if "make test-integration" in text:
            return i
        if 'pytest tests/ -m "not fragile"' in text:
            return i
    return -1


@pytest.mark.parametrize(
    "wf_path",
    [TEST_YML_PATH, DEPLOY_YML_PATH],
    ids=["test.yml", "deploy.yml"],
)
def test_lint_imports_invoked_in_workflow(wf_path: Path):
    """Both workflows must invoke ``lint-imports`` in the blocking tier.

    Pillar 2 originally shipped a dedicated `Import-linter` step;
    Pillar 5 (86b9ve11y) folded the gate into the contract tier so
    the workflow no longer has a standalone lint-imports step. Either
    shape satisfies the Pillar 2 invariant — the helper detects both.

    Without the deploy.yml mirror, a direct push to main could ship a
    layering violation that test.yml would have caught on a PR.
    """
    steps = _split_steps(wf_path.read_text())
    idx = _step_index_running_lint_imports(steps)
    assert idx >= 0, (
        f"{wf_path.name} has no step that invokes lint-imports — neither "
        f"directly (`run: lint-imports`) nor via the Pillar 5 contract "
        f"tier (`run: make test-contract`, where the Make recipe ends "
        f"with $(LINT_IMPORTS)). Pillar 2 wires the gate into both "
        f"test.yml + deploy.yml; dropping it from either path opens a "
        f"contract-bypass route."
    )


@pytest.mark.parametrize(
    "wf_path",
    [TEST_YML_PATH, DEPLOY_YML_PATH],
    ids=["test.yml", "deploy.yml"],
)
def test_lint_imports_runs_before_broad_pytest(wf_path: Path):
    """``lint-imports`` must precede the broad-pytest step.

    Layering check is ~0.5s vs pytest collection at ~2s+; failing fast
    on contract violations is the point.

    Pillar 5 (86b9ve11y) renamed the broad step from "Run blocking
    tests" (single pytest invocation) to "Integration tier"
    (make test-integration). The ordering invariant is unchanged: the
    contract gate runs before the catch-all integration tier.
    """
    steps = _split_steps(wf_path.read_text())
    lint_idx = _step_index_running_lint_imports(steps)
    integration_idx = _step_index_running_broad_pytest(steps)
    assert lint_idx >= 0, (
        f"{wf_path.name}: missing lint-imports invocation (direct or "
        f"via make test-contract)."
    )
    assert integration_idx >= 0, (
        f"{wf_path.name}: missing broad-pytest step (make test-integration "
        f"or the legacy `pytest tests/ -m \"not fragile\"`)."
    )
    assert lint_idx < integration_idx, (
        f"{wf_path.name}: lint-imports invocation (step idx {lint_idx}) "
        f"must precede the integration tier (step idx {integration_idx}). "
        f"Fail-fast on layering violations is the design intent."
    )


# ─── 4. Live behavior ───────────────────────────────────────────────────────


def _lint_imports_cmd() -> list[str] | None:
    """Locate ``lint-imports``; returns None if not installed.

    Search order: PATH → sys.prefix/bin → site.getuserbase()/bin →
    sysconfig scripts. The third path matches user-site pip installs
    (~/Library/Python/X.Y/bin on macOS) which are not on the default
    PATH but are where unprivileged ``pip install`` typically lands.
    """
    binary = shutil.which("lint-imports")
    if binary:
        return [binary]
    try:
        import importlinter  # noqa: F401
    except ImportError:
        return None
    candidates = [
        Path(sys.prefix) / "bin" / "lint-imports",
        Path(site.getuserbase()) / "bin" / "lint-imports",
        Path(sysconfig.get_path("scripts")) / "lint-imports",
    ]
    for cand in candidates:
        if cand.exists():
            return [str(cand)]
    return None


def _require_lint_imports() -> list[str]:
    """Resolve ``lint-imports`` cmd or fail/skip per environment.

    R1 M5 fix: in CI the binary MUST be present (otherwise the live-
    behavior gate has silently disappeared). On dev machines without
    ``[dev]`` extras installed we skip — re-running with
    ``pip install -e '.[dev]'`` recovers full coverage.
    """
    cmd = _lint_imports_cmd()
    if cmd is not None:
        return cmd
    if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
        pytest.fail(
            "lint-imports binary not on PATH despite CI environment. "
            "Verify `pip install -e '.[dev]'` ran successfully and "
            "import-linter installed (it's pinned in pyproject.toml "
            "[project.optional-dependencies].dev). The live-behavior "
            "gate cannot silently skip in CI — that would defeat the "
            "fail-fast contract enforcement Pillar 2 ships."
        )
    pytest.skip("lint-imports binary not on PATH; install via `pip install -e .[dev]`")


def test_lint_imports_passes_on_current_tree():
    """Smoke: ``lint-imports`` exits 0 on the current commit.

    The contract reflects reality, not aspirations — a green main is
    the AC for the Bit. Skips on dev machines without [dev] extras;
    HARD FAILS in CI.
    """
    cmd = _require_lint_imports()
    result = subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"lint-imports failed on current tree:\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


def test_lint_imports_fails_when_carve_out_removed(tmp_path: Path):
    """Negative smoke: removing the ignore_imports for probability.py
    must cause ``lint-imports`` to FAIL.

    Proves both that (a) the engines→_impl contract is real, and (b)
    the carve-out is the only thing keeping main green — i.e., the
    carve-out is load-bearing, not decorative. If a future refactor
    lifts the late-binding (path (b) of the ticket), the carve-out
    can be removed AND this test should be updated to expect lint-
    imports to pass without it.
    """
    cmd = _require_lint_imports()
    src = IMPORTLINTER_PATH.read_text()
    assert "bot.engines.probability -> bot._impl" in src, (
        "Carve-out missing — separate test test_importlinter_engines_"
        "carve_out_for_bit_6_2 should already have flagged this."
    )
    mutated_config = tmp_path / "importlinter_no_carveout"
    mutated = src.replace(
        "ignore_imports =\n    bot.engines.probability -> bot._impl\n",
        "",
    )
    # Defense-in-depth: if the replace was a no-op (string drift), the
    # mutated file would still pass and the test would silently false-
    # green. Pin the mutation actually changed something.
    assert mutated != src, (
        "Mutation no-op — carve-out string format drifted. Update the "
        "replace target in this test in lock-step with .importlinter."
    )
    mutated_config.write_text(mutated)
    result = subprocess.run(
        [*cmd, "--config", str(mutated_config), "--no-cache"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode != 0, (
        "lint-imports passed without the ignore_imports carve-out — "
        "either the contract is no longer enforcing engines→_impl, "
        "or the late-binding has been refactored away (in which case "
        "delete the carve-out from .importlinter AND update this "
        "test).\nstdout:\n" + result.stdout
    )
