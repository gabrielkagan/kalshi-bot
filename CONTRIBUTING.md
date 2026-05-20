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
(see Makefile + tests/unit/test_makefile.py for the canonical order).

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
  `bot/infra/`, `bot/shadows/`, `bot/models.py`, `bot/config.py`,
  `migrations/` (top-level, Sprint 10.6). `bot/_impl.py` was DELETED in
  Bit 9.3-iii.c (2026-05-11) — Sprint 9 main modularization is CLOSED.
  New code goes in canonical submodules; `bot/runtime_config.py`
  (PEP 562 dual-probe of `bot.constants` → `bot.config`) is the
  runtime-config view for bot/snapshots/dashboard_snapshot.py +
  bot/snapshots/supabase_sync.py (Sprint 10.4, 2026-05-12; Bit 12.1
  2026-05-12 relocated `config.py` from repo root → `bot/config.py`).
- **`collector/__main__.py` is the entrypoint shim for the Data Corpus
  collector** — a top-level Python package SIBLING to `bot/`
  (D1.1 SHIPPED 2026-05-16, ticket `86b9ypn49`; D1.1.5 SHIPPED 2026-05-16,
  ticket `86b9zdhz2`). Same sacred-boundary discipline as `bot/__main__.py`:
  no business logic in the shim, body lives in `collector/<module>.py`
  (`main_loop.py`, `ws_connection.py`, `rest_snapshot.py`, `writer.py`,
  `uploader.py`, `subscription_manager.py`). `collector/auth.py` was DELETED
  at D1.1.5 — auth flows through `kalshi_wire.auth` instead. Structural
  bot-isolation contract: `collector/` has ZERO `bot.*` imports, enforced
  by the `[importlinter:contract:collector-no-bot]` forbidden contract +
  AST defense-in-depth in `tests/contracts/test_collector_no_bot_imports.py`.
  See `kb/decisions/data-corpus-architecture.md` for the bronze/silver/gold
  architecture. D1.1 shipped scaffolding; D1.1.5 wired
  `collector/ws_connection.py`'s `BronzeArchiver` to
  `kalshi_wire.ws_client.WSClient`; **D1.2 SHIPPED 2026-05-16
  (ticket `86b9ypn66`)**: `collector/writer.py` + `collector/uploader.py`
  + `collector/main_loop.py::run()` body + `BronzeArchiver.run()` body
  all wired (the bronze data plumbing — WS frame → JSONL.zst → S3 via
  rclone). **D1.3 SHIPPED 2026-05-16 (ticket `86b9ypn72`)**:
  `collector/subscription_manager.py` body (tier-aware per-conn ticker
  assignment + subscribe-frame batching at the Kalshi WS message-size
  cap) + `BronzeArchiver.on_session_start` callback (dispatches the
  pre-built subscribe frames + binds sid→channel from `type=subscribed`/
  `type=ok` acks) + `main_loop` multi-conn fan-out (one BronzeArchiver
  per conn, channel-aware `writers_by_channel` dispatch with `_unrouted`
  fallback). First-bronze-flow happens at D1.3, not D1.2 (R1-C3
  acceptance criterion deferred from D1.2 closed here). **D1.4 SHIPPED
  2026-05-16, ticket `86b9ypn8r`** — `collector/rest_snapshot.py` body
  (Kalshi REST `/markets?status=open` paginated fetch via
  `kalshi_wire.auth.make_rest_headers` + `RestSnapshotRefresher` hourly
  poll + `BronzeArchiver.update_subscriptions` / `request_reconnect`
  surface + `main_loop._replan_for_archivers` callback that rebuilds
  per-conn subscribe frames + force-reconnects each WS conn — STAGGERED
  in wall-clock time by `_RECONNECT_STAGGER_SECONDS`=20s per
  D1.3-fu4-oom-closure 2026-05-19 — when the REST catalog refresh
  detects a ticker-set change). Hourly REST is
  now the default ticker source; `COLLECTOR_TICKERS_FILE` retained as
  the offline/test boot seam. **D1.5 SHIPPED 2026-05-16, ticket
  `86b9ypna4`** (REQUIRES-APPROVAL discipline tier) — wrote
  `ops/kalshi-collector.service` (Nice=10,
  MemoryHigh=400M, MemoryMax=512M, MemorySwapMax=0, LimitNOFILE=4096,
  `Restart=on-failure`+`RestartSec=10s`, `EnvironmentFile=/home/botuser/.env.collector`,
  `ExecStart=/home/botuser/kalshi-bot-repo/collector-start.sh`;
  CPUAffinity=1 at ship — retired 2026-05-19 ticket `86ba12rv6`),
  extended `ops/install.sh` to a multi-unit installer (parallel-array
  form; per-unit env-file check distinguishes the bot's repo-rooted
  `.env` from the collector's home-rooted `.env.collector`), refreshed
  `collector-start.sh` body to source `/home/botuser/.env.collector`
  exclusively. Bronze day-zero = first-chunk-in-S3 timestamp after
  `systemctl start kalshi-collector`.
- **`kalshi_wire/` is the shared Kalshi WS transport library** — a
  top-level Python package SIBLING to both `bot/` and `collector/`
  (D1.1.5 SHIPPED 2026-05-16, ticket `86b9zdhz2`). Pure-transport leaf:
  ZERO `bot.*` imports AND ZERO `collector.*` imports (pinned by
  `[importlinter:contract:kalshi_wire-no-bot]` +
  `[importlinter:contract:kalshi_wire-no-collector]`). Consumed by
  `bot/feeds/kalshi.py` (KalshiFeed, via 4 sync callbacks:
  `on_session_start` / `on_frame` / `on_session_end` / `on_drain_tick`)
  AND `collector/ws_connection.py` (BronzeArchiver). The 2026-05-16
  AMENDMENT to `kb/decisions/data-corpus-architecture.md` §5 adopted
  the "two sides of the same coin" shared-transport shape after
  external-advisor feedback. WSClient owns: asyncio thread, WS
  connect/reconnect with exponential backoff, RSA-PSS handshake auth,
  silence watchdog (Apr-24 ordering invariant: `_last_msg_ts` set BEFORE
  invoking `on_frame`), thread-safe outgoing-frame queue, frame parse +
  seq-gap detect. Differential test
  `tests/equivalence/test_kalshi_wire_differential.py` pins byte-identical
  frame capture across the two consumers (Pillar 3 load-bearing).
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
| `test-integration` | <30s (Bit-9 alias for both shards) | Before PR |
| `test-integration-shard-0` | <30s (half the corpus, xdist + pytest-shard) | Auto-runs via `make test` (Bit-9) |
| `test-integration-shard-1` | <30s (other half) | Auto-runs via `make test` (Bit-9) |
| `test-integration-serial` | <20s (timing-sensitive @serial) | Before PR (auto-runs via `make test`) |
| `test-research` | varies | During Phase 0 falsification work (NOT deploy-blocking; see `tests/CLAUDE.md`) |

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
