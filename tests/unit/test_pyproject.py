"""Regression tests for pyproject.toml (Bit 1.1 of repo modularization).

Sprint 1 of repo modularization plan (kb/decisions/repo-modularization-plan-may05.md).

These tests pin the Bit 1.1 contract:
- pyproject.toml exists and parses.
- Required runtime deps from requirements.txt are mirrored.
- Pytest config from the deleted pytest.ini was faithfully ported.
- Ruff config selects pycodestyle (E) and pyflakes (F) per the lenient spec.
- Python version pin is compatible with the VPS interpreter (3.9).
- pytest.ini is gone (single source of truth = pyproject.toml).

Sprint-1 canary `test_pyproject_no_installable_code_yet` was retired in Bit
2.1b. Its replacement is `test_pyproject_packages_find_scoped_to_bot` —
flat-layout discovery on this repo discovers ~10 top-level Python-identifier
dirs (kb/, data/, models/, agent_docs/, …) and aborts `pip install -e .`
unless `[tool.setuptools.packages.find]` is scoped to `bot*`. The collision
invariant lives in `test_bot_module_and_bot_package_dont_collide` below.
"""
import os
import re
import uuid
from pathlib import Path

import pytest
import bot.scanner  # noqa: F401

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"

# Python <3.11 needs tomli; 3.11+ ships tomllib in stdlib.
# If neither is available (e.g. clean VPS Python 3.9 without `pip install -e .[dev]`),
# skip the whole module rather than fail at collection — that lets `pytest`
# on the VPS keep working even when this dev-only test file can't run.
try:
    import tomllib as _toml  # type: ignore[import]
except ModuleNotFoundError:  # pragma: no cover - Python <3.11 path
    try:
        import tomli as _toml  # type: ignore[import]
    except ModuleNotFoundError:  # pragma: no cover
        pytest.skip(
            "Neither tomllib (stdlib 3.11+) nor tomli installed; "
            "run `pip install -e .[dev]` to enable pyproject regression tests.",
            allow_module_level=True,
        )


def _load() -> dict:
    with PYPROJECT.open("rb") as f:
        return _toml.load(f)


def test_pyproject_exists():
    assert PYPROJECT.exists(), (
        "pyproject.toml missing at repo root. Bit 1.1 of repo modularization "
        "plan ships this file."
    )


def test_pyproject_parses():
    data = _load()
    assert isinstance(data, dict)
    assert "project" in data
    assert "build-system" in data


def test_pyproject_project_metadata():
    data = _load()
    project = data["project"]
    assert project["name"] == "kalshi-bot"
    assert isinstance(project.get("version"), str) and project["version"]
    assert isinstance(project.get("description"), str) and project["description"]


def test_pyproject_python_version_admits_vps_interpreter():
    """VPS runs Python 3.9.6. requires-python must admit that exact version.

    Substring check (`">=3.9" in requires`) is too permissive — a future
    spec like `">=3.9.10"` would pass it but reject the VPS. Use PEP 440
    proper via `packaging.specifiers.SpecifierSet`.
    """
    from packaging.specifiers import SpecifierSet
    data = _load()
    requires = data["project"]["requires-python"]
    spec = SpecifierSet(requires)
    assert spec.contains("3.9.6"), (
        f"requires-python = {requires!r} excludes the VPS interpreter "
        f"(3.9.6). Tighten the spec ONLY when the VPS Python is upgraded "
        f"in lockstep."
    )


