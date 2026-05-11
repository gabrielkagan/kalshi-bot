# Bit 1.2 of repo modularization plan
# (kb/decisions/repo-modularization-plan-may05.md).
# Sprint 1: developer-convenience targets only — no runtime impact, no
# code moves. Targets call existing scripts where they exist
# (scripts/cal_mlp/deploy_check.sh, scripts/doc_drift_check.py) rather
# than reinventing.
#
# Make's "missing separator" error on a tab/space mistake is opaque, so
# all recipe lines below MUST be indented with a literal TAB.
# tests/test_makefile.py pins this contract so an editor-on-save
# expand-tab can't silently break the file.
#
# Tested with GNU Make 3.81 (macOS default) and 4.x (Linux). No Make
# 4-only features used; stay 3.81-compatible until VPS/CI reasons emerge.

.DEFAULT_GOAL := help

# CWD guard — Make resolves recipe paths against `$(CURDIR)`, not the
# Makefile's location. A contributor who `cd tests/ && make test` would
# otherwise get either "No rule to make target" or, worse, a recipe
# that looks up `bot/_impl.py` / `scripts/` relative to the wrong dir and
# silently misbehaves. Fail loudly at parse time with a clear remedy.
ifeq ($(wildcard pyproject.toml),)
$(error Makefile must be invoked from the repo root (where pyproject.toml lives); current dir is $(CURDIR))
endif

.PHONY: help install install-hooks test test-unit test-contract test-contract-pytest test-contract-lint test-equivalence test-integration test-affected test-changed test-fast test-mutmut ast-check lint doc-drift deploy-check api-snapshot-regen data-health alpha-audit 15m-audit hourly-audit 15m-alpha no-side

# Override at invocation time if needed: `make PYTHON=python3.11 test`.
# NOTE: CI runs Python 3.11 (.github/workflows/test.yml), local default
# is whatever `python3` resolves to (3.9.6 on the dev box). Predates
# Bit 1.2 — flag here so the asymmetry doesn't surprise anyone running
# `make test` locally and seeing CI behave differently.
PYTHON ?= python3

# Resolve ruff at parse time (`:=` not `?=` so the `$(shell ...)` runs
# exactly once, regardless of how many times the var is expanded).
# Prefer PATH, fall back to venv/bin/ruff — mirrors
# tests/test_pyproject.py::test_extend_exclude_actually_excludes. If
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
UNIT_FILES := \
	tests/test_pyproject.py \
	tests/test_repo_hygiene.py \
	tests/test_makefile.py \
	tests/test_agents_md_symlink.py \
	tests/test_claude_md_size.py \
	tests/test_no_root_test_files.py \
	tests/test_ops_systemd_unit_matches_repo.py \
	tests/test_post_deploy_scan_gate.py \
	tests/test_tdd_guard_hook.py

# `tests/contracts` is a directory — passing a dir to pytest collects
# the whole subtree (Pillar 1 + Pillar 2 + future contract tests) so
# new entries land in the contract tier automatically.
CONTRACT_FILES := \
	tests/contracts \
	tests/test_call_sites.py \
	tests/test_config_consistency.py \
	tests/test_constants_extraction.py \
	tests/test_db_signatures.py \
	tests/test_decided_contract.py \
	tests/test_engines_extraction.py \
	tests/test_feeds_extraction.py \
	tests/test_fetchers_extraction.py \
	tests/test_helpers_extraction.py \
	tests/test_kalshi_client_extraction.py \
	tests/test_logger_extraction.py \
	tests/test_notifier_extraction.py \
	tests/test_order_outcome_vocab.py

# Integration ignores = unit + contract + equivalence + the
# Pillar-3-unmasked breakeven_wr fixture bug (tracked separately as
# 86b9vfn5r — remove that ignore when the fixture lands).
INTEGRATION_IGNORES := \
	$(addprefix --ignore=,$(UNIT_FILES) $(CONTRACT_FILES)) \
	--ignore=tests/equivalence \
	--ignore=tests/test_calmlp_sigma_winsorize.py

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
# native dependencies. See scripts/_mutmut_lock.py for the
# fcntl.flock(LOCK_EX | LOCK_NB) implementation + behavior contract.
#
# Lockfile lives at the repo root (gitignored — `.mutmut.lock` entry
# in .gitignore). The wrapper opens it with O_CREAT so the file
# appears on first invocation; fcntl locks the FD, not the path, so
# the lockfile contents are irrelevant.
MUTMUT_LOCK := .mutmut.lock
MUTMUT_GUARD := $(PYTHON) scripts/_mutmut_lock.py acquire $(MUTMUT_LOCK) --

