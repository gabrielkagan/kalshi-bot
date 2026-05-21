# tests/

Pytest test suite, organized by tier (Bit 12.2, Sprint 12).

## Tier dirs

| Dir | Tier | Make target | Budget | What lives here |
|---|---|---|---|---|
| `unit/` | 1 | `make test-unit` | <10s (sub-sec actual) | Pure invariants: pyproject parsing, Makefile, repo hygiene, hook contracts. No DB, no network. |
| `contracts/` | 2 | `make test-contract` | <5s | Pillar 1 public_api snapshot + Pillar 2 import-linter + AST guards (extraction tests, call_sites, db_signatures, config_consistency, order_outcome_vocab). |
| `equivalence/` | 3 | `make test-equivalence` | <30s | Pillar 3 numeric snapshots + property tests for `bot/engines/{volatility,probability}.py`. **Snapshot regen is human-only** — see `equivalence/REGEN.md`. |
| `integration/` | 4 | `make test-integration-shard-0` + `test-integration-shard-1` + `test-integration-serial` | <30s × 2 shards + <20s serial (concurrent in CI) | Everything else. Broad behavioral suite, real-DB tests. Bit-5 parallelizes via `pytest-xdist --dist=loadfile -n auto`. Bit-9 (2026-05-17) further splits into 2 hash-balanced shards via `pytest-shard`; each runs ~half the corpus on its own GH job. 11 timing-sensitive `@pytest.mark.serial` tests (subprocess/Barrier/SIGALRM/daemon-thread-log-race) run in the single-worker pass. |
| `regression/` | (legacy) | runs under integration | — | 8 Sprint-1 files pinned by `tests/unit/test_no_root_test_files.py::test_relocated_real_tests_present`. New regression tests go alongside their feature in `integration/` and follow the `test_<bug_keyword>_regression` naming. |
| `research/` | 5 (research) | `make test-research` | varies (NOT deploy-blocking) | Falsification spike tests + cross-system research (paired with `scripts/research/`). NOT in `make test` aggregate; ignored via `INTEGRATION_IGNORES` so deploy-blocking integration shards do not collect intentional `NotImplementedError` stubs during scaffold-first phase per TDD-first discipline. CT-MDP F0.1 (2026-05-20, 15) + F0.4 (2026-05-21, 18 scaffold) are the current inhabitants. |

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