def test_pyproject_runtime_deps_match_requirements_txt():
    """[project].dependencies must contain the same DEP NAMES as requirements.txt.

    requirements.txt is the CI / VPS install path
    (see .github/workflows/test.yml + start.sh); pyproject is the packaging
    view. The two must be identical SETS OF NAMES so a dep added to one
    but not the other can't silently break either side.

    Names-only (PEP 503-normalized) — version constraints are NOT compared.
    Pyproject is allowed to express tighter constraints than requirements.txt
    when needed (e.g., pinning for reproducibility); requirements.txt is
    allowed to be bare. Drift in pinning intent is intentional latitude,
    not a regression. If you need to pin in both, it's the contributor's
    responsibility to keep them aligned.
    """
    data = _load()
    declared = {
        _basename(d).lower()
        for d in data["project"]["dependencies"]
    }
    required = set()
    for line in (REPO_ROOT / "requirements.txt").read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        # Skip pip-only directives (`-e .`, `-r other.txt`, `-c constraints.txt`)
        # — they aren't deps, just installer instructions.
        if line.startswith("-"):
            continue
        required.add(_basename(line).lower())
    missing_from_pyproject = required - declared
    missing_from_requirements = declared - required
    assert not missing_from_pyproject, (
        f"requirements.txt deps missing from pyproject [project].dependencies: "
        f"{sorted(missing_from_pyproject)}."
    )
    assert not missing_from_requirements, (
        f"pyproject [project].dependencies has deps not in requirements.txt: "
        f"{sorted(missing_from_requirements)}. CI installs from "
        f"requirements.txt — divergence breaks the build."
    )


def test_basename_helper_handles_pep_508_forms():
    """Round-trip the helper on adversarial inputs."""
    cases = {
        "requests": "requests",
        "requests>=2.0": "requests",
        "websocket-client": "websocket-client",
        "websocket_client": "websocket-client",  # PEP 503 _ -> -
        "zope.interface": "zope-interface",      # PEP 503 . -> -
        "Foo_Bar.baz": "foo-bar-baz",            # mixed + multi-sep collapse
        "pkg[extra]": "pkg",
        "pkg ; python_version < '3.11'": "pkg",
        "pkg @ git+https://github.com/x/y.git": "pkg",
        "PyPkg": "pypkg",
        "pkg~=1.2.3": "pkg",
        "pkg!=1.0": "pkg",
    }
    for raw, expected in cases.items():
        assert _basename(raw) == expected, f"_basename({raw!r}) -> {_basename(raw)!r}, want {expected!r}"

    # Adversarial: malformed inputs must raise, not return an empty string
    # that silently equals other malformed-and-empty results.
    for bad in ("@foo", ";bar", "=baz", "---", "", "   "):
        with pytest.raises(ValueError):
            _basename(bad)


def _basename(req: str) -> str:
    """Extract the canonical project name from a requirement spec.

    Handles PEP 508 forms: extras (`pkg[x]`), version specifiers
    (`pkg>=1`), env markers (`pkg; python_version<'3.11'`), and
    direct-URL refs (`pkg @ git+https://...`).
    Returns the PEP 503-normalized name: any run of `[-_.]` becomes a
    single `-`, lowercased — so `Foo_Bar.baz` and `foo-bar-baz` compare
    equal.

    Raises ValueError on inputs that yield an empty result (e.g.,
    `_basename('@foo')` would otherwise silently collapse to `''` and
    let two malformed entries match each other across the bidirectional
    sync test).
    """
    original = req
    for sep in ("[", "<", ">", "=", "!", "~", ";", "@", " "):
        idx = req.find(sep)
        if idx != -1:
            req = req[:idx]
    name = re.sub(r"[-_.]+", "-", req.strip()).strip("-").lower()
    if not name:
        raise ValueError(
            f"_basename({original!r}) yielded empty name — malformed dep spec."
        )
    return name


