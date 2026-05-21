# Agent Onboarding

Sprint 13 Bit 13.6 (2026-05-11). Agent-session-facing companion to
`CONTRIBUTING.md`. Read this first when entering a fresh session on
this repo.

## The 5-minute orientation

This is a single-author Kalshi crypto prediction-market trading bot.
Single-writer SQLite, synchronous + threading (no async), Python
3.9+. Auto-deploys to a $48/mo Digital Ocean VPS on `git push main`.

Live: 15m crypto markets on BTC/ETH/SOL/XRP. Shadow observation:
HYPE/DOGE (T1 from 2026-05-10). Hourly + weather + sports + SPX in
observation mode.

## First moves in a new session

1. **Read `CLAUDE.md`** (auto-loaded). It carries the sacred rules
   + skill-routing table + critical anti-patterns.
2. **Check `agent_docs/repository_map.md`** for the current bot/
   module structure. Auto-generated via `make refresh-map`
   (Bit 13.3).
3. **Check `kb/decisions/session-resume-*.md`** for the latest
   resume doc. Each major Sprint ship gets a resume doc; the
   "CURRENT RESUME DOC for <track>" header signals the live one.

## Skill routing (load-bearing)

When a user request fits a skill, prefer the skill over ad-hoc work.
Top-level routing table is in `CLAUDE.md` § "Skill routing". Don't
guess skill names — invoke only ones in the available-skills list
emitted by the system.

Common patterns:
- "How's it going?" → `/status`
- "Why is X losing?" → `/investigate`
- "Run the audit" → `/audit <system>` or `/alpha-audit`
- "Deploy" → `/deploy`
- Followup tracking → `/ticket` (always, never bury in bullets)

## Discipline (load-bearing)

Per `CLAUDE.md` + the modularization track:

- **RCA before patching** every CRITICAL/MAJOR finding.
- **TDD-first on `bot/` edits.** The `.claude/hooks/tdd_guard.py`
  PreToolUse hook blocks `Edit|Write|MultiEdit` on `bot/**/*.py`
  unless a `tests/` edit happened first in the session.
- **Adversarial review to 2 consecutive zero CRITICAL/MAJOR rounds**
  before ship. Dispatch `general-purpose` sub-agents for review;
  don't self-review.
- **HARD GATE before push.** Always present the change summary;
  wait for explicit user approval. Pushing to `main` auto-deploys.
- **Followups → ClickUp via `/ticket`.** Never bury in bullets or
  TODO comments.

## Pre-commit gate

Run `make pre-commit-checks` before any commit (chains 5 cheap-tier
gates per Bit 12.4 — ast-check + lint + doc-drift + test-unit +
test-contract, <30s wall-clock). The PSC P5.3 git pre-commit hook
covers parallel-session-coordination; the broader gate is
operator-invoked.

## Critical no-no's

- Don't import `torch` / `pandas` directly under `bot/*.py`.
  `.importlinter` contracts (`bot-no-torch`, `bot-no-pandas`, Bit
  12.3) enforce this. Use `scripts/cal_mlp/integration.py` as the
  single entry point (it constrains threads at module-import time).
- Don't recreate `bot/_impl.py`. The file was DELETED in Bit 9.3-iii.c
  (2026-05-11) — Sprint 9 main modularization is CLOSED. Logic lives
  in canonical submodules (`bot/main_loop.py`, `bot/scanner/__init__.py`,
  `bot/executor.py`, `bot/settlement.py`, `bot/state.py`, `bot/order_flow.py`,
  `bot/orphan_db_watchdog.py`, `bot/boot.py`, `bot/runtime_config.py`,
  `bot/engines/`, `bot/feeds/`, `bot/fetchers/`, `bot/helpers/`, etc.).
- Don't add async.
- Don't switch from SQLite / JSONL journals.
- Don't write tests unsolicited (regression tests after bug fixes
  only).

## File locations

- `bot/` — production code (sub-packaged per Sprint 4-9
  modularization)
- `scripts/` — operator audit + alpha-research + ops scripts
- `tests/` — pytest suite (tiered: unit / contract / equivalence /
  integration / research per Pillar 5)
- `tests/contracts/public_api.json` — Pillar 1 public-surface
  snapshot (regen via `make api-snapshot-regen`)
- `agent_docs/` — durable agent-facing docs (tracked in git)
- `kb/`, `kb-research/` — local-only knowledge base (NOT
  `git add`-ed by convention)
- `.claude/` — agent toolchain (skills, hooks, templates,
  references)
- `.claude/skills/<name>/SKILL.md` — operator skills
- `.claude/skills/references/{db-sync,preflight}.md` — shared
  reference checklists
- `.claude/templates/new-skill.md` — scaffold for new skills (Bit
  13.2)
- `.claude/hooks/tdd_guard.py` — Pillar 4 TDD enforcement
- `ops/` — systemd unit, start.sh, VPS deploy artifacts

## Common workflows

- **Audit a system:** `/audit 15m` or `make 15m-audit` (operator
  shortcut from Bit 11.3)
- **Deep-dive:** `/15m-alpha` or `make 15m-alpha`
- **Health check:** `/status` or `make data-health`
- **Investigate a loss:** `/investigate <ticker-or-event>`
- **Ship a Bit:** RCA → TDD scaffold → implement → 2-zero gate →
  HARD GATE → `git push main`

## Memory + persistence

You maintain a per-project memory at
`/Users/gabrielkagan/.claude/projects/-Users-gabrielkagan-Documents-kalshi-bot/memory/`.
`MEMORY.md` is the index (auto-loaded). Write new memories as you
learn user preferences, project context, or non-obvious facts.
Don't write code patterns / git history / current task state —
those go in plans, tasks, or git itself.

See `kb/decisions/session-resume-*.md` for the cross-session
continuity convention (each major Sprint ship gets a resume doc;
new sessions read the "CURRENT RESUME DOC for <track>" entry).
