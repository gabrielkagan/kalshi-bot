"""Regression tests for Makefile (Bit 1.2 of repo modularization).

Sprint 1 of repo modularization plan
(kb/decisions/repo-modularization-plan-may05.md).

Pins the Bit 1.2 contract:
- Makefile exists at repo root with all 6 required targets.
- All required targets are .PHONY (no file-name collisions).
- Bare `make` prints help (`.DEFAULT_GOAL := help`).
- Recipe lines are tab-indented (Make's "missing separator" error is
  opaque; an editor-on-save expand-tab silently breaks the file).
- `test` matches CI's blocking filter (`-m "not fragile"`).
- `ast-check` covers `bot.py` (CLAUDE.md sacred-file rule).
- `deploy-check` and `doc-drift` route through existing scripts (no
  reinvention).
- `make -n <target>` parses cleanly for every target.
"""
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MAKEFILE = REPO_ROOT / "Makefile"

REQUIRED_TARGETS = (
    "test",
    "test-fast",
    "ast-check",
    "lint",
    "doc-drift",
    "deploy-check",
)
# Targets we ship beyond the Bit 1.2 spec ("optional" only in the
# spec-conformance sense — they're shipped and must be exercised by the
# .PHONY / dry-run / help-listing invariants exactly like REQUIRED_TARGETS).
OPTIONAL_TARGETS = ("install",)
ALL_TARGETS = REQUIRED_TARGETS + OPTIONAL_TARGETS


def _content() -> str:
    return MAKEFILE.read_text()


def _content_logical_lines() -> str:
    """Return Makefile text with backslash-newline continuations folded.

    Make treats `\\\n` as a logical-line continuation inside `.PHONY:` and
    other declarations. A naive line-by-line regex that doesn't fold
    continuations will silently miss targets declared on continuation
    lines — and report false-positive "missing .PHONY" errors as the
    list grows.
    """
    return re.sub(r"\\\n", " ", MAKEFILE.read_text())


def test_makefile_exists():
    assert MAKEFILE.exists(), (
        "Makefile missing at repo root. Bit 1.2 of repo modularization plan "
        "ships this file."
    )


def test_required_targets_present():
    text = _content()
    for tgt in REQUIRED_TARGETS:
        # Match `<target>:` at start of line, allowing prerequisites
        # (`<target>: dep1 dep2`) but not variable assignments
        # (`<target> := value`).
        assert re.search(rf"^{re.escape(tgt)}:(?!=)", text, re.M), (
            f"Makefile missing target {tgt!r}. Bit 1.2 spec lists 6 required "
            f"targets: {REQUIRED_TARGETS}."
        )


def test_all_targets_phony():
    """Every shipped target (required + optional) must be in .PHONY.

    Continuation-aware: folds `\\\n` first so a future split across lines
    doesn't make targets vanish from the parser's view.
    """
    text = _content_logical_lines()
    phony_lines = re.findall(r"^\.PHONY:\s*(.+)$", text, re.M)
    assert phony_lines, ".PHONY declaration missing."
    declared = set()
    for line in phony_lines:
        declared.update(line.split())
    for tgt in ALL_TARGETS:
        assert tgt in declared, (
            f"Target {tgt!r} not in .PHONY. Without .PHONY, Make skips the "
            f"recipe if a same-named file exists at the repo root (e.g., a "
            f"future `test` directory would shadow `make test`)."
        )


def test_default_goal_is_help():
    text = _content()
    m = re.search(r"^\.DEFAULT_GOAL\s*:=\s*(\S+)", text, re.M)
    assert m, (
        ".DEFAULT_GOAL not set. Bare `make` should print the help menu "
        "rather than running the first declared target."
    )
    assert m.group(1) == "help", (
        f".DEFAULT_GOAL = {m.group(1)!r}; expected 'help' so a contributor "
        f"running `make` with no args sees the menu."
    )


def test_help_target_exists():
    """`.DEFAULT_GOAL := help` is dead-letter without a `help:` recipe."""
    text = _content()
    assert re.search(r"^help:", text, re.M), (
        "`help:` recipe missing despite .DEFAULT_GOAL := help. "
        "`make` would fail with 'No rule to make target help'."
    )


