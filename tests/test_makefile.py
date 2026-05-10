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
- `ast-check` covers `bot/_impl.py` (CLAUDE.md sacred-file rule).
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
#
# Pillar 5 (ticket 86b9ve11y) added the tiered-suite + testmon +
# mutmut targets. They're listed here so the existing .PHONY /
# dry-run / help-listing invariants exercise them uniformly with the
# Bit 1.2 set; tier-specific contracts (e.g., test-affected uses
# testmon) live in dedicated Pillar 5 tests below.
PILLAR_5_TARGETS = (
    "test-unit",
    "test-contract",
    "test-contract-pytest",
    "test-contract-lint",
    "test-equivalence",
    "test-integration",
    "test-affected",
    "test-changed",
    "test-mutmut",
)
OPTIONAL_TARGETS = ("install", "api-snapshot-regen") + PILLAR_5_TARGETS
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


def test_test_target_chains_tiered_targets():
    """`make test` must orchestrate the four Pillar 5 tiers in order.

    Pillar 5 (86b9ve11y) split the historical one-shot `pytest tests/`
    into four tiers (unit < contract < equivalence < integration). The
    `test:` recipe now chains them via `$(MAKE) test-<tier>` so each
    tier's failure aborts the next via Make's default fail-on-nonzero.
    The CI symmetry that Bit 1.2 pinned (filter alignment with
    .github/workflows/test.yml) now holds at the tier level — see
    `test_test_integration_matches_ci_blocking_filter` for the
    integration-tier pin and `test_pillar_5_workflow_calls_tier_targets`
    for CI's parallel obligation.
    """
    recipe = _recipe_for("test")
    for tier in ("test-unit", "test-contract", "test-equivalence", "test-integration"):
        assert tier in recipe, (
            f"`make test` recipe missing `{tier}` invocation. Pillar 5 "
            f"requires all four tiers to chain in order. Recipe was: {recipe!r}"
        )
    # Ordering matters — fail-fast on the cheapest tier first. Match the
    # tier names in the order they should appear; if any pair is
    # transposed, the find-then-find-after pattern will catch it.
    cursor = 0
    for tier in ("test-unit", "test-contract", "test-equivalence", "test-integration"):
        idx = recipe.find(tier, cursor)
        assert idx >= 0, (
            f"`make test` chains tiers out of order — {tier!r} not found "
            f"after position {cursor}. Order should be unit → contract → "
            f"equivalence → integration so the cheapest tier fails fastest."
        )
        cursor = idx + len(tier)


def test_test_integration_matches_ci_blocking_filter():
    """test-integration must use the same -m filter as CI's broad pytest step.

    With Pillar 5, the historical `pytest tests/ -m "not fragile"` lives
    in test-integration (the catch-all tier). CI's broad pytest step in
    .github/workflows/test.yml + deploy.yml mirrors this — drift breaks
    the local-CI symmetry the Bit 1.2 contract guarded.
    """
    recipe = _recipe_for("test-integration")
    assert "pytest" in recipe, "`make test-integration` recipe must invoke pytest."
    assert "tests/" in recipe or "tests " in recipe, (
        "`make test-integration` recipe must target tests/ (CI does)."
    )
    assert '-m "not fragile"' in recipe or "-m 'not fragile'" in recipe, (
        f"`make test-integration` recipe missing `-m \"not fragile\"`. "
        f"CI's broad pytest step in .github/workflows/test.yml uses this "
        f"filter; drift breaks the local-CI symmetry. Recipe was: {recipe!r}"
    )


