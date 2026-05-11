# Contributing

Sprint 13 Bit 13.6 (2026-05-11). Single-author repo + AI-agent
collaborators; this doc captures the workflow conventions enforced by
the project's pre-commit gates, tests, and modularization plan.

For agent sessions: see `.claude/onboarding.md` for the agent-facing
view.

## Quickstart

```bash
make install           # editable install + dev extras
make install-hooks     # symlink parallel-session-coordination pre-commit hook
make pre-commit-checks # run the full local gate (~30s)
```

The `pre-commit-checks` target chains the 5 fastest gates:
`ast-check` → `lint` → `doc-drift` → `test-unit` → `test-contract`
(see Makefile + tests/test_makefile.py for the canonical order).

## Discipline

Per `CLAUDE.md` interaction rules + the modularization plan
(`kb/decisions/repo-modularization-plan-may05.md`):

1. **RCA before patching.** For any CRITICAL/MAJOR finding, identify
   the root cause before applying a fix. Never just patch the symptom.
2. **TDD-first on `bot/` edits.** Scaffold a failing test before
   modifying `bot/*.py`. The `.claude/hooks/tdd_guard.py` Pillar 4
   hook enforces this structurally — it blocks `Edit|Write|MultiEdit`
   on `bot/**/*.py` unless a `tests/` edit happened first in the
   session.
3. **Adversarial review to 2 consecutive zero CRITICAL/MAJOR rounds**
   before ship. Most Bits hit the gate on R2; complex ones (Sprint 9
   extractions, Bit 9.3.5) needed 5-8.
4. **HARD GATE before push.** Present the change summary; wait for
   explicit approval. The CLAUDE.md sacred rule "Don't deploy without
   explicit confirmation" is load-bearing — pushing to `main`
   auto-deploys to the VPS.
5. **Follow-ups go to ClickUp, not buried bullets.** Use `/ticket` to
   file. KB↔ClickUp linkage convention: ticket has a `Meta` section
   pointing to the KB file; KB file has the ticket ID in frontmatter.

## Code conventions

- **No async.** Synchronous + threading is the design for WS feeds.
- **SQLite single-writer**, JSONL append-only journals. Don't switch.
- **`bot/__main__.py` is the entrypoint shim — sacred boundary, no
  logic.** Logic lives in subpackages: `bot/main_loop.py`,
  `bot/scanner/`, `bot/executor.py`, `bot/settlement.py`,
  `bot/order_flow.py`, `bot/orphan_db_watchdog.py`, `bot/engines/`,
  `bot/feeds/`, `bot/fetchers/`, `bot/helpers/`, `bot/notifier.py`,
  `bot/logger.py`, `bot/state.py`, `bot/kalshi_client.py`,
  `bot/infra/`, `bot/shadows/`, `bot/models.py`, `migrations/`
  (top-level, Sprint 10.6). `bot/_impl.py` is a residual re-export
  shim (~582 LOC post-Bit-9.3-ii) scheduled for deletion in Bit
  9.3-iii; new code does NOT go there.
- **`scripts/cal_mlp/integration.py` is the single torch entry point.**
  Direct `import torch` / `import pandas` anywhere under `bot/` is
  blocked by `.importlinter` contracts (`bot-no-torch`, `bot-no-pandas`).
  numpy/scipy/torch C-extensions cache OpenBLAS thread count at load
  time — `bot._thread_env` must import BEFORE numerical libs.

## Tests

`make test` runs the full tiered suite in order, fail-fast:

| Tier | Budget | When |
|---|---|---|
| `test-unit` | <10s | Every save |
| `test-contract` | <5s budget / ~12s actual on Mac | Every `bot/` / `pyproject.toml` / `.importlinter` edit |
| `test-equivalence` | <30s | Every `bot/engines/` / `bot/constants.py` edit |
| `test-integration` | <2min | Before PR |

`make test-affected` is testmon-driven (re-runs only tests with
changed dependencies). Right tool for the tight inner loop.

## Deploying

Don't push to `main` without going through the full gate:

```bash
make pre-commit-checks   # local gate, <30s
# present change summary, wait for approval
git push origin HEAD:main  # triggers auto-deploy via .github/workflows/deploy.yml
```

After deploy, verify:
1. VPS pulled the new commit (`ssh botuser@45.55.181.30 'cd ~/kalshi-bot-repo && git log -1'`)
2. Service is active (`systemctl is-active kalshi-bot`)
3. Expected DB rows are being written (the load-bearing log signature
   for that Bit fires — e.g., `[CALMLP_PARITY] 18 constants verified`)

## Skills

Operator skills live under `.claude/skills/<name>/SKILL.md`. New skill:

```bash
cp .claude/templates/new-skill.md .claude/skills/<new-name>/SKILL.md
# replace <PLACEHOLDER> tokens, delete inapplicable sections
```

See `.claude/templates/new-skill.md` (Bit 13.2) for the canonical
structure. Existing skills retargeted to `make X` wrappers per
Bit 11.1a/c/d.

## KB conventions

- `kb/` and `kb-research/` are LOCAL-only by convention. Existing
  tracked entries are pre-rule legacy. New `kb/` files don't get
  `git add`-ed; raise a separate Bit to track if needed.
- See `kb/CLAUDE.md` + `kb/_meta/MAINTENANCE.md` for the full
  conventions.

## Anti-patterns

- Don't carve new `bot/<subpackage>/` outside the planned modularization
  track (Sprints 4-13).
- Don't add async.
- Don't switch from SQLite or JSONL journals.
- Don't refactor for readability during a bug fix.
- Don't change Kelly fraction / blend weights / edge thresholds
  without backing data.
- Don't write tests unsolicited — regression tests after bug fixes only.

## Where things live

- `agent_docs/repository_map.md` — auto-generated module map (`make refresh-map`)
- `agent_docs/bot_layout.md` — hand-maintained file inventory
- `agent_docs/current_state.md` — live vs shadow vs disabled summary
- `agent_docs/config_reference.md` — every constant in `bot/constants.py`
- `agent_docs/db_schema.md` — `state.db` schema for all tables
- `agent_docs/calibration_pipeline.md` — cal_mlp + three-layer
- `kb/_index.md` — design decisions, postmortems, strategy specs