def test_pyproject_pytest_config_ported_from_pytest_ini():
    """pytest.ini → [tool.pytest.ini_options]. Verify faithful port."""
    data = _load()
    pt = data.get("tool", {}).get("pytest", {}).get("ini_options", {})
    assert pt, "[tool.pytest.ini_options] missing."

    # Mirror the deleted pytest.ini: testpaths=., python_files=test_*.py, etc.
    # Strict equality — `["."] + extras` would silently expand collection
    # scope vs the deleted pytest.ini's exact `testpaths = .`.
    assert pt.get("testpaths") == ["."], (
        f"testpaths = {pt.get('testpaths')!r}; pytest.ini had `testpaths = .` "
        f"and TOML form is `testpaths = [\".\"]`."
    )
    # Pin to list form — the lenient port-check lets `python_files = "test_*.py"`
    # pass while `addopts` is locked to list form (pyproject:73). Single
    # source of truth: list form everywhere in TOML.
    assert pt.get("python_files") == ["test_*.py"]
    assert pt.get("python_classes") == ["Test*"]
    assert pt.get("python_functions") == ["test_*"]

    markers = pt.get("markers") or []
    marker_names = {m.split(":", 1)[0].strip() for m in markers}
    for required in ("slow", "integration", "smoke", "fragile"):
        assert required in marker_names, (
            f"marker {required!r} missing from [tool.pytest.ini_options].markers; "
            f"pytest.ini had it."
        )

    addopts = pt.get("addopts", "")
    if isinstance(addopts, list):
        addopts = " ".join(addopts)
    for flag in ("-v", "--tb=short", "--ignore=venv"):
        assert flag in addopts, (
            f"pytest addopts missing {flag!r}; pytest.ini had it. Got: {addopts!r}"
        )


def test_pyproject_ruff_select_minimal():
    """Lenient ruff config: pycodestyle (E) + pyflakes (F) ONLY.

    Bit 1.1 spec: "lenient (E, F only)". Adding rule-groups now means
    fixing the 1000+ existing violations before they trip CI / pre-commit
    hooks — out of Bit 1.1 scope. Future bits can expand `select` after
    targeted cleanup.
    """
    data = _load()
    # Ruff's `[tool.ruff].select` (top-level) was deprecated in 0.5; with our
    # pin `ruff>=0.6`, ruff silently no-ops a top-level `select`. Assert the
    # config lives at the modern path so a contributor refactoring back to
    # the old form doesn't get green tests + dead lint config.
    ruff_lint = data.get("tool", {}).get("ruff", {}).get("lint", {})
    select = ruff_lint.get("select")
    assert select, (
        "ruff config missing `select` under [tool.ruff.lint]. "
        "(Top-level [tool.ruff].select was deprecated in 0.5 and silently "
        "ignored by 0.6+; ours is pinned >=0.6.)"
    )
    select_set = set(select)
    assert select_set == {"E", "F"}, (
        f"Bit 1.1 contract: ruff select must be exactly {{'E', 'F'}}; "
        f"got {sorted(select_set)!r}. Adding rules expands the violation "
        f"surface — do that in a dedicated cleanup bit, not Bit 1.1."
    )


def test_pyproject_ruff_target_version_pinned_to_vps():
    """Ruff's target-version must pin to the VPS interpreter (py39).

    Pinning matters because ruff lints / fixes use syntax features available
    in target-version. Drift between target-version and the VPS interpreter
    means ruff might propose a fix that uses 3.10+ syntax and crashes on
    deploy.
    """
    data = _load()
    ruff = data.get("tool", {}).get("ruff", {})
    target = ruff.get("target-version")
    if target is None:
        pytest.skip("ruff target-version not set; default applies.")
    assert target == "py39", (
        f"ruff target-version = {target!r}; must be 'py39' to match VPS. "
        f"When the VPS Python is upgraded, change here AND in start.sh / "
        f"systemd unit / requires-python in [project]."
    )


