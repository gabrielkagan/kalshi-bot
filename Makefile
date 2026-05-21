# Bit 1.2 of repo modularization plan
# (kb/decisions/repo-modularization-plan-may05.md).
# Sprint 1: developer-convenience targets only — no runtime impact, no
# code moves. Targets call existing scripts where they exist
# (scripts/cal_mlp/deploy_check.sh, scripts/audit/doc_drift_check.py) rather
# than reinventing.
#
# Make's "missing separator" error on a tab/space mistake is opaque, so
# all recipe lines below MUST be indented with a literal TAB.
# tests/unit/test_makefile.py pins this contract so an editor-on-save
# expand-tab can't silently break the file.
#
# Tested with GNU Make 3.81 (macOS default) and 4.x (Linux). No Make
# 4-only features used; stay 3.81-compatible until VPS/CI reasons emerge.

.DEFAULT_GOAL := help

# CWD guard — Make resolves recipe paths against `$(CURDIR)`, not the
# Makefile's location. A contributor who `cd tests/ && make test` would
# otherwise get either "No rule to make target" or, worse, a recipe
# that looks up `bot/constants.py` / `scripts/` relative to the wrong dir and
# silently misbehaves. Fail loudly at parse time with a clear remedy.
ifeq ($(wildcard pyproject.toml),)
$(error Makefile must be invoked from the repo root (where pyproject.toml lives); current dir is $(CURDIR))
endif

.PHONY: help install install-hooks test test-unit test-contract test-contract-pytest test-contract-lint test-equivalence test-integration test-integration-shard-0 test-integration-shard-1 test-integration-serial test-research test-affected test-changed test-fast test-mutmut ast-check lint doc-drift deploy-check api-snapshot-regen data-health alpha-audit 15m-audit hourly-audit 15m-alpha no-side skill-smoke pre-commit-checks refresh-map

# Override at invocation time if needed: `make PYTHON=python3.11 test`.
# NOTE: CI runs Python 3.11 (.github/workflows/test.yml), local default
# is whatever `python3` resolves to (3.9.6 on the dev box). Predates
# Bit 1.2 — flag here so the asymmetry doesn't surprise anyone running
# `make test` locally and seeing CI behave differently.
PYTHON ?= python3

# Resolve ruff at parse time (`:=` not `?=` so the `$(shell ...)` runs
# exactly once, regardless of how many times the var is expanded).
# Prefer PATH, fall back to venv/bin/ruff — mirrors
# tests/unit/test_pyproject.py::test_extend_exclude_actually_excludes. If
# neither exists, the lint recipe fails with a clear "ruff: command not
# found"; remedy is `make install` (which is a SUPERSET of CI's
# requirements.txt install — CI doesn't bring in ruff/tomli).
RUFF := $(shell command -v ruff 2>/dev/null || echo venv/bin/ruff)

