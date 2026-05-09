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

.PHONY: help install test test-unit test-contract test-equivalence test-integration test-affected test-changed test-fast test-mutmut ast-check lint doc-drift deploy-check api-snapshot-regen

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
	@echo "  make ast-check            syntax-check bot/_impl.py + bot/constants.py"
	@echo "  make lint                 ruff check ."
	@echo "  make doc-drift            scripts/doc_drift_check.py"
	@echo "  make deploy-check         pre-deploy aggregator"
	@echo "  make api-snapshot-regen   regenerate Pillar 1 public_api.json"

install:
	$(PYTHON) -m pip install -e '.[dev]'

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

# Tier 2: contract. Two parts:
#   1. Pytest suite — public_api snapshot, AST guards, extraction tests.
#   2. import-linter CLI — layering contracts (`.importlinter`).
# Both must pass; pytest first because it's the louder failure.
#
# `-m "not fragile"` matters here: tests/test_decided_contract.py
# (in CONTRACT_FILES) ships 7 @pytest.mark.fragile tests. Without this
# filter, a fragile-test flake would block deploys via deploy.yml's
# blocking contract step (R1 C1 fix).
test-contract:
	$(PYTHON) -m pytest -m "not fragile" $(CONTRACT_FILES)
	$(LINT_IMPORTS)

# Tier 3: equivalence. Pillar 3 numeric snapshots (volatility +
# probability engines). ~3s actual; <30s budget gives Bit 6.3+
# headroom for the calibrator oracle.
#
# `-m "not fragile"` is defensive (no fragile tests under
# tests/equivalence/ today; same reasoning as test-unit).
test-equivalence:
	$(PYTHON) -m pytest -m "not fragile" tests/equivalence/

# Tier 4: integration. Everything else. Mirrors the historical
# `make test` semantics minus the tiers above.
test-integration:
	$(PYTHON) -m pytest tests/ -m "not fragile" $(INTEGRATION_IGNORES)

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
test-mutmut:
	mutmut run

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