def test_pyproject_ruff_perfile_ignores_post_bit_1_5():
    """Per-file-ignores must cover tests/ (recursively) and not regress to
    a legacy root pattern.

    Bit 1.5 (modularization Sprint 1) moved all root-level test_*.py files
    into tests/regression/, then dropped the transitional `test_*.py`
    per-file-ignore pattern. The repo-root invariant ("no test_*.py at
    root") is enforced by tests/unit/test_no_root_test_files.py — this test
    pins the lint side of the same contract.
    """
    data = _load()
    ignores = (
        data.get("tool", {}).get("ruff", {}).get("lint", {}).get("per-file-ignores", {})
    )
    nested_test_glob = ignores.get("tests/**")
    assert nested_test_glob, (
        "Per-file-ignores missing `tests/**`. Bit 1.5 made this the sole "
        "source of test-file ignores; without it, tests/regression/ "
        "files lose their unused-import / line-length grace."
    )
    legacy_root = ignores.get("test_*.py") or ignores.get("**/test_*.py")
    assert legacy_root is None, (
        f"Legacy root test_*.py per-file-ignore reappeared: {legacy_root!r}. "
        f"Bit 1.5 dropped this pattern. Real tests belong under "
        f"tests/regression/; root-level test_*.py is rejected by "
        f"tests/unit/test_no_root_test_files.py."
    )


def test_pyproject_packages_find_scoped_to_bot_and_collector_and_wire():
    """Bit 2.1b replacement for the retired Sprint-1 canary, extended at
    D1.1 (ticket 86b9ypn49, 2026-05-16) for the new `collector/` sibling
    and at D1.1.5 (ticket 86b9zdhz2, 2026-05-16) for the new `kalshi_wire/`
    shared transport library.

    Without scoping, setuptools >=64 flat-layout discovery sees the repo's
    9 sibling top-level Python-identifier dirs (agent_docs/, analysis/,
    data/, kb/, models/, ops/, reports/, research/, templates/) alongside
    bot/ + collector/ + kalshi_wire/ and aborts `pip install -e .` with
    PackageDiscoveryError("Multiple top-level packages discovered in a
    flat-layout: ..."). The `make install` target (Makefile:57) — the
    documented dev-onboarding path — would explode on a fresh dev box.

    Pin the scoping so a future contributor doesn't drop the constraint and
    re-introduce the explosion.
    include=["bot", "bot.*", "collector", "collector.*", "kalshi_wire",
    "kalshi_wire.*"] matches all three parent packages and their
    subpackages. Both ``X`` + ``X.*`` patterns are needed for each parent
    because ``X.*`` requires a literal dot and doesn't match the bare
    parent name. namespaces=false provides defense-in-depth against future
    include-pattern broadening that might sweep in a PEP 420 namespace dir
    lacking __init__.py.

    Per kb/decisions/data-corpus-architecture.md §5 (2026-05-16
    AMENDMENT), kalshi_wire/ is a SIBLING to bot/ and collector/ — the
    pure-transport-leaf contracts (`kalshi_wire-no-bot` +
    `kalshi_wire-no-collector` in .importlinter) require structural
    independence. Listing all three at the same depth in the setuptools
    include matches that layout.
    """
    data = _load()
    find_cfg = (
        data.get("tool", {})
        .get("setuptools", {})
        .get("packages", {})
        .get("find", {})
    )
    assert find_cfg, (
        "[tool.setuptools.packages.find] missing — flat-layout discovery "
        "will explode with PackageDiscoveryError on this multi-top-level repo. "
        "Add: include=[\"bot\", \"bot.*\", \"collector\", \"collector.*\", "
        "\"kalshi_wire\", \"kalshi_wire.*\"], namespaces=false."
    )
    include = find_cfg.get("include") or []
    for entry in ("bot", "bot.*", "collector", "collector.*",
                  "kalshi_wire", "kalshi_wire.*"):
        assert entry in include, (
            f"include={include!r} must contain {entry!r}. "
            f"D1.1 (86b9ypn49) added `collector` entries; "
            f"D1.1.5 (86b9zdhz2, 2026-05-16) added `kalshi_wire` entries "
            "for the shared transport library; bot entries were locked in "
            "Bit 2.1b. Both ``X`` + ``X.*`` patterns are needed for each "
            "parent (the dotted pattern doesn't match the bare name)."
        )
    assert find_cfg.get("namespaces") is False, (
        f"namespaces must be False so PEP 420 namespace dirs (e.g., kb/, "
        f"data/, agent_docs/ — none have __init__.py) cannot sneak into "
        f"the build. Got: {find_cfg.get('namespaces')!r}."
    )