# Pillar 2 (import-linter) ships a `lint-imports` console script via
# `pip install import-linter`. Same PATH-vs-fallback pattern as RUFF —
# CI's install puts it on PATH directly; macOS `pip install --user`
# (the dev-box default) drops it under `~/Library/Python/<X.Y>/bin`,
# which is on PATH only if the user has set it up. Fall back rather
# than break `make test-contract` on a fresh clone.
#
# Python version is detected at parse time so the fallback path tracks
# whichever interpreter `python3` resolves to (3.9 on the dev box
# today; trivially upgradable). A hard-coded `3.9` would silently break
# on any future Python upgrade. The `2>/dev/null` swallows the
# (unlikely) missing-python3 case so parse doesn't fail; the fallback
# becomes literally `~/Library/Python//bin/lint-imports` which fails
# loudly with a clear path on first invocation.
PYTHON_USER_SITE_VER := $(shell python3 -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")' 2>/dev/null)
LINT_IMPORTS := $(shell command -v lint-imports 2>/dev/null || echo $(HOME)/Library/Python/$(PYTHON_USER_SITE_VER)/bin/lint-imports)

# Pillar 5 of testing-foundation-sprint (ticket 86b9ve11y): tier the
# pytest suite so agent edit loops can target the fast tiers, and CI
# can gate blocking-vs-informational by tier.
#
# Tier classification (path-based, no marker rewrites needed):
#   * unit         — pure-Python invariant tests, no DB/network. <10s.
#   * contract     — structural gates: public_api snapshot, import-linter,
#                    AST guards, extraction tests. <5s.
#   * equivalence  — Pillar 3 numeric snapshots + property tests. <30s.
#   * integration  — current full suite minus the above. <2min.
#
# UNIT_FILES + CONTRACT_FILES are enumerated so test-integration's
# ignore-list is the exact complement (single source of truth — adding
# a file to a tier auto-removes it from integration). Update both lists
# AND tests/CLAUDE.md when the classification grows.
# Bit 12.2 (Sprint 12, 2026-05-11): UNIT_FILES + CONTRACT_FILES are now
# directory globs. Pre-12.2 they were enumerated file lists at tests/
# root — relocated into tests/unit/ and tests/contracts/ so the tier
# is visible from the tree (Hypothesis cover/nocover pattern).
UNIT_FILES := tests/unit

# `tests/contracts` is a directory — passing a dir to pytest collects
# the whole subtree (Pillar 1 + Pillar 2 + future contract tests) so
# new entries land in the contract tier automatically.
CONTRACT_FILES := tests/contracts

# Integration ignores = unit + contract + equivalence + research + the
# Pillar-3-unmasked breakeven_wr fixture bug (tracked separately as
# 86b9vfn5r — remove that ignore when the fixture lands). The research
# ignore (Phase-0 CT-MDP falsifications: F0.1 2026-05-20, F0.4 2026-05-21,
# F0.5 2026-05-21, ...) keeps the falsification scaffolds with intentional
# NotImplementedError stubs out of the deploy-blocking integration tier;
# operator runs them via `make test-research`.
INTEGRATION_IGNORES := \
	$(addprefix --ignore=,$(UNIT_FILES) $(CONTRACT_FILES)) \
	--ignore=tests/equivalence \
	--ignore=tests/research \
	--ignore=tests/integration/test_calmlp_sigma_winsorize.py

# Ticket 86b9vgh1a — cross-platform exclusive-lock guard.
#
# `make test-mutmut` mutates bot/engines/{volatility,probability}.py
# in-place during the ~1-2h baseline. Concurrent `make test-equivalence`
# or `make test-integration` reading those files mid-flight will see
# mutated source and surface false failures — a real bug class, not
# theoretical (the prior advice was a tests/CLAUDE.md prose warning,
# which is honor-system).
#
# Linux ships `flock(1)` in /usr/bin; macOS (the dev box) does NOT.
# A `flock --nonblock --exclusive ...` recipe would silently no-op
# on darwin. The fcntl module is in Python's stdlib on both platforms,
# so a tiny Python wrapper closes the cross-platform gap with no new
# native dependencies. See scripts/ops/_mutmut_lock.py for the
# fcntl.flock(LOCK_EX | LOCK_NB) implementation + behavior contract.
#
# Lockfile lives at the repo root (gitignored — `.mutmut.lock` entry
# in .gitignore). The wrapper opens it with O_CREAT so the file
# appears on first invocation; fcntl locks the FD, not the path, so
# the lockfile contents are irrelevant.
MUTMUT_LOCK := .mutmut.lock
MUTMUT_GUARD := $(PYTHON) scripts/ops/_mutmut_lock.py acquire $(MUTMUT_LOCK) --

help:
	@echo "Kalshi-bot dev targets (Pillar 5 tiered suite — 86b9ve11y)"
	@echo
	@echo "Tiered tests (run in this order, each ~10x prior):"
	@echo "  make test-unit        pure invariants, no DB/network    (<10s)"
	@echo "  make test-contract    public_api + import-linter + AST  (<5s)"
	@echo "  make test-equivalence Pillar 3 engine snapshots         (<30s)"
	@echo "  make test-integration full integration tier (alias for shard-0 + shard-1)"
	@echo "  make test-integration-shard-0 first half of integration, xdist (<30s)"
	@echo "  make test-integration-shard-1 second half of integration, xdist (<30s)"
	@echo "  make test-integration-serial @serial-marked timing-sensitive tests (<20s)"
	@echo "  make test-research    Phase-0 falsification spikes (not deploy-blocking)"
	@echo "  make test             all tiers + serial, fail-fast    (<2min)"
	@echo
	@echo "Incremental:"
	@echo "  make test-affected    testmon-driven, only changed-touch (<5s typical)"
	@echo "  make test-changed     alias for test-affected"
	@echo "  make test-fast        alias for test-unit (legacy name)"
	@echo
	@echo "Mutation testing (one-time baseline; ~1-2h on Mac):"
	@echo "  make test-mutmut      mutmut run on bot/engines/{volatility,probability}.py"
	@echo
	@echo "Other:"
	@echo "  make install              pip install -e .[dev]"
	@echo "  make install-hooks        symlink scripts/git_hooks/pre-commit → .git/hooks/ (Sprint PSC P5.3)"
	@echo "  make ast-check            syntax-check bot/constants.py + main_loop.py + scanner/"
	@echo "  make lint                 ruff check ."
	@echo "  make doc-drift            scripts/audit/doc_drift_check.py"
	@echo "  make deploy-check         pre-deploy aggregator"
	@echo "  make api-snapshot-regen   regenerate Pillar 1 public_api.json"
	@echo "  make refresh-map          regenerate agent_docs/repository_map.md (Bit 13.3 nav aid)"
	@echo
	@echo "Operator audits (Bit 11.3 — wraps --db /tmp/state.db):"
	@echo "  make data-health          scripts/audit/data_health_monitor.py --verbose"
	@echo "  make alpha-audit          scripts/audit/alpha_audit.py --days 14"
	@echo "  make 15m-audit            scripts/audit/15m_live_audit.py --regime auto"
	@echo "  make hourly-audit         scripts/audit/hourly_shadow_audit.py --regime auto"
	@echo "  make 15m-alpha            scripts/audit/15m_alpha_research.py --regime auto"
	@echo "  make no-side              scripts/audit/no_side_status.py"
	@echo "  make skill-smoke          end-to-end smoke of the 6 wrappers (Bit 11.1b)"
	@echo
	@echo "Pre-commit composite gate (Bit 12.4 — chains existing checks):"
	@echo "  make pre-commit-checks    ast-check + lint + doc-drift + test-unit + test-contract (<30s)"

install:
	$(PYTHON) -m pip install -e '.[dev]'

# Sprint PSC Bit P5.3 — install the parallel-session-coordination
# pre-commit hook. Idempotent: rerunning replaces the existing symlink.
#
# R1 M2 — Hook chaining. If an EXISTING non-symlink pre-commit hook is
# present (e.g. the historic 488-byte ast-check shell hook on the main
# checkout), it is preserved by renaming to `pre-commit.local` before
# installing the P5.3 symlink. The P5.3 hook then invokes
# `pre-commit.local` first; nonzero exit from .local propagates
# directly. This avoids regressing the operator's prior workflow when
# P5.3 ships.
#
# Worktree note: git stores hooks at $(git rev-parse --git-common-dir)/hooks,
# i.e. the MAIN checkout's `.git/hooks/` is shared across all worktrees.
# Installing once from any worktree covers every worktree.
#
# The hook itself is fail-open by design — see scripts/git_hooks/pre-commit
# docstring. Worst case (broken hook) it allows commits; never blocks.
install-hooks:
	@hooks_dir=$$(git rev-parse --git-path hooks 2>/dev/null); \
	if [ -z "$$hooks_dir" ]; then \
		echo "ERROR: not in a git repo. Run from inside the kalshi-bot checkout."; \
		exit 1; \
	fi; \
	mkdir -p "$$hooks_dir"; \
	dst="$$hooks_dir/pre-commit"; \
	chained="$$hooks_dir/pre-commit.local"; \
	if [ -e "$$dst" ] && [ ! -L "$$dst" ]; then \
		if [ -e "$$chained" ]; then \
			echo "ERROR: $$dst is a non-symlink AND $$chained already exists."; \
			echo "Refusing to clobber either. Resolve manually:"; \
			echo "  - if $$chained is yours, move it aside"; \
			echo "  - if $$dst is what you want chained, mv it onto $$chained"; \
			exit 1; \
		fi; \
		echo "Existing non-symlink pre-commit hook detected at $$dst."; \
		echo "Preserving as $$chained (will be invoked by P5.3 hook before Part A/B)."; \
		mv "$$dst" "$$chained"; \
		chmod +x "$$chained"; \
	fi; \
	src=$$(pwd)/scripts/git_hooks/pre-commit; \
	chmod +x "$$src"; \
	ln -sf "$$src" "$$dst"; \
	echo "Installed $$src as $$dst"; \
	if [ -e "$$chained" ]; then \
		echo "Chained pre-commit.local: $$chained"; \
	fi; \
	echo "Verify with: scripts/git_hooks/pre-commit --self-test"

# Pillar 5: the canonical entrypoint. Each tier's failure aborts the
# next via Make's default "fail on nonzero" (no `-` prefix anywhere).
# Mirrors CI structure in .github/workflows/test.yml — keep them in
# lockstep so a green local `make test` doesn't surprise a red CI run.
test:
	$(MAKE) test-unit
	$(MAKE) test-contract
	$(MAKE) test-equivalence
	$(MAKE) test-integration-shard-0
	$(MAKE) test-integration-shard-1
	$(MAKE) test-integration-serial

# Tier 1: unit. Pure-Python invariants (pyproject parsing, Makefile
# parsing, repo hygiene). Sub-second. Run on every save.
#
# `-m "not fragile"` is defensive: no fragile-marked tests live in
# UNIT_FILES today, but the marker is the cross-cutting "informational"
# signal in this repo (`pyproject.toml [tool.pytest.ini_options].markers`),
# and a future addition of a fragile-marked test to a unit file
# shouldn't silently start blocking deploys. Symmetric with the
# contract / equivalence / integration recipes below — every blocking
# tier excludes fragile.
test-unit:
	$(PYTHON) -m pytest -m "not fragile" $(UNIT_FILES)

# Tier 2: contract. Two parts, each in its own target (ticket
# 86b9vgh3t) so CI can surface them as distinct steps. Pre-split,
# both halves lived in one recipe — a red contract step left the
# operator guessing which half broke. After 86b9vgh3t, CI's
# `Contract tier — pytest` step runs `make test-contract-pytest`
# and `Contract tier — import-linter` runs `make test-contract-lint`,
# so a red status maps unambiguously to one half.
#
#   1. test-contract-pytest — public_api snapshot, AST guards,
#      extraction tests.
#   2. test-contract-lint   — Pillar 2 layering contracts via
#      `lint-imports` (.importlinter).
#
# `make test-contract` remains the orchestrator that runs both via
# $(MAKE) so each sub-target's failure aborts the next via Make's
# default fail-on-nonzero. Pytest first because it's the louder
# failure (more lines of output to read).
#
# `-m "not fragile"` matters in test-contract-pytest:
# tests/contracts/test_decided_contract.py (in CONTRACT_FILES) ships 7
# @pytest.mark.fragile tests. Without this filter, a fragile-test
# flake would block deploys via deploy.yml's blocking contract step
# (R1 C1 fix, preserved across the split).
test-contract-pytest:
	$(PYTHON) -m pytest -m "not fragile" $(CONTRACT_FILES)

test-contract-lint:
	$(LINT_IMPORTS)

test-contract:
	$(MAKE) test-contract-pytest
	$(MAKE) test-contract-lint

# Tier 3: equivalence. Pillar 3 numeric snapshots (volatility +
# probability engines). ~3s actual; <30s budget gives Bit 6.3+
# headroom for the calibrator oracle.
#
# `-m "not fragile"` is defensive (no fragile tests under
# tests/equivalence/ today; same reasoning as test-unit).
#
# Ticket 86b9vgh1a — guarded by $(MUTMUT_GUARD) to fail-fast if a
# `make test-mutmut` is already running. Pre-guard, a parallel
# mutmut would mutate bot/engines/ mid-run and silently corrupt the
# numeric-snapshot comparison.
test-equivalence:
	$(MUTMUT_GUARD) $(PYTHON) -m pytest -m "not fragile" tests/equivalence/

# Tier 6: research (paired with scripts/research/). Falsification spikes +
# cross-system research. NOT part of test-integration (integration ignores
# tests/research/ via INTEGRATION_IGNORES) — research tests may carry
# intentional NotImplementedError stubs during the scaffold-first
# extraction-Bit phase per CLAUDE.md TDD-first discipline. Operator runs
# this target during Phase 0 falsification work; not deploy-blocking.
test-research:
	$(MUTMUT_GUARD) $(PYTHON) -m pytest -m "not fragile and not serial" tests/research/

# Tier 4: integration. Everything else. Mirrors the historical
# `make test` semantics minus the tiers above.
#
# Ticket 86b9vgh1a — same guard as test-equivalence: parallel
# mutmut would corrupt the broad integration run via in-place
# mutation of bot/engines/.
#
# Bit-5 (CI perf umbrella 86b9zjtzk): pytest-xdist parallelizes across
# workers. `--dist=loadfile` keeps all tests in one file on the same
# worker so module-scoped fixtures + intra-file shared state stay
# coherent. Tests marked `@pytest.mark.serial` (timing-sensitive
# subprocess/threading-Barrier/SIGALRM tests with tight wall-clock
# buffers) run in a separate single-worker pass via
# test-integration-serial.
#
# Bit-9 (CI perf umbrella 86b9zjtzk, 2026-05-17): the integration tier
# is further split into TWO hash-balanced shards via pytest-shard's
# `--shard-id=N --num-shards=2` flags. Each shard runs ~half the test
# corpus on its own concurrent GH job, halving the integration-tier
# wall time. `test-integration` retained as an alias that runs both
# shards sequentially (operator convenience for local-mac dev — CI
# uses the per-shard targets directly).
test-integration: test-integration-shard-0 test-integration-shard-1

test-integration-shard-0:
	$(MUTMUT_GUARD) $(PYTHON) -m pytest tests/ -m "not fragile and not serial" -n auto --dist=loadfile --shard-id=0 --num-shards=2 $(INTEGRATION_IGNORES)

test-integration-shard-1:
	$(MUTMUT_GUARD) $(PYTHON) -m pytest tests/ -m "not fragile and not serial" -n auto --dist=loadfile --shard-id=1 --num-shards=2 $(INTEGRATION_IGNORES)

test-integration-serial:
	$(MUTMUT_GUARD) $(PYTHON) -m pytest tests/ -m "serial" $(INTEGRATION_IGNORES)

# testmon-driven incremental run. First invocation seeds .testmondata
# with a full pass (slow); subsequent invocations re-run only tests
# touching code that changed since. Cache lives in `.testmondata`
# (gitignored — per-machine state).
#
# `--ignore=tests/equivalence` because Pillar-3 snapshot regen-detection
# clashes with testmon's "skip unchanged" semantics (a snapshot YAML
# diff is real signal that testmon would silently skip if no .py file
# in the equivalence dir changed). Equivalence stays in its own tier
# and runs as a dedicated CI step.
test-affected:
	$(PYTHON) -m pytest --testmon -m "not fragile" --ignore=tests/equivalence --ignore=tests/research --ignore=tests/integration/test_calmlp_sigma_winsorize.py

# Alias for the user's preferred name (per Pillar 5 remote-control
# spec). Both names hit the same recipe so either docs / muscle memory
# work.
test-changed: test-affected

# Legacy alias — Bit 1.2 shipped `test-fast` as the curated 8-file
# dev-tooling invariant set (== UNIT_FILES); Pillar 4 added
# tests/unit/test_tdd_guard_hook.py to the unit tier (folded into
# UNIT_FILES on rebase). Kept as `test-unit` alias so existing
# scripts / docs / hooks that call `make test-fast` don't break.
test-fast: test-unit

# Pillar 5: one-time mutation-survival baseline. Reads [tool.mutmut]
# from pyproject.toml. Long-running (~1-2h on Mac); typically launched
# in the background:
#   make test-mutmut > /tmp/mutmut.log 2>&1 &
# Output goes to `mutants/` (gitignored). Surface the tally to
# kb/findings/mutmut-baseline-mayDD.md per ticket AC.
#
# Ticket 86b9vgh1a — guarded by $(MUTMUT_GUARD). The guard ALSO
# fires here (not just on the reader tiers): if an operator typos a
# second `make test-mutmut` while the first is still running, both
# would race on the in-place mutation of bot/engines/ — the same
# corruption mode, just author-vs-author instead of author-vs-reader.
test-mutmut:
	$(MUTMUT_GUARD) mutmut run

# Bit 9.3-iii.c (2026-05-11): bot/_impl.py was DELETED. Syntax-check before
# any push that touches the bot/ runtime hotspot files: bot/constants.py
# (module-level constants — Bit 3.1) + bot/main_loop.py (MainLoop body —
# Bit 9.3) + bot/scanner/__init__.py (scanner body — Bit 8.1). A syntax
# error in any of these crashes bot start. Mirrors deploy_check.sh gate 1.
ast-check:
	$(PYTHON) -c "import ast; ast.parse(open('bot/constants.py').read()); ast.parse(open('bot/main_loop.py').read()); ast.parse(open('bot/scanner/__init__.py').read())"

# Per Bit 1.1, ruff config is lenient (E+F only). `ruff check .`
# reports ~1069 errors as of Bit 1.1 ship (the count drifts as the repo
# evolves) and `--fix` is a no-op (`fixable=[]` in pyproject) so this
# target cannot silently mutate bot/_impl.py.
lint:
	$(RUFF) check .

doc-drift:
	$(PYTHON) scripts/audit/doc_drift_check.py

# scripts/cal_mlp/deploy_check.sh is the canonical pre-deploy aggregator
# (gates: ast.parse bot/constants.py + bot/main_loop.py + bot/scanner/__init__.py
# + cal_mlp modules, cal_mlp invariants, full pytest suite, smoke_check —
# Bit 9.3-iii.c (2026-05-11) deleted bot/_impl.py; the runtime hotspots are
# now the canonical submodules). Don't duplicate gates here — single
# source of truth.
deploy-check:
	bash scripts/cal_mlp/deploy_check.sh

# Pillar 1 of testing-foundation-sprint: regenerate the public API
# contract snapshot after an intentional surface change. Consumed by
# tests/contracts/test_public_api_snapshot.py. See parent ticket
# 86b9ve0wa. Requires griffe (in dev extras) — `make install` first if
# not already installed.
api-snapshot-regen:
	$(PYTHON) scripts/audit/dump_public_api.py

# Bit 13.3 (Sprint 13, 2026-05-11) — auto-regen the navigation-aid
# repository map at agent_docs/repository_map.md. Walks bot/ via AST,
# extracts top-level classes + public functions + LOC per module.
# Complementary to tests/contracts/public_api.json (Pillar 1) which is
# the public-surface SNAPSHOT contract; this is the NAVIGATION map for
# agent sessions. Run manually whenever the bot/ structure changes
# (extractions, new modules, etc.) — not in CI, not in pre-commit
# (output is local-only convention, agent_docs/ is kept tracked but
# regen is human-driven per `Don't write tests unsolicited` discipline).
# Contract pin: tests/unit/test_makefile.py::test_bit_13_3_refresh_map_target.
refresh-map:
	$(PYTHON) scripts/ops/refresh_repo_map.py

# ─────────────────────────────────────────────────────────────────────
# Bit 11.3 (Sprint 11, 2026-05-11) — operator-convenience wrappers
# ─────────────────────────────────────────────────────────────────────
# Wraps the most-frequently-skill-referenced audit + alpha-research
# scripts with the canonical `--db /tmp/state.db` operator argument.
# Each target matches the corresponding `/<skill>` skill's primary
# invocation in .claude/skills/*/SKILL.md (search: `python3 scripts/`).
# Custom-arg invocations stay as direct `python3 scripts/...` —
# Make's positional-arg passing is awkward and the skill docs already
# document the full surface. Six narrow wrappers ship in this Bit;
# wider script-set (`maker-cost`, `weekend-discount`, `spx-audit`,
# `weather-audit`, `sports-audit`, `hourly-alpha`, `spx-alpha`,
# `weather-alpha`, `sports-alpha`, `calibrator-health`,
# `quiet-monitor`) deferred to follow-up Bit. Contract pin:
# tests/unit/test_makefile.py::test_bit_11_3_targets_point_to_real_scripts.

data-health:
	$(PYTHON) scripts/audit/data_health_monitor.py --db /tmp/state.db --verbose

alpha-audit:
	$(PYTHON) scripts/audit/alpha_audit.py --db /tmp/state.db --days 14

15m-audit:
	$(PYTHON) scripts/audit/15m_live_audit.py --db /tmp/state.db --regime auto

hourly-audit:
	$(PYTHON) scripts/audit/hourly_shadow_audit.py --db /tmp/state.db --regime auto

15m-alpha:
	$(PYTHON) scripts/audit/15m_alpha_research.py --db /tmp/state.db --regime auto

no-side:
	$(PYTHON) scripts/audit/no_side_status.py --db /tmp/state.db

# Bit 12.4 (Sprint 12, 2026-05-11) — composed pre-commit gate.
# Per master plan §Bit 12.4: "ast-parse, doc-drift, iCloud-dup
# detector, ruff, import-linter." All five checks exist as
# Makefile / pytest targets:
#   - ast-parse        → make ast-check (existing)
#   - ruff             → make lint (existing)
#   - doc-drift        → make doc-drift (existing,
#                        scripts/audit/doc_drift_check.py — walks
#                        SOURCE_FILES × DOC_FILES for config-constant
#                        ↔ README/whitepaper/CLAUDE.md consistency)
#   - iCloud-dup       → tests/unit/test_repo_hygiene.py::test_no_icloud_*
#                        (collected by `make test-unit`)
#   - import-linter    → make test-contract (lint-imports + AST guards)
# This target chains the five cheap-tier checks (<30s wall-clock total)
# so an operator can run a single command before `git commit`. The
# Sprint PSC P5.3 git hook stays narrow-focused on parallel-session
# coordination — operators who want the broader gate run this manually
# (or wire it into their own `.git/hooks/pre-commit.local`).
#
# Order is significant: ast-check is fastest (~0.5s) and the most
# common failure mode (syntax error in bot/constants.py / bot/main_loop.py /
# bot/scanner/__init__.py — the runtime hotspots ast-check scans post-Bit-9.3-iii.c);
# lint is next (~5s); doc-drift (~2s); test-unit is invariant tests
# <10s; test-contract is AST + lint-imports <15s on Mac. Fail-fast on
# the cheapest gate.
# Contract pin: tests/unit/test_makefile.py::test_bit_12_4_pre_commit_checks_*.
pre-commit-checks: ast-check lint doc-drift test-unit test-contract
	@echo "✓ pre-commit-checks (ast-check + lint + doc-drift + test-unit + test-contract)"

# Bit 11.1b (Sprint 11, 2026-05-11) — end-to-end smoke for the 6 Bit-11.3
# wrappers. Each wrapper invoked with a 60s `perl alarm` timeout (macOS
# has no `timeout(1)` by default). Exit-code policy:
#   0  — clean run
#   1  — data-health flagged WARN-only findings (scripts/audit/data_health_monitor.py:569
#        `sys.exit(1)` when WARNINGS exist but no critical). NOT a wrapper
#        bug; the script's documented exit code for "warnings only".
#   2  — data-health flagged critical findings (scripts/audit/data_health_monitor.py:567
#        `sys.exit(2)`). NOT a wrapper bug; the script's documented exit code.
#   127 / timeout (142/124) — wrapper broken; smoke fails.
#   any other non-zero — script crash; smoke fails.
# data-health's three-level exit code (0/1/2 = clean/warn/crit) is the
# documented contract per scripts/audit/data_health_monitor.py:565 comment.
# Other wrappers (alpha-audit / 15m-audit / hourly-audit / 15m-alpha /
# no-side) exit 0 on success and non-zero on script crash — they don't
# have a WARN-tier exit code, so the `1)` case fires ONLY for data-health
# WARN scenarios in practice. Trade-off accepted: a real crash of one of
# the non-data-health wrappers would exit 1 and be misclassified as
# WARN-pass; the alternative (per-wrapper exit-code policy) is too brittle.
# Findings + audit ship doc: kb/findings/skill-audit-may11-bit-11.1b.md.
# Test pin: tests/unit/test_makefile.py::test_bit_11_1b_skill_smoke_target_exit_code_policy.
skill-smoke:
	@for t in data-health alpha-audit 15m-audit hourly-audit 15m-alpha no-side; do \
		log=/tmp/skill_smoke_$$t.log; \
		echo "→ make $$t"; \
		perl -e 'alarm 60; exec @ARGV' $(MAKE) $$t > $$log 2>&1 && rc=0 || rc=$$?; \
		case "$$rc" in \
			0) echo "  ✓ exit=0 (clean)";; \
			1) echo "  ✓ exit=1 (WARN-only findings — see $$log; data-health-only)";; \
			2) echo "  ✓ exit=2 (CRIT findings — see $$log; data-health-only)";; \
			142|124) echo "  ✗ TIMEOUT (see $$log)"; exit 1;; \
			127) echo "  ✗ COMMAND NOT FOUND (Makefile target missing?)"; exit 1;; \
			*) echo "  ✗ exit=$$rc (script crash — see $$log)"; exit 1;; \
		esac; \
	done; \
	echo "✓ skill-smoke (6 Bit-11.3 wrappers)"
# R3 adversarial fix 2026-05-11: removed `set -e` and replaced `; rc=$$?`
# with `&& rc=0 || rc=$$?` to capture perl-exec's exit code WITHOUT
# triggering early-abort under `set -e`. Pre-fix recipe failed on first
# real use: `make data-health` exits 2 on CRIT findings (the case this
# Bit is designed for), but `set -e` aborted before the case-block read
# `rc`. Reproduced via R3 review — `make: *** [skill-smoke] Error 2`
# instead of `✓ exit=2 (CRIT findings — see ...)`. The textual test pin
# (test_bit_11_1b_skill_smoke_target_exit_code_policy) checked recipe
# SOURCE for `1)` / `2)` cases but did not exercise runtime; R3 added a
# runtime smoke pin to close that gap.
