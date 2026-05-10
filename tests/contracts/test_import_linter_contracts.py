"""Pillar 2 of the testing-foundation-sprint (ticket 86b9ve0yt).

Pins the import-linter wiring so a future edit can't silently
disable it:

1. ``import-linter`` is in ``[project.optional-dependencies].dev`` —
   the dev extras install path used by both CI workflows.
2. ``.importlinter`` exists at the repo root with the four contracts
   the Bit shipped (engines / fetchers / feeds / helpers boundaries).
   **Bit 6.3 path-B (2026-05-10)** lifted the ``ignore_imports``
   carve-out for the Bit 6.2 late-binding in
   ``bot/engines/probability.py`` by relocating the
   ``_CALIBRATION_ENGINE`` singleton + ``_resolve_cal_engine`` helper
   from ``bot/_impl.py`` to ``bot/engines/calibration.py``.
3. The ``lint-imports`` step is wired into ``.github/workflows/test.yml``
   AND ``.github/workflows/deploy.yml`` BEFORE the pytest step so a
   layering violation fails fast.
4. ``lint-imports`` exits 0 on the current tree (the contracts must
   reflect reality, not aspirations).
5. **Post-Bit-6.3**: removing/restoring the historical carve-out
   string from ``.importlinter`` is a no-op for the linter (no edge
   matches anymore). The negative-smoke test now asserts the carve-out
   is GONE and that ``lint-imports`` passes without it — guards
   against accidental re-introduction of the engines→_impl import.
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

import ast
import configparser
import os
import re
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
STATE_PY = REPO_ROOT / "bot" / "state.py"
TEST_YML_PATH = REPO_ROOT / ".github" / "workflows" / "test.yml"
DEPLOY_YML_PATH = REPO_ROOT / ".github" / "workflows" / "deploy.yml"

EXPECTED_CONTRACTS = (
    "engines-no-impl",
    "fetchers-no-engines",
    "feeds-no-engines",
    "helpers-leaf",
    "state-no-impl-toplevel",
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


def test_importlinter_engines_no_impl_has_no_carve_out_post_bit_6_3():
    """The Bit 6.2 late-binding carve-out
    (``bot.engines.probability -> bot._impl``) was lifted by Bit 6.3
    path-B (2026-05-10). The relocation moved
    ``_CALIBRATION_ENGINE`` + ``_CAL_REGISTRY`` + ``_resolve_cal_engine``
    from ``bot/_impl.py`` to ``bot/engines/calibration.py``;
    probability.py now reaches them via top-level
    ``from bot.engines import calibration as _cal_state``.

    This test asserts the carve-out is ABSENT and the contract has no
    ``ignore_imports`` directive — locking the path-B refactor against
    a future "restore the carve-out" rollback that would silently
    re-introduce the engines→_impl edge.

    Configparser-level check (not a substring scan) so a future edit
    that comments-out / moves the carve-out string but leaves the
    inline doc block intact CANNOT false-green this test.
    """
    cp = _read_importlinter()
    section = "importlinter:contract:engines-no-impl"
    assert cp.has_section(section), "engines-no-impl contract missing"
    ignore_imports = _multiline_values(cp, section, "ignore_imports")
    assert "bot.engines.probability -> bot._impl" not in ignore_imports, (
        "Bit 6.2 carve-out `bot.engines.probability -> bot._impl` "
        "reappeared in [importlinter:contract:engines-no-impl] "
        "ignore_imports — Bit 6.3 path-B should have lifted it. "
        "Either an unrelated late-binding has been re-introduced (then "
        "investigate WHY and amend the contract with KB rationale) or a "
        "rebase/merge re-pulled the old contract — drop the line and "
        "verify lint-imports + the equivalence harness still pass."
    )
    assert not ignore_imports, (
        f"engines-no-impl contract has ignore_imports entries: "
        f"{ignore_imports}. Path-B refactor expected zero. If a new "
        f"carve-out is genuinely needed, add it explicitly here AND "
        f"document the rationale in a KB doc."
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


# ─── 2.5. state-no-impl-toplevel pins (Bit 7.1 fu, ticket 86b9vhca0) ─────────


def test_state_late_binding_is_inside_helper_function():
    """Positive: bot/state.py reaches bot._impl ONLY via a method-body
    import inside ``_get_compute_for_15m_main_path()``.

    Bit 7.1 (790214f, 2026-05-10) extracted StateManager via path-A++.
    The single-name late-binding helper sidesteps the load-order cycle
    (bot._impl re-exports bot.state — search anchor:
    ``from bot.state import StateManager`` — and
    ``compute_for_15m_main_path`` is bound below that re-export via
    ``make_compute_for_15m_main_path()``). The import-linter contract
    ``state-no-impl-toplevel`` documents the ``bot.state -> bot._impl``
    edge as an explicit carve-out; this AST pin asserts the carve-out is
    used the way the contract describes (method-body inside the helper),
    not at module top-level.

    Three layers because:
      1. import-linter sees the edge in the grimp graph (handled by the
         state-no-impl-toplevel contract + its ignore_imports carve-out).
      2. AST walk confirms the import lives inside the helper (this test).
      3. AST walk in tests/test_state_extraction.py
         (``test_state_no_top_level_bot_impl_import``) confirms NO
         top-level import. Both pins must hold.
    """
    src = STATE_PY.read_text()
    tree = ast.parse(src)
    helper_fn = next(
        (
            node
            for node in ast.iter_child_nodes(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "_get_compute_for_15m_main_path"
        ),
        None,
    )
    assert helper_fn is not None, (
        "bot/state.py is missing `def _get_compute_for_15m_main_path()` — "
        "the path-A++ single-name late-binding helper. See "
        "kb/decisions/bit-7.1-shipped-may10.md for context."
    )
    found = False
    for node in ast.walk(helper_fn):
        if isinstance(node, ast.Import) and any(
            alias.name == "bot._impl" for alias in node.names
        ):
            # Matches `import bot._impl` and `import bot._impl as X`.
            found = True
            break
        if isinstance(node, ast.ImportFrom):
            if node.module == "bot._impl":
                # Matches `from bot._impl import X`.
                found = True
                break
            if node.module == "bot" and any(
                alias.name == "_impl" for alias in node.names
            ):
                # Matches `from bot import _impl` (and `... as X`).
                # Restricted to the `_impl` name so that a refactor
                # importing a DIFFERENT bot name from inside the helper
                # (e.g., `from bot import constants`) — which would not
                # late-bind bot._impl — does not silently keep this test
                # green.
                found = True
                break
    assert found, (
        "bot/state.py::_get_compute_for_15m_main_path() does not perform a "
        "method-body import that names `bot._impl` (`import bot._impl`, "
        "`from bot._impl import X`, or `from bot import _impl`). The "
        "late-binding it provides is the only legitimate way for bot/state.py "
        "to reach `compute_for_15m_main_path` (which is bound below the "
        "line-~109 re-export of bot.state inside bot/_impl.py — search "
        "anchor: `from bot.state import StateManager`). Reverting the helper "
        "would re-introduce the load-order cycle that path-A++ fixed."
    )


def test_state_no_toplevel_bot_impl_import_at_contract_layer():
    """Negative (peer to ``test_state_extraction.py``): bot/state.py has
    NO top-level ``import bot._impl`` or ``from bot._impl import ...``.

    Pillar 2's import-linter sees both top-level and method-body imports
    as the same grimp edge — the ``state-no-impl-toplevel`` contract's
    ``ignore_imports = bot.state -> bot._impl`` carve-out covers the
    helper's method-body import but would also silently mask a future
    regression that hoists the import to module top-level. This AST-level
    pin closes that gap by walking module-level statements directly.

    Redundant with ``test_state_no_top_level_bot_impl_import`` in
    tests/test_state_extraction.py — both seals are intentional. The
    extraction-test pin lives next to the StateManager schema/method
    pins; this one lives next to the import-linter contract that pairs
    with it. Same invariant, two anchors.
    """
    src = STATE_PY.read_text()
    tree = ast.parse(src)
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module != "bot._impl", (
                f"bot/state.py has top-level "
                f"`from bot._impl import {[a.name for a in node.names]}` — "
                f"forbidden by Pillar 2 contract `state-no-impl-toplevel`. "
                f"Only method-body late-binding inside "
                f"_get_compute_for_15m_main_path() is allowed."
            )
            if node.module == "bot":
                for alias in node.names:
                    assert alias.name != "_impl", (
                        "bot/state.py has top-level `from bot import _impl` — "
                        "forbidden by Pillar 2 contract `state-no-impl-toplevel`. "
                        "Only method-body late-binding is allowed."
                    )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name != "bot._impl", (
                    "bot/state.py has top-level `import bot._impl` — "
                    "forbidden by Pillar 2 contract `state-no-impl-toplevel`. "
                    "Only method-body late-binding is allowed."
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


def _makefile_test_contract_invokes_lint_imports() -> bool:
    """Verify the `test-contract` Makefile recipe invokes lint-imports.

    R1 M2 follow-up: when `_step_index_running_lint_imports` accepts
    the Pillar 5 indirection (workflow step says `run: make test-contract`),
    we need a second anchor — the recipe itself — to be sure
    lint-imports actually executes. A future Makefile edit that
    drops `$(LINT_IMPORTS)` from `test-contract` would otherwise
    silently weaken the Pillar 2 contract while this test stays green.

    Folds backslash-continuations so a multi-line `test-contract`
    recipe parses correctly. Looks for `LINT_IMPORTS` (the Make
    variable) OR `lint-imports` (direct CLI invocation) anywhere in
    the recipe body.
    """
    makefile = REPO_ROOT / "Makefile"
    if not makefile.exists():
        return False
    folded = re.sub(r"\\\n", " ", makefile.read_text())
    m = re.search(
        r"^test-contract:[^\n]*\n((?:\t.*\n?)+)",
        folded,
        re.M,
    )
    if not m:
        return False
    recipe = m.group(1)
    return "LINT_IMPORTS" in recipe or "lint-imports" in recipe


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
          the Make recipe terminates with `$(LINT_IMPORTS)`. The
          accept-this-shape branch ALSO requires the Makefile recipe
          to actually invoke lint-imports (R1 M2 fix) — without
          that double-anchor, a future Makefile edit could drop
          `$(LINT_IMPORTS)` while this test stays green.
    """
    for i, block in enumerate(steps):
        body = block.split("\n", 1)[1] if "\n" in block else ""
        if "run: lint-imports" in block or "run: lint-imports" in body:
            return i
        if "run: make test-contract" in block or "run: make test-contract" in body:
            if _makefile_test_contract_invokes_lint_imports():
                return i
            # Fall through — the workflow delegates to a Make recipe
            # that no longer runs lint-imports. Treat as "not present"
            # so the assertion below fires with a clear message.
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