help:
	@echo "Kalshi-bot dev targets (Pillar 5 tiered suite — 86b9ve11y)"
	@echo
	@echo "Tiered tests (run in this order, each ~10x prior):"
	@echo "  make test-unit        pure invariants, no DB/network    (<10s)"
	@echo "  make test-contract    public_api + import-linter + AST  (<5s)"
	@echo "  make test-equivalence Pillar 3 engine snapshots         (<30s)"
	@echo "  make test-integration full suite minus the above        (<2min)"
	@echo "  make test             all tiers, fail-fast              (<3min)"
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
	@echo "  make ast-check            syntax-check bot/_impl.py + bot/constants.py"
	@echo "  make lint                 ruff check ."
	@echo "  make doc-drift            scripts/doc_drift_check.py"
	@echo "  make deploy-check         pre-deploy aggregator"
	@echo "  make api-snapshot-regen   regenerate Pillar 1 public_api.json"
	@echo
	@echo "Operator audits (Bit 11.3 — wraps --db /tmp/state.db):"
	@echo "  make data-health          scripts/data_health_monitor.py --verbose"
	@echo "  make alpha-audit          scripts/alpha_audit.py --days 14"
	@echo "  make 15m-audit            scripts/15m_live_audit.py --regime auto"
	@echo "  make hourly-audit         scripts/hourly_shadow_audit.py --regime auto"
	@echo "  make 15m-alpha            scripts/15m_alpha_research.py --regime auto"
	@echo "  make no-side              scripts/no_side_status.py"

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
	$(MAKE) test-integration

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
# tests/test_decided_contract.py (in CONTRACT_FILES) ships 7
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

# Tier 4: integration. Everything else. Mirrors the historical
# `make test` semantics minus the tiers above.
#
# Ticket 86b9vgh1a — same guard as test-equivalence: parallel
# mutmut would corrupt the broad integration run via in-place
# mutation of bot/engines/.
test-integration:
	$(MUTMUT_GUARD) $(PYTHON) -m pytest tests/ -m "not fragile" $(INTEGRATION_IGNORES)

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
	$(PYTHON) -m pytest --testmon -m "not fragile" --ignore=tests/equivalence --ignore=tests/test_calmlp_sigma_winsorize.py

# Alias for the user's preferred name (per Pillar 5 remote-control
# spec). Both names hit the same recipe so either docs / muscle memory
# work.
test-changed: test-affected

# Legacy alias — Bit 1.2 shipped `test-fast` as the curated 8-file
# dev-tooling invariant set (== UNIT_FILES); Pillar 4 added
# tests/test_tdd_guard_hook.py to the unit tier (folded into
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

# bot/_impl.py is the renamed `bot.py` (sacred per CLAUDE.md). Syntax-check
# before any push that touches it OR bot/constants.py (Bit 3.1: module-level
# constants live in bot/constants.py post-extraction; a syntax error there
# crashes bot start the same way an _impl.py error would). Mirrors
# deploy_check.sh gate 1.
ast-check:
	$(PYTHON) -c "import ast; ast.parse(open('bot/_impl.py').read()); ast.parse(open('bot/constants.py').read())"

# Per Bit 1.1, ruff config is lenient (E+F only). `ruff check .`
# reports ~1069 errors as of Bit 1.1 ship (the count drifts as the repo
# evolves) and `--fix` is a no-op (`fixable=[]` in pyproject) so this
# target cannot silently mutate bot/_impl.py.
lint:
	$(RUFF) check .

doc-drift:
	$(PYTHON) scripts/doc_drift_check.py

# scripts/cal_mlp/deploy_check.sh is the canonical pre-deploy aggregator
# (gates: ast.parse bot/_impl.py + cal_mlp modules, cal_mlp invariants, full
# pytest suite, smoke_check). Don't duplicate gates here — single
# source of truth.
deploy-check:
	bash scripts/cal_mlp/deploy_check.sh

# Pillar 1 of testing-foundation-sprint: regenerate the public API
# contract snapshot after an intentional surface change. Consumed by
# tests/contracts/test_public_api_snapshot.py. See parent ticket
# 86b9ve0wa. Requires griffe (in dev extras) — `make install` first if
# not already installed.
api-snapshot-regen:
	$(PYTHON) scripts/dump_public_api.py

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
# tests/test_makefile.py::test_bit_11_3_targets_point_to_real_scripts.

data-health:
	$(PYTHON) scripts/data_health_monitor.py --db /tmp/state.db --verbose

alpha-audit:
	$(PYTHON) scripts/alpha_audit.py --db /tmp/state.db --days 14

15m-audit:
	$(PYTHON) scripts/15m_live_audit.py --db /tmp/state.db --regime auto

hourly-audit:
	$(PYTHON) scripts/hourly_shadow_audit.py --db /tmp/state.db --regime auto

15m-alpha:
	$(PYTHON) scripts/15m_alpha_research.py --db /tmp/state.db --regime auto

no-side:
	$(PYTHON) scripts/no_side_status.py --db /tmp/state.db