def test_recipe_lines_tab_indented():
    """Make requires TAB for recipe lines; spaces silently break.

    Walks the file line-by-line tracking whether we're inside a recipe
    block. Any indented line inside a recipe must start with a literal
    tab. An editor-on-save expand-tab is the realistic regression vector.

    Special-target lines (`.PHONY:`, `.DEFAULT_GOAL :=`, `.SUFFIXES:`)
    are explicitly excluded from "starts a recipe" classification —
    they're declarations, not rules with bodies. Without this exclusion,
    the parser would mark `.PHONY:` as opening a recipe; the next
    indented line then trips a false alarm.
    """
    # Fold backslash-continuations into single logical lines so a
    # multi-line `.PHONY:` (or any other declaration) doesn't confuse
    # the in_recipe state machine. Mirrors `_content_logical_lines()`.
    raw = re.sub(r"\\\n", " ", MAKEFILE.read_text())
    in_recipe = False
    failures = []
    # GNU Make special target/variable names that look like targets at
    # line-start. None should flip `in_recipe = True`. Includes both
    # documented "Special Built-in Target Names" (`.PHONY`, `.SUFFIXES`,
    # `.DEFAULT`, `.PRECIOUS`, `.INTERMEDIATE`, `.NOTINTERMEDIATE`,
    # `.SECONDARY`, `.SECONDEXPANSION`, `.DELETE_ON_ERROR`, `.IGNORE`,
    # `.LOW_RESOLUTION_TIME`, `.SILENT`, `.EXPORT_ALL_VARIABLES`,
    # `.NOTPARALLEL`, `.ONESHELL`, `.POSIX`) and special VARIABLES that
    # are commonly written `:=` style at column 0 (`.DEFAULT_GOAL`,
    # `.RECIPEPREFIX`). Variables are technically already filtered by
    # the `(?!=)` lookahead in the regex below, but listing them here
    # keeps the intent explicit and the set self-documenting.
    SPECIAL_TARGETS = {
        ".PHONY", ".DEFAULT_GOAL", ".SUFFIXES",
        ".SECONDARY", ".INTERMEDIATE", ".NOTINTERMEDIATE", ".NOTPARALLEL",
        ".SECONDEXPANSION", ".ONESHELL", ".POSIX",
        ".DELETE_ON_ERROR", ".IGNORE", ".SILENT",
        ".PRECIOUS", ".EXPORT_ALL_VARIABLES", ".RECIPEPREFIX",
        ".LOW_RESOLUTION_TIME", ".DEFAULT",
    }
    for n, line in enumerate(raw.splitlines(), start=1):
        # Target rule starts a recipe: `name:` or `name: deps`.
        # Variable assignments (`X := y`, `X = y`, `X ?= y`) DON'T.
        # Disambiguate by checking the char immediately after the colon
        # — a target rule has space/end-of-line; an `:=` does not.
        target_match = re.match(
            r"^([A-Za-z_./][A-Za-z0-9_./-]*)\s*:(?!=)(\s|$)",
            line,
        )
        if target_match and target_match.group(1) not in SPECIAL_TARGETS:
            in_recipe = True
            continue
        # Blank line ends a recipe block.
        if line.strip() == "":
            in_recipe = False
            continue
        # Non-indented line (variable assignment, comment, directive)
        # ends a recipe block. Comments at column 0 are common.
        if in_recipe and line[:1] not in (" ", "\t"):
            in_recipe = False
            continue
        # Inside a recipe: indentation must be a tab.
        if in_recipe and line[:1] == " ":
            failures.append(f"  line {n}: {line!r}")
    assert not failures, (
        "Makefile has space-indented recipe lines; Make requires tab. "
        "Editor-on-save expand-tab is the typical cause.\n"
        + "\n".join(failures)
    )


def _recipe_for(target: str) -> str:
    """Extract the recipe body for a target (the indented lines below it).

    Returns the raw recipe text (including leading tabs/newlines).
    """
    text = _content()
    # Match `target:` (optionally with deps) followed by 1+ indented lines.
    pattern = rf"^{re.escape(target)}:[^\n]*\n((?:[ \t]+[^\n]*\n?)+)"
    m = re.search(pattern, text, re.M)
    assert m, f"Could not locate recipe for `{target}:`"
    return m.group(1)


