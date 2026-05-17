# tests/

Pytest test suite, organized by tier (Bit 12.2, Sprint 12).

## Tier dirs

| Dir | Tier | Make target | Budget | What lives here |
|---|---|---|---|---|
| `unit/` | 1 | `make test-unit` | <10s (sub-sec actual) | Pure invariants: pyproject parsing, Makefile, repo hygiene, hook contracts. No DB, no network. |
| `contracts/` | 2 | `make test-contract` | <5s | Pillar 1 public_api snapshot + Pillar 2 import-linter + AST guards (extraction tests, call_sites, db_signatures, config_consistency, order_outcome_vocab). |
| `equivalence/` | 3 | `make test-equivalence` | <30s | Pillar 3 numeric snapshots + property tests for `bot/engines/{volatility,probability}.py`. **Snapshot regen is human-only** — see `equivalence/REGEN.md`. |
| `integration/` | 4 | `make test-integration` + `test-integration-serial` | <30s parallel + <20s serial | Everything else. Broad behavioral suite, real-DB tests. Bit-5 (CI perf umbrella 86b9zjtzk) parallelizes via `pytest-xdist --dist=loadfile -n auto`; 11 timing-sensitive `@pytest.mark.serial` tests (subprocess/Barrier/SIGALRM/daemon-thread-log-race) run in the single-worker pass. |
| `regression/` | (legacy) | runs under integration | — | 8 Sprint-1 files pinned by `tests/unit/test_no_root_test_files.py::test_relocated_real_tests_present`. New regression tests go alongside their feature in `integration/` and follow the `test_<bug_keyword>_regression` naming. |

## Non-test subdirs

- `hooks/` — test-infrastructure helpers (pre-commit hook tests).
- `fixtures/` — shared fixture data (non-test files; scenario SQL, JSON samples).

## Quick reference

```bash
make test               # all tiers in order, fail-fast on cheapest
make test-affected      # testmon-driven incremental (per-machine cache)
make pre-commit-checks  # ast-check + lint + doc-drift + unit + contract (<30s)
```

For full operator runbook (CI workflows, deploy-blocking integration tier,
mutmut concurrency guard, TDD-with-hook bypass markers) see
[`tests/CLAUDE.md`](CLAUDE.md).
