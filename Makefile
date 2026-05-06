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
# that looks up `bot.py` / `scripts/` relative to the wrong dir and
# silently misbehaves. Fail loudly at parse time with a clear remedy.
ifeq ($(wildcard pyproject.toml),)
$(error Makefile must be invoked from the repo root (where pyproject.toml lives); current dir is $(CURDIR))
endif

.PHONY: help install test test-fast ast-check lint doc-drift deploy-check

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

help:
	@echo "Kalshi-bot dev targets (Bit 1.2 of modularization plan)"
	@echo
	@echo "  make install       pip install -e .[dev]  (brings in pytest + ruff + tomli)"
	@echo "  make test          full suite, blocking subset (matches CI: -m 'not fragile')"
	@echo "  make test-fast     dev-tooling invariant tests (~1s)"
	@echo "  make ast-check     syntax-check bot.py (CLAUDE.md sacred-file rule)"
	@echo "  make lint          ruff check ."
	@echo "  make doc-drift     scripts/doc_drift_check.py"
	@echo "  make deploy-check  scripts/cal_mlp/deploy_check.sh (full pre-deploy aggregator)"

install:
	$(PYTHON) -m pip install -e '.[dev]'

# Mirrors .github/workflows/test.yml blocking step. The `fragile` marker
# is how informational tests opt out of CI gating per pyproject.toml —
# keep this filter symmetric with CI so a green local `make test`
# doesn't surprise a red CI run.
test:
	$(PYTHON) -m pytest tests/ -m "not fragile"

# Curated list of dev-tooling invariant tests (sub-second). Catches the
# common "I broke pyproject / Makefile / repo hygiene" class. The
# `smoke` marker exists in pyproject but has zero @pytest.mark.smoke
# usages today; running an explicit file list is more honest than
# `-m smoke` collecting nothing.
test-fast:
	$(PYTHON) -m pytest tests/test_pyproject.py tests/test_repo_hygiene.py tests/test_makefile.py tests/test_agents_md_symlink.py

# bot.py is sacred per CLAUDE.md. Syntax-check before any push that
# touches it. Mirrors deploy_check.sh gate 1.
#
# Sprint-2 hand-off note: this recipe and the help-banner line above
# both hardcode the literal `bot.py`. When Bit 2.1 creates
# `bot/__init__.py` (and Bit 1.1's
# `test_bot_module_and_bot_package_dont_collide` forces `bot.py` to
# be deleted in the SAME commit), update BOTH this recipe AND the
# help-banner description to point at the new entry — likely
# `import bot` plus a recursive parse of `bot/*.py`.
#
# What the tests catch (and don't):
#   - `test_ast_check_targets_bot_py` pins the literal "bot.py" + "ast.parse"
#     in this recipe. It fails ONLY when the recipe text is rewritten to
#     no longer contain those literals — useful as a forward-direction
#     guard against an accidental edit, but it does NOT verify that
#     `bot.py` exists on disk.
#   - `test_help_lists_all_targets` only verifies the target NAME
#     (`make ast-check`) appears in the help banner; it does NOT pin
#     the description text "syntax-check bot.py".
# So if Bit 2.1 deletes `bot.py` from disk but leaves this recipe
# untouched, no test fires — `make ast-check` would raise
# `FileNotFoundError` at runtime only. Run `make ast-check` manually
# in the Bit 2.1 commit before pushing.
ast-check:
	$(PYTHON) -c "import ast; ast.parse(open('bot.py').read())"

# Per Bit 1.1, ruff config is lenient (E+F only). `ruff check .`
# reports ~1069 errors as of Bit 1.1 ship (the count drifts as the repo
# evolves) and `--fix` is a no-op (`fixable=[]` in pyproject) so this
# target cannot silently mutate bot.py.
lint:
	$(RUFF) check .

doc-drift:
	$(PYTHON) scripts/doc_drift_check.py

# scripts/cal_mlp/deploy_check.sh is the canonical pre-deploy aggregator
# (gates: ast.parse bot.py + cal_mlp modules, cal_mlp invariants, full
# pytest suite, smoke_check). Don't duplicate gates here — single
# source of truth.
deploy-check:
	bash scripts/cal_mlp/deploy_check.sh