def test_test_target_matches_ci_blocking_filter():
    """`make test` must use the same -m filter as CI blocking step.

    .github/workflows/test.yml blocks on `pytest tests/ -m "not fragile"`.
    Drift between local `make test` and CI means a green local build
    can still fail CI.
    """
    recipe = _recipe_for("test")
    assert "pytest" in recipe, "`make test` recipe must invoke pytest."
    assert "tests/" in recipe or "tests " in recipe, (
        "`make test` recipe must target tests/ (CI does)."
    )
    assert '-m "not fragile"' in recipe or "-m 'not fragile'" in recipe, (
        f"`make test` recipe missing `-m \"not fragile\"`. CI blocking step "
        f"in .github/workflows/test.yml uses this filter; drift breaks the "
        f"local-CI symmetry. Recipe was: {recipe!r}"
    )


def test_makefile_ci_symmetry_via_pyproject_addopts():
    """Guards the Makefile<->CI exit-code symmetry contract.

    `make test` runs `pytest tests/ -m "not fragile"` (no other flags).
    CI runs `pytest tests/ -m "not fragile" -v --tb=short --ignore=venv`.
    They produce equivalent exit codes today only because pyproject's
    `[tool.pytest.ini_options].addopts` injects `--ignore=venv` into
    `make test` automatically — if pyproject drops it, `make test`
    starts collecting the venv (if any) and may fail while CI passes.

    Scope is deliberately narrow vs `tests/test_pyproject.py::test_pyproject_pytest_config_ported_from_pytest_ini`
    (which asserts ALL three CI-explicit flags are in addopts as a
    Bit 1.1 invariant). This test is the Bit 1.2 contract: ONLY
    `--ignore=venv` affects exit-code divergence; `-v` and
    `--tb=short` are output-formatting flags that diverge legibly
    without breaking `make test`'s green/red status. Keeping the
    scope tight here prevents this test from blocking a future
    legitimate addopts edit (e.g., dropping `-v` for less-noisy CI
    runs) — the Bit 1.1 test would catch that anyway, and this
    test stays focused on the symmetry-of-correctness invariant.

    Sibling-pair note: `testpaths = ['.']` in pyproject means a future
    edit that drops the explicit `tests/` arg from the `make test`
    recipe would silently expand collection to the whole repo
    (snapshot DBs, scripts/, etc.) — `test_test_target_matches_ci_blocking_filter`
    is the dedicated guard that pins the recipe's `tests/` arg, so the
    pair (this test + that test) together enforce CI symmetry.
    """
    if not MAKEFILE.exists():
        pytest.skip("Makefile missing; covered by test_makefile_exists.")
    try:
        import tomllib as _toml  # type: ignore[import]
    except ModuleNotFoundError:
        try:
            import tomli as _toml  # type: ignore[import]
        except ModuleNotFoundError:
            pytest.skip("tomli/tomllib not installed.")
    pyproject_path = REPO_ROOT / "pyproject.toml"
    with pyproject_path.open("rb") as f:
        data = _toml.load(f)
    addopts = (
        data.get("tool", {}).get("pytest", {}).get("ini_options", {}).get("addopts", [])
    )
    if isinstance(addopts, str):
        addopts = addopts.split()
    # Single-element loop is intentional — see docstring. Future flags
    # that are correctness-critical (not just output-formatting) go
    # here; output-only flags belong in the Bit 1.1 test.
    for flag in ("--ignore=venv",):
        assert flag in addopts, (
            f"pyproject addopts no longer contains {flag!r}. CI "
            f"(.github/workflows/test.yml) passes it explicitly; `make "
            f"test` recipe relies on pyproject to inject it. Either "
            f"restore it to addopts OR pin it directly in the Makefile "
            f"`test:` recipe."
        )


def test_ast_check_targets_bot_py():
    recipe = _recipe_for("ast-check")
    assert "ast.parse" in recipe, (
        "ast-check recipe must call `ast.parse(...)` to syntax-check."
    )
    assert "bot.py" in recipe, (
        "ast-check recipe must target bot.py per CLAUDE.md sacred-file rule."
    )