def test_pytest_ini_removed():
    """pytest.ini is the deleted predecessor; if it reappears, pytest will
    silently prefer it over [tool.pytest.ini_options] and the configs will drift."""
    assert not (REPO_ROOT / "pytest.ini").exists(), (
        "pytest.ini reappeared. Bit 1.1 deleted it in favor of "
        "[tool.pytest.ini_options] in pyproject.toml. Pytest prefers pytest.ini "
        "over pyproject — keeping both invites silent drift."
    )


def test_pyproject_mutmut_targets_engines():
    """Pin Pillar 5 [tool.mutmut].paths_to_mutate to the extracted engines.

    Without this, a future edit could retarget mutmut without anyone
    noticing — e.g., to bot/_impl.py (5,000+ LOC; would turn the 1-2h
    baseline into >24h) or to bot/helpers/ (the closeout doc would
    silently lie about which surface was graded).

    Pillar 5 (ticket 86b9ve11y) initial scope: ONLY the engines that
    have an equivalence harness (Pillar 3). Helpers + _impl are out
    of scope until extraction is further along — see
    `kb/decisions/testing-foundation-pillar-5-shipped-may09.md` if you
    need to rationale-check this set before bumping.
    """
    data = _load()
    mut = data.get("tool", {}).get("mutmut", {})
    paths = mut.get("paths_to_mutate")
    assert paths is not None, (
        "[tool.mutmut].paths_to_mutate missing. Pillar 5 (86b9ve11y) "
        "ships this config; if it's gone, either the section was "
        "deleted or the key was renamed — both cases break "
        "`make test-mutmut`."
    )
    # Tolerate either the TOML list form (preferred — no whitespace
    # fragility) or the legacy comma-separated string form (mutmut
    # 2.5.1 accepts both per `mutmut/__main__.py:328`). String form
    # has a known footgun: `split_paths` does NOT strip whitespace,
    # so `"a.py, b.py"` silently drops `b.py`.
    if isinstance(paths, str):
        targets = {p.strip() for p in paths.split(",") if p.strip()}
        # If string form is in use, defensively confirm none of the
        # entries will be dropped by mutmut's no-strip behavior.
        raw_split = paths.split(",")
        for entry in raw_split:
            assert entry == entry.strip(), (
                f"[tool.mutmut].paths_to_mutate entry {entry!r} has "
                f"leading/trailing whitespace. mutmut 2.5.1's "
                f"split_paths does not strip — this entry would be "
                f"silently dropped at runtime. Use the TOML list form "
                f"(`paths_to_mutate = [\"a.py\", \"b.py\"]`) to "
                f"sidestep the fragility entirely."
            )
    else:
        targets = set(paths)

    expected = {
        "bot/engines/volatility.py",
        "bot/engines/probability.py",
    }
    assert targets == expected, (
        f"[tool.mutmut].paths_to_mutate = {sorted(targets)}; expected "
        f"{sorted(expected)}. Pillar 5 (86b9ve11y) initial scope is "
        f"the equivalence-harnessed engines only. Adding bot/_impl.py "
        f"or bot/helpers/* expands the runtime by orders of magnitude "
        f"and breaks the closeout doc's `mutmut-baseline-may09.md` "
        f"surface claim. If you mean to expand: ship a new findings "
        f"doc + bump this assertion in the same commit."
    )

    # Sanity: every targeted file must exist on disk. mutmut's
    # split_paths filters non-existent paths silently — this catches
    # a renamed or deleted target before mutmut treats it as "no
    # mutants here, baseline is trivially perfect".
    for target in targets:
        assert (REPO_ROOT / target).is_file(), (
            f"[tool.mutmut].paths_to_mutate references {target!r} but "
            f"the file does not exist. mutmut would silently drop it; "
            f"the baseline run would over-report coverage."
        )


