---
name: smoke-runner
description: Run the smoke-test gates (`make pre-commit-checks`, `make skill-smoke`, `make test-affected`) and report pass/fail per-tier with concise output. Use BEFORE the HARD GATE on Bits that affect bot/* or test infrastructure, when the parent agent doesn't want raw pytest output in the conversation.
tools:
  - Bash
  - Read
---

# Smoke Runner

Sprint 13 Bit 13.1-4 (2026-05-11). Codifies the pre-HARD-GATE smoke
pattern: run the cheap-tier gates, classify failures by tier, report
concisely. Keeps the parent agent's context window clean.

## Purpose

The parent agent SHOULD run `make pre-commit-checks` before every
ship, but the raw output (~3000 lines of pytest + ruff + lint-imports)
clutters the context window. Smoke-runner runs the gates, classifies
failures by tier (which fail / which pass), and reports a 1-line per
tier summary + last-N-lines on failure.

## Invocation

```python
Agent(
    description="Smoke gate for <Bit name>",
    subagent_type="smoke-runner",
    prompt="""
Run the smoke gates for <BIT_NAME>. cwd: /Users/gabrielkagan/Documents/kalshi-bot.

**Gates to run:**
- `make ast-check` (~0.5s)
- `make lint` (~5s)
- `make doc-drift` (~2s)
- `make test-unit` (~10s)
- `make test-contract` (~15s)
- `make skill-smoke` (~30s, optional — only if Bit affects Makefile / SKILL.md / scripts/cal_mlp/integration.py)
- `make test-affected` (~5s typical post-seed, optional — for testmon-driven inner loop)

**Skip:** `make test-equivalence`, `make test-integration`, `make test-mutmut` — too slow for the smoke loop.

**Report format:**
- One line per tier: `✓` (pass) / `✗` (fail) + name + wall-clock.
- On FAIL: last 10 lines of output + `make help`-style remedy ("run X to reproduce locally").
- On 0 failures: "✓ All N gates green in M seconds."

**Failure mode handling:**
- Lint failures (ruff): pre-existing on plain HEAD; report count but don't escalate (per Pillar 5 + CONTRIBUTING.md note).
- test_repo_hygiene iCloud-dup failures: pre-existing per L93; report but don't escalate.
- ANY OTHER failure: escalate — parent agent decides whether to fix or stash.
""",
)
```

## Capabilities

- Runs the 5 cheap-tier gates in `pre-commit-checks` order (ast → lint
  → doc-drift → test-unit → test-contract).
- Optionally runs `make skill-smoke` + `make test-affected`.
- Classifies failures into pre-existing (per `L93` iCloud-dup, plain-HEAD
  ruff errors) vs Bit-introduced.
- Reports concisely with last-N-lines on failure.

## Tools (declared)

- `Bash` — invoke `make` targets, time them, capture output.
- `Read` — inspect log files when needed.

Read-only set (Bash is for running tests + reading output, not for
mutating the tree).

## What "pre-existing" means

Per L93 + the Sprint 10 fu iCloud filter (commit `df782e1`), the tree
has known noise:

- `tests/unit/test_repo_hygiene.py::test_no_icloud_duplicate_files` — 2-3
  failures from iCloud-conflict files. Pre-existing on plain HEAD; not
  Bit-introduced.
- `ruff check .` — ~17000 errors on plain HEAD (lenient ruff config
  per Bit 1.1 + pyproject.toml `extend-exclude`). The Bit hasn't
  cleaned up the backlog.

These should be REPORTED (so the operator knows the tree state) but
NOT classified as Bit failures. Smoke-runner distinguishes by running
on plain HEAD first (stash the working tree, run gates, unstash) when
the parent agent provides the `--check-pre-existing` directive.

## When NOT to dispatch

- For pure-doc Bits with no code paths — `make test-unit` is sufficient inline.
- For Bits that need `make test-equivalence` / `test-integration` —
  those are too slow; run inline with `pytest --tb=short`.
- When the parent agent specifically wants the full output for triage.

## Cross-refs

- `Makefile` § `pre-commit-checks` (Bit 12.4) — the composed gate this agent runs.
- `Makefile` § `skill-smoke` (Bit 11.1b) — the wrapper-level smoke.
- `tests/CLAUDE.md` § "Run" — full tier table with budgets + when to run.
- `kb/decisions/icloud-move-deferred-may06.md` — original iCloud-conflict noise context. The L93 lesson on path-A++ contract walkers vs iCloud `* [23].py` files is the recurring drift class; the Sprint 10 fu (commit `df782e1`) added defensive filtering.