def test_lint_target_runs_ruff():
    """lint must invoke `ruff check`, either directly or via a Make variable.

    Variable indirection (`$(RUFF) check .`) is fine as long as the
    variable resolves to something with `ruff` in it.
    """
    recipe = _recipe_for("lint")
    assert "check" in recipe, "lint recipe must run a `check` subcommand."

    text = _content()
    if "ruff" in recipe.lower():
        return  # Direct invocation — done.
    # Otherwise, the recipe should use a variable that resolves to ruff.
    # Find variable references in the recipe and confirm at least one is
    # bound to a value containing "ruff" elsewhere in the file.
    var_refs = re.findall(r"\$\(([A-Za-z_][A-Za-z0-9_]*)\)", recipe)
    resolved_to_ruff = False
    for var in var_refs:
        # Match `VAR := ...`, `VAR = ...`, `VAR ?= ...`.
        m = re.search(rf"^{re.escape(var)}\s*[:?]?=\s*(.+)$", text, re.M)
        if m and "ruff" in m.group(1).lower():
            resolved_to_ruff = True
            break
    assert resolved_to_ruff, (
        f"lint recipe doesn't reference ruff directly or via a Make "
        f"variable that resolves to ruff. Recipe: {recipe!r}"
    )


def test_deploy_check_routes_through_existing_script():
    recipe = _recipe_for("deploy-check")
    expected = "scripts/cal_mlp/deploy_check.sh"
    assert expected in recipe, (
        f"deploy-check must call {expected!r} (the canonical pre-deploy "
        f"aggregator), not reimplement gates inline. Recipe was: {recipe!r}"
    )
    assert (REPO_ROOT / expected).exists(), (
        f"{expected} referenced by Makefile but doesn't exist on disk. "
        f"Either ship the script or update the Makefile."
    )


def test_doc_drift_routes_through_existing_script():
    recipe = _recipe_for("doc-drift")
    expected = "scripts/doc_drift_check.py"
    assert expected in recipe, (
        f"doc-drift must call {expected!r}, not reimplement drift checking."
    )
    assert (REPO_ROOT / expected).exists(), (
        f"{expected} referenced by Makefile but doesn't exist on disk."
    )


def test_test_fast_invokes_invariant_tests():
    """test-fast must run dev-tooling invariant suites (sub-second).

    Curated explicit list rather than `-m smoke` because the smoke
    marker has zero usages today (`grep -rn '@pytest.mark.smoke' tests/`).
    """
    recipe = _recipe_for("test-fast")
    assert "pytest" in recipe, "test-fast must invoke pytest."
    # At least the meta-invariant trio should be in the list.
    for fragment in ("test_pyproject.py", "test_repo_hygiene.py", "test_makefile.py"):
        assert fragment in recipe, (
            f"test-fast recipe missing {fragment!r}. The dev-tooling "
            f"invariant suite is the trio that catches packaging/Makefile/"
            f"hygiene regressions."
        )


def test_cwd_guard_fires_when_invoked_outside_repo_root():
    """Operator-facing footgun: `cd subdir/ && make test` would
    silently look up `bot.py` and `scripts/` against the wrong dir.
    The Makefile's `ifeq ($(wildcard pyproject.toml),)` guard fails
    fast at parse time with a clear message. This test pins that
    behavior so a future Bit can't remove the guard silently.
    """
    make = shutil.which("make")
    if make is None:
        pytest.skip("make not installed.")
    if not MAKEFILE.exists():
        pytest.skip("Makefile missing; covered by test_makefile_exists.")
    # Use a temp dir that definitely has no pyproject.toml. `-f` lets
    # Make load OUR Makefile from any cwd; the guard reads `$(CURDIR)`
    # via `wildcard pyproject.toml`, so it should still trip.
    with tempfile.TemporaryDirectory() as td:
        result = subprocess.run(
            [make, "-f", str(MAKEFILE), "help"],
            cwd=td,
            capture_output=True,
            text=True,
            timeout=10,
        )
    assert result.returncode != 0, (
        f"CWD guard didn't fire — `make help` from {td!r} succeeded "
        f"(exit 0). The Makefile must abort when invoked outside the "
        f"repo root. stdout={result.stdout[:200]!r}"
    )
    # Stderr should mention pyproject.toml so the operator gets a
    # remedy, not just an opaque error.
    combined = (result.stdout + result.stderr).lower()
    assert "pyproject.toml" in combined or "repo root" in combined, (
        f"CWD guard fired but message is unhelpful — operator can't "
        f"tell what to do. stderr={result.stderr[:300]!r}"
    )