def test_extend_exclude_actually_excludes():
    """Verify each `extend-exclude` entry is functional, not just declarative.

    Caught a CRITICAL in adversarial review: `extend-exclude = ["/kb", ...]`
    with a leading `/` is silently a no-op in ruff (unlike gitignore's
    semantics). The fix dropped the slashes; this test guards against
    regressions where the leading-slash mistake is reintroduced or a new
    pattern is added that doesn't actually match.

    Methodology: drop a probe `.py` containing a guaranteed F401 into each
    excluded dir, run `ruff check --force-exclude` on the probe path
    (`--force-exclude` is required because ruff defaults to linting
    explicit file arguments regardless of exclude config), assert ruff
    reports no F401 (because exclusion silenced it). Cleans up after itself.
    """
    import shutil
    import subprocess

    data = _load()
    excludes = (
        data.get("tool", {}).get("ruff", {}).get("extend-exclude", []) or []
    )
    if not excludes:
        pytest.skip("No extend-exclude patterns to verify.")

    ruff = shutil.which("ruff")
    if ruff is None:
        venv_ruff = REPO_ROOT / "venv" / "bin" / "ruff"
        if venv_ruff.exists():
            ruff = str(venv_ruff)
    if ruff is None:
        pytest.skip("ruff not installed; install with `pip install -e .[dev]`.")

    # Unique probe name per process — defensive against pytest-xdist or
    # parallel runs colliding on the same `kb/_ruff_exclude_probe.py` file.
    probe_name = f"_ruff_exclude_probe_{os.getpid()}_{uuid.uuid4().hex[:8]}.py"

    def _ruff_check(file_path):
        return subprocess.run(
            [
                ruff, "check", "--no-cache", "--force-exclude",
                "--config", str(REPO_ROOT / "pyproject.toml"),
                str(file_path),
            ],
            capture_output=True, text=True, cwd=str(REPO_ROOT), timeout=60,
        )

    # Positive control: drop a probe in scripts/ (a non-excluded dir with
    # tracked Python) and confirm ruff DOES report F401. Without this,
    # any test-infra bug (wrong cwd, ruff binary points elsewhere, etc.)
    # would silently masquerade as exclusion working.
    control_dir = REPO_ROOT / "scripts"
    if not control_dir.is_dir():
        pytest.skip("No scripts/ dir — cannot establish positive control.")
    control_probe = control_dir / probe_name
    try:
        control_probe.write_text("import os\nimport sys\n")
        try:
            control = _ruff_check(control_probe)
        except subprocess.TimeoutExpired:
            pytest.fail("ruff hung on positive-control probe; test infra broken.")
        assert "F401" in control.stdout, (
            f"Positive control failed: ruff did NOT report F401 on a "
            f"non-excluded probe at {control_probe.relative_to(REPO_ROOT)}. "
            f"This means the test cannot trust subsequent exclusion checks. "
            f"exit={control.returncode}, stdout={control.stdout[:200]!r}"
        )
    finally:
        try:
            if control_probe.exists():
                control_probe.unlink()
        except OSError:
            pass

    failed = []
    for pattern in excludes:
        candidate_dir = REPO_ROOT / pattern.lstrip("/")
        if not candidate_dir.is_dir():
            # Some excluded paths (.smart-env, models, data, reports) are
            # local/runtime artifacts not present in a fresh `git clone`.
            # Functional probe in dirs that DO exist suffices.
            continue
        probe = candidate_dir / probe_name
        try:
            try:
                probe.write_text("import os\nimport sys\n")
            except (PermissionError, OSError) as exc:
                # Read-only mount or hostile FS — record and continue rather
                # than crash with an unhelpful traceback.
                failed.append(
                    f"  pattern {pattern!r}: cannot write probe to "
                    f"{candidate_dir.relative_to(REPO_ROOT)}/ ({exc!r}). "
                    f"If this dir is mounted read-only, exclude it from this "
                    f"test or skip the pattern."
                )
                continue
            try:
                result = _ruff_check(probe)
            except subprocess.TimeoutExpired:
                failed.append(
                    f"  pattern {pattern!r}: ruff hung past 60s. "
                    f"Likely a corrupt config or pyproject parse error."
                )
                continue
            # Ruff exit codes (0.x): 0=no issues, 1=issues found, 2=error.
            # We only know exclusion worked iff returncode==0 with no F401.
            # Anything else (parse error, missing file race, schema rename)
            # is "test infrastructure broken" and we must surface it — not
            # silently pass because F401 wasn't in stdout.
            if result.returncode not in (0, 1):
                failed.append(
                    f"  pattern {pattern!r}: ruff failed unexpectedly "
                    f"(exit={result.returncode}). "
                    f"stdout={result.stdout[:200]!r} stderr={result.stderr[:200]!r}"
                )
            elif result.returncode == 0 and "F401" in result.stdout:
                failed.append(
                    f"  pattern {pattern!r}: rc=0 but F401 in stdout — ruff "
                    f"behaviour changed. stdout={result.stdout[:200]!r}"
                )
            elif "F401" in result.stdout:
                failed.append(
                    f"  pattern {pattern!r} did NOT exclude "
                    f"{probe.relative_to(REPO_ROOT)}: "
                    f"exit={result.returncode}, stdout={result.stdout[:200]!r}"
                )
        finally:
            try:
                if probe.exists():
                    probe.unlink()
            except OSError:
                pass


    assert not failed, (
        "extend-exclude patterns are not functional. Common cause: a leading "
        "`/` (gitignore-style anchor) which ruff treats as filesystem-absolute, "
        "silently no-op'ing. Drop the leading slash.\n" + "\n".join(failed)
    )


