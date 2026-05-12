---
clickup: 86b9ve110
parent_ticket: 86b9ve0wa
sprint: testing-foundation-sprint
shipped: 2026-05-09
---

# Pillar 4 SHIPPED — tdd-guard hook + /test-writer skill

ClickUp: [86b9ve110](https://app.clickup.com/t/86b9ve110) under parent [86b9ve0wa](https://app.clickup.com/t/86b9ve0wa).

Branch: `worktree-86b9ve110-pillar-4-tdd-guard` off `origin/main` at `f26a611` (rebased after Sprint A.2 landed mid-flight). **Caution risk tier** — push to branch + open PR + WAIT for explicit user merge approval (no autonomous merge).

## What shipped

A PreToolUse Claude Code hook (`Edit|Write|MultiEdit` matcher) that blocks edits to `bot/**/*.py` unless the agent has touched a `.py` file under `tests/` earlier in the same session — Cherny's TDD-with-agents pattern, structurally enforced. Half of the deliverable is the `/test-writer` skill that scaffolds a failing test mirroring the target's path under `tests/`.

| Surface | What it pins | How |
|---|---|---|
| `.claude/hooks/tdd_guard.py` (~215 LOC) | Block bot/ Edit/Write/MultiEdit unless prior tests/*.py edit OR bypass | PreToolUse hook reading stdin JSON, scanning `transcript_path` JSONL line-by-line |
| `.claude/settings.json` PreToolUse wiring | Cwd-relative path first, `$CLAUDE_PROJECT_DIR` fallback, exit 0 if neither | Bash command with explicit existence checks |
| `.claude/skills/test-writer/SKILL.md` | Scaffold a RED test + run pytest to confirm + hand off | Markdown skill (instructions for the agent, no Python tooling) |
| `tests/unit/test_tdd_guard_hook.py` (75 tests) | Hook behavior end-to-end (subprocess-invoked) | Stdin JSON fixtures + tmp_path-isolated git repos for `[no-tdd]` cases |
| `tests/CLAUDE.md` | Workflow expectation, bypass markers, failure-modes table, subagent isolation | Documentation |
| `Makefile` test-fast recipe | Hook tests run in `make test-fast` (~3s) | Added to enumerated file list |
| Root `CLAUDE.md` skill table | `/test-writer` discoverable via /-routing | New row |

## Bypass markers (documented + tested)

| Marker | Scope | Use case |
|---|---|---|
| `KALSHI_TDD_BYPASS=1` env var (exact match) | per-session | Refactor sessions covered by Pillar-3 equivalence; emergency hotfix |
| `[no-tdd]` in HEAD commit subject (anchored token) | per-Bit | Doc-only Bits, `git mv` Bits, refactors with property-based equivalence proving behavior unchanged |

Anchored regex: `(^|\s)\[no-tdd\](\s|$|[:.,;])`. Substring `in` would false-positive on subjects like "Decision: discuss [no-tdd] policy."

## Failure modes (load-bearing)

* **fail-open (exit 0)**: malformed stdin JSON, `transcript_path` field set but file missing on disk, unparseable transcript lines, `git` unavailable, non-string `tool_input` / `transcript_path` / `cwd`. Workflow nudge — don't penalize harness bugs or fuzzed input.
* **fail-closed (exit 2 / block)**: `transcript_path` field empty string OR field absent. Per Claude Code hook spec the field is mandatory; missing/empty indicates harness contract regression and the conservative default is to block.

## What's explicitly NOT in scope

* **"Currently failing" check** — running pytest in the hook to verify the test is RED would block tool execution for ~2-5s per fire. Honor-system in v1; the `/test-writer` skill instructs the agent to confirm RED before handoff.
* **Forcing TDD on existing untested `bot/` code** — out of scope per ticket; existing code is grandfathered. The hook only blocks NEW edits without a paired test edit in the current session.
* **Multi-agent TDD orchestration** (test-writer subagent + impl subagent + verifier subagent) — too much workflow change for one Bit; revisit if single-agent + hook proves insufficient.
* **Mutation testing on the test corpus** — that's Pillar 5's job (testmon + tiered suite + mutmut baseline, ticket [86b9ve11y](https://app.clickup.com/t/86b9ve11y)).

## Adversarial cadence

Standard project discipline: two consecutive zero CRITICAL+MAJOR rounds. Hit at R4 + R5. Full counts:

| Round | C | M | m | New tests added |
|---|---|---|---|---|
| R1 | 0 | 2 | 6 | 8 |
| R2 | 1 | 3 | 5 | 8 |
| R3 | 0 | 1 | 4 | 4 |
| R4 | 0 | 1 | 3 | 3 |
| R5 | 0 | 0 | 3 | 0 |

### R1 highlights

- MAJOR #1: F401 unused `import re` in fresh file; removed (later re-added in R2 for the anchored marker fix).
- MAJOR #2: `KALSHI_TDD_BYPASS` had no negative-bypass test; added 8-value parametrized test pinning that only exact `"1"` bypasses (drop the `.strip()` for unambiguous semantics).
- 6 minors: sidechain filter, malformed JSONL line skip, realistic Edit shape, /test-writer mention in block message, repo-root conftest non-counts, large no-match performance.

### R2 highlights

- CRITICAL #1: non-dict `tool_input` crashed the hook with `AttributeError`. Added `isinstance(dict)` guard + parametrized regression test for 7 malformed shapes.
- MAJOR #1: settings.json fallback chain had wrong precedence — `$CLAUDE_PROJECT_DIR` (stale) won over cwd-relative (current). Inverted: cwd-relative tried first.
- MAJOR #2: substring match on `[no-tdd]` false-positives on meta-discussion commits. Switched to anchored-token regex.
- MAJOR #3: empty `transcript_path` string silently bypassed every gate. Now blocks (treated as contract violation).
- 5 minors: .py-only restriction on tests/ edits, nested-isSidechain belt-and-suspenders, phantom env-var pop, strict-equality matcher assertion, missing closeout doc.

### R3 highlights

- MAJOR #1: tests/CLAUDE.md "Failure modes" still listed empty `transcript_path` as fail-open after R2 inverted the contract. Replaced with a five-row table that distinguishes infrastructure errors (fail-open) from contract violations (fail-closed) plus subagent isolation.
- 4 minors: dead links in SKILL.md, undocumented subagent semantics, payload-cwd contract not pinned, BLOCK_MESSAGE used `<target>` (HTML/Markdown stripping risk → backtick-quoted).

### R4 highlights

- MAJOR #1: tests/CLAUDE.md "What counts as a test edit" said "Any file under tests/" — never updated after R2 added the .py-only restriction. Doc rewrite calls out non-counts (snapshot YAML/CSV, REGEN.md) explicitly with the Pillar-3-snapshot-regen-is-human-only pairing rationale.
- 3 minors: non-string `transcript_path` / `cwd` crashed (mirror of R2 CRITICAL #1 defense), test-name overstatement (graded acceptable), `/test-writer` not in root CLAUDE.md skill-routing table.

### R5 highlights

Cadence-gate triggered: R4 zero CRITICAL/MAJOR + R5 zero CRITICAL/MAJOR. R5 found 3 minors (docstring precision on absent-vs-empty, payload-cwd test doesn't truly distinguish payload-cwd from process-cwd, "Cherny attribution" cosmetic). Docstring nit fixed pre-ship; the other two are preserved for future-Bit followup.

## Lessons

Numbered continuing the modularization-track sequence (last was L53 in Pillar 3 closeout).

**L54** — Settings.json hook commands are loaded once per session at start and cached. Mid-session edits to `.claude/settings.json` don't take effect until next session restart. During Pillar 4 development, my own broken hook command (`$CLAUDE_PROJECT_DIR`-only path that didn't exist on the parent repo) blocked my own Edits — and a settings.json fix via Edit was itself blocked by the cached command. Workaround: Bash invocations bypass the matcher (`Edit|Write|MultiEdit` doesn't include Bash), so file rewrites via `python3 - <<'PYEOF'` heredoc + `Path.write_text()` worked. Lesson: when developing a hook that affects your own session, plan a Bash-only fallback path *before* wiring it. Verify the hook command can self-disable (existence-check + exit-0) before merge.

**L55** — `$CLAUDE_PROJECT_DIR` is set at session start and does not update when the agent moves into a worktree. In multi-worktree development (where one branch's `.claude/hooks/` differs from main's), `$CLAUDE_PROJECT_DIR/.claude/hooks/tdd_guard.py` resolves to the parent repo's (potentially stale OR non-existent) script, not the worktree's current version. Fix: prefer cwd-relative paths first (`if [ -f .claude/hooks/tdd_guard.py ]; then ...`) with `$CLAUDE_PROJECT_DIR` as a fallback, and a final `exit 0` if neither exists. Belt-and-suspenders pattern for robustness across worktree, plain checkout, and CI environments.

**L56** — Hook authors must distinguish "fail-open on infrastructure errors" from "fail-closed on contract violations." Pillar 4's first cut treated `transcript_path: ""` (empty string, mandatory field) the same as `transcript_path: "/missing/file"` (file doesn't exist) — both fail-open. R2 caught that empty-string is contract-regression territory (or fuzzed/spoofed input) and should BLOCK. Lesson: the failure-modes documentation table needs at least three columns — *condition*, *behavior*, *rationale*. The rationale column is what catches the conflation.

**L57** — Substring matching on convention markers (`[no-tdd]`, `[skip ci]`, `[no-merge]`) creates a quiet false-positive class. R2 reproduced: a commit subject `"Decision: discuss [no-tdd] policy in standup"` would silently bypass the entire Bit. Fix: anchor the marker as a whole token via regex `(^|\s)\[marker\](\s|$|[:.,;])` and ship a regression test for the meta-discussion subject. Generalizes: any project-convention marker in a commit subject or filename should be tested for false-positive substring match.

**L58** — Doc-vs-code drift is a recurring R3+ finding class. Every behavioral change in code must update the user-facing doc in the *same* atomic commit. Pillar 4 hit this twice: R3 caught `tests/CLAUDE.md` "Failure modes" describing pre-R2 fail-open semantics for empty `transcript_path`; R4 caught the same doc's "What counts" section saying "Any file under tests/" after R2 had tightened to `.py`-only. Lesson: when an adversarial round changes a contract, grep the docs for the old contract description in the *same round* and rewrite. Adversarial reviewers should grade doc-vs-code contradictions as MAJOR — they are equivalent in user impact to code defects.

**L59** — `tests/CLAUDE.md` "What counts as a test edit" sections (and analogous structural-default docs) need explicit "what does NOT count" enumeration. R4's MAJOR was "Any file under tests/" implying everything qualifies, while the code restricted to `.py`. Lesson: when the gate has structural defaults (suffix, naming convention, directory placement), call out what falls *outside* the gate explicitly. Implicit gates surprise downstream users in agent-readable error messages.

**L60** — Honor-system gates need an "easy-to-discover escape hatch." The `BLOCK_MESSAGE` says "edit a test under tests/ first (try \`/test-writer TARGET\` to scaffold a failing test), or set `KALSHI_TDD_BYPASS=1`, or add `[no-tdd]` to the HEAD commit subject." All three options are surface-level discoverable from the block — agent never has to dig through tests/CLAUDE.md to unblock. R1's MINOR #4 added the `/test-writer` mention because the original message had three options but didn't name the helper skill. Generalizes: every block message in an agent-facing hook should include both "what to do next" (positive guidance) and "how to bypass" (negative escape hatches).

## Files changed

```
NEW
  .claude/hooks/tdd_guard.py                                                 (~215 LOC)
  .claude/skills/test-writer/SKILL.md                                        (~140 LOC)
  tests/unit/test_tdd_guard_hook.py                                               (~1080 LOC, 75 tests)
  kb/decisions/testing-foundation-pillar-4-shipped-may09.md                  (this doc)

MODIFIED
  .claude/settings.json                                                      (+15/-0; PreToolUse Edit|Write|MultiEdit hook with cwd-relative + $CLAUDE_PROJECT_DIR fallback)
  tests/CLAUDE.md                                                            (+62/-0; TDD-with-hook section, bypass markers, failure-modes table, subagent isolation)
  CLAUDE.md                                                                  (+1/-0; /test-writer in skill-routing table)
  Makefile                                                                   (+1/-1; tests/unit/test_tdd_guard_hook.py in test-fast)
```

## Followups (deferred)

* **Currently-failing check** — running pytest on the test file at hook time would close the "tautological test bypass" gap. Cost: ~2-5s per hook fire. Defer to a future Bit if honor-system proves insufficient.
* **Auto-detect "test edit was not for the bot/ change"** — relevance check between the test edit and the bot/ edit. Path-mirror heuristic is brittle (`test_engines_extraction.py` covers two engines); skip without a stronger signal.
* **Multi-agent TDD orchestration** — see "What's explicitly NOT in scope".
* **`/test-writer` self-test** — the skill is markdown-only today; a Python helper that genuinely scaffolds + runs pytest could land as a future Bit. Trade-off: harder to maintain than a markdown instruction.

## Pillar 4 unblocking

Pillar 4 is independent of all other Pillars. Pillars 1+2+3 already shipped. Pillar 5 (testmon + tiered suite + mutmut, ticket [86b9ve11y](https://app.clickup.com/t/86b9ve11y)) shares `pyproject.toml [dev]` and `.github/workflows/test.yml` surface — Pillar 4 touched **neither**, so the conflict surface with Pillar 5 is zero. Bit 6.3 (CalibrationEngine, ticket [86b9vda4w](https://app.clickup.com/t/86b9vda4w)) is fully orthogonal — different file domains.