def test_dry_run_each_target_clean():
    """Functional check: `make -n <target>` parses and prints commands.

    Catches Makefile *syntax* errors and parse-time variable expansion
    failures (dangling backslash, undefined `$(VAR)` in a recipe, bad
    `$(shell ...)` form). DOES NOT catch recipe-content errors — Make
    doesn't shell-eval the printed commands, so a typo'd script path
    or a missing dot in `bot.py` would not trip this test. Existence
    checks for referenced files live in dedicated tests.
    """
    make = shutil.which("make")
    if make is None:
        pytest.skip("make not installed.")
    for tgt in ("help",) + ALL_TARGETS:
        result = subprocess.run(
            [make, "-n", tgt],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"`make -n {tgt}` failed (exit {result.returncode}). "
            f"stdout={result.stdout[:300]!r} stderr={result.stderr[:300]!r}"
        )
        assert result.stdout.strip(), (
            f"`make -n {tgt}` produced empty output — recipe is empty?"
        )


def test_make_aliases_in_tracked_docs_resolve_to_real_targets():
    """Tracked docs (CLAUDE.md, agent_docs/) reference `make <name>`
    aliases for legacy commands. If a future Bit renames a target, the
    docs go stale silently — `scripts/doc_drift_check.py` knows about
    config-value drift, not Makefile target names.

    This test scans tracked docs for `make <name>` patterns and asserts
    each `<name>` resolves to a real target in the Makefile.

    Limited to a curated allow-list of docs to avoid false positives
    from incidental occurrences ("make sure", "make a backup") and
    from third-party README content.
    """
    if not MAKEFILE.exists():
        # `test_makefile_exists` is the dedicated canary for this case;
        # skip here so a single missing-Makefile failure doesn't
        # cascade into a noisy stack trace from `MAKEFILE.read_text()`.
        pytest.skip("Makefile missing; covered by test_makefile_exists.")
    text = _content()
    # Real targets — use the parser invariant (start of line, colon, no `=`).
    real_targets = set(re.findall(r"^([A-Za-z_][A-Za-z0-9_-]*):(?!=)", text, re.M))

    docs_to_scan = [
        REPO_ROOT / "CLAUDE.md",
        REPO_ROOT / "agent_docs" / "config_reference.md",
    ]
    # Tokens after `make ` that are NOT real Make targets — flag-style
    # words a contributor might use in prose. Avoid false positives by
    # matching a strict identifier shape (lowercase + hyphen only) and
    # by REQUIRING a context that looks like a CLI invocation
    # (backticks or end-of-token bound).
    pattern = re.compile(r"`make\s+([a-z][a-z0-9-]*)`")
    failures = []
    for doc in docs_to_scan:
        if not doc.exists():
            continue
        content = doc.read_text()
        for tgt in pattern.findall(content):
            if tgt not in real_targets:
                failures.append(
                    f"  {doc.relative_to(REPO_ROOT)}: references `make {tgt}` "
                    f"but no `{tgt}:` target exists in Makefile."
                )
    assert not failures, (
        "Tracked docs reference Makefile targets that don't exist:\n"
        + "\n".join(failures)
    )


def test_help_lists_all_targets():
    """Help recipe must mention every shipped target — REQUIRED + OPTIONAL.

    Word-boundary match (`make <tgt>\\b`), NOT substring containment. The
    naive `tgt in help_body` check false-passes for `tgt='test'` because
    the help body always contains `'make test-fast'` and `'tests'` —
    so a renamed `test:` target with no help line would slip through.
    """
    text = _content()
    m = re.search(r"^help:[^\n]*\n((?:[ \t]+[^\n]*\n?)+)", text, re.M)
    assert m, "help: recipe not found."
    help_body = m.group(1)
    for tgt in ALL_TARGETS:
        # `make <tgt>` followed by whitespace/end-of-line. Hyphens are
        # not regex word-boundary chars on the right side, so we match
        # whitespace explicitly.
        assert re.search(rf"make\s+{re.escape(tgt)}(?:\s|$)", help_body, re.M), (
            f"help: recipe doesn't mention `make {tgt}`. A contributor "
            f"running `make` would not discover this target."
        )