def test_lint_imports_fails_when_engines_to_impl_edge_re_introduced(
    tmp_path: Path,
):
    """Negative smoke: simulating a regression that re-adds an
    ``engines → _impl`` import edge must cause ``lint-imports`` to
    FAIL. Proves the engines-no-impl contract is genuinely enforced
    and not silently bypassed by some misconfigured directive.

    **Bit 6.3 path-B inversion**: pre-Bit-6.3 this test mutated
    ``.importlinter`` to remove the Bit 6.2 ``ignore_imports``
    carve-out and asserted the linter then failed (proving the
    carve-out was load-bearing). Path-B lifted the late-binding so
    the carve-out is gone; instead, this test mutates
    ``bot/engines/probability.py`` to add a literal
    ``import bot._impl`` at module top + a reference inside
    ``compute()`` (so grimp records the edge), copies the rest of
    the project tree into ``tmp_path`` to keep ``.importlinter``'s
    relative paths working, and asserts ``lint-imports`` exits
    non-zero with the contract reporting BROKEN.

    The fixture-tree mutation pattern (vs the previous
    ``--config <mutated>``) is required because grimp resolves the
    target package from the cwd at lint-time; mutating just the
    config doesn't move the code.
    """
    cmd = _require_lint_imports()

    # The companion test (test_importlinter_engines_no_impl_has_no_
    # carve_out_post_bit_6_3) covers the precondition that the
    # carve-out is absent. Don't repeat it here — substring-matching
    # the raw .importlinter text is fragile (the doc block may
    # mention the historical edge string). This regression test
    # injects a real import edge and verifies the linter catches it,
    # which is independent of whether the carve-out string appears
    # in a comment.

    # Set up an isolated copy of the project so we can mutate
    # bot/engines/probability.py without dirtying the real tree.
    fixture_root = tmp_path / "project"
    # Copy only what import-linter needs to resolve the graph: the
    # bot/ package and the .importlinter file. Skipping VCS state and
    # large unrelated dirs keeps the copy cheap.
    shutil.copytree(REPO_ROOT / "bot", fixture_root / "bot")
    shutil.copy(IMPORTLINTER_PATH, fixture_root / ".importlinter")

    # Inject a real engines→_impl import edge into probability.py so
    # grimp records it. We add a top-level `import bot._impl` AND a
    # reference at module scope (the import alone is enough for grimp
    # but the reference makes the failure observable to humans).
    probability_path = fixture_root / "bot" / "engines" / "probability.py"
    src = probability_path.read_text()
    mutated = (
        "import bot._impl  # path-B regression smoke: forbidden edge\n"
        "_REGRESSION_PROBE = bot._impl  # noqa\n\n"
        + src
    )
    assert mutated != src, "mutation no-op — regression-probe insertion failed"
    probability_path.write_text(mutated)

    result = subprocess.run(
        cmd,
        cwd=fixture_root,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode != 0, (
        "lint-imports passed despite a fresh `import bot._impl` at the "
        "top of bot/engines/probability.py — the engines→_impl "
        "contract is no longer enforced. Either the contract was "
        "loosened (check .importlinter) or a new ignore_imports edge "
        "was added that subsumes this one. Investigate before merging.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    # Defense-in-depth: confirm the failure mentions the expected
    # contract / edge so we know it failed for the RIGHT reason.
    combined = result.stdout + result.stderr
    assert "engines-no-impl" in combined or "bot._impl" in combined, (
        "lint-imports failed but neither the contract id "
        "`engines-no-impl` nor the forbidden module `bot._impl` "
        "appears in the output — failure may be unrelated.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )


def test_lint_imports_fails_when_state_carve_out_removed(tmp_path: Path):
    """Negative smoke: removing the ``bot.state -> bot._impl`` entry from
    the ``state-no-impl-toplevel`` contract's ``ignore_imports`` MUST
    cause lint-imports to fail — proving the carve-out is load-bearing
    for the method-body late-binding inside
    ``_get_compute_for_15m_main_path()`` in bot/state.py (Bit 7.1; search
    anchor: ``def _get_compute_for_15m_main_path``).

    Mirrors the pre-Bit-6.3 ``test_lint_imports_fails_when_carve_out_removed``
    pattern (since lifted by path-B for engines). The state carve-out
    cannot be lifted the same way: the load-order cycle (bot._impl
    re-exports bot.state — search anchor: ``from bot.state import
    StateManager`` — and ``compute_for_15m_main_path`` is bound below
    that re-export via ``make_compute_for_15m_main_path()``)
    makes a top-level import structurally impossible. The carve-out is
    permanent until that cycle is structurally redesigned, so this test
    locks the configuration against an accidental removal of the ignore
    line.

    Mutation strategy: parse ``.importlinter`` with configparser, drop
    the ``ignore_imports`` key from the state contract section, write
    back. Comments don't survive the round-trip but the contracts
    themselves are preserved verbatim — what lint-imports cares about.
    """
    cmd = _require_lint_imports()

    fixture_root = tmp_path / "project"
    shutil.copytree(REPO_ROOT / "bot", fixture_root / "bot")
    shutil.copy(IMPORTLINTER_PATH, fixture_root / ".importlinter")

    cp = configparser.RawConfigParser()
    parsed = cp.read(fixture_root / ".importlinter")
    assert parsed, "test setup error: copied .importlinter is unreadable"
    section = "importlinter:contract:state-no-impl-toplevel"
    assert cp.has_section(section), (
        "test setup error: state-no-impl-toplevel contract missing from "
        "the copied .importlinter — the contract under test isn't in place."
    )
    assert cp.has_option(section, "ignore_imports"), (
        "test setup error: state-no-impl-toplevel contract has no "
        "ignore_imports key to remove — the carve-out this test guards "
        "is already absent. If the carve-out was deliberately lifted "
        "(load-order cycle redesigned), drop this test in the same commit."
    )
    cp.remove_option(section, "ignore_imports")
    with open(fixture_root / ".importlinter", "w") as fh:
        cp.write(fh)

    result = subprocess.run(
        cmd,
        cwd=fixture_root,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode != 0, (
        "lint-imports passed with the state-no-impl-toplevel ignore_imports "
        "carve-out removed — the bot.state→bot._impl edge from the "
        "method-body late-binding in _get_compute_for_15m_main_path() "
        "was not caught. Either the helper no longer imports bot._impl "
        "(load-order cycle resolved? KB closeout doc must record it) or "
        "another ignore_imports line subsumes this edge. Investigate "
        "before merging.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert "state-no-impl-toplevel" in combined or "bot._impl" in combined, (
        "lint-imports failed but neither the contract id "
        "`state-no-impl-toplevel` nor the forbidden module `bot._impl` "
        "appears in the output — failure may be unrelated to the "
        "carve-out removal.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
