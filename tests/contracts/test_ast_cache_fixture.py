r"""Bit-4.5 CI perf — `repo_ast_cache` session-scoped fixture meta-tests.

Two contract surfaces pinned here:

1. **Cache identity contract** — the session-scoped `repo_ast_cache`
   fixture parses every .py in the canonical 4-glob exactly once and
   returns a dict[Path, ast.Module | None]. ``None`` marks files that
   failed to parse (unicode/IO/syntax). The cache excludes
   ``.claude/worktrees/`` (sibling worktrees from sister sessions).

   Without this contract, a cache-builder regression (wrong scope,
   silent file drop, wrong parser settings) could let every consumer
   audit silently false-green.

2. **Each refactored audit still detects its stale-import pattern.**
   For every audit refactored to consume ``repo_ast_cache``, a small
   meta-test calls the audit function directly with a synthetic cache
   containing one stale-import file and asserts the audit's
   ``AssertionError`` fires with the synthetic path in the message.

   Without this meta-test, a refactor regression where ``ast.walk`` or
   the per-test ``isinstance`` predicate is silently short-circuited
   would let every Sprint 10 modularization Bit's gate fail open. The
   audits ARE the gate; this test gates the gate.

Status: RED until Bit-4.5 lands tests/conftest.py with the fixture +
refactors the 43 in-scope audits. Out-of-scope audits
(``test_no_impl_star_import.py``, ``test_bit_10_3_ai_subpackage.py``)
walk a broader scope than the 4-glob and are filed as a followup
under ClickUp ticket ``86b9zk0ww`` (Bit-4.5-fu — extend cache to
broader-scope audits) for a separate Bit.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))


# ═════════════════════════════════════════════════════════════════════════════
# Helpers — independently re-enumerate the canonical 4-glob so the contract
# test is INDEPENDENT of the cache's own enumeration.
# ═════════════════════════════════════════════════════════════════════════════


def _canonical_4glob_paths() -> set[Path]:
    """Independently enumerate the canonical 4-glob scope.

    Returns the same path set the Bit-1 d.1 spec walks: repo-root *.py
    + bot/**/*.py + tests/**/*.py + scripts/**/*.py, excluding
    ``.claude/worktrees/``. Used to verify the cache fixture's scope.
    """
    paths: list[Path] = []
    paths.extend(REPO_ROOT.glob("*.py"))
    paths.extend((REPO_ROOT / "bot").rglob("*.py"))
    paths.extend((REPO_ROOT / "tests").rglob("*.py"))
    paths.extend((REPO_ROOT / "scripts").rglob("*.py"))
    return {
        p.resolve()
        for p in paths
        if ".claude/worktrees/" not in str(p)
    }


# ═════════════════════════════════════════════════════════════════════════════
# Section 1 — Cache identity contract
# ═════════════════════════════════════════════════════════════════════════════


class TestCacheIdentityContract:
    """Pin the fixture's return-shape + scope + parse semantics.

    These tests fail RED before tests/conftest.py exists; turn GREEN
    once the fixture lands. Each property is independently asserted so
    a regression that violates only ONE property surfaces precisely.
    """

    def test_fixture_returns_dict(self, repo_ast_cache):
        assert isinstance(repo_ast_cache, dict), (
            "repo_ast_cache must return a dict. Got "
            f"{type(repo_ast_cache).__name__}."
        )

    def test_fixture_has_sanity_floor_size(self, repo_ast_cache):
        """Cache has at least 350 entries (the 4-glob today is ~420).

        Floor of 350 hardens against partial scope regressions where
        one of the 3 large subtrees (bot/=67, tests/=273, scripts/=79)
        gets silently dropped. Floors below 350 would still pass even
        if e.g. ``tests/`` entirely (273 files, 65%) were missing.

        Independent re-enumeration in
        ``test_fixture_scope_matches_canonical_4glob_exactly`` is the
        authoritative scope check; this floor is a fast-fail backstop.
        """
        assert len(repo_ast_cache) >= 350, (
            f"repo_ast_cache only has {len(repo_ast_cache)} entries — "
            "the canonical 4-glob today returns ~420. Cache builder "
            "likely regressed (wrong scope / silent-drop of a subtree)."
        )

    def test_fixture_scope_matches_canonical_4glob_exactly(
        self, repo_ast_cache
    ):
        """Cache key-set is exactly the canonical 4-glob path set.

        Independent re-enumeration via ``_canonical_4glob_paths``
        catches drift in either direction (silent narrowing OR silent
        widening).
        """
        expected = _canonical_4glob_paths()
        actual = {p.resolve() for p in repo_ast_cache.keys()}
        missing = expected - actual
        extra = actual - expected
        assert not missing and not extra, (
            f"repo_ast_cache scope drift:\n"
            f"  missing (in 4-glob, absent from cache): {sorted(missing)[:5]}\n"
            f"  extra (in cache, outside 4-glob): {sorted(extra)[:5]}\n"
            "Cache builder's glob/filter logic regressed."
        )

    def test_fixture_values_are_ast_module_or_none(self, repo_ast_cache):
        for path, tree in repo_ast_cache.items():
            assert tree is None or isinstance(tree, ast.Module), (
                f"repo_ast_cache[{path!s}] is "
                f"{type(tree).__name__}; expected ast.Module or None."
            )

    def test_fixture_excludes_worktrees(self, repo_ast_cache):
        for path in repo_ast_cache:
            assert ".claude/worktrees/" not in str(path), (
                f"repo_ast_cache contains worktree path {path!s} — "
                "sibling-session contamination."
            )

    def test_fixture_parse_matches_fresh_parse(self, repo_ast_cache):
        """Sampled cache trees match a fresh ast.parse() of the file.

        Samples 1-2 non-None entries from EACH of the 4 subtrees
        (repo-root, bot/, tests/, scripts/) so a corruption isolated
        to one subtree (e.g., a future encoding bug specific to
        scripts/) surfaces. Dict-order sampling would only cover the
        first 2-3 subtrees. ``ast.dump`` is the canonical AST equality
        check.
        """
        per_subtree: dict[str, list] = {
            "root": [], "bot": [], "tests": [], "scripts": [],
        }
        for path, tree in repo_ast_cache.items():
            if tree is None:
                continue
            if path.parent == REPO_ROOT:
                bucket = "root"
            elif (REPO_ROOT / "bot") in path.parents:
                bucket = "bot"
            elif (REPO_ROOT / "tests") in path.parents:
                bucket = "tests"
            elif (REPO_ROOT / "scripts") in path.parents:
                bucket = "scripts"
            else:
                continue
            if len(per_subtree[bucket]) < 2:
                per_subtree[bucket].append((path, tree))
        sampled = [item for items in per_subtree.values() for item in items]
        assert sampled, "no parseable entries in cache — fixture broken"
        assert all(per_subtree[k] for k in ("bot", "tests", "scripts")), (
            "parse-match sampling missed one of the 3 large subtrees: "
            f"{ {k: len(v) for k, v in per_subtree.items()} }"
        )
        for path, cached_tree in sampled:
            fresh = ast.parse(path.read_text(encoding="utf-8"))
            assert ast.dump(cached_tree) == ast.dump(fresh), (
                f"repo_ast_cache[{path!s}] differs from fresh ast.parse() — "
                "cache builder corrupted the tree (wrong encoding / mode / "
                "feature_version)."
            )

    def test_fixture_is_session_scoped_identity(self, repo_ast_cache, request):
        """Two requests for the fixture in the same session return the
        SAME object (id identity). Catches a regression to
        ``scope="function"`` which would defeat the cache's entire
        purpose.
        """
        same = request.getfixturevalue("repo_ast_cache")
        assert repo_ast_cache is same, (
            "repo_ast_cache returned a different object on re-request — "
            "fixture is not session-scoped (defeats caching)."
        )


# ═════════════════════════════════════════════════════════════════════════════
# Section 2 — Per-audit synthetic detection meta-tests
# ═════════════════════════════════════════════════════════════════════════════
#
# Each refactored audit is invoked DIRECTLY with a synthetic
# ``repo_ast_cache`` dict containing exactly one file that exhibits the
# pattern the audit is written to detect. We then assert the audit's
# ``AssertionError`` fires AND the synthetic path appears in the
# message. Without this, a silent short-circuit in the refactor (e.g.,
# the per-test ``ast.walk`` loop body got removed, or the cache iteration
# became a no-op) would still let every audit pass on real code (which
# today has no offenders) and silently neuter the contract surface.


def _fake_cache(source: str, scope_dir: str = "tests", name: str = "fake.py") -> dict[Path, ast.Module]:
    """Build a synthetic 1-entry cache from a string source.

    The returned cache maps a single Path UNDER REPO_ROOT (need not exist
    on disk — the refactored audits walk the cached AST tree, not the
    file) to its parsed ``ast.Module``. Path is under ``scope_dir`` so
    subset-filter audits (test_bit_12_1: bot/+scripts/+tests/,
    test_bit_9_3_iii_c: bot/+scripts/) accept it.
    """
    return {REPO_ROOT / scope_dir / name: ast.parse(source)}


class TestSprint10AuditsDetectSynthetic:
    """One per audit family in tests/integration/test_sprint_10_*.py."""

    def test_sprint_10_1a_sports_data(self):
        from tests.integration.test_sprint_10_1a_sports_data_move import (
            test_no_stale_from_sports_data_imports,
            test_no_stale_import_sports_data,
        )
        cache = _fake_cache("from sports_data import Foo\n")
        with pytest.raises(AssertionError, match="fake.py"):
            test_no_stale_from_sports_data_imports(cache)
        cache2 = _fake_cache("import sports_data\n", name="fake2.py")
        with pytest.raises(AssertionError, match="fake2.py"):
            test_no_stale_import_sports_data(cache2)

    def test_sprint_10_1b_spx_engine(self):
        from tests.integration.test_sprint_10_1b_spx_engine_move import (
            test_no_stale_from_spx_engine_imports,
            test_no_stale_import_spx_engine,
        )
        cache = _fake_cache("from spx_engine import Foo\n")
        with pytest.raises(AssertionError, match="fake.py"):
            test_no_stale_from_spx_engine_imports(cache)
        cache2 = _fake_cache("import spx_engine\n", name="fake2.py")
        with pytest.raises(AssertionError, match="fake2.py"):
            test_no_stale_import_spx_engine(cache2)

    def test_sprint_10_1c_weather_engine(self):
        from tests.integration.test_sprint_10_1c_weather_engine_move import (
            test_no_stale_from_weather_engine_imports,
            test_no_stale_import_weather_engine,
        )
        cache = _fake_cache("from weather_engine import Foo\n")
        with pytest.raises(AssertionError, match="fake.py"):
            test_no_stale_from_weather_engine_imports(cache)
        cache2 = _fake_cache("import weather_engine\n", name="fake2.py")
        with pytest.raises(AssertionError, match="fake2.py"):
            test_no_stale_import_weather_engine(cache2)

    def test_sprint_10_1d_sports_engine(self):
        from tests.integration.test_sprint_10_1d_sports_engine_move import (
            test_no_stale_from_sports_engine_imports,
            test_no_stale_import_sports_engine,
        )
        cache = _fake_cache("from sports_engine import Foo\n")
        with pytest.raises(AssertionError, match="fake.py"):
            test_no_stale_from_sports_engine_imports(cache)
        cache2 = _fake_cache("import sports_engine\n", name="fake2.py")
        with pytest.raises(AssertionError, match="fake2.py"):
            test_no_stale_import_sports_engine(cache2)

    def test_sprint_10_2_shadows(self):
        from tests.integration.test_sprint_10_2_shadows_move import (
            test_no_stale_from_shadow_imports,
            test_no_stale_import_shadow,
        )
        cache = _fake_cache("from fifteenm_shadow import Foo\n")
        with pytest.raises(AssertionError, match="fake.py"):
            test_no_stale_from_shadow_imports("fifteenm_shadow", cache)
        cache2 = _fake_cache("import fifteenm_shadow\n", name="fake2.py")
        with pytest.raises(AssertionError, match="fake2.py"):
            test_no_stale_import_shadow("fifteenm_shadow", cache2)

    def test_sprint_10_5a_infra(self):
        from tests.integration.test_sprint_10_5a_infra_move import (
            test_no_stale_from_module_imports,
            test_no_stale_import_module,
        )
        cache = _fake_cache("from circuit_breaker import Foo\n")
        with pytest.raises(AssertionError, match="fake.py"):
            test_no_stale_from_module_imports("circuit_breaker", cache)
        cache2 = _fake_cache("import circuit_breaker\n", name="fake2.py")
        with pytest.raises(AssertionError, match="fake2.py"):
            test_no_stale_import_module("circuit_breaker", cache2)

    def test_sprint_10_5b_models(self):
        from tests.integration.test_sprint_10_5b_models_move import (
            test_no_stale_from_models_imports,
            test_no_stale_import_models,
        )
        cache = _fake_cache("from models import Foo\n")
        with pytest.raises(AssertionError, match="fake.py"):
            test_no_stale_from_models_imports(cache)
        cache2 = _fake_cache("import models\n", name="fake2.py")
        with pytest.raises(AssertionError, match="fake2.py"):
            test_no_stale_import_models(cache2)

    def test_sprint_10_6_migrations(self):
        from tests.integration.test_sprint_10_6_migrations_move import (
            test_no_stale_import_migration,
        )
        cache = _fake_cache("import migrate_to_supabase\n")
        with pytest.raises(AssertionError, match="fake.py"):
            test_no_stale_import_migration(
                "migrate_to_supabase.py", "migrate_to_supabase", cache
            )


class TestExtractionAuditsDetectSynthetic:
    """test_bit_10_4 + test_bit_12_1 + test_bit_9_3_iii_c — the in-scope
    subset of extraction_ast_walk. test_no_impl_star_import +
    test_bit_10_3 are out-of-scope for Bit-4.5 (broader-than-4-glob).
    """

    def test_bit_10_4_snapshots(self):
        from tests.contracts.test_bit_10_4_snapshots_extraction import (
            test_no_stale_from_imports,
            test_no_stale_bare_imports,
        )
        cache = _fake_cache("from dashboard_snapshot import Foo\n")
        with pytest.raises(AssertionError, match="fake.py"):
            test_no_stale_from_imports("dashboard_snapshot", cache)
        cache2 = _fake_cache("import dashboard_snapshot\n", name="fake2.py")
        with pytest.raises(AssertionError, match="fake2.py"):
            test_no_stale_bare_imports("dashboard_snapshot", cache2)

    def test_bit_12_1_config(self):
        """test_bit_12_1 walks bot/+scripts/+tests/ (subset of 4-glob,
        excludes repo-root *.py). The refactored audit filters the
        cache to those 3 dirs. Path under tests/ passes the filter.
        """
        from tests.contracts.test_bit_12_1_config_consolidation import (
            test_no_stale_from_config_imports_in_executable_code,
            test_no_stale_import_config_in_executable_code,
        )
        cache = _fake_cache(
            "from config import Foo\n",
            scope_dir="tests",
            name="_bit_4_5_fake_stale_config.py",
        )
        with pytest.raises(AssertionError, match="_bit_4_5_fake_stale_config"):
            test_no_stale_from_config_imports_in_executable_code(cache)
        cache2 = _fake_cache(
            "import config\n",
            scope_dir="tests",
            name="_bit_4_5_fake_stale_config2.py",
        )
        with pytest.raises(AssertionError, match="_bit_4_5_fake_stale_config2"):
            test_no_stale_import_config_in_executable_code(cache2)

    def test_bit_9_3_iii_c_impl(self):
        """test_bit_9_3_iii_c walks bot/+scripts/ (subset of 4-glob).
        Path under bot/ passes the filter.
        """
        from tests.integration.test_bit_9_3_iii_c_impl_deletion import (
            test_no_production_caller_imports_bot_impl,
        )
        cache = _fake_cache(
            "from bot._impl import X\n",
            scope_dir="bot",
            name="_bit_4_5_fake_stale_impl.py",
        )
        with pytest.raises(AssertionError, match="_bit_4_5_fake_stale_impl"):
            test_no_production_caller_imports_bot_impl(cache)


# ═════════════════════════════════════════════════════════════════════════════
# Section 3 — Cache integrity smoke (does the cache actually save work?)
# ═════════════════════════════════════════════════════════════════════════════


class TestCacheUsefulness:
    """Sanity: the cache contains at least one bot/ + tests/ + scripts/
    entry. Catches a regression where the cache builder silently drops
    one of the three subtrees (e.g., a typo in the rglob target)."""

    def test_cache_contains_bot_entries(self, repo_ast_cache):
        bot_entries = [
            p for p in repo_ast_cache
            if (REPO_ROOT / "bot") in p.parents
        ]
        assert len(bot_entries) >= 10, (
            f"cache has only {len(bot_entries)} bot/ entries — "
            "scope regression."
        )

    def test_cache_contains_tests_entries(self, repo_ast_cache):
        tests_entries = [
            p for p in repo_ast_cache
            if (REPO_ROOT / "tests") in p.parents
        ]
        assert len(tests_entries) >= 50, (
            f"cache has only {len(tests_entries)} tests/ entries — "
            "scope regression."
        )

    def test_cache_contains_scripts_entries(self, repo_ast_cache):
        scripts_entries = [
            p for p in repo_ast_cache
            if (REPO_ROOT / "scripts") in p.parents
        ]
        assert len(scripts_entries) >= 10, (
            f"cache has only {len(scripts_entries)} scripts/ entries — "
            "scope regression."
        )

    def test_cache_contains_repo_root_entries(self, repo_ast_cache):
        """Repo root has 3 .py files (egarch_state.json isn't .py)."""
        root_entries = [
            p for p in repo_ast_cache if p.parent == REPO_ROOT
        ]
        assert len(root_entries) >= 1, (
            "cache has 0 repo-root *.py entries — scope regression."
        )