def test_bot_module_and_bot_package_dont_collide():
    """Sprint-2 guard: never let the OLD top-level `bot.py` and `bot/` package coexist.

    Python resolves `import bot` ambiguously when both exist (regular package
    > module > namespace package). Sprint 1 shipped `bot.py` only; Bit 2.1a
    renamed `bot.py` → `bot/_impl.py` in the same commit it created
    `bot/__init__.py` — so the OLD root-level `bot.py` must not return.

    Test holds post-Bit-2.1a: `bot.py` doesn't exist, the assertion's left
    arm is False, the AND is False, and `not False` is True (passes).
    """
    bot_py = REPO_ROOT / "bot.py"
    bot_dir = REPO_ROOT / "bot"
    # Catches both regular packages (bot/__init__.py) AND namespace packages
    # (bot/ with submodules but no __init__.py). Either form coexisting with
    # the OLD top-level bot.py creates an ambiguous import.
    bot_dir_has_python = bot_dir.is_dir() and any(
        p.suffix == ".py" for p in bot_dir.rglob("*.py")
    )
    assert not (bot_py.exists() and bot_dir_has_python), (
        "BOTH the OLD top-level `bot.py` and a `bot/` package directory with "
        "Python content exist at the repo root. Python's import resolver picks "
        "one ambiguously based on sys.path order — production at risk. The "
        "Bit 2.1a commit should have renamed `bot.py` → `bot/_impl.py`."
    )


def test_build_system_setuptools():
    data = _load()
    bs = data["build-system"]
    requires = " ".join(bs.get("requires", []))
    assert "setuptools" in requires, (
        "build-system.requires must include setuptools (PEP 517 backend)."
    )
    assert bs.get("build-backend") == "setuptools.build_meta"