def test_makefile_ci_symmetry_via_pyproject_addopts():
    """Guards the Makefile<->CI exit-code symmetry contract.

    Pillar 5 (86b9ve11y) split the historical one-shot pytest
    invocation across four tier targets (test-unit, test-contract,
    test-equivalence, test-integration). None of the tier recipes
    pass `--ignore=venv` explicitly — they all rely on pyproject's
    `[tool.pytest.ini_options].addopts` to inject it.

    CI runs `pytest tests/ -m "not fragile" -v --tb=short --ignore=venv`
    in the broad integration step. The four tier recipes produce
    equivalent exit codes only because `--ignore=venv` is injected
    via addopts — if pyproject drops it, every tier (including
    test-affected's testmon run) may start collecting the venv (if
    any) and surface false failures while CI passes.

    Scope is deliberately narrow vs `tests/test_pyproject.py::test_pyproject_pytest_config_ported_from_pytest_ini`
    (which asserts ALL three CI-explicit flags are in addopts as a
    Bit 1.1 invariant). This test is the Bit 1.2 + Pillar 5 contract:
    ONLY `--ignore=venv` affects exit-code divergence; `-v` and
    `--tb=short` are output-formatting flags that diverge legibly
    without breaking the tier targets' green/red status.

    Sibling-pair note: `testpaths = ['.']` in pyproject means a future
    edit that drops the explicit `tests/` arg from any tier recipe
    would silently expand collection to the whole repo (snapshot DBs,
    scripts/, etc.) — `test_test_integration_matches_ci_blocking_filter`
    pins the integration tier's `tests/` arg, so the pair (this test +
    that test) together enforce CI symmetry.
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


def test_ast_check_targets_bot_impl():
    recipe = _recipe_for("ast-check")
    assert "ast.parse" in recipe, (
        "ast-check recipe must call `ast.parse(...)` to syntax-check."
    )
    assert "bot/_impl.py" in recipe, (
        "ast-check recipe must target bot/_impl.py per CLAUDE.md sacred-file rule."
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


def _unit_tier_file_list() -> str:
    """Return the concatenation of test-unit's recipe body + UNIT_FILES
    variable body, so substring checks for specific test files work
    regardless of whether the file is hardcoded in the recipe or moved
    out into the Make variable.

    Pillar 5 (86b9ve11y) introduced the UNIT_FILES variable so the
    integration tier's `--ignore=...` list could be auto-derived (single
    source of truth). The trade-off: a recipe like `pytest $(UNIT_FILES)`
    no longer mentions specific test files inline, so substring checks
    that target the recipe body alone would false-fail. Sibling tests in
    test_agents_md_symlink.py / test_claude_md_size.py / etc. use the
    same `combined = recipe + var-body` pattern.
    """
    text = _content()
    folded = _content_logical_lines()
    recipe_match = re.search(
        r"^test-unit:[^\n]*\n((?:\t.*\n?)+)",
        folded,
        re.M,
    )
    recipe = recipe_match.group(1) if recipe_match else ""
    # UNIT_FILES variable definition. May span multiple physical lines
    # via `\\\n` continuation; folded form puts it on one line.
    var_match = re.search(
        r"^UNIT_FILES\s*[:?]?=\s*([^\n]+)$",
        folded,
        re.M,
    )
    var_body = var_match.group(1) if var_match else ""
    return recipe + "\n" + var_body


def test_test_unit_invokes_invariant_tests():
    """test-unit must run dev-tooling invariant suites (sub-second).

    Pillar 5 (86b9ve11y) renamed the curated invariant tier from
    `test-fast` to `test-unit` to fit the four-tier scheme
    (unit/contract/equivalence/integration). The Bit 1.2 trio
    (test_pyproject.py, test_repo_hygiene.py, test_makefile.py) is
    still the load-bearing sub-second invariant — keep them in this
    tier as the hygiene-of-hygiene canary.

    The trio may live inline in the recipe OR in the UNIT_FILES
    variable that the recipe references; both forms are valid.
    """
    recipe = _recipe_for("test-unit")
    assert "pytest" in recipe, "test-unit must invoke pytest."
    combined = _unit_tier_file_list()
    # At least the meta-invariant trio should be in the list.
    for fragment in ("test_pyproject.py", "test_repo_hygiene.py", "test_makefile.py"):
        assert fragment in combined, (
            f"test-unit recipe + UNIT_FILES variable missing {fragment!r}. "
            f"The dev-tooling invariant suite is the trio that catches "
            f"packaging/Makefile/hygiene regressions. Pillar 5 expects "
            f"this trio under the unit tier (Bit 1.2 had it under test-fast; "
            f"test-fast is now a backward-compat alias for test-unit)."
        )


def test_test_fast_aliases_test_unit():
    """test-fast preserves Bit 1.2 muscle memory by aliasing test-unit.

    Pillar 5 (86b9ve11y) split the test suite into four tiers; the Bit
    1.2 `test-fast` target's role (curated invariant set) maps cleanly
    onto the new `test-unit` tier. Rather than break every doc / hook
    that calls `make test-fast`, keep the name as a prerequisite-only
    alias whose recipe is empty.

    Two valid shapes pass this test:
      (a) `test-fast: test-unit` with no recipe (current shape)
      (b) `test-fast:` with a recipe that mirrors test-unit's content
    """
    text = _content()
    # Shape (a): prereq-only alias — `test-fast: test-unit` with the
    # next non-blank line either being a non-indented declaration
    # (target / variable / blank) or end-of-file. Detect by matching
    # the dependency list.
    m = re.search(r"^test-fast:\s*([^\n]*)$", text, re.M)
    assert m, "test-fast target missing."
    deps = m.group(1).strip().split()
    if "test-unit" in deps:
        return  # Shape (a) — alias via prereq.
    # Shape (b): inline recipe. Fall through to the trio assertion.
    recipe = _recipe_for("test-fast")
    for fragment in ("test_pyproject.py", "test_repo_hygiene.py", "test_makefile.py"):
        assert fragment in recipe, (
            f"test-fast is neither aliased to test-unit nor inlines the "
            f"invariant trio. Pillar 5 expects either shape; got "
            f"deps={deps!r}, recipe={recipe!r}."
        )


def test_test_contract_invokes_pytest_and_lint_imports():
    """test-contract must gate both the contract-tier pytest selection
    AND the Pillar 2 import-linter CLI.

    The contract tier's two halves:
      1. Pytest suite — public_api snapshot (Pillar 1), AST guards,
         extraction tests under tests/contracts/ + top-level test_*.py.
      2. Pillar 2 layering contracts via `lint-imports` (or the
         `$(LINT_IMPORTS)` Make-variable resolution form for macOS
         user-site installs).

    Two valid shapes pass this test:
      (a) Monolithic: `test-contract` recipe directly invokes both
          pytest and lint-imports (pre-86b9vgh3t shape).
      (b) Split (post-86b9vgh3t): `test-contract` orchestrates
          `test-contract-pytest` (recipe contains pytest + $(CONTRACT_FILES))
          and `test-contract-lint` (recipe contains lint-imports /
          $(LINT_IMPORTS)). The orchestrator chains both via `$(MAKE)`
          so a failure in either half aborts the next via Make's
          fail-on-nonzero. CI splits these into two distinct steps so
          a red status unambiguously points at one half (see
          test_pillar_5_workflow_calls_contract_split).
    """
    recipe = _recipe_for("test-contract")
    # Detect shape: split has `$(MAKE) test-contract-pytest` chaining.
    if "test-contract-pytest" in recipe:
        # Shape (b) — split. Validate both child recipes carry the
        # load-bearing invocations.
        assert "test-contract-lint" in recipe, (
            "Split test-contract orchestrator references "
            "test-contract-pytest but not test-contract-lint. Both "
            "halves must chain for the tier to gate completely."
        )
        pytest_recipe = _recipe_for("test-contract-pytest")
        assert "pytest" in pytest_recipe, (
            f"test-contract-pytest recipe must invoke pytest. "
            f"Recipe was: {pytest_recipe!r}"
        )
        assert "$(CONTRACT_FILES)" in pytest_recipe or "tests/contracts" in pytest_recipe, (
            f"test-contract-pytest recipe must select the contract-tier "
            f"pytest files. Recipe was: {pytest_recipe!r}"
        )
        lint_recipe = _recipe_for("test-contract-lint")
        assert "lint-imports" in lint_recipe or "LINT_IMPORTS" in lint_recipe, (
            f"test-contract-lint recipe must invoke lint-imports. "
            f"Recipe was: {lint_recipe!r}"
        )
        return
    # Shape (a) — monolithic.
    assert "pytest" in recipe, "test-contract recipe must invoke pytest."
    assert "$(CONTRACT_FILES)" in recipe or "tests/contracts" in recipe, (
        f"test-contract recipe must select the contract-tier pytest "
        f"files (via $(CONTRACT_FILES) or explicit tests/contracts path). "
        f"Recipe was: {recipe!r}"
    )
    # `lint-imports` direct OR via $(LINT_IMPORTS) variable indirection
    # (the macOS user-site fallback).
    assert "lint-imports" in recipe or "LINT_IMPORTS" in recipe, (
        f"test-contract recipe must invoke lint-imports (Pillar 2). "
        f"Recipe was: {recipe!r}"
    )


def test_test_contract_pytest_and_lint_split_targets_exist():
    """Ticket 86b9vgh3t: split CI contract step into pytest + lint halves.

    The two halves were originally chained inside a single
    `test-contract` recipe; this conflated CI failure attribution
    (operator couldn't tell pytest vs lint-imports failure at a glance).
    After 86b9vgh3t, the Makefile ships THREE targets:
      * test-contract-pytest — pytest -m "not fragile" $(CONTRACT_FILES)
      * test-contract-lint   — $(LINT_IMPORTS)
      * test-contract        — orchestrator that chains both via $(MAKE)

    CI workflows wire `Contract tier — pytest` and `Contract tier —
    import-linter` as separate steps so red status points at the
    failing half.
    """
    text = _content()
    for tgt, must_contain in (
        ("test-contract-pytest", ("pytest", "$(CONTRACT_FILES)")),
        ("test-contract-lint", ("lint-imports",)),  # OR LINT_IMPORTS
    ):
        assert re.search(rf"^{re.escape(tgt)}:(?!=)", text, re.M), (
            f"Missing Makefile target {tgt!r} (ticket 86b9vgh3t — split "
            f"contract step). Expected the recipe to wrap "
            f"{' / '.join(must_contain)!r}."
        )
        recipe = _recipe_for(tgt)
        if tgt == "test-contract-lint":
            assert "lint-imports" in recipe or "LINT_IMPORTS" in recipe, (
                f"{tgt} recipe doesn't invoke lint-imports. Recipe: {recipe!r}"
            )
        else:
            for token in must_contain:
                assert token in recipe, (
                    f"{tgt} recipe missing {token!r}. Recipe: {recipe!r}"
                )
    # Orchestrator must chain both via $(MAKE) so each failure aborts
    # the next (Make's default fail-on-nonzero). Inline `$(LINT_IMPORTS)`
    # in the same recipe would re-monolithize.
    orchestrator = _recipe_for("test-contract")
    assert "test-contract-pytest" in orchestrator, (
        "test-contract orchestrator must chain test-contract-pytest. "
        f"Recipe was: {orchestrator!r}"
    )
    assert "test-contract-lint" in orchestrator, (
        "test-contract orchestrator must chain test-contract-lint. "
        f"Recipe was: {orchestrator!r}"
    )


@pytest.mark.parametrize("wf_name", ["test.yml", "deploy.yml"])
def test_pillar_5_workflow_calls_contract_split(wf_name):
    """Ticket 86b9vgh3t: both CI workflows must surface the contract
    split as TWO distinct steps so a red check unambiguously identifies
    pytest-half vs import-linter-half.

    Expected steps (names match the strings the operator sees in the
    GitHub Actions UI):
      * `Contract tier — pytest` running `make test-contract-pytest`
      * `Contract tier — import-linter` running `make test-contract-lint`

    The em-dash (U+2014) is the canonical separator used by sibling
    tier step names ("Unit tier (Pillar 5)" vs "Contract tier —
    pytest") — accept either em-dash or ASCII `-` for resilience.
    """
    wf_path = REPO_ROOT / ".github" / "workflows" / wf_name
    assert wf_path.exists(), f"{wf_name} missing at expected path."
    text = wf_path.read_text()
    # Pytest half — name + run line within the same step block.
    # Use a small "name then run" window match so a typo'd `run` on
    # an unrelated step can't false-pass.
    pytest_step = re.search(
        r"- name:\s*Contract tier[^\n]*pytest[^\n]*\n(?:[^\n]*\n){0,6}?\s*run:\s*make\s+test-contract-pytest(?![\w-])",
        text,
    )
    assert pytest_step, (
        f"{wf_name} missing `Contract tier — pytest` step with "
        f"`run: make test-contract-pytest`. Ticket 86b9vgh3t requires "
        f"two distinct contract steps so red status unambiguously "
        f"points at pytest-half vs import-linter-half."
    )
    lint_step = re.search(
        r"- name:\s*Contract tier[^\n]*(?:import-linter|lint)[^\n]*\n(?:[^\n]*\n){0,6}?\s*run:\s*make\s+test-contract-lint(?![\w-])",
        text,
    )
    assert lint_step, (
        f"{wf_name} missing `Contract tier — import-linter` step with "
        f"`run: make test-contract-lint`. Ticket 86b9vgh3t requires "
        f"the lint half to live in its own CI step."
    )


def test_test_equivalence_invokes_pytest_on_equivalence_dir():
    """test-equivalence must run pytest against tests/equivalence/.

    Pillar 3 (86b9ve0zu) ships the equivalence harness; Pillar 5
    promotes it to its own tier so CI can gate it ahead of the
    broader integration suite.
    """
    recipe = _recipe_for("test-equivalence")
    assert "pytest" in recipe, "test-equivalence recipe must invoke pytest."
    assert "tests/equivalence" in recipe, (
        f"test-equivalence recipe must target tests/equivalence/. "
        f"Recipe was: {recipe!r}"
    )


def test_test_integration_ignores_other_tiers():
    """test-integration must NOT re-run tests already covered by
    earlier tiers — that's the whole point of tiering.

    The recipe should ignore tests/equivalence (Pillar 3) at minimum;
    the unit + contract file lists are referenced via INTEGRATION_IGNORES
    (or equivalent) so adding a file to a tier auto-removes it from
    integration.
    """
    recipe = _recipe_for("test-integration")
    assert "tests/equivalence" in recipe or "INTEGRATION_IGNORES" in recipe, (
        f"test-integration recipe doesn't ignore tests/equivalence. "
        f"Either pass `--ignore=tests/equivalence` directly or include it "
        f"in $(INTEGRATION_IGNORES). Recipe was: {recipe!r}"
    )
    # If using a Make variable, sanity-check it resolves to the unit +
    # contract file lists. Substring check is sufficient — the integrity
    # of the variable expansion is exercised by `make -n test-integration`
    # in test_dry_run_each_target_clean.
    if "INTEGRATION_IGNORES" in recipe:
        text = _content_logical_lines()
        m = re.search(r"^INTEGRATION_IGNORES\s*[:?]?=\s*(.+?)(?=^[A-Za-z_.])", text, re.M | re.S)
        assert m, "$(INTEGRATION_IGNORES) referenced but not defined."
        ignores_body = m.group(1)
        for must_ignore in ("UNIT_FILES", "CONTRACT_FILES", "tests/equivalence"):
            assert must_ignore in ignores_body, (
                f"$(INTEGRATION_IGNORES) doesn't include {must_ignore!r}. "
                f"Body was: {ignores_body!r}"
            )


def test_test_affected_uses_testmon():
    """test-affected must invoke testmon for incremental selection.

    Pillar 5's testmon-driven incremental tier — the agent loop
    optimization. `--testmon` is the pytest-testmon plugin flag.
    """
    recipe = _recipe_for("test-affected")
    assert "--testmon" in recipe, (
        f"test-affected recipe must pass `--testmon` to pytest. "
        f"Recipe was: {recipe!r}"
    )


def test_test_changed_aliases_test_affected():
    """test-changed is the user-facing name for test-affected (Pillar 5
    remote-control spec) — both should resolve to the same testmon
    invocation.

    Either shape is valid:
      (a) `test-changed: test-affected` (prereq alias, no recipe)
      (b) `test-changed:` with a recipe that also passes --testmon
    """
    text = _content()
    m = re.search(r"^test-changed:\s*([^\n]*)$", text, re.M)
    assert m, "test-changed target missing."
    deps = m.group(1).strip().split()
    if "test-affected" in deps:
        return  # Shape (a).
    recipe = _recipe_for("test-changed")
    assert "--testmon" in recipe, (
        f"test-changed is neither aliased to test-affected nor passes "
        f"--testmon directly. deps={deps!r}, recipe={recipe!r}."
    )


def test_test_mutmut_invokes_mutmut_run():
    """test-mutmut must invoke `mutmut run` to read [tool.mutmut] and
    execute the mutation baseline.

    Per ticket 86b9ve11y AC: targets bot/engines/{volatility,probability}.py;
    that's pinned in pyproject [tool.mutmut].paths_to_mutate (asserted by
    `tests/test_pyproject.py::test_pyproject_mutmut_targets_engines`),
    not the Makefile recipe. The Makefile's job is just to surface the
    entrypoint.
    """
    recipe = _recipe_for("test-mutmut")
    assert "mutmut run" in recipe or "mutmut\trun" in recipe, (
        f"test-mutmut recipe must invoke `mutmut run`. Recipe was: {recipe!r}"
    )


@pytest.mark.parametrize(
    "wf_name,blocking_integration",
    [("test.yml", False), ("deploy.yml", True)],
)
def test_pillar_5_workflow_calls_tier_targets(wf_name, blocking_integration):
    """Both CI workflows must invoke each Pillar 5 tier target.

    R2 followup: closes the symmetry gap between the Makefile
    (single-source-of-truth for tier definitions) and the CI workflows
    (which call into the Makefile). Without this, a future workflow
    edit could silently drop a tier (e.g., remove `make test-equivalence`
    on a perceived "redundant" cleanup) and the suite would no longer
    gate that tier in CI even though `make test` still does locally.

    Per ticket 86b9ve11y AC: blocking = unit + contract + equivalence;
    integration = informational on test.yml, BLOCKING on deploy.yml
    (deploys are the higher-stakes gate). The `blocking_integration`
    parameter encodes this asymmetry — for test.yml the integration
    step must include `continue-on-error: true`; for deploy.yml it
    must NOT.
    """
    wf_path = REPO_ROOT / ".github" / "workflows" / wf_name
    assert wf_path.exists(), f"{wf_name} missing at expected path."
    text = wf_path.read_text()
    # Each tier must appear as `run: make test-<tier>` somewhere in
    # the workflow body. The trailing `(?![\w-])` (negative lookahead
    # for word-or-hyphen) is load-bearing: `\b` would treat the
    # letter→hyphen boundary as a word boundary, so a typo like
    # `run: make test-unit-extra` would falsely satisfy the
    # `test-unit\b` pattern while NOT actually invoking the tier
    # (R3 MAJOR fix). The negative lookahead requires the match end
    # at a non-identifier character — whitespace, end-of-line, or
    # punctuation.
    #
    # Ticket 86b9vgh3t: `test-contract` is satisfied by EITHER the
    # orchestrator (`run: make test-contract`) OR the split halves
    # (`run: make test-contract-pytest` AND `run: make
    # test-contract-lint`). CI uses the split form so red status
    # unambiguously identifies the failing half; either form
    # preserves the local `make test` orchestration symmetry.
    for tier in ("test-unit", "test-contract", "test-equivalence", "test-integration"):
        direct = re.search(rf"run:\s*make\s+{re.escape(tier)}(?![\w-])", text)
        if direct:
            continue
        # Fall-through only legal for `test-contract` (split form).
        if tier == "test-contract":
            pytest_half = re.search(r"run:\s*make\s+test-contract-pytest(?![\w-])", text)
            lint_half = re.search(r"run:\s*make\s+test-contract-lint(?![\w-])", text)
            if pytest_half and lint_half:
                continue
            assert False, (
                f"{wf_name} missing `run: make test-contract` step AND "
                f"neither the test-contract-pytest+test-contract-lint "
                f"split pair is present. Ticket 86b9vgh3t allows either "
                f"the orchestrator OR the split halves; CI must invoke "
                f"one of those shapes."
            )
        assert False, (
            f"{wf_name} missing `run: make {tier}` step. Pillar 5 "
            f"requires CI to invoke each tier target so the local "
            f"`make test` orchestration matches the CI gate behavior."
        )
    # Asymmetric integration policy. Find the integration step block
    # and inspect its `continue-on-error` setting. Step block ends at
    # the next `- name:` line at THE SAME INDENT LEVEL, the next
    # job-level `<word>:` declaration (e.g. `deploy:` in deploy.yml),
    # OR end of file. Ticket 86b9vggzr: the prior `\n\s*- name:`
    # boundary was too loose — in deploy.yml the next `- name:` is in
    # the SEPARATE `deploy:` job, so the integration-step capture
    # bled across the job boundary. A `continue-on-error: true`
    # placed on the deploy: job (not on the integration step) would
    # then falsely satisfy the PR-gate-informational assertion.
    #
    # Boundaries (any one ends the block):
    #   * `\n      - name:`  — next step at the same step indent
    #     (steps under `jobs.<job>.steps:` are at 6 spaces in this
    #     repo's workflow style: 2 for `jobs:`, 2 for `<job>:`, 2 for
    #     `steps:`).
    #   * `\n  \w+:`         — next job-level key (2-space indent).
    #     The `\w+` is loose by design — captures `deploy:`, `lint:`,
    #     any future sibling job.
    #   * `\Z`               — end of file.
    integration_match = re.search(
        r"(?ms)- name:[^\n]*Integration tier[^\n]*\n(.*?)(?=\n      - name:|\n  \w+:|\Z)",
        text,
    )
    assert integration_match, (
        f"{wf_name} has no `Integration tier` step block. Pillar 5 "
        f"explicit step naming required for the asymmetric "
        f"informational/blocking policy."
    )
    block = integration_match.group(1)
    has_continue_on_error = bool(
        re.search(r"^\s*continue-on-error:\s*true", block, re.M)
    )
    if blocking_integration:
        assert not has_continue_on_error, (
            f"{wf_name}'s Integration tier step has "
            f"`continue-on-error: true` but Pillar 5 spec says the "
            f"deploy gate is BLOCKING — a regression would silently "
            f"deploy. Remove the continue-on-error line."
        )
    else:
        assert has_continue_on_error, (
            f"{wf_name}'s Integration tier step is missing "
            f"`continue-on-error: true`. Pillar 5 spec says the PR "
            f"gate is INFORMATIONAL for integration so flaky integration "
            f"tests don't block every PR. Either add the line or "
            f"document the spec change."
        )


def test_integration_step_regex_rejects_cross_job_continue_on_error():
    """Ticket 86b9vggzr: regression — `continue-on-error: true` placed
    on the deploy: JOB (sibling to test: job) must NOT false-pass as
    if it applied to the integration STEP inside the test: job.

    Before the fix, the boundary `(?=\\n\\s*- name:|\\Z)` would scan
    forward from the integration step in test: through the YAML
    document to find the next `- name:` line. In deploy.yml that next
    `- name:` is `- name: Deploy to VPS` inside the `deploy:` job —
    so the captured "integration step block" actually included
    everything in between: the rest of the test: job's steps, the
    blank line, AND any top-level keys placed on the deploy: job
    (continue-on-error, environment, env, etc.).

    The fix narrows the boundary to:
      * `\\n      - name:` (next step at same indent), OR
      * `\\n  \\w+:` (next job-level declaration at 2-space indent), OR
      * `\\Z` (EOF).

    This regression test builds a synthetic deploy.yml-shaped string
    with the integration step CORRECTLY blocking (no continue-on-error
    inside) but a misplaced `continue-on-error: true` on the deploy:
    job. The tightened boundary must stop the capture at `deploy:`,
    so `has_continue_on_error` for the integration block is False,
    so the blocking-integration assertion succeeds. The loose boundary
    would extend past deploy: and pick up its continue-on-error,
    flipping the assertion to false-pass-as-informational on a
    blocking workflow.
    """
    # Synthetic workflow text that mirrors deploy.yml's two-job
    # layout. The integration step has NO continue-on-error (the
    # blocking-deploy contract). The misplaced flag is on `deploy:`.
    synthetic = (
        "name: Deploy to VPS\n"
        "jobs:\n"
        "  test:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - name: Integration tier (Pillar 5 — BLOCKING on deploy)\n"
        "        run: make test-integration\n"
        "\n"
        "  deploy:\n"
        "    needs: test\n"
        "    continue-on-error: true  # MISPLACED — applies to job, not step\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - name: Deploy to VPS\n"
        "        run: echo deploy\n"
    )
    # Tightened regex (must match the production regex in
    # test_pillar_5_workflow_calls_tier_targets).
    integration_match = re.search(
        r"(?ms)- name:[^\n]*Integration tier[^\n]*\n(.*?)(?=\n      - name:|\n  \w+:|\Z)",
        synthetic,
    )
    assert integration_match, "Tightened regex failed to match the integration step at all."
    block = integration_match.group(1)
    # The block MUST NOT contain `continue-on-error: true` (it lives on
    # the deploy: job, two indent levels out and AFTER the boundary).
    assert "continue-on-error" not in block, (
        f"Integration-step regex bled across job boundary into deploy: — "
        f"captured `continue-on-error` that lives on a sibling job. This "
        f"is the 86b9vggzr false-pass that the tightened boundary "
        f"prevents. Block was: {block!r}"
    )
    # And the loose regex (pre-fix) DOES exhibit the bug — keep this
    # half of the test as a forward-locked witness that the fix is
    # load-bearing. If a future change reverts the boundary, this
    # assertion is what alerts. Without it, a silent revert to the
    # loose pattern would leave only the positive half passing.
    loose_match = re.search(
        r"(?ms)- name:[^\n]*Integration tier[^\n]*\n(.*?)(?=\n\s*- name:|\Z)",
        synthetic,
    )
    assert loose_match, "Sanity: loose regex should still match."
    loose_block = loose_match.group(1)
    assert "continue-on-error" in loose_block, (
        "Loose regex no longer exhibits the cross-job bleed — synthetic "
        "fixture has drifted away from the bug shape it was meant to "
        "demonstrate. If the loose pattern was retired entirely, this "
        "assertion is the canary; refresh the synthetic to a current "
        "false-pass shape OR delete this half of the test."
    )


def test_cwd_guard_fires_when_invoked_outside_repo_root():
    """Operator-facing footgun: `cd subdir/ && make test` would
    silently look up `bot/_impl.py` and `scripts/` against the wrong dir.
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
    or a missing dot in `bot/_impl.py` would not trip this test. Existence
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

    Special-cased: test-contract-pytest + test-contract-lint are
    PLUMBING targets surfaced via the `test-contract` orchestrator —
    operators invoke `make test-contract` and CI splits the two halves
    into distinct steps; neither plumbing target needs a top-level help
    line. test-fast is a back-compat alias and is also exempted.
    """
    text = _content()
    m = re.search(r"^help:[^\n]*\n((?:[ \t]+[^\n]*\n?)+)", text, re.M)
    assert m, "help: recipe not found."
    help_body = m.group(1)
    PLUMBING = {"test-contract-pytest", "test-contract-lint"}
    for tgt in ALL_TARGETS:
        if tgt in PLUMBING:
            continue
        # `make <tgt>` followed by whitespace/end-of-line. Hyphens are
        # not regex word-boundary chars on the right side, so we match
        # whitespace explicitly.
        assert re.search(rf"make\s+{re.escape(tgt)}(?:\s|$)", help_body, re.M), (
            f"help: recipe doesn't mention `make {tgt}`. A contributor "
            f"running `make` would not discover this target."
        )


# ----- Ticket 86b9vgh1a: cross-platform flock-style guard for mutmut -----

MUTMUT_LOCK_SCRIPT = REPO_ROOT / "scripts" / "_mutmut_lock.py"


def test_mutmut_lock_wrapper_script_exists():
    """Ticket 86b9vgh1a: cross-platform mutmut lock wrapper.

    Linux ships `flock(1)` in /usr/bin; macOS does NOT. The dev box
    is macOS, so a Linux-only `flock --nonblock --exclusive` recipe
    would silently no-op (or error opaquely) when an operator runs
    `make test-mutmut` locally. Solution: a Python wrapper using
    `fcntl.flock` — which is in Python's stdlib on both darwin and
    Linux — guards the same coordination primitive.
    """
    assert MUTMUT_LOCK_SCRIPT.exists(), (
        f"Missing {MUTMUT_LOCK_SCRIPT.relative_to(REPO_ROOT)!s}. "
        f"Ticket 86b9vgh1a requires a fcntl-based wrapper to gate "
        f"`make test-mutmut` / test-equivalence / test-integration "
        f"against concurrent invocation that would race against "
        f"mutmut's in-place mutations of bot/engines/."
    )


def test_mutmut_lock_wrapper_uses_fcntl():
    """The wrapper must use fcntl.flock (cross-platform stdlib) — not
    a wrapper around the Linux-only `flock(1)` binary.

    Substring check: the script body must `import fcntl` and call
    `fcntl.flock(...)` with LOCK_EX (exclusive) + LOCK_NB (non-block).
    """
    body = MUTMUT_LOCK_SCRIPT.read_text()
    assert "import fcntl" in body or "from fcntl" in body, (
        f"{MUTMUT_LOCK_SCRIPT.name} must import fcntl. Body had no "
        f"`import fcntl` line — wrapper would not work on macOS if it "
        f"shells out to flock(1)."
    )
    assert "fcntl.flock" in body, (
        f"{MUTMUT_LOCK_SCRIPT.name} must call fcntl.flock(...). "
        f"Body had no such call."
    )
    # LOCK_EX (exclusive) + LOCK_NB (non-block). Non-block is critical:
    # without it, contending invocations BLOCK indefinitely, which
    # masks the bug rather than fails loudly.
    assert "LOCK_EX" in body, (
        f"{MUTMUT_LOCK_SCRIPT.name} must use fcntl.LOCK_EX (exclusive). "
        f"Without exclusivity, two mutmut runs could acquire the lock "
        f"simultaneously."
    )
    assert "LOCK_NB" in body, (
        f"{MUTMUT_LOCK_SCRIPT.name} must use fcntl.LOCK_NB (non-block) "
        f"so a contended invocation FAILS FAST rather than blocking "
        f"silently. Blocking would mask the concurrency bug."
    )


def test_mutmut_lock_recipes_guard_long_running_tiers():
    """Ticket 86b9vgh1a: test-mutmut + test-equivalence + test-integration
    Makefile recipes must invoke the lock wrapper.

    These are the three recipes that either mutate
    bot/engines/{volatility,probability}.py in-place (test-mutmut) or
    read those files during a test run (test-equivalence,
    test-integration). Without the guard, a background `make
    test-mutmut` clobbers an in-progress equivalence/integration run.

    Variable-resolution aware: the recipe may invoke the script
    directly (`python scripts/_mutmut_lock.py ...`) OR via a Make
    variable (`$(MUTMUT_GUARD) ...`) whose body expands to the same
    script invocation. Both shapes are valid.
    """
    text = _content()
    for tgt in ("test-mutmut", "test-equivalence", "test-integration"):
        recipe = _recipe_for(tgt)
        if "_mutmut_lock.py" in recipe:
            continue
        # Variable-resolution path: find `$(VAR)` refs in the recipe
        # and check if any resolves to a value containing
        # `_mutmut_lock.py`.
        var_refs = re.findall(r"\$\(([A-Za-z_][A-Za-z0-9_]*)\)", recipe)
        resolved = False
        for var in var_refs:
            m = re.search(rf"^{re.escape(var)}\s*[:?]?=\s*(.+)$", text, re.M)
            if m and "_mutmut_lock.py" in m.group(1):
                resolved = True
                break
        assert resolved, (
            f"{tgt} recipe does not invoke scripts/_mutmut_lock.py "
            f"directly or via a Make variable that resolves to it. "
            f"Ticket 86b9vgh1a requires all three long-running tier "
            f"recipes to acquire the lock so concurrent invocations "
            f"fail-fast rather than corrupting each other. Recipe was: "
            f"{recipe!r}"
        )


def test_mutmut_lock_contention_fails_fast_not_blocks():
    """Functional regression: two concurrent invocations of the lock
    wrapper must NOT both succeed; the contender must exit non-zero
    with a clear stderr message.

    Spawn two subprocess.Popen wrappers around `_mutmut_lock.py`. The
    first runs a 2-second `sleep` (holds the lock); the second tries
    to acquire and run `true` (would succeed if the lock weren't held).

    Expected:
      * Holder exits 0 (sleep completes).
      * Contender exits NON-ZERO and stderr mentions "lock" / "busy" /
        "contention" (operator-readable signal).
    """
    if not MUTMUT_LOCK_SCRIPT.exists():
        pytest.skip("Lock wrapper not yet implemented.")
    python = shutil.which("python3") or shutil.which("python")
    if python is None:
        pytest.skip("No python interpreter on PATH.")
    with tempfile.TemporaryDirectory() as td:
        lock_path = Path(td) / "test.lock"
        # Holder: 2s sleep. Use `sh -c sleep 2` so the wrapper has a
        # real subprocess to exec into (mirrors mutmut run shape).
        holder = subprocess.Popen(
            [python, str(MUTMUT_LOCK_SCRIPT), "acquire", str(lock_path), "--", "sh", "-c", "sleep 2"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        # Give the holder ~200ms to acquire before contender attempts.
        # Polling for the lockfile to exist would be more deterministic
        # but fcntl-style flock holds the kernel-level lock on the FD
        # without necessarily creating a separate sentinel file, so a
        # short fixed wait is the pragmatic choice for this regression.
        import time as _time
        _time.sleep(0.3)
        contender = subprocess.run(
            [python, str(MUTMUT_LOCK_SCRIPT), "acquire", str(lock_path), "--", "true"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        holder_stdout, holder_stderr = holder.communicate(timeout=10)
    assert holder.returncode == 0, (
        f"Holder invocation failed unexpectedly. exit={holder.returncode}, "
        f"stdout={holder_stdout!r}, stderr={holder_stderr!r}"
    )
    assert contender.returncode != 0, (
        f"Contender acquired lock while holder was active — exit 0. "
        f"fcntl.flock with LOCK_NB should reject concurrent acquisition. "
        f"stdout={contender.stdout!r}, stderr={contender.stderr!r}"
    )
    # Stderr message must mention something operator-actionable.
    combined = (contender.stdout + contender.stderr).lower()
    assert any(token in combined for token in ("lock", "busy", "contention", "another")), (
        f"Contender error message is unhelpful — operator can't tell "
        f"this is a lock contention. stderr={contender.stderr!r}"
    )


def test_gitignore_covers_mutmut_lock_file():
    """Ticket 86b9vgh1a: the .mutmut.lock sentinel must be gitignored.

    fcntl.flock locks an FD, not a path — but the wrapper still creates
    a sentinel file at the lock path so the FD can be opened. A stray
    `.mutmut.lock` in `git status` is noise (and on macOS, iCloud Drive
    spawns `.mutmut 2.lock` conflict copies of any unignored lockfile).
    """
    gitignore = (REPO_ROOT / ".gitignore").read_text()
    # Accept any pattern that ends in `.mutmut.lock` — bare filename,
    # leading `/`, leading `**/`. The existing `.claude/scheduled_tasks*.lock`
    # entry is for a DIFFERENT lockfile and does NOT cover this one.
    assert re.search(r"(^|/|\*)\.mutmut\.lock\b", gitignore, re.M), (
        f".gitignore doesn't cover `.mutmut.lock`. Ticket 86b9vgh1a "
        f"requires it so the lockfile sentinel doesn't show up in "
        f"`git status` after a `make test-mutmut` invocation."
    )
